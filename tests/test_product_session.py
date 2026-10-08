"""Product session integration: no physical camera, hooks, keys or real waits."""

import contextlib
from dataclasses import replace
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import test_desktop as fakes
from evidence import EvidenceConfig
from monitoring import EventType
from session_storage import SessionStorageConfig, SessionStore
from ui.session import DISPLAY_TIMEZONE, SessionConfig
from ui.vision_worker import VisionWorker
from vision.yolo_detector import Detection


class ProductSessionChecks(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.directory)
        self.config = replace(SessionConfig(), storage=SessionStorageConfig(directory=self.root / "sessions"),
                              evidence=EvidenceConfig(directory=self.root / "legacy-evidence"))
        self.start = datetime(2026, 10, 8, 14, 32, tzinfo=DISPLAY_TIMEZONE)
        self.now = 100.0

    def run_session(self, *, config=None, store_factory=SessionStore, phone=True, camera_failure=False,
                    evidence_factory=None, detector_failure=False):
        capture, tracker, security = fakes.FakeCapture(frames=5, failure=camera_failure), fakes.FakeFace(), fakes.FakeSecurity()
        detector = Mock()

        def detect(frame):
            self.now += 1
            if detector_failure:
                raise RuntimeError("synthetic detector failure")
            return [Detection(67, .92, (10, 10, 25, 35))] if phone else []

        detector.detect.side_effect = detect
        options = {} if evidence_factory is None else {"evidence_factory": evidence_factory}
        worker = VisionWorker(config or self.config, clock=lambda: self.now,
            wall_clock=lambda: self.start + timedelta(seconds=self.now - 100),
            camera_opener=lambda cv2, index: (capture, fakes.usable_frame()),
            detector_factory=lambda *args: detector, face_factory=lambda *args: tracker,
            security_factory=lambda: security, store_factory=store_factory, **options)
        capture.worker = worker
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            worker.run()
        self.assertTrue(capture.released)
        self.assertTrue(tracker.closed)
        self.assertTrue(security.stopped)
        return worker

    def test_completed_session_saves_metadata_and_complete_events(self):
        worker = self.run_session()
        result = worker.result
        self.assertIsNone(result.error)
        self.assertIsNone(result.persistence_error)
        metadata = json.loads((result.session_directory / "session.json").read_text(encoding="utf-8"))
        events = json.loads((result.session_directory / "events.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["student"], "Student")
        self.assertEqual(metadata["exam"], "Practice Exam")
        self.assertEqual(metadata["duration_seconds"], result.duration_seconds)
        self.assertEqual(metadata["risk_score"], 30)
        self.assertEqual(metadata["risk_level"], "MODERATE")
        self.assertEqual(metadata["started_at"], result.started_at.isoformat())
        self.assertEqual(metadata["ended_at"], result.ended_at.isoformat())
        self.assertEqual(events[0]["type"], "PHONE_DETECTED")
        self.assertEqual(events[0]["risk_delta"], 30)
        self.assertEqual(events[0]["confidence"], .92)
        self.assertEqual(events[0]["evidence_path"].split("/")[0], "evidence")
        self.assertTrue((result.session_directory / events[0]["evidence_path"]).is_file())

    def test_evidence_belongs_to_session_folder(self):
        result = self.run_session().result
        evidence = Path(result.events[0].evidence_path)
        self.assertEqual(evidence.parent, result.session_directory / "evidence")
        self.assertFalse((self.root / "legacy-evidence").exists())

    def test_empty_session_still_saves_zero_counts_and_empty_history(self):
        result = self.run_session(phone=False).result
        metadata = json.loads((result.session_directory / "session.json").read_text(encoding="utf-8"))
        events = json.loads((result.session_directory / "events.json").read_text(encoding="utf-8"))
        self.assertEqual(events, [])
        self.assertEqual(metadata["risk_score"], 0)
        self.assertEqual(metadata["event_counts"]["PHONE_DETECTED"], 0)
        self.assertEqual(list((result.session_directory / "evidence").iterdir()), [])

    def test_repeated_sessions_do_not_overwrite_prior_metadata_or_evidence(self):
        first = self.run_session().result
        contents = (first.session_directory / "events.json").read_bytes()
        photo = Path(first.events[0].evidence_path)
        pixels = photo.read_bytes()
        self.now = 100
        second = self.run_session(phone=False).result
        self.assertNotEqual(first.session_directory, second.session_directory)
        self.assertEqual((first.session_directory / "events.json").read_bytes(), contents)
        self.assertEqual(photo.read_bytes(), pixels)
        self.assertEqual(second.risk_score, 0)
        self.assertEqual(second.events, ())

    def test_runtime_camera_failure_saves_partial_session_after_cleanup(self):
        result = self.run_session(camera_failure=True).result
        self.assertIn("Camera disconnected", result.error)
        self.assertTrue((result.session_directory / "session.json").is_file())
        self.assertEqual(len(result.events), 1)
        self.assertTrue(Path(result.events[0].evidence_path).is_file())

    def test_runtime_inference_failure_is_technical_error_and_still_saves_session(self):
        result = self.run_session(detector_failure=True).result
        self.assertIn("AI detection failed", result.error)
        self.assertEqual(result.risk_score, 0)
        self.assertEqual(result.events, ())
        self.assertTrue((result.session_directory / "session.json").exists())

    def test_folder_failure_continues_in_memory_without_shared_fallback_evidence(self):
        store = Mock()
        store.create_session.side_effect = PermissionError("read-only storage")
        worker = self.run_session(store_factory=lambda config: store)
        self.assertIsNone(worker.result.error)
        self.assertIn("Cannot create session", worker.result.persistence_error)
        self.assertIsNone(worker.result.session_directory)
        self.assertEqual(worker.result.risk_score, 30)
        self.assertIsNone(worker.result.events[0].evidence_path)
        self.assertEqual(worker.statuses["Session"][0], "ERROR")
        self.assertEqual(worker.statuses["Evidence"][0], "UNAVAILABLE")
        self.assertFalse((self.root / "legacy-evidence").exists())
        store.save_session.assert_not_called()

    def test_json_save_failure_preserves_risk_history_and_evidence(self):
        store = SessionStore(self.config.storage)
        store.save_session = Mock(side_effect=OSError("disk full"))
        worker = self.run_session(store_factory=lambda config: store)
        self.assertIsNone(worker.result.error)
        self.assertIn("Session save failed", worker.result.persistence_error)
        self.assertEqual(worker.statuses["Session"][0], "ERROR")
        self.assertEqual(worker.result.risk_score, 30)
        self.assertTrue(Path(worker.result.events[0].evidence_path).is_file())

    def test_evidence_initialization_failure_does_not_stop_monitoring_or_scoring(self):
        worker = self.run_session(evidence_factory=Mock(side_effect=OSError("encoder unavailable")))
        self.assertIsNone(worker.result.error)
        self.assertEqual(worker.result.risk_score, 30)
        self.assertIsNone(worker.result.events[0].evidence_path)
        self.assertEqual(worker.statuses["Evidence"][0], "UNAVAILABLE")
        self.assertTrue((worker.result.session_directory / "session.json").is_file())

    def test_runtime_status_packet_contains_all_six_components(self):
        worker = self.run_session()
        packet = worker.take_update()
        self.assertEqual(set(packet.statuses), {"Camera", "AI Detection", "Face Tracking", "Security Monitor", "Evidence", "Session"})
        self.assertTrue(all(state == "ACTIVE" for state, _ in packet.statuses.values()))
        self.assertEqual(worker.statuses["Session"][0], "READY")

    def test_save_finishes_before_worker_returns_and_after_resource_cleanup(self):
        observations = []
        capture, tracker, security = fakes.FakeCapture(frames=1), fakes.FakeFace(), fakes.FakeSecurity()
        store = SessionStore(self.config.storage)
        save = store.save_session

        def checked_save(result):
            observations.append((capture.released, tracker.closed, security.stopped))
            save(result)

        store.save_session = checked_save
        worker = VisionWorker(self.config, camera_opener=lambda cv2, index: (capture, fakes.usable_frame()),
            detector_factory=lambda *args: SimpleDetector(), face_factory=lambda *args: tracker,
            security_factory=lambda: security, store_factory=lambda config: store)
        capture.worker = worker
        worker.run()
        self.assertEqual(observations, [(True, True, True)])
        self.assertTrue((worker.result.session_directory / "events.json").is_file())


class SimpleDetector:
    def detect(self, frame):
        return []


if __name__ == "__main__":
    unittest.main()
