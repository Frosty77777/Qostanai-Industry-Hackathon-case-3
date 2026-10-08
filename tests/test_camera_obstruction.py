"""Synthetic near-lens covers through quality sampling and desktop packets.

No webcam, OS keyboard hook, or real-time sleep is used. These fixtures model
soft colored gradients and sensor grain; they are not labelled real footage.
"""

from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import test_monitoring_reliability as reliability
from monitoring import EventType
from vision.camera_quality import CameraQualityConfig, RecentUsableFrame, analyze_camera_frame


def soft_cover(base=(40, 75, 155), *, seed=7):
    """A colored, softly textured palm/cloth with a broad light gradient."""
    y, x = np.indices((480, 640))
    illumination = 22 * np.sin(x / 180) + 14 * np.cos(y / 200)
    grain = np.random.default_rng(seed).normal(0, 5, (480, 640, 1))
    return np.clip(np.array(base) + illumination[:, :, None] + grain, 0, 255).astype(np.uint8)


def clear_scene(base=45, contrast=65):
    """Scene edges survive sampling, even with dim lighting/ordinary blur."""
    y, x = np.indices((480, 640))
    gray = (((x // 55 + y // 55) % 2) * contrast + base).astype(np.uint8)
    return np.repeat(gray[:, :, None], 3, axis=2)


class NearLensQualityChecks(unittest.TestCase):
    def setUp(self):
        self.config = CameraQualityConfig()

    def test_textured_palm_is_obstructed_without_being_dark_or_uniform(self):
        result = analyze_camera_frame(soft_cover(), self.config)
        self.assertGreater(result.mean_brightness, self.config.low_brightness)
        self.assertGreater(result.grayscale_variance, self.config.dark_max_variance)
        self.assertTrue(result.obstructed)
        self.assertEqual(result.obstruction_reason, "low_detail_blur")

    def test_bright_soft_object_cover_is_obstructed(self):
        result = analyze_camera_frame(soft_cover((160, 175, 195)), self.config)
        self.assertGreater(result.mean_brightness, 150)
        self.assertTrue(result.obstructed)

    def test_dark_textured_cover_is_obstructed(self):
        result = analyze_camera_frame(soft_cover((5, 10, 20)), self.config)
        self.assertGreater(result.grayscale_variance, self.config.dark_max_variance)
        self.assertTrue(result.obstructed)

    def test_clear_dim_scene_is_not_obstructed(self):
        result = analyze_camera_frame(clear_scene(0, 22), self.config)
        self.assertLess(result.mean_brightness, self.config.low_brightness)
        self.assertFalse(result.obstructed)

    def test_clear_normal_room_structure_is_not_obstructed(self):
        self.assertFalse(analyze_camera_frame(clear_scene(), self.config).obstructed)

    def test_ordinary_mild_defocus_preserves_scene_structure(self):
        scene = cv2.GaussianBlur(clear_scene(), (9, 9), 2)
        self.assertFalse(analyze_camera_frame(scene, self.config).obstructed)

    def test_partial_cover_with_visible_scene_detail_is_not_full_obstruction(self):
        frame = soft_cover()
        frame[:, frame.shape[1] // 2:] = clear_scene()[:, frame.shape[1] // 2:]
        self.assertFalse(analyze_camera_frame(frame, self.config).obstructed)

    def test_high_contrast_gradient_is_not_a_soft_cover(self):
        gray = np.tile(np.linspace(0, 255, 640, dtype=np.uint8), (480, 1))
        frame = np.repeat(gray[:, :, None], 3, axis=2)
        self.assertFalse(analyze_camera_frame(frame, self.config).obstructed)

    def test_blur_branch_can_be_disabled_without_disabling_shutter_detection(self):
        config = replace(self.config, blur_check_enabled=False)
        self.assertFalse(analyze_camera_frame(soft_cover(), config).obstructed)
        self.assertTrue(analyze_camera_frame(np.zeros((480, 640, 3), dtype=np.uint8), config).obstructed)

    def test_soft_cover_variance_limit_is_configurable(self):
        config = replace(self.config, blur_max_variance=10)
        self.assertFalse(analyze_camera_frame(soft_cover(), config).obstructed)

    def test_focus_limit_is_configurable(self):
        config = replace(self.config, blur_max_laplacian_variance=0)
        self.assertFalse(analyze_camera_frame(soft_cover(), config).obstructed)

    def test_metrics_are_finite_json_serializable_evidence_metadata(self):
        result = analyze_camera_frame(soft_cover(), self.config)
        metadata = result.to_dict()
        json.dumps(metadata, allow_nan=False)
        self.assertEqual(metadata["obstruction_reason"], "low_detail_blur")
        self.assertIn("laplacian_variance", metadata)
        self.assertIn("edge_fraction", metadata)

    def test_invalid_blur_configuration_is_rejected(self):
        for changes in ({"blur_check_enabled": 1}, {"blur_max_variance": -1},
                        {"blur_max_variance": float("nan")},
                        {"blur_max_laplacian_variance": float("inf")},
                        {"blur_edge_threshold": -1}, {"blur_max_edge_fraction": 1.1},
                        {"blur_max_edge_fraction": float("nan")}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.config, **changes)

    def test_soft_cover_does_not_replace_last_usable_evidence_frame(self):
        cache = RecentUsableFrame(self.config)
        clear, cover = clear_scene(), soft_cover()
        cache.remember(clear, analyze_camera_frame(clear, self.config), 0)
        cache.remember(cover, analyze_camera_frame(cover, self.config), 1)
        image, metadata = cache.evidence_frame(cover, 2)
        np.testing.assert_array_equal(image, clear)
        self.assertEqual(metadata["evidence_frame_source"], "last_usable")


class NearLensDesktopChecks(unittest.TestCase):
    monitor = reliability.ReliabilityWorkerChecks.monitor

    def test_short_palm_cover_does_not_emit_obstruction(self):
        cover = soft_cover()
        result = self.monitor([0, 1.999], frames=[cover, cover], faces=[reliability.UNAVAILABLE] * 2)
        self.assertEqual(result.events, ())
        self.assertFalse(any(packet.camera_obstructed_warning for packet in self.updates))

    def test_persistent_palm_cover_reaches_worker_warning_without_event_spam(self):
        cover = soft_cover()
        result = self.monitor([0, 1.999, 2, 5, 30], frames=[cover] * 5,
                              faces=[reliability.UNAVAILABLE] * 5)
        self.assertEqual([entry.type for entry in result.events], [EventType.CAMERA_OBSTRUCTED])
        self.assertEqual([packet.camera_obstructed_warning for packet in self.updates],
                         [False, False, True, True, True])
        self.assertFalse(any(packet.face_missing_warning for packet in self.updates))
        self.assertEqual(result.risk_score, 20)
        self.assertEqual(result.events[0].metadata["camera_quality"]["obstruction_reason"], "low_detail_blur")

    def test_clear_scene_immediately_resets_palm_cover_warning(self):
        cover = soft_cover()
        result = self.monitor([0, 2, 3], frames=[cover, cover, clear_scene()],
                              faces=[reliability.UNAVAILABLE] * 3)
        self.assertEqual([entry.type for entry in result.events], [EventType.CAMERA_OBSTRUCTED])
        self.assertEqual([packet.camera_obstructed_warning for packet in self.updates], [False, True, False])


if __name__ == "__main__":
    unittest.main()
