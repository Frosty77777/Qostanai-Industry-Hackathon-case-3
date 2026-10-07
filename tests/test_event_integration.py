"""CV signal mapping, event overlays, and deterministic webcam-loop checks."""

import contextlib
from dataclasses import replace
from datetime import datetime, timezone
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main
from monitoring import EventEngine, EventEngineConfig, EventType, FrameSignals
from vision.face_tracker import FaceResult, FaceStatus, HeadDirection
from vision.yolo_detector import Detection


class EventIntegrationChecks(unittest.TestCase):
    def setUp(self):
        monitor = MagicMock()
        monitor.poll.return_value = []
        self.enterContext(patch.object(main, "SecurityMonitor", return_value=monitor))
        self.face = FaceResult(FaceStatus.FACE_DETECTED, 1, HeadDirection.CENTER)
        self.frame = np.zeros((480, 640, 3), np.uint8)
        self.timestamp = datetime(2026, 10, 7, 14, 32, 5, tzinfo=timezone.utc)
        # Existing loop tests keep evidence I/O isolated; dedicated evidence
        # tests exercise real temporary files and failure paths.
        self.evidence_capture = self.enterContext(patch.object(
            main.EvidenceManager, "capture", side_effect=lambda event, frame: event,
        ))

    def phone_event(self):
        engine = EventEngine(wall_clock=lambda: self.timestamp)
        engine.update(FrameSignals(phone_detected=True, phone_confidence=0.91), now=0)
        return engine.update(FrameSignals(phone_detected=True, phone_confidence=0.91), now=0.82)[0]

    def test_maps_counts_status_and_confidence(self):
        detections = [
            Detection(0, 0.7, (0, 0, 80, 120)),
            Detection(0, 0.9, (100, 0, 250, 350)),
            Detection(0, 0.8, (300, 0, 400, 250)),
            Detection(67, 0.6, (0, 10, 10, 20)),
            Detection(67, 0.91, (10, 10, 20, 20)),
        ]
        face = FaceResult(FaceStatus.MULTIPLE_FACES, 2)
        signals = main.frame_signals(detections, face, self.frame.shape)
        self.assertTrue(signals.phone_detected)
        self.assertEqual(signals.phone_confidence, 0.91)
        self.assertEqual((signals.person_count, signals.persons_confidence), (3, 0.8))
        self.assertEqual((signals.face_status, signals.face_count, signals.head_direction), ("MULTIPLE_FACES", 2, "UNKNOWN"))
        engine = EventEngine()
        engine.update(signals, now=0)
        events = engine.update(signals, now=1)
        self.assertEqual([e.type for e in events], [EventType.PHONE_DETECTED, EventType.MULTIPLE_PERSONS, EventType.MULTIPLE_FACES])
        self.assertEqual(events[1].confidence, 0.8)

    def test_unavailable_tracker_keeps_yolo_events(self):
        signals = main.frame_signals([Detection(67, 0.9, (0, 0, 10, 10))], FaceResult(FaceStatus.UNAVAILABLE, None), self.frame.shape)
        engine = EventEngine()
        engine.update(signals, now=0)
        self.assertEqual([e.type for e in engine.update(signals, now=10)], [EventType.PHONE_DETECTED])

    def test_weak_secondary_person_keeps_raw_boxes_count_and_phone_detection(self):
        detections = [Detection(0, 0.91, (5, 10, 150, 300)),
                      Detection(0, 0.49, (200, 150, 280, 250)),
                      Detection(67, 0.62, (350, 200, 380, 250))]
        signals = main.frame_signals(detections, self.face, self.frame.shape)
        self.assertEqual((signals.person_count, signals.persons_confidence), (2, 0.49))
        self.assertEqual(signals.phone_confidence, 0.62)
        with patch.object(cv2, "putText", wraps=cv2.putText) as text:
            main.draw_preview(cv2, self.frame, detections, 20, self.face)
        labels = [call.args[1] for call in text.call_args_list]
        for expected in ("person 0.91", "person 0.49", "Persons: 2", "cell phone 0.62", "Phone detected: YES"):
            self.assertIn(expected, labels)
        engine = EventEngine()
        engine.update(signals, now=0)
        self.assertEqual([event.type for event in engine.update(signals, now=0.7)], [EventType.PHONE_DETECTED])
        self.assertEqual(engine.update(signals, now=100), [])

    def test_empty_detection_mapping(self):
        signals = main.frame_signals([], self.face, self.frame.shape)
        self.assertFalse(signals.phone_detected)
        self.assertEqual(signals.person_count, 0)
        self.assertIsNone(signals.phone_confidence)
        self.assertIsNone(signals.persons_confidence)

    def test_console_format(self):
        self.assertEqual(main.format_event(self.phone_event()), "[14:32:05] CRITICAL | PHONE_DETECTED | Cell phone detected | duration=0.82s")

    def test_event_preview_preserves_all_existing_overlays(self):
        detections = [Detection(0, 0.9, (1, 2, 30, 50)), Detection(67, 0.8, (50, 60, 80, 90))]
        with patch.object(cv2, "putText", wraps=cv2.putText) as put_text:
            preview = main.draw_preview(cv2, self.frame, detections, 20, self.face, self.phone_event())
        texts = [call.args[1] for call in put_text.call_args_list]
        for text in ("person 0.90", "cell phone 0.80", "Persons: 1", "Phone detected: YES", "FPS: 20.0 | Q: quit", "Face: detected", "Head: CENTER", "Faces: 1 (FACE_DETECTED)", "Latest: CRITICAL | PHONE_DETECTED", "Cell phone detected | duration=0.82s"):
            self.assertIn(text, texts)
        self.assertEqual(preview.shape, (640, 640, 3))
        np.testing.assert_array_equal(preview[:480], self.frame)

    def test_webcam_loop_prints_once_expires_preview_and_exits_on_q(self):
        # Six frames span 12 seconds without sleeping; phone stays visible.
        times = (0, 0.5, 1, 2, 4, 12)
        fake_cv2 = MagicMock()
        fake_cv2.waitKey.side_effect = [0] * (len(times) - 1) + [ord("q")]
        fake_cv2.getWindowProperty.return_value = 1
        camera = MagicMock()
        camera.read.return_value = (True, self.frame)
        detector = MagicMock()
        detector.detect.return_value = [Detection(67, 0.91, (0, 0, 10, 10))]
        tracker = MagicMock()
        tracker.available = True
        tracker.process.return_value = self.face
        engine = EventEngine(wall_clock=lambda: self.timestamp)
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["main.py"]),
            patch.dict(sys.modules, {"cv2": fake_cv2}),
            patch.object(main, "YoloDetector", return_value=detector),
            patch.object(main, "FaceTracker", return_value=tracker),
            patch.object(main, "EventEngine", return_value=engine),
            patch.object(main, "open_webcam", return_value=(camera, self.frame)),
            patch.object(main, "draw_preview", return_value=self.frame) as draw,
            patch.object(main, "perf_counter", side_effect=[value for t in times for value in (t, t, t + 0.01)]),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(main.main(), 0)
        self.assertEqual(output.getvalue().count("CRITICAL | PHONE_DETECTED"), 1)
        self.assertEqual(len(engine.history), 1)
        previews = [call.args[5] for call in draw.call_args_list]
        self.assertEqual(previews, [None, None, engine.history[0], engine.history[0], None, None])
        self.assertEqual(detector.detect.call_count, len(times))
        self.assertEqual(tracker.process.call_count, len(times))
        camera.release.assert_called_once()
        tracker.close.assert_called_once()
        fake_cv2.destroyAllWindows.assert_called_once()

    def test_simultaneous_events_print_all_and_preview_only_latest(self):
        fake_cv2 = MagicMock()
        fake_cv2.waitKey.side_effect = [0, ord("Q")]
        fake_cv2.getWindowProperty.return_value = 1
        camera = MagicMock()
        camera.read.return_value = (True, self.frame)
        detector = MagicMock()
        detector.detect.return_value = [
            Detection(0, 0.9, (0, 0, 150, 350)),
            Detection(0, 0.8, (300, 0, 400, 250)),
            Detection(67, 0.91, (20, 0, 30, 10)),
        ]
        tracker = MagicMock()
        tracker.available = True
        tracker.process.return_value = FaceResult(FaceStatus.MULTIPLE_FACES, 2)
        engine = EventEngine(wall_clock=lambda: self.timestamp)
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["main.py"]),
            patch.dict(sys.modules, {"cv2": fake_cv2}),
            patch.object(main, "YoloDetector", return_value=detector),
            patch.object(main, "FaceTracker", return_value=tracker),
            patch.object(main, "EventEngine", return_value=engine),
            patch.object(main, "open_webcam", return_value=(camera, self.frame)),
            patch.object(main, "draw_preview", return_value=self.frame) as draw,
            patch.object(main, "perf_counter", side_effect=[0, 0, 0.01, 1, 1, 1.01]),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(main.main(), 0)
        self.assertEqual([event.type for event in engine.history], [EventType.PHONE_DETECTED, EventType.MULTIPLE_PERSONS, EventType.MULTIPLE_FACES])
        for event in engine.history:
            self.assertIn(main.format_event(event), output.getvalue())
        self.assertEqual(draw.call_args.args[5].type, EventType.MULTIPLE_FACES)

    def run_warning_frames(self, samples, config=None):
        """Exercise the real loop, engine and drawing with mock frame time."""
        fake_cv2 = MagicMock()
        fake_cv2.waitKey.side_effect = [0] * (len(samples) - 1) + [ord("q")]
        fake_cv2.getWindowProperty.return_value = 1
        camera = MagicMock()
        camera.read.return_value = (True, self.frame)
        detector = MagicMock()
        detector.detect.return_value = [Detection(0, 0.9, (1, 2, 30, 50))]
        tracker = MagicMock()
        tracker.available = samples[0][1].status != FaceStatus.UNAVAILABLE
        faces = iter(face for _, face in samples)

        def process_frame(frame):
            face = next(faces)
            tracker.available = face.status != FaceStatus.UNAVAILABLE
            return face

        tracker.process.side_effect = process_frame
        engine = EventEngine(config, wall_clock=lambda: self.timestamp)
        original_draw = main.draw_preview
        rendered = []

        def render(_cv2, *args, **kwargs):
            with patch.object(cv2, "putText", wraps=cv2.putText) as put_text:
                preview = original_draw(cv2, *args, **kwargs)
            rendered.append({
                "warning": kwargs["face_missing_warning"],
                "texts": [call.args[1] for call in put_text.call_args_list],
                "preview": preview,
                "latest_event": args[4],
            })
            return preview

        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["main.py"]),
            patch.dict(sys.modules, {"cv2": fake_cv2}),
            patch.object(main, "YoloDetector", return_value=detector),
            patch.object(main, "FaceTracker", return_value=tracker),
            patch.object(main, "EventEngine", return_value=engine),
            patch.object(main, "open_webcam", return_value=(camera, self.frame)),
            patch.object(main, "draw_preview", side_effect=render),
            patch.object(main, "perf_counter", side_effect=[value for t, _ in samples for value in (t, t, t + 0.01)]),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main.main(), 0)
        self.assertEqual(detector.detect.call_count, len(samples))
        self.assertEqual(tracker.process.call_count, len(samples))
        camera.release.assert_called_once()
        tracker.close.assert_called_once()
        fake_cv2.destroyAllWindows.assert_called_once()
        return engine, rendered, output.getvalue()

    def test_face_warning_appears_after_configured_threshold(self):
        absent = FaceResult(FaceStatus.NO_FACE, 0)
        for threshold in (3.0, 2.0):
            with self.subTest(threshold=threshold):
                rules = dict(EventEngineConfig().rules)
                rules[EventType.FACE_MISSING] = replace(rules[EventType.FACE_MISSING], threshold_seconds=threshold)
                engine, rendered, _ = self.run_warning_frames(
                    [(0, absent), (threshold - 0.01, absent), (threshold, absent)],
                    EventEngineConfig(rules=rules),
                )
                self.assertEqual([r["warning"] for r in rendered], [False, False, True])
                self.assertNotIn(main.FACE_MISSING_WARNING_TEXT, rendered[1]["texts"])
                self.assertIn("WARNING: STUDENT LEFT CAMERA VIEW", rendered[2]["texts"])
                self.assertEqual([event.type for event in engine.history], [EventType.FACE_MISSING])

    def test_face_warning_persists_after_banner_and_cooldown_without_spam(self):
        absent = FaceResult(FaceStatus.NO_FACE, 0)
        engine, rendered, output = self.run_warning_frames(
            [(0, absent), (3, absent), (6, absent), (14, absent), (30, absent)],
        )
        self.assertEqual([r["warning"] for r in rendered], [False, True, True, True, True])
        for result in rendered[1:]:
            self.assertIn(main.FACE_MISSING_WARNING_TEXT, result["texts"])
        self.assertIsNone(rendered[2]["latest_event"])
        self.assertEqual(len(engine.history), 1)
        self.assertEqual(output.count("HIGH | FACE_MISSING"), 1)

    def test_face_warning_disappears_when_face_returns(self):
        absent = FaceResult(FaceStatus.NO_FACE, 0)
        _, rendered, _ = self.run_warning_frames([(0, absent), (3, absent), (4, self.face)])
        self.assertIn(main.FACE_MISSING_WARNING_TEXT, rendered[1]["texts"])
        self.assertFalse(rendered[2]["warning"])
        self.assertNotIn(main.FACE_MISSING_WARNING_TEXT, rendered[2]["texts"])
        self.assertIn("Head: CENTER", rendered[2]["texts"])

    def test_multiple_faces_clear_absence_warning(self):
        absent = FaceResult(FaceStatus.NO_FACE, 0)
        multiple = FaceResult(FaceStatus.MULTIPLE_FACES, 2)
        _, rendered, _ = self.run_warning_frames([(0, absent), (3, absent), (4, multiple)])
        self.assertFalse(rendered[2]["warning"])
        self.assertNotIn(main.FACE_MISSING_WARNING_TEXT, rendered[2]["texts"])
        self.assertIn("Faces: 2 (MULTIPLE_FACES)", rendered[2]["texts"])

    def test_unavailable_tracker_never_triggers_face_warning(self):
        unavailable = FaceResult(FaceStatus.UNAVAILABLE, None)
        engine, rendered, _ = self.run_warning_frames([(0, unavailable), (3, unavailable), (30, unavailable)])
        self.assertEqual(engine.history, [])
        for result in rendered:
            self.assertFalse(result["warning"])
            self.assertNotIn(main.FACE_MISSING_WARNING_TEXT, result["texts"])
            self.assertIn("Face: unavailable", result["texts"])

    def test_tracking_failure_clears_existing_face_warning(self):
        absent = FaceResult(FaceStatus.NO_FACE, 0)
        unavailable = FaceResult(FaceStatus.UNAVAILABLE, None)
        engine, rendered, _ = self.run_warning_frames(
            [(0, absent), (3, absent), (4, unavailable), (30, unavailable)],
        )
        self.assertTrue(rendered[1]["warning"])
        for result in rendered[2:]:
            self.assertFalse(result["warning"])
            self.assertNotIn(main.FACE_MISSING_WARNING_TEXT, result["texts"])
            self.assertIn("Face: unavailable", result["texts"])
        self.assertEqual(len(engine.history), 1)

    def test_new_absence_warning_qualifies_while_event_cooldown_is_preserved(self):
        absent = FaceResult(FaceStatus.NO_FACE, 0)
        engine, rendered, output = self.run_warning_frames(
            [(0, absent), (3, absent), (4, self.face), (5, absent), (8, absent), (13, absent)],
        )
        self.assertEqual([r["warning"] for r in rendered], [False, True, False, False, True, True])
        self.assertEqual([event.type for event in engine.history], [EventType.FACE_MISSING] * 2)
        self.assertEqual(output.count("HIGH | FACE_MISSING"), 2)

    def test_face_warning_uses_red_footer_and_preserves_existing_overlays(self):
        absent = FaceResult(FaceStatus.NO_FACE, 0)
        _, rendered, _ = self.run_warning_frames([(0, absent), (3, absent)])
        preview = rendered[1]["preview"]
        self.assertEqual(preview.shape, (760, 640, 3))
        np.testing.assert_array_equal(preview[700, 0], [0, 0, 180])
        np.testing.assert_array_equal(preview[:480], self.frame)
        for text in ("person 0.90", "Persons: 1", "Phone detected: NO", "FPS: warming up | Q: quit", "Head: UNKNOWN", "Faces: 0 (NO_FACE)", "Latest: HIGH | FACE_MISSING"):
            self.assertIn(text, rendered[1]["texts"])

    def test_preview_suppresses_stale_warning_for_valid_or_unavailable_faces(self):
        for face in (self.face, FaceResult(FaceStatus.UNAVAILABLE, None), None):
            with self.subTest(face=face), patch.object(cv2, "putText", wraps=cv2.putText) as put_text:
                preview = main.draw_preview(cv2, self.frame, [], 20, face, face_missing_warning=True)
                self.assertNotIn(main.FACE_MISSING_WARNING_TEXT, [call.args[1] for call in put_text.call_args_list])
                self.assertEqual(preview.shape, (580, 640, 3))


if __name__ == "__main__":
    unittest.main()
