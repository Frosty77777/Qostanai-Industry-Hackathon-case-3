"""Run with: python -m unittest discover -s tests -v."""

import contextlib
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main
from vision.face_tracker import (
    FaceResult, FaceStatus, FaceTracker, FaceTrackerConfig, HeadDirection,
    estimate_head_direction, summarize_faces,
)
from vision.yolo_detector import Detection


def face_transform(yaw=0, pitch=0):
    yaw, pitch = np.radians([yaw, pitch])
    y = np.array([[np.cos(yaw), 0, np.sin(yaw)], [0, 1, 0], [-np.sin(yaw), 0, np.cos(yaw)]])
    x = np.array([[1, 0, 0], [0, np.cos(pitch), -np.sin(pitch)], [0, np.sin(pitch), np.cos(pitch)]])
    matrix = np.eye(4)
    matrix[:3, :3] = x @ y
    return matrix


class FaceChecks(unittest.TestCase):
    def setUp(self):
        monitor = MagicMock()
        monitor.poll.return_value = []
        self.enterContext(patch.object(main, "SecurityMonitor", return_value=monitor))

    def test_direction_conventions(self):
        for yaw, pitch, expected in [(0, 0, "CENTER"), (30, 0, "LEFT"), (-30, 0, "RIGHT"), (0, 25, "DOWN"), (30, 30, "DOWN"), (0, -25, "UP"), (30, -30, "UP"), (80, 0, "UNKNOWN"), (0, -80, "UNKNOWN")]:
            with self.subTest(yaw=yaw, pitch=pitch):
                self.assertEqual(estimate_head_direction(face_transform(yaw, pitch)), expected)

    def test_upward_pitch_is_up(self):
        self.assertEqual(estimate_head_direction(face_transform(pitch=-25)), HeadDirection.UP)

    def test_neutral_pitch_is_center(self):
        for pitch in (-5, 0, 5):
            with self.subTest(pitch=pitch):
                self.assertEqual(estimate_head_direction(face_transform(pitch=pitch)), HeadDirection.CENTER)

    def test_downward_pitch_is_down(self):
        self.assertEqual(estimate_head_direction(face_transform(pitch=25)), HeadDirection.DOWN)

    def test_up_threshold_is_independent_and_configurable(self):
        config = FaceTrackerConfig(up_threshold_degrees=5)
        self.assertEqual(estimate_head_direction(face_transform(pitch=-10)), HeadDirection.CENTER)
        self.assertEqual(estimate_head_direction(face_transform(pitch=-10), config), HeadDirection.UP)
        self.assertEqual(estimate_head_direction(face_transform(pitch=10), config), HeadDirection.CENTER)
        self.assertEqual(estimate_head_direction(face_transform(pitch=25), config), HeadDirection.DOWN)

    def test_threshold_configuration(self):
        self.assertEqual(estimate_head_direction(face_transform(10)), "CENTER")
        config = FaceTrackerConfig(yaw_threshold_degrees=5)
        self.assertEqual(estimate_head_direction(face_transform(10), config), "LEFT")
        for kwargs in ({"max_faces": 0}, {"detection_confidence": 2}, {"yaw_threshold_degrees": 90}, {"down_threshold_degrees": 0}, {"up_threshold_degrees": 0}, {"up_threshold_degrees": 75}, {"up_threshold_degrees": float("nan")}, {"min_axis_length": float("nan")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                FaceTrackerConfig(**kwargs)

    def test_translation_and_uniform_scale(self):
        matrix = face_transform(-30)
        matrix[:3, :3] *= 3
        matrix[:3, 3] = [100, -50, -30]
        self.assertEqual(estimate_head_direction(matrix), "RIGHT")

    def test_invalid_matrix_is_unknown(self):
        for matrix in (None, np.zeros((4, 4)), np.eye(3), np.full((4, 4), np.nan)):
            with self.subTest(matrix=matrix):
                self.assertEqual(estimate_head_direction(matrix), "UNKNOWN")

    def test_face_states_and_missing_pose(self):
        for count, matrices, status, direction in [
            (0, [], "NO_FACE", "UNKNOWN"),
            (1, [np.eye(4)], "FACE_DETECTED", "CENTER"),
            (1, [], "FACE_DETECTED", "UNKNOWN"),
            (2, [np.eye(4), np.eye(4)], "MULTIPLE_FACES", "UNKNOWN"),
        ]:
            result = summarize_faces(SimpleNamespace(face_landmarks=[[]] * count, facial_transformation_matrixes=matrices))
            self.assertEqual((result.status, result.face_count, result.head_direction), (status, count, direction))

    def test_missing_mediapipe_is_unavailable(self):
        with patch.dict(sys.modules, {"mediapipe": None}):
            tracker = FaceTracker()
        self.assertFalse(tracker.available)
        self.assertIn("ModuleNotFoundError", tracker.error)
        self.assertEqual(tracker.process(np.zeros((2, 2, 3), np.uint8)).status, "UNAVAILABLE")
        self.assertIsNone(tracker.process(None).face_count)
        tracker.close()

    def mock_tracker(self):
        mp = MagicMock()
        landmarker = mp.tasks.vision.FaceLandmarker.create_from_options.return_value
        landmarker.detect_for_video.return_value = SimpleNamespace(face_landmarks=[], facial_transformation_matrixes=[])
        with patch.dict(sys.modules, {"mediapipe": mp}), patch.object(Path, "is_file", return_value=True):
            tracker = FaceTracker()
        return tracker, mp, landmarker

    def test_cpu_video_mode_rgb_and_timestamps(self):
        tracker, mp, landmarker = self.mock_tracker()
        frame = np.full((2, 2, 3), [5, 10, 20], dtype=np.uint8)
        with patch("vision.face_tracker.perf_counter", return_value=1):
            tracker.process(frame)
            tracker.process(frame)
        self.assertEqual([call.args[1] for call in landmarker.detect_for_video.call_args_list], [1000, 1001])
        np.testing.assert_array_equal(mp.Image.call_args.kwargs["data"][0, 0], [20, 10, 5])
        options = mp.tasks.vision.FaceLandmarkerOptions.call_args.kwargs
        self.assertEqual(options["num_faces"], 2)
        self.assertFalse(options["output_face_blendshapes"])
        self.assertTrue(options["output_facial_transformation_matrixes"])
        self.assertEqual(options["running_mode"], mp.tasks.vision.RunningMode.VIDEO)
        self.assertEqual(mp.tasks.BaseOptions.call_args.kwargs["delegate"], mp.tasks.BaseOptions.Delegate.CPU)
        tracker.close()
        tracker.close()
        landmarker.close.assert_called_once()

    def test_runtime_failure_closes_and_disables(self):
        tracker, _, landmarker = self.mock_tracker()
        landmarker.detect_for_video.side_effect = RuntimeError("native task failed")
        result = tracker.process(np.zeros((2, 2, 3), np.uint8))
        self.assertEqual(result.status, "UNAVAILABLE")
        self.assertFalse(tracker.available)
        self.assertIn("native task failed", tracker.error)
        tracker.process(None)
        landmarker.detect_for_video.assert_called_once()
        landmarker.close.assert_called_once()

    def test_initialization_failure(self):
        mp = MagicMock()
        mp.tasks.vision.FaceLandmarker.create_from_options.side_effect = RuntimeError("cannot initialize")
        with patch.dict(sys.modules, {"mediapipe": mp}), patch.object(Path, "is_file", return_value=True):
            tracker = FaceTracker()
        self.assertFalse(tracker.available)
        self.assertIn("cannot initialize", tracker.error)

    def test_preview_preserves_yolo_labels_and_face_text(self):
        frame = np.zeros((480, 640, 3), np.uint8)
        detections = [Detection(0, .9, (1, 2, 30, 50)), Detection(67, .8, (50, 60, 80, 90))]
        with patch.object(cv2, "putText", wraps=cv2.putText) as put_text:
            preview = main.draw_preview(cv2, frame, detections, 12.5, FaceResult(FaceStatus.FACE_DETECTED, 1, HeadDirection.LEFT))
        texts = [call.args[1] for call in put_text.call_args_list]
        for text in ("person 0.90", "cell phone 0.80", "Persons: 1", "Phone detected: YES", "FPS: 12.5 | Q: quit", "Face: detected", "Head: LEFT", "Faces: 1 (FACE_DETECTED)"):
            self.assertIn(text, texts)
        self.assertEqual(preview.shape, (580, 640, 3))
        np.testing.assert_array_equal(preview[:480], frame)

    def test_preview_displays_up(self):
        frame = np.zeros((480, 640, 3), np.uint8)
        face = summarize_faces(SimpleNamespace(
            face_landmarks=[[]],
            facial_transformation_matrixes=[face_transform(pitch=-25)],
        ))
        with patch.object(cv2, "putText", wraps=cv2.putText) as put_text:
            main.draw_preview(cv2, frame, [], None, face)
        self.assertIn("Head: UP", [call.args[1] for call in put_text.call_args_list])

    def test_yolo_continues_if_face_initialization_fails(self):
        fake_cv2 = MagicMock()
        fake_cv2.waitKey.return_value = ord("q")
        camera = MagicMock()
        frame = np.zeros((480, 640, 3), np.uint8)
        face_tracker = MagicMock()
        face_tracker.available = False
        face_tracker.error = "init failed"
        face_tracker.process.return_value = FaceResult(FaceStatus.UNAVAILABLE, None)
        detector = MagicMock()
        detector.detect.return_value = []
        with patch.object(sys, "argv", ["main.py"]), patch.dict(sys.modules, {"cv2": fake_cv2}), patch.object(main, "YoloDetector", return_value=detector), patch.object(main, "FaceTracker", return_value=face_tracker), patch.object(main, "open_webcam", return_value=(camera, frame)), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main.main(), 0)
        detector.detect.assert_called_once_with(frame)
        camera.release.assert_called_once()
        face_tracker.close.assert_called_once()
        fake_cv2.destroyAllWindows.assert_called_once()


if __name__ == "__main__":
    unittest.main()
