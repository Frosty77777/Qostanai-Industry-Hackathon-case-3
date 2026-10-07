"""Offscreen desktop lifecycle tests. No webcam, native hooks or OS keys."""

import contextlib
from dataclasses import replace
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np
from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QFont, QFontDatabase, QImage
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from evidence import EvidenceConfig, EvidenceManager
from monitoring import EventType, PhoneDetectionConfig, ProctoringEvent, Severity
from security import SecurityConfig, WindowsSecurityBackend
from ui.exam_page import TIMELINE_LIMIT
from ui.main_window import MainWindow
from ui.session import FrameUpdate, SessionConfig, SessionResult, SessionTimer, format_duration
from ui.vision_worker import CameraDiscoveryWorker, UpdateMailbox, VisionWorker, frame_to_image
from vision.face_tracker import FaceResult, FaceStatus, HeadDirection
from vision.yolo_detector import Detection

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
if sys.platform == "win32":
    # The offscreen plugin does not enumerate Windows system fonts itself.
    QFontDatabase.addApplicationFont("C:/Windows/Fonts/segoeui.ttf")
    APP.setFont(QFont("Segoe UI", 10))
TIMESTAMP = datetime(2026, 10, 7, 18, 42, 11, tzinfo=timezone.utc)
READY = {name: ("READY", "Test preflight") for name in (
    "Camera", "AI Detection", "Face Tracking", "Security Monitor",
)}


def usable_frame():
    """Textured camera fixture: black images now intentionally signal obstruction."""
    gray = np.indices((48, 64)).sum(axis=0) % 2 * 100 + 60
    return np.repeat(gray[:, :, None], 3, axis=2).astype(np.uint8)


def event(kind=EventType.PHONE_DETECTED, delta=30):
    return ProctoringEvent(kind, TIMESTAMP, Severity.HIGH, 0, "Test event", risk_delta=delta)


def update(*, events=(), face=FaceStatus.FACE_DETECTED, score=0, level="LOW", warning=False):
    image = QImage(640, 480, QImage.Format.Format_RGB888)
    image.fill(0x203040)
    return FrameUpdate(image, FaceResult(face, 0 if face == FaceStatus.NO_FACE else 1),
                       1, False, 20.0, tuple(events), score, level, warning,
                       {**READY, "Security Monitor": ("ACTIVE", "Fake security active")})


class FakeDiscovery(QObject):
    discovered = Signal(object, object)
    finished = Signal()

    def __init__(self, config, parent):
        super().__init__(parent)
        self.stopped = False

    def start(self):
        self.discovered.emit([(0, "Camera 0")], READY)
        self.finished.emit()

    def wait(self):
        return True

    def requestInterruption(self):
        self.stopped = True


class FakeWorker(QObject):
    session_started = Signal(float)
    components_changed = Signal(object)
    update_ready = Signal(object)
    failed = Signal(str)
    notice = Signal(str)
    finished = Signal()

    def __init__(self, config, parent):
        super().__init__(parent)
        self.config = config
        self.started = False
        self.stopped = False
        self.joined = False
        self.result = None
        self.pending = None

    def start(self):
        self.started = True

    def request_stop(self):
        self.stopped = True

    def wait(self):
        self.joined = True
        return True

    def take_update(self):
        result, self.pending = self.pending, None
        return result

    def complete(self, events=(), error=None):
        self.result = SessionResult(self.config.student, self.config.exam, 65, 30, "MODERATE",
                                    tuple(events), {}, error)
        self.finished.emit()


