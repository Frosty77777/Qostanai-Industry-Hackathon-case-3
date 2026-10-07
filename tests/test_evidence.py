"""Event-only snapshots using temporary files and mocked failures; no webcam."""

import contextlib
from dataclasses import replace
from datetime import datetime, timezone
import io
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, Mock, patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main
from evidence import EvidenceConfig, EvidenceManager
from evidence.evidence_manager import DEFAULT_EVIDENCE_DIRECTORY, _encode_jpeg
from monitoring import EventEngine, EventType, FrameSignals, ProctoringEvent, Severity
from vision.face_tracker import FaceResult, FaceStatus, HeadDirection
from vision.yolo_detector import Detection


class EvidenceManagerChecks(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.directory = self.root / "sessions" / "current" / "evidence"
        self.manager = EvidenceManager(EvidenceConfig(directory=self.directory))
        self.frame = np.full((48, 64, 3), [10, 20, 30], dtype=np.uint8)
        self.timestamp = datetime(2026, 10, 7, 17, 35, 12, 481000, tzinfo=timezone.utc)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def event(self, event_type=EventType.PHONE_DETECTED):
        return ProctoringEvent(
            type=event_type, timestamp=self.timestamp, severity=Severity.CRITICAL,
            duration=1.1, message="Test event", confidence=0.92,
            metadata={"person_count": 2},
        )

    def assert_snapshot_created(self, event_type):
        event = self.manager.capture(self.event(event_type), self.frame)
        path = Path(event.evidence_path)
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, self.directory.resolve())
        self.assertEqual(path.name, f"{event_type.value}_20261007_173512_481.jpg")
        image = cv2.imread(str(path))
        self.assertIsNotNone(image)
        self.assertEqual(image.shape, self.frame.shape)

    def test_phone_evidence_file_created(self):
        self.assert_snapshot_created(EventType.PHONE_DETECTED)

    def test_multiple_person_evidence_file_created(self):
        self.assert_snapshot_created(EventType.MULTIPLE_PERSONS)

    def test_multiple_face_evidence_file_created(self):
        self.assert_snapshot_created(EventType.MULTIPLE_FACES)

    def test_face_missing_evidence_file_created(self):
        self.assert_snapshot_created(EventType.FACE_MISSING)

    def test_head_evidence_disabled_by_default(self):
        encoder = Mock()
        manager = EvidenceManager(EvidenceConfig(directory=self.directory), encoder=encoder)
        for event_type in (EventType.LOOK_LEFT, EventType.LOOK_RIGHT, EventType.LOOK_UP, EventType.LOOK_DOWN):
            with self.subTest(event_type=event_type):
                original = self.event(event_type)
                event = manager.capture(original, self.frame)
                self.assertIs(event, original)
                self.assertIsNone(event.evidence_path)
        encoder.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_head_evidence_enabled_for_all_directions(self):
        manager = EvidenceManager(EvidenceConfig(directory=self.directory, head_direction_enabled=True))
        for event_type in (EventType.LOOK_LEFT, EventType.LOOK_RIGHT, EventType.LOOK_UP, EventType.LOOK_DOWN):
            with self.subTest(event_type=event_type):
                event = manager.capture(self.event(event_type), self.frame)
                self.assertTrue(Path(event.evidence_path).is_file())
        self.assertEqual(len(list(self.directory.glob("*.jpg"))), 4)

    def test_path_attached_and_other_event_data_unchanged(self):
        original = self.event()
        event = self.manager.capture(original, self.frame)
        self.assertIsNone(original.evidence_path)
        self.assertIsNotNone(event.evidence_path)
        self.assertEqual(replace(event, evidence_path=None), original)
        self.assertEqual(event.to_dict()["evidence_path"], event.evidence_path)

    def test_encode_exception_keeps_event_and_logs_error(self):
        original = self.event()
        manager = EvidenceManager(EvidenceConfig(directory=self.directory), encoder=Mock(side_effect=RuntimeError("encoding failed")))
        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            event = manager.capture(original, self.frame)
        self.assertEqual(event, original)
        self.assertIsNone(event.to_dict()["evidence_path"])
        self.assertIn("[EVIDENCE] Error saving PHONE_DETECTED: encoding failed", error.getvalue())
        self.assertFalse(self.directory.exists())

    def test_empty_encoding_is_failure(self):
        manager = EvidenceManager(EvidenceConfig(directory=self.directory), encoder=lambda frame, quality: b"")
        with contextlib.redirect_stderr(io.StringIO()):
            event = manager.capture(self.event(), self.frame)
        self.assertIsNone(event.evidence_path)
        self.assertFalse(self.directory.exists())

    def test_opencv_false_encoding_is_failure(self):
        with patch.object(cv2, "imencode", return_value=(False, None)), contextlib.redirect_stderr(io.StringIO()):
            event = self.manager.capture(self.event(), self.frame)
        self.assertIsNone(event.evidence_path)
        self.assertFalse(self.directory.exists())

    def test_directory_failure_keeps_event(self):
        with patch.object(Path, "mkdir", side_effect=PermissionError("read only")), contextlib.redirect_stderr(io.StringIO()):
            event = self.manager.capture(self.event(), self.frame)
        self.assertIsNone(event.evidence_path)
        self.assertEqual(event, self.event())

    def test_file_open_failure_keeps_event(self):
        with patch.object(Path, "open", side_effect=PermissionError("cannot write")), contextlib.redirect_stderr(io.StringIO()):
            event = self.manager.capture(self.event(), self.frame)
        self.assertIsNone(event.evidence_path)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_partial_write_failure_removes_incomplete_file(self):
        original_open = Path.open

        def short_write(path, mode):
            stream = original_open(path, mode)
            wrapper = MagicMock()
            wrapper.__enter__.return_value = wrapper
            wrapper.__exit__.side_effect = lambda *args: stream.close()
            wrapper.write.side_effect = lambda data: stream.write(data[:3])
            return wrapper

        with patch.object(Path, "open", short_write), contextlib.redirect_stderr(io.StringIO()):
            event = self.manager.capture(self.event(), self.frame)
        self.assertIsNone(event.evidence_path)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_same_timestamp_names_are_unique_across_managers(self):
        first = self.manager.capture(self.event(), self.frame)
        original_bytes = Path(first.evidence_path).read_bytes()
        second_manager = EvidenceManager(EvidenceConfig(directory=self.directory))
        second = second_manager.capture(self.event(), np.zeros_like(self.frame))
        third = self.manager.capture(self.event(), self.frame)
        self.assertEqual(len({first.evidence_path, second.evidence_path, third.evidence_path}), 3)
        self.assertTrue(Path(second.evidence_path).name.endswith("_481_1.jpg"))
        self.assertTrue(Path(third.evidence_path).name.endswith("_481_2.jpg"))
        self.assertEqual(Path(first.evidence_path).read_bytes(), original_bytes)

    def test_attached_event_is_not_saved_again(self):
        event = self.manager.capture(self.event(), self.frame)
        with patch.object(self.manager, "_encoder") as encoder:
            self.assertIs(self.manager.capture(event, self.frame), event)
        encoder.assert_not_called()
        self.assertEqual(len(list(self.directory.glob("*.jpg"))), 1)

    def test_no_evidence_without_emitted_event(self):
        engine = EventEngine()
        for at in (0, 0.1, 0.69):
            events = engine.update(FrameSignals(phone_detected=True, phone_confidence=0.91), now=at)
            for event in events:
                self.manager.capture(event, self.frame)
            self.assertEqual(events, [])
        self.assertFalse(self.directory.exists())

    def test_success_confirmation(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            event = self.manager.capture(self.event(), self.frame)
        self.assertIn(f"[EVIDENCE] Saved: {event.evidence_path}", output.getvalue())

    def test_project_relative_path_and_default_directory(self):
        self.assertEqual(DEFAULT_EVIDENCE_DIRECTORY, main.ROOT / "sessions" / "current" / "evidence")
        with patch("evidence.evidence_manager.PROJECT_ROOT", self.root):
            event = self.manager.capture(self.event(), self.frame)
        self.assertEqual(event.evidence_path, "sessions/current/evidence/PHONE_DETECTED_20261007_173512_481.jpg")

    def test_quality_configuration(self):
        for quality in (0, 101, 80.5):
            with self.subTest(quality=quality), self.assertRaises(ValueError):
                EvidenceConfig(jpeg_quality=quality)
        encoder = Mock(return_value=b"test JPEG")
        manager = EvidenceManager(EvidenceConfig(directory=self.directory, jpeg_quality=75), encoder=encoder)
        manager.capture(self.event(), self.frame)
        encoder.assert_called_once_with(self.frame, 75)


class EvidenceWebcamChecks(unittest.TestCase):
    def setUp(self):
        monitor = MagicMock()
        monitor.poll.return_value = []
        self.enterContext(patch.object(main, "SecurityMonitor", return_value=monitor))
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.directory = self.root / "evidence"
        self.face = FaceResult(FaceStatus.FACE_DETECTED, 1, HeadDirection.CENTER)
        self.frame = np.zeros((480, 640, 3), np.uint8)
        self.timestamp = datetime(2026, 10, 7, 17, 35, 12, 481000, tzinfo=timezone.utc)

    def run_loop(self, samples, detections, *, head_evidence=False, fail=False):
        fake_cv2 = MagicMock()
        fake_cv2.waitKey.side_effect = [0] * (len(samples) - 1) + [ord("q")]
        fake_cv2.getWindowProperty.return_value = 1
        camera = MagicMock()
        camera.read.return_value = (True, self.frame)
        detector = MagicMock()
        detector.detect.return_value = detections
        tracker = MagicMock()
        tracker.available = True
        tracker.process.side_effect = [face for _, face in samples]
        encoder = Mock(side_effect=OSError("disk test failure") if fail else lambda frame, quality: cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])[1].tobytes())
        manager = EvidenceManager(EvidenceConfig(directory=self.directory, head_direction_enabled=head_evidence), encoder=encoder)
        engine = EventEngine(wall_clock=lambda: self.timestamp)
        original_draw = main.draw_preview
        output, error = io.StringIO(), io.StringIO()
        argv = ["main.py", "--head-evidence"] if head_evidence else ["main.py"]
        with (
            patch.object(sys, "argv", argv),
            patch.dict(sys.modules, {"cv2": fake_cv2}),
            patch.object(main, "YoloDetector", return_value=detector),
            patch.object(main, "FaceTracker", return_value=tracker),
            patch.object(main, "EventEngine", return_value=engine),
            patch.object(main, "EvidenceManager", return_value=manager) as create_manager,
            patch.object(main, "open_webcam", return_value=(camera, self.frame)),
            patch.object(main, "draw_preview", side_effect=lambda _cv2, *args, **kwargs: original_draw(cv2, *args, **kwargs)),
            patch.object(main, "perf_counter", side_effect=[value for t, _ in samples for value in (t, t, t + 0.01)]),
            contextlib.redirect_stdout(output), contextlib.redirect_stderr(error),
        ):
            self.assertEqual(main.main(), 0)
        self.assertEqual(create_manager.call_args.args[0].head_direction_enabled, head_evidence)
        self.assertEqual(detector.detect.call_count, len(samples))
        self.assertEqual(tracker.process.call_count, len(samples))
        camera.release.assert_called_once()
        tracker.close.assert_called_once()
        fake_cv2.destroyAllWindows.assert_called_once()
        return engine, encoder, output.getvalue(), error.getvalue()

    def test_phone_snapshot_once_attached_to_history(self):
        detections = [Detection(67, 0.92, (10, 20, 100, 200))]
        engine, encoder, output, _ = self.run_loop([(0, self.face), (1, self.face), (20, self.face)], detections)
        encoder.assert_called_once()
        self.assertEqual(len(engine.history), 1)
        event = engine.history[0]
        self.assertTrue(Path(event.evidence_path).is_file())
        self.assertEqual((event.type, event.duration, event.confidence), (EventType.PHONE_DETECTED, 1, 0.92))
        self.assertEqual(event.to_dict()["evidence_path"], event.evidence_path)
        self.assertEqual(output.count("[EVIDENCE] Saved:"), 1)
        self.assertEqual(len(list(self.directory.glob("*.jpg"))), 1)

    def test_snapshot_contains_cv_overlay_without_temporary_banners(self):
        absent = FaceResult(FaceStatus.NO_FACE, 0)
        detections = [Detection(0, 0.9, (10, 20, 100, 200))]
        engine, encoder, _, _ = self.run_loop([(0, absent), (3, absent), (20, absent)], detections)
        self.assertEqual(engine.history[0].type, EventType.FACE_MISSING)
        encoded_frame = encoder.call_args.args[0]
        self.assertEqual(encoded_frame.shape, (580, 640, 3))
        # Box pixels and permanent labels remain; event/warning rows start at 580.
        self.assertTrue(np.any(encoded_frame[20:200, 10:100]))
        self.assertTrue(np.any(encoded_frame[480:580] != 20))
        saved = cv2.imread(engine.history[0].evidence_path)
        self.assertEqual(saved.shape, (580, 640, 3))

    def test_no_snapshot_when_loop_emits_no_event(self):
        _, encoder, output, _ = self.run_loop([(0, self.face), (1, self.face), (20, self.face)], [])
        encoder.assert_not_called()
        self.assertFalse(self.directory.exists())
        self.assertNotIn("[EVIDENCE]", output)

    def test_chair_false_phone_does_not_save_cli_evidence(self):
        detections = [Detection(67, .39, (350, 100, 450, 220))]
        engine, encoder, output, _ = self.run_loop([(0, self.face), (.7, self.face), (20, self.face)], detections)
        self.assertEqual(engine.history, [])
        encoder.assert_not_called()
        self.assertFalse(self.directory.exists())
        self.assertNotIn("[EVIDENCE]", output)

    def test_save_failure_does_not_stop_webcam_loop(self):
        detections = [Detection(67, 0.92, (10, 20, 100, 200))]
        engine, encoder, output, error = self.run_loop([(0, self.face), (1, self.face), (20, self.face)], detections, fail=True)
        encoder.assert_called_once()
        self.assertEqual(len(engine.history), 1)
        self.assertIsNone(engine.history[0].evidence_path)
        self.assertEqual(engine.history[0].risk_delta, 30)
        self.assertIn("CRITICAL | PHONE_DETECTED", output)
        self.assertIn("Session risk: 30/100", output)
        self.assertIn("[EVIDENCE] Error saving", error)
        self.assertFalse(self.directory.exists())

    def test_simultaneous_events_save_one_image_per_event(self):
        multiple = FaceResult(FaceStatus.MULTIPLE_FACES, 2)
        detections = [Detection(0, 0.9, (0, 0, 150, 350)), Detection(0, 0.8, (300, 0, 400, 250)), Detection(67, 0.92, (60, 0, 80, 20))]
        engine, encoder, _, _ = self.run_loop([(0, multiple), (1, multiple), (30, multiple)], detections)
        self.assertEqual(encoder.call_count, 3)
        self.assertEqual(len(engine.history), 3)
        self.assertEqual(len({event.evidence_path for event in engine.history}), 3)
        self.assertEqual(len(list(self.directory.glob("*.jpg"))), 3)

    def test_head_evidence_cli_is_optional(self):
        left = FaceResult(FaceStatus.FACE_DETECTED, 1, HeadDirection.LEFT)
        engine, encoder, _, _ = self.run_loop([(0, left), (4, left)], [])
        self.assertEqual(engine.history[0].type, EventType.LOOK_LEFT)
        self.assertIsNone(engine.history[0].evidence_path)
        encoder.assert_not_called()
        engine, encoder, _, _ = self.run_loop([(0, left), (4, left)], [], head_evidence=True)
        encoder.assert_called_once()
        self.assertTrue(Path(engine.history[0].evidence_path).is_file())


if __name__ == "__main__":
    unittest.main()
