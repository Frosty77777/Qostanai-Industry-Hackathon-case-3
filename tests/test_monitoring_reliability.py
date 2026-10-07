"""Reliability regressions with fake monotonic time, frames, cameras and keys."""

import contextlib
from dataclasses import replace
from datetime import datetime, timezone
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from test_desktop import APP, FakeDiscovery, FakeFace, FakeSecurity, FakeWorker, READY, event, update, usable_frame
from test_face_tracker import face_transform
from evidence import EvidenceConfig, EvidenceManager
from monitoring import EventEngine, EventEngineConfig, EventRule, EventType, FrameSignals, RiskEngine, Severity
from monitoring.break_manager import BreakConfig, BreakManager
from ui.exam_page import ExamPage
from ui.main_window import MainWindow
from ui.session import SessionConfig
from ui.vision_worker import VisionWorker
from vision.camera_quality import CameraQualityConfig, RecentUsableFrame, analyze_camera_frame
from vision.face_tracker import FaceResult, FaceStatus, HeadDirection, summarize_faces

ABSENT = FaceResult(FaceStatus.NO_FACE, 0)
PRESENT = FaceResult(FaceStatus.FACE_DETECTED, 1, HeadDirection.CENTER)
UNAVAILABLE = FaceResult(FaceStatus.UNAVAILABLE, None)
BLACK = np.zeros((48, 64, 3), dtype=np.uint8)
PIN = "9876"


