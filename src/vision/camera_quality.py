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
    # A near-lens palm/cloth can be bright and have a soft illumination
    # gradient, so global brightness/variance alone cannot identify it.
    blur_check_enabled: bool = True
    blur_max_variance: float = 1600.0
    blur_max_laplacian_variance: float = 8.0
    blur_edge_threshold: float = 24.0
    blur_max_edge_fraction: float = 0.02

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in (self.sample_width, self.sample_height)):
            raise ValueError("Quality sample dimensions must be positive integers")
        if not isfinite(self.low_brightness) or not 0 <= self.low_brightness <= 255:
            raise ValueError("Brightness threshold must be between 0 and 255")
        for value in (self.dark_max_variance, self.uniform_max_variance, self.last_usable_max_age_seconds,
                      self.blur_max_variance, self.blur_max_laplacian_variance, self.blur_edge_threshold):
            if not isfinite(value) or value < 0:
                raise ValueError("Quality thresholds and cache age must be finite and nonnegative")
        if type(self.blur_check_enabled) is not bool:
            raise ValueError("Blur check enabled must be a boolean")
        if not isfinite(self.blur_max_edge_fraction) or not 0 <= self.blur_max_edge_fraction <= 1:
            raise ValueError("Blur edge fraction must be finite and between 0 and 1")


@dataclass(frozen=True)
class CameraQuality:
    obstructed: bool
    mean_brightness: float
    grayscale_variance: float
    laplacian_variance: float = 0.0
    edge_fraction: float = 0.0
    obstruction_reason: str | None = None

    def to_dict(self):
        return {"mean_brightness": self.mean_brightness, "grayscale_variance": self.grayscale_variance,
                "laplacian_variance": self.laplacian_variance, "edge_fraction": self.edge_fraction,
                "obstruction_reason": self.obstruction_reason}


def analyze_camera_frame(frame, config: CameraQualityConfig) -> CameraQuality:
    """Analyze raw pixels before boxes/text for sustained low-detail covers.

    Darkness alone is insufficient. Uniform covers and softly textured,
    out-of-focus covers also qualify. Sharp edges or substantial global
    contrast keep ordinary textured/dim scenes usable. This is an image
    quality heuristic; it cannot establish intent or distinguish every
    covered lens from an equally featureless, out-of-focus scene.
    The Event Engine, rather than this stateless sampler, enforces persistence.
    """
    import cv2

    sample = cv2.resize(frame, (config.sample_width, config.sample_height), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(sample, cv2.COLOR_BGR2GRAY)
    brightness, variance = float(gray.mean()), float(gray.var())
    # Denoising prevents sensor grain in a dark cover from being mistaken for
    # usable scene detail. All filtering stays on the small quality sample.
    smooth = cv2.GaussianBlur(gray, (3, 3), 0)
    laplacian = float(cv2.Laplacian(smooth, cv2.CV_32F).var())
    gradient_x = cv2.Sobel(smooth, cv2.CV_32F, 1, 0, ksize=3)
    gradient_y = cv2.Sobel(smooth, cv2.CV_32F, 0, 1, ksize=3)
    edges = float((cv2.magnitude(gradient_x, gradient_y) >= config.blur_edge_threshold).mean())
    reason = None
    if variance <= config.uniform_max_variance:
        reason = "uniform"
    elif brightness <= config.low_brightness and variance <= config.dark_max_variance:
        reason = "dark_flat"
    elif (config.blur_check_enabled and variance <= config.blur_max_variance
          and laplacian <= config.blur_max_laplacian_variance
          and edges <= config.blur_max_edge_fraction):
        reason = "low_detail_blur"
    return CameraQuality(reason is not None, brightness, variance, laplacian, edges, reason)


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
