"""Small image-quality heuristic and bounded recent evidence cache; no ML."""

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class CameraQualityConfig:
    sample_width: int = 160
    sample_height: int = 120
    low_brightness: float = 12.0
    dark_max_variance: float = 64.0
    uniform_max_variance: float = 4.0
    last_usable_max_age_seconds: float = 10.0

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in (self.sample_width, self.sample_height)):
            raise ValueError("Quality sample dimensions must be positive integers")
        if not isfinite(self.low_brightness) or not 0 <= self.low_brightness <= 255:
            raise ValueError("Brightness threshold must be between 0 and 255")
        for value in (self.dark_max_variance, self.uniform_max_variance, self.last_usable_max_age_seconds):
            if not isfinite(value) or value < 0:
                raise ValueError("Quality thresholds and cache age must be finite and nonnegative")


@dataclass(frozen=True)
class CameraQuality:
    obstructed: bool
    mean_brightness: float
    grayscale_variance: float

    def to_dict(self):
        return {"mean_brightness": self.mean_brightness, "grayscale_variance": self.grayscale_variance}


def analyze_camera_frame(frame, config: CameraQualityConfig) -> CameraQuality:
    """Analyze raw pixels before boxes/text: dark AND flat, or nearly uniform.

    Darkness alone is insufficient. A bright uniform cover also qualifies.
    The Event Engine, rather than this stateless sampler, enforces persistence.
    """
    import cv2

    sample = cv2.resize(frame, (config.sample_width, config.sample_height), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(sample, cv2.COLOR_BGR2GRAY)
    brightness, variance = float(gray.mean()), float(gray.var())
    blocked = (variance <= config.uniform_max_variance
               or (brightness <= config.low_brightness and variance <= config.dark_max_variance))
    return CameraQuality(blocked, brightness, variance)


class RecentUsableFrame:
    """Keep one detached annotated frame, with a monotonic freshness limit."""

    def __init__(self, config: CameraQualityConfig):
        self.config = config
        self._frame = None
        self._at = None

    def remember(self, annotated, quality: CameraQuality, now: float):
        if not quality.obstructed:
            self._frame, self._at = annotated.copy(), now

    def evidence_frame(self, current, now: float):
        age = None if self._at is None else now - self._at
        if self._frame is not None and 0 <= age <= self.config.last_usable_max_age_seconds:
            return self._frame, {"evidence_frame_source": "last_usable", "last_usable_age_seconds": age}
        return current, {"evidence_frame_source": "current", "last_usable_age_seconds": age}
