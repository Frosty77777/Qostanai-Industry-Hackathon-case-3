"""MainWindow focus recovery integration with fake clocks, timers and Qt actions."""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_builtin_exam import AcademicWorker, definition
from test_desktop import APP, READY, TIMESTAMP, FakeDiscovery, event, update
from PySide6.QtCore import QCoreApplication, QEvent, Qt

from i18n import language_manager
from monitoring import EventType
from security.secure_window import SecureWindowConfig
from ui.assistant_model import AssistantPreferencesStore
from ui.main_window import MainWindow
from ui.session import SessionConfig


class RecordingWorker(AcademicWorker):
    def __init__(self, config, parent):
        super().__init__(config, parent)
        self.security_attempts = []
        self.order = None

    def request_security_attempt(self, kind, source, blocked):
        self.security_attempts.append((kind, source, blocked))
        if self.order is not None:
            self.order.append("event")


class ManualSingleShotTimer:
    """Mirror single-shot scheduling without time passing or actual Qt timeouts."""
    def __init__(self, callback):
        self.callback = callback
        self.active = False
        self.starts = []
        self.stops = 0

    def isActive(self): return self.active
    def start(self, interval):
        self.starts.append(interval)
        self.active = True
    def stop(self):
        self.stops += 1
        self.active = False
    def fire(self):
        self.active = False
        self.callback()


