"""FACE_MISSING regressions through worker packets and the desktop controller.

All frame times are supplied explicitly. No webcam, real keyboard hook, model
inference, or sleeping is used; the real event/risk/evidence path is retained.
"""

from dataclasses import replace
import contextlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_monitoring_reliability as reliability
from test_desktop import APP, FakeDiscovery, FakeSecurity, FakeWorker, READY, usable_frame

from evidence import EvidenceConfig, EvidenceManager
from monitoring import EventEngineConfig, EventType
from session_storage import SessionStorageConfig
from ui.main_window import MainWindow
from ui.session import SessionConfig
from ui.vision_worker import VisionWorker
from vision.face_tracker import DEFAULT_CONFIG, FaceResult, FaceStatus, FaceTracker
from vision.yolo_detector import Detection


class FaceMissingDesktopChecks(unittest.TestCase):
    # Reuse only the helper, not its TestCase subclass: discovery must not run
    # the existing reliability tests twice.
    monitor = reliability.ReliabilityWorkerChecks.monitor

    def monitor_tracker(self, times, landmarker_results):
        """Inject a real FaceTracker with a fake MediaPipe result source."""
        import cv2

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = SessionConfig(
            evidence=EvidenceConfig(directory=Path(directory.name) / "evidence"),
            storage=SessionStorageConfig(directory=Path(directory.name) / "sessions"))
        tracker = FaceTracker.__new__(FaceTracker)
        tracker.config = DEFAULT_CONFIG
        tracker.error = None if landmarker_results is not None else "Local model not loaded"
        tracker._last_timestamp_ms = -1
        tracker._cv2 = cv2
        tracker._mp = SimpleNamespace(Image=lambda **kwargs: kwargs,
                                     ImageFormat=SimpleNamespace(SRGB="SRGB"))
        landmarker = Mock()
        tracker._landmarker = landmarker if landmarker_results is not None else None
        landmarker.detect_for_video.side_effect = landmarker_results
        self.addCleanup(tracker.close)
        capture, detector, security = Mock(), Mock(), FakeSecurity()
        state = {"index": -1, "now": times[0]}
        self.updates, self.snapshots = [], []

        def read():
            if state["index"] + 1 >= len(times):
                self.worker.request_stop()
            return True, usable_frame()

        def detect(frame):
            state["index"] += 1
            state["now"] = times[state["index"]]
            return []

        def encode(frame, quality):
            self.snapshots.append(frame.copy())
            return b"fake JPEG"

        capture.read.side_effect = read
        detector.detect.side_effect = detect
        self.worker = VisionWorker(config, clock=lambda: state["now"],
            camera_opener=lambda cv2, index: (capture, usable_frame()),
            detector_factory=lambda *args: detector,
            face_factory=lambda *args: tracker, security_factory=lambda: security,
            evidence_factory=lambda options: EvidenceManager(options, encoder=encode))
        self.worker.update_ready.connect(lambda _: self.updates.append(self.worker.take_update()))
        diagnostics = io.StringIO()
        with patch("vision.face_tracker.perf_counter", side_effect=lambda: state["now"]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(diagnostics):
            self.worker.run()
        self.diagnostics = diagnostics.getvalue()
        self.assertIsNone(self.worker.result.error)
        capture.release.assert_called_once()
        self.assertFalse(tracker.available)
        self.assertTrue(security.stopped)
        if landmarker_results is not None:
            landmarker.close.assert_called_once()
        return self.worker.result, landmarker

    def desktop(self):
        window = MainWindow(self.worker.config, worker_factory=FakeWorker,
                            discovery_factory=FakeDiscovery,
                            clock=lambda: 0, auto_discover=False)
        window.setup_page.set_discovery([(0, "Camera 0")], READY)
        window.start_session("Student", "Exam", 0)
        adapter = window.worker
        adapter.session_started.emit(0)

        def close():
            window.exam_page.banner_timer.stop()
            window.finish_session()
            adapter.complete()
            window.close()
            window.deleteLater()
            APP.processEvents()

        self.addCleanup(close)
        self.assertEqual(window.state, "EXAM")
        return window, adapter

    def deliver(self, window, adapter, packet):
        adapter.pending = packet
        adapter.update_ready.emit(packet)
        self.assertIsNone(adapter.pending)
        return window.exam_page

    def page(self):
        window, adapter = self.desktop()
        for packet in self.updates:
            page = self.deliver(window, adapter, packet)
        return page

    def test_valid_face_with_no_yolo_person_does_not_show_absence_warning(self):
        result = self.monitor([0, 3, 30], faces=[reliability.PRESENT] * 3)
        self.assertEqual([packet.person_count for packet in self.updates], [0, 0, 0])
        self.assertFalse(any(packet.face_missing_warning for packet in self.updates))
        self.assertEqual(result.events, ())
        self.assertTrue(self.page().face_warning.isHidden())

    def test_no_face_below_threshold_remains_tracking_without_warning(self):
        result = self.monitor([0, 2.999], faces=[reliability.ABSENT] * 2)
        self.assertEqual(result.events, ())
        self.assertEqual(result.risk_score, 0)
        self.assertEqual(self.snapshots, [])
        self.assertFalse(any(packet.face_missing_warning for packet in self.updates))
        self.assertTrue(self.page().face_warning.isHidden())

    def test_threshold_emits_event_and_renders_persistent_desktop_warning(self):
        result = self.monitor([0, 2.999, 3], faces=[reliability.ABSENT] * 3)
        self.assertEqual([event.type for event in result.events], [EventType.FACE_MISSING])
        self.assertEqual(result.events[0].duration, 3)
        self.assertEqual(result.events[0].risk_delta, 15)
        self.assertTrue(Path(result.events[0].evidence_path).is_file())
        self.assertEqual([packet.face_missing_warning for packet in self.updates], [False, False, True])
        window, adapter = self.desktop()
        window.show()
        for packet in self.updates:
            page = self.deliver(window, adapter, packet)
        APP.processEvents()
        self.assertTrue(page.face_warning.isVisible())
        self.assertEqual(page.face_warning.text(), "WARNING\nSTUDENT LEFT CAMERA VIEW")
        self.assertEqual(page.values["Face"].text(), "Missing")
        self.assertEqual(page.timeline.count(), 1)
        self.assertEqual(page.risk_score.text(), "15 / 100")

    def test_timer_timeout_and_later_eventless_packets_do_not_hide_warning(self):
        self.monitor([0, 3, 6, 30], faces=[reliability.ABSENT] * 4)
        window, adapter = self.desktop()
        self.deliver(window, adapter, self.updates[0])
        page = self.deliver(window, adapter, self.updates[1])
        self.assertEqual(page.banner.text(), "FACE MISSING")
        page.banner_timer.timeout.emit()
        self.assertEqual(page.banner.text(), "LOCAL MONITORING ACTIVE")
        self.assertFalse(page.face_warning.isHidden())
        for packet in self.updates[2:]:
            self.assertEqual(packet.events, ())
            self.deliver(window, adapter, packet)
            self.assertFalse(page.face_warning.isHidden())

    def test_continuous_absence_for_thirty_seconds_keeps_one_event_and_image(self):
        result = self.monitor([0, 1, 3, 10, 13, 20, 30],
                              faces=[reliability.ABSENT] * 7)
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.event_counts[EventType.FACE_MISSING], 1)
        self.assertEqual(result.risk_score, 15)
        self.assertEqual(len(self.snapshots), 1)
        self.assertTrue(all(packet.face_missing_warning for packet in self.updates[2:]))
        self.assertFalse(self.page().face_warning.isHidden())

    def test_valid_single_face_immediately_clears_warning(self):
        self.monitor([0, 3, 3.001], faces=[reliability.ABSENT, reliability.ABSENT,
                                         reliability.PRESENT])
        window, adapter = self.desktop()
        self.deliver(window, adapter, self.updates[0])
        page = self.deliver(window, adapter, self.updates[1])
        self.assertFalse(page.face_warning.isHidden())
        self.deliver(window, adapter, self.updates[2])
        self.assertFalse(self.updates[2].face_missing_warning)
        self.assertTrue(page.face_warning.isHidden())
        self.assertEqual(page.values["Face"].text(), "Detected")

    def test_second_absence_requires_fresh_threshold_then_repeat_risk_and_evidence(self):
        result = self.monitor([0, 3, 4, 11, 13.999, 14],
            faces=[reliability.ABSENT, reliability.ABSENT, reliability.PRESENT,
                   reliability.ABSENT, reliability.ABSENT, reliability.ABSENT])
        self.assertEqual([packet.face_missing_warning for packet in self.updates],
                         [False, True, False, False, False, True])
        self.assertEqual([event.type for event in result.events],
                         [EventType.FACE_MISSING, EventType.FACE_MISSING])
        self.assertEqual([event.duration for event in result.events], [3, 3])
        self.assertEqual([event.risk_delta for event in result.events], [15, 8])
        self.assertEqual(result.risk_score, 23)
        self.assertEqual(len(self.snapshots), 2)
        self.assertNotEqual(result.events[0].evidence_path, result.events[1].evidence_path)
        self.assertFalse(self.page().face_warning.isHidden())

    def test_new_qualified_absence_warns_during_cooldown_without_early_penalty(self):
        result = self.monitor([0, 3, 4, 5, 8, 13, 30],
            faces=[reliability.ABSENT, reliability.ABSENT, reliability.PRESENT,
                   reliability.ABSENT, reliability.ABSENT, reliability.ABSENT,
                   reliability.ABSENT])
        self.assertTrue(self.updates[4].face_missing_warning)
        self.assertEqual(self.updates[4].events, ())
        self.assertEqual(self.updates[4].risk_score, 15)
        self.assertEqual([event.type for event in result.events],
                         [EventType.FACE_MISSING, EventType.FACE_MISSING])
        self.assertEqual(result.risk_score, 23)
        self.assertEqual(len(self.snapshots), 2)
        window, adapter = self.desktop()
        for packet in self.updates[:5]:
            page = self.deliver(window, adapter, packet)
        self.assertFalse(page.face_warning.isHidden())
        self.assertEqual(page.timeline.count(), 1)

    def test_unavailable_tracker_has_no_absence_warning_or_violation(self):
        result = self.monitor([0, 3, 30], faces=[reliability.UNAVAILABLE] * 3)
        self.assertEqual(result.events, ())
        self.assertEqual(result.risk_score, 0)
        self.assertEqual(self.snapshots, [])
        self.assertFalse(any(packet.face_missing_warning for packet in self.updates))
        page = self.page()
        self.assertTrue(page.face_warning.isHidden())
        self.assertEqual(page.values["Face"].text(), "Unavailable")

    def test_tracker_becoming_unavailable_clears_existing_absence_warning(self):
        self.monitor([0, 3, 3.001], faces=[reliability.ABSENT, reliability.ABSENT,
                                         reliability.UNAVAILABLE])
        self.assertFalse(self.updates[-1].face_missing_warning)
        self.assertTrue(self.page().face_warning.isHidden())

    def test_multiple_faces_emits_its_own_event_and_never_absence(self):
        multiple = FaceResult(FaceStatus.MULTIPLE_FACES, 2)
        result = self.monitor([0, 1, 3, 30], faces=[multiple] * 4)
        self.assertEqual([event.type for event in result.events], [EventType.MULTIPLE_FACES])
        self.assertFalse(any(packet.face_missing_warning for packet in self.updates))
        self.assertTrue(self.page().face_warning.isHidden())

    def test_authorized_break_suppresses_absence_warning_risk_and_evidence(self):
        result = self.monitor([0, 3, 30], faces=[reliability.ABSENT] * 3,
                              commands={0: [("start", reliability.PIN, 60)]})
        self.assertEqual([event.type for event in result.events],
                         [EventType.AUTHORIZED_BREAK_STARTED])
        self.assertEqual(result.risk_score, 0)
        self.assertEqual(self.snapshots, [])
        self.assertTrue(all(packet.break_active for packet in self.updates))
        self.assertFalse(any(packet.face_missing_warning for packet in self.updates))
        page = self.page()
        self.assertTrue(page.face_warning.isHidden())
        self.assertFalse(page.break_banner.isHidden())

    def test_break_clears_warning_and_ending_break_restarts_absence_threshold(self):
        result = self.monitor([0, 3, 4, 11, 13.999, 14],
            faces=[reliability.ABSENT] * 6,
            commands={2: [("start", reliability.PIN, 60)], 3: [("end",)]})
        self.assertEqual([packet.face_missing_warning for packet in self.updates],
                         [False, True, False, False, False, True])
        missing = [event for event in result.events if event.type == EventType.FACE_MISSING]
        self.assertEqual([event.duration for event in missing], [3, 3])
        self.assertEqual([event.risk_delta for event in missing], [15, 8])
        window, adapter = self.desktop()
        for packet in self.updates[:3]:
            page = self.deliver(window, adapter, packet)
        self.assertTrue(page.face_warning.isHidden())
        self.assertFalse(page.break_banner.isHidden())
        for packet in self.updates[3:]:
            self.deliver(window, adapter, packet)
        self.assertFalse(page.face_warning.isHidden())
        self.assertTrue(page.break_banner.isHidden())

    def test_confirmed_obstruction_has_visual_and_event_precedence_over_absence(self):
        result = self.monitor([0, 1.999, 2, 3, 30],
                              frames=[reliability.BLACK] * 5,
                              faces=[reliability.ABSENT] * 5)
        self.assertEqual([event.type for event in result.events], [EventType.CAMERA_OBSTRUCTED])
        self.assertFalse(any(packet.face_missing_warning for packet in self.updates))
        self.assertTrue(all(packet.camera_obstructed_warning for packet in self.updates[2:]))
        page = self.page()
        page.banner_timer.timeout.emit()
        self.assertFalse(page.obstruction_warning.isHidden())
        self.assertTrue(page.face_warning.isHidden())

    def test_usable_frame_after_confirmed_cover_starts_fresh_absence_duration(self):
        result = self.monitor([0, 1, 3, 4, 6.999, 7],
            frames=[usable_frame(), reliability.BLACK, reliability.BLACK,
                    usable_frame(), usable_frame(), usable_frame()],
            faces=[reliability.ABSENT] * 6)
        self.assertEqual([event.type for event in result.events],
                         [EventType.CAMERA_OBSTRUCTED, EventType.FACE_MISSING])
        self.assertEqual(result.events[-1].duration, 3)
        self.assertEqual([packet.face_missing_warning for packet in self.updates],
                         [False, False, False, False, False, True])
        self.assertFalse(self.updates[-1].camera_obstructed_warning)
        page = self.page()
        self.assertFalse(page.face_warning.isHidden())
        self.assertTrue(page.obstruction_warning.isHidden())

    def test_intermittent_dark_candidates_do_not_reset_continuous_face_absence(self):
        result = self.monitor([0, 1, 1.5, 2, 2.5, 3, 30],
            frames=[usable_frame(), reliability.BLACK, usable_frame(),
                    reliability.BLACK, usable_frame(), usable_frame(), usable_frame()],
            faces=[reliability.ABSENT] * 7)
        self.assertEqual([event.type for event in result.events], [EventType.FACE_MISSING])
        self.assertEqual(result.events[0].duration, 3)
        self.assertFalse(any(packet.camera_obstructed_warning for packet in self.updates))
        self.assertTrue(self.updates[5].face_missing_warning)
        self.assertTrue(self.updates[6].face_missing_warning)
        self.assertEqual(result.risk_score, 15)
        self.assertEqual(len(self.snapshots), 1)
        self.assertFalse(self.page().face_warning.isHidden())

    def test_yolo_person_does_not_override_no_face_absence_signal(self):
        person = Detection(0, 0.95, (5, 3, 50, 46))
        result = self.monitor([0, 3], faces=[reliability.ABSENT] * 2,
                              detections=[person])
        self.assertEqual([packet.person_count for packet in self.updates], [1, 1])
        self.assertEqual([event.type for event in result.events], [EventType.FACE_MISSING])
        self.assertFalse(self.page().face_warning.isHidden())

    def test_unconfirmed_quality_sample_does_not_clear_existing_absence_warning(self):
        result = self.monitor([0, 3, 3.5, 4, 6],
            frames=[usable_frame(), usable_frame(), reliability.BLACK,
                    usable_frame(), usable_frame()],
            faces=[reliability.ABSENT] * 5)
        self.assertEqual([packet.face_missing_warning for packet in self.updates],
                         [False, True, True, True, True])
        self.assertFalse(any(packet.camera_obstructed_warning for packet in self.updates))
        self.assertEqual([event.type for event in result.events], [EventType.FACE_MISSING])
        self.assertEqual(result.risk_score, 15)
        self.assertEqual(len(self.snapshots), 1)
        self.assertFalse(self.page().face_warning.isHidden())

    def test_confirmed_cover_in_cooldown_suppresses_absence_without_repeat_event(self):
        result = self.monitor([0, 2, 3, 4, 6],
            frames=[reliability.BLACK, reliability.BLACK, usable_frame(),
                    reliability.BLACK, reliability.BLACK],
            faces=[reliability.ABSENT] * 5)
        self.assertEqual([event.type for event in result.events], [EventType.CAMERA_OBSTRUCTED])
        self.assertTrue(self.updates[-1].camera_obstructed_warning)
        self.assertFalse(self.updates[-1].face_missing_warning)
        self.assertEqual(self.updates[-1].events, ())
        self.assertEqual(result.risk_score, 20)
        self.assertEqual(len(self.snapshots), 1)
        page = self.page()
        self.assertFalse(page.obstruction_warning.isHidden())
        self.assertTrue(page.face_warning.isHidden())

    def test_worker_and_desktop_use_configured_absence_threshold(self):
        config = EventEngineConfig()
        rules = dict(config.rules)
        rules[EventType.FACE_MISSING] = replace(rules[EventType.FACE_MISSING],
                                              threshold_seconds=1.25)
        result = self.monitor([0, 1.249, 1.25], faces=[reliability.ABSENT] * 3,
                              config=SessionConfig(events=replace(config, rules=rules)))
        self.assertEqual([packet.face_missing_warning for packet in self.updates],
                         [False, False, True])
        self.assertEqual(result.events[0].duration, 1.25)
        self.assertFalse(self.page().face_warning.isHidden())

    def test_real_face_tracker_no_landmarks_result_reaches_worker_and_exam_page(self):
        # Bypass only model initialization. The production FaceTracker.process,
        # summarize_faces, worker, event/risk/evidence code and UI remain real.
        present = SimpleNamespace(face_landmarks=[[]],
                                  facial_transformation_matrixes=[])
        absent = SimpleNamespace(face_landmarks=[],
                                 facial_transformation_matrixes=[])
        result, landmarker = self.monitor_tracker([0, 1, 3.999, 4, 30, 30.001],
                                                  [present, absent, absent,
                                                   absent, absent, present])
        self.assertEqual(landmarker.detect_for_video.call_count, 6)
        self.assertEqual([packet.face.status for packet in self.updates],
            [FaceStatus.FACE_DETECTED, FaceStatus.NO_FACE, FaceStatus.NO_FACE,
             FaceStatus.NO_FACE, FaceStatus.NO_FACE, FaceStatus.FACE_DETECTED])
        self.assertEqual([packet.face_missing_warning for packet in self.updates],
                         [False, False, False, True, True, False])
        self.assertEqual([event.type for event in result.events], [EventType.FACE_MISSING])
        window, adapter = self.desktop()
        for packet in self.updates[:5]:
            page = self.deliver(window, adapter, packet)
        self.assertFalse(page.face_warning.isHidden())
        self.deliver(window, adapter, self.updates[-1])
        self.assertTrue(page.face_warning.isHidden())

    def test_real_tracker_failure_after_absence_clears_warning_as_technical_state(self):
        absent = SimpleNamespace(face_landmarks=[],
                                 facial_transformation_matrixes=[])
        result, landmarker = self.monitor_tracker([0, 3, 3.001, 30],
            [absent, absent, RuntimeError("Synthetic MediaPipe video failure")])
        self.assertEqual(landmarker.detect_for_video.call_count, 3)
        self.assertEqual([packet.face_missing_warning for packet in self.updates],
                         [False, True, False, False])
        self.assertEqual([event.type for event in result.events], [EventType.FACE_MISSING])
        self.assertEqual(result.risk_score, 15)
        self.assertEqual(len(self.snapshots), 1)
        self.assertEqual(self.updates[-1].face.status, FaceStatus.UNAVAILABLE)
        state, detail = self.updates[-1].statuses["Face Tracking"]
        self.assertEqual(state, "UNAVAILABLE")
        self.assertIn("Synthetic MediaPipe video failure", detail)
        page = self.page()
        self.assertTrue(page.face_warning.isHidden())
        self.assertEqual(page.values["Face"].text(), "Unavailable")
        self.assertIn("Synthetic MediaPipe video failure", page.values["Face"].toolTip())
        self.assertFalse(page.face_tracking_notice.isHidden())
        self.assertIn("Synthetic MediaPipe video failure", page.face_tracking_notice.text())
        self.assertIn("absence monitoring paused", page.face_tracking_notice.text())
        self.assertNotIn("STUDENT LEFT CAMERA VIEW", page.face_tracking_notice.text())
        self.assertEqual(self.diagnostics.count("Synthetic MediaPipe video failure"), 1)

    def test_real_tracker_unavailable_initially_never_qualifies_student_absence(self):
        result, landmarker = self.monitor_tracker([0, 3, 30], None)
        landmarker.detect_for_video.assert_not_called()
        self.assertEqual(result.events, ())
        self.assertEqual(result.risk_score, 0)
        self.assertEqual(self.snapshots, [])
        self.assertFalse(any(packet.face_missing_warning for packet in self.updates))
        self.assertEqual(self.updates[-1].statuses["Face Tracking"][0], "UNAVAILABLE")
        page = self.page()
        self.assertTrue(page.face_warning.isHidden())
        self.assertEqual(page.values["Face"].text(), "Unavailable")
        self.assertFalse(page.face_tracking_notice.isHidden())
        self.assertIn("Local model not loaded", page.face_tracking_notice.text())

    def test_technical_notice_hides_on_healthy_status_and_is_cleared_on_reset(self):
        self.monitor_tracker([0, 3], None)
        page = self.page()
        self.assertFalse(page.face_tracking_notice.isHidden())
        page.set_statuses({"Face Tracking": ("ACTIVE", "Tracker healthy")})
        self.assertTrue(page.face_tracking_notice.isHidden())
        page.set_statuses({"Face Tracking": ("ERROR", "Synthetic technical failure")})
        self.assertFalse(page.face_tracking_notice.isHidden())
        self.assertIn("Synthetic technical failure", page.face_tracking_notice.text())
        self.assertTrue(page.face_warning.isHidden())
        page.reset()
        self.assertTrue(page.face_tracking_notice.isHidden())
        self.assertEqual(page.face_tracking_notice.text(), "")
        self.assertTrue(page.face_warning.isHidden())


if __name__ == "__main__":
    unittest.main()
