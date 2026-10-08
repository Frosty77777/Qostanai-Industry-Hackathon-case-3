"""Confidence-gated phone continuity without extra inference or dependencies.

The existing Ultralytics detector supplies boxes. A small IoU/center/size
association retains one probable phone across a short interruption. It does
not hallucinate or draw new boxes and does not lower the application confidence
floor. Observed duration excludes intervals whose preceding sample was absent.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from math import hypot, isfinite
from time import perf_counter

from monitoring.event_engine import FrameSignals, PhoneDetectionConfig
from .yolo_detector import Detection, PHONE_ID


class PhoneState(StrEnum):
    NONE = "NONE"
    DETECTED = "DETECTED"
    TRACKED_OCCLUDED = "TRACKED_OCCLUDED"


@dataclass(frozen=True)
class PhoneTrackingResult:
    state: PhoneState = PhoneState.NONE
    track_id: int | None = None
    confidence: float | None = None
    bbox: tuple[float, float, float, float] | None = None
    observed_duration_seconds: float = 0.0
    confident_hits: int = 0

    @property
    def probable_phone(self) -> bool:
        return self.state != PhoneState.NONE

    def apply_to(self, signals: FrameSignals) -> FrameSignals:
        """Retain other CV signals while supplying the engine's observed guard."""
        return replace(signals, phone_detected=self.probable_phone,
                       phone_confidence=self.confidence,
                       phone_tracking_state=self.state.value,
                       phone_track_id=self.track_id,
                       phone_observed_duration_seconds=self.observed_duration_seconds,
                       phone_tracking_hits=self.confident_hits)


def _area(box) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


def _iou(first, second) -> float:
    overlap = (max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
               * max(0.0, min(first[3], second[3]) - max(first[1], second[1])))
    return overlap / (_area(first) + _area(second) - overlap)


class PhoneTracker:
    """One conservative track, monotonic time, bounded state and fake-time API.

    At least minimum_tracking_hits observations are needed to retain a phone
    through missing detections. Missing/weak boxes never refresh the last-seen
    time, hits, or observed duration. A new distant/implausibly resized box starts
    a new identity; the Event Engine independently resets that episode.
    """

    def __init__(self, config: PhoneDetectionConfig | None = None, *,
                 clock: Callable[[], float] = perf_counter):
        self.config = config if config is not None else PhoneDetectionConfig()
        self._clock = clock
        self._last_update_at: float | None = None
        self._last_seen_at: float | None = None
        self._next_track_id = 1
        self.current = PhoneTrackingResult()

    def reset(self) -> None:
        self._last_seen_at = None
        self.current = PhoneTrackingResult()

    def _candidate(self, detection, frame_shape):
        if detection.class_id != PHONE_ID or not self.config.accepts(detection.confidence):
            return None
        box = detection.xyxy
        if len(box) != 4 or not all(isfinite(coordinate) for coordinate in box):
            return None
        height, width = frame_shape[:2]
        clipped = (max(0.0, min(width, box[0])), max(0.0, min(height, box[1])),
                   max(0.0, min(width, box[2])), max(0.0, min(height, box[3])))
        if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
            return None
        return detection.confidence, clipped

    def _matches(self, box) -> bool:
        previous = self.current.bbox
        area_ratio = max(_area(previous), _area(box)) / min(_area(previous), _area(box))
        if area_ratio > self.config.maximum_area_ratio:
            return False
        center_distance = hypot((previous[0] + previous[2] - box[0] - box[2]) / 2,
                                (previous[1] + previous[3] - box[1] - box[3]) / 2)
        previous_diagonal = hypot(previous[2] - previous[0], previous[3] - previous[1])
        return (_iou(previous, box) >= self.config.minimum_association_iou
                or center_distance <= previous_diagonal * self.config.maximum_center_distance_ratio)

    def update(self, detections: Iterable[Detection], frame_shape: tuple[int, ...], *,
               now: float | None = None) -> PhoneTrackingResult:
        now = self._clock() if now is None else now
        if not isfinite(now) or (self._last_update_at is not None and now < self._last_update_at):
            raise ValueError("Phone tracking time must be finite and nondecreasing")
        if len(frame_shape) < 2 or any(not isfinite(size) or size <= 0 for size in frame_shape[:2]):
            raise ValueError("Phone tracking requires positive frame dimensions")
        candidates = [candidate for detection in detections
                      if (candidate := self._candidate(detection, frame_shape)) is not None]
        # Expire only missing samples: consecutive observed frames keep the
        # original sampled-duration behavior at lower inference frame rates.
        if (self.current.state == PhoneState.TRACKED_OCCLUDED
                and now - self._last_seen_at > self.config.occlusion_grace_seconds):
            self.reset()
        matches = ([candidate for candidate in candidates if self._matches(candidate[1])]
                   if self.current.bbox is not None else [])
        candidate = (max(matches, key=lambda item: (_iou(self.current.bbox, item[1]), item[0]))
                     if matches else max(candidates, default=None, key=lambda item: item[0]))
        if candidate is not None:
            confidence, box = candidate
            if not matches:
                self.current = PhoneTrackingResult(PhoneState.DETECTED, self._next_track_id,
                                                    confidence, box, 0.0, 1)
                self._next_track_id += 1
            else:
                observed_delta = (now - self._last_update_at
                                  if self.current.state == PhoneState.DETECTED else 0.0)
                self.current = replace(self.current, state=PhoneState.DETECTED,
                                       confidence=confidence, bbox=box,
                                       observed_duration_seconds=self.current.observed_duration_seconds + observed_delta,
                                       confident_hits=self.current.confident_hits + 1)
            self._last_seen_at = now
        elif (self.config.tracking_enabled and self.current.track_id is not None
              and self.current.confident_hits >= self.config.minimum_tracking_hits
              and now - self._last_seen_at <= self.config.occlusion_grace_seconds):
            self.current = replace(self.current, state=PhoneState.TRACKED_OCCLUDED)
        else:
            self.reset()
        self._last_update_at = now
        return self.current
