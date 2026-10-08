"""Best-effort exam window recovery, separate from native keyboard hooks."""

from dataclasses import dataclass
from math import isfinite
from time import perf_counter

from monitoring import EventType
from .security_monitor import SUPPRESSIBLE_EVENTS


@dataclass(frozen=True)
class SecureWindowConfig:
    # The desktop entry point requires secure mode. Trusted local launchers may
    # keep WINDOWED for development; choosing a secure mode always activates it.
    required: bool = False
    focus_recovery_enabled: bool = True
    focus_recovery_cooldown_seconds: float = 1.0
    suppressed_shortcuts: frozenset[EventType] = frozenset()

    def __post_init__(self):
        if not isinstance(self.required, bool) or not isinstance(self.focus_recovery_enabled, bool):
            raise ValueError("Secure window switches must be boolean")
        value = self.focus_recovery_cooldown_seconds
        if isinstance(value, bool) or not isfinite(value) or value < 0:
            raise ValueError("Focus recovery cooldown must be finite and nonnegative")
        if not set(self.suppressed_shortcuts) <= SUPPRESSIBLE_EVENTS:
            raise ValueError("Secure mode supports only existing safe shortcut suppression")
        object.__setattr__(self, "suppressed_shortcuts", frozenset(self.suppressed_shortcuts))


class SecureWindowGuard:
    """Rate-limit Qt restore/activation requests; never promise OS lockdown.

    The caller records focus loss before calling recover. A window adapter makes
    this testable without foregrounding an OS window or pressing native keys.
    """

    def __init__(self, config=None, *, clock=perf_counter):
        self.config = config or SecureWindowConfig()
        self._clock = clock
        self.active = False
        self.mode = "WINDOWED"
        self._last_attempt = None
        self.last_error = None

    def activate(self, mode):
        if mode not in {"WINDOWED", "MAXIMIZED", "FULLSCREEN"}:
            raise ValueError("Unsupported secure window mode")
        self.mode = mode
        self.active = mode != "WINDOWED"
        self._last_attempt = None
        self.last_error = None

    def release(self):
        self.active = False

    def recover(self, window):
        if not self.active or not self.config.focus_recovery_enabled:
            return False
        now = self._clock()
        if not isfinite(now):
            return False
        if (self._last_attempt is not None
                and now - self._last_attempt < self.config.focus_recovery_cooldown_seconds):
            return False
        self._last_attempt = now
        self.last_error = None
        # These are independent attempts. A failed raise must not prevent the
        # activation request. Windows may deny foreground activation altogether.
        actions = []
        try:
            minimized = window.isMinimized()
        except Exception as exc:
            minimized = False
            self.last_error = f"{type(exc).__name__}: {exc}"
        if minimized:
            actions.append(window.showFullScreen if self.mode == "FULLSCREEN" else window.showMaximized)
        actions.extend((window.raise_, window.activateWindow))
        for action in actions:
            try:
                action()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
        return True
