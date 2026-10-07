"""Conservative multiple-person qualification using explicit sample times."""

from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from monitoring import EventEngine, EventEngineConfig, EventType, FrameSignals, MultiplePersonsConfig


class MultiplePersonsChecks(unittest.TestCase):
    def setUp(self):
        self.engine = EventEngine()
        self.single_face = FrameSignals(person_count=2, persons_confidence=0.85,
                                         face_status="FACE_DETECTED", face_count=1)
        self.two_faces = replace(self.single_face, face_status="MULTIPLE_FACES", face_count=2)

    def sample(self, signals, now, engine=None):
        return [event for event in (engine or self.engine).update(signals, now=now)
                if event.type == EventType.MULTIPLE_PERSONS]

    def test_weak_secondary_detection_never_emits_multiple_persons(self):
        for confidence in (0.49, 0.59):
            with self.subTest(confidence=confidence):
                engine = EventEngine()
                signals = replace(self.single_face, persons_confidence=confidence)
                self.assertEqual(self.sample(signals, 0, engine), [])
                self.assertEqual(self.sample(signals, 30, engine), [])
                self.assertEqual(engine.history, [])

    def test_short_lived_secondary_detection_does_not_emit(self):
        signals = replace(self.single_face, persons_confidence=0.64)
        self.sample(signals, 0)
        self.assertEqual(self.sample(signals, 2.9), [])
        self.assertEqual(self.sample(replace(signals, person_count=1), 3), [])
        self.assertEqual(self.sample(replace(signals, person_count=1), 20), [])
        self.assertEqual(self.engine.history, [])

    def test_stable_high_confidence_secondary_emits_after_three_seconds(self):
        self.sample(self.single_face, 0)
        self.assertEqual(self.sample(self.single_face, 2.99), [])
        event = self.sample(self.single_face, 3)[0]
        self.assertEqual(event.duration, 3)
        self.assertEqual(event.confidence, 0.85)
        self.assertEqual(event.metadata["multiple_persons_validation"]["path"], "persistent_yolo")

    def test_two_persons_and_two_faces_emit_after_one_second(self):
        signals = replace(self.two_faces, persons_confidence=0.64)
        self.sample(signals, 0)
        self.assertEqual(self.sample(signals, 0.99), [])
        event = self.sample(signals, 1)[0]
        self.assertEqual(event.duration, 1)
        self.assertEqual(event.metadata["multiple_persons_validation"]["path"], "face_corroborated")
        self.assertEqual([event.type for event in self.engine.history],
                         [EventType.MULTIPLE_PERSONS, EventType.MULTIPLE_FACES])

    def test_second_person_without_a_visible_face_can_emit(self):
        self.sample(self.single_face, 0)
        self.assertEqual(self.sample(self.single_face, 1), [])
        self.assertEqual(self.sample(self.single_face, 2.99), [])
        self.assertEqual(len(self.sample(self.single_face, 3)), 1)

    def test_no_visible_faces_do_not_prevent_multiple_persons_event(self):
        signals = replace(self.single_face, face_status="NO_FACE", face_count=0)
        self.sample(signals, 0)
        self.assertEqual(self.sample(signals, 1), [])
        self.assertEqual(len(self.sample(signals, 3)), 1)

    def test_unavailable_tracker_uses_longer_yolo_persistence(self):
        signals = replace(self.single_face, face_status="UNAVAILABLE", face_count=None)
        self.sample(signals, 0)
        self.assertEqual(self.sample(signals, 1), [])
        self.assertEqual(len(self.sample(signals, 3)), 1)

    def test_one_person_remains_unaffected(self):
        signals = replace(self.single_face, person_count=1, persons_confidence=1.0)
        self.sample(signals, 0)
        self.assertEqual(self.sample(signals, 100), [])
        self.assertEqual(self.engine.history, [])

    def test_minimum_confidence_boundary_is_inclusive(self):
        signals = replace(self.single_face, persons_confidence=0.60)
        self.sample(signals, 0)
        self.assertEqual(len(self.sample(signals, 3)), 1)

    def test_missing_or_invalid_confidence_cannot_qualify(self):
        for confidence in (None, float("nan"), float("inf"), -float("inf"), -1, 1.1):
            with self.subTest(confidence=confidence):
                engine = EventEngine()
                signals = replace(self.two_faces, persons_confidence=confidence)
                self.sample(signals, 0, engine)
                self.assertEqual(self.sample(signals, 100, engine), [])

    def test_weak_frame_resets_secondary_persistence(self):
        self.sample(self.single_face, 0)
        self.assertEqual(self.sample(self.single_face, 2.9), [])
        self.sample(replace(self.single_face, persons_confidence=0.59), 2.99)
        self.sample(self.single_face, 3)
        self.assertEqual(self.sample(self.single_face, 5.99), [])
        event = self.sample(self.single_face, 6)[0]
        self.assertEqual(event.duration, 3)

    def test_second_face_flicker_does_not_bypass_longer_persistence(self):
        self.sample(self.single_face, 0)
        self.assertEqual(self.sample(self.two_faces, 1), [])
        self.assertEqual(self.sample(self.two_faces, 1.8), [])
        self.assertEqual(self.sample(self.single_face, 1.9), [])
        self.assertEqual(self.sample(self.single_face, 2.99), [])
        self.assertEqual(len(self.sample(self.single_face, 3)), 1)

    def test_late_face_corroboration_needs_its_own_continuous_duration(self):
        self.sample(self.single_face, 0)
        self.assertEqual(self.sample(self.two_faces, 1), [])
        self.assertEqual(self.sample(self.two_faces, 1.99), [])
        event = self.sample(self.two_faces, 2)[0]
        self.assertEqual(event.duration, 2)
        self.assertEqual(event.metadata["multiple_persons_validation"]["face_corroboration_duration"], 1)

    def test_corroboration_loss_does_not_reset_yolo_only_duration(self):
        self.sample(self.single_face, 0)
        self.sample(self.two_faces, 0.5)
        self.sample(self.single_face, 1.4)
        self.assertEqual(self.sample(self.single_face, 2.99), [])
        event = self.sample(self.single_face, 3)[0]
        self.assertEqual(event.duration, 3)

    def test_unavailable_or_inconsistent_face_state_never_shortens_wait(self):
        for status, count in (("UNAVAILABLE", 2), ("FACE_DETECTED", 2), ("MULTIPLE_FACES", None), ("MULTIPLE_FACES", 1)):
            with self.subTest(status=status, count=count):
                engine = EventEngine()
                signals = replace(self.single_face, face_status=status, face_count=count)
                self.sample(signals, 0, engine)
                self.assertEqual(self.sample(signals, 1, engine), [])
                self.assertEqual(len(self.sample(signals, 3, engine)), 1)

    def test_two_faces_do_not_override_secondary_confidence_floor(self):
        signals = replace(self.two_faces, persons_confidence=0.49)
        self.sample(signals, 0)
        self.assertEqual(self.sample(signals, 100), [])

    def test_continuous_valid_detection_keeps_existing_one_event_latch(self):
        self.sample(self.two_faces, 0)
        self.assertEqual(len(self.sample(self.two_faces, 1)), 1)
        self.assertEqual(self.sample(self.single_face, 2), [])
        self.assertEqual(self.sample(self.single_face, 30), [])
        self.assertEqual(sum(event.type == EventType.MULTIPLE_PERSONS for event in self.engine.history), 1)

    def test_cooldown_survives_reset_and_repeats_at_boundary(self):
        self.sample(self.single_face, 0)
        self.sample(self.single_face, 3)
        self.sample(replace(self.single_face, person_count=1), 4)
        self.sample(self.single_face, 5)
        self.assertEqual(self.sample(self.single_face, 12.99), [])
        self.assertEqual(len(self.sample(self.single_face, 13)), 1)

    def test_confidence_and_both_persistence_durations_are_configurable(self):
        config = EventEngineConfig(multiple_persons=MultiplePersonsConfig(0.7, 5, 2))
        for confidence, faces, threshold in ((0.69, True, None), (0.75, True, 2), (0.75, False, 5)):
            with self.subTest(confidence=confidence, faces=faces):
                engine = EventEngine(config)
                signals = replace(self.two_faces if faces else self.single_face, persons_confidence=confidence)
                self.sample(signals, 0, engine)
                if threshold is None:
                    self.assertEqual(self.sample(signals, 100, engine), [])
                else:
                    self.assertEqual(self.sample(signals, threshold - 0.01, engine), [])
                    self.assertEqual(len(self.sample(signals, threshold, engine)), 1)

    def test_original_event_rule_is_still_a_minimum_duration(self):
        rules = dict(EventEngineConfig().rules)
        rules[EventType.MULTIPLE_PERSONS] = replace(rules[EventType.MULTIPLE_PERSONS], threshold_seconds=5)
        engine = EventEngine(EventEngineConfig(rules=rules))
        self.sample(self.two_faces, 0, engine)
        self.assertEqual(self.sample(self.two_faces, 4.99, engine), [])
        self.assertEqual(len(self.sample(self.two_faces, 5, engine)), 1)

    def test_phone_event_is_unaffected_by_weak_secondary_person(self):
        signals = replace(self.single_face, persons_confidence=0.49, phone_detected=True, phone_confidence=0.91)
        self.engine.update(signals, now=0)
        emitted = self.engine.update(signals, now=0.7)
        self.assertEqual([event.type for event in emitted], [EventType.PHONE_DETECTED])
        self.assertEqual(emitted[0].confidence, 0.91)
        self.assertEqual(self.sample(signals, 100), [])

    def test_invalid_configuration_is_rejected(self):
        for confidence in (0, -1, 1.1, float("nan"), float("inf")):
            with self.subTest(confidence=confidence), self.assertRaises(ValueError):
                MultiplePersonsConfig(minimum_secondary_confidence=confidence)
        for value in (0, -1, float("nan"), float("inf")):
            for name in ("uncorroborated_persistence_seconds", "corroborated_persistence_seconds"):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    MultiplePersonsConfig(**{name: value})
        with self.assertRaises(ValueError):
            MultiplePersonsConfig(uncorroborated_persistence_seconds=1, corroborated_persistence_seconds=2)
        with self.assertRaises(ValueError):
            EventEngineConfig(multiple_persons=None)


if __name__ == "__main__":
    unittest.main()
