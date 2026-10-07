"""Deterministic event tests: no webcam, models, or real-time sleeps."""

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from monitoring import EventEngine, EventEngineConfig, EventRule, EventType, FrameSignals, Severity


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class EventEngineChecks(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.timestamp = datetime(2026, 10, 7, 14, 32, 5, tzinfo=timezone.utc)
        self.engine = EventEngine(clock=self.clock, wall_clock=lambda: self.timestamp)
        self.phone = FrameSignals(phone_detected=True, phone_confidence=0.91)

    def sample(self, signals, at):
        self.clock.now = at
        return self.engine.update(signals)

    def assert_threshold(self, signals, event_type, threshold):
        self.assertEqual(self.sample(signals, 0), [])
        self.assertEqual(self.sample(signals, threshold - 0.01), [])
        events = self.sample(signals, threshold)
        self.assertEqual([event.type for event in events], [event_type])
        self.assertEqual(events[0].duration, threshold)

    def test_no_phone_below_threshold(self):
        self.assertEqual(self.sample(self.phone, 0), [])
        self.assertEqual(self.sample(self.phone, 0.69), [])
        self.assertEqual(self.engine.history, [])

    def test_phone_after_threshold(self):
        self.assert_threshold(self.phone, EventType.PHONE_DETECTED, 0.7)

    def test_continuous_phone_does_not_spam_even_after_cooldown(self):
        self.sample(self.phone, 0)
        self.assertEqual(len(self.sample(self.phone, 0.7)), 1)
        for at in (0.8, 1.0, 10.7, 20, 100):
            self.assertEqual(self.sample(self.phone, at), [])
        self.assertEqual(len(self.engine.history), 1)

    def test_phone_can_repeat_after_reset_and_cooldown(self):
        self.sample(self.phone, 0)
        self.sample(self.phone, 0.7)
        self.sample(FrameSignals(), 1)
        self.sample(self.phone, 11)
        events = self.sample(self.phone, 12)
        self.assertEqual([event.type for event in events], [EventType.PHONE_DETECTED])
        self.assertEqual(events[0].duration, 1)
        self.assertEqual(len(self.engine.history), 2)

    def test_multiple_person_threshold(self):
        self.assert_threshold(FrameSignals(person_count=2, persons_confidence=0.85), EventType.MULTIPLE_PERSONS, 3.0)

    def test_multiple_face_threshold(self):
        self.assert_threshold(FrameSignals(face_status="MULTIPLE_FACES", face_count=2), EventType.MULTIPLE_FACES, 1.0)

    def test_face_missing_threshold(self):
        self.assert_threshold(FrameSignals(face_status="NO_FACE", face_count=0), EventType.FACE_MISSING, 3.0)

    def test_left_threshold(self):
        self.assert_threshold(FrameSignals(face_status="FACE_DETECTED", head_direction="LEFT"), EventType.LOOK_LEFT, 4.0)

    def test_right_threshold(self):
        self.assert_threshold(FrameSignals(face_status="FACE_DETECTED", head_direction="RIGHT"), EventType.LOOK_RIGHT, 4.0)

    def test_up_threshold(self):
        self.assert_threshold(FrameSignals(face_status="FACE_DETECTED", head_direction="UP"), EventType.LOOK_UP, 4.0)

    def test_down_threshold(self):
        self.assert_threshold(FrameSignals(face_status="FACE_DETECTED", head_direction="DOWN"), EventType.LOOK_DOWN, 4.0)

    def test_center_creates_no_event(self):
        signals = FrameSignals(face_status="FACE_DETECTED", head_direction="CENTER")
        self.assertEqual(self.sample(signals, 0), [])
        self.assertEqual(self.sample(signals, 100), [])

    def test_unknown_creates_no_event(self):
        signals = FrameSignals(face_status="FACE_DETECTED", head_direction="UNKNOWN")
        self.assertEqual(self.sample(signals, 0), [])
        self.assertEqual(self.sample(signals, 100), [])

    def test_reset_before_threshold_prevents_event(self):
        self.sample(self.phone, 0)
        self.sample(self.phone, 0.6)
        self.sample(FrameSignals(), 0.61)
        self.assertEqual(self.sample(self.phone, 1), [])
        self.assertEqual(self.sample(self.phone, 1.69), [])
        self.assertEqual(self.engine.history, [])
        self.assertEqual(len(self.sample(self.phone, 1.8)), 1)

    def test_single_false_frame_resets_each_condition(self):
        for event_type, signals in self.all_conditions():
            with self.subTest(event_type=event_type):
                engine = EventEngine()
                threshold = engine.config.rules[event_type].threshold_seconds
                if event_type == EventType.MULTIPLE_PERSONS:
                    threshold = max(threshold, engine.config.multiple_persons.uncorroborated_persistence_seconds)
                engine.update(signals, now=0)
                engine.update(FrameSignals(), now=threshold / 2)
                self.assertEqual(engine.update(signals, now=threshold), [])
                self.assertEqual(engine.update(signals, now=threshold * 1.9), [])
                self.assertEqual(len(engine.update(signals, now=threshold * 2.1)), 1)

    def test_cooldown_survives_reset_and_emits_at_boundary(self):
        self.sample(self.phone, 0)
        self.sample(self.phone, 0.7)
        self.sample(FrameSignals(), 1)
        self.sample(self.phone, 2)
        self.assertEqual(self.sample(self.phone, 3), [])
        self.assertEqual(self.sample(self.phone, 10.69), [])
        events = self.sample(self.phone, 10.7)
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[0].duration, 8.7)
        self.assertEqual(self.sample(self.phone, 100), [])

    def test_blocked_episode_that_disappears_emits_nothing(self):
        self.sample(self.phone, 0)
        self.sample(self.phone, 0.7)
        self.sample(FrameSignals(), 1)
        self.sample(self.phone, 2)
        self.sample(self.phone, 3)
        self.sample(FrameSignals(), 4)
        self.assertEqual(self.sample(FrameSignals(), 100), [])
        self.assertEqual(len(self.engine.history), 1)

    def test_cooldowns_for_all_types(self):
        for event_type, signals in self.all_conditions():
            with self.subTest(event_type=event_type):
                engine = EventEngine()
                rule = engine.config.rules[event_type]
                first = rule.threshold_seconds
                if event_type == EventType.MULTIPLE_PERSONS:
                    first = max(first, engine.config.multiple_persons.uncorroborated_persistence_seconds)
                engine.update(signals, now=0)
                self.assertEqual(len(engine.update(signals, now=first)), 1)
                engine.update(FrameSignals(), now=first + 0.1)
                engine.update(signals, now=first + 0.2)
                boundary = first + rule.cooldown_seconds
                self.assertEqual(engine.update(signals, now=boundary - 0.01), [])
                self.assertEqual([e.type for e in engine.update(signals, now=boundary)], [event_type])

    def test_direction_change_restarts_tracking(self):
        left = FrameSignals(face_status="FACE_DETECTED", head_direction="LEFT")
        right = replace(left, head_direction="RIGHT")
        self.sample(left, 0)
        self.sample(left, 3.9)
        self.assertEqual(self.sample(right, 4), [])
        self.assertEqual(self.sample(right, 7.9), [])
        self.assertEqual([e.type for e in self.sample(right, 8)], [EventType.LOOK_RIGHT])
        self.assertEqual(self.sample(left, 9), [])
        self.assertEqual([e.type for e in self.sample(left, 13)], [EventType.LOOK_LEFT])

    def test_unavailable_faces_do_not_emit_or_extend_tracking(self):
        self.sample(FrameSignals(face_status="NO_FACE"), 0)
        self.assertEqual(self.sample(FrameSignals(face_status="UNAVAILABLE", head_direction="LEFT"), 10), [])
        self.assertEqual(self.sample(FrameSignals(face_status="NO_FACE"), 11), [])
        self.assertEqual(self.sample(FrameSignals(face_status="NO_FACE"), 13.9), [])
        self.assertEqual([e.type for e in self.sample(FrameSignals(face_status="NO_FACE"), 14)], [EventType.FACE_MISSING])

    def test_head_events_require_single_detected_face(self):
        for status in ("NO_FACE", "MULTIPLE_FACES", "UNAVAILABLE"):
            with self.subTest(status=status):
                engine = EventEngine()
                signals = FrameSignals(face_status=status, head_direction="LEFT")
                engine.update(signals, now=0)
                events = engine.update(signals, now=10)
                self.assertFalse(any(e.type == EventType.LOOK_LEFT for e in events))

    def test_independent_simultaneous_conditions(self):
        signals = FrameSignals(phone_detected=True, phone_confidence=0.91, person_count=3, persons_confidence=0.8, face_status="MULTIPLE_FACES", face_count=2)
        self.sample(signals, 0)
        self.assertEqual([e.type for e in self.sample(signals, 0.7)], [EventType.PHONE_DETECTED])
        self.assertEqual([e.type for e in self.sample(signals, 1)], [EventType.MULTIPLE_PERSONS, EventType.MULTIPLE_FACES])
        self.assertEqual(len(self.engine.history), 3)

    def test_one_person_creates_no_multiple_person_event(self):
        self.sample(FrameSignals(person_count=1), 0)
        self.assertEqual(self.sample(FrameSignals(person_count=1), 100), [])

    def test_event_fields_serialization_and_session_history(self):
        self.sample(self.phone, 0)
        event = self.sample(self.phone, 1.2)[0]
        self.assertEqual(event.timestamp, self.timestamp)
        self.assertEqual(event.severity, Severity.CRITICAL)
        self.assertEqual(event.message, "Cell phone detected")
        self.assertEqual(event.duration, 1.2)
        self.assertEqual(event.confidence, 0.91)
        payload = event.to_dict()
        self.assertEqual(payload["type"], "PHONE_DETECTED")
        self.assertEqual(payload["timestamp"], self.timestamp.isoformat())
        self.assertEqual(payload["severity"], "critical")
        self.assertEqual(json.loads(json.dumps(payload)), payload)
        payload["metadata"]["person_count"] = 999
        self.assertEqual(event.metadata["person_count"], 0)
        self.assertEqual(self.engine.history, [event])
        self.assertEqual(EventEngine().history, [])

    def test_absent_confidence_is_omitted(self):
        signals = FrameSignals(face_status="NO_FACE")
        self.sample(signals, 0)
        event = self.sample(signals, 3)[0]
        self.assertIsNone(event.confidence)
        self.assertNotIn("confidence", event.to_dict())

    def test_default_config_values(self):
        config = EventEngineConfig()
        for event_type in config.rules:
            with self.subTest(event_type=event_type):
                rule = config.rules[event_type]
                look = event_type.value.startswith("LOOK_")
                self.assertEqual(rule.cooldown_seconds, 8 if look else 10)
                self.assertEqual(rule.severity, Severity.MEDIUM if look else Severity.HIGH if event_type in (EventType.FACE_MISSING, EventType.CAMERA_OBSTRUCTED) else Severity.CRITICAL)
        self.assertEqual(config.latest_event_display_seconds, 3)

    def test_custom_threshold_cooldown_and_severity(self):
        rules = dict(EventEngineConfig().rules)
        rules[EventType.PHONE_DETECTED] = EventRule(2, 3, Severity.HIGH, "Custom phone message")
        config = EventEngineConfig(rules=rules)
        engine = EventEngine(config)
        rules.clear()
        engine.update(self.phone, now=0)
        self.assertEqual(engine.update(self.phone, now=1), [])
        event = engine.update(self.phone, now=2)[0]
        self.assertEqual((event.severity, event.message), (Severity.HIGH, "Custom phone message"))
        engine.update(FrameSignals(), now=2.5)
        engine.update(self.phone, now=3)
        self.assertEqual(engine.update(self.phone, now=4.9), [])
        self.assertEqual(len(engine.update(self.phone, now=5)), 1)

    def test_invalid_configuration(self):
        for threshold, cooldown in ((0, 10), (-1, 10), (float("nan"), 10), (1, -1), (1, float("inf"))):
            with self.subTest(threshold=threshold, cooldown=cooldown), self.assertRaises(ValueError):
                EventRule(threshold, cooldown, Severity.HIGH, "Test")
        with self.assertRaises(ValueError):
            EventEngineConfig(rules={})
        with self.assertRaises(ValueError):
            EventEngineConfig(latest_event_display_seconds=0)

    def test_invalid_time_does_not_mutate_state(self):
        self.sample(self.phone, 2)
        for at in (1, float("nan"), float("inf")):
            with self.subTest(at=at), self.assertRaises(ValueError):
                self.sample(FrameSignals(), at)
        self.assertEqual(len(self.sample(self.phone, 3)), 1)

    def test_equal_timestamps_do_not_advance_duration(self):
        for _ in range(20):
            self.assertEqual(self.sample(self.phone, 0), [])
        self.assertEqual(len(self.sample(self.phone, 0.7)), 1)

    @staticmethod
    def all_conditions():
        return [
            (EventType.PHONE_DETECTED, FrameSignals(phone_detected=True, phone_confidence=0.91)),
            (EventType.MULTIPLE_PERSONS, FrameSignals(person_count=2, persons_confidence=0.85)),
            (EventType.MULTIPLE_FACES, FrameSignals(face_status="MULTIPLE_FACES")),
            (EventType.FACE_MISSING, FrameSignals(face_status="NO_FACE")),
            (EventType.CAMERA_OBSTRUCTED, FrameSignals(camera_obstructed=True)),
            *[(EventType[f"LOOK_{direction}"], FrameSignals(face_status="FACE_DETECTED", head_direction=direction)) for direction in ("LEFT", "RIGHT", "UP", "DOWN")],
        ]


if __name__ == "__main__":
    unittest.main()
