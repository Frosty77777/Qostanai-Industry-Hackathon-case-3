"""Fake Win32 inputs only: tests never install OS hooks or press real keys."""

import contextlib
import ctypes
from dataclasses import replace
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, Mock, patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main
from evidence import EvidenceConfig, EvidenceManager
from monitoring import EventEngine, EventType, ProctoringEvent, RiskConfig, RiskEngine, Severity
from monitoring.event_engine import SECURITY_EVENT_TYPES
from security import KeyInput, SecurityConfig, SecurityMonitor, SecurityRule, WindowsSecurityBackend
from vision.face_tracker import FaceResult, FaceStatus, HeadDirection
from vision.yolo_detector import Detection


class FakeBackend:
    def __init__(self):
        self.focused = True
        self.keys = []
        self.keyboard_error = None
        self.init_error = None
        self.keyboard_starts = True
        self.keyboard_start_error = None
        self.stops = 0
        self.starts = 0

    def initialize(self, window_name):
        self.starts += 1
        if self.init_error:
            raise self.init_error

    def start_keyboard(self):
        if self.keyboard_start_error:
            raise self.keyboard_start_error
        return self.keyboard_starts

    def is_focused(self):
        if isinstance(self.focused, Exception):
            raise self.focused
        return self.focused

    def drain_keys(self):
        keys, self.keys = self.keys, []
        return keys

    def stop(self):
        self.stops += 1


