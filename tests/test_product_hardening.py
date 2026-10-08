"""Controller authorization, secure window recovery and cleanup regressions."""

from dataclasses import replace
import contextlib
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_builtin_exam import AcademicWorker, definition
from test_desktop import (APP, READY, TIMESTAMP, FakeCapture, FakeDiscovery, FakeFace,
                          FakeSecurity, event, update, usable_frame)

from PySide6.QtCore import QEventLoop, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QImage, QKeyEvent
from PySide6.QtWidgets import QLabel

from evidence import EvidenceManager
from monitoring import EventType
from security import SecurityMonitor
from security.secure_window import SecureWindowConfig, SecureWindowGuard
from session_storage import SessionStorageConfig
from session_storage import SessionStore
from session_storage.session_repository import LocalSessionRepository, SessionSummary
from ui.main_window import MainWindow
from ui.review_worker import ReviewLoadWorker
from ui.session import SessionConfig, SessionResult
from ui.vision_worker import VisionWorker


def stored_result(student="Student", risk=42):
    from exams import ExamAttempt
    attempt = ExamAttempt(definition())
    attempt.start(TIMESTAMP)
    attempt.set_answer("single", "B")
    academic = attempt.submit(submitted_at=TIMESTAMP, duration_seconds=12)
    return SessionResult(student, "Saved Exam", 12, risk, "MODERATE", (event(),), {},
                         started_at=TIMESTAMP, ended_at=TIMESTAMP,
                         session_directory=Path("session-fixture"), exam_result=academic)


class SecurityWorker(AcademicWorker):
    def __init__(self, config, parent):
        super().__init__(config, parent)
        self.security_attempts = []

    def request_security_attempt(self, kind, source, blocked):
        self.security_attempts.append((kind, source, blocked))


class FakeRepository:
    errors = ()
    def __init__(self):
        self.list_calls = 0
        self.load_calls = []
        self.results = {"one": stored_result("Alice"), "two": stored_result("Bob", 20)}
        self.rows = tuple(SessionSummary(key, result.student, result.exam, TIMESTAMP, 12,
                                         1, 2, result.risk_score, result.risk_level)
                          for key, result in self.results.items())
    def list_completed(self):
        self.list_calls += 1
        return self.rows
    def load(self, session_id):
        self.load_calls.append(session_id)
        return self.results[session_id]


class FakeReader(QObject):
    listed = Signal(object, object)
    loaded = Signal(object)
    failed = Signal(str)
    finished = Signal()
    def __init__(self, repository, parent=None, *, session_id=None):
        super().__init__(parent)
        self.repository, self.session_id = repository, session_id
        self.interrupted = self.joined = False
    def start(self):
        try:
            if self.session_id is None:
                self.listed.emit(self.repository.list_completed(), self.repository.errors)
            else:
                self.loaded.emit(self.repository.load(self.session_id))
        except Exception as exc:
            self.failed.emit(str(exc))
        self.finished.emit()
    def requestInterruption(self): self.interrupted = True
    def wait(self):
        self.joined = True
        return True


class DeferredReader(FakeReader):
    def start(self): pass


