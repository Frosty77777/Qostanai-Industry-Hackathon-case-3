"""Local MediaPipe landmarks and coarse head direction, not eye-gaze tracking."""

from dataclasses import dataclass
from enum import StrEnum
from math import atan2, degrees, hypot
from pathlib import Path
from time import perf_counter

import numpy as np


class FaceStatus(StrEnum):
    FACE_DETECTED = "FACE_DETECTED"
    NO_FACE = "NO_FACE"
    MULTIPLE_FACES = "MULTIPLE_FACES"
    UNAVAILABLE = "UNAVAILABLE"


class HeadDirection(StrEnum):
    CENTER = "CENTER"
    LEFT = "LEFT"
    RIGHT = "RIGHT"
    UP = "UP"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FaceTrackerConfig:
    """All detection, tracking, and direction thresholds are configured here."""

    max_faces: int = 2
    detection_confidence: float = 0.5
    presence_confidence: float = 0.5
    tracking_confidence: float = 0.5
    yaw_threshold_degrees: float = 20.0
    down_threshold_degrees: float = 15.0
    max_pose_degrees: float = 75.0
    min_axis_length: float = 1e-6
    up_threshold_degrees: float = 15.0

    def __post_init__(self) -> None:
        if self.max_faces < 1:
            raise ValueError("max_faces must be at least 1")
        for value in (self.detection_confidence, self.presence_confidence, self.tracking_confidence):
            if not 0 <= value <= 1:
                raise ValueError("Face confidence thresholds must be between 0 and 1")
        if not 0 < self.yaw_threshold_degrees < self.max_pose_degrees < 90:
            raise ValueError("Require 0 < yaw threshold < maximum pose < 90 degrees")
        if not 0 < self.down_threshold_degrees < self.max_pose_degrees:
            raise ValueError("Down threshold must be positive and below maximum pose")
        if not 0 < self.up_threshold_degrees < self.max_pose_degrees:
            raise ValueError("Up threshold must be positive and below maximum pose")
        if not np.isfinite(self.min_axis_length) or self.min_axis_length <= 0:
            raise ValueError("Minimum axis length must be finite and positive")


DEFAULT_CONFIG = FaceTrackerConfig()
DEFAULT_MODEL_PATH = Path(__file__).resolve().parents[2] / "models" / "face_landmarker.task"


@dataclass(frozen=True)
class FaceResult:
    status: FaceStatus
    face_count: int | None
    head_direction: HeadDirection = HeadDirection.UNKNOWN


def estimate_head_direction(matrix, config: FaceTrackerConfig = DEFAULT_CONFIG) -> HeadDirection:
    """Classify the canonical face's forward axis in MediaPipe camera space.

    MediaPipe geometry uses +X right, +Y up, and +Z toward the camera.
    For an unmirrored frame, positive yaw is the subject's LEFT; positive
    downward pitch is DOWN and negative pitch is UP. Translation and uniform
    scale do not affect it.
    """
    try:
        transform = np.asarray(matrix, dtype=np.float64)
    except (TypeError, ValueError):
        return HeadDirection.UNKNOWN
    if transform.shape != (4, 4) or not np.isfinite(transform).all():
        return HeadDirection.UNKNOWN
    forward = transform[:3, 2]
    if np.linalg.norm(forward) < config.min_axis_length:
        return HeadDirection.UNKNOWN
    yaw = degrees(atan2(forward[0], forward[2]))
    down_pitch = degrees(atan2(-forward[1], hypot(forward[0], forward[2])))
    if abs(yaw) > config.max_pose_degrees or abs(down_pitch) > config.max_pose_degrees:
        return HeadDirection.UNKNOWN
    if down_pitch <= -config.up_threshold_degrees:
        return HeadDirection.UP
    if down_pitch >= config.down_threshold_degrees:
        return HeadDirection.DOWN
    if yaw >= config.yaw_threshold_degrees:
        return HeadDirection.LEFT
    if yaw <= -config.yaw_threshold_degrees:
        return HeadDirection.RIGHT
    return HeadDirection.CENTER


def summarize_faces(result, config: FaceTrackerConfig = DEFAULT_CONFIG) -> FaceResult:
    """Only estimate a head direction when exactly one face is present."""
    count = len(result.face_landmarks)
    if count == 0:
        return FaceResult(FaceStatus.NO_FACE, 0)
    if count > 1:
        return FaceResult(FaceStatus.MULTIPLE_FACES, count)
    matrices = result.facial_transformation_matrixes
    direction = estimate_head_direction(matrices[0], config) if len(matrices) == 1 else HeadDirection.UNKNOWN
    return FaceResult(FaceStatus.FACE_DETECTED, 1, direction)


class FaceTracker:
    """Reuse a CPU video-mode landmarker; degrade to UNAVAILABLE on failure."""

    def __init__(self, model_path: Path = DEFAULT_MODEL_PATH, config: FaceTrackerConfig = DEFAULT_CONFIG) -> None:
        self.config = config
        self.error: str | None = None
        self._landmarker = None
        self._last_timestamp_ms = -1
        try:
            import cv2
            import mediapipe as mp

            model_path = Path(model_path).expanduser().resolve()
            if not model_path.is_file():
                raise FileNotFoundError(f"Missing local face model: {model_path}. See README.md for setup.")
            self._cv2 = cv2
            self._mp = mp
            options = mp.tasks.vision.FaceLandmarkerOptions(
                base_options=mp.tasks.BaseOptions(
                    model_asset_path=str(model_path),
                    delegate=mp.tasks.BaseOptions.Delegate.CPU,
                ),
                running_mode=mp.tasks.vision.RunningMode.VIDEO,
                num_faces=config.max_faces,
                min_face_detection_confidence=config.detection_confidence,
                min_face_presence_confidence=config.presence_confidence,
                min_tracking_confidence=config.tracking_confidence,
                output_face_blendshapes=False,
                output_facial_transformation_matrixes=True,
            )
            self._landmarker = mp.tasks.vision.FaceLandmarker.create_from_options(options)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    @property
    def available(self) -> bool:
        return self._landmarker is not None

    def process(self, frame: np.ndarray) -> FaceResult:
        if not self.available:
            return FaceResult(FaceStatus.UNAVAILABLE, None)
        try:
            rgb = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
            image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
            # VIDEO requires strictly increasing monotonic millisecond timestamps.
            timestamp_ms = max(int(perf_counter() * 1000), self._last_timestamp_ms + 1)
            self._last_timestamp_ms = timestamp_ms
            result = self._landmarker.detect_for_video(image, timestamp_ms)
            return summarize_faces(result, self.config)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.close()
            return FaceResult(FaceStatus.UNAVAILABLE, None)

    def close(self) -> None:
        landmarker, self._landmarker = self._landmarker, None
        if landmarker is not None:
            try:
                landmarker.close()
            except Exception:
                # Cleanup must not interrupt YOLO or camera resource release.
                pass
