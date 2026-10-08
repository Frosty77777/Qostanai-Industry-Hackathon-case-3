"""Deterministic mascot presentation and optional local user preferences.

This module never scores events, reads evidence or changes monitoring. Only
known event types select fixed, supportive prompts. The Qt widget owns a
single-shot timer based on ``next_deadline``; this model has no animation loop.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
import json
import logging
import math
import os
from pathlib import Path
import tempfile
from time import perf_counter
from types import MappingProxyType


ROOT = Path(__file__).resolve().parents[2]
CONTEXTS = frozenset({"setup", "exam", "completion"})
PREFERENCES_SCHEMA = 1
MAX_PREFERENCES_BYTES = 32_768
LOGGER = logging.getLogger(__name__)


class AssistantState(StrEnum):
    CALM = "CALM"
    NEUTRAL = "NEUTRAL"
    ALERT = "ALERT"
    SERIOUS = "SERIOUS"
    CRITICAL = "CRITICAL"


def _asset_files() -> dict[AssistantState, str]:
    return {
        AssistantState.CALM: "calm.png",
        AssistantState.NEUTRAL: "neutral.png",
        AssistantState.ALERT: "warning.png",
        AssistantState.SERIOUS: "serious.png",
        AssistantState.CRITICAL: "critical.png",
    }


def _state_messages() -> dict[AssistantState, str]:
    return {
        AssistantState.CALM: "Everything looks good. Good luck.",
        AssistantState.NEUTRAL: "Please stay focused on the exam.",
        AssistantState.ALERT: "Suspicious activity has been detected.",
        AssistantState.SERIOUS: "Please return your attention to the exam.",
        AssistantState.CRITICAL: (
            "Repeated violations detected. The instructor will review this session."
        ),
    }


def _event_messages() -> dict[str, str]:
    return {
        "PHONE_DETECTED": "Please put your phone away.",
        "FACE_MISSING": "Please return to the camera view.",
        "CAMERA_OBSTRUCTED": "Please clear the camera view.",
        "MULTIPLE_PERSONS": "Please make sure only you are in the camera view.",
        "MULTIPLE_FACES": "Please make sure only you are in the camera view.",
        "LOOK_LEFT": "Please return your attention to the exam.",
        "LOOK_RIGHT": "Please return your attention to the exam.",
        "LOOK_UP": "Please return your attention to the exam.",
        "LOOK_DOWN": "Please return your attention to the exam.",
        "ALT_TAB_ATTEMPT": "Leaving the exam window is not allowed.",
        "WINDOW_FOCUS_LOST": "Leaving the exam window is not allowed.",
        "COPY_ATTEMPT": "Please use only permitted exam controls.",
        "PASTE_ATTEMPT": "Please use only permitted exam controls.",
        "PRINTSCREEN_ATTEMPT": "Please keep exam content within this window.",
        "ESCAPE_ATTEMPT": "Please stay in the exam window.",
        "AUTHORIZED_BREAK_STARTED": "Authorized break started.",
        "AUTHORIZED_BREAK_ENDED": "Authorized break ended. Monitoring is active again.",
        "AUTHORIZED_BREAK_EXPIRED": "Authorized break ended. Monitoring is active again.",
    }


BREAK_EVENT_TYPES = frozenset({
    "AUTHORIZED_BREAK_STARTED", "AUTHORIZED_BREAK_ENDED", "AUTHORIZED_BREAK_EXPIRED"
})


@dataclass(frozen=True)
class AssistantConfig:
    """Central presentation settings; defaults do not need real image files."""

    asset_directory: Path = ROOT / "assets" / "assistant"
    asset_files: Mapping[AssistantState, str] = field(default_factory=_asset_files)
    state_messages: Mapping[AssistantState, str] = field(default_factory=_state_messages)
    event_messages: Mapping[str, str] = field(default_factory=_event_messages)
    risk_boundaries: tuple[int, int, int, int] = (20, 45, 70, 85)
    event_message_seconds: float = 4.0
    event_debounce_seconds: float = 0.75
    setup_message: str = "Welcome to AI Exam Guard. Good luck on your exam."
    setup_reminder: str = "Please stay focused and keep your face visible during the session."
    break_message: str = "Authorized break is active. Take a short rest."
    completion_message: str = "Your exam has been submitted successfully."

    def __post_init__(self) -> None:
        if set(self.asset_files) != set(AssistantState):
            raise ValueError("Configure an asset filename for each assistant state")
        if set(self.state_messages) != set(AssistantState):
            raise ValueError("Configure a generic message for each assistant state")
        boundaries = self.risk_boundaries
        if len(boundaries) != 4 or any(type(value) is not int for value in boundaries):
            raise ValueError("Assistant risk boundaries must contain four integers")
        if not (0 <= boundaries[0] < boundaries[1] < boundaries[2] < boundaries[3] < 100):
            raise ValueError("Assistant risk boundaries must increase within 0 to 100")
        for value in (self.event_message_seconds, self.event_debounce_seconds):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("Assistant message timing must be finite")
        if self.event_message_seconds <= 0 or self.event_debounce_seconds < 0:
            raise ValueError("Require positive message duration and nonnegative debounce")
        for filename in self.asset_files.values():
            if (not isinstance(filename, str) or not filename
                    or Path(filename).name != filename or "/" in filename or "\\" in filename):
                raise ValueError("Assistant assets must be filenames within the asset directory")
        all_messages = (*self.state_messages.values(), *self.event_messages.values(),
                        self.setup_message, self.setup_reminder, self.break_message,
                        self.completion_message)
        if any(not isinstance(message, str) or not message.strip() for message in all_messages):
            raise ValueError("Assistant messages must be nonempty strings")
        if any(not isinstance(kind, str) or not kind for kind in self.event_messages):
            raise ValueError("Assistant event message keys must be event type strings")
        object.__setattr__(self, "asset_directory", Path(self.asset_directory))
        object.__setattr__(self, "asset_files", MappingProxyType(dict(self.asset_files)))
        object.__setattr__(self, "state_messages", MappingProxyType(dict(self.state_messages)))
        object.__setattr__(self, "event_messages", MappingProxyType(dict(self.event_messages)))
        object.__setattr__(self, "risk_boundaries", tuple(boundaries))

    def asset_path(self, state: AssistantState | str) -> Path:
        return self.asset_directory / self.asset_files[AssistantState(state)]

    def state_for_risk(self, score: int) -> AssistantState:
        if type(score) is not int:
            raise TypeError("Assistant risk score must be an integer")
        clamped = min(100, max(0, score))
        for state, upper in zip(AssistantState, self.risk_boundaries):
            if clamped <= upper:
                return state
        return AssistantState.CRITICAL


class AssistantModel:
    """Risk-driven visual state with debounced, temporary fixed prompts.

    The first important event appears immediately. Events within the configured
    debounce interval replace a single pending prompt, which appears once at
    the next boundary. Identical visible prompts do not extend their lifetime.
    A model reset clears all transient state without touching user preferences.
    """

    def __init__(self, context: str = "setup", *, config: AssistantConfig | None = None,
                 clock: Callable[[], float] = perf_counter) -> None:
        self.config = config or AssistantConfig()
        self._clock = clock
        self._last_clock: float | None = None
        self.reset(context)

    @property
    def now(self) -> float:
        value = self._clock()
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)):
            raise ValueError("Assistant clock must return a finite monotonic time")
        if self._last_clock is not None and value < self._last_clock:
            raise ValueError("Assistant clock must not move backwards")
        self._last_clock = float(value)
        return float(value)

    @property
    def state(self) -> AssistantState:
        return self.config.state_for_risk(self.risk_score)

    @property
    def risk_score(self) -> int:
        return self._risk_score

    @property
    def break_active(self) -> bool:
        return self._break_active

    @property
    def next_deadline(self) -> float | None:
        deadlines = tuple(deadline for deadline in (self._pending_at, self._override_until)
                          if deadline is not None)
        return min(deadlines) if deadlines else None

    def delay_until_deadline(self) -> float | None:
        deadline = self.next_deadline
        return None if deadline is None else max(0.0, deadline - self.now)

    def reset(self, context: str = "setup") -> None:
        if context not in CONTEXTS:
            raise ValueError("Assistant context must be setup, exam, or completion")
        self.context = context
        self._risk_score = 0
        self._break_active = False
        self._override: str | None = None
        self._override_kind: str | None = None
        self._override_until: float | None = None
        self._pending: tuple[str, str] | None = None
        self._pending_at: float | None = None
        self._last_event_shown: float | None = None

    def _generic_message(self) -> str:
        if self.context == "setup":
            return self.config.setup_message
        if self.context == "completion":
            return self.config.completion_message
        if self.break_active:
            return self.config.break_message
        return self.config.state_messages[self.state]

    def _displayed_message(self) -> str:
        return self._override or self._generic_message()

    def current_message(self) -> str:
        self.advance()
        return self._displayed_message()

    def set_risk(self, score: int) -> bool:
        advanced = self.advance()
        if type(score) is not int:
            raise TypeError("Assistant risk score must be an integer")
        before = (self.state, self._displayed_message())
        self._risk_score = min(100, max(0, score))
        return advanced or before != (self.state, self._displayed_message())

    def set_break_active(self, active: bool) -> bool:
        if type(active) is not bool:
            raise TypeError("Assistant break status must be a boolean")
        advanced = self.advance()
        before = self._displayed_message()
        if active != self._break_active:
            self._break_active = active
            if self._override_kind not in BREAK_EVENT_TYPES:
                self._override = self._override_kind = self._override_until = None
            if self._pending is not None and self._pending[0] not in BREAK_EVENT_TYPES:
                self._pending = self._pending_at = None
        return advanced or before != self._displayed_message()

    def _show_event(self, kind: str, message: str, now: float) -> None:
        self._override = message
        self._override_kind = kind
        self._override_until = now + self.config.event_message_seconds
        self._last_event_shown = now

    def handle_event(self, event_or_type: object) -> bool:
        """Read only a known type; never render arbitrary event/report content."""
        self.advance()
        kind = getattr(event_or_type, "type", event_or_type)
        if not isinstance(kind, str):
            return False
        message = self.config.event_messages.get(kind)
        if (message is None or self.context != "exam"
                or (self.break_active and kind not in BREAK_EVENT_TYPES)):
            return False
        if message == self._override:
            # The newest event agrees with the visible prompt; cancel an older
            # queued alternative without extending the visible prompt's timer.
            self._pending = self._pending_at = None
            return False
        now = self.now
        if (self._last_event_shown is None
                or now >= self._last_event_shown + self.config.event_debounce_seconds):
            before = self._displayed_message()
            self._pending = self._pending_at = None
            self._show_event(kind, message, now)
            return before != self._displayed_message()
        self._pending = (kind, message)
        self._pending_at = self._last_event_shown + self.config.event_debounce_seconds
        return False

    def advance(self) -> bool:
        """Process due transitions once; suitable for a Qt single-shot timeout."""
        now = self.now
        before = self._displayed_message()
        if self._override_until is not None and now >= self._override_until:
            self._override = self._override_kind = self._override_until = None
        if self._pending_at is not None and now >= self._pending_at:
            pending = self._pending
            scheduled_at = self._pending_at
            self._pending = self._pending_at = None
            if (pending is not None
                    and now < scheduled_at + self.config.event_message_seconds):
                # A hidden widget or delayed event loop must not restart an old
                # prompt when its single-shot timer eventually gets a turn.
                self._show_event(*pending, scheduled_at)
        return before != self._displayed_message()


def _normalized_position(position: object) -> tuple[float, float]:
    if not isinstance(position, (tuple, list)) or len(position) != 2:
        raise ValueError("Assistant position must contain two normalized coordinates")
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or not 0 <= value <= 1 for value in position):
        raise ValueError("Assistant position coordinates must be finite values within 0 to 1")
    return float(position[0]), float(position[1])


@dataclass(frozen=True)
class AssistantPreferences:
    visible: bool = True
    message_visible: bool = True
    positions: Mapping[str, tuple[float, float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.visible) is not bool or type(self.message_visible) is not bool:
            raise ValueError("Assistant visibility preferences must be booleans")
        if not isinstance(self.positions, Mapping):
            raise ValueError("Assistant positions must be a context mapping")
        if any(context not in CONTEXTS for context in self.positions):
            raise ValueError("Unknown assistant position context")
        positions = {context: _normalized_position(position)
                     for context, position in self.positions.items()}
        object.__setattr__(self, "positions", MappingProxyType(positions))

    def position(self, context: str, default: tuple[float, float] = (1.0, 1.0)) -> tuple[float, float]:
        if context not in CONTEXTS:
            raise ValueError("Unknown assistant position context")
        return self.positions.get(context, _normalized_position(default))


def default_preferences_path() -> Path:
    local_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_data) if local_data else Path.home() / "AppData" / "Local"
    return base / "AIExamGuard" / "assistant.json"


class AssistantPreferencesStore:
    """Small, explicit JSON preference writes with shared in-memory updates.

    Loading missing/corrupt settings never creates a file. Only a user toggle
    or completed drag should call ``save``/``update``. Errors leave the current
    session usable, including when its preference directory is unwritable.
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_preferences_path()
        self.error: str | None = None
        self._preferences = AssistantPreferences()
        self._dirty = False
        self._subscribers: list[Callable[[AssistantPreferences], None]] = []
        self.load()

    @property
    def preferences(self) -> AssistantPreferences:
        return self._preferences

    def subscribe(self, callback: Callable[[AssistantPreferences], None]) -> Callable[[], None]:
        if not callable(callback):
            raise TypeError("Assistant preference observer must be callable")
        if callback not in self._subscribers:
            self._subscribers.append(callback)
        return lambda: self.unsubscribe(callback)

    def unsubscribe(self, callback: Callable[[AssistantPreferences], None]) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def _publish(self, preferences: AssistantPreferences) -> None:
        changed = preferences != self._preferences
        self._preferences = preferences
        if changed:
            for callback in tuple(self._subscribers):
                try:
                    callback(preferences)
                except Exception:
                    LOGGER.exception("Assistant preference observer failed")

    def _failure(self, operation: str, exc: Exception) -> None:
        self.error = f"Assistant preferences {operation} failed: {type(exc).__name__}: {exc}"
        LOGGER.warning(self.error)

    def load(self) -> AssistantPreferences:
        # Explicit user choices take precedence for this running application
        # even if saving was denied. A later page show must not re-enable a
        # hidden mascot by reloading missing or stale disk settings.
        if self._dirty:
            return self.preferences
        self.error = None
        preferences = AssistantPreferences()
        try:
            if self.path.exists():
                if self.path.stat().st_size > MAX_PREFERENCES_BYTES:
                    raise ValueError("Preference file exceeds the size limit")
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or raw.get("version", PREFERENCES_SCHEMA) != PREFERENCES_SCHEMA:
                    raise ValueError("Unsupported assistant preference format")
                visible = raw.get("visible", True)
                message_visible = raw.get("message_visible", True)
                visible = visible if type(visible) is bool else True
                message_visible = message_visible if type(message_visible) is bool else True
                positions = {}
                raw_positions = raw.get("positions", {})
                if isinstance(raw_positions, dict):
                    for context, position in raw_positions.items():
                        if context in CONTEXTS:
                            try:
                                positions[context] = _normalized_position(position)
                            except ValueError:
                                pass
                preferences = AssistantPreferences(visible, message_visible, positions)
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            self._failure("load", exc)
        self._publish(preferences)
        return preferences

    def save(self, preferences: AssistantPreferences | None = None) -> bool:
        if preferences is None:
            preferences = self.preferences
        if not isinstance(preferences, AssistantPreferences):
            raise TypeError("Save AssistantPreferences only")
        self._dirty = True
        self._publish(preferences)
        self.error = None
        temporary: Path | None = None
        try:
            raw = {"version": PREFERENCES_SCHEMA, "visible": preferences.visible,
                   "message_visible": preferences.message_visible,
                   "positions": {context: list(position)
                                 for context, position in preferences.positions.items()}}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=f".{self.path.stem}.", suffix=".tmp",
                                             delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(raw, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
            os.replace(temporary, self.path)
            temporary = None
            self._dirty = False
            return True
        except (OSError, ValueError, TypeError) as exc:
            self._failure("save", exc)
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def update(self, *, visible: bool | None = None, message_visible: bool | None = None,
               context: str | None = None, position: tuple[float, float] | None = None) -> bool:
        changes = {}
        if visible is not None:
            changes["visible"] = visible
        if message_visible is not None:
            changes["message_visible"] = message_visible
        if context is not None or position is not None:
            if context not in CONTEXTS or position is None:
                raise ValueError("Supply both a valid assistant context and position")
            changes["positions"] = dict(self.preferences.positions, **{
                context: _normalized_position(position)
            })
        return self.save(replace(self.preferences, **changes))


# Alias retained for callers that prefer the singular form.
AssistantPreferenceStore = AssistantPreferencesStore
