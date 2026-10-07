"""Phone confidence and secondary-person geometry without models/camera."""

from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main
from monitoring import EventEngine, EventEngineConfig, EventType, MultiplePersonsConfig, PhoneDetectionConfig, RiskEngine
from vision.detection_filters import display_detections, secondary_person_confidence
from vision.face_tracker import FaceResult, FaceStatus
from vision.yolo_detector import Detection


class DetectionFilterChecks(unittest.TestCase):
    def setUp(self):
        self.frame = np.zeros((480, 640, 3), np.uint8)
        self.face = FaceResult(FaceStatus.FACE_DETECTED, 1)
        self.primary = Detection(0, .91, (100, 0, 350, 470))
        self.secondary = Detection(0, .85, (400, 30, 620, 450))
        self.hand = Detection(0, .85, (450, 250, 490, 290))
        self.config = EventEngineConfig()

    def sample(self, detections, *, face=None, config=None, times=(0, 3)):
        config = config or self.config
        engine = EventEngine(config)
        for time in times:
            signals = main.frame_signals(detections, face or self.face, self.frame.shape, config)
            engine.update(signals, now=time)
        return engine

    def test_low_confidence_chair_phone_never_triggers_or_increases_risk(self):
        detections = [self.primary, Detection(67, .39, (380, 180, 450, 260))]
        engine = self.sample(detections, times=(0, .7, 3, 100))
        self.assertEqual(engine.history, [])
        risk = RiskEngine()
        for event in engine.history:
            risk.process(event)
        self.assertEqual(risk.current_score, 0)
        signals = main.frame_signals(detections, self.face, self.frame.shape)
        self.assertFalse(signals.phone_detected)
        self.assertIsNone(signals.phone_confidence)

    def test_real_phone_above_floor_keeps_point_seven_second_threshold(self):
        detections = [self.primary, Detection(67, .65, (380, 180, 410, 240))]
        signals = main.frame_signals(detections, self.face, self.frame.shape)
        engine = EventEngine()
        self.assertEqual(engine.update(signals, now=0), [])
        self.assertEqual(engine.update(signals, now=.69), [])
        event = engine.update(signals, now=.7)[0]
        self.assertEqual(event.type, EventType.PHONE_DETECTED)
        self.assertEqual((event.duration, event.confidence), (.7, .65))

    def test_weak_phone_box_is_not_drawn_but_person_box_is_preserved(self):
        detections = [self.primary, Detection(67, .39, (380, 180, 450, 260))]
        with patch.object(cv2, "putText", wraps=cv2.putText) as text, patch.object(cv2, "rectangle", wraps=cv2.rectangle) as boxes:
            main.draw_preview(cv2, self.frame, detections, 20, self.face)
        labels = [call.args[1] for call in text.call_args_list]
        self.assertIn("person 0.91", labels)
        self.assertIn("Phone detected: NO", labels)
        self.assertFalse(any(label.startswith("cell phone") for label in labels))
        boxes.assert_called_once()

    def test_phone_threshold_boundary_and_custom_threshold(self):
        for confidence, floor, expected in ((.5499, .55, False), (.55, .55, True), (.52, .5, True), (.65, .7, False)):
            with self.subTest(confidence=confidence, floor=floor):
                config = replace(self.config, phone=PhoneDetectionConfig(floor))
                detections = [Detection(67, confidence, (20, 20, 40, 50))]
                signals = main.frame_signals(detections, self.face, self.frame.shape, config)
                self.assertEqual(signals.phone_detected, expected)
                filtered = display_detections(detections, config.phone)
                self.assertEqual(bool(filtered), expected)
                with patch.object(cv2, "putText", wraps=cv2.putText) as text:
                    main.draw_preview(cv2, self.frame.copy(), detections, 20, self.face, event_config=config)
                self.assertEqual(any(call.args[1].startswith("cell phone") for call in text.call_args_list), expected)

    def test_engine_also_rejects_unfiltered_low_confidence_phone_signals(self):
        from monitoring import FrameSignals
        for confidence in (.39, .54, None, float("nan"), float("inf"), 1.1, -1):
            with self.subTest(confidence=confidence):
                engine = EventEngine()
                signals = FrameSignals(phone_detected=True, phone_confidence=confidence)
                engine.update(signals, now=0)
                self.assertEqual(engine.update(signals, now=100), [])

    def test_phone_confidence_dip_resets_duration_and_preserves_cooldown(self):
        phone = Detection(67, .75, (380, 180, 410, 240))
        strong = main.frame_signals([phone], self.face, self.frame.shape)
        weak = main.frame_signals([replace(phone, confidence=.39)], self.face, self.frame.shape)
        engine = EventEngine()
        engine.update(strong, now=0)
        engine.update(weak, now=.6)
        engine.update(strong, now=1)
        self.assertEqual(engine.update(strong, now=1.69), [])
        self.assertEqual(len(engine.update(strong, now=1.8)), 1)
        self.assertEqual(engine.update(strong, now=30), [])
        engine.update(weak, now=31)
        engine.update(strong, now=32)
        self.assertEqual(len(engine.update(strong, now=33)), 1)

    def test_small_hand_like_secondary_does_not_trigger_multiple_persons(self):
        engine = self.sample([self.primary, self.hand], times=(0, 3, 100))
        self.assertEqual(engine.history, [])
        signals = main.frame_signals([self.primary, self.hand], self.face, self.frame.shape)
        self.assertEqual(signals.person_count, 2)
        self.assertIsNone(signals.persons_confidence)

    def test_real_secondary_person_emits_after_existing_longer_persistence(self):
        engine = self.sample([self.primary, self.secondary], times=(0, 2.99, 3))
        self.assertEqual([event.type for event in engine.history], [EventType.MULTIPLE_PERSONS])
        self.assertEqual(engine.history[0].duration, 3)
        self.assertEqual(engine.history[0].confidence, .85)

    def test_partially_visible_person_still_emits_when_visible_size_is_sufficient(self):
        partial = Detection(0, .75, (565, 120, 730, 300))
        engine = self.sample([self.primary, partial])
        self.assertEqual([event.type for event in engine.history], [EventType.MULTIPLE_PERSONS])
        self.assertEqual(engine.history[0].confidence, .75)

    def test_two_faces_keep_shorter_corroborated_persistence(self):
        engine = self.sample([self.primary, self.secondary], face=FaceResult(FaceStatus.MULTIPLE_FACES, 2), times=(0, .99, 1))
        events = [event for event in engine.history if event.type == EventType.MULTIPLE_PERSONS]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].duration, 1)

    def test_two_faces_cannot_override_invalid_secondary_geometry(self):
        engine = self.sample([self.primary, self.hand], face=FaceResult(FaceStatus.MULTIPLE_FACES, 2), times=(0, 1, 100))
        self.assertFalse(any(event.type == EventType.MULTIPLE_PERSONS for event in engine.history))
        self.assertTrue(any(event.type == EventType.MULTIPLE_FACES for event in engine.history))

    def test_secondary_confidence_floor_remains_point_six(self):
        engine = self.sample([self.primary, replace(self.secondary, confidence=.59)], times=(0, 3, 100))
        self.assertEqual(engine.history, [])

    def test_primary_person_is_exempt_from_secondary_geometry_rules(self):
        geometry = MultiplePersonsConfig(minimum_secondary_area_ratio=.9,
                                          minimum_secondary_width_ratio=.9, minimum_secondary_height_ratio=.9)
        config = replace(self.config, multiple_persons=geometry)
        detections = [Detection(0, .91, (100, 100, 150, 180))]
        signals = main.frame_signals(detections, self.face, self.frame.shape, config)
        self.assertEqual(signals.person_count, 1)
        self.assertEqual(display_detections(detections, config.phone), detections)
        with patch.object(cv2, "putText", wraps=cv2.putText) as text:
            main.draw_preview(cv2, self.frame, detections, 20, self.face, event_config=config)
        self.assertIn("person 0.91", [call.args[1] for call in text.call_args_list])

    def test_higher_confidence_hand_cannot_take_primary_geometry_exemption(self):
        detections = [replace(self.hand, confidence=.99), self.primary]
        self.assertEqual(self.sample(detections).history, [])

    def test_invalid_hand_does_not_hide_a_valid_third_person(self):
        detections = [self.primary, replace(self.hand, confidence=.99), self.secondary]
        engine = self.sample(detections)
        self.assertEqual([event.type for event in engine.history], [EventType.MULTIPLE_PERSONS])
        self.assertEqual(engine.history[0].confidence, .85)
        self.assertEqual(engine.history[0].metadata["person_count"], 3)

    def test_offscreen_bbox_uses_visible_area_and_rejects_invalid_extents(self):
        for box in ((630, 120, 1000, 470), (400, 120, 390, 300), (-500, -500, -10, -10), (400, 100, 400, 300)):
            with self.subTest(box=box):
                secondary = replace(self.secondary, xyxy=box)
                engine = self.sample([self.primary, secondary])
                self.assertEqual(engine.history, [])

    def test_each_geometry_threshold_is_configurable(self):
        secondary = Detection(0, .85, (400, 120, 480, 220))  # 2.6% area, 12.5% width, 20.8% height.
        self.assertEqual(len(self.sample([self.primary, secondary]).history), 1)
        for field, threshold in (("minimum_secondary_area_ratio", .04), ("minimum_secondary_width_ratio", .2),
                                 ("minimum_secondary_height_ratio", .3)):
            with self.subTest(field=field):
                config = replace(self.config, multiple_persons=replace(self.config.multiple_persons, **{field: threshold}))
                self.assertEqual(self.sample([self.primary, secondary], config=config).history, [])

    def test_geometry_scales_with_resolution(self):
        detections = [self.primary, self.secondary]
        config = self.config.multiple_persons
        confidence = secondary_person_confidence(detections, (480, 640, 3), config)
        scaled = [replace(d, xyxy=tuple(value * 2 for value in d.xyxy)) for d in detections]
        self.assertEqual(secondary_person_confidence(scaled, (960, 1280, 3), config), confidence)

    def test_geometry_rejection_resets_secondary_person_persistence(self):
        strong = main.frame_signals([self.primary, self.secondary], self.face, self.frame.shape)
        hand = main.frame_signals([self.primary, self.hand], self.face, self.frame.shape)
        engine = EventEngine()
        engine.update(strong, now=0)
        engine.update(hand, now=2.9)
        engine.update(strong, now=3)
        self.assertEqual(engine.update(strong, now=5.99), [])
        event = engine.update(strong, now=6)[0]
        self.assertEqual((event.type, event.duration), (EventType.MULTIPLE_PERSONS, 3))

    def test_geometry_boundaries_are_inclusive_and_can_be_disabled(self):
        # Exactly 5% frame width and 2% frame area.
        secondary = Detection(0, .85, (400, 10, 432, 202))
        self.assertEqual(len(self.sample([self.primary, secondary]).history), 1)
        # Exactly 15% frame height, with enough width/area.
        secondary = replace(secondary, xyxy=(400, 10, 496, 82))
        self.assertEqual(len(self.sample([self.primary, secondary]).history), 1)
        config = replace(self.config, multiple_persons=replace(self.config.multiple_persons,
                         minimum_secondary_area_ratio=0, minimum_secondary_width_ratio=0,
                         minimum_secondary_height_ratio=0))
        self.assertEqual(len(self.sample([self.primary, self.hand], config=config).history), 1)

    def test_small_hand_boxes_are_retained_for_normal_person_display(self):
        detections = [self.primary, self.hand]
        with patch.object(cv2, "putText", wraps=cv2.putText) as text, patch.object(cv2, "rectangle", wraps=cv2.rectangle) as boxes:
            main.draw_preview(cv2, self.frame, detections, 20, self.face)
        self.assertIn("Persons: 2", [call.args[1] for call in text.call_args_list])
        self.assertEqual(boxes.call_count, 2)
        self.assertEqual(display_detections(detections, self.config.phone), detections)

    def test_defaults_and_cli_overrides_share_configuration(self):
        with patch.object(sys, "argv", ["main.py"]):
            config = main.event_config_from_args(main.parse_args())
        self.assertEqual(config.phone.minimum_confidence, .55)
        self.assertEqual(config.multiple_persons.minimum_secondary_area_ratio, .02)
        with patch.object(sys, "argv", ["desktop.py", "--phone-conf", ".65", "--secondary-person-min-area", ".03",
                                         "--secondary-person-min-width", ".1", "--secondary-person-min-height", ".2"]):
            config = main.event_config_from_args(main.parse_args())
        self.assertEqual(config.phone.minimum_confidence, .65)
        self.assertEqual(config.multiple_persons.minimum_secondary_area_ratio, .03)
        self.assertEqual(config.multiple_persons.minimum_secondary_width_ratio, .1)
        self.assertEqual(config.multiple_persons.minimum_secondary_height_ratio, .2)

    def test_invalid_filter_configuration_is_rejected(self):
        for value in (0, -1, 1.1, float("nan"), float("inf")):
            with self.subTest(phone=value), self.assertRaises(ValueError):
                PhoneDetectionConfig(value)
        for field in ("minimum_secondary_area_ratio", "minimum_secondary_width_ratio", "minimum_secondary_height_ratio"):
            for value in (-1, 1.1, float("nan"), float("inf")):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    MultiplePersonsConfig(**{field: value})
        with self.assertRaises(ValueError):
            EventEngineConfig(phone=None)
        with self.assertRaises(ValueError):
            secondary_person_confidence([self.primary, self.secondary], (0, 640, 3), self.config.multiple_persons)


if __name__ == "__main__":
    unittest.main()