class CameraQualityChecks(unittest.TestCase):
    def test_black_and_uniform_bright_covers_are_unusable(self):
        for brightness in (0, 8, 100, 255):
            with self.subTest(brightness=brightness):
                result = analyze_camera_frame(np.full_like(BLACK, brightness), CameraQualityConfig())
                self.assertTrue(result.obstructed)
                self.assertEqual(result.grayscale_variance, 0)

    def test_dark_textured_scene_is_usable(self):
        for offset, contrast in ((5, 40), (0, 22)):
            with self.subTest(offset=offset, contrast=contrast):
                gray = ((np.indices((120, 160)).sum(axis=0) // 10) % 2 * contrast + offset).astype(np.uint8)
                result = analyze_camera_frame(np.repeat(gray[:, :, None], 3, axis=2), CameraQualityConfig())
                self.assertLess(result.mean_brightness, 30)
                self.assertFalse(result.obstructed)

    def test_normal_frame_is_usable(self):
        self.assertFalse(analyze_camera_frame(usable_frame(), CameraQualityConfig()).obstructed)

    def test_quality_thresholds_are_configurable(self):
        image = usable_frame()
        result = analyze_camera_frame(image, replace(CameraQualityConfig(), uniform_max_variance=10000))
        self.assertTrue(result.obstructed)

    def test_invalid_quality_configuration_is_rejected(self):
        for kwargs in ({"sample_width": 0}, {"sample_height": 1.5}, {"low_brightness": 256},
                       {"low_brightness": float("nan")}, {"uniform_max_variance": -1},
                       {"dark_max_variance": float("inf")}, {"last_usable_max_age_seconds": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                CameraQualityConfig(**kwargs)

    def test_last_usable_frame_is_detached_and_not_replaced_by_cover(self):
        cache = RecentUsableFrame(CameraQualityConfig())
        frame = usable_frame()
        original = frame.copy()
        cache.remember(frame, analyze_camera_frame(frame, cache.config), 0)
        frame[:] = 0
        cache.remember(BLACK, analyze_camera_frame(BLACK, cache.config), 1)
        saved, source = cache.evidence_frame(BLACK, 3)
        np.testing.assert_array_equal(saved, original)
        self.assertEqual(source, {"evidence_frame_source": "last_usable", "last_usable_age_seconds": 3})

    def test_stale_or_missing_usable_frame_falls_back_to_current(self):
        cache = RecentUsableFrame(CameraQualityConfig(last_usable_max_age_seconds=1))
        saved, source = cache.evidence_frame(BLACK, 0)
        self.assertIs(saved, BLACK)
        self.assertEqual(source["evidence_frame_source"], "current")
        good = usable_frame()
        cache.remember(good, analyze_camera_frame(good, cache.config), 0)
        saved, source = cache.evidence_frame(BLACK, 2)
        self.assertIs(saved, BLACK)
        self.assertEqual(source["last_usable_age_seconds"], 2)

    def test_obstruction_has_persistence_and_continuous_episode_latch(self):
        engine = EventEngine()
        signals = FrameSignals(camera_obstructed=True)
        self.assertEqual(engine.update(signals, now=0), [])
        self.assertEqual(engine.update(signals, now=1.99), [])
        self.assertFalse(engine.condition_qualified(EventType.CAMERA_OBSTRUCTED))
        emitted = engine.update(signals, now=2)
        self.assertEqual([e.type for e in emitted], [EventType.CAMERA_OBSTRUCTED])
        self.assertEqual(emitted[0].severity, Severity.HIGH)
        self.assertTrue(engine.condition_qualified(EventType.CAMERA_OBSTRUCTED))
        self.assertEqual(engine.update(signals, now=50), [])

    def test_obstruction_repeat_respects_reset_and_cooldown(self):
        engine = EventEngine()
        blocked = FrameSignals(camera_obstructed=True)
        engine.update(blocked, now=0)
        engine.update(blocked, now=2)
        engine.update(FrameSignals(), now=3)
        engine.update(blocked, now=4)
        self.assertEqual(engine.update(blocked, now=6), [])
        self.assertTrue(engine.condition_qualified(EventType.CAMERA_OBSTRUCTED))
        self.assertEqual(len(engine.update(blocked, now=12)), 1)

    def test_obstruction_risk_first_repeat_and_cap(self):
        risk = RiskEngine()
        emitted = replace(event(EventType.CAMERA_OBSTRUCTED), risk_delta=None)
        self.assertEqual(risk.process(emitted).risk_delta, 20)
        self.assertEqual(risk.process(emitted).risk_delta, 10)
        for _ in range(20):
            risk.process(emitted)
        self.assertEqual(risk.current_score, 100)

    def test_obstruction_and_break_duration_events_are_distinct(self):
        engine = EventEngine()
        with self.assertRaises(ValueError):
            engine.record_session_events([event(EventType.CAMERA_OBSTRUCTED)])
        with self.assertRaises(ValueError):
            engine.record_security_events([event(EventType.AUTHORIZED_BREAK_STARTED)])
        engine.record_session_events([event(EventType.AUTHORIZED_BREAK_STARTED, 0)])
        self.assertEqual(len(engine.history), 1)


class BreakManagerChecks(unittest.TestCase):
    def setUp(self):
        self.manager = BreakManager(BreakConfig(teacher_pin=PIN))

    def test_valid_pin_starts_break_with_deadline_and_no_pin_metadata(self):
        emitted = self.manager.start(PIN, 60, now=10)
        self.assertEqual(emitted.type, EventType.AUTHORIZED_BREAK_STARTED)
        self.assertTrue(self.manager.active)
        self.assertEqual(self.manager.remaining_seconds(10), 60)
        self.assertEqual(self.manager.remaining_seconds(10.1), 60)
        self.assertEqual(self.manager.remaining_seconds(11), 59)
        self.assertNotIn(PIN, str(emitted.to_dict()))

    def test_invalid_pin_does_not_start_break(self):
        for pin in ("", "wrong", None, 9876):
            with self.subTest(pin=pin):
                self.assertIsNone(self.manager.start(pin, 60, now=0))
                self.assertFalse(self.manager.active)

    def test_only_configured_durations_are_allowed(self):
        self.assertIsNone(self.manager.start(PIN, 90, now=0))
        self.assertFalse(self.manager.active)

    def test_all_default_durations_can_be_authorized(self):
        for duration in (60, 180, 300):
            manager = BreakManager(BreakConfig(teacher_pin=PIN))
            self.assertIsNotNone(manager.start(PIN, duration, now=0))
            self.assertEqual(manager.remaining_seconds(0), duration)

    def test_active_break_cannot_be_extended_by_another_start(self):
        self.manager.start(PIN, 60, now=0)
        self.assertIsNone(self.manager.start(PIN, 300, now=10))
        self.assertEqual(self.manager.remaining_seconds(10), 50)

    def test_expiry_occurs_at_deadline_exactly_once(self):
        self.manager.start(PIN, 60, now=0)
        self.assertIsNone(self.manager.expire(now=59.99))
        emitted = self.manager.expire(now=60)
        self.assertEqual(emitted.type, EventType.AUTHORIZED_BREAK_EXPIRED)
        self.assertEqual(emitted.duration, 60)
        self.assertFalse(self.manager.active)
        self.assertIsNone(self.manager.expire(now=61))

    def test_early_end_occurs_once(self):
        self.manager.start(PIN, 60, now=0)
        emitted = self.manager.end(now=12)
        self.assertEqual(emitted.type, EventType.AUTHORIZED_BREAK_ENDED)
        self.assertEqual(emitted.duration, 12)
        self.assertFalse(self.manager.active)
        self.assertIsNone(self.manager.end(now=13))

    def test_end_at_deadline_is_expiry(self):
        self.manager.start(PIN, 60, now=0)
        self.assertEqual(self.manager.end(now=60).type, EventType.AUTHORIZED_BREAK_EXPIRED)

    def test_break_events_have_zero_risk_and_no_evidence(self):
        risk = RiskEngine()
        manager = EvidenceManager(encoder=Mock(side_effect=AssertionError("No break evidence")))
        events = [self.manager.start(PIN, 60, now=0), self.manager.end(now=10)]
        self.manager.start(PIN, 60, now=20)
        events.append(self.manager.expire(now=80))
        for emitted in events:
            scored = risk.process(emitted)
            self.assertEqual(scored.risk_delta, 0)
            self.assertFalse(manager.should_capture(scored))
            self.assertIsNone(manager.capture(scored, BLACK).evidence_path)
        self.assertEqual(risk.current_score, 0)

    def test_teacher_pin_from_environment_and_config_repr_hides_secret(self):
        with patch.dict("os.environ", {"AI_EXAM_TEACHER_PIN": "teacher-secret"}):
            config = BreakConfig()
        self.assertTrue(config.accepts_pin("teacher-secret"))
        self.assertFalse(config.accepts_pin("1234"))
        self.assertNotIn("teacher-secret", repr(config))

    def test_invalid_configuration_is_rejected(self):
        for kwargs in ({"teacher_pin": ""}, {"teacher_pin": None}, {"durations_seconds": ()},
                       {"durations_seconds": (0,)}, {"durations_seconds": (1.5,)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                BreakConfig(**kwargs)

    def test_backwards_or_invalid_time_does_not_mutate_active_break(self):
        self.manager.start(PIN, 60, now=10)
        for now in (9, float("nan"), float("inf")):
            with self.subTest(now=now), self.assertRaises(ValueError):
                self.manager.expire(now=now)
        self.assertEqual(self.manager.remaining_seconds(10), 60)


class ReliabilityWorkerChecks(unittest.TestCase):
    def monitor(self, times, *, frames=None, faces=None, commands=None, security_events=None,
                config=None, encoder=None, detections=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = config or SessionConfig(breaks=BreakConfig(teacher_pin=PIN))
        config = replace(config, evidence=EvidenceConfig(directory=Path(directory.name)))
        frames = frames or [usable_frame() for _ in times]
        faces = faces or [PRESENT for _ in times]
        state = {"index": -1, "now": times[0]}
        capture, detector, tracker, security = Mock(), Mock(), FakeFace(), FakeSecurity()
        tracker.process = lambda frame: faces[state["index"]]
        self.snapshots, self.updates = [], []

        def encode(frame, quality):
            self.snapshots.append(frame.copy())
            if encoder is not None:
                return encoder(frame, quality)
            return b"fake JPEG"

        def read():
            index = state["index"] + 1
            if index >= len(times):
                self.worker.request_stop()
                return True, frames[-1].copy()
            return True, frames[index].copy()

        def detect(frame):
            state["index"] += 1
            index = state["index"]
            state["now"] = times[index]
            for command in (commands or {}).get(index, ()):
                if command[0] == "start":
                    self.worker.request_authorized_break(command[1], command[2])
                else:
                    self.worker.request_end_break()
            security.events = (security_events or {}).get(index, [])
            return detections or []

        capture.read.side_effect = read
        detector.detect.side_effect = detect
        self.worker = VisionWorker(config, clock=lambda: state["now"],
            camera_opener=lambda cv2, index: (capture, frames[0].copy()),
            detector_factory=lambda *args: detector, face_factory=lambda *args: tracker,
            security_factory=lambda: security,
            evidence_factory=lambda options: EvidenceManager(options, encoder=encode))
        self.worker.update_ready.connect(lambda _: self.updates.append(self.worker.take_update()))
        self.notices = []
        self.worker.notice.connect(self.notices.append)
        with contextlib.redirect_stdout(io.StringIO()):
            self.worker.run()
        self.assertIsNone(self.worker.result.error)
        capture.release.assert_called_once()
        self.assertTrue(tracker.closed)
        self.assertTrue(security.stopped)
        return self.worker.result

    def page(self):
        page = ExamPage()
        page.start("Student", "Exam")
        self.addCleanup(page.deleteLater)
        self.addCleanup(page.banner_timer.stop)
        for packet in self.updates:
            page.apply_update(packet)
        return page

    def test_all_head_directions_reach_worker_and_ui_from_pose_matrices(self):
        cases = [(0, 0, "CENTER"), (30, 0, "LEFT"), (-30, 0, "RIGHT"),
                 (0, -25, "UP"), (0, 25, "DOWN"), (80, 0, "UNKNOWN")]
        faces = [summarize_faces(SimpleNamespace(face_landmarks=[[]],
                 facial_transformation_matrixes=[face_transform(yaw, pitch)])) for yaw, pitch, _ in cases]
        result = self.monitor(list(range(len(cases))), faces=faces)
        page = ExamPage()
        self.addCleanup(page.deleteLater)
        for packet, (_, _, expected) in zip(self.updates, cases):
            with self.subTest(direction=expected):
                page.apply_update(packet)
                self.assertEqual(packet.face.head_direction.value, expected)
                self.assertEqual(page.values["Head"].text(), expected)
                self.assertIn(f"HEAD: {expected}", page.head_badge.text())
        self.assertEqual(result.events, ())

    def test_prolonged_head_directions_reach_timeline_risk_and_banner(self):
        for direction, delta in (("LEFT", 8), ("RIGHT", 8), ("UP", 5), ("DOWN", 10)):
            with self.subTest(direction=direction):
                face = FaceResult(FaceStatus.FACE_DETECTED, 1, HeadDirection[direction])
                result = self.monitor([0, 3.99, 4, 20], faces=[face] * 4)
                self.assertEqual([e.type for e in result.events], [EventType[f"LOOK_{direction}"]])
                self.assertEqual(result.risk_score, delta)
                self.assertEqual(self.updates[1].head_duration_seconds, 3.99)
                self.assertEqual(self.snapshots, [])
                page = self.page()
                self.assertEqual(page.timeline.count(), 1)
                self.assertIn(f"LOOK {direction}", page.timeline.item(0).text())
                self.assertEqual(page.banner.text(), f"LOOK {direction}")
                self.assertEqual(page.risk_score.text(), f"{delta} / 100")

    def test_face_warning_qualifies_persists_and_retains_history_risk_evidence(self):
        result = self.monitor([0, 2.99, 3, 30], faces=[ABSENT] * 4)
        self.assertEqual([p.face_missing_warning for p in self.updates], [False, False, True, True])
        self.assertEqual([e.type for e in result.events], [EventType.FACE_MISSING])
        self.assertEqual(result.risk_score, 15)
        self.assertTrue(Path(result.events[0].evidence_path).is_file())
        page = self.page()
        page._clear_banner()
        self.assertFalse(page.face_warning.isHidden())

    def test_face_return_clears_warning_immediately(self):
        self.monitor([0, 3, 4], faces=[ABSENT, ABSENT, PRESENT])
        self.assertEqual([p.face_missing_warning for p in self.updates], [False, True, False])
        self.assertTrue(self.page().face_warning.isHidden())

    def test_tracker_unavailable_never_creates_absence_violation(self):
        result = self.monitor([0, 3, 30], faces=[UNAVAILABLE] * 3)
        self.assertEqual(result.events, ())
        self.assertFalse(any(p.face_missing_warning for p in self.updates))
        self.assertTrue(self.page().face_warning.isHidden())

    def test_tracker_failure_clears_existing_absence_warning(self):
        self.monitor([0, 3, 4], faces=[ABSENT, ABSENT, UNAVAILABLE])
        self.assertFalse(self.updates[-1].face_missing_warning)

    def test_new_absence_warning_appears_during_cooldown_without_repeat_event(self):
        result = self.monitor([0, 3, 4, 5, 8], faces=[ABSENT, ABSENT, PRESENT, ABSENT, ABSENT])
        self.assertEqual(len(result.events), 1)
        self.assertTrue(self.updates[-1].face_missing_warning)
        self.assertEqual(result.risk_score, 15)

    def test_obstruction_never_emits_from_one_dark_frame(self):
        result = self.monitor([0, 1, 20], frames=[BLACK, usable_frame(), usable_frame()])
        self.assertEqual(result.events, ())
        self.assertFalse(any(p.camera_obstructed_warning for p in self.updates))
        self.assertEqual(self.snapshots, [])

    def test_sustained_cover_emits_once_with_persistent_warning_and_risk(self):
        result = self.monitor([0, 1.99, 2, 30], frames=[BLACK] * 4, faces=[ABSENT] * 4)
        self.assertEqual([e.type for e in result.events], [EventType.CAMERA_OBSTRUCTED])
        self.assertEqual(result.risk_score, 20)
        self.assertEqual([p.camera_obstructed_warning for p in self.updates], [False, False, True, True])
        self.assertFalse(any(p.face_missing_warning for p in self.updates))
        page = self.page()
        page._clear_banner()
        self.assertFalse(page.obstruction_warning.isHidden())
        self.assertTrue(page.face_warning.isHidden())
        self.assertEqual(len(self.snapshots), 1)

    def test_obstruction_clears_immediately_on_usable_frame(self):
        self.monitor([0, 2, 3], frames=[BLACK, BLACK, usable_frame()])
        self.assertEqual([p.camera_obstructed_warning for p in self.updates], [False, True, False])
        self.assertTrue(self.page().obstruction_warning.isHidden())

    def test_obstruction_evidence_uses_recent_usable_frame(self):
        result = self.monitor([0, 1, 3], frames=[usable_frame(), BLACK, BLACK], faces=[PRESENT, ABSENT, ABSENT])
        emitted = result.events[0]
        self.assertEqual(emitted.metadata["evidence_frame_source"], "last_usable")
        self.assertEqual(emitted.metadata["last_usable_age_seconds"], 3)
        self.assertGreater(self.snapshots[0][20, 20, 0], 0)
        self.assertTrue(Path(emitted.evidence_path).is_file())

    def test_obstruction_with_no_usable_frame_attempts_current_evidence(self):
        result = self.monitor([0, 2], frames=[BLACK] * 2)
        self.assertEqual(result.events[0].metadata["evidence_frame_source"], "current")
        self.assertTrue(Path(result.events[0].evidence_path).is_file())

    def test_valid_pin_starts_break_and_suppresses_normal_absence(self):
        result = self.monitor([0, 3, 59], faces=[ABSENT] * 3, commands={0: [("start", PIN, 60)]})
        self.assertEqual([e.type for e in result.events], [EventType.AUTHORIZED_BREAK_STARTED])
        self.assertEqual(result.events[0].risk_delta, 0)
        self.assertEqual(result.risk_score, 0)
        self.assertTrue(all(p.break_active for p in self.updates))
        self.assertFalse(any(p.face_missing_warning for p in self.updates))
        self.assertEqual(self.snapshots, [])
        page = self.page()
        self.assertFalse(page.break_banner.isHidden())
        self.assertIn("00:01 remaining", page.break_banner.text())
        self.assertTrue(page.face_warning.isHidden())
        self.assertFalse(page.end_break_button.isHidden())

    def test_invalid_pin_cannot_bypass_worker_authorization(self):
        result = self.monitor([0, 3], faces=[ABSENT] * 2, commands={0: [("start", "wrong", 60)]})
        self.assertEqual([e.type for e in result.events], [EventType.FACE_MISSING])
        self.assertFalse(any(p.break_active for p in self.updates))
        self.assertTrue(any("not authorized" in text for text in self.notices))

    def test_break_expiry_resumes_fresh_face_missing_duration(self):
        result = self.monitor([0, 59, 60, 62.99, 63], faces=[ABSENT] * 5,
                              commands={0: [("start", PIN, 60)]})
        self.assertEqual([e.type for e in result.events], [EventType.AUTHORIZED_BREAK_STARTED,
                         EventType.AUTHORIZED_BREAK_EXPIRED, EventType.FACE_MISSING])
        self.assertEqual([p.face_missing_warning for p in self.updates], [False, False, False, False, True])
        self.assertEqual(result.events[-1].duration, 3)
        self.assertEqual(result.risk_score, 15)
        self.assertTrue(self.page().break_banner.isHidden())

    def test_early_break_end_restores_fresh_face_missing_duration(self):
        result = self.monitor([0, 10, 12.99, 13], faces=[ABSENT] * 4,
                 commands={0: [("start", PIN, 60)], 1: [("end",)]})
        self.assertEqual([e.type for e in result.events], [EventType.AUTHORIZED_BREAK_STARTED,
                         EventType.AUTHORIZED_BREAK_ENDED, EventType.FACE_MISSING])
        self.assertEqual(result.events[-1].duration, 3)
        self.assertEqual(result.risk_score, 15)

    def test_authorizing_break_clears_preexisting_missing_warning(self):
        result = self.monitor([0, 3, 4, 30], faces=[ABSENT] * 4, commands={2: [("start", PIN, 60)]})
        self.assertEqual([p.face_missing_warning for p in self.updates], [False, True, False, False])
        self.assertEqual(result.risk_score, 15)  # Existing violation is retained.
        self.assertEqual([e.type for e in result.events], [EventType.FACE_MISSING, EventType.AUTHORIZED_BREAK_STARTED])

    def test_security_remains_active_during_break_with_risk_and_no_evidence(self):
        shortcut = replace(event(EventType.ALT_TAB_ATTEMPT), risk_delta=None)
        result = self.monitor([0, 3, 20], faces=[ABSENT] * 3, commands={0: [("start", PIN, 60)]},
                              security_events={1: [shortcut]})
        self.assertEqual([e.type for e in result.events], [EventType.AUTHORIZED_BREAK_STARTED, EventType.ALT_TAB_ATTEMPT])
        self.assertEqual(result.risk_score, 15)
        self.assertEqual(self.snapshots, [])
        self.assertTrue(self.updates[-1].break_active)
        self.assertIn("ALT TAB ATTEMPT", self.page().timeline.item(0).text())

    def test_covered_camera_still_scores_during_break(self):
        result = self.monitor([0, 2, 20], frames=[BLACK] * 3, faces=[ABSENT] * 3,
                              commands={0: [("start", PIN, 60)]})
        self.assertEqual([e.type for e in result.events], [EventType.AUTHORIZED_BREAK_STARTED, EventType.CAMERA_OBSTRUCTED])
        self.assertEqual(result.risk_score, 20)
        page = self.page()
        self.assertFalse(page.obstruction_warning.isHidden())
        self.assertFalse(page.break_banner.isHidden())
        self.assertTrue(page.face_warning.isHidden())

    def test_break_start_end_same_sample_resets_pre_break_absence_duration(self):
        result = self.monitor([0, 2.99, 3, 6], faces=[ABSENT] * 4,
                              commands={2: [("start", PIN, 60), ("end",)]})
        self.assertFalse(self.updates[2].face_missing_warning)
        self.assertEqual(result.events[-1].type, EventType.FACE_MISSING)
        self.assertEqual(result.events[-1].duration, 3)

    def test_custom_obstruction_duration_reaches_desktop(self):
        config = SessionConfig(breaks=BreakConfig(teacher_pin=PIN))
        rules = dict(config.events.rules)
        rules[EventType.CAMERA_OBSTRUCTED] = EventRule(5, 10, Severity.HIGH, "Camera view obstructed")
        config = replace(config, events=replace(config.events, rules=rules))
        result = self.monitor([0, 2, 5], frames=[BLACK] * 3, config=config)
        self.assertEqual([p.camera_obstructed_warning for p in self.updates], [False, False, True])
        self.assertEqual(result.events[0].duration, 5)

    def test_obstruction_save_failure_retains_warning_history_and_risk(self):
        with contextlib.redirect_stderr(io.StringIO()):
            result = self.monitor([0, 2, 20], frames=[BLACK] * 3,
                                  encoder=Mock(side_effect=OSError("disk full")))
        self.assertEqual([e.type for e in result.events], [EventType.CAMERA_OBSTRUCTED])
        self.assertIsNone(result.events[0].evidence_path)
        self.assertEqual(result.risk_score, 20)
        self.assertTrue(self.updates[-1].camera_obstructed_warning)
        self.assertTrue(any("Evidence save failed" in text for text in self.notices))

    def test_expiry_with_returned_face_has_no_missing_violation(self):
        result = self.monitor([0, 59, 60, 90], faces=[ABSENT, ABSENT, PRESENT, PRESENT],
                              commands={0: [("start", PIN, 60)]})
        self.assertEqual([e.type for e in result.events], [EventType.AUTHORIZED_BREAK_STARTED,
                         EventType.AUTHORIZED_BREAK_EXPIRED])
        self.assertEqual(result.risk_score, 0)
        self.assertFalse(any(p.face_missing_warning for p in self.updates))

    def test_stop_clears_pending_pin_commands_and_rejects_new_requests(self):
        self.monitor([0])
        self.assertTrue(self.worker._break_commands.empty())
        self.assertFalse(self.worker.request_authorized_break(PIN, 60))
        self.assertTrue(self.worker._break_commands.empty())

    def test_phone_remains_active_during_break(self):
        from vision.yolo_detector import Detection

        result = self.monitor([0, 1, 20], faces=[ABSENT] * 3,
                              commands={0: [("start", PIN, 60)]},
                              detections=[Detection(67, .92, (10, 10, 25, 35))])
        self.assertEqual([e.type for e in result.events], [EventType.AUTHORIZED_BREAK_STARTED,
                         EventType.PHONE_DETECTED])
        self.assertEqual(result.risk_score, 30)
        self.assertEqual(len(self.snapshots), 1)

    def test_early_end_retains_pre_break_event_cooldown(self):
        result = self.monitor([0, 3, 4, 5, 8, 13], faces=[ABSENT] * 6,
                              commands={2: [("start", PIN, 60)], 3: [("end",)]})
        self.assertTrue(self.updates[4].face_missing_warning)
        self.assertEqual(self.updates[4].risk_score, 15)
        missing = [e for e in result.events if e.type == EventType.FACE_MISSING]
        self.assertEqual([e.risk_delta for e in missing], [15, 8])
        self.assertEqual(missing[-1].duration, 8)


class BreakControlsChecks(unittest.TestCase):
    def setUp(self):
        config = SessionConfig(breaks=BreakConfig(teacher_pin=PIN))
        self.window = MainWindow(config, worker_factory=FakeWorker, discovery_factory=FakeDiscovery, auto_discover=False)
        self.window.setup_page.set_discovery([(0, "Camera 0")], READY)
        self.window.start_session("Student", "Exam", 0)
        self.worker = self.window.worker
        self.worker.request_authorized_break = Mock()
        self.worker.request_end_break = Mock()
        self.worker.session_started.emit(0)
        self.page = self.window.exam_page

    def tearDown(self):
        self.window.finish_session()
        self.worker.complete()
        self.window.close()
        self.window.deleteLater()
        APP.processEvents()

    def test_pin_required_before_duration_choices_and_worker_request(self):
        self.page.authorize_break_button.click()
        self.assertFalse(self.page.authorization_panel.isHidden())
        self.assertTrue(self.page.break_duration.isHidden())
        self.assertTrue(self.page.start_break_button.isHidden())
        self.page._submit_break()
        self.worker.request_authorized_break.assert_not_called()

    def test_invalid_pin_keeps_break_disabled(self):
        self.page.authorize_break_button.click()
        self.page.pin_input.setText("wrong")
        self.page.verify_pin_button.click()
        self.assertIn("Invalid", self.page.authorization_message.text())
        self.assertTrue(self.page.start_break_button.isHidden())
        self.worker.request_authorized_break.assert_not_called()

    def test_valid_pin_reveals_durations_and_sends_selected_command(self):
        self.page.authorize_break_button.click()
        self.page.pin_input.setText(PIN)
        self.page.verify_pin_button.click()
        self.assertEqual([self.page.break_duration.itemData(i) for i in range(3)], [60, 180, 300])
        self.assertFalse(self.page.break_duration.isHidden())
        self.page.break_duration.setCurrentIndex(1)
        self.page.start_break_button.click()
        self.worker.request_authorized_break.assert_called_once_with(PIN, 180)
        self.assertTrue(self.page.authorization_panel.isHidden())
        self.assertEqual(self.page.pin_input.text(), "")

    def test_q_is_temporarily_disabled_during_pin_entry_then_restored(self):
        self.page.authorize_break_button.click()
        self.assertFalse(self.window._q_shortcut.isEnabled())
        self.page._close_authorization()
        self.assertTrue(self.window._q_shortcut.isEnabled())

    def test_end_break_button_sends_worker_command(self):
        self.page.apply_update(replace(update(), break_active=True, break_remaining_seconds=58))
        self.assertIn("00:58", self.page.break_banner.text())
        self.page.end_break_button.click()
        self.worker.request_end_break.assert_called_once()

    def test_break_and_obstruction_warnings_fit_laptop_without_hiding_timeline(self):
        self.window.resize(1366, 728)
        self.window.show()
        self.page.apply_update(replace(update(), break_active=True, break_remaining_seconds=60,
                                       camera_obstructed_warning=True))
        APP.processEvents()
        self.assertLessEqual(self.window.minimumSizeHint().height(), 728)
        self.assertGreater(self.page.event_count.geometry().top(), self.page.timeline.geometry().bottom())
        self.assertFalse(self.page.obstruction_warning.isHidden())
        self.assertFalse(self.page.break_banner.isHidden())

    def test_stopping_disables_break_controls_and_clears_pin(self):
        self.page.authorize_break_button.click()
        self.page.pin_input.setText(PIN)
        self.window.finish_session()
        self.assertFalse(self.page.authorize_break_button.isEnabled())
        self.assertFalse(self.page.end_break_button.isEnabled())
        self.assertEqual(self.page.pin_input.text(), "")
        self.assertFalse(self.window._q_shortcut.isEnabled())


if __name__ == "__main__":
    unittest.main()
