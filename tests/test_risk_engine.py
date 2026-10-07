"""Event-based risk scoring and webcam integration, without real-time waits."""

import contextlib
from dataclasses import replace
from datetime import datetime, timezone
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main
from monitoring import (
    EventEngine, EventType, FrameSignals, ProctoringEvent, RiskConfig,
    RiskEngine, RiskLevel, RiskWeight, Severity,
)
from vision.face_tracker import FaceResult, FaceStatus, HeadDirection
from vision.yolo_detector import Detection


class RiskEngineChecks(unittest.TestCase):
    def setUp(self):
        self.risk = RiskEngine()
        self.timestamp = datetime(2026, 10, 7, 14, 32, 5, tzinfo=timezone.utc)

    def event(self, event_type=EventType.PHONE_DETECTED):
        return ProctoringEvent(event_type, self.timestamp, Severity.CRITICAL, 1.2, "Test event")

    def risk_at(self, score):
        weights = dict(RiskConfig().weights)
        weights[EventType.PHONE_DETECTED] = RiskWeight(score)
        risk = RiskEngine(RiskConfig(weights=weights))
        risk.process(self.event())
        return risk

    def test_initial_score_zero(self):
        self.assertEqual(self.risk.current_score, 0)
        self.assertEqual(self.risk.current_level, RiskLevel.LOW)
        self.assertEqual(dict(self.risk.event_counts), {event_type: 0 for event_type in EventType})

    def test_first_phone_adds_30(self):
        event = self.risk.process(self.event())
        self.assertEqual(self.risk.current_score, 30)
        self.assertEqual(event.risk_delta, 30)

    def test_repeated_phone_adds_10(self):
        self.risk.process(self.event())
        event = self.risk.process(self.event())
        self.assertEqual(self.risk.current_score, 40)
        self.assertEqual(event.risk_delta, 10)

    def test_multiple_types_accumulate(self):
        deltas = [self.risk.process(self.event(event_type)).risk_delta for event_type in (
            EventType.PHONE_DETECTED, EventType.FACE_MISSING, EventType.MULTIPLE_FACES,
        )]
        self.assertEqual(deltas, [30, 15, 25])
        self.assertEqual(self.risk.current_score, 70)

    def test_all_default_first_and_repeated_weights(self):
        expected = {
            EventType.PHONE_DETECTED: (30, 10),
            EventType.MULTIPLE_PERSONS: (30, 15),
            EventType.MULTIPLE_FACES: (25, 10),
            EventType.FACE_MISSING: (15, 8),
            EventType.LOOK_LEFT: (8, 8), EventType.LOOK_RIGHT: (8, 8),
            EventType.LOOK_UP: (5, 5), EventType.LOOK_DOWN: (10, 10),
        }
        for event_type, (first, repeated) in expected.items():
            with self.subTest(event_type=event_type):
                risk = RiskEngine()
                self.assertEqual(risk.process(self.event(event_type)).risk_delta, first)
                self.assertEqual(risk.process(self.event(event_type)).risk_delta, repeated)
                self.assertEqual(risk.current_score, first + repeated)

    def test_score_caps_at_100_and_delta_is_actual_increase(self):
        for event_type in (EventType.PHONE_DETECTED, EventType.MULTIPLE_PERSONS, EventType.MULTIPLE_FACES):
            self.risk.process(self.event(event_type))
        self.assertEqual(self.risk.current_score, 85)
        event = self.risk.process(self.event(EventType.MULTIPLE_PERSONS))
        self.assertEqual(self.risk.current_score, 100)
        self.assertEqual(event.risk_delta, 15)
        event = self.risk.process(self.event(EventType.PHONE_DETECTED))
        self.assertEqual(event.risk_delta, 0)
        self.assertEqual(event.to_dict()["risk_delta"], 0)
        self.assertEqual(self.risk.current_score, 100)
        self.assertEqual(self.risk.event_counts[EventType.PHONE_DETECTED], 2)

    def test_partial_delta_at_score_cap(self):
        self.risk = self.risk_at(95)
        event = self.risk.process(self.event())
        self.assertEqual(event.risk_delta, 5)
        self.assertEqual(self.risk.current_score, 100)

    def test_weight_above_100_is_clamped(self):
        risk = self.risk_at(1000)
        self.assertEqual(risk.current_score, 100)

    def test_low_level(self):
        for score in (0, 20):
            with self.subTest(score=score):
                self.assertEqual(self.risk_at(score).current_level, RiskLevel.LOW)

    def test_moderate_level(self):
        for score in (21, 45):
            with self.subTest(score=score):
                self.assertEqual(self.risk_at(score).current_level, RiskLevel.MODERATE)

    def test_high_level(self):
        for score in (46, 70):
            with self.subTest(score=score):
                self.assertEqual(self.risk_at(score).current_level, RiskLevel.HIGH)

    def test_critical_level(self):
        for score in (71, 100):
            with self.subTest(score=score):
                self.assertEqual(self.risk_at(score).current_level, RiskLevel.CRITICAL)

    def test_counts_track_each_type_independently(self):
        for event_type in (EventType.PHONE_DETECTED, EventType.MULTIPLE_PERSONS, EventType.PHONE_DETECTED):
            self.risk.process(self.event(event_type))
        self.assertEqual(self.risk.event_counts[EventType.PHONE_DETECTED], 2)
        self.assertEqual(self.risk.event_counts[EventType.MULTIPLE_PERSONS], 1)
        self.assertEqual(self.risk.event_counts[EventType.FACE_MISSING], 0)

    def test_reset_restores_score_level_counts_and_first_occurrence(self):
        self.risk.process(self.event())
        self.risk.process(self.event(EventType.MULTIPLE_FACES))
        self.risk.reset()
        self.assertEqual(self.risk.current_score, 0)
        self.assertEqual(self.risk.current_level, RiskLevel.LOW)
        self.assertTrue(all(count == 0 for count in self.risk.event_counts.values()))
        self.assertEqual(self.risk.process(self.event()).risk_delta, 30)

    def test_center_and_unknown_produce_no_risk_events(self):
        engine = EventEngine()
        for at, direction in ((0, "CENTER"), (10, "CENTER"), (20, "UNKNOWN"), (100, "UNKNOWN")):
            events = engine.update(FrameSignals(face_status="FACE_DETECTED", head_direction=direction), now=at)
            for event in events:
                self.risk.process(event)
            self.assertEqual(events, [])
        self.assertEqual(self.risk.current_score, 0)
        self.assertTrue(all(count == 0 for count in self.risk.event_counts.values()))

    def test_raw_frames_and_invalid_event_types_cannot_change_score(self):
        with self.assertRaises(TypeError):
            self.risk.process(FrameSignals())
        for invalid_type in ("CENTER", "UNKNOWN", "INVALID"):
            with self.subTest(invalid_type=invalid_type), self.assertRaises(ValueError):
                self.risk.process(replace(self.event(), type=invalid_type))
        self.assertEqual(self.risk.current_score, 0)
        self.assertTrue(all(count == 0 for count in self.risk.event_counts.values()))

    def test_risk_delta_attached_and_other_event_fields_preserved(self):
        original = replace(self.event(), confidence=0.92, metadata={"person_count": 1}, evidence_path="sessions/current/evidence/test.jpg")
        scored = self.risk.process(original)
        self.assertEqual(scored.risk_delta, 30)
        self.assertEqual(scored.to_dict()["risk_delta"], 30)
        self.assertEqual(replace(scored, risk_delta=None), original)
        self.assertIsNone(original.risk_delta)

    def test_custom_weights_copied_and_zero_weight_counts_events(self):
        weights = dict(RiskConfig().weights)
        weights[EventType.PHONE_DETECTED] = RiskWeight(2, 0)
        config = RiskConfig(weights=weights)
        weights.clear()
        risk = RiskEngine(config)
        self.assertEqual(risk.process(self.event()).risk_delta, 2)
        self.assertEqual(risk.process(self.event()).risk_delta, 0)
        self.assertEqual(risk.current_score, 2)
        self.assertEqual(risk.event_counts[EventType.PHONE_DETECTED], 2)

    def test_default_repeated_weight_uses_first_weight(self):
        weights = dict(RiskConfig().weights)
        weights[EventType.PHONE_DETECTED] = RiskWeight(7)
        risk = RiskEngine(RiskConfig(weights=weights))
        self.assertEqual(risk.process(self.event()).risk_delta, 7)
        self.assertEqual(risk.process(self.event()).risk_delta, 7)

    def test_negative_or_noninteger_weights_rejected(self):
        for first, repeated in ((-1, 1), (1, -1), (1.5, 1), (1, float("inf")), (None, 1), (True, 1)):
            with self.subTest(first=first, repeated=repeated), self.assertRaises(ValueError):
                RiskWeight(first, repeated)

    def test_configuration_requires_all_event_weights(self):
        with self.assertRaises(ValueError):
            RiskConfig(weights={})
        weights = dict(RiskConfig().weights)
        weights[EventType.PHONE_DETECTED] = 30
        with self.assertRaises(ValueError):
            RiskConfig(weights=weights)

    def test_level_boundaries_configurable_and_validated(self):
        risk = RiskEngine(RiskConfig(low_max=30, moderate_max=50, high_max=80))
        risk.process(self.event())
        self.assertEqual(risk.current_level, RiskLevel.LOW)
        for boundaries in ((-1, 45, 70), (20, 20, 70), (20, 45, 100), (20.5, 45, 70)):
            with self.subTest(boundaries=boundaries), self.assertRaises(ValueError):
                RiskConfig(low_max=boundaries[0], moderate_max=boundaries[1], high_max=boundaries[2])

    def test_public_score_and_counts_cannot_be_mutated(self):
        with self.assertRaises(AttributeError):
            self.risk.current_score = -1
        with self.assertRaises(TypeError):
            self.risk.event_counts[EventType.PHONE_DETECTED] = 100
        self.assertEqual(self.risk.current_score, 0)


