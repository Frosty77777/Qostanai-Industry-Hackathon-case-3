"""Desktop session controller. Widgets never call inference or read a camera."""

from dataclasses import replace

from PySide6.QtCore import QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QApplication, QMainWindow, QStackedWidget

from .exam_page import ExamPage
from .report_page import ReportPage
from .session import SessionConfig, SessionTimer
from .setup_page import SetupPage
from .theme import STYLESHEET
from .vision_worker import CameraDiscoveryWorker, VisionWorker


class MainWindow(QMainWindow):
    def __init__(self, config=None, *, worker_factory=VisionWorker,
                 discovery_factory=CameraDiscoveryWorker, clock=None, auto_discover=True):
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
        self.setup_page = SetupPage()
        self.exam_page = ExamPage()
        self.report_page = ReportPage()
        for page in (self.setup_page, self.exam_page, self.report_page):
            self.pages.addWidget(page)
        self.setCentralWidget(self.pages)
        self.setup_page.start_requested.connect(self.start_session)
        self.setup_page.refresh_requested.connect(self.discover)
        self.exam_page.finish_requested.connect(self.finish_session)
        self.report_page.new_session_requested.connect(self.new_session)
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._tick)
        self._q_shortcut = QShortcut(QKeySequence("Q"), self)
        self._q_shortcut.setEnabled(False)
        self._q_shortcut.activated.connect(self.finish_session)
        self.exam_page.break_requested.connect(self._authorize_break)
        self.exam_page.end_break_requested.connect(self._end_break)
        self.exam_page.authorization_mode_changed.connect(self._authorization_mode)
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
        if self.worker is not None or self.discovery is not None or self._closing or self.state != "SETUP":
            return
        if camera_index is None or not self.setup_page._camera_ready or not self.setup_page._ai_ready:
            self.setup_page.message.setText("Camera and AI detection must be ready before starting.")
            return
        self.result = None
        self._startup_error = None
        self._session_started = False
        self.session_timer.reset()
        session = replace(self.config, student=student.strip() or "Student", exam=exam.strip() or "Practice Exam",
                          camera_index=camera_index, window_handle=int(self.winId()))
        self.exam_page.start(session.student, session.exam, session.events.latest_event_display_seconds, session.breaks)
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
        self._timer.start()
        self.state = "EXAM"
        self.pages.setCurrentWidget(self.exam_page)
        self._q_shortcut.setEnabled(True)

    def _updated(self, _signal_update=None):
        if self.worker is not None:
            update = self.worker.take_update()
            if update is not None:
                self.exam_page.apply_update(update)

    def _notice(self, message):
        if self.state == "EXAM":
            self.exam_page.show_banner(message)
        else:
            self.setup_page.message.setText(message)

    def _failed(self, message):
        self._startup_error = message
        if self.state == "EXAM":
            self.exam_page.set_stopping()

    def _tick(self):
        self.exam_page.set_elapsed(self.session_timer.elapsed)

    def _authorize_break(self, pin, duration):
        if self.state == "EXAM" and self.worker is not None:
            self.worker.request_authorized_break(pin, duration)

    def _end_break(self):
        if self.state == "EXAM" and self.worker is not None:
            self.worker.request_end_break()

    def _authorization_mode(self, active):
        # Allow arbitrary configured PIN text without Q closing the exam.
        self._q_shortcut.setEnabled(self.state == "EXAM" and not active)

    def finish_session(self):
        if self.worker is None or self.state not in ("STARTING", "EXAM"):
            return
        self.state = "STOPPING"
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
        self.worker = None
        worker.deleteLater()
        self._timer.stop()
        self.session_timer.stop()
        self._q_shortcut.setEnabled(False)
        self.exam_page.banner_timer.stop()
        self.setup_page.set_busy(False)
        if self._session_started and self.result is not None:
            self.state = "REPORT"
            self.report_page.set_result(self.result)
            if not self._closing:
                self.pages.setCurrentWidget(self.report_page)
        else:
            self.state = "SETUP"
            self.pages.setCurrentWidget(self.setup_page)
            self.setup_page.message.setText(self._startup_error or "Session did not start. You can recheck and try again.")
        self._finish_close_if_ready()

    def new_session(self):
        if self.worker is not None or self.discovery is not None or self.state != "REPORT":
            return
        self.result = None
        self.session_timer.reset()
        self._session_started = False
        self._startup_error = None
        self.exam_page.reset()
        self.report_page.reset()
        self.setup_page.reset()
        self.state = "SETUP"
        self.pages.setCurrentWidget(self.setup_page)
        self.discover()

    def closeEvent(self, event):
        if self.worker is not None or self.discovery is not None:
            event.ignore()
            self._closing = True
            if self.worker is not None:
                self.finish_session()
                self.worker.request_stop()
            if self.discovery is not None:
                self.discovery.requestInterruption()
            self.setup_page.set_busy(True, "Closing after background resources are released…")
        else:
            self._timer.stop()
            self.exam_page.banner_timer.stop()
            event.accept()

    def _finish_close_if_ready(self):
        if self._closing and self.worker is None and self.discovery is None:
            QTimer.singleShot(0, self.close)

    def shutdown_blocking(self):
        """Last-resort orderly cleanup if QApplication is quit externally."""
        if self.worker is not None:
            self.worker.request_stop()
            self.worker.wait()
        if self.discovery is not None:
            self.discovery.requestInterruption()
            self.discovery.wait()
