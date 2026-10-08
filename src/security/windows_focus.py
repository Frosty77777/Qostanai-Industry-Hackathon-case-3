"""Narrow Win32 foreground requests for the protected window of this process.

The helper does not change foreground-lock policy, attach input queues, synthesize
keys, make a window permanently topmost, or interact with other processes. Windows
can refuse these requests. Qt owns fullscreen geometry; SW_SHOW preserves it.
"""

import ctypes
from ctypes import wintypes
import os
import sys


SW_SHOWMAXIMIZED = 3
SW_SHOW = 5


class WindowsFocusRecovery:
    """An injectable, failure-tolerant wrapper around ordinary User32 calls."""

    def __init__(self, *, api=None, platform=None, process_id=None):
        self.last_error = None
        self._process_id = os.getpid() if process_id is None else process_id
        self._api = None
        if (sys.platform if platform is None else platform) != "win32":
            return
        try:
            self._api = api if api is not None else self._load_api()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _load_api():
        api = ctypes.WinDLL("user32", use_last_error=True)
        for name in ("IsWindow", "BringWindowToTop", "SetForegroundWindow"):
            function = getattr(api, name)
            function.argtypes = [wintypes.HWND]
            function.restype = wintypes.BOOL
        api.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        api.ShowWindow.restype = wintypes.BOOL
        api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        api.GetWindowThreadProcessId.restype = wintypes.DWORD
        api.GetForegroundWindow.argtypes = []
        api.GetForegroundWindow.restype = wintypes.HWND
        return api

    @property
    def available(self):
        return self._api is not None

    def _valid_window(self, window_handle):
        if self._api is None:
            return False
        if isinstance(window_handle, bool) or not isinstance(window_handle, int) or window_handle <= 0:
            self.last_error = "Protected window handle is invalid"
            return False
        if not self._api.IsWindow(window_handle):
            self.last_error = "Protected window is no longer available"
            return False
        owner = wintypes.DWORD()
        thread = self._api.GetWindowThreadProcessId(window_handle, ctypes.byref(owner))
        if not thread or owner.value != self._process_id:
            self.last_error = "Protected window does not belong to this process"
            return False
        return True

    def is_foreground(self, window_handle):
        """Return None when unavailable, otherwise inspect the actual foreground."""
        try:
            if not self._valid_window(window_handle):
                return None
            return self._api.GetForegroundWindow() == window_handle
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None

    def recover(self, window_handle, mode):
        """Make one request; an observed foreground match is the success signal."""
        try:
            if mode not in {"MAXIMIZED", "FULLSCREEN"} or not self._valid_window(window_handle):
                return False
            self.last_error = None
            # ShowWindow's return value means previous visibility, not success.
            self._api.ShowWindow(window_handle, SW_SHOWMAXIMIZED if mode == "MAXIMIZED" else SW_SHOW)
            self._api.BringWindowToTop(window_handle)
            self._api.SetForegroundWindow(window_handle)
            return self._api.GetForegroundWindow() == window_handle
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
