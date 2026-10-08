"""Bounded recovery and isolated Win32 calls; no OS windows or keys are touched."""

from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from security.secure_window import SecureWindowConfig, SecureWindowGuard
from security.windows_focus import SW_SHOW, SW_SHOWMAXIMIZED, WindowsFocusRecovery


class FakeWindow:
    def __init__(self):
        self.calls = []
        self.focused = False
        self.on_activate = None

    def show(self): self.calls.append("show")
    def showMaximized(self): self.calls.append("maximize")
    def showFullScreen(self): self.calls.append("fullscreen")
    def raise_(self): self.calls.append("raise")
    def activateWindow(self):
        self.calls.append("activate")
        if self.on_activate:
            self.on_activate()
    def isActiveWindow(self): return self.focused


class FakeNative:
    last_error = None
    def __init__(self):
        self.foreground = False
        self.calls = []
        self.succeed_on = None
    def is_foreground(self, handle): return self.foreground
    def recover(self, handle, mode):
        self.calls.append((handle, mode))
        self.foreground = len(self.calls) == self.succeed_on
        return self.foreground


class RecoveryBurstTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.window = FakeWindow()
        self.native = FakeNative()
        self.guard = SecureWindowGuard(clock=lambda: self.now, native_helper=self.native)
        self.guard.activate("MAXIMIZED")

    def advance(self):
        self.now += .201
        return self.guard.retry_recovery(self.window, window_handle=42)

    def test_focus_loss_starts_visible_maximized_raised_activated_recovery(self):
        self.assertTrue(self.guard.begin_recovery(self.window, window_handle=42))
        self.assertEqual(self.window.calls, ["show", "maximize", "raise", "activate"])
        self.assertEqual(self.native.calls, [(42, "MAXIMIZED")])
        self.assertTrue(self.guard.recovery_pending)

    def test_fullscreen_is_reasserted_even_when_not_minimized(self):
        self.guard.activate("FULLSCREEN")
        self.guard.begin_recovery(self.window, window_handle=42)
        self.assertEqual(self.window.calls, ["show", "fullscreen", "raise", "activate"])
        self.assertEqual(self.native.calls, [(42, "FULLSCREEN")])

    def test_four_bounded_attempts_then_no_infinite_focus_war(self):
        self.guard.begin_recovery(self.window, window_handle=42)
        for _ in range(3): self.assertTrue(self.advance())
        self.assertFalse(self.guard.recovery_pending)
        self.assertEqual(self.guard.recovery_attempts, 4)
        self.assertEqual(len(self.native.calls), 4)
        for _ in range(10):
            self.now += 1
            self.assertFalse(self.guard.begin_recovery(self.window, window_handle=42))
            self.assertFalse(self.guard.retry_recovery(self.window, window_handle=42))
        self.assertEqual(len(self.native.calls), 4)

    def test_early_timer_does_not_consume_attempt_budget(self):
        self.guard.begin_recovery(self.window, window_handle=42)
        self.now += .1
        self.assertFalse(self.guard.retry_recovery(self.window, window_handle=42))
        self.assertEqual(self.guard.recovery_attempts, 1)
        self.assertTrue(self.guard.recovery_pending)

    def test_duplicate_missing_focus_packets_do_not_restart_pending_burst(self):
        self.guard.begin_recovery(self.window, window_handle=42)
        self.assertFalse(self.guard.begin_recovery(self.window, window_handle=42))
        self.assertEqual(self.guard.recovery_attempts, 1)
        self.assertTrue(self.advance())
        self.assertEqual(self.guard.recovery_attempts, 2)

    def test_stale_loss_with_native_window_already_foreground_does_not_restore_again(self):
        self.native.foreground = True
        self.assertFalse(self.guard.begin_recovery(self.window, window_handle=42))
        self.assertEqual(self.window.calls, [])
        self.assertEqual(self.native.calls, [])
        self.assertFalse(self.guard.recovery_pending)

    def test_native_foreground_observation_resets_exhausted_episode_without_qt_signal(self):
        self.guard.begin_recovery(self.window, window_handle=42)
        for _ in range(3): self.advance()
        self.native.foreground = True
        self.assertFalse(self.guard.begin_recovery(self.window, window_handle=42))
        self.assertEqual(len(self.native.calls), 4)
        self.native.foreground = False
        self.assertTrue(self.guard.begin_recovery(self.window, window_handle=42))
        self.assertEqual(self.guard.recovery_attempts, 1)
        self.assertEqual(len(self.native.calls), 5)

    def test_successful_native_recovery_stops_after_second_attempt(self):
        self.native.succeed_on = 2
        self.guard.begin_recovery(self.window, window_handle=42)
        self.assertTrue(self.advance())
        self.assertFalse(self.guard.recovery_pending)
        self.assertEqual(self.guard.recovery_attempts, 2)
        self.assertFalse(self.advance())
        self.assertEqual(len(self.native.calls), 2)

    def test_already_regained_focus_cancels_retry_without_more_requests(self):
        self.guard.begin_recovery(self.window, window_handle=42)
        self.native.foreground = True
        self.assertFalse(self.advance())
        self.assertFalse(self.guard.recovery_pending)
        self.assertEqual(len(self.native.calls), 1)

    def test_qt_success_avoids_native_request_when_native_unavailable(self):
        self.native.is_foreground = lambda handle: None
        self.window.on_activate = lambda: setattr(self.window, "focused", True)
        self.guard.begin_recovery(self.window, window_handle=42)
        self.assertEqual(self.native.calls, [])
        self.assertFalse(self.guard.recovery_pending)

    def test_observed_focus_regain_allows_a_fresh_loss_after_exhaustion(self):
        self.guard.begin_recovery(self.window, window_handle=42)
        for _ in range(3): self.advance()
        self.guard.observe_focus(True)
        self.assertTrue(self.guard.begin_recovery(self.window, window_handle=42))
        self.assertEqual(self.guard.recovery_attempts, 1)
        self.assertEqual(len(self.native.calls), 5)

    def test_teacher_authorized_release_disables_pending_and_future_recovery(self):
        self.guard.begin_recovery(self.window, window_handle=42)
        self.guard.release()
        self.assertFalse(self.guard.recovery_pending)
        self.assertFalse(self.advance())
        self.assertFalse(self.guard.begin_recovery(self.window, window_handle=42))
        self.assertEqual(len(self.native.calls), 1)

    def test_release_during_qt_activation_cancels_native_attempt(self):
        self.window.on_activate = self.guard.release
        self.assertTrue(self.guard.begin_recovery(self.window, window_handle=42))
        self.assertFalse(self.guard.recovery_pending)
        self.assertEqual(self.native.calls, [])

    def test_disabled_recovery_does_nothing(self):
        self.guard.config = replace(self.guard.config, focus_recovery_enabled=False)
        self.assertFalse(self.guard.begin_recovery(self.window, window_handle=42))
        self.assertEqual(self.window.calls, [])

    def test_native_switch_disabled_still_requests_qt(self):
        self.guard.config = replace(self.guard.config, native_focus_recovery_enabled=False)
        self.guard.begin_recovery(self.window, window_handle=42)
        self.assertEqual(self.window.calls, ["show", "maximize", "raise", "activate"])
        self.assertEqual(self.native.calls, [])

    def test_qt_failure_does_not_prevent_other_actions_or_native_attempt(self):
        self.window.raise_ = Mock(side_effect=RuntimeError("raise denied"))
        self.guard.begin_recovery(self.window, window_handle=42)
        self.assertIn("activate", self.window.calls)
        self.assertEqual(len(self.native.calls), 1)
        self.assertIn("raise denied", self.guard.last_error)

    def test_native_exception_keeps_burst_bounded_without_crash(self):
        self.native.recover = Mock(side_effect=RuntimeError("native unavailable"))
        self.guard.begin_recovery(self.window, window_handle=42)
        for _ in range(3): self.advance()
        self.assertFalse(self.guard.recovery_pending)
        self.assertEqual(self.native.recover.call_count, 4)
        self.assertIn("native unavailable", self.guard.last_error)

    def test_invalid_time_does_not_start_recovery(self):
        self.now = float("nan")
        self.assertFalse(self.guard.begin_recovery(self.window, window_handle=42))
        self.assertFalse(self.guard.recovery_pending)
        self.assertEqual(self.window.calls, [])

    def test_invalid_retry_time_stops_the_pending_loop(self):
        self.guard.begin_recovery(self.window, window_handle=42)
        self.now = float("nan")
        self.assertFalse(self.guard.retry_recovery(self.window, window_handle=42))
        self.assertFalse(self.guard.recovery_pending)
        self.assertEqual(len(self.native.calls), 1)

    def test_retry_configuration_is_bounded_and_validated(self):
        for values in ({"focus_recovery_retry_interval_seconds": 0},
                       {"focus_recovery_retry_interval_seconds": float("inf")},
                       {"focus_recovery_retry_interval_seconds": "soon"},
                       {"focus_recovery_max_attempts": 0},
                       {"focus_recovery_max_attempts": 9},
                       {"focus_recovery_max_attempts": True},
                       {"native_focus_recovery_enabled": "yes"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                SecureWindowConfig(**values)


class FakeUser32:
    def __init__(self):
        self.owner = 123
        self.valid = True
        self.foreground = 99
        self.calls = []
        self.allow_foreground = True
    def IsWindow(self, handle): return self.valid
    def GetWindowThreadProcessId(self, handle, owner):
        owner._obj.value = self.owner
        return 456
    def GetForegroundWindow(self): return self.foreground
    def ShowWindow(self, handle, command):
        self.calls.append(("show", handle, command))
        return False  # previously hidden is not a failure
    def BringWindowToTop(self, handle):
        self.calls.append(("raise", handle))
        return True
    def SetForegroundWindow(self, handle):
        self.calls.append(("activate", handle))
        if self.allow_foreground: self.foreground = handle
        return self.allow_foreground


class WindowsHelperTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeUser32()
        self.helper = WindowsFocusRecovery(api=self.api, platform="win32", process_id=123)

    def test_own_process_maximized_window_uses_ordinary_foreground_calls(self):
        self.assertTrue(self.helper.recover(42, "MAXIMIZED"))
        self.assertEqual(self.api.calls, [("show", 42, SW_SHOWMAXIMIZED), ("raise", 42), ("activate", 42)])

    def test_fullscreen_preserves_qt_geometry(self):
        self.assertTrue(self.helper.recover(42, "FULLSCREEN"))
        self.assertEqual(self.api.calls[0], ("show", 42, SW_SHOW))

    def test_foreground_success_requires_observation_not_api_claim(self):
        self.api.SetForegroundWindow = lambda handle: True
        self.assertFalse(self.helper.recover(42, "MAXIMIZED"))
        self.assertFalse(self.helper.is_foreground(42))

    def test_foreground_refusal_returns_false(self):
        self.api.allow_foreground = False
        self.assertFalse(self.helper.recover(42, "MAXIMIZED"))
        self.assertEqual(len(self.api.calls), 3)

    def test_other_process_window_is_never_manipulated(self):
        self.api.owner = 999
        self.assertFalse(self.helper.recover(42, "MAXIMIZED"))
        self.assertIsNone(self.helper.is_foreground(42))
        self.assertEqual(self.api.calls, [])
        self.assertIn("does not belong", self.helper.last_error)

    def test_invalid_or_destroyed_handle_is_never_manipulated(self):
        for handle in (None, True, 0, -1, "42"):
            with self.subTest(handle=handle):
                self.assertFalse(self.helper.recover(handle, "MAXIMIZED"))
        self.api.valid = False
        self.assertFalse(self.helper.recover(42, "MAXIMIZED"))
        self.assertEqual(self.api.calls, [])

    def test_unsupported_mode_is_not_manipulated(self):
        self.assertFalse(self.helper.recover(42, "WINDOWED"))
        self.assertEqual(self.api.calls, [])

    def test_native_call_failure_is_graceful(self):
        self.api.BringWindowToTop = Mock(side_effect=OSError("API unavailable"))
        self.assertFalse(self.helper.recover(42, "MAXIMIZED"))
        self.assertIn("API unavailable", self.helper.last_error)

    def test_native_initialization_failure_is_graceful(self):
        with patch.object(WindowsFocusRecovery, "_load_api", side_effect=OSError("User32 unavailable")):
            helper = WindowsFocusRecovery(platform="win32", process_id=123)
        self.assertFalse(helper.available)
        self.assertFalse(helper.recover(42, "MAXIMIZED"))
        self.assertIsNone(helper.is_foreground(42))
        self.assertIn("User32 unavailable", helper.last_error)

    def test_non_windows_platform_performs_no_native_calls(self):
        helper = WindowsFocusRecovery(api=self.api, platform="linux", process_id=123)
        self.assertFalse(helper.available)
        self.assertFalse(helper.recover(42, "MAXIMIZED"))
        self.assertIsNone(helper.is_foreground(42))
        self.assertEqual(self.api.calls, [])


if __name__ == "__main__":
    unittest.main()