class HardenedControllerChecks(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.windows = []
        self.repository = FakeRepository()
        self.window = self.make_window()

    def make_window(self, *, secure=False, reader=FakeReader, repository=None):
        config = replace(SessionConfig(), exam_definition=definition(),
                         secure_mode="MAXIMIZED" if secure else "WINDOWED",
                         secure_window=SecureWindowConfig(required=secure))
        window = MainWindow(config, worker_factory=SecurityWorker,
                            discovery_factory=FakeDiscovery, auto_discover=False,
                            clock=lambda: self.now, wall_clock=lambda: TIMESTAMP,
                            session_repository=repository or self.repository, review_worker_factory=reader)
        window.setup_page.set_discovery([(0, "Fake camera")], READY)
        self.windows.append(window)
        return window

    def tearDown(self):
        for window in self.windows:
            if window.worker is not None:
                window.finish_session()
                window.worker.complete()
            if window.review_worker is not None:
                window.review_worker.finished.emit()
            window.close()
            window.deleteLater()
        APP.processEvents()

    def start(self, window=None):
        window = window or self.window
        window.start_session("Student", "Exam", 0)
        worker = window.worker
        worker.session_started.emit(self.now)
        return worker

    def unlock(self, window=None):
        window = window or self.window
        window.open_teacher_review()
        window.teacher_review_page.pin_input.setText("1234")
        window.teacher_review_page.unlock_button.click()

    def test_required_secure_mode_cannot_select_windowed(self):
        window = self.make_window(secure=True)
        window.setup_page.set_secure_mode("WINDOWED")  # trusted API; start still enforces policy
        worker = self.start(window)
        self.assertEqual(worker.config.secure_mode, "MAXIMIZED")
        self.assertTrue(window.isMaximized())
        self.assertTrue(window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertEqual(worker.config.window_handle, int(window.winId()))
        self.assertFalse(window._q_shortcut.isEnabled())

    def test_escape_does_not_stop_student_secure_session_and_is_recorded(self):
        window = self.make_window(secure=True)
        worker = self.start(window)
        key = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        window.keyPressEvent(key)
        self.assertTrue(key.isAccepted())
        self.assertEqual(window.state, "EXAM")
        self.assertFalse(worker.stopped)
        self.assertEqual(worker.security_attempts[0][0], EventType.ESCAPE_ATTEMPT)

    def test_ordinary_close_cannot_leave_student_secure_session(self):
        window = self.make_window(secure=True)
        worker = self.start(window)
        closing = QCloseEvent()
        with patch.object(window, "_recover_focus"):
            window.closeEvent(closing)
        self.assertFalse(closing.isAccepted())
        self.assertFalse(worker.stopped)
        self.assertFalse(window._closing)
        self.assertEqual(window.state, "EXAM")

    def test_teacher_pin_leaves_secure_mode_without_rebinding_protected_hwnd(self):
        window = self.make_window(secure=True)
        worker = self.start(window)
        bound = worker.config.window_handle
        window._exit_secure_mode("wrong")
        self.assertTrue(window._secure_mode_active)
        window._exit_secure_mode("1234")
        self.assertFalse(window._secure_mode_active)
        self.assertFalse(window.secure_guard.active)
        self.assertEqual(int(window.winId()), bound)
        self.assertEqual(window.state, "EXAM")
        self.assertFalse(worker.stopped)

    def test_chrome_restored_only_after_security_cleanup(self):
        window = self.make_window(secure=True)
        worker = self.start(window)
        window._submit_exam()
        self.assertTrue(window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        worker.complete()
        self.assertFalse(window.windowFlags() & Qt.WindowType.FramelessWindowHint)
        self.assertFalse(window.secure_guard.active)

    def test_application_focus_loss_queues_event_before_recovery(self):
        window = self.make_window(secure=True)
        worker = self.start(window)
        order = []
        worker.request_security_attempt = lambda *args: order.append(("event", args))
        with patch.object(window, "_recover_focus", side_effect=lambda: order.append(("recover",))):
            window._application_state_changed(Qt.ApplicationState.ApplicationInactive)
        self.assertEqual([item[0] for item in order], ["event", "recover"])
        self.assertEqual(order[0][1][0], EventType.WINDOW_FOCUS_LOST)

    def test_focus_loss_packet_recovers_even_if_event_cooldown_suppresses_repeat(self):
        window = self.make_window(secure=True)
        worker = self.start(window)
        worker.pending = replace(update(), window_focus_lost=True)
        with patch.object(window, "_recover_focus") as recovery:
            window._updated()
        recovery.assert_called_once()

    def test_secure_shortcut_suppression_uses_existing_hook_configuration(self):
        window = self.make_window(secure=True)
        window.config = replace(window.config, secure_window=SecureWindowConfig(
            required=True, suppressed_shortcuts={EventType.PASTE_ATTEMPT, EventType.ESCAPE_ATTEMPT}))
        worker = self.start(window)
        self.assertEqual(worker.config.security.blocked_events,
                         {EventType.PASTE_ATTEMPT, EventType.ESCAPE_ATTEMPT})
        self.assertTrue(window.exam_page.question_panel.text_answer.suppress_paste)

    def test_submission_renders_only_student_completion_page(self):
        worker = self.start()
        self.window._submit_exam()
        worker.complete(events=(event(),))
        self.window.show()
        APP.processEvents()
        page = self.window.report_page
        self.assertTrue(page.completion_page.isVisible())
        self.assertFalse(page.detail_widget.isVisible())
        self.assertFalse(page.risk_score.isVisible())
        self.assertFalse(page.tabs.isVisible())
        self.assertEqual(self.window.result.risk_score, 30)
        visible = " ".join(label.text() for label in page.findChildren(QLabel) if label.isVisible())
        self.assertIn("EXAM SUBMITTED", visible)
        for secret in ("PHONE_DETECTED", "PHONE DETECTED", "SESSION RISK", "Total Events", "30 / 100"):
            self.assertNotIn(secret, visible)

    def test_student_cannot_open_teacher_data_without_pin(self):
        self.window.open_teacher_review()
        self.window._open_teacher_session("one")
        self.window._load_teacher_sessions()
        self.assertEqual(self.repository.list_calls, 0)
        self.assertEqual(self.repository.load_calls, [])
        self.window.teacher_review_page.pin_input.setText("wrong")
        self.window.teacher_review_page.unlock_button.click()
        self.assertFalse(self.window._teacher_authorized)
        self.assertEqual(self.window.state, "TEACHER_LOGIN")
        self.assertEqual(self.window.teacher_review_page.sessions_model.rowCount(), 0)
        self.assertIn("Access denied", self.window.teacher_review_page.pin_error.text())
        self.assertEqual(self.repository.list_calls, 0)

    def test_valid_teacher_pin_lists_completed_sessions(self):
        self.unlock()
        self.assertTrue(self.window._teacher_authorized)
        self.assertEqual(self.window.state, "TEACHER_REVIEW")
        self.assertEqual(self.repository.list_calls, 1)
        self.assertEqual(self.window.teacher_review_page.sessions_model.rowCount(), 2)

    def test_teacher_opens_exact_selected_report_and_returns_to_list(self):
        self.unlock()
        self.window.teacher_review_page._open_session(1)
        self.assertEqual(self.repository.load_calls, ["two"])
        self.assertEqual(self.window.report_page.values["Student"].text(), "Bob")
        self.assertEqual(self.window.report_page.risk_score.text(), "20 / 100")
        self.assertEqual(self.window.state, "TEACHER_REPORT")
        self.window._teacher_back_to_list()
        self.assertEqual(self.window.state, "TEACHER_REVIEW")
        self.assertIs(self.window.pages.currentWidget(), self.window.teacher_review_page)

    def test_teacher_logout_hides_report_and_requires_pin_again(self):
        self.unlock()
        self.window._open_teacher_session("one")
        self.window._leave_teacher_review()
        self.assertFalse(self.window._teacher_authorized)
        self.assertEqual(self.window.state, "SETUP")
        self.window.open_teacher_review()
        self.assertEqual(self.window.state, "TEACHER_LOGIN")
        self.assertEqual(self.window.teacher_review_page.sessions_model.rowCount(), 0)
        self.window._open_teacher_session("one")
        self.assertEqual(self.repository.load_calls, ["one"])

    def test_teacher_return_to_student_completion_hides_other_saved_student(self):
        worker = self.start()
        self.window._submit_exam()
        worker.complete()
        self.unlock()
        self.window._open_teacher_session("two")
        self.window._leave_teacher_review()
        self.assertEqual(self.window.state, "REPORT")
        self.assertEqual(self.window.report_page.completion_page.values["Student"].text(), "Student")
        self.assertFalse(self.window._teacher_authorized)

    def test_current_unsaved_session_remains_reviewable_with_teacher_pin(self):
        worker = self.start()
        self.window._submit_exam()
        worker.complete(events=(event(),))
        self.unlock()
        self.assertEqual(self.window.teacher_review_page.sessions_model.rowCount(), 3)
        self.assertIn("retained in memory", self.window.teacher_review_page.list_error.text())
        self.window._open_teacher_session(self.window.CURRENT_MEMORY_SESSION_ID)
        self.assertEqual(self.window.state, "TEACHER_REPORT")
        self.assertEqual(self.window.report_page.risk_score.text(), "30 / 100")
        self.assertEqual(self.window.report_page.timeline_model.rowCount(), 1)
        self.assertEqual(self.repository.load_calls, [])

    def test_current_unsaved_report_cannot_bypass_teacher_authorization(self):
        worker = self.start()
        self.window._submit_exam()
        worker.complete()
        self.window.open_teacher_review()
        self.window._open_teacher_session(self.window.CURRENT_MEMORY_SESSION_ID)
        self.assertEqual(self.window.state, "TEACHER_LOGIN")
        self.assertFalse(self.window._teacher_authorized)

    def test_logout_discards_pending_async_report(self):
        window = self.make_window(reader=DeferredReader)
        self.unlock(window)
        reader = window.review_worker
        window._leave_teacher_review()
        self.assertTrue(reader.interrupted)
        reader.loaded.emit(self.repository.results["one"])
        self.assertEqual(window.state, "SETUP")
        self.assertFalse(window._teacher_authorized)
        reader.finished.emit()
        self.assertTrue(reader.joined)

    def test_teacher_review_is_unavailable_during_active_exam(self):
        self.start()
        self.window.open_teacher_review()
        self.assertEqual(self.window.state, "EXAM")
        self.assertFalse(self.window._teacher_authorized)
        self.assertEqual(self.repository.list_calls, 0)

    def test_reauthorization_rejects_old_reader_and_starts_fresh_list_after_join(self):
        window = self.make_window(reader=DeferredReader)
        self.unlock(window)
        old_reader = window.review_worker
        window._leave_teacher_review()
        self.unlock(window)
        self.assertTrue(window._teacher_authorized)
        old_reader.loaded.emit(self.repository.results["one"])
        old_reader.listed.emit(self.repository.rows, ())
        old_reader.failed.emit("old failure")
        self.assertEqual(window.state, "TEACHER_REVIEW")
        self.assertEqual(window.teacher_review_page.sessions_model.rowCount(), 0)
        self.assertNotIn("old failure", window.teacher_review_page.list_error.text())
        old_reader.finished.emit()
        fresh_reader = window.review_worker
        self.assertIsNotNone(fresh_reader)
        self.assertIsNot(fresh_reader, old_reader)
        fresh_reader.listed.emit(self.repository.rows, ())
        self.assertEqual(window.teacher_review_page.sessions_model.rowCount(), 2)
        fresh_reader.finished.emit()

    def test_persisted_session_and_evidence_open_only_after_teacher_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            storage = SessionStorageConfig(Path(directory))
            store = SessionStore(storage)
            folder = store.create_session("Archived Student", TIMESTAMP)
            picture = folder / "evidence" / "PHONE_DETECTED.jpg"
            image = QImage(64, 48, QImage.Format.Format_RGB888)
            image.fill(0x305070)
            self.assertTrue(image.save(str(picture)))
            saved = replace(stored_result("Archived Student", 30), session_directory=folder,
                            events=(replace(event(), evidence_path=str(picture)),),
                            event_counts={EventType.PHONE_DETECTED: 1})
            store.save_session(saved)
            original = {file.name: file.read_bytes() for file in (folder / "session.json", folder / "events.json", picture)}
            window = self.make_window(repository=LocalSessionRepository(storage))
            self.unlock(window)
            self.assertEqual(window.teacher_review_page.sessions_model.rowCount(), 1)
            window.teacher_review_page._open_session(0)
            self.assertEqual(window.state, "TEACHER_REPORT")
            self.assertEqual(window.report_page.values["Student"].text(), "Archived Student")
            gallery = window.report_page.evidence
            self.assertEqual(len(gallery.cards), 1)
            self.assertEqual(gallery.cards[0][3], "")
            dialog = gallery.open_event(gallery.events[0])
            self.assertEqual(dialog.error, "")
            window.report_page.back_to_sessions_button.click()
            self.assertIsNone(gallery.dialog)
            self.assertEqual(window.state, "TEACHER_REVIEW")
            window._leave_teacher_review()
            self.assertEqual(original, {file.name: file.read_bytes() for file in (folder / "session.json", folder / "events.json", picture)})


class SecureWindowGuardChecks(unittest.TestCase):
    def test_recovery_raises_and_activates_without_native_api_or_os_keys(self):
        window = Mock()
        window.isMinimized.return_value = False
        guard = SecureWindowGuard(clock=lambda: 100)
        guard.activate("MAXIMIZED")
        self.assertTrue(guard.recover(window))
        window.raise_.assert_called_once()
        window.activateWindow.assert_called_once()
        window.showMaximized.assert_not_called()

    def test_minimized_window_is_restored_and_recovery_is_rate_limited(self):
        now = [100]
        window = Mock()
        window.isMinimized.return_value = True
        guard = SecureWindowGuard(clock=lambda: now[0])
        guard.activate("FULLSCREEN")
        self.assertTrue(guard.recover(window))
        window.showFullScreen.assert_called_once()
        now[0] = 100.9
        self.assertFalse(guard.recover(window))
        now[0] = 101
        self.assertTrue(guard.recover(window))
        self.assertEqual(window.raise_.call_count, 2)

    def test_recovery_failure_is_graceful_and_still_attempts_activation(self):
        window = Mock()
        window.isMinimized.return_value = False
        window.raise_.side_effect = RuntimeError("Denied")
        guard = SecureWindowGuard(clock=lambda: 100)
        guard.activate("MAXIMIZED")
        self.assertTrue(guard.recover(window))
        window.activateWindow.assert_called_once()
        self.assertIn("Denied", guard.last_error)

    def test_released_or_disabled_guard_does_not_attempt_focus_recovery(self):
        window = Mock()
        guard = SecureWindowGuard(SecureWindowConfig(focus_recovery_enabled=False))
        guard.activate("FULLSCREEN")
        self.assertFalse(guard.recover(window))
        guard.release()
        self.assertFalse(guard.active)
        window.raise_.assert_not_called()

    def test_window_adapter_state_failure_does_not_crash_recovery(self):
        window = Mock()
        window.isMinimized.side_effect = RuntimeError("Window state unavailable")
        guard = SecureWindowGuard(clock=lambda: 100)
        guard.activate("MAXIMIZED")
        self.assertTrue(guard.recover(window))
        window.activateWindow.assert_called_once()
        self.assertIn("Window state unavailable", guard.last_error)

    def test_unsafe_suppression_and_invalid_timing_are_rejected(self):
        for invalid in ({"suppressed_shortcuts": {EventType.ALT_TAB_ATTEMPT}},
                        {"focus_recovery_cooldown_seconds": -1},
                        {"focus_recovery_cooldown_seconds": float("nan")}, {"required": "yes"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                SecureWindowConfig(**invalid)


class HardenedSecurityWorkerChecks(unittest.TestCase):
    def test_application_focus_and_escape_events_keep_risk_history_without_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            capture, tracker, security = FakeCapture(), FakeFace(), FakeSecurity()
            encoder = Mock(return_value=b"fixture")
            worker = VisionWorker(
                replace(SessionConfig(), storage=SessionStorageConfig(Path(directory))),
                clock=lambda: 100, wall_clock=lambda: TIMESTAMP,
                camera_opener=lambda cv2, index: (capture, usable_frame()),
                detector_factory=lambda *args: Mock(detect=Mock(return_value=[])),
                face_factory=lambda *args: tracker, security_factory=lambda: security,
                evidence_factory=lambda config: EvidenceManager(config, encoder=encoder),
            )
            capture.worker = worker
            for _ in range(10):
                worker.request_security_attempt(EventType.WINDOW_FOCUS_LOST, "qt_application_deactivation")
                worker.request_security_attempt(EventType.ESCAPE_ATTEMPT, "qt_secure_window")
            self.assertFalse(worker.request_security_attempt(EventType.PHONE_DETECTED, "fake"))
            with contextlib.redirect_stdout(io.StringIO()): worker.run()
            self.assertEqual({event.type for event in worker.result.events},
                             {EventType.WINDOW_FOCUS_LOST, EventType.ESCAPE_ATTEMPT})
            self.assertEqual(worker.result.risk_score, 20)
            self.assertTrue(all(event.evidence_path is None for event in worker.result.events))
            encoder.assert_not_called()
            self.assertTrue(capture.released and tracker.closed and security.stopped)
            self.assertFalse(worker.request_security_attempt(EventType.ESCAPE_ATTEMPT, "qt"))

    def test_native_and_application_security_events_share_cooldown(self):
        monitor = SecurityMonitor(clock=lambda: 100, wall_clock=lambda: TIMESTAMP)
        first = monitor.record_application_event(EventType.WINDOW_FOCUS_LOST, source="qt", now=100)
        self.assertFalse(first.metadata["blocked"])
        self.assertIsNone(monitor._emit(EventType.WINDOW_FOCUS_LOST, 101, {"source": "window_focus"}))
        self.assertIsNotNone(monitor.record_application_event(EventType.WINDOW_FOCUS_LOST, source="qt", now=103))
        blocked = monitor.record_application_event(EventType.ESCAPE_ATTEMPT, source="qt", blocked=True, now=103)
        self.assertTrue(blocked.metadata["blocked"])
        with self.assertRaises(ValueError):
            monitor.record_application_event(EventType.PHONE_DETECTED, source="invalid")


class ReviewThreadChecks(unittest.TestCase):
    def test_official_desktop_entry_point_requires_maximized_secure_session(self):
        import desktop
        from main import parse_args
        with patch.object(sys, "argv", ["desktop.py"]):
            args = parse_args()
        application = Mock()
        application.exec.return_value = 0
        factory = Mock()
        with patch.object(desktop, "parse_args", return_value=args), \
                patch.object(desktop, "prepare_face_tracking_runtime"), \
                patch("PySide6.QtWidgets.QApplication", return_value=application), \
                patch.dict(sys.modules, {"ui.main_window": SimpleNamespace(MainWindow=factory)}), \
                patch.object(desktop.signal, "signal"):
            self.assertEqual(desktop.main(), 0)
        config = factory.call_args.args[0]
        self.assertEqual(config.secure_mode, "MAXIMIZED")
        self.assertTrue(config.secure_window.required)

    def test_read_only_review_uses_qthread_and_emits_normalized_result(self):
        repository = FakeRepository()
        worker = ReviewLoadWorker(repository, session_id="two")
        received, errors = [], []
        loop, watchdog = QEventLoop(), QTimer()
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(loop.quit)
        worker.loaded.connect(received.append)
        worker.failed.connect(errors.append)
        worker.finished.connect(loop.quit)
        try:
            worker.start()
            watchdog.start(5000)
            loop.exec()
            self.assertTrue(watchdog.isActive())
            self.assertTrue(worker.wait(1000))
            self.assertEqual(errors, [])
            self.assertEqual(received[0].student, "Bob")
            self.assertEqual(repository.load_calls, ["two"])
        finally:
            worker.requestInterruption()
            worker.wait()
            worker.deleteLater()
            watchdog.stop()
            APP.processEvents()


if __name__ == "__main__":
    unittest.main()
