"""Instructor-authorized local breaks, with deterministic monotonic deadlines."""

from dataclasses import dataclass, field
from datetime import datetime
from hmac import compare_digest
from math import ceil, isfinite
import os

from .event_engine import EventType, ProctoringEvent, Severity

DEMO_TEACHER_PIN = "1234"
TEACHER_PIN_ENV = "AI_EXAM_TEACHER_PIN"


@dataclass(frozen=True)
class BreakConfig:
    teacher_pin: str = field(default_factory=lambda: os.environ.get(TEACHER_PIN_ENV, DEMO_TEACHER_PIN), repr=False)
    durations_seconds: tuple[int, ...] = (60, 180, 300)

    def __post_init__(self):
        if not isinstance(self.teacher_pin, str) or not self.teacher_pin:
            raise ValueError("Teacher PIN must be a nonempty string")
        if not self.durations_seconds or any(type(value) is not int or value <= 0 for value in self.durations_seconds):
            raise ValueError("Break durations must be positive integer seconds")
        object.__setattr__(self, "durations_seconds", tuple(self.durations_seconds))

    def accepts_pin(self, pin: str) -> bool:
        return isinstance(pin, str) and compare_digest(pin.encode("utf-8"), self.teacher_pin.encode("utf-8"))


class BreakManager:
    def __init__(self, config: BreakConfig, *, wall_clock=lambda: datetime.now().astimezone()):
        self.config, self._wall_clock = config, wall_clock
        self._deadline = self._started_at = None
        self._last_at = None

    @property
    def active(self):
        return self._deadline is not None

    def _check_time(self, now):
        if not isfinite(now) or (self._last_at is not None and now < self._last_at):
            raise ValueError("Break time must be finite and nondecreasing")
        self._last_at = now

    def remaining_seconds(self, now):
        return max(0, ceil(self._deadline - now)) if self.active else 0

    def _event(self, kind, now, message, metadata=None):
        return ProctoringEvent(kind, self._wall_clock(), Severity.INFO,
                              0 if self._started_at is None else now - self._started_at,
                              message, metadata=metadata)

    def start(self, pin: str, duration: int, *, now: float):
        self._check_time(now)
        if (self.active or not self.config.accepts_pin(pin) or type(duration) is not int
                or duration not in self.config.durations_seconds):
            return None
        self._started_at, self._deadline = now, now + duration
        return self._event(EventType.AUTHORIZED_BREAK_STARTED, now, "Instructor-authorized break started",
                           {"duration_seconds": duration})

    def end(self, *, now: float):
        self._check_time(now)
        if not self.active:
            return None
        # At the deadline the break has expired, even if END is queued concurrently.
        if now >= self._deadline:
            return self.expire(now=now)
        event = self._event(EventType.AUTHORIZED_BREAK_ENDED, now, "Authorized break ended early")
        self._deadline = self._started_at = None
        return event

    def expire(self, *, now: float):
        self._check_time(now)
        if not self.active or now < self._deadline:
            return None
        event = self._event(EventType.AUTHORIZED_BREAK_EXPIRED, now, "Authorized break expired")
        self._deadline = self._started_at = None
        return event