class RiskWebcamChecks(unittest.TestCase):
    def setUp(self):
        monitor = MagicMock()
        monitor.poll.return_value = []
        self.enterContext(patch.object(main, "SecurityMonitor", return_value=monitor))
        self.face = FaceResult(FaceStatus.FACE_DETECTED, 1, HeadDirection.CENTER)
        self.frame = np.zeros((480, 640, 3), np.uint8)
        self.timestamp = datetime(2026, 10, 7, 14, 32, 5, tzinfo=timezone.utc)
        self.phone = [Detection(67, 0.92, (10, 20, 100, 200))]

    def run_loop(self, samples):
        fake_cv2 = MagicMock()
        fake_cv2.waitKey.side_effect = [0] * (len(samples) - 1) + [ord("q")]
        fake_cv2.getWindowProperty.return_value = 1
        camera = MagicMock()
        camera.read.return_value = (True, self.frame)
        detector = MagicMock()
        detector.detect.side_effect = [detections for _, detections, _ in samples]
        tracker = MagicMock()
        tracker.available = True
        tracker.process.side_effect = [face for _, _, face in samples]
        engine = EventEngine(wall_clock=lambda: self.timestamp)
        risk = RiskEngine()
        captured = Mock(side_effect=lambda event, image: replace(event, evidence_path=f"sessions/current/evidence/{event.type.value}.jpg"))
        original_draw = main.draw_preview
        rendered = []

        def render(_cv2, *args, **kwargs):
            with patch.object(cv2, "putText", wraps=cv2.putText) as put_text:
                preview = original_draw(cv2, *args, **kwargs)
            rendered.append([call.args[1] for call in put_text.call_args_list])
            return preview

        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["main.py"]),
            patch.dict(sys.modules, {"cv2": fake_cv2}),
            patch.object(main, "YoloDetector", return_value=detector),
            patch.object(main, "FaceTracker", return_value=tracker),
            patch.object(main, "EventEngine", return_value=engine),
            patch.object(main, "RiskEngine", return_value=risk),
            patch.object(main.EvidenceManager, "capture", side_effect=captured),
            patch.object(main, "open_webcam", return_value=(camera, self.frame)),
            patch.object(main, "draw_preview", side_effect=render),
            patch.object(main, "perf_counter", side_effect=[value for t, _, _ in samples for value in (t, t, t + 0.01)]),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(main.main(), 0)
        self.assertEqual(detector.detect.call_count, len(samples))
        self.assertEqual(tracker.process.call_count, len(samples))
        camera.release.assert_called_once()
        tracker.close.assert_called_once()
        fake_cv2.destroyAllWindows.assert_called_once()
        return risk, engine, captured, rendered, output.getvalue()

    def test_risk_updates_only_on_emitted_events_and_preserves_evidence(self):
        risk, engine, captured, rendered, output = self.run_loop([
            (0, self.phone, self.face), (0.69, self.phone, self.face),
            (1, self.phone, self.face), (20, self.phone, self.face),
        ])
        self.assertEqual(risk.current_score, 30)
        self.assertEqual(risk.event_counts[EventType.PHONE_DETECTED], 1)
        self.assertEqual(len(engine.history), 1)
        self.assertEqual(engine.history[0].risk_delta, 30)
        self.assertIsNotNone(engine.history[0].evidence_path)
        captured.assert_called_once()
        self.assertEqual(captured.call_args.args[0].risk_delta, 30)
        self.assertEqual(captured.call_args.args[1].shape, (580, 640, 3))
        self.assertIn("SESSION RISK: 0 / 100", rendered[0])
        self.assertIn("SESSION RISK: 0 / 100", rendered[1])
        self.assertIn("SESSION RISK: 30 / 100", rendered[2])
        self.assertIn("SESSION RISK: 30 / 100", rendered[3])
        self.assertIn("LEVEL: MODERATE", rendered[3])
        self.assertIn("+30 risk | Session risk: 30/100", output)

    def test_repeat_risk_requires_event_reset_and_cooldown(self):
        risk, engine, captured, rendered, output = self.run_loop([
            (0, self.phone, self.face), (1, self.phone, self.face),
            (2, [], self.face), (3, self.phone, self.face),
            (4, self.phone, self.face), (11, self.phone, self.face),
            (30, self.phone, self.face),
        ])
        self.assertEqual([event.risk_delta for event in engine.history], [30, 10])
        self.assertEqual(risk.current_score, 40)
        self.assertEqual(risk.event_counts[EventType.PHONE_DETECTED], 2)
        self.assertEqual(captured.call_count, 2)
        self.assertIn("SESSION RISK: 30 / 100", rendered[4])
        self.assertIn("SESSION RISK: 40 / 100", rendered[5])
        self.assertIn("+10 risk | Session risk: 40/100", output)

    def test_simultaneous_events_log_individual_running_scores(self):
        multiple = FaceResult(FaceStatus.MULTIPLE_FACES, 2)
        detections = [*self.phone, Detection(0, 0.9, (0, 0, 150, 350)), Detection(0, 0.8, (300, 0, 400, 250))]
        risk, engine, captured, rendered, output = self.run_loop([(0, detections, multiple), (1, detections, multiple)])
        self.assertEqual([event.risk_delta for event in engine.history], [30, 30, 25])
        self.assertEqual(risk.current_score, 85)
        self.assertEqual(risk.current_level, RiskLevel.CRITICAL)
        self.assertEqual(captured.call_count, 3)
        for score in (30, 60, 85):
            self.assertIn(f"Session risk: {score}/100", output)
        self.assertIn("SESSION RISK: 85 / 100", rendered[1])
        self.assertIn("LEVEL: CRITICAL", rendered[1])

    def test_weak_phone_has_no_cli_event_risk_evidence_or_phone_box(self):
        detections = [Detection(67, .39, (350, 100, 450, 220))]
        risk, engine, captured, rendered, _ = self.run_loop([(0, detections, self.face), (1, detections, self.face), (20, detections, self.face)])
        self.assertEqual(risk.current_score, 0)
        self.assertEqual(engine.history, [])
        captured.assert_not_called()
        for labels in rendered:
            self.assertIn("Phone detected: NO", labels)
            self.assertNotIn("cell phone 0.39", labels)

    def test_phone_at_application_floor_keeps_cli_threshold_and_risk(self):
        detections = [Detection(67, .55, (350, 100, 380, 160))]
        samples = [(at, detections, self.face) for at in (0, .69, .7, 20)]
        risk, engine, captured, _, _ = self.run_loop(samples)
        self.assertEqual(risk.current_score, 30)
        self.assertEqual(len(engine.history), 1)
        self.assertEqual(engine.history[0].duration, .7)
        captured.assert_called_once()

    def test_hand_false_person_keeps_cli_count_without_event_risk_or_evidence(self):
        detections = [Detection(0, .91, (100, 0, 350, 470)), Detection(0, .85, (450, 250, 490, 290))]
        risk, engine, captured, rendered, _ = self.run_loop([(0, detections, self.face), (3, detections, self.face), (20, detections, self.face)])
        self.assertEqual(engine.history, [])
        self.assertEqual(risk.current_score, 0)
        captured.assert_not_called()
        for labels in rendered:
            self.assertIn("Persons: 2", labels)
            self.assertIn("person 0.85", labels)

    def test_center_unknown_leave_risk_at_zero_in_preview(self):
        unknown = FaceResult(FaceStatus.FACE_DETECTED, 1, HeadDirection.UNKNOWN)
        risk, engine, captured, rendered, _ = self.run_loop([(0, [], self.face), (10, [], self.face), (20, [], unknown), (100, [], unknown)])
        self.assertEqual(risk.current_score, 0)
        self.assertEqual(engine.history, [])
        captured.assert_not_called()
        for texts in rendered:
            self.assertIn("SESSION RISK: 0 / 100", texts)
            self.assertIn("LEVEL: LOW", texts)

    def test_face_warning_and_risk_coexist(self):
        absent = FaceResult(FaceStatus.NO_FACE, 0)
        risk, engine, captured, rendered, _ = self.run_loop([(0, [], absent), (3, [], absent), (20, [], absent)])
        self.assertEqual(risk.current_score, 15)
        self.assertEqual(engine.history[0].risk_delta, 15)
        captured.assert_called_once()
        self.assertIn("SESSION RISK: 15 / 100", rendered[2])
        self.assertIn(main.FACE_MISSING_WARNING_TEXT, rendered[2])

    def test_console_format_includes_applied_delta_and_session_score(self):
        risk = RiskEngine()
        event = risk.process(ProctoringEvent(EventType.PHONE_DETECTED, self.timestamp, Severity.CRITICAL, 0.82, "Cell phone detected"))
        line = main.format_event(event, risk.current_score)
        self.assertEqual(line, "[14:32:05] CRITICAL | PHONE_DETECTED | Cell phone detected | duration=0.82s | +30 risk | Session risk: 30/100")


if __name__ == "__main__":
    unittest.main()