class SecurityMonitorChecks(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.timestamp = datetime(2026, 10, 7, 18, 44, 12, tzinfo=timezone.utc)
        self.backend = FakeBackend()
        self.monitor = SecurityMonitor(
            backend_factory=lambda config: self.backend, clock=lambda: self.now,
            wall_clock=lambda: self.timestamp,
        )
        self.assertTrue(self.monitor.start("Protected preview"))
        self.addCleanup(self.monitor.stop)
        self.monitor.poll()  # Arm focus-loss detection after initial focus.
        self.errors = self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def key(self, key, *, ctrl=False, alt=False, focused=True, blocked=False):
        self.backend.keys.append(KeyInput(key, self.now, ctrl, alt, focused, blocked))
        return self.monitor.poll()

    def assert_shortcut(self, key, event_type, **modifiers):
        events = self.key(key, **modifiers)
        self.assertEqual([event.type for event in events], [event_type])
        event = events[0]
        self.assertEqual(event.duration, 0)
        self.assertEqual(event.timestamp, self.timestamp)
        self.assertEqual(event.metadata["action"], "DETECTED")
        self.assertFalse(event.metadata["blocked"])
        self.assertIsNone(event.evidence_path)

    def test_alt_tab_detection(self):
        self.assert_shortcut("tab", EventType.ALT_TAB_ATTEMPT, alt=True)

    def test_copy_detection(self):
        self.assert_shortcut("c", EventType.COPY_ATTEMPT, ctrl=True)

    def test_paste_detection(self):
        self.assert_shortcut("v", EventType.PASTE_ATTEMPT, ctrl=True)

    def test_printscreen_detection(self):
        self.assert_shortcut("printscreen", EventType.PRINTSCREEN_ATTEMPT)

    def test_escape_detection(self):
        self.assert_shortcut("escape", EventType.ESCAPE_ATTEMPT)

    def test_focus_loss_detection(self):
        self.backend.focused = False
        events = self.monitor.poll()
        self.assertEqual([event.type for event in events], [EventType.WINDOW_FOCUS_LOST])
        self.assertEqual(events[0].severity, Severity.HIGH)

    def test_cooldown_prevents_spam_and_repeats_at_boundary(self):
        for key, modifiers, cooldown in (
            ("tab", {"alt": True}, 3), ("c", {"ctrl": True}, 2),
            ("v", {"ctrl": True}, 2), ("printscreen", {}, 3), ("escape", {}, 2),
        ):
            with self.subTest(key=key):
                self.assertEqual(len(self.key(key, **modifiers)), 1)
                self.now += cooldown - 0.01
                self.assertEqual(self.key(key, **modifiers), [])
                self.now += 0.02
                self.assertEqual(len(self.key(key, **modifiers)), 1)
                self.now += 10

    def test_focus_loss_stays_one_event_until_refocused(self):
        self.backend.focused = False
        self.assertEqual(len(self.monitor.poll()), 1)
        self.now = 10
        self.assertEqual(self.monitor.poll(), [])
        self.backend.focused = True
        self.monitor.poll()
        self.backend.focused = False
        self.assertEqual(len(self.monitor.poll()), 1)

    def test_focus_cooldown_survives_refocus(self):
        self.backend.focused = False
        self.monitor.poll()
        self.backend.focused = True
        self.monitor.poll()
        self.now = 2
        self.backend.focused = False
        self.assertEqual(self.monitor.poll(), [])
        self.backend.focused = True
        self.monitor.poll()
        self.now = 3
        self.backend.focused = False
        self.assertEqual(len(self.monitor.poll()), 1)

    def test_initial_unfocused_window_is_not_a_loss(self):
        self.monitor.stop()
        self.backend.focused = False
        self.monitor.start("Protected preview")
        self.assertEqual(self.monitor.poll(), [])
        self.now = 10
        self.assertEqual(self.monitor.poll(), [])
        self.backend.focused = True
        self.monitor.poll()
        self.backend.focused = False
        self.assertEqual(len(self.monitor.poll()), 1)

    def test_unknown_focus_does_not_create_false_violation(self):
        self.backend.focused = None
        self.assertEqual(self.monitor.poll(), [])
        self.backend.focused = False
        self.assertEqual(len(self.monitor.poll()), 1)

    def test_unrelated_keys_ignored_and_background_attempt_retains_focus_state(self):
        self.assertEqual(self.key("c"), [])
        self.assertEqual(self.key("tab"), [])
        self.assertEqual(self.key("q"), [])
        self.assertEqual(self.key("c", ctrl=True, alt=True), [])
        event = self.key("c", ctrl=True, focused=False)[0]
        self.assertEqual(event.type, EventType.COPY_ATTEMPT)
        self.assertFalse(event.metadata["protected_window_focused"])
        self.assertFalse(event.metadata["blocked"])

    def test_optional_focused_window_only_detection(self):
        self.monitor.stop()
        self.monitor = SecurityMonitor(
            SecurityConfig(detect_only_when_focused=True),
            backend_factory=lambda config: self.backend, clock=lambda: self.now,
        )
        self.addCleanup(self.monitor.stop)
        self.monitor.start("Protected preview")
        self.assertEqual(self.key("c", ctrl=True, focused=False), [])
        self.assertEqual(len(self.key("c", ctrl=True)), 1)

    def test_configurable_cooldown_and_severity(self):
        self.monitor.stop()
        rules = dict(SecurityConfig().rules)
        rules[EventType.COPY_ATTEMPT] = SecurityRule(5, Severity.HIGH, "Custom copy warning")
        self.monitor = SecurityMonitor(SecurityConfig(rules=rules), backend_factory=lambda config: self.backend, clock=lambda: self.now)
        self.monitor.start("Protected preview")
        rules.clear()
        event = self.key("c", ctrl=True)[0]
        self.assertEqual((event.severity, event.message), (Severity.HIGH, "Custom copy warning"))
        self.now = 2
        self.assertEqual(self.key("c", ctrl=True), [])
        self.now = 5
        self.assertEqual(len(self.key("c", ctrl=True)), 1)
        self.monitor.stop()

    def test_default_severities_and_blocking_disabled(self):
        config = SecurityConfig()
        self.assertEqual(config.blocked_events, frozenset())
        for event_type, rule in config.rules.items():
            high = event_type in (EventType.WINDOW_FOCUS_LOST, EventType.ALT_TAB_ATTEMPT, EventType.PRINTSCREEN_ATTEMPT)
            self.assertEqual(rule.severity, Severity.HIGH if high else Severity.MEDIUM)
            self.assertEqual(rule.cooldown_seconds, 3 if high else 2)

    def test_hook_failure_retains_focus_detection(self):
        self.monitor.stop()
        self.backend.keyboard_starts = False
        self.backend.keyboard_error = "Hook denied"
        self.assertTrue(self.monitor.start("Protected preview"))
        self.monitor.poll()
        self.backend.focused = False
        self.assertEqual(len(self.monitor.poll()), 1)
        self.assertIn("keyboard listener unavailable", self.errors.getvalue())

    def test_partial_listener_start_failure_is_cleaned_up(self):
        self.monitor.stop()
        self.backend.keyboard_start_error = OSError("Partial hook start failed")
        previous_stops = self.backend.stops
        self.assertTrue(self.monitor.start("Protected preview"))
        self.assertEqual(self.backend.stops, previous_stops + 1)
        self.monitor.poll()
        self.backend.focused = False
        self.assertEqual(len(self.monitor.poll()), 1)

    def test_failed_initialization_returns_no_events(self):
        self.monitor.stop()
        self.backend.init_error = OSError("Win32 unavailable")
        self.assertFalse(self.monitor.start("Protected preview"))
        self.assertEqual(self.monitor.poll(), [])
        self.assertIn("Win32 unavailable", self.errors.getvalue())

    def test_runtime_focus_failure_warns_once_without_violation(self):
        self.backend.focused = OSError("Bad HWND")
        self.assertEqual(self.monitor.poll(), [])
        self.assertEqual(self.monitor.poll(), [])
        self.assertEqual(self.errors.getvalue().count("Bad HWND"), 1)

    def test_runtime_hook_failure_warns_once(self):
        self.backend.keyboard_error = "Hook thread stopped"
        self.assertEqual(self.monitor.poll(), [])
        self.assertEqual(self.monitor.poll(), [])
        self.assertEqual(self.errors.getvalue().count("Hook thread stopped"), 1)

    def test_listener_stop_is_idempotent_and_start_is_idempotent(self):
        previous_starts = self.backend.starts
        self.monitor.start("Protected preview")
        self.assertEqual(self.backend.starts, previous_starts)
        self.monitor.stop()
        previous_stops = self.backend.stops
        self.monitor.stop()
        self.assertEqual(self.backend.stops, previous_stops)
        self.assertEqual(self.monitor.poll(), [])

    def test_risk_integration_defaults_repeats_and_cap(self):
        risk = RiskEngine()
        for event_type in SECURITY_EVENT_TYPES:
            isolated = RiskEngine()
            event = ProctoringEvent(event_type, self.timestamp, Severity.HIGH, 0, "Security attempt")
            high = event_type in (EventType.ALT_TAB_ATTEMPT, EventType.WINDOW_FOCUS_LOST, EventType.PRINTSCREEN_ATTEMPT)
            self.assertEqual(isolated.process(event).risk_delta, 15 if high else 5)
            self.assertEqual(isolated.process(event).risk_delta, 8 if high else 3)
            for _ in range(20):
                risk.process(event)
        self.assertEqual(risk.current_score, 100)

    def test_security_events_recorded_in_event_history(self):
        engine = EventEngine()
        events = self.key("tab", alt=True)
        recorded = engine.record_security_events(events)
        self.assertEqual(recorded, events)
        self.assertEqual(engine.history, events)
        with self.assertRaises(ValueError):
            engine.record_security_events([events[0], replace(events[0], type=EventType.PHONE_DETECTED)])
        self.assertEqual(engine.history, events)

    def test_security_events_never_create_evidence_even_with_head_evidence(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary) / "evidence"
            encoder = Mock()
            manager = EvidenceManager(EvidenceConfig(directory=directory, head_direction_enabled=True), encoder=encoder)
            for event_type in SECURITY_EVENT_TYPES:
                event = ProctoringEvent(event_type, self.timestamp, Severity.HIGH, 0, "Security attempt")
                self.assertIs(manager.capture(event, None), event)
                self.assertIsNone(event.evidence_path)
            encoder.assert_not_called()
            self.assertFalse(directory.exists())

    def test_invalid_security_configuration_rejected(self):
        with self.assertRaises(ValueError):
            SecurityConfig(rules={})
        for blocked in (EventType.ALT_TAB_ATTEMPT, EventType.WINDOW_FOCUS_LOST, EventType.PHONE_DETECTED):
            with self.subTest(blocked=blocked), self.assertRaises(ValueError):
                SecurityConfig(blocked_events=frozenset({blocked}))
        with self.assertRaises(ValueError):
            SecurityRule(-1, Severity.HIGH, "Invalid")
        with self.assertRaises(ValueError):
            SecurityConfig(message_pump_interval_seconds=0)


@unittest.skipUnless(sys.platform == "win32", "Fake Win32 adapter uses Windows ctypes types")
class WindowsBackendChecks(unittest.TestCase):
    def setUp(self):
        self.backend = WindowsSecurityBackend(SecurityConfig(), clock=lambda: 1.0)
        self.user32 = MagicMock()
        self.kernel32 = MagicMock()
        self.backend._user32, self.backend._kernel32 = self.user32, self.kernel32
        self.backend._bind_api()  # Binds fake callable signatures, no OS calls.
        self.backend._hwnd = 123
        self.user32.IsWindow.return_value = True
        self.user32.GetForegroundWindow.return_value = 123
        self.user32.GetAncestor.side_effect = lambda hwnd, root: hwnd
        self.user32.GetAsyncKeyState.return_value = 0
        self.user32.SetWindowsHookExW.return_value = 456
        self.user32.UnhookWindowsHookEx.return_value = True
        self.user32.PeekMessageW.return_value = False
        self.addCleanup(self.backend.stop)

    def test_native_focus_compares_root_window_handles(self):
        self.assertTrue(self.backend.is_focused())
        self.user32.GetForegroundWindow.return_value = 999
        self.assertFalse(self.backend.is_focused())
        self.user32.GetForegroundWindow.return_value = None
        self.assertIsNone(self.backend.is_focused())

    def test_invalid_window_handle_is_technical_error(self):
        self.user32.IsWindow.return_value = False
        with self.assertRaises(OSError):
            self.backend.is_focused()

    def test_hwnd_lookup_requires_current_process(self):
        self.user32.FindWindowW.return_value = 123
        self.user32.GetWindowThreadProcessId.side_effect = lambda hwnd, pid: setattr(pid._obj, "value", os.getpid())
        with patch("security.security_monitor.ctypes.WinDLL", side_effect=[self.user32, self.kernel32]):
            self.backend.initialize("Protected preview")
        self.assertEqual(self.backend._hwnd, 123)
        self.user32.GetWindowThreadProcessId.side_effect = lambda hwnd, pid: setattr(pid._obj, "value", os.getpid() + 1)
        with patch("security.security_monitor.ctypes.WinDLL", side_effect=[self.user32, self.kernel32]), self.assertRaises(OSError):
            self.backend.initialize("Protected preview")

    def test_missing_window_lookup_fails_without_hook(self):
        self.user32.FindWindowW.return_value = None
        with patch("security.security_monitor.ctypes.WinDLL", side_effect=[self.user32, self.kernel32]), self.assertRaises(OSError):
            self.backend.initialize("Protected preview")
        self.user32.SetWindowsHookExW.assert_not_called()

    def test_native_modifier_and_shortcut_mapping(self):
        self.backend._handle_key(0xA4, 0x0100)
        self.backend._handle_key(0x09, 0x0100)
        self.backend._handle_key(0x09, 0x0101)
        self.backend._handle_key(0xA4, 0x0101)
        self.backend._handle_key(0xA2, 0x0100)
        self.backend._handle_key(0x43, 0x0100)
        self.backend._handle_key(0x43, 0x0101)
        self.backend._handle_key(0x56, 0x0100)
        self.backend._handle_key(0x56, 0x0101)
        self.backend._handle_key(0xA2, 0x0101)
        self.backend._handle_key(0x2C, 0x0100)
        self.backend._handle_key(0x1B, 0x0100)
        inputs = self.backend.drain_keys()
        self.assertEqual([key.key for key in inputs], ["tab", "c", "v", "printscreen", "escape"])
        self.assertTrue(inputs[0].alt)
        self.assertTrue(inputs[1].ctrl)
        self.assertFalse(any(key.blocked for key in inputs))

    def test_key_auto_repeat_requires_release_before_another_attempt(self):
        for _ in range(10):
            self.backend._handle_key(0x1B, 0x0100)
        self.assertEqual(len(self.backend.drain_keys()), 1)
        self.backend._handle_key(0x1B, 0x0101)
        self.backend._handle_key(0x1B, 0x0100)
        self.assertEqual(len(self.backend.drain_keys()), 1)

    def test_background_shortcut_detected_but_never_suppressed(self):
        self.backend.config = SecurityConfig(blocked_events=frozenset({EventType.COPY_ATTEMPT}))
        self.user32.GetForegroundWindow.return_value = 999
        self.backend._handle_key(0xA2, 0x0100)
        self.assertFalse(self.backend._handle_key(0x43, 0x0100))
        inputs = self.backend.drain_keys()
        self.assertEqual(len(inputs), 1)
        self.assertFalse(inputs[0].focused)
        self.assertFalse(inputs[0].blocked)

    def test_optional_suppression_and_paired_release(self):
        self.backend.config = SecurityConfig(blocked_events=frozenset({EventType.COPY_ATTEMPT}))
        self.backend._handle_key(0xA2, 0x0100)
        self.assertTrue(self.backend._handle_key(0x43, 0x0100))
        self.assertTrue(self.backend._handle_key(0x43, 0x0100))
        self.assertTrue(self.backend._handle_key(0x43, 0x0101))
        self.assertFalse(self.backend._handle_key(0xA2, 0x0101))
        inputs = self.backend.drain_keys()
        self.assertEqual(len(inputs), 1)
        self.assertTrue(inputs[0].blocked)

    def test_callback_returns_nonzero_only_for_configured_suppression(self):
        data = self.backend._keyboard_data(vkCode=0x1B)
        address = ctypes.addressof(data)
        self.user32.CallNextHookEx.return_value = 0
        self.assertEqual(self.backend._keyboard_callback(0, 0x0100, address), 0)
        self.backend._handle_key(0x1B, 0x0101)
        self.backend.config = SecurityConfig(blocked_events=frozenset({EventType.ESCAPE_ATTEMPT}))
        self.assertEqual(self.backend._keyboard_callback(0, 0x0100, address), 1)
        self.assertEqual(self.backend._keyboard_callback(-1, 0x0100, address), 0)

    def test_callback_failure_fails_open(self):
        data = self.backend._keyboard_data(vkCode=0x1B)
        self.user32.CallNextHookEx.return_value = 0
        with patch.object(self.backend, "_handle_key", side_effect=RuntimeError("Callback failed")):
            self.assertEqual(self.backend._keyboard_callback(0, 0x0100, ctypes.addressof(data)), 0)
        self.assertTrue(self.backend._stop.is_set())
        self.assertIn("Callback failed", self.backend.keyboard_error)

    def test_escape_suppression_does_not_block_modified_os_shortcuts(self):
        self.backend.config = SecurityConfig(blocked_events=frozenset({EventType.ESCAPE_ATTEMPT}))
        for modifiers in ((0xA2, 0xA0), (0x5B,), (0xA4,)):
            with self.subTest(modifiers=modifiers):
                for vk in modifiers:
                    self.backend._handle_key(vk, 0x0100)
                self.assertFalse(self.backend._handle_key(0x1B, 0x0100))
                key = self.backend.drain_keys()[0]
                self.assertFalse(key.blocked)
                self.backend._handle_key(0x1B, 0x0101)
                for vk in modifiers:
                    self.backend._handle_key(vk, 0x0101)

    def test_suppression_does_not_continue_for_repeats_in_other_windows(self):
        self.backend.config = SecurityConfig(blocked_events=frozenset({EventType.ESCAPE_ATTEMPT}))
        self.assertTrue(self.backend._handle_key(0x1B, 0x0100))
        self.user32.GetForegroundWindow.return_value = 999
        self.assertFalse(self.backend._handle_key(0x1B, 0x0100))
        self.assertFalse(self.backend._handle_key(0x1B, 0x0101))

    def test_hook_thread_unhooks_and_joins_cleanly(self):
        self.assertTrue(self.backend.start_keyboard())
        thread = self.backend._thread
        self.backend.stop()
        self.assertFalse(thread.is_alive())
        self.assertIsNone(self.backend._thread)
        self.user32.UnhookWindowsHookEx.assert_called_once_with(456)
        self.backend.stop()
        self.user32.UnhookWindowsHookEx.assert_called_once()

    def test_failed_hook_thread_initialization_is_joined(self):
        self.user32.SetWindowsHookExW.return_value = None
        self.assertFalse(self.backend.start_keyboard())
        thread = self.backend._thread
        self.backend.stop()
        self.assertFalse(thread.is_alive())
        self.assertIsNotNone(self.backend.keyboard_error)
        self.user32.UnhookWindowsHookEx.assert_not_called()


class SecurityWebcamChecks(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.monitor = SecurityMonitor(backend_factory=lambda config: self.backend)
        self.face = FaceResult(FaceStatus.FACE_DETECTED, 1, HeadDirection.CENTER)
        self.frame = np.zeros((480, 640, 3), np.uint8)
        self.timestamp = datetime(2026, 10, 7, 18, 44, 12, tzinfo=timezone.utc)

    def run_loop(self, *, fail_security=False, phone=False, fail_cv=False, face_missing=False):
        if fail_security:
            self.backend.init_error = OSError("Windows API failed")
        cv_api = MagicMock()
        cv_api.waitKey.side_effect = [0, ord("q")]
        cv_api.getWindowProperty.return_value = 1
        camera = MagicMock()
        camera.read.return_value = (True, self.frame)
        detector = MagicMock()
        detector.detect.return_value = [Detection(67, 0.91, (10, 20, 100, 200))] if phone else []
        if fail_cv:
            detector.detect.side_effect = RuntimeError("CV test failure")
        tracker = MagicMock()
        tracker.available = True
        tracker.process.return_value = FaceResult(FaceStatus.NO_FACE, 0) if face_missing else self.face
        engine = EventEngine(wall_clock=lambda: self.timestamp)
        risk = RiskEngine()
        encoder = Mock(return_value=b"test JPEG")
        directory = Path(self.enterContext(TemporaryDirectory())) / "evidence"
        evidence = EvidenceManager(EvidenceConfig(directory=directory), encoder=encoder)
        original_draw = main.draw_preview
        rendered = []
        original_poll = self.monitor.poll
        polls = 0

        def poll():
            nonlocal polls
            if polls == 1:
                self.backend.keys.append(KeyInput("tab", 1, alt=True))
            polls += 1
            return original_poll()

        def render(_cv2, *args, **kwargs):
            with patch.object(cv2, "putText", wraps=cv2.putText) as put_text:
                preview = original_draw(cv2, *args, **kwargs)
            rendered.append([call.args[1] for call in put_text.call_args_list])
            return preview

        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(sys, "argv", ["main.py"]),
            patch.dict(sys.modules, {"cv2": cv_api}),
            patch.object(main, "YoloDetector", return_value=detector),
            patch.object(main, "FaceTracker", return_value=tracker),
            patch.object(main, "EventEngine", return_value=engine),
            patch.object(main, "RiskEngine", return_value=risk),
            patch.object(main, "EvidenceManager", return_value=evidence),
            patch.object(main, "SecurityMonitor", return_value=self.monitor),
            patch.object(self.monitor, "poll", side_effect=poll),
            patch.object(main, "open_webcam", return_value=(camera, self.frame)),
            patch.object(main, "draw_preview", side_effect=render),
            patch.object(main, "perf_counter", side_effect=[0, 0, 0.01, 3 if face_missing else 1, 3 if face_missing else 1, 3.01 if face_missing else 1.01]),
            contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors),
        ):
            status = main.main()
        self.assertEqual(status, 1 if fail_cv else 0)
        self.assertEqual(self.backend.stops, 1)
        camera.release.assert_called_once()
        tracker.close.assert_called_once()
        cv_api.destroyAllWindows.assert_called_once()
        return engine, risk, evidence, encoder, directory, rendered, output.getvalue(), errors.getvalue()

    def test_security_history_risk_banner_without_evidence(self):
        engine, risk, _, encoder, directory, rendered, output, _ = self.run_loop()
        self.assertEqual([event.type for event in engine.history], [EventType.ALT_TAB_ATTEMPT])
        self.assertEqual(engine.history[0].risk_delta, 15)
        self.assertIsNone(engine.history[0].evidence_path)
        self.assertEqual(risk.current_score, 15)
        encoder.assert_not_called()
        self.assertFalse(directory.exists())
        self.assertIn("Latest: HIGH | ALT_TAB_ATTEMPT", rendered[1])
        self.assertIn("+15 risk | Session risk: 15/100", output)

    def test_cv_and_security_together_save_only_cv_evidence(self):
        engine, risk, _, encoder, directory, _, output, _ = self.run_loop(phone=True)
        self.assertEqual([event.type for event in engine.history], [EventType.PHONE_DETECTED, EventType.ALT_TAB_ATTEMPT])
        self.assertIsNotNone(engine.history[0].evidence_path)
        self.assertIsNone(engine.history[1].evidence_path)
        encoder.assert_called_once()
        self.assertEqual(len(list(directory.glob("*.jpg"))), 1)
        self.assertEqual(risk.current_score, 45)
        self.assertIn("Session risk: 45/100", output)

    def test_failed_security_initialization_preserves_cv_and_q_exit(self):
        engine, risk, _, encoder, _, _, _, errors = self.run_loop(fail_security=True, phone=True)
        self.assertIn("[SECURITY] Warning:", errors)
        self.assertIn("Windows API failed", errors)
        self.assertEqual([event.type for event in engine.history], [EventType.PHONE_DETECTED])
        self.assertEqual(risk.current_score, 30)
        encoder.assert_called_once()

    def test_cv_failure_also_stops_security_listener(self):
        _, _, _, _, _, _, _, errors = self.run_loop(fail_cv=True)
        self.assertIn("CV test failure", errors)

    def test_security_banner_preserves_face_missing_warning(self):
        engine, risk, _, encoder, _, rendered, _, _ = self.run_loop(face_missing=True)
        self.assertEqual([event.type for event in engine.history], [EventType.FACE_MISSING, EventType.ALT_TAB_ATTEMPT])
        self.assertEqual(risk.current_score, 30)
        encoder.assert_called_once()
        self.assertIn("Latest: HIGH | ALT_TAB_ATTEMPT", rendered[1])
        self.assertIn(main.FACE_MISSING_WARNING_TEXT, rendered[1])


if __name__ == "__main__":
    unittest.main()
