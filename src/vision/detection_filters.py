"""Cheap application filters on existing YOLO output; no additional inference."""

from math import isfinite

from monitoring import MultiplePersonsConfig, PhoneDetectionConfig
from .yolo_detector import Detection, PERSON_ID, PHONE_ID


def display_detections(detections: list[Detection], phone: PhoneDetectionConfig) -> list[Detection]:
    """Retain all person boxes and only confidence-qualified phone boxes."""
    return [d for d in detections if d.class_id != PHONE_ID or phone.accepts(d.confidence)]


def _visible_size(detection: Detection, width: int, height: int) -> tuple[float, float]:
    x1, y1, x2, y2 = detection.xyxy
    if not all(isfinite(coordinate) for coordinate in detection.xyxy):
        return 0.0, 0.0
    # Assess the visible part: a mostly offscreen box cannot inflate its size.
    visible_width = max(0.0, min(width, x2) - max(0, x1))
    visible_height = max(0.0, min(height, y2) - max(0, y1))
    return visible_width, visible_height


def secondary_person_confidence(
    detections: list[Detection], frame_shape: tuple[int, ...], config: MultiplePersonsConfig,
) -> float | None:
    """Choose a primary reference, then validate other boxes using frame ratios.

    The largest visible person box is the reference and is exempt from secondary
    geometry rules. All original boxes/counts remain displayable. This reference
    is a size heuristic, not student identification or persistent person tracking.
    """
    height, width = frame_shape[:2]
    if width <= 0 or height <= 0:
        raise ValueError("Geometry validation requires positive frame dimensions")
    persons = [d for d in detections if d.class_id == PERSON_ID]
    if len(persons) < 2:
        return None
    sized = [(d, *_visible_size(d, width, height)) for d in persons]
    primary = max(range(len(sized)), key=lambda index: sized[index][1] * sized[index][2])
    secondaries = [
        d.confidence for index, (d, box_width, box_height) in enumerate(sized)
        if index != primary and box_width > 0 and box_height > 0
        and isfinite(d.confidence) and 0 <= d.confidence <= 1
        and box_width / width >= config.minimum_secondary_width_ratio
        and box_height / height >= config.minimum_secondary_height_ratio
        and box_width * box_height / (width * height) >= config.minimum_secondary_area_ratio
    ]
    if not secondaries:
        return None
    # Preserve the existing conservative weaker-pair confidence semantics.
    primary_confidence = sized[primary][0].confidence
    if not isfinite(primary_confidence) or not 0 <= primary_confidence <= 1:
        return None
    return min(primary_confidence, max(secondaries))
