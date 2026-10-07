"""Configurable, event-based session risk; independent of vision and time."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType

from .event_engine import EventType, ProctoringEvent

MIN_SCORE = 0
MAX_SCORE = 100


class RiskLevel(StrEnum):
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class RiskWeight:
    first: int
    repeated: int | None = None

    def __post_init__(self) -> None:
        for value in (self.first, self.repeated):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Risk weights must be nonnegative integers")
        if self.first is None:
            raise ValueError("A first-occurrence risk weight is required")


def _default_weights() -> dict[EventType, RiskWeight]:
    return {
        EventType.PHONE_DETECTED: RiskWeight(30, 10),
        EventType.MULTIPLE_PERSONS: RiskWeight(30, 15),
        EventType.MULTIPLE_FACES: RiskWeight(25, 10),
        EventType.FACE_MISSING: RiskWeight(15, 8),
        EventType.LOOK_LEFT: RiskWeight(8),
        EventType.LOOK_RIGHT: RiskWeight(8),
        EventType.LOOK_UP: RiskWeight(5),
        EventType.LOOK_DOWN: RiskWeight(10),
        EventType.ALT_TAB_ATTEMPT: RiskWeight(15, 8),
        EventType.WINDOW_FOCUS_LOST: RiskWeight(15, 8),
        EventType.PRINTSCREEN_ATTEMPT: RiskWeight(15, 8),
        EventType.COPY_ATTEMPT: RiskWeight(5, 3),
        EventType.PASTE_ATTEMPT: RiskWeight(5, 3),
        EventType.ESCAPE_ATTEMPT: RiskWeight(5, 3),
        EventType.CAMERA_OBSTRUCTED: RiskWeight(20, 10),
        EventType.AUTHORIZED_BREAK_STARTED: RiskWeight(0),
        EventType.AUTHORIZED_BREAK_ENDED: RiskWeight(0),
        EventType.AUTHORIZED_BREAK_EXPIRED: RiskWeight(0),
    }


@dataclass(frozen=True)
class RiskConfig:
    weights: Mapping[EventType, RiskWeight] = field(default_factory=_default_weights)
    low_max: int = 20
    moderate_max: int = 45
    high_max: int = 70

    def __post_init__(self) -> None:
        if set(self.weights) != set(EventType):
            raise ValueError("Configure exactly one risk weight for each event type")
        if any(not isinstance(weight, RiskWeight) for weight in self.weights.values()):
            raise ValueError("Risk configuration values must be RiskWeight instances")
        boundaries = (self.low_max, self.moderate_max, self.high_max)
        if any(type(value) is not int for value in boundaries) or not (
            MIN_SCORE <= self.low_max < self.moderate_max < self.high_max < MAX_SCORE
        ):
            raise ValueError("Require integer risk level boundaries: 0 <= LOW < MODERATE < HIGH < 100")
        object.__setattr__(self, "weights", MappingProxyType(dict(self.weights)))


class RiskEngine:
    """Accumulate one contribution for each newly emitted event passed in.

    Call process exactly once per emitted event; this is not a replay or event
    deduplication store. First/repeat counts are per type and per engine session.
    risk_delta is the actual applied increase, including zero at the score cap.
    Scores do not decay and no frames or elapsed time can change them.
    """

    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config if config is not None else RiskConfig()
        self._current_score = MIN_SCORE
        self._event_counts = {event_type: 0 for event_type in EventType}

    @property
    def current_score(self) -> int:
        return self._current_score

    @property
    def current_level(self) -> RiskLevel:
        if self.current_score <= self.config.low_max:
            return RiskLevel.LOW
        if self.current_score <= self.config.moderate_max:
            return RiskLevel.MODERATE
        if self.current_score <= self.config.high_max:
            return RiskLevel.HIGH
        return RiskLevel.CRITICAL

    @property
    def event_counts(self) -> Mapping[EventType, int]:
        return MappingProxyType(self._event_counts)

    def process(self, event: ProctoringEvent) -> ProctoringEvent:
        """Return a scored copy, preserving all CV, evidence and event fields."""
        if not isinstance(event, ProctoringEvent):
            raise TypeError("RiskEngine processes emitted ProctoringEvent objects only")
        if not isinstance(event.type, EventType):
            raise ValueError("Unsupported risk event type")
        weight = self.config.weights[event.type]
        repeated = self._event_counts[event.type] > 0
        contribution = weight.repeated if repeated and weight.repeated is not None else weight.first
        next_score = max(MIN_SCORE, min(MAX_SCORE, self.current_score + contribution))
        scored = replace(event, risk_delta=next_score - self.current_score)
        self._current_score = next_score
        self._event_counts[event.type] += 1
        return scored

    def reset(self) -> None:
        """Reset risk and occurrence counts; the caller owns history/evidence."""
        self._current_score = MIN_SCORE
        for event_type in self._event_counts:
            self._event_counts[event_type] = 0