class FocusControllerChecks(unittest.TestCase):
    def setUp(self):
        self.previous_language = language_manager.language
        self.directory = TemporaryDirectory()
        self.now = 100.0
        self.focused = False
        self.application_state = Qt.ApplicationState.ApplicationInactive
        self.order = []
        config = replace(SessionConfig(), exam_definition=definition(), secure_mode="MAXIMIZED",
                         secure_window=SecureWindowConfig(required=True, native_focus_recovery_enabled=False))
        preferences = AssistantPreferencesStore(Path(self.directory.name) / "assistant.json")
        self.window = MainWindow(config, worker_factory=RecordingWorker,
                                 discovery_factory=FakeDiscovery, auto_discover=False,
                                 clock=lambda: self.now, wall_clock=lambda: TIMESTAMP,
                                 assistant_preferences=preferences)
        self.window.setup_page.set_discovery([(0, "Fake camera")], READY)
        self.window.start_session("Student", "Exam", 0)
        self.worker = self.window.worker
        self.worker.session_started.emit(self.now)
        self.worker.order = self.order
        self.assertTrue(self.window._focus_retry_timer.isSingleShot())
        self.window._focus_retry_timer.stop()
        self.timer = ManualSingleShotTimer(self.window._retry_focus)
        self.window._focus_retry_timer = self.timer
        self.patches = []
        for name, action in (("show", "show"), ("showMaximized", "maximize"),
                             ("showFullScreen", "fullscreen"), ("raise_", "raise"),
                             ("activateWindow", "activate")):
            patched = patch.object(self.window, name, side_effect=lambda selected=action: self.order.append(selected))
            patched.start()
            self.patches.append(patched)
        for patched in (
                patch.object(self.window, "isActiveWindow", side_effect=lambda: self.focused),
                patch("ui.main_window.QApplication.applicationState", side_effect=lambda: self.application_state),
                patch("ui.main_window.QApplication.activeModalWidget", return_value=None)):
            patched.start()
            self.patches.append(patched)

    def tearDown(self):
        self.window.finish_session()
        if self.window.worker is not None:
            self.window.worker.complete()
        for patched in reversed(self.patches): patched.stop()
        self.window.close()
        self.window.deleteLater()
        APP.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        language_manager.set_language(self.previous_language)
        self.directory.cleanup()

    def lose_focus(self):
        self.window._application_state_changed(Qt.ApplicationState.ApplicationInactive)

    def retry(self):
        self.now += .201
        self.timer.fire()

    def test_deactivation_queues_security_before_restore_attempts(self):
        self.lose_focus()
        self.assertEqual(self.order, ["event", "show", "maximize", "raise", "activate"])
        self.assertEqual(self.worker.security_attempts, [
            (EventType.WINDOW_FOCUS_LOST, "qt_application_deactivation", False)])
        self.assertEqual(self.window.secure_guard.recovery_attempts, 1)
        self.assertEqual(self.timer.starts, [200])

    def test_emitted_worker_event_reaches_exam_view_before_recovery(self):
        self.worker.pending = replace(update(events=(event(EventType.WINDOW_FOCUS_LOST, 15),), score=15),
                                      window_focus_lost=True)
        original = self.window.exam_page.apply_update
        def applied(packet):
            self.order.append("apply event")
            original(packet)
        with patch.object(self.window.exam_page, "apply_update", side_effect=applied):
            self.window._updated()
        self.assertEqual(self.order[0], "apply event")
        self.assertEqual(self.order[1:], ["show", "maximize", "raise", "activate"])
        self.assertEqual(self.window.exam_page.risk_score.text(), "15 / 100")

    def test_missing_focus_frames_do_not_postpone_already_scheduled_retry(self):
        self.lose_focus()
        for _ in range(10):
            self.now += .01
            self.worker.pending = replace(update(), window_focus_lost=True)
            self.window._updated()
        self.assertEqual(self.timer.starts, [200])
        self.assertEqual(self.window.secure_guard.recovery_attempts, 1)
        self.assertTrue(self.timer.isActive())
        self.retry()
        self.assertEqual(self.window.secure_guard.recovery_attempts, 2)
        self.assertEqual(self.timer.starts, [200, 200])

    def test_burst_exhausts_after_four_controller_attempts(self):
        self.lose_focus()
        for _ in range(3): self.retry()
        self.assertEqual(self.order.count("activate"), 4)
        self.assertFalse(self.timer.isActive())
        self.assertFalse(self.window.secure_guard.recovery_pending)
        for _ in range(5):
            self.worker.pending = replace(update(), window_focus_lost=True)
            self.window._updated()
            self.retry()
        self.assertEqual(self.order.count("activate"), 4)
        self.assertEqual(self.timer.starts, [200, 200, 200])

    def test_confirmed_application_focus_stops_retry_immediately(self):
        self.lose_focus()
        self.focused = True
        self.application_state = Qt.ApplicationState.ApplicationActive
        self.window._application_state_changed(Qt.ApplicationState.ApplicationActive)
        self.assertFalse(self.timer.isActive())
        self.assertFalse(self.window.secure_guard.recovery_pending)
        self.retry()
        self.assertEqual(self.order.count("activate"), 1)
        self.assertEqual(len(self.worker.security_attempts), 1)

    def test_retry_detects_success_before_a_delayed_application_activation_signal(self):
        self.lose_focus()
        self.focused = True
        self.application_state = Qt.ApplicationState.ApplicationActive
        self.retry()
        self.assertFalse(self.timer.isActive())
        self.assertEqual(self.order.count("activate"), 1)

    def test_false_unavailable_native_packets_do_not_renew_exhausted_budget(self):
        self.lose_focus()
        for _ in range(3): self.retry()
        for _ in range(5):
            self.worker.pending = update()  # false can also mean focus monitor unavailable
            self.window._updated()
            self.window._recover_focus()
        self.assertEqual(self.order.count("activate"), 4)
        self.assertFalse(self.timer.isActive())

    def test_fresh_loss_after_confirmed_regain_gets_a_new_bounded_burst(self):
        self.lose_focus()
        for _ in range(3): self.retry()
        self.focused = True
        self.application_state = Qt.ApplicationState.ApplicationActive
        self.window._application_state_changed(Qt.ApplicationState.ApplicationActive)
        self.focused = False
        self.application_state = Qt.ApplicationState.ApplicationInactive
        self.lose_focus()
        self.assertEqual(self.window.secure_guard.recovery_attempts, 1)
        self.assertEqual(self.order.count("activate"), 5)
        self.assertEqual(len(self.worker.security_attempts), 2)

    def test_invalid_teacher_pin_does_not_disable_recovery(self):
        self.lose_focus()
        self.window._exit_secure_mode("wrong")
        self.assertTrue(self.window._secure_mode_active)
        self.assertTrue(self.window.secure_guard.recovery_pending)
        self.assertTrue(self.timer.isActive())

    def test_valid_teacher_pin_cancels_pending_and_future_retries(self):
        self.lose_focus()
        self.window._exit_secure_mode("1234")
        self.assertFalse(self.window._secure_mode_active)
        self.assertFalse(self.window.secure_guard.active)
        self.assertFalse(self.timer.isActive())
        self.retry()
        self.lose_focus()
        self.assertEqual(self.order.count("activate"), 1)
        self.assertEqual(len(self.worker.security_attempts), 1)

    def test_finish_session_cancels_pending_recovery_before_worker_cleanup(self):
        self.lose_focus()
        self.window.finish_session()
        self.assertEqual(self.window.state, "STOPPING")
        self.assertTrue(self.worker.stopped)
        self.assertFalse(self.window.secure_guard.active)
        self.assertFalse(self.timer.isActive())
        self.retry()
        self.assertEqual(self.order.count("activate"), 1)

    def test_external_shutdown_cancels_recovery_and_joins_worker(self):
        self.lose_focus()
        self.window.shutdown_blocking()
        self.assertTrue(self.worker.stopped)
        self.assertTrue(self.worker.joined)
        self.assertFalse(self.window.secure_guard.active)
        self.assertFalse(self.timer.isActive())
        self.retry()
        self.assertEqual(self.order.count("activate"), 1)

    def test_restore_failure_keeps_other_requests_and_bounded_cleanup(self):
        with patch.object(self.window, "showMaximized", side_effect=RuntimeError("restore refused")):
            self.lose_focus()
        self.assertEqual(self.order, ["event", "show", "raise", "activate"])
        self.assertTrue(self.timer.isActive())
        self.assertIn("restore refused", self.window.secure_guard.last_error)
        self.window.finish_session()
        self.assertFalse(self.timer.isActive())

    def test_fullscreen_configuration_is_reasserted_by_controller(self):
        self.window.secure_guard.activate("FULLSCREEN")
        self.lose_focus()
        self.assertEqual(self.order, ["event", "show", "fullscreen", "raise", "activate"])

    def test_native_requests_use_captured_protected_hwnd(self):
        self.window.secure_guard.config = replace(self.window.secure_guard.config,
                                                   native_focus_recovery_enabled=True)
        native = SimpleNamespace(last_error=None, is_foreground=Mock(return_value=False),
                                 recover=Mock(return_value=False))
        self.window.secure_guard._native_helper = native
        protected = self.worker.config.window_handle
        with patch.object(self.window, "winId", return_value=999999):
            self.lose_focus()
            self.retry()
        self.assertEqual(native.recover.call_args_list[0].args, (protected, "MAXIMIZED"))
        self.assertEqual(native.recover.call_args_list[1].args, (protected, "MAXIMIZED"))

    def test_stale_worker_packet_after_native_success_does_not_repeat_activation(self):
        self.window.secure_guard.config = replace(self.window.secure_guard.config,
                                                   native_focus_recovery_enabled=True)
        native = SimpleNamespace(last_error=None, foreground=False)
        native.is_foreground = lambda handle: native.foreground
        def succeed(handle, mode):
            native.foreground = True
            return True
        native.recover = Mock(side_effect=succeed)
        self.window.secure_guard._native_helper = native
        self.lose_focus()
        self.assertFalse(self.timer.isActive())
        self.assertEqual(self.order.count("activate"), 1)
        self.worker.pending = replace(update(), window_focus_lost=True)
        self.window._updated()  # Qt still reports inactive in this fixture.
        self.assertFalse(self.timer.isActive())
        self.assertEqual(self.order.count("activate"), 1)
        native.recover.assert_called_once()
        self.assertEqual(len(self.worker.security_attempts), 1)

    def test_trusted_active_modal_is_not_covered_by_forced_parent_activation(self):
        from PySide6.QtWidgets import QWidget
        modal = QWidget(self.window)
        self.application_state = Qt.ApplicationState.ApplicationActive
        with patch("ui.main_window.QApplication.activeModalWidget", return_value=modal), \
                patch.object(modal, "isActiveWindow", return_value=True):
            self.assertFalse(self.window._recover_focus())
        self.assertEqual(self.order, [])
        self.assertFalse(self.timer.isActive())
        modal.deleteLater()


if __name__ == "__main__":
    unittest.main()
