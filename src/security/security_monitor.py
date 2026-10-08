"""Focus transitions and selected shortcuts, without collecting typed text.

The backend isolates Win32 HWND lookup and a WH_KEYBOARD_LL message-pump
thread. SecurityMonitor debounces normalized inputs on the application thread.
Tests inject a backend and clocks; replacing the backend with native UI focus
signals later requires no changes to the CV or risk engines.
"""

from collections.abc import Callable, Mapping
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
import os
from queue import Empty, SimpleQueue
import sys
from threading import Event, Thread
from time import perf_counter
from types import MappingProxyType
from typing import Protocol

from monitoring import EventType, ProctoringEvent, Severity
from monitoring.event_engine import SECURITY_EVENT_TYPES

SUPPRESSIBLE_EVENTS = frozenset({
    EventType.COPY_ATTEMPT, EventType.PASTE_ATTEMPT,
    EventType.PRINTSCREEN_ATTEMPT, EventType.ESCAPE_ATTEMPT,
})


@dataclass(frozen=True)
class SecurityRule:
    cooldown_seconds: float
    severity: Severity
    message: str

    def __post_init__(self) -> None:
        if not isfinite(self.cooldown_seconds) or self.cooldown_seconds < 0:
            raise ValueError("Security cooldowns must be finite and nonnegative")


def _default_rules() -> dict[EventType, SecurityRule]:
    return {
        EventType.WINDOW_FOCUS_LOST: SecurityRule(3, Severity.HIGH, "Protected window focus lost"),
        EventType.ALT_TAB_ATTEMPT: SecurityRule(3, Severity.HIGH, "Alt+Tab attempt detected"),
        EventType.COPY_ATTEMPT: SecurityRule(2, Severity.MEDIUM, "Ctrl+C attempt detected"),
        EventType.PASTE_ATTEMPT: SecurityRule(2, Severity.MEDIUM, "Ctrl+V attempt detected"),
        EventType.PRINTSCREEN_ATTEMPT: SecurityRule(3, Severity.HIGH, "PrintScreen attempt detected"),
        EventType.ESCAPE_ATTEMPT: SecurityRule(2, Severity.MEDIUM, "Escape attempt detected"),
    }


@dataclass(frozen=True)
class SecurityConfig:
    rules: Mapping[EventType, SecurityRule] = field(default_factory=_default_rules)
    blocked_events: frozenset[EventType] = frozenset()
    detect_only_when_focused: bool = False
    hook_start_timeout_seconds: float = 3.0
    message_pump_interval_seconds: float = 0.01

    def __post_init__(self) -> None:
        if set(self.rules) != SECURITY_EVENT_TYPES:
            raise ValueError("Configure exactly one rule for each security event type")
        if any(not isinstance(rule, SecurityRule) for rule in self.rules.values()):
            raise ValueError("Security rules must be SecurityRule instances")
        if not set(self.blocked_events) <= SUPPRESSIBLE_EVENTS:
            raise ValueError("Only copy/paste/PrintScreen/Escape suppression is supported")
        for value in (self.hook_start_timeout_seconds, self.message_pump_interval_seconds):
            if not isfinite(value) or value <= 0:
                raise ValueError("Security thread timings must be finite and positive")
        object.__setattr__(self, "rules", MappingProxyType(dict(self.rules)))
        object.__setattr__(self, "blocked_events", frozenset(self.blocked_events))


@dataclass(frozen=True)
class KeyInput:
    key: str
    occurred_at: float
    ctrl: bool = False
    alt: bool = False
    focused: bool | None = True
    blocked: bool = False
    injected: bool = False
    shift: bool = False
    win: bool = False


