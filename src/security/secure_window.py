"""Best-effort exam window recovery, separate from native keyboard hooks."""

from dataclasses import dataclass
from math import isfinite
from time import perf_counter

from monitoring import EventType
from .security_monitor import SUPPRESSIBLE_EVENTS
from .windows_focus import WindowsFocusRecovery


@dataclass(frozen=True)
class SecureWindowConfig:
    # The desktop entry point requires secure mode. Trusted local launchers may
    # keep WINDOWED for development; choosing a secure mode always activates it.
    required: bool = False
    focus_recovery_enabled: bool = True
    focus_recovery_cooldown_seconds: float = 1.0
    suppressed_shortcuts: frozenset[EventType] = frozenset()
    focus_recovery_retry_interval_seconds: float = 0.2
    focus_recovery_max_attempts: int = 4
    native_focus_recovery_enabled: bool = True

    def __post_init__(self):
        if (not isinstance(self.required, bool) or not isinstance(self.focus_recovery_enabled, bool)
                or not isinstance(self.native_focus_recovery_enabled, bool)):
            raise ValueError("Secure window switches must be boolean")
        value = self.focus_recovery_cooldown_seconds
        if isinstance(value, bool) or not isfinite(value) or value < 0:
            raise ValueError("Focus recovery cooldown must be finite and nonnegative")
        interval = self.focus_recovery_retry_interval_seconds
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not isfinite(interval) or interval <= 0:
            raise ValueError("Focus recovery retry interval must be finite and positive")
        attempts = self.focus_recovery_max_attempts
        if isinstance(attempts, bool) or not isinstance(attempts, int) or not 1 <= attempts <= 8:
            raise ValueError("Focus recovery must use between one and eight attempts")
        if not set(self.suppressed_shortcuts) <= SUPPRESSIBLE_EVENTS:
            raise ValueError("Secure mode supports only existing safe shortcut suppression")
        object.__setattr__(self, "suppressed_shortcuts", frozenset(self.suppressed_shortcuts))


class SecureWindowGuard:
    """Bound restoration retries per loss episode; never promise OS lockdown.

    The caller records focus loss before calling recover. A window adapter makes
    this testable without foregrounding an OS window or pressing native keys.
    """

    def __init__(self, config=None, *, clock=perf_counter, native_helper=None):
        self.config = config or SecureWindowConfig()
        self._clock = clock
        self.active = False
        self.mode = "WINDOWED"
        self._last_attempt = None
        self.last_error = None
        self._native_helper = native_helper
        self._loss_episode = False
        self._recovery_pending = False
        self._recovery_attempts = 0
        self._next_recovery_at = None
        self._generation = 0

    def activate(self, mode):
        if mode not in {"WINDOWED", "MAXIMIZED", "FULLSCREEN"}:
            raise ValueError("Unsupported secure window mode")
        self.mode = mode
        self.active = mode != "WINDOWED"
        self._last_attempt = None
        self.last_error = None
        self._cancel_recovery()

    def release(self):
        self.active = False
        self._cancel_recovery()

    @property
    def recovery_pending(self):
        return self.active and self.config.focus_recovery_enabled and self._recovery_pending

    @property
    def recovery_attempts(self):
        return self._recovery_attempts

    def _cancel_recovery(self):
        self._generation += 1
        self._loss_episode = False
        self._recovery_pending = False
        self._recovery_attempts = 0
        self._next_recovery_at = None

    def observe_focus(self, focused):
        """A confirmed regain permits a future loss episode, including after exhaustion."""
        if focused is True:
            self._cancel_recovery()

    def begin_recovery(self, window, *, window_handle=None):
        """Start one bounded loss episode after the caller has recorded its event.

        Repeated absence packets do not renew the attempt budget. The Qt controller
        schedules retry_recovery with a short timer; this class never sleeps.
        """
        if not self.active or not self.config.focus_recovery_enabled:
            return False
        # A worker packet or Qt deactivation notification can arrive after a
        # successful native foreground request. Confirm current focus before
        # creating another burst, even if Qt's activation signal has not arrived.
        if self._focused(window, window_handle):
            self.observe_focus(True)
            return False
        if self._loss_episode:
            return False
        now = self._clock()
        if not isfinite(now):
            return False
        self._loss_episode = True
        self._recovery_pending = True
        self._recovery_attempts = 0
        self._next_recovery_at = now
        return self.retry_recovery(window, window_handle=window_handle)

    def _native(self):
        if not self.config.native_focus_recovery_enabled:
            return None
        if self._native_helper is None:
            self._native_helper = WindowsFocusRecovery()
        return self._native_helper

    def _focused(self, window, window_handle):
        native = self._native()
        if native is not None and window_handle is not None:
            try:
                focused = native.is_foreground(window_handle)
                if focused is not None:
                    return focused is True
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
        try:
            return window.isActiveWindow() is True
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False

    def retry_recovery(self, window, *, window_handle=None):
        if not self.recovery_pending or not self.config.focus_recovery_enabled:
            return False
        now = self._clock()
        if not isfinite(now):
            self._recovery_pending = False
            self._next_recovery_at = None
            self.last_error = "Focus recovery clock is unavailable"
            return False
        if now < self._next_recovery_at:
            return False
        if self._recovery_attempts and self._focused(window, window_handle):
            self.observe_focus(True)
            return False
        self.last_error = None
        self._recovery_attempts += 1
        generation = self._generation
        actions = (window.show,
                   window.showFullScreen if self.mode == "FULLSCREEN" else window.showMaximized,
                   window.raise_, window.activateWindow)
        for action in actions:
            if not self.active or self._generation != generation:
                return True
            try:
                action()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
        if not self.active or self._generation != generation:
            return True
        focused = self._focused(window, window_handle)
        if not focused:
            native = self._native()
            if native is not None and window_handle is not None:
                try:
                    focused = native.recover(window_handle, self.mode) is True
                    if getattr(native, "last_error", None):
                        self.last_error = native.last_error
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
        if focused:
            # Keep the count inspectable until the next observed focus change.
            self._recovery_pending = False
            self._loss_episode = False
            self._next_recovery_at = None
        elif self._recovery_attempts >= self.config.focus_recovery_max_attempts:
            self._recovery_pending = False
            self._next_recovery_at = None
        else:
            self._next_recovery_at = now + self.config.focus_recovery_retry_interval_seconds
        return True

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
