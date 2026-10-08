"""Desktop session controller. Widgets never call inference or read a camera."""

from dataclasses import replace
from math import ceil
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QMainWindow, QStackedWidget

from exams import ExamAttempt
from monitoring import EventType
from security.secure_window import SecureWindowGuard
from session_storage.session_repository import LocalSessionRepository, SessionSummary

from .assistant_model import AssistantPreferencesStore
from .exam_page import ExamPage
from .report_page import ReportPage
from .review_worker import ReviewLoadWorker
from .session import SessionConfig, SessionTimer, session_wall_time
from .setup_page import SetupPage
from .teacher_review_page import TeacherReviewPage
from .theme import STYLESHEET
from .vision_worker import CameraDiscoveryWorker, VisionWorker


class MainWindow(QMainWindow):
    CURRENT_MEMORY_SESSION_ID = "memory://current"

    def __init__(self, config=None, *, worker_factory=VisionWorker,
                 discovery_factory=CameraDiscoveryWorker, clock=None, auto_discover=True,
                 wall_clock=session_wall_time, session_repository=None,
                 review_worker_factory=ReviewLoadWorker, assistant_preferences=None):
        super().__init__()
        self.config = config or SessionConfig()
        self._worker_factory = worker_factory
        self._discovery_factory = discovery_factory
        self.worker = None
        self.discovery = None
        self.result = None
        self.state = "SETUP"
        self._closing = False
        self._session_started = False
        self._startup_error = None
        self._wall_clock = wall_clock
        self.exam_attempt = None
        self._session_config = None
        self._authorization_active = self._answer_editing = False
        self._secure_mode_active = False
        self._normal_geometry = None
        self._normal_flags = self.windowFlags()
        self._chrome_removed = False
        self._focus_warning = None
        self.secure_guard = SecureWindowGuard(self.config.secure_window) if clock is None else SecureWindowGuard(
            self.config.secure_window, clock=clock)
        self._teacher_authorized = False
        self._teacher_return_state = "SETUP"
        self._session_repository = session_repository or LocalSessionRepository(self.config.storage)
        self._review_worker_factory = review_worker_factory
        self.review_worker = None
        self._review_epoch = 0
        self._review_refresh_pending = False
        self.session_timer = SessionTimer() if clock is None else SessionTimer(clock)
        self.setWindowTitle("AI Exam Guard")
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        width = min(1280, available.width()) if available else 1280
        height = min(800, available.height()) if available else 800
        self.setMinimumSize(min(1180, width), min(720, height))
        self.resize(width, height)
        self.setStyleSheet(STYLESHEET)
        self.pages = QStackedWidget()
        self.assistant_preferences = (assistant_preferences if assistant_preferences is not None
                                      else AssistantPreferencesStore())
        self.setup_page = SetupPage(assistant_preferences=self.assistant_preferences,
                                    assistant_clock=clock)
        self.setup_page.set_secure_mode(self.config.secure_mode)
        self.setup_page.set_secure_window_required(self.config.secure_window.required)
        if self.config.exam_definition is not None:
            self.setup_page.set_exam_definition(self.config.exam_definition)
        self.exam_page = ExamPage(assistant_preferences=self.assistant_preferences,
                                 assistant_clock=clock)
        self.report_page = ReportPage()
        self.teacher_review_page = TeacherReviewPage()
        for page in (self.setup_page, self.exam_page, self.report_page, self.teacher_review_page):
            self.pages.addWidget(page)
        self.setCentralWidget(self.pages)
        self.setup_page.start_requested.connect(self.start_session)
        self.setup_page.refresh_requested.connect(self.discover)
        self.exam_page.finish_requested.connect(self.finish_session)
        self.report_page.new_session_requested.connect(self.new_session)
        self.setup_page.teacher_review_requested.connect(self.open_teacher_review)
        self.report_page.teacher_review_requested.connect(self.open_teacher_review)
        self.report_page.back_to_sessions_requested.connect(self._teacher_back_to_list)
        self.teacher_review_page.pin_requested.connect(self._authorize_teacher)
        self.teacher_review_page.session_open_requested.connect(self._open_teacher_session)
        self.teacher_review_page.refresh_requested.connect(self._load_teacher_sessions)
        self.teacher_review_page.back_requested.connect(self._leave_teacher_review)
        self.teacher_review_page.logout_requested.connect(self._leave_teacher_review)
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._tick)
        self._q_shortcut = QShortcut(QKeySequence("Q"), self)
        self._q_shortcut.setEnabled(False)
        self._q_shortcut.activated.connect(self.finish_session)
        self.exam_page.break_requested.connect(self._authorize_break)
        self.exam_page.end_break_requested.connect(self._end_break)
        self.exam_page.authorization_mode_changed.connect(self._authorization_mode)
        self.exam_page.secure_exit_requested.connect(self._exit_secure_mode)
        questions = self.exam_page.question_panel
        questions.submit_requested.connect(self._request_submission)
        questions.submission_confirmed.connect(self._submit_exam)
        questions.answer_changed.connect(self._publish_exam_snapshot)
        questions.editing_changed.connect(self._answer_editing_changed)
        questions.local_paste_requested.connect(self._paste_attempt)
        QApplication.instance().applicationStateChanged.connect(self._application_state_changed)
        if auto_discover:
            QTimer.singleShot(0, self.discover)

    def discover(self):
        if self._closing or self.worker is not None or self.discovery is not None:
            return
        self.setup_page.set_busy(True, "Checking available cameras and local model files…")
        self.discovery = self._discovery_factory(self.config, self)
        self.discovery.discovered.connect(self._discovered)
        self.discovery.finished.connect(self._discovery_finished)
        self.discovery.start()

    def _discovered(self, cameras, statuses):
        if not self._closing:
            self.setup_page.set_discovery(cameras, statuses)

    def _discovery_finished(self):
        discovery, self.discovery = self.discovery, None
        if discovery is not None:
            discovery.wait()
            discovery.deleteLater()
        self.setup_page.set_busy(False)
        self._finish_close_if_ready()

    def start_session(self, student, exam, camera_index):
        if (self.worker is not None or self.discovery is not None or self.review_worker is not None
                or self._closing or self.state != "SETUP"):
            return
        if camera_index is None or not self.setup_page._camera_ready or not self.setup_page._ai_ready:
            self.setup_page.message.setText("Camera and AI detection must be ready before starting.")
            return
        self.result = None
        self._startup_error = None
        self._session_started = False
        self.session_timer.reset()
        definition = self.setup_page.selected_exam
        if definition is None:
            self.setup_page.message.setText("Load a valid local exam before starting.")
            return
        self.exam_attempt = ExamAttempt(definition)
        self._authorization_active = self._answer_editing = False
        self._teacher_authorized = False
        mode = self.setup_page.selected_secure_mode
        if mode == "WINDOWED" and self.config.secure_window.required:
            mode = "MAXIMIZED"
        self._normal_geometry = self.saveGeometry()
        self._secure_mode_active = mode != "WINDOWED"
        self.secure_guard.activate(mode)
        if self._secure_mode_active:
            # Change chrome before binding the protected HWND. Keep these flags
            # until hooks stop; toggling flags mid-session can recreate the HWND.
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
            self._chrome_removed = True
        if mode == "FULLSCREEN":
            self.showFullScreen()
        elif mode == "MAXIMIZED":
            self.showMaximized()
        security = self.config.security
        if self._secure_mode_active and self.config.secure_window.suppressed_shortcuts:
            security = replace(security, blocked_events=security.blocked_events | self.config.secure_window.suppressed_shortcuts)
        session = replace(self.config, student=student.strip() or "Student", exam=exam.strip() or "Practice Exam",
                          camera_index=camera_index, window_handle=int(self.winId()),
                          exam_definition=definition, secure_mode=mode, security=security)
        self._session_config = session
        self.exam_page.start(session.student, session.exam, session.events.latest_event_display_seconds, session.breaks)
        self.exam_page.set_exam_attempt(self.exam_attempt, session.security)
        self.exam_page.question_panel.set_active(False)
        self.exam_page.set_secure_mode_active(self._secure_mode_active)
        self.worker = self._worker_factory(session, self)
        self.worker.components_changed.connect(self._components)
        self.worker.session_started.connect(self._started)
        self.worker.update_ready.connect(self._updated)
        self.worker.failed.connect(self._failed)
        self.worker.notice.connect(self._notice)
        self.worker.finished.connect(self._worker_finished)
        self.state = "STARTING"
        self.setup_page.set_busy(True, "Initializing camera, AI and security monitoring…")
        self.worker.start()

    def _components(self, statuses):
        if self.state == "STARTING":
            self.setup_page.set_statuses(statuses)
        self.exam_page.set_statuses(statuses)

    def _started(self, started_at):
        if self._closing or self.state != "STARTING":
            return
        self._session_started = True
        self.session_timer.start(started_at)
        if self.exam_attempt is not None:
            self.exam_attempt.start(getattr(self.worker, "exam_started_at", None) or self._wall_clock())
            self.exam_page.question_panel.set_active(True)
        self._timer.start()
        self.state = "EXAM"
        self.pages.setCurrentWidget(self.exam_page)
        self._update_q_shortcut()
        self._publish_exam_snapshot()
        self._tick()

    def _updated(self, _signal_update=None):
        if self.worker is not None:
            update = self.worker.take_update()
            if update is not None:
                self.exam_page.apply_update(update)
                if (self.state == "EXAM" and self._secure_mode_active
                        and (update.window_focus_lost or any(event.type == EventType.WINDOW_FOCUS_LOST for event in update.events))):
                    self._recover_focus()

    def _notice(self, message):
        if self.state == "EXAM":
            self.exam_page.show_banner(message)
        else:
            self.setup_page.message.setText(message)

    def _failed(self, message):
        self._startup_error = message
        if self.state == "EXAM":
            self.finish_session()

    def _tick(self):
        elapsed = self.session_timer.elapsed
        self.exam_page.set_elapsed(elapsed)
        limit = self.exam_attempt.exam.time_limit_seconds if self.exam_attempt else None
        if limit is not None:
            self.exam_page.set_remaining(max(0, ceil(limit - elapsed)))
        if self.state == "EXAM" and limit is not None and elapsed >= limit:
            self._submit_exam(reason="expired")

    def _authorize_break(self, pin, duration):
        if self.state == "EXAM" and self.worker is not None:
            self.worker.request_authorized_break(pin, duration)

    def _end_break(self):
        if self.state == "EXAM" and self.worker is not None:
            self.worker.request_end_break()

    def _authorization_mode(self, active):
        # Allow arbitrary configured PIN text without Q closing the exam.
        self._authorization_active = bool(active)
        self._update_q_shortcut()

    def _answer_editing_changed(self, active):
        self._answer_editing = bool(active)
        self._update_q_shortcut()

    def _update_q_shortcut(self):
        self._q_shortcut.setEnabled(self.state == "EXAM" and not self._authorization_active
                                    and not self._answer_editing and not self._secure_mode_active)

    def _publish_exam_snapshot(self):
        if (self.worker is None or self.exam_attempt is None or self.exam_attempt.started_at is None
                or self.exam_attempt.submitted):
            return
        snapshot = self.exam_attempt.snapshot(
            submitted_at=max(self._wall_clock(), self.exam_attempt.started_at),
            duration_seconds=self.session_timer.elapsed,
        )
        sender = getattr(self.worker, "set_exam_result", None)
        if callable(sender):
            sender(snapshot)

    def _request_submission(self):
        if self.state == "EXAM" and self.exam_attempt is not None and not self.exam_attempt.submitted:
            self.exam_page.question_panel.show_submission_confirmation()

    def _finalize_exam(self, reason):
        if self.exam_attempt is None or self.exam_attempt.started_at is None:
            return None
        result = self.exam_attempt.submit(
            submitted_at=max(self._wall_clock(), self.exam_attempt.started_at),
            duration_seconds=self.session_timer.elapsed, reason=reason,
        )
        sender = getattr(self.worker, "set_exam_result", None)
        if callable(sender):
            sender(result)
        self.exam_page.question_panel.set_active(False)
        return result

    def _submit_exam(self, *, reason="submitted"):
        if self.state != "EXAM" or self.worker is None:
            return
        limit = self.exam_attempt.exam.time_limit_seconds if self.exam_attempt else None
        if reason == "submitted" and limit is not None and self.session_timer.elapsed >= limit:
            reason = "expired"
        self._finalize_exam(reason)
        self.finish_session()

    def _paste_attempt(self, source):
        if self.state == "EXAM" and self.worker is not None:
            receiver = getattr(self.worker, "request_paste_attempt", None)
            if callable(receiver):
                blocked = EventType.PASTE_ATTEMPT in self._session_config.security.blocked_events
                receiver(source, blocked)

    def _exit_secure_mode(self, pin):
        if (self.state != "EXAM" or not self._secure_mode_active or self._session_config is None):
            return
        if not self._session_config.breaks.accepts_pin(pin):
            self.exam_page.show_banner("Secure mode exit not authorized: invalid instructor PIN")
            return
        self._restore_window_mode()
        self.exam_page.set_secure_mode_active(False)
        self._update_q_shortcut()

    def _recover_focus(self):
        if self.state != "EXAM" or not self._secure_mode_active or self._closing:
            return False
        attempted = self.secure_guard.recover(self)
        error = self.secure_guard.last_error
        if error and error != self._focus_warning:
            self._focus_warning = error
            print(f"[SECURITY] Exam focus recovery failed: {error}", file=sys.stderr)
        return attempted

    def _queue_application_security(self, kind, source):
        if self.state == "EXAM" and self.worker is not None:
            receiver = getattr(self.worker, "request_security_attempt", None)
            if callable(receiver):
                receiver(kind, source, False)

    def _application_state_changed(self, application_state):
        if (application_state == Qt.ApplicationState.ApplicationInactive
                and self.state == "EXAM" and self._secure_mode_active and not self._closing):
            # Record deactivation first. Recovery may succeed before the native
            # monitor's next frame poll, so the queued normalized edge matters.
            self._queue_application_security(EventType.WINDOW_FOCUS_LOST, "qt_application_deactivation")
            self._recover_focus()

    def keyPressEvent(self, event):
        if self.state == "EXAM" and self._secure_mode_active and event.key() == Qt.Key.Key_Escape:
            self._queue_application_security(EventType.ESCAPE_ATTEMPT, "qt_secure_window")
            event.accept()
            return
        super().keyPressEvent(event)

    def _restore_window_mode(self):
        if self._secure_mode_active:
            self._secure_mode_active = False
            self.secure_guard.release()
            if not self._closing:
                self.showNormal()
                if self._normal_geometry is not None:
                    self.restoreGeometry(self._normal_geometry)

    def _restore_chrome_after_cleanup(self):
        if self._chrome_removed:
            self._chrome_removed = False
            self.setWindowFlags(self._normal_flags)
            if not self._closing:
                self.showNormal()
                if self._normal_geometry is not None:
                    self.restoreGeometry(self._normal_geometry)

    def finish_session(self):
        if self.worker is None or self.state not in ("STARTING", "EXAM"):
            return
        self._finalize_exam("interrupted")
        self.state = "STOPPING"
        self._timer.stop()
        self._q_shortcut.setEnabled(False)
        self.exam_page.set_stopping()
        self.setup_page.set_busy(True, "Stopping monitoring and releasing resources…")
        self.worker.request_stop()

    def _worker_finished(self):
        worker = self.worker
        if worker is None:
            return
        worker.wait()  # Finished run; synchronize thread-local cleanup too.
        self._updated()
        self.result = worker.result
        if self._session_started and self.result is not None and self.exam_attempt is not None:
            # Legacy/fake workers may not implement the snapshot handoff. Real
            # VisionWorker already persisted this result before finished fires.
            academic = self.result.exam_result
            if academic is None:
                academic = self._finalize_exam("interrupted")
                self.result = replace(self.result, exam_result=academic)
            elif not self.exam_attempt.submitted:
                self.exam_attempt.submit(submitted_at=academic.submitted_at,
                                         duration_seconds=academic.duration_seconds,
                                         reason=academic.submission_reason)
        self.worker = None
        worker.deleteLater()
        self._timer.stop()
        self.session_timer.stop()
        self._q_shortcut.setEnabled(False)
        self.exam_page.banner_timer.stop()
        self.setup_page.set_busy(False)
        self._restore_window_mode()
        self._restore_chrome_after_cleanup()
        if self._session_started and self.result is not None:
            self.state = "REPORT"
            self.report_page.set_result(self.result)
            self.report_page.set_teacher_review(False)
            self.report_page.show_completion(self.result)
            if not self._closing:
                self.pages.setCurrentWidget(self.report_page)
        else:
            self.state = "SETUP"
            self.pages.setCurrentWidget(self.setup_page)
            self.setup_page.message.setText(self._startup_error or "Session did not start. You can recheck and try again.")
        self._finish_close_if_ready()

    def new_session(self):
        if (self.worker is not None or self.discovery is not None or self.review_worker is not None
                or self.state != "REPORT"):
            return
        self._teacher_authorized = False
        self.result = None
        self.exam_attempt = None
        self._session_config = None
        self._authorization_active = self._answer_editing = False
        self.session_timer.reset()
        self._session_started = False
        self._startup_error = None
        self.exam_page.reset()
        self.report_page.reset()
        self.setup_page.reset()
        self.state = "SETUP"
        self.pages.setCurrentWidget(self.setup_page)
        self.discover()

    def open_teacher_review(self):
        if self.worker is not None or self._closing or self.state not in {"SETUP", "REPORT"}:
            return
        self._teacher_return_state = self.state
        self._teacher_authorized = False
        self._review_epoch += 1
        self.teacher_review_page.show_locked()
        self.state = "TEACHER_LOGIN"
        self.pages.setCurrentWidget(self.teacher_review_page)

    def _authorize_teacher(self, pin):
        if self.state != "TEACHER_LOGIN" or self._closing:
            return
        if not self.config.breaks.accepts_pin(pin):
            self.teacher_review_page.set_error("Invalid Teacher PIN. Access denied.")
            return
        self._teacher_authorized = True
        self.state = "TEACHER_REVIEW"
        self.teacher_review_page.show_sessions(())
        self._load_teacher_sessions()

    def _start_review_worker(self, *, session_id=None):
        if (not self._teacher_authorized or self._closing or self.worker is not None
                or self.review_worker is not None):
            return
        reader = self._review_worker_factory(self._session_repository, self, session_id=session_id)
        self.review_worker = reader
        epoch = self._review_epoch
        reader.listed.connect(lambda rows, errors, epoch=epoch: self._teacher_sessions_ready(rows, errors, epoch=epoch))
        reader.loaded.connect(lambda result, epoch=epoch: self._teacher_session_ready(result, epoch=epoch))
        reader.failed.connect(lambda message, epoch=epoch: self._teacher_load_failed(message, epoch=epoch))
        reader.finished.connect(lambda reader=reader, epoch=epoch: self._review_finished(reader, epoch=epoch))
        self.teacher_review_page.set_error("Loading local completed sessions…")
        reader.start()

    def _load_teacher_sessions(self):
        if self.state == "TEACHER_REVIEW" and self._teacher_authorized:
            if self.review_worker is not None:
                self._review_refresh_pending = True
            else:
                self._review_refresh_pending = False
                self._start_review_worker()

    def _teacher_sessions_ready(self, rows, errors, *, epoch=None):
        if (self._teacher_authorized and self.state == "TEACHER_REVIEW" and not self._closing
                and (epoch is None or epoch == self._review_epoch)):
            rows, errors = tuple(rows), tuple(errors)
            if self._current_memory_result() is not None:
                result = self.result
                academic = result.exam_result
                summary = SessionSummary(
                    self.CURRENT_MEMORY_SESSION_ID, result.student, result.exam,
                    result.started_at or (academic.started_at if academic else self._wall_clock()),
                    result.duration_seconds, academic.score if academic else None,
                    academic.max_score if academic else None, result.risk_score, result.risk_level,
                )
                rows = (summary,) + rows
                errors += ("The current completed session is retained in memory; its local save was unavailable. Review it before starting another session or closing.",)
            self.teacher_review_page.show_sessions(rows, errors)

    def _current_memory_result(self):
        return (self.result if self.result is not None
                and (self.result.persistence_error or self.result.session_directory is None) else None)

    def _open_teacher_session(self, session_id):
        if self.state != "TEACHER_REVIEW" or not self._teacher_authorized or not isinstance(session_id, str):
            return
        if session_id == self.CURRENT_MEMORY_SESSION_ID:
            result = self._current_memory_result()
            if result is not None:
                self._teacher_session_ready(result, epoch=self._review_epoch)
            return
        self._start_review_worker(session_id=session_id)

    def _teacher_session_ready(self, result, *, epoch=None):
        if (self._teacher_authorized and self.state == "TEACHER_REVIEW" and not self._closing
                and (epoch is None or epoch == self._review_epoch)):
            self.report_page.set_result(result)
            self.report_page.set_teacher_review(True)
            self.report_page.show_instructor_report()
            self.state = "TEACHER_REPORT"
            self.pages.setCurrentWidget(self.report_page)

    def _teacher_load_failed(self, message, *, epoch=None):
        if self._teacher_authorized and not self._closing and (epoch is None or epoch == self._review_epoch):
            self.teacher_review_page.set_error(f"Cannot open local report: {message}")

    def _review_finished(self, reader=None, *, epoch=None):
        reader = reader or self.review_worker
        if reader is self.review_worker:
            self.review_worker = None
        if reader is not None:
            reader.wait()
            reader.deleteLater()
        if (self._review_refresh_pending and self._teacher_authorized
                and self.state == "TEACHER_REVIEW" and not self._closing):
            self._load_teacher_sessions()
        self._finish_close_if_ready()

    def _teacher_back_to_list(self):
        if self._teacher_authorized and self.state == "TEACHER_REPORT":
            self.state = "TEACHER_REVIEW"
            self.pages.setCurrentWidget(self.teacher_review_page)

    def _leave_teacher_review(self):
        if self.state not in {"TEACHER_LOGIN", "TEACHER_REVIEW", "TEACHER_REPORT"}:
            return
        self._teacher_authorized = False
        self._review_epoch += 1
        self._review_refresh_pending = False
        if self.review_worker is not None:
            self.review_worker.requestInterruption()
        self.teacher_review_page.show_locked()
        self.report_page.set_teacher_review(False)
        if self._teacher_return_state == "REPORT" and self.result is not None:
            self.report_page.set_result(self.result)
            self.report_page.show_completion(self.result)
            self.pages.setCurrentWidget(self.report_page)
            self.state = "REPORT"
        else:
            self.report_page.reset()
            self.pages.setCurrentWidget(self.setup_page)
            self.state = "SETUP"

    def closeEvent(self, event):
        if self.state == "EXAM" and self._secure_mode_active and not self._closing:
            event.ignore()
            self.exam_page.show_banner("Teacher PIN is required to leave the secure session")
            self._recover_focus()
            return
        if self.worker is not None or self.discovery is not None or self.review_worker is not None:
            event.ignore()
            self._closing = True
            if self.worker is not None:
                self.finish_session()
                self.worker.request_stop()
            if self.discovery is not None:
                self.discovery.requestInterruption()
            if self.review_worker is not None:
                self._teacher_authorized = False
                self.review_worker.requestInterruption()
            self.setup_page.set_busy(True, "Closing after background resources are released…")
        else:
            self._timer.stop()
            self.exam_page.banner_timer.stop()
            self.setup_page.assistant.set_stopping()
            self.exam_page.assistant.set_stopping()
            event.accept()

    def _finish_close_if_ready(self):
        if self._closing and self.worker is None and self.discovery is None and self.review_worker is None:
            QTimer.singleShot(0, self.close)

    def shutdown_blocking(self):
        """Last-resort orderly cleanup if QApplication is quit externally."""
        self.setup_page.assistant.set_stopping()
        self.exam_page.assistant.set_stopping()
        if self.worker is not None:
            self._finalize_exam("interrupted")
            self.exam_page.question_panel.set_active(False)
            self.worker.request_stop()
            self.worker.wait()
        if self.discovery is not None:
            self.discovery.requestInterruption()
            self.discovery.wait()
        if self.review_worker is not None:
            self.review_worker.requestInterruption()
            self.review_worker.wait()
