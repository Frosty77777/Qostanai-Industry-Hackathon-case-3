"""Temporal phone regressions: fake time/boxes, no camera or OS key presses."""

import contextlib
from dataclasses import replace
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from main import draw_preview, frame_signals
from monitoring import EventEngine, EventEngineConfig, EventType, PhoneDetectionConfig, RiskEngine
from vision.face_tracker import FaceResult, FaceStatus
from vision.phone_tracker import PhoneState, PhoneTracker
from vision.yolo_detector import Detection

PHONE = Detection(67, .82, (250, 200, 300, 300))
PRESENT = FaceResult(FaceStatus.FACE_DETECTED, 1)
SHAPE = (480, 640, 3)


class PhoneTrackingChecks(unittest.TestCase):
    def setUp(self):
        self.config = EventEngineConfig()
        self.tracker = PhoneTracker(self.config.phone)
        self.engine = EventEngine(self.config)

    def sample(self, now, detections):
        tracked = self.tracker.update(detections, SHAPE, now=now)
        signals = tracked.apply_to(frame_signals(detections, PRESENT, SHAPE, self.config))
        return tracked, self.engine.update(signals, now=now)

    def test_initial_state_is_none(self):
        self.assertEqual(self.tracker.current.state, PhoneState.NONE)
        self.assertFalse(self.tracker.current.probable_phone)

    def test_confident_observation_establishes_detected_track(self):
        tracked, events = self.sample(0, [PHONE])
        self.assertEqual(tracked.state, PhoneState.DETECTED)
        self.assertEqual(tracked.confidence, .82)
        self.assertIsNotNone(tracked.track_id)
        self.assertEqual(tracked.confident_hits, 1)
        self.assertEqual(events, [])

    def test_weak_chair_false_phone_cannot_establish_track(self):
        for confidence in (.39, .54, None, float("nan"), float("inf"), 1.1, -1):
            with self.subTest(confidence=confidence):
                tracker = PhoneTracker()
                result = tracker.update([replace(PHONE, confidence=confidence)], SHAPE, now=0)
                self.assertEqual(result.state, PhoneState.NONE)
                self.assertIsNone(result.track_id)

    def test_two_observations_retain_probable_phone_through_partial_occlusion(self):
        self.sample(0, [PHONE])
        seen, _ = self.sample(.2, [PHONE])
        occluded, events = self.sample(.5, [])
        self.assertEqual(occluded.state, PhoneState.TRACKED_OCCLUDED)
        self.assertEqual(occluded.track_id, seen.track_id)
        self.assertEqual(occluded.observed_duration_seconds, .2)
        self.assertTrue(occluded.probable_phone)
        self.assertEqual(events, [])

    def test_one_isolated_confident_box_does_not_survive_as_ghost_phone(self):
        self.sample(0, [PHONE])
        hidden, events = self.sample(.1, [])
        self.assertEqual(hidden.state, PhoneState.NONE)
        self.assertEqual(events, [])

    def test_long_disappearance_clears_track(self):
        self.sample(0, [PHONE])
        self.sample(.1, [PHONE])
        self.assertEqual(self.sample(.5, [])[0].state, PhoneState.TRACKED_OCCLUDED)
        result, events = self.sample(.701, [])
        self.assertEqual(result.state, PhoneState.NONE)
        self.assertIsNone(result.track_id)
        self.assertIsNone(result.confidence)
        self.assertEqual(events, [])
        self.assertEqual(self.engine.condition_duration(EventType.PHONE_DETECTED), 0)

    def test_weak_box_does_not_extend_grace_refresh_hits_or_duration(self):
        self.sample(0, [PHONE])
        visible, _ = self.sample(.1, [PHONE])
        weak, _ = self.sample(.5, [replace(PHONE, confidence=.39)])
        self.assertEqual(weak.state, PhoneState.TRACKED_OCCLUDED)
        self.assertEqual(weak.confident_hits, visible.confident_hits)
        self.assertEqual(weak.observed_duration_seconds, visible.observed_duration_seconds)
        self.assertEqual(self.sample(.701, [replace(PHONE, confidence=.39)])[0].state, PhoneState.NONE)

    def test_prediction_never_emits_violation_when_duration_reaches_threshold(self):
        self.sample(0, [PHONE])
        self.sample(.2, [PHONE])
        tracked, events = self.sample(.7, [])
        self.assertEqual(tracked.state, PhoneState.TRACKED_OCCLUDED)
        self.assertEqual(events, [])
        self.assertEqual(self.engine.history, [])

    def test_returning_detection_cannot_count_occlusion_as_observed_duration(self):
        self.sample(0, [PHONE])
        self.sample(.2, [PHONE])
        self.sample(.5, [])
        tracked, events = self.sample(.7, [PHONE])
        self.assertEqual(tracked.state, PhoneState.DETECTED)
        self.assertEqual(tracked.observed_duration_seconds, .2)
        self.assertEqual(events, [])
        _, events = self.sample(1.3, [PHONE])
        self.assertEqual([event.type for event in events], [EventType.PHONE_DETECTED])
        self.assertAlmostEqual(events[0].metadata["phone_tracking"]["observed_duration_seconds"], .8)
        self.assertEqual(events[0].duration, 1.3)

    def test_visible_phone_keeps_existing_point_seven_second_threshold(self):
        self.sample(0, [PHONE])
        self.assertEqual(self.sample(.69, [PHONE])[1], [])
        events = self.sample(.7, [PHONE])[1]
        self.assertEqual([event.type for event in events], [EventType.PHONE_DETECTED])
        self.assertEqual(events[0].confidence, .82)

    def test_tracking_continuity_does_not_spam_events_after_emission(self):
        self.sample(0, [PHONE])
        self.sample(.8, [PHONE])
        for time, boxes in ((.9, []), (1.2, [PHONE]), (1.3, []), (1.5, [PHONE]), (20, [PHONE])):
            self.assertEqual(self.sample(time, boxes)[1], [])
        self.assertEqual(len(self.engine.history), 1)
        risk = RiskEngine()
        for event in self.engine.history:
            risk.process(event)
        self.assertEqual(risk.current_score, 30)

    def test_new_episode_preserves_original_event_cooldown(self):
        self.sample(0, [PHONE])
        self.sample(.8, [PHONE])
        self.sample(1.5, [])
        self.sample(2, [PHONE])
        self.assertEqual(self.sample(3, [PHONE])[1], [])
        self.assertEqual(len(self.sample(10.8, [PHONE])[1]), 1)
        self.assertEqual(len(self.engine.history), 2)

    def test_distant_box_starts_new_identity_and_resets_episode(self):
        first, _ = self.sample(0, [PHONE])
        second, events = self.sample(.6, [replace(PHONE, xyxy=(500, 100, 550, 200))])
        self.assertNotEqual(first.track_id, second.track_id)
        self.assertEqual(second.observed_duration_seconds, 0)
        self.assertEqual(events, [])
        self.assertEqual(self.engine.condition_duration(EventType.PHONE_DETECTED), 0)
        self.assertEqual(self.sample(.8, [replace(PHONE, xyxy=(500, 100, 550, 200))])[1], [])

    def test_implausible_size_change_starts_new_track(self):
        first, _ = self.sample(0, [PHONE])
        second, _ = self.sample(.2, [replace(PHONE, xyxy=(220, 100, 380, 400))])
        self.assertNotEqual(first.track_id, second.track_id)

    def test_small_bbox_motion_retains_identity(self):
        first, _ = self.sample(0, [PHONE])
        second, _ = self.sample(.2, [replace(PHONE, xyxy=(260, 205, 310, 305))])
        self.assertEqual(first.track_id, second.track_id)

    def test_associated_phone_wins_over_unrelated_higher_confidence_box(self):
        first, _ = self.sample(0, [PHONE])
        unrelated = replace(PHONE, confidence=.99, xyxy=(500, 100, 550, 200))
        second, _ = self.sample(.2, [unrelated, PHONE])
        self.assertEqual(first.track_id, second.track_id)
        self.assertEqual(second.bbox, tuple(PHONE.xyxy))

    def test_invalid_and_fully_offscreen_boxes_cannot_establish_track(self):
        for box in ((20, 20, 10, 30), (20, 20, 20, 40), (-50, -50, -10, -10),
                    (0, 0, float("nan"), 30), (650, 10, 700, 50)):
            with self.subTest(box=box):
                self.assertEqual(PhoneTracker().update([replace(PHONE, xyxy=box)], SHAPE, now=0).state,
                                 PhoneState.NONE)

    def test_time_validation_does_not_mutate_current_track(self):
        self.sample(1, [PHONE])
        original = self.tracker.current
        for time in (float("nan"), float("inf"), .9):
            with self.subTest(time=time), self.assertRaises(ValueError):
                self.tracker.update([], SHAPE, now=time)
            self.assertEqual(self.tracker.current, original)

    def test_injected_clock_is_supported(self):
        tracker = PhoneTracker(clock=lambda: 2)
        self.assertEqual(tracker.update([PHONE], SHAPE).state, PhoneState.DETECTED)

    def test_invalid_frame_dimensions_fail_before_mutating_track(self):
        self.sample(0, [PHONE])
        original = self.tracker.current
        for shape in ((0, 640), (480, -1), (), (float("inf"), 640)):
            with self.subTest(shape=shape), self.assertRaises(ValueError):
                self.tracker.update([], shape, now=.1)
            self.assertEqual(self.tracker.current, original)

    def test_grace_and_confirmation_hits_are_configurable(self):
        tracker = PhoneTracker(PhoneDetectionConfig(occlusion_grace_seconds=1.5, minimum_tracking_hits=3))
        for time in (0, .1, .2):
            tracker.update([PHONE], SHAPE, now=time)
        self.assertEqual(tracker.update([], SHAPE, now=1.5).state, PhoneState.TRACKED_OCCLUDED)
        self.assertEqual(tracker.update([], SHAPE, now=1.701).state, PhoneState.NONE)

    def test_disabled_tracking_never_holds_occluded_state(self):
        tracker = PhoneTracker(PhoneDetectionConfig(tracking_enabled=False))
        tracker.update([PHONE], SHAPE, now=0)
        tracker.update([PHONE], SHAPE, now=.1)
        self.assertEqual(tracker.update([], SHAPE, now=.2).state, PhoneState.NONE)

    def test_invalid_tracking_configuration_is_rejected(self):
        invalid = ({"tracking_enabled": 1}, {"occlusion_grace_seconds": -1},
                   {"occlusion_grace_seconds": float("nan")}, {"minimum_tracking_hits": 1},
                   {"minimum_tracking_hits": True}, {"minimum_tracking_hits": 2.5},
                   {"minimum_association_iou": 0}, {"minimum_association_iou": float("inf")},
                   {"maximum_center_distance_ratio": -1}, {"maximum_center_distance_ratio": 2},
                   {"maximum_area_ratio": .9}, {"maximum_area_ratio": float("nan")})
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                PhoneDetectionConfig(**fields)

    def test_tracker_adapter_preserves_person_face_head_and_break_signals(self):
        person = Detection(0, .95, (20, 20, 100, 400))
        signals = replace(frame_signals([person], PRESENT, SHAPE), authorized_break=True, camera_obstructed=True)
        updated = self.tracker.update([PHONE], SHAPE, now=0).apply_to(signals)
        self.assertEqual((updated.person_count, updated.face_status, updated.face_count, updated.head_direction),
                         (signals.person_count, signals.face_status, signals.face_count, signals.head_direction))
        self.assertTrue(updated.authorized_break)
        self.assertTrue(updated.camera_obstructed)

    def test_raw_signal_adapter_still_has_no_tracking_requirement(self):
        signals = frame_signals([PHONE], PRESENT, SHAPE)
        self.assertIsNone(signals.phone_tracking_state)
        self.engine.update(signals, now=0)
        self.assertEqual(len(self.engine.update(signals, now=.7)), 1)


