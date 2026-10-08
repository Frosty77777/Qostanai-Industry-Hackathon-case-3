"""Convert sampled frame signals into duration-qualified session events.

Durations and cooldowns use monotonic seconds; event timestamps use local,
timezone-aware wall time. One false sample resets the duration and episode
latch, but retains the cooldown. There is no interruption debounce tolerance.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from math import isfinite
from time import perf_counter
from types import MappingProxyType
from typing import Any


class EventType(StrEnum):
    PHONE_DETECTED = "PHONE_DETECTED"
    MULTIPLE_PERSONS = "MULTIPLE_PERSONS"
    MULTIPLE_FACES = "MULTIPLE_FACES"
    FACE_MISSING = "FACE_MISSING"
    LOOK_LEFT = "LOOK_LEFT"
    LOOK_RIGHT = "LOOK_RIGHT"
    LOOK_UP = "LOOK_UP"
    LOOK_DOWN = "LOOK_DOWN"
    WINDOW_FOCUS_LOST = "WINDOW_FOCUS_LOST"
    ALT_TAB_ATTEMPT = "ALT_TAB_ATTEMPT"
    COPY_ATTEMPT = "COPY_ATTEMPT"
    PASTE_ATTEMPT = "PASTE_ATTEMPT"
    PRINTSCREEN_ATTEMPT = "PRINTSCREEN_ATTEMPT"
    ESCAPE_ATTEMPT = "ESCAPE_ATTEMPT"
    CAMERA_OBSTRUCTED = "CAMERA_OBSTRUCTED"
    AUTHORIZED_BREAK_STARTED = "AUTHORIZED_BREAK_STARTED"
    AUTHORIZED_BREAK_ENDED = "AUTHORIZED_BREAK_ENDED"
    AUTHORIZED_BREAK_EXPIRED = "AUTHORIZED_BREAK_EXPIRED"


SECURITY_EVENT_TYPES = frozenset({
    EventType.WINDOW_FOCUS_LOST, EventType.ALT_TAB_ATTEMPT,
    EventType.COPY_ATTEMPT, EventType.PASTE_ATTEMPT,
    EventType.PRINTSCREEN_ATTEMPT, EventType.ESCAPE_ATTEMPT,
})
SESSION_EVENT_TYPES = frozenset({
    EventType.AUTHORIZED_BREAK_STARTED, EventType.AUTHORIZED_BREAK_ENDED,
    EventType.AUTHORIZED_BREAK_EXPIRED,
})
CV_EVENT_TYPES = frozenset(EventType) - SECURITY_EVENT_TYPES - SESSION_EVENT_TYPES


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    INFO = "info"


@dataclass(frozen=True)
class EventRule:
    threshold_seconds: float
    cooldown_seconds: float
    severity: Severity
    message: str

    def __post_init__(self) -> None:
        if not isfinite(self.threshold_seconds) or self.threshold_seconds <= 0:
            raise ValueError("Event duration thresholds must be finite and positive")
        if not isfinite(self.cooldown_seconds) or self.cooldown_seconds < 0:
            raise ValueError("Event cooldowns must be finite and nonnegative")


def _default_rules() -> dict[EventType, EventRule]:
    return {
        EventType.PHONE_DETECTED: EventRule(0.7, 10.0, Severity.CRITICAL, "Cell phone detected"),
        EventType.MULTIPLE_PERSONS: EventRule(1.0, 10.0, Severity.CRITICAL, "Multiple persons detected"),
        EventType.MULTIPLE_FACES: EventRule(1.0, 10.0, Severity.CRITICAL, "Multiple faces detected"),
        EventType.FACE_MISSING: EventRule(3.0, 10.0, Severity.HIGH, "Face missing"),
        EventType.LOOK_LEFT: EventRule(4.0, 8.0, Severity.MEDIUM, "Looking left"),
        EventType.LOOK_RIGHT: EventRule(4.0, 8.0, Severity.MEDIUM, "Looking right"),
        EventType.LOOK_UP: EventRule(4.0, 8.0, Severity.MEDIUM, "Looking up"),
        EventType.LOOK_DOWN: EventRule(4.0, 8.0, Severity.MEDIUM, "Looking down"),
        EventType.CAMERA_OBSTRUCTED: EventRule(2.0, 10.0, Severity.HIGH, "Camera view obstructed"),
    }


@dataclass(frozen=True)
class PhoneDetectionConfig:
    """Application phone confidence and conservative temporal association.

    Grace retains a probable phone only after multiple confident observations.
    Event qualification still requires the existing observed-duration threshold;
    unobserved grace time never contributes to that duration.
    """

    minimum_confidence: float = 0.55
    tracking_enabled: bool = True
    occlusion_grace_seconds: float = 0.6
    minimum_tracking_hits: int = 2
    minimum_association_iou: float = 0.15
    maximum_center_distance_ratio: float = 0.5
    maximum_area_ratio: float = 2.5

    def __post_init__(self) -> None:
        if not isfinite(self.minimum_confidence) or not 0 < self.minimum_confidence <= 1:
            raise ValueError("Phone confidence must be finite and between 0 and 1")
        if not isinstance(self.tracking_enabled, bool):
            raise ValueError("Phone tracking_enabled must be a boolean")
        if not isfinite(self.occlusion_grace_seconds) or self.occlusion_grace_seconds < 0:
            raise ValueError("Phone occlusion grace must be finite and nonnegative")
        if (isinstance(self.minimum_tracking_hits, bool)
                or not isinstance(self.minimum_tracking_hits, int) or self.minimum_tracking_hits < 2):
            raise ValueError("Phone tracking requires at least two confident hits")
        if not isfinite(self.minimum_association_iou) or not 0 < self.minimum_association_iou <= 1:
            raise ValueError("Phone association IoU must be finite and between 0 and 1")
        if (not isfinite(self.maximum_center_distance_ratio)
                or not 0 <= self.maximum_center_distance_ratio <= 1):
            raise ValueError("Phone center distance ratio must be finite and between 0 and 1")
        if not isfinite(self.maximum_area_ratio) or self.maximum_area_ratio < 1:
            raise ValueError("Phone association area ratio must be finite and at least one")

    def accepts(self, confidence: float | None) -> bool:
        return (confidence is not None and isfinite(confidence)
                and self.minimum_confidence <= confidence <= 1)


@dataclass(frozen=True)
class MultiplePersonsConfig:
    """Validate events independently of the raw YOLO display/count threshold.

    The reference person and strongest geometry-qualified secondary person must
    pass the existing weaker-pair confidence floor. Geometry rules apply only
    to secondary candidates in the shared CV signal adapter.
    Sustained multi-face corroboration allows a shorter persistence requirement;
    a second person with no visible face can still qualify through YOLO alone.
    """

    minimum_secondary_confidence: float = 0.60
    uncorroborated_persistence_seconds: float = 3.0
    corroborated_persistence_seconds: float = 1.0
    minimum_secondary_area_ratio: float = 0.02
    minimum_secondary_width_ratio: float = 0.05
    minimum_secondary_height_ratio: float = 0.15

    def __post_init__(self) -> None:
        if not isfinite(self.minimum_secondary_confidence) or not 0 < self.minimum_secondary_confidence <= 1:
            raise ValueError("Secondary person confidence must be finite and between 0 and 1")
        for duration in (self.uncorroborated_persistence_seconds, self.corroborated_persistence_seconds):
            if not isfinite(duration) or duration <= 0:
                raise ValueError("Secondary person persistence must be finite and positive")
        if self.corroborated_persistence_seconds > self.uncorroborated_persistence_seconds:
            raise ValueError("Face-corroborated persistence cannot exceed uncorroborated persistence")
        for ratio in (self.minimum_secondary_area_ratio, self.minimum_secondary_width_ratio,
                      self.minimum_secondary_height_ratio):
            if not isfinite(ratio) or not 0 <= ratio <= 1:
                raise ValueError("Secondary person geometry ratios must be finite and between 0 and 1")


@dataclass(frozen=True)
class EventEngineConfig:
    rules: Mapping[EventType, EventRule] = field(default_factory=_default_rules)
    latest_event_display_seconds: float = 3.0
    multiple_persons: MultiplePersonsConfig = field(default_factory=MultiplePersonsConfig)
    phone: PhoneDetectionConfig = field(default_factory=PhoneDetectionConfig)

    def __post_init__(self) -> None:
        if set(self.rules) != CV_EVENT_TYPES:
            raise ValueError("Configure exactly one duration rule for each CV event type")
        if not isfinite(self.latest_event_display_seconds) or self.latest_event_display_seconds <= 0:
            raise ValueError("Latest event display duration must be finite and positive")
        if not isinstance(self.multiple_persons, MultiplePersonsConfig):
            raise ValueError("Multiple-person validation must be MultiplePersonsConfig")
        if not isinstance(self.phone, PhoneDetectionConfig):
            raise ValueError("Phone validation must be PhoneDetectionConfig")
        # Copy to keep caller changes from altering rules during an active session.
        object.__setattr__(self, "rules", MappingProxyType(dict(self.rules)))


@dataclass(frozen=True)
class FrameSignals:
    """Current-frame signals; strings also accept the vision module's StrEnums.

    UNAVAILABLE (or an omitted face status) disables all face-related conditions.
    Confidence is the current frame's evidence, not an aggregate probability.
    The CV adapter applies secondary geometry validation before supplying
    persons_confidence (the weaker confidence of the primary/reference person
    and the strongest geometry-qualified secondary person). Aggregated signal
    producers must perform geometry validation before calling this engine;
    missing or invalid confidence cannot qualify a multiple-person event.
    """

    phone_detected: bool = False
    person_count: int = 0
    face_status: str = "UNAVAILABLE"
    face_count: int | None = None
    head_direction: str = "UNKNOWN"
    phone_confidence: float | None = None
    persons_confidence: float | None = None
    camera_obstructed: bool = False
    camera_quality: dict[str, Any] | None = None
    authorized_break: bool = False
    # Optional tracker data. Omitted fields retain the original raw-detection
    # contract used by signal producers and existing engine integrations.
    phone_tracking_state: str | None = None
    phone_track_id: int | None = None
    phone_observed_duration_seconds: float | None = None
    phone_tracking_hits: int | None = None


@dataclass(frozen=True)
class ProctoringEvent:
    type: EventType
    timestamp: datetime
    severity: Severity
    duration: float
    message: str
    confidence: float | None = None
    metadata: dict[str, Any] | None = None
    evidence_path: str | None = None
    risk_delta: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible event with a nullable evidence path."""
        result = {
            "type": self.type.value,
            "timestamp": self.timestamp.isoformat(),
            "severity": self.severity.value,
            "duration": self.duration,
            "message": self.message,
            "evidence_path": self.evidence_path,
        }
        if self.confidence is not None:
            result["confidence"] = self.confidence
        if self.risk_delta is not None:
            result["risk_delta"] = self.risk_delta
        if self.metadata is not None:
            result["metadata"] = dict(self.metadata)
        return result