def classify_shortcut(key: str, *, ctrl: bool = False, alt: bool = False) -> EventType | None:
    if key == "tab" and alt:
        return EventType.ALT_TAB_ATTEMPT
    if key == "c" and ctrl and not alt:
        return EventType.COPY_ATTEMPT
    if key == "v" and ctrl and not alt:
        return EventType.PASTE_ATTEMPT
    if key == "printscreen":
        return EventType.PRINTSCREEN_ATTEMPT
    if key == "escape":
        return EventType.ESCAPE_ATTEMPT
    return None


class SecurityBackend(Protocol):
    keyboard_error: str | None

    def initialize(self, window_name: str) -> None: ...
    def start_keyboard(self) -> bool: ...
    def is_focused(self) -> bool | None: ...
    def drain_keys(self) -> list[KeyInput]: ...
    def stop(self) -> None: ...


class WindowsSecurityBackend:
    """Win32 adapter. No keyboard hook or native call occurs on import."""

    WH_KEYBOARD_LL = 13
    GA_ROOT = 2
    PM_REMOVE = 1
    WM_QUIT = 0x0012
    KEY_DOWN = frozenset({0x0100, 0x0104})
    KEY_UP = frozenset({0x0101, 0x0105})
    CTRL_KEYS = frozenset({0x11, 0xA2, 0xA3})
    ALT_KEYS = frozenset({0x12, 0xA4, 0xA5})
    SHIFT_KEYS = frozenset({0x10, 0xA0, 0xA1})
    WIN_KEYS = frozenset({0x5B, 0x5C})
    SHORTCUT_KEYS = {0x09: "tab", 0x43: "c", 0x56: "v", 0x2C: "printscreen", 0x1B: "escape"}

    def __init__(
        self, config: SecurityConfig, *, clock: Callable[[], float] = perf_counter,
        window_handle: int | None = None,
    ) -> None:
        self.config = config
        self._clock = clock
        self._inputs: SimpleQueue[KeyInput] = SimpleQueue()
        self._stop = Event()
        self._ready = Event()
        self._thread: Thread | None = None
        self._hook = None
        self._hook_callback = None
        self._held: set[int] = set()
        self._suppressed: set[int] = set()
        self.keyboard_error: str | None = None
        self._hwnd = None
        self._window_handle = window_handle

    def initialize(self, window_name: str) -> None:
        if sys.platform != "win32":
            raise OSError("Security monitoring currently requires Windows")
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._bind_api()
        hwnd = self._window_handle or self._user32.FindWindowW(None, window_name)
        if not hwnd:
            raise OSError("Cannot find the protected OpenCV window by its exact title")
        if not self._user32.IsWindow(hwnd):
            raise OSError("Protected window handle is no longer valid")
        pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != os.getpid():
            raise OSError("Protected window title belongs to a different process")
        self._hwnd = self._user32.GetAncestor(hwnd, self.GA_ROOT) or hwnd

    def _bind_api(self) -> None:
        # Explicit pointer-width signatures are required on 64-bit Python.
        self._callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        class KeyboardData(ctypes.Structure):
            _fields_ = [
                ("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t),
            ]

        self._keyboard_data = KeyboardData
        signatures = {
            "FindWindowW": ([wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND),
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            "GetAncestor": ([wintypes.HWND, wintypes.UINT], wintypes.HWND),
            "GetForegroundWindow": ([], wintypes.HWND),
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "GetAsyncKeyState": ([ctypes.c_int], ctypes.c_short),
            "SetWindowsHookExW": ([ctypes.c_int, self._callback_type, wintypes.HINSTANCE, wintypes.DWORD], wintypes.HANDLE),
            "UnhookWindowsHookEx": ([wintypes.HANDLE], wintypes.BOOL),
            "CallNextHookEx": ([wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM], ctypes.c_ssize_t),
            "PeekMessageW": ([ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
            "TranslateMessage": ([ctypes.POINTER(wintypes.MSG)], wintypes.BOOL),
            "DispatchMessageW": ([ctypes.POINTER(wintypes.MSG)], ctypes.c_ssize_t),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self._user32, name)
            function.argtypes, function.restype = arguments, result
        self._kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self._kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    def is_focused(self) -> bool | None:
        if not self._hwnd or not self._user32.IsWindow(self._hwnd):
            raise OSError("Protected window handle is no longer valid")
        foreground = self._user32.GetForegroundWindow()
        # Windows can return NULL during activation transitions: unknown, not loss.
        if not foreground:
            return None
        return (self._user32.GetAncestor(foreground, self.GA_ROOT) or foreground) == self._hwnd

    def start_keyboard(self) -> bool:
        if self._thread is not None:
            return self._hook is not None and self.keyboard_error is None
        self._thread = Thread(target=self._run_keyboard, name="proctor-security-keyboard", daemon=False)
        try:
            self._thread.start()
        except Exception:
            self._thread = None
            raise
        if not self._ready.wait(self.config.hook_start_timeout_seconds):
            self.keyboard_error = "Keyboard hook startup timed out"
            self.stop()
        return self._hook is not None and self.keyboard_error is None

    def _run_keyboard(self) -> None:
        try:
            # Seed modifiers outside the callback; subsequent state comes from
            # key down/up messages, not asynchronous state queried in a callback.
            for vk in (0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C):
                if self._user32.GetAsyncKeyState(vk) & 0x8000:
                    self._held.add(vk)
            self._hook_callback = self._callback_type(self._keyboard_callback)
            self._hook = self._user32.SetWindowsHookExW(
                self.WH_KEYBOARD_LL, self._hook_callback,
                self._kernel32.GetModuleHandleW(None), 0,
            )
            if not self._hook:
                self._hook = None
                raise ctypes.WinError(ctypes.get_last_error())
            self._ready.set()
            message = wintypes.MSG()
            # PeekMessage dispatches sent hook notifications. Waiting on an Event
            # between polls makes shutdown independent of PostThreadMessage and
            # avoids a busy spin or an indefinitely blocked GetMessage call.
            while not self._stop.is_set():
                while self._user32.PeekMessageW(ctypes.byref(message), None, 0, 0, self.PM_REMOVE):
                    if message.message == self.WM_QUIT or self._stop.is_set():
                        self._stop.set()
                        break
                    self._user32.TranslateMessage(ctypes.byref(message))
                    self._user32.DispatchMessageW(ctypes.byref(message))
                self._stop.wait(self.config.message_pump_interval_seconds)
        except Exception as exc:
            self.keyboard_error = f"{type(exc).__name__}: {exc}"
        finally:
            if self._hook is not None:
                try:
                    if not self._user32.UnhookWindowsHookEx(self._hook):
                        self.keyboard_error = "Keyboard hook removal reported failure"
                except Exception as exc:
                    self.keyboard_error = f"Keyboard hook removal failed: {exc}"
                finally:
                    self._hook = None
            self._ready.set()

    def _keyboard_callback(self, code: int, message: int, data_address: int) -> int:
        if code < 0:
            return self._user32.CallNextHookEx(self._hook, code, message, data_address)
        try:
            data = ctypes.cast(data_address, ctypes.POINTER(self._keyboard_data)).contents
            if self._handle_key(data.vkCode, message, data.flags):
                return 1  # Suppression was actually requested for this hook input.
        except Exception as exc:
            # Fail open: never swallow keys after an internal monitoring error.
            self.keyboard_error = f"Keyboard callback failed: {exc}"
            self._stop.set()
        return self._user32.CallNextHookEx(self._hook, code, message, data_address)

    def _handle_key(self, vk: int, message: int, flags: int = 0) -> bool:
        if message in self.KEY_UP:
            self._held.discard(vk)
            suppressed = vk in self._suppressed
            self._suppressed.discard(vk)
            return suppressed
        if message not in self.KEY_DOWN:
            return False
        repeated = vk in self._held
        self._held.add(vk)
        if repeated:
            # Stop suppressing repeats after focus leaves the protected window.
            if vk in self._suppressed and self.is_focused() is not True:
                self._suppressed.discard(vk)
            return vk in self._suppressed
        key = self.SHORTCUT_KEYS.get(vk)
        if key is None:
            return False
        ctrl = bool(self._held & self.CTRL_KEYS)
        alt = bool(self._held & self.ALT_KEYS) or bool(flags & 0x20)
        shift = bool(self._held & self.SHIFT_KEYS)
        win = bool(self._held & self.WIN_KEYS)
        event_type = classify_shortcut(key, ctrl=ctrl, alt=alt)
        if event_type is None:
            return False
        focused = self.is_focused()
        if self.config.detect_only_when_focused and focused is not True:
            return False
        blocked = event_type in self.config.blocked_events and focused is True
        # Modified Escape includes OS management/accessibility shortcuts (for
        # example Ctrl+Shift+Esc). Only bare Escape is eligible for suppression.
        if event_type == EventType.ESCAPE_ATTEMPT and (ctrl or alt or shift or win):
            blocked = False
        if blocked:
            self._suppressed.add(vk)
        self._inputs.put(KeyInput(
            key, self._clock(), ctrl, alt, focused, blocked, bool(flags & 0x10), shift, win,
        ))
        return blocked

    def drain_keys(self) -> list[KeyInput]:
        inputs = []
        while True:
            try:
                inputs.append(self._inputs.get_nowait())
            except Empty:
                return inputs

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            # The message-pump wait wakes immediately on this Event. Joining
            # ensures the hook thread's finally/unhook finishes before return.
            self._thread.join()
            self._thread = None
        self._held.clear()
        self._suppressed.clear()
        self.drain_keys()


class SecurityMonitor:
    """Normalize focus edges and shortcut impulses with independent cooldowns."""

    def __init__(
        self, config: SecurityConfig | None = None, *,
        backend_factory: Callable[[SecurityConfig], SecurityBackend] = WindowsSecurityBackend,
        clock: Callable[[], float] = perf_counter,
        wall_clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        self.config = config if config is not None else SecurityConfig()
        self._backend_factory = backend_factory
        self._clock, self._wall_clock = clock, wall_clock
        self._backend: SecurityBackend | None = None
        self._focus_available = False
        self._keyboard_available = False
        self._last_focused: bool | None = None
        self._last_emitted: dict[EventType, float] = {}
        self._warnings: set[str] = set()

    @property
    def focus_available(self) -> bool:
        return self._focus_available

    @property
    def keyboard_available(self) -> bool:
        return self._keyboard_available

    @property
    def current_focused(self) -> bool | None:
        return self._last_focused if self._focus_available else None

    def _warn(self, component: str, error: str) -> None:
        if component not in self._warnings:
            print(f"[SECURITY] Warning: {component} unavailable ({error}). Webcam monitoring continues.", file=sys.stderr)
            self._warnings.add(component)

    def start(self, window_name: str) -> bool:
        if self._backend is not None:
            return self._focus_available
        try:
            self._backend = self._backend_factory(self.config)
            self._backend.initialize(window_name)
            self._focus_available = True
            # Capture the initial state before first-frame inference/warmup so a
            # focus loss before the first poll is still a real transition.
            self._last_focused = self._backend.is_focused()
        except Exception as exc:
            self._warn("Windows focus monitoring", str(exc))
            self.stop()
            return False
        try:
            self._keyboard_available = self._backend.start_keyboard()
            if not self._keyboard_available:
                self._warn("keyboard listener", self._backend.keyboard_error or "hook initialization failed")
        except Exception as exc:
            self._warn("keyboard listener", str(exc))
            # A failed start may have partially created a listener. Stop it while
            # retaining the HWND adapter so focus monitoring can still work.
            try:
                self._backend.stop()
            except Exception as cleanup_exc:
                self._warn("listener cleanup", str(cleanup_exc))
        return self._focus_available

    def _emit(self, event_type: EventType, now: float, metadata: dict) -> ProctoringEvent | None:
        if not isfinite(now):
            return None
        rule = self.config.rules[event_type]
        last = self._last_emitted.get(event_type)
        if last is not None and now - last < rule.cooldown_seconds:
            return None
        self._last_emitted[event_type] = now
        return ProctoringEvent(event_type, self._wall_clock(), rule.severity, 0.0, rule.message, metadata=metadata)

    def poll(self) -> list[ProctoringEvent]:
        if self._backend is None:
            return []
        events = []
        if self._focus_available:
            try:
                focused = self._backend.is_focused()
                if focused is not None:
                    if self._last_focused is True and not focused:
                        event = self._emit(EventType.WINDOW_FOCUS_LOST, self._clock(), {
                            "source": "window_focus", "action": "DETECTED", "blocked": False,
                        })
                        if event is not None:
                            events.append(event)
                    self._last_focused = focused
            except Exception as exc:
                self._focus_available = False
                self._warn("Windows focus monitoring", str(exc))
        if self._keyboard_available:
            if self._backend.keyboard_error:
                self._keyboard_available = False
                self._warn("keyboard listener", self._backend.keyboard_error)
            try:
                for key in self._backend.drain_keys():
                    event_type = classify_shortcut(key.key, ctrl=key.ctrl, alt=key.alt)
                    if event_type is None or (self.config.detect_only_when_focused and key.focused is not True):
                        continue
                    event = self._emit(event_type, key.occurred_at, {
                        "source": "windows_keyboard_hook", "key": key.key,
                        "ctrl": key.ctrl, "alt": key.alt, "injected": key.injected,
                        "shift": key.shift, "win": key.win,
                        "protected_window_focused": key.focused,
                        "action": "BLOCKED" if key.blocked else "DETECTED", "blocked": key.blocked,
                    })
                    if event is not None:
                        events.append(event)
            except Exception as exc:
                self._keyboard_available = False
                self._warn("keyboard listener", str(exc))
        return events

    def record_application_paste(self, *, source: str, blocked: bool,
                                 now: float | None = None) -> ProctoringEvent | None:
        """Normalize a paste in our editor, including non-keyboard paste paths.

        Called on the monitoring thread after poll(), sharing the native hook's
        cooldown. This also works when global hooks are unavailable. The editor
        reports actual suppression; this method does not prevent OS actions.
        """
        return self._emit(EventType.PASTE_ATTEMPT, self._clock() if now is None else now, {
            "source": "exam_answer_editor", "paste_source": source,
            "protected_window_focused": True,
            "action": "BLOCKED" if blocked else "DETECTED", "blocked": bool(blocked),
        })

    def record_application_event(self, event_type: EventType, *, source: str,
                                 blocked: bool = False, now: float | None = None) -> ProctoringEvent | None:
        """Normalize observed application focus/Escape attempts with hook cooldowns.

        Qt application deactivation covers brief focus losses before the next CV
        poll. No arbitrary typed text, clipboard contents or OS prevention is
        inferred. Application events remain available if native hooks fail.
        """
        event_type = EventType(event_type)
        if event_type not in {EventType.WINDOW_FOCUS_LOST, EventType.ESCAPE_ATTEMPT}:
            raise ValueError("Unsupported application security event")
        if event_type == EventType.WINDOW_FOCUS_LOST:
            blocked = False
        return self._emit(event_type, self._clock() if now is None else now, {
            "source": source, "action_scope": "application",
            "action": "BLOCKED" if blocked else "DETECTED", "blocked": bool(blocked),
        })

    def stop(self) -> None:
        backend, self._backend = self._backend, None
        if backend is not None:
            try:
                backend.stop()
                if backend.keyboard_error:
                    self._warn("keyboard listener", backend.keyboard_error)
            except Exception as exc:
                self._warn("listener cleanup", str(exc))
        self._focus_available = self._keyboard_available = False
        self._last_focused = None
        self._last_emitted.clear()