class PhoneTrackingWorkerChecks(unittest.TestCase):
    def monitor(self, times, boxes, *, tracking_enabled=True):
        from evidence import EvidenceConfig, EvidenceManager
        from session_storage import SessionStorageConfig
        from test_desktop import APP, FakeFace, FakeSecurity, usable_frame
        from ui.session import SessionConfig
        from ui.vision_worker import VisionWorker

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = SessionConfig(evidence=EvidenceConfig(directory=Path(directory.name) / "evidence"),
                               storage=SessionStorageConfig(directory=Path(directory.name) / "sessions"),
                               events=replace(EventEngineConfig(), phone=PhoneDetectionConfig(tracking_enabled=tracking_enabled)))
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
            return boxes[state["index"]]

        def encode(frame, quality):
            self.snapshots.append(frame.copy())
            return b"fake JPEG"

        capture.read.side_effect = read
        detector.detect.side_effect = detect
        self.worker = VisionWorker(config, clock=lambda: state["now"],
                                   camera_opener=lambda cv2, index: (capture, usable_frame()),
                                   detector_factory=lambda *args: detector,
                                   face_factory=lambda *args: FakeFace(),
                                   security_factory=lambda: security,
                                   evidence_factory=lambda options: EvidenceManager(options, encoder=encode))
        self.worker.update_ready.connect(lambda _: self.updates.append(self.worker.take_update()))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.worker.run()
        self.assertIsNone(self.worker.result.error)
        capture.release.assert_called_once()
        self.assertTrue(security.stopped)
        return self.worker.result

    @staticmethod
    def small_phone(confidence=.82):
        # Fake capture is only 64x48; use coordinates inside that frame.
        return Detection(67, confidence, (25, 20, 35, 40))

    def test_worker_exposes_occlusion_state_and_clears_long_loss(self):
        phone = self.small_phone()
        result = self.monitor([0, .2, .5, .7, 1.5], [[phone], [phone], [], [], []])
        self.assertEqual([packet.phone_state for packet in self.updates],
                         ["DETECTED", "DETECTED", "TRACKED_OCCLUDED", "TRACKED_OCCLUDED", "NONE"])
        self.assertEqual([packet.phone_detected for packet in self.updates], [True, True, True, True, False])
        self.assertEqual(result.events, ())
        self.assertEqual(result.risk_score, 0)
        self.assertEqual(self.snapshots, [])

    def test_worker_observed_event_has_one_risk_delta_and_one_evidence_image(self):
        phone = self.small_phone()
        result = self.monitor([0, .8, .9, 1.2, 20], [[phone], [phone], [], [phone], [phone]])
        self.assertEqual([event.type for event in result.events], [EventType.PHONE_DETECTED])
        self.assertEqual(result.risk_score, 30)
        self.assertEqual(len(self.snapshots), 1)
        self.assertTrue(Path(result.events[0].evidence_path).is_file())
        self.assertEqual(result.events[0].metadata["phone_tracking"]["state"], "DETECTED")

    def test_worker_occlusion_alone_cannot_emit_risk_or_evidence(self):
        phone = self.small_phone()
        result = self.monitor([0, .2, .7], [[phone], [phone], []])
        self.assertEqual(self.updates[-1].phone_state, "TRACKED_OCCLUDED")
        self.assertEqual(result.events, ())
        self.assertEqual(result.risk_score, 0)
        self.assertEqual(self.snapshots, [])

    def test_worker_weak_false_phone_never_tracks_or_penalizes(self):
        weak = self.small_phone(.39)
        result = self.monitor([0, .8, 20], [[weak], [weak], [weak]])
        self.assertTrue(all(packet.phone_state == "NONE" for packet in self.updates))
        self.assertEqual(result.events, ())
        self.assertEqual(self.snapshots, [])

    def test_worker_can_use_original_raw_detection_behavior(self):
        phone = self.small_phone()
        result = self.monitor([0, .6, .7, 1.4], [[phone], [phone], [], [phone]], tracking_enabled=False)
        self.assertEqual([packet.phone_state for packet in self.updates], ["DETECTED", "DETECTED", "NONE", "DETECTED"])
        self.assertEqual(result.events, ())

    def test_occluded_preview_labels_state_without_drawing_predicted_box(self):
        import cv2
        import numpy as np

        frame = np.zeros(SHAPE, dtype=np.uint8)
        with patch.object(cv2, "putText", wraps=cv2.putText) as text, \
                patch.object(cv2, "rectangle", wraps=cv2.rectangle) as boxes:
            draw_preview(cv2, frame, [], 20, PRESENT, phone_state="TRACKED_OCCLUDED")
        self.assertIn("Phone: TRACKED_OCCLUDED", [call.args[1] for call in text.call_args_list])
        boxes.assert_not_called()


if __name__ == "__main__":
    unittest.main()