@dataclass
class _ConditionState:
    started_at: float | None = None
    last_emitted_at: float | None = None
    emitted_for_episode: bool = False


class EventEngine:
    """One event per uninterrupted episode, with independent type cooldowns.

    A new episode blocked by cooldown can emit once both its duration threshold
    and the cooldown from the previous emission have elapsed. A continuously
    active episode never repeats, even after its cooldown expires. Call update
    for every processed frame. History belongs to this engine/session only.
    """

    def __init__(
        self,
        config: EventEngineConfig | None = None,
        *,
        clock: Callable[[], float] = perf_counter,
        wall_clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self.config = config if config is not None else EventEngineConfig()
        self._clock = clock
        self._wall_clock = wall_clock
        self._states = {event_type: _ConditionState() for event_type in self.config.rules}
        self._last_update_at: float | None = None
        self._person_corroboration_started_at: float | None = None
        self._phone_track_id: int | None = None
        self.history: list[ProctoringEvent] = []

    def record_security_events(self, events: Iterable[ProctoringEvent]) -> list[ProctoringEvent]:
        """Store normalized security impulses already debounced by their monitor.

        Security shortcuts have no continuous-frame threshold. Their independent
        monitor owns edge tracking and cooldowns; CV tracking is left untouched.
        Validate the whole batch before modifying session history.
        """
        events = list(events)
        if any(not isinstance(event, ProctoringEvent) or event.type not in SECURITY_EVENT_TYPES for event in events):
            raise ValueError("Only normalized security events can enter this flow")
        self.history.extend(events)
        return events

    def record_session_events(self, events: Iterable[ProctoringEvent]) -> list[ProctoringEvent]:
        """Record break transitions produced by the authorization state machine."""
        events = list(events)
        if any(not isinstance(event, ProctoringEvent) or event.type not in SESSION_EVENT_TYPES for event in events):
            raise ValueError("Only normalized session events can enter this flow")
        self.history.extend(events)
        return events

    def condition_duration(self, event_type: EventType) -> float:
        """Current continuous duration, independent of emission cooldowns."""
        state = self._states[event_type]
        if state.started_at is None or self._last_update_at is None:
            return 0.0
        return self._last_update_at - state.started_at

    def reset_condition(self, event_type: EventType) -> None:
        """Reset an episode on an authorized transition, retaining its cooldown."""
        state = self._states[event_type]
        state.started_at = None
        state.emitted_for_episode = False

    def condition_qualified(self, event_type: EventType) -> bool:
        """Whether a live condition reached its warning threshold, even in cooldown."""
        return (self._states[event_type].started_at is not None
                and self.condition_duration(event_type) >= self.config.rules[event_type].threshold_seconds)

    def update(self, signals: FrameSignals, *, now: float | None = None) -> list[ProctoringEvent]:
        """Return only newly emitted events, in EventType declaration order.

        Explicit now values or an injected clock allow deterministic tests.
        Timestamps must be finite and nondecreasing; bad time does not mutate
        tracking state or history.
        """
        now = self._clock() if now is None else now
        if not isfinite(now) or (self._last_update_at is not None and now < self._last_update_at):
            raise ValueError("Event time must be finite and nondecreasing")
        self._last_update_at = now
        if (signals.phone_track_id is not None and self._phone_track_id is not None
                and signals.phone_track_id != self._phone_track_id):
            # Two unrelated boxes must not combine their persistence or latch.
            self.reset_condition(EventType.PHONE_DETECTED)
        self._phone_track_id = signals.phone_track_id
        single_face = signals.face_status == "FACE_DETECTED"
        validation = self.config.multiple_persons
        secondary_confidence = signals.persons_confidence
        secondary_valid = (
            signals.person_count > 1 and secondary_confidence is not None
            and isfinite(secondary_confidence)
            and validation.minimum_secondary_confidence <= secondary_confidence <= 1
        )
        face_corroborated = (
            secondary_valid and signals.face_status == "MULTIPLE_FACES"
            and signals.face_count is not None and signals.face_count >= 2
        )
        if face_corroborated:
            if self._person_corroboration_started_at is None:
                self._person_corroboration_started_at = now
        else:
            self._person_corroboration_started_at = None
        # A raw low-texture/dark sample is only an obstruction candidate.
        # Short candidates must not restart otherwise continuous face absence.
        # Use the current duration (including this sample), not last frame's
        # warning or event emission: a qualified cover wins even in cooldown.
        obstruction_state = self._states[EventType.CAMERA_OBSTRUCTED]
        obstruction_confirmed = (
            signals.camera_obstructed and obstruction_state.started_at is not None
            and now - obstruction_state.started_at
            >= self.config.rules[EventType.CAMERA_OBSTRUCTED].threshold_seconds
        )
        conditions = {
            EventType.PHONE_DETECTED: signals.phone_detected and self.config.phone.accepts(signals.phone_confidence),
            EventType.MULTIPLE_PERSONS: secondary_valid,
            EventType.MULTIPLE_FACES: signals.face_status == "MULTIPLE_FACES",
            EventType.FACE_MISSING: (signals.face_status == "NO_FACE"
                                     and not signals.authorized_break and not obstruction_confirmed),
            EventType.LOOK_LEFT: single_face and signals.head_direction == "LEFT",
            EventType.LOOK_RIGHT: single_face and signals.head_direction == "RIGHT",
            EventType.LOOK_UP: single_face and signals.head_direction == "UP",
            EventType.LOOK_DOWN: single_face and signals.head_direction == "DOWN",
            EventType.CAMERA_OBSTRUCTED: signals.camera_obstructed,
        }
        events = []
        for event_type, active in conditions.items():
            state = self._states[event_type]
            if not active:
                state.started_at = None
                state.emitted_for_episode = False
                continue
            if state.started_at is None:
                state.started_at = now
            if state.emitted_for_episode:
                continue
            rule = self.config.rules[event_type]
            duration = now - state.started_at
            if duration < rule.threshold_seconds:
                continue
            if event_type == EventType.PHONE_DETECTED and signals.phone_tracking_state is not None:
                observed_duration = signals.phone_observed_duration_seconds
                if (signals.phone_tracking_state != "DETECTED" or observed_duration is None
                        or not isfinite(observed_duration) or observed_duration < rule.threshold_seconds
                        or signals.phone_tracking_hits is None
                        or signals.phone_tracking_hits < self.config.phone.minimum_tracking_hits):
                    # Occlusion can preserve an episode, never qualify a new
                    # violation using prediction or one isolated confident box.
                    continue
            person_validation = None
            if event_type == EventType.MULTIPLE_PERSONS:
                corroboration_duration = (
                    0.0 if self._person_corroboration_started_at is None
                    else now - self._person_corroboration_started_at
                )
                corroborated_threshold = max(rule.threshold_seconds, validation.corroborated_persistence_seconds)
                uncorroborated_threshold = max(rule.threshold_seconds, validation.uncorroborated_persistence_seconds)
                corroboration_ready = face_corroborated and corroboration_duration >= corroborated_threshold
                if not corroboration_ready and duration < uncorroborated_threshold:
                    continue
                person_validation = {
                    "minimum_secondary_confidence": validation.minimum_secondary_confidence,
                    "path": "face_corroborated" if corroboration_ready else "persistent_yolo",
                    "required_persistence_seconds": corroborated_threshold if corroboration_ready else uncorroborated_threshold,
                    "face_corroboration_duration": corroboration_duration,
                    "minimum_secondary_area_ratio": validation.minimum_secondary_area_ratio,
                    "minimum_secondary_width_ratio": validation.minimum_secondary_width_ratio,
                    "minimum_secondary_height_ratio": validation.minimum_secondary_height_ratio,
                }
            if state.last_emitted_at is not None and now - state.last_emitted_at < rule.cooldown_seconds:
                continue
            confidence = None
            if event_type == EventType.PHONE_DETECTED:
                confidence = signals.phone_confidence
            elif event_type == EventType.MULTIPLE_PERSONS:
                confidence = signals.persons_confidence
            metadata = {
                "person_count": signals.person_count,
                "face_status": str(signals.face_status),
                "face_count": signals.face_count,
                "head_direction": str(signals.head_direction),
            }
            if person_validation is not None:
                metadata["multiple_persons_validation"] = person_validation
            if event_type == EventType.PHONE_DETECTED and signals.phone_tracking_state is not None:
                metadata["phone_tracking"] = {
                    "state": str(signals.phone_tracking_state),
                    "track_id": signals.phone_track_id,
                    "observed_duration_seconds": signals.phone_observed_duration_seconds,
                    "confident_hits": signals.phone_tracking_hits,
                    "occlusion_grace_seconds": self.config.phone.occlusion_grace_seconds,
                }
            if event_type == EventType.CAMERA_OBSTRUCTED:
                metadata["camera_quality"] = dict(signals.camera_quality or {})
            event = ProctoringEvent(
                type=event_type,
                timestamp=self._wall_clock(),
                severity=rule.severity,
                duration=duration,
                message=rule.message,
                confidence=confidence,
                metadata=metadata,
            )
            state.last_emitted_at = now
            state.emitted_for_episode = True
            self.history.append(event)
            events.append(event)
        return events