class DesktopChecks(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.window = MainWindow(worker_factory=FakeWorker, discovery_factory=FakeDiscovery,
                                 clock=lambda: self.now, auto_discover=False)
        self.window.setup_page.set_discovery([(0, "Camera 0"), (2, "Camera 2")], READY)

    def tearDown(self):
        if self.window.worker is not None:
            worker = self.window.worker
            worker.request_stop()
            worker.complete()
        self.window.close()
        self.window.deleteLater()
        APP.processEvents()

    def start(self):
        self.window.setup_page.start_button.click()
        worker = self.window.worker
        worker.session_started.emit(self.now)
        return worker

    def test_starts_on_setup_with_title_and_ready_camera_options(self):
        self.assertEqual(self.window.windowTitle(), "AI Exam Guard")
        self.assertIs(self.window.pages.currentWidget(), self.window.setup_page)
        self.assertEqual(self.window.setup_page.camera_select.count(), 2)
        self.assertTrue(self.window.setup_page.start_button.isEnabled())

    def test_camera_unavailable_prevents_start(self):
        self.window.setup_page.set_discovery([], {**READY, "Camera": ("UNAVAILABLE", "No camera")})
        self.window.start_session("Student", "Exam", None)
        self.assertIsNone(self.window.worker)
        self.assertFalse(self.window.setup_page.start_button.isEnabled())

    def test_missing_ai_prevents_start(self):
        self.window.setup_page.set_statuses({"AI Detection": ("UNAVAILABLE", "Missing weights")})
        self.window.start_session("Student", "Exam", 0)
        self.assertIsNone(self.window.worker)

    def test_empty_names_default_and_selected_camera_passed_to_worker(self):
        self.window.setup_page.camera_select.setCurrentIndex(1)
        self.window.setup_page.start_button.click()
        self.assertEqual(self.window.worker.config.student, "Student")
        self.assertEqual(self.window.worker.config.exam, "Practice Exam")
        self.assertEqual(self.window.worker.config.camera_index, 2)
        self.assertIsInstance(self.window.worker.config.window_handle, int)

    def test_session_does_not_start_timer_before_initialization_completes(self):
        self.window.setup_page.start_button.click()
        self.now += 10
        self.assertEqual(self.window.state, "STARTING")
        self.assertEqual(self.window.session_timer.elapsed, 0)
        self.assertIs(self.window.pages.currentWidget(), self.window.setup_page)

    def test_ready_signal_transitions_to_exam_and_starts_timer(self):
        self.start()
        self.now += 12
        self.window._tick()
        self.assertEqual(self.window.state, "EXAM")
        self.assertIs(self.window.pages.currentWidget(), self.window.exam_page)
        self.assertEqual(self.window.exam_page.timer_label.text(), "00:00:12")

    def test_no_duplicate_worker_on_double_start(self):
        worker = self.start()
        self.window.start_session("Other", "Other", 2)
        self.assertIs(self.window.worker, worker)

    def test_event_timeline_and_risk_are_updated_from_worker_packet(self):
        worker = self.start()
        worker.pending = update(events=[event()], score=30, level="MODERATE")
        worker.update_ready.emit(worker.pending)
        page = self.window.exam_page
        self.assertEqual(page.timeline.count(), 1)
        self.assertIn("PHONE DETECTED", page.timeline.item(0).text())
        self.assertIn("+30 risk", page.timeline.item(0).text())
        self.assertEqual(page.risk_score.text(), "30 / 100")
        self.assertEqual(page.risk_level.text(), "MODERATE")
        self.assertEqual(page.risk_bar.value(), 30)

    def test_security_event_uses_timeline_and_major_banner(self):
        worker = self.start()
        worker.pending = update(events=[event(EventType.ALT_TAB_ATTEMPT, 15)], score=15)
        worker.update_ready.emit(worker.pending)
        self.assertEqual(self.window.exam_page.banner.text(), "WINDOW SWITCH ATTEMPT")
        self.assertIn("ALT TAB ATTEMPT", self.window.exam_page.timeline.item(0).text())

    def test_banner_timeout_does_not_clear_persistent_face_warning(self):
        worker = self.start()
        worker.pending = update(events=[event(EventType.FACE_MISSING, 15)], face=FaceStatus.NO_FACE, warning=True)
        worker.update_ready.emit(worker.pending)
        self.assertFalse(self.window.exam_page.face_warning.isHidden())
        self.window.exam_page.banner_timer.timeout.emit()
        self.assertFalse(self.window.exam_page.face_warning.isHidden())

    def test_face_return_and_unavailable_clear_warning(self):
        self.start()
        for face in (FaceStatus.FACE_DETECTED, FaceStatus.MULTIPLE_FACES, FaceStatus.UNAVAILABLE):
            self.window.exam_page.apply_update(update(face=FaceStatus.NO_FACE, warning=True))
            self.window.exam_page.apply_update(update(face=face, warning=True))
            self.assertTrue(self.window.exam_page.face_warning.isHidden())

    def test_timeline_is_bounded_but_total_count_retained(self):
        self.start()
        for _ in range(TIMELINE_LIMIT + 5):
            self.window.exam_page.add_event(event())
        self.assertEqual(self.window.exam_page.timeline.count(), TIMELINE_LIMIT)
        self.assertIn(str(TIMELINE_LIMIT + 5), self.window.exam_page.event_count.text())

    def test_finish_requests_stop_and_waits_for_worker_before_report(self):
        worker = self.start()
        self.window.exam_page.finish_button.click()
        self.assertTrue(worker.stopped)
        self.assertEqual(self.window.state, "STOPPING")
        self.assertIs(self.window.pages.currentWidget(), self.window.exam_page)
        self.assertFalse(self.window.exam_page.finish_button.isEnabled())
        worker.complete([event()])
        self.assertTrue(worker.joined)
        self.assertIsNone(self.window.worker)
        self.assertIs(self.window.pages.currentWidget(), self.window.report_page)
        self.assertEqual(self.window.report_page.values["Total Events"].text(), "1")
        self.assertEqual(self.window.result.events, (event(),))

    def test_report_placeholder_disables_unimplemented_view_report(self):
        worker = self.start()
        self.window.finish_session()
        worker.complete()
        self.assertFalse(self.window.report_page.view_button.isEnabled())
        self.assertEqual(self.window.report_page.values["Duration"].text(), "00:01:05")

    def test_new_session_resets_state_and_can_start_again(self):
        worker = self.start()
        self.window.exam_page.add_event(event())
        self.window.finish_session()
        worker.complete([event()])
        self.window.report_page.new_button.click()
        self.assertEqual(self.window.state, "SETUP")
        self.assertIsNone(self.window.result)
        self.assertEqual(self.window.session_timer.elapsed, 0)
        self.assertEqual(self.window.exam_page.timeline.count(), 0)
        self.assertEqual(self.window.exam_page.risk_score.text(), "0 / 100")
        self.assertEqual(self.window.setup_page.student_input.text(), "")
        second = self.start()
        self.assertIsNot(second, worker)
        self.assertEqual(self.window.state, "EXAM")

    def test_initialization_error_returns_to_setup_without_starting_session(self):
        self.window.setup_page.start_button.click()
        worker = self.window.worker
        worker.components_changed.emit({**READY, "Camera": ("ERROR", "Disconnected")})
        worker.failed.emit("Camera disconnected")
        worker.complete(error="Camera disconnected")
        self.assertEqual(self.window.state, "SETUP")
        self.assertIn("Camera disconnected", self.window.setup_page.message.text())
        self.assertEqual(self.window.session_timer.elapsed, 0)
        self.assertEqual(self.window.setup_page.status_labels["Camera"].text(), "ERROR")

    def test_runtime_error_preserves_partial_result_and_displays_report(self):
        worker = self.start()
        worker.failed.emit("Camera disconnected")
        worker.complete([event()], error="Camera disconnected")
        self.assertEqual(self.window.state, "REPORT")
        self.assertIn("Camera disconnected", self.window.report_page.message.text())
        self.assertEqual(len(self.window.result.events), 1)

    def test_close_is_deferred_until_worker_cleanup(self):
        worker = self.start()
        close = QCloseEvent()
        self.window.closeEvent(close)
        self.assertFalse(close.isAccepted())
        self.assertTrue(worker.stopped)
        worker.complete()
        close = QCloseEvent()
        self.window.closeEvent(close)
        self.assertTrue(close.isAccepted())

    def test_q_finishes_exam_but_is_disabled_on_setup(self):
        self.assertFalse(self.window._q_shortcut.isEnabled())
        worker = self.start()
        self.assertTrue(self.window._q_shortcut.isEnabled())
        self.window._q_shortcut.activated.emit()
        self.assertTrue(worker.stopped)

    def test_shutdown_fallback_requests_stop_and_joins(self):
        worker = self.start()
        self.window.shutdown_blocking()
        self.assertTrue(worker.stopped)
        self.assertTrue(worker.joined)

    def test_notice_shows_technical_failure_banner_without_risk_increment(self):
        worker = self.start()
        worker.notice.emit("Evidence save failed")
        self.assertEqual(self.window.exam_page.banner.text(), "Evidence save failed")
        self.assertEqual(self.window.exam_page.timeline.count(), 0)
        self.assertEqual(self.window.exam_page.risk_score.text(), "0 / 100")

    def test_laptop_height_keeps_timeline_footer_below_scroll_area(self):
        self.start()
        self.window.resize(1366, 728)
        self.window.show()
        self.window.exam_page.apply_update(update(face=FaceStatus.NO_FACE, warning=True))
        APP.processEvents()
        page = self.window.exam_page
        self.assertGreater(page.event_count.geometry().top(), page.timeline.geometry().bottom())
        self.assertLessEqual(self.window.minimumSizeHint().height(), 728)


class SessionDataChecks(unittest.TestCase):
    def test_timer_start_stop_and_reset_use_controllable_time(self):
        now = [100.0]
        timer = SessionTimer(lambda: now[0])
        self.assertEqual(timer.elapsed, 0)
        timer.start()
        now[0] += 65
        self.assertEqual(format_duration(timer.elapsed), "00:01:05")
        timer.stop()
        now[0] += 100
        self.assertEqual(timer.elapsed, 65)
        timer.reset()
        self.assertEqual(timer.elapsed, 0)

    def test_duration_supports_hours_and_never_negative(self):
        self.assertEqual(format_duration(3661), "01:01:01")
        self.assertEqual(format_duration(-5), "00:00:00")

    def test_qimage_detaches_pixels_and_preserves_bgr_colors(self):
        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        frame[:] = (10, 20, 30)
        image = frame_to_image(frame)
        frame[:] = 0
        self.assertEqual(image.pixelColor(0, 0).getRgb()[:3], (30, 20, 10))

    def test_mailbox_coalesces_frames_without_losing_events(self):
        box = UpdateMailbox()
        first, second = event(), event(EventType.COPY_ATTEMPT, 5)
        self.assertTrue(box.publish(update(events=[first], score=30)))
        self.assertFalse(box.publish(update(events=[second], score=35)))
        self.assertFalse(box.publish(update(score=35)))
        latest = box.take()
        self.assertEqual(latest.events, (first, second))
        self.assertEqual(latest.risk_score, 35)
        self.assertIsNone(box.take())
        self.assertTrue(box.publish(update()))


class ThreadedDesktopChecks(unittest.TestCase):
    def run_lifecycle(self, closing):
        capture = FakeCapture()
        tracker, security = FakeFace(), FakeSecurity()
        gate = Event()
        detector = Mock()
        detector.detect.side_effect = lambda frame: (gate.wait(3), [])[1]
        holder = []

        def factory(config, parent):
            worker = VisionWorker(config, parent,
                camera_opener=lambda cv2, index: (capture, np.zeros((48, 64, 3), dtype=np.uint8)),
                detector_factory=lambda *args: detector,
                face_factory=lambda *args: tracker,
                security_factory=lambda: security,
            )
            capture.worker = worker
            holder.append(worker)
            stop = worker.request_stop
            worker.request_stop = lambda: (stop(), gate.set())
            return worker

        window = MainWindow(worker_factory=factory, discovery_factory=FakeDiscovery, auto_discover=False)
        window.setup_page.set_discovery([(0, "Camera 0")], READY)
        loop = QEventLoop()
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(loop.quit)
        try:
            window.start_session("Student", "Exam", 0)
            worker = holder[0]

            def stop_session(_):
                self.assertEqual(window.state, "EXAM")
                window.close() if closing else window.finish_session()

            worker.session_started.connect(stop_session)
            worker.finished.connect(loop.quit)
            timeout.start(5000)
            loop.exec()
            self.assertTrue(timeout.isActive(), "Worker did not stop within five seconds")
            # deleteLater may already have run after the controller joined the
            # thread. Both destruction and a stopped live object are valid.
            self.assertTrue(not isValid(worker) or not worker.isRunning())
            self.assertIsNone(window.worker)
            self.assertTrue(capture.released)
            self.assertTrue(tracker.closed)
            self.assertTrue(security.stopped)
            if not closing:
                self.assertEqual(window.state, "REPORT")
        finally:
            gate.set()
            window.shutdown_blocking()
            window.close()
            window.deleteLater()
            timeout.stop()
            APP.processEvents()

    def test_actual_queued_qthread_signals_finish_exam_and_release_resources(self):
        self.run_lifecycle(False)

    def test_actual_queued_qthread_signals_close_app_without_running_worker(self):
        self.run_lifecycle(True)


class FakeCapture:
    def __init__(self, worker=None, frames=8, failure=False):
        self.worker, self.frames, self.failure = worker, frames, failure
        self.reads = 0
        self.released = False

    def read(self):
        self.reads += 1
        if self.reads >= self.frames:
            if self.failure:
                return False, None
            self.worker.request_stop()
        return True, usable_frame()

    def release(self):
        self.released = True


class FakeFace:
    available = True
    error = None

    def __init__(self):
        self.closed = False
        self.face = FaceResult(FaceStatus.FACE_DETECTED, 1)

    def process(self, frame):
        return self.face

    def close(self):
        self.closed = True


class FakeSecurity:
    focus_available = keyboard_available = True

    def __init__(self):
        self.stopped = False
        self.events = []

    def start(self, window):
        return True

    def poll(self):
        events, self.events = self.events, []
        return events

    def stop(self):
        self.stopped = True


class VisionWorkerChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 100.0
        self.tracker = FakeFace()
        self.security = FakeSecurity()
        self.capture = FakeCapture()
        self.detector = Mock()
        self.detections = []
        self.detector.detect.side_effect = self.detect
        self.encoder = Mock(return_value=b"fake jpeg")
        self.config = replace(SessionConfig(), evidence=EvidenceConfig(directory=Path(self.directory.name)))
        self.worker = VisionWorker(
            self.config, clock=lambda: self.now,
            camera_opener=lambda cv2, index: (self.capture, usable_frame()),
            detector_factory=lambda *args: self.detector, face_factory=lambda *args: self.tracker,
            security_factory=lambda: self.security,
            evidence_factory=lambda config: EvidenceManager(config, encoder=self.encoder),
        )
        self.capture.worker = self.worker
        self.output = io.StringIO()

    def detect(self, frame):
        self.now += 1
        return self.detections

    def run_worker(self):
        with contextlib.redirect_stdout(self.output):
            self.worker.run()

    def test_worker_runs_backend_and_cleans_every_component(self):
        self.run_worker()
        self.assertTrue(self.capture.released)
        self.assertTrue(self.tracker.closed)
        self.assertTrue(self.security.stopped)
        self.assertIsNone(self.worker.result.error)
        self.assertGreater(self.detector.detect.call_count, 1)

    def test_qthread_start_stop_and_join_with_fake_camera(self):
        # Actual QThread; only its native camera/security dependencies are faked.
        with contextlib.redirect_stdout(self.output):
            self.worker.start()
            self.assertTrue(self.worker.wait(5000))
        self.assertFalse(self.worker.isRunning())
        self.assertTrue(self.capture.released)
        self.assertTrue(self.security.stopped)

    def test_camera_failure_never_emits_session_started(self):
        started = Mock()
        self.worker.session_started.connect(started)
        self.worker._camera_opener = Mock(side_effect=RuntimeError("No camera"))
        self.run_worker()
        started.assert_not_called()
        self.assertEqual(self.worker.result.error, "No camera")
        self.assertEqual(self.worker.statuses["Camera"][0], "ERROR")

    def test_yolo_initialization_failure_releases_camera(self):
        self.worker._detector_factory = Mock(side_effect=RuntimeError("YOLO unavailable"))
        self.run_worker()
        self.assertTrue(self.capture.released)
        self.assertEqual(self.worker.statuses["AI Detection"][0], "ERROR")

    def test_camera_disappears_preserves_result_and_cleanup(self):
        self.capture.failure = True
        self.detections = [Detection(67, .92, (2, 2, 15, 20))]
        self.run_worker()
        self.assertIn("Camera disconnected", self.worker.result.error)
        self.assertEqual(len(self.worker.result.events), 1)
        self.assertEqual(self.worker.result.risk_score, 30)
        self.assertTrue(self.security.stopped)

    def test_phone_event_retains_evidence_risk_and_history(self):
        self.detections = [Detection(67, .92, (2, 2, 15, 20))]
        self.run_worker()
        self.assertEqual(len(self.worker.result.events), 1)
        emitted = self.worker.result.events[0]
        self.assertEqual(emitted.type, EventType.PHONE_DETECTED)
        self.assertEqual(emitted.risk_delta, 30)
        self.assertTrue(Path(emitted.evidence_path).is_file())
        self.encoder.assert_called_once()
        # The evidence preserves the old CV footer; the UI omits duplicate text.
        self.assertEqual(self.encoder.call_args.args[0].shape[:2], (148, 64))
        self.assertEqual(self.worker.take_update().image.height(), 48)

    def test_security_event_scores_without_webcam_evidence(self):
        self.security.events = [replace(event(EventType.ALT_TAB_ATTEMPT), risk_delta=None)]
        self.run_worker()
        self.encoder.assert_not_called()
        emitted = self.worker.result.events[0]
        self.assertEqual(emitted.risk_delta, 15)
        self.assertIsNone(emitted.evidence_path)
        self.assertEqual(self.worker.result.risk_score, 15)

    def test_weak_secondary_person_remains_visible_without_risk_or_evidence(self):
        self.detections = [Detection(0, .91, (2, 2, 15, 30)), Detection(0, .49, (20, 2, 35, 30))]
        self.run_worker()
        self.assertEqual(self.worker.result.events, ())
        self.assertEqual(self.worker.result.risk_score, 0)
        self.assertEqual(self.worker.take_update().person_count, 2)
        self.encoder.assert_not_called()

    def test_persistent_secondary_person_keeps_desktop_risk_evidence_and_history(self):
        self.detections = [Detection(0, .91, (2, 2, 15, 30)), Detection(0, .85, (20, 2, 35, 30))]
        self.run_worker()
        self.assertEqual(len(self.worker.result.events), 1)
        emitted = self.worker.result.events[0]
        self.assertEqual(emitted.type, EventType.MULTIPLE_PERSONS)
        self.assertEqual(emitted.duration, 3)
        self.assertEqual(emitted.risk_delta, 30)
        self.assertTrue(Path(emitted.evidence_path).is_file())
        self.encoder.assert_called_once()
        self.assertEqual(self.worker.take_update().person_count, 2)

    def test_chair_false_phone_has_no_desktop_risk_evidence_or_phone_state(self):
        self.detections = [Detection(0, .91, (2, 2, 30, 46)), Detection(67, .39, (40, 8, 60, 28))]
        self.run_worker()
        self.assertEqual(self.worker.result.events, ())
        self.assertEqual(self.worker.result.risk_score, 0)
        self.encoder.assert_not_called()
        packet = self.worker.take_update()
        self.assertFalse(packet.phone_detected)
        self.assertEqual(packet.person_count, 1)

    def test_small_hand_secondary_has_no_desktop_risk_or_evidence(self):
        self.detections = [Detection(0, .91, (2, 2, 30, 46)), Detection(0, .85, (40, 20, 43, 23))]
        self.run_worker()
        self.assertEqual(self.worker.result.events, ())
        self.assertEqual(self.worker.result.risk_score, 0)
        self.encoder.assert_not_called()
        self.assertEqual(self.worker.take_update().person_count, 2)

    def test_partially_visible_secondary_keeps_desktop_event_risk_and_evidence(self):
        self.detections = [Detection(0, .91, (2, 2, 30, 46)), Detection(0, .75, (55, 8, 85, 38))]
        self.run_worker()
        self.assertEqual(len(self.worker.result.events), 1)
        emitted = self.worker.result.events[0]
        self.assertEqual(emitted.type, EventType.MULTIPLE_PERSONS)
        self.assertEqual(emitted.risk_delta, 30)
        self.assertTrue(Path(emitted.evidence_path).is_file())
        self.encoder.assert_called_once()

    def test_custom_phone_floor_is_used_for_desktop_event_and_state(self):
        self.worker.config = replace(self.config, events=replace(self.config.events, phone=PhoneDetectionConfig(.7)))
        self.detections = [Detection(67, .65, (40, 8, 60, 28))]
        self.run_worker()
        self.assertEqual(self.worker.result.events, ())
        self.assertFalse(self.worker.take_update().phone_detected)
        self.encoder.assert_not_called()

    def test_face_threshold_warning_persists_without_repeated_event(self):
        self.tracker.face = FaceResult(FaceStatus.NO_FACE, 0)
        self.run_worker()
        packet = self.worker.take_update()
        self.assertTrue(packet.face_missing_warning)
        self.assertEqual([e.type for e in self.worker.result.events], [EventType.FACE_MISSING])

    def test_face_warning_clears_after_face_returns(self):
        def detect(frame):
            self.now += 1
            self.tracker.face = FaceResult(FaceStatus.NO_FACE, 0) if self.now < 106 else FaceResult(FaceStatus.FACE_DETECTED, 1)
            return []
        self.detector.detect.side_effect = detect
        self.run_worker()
        self.assertEqual(len(self.worker.result.events), 1)
        self.assertFalse(self.worker.take_update().face_missing_warning)

    def test_mediapipe_initialization_failure_is_unavailable_and_continues(self):
        self.worker._face_factory = Mock(side_effect=RuntimeError("MediaPipe unavailable"))
        self.run_worker()
        self.assertIsNone(self.worker.result.error)
        self.assertEqual(self.worker.statuses["Face Tracking"][0], "UNAVAILABLE")
        self.assertFalse(self.worker.take_update().face_missing_warning)

    def test_face_runtime_failure_continues_and_closes_tracker(self):
        self.tracker.process = Mock(side_effect=RuntimeError("Face failure"))
        self.run_worker()
        self.assertTrue(self.tracker.closed)
        self.assertIsNone(self.worker.result.error)
        self.assertFalse(self.worker.take_update().face_missing_warning)

    def test_security_initialization_failure_continues_cv_and_cleans_listener(self):
        self.security.start = Mock(side_effect=RuntimeError("Hook unavailable"))
        self.run_worker()
        self.assertIsNone(self.worker.result.error)
        self.assertTrue(self.capture.released)
        self.assertTrue(self.security.stopped)
        self.assertEqual(self.worker.statuses["Security Monitor"][0], "UNAVAILABLE")

    def test_face_processing_and_cleanup_failure_still_allow_cv_to_continue(self):
        self.tracker.process = Mock(side_effect=RuntimeError("Face failure"))
        self.tracker.close = Mock(side_effect=RuntimeError("Face cleanup error"))
        self.run_worker()
        self.assertIsNone(self.worker.result.error)
        self.assertTrue(self.capture.released)
        self.assertFalse(self.worker.take_update().face_missing_warning)

    def test_security_runtime_and_cleanup_failure_still_allow_cv_to_continue(self):
        self.security.poll = Mock(side_effect=RuntimeError("Security poll failure"))
        self.security.stop = Mock(side_effect=RuntimeError("Security cleanup failure"))
        self.run_worker()
        self.assertIsNone(self.worker.result.error)
        self.assertTrue(self.capture.released)
        self.assertEqual(self.worker.statuses["Security Monitor"][0], "UNAVAILABLE")

    def test_yolo_runtime_failure_cleans_up_all_resources(self):
        self.detector.detect.side_effect = RuntimeError("Inference failed")
        self.run_worker()
        self.assertIn("AI detection failed", self.worker.result.error)
        self.assertTrue(self.capture.released)
        self.assertTrue(self.tracker.closed)
        self.assertTrue(self.security.stopped)

    def test_evidence_failure_retains_event_and_running_session(self):
        self.detections = [Detection(67, .92, (2, 2, 15, 20))]
        self.encoder.side_effect = OSError("Disk full")
        with contextlib.redirect_stderr(self.output):
            self.run_worker()
        self.assertIsNone(self.worker.result.error)
        self.assertEqual(len(self.worker.result.events), 1)
        self.assertIsNone(self.worker.result.events[0].evidence_path)

    def test_cleanup_failure_does_not_skip_camera_release(self):
        self.security.stop = Mock(side_effect=RuntimeError("Hook cleanup error"))
        self.run_worker()
        self.assertTrue(self.tracker.closed)
        self.assertTrue(self.capture.released)
        self.assertIn("Cleanup failed", self.worker.result.error)

    def test_stop_during_initialization_skips_further_components(self):
        def camera(cv2, index):
            self.worker.request_stop()
            return self.capture, np.zeros((48, 64, 3), dtype=np.uint8)
        self.worker._camera_opener = camera
        self.worker._detector_factory = Mock()
        self.run_worker()
        self.worker._detector_factory.assert_not_called()
        self.assertTrue(self.capture.released)

    def test_camera_discovery_releases_probes_and_skips_unusable_cameras(self):
        probe = Mock()
        worker = CameraDiscoveryWorker(self.config, indices=[0, 2])
        result = []
        worker.discovered.connect(lambda cameras, statuses: result.append((cameras, statuses)))
        with patch("ui.vision_worker.open_webcam", side_effect=[(probe, np.zeros((2, 2, 3))), RuntimeError("Unavailable")]):
            worker.run()
        probe.release.assert_called_once()
        self.assertEqual(result[0][0], [(0, "Camera 0 · Default")])
        self.assertEqual(result[0][1]["Camera"][0], "READY")

    @unittest.skipUnless(sys.platform == "win32", "Win32 ctypes adapter")
    def test_qt_native_window_handle_bypasses_title_lookup(self):
        user32, kernel32 = Mock(), Mock()
        user32.IsWindow.return_value = True
        user32.GetAncestor.return_value = 123
        user32.GetWindowThreadProcessId.side_effect = lambda hwnd, pid: setattr(pid._obj, "value", os.getpid())
        backend = WindowsSecurityBackend(SecurityConfig(), window_handle=123)
        with patch("security.security_monitor.ctypes.WinDLL", side_effect=[user32, kernel32]):
            backend.initialize("AI Exam Guard")
        user32.FindWindowW.assert_not_called()
        self.assertEqual(backend._hwnd, 123)


if __name__ == "__main__":
    unittest.main()
