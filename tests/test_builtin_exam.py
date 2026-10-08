"""Full desktop/worker exam flow with fake clocks, cameras and security inputs."""

import contextlib
from dataclasses import replace
from datetime import timedelta
import io
import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import Mock

from test_desktop import (APP, READY, TIMESTAMP, FakeCapture, FakeDiscovery,
                          FakeFace, FakeSecurity, FakeWorker, event, update, usable_frame)

from PySide6.QtCore import QEventLoop, QMimeData, QTimer
from shiboken6 import isValid

from evidence import EvidenceManager
from exams import ExamAttempt, ExamDefinition, Question, QuestionType
from monitoring import EventEngine, EventType, RiskEngine
from security import SecurityConfig, SecurityMonitor
from security.security_monitor import KeyInput
from session_storage import SessionStorageConfig
from ui.main_window import MainWindow
from ui.session import SessionConfig, SessionResult
from ui.vision_worker import VisionWorker
from vision.yolo_detector import Detection


def definition(limit=None):
    return ExamDefinition("integration", "Integration Exam", (
        Question("single", QuestionType.SINGLE_CHOICE, "Choose B", ("A", "B"), "B"),
        Question("multi", QuestionType.MULTIPLE_CHOICE, "Choose A and C", ("A", "B", "C"), ("A", "C")),
        Question("text", QuestionType.TEXT, "Explain your answer"),
    ), time_limit_seconds=limit)


class AcademicWorker(FakeWorker):
    exam_started_at = TIMESTAMP

    def __init__(self, config, parent):
        super().__init__(config, parent)
        self.snapshots = []
        self.snapshot_at_stop = None
        self.pastes = []
        self.break_requests = []

    def set_exam_result(self, result):
        self.snapshots.append(result)

    def request_stop(self):
        self.snapshot_at_stop = self.snapshots[-1] if self.snapshots else None
        super().request_stop()

    def request_paste_attempt(self, source, blocked):
        self.pastes.append((source, blocked))

    def request_authorized_break(self, pin, duration):
        self.break_requests.append((pin, duration))

    def complete(self, events=(), error=None):
        self.result = SessionResult(self.config.student, self.config.exam, 65, 30, "MODERATE",
                                    tuple(events), {}, error, exam_result=self.snapshots[-1] if self.snapshots else None)
        self.finished.emit()


class BuiltinExamDesktopChecks(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.windows = []
        self.window = self.create_window()

    def create_window(self, **changes):
        changes.setdefault("exam_definition", definition())
        config = replace(SessionConfig(), **changes)
        window = MainWindow(config, worker_factory=AcademicWorker,
                            discovery_factory=FakeDiscovery, auto_discover=False,
                            clock=lambda: self.now,
                            wall_clock=lambda: TIMESTAMP + timedelta(seconds=self.now - 100))
        window.setup_page.set_discovery([(0, "Fake camera")], READY)
        self.windows.append(window)
        return window

    def tearDown(self):
        for window in self.windows:
            if window.worker is not None:
                window.finish_session()
                window.worker.complete()
            window.close()
            window.deleteLater()
        APP.processEvents()

    def start(self, window=None):
        window = window or self.window
        window.start_session("Student", "Exam session", 0)
        worker = window.worker
        worker.session_started.emit(self.now)
        return worker

    def test_attempt_starts_only_after_monitoring_is_ready(self):
        self.window.start_session("Student", "Exam session", 0)
        self.assertIsNone(self.window.exam_attempt.started_at)
        self.assertFalse(self.window.exam_page.question_panel.submit_button.isEnabled())
        self.window.worker.session_started.emit(self.now)
        self.assertEqual(self.window.exam_attempt.started_at, TIMESTAMP)
        self.assertTrue(self.window.exam_page.question_panel.submit_button.isEnabled())

    def test_visible_exam_selection_wins_after_configured_exam(self):
        selected = ExamDefinition("new", "Another Exam", (
            Question("q", QuestionType.TEXT, "Explain"),
        ))
        self.assertEqual(self.window.setup_page.selected_exam.id, "integration")
        self.window.setup_page.set_exam_definition(selected)
        worker = self.start()
        self.assertEqual(self.window.exam_attempt.exam.id, "new")
        self.assertEqual(worker.config.exam_definition.id, "new")

    def test_submit_click_requires_explicit_confirmation(self):
        worker = self.start()
        panel = self.window.exam_page.question_panel
        panel.submit_button.click()
        self.assertFalse(worker.stopped)
        self.assertFalse(self.window.exam_attempt.submitted)
        self.assertEqual(panel.confirmation_message.text(),
                         "Submit exam? You will not be able to change your answers.")
        panel.cancel_button.click()
        self.assertFalse(worker.stopped)
        self.assertTrue(panel.submit_button.isEnabled())

    def test_confirmation_freezes_answers_before_requesting_stop(self):
        worker = self.start()
        panel = self.window.exam_page.question_panel
        panel._option_widgets[1].click()
        self.now += 12
        panel.submit_button.click()
        panel.confirm_button.click()
        self.assertEqual(self.window.state, "STOPPING")
        self.assertTrue(worker.stopped)
        self.assertEqual(worker.snapshot_at_stop.answers["single"], "B")
        self.assertEqual(worker.snapshot_at_stop.submission_reason, "submitted")
        self.assertEqual(worker.snapshot_at_stop.duration_seconds, 12)
        self.assertEqual(worker.snapshot_at_stop.score, 1)
        self.assertFalse(panel.submit_button.isEnabled())
        with self.assertRaises(ValueError):
            self.window.exam_attempt.set_answer("single", "A")

    def test_answer_changes_publish_independent_immutable_checkpoints(self):
        worker = self.start()
        panel = self.window.exam_page.question_panel
        panel._option_widgets[0].click()
        previous = worker.snapshots[-1]
        panel._option_widgets[1].click()
        self.assertEqual(previous.answers["single"], "A")
        self.assertEqual(worker.snapshots[-1].answers["single"], "B")
        self.assertFalse(self.window.exam_attempt.submitted)

    def test_text_answer_survives_navigation_and_is_pending_review(self):
        worker = self.start()
        panel = self.window.exam_page.question_panel
        panel._go_to(2)
        panel.text_answer.setPlainText("My answer\nsecond line")
        panel.previous_button.click()
        panel.next_button.click()
        self.assertEqual(panel.text_answer.toPlainText(), "My answer\nsecond line")
        panel.submit_button.click()
        panel.confirm_button.click()
        self.assertEqual(worker.snapshot_at_stop.text_pending_count, 1)
        self.assertEqual(worker.snapshot_at_stop.max_score, 2)

    def test_double_submission_does_not_replace_or_rescore_result(self):
        worker = self.start()
        self.window._submit_exam()
        result = worker.snapshot_at_stop
        self.now += 10
        self.window._submit_exam()
        self.assertIs(self.window.exam_attempt.result, result)
        self.assertIs(worker.snapshot_at_stop, result)

    def test_complete_report_waits_for_worker_cleanup_and_keeps_risk_separate(self):
        worker = self.start()
        self.window.exam_page.question_panel._option_widgets[1].click()
        self.window._submit_exam()
        self.assertEqual(self.window.state, "STOPPING")
        worker.complete(events=(event(),))
        self.assertTrue(worker.joined)
        self.assertEqual(self.window.state, "REPORT")
        self.assertEqual(self.window.result.exam_result.score, 1)
        self.assertEqual(self.window.result.risk_score, 30)
        self.assertEqual(self.window.report_page.answers_model.rowCount(), 3)
        self.assertEqual(len(self.window.result.events), 1)

    def test_remaining_timer_and_exact_expiry_auto_submit_once(self):
        window = self.create_window(exam_definition=definition(10))
        worker = self.start(window)
        self.now += 9.2
        window._tick()
        self.assertEqual(window.exam_page.timer_heading.text(), "TIME REMAINING")
        self.assertEqual(window.exam_page.timer_label.text(), "00:00:01")
        self.assertFalse(worker.stopped)
        self.now = 110
        window._tick()
        self.assertTrue(worker.stopped)
        self.assertEqual(worker.snapshot_at_stop.submission_reason, "expired")
        self.assertEqual(worker.snapshot_at_stop.duration_seconds, 10)
        count = len(worker.snapshots)
        window._tick()
        self.assertEqual(len(worker.snapshots), count)

    def test_untimed_exam_displays_elapsed_time(self):
        self.start()
        self.now += 62
        self.window._tick()
        self.assertEqual(self.window.exam_page.timer_label.text(), "00:01:02")
        self.assertEqual(self.window.exam_page.timer_heading.text(), "SESSION TIME")

    def test_confirmation_after_deadline_records_expiry_before_next_timer_tick(self):
        window = self.create_window(exam_definition=definition(10))
        worker = self.start(window)
        panel = window.exam_page.question_panel
        panel.submit_button.click()
        self.now = 110
        panel.confirm_button.click()
        self.assertEqual(worker.snapshot_at_stop.submission_reason, "expired")
        self.assertTrue(worker.stopped)

    def test_break_keeps_exam_timer_running_and_monitoring_commands_independent(self):
        worker = self.start()
        self.window._authorize_break("1234", 60)
        self.assertEqual(worker.break_requests, [("1234", 60)])
        self.now += 20
        self.window._tick()
        self.assertEqual(self.window.session_timer.elapsed, 20)
        self.assertFalse(self.window.exam_attempt.submitted)

    def test_monitoring_packet_updates_during_navigation_without_changing_answers(self):
        worker = self.start()
        panel = self.window.exam_page.question_panel
        panel._option_widgets[1].click()
        panel.next_button.click()
        worker.pending = update(events=(event(),), score=30, level="MODERATE")
        self.window._updated()
        self.assertEqual(self.window.exam_page.risk_score.text(), "30 / 100")
        self.assertEqual(self.window.exam_page.timeline.count(), 1)
        self.assertEqual(self.window.exam_attempt.answer_for("single"), "B")
        self.assertEqual(self.window.exam_attempt.current_index, 1)

    def test_legacy_q_exit_preserves_partial_answers_as_interrupted(self):
        worker = self.start()
        self.window.exam_page.question_panel._option_widgets[1].click()
        self.window._q_shortcut.activated.emit()
        self.assertTrue(worker.stopped)
        self.assertEqual(worker.snapshot_at_stop.submission_reason, "interrupted")
        self.assertEqual(worker.snapshot_at_stop.answers["single"], "B")

    def test_q_shortcut_is_disabled_while_typing_or_authorizing(self):
        self.start()
        panel = self.window.exam_page.question_panel
        panel.editing_changed.emit(True)
        self.assertFalse(self.window._q_shortcut.isEnabled())
        self.window._authorization_mode(True)
        panel.editing_changed.emit(False)
        self.assertFalse(self.window._q_shortcut.isEnabled())
        self.window._authorization_mode(False)
        self.assertTrue(self.window._q_shortcut.isEnabled())

    def test_context_paste_uses_existing_security_configuration(self):
        window = self.create_window(security=SecurityConfig(blocked_events={EventType.PASTE_ATTEMPT}))
        worker = self.start(window)
        panel = window.exam_page.question_panel
        panel._go_to(2)
        mime = QMimeData()
        mime.setText("clipboard answer")
        panel.text_answer.insertFromMimeData(mime)
        self.assertEqual(panel.text_answer.toPlainText(), "")
        self.assertEqual(worker.pastes, [("context_or_programmatic", True)])

    def test_detect_only_paste_saves_text_and_queues_security_attempt(self):
        worker = self.start()
        panel = self.window.exam_page.question_panel
        panel._go_to(2)
        mime = QMimeData()
        mime.setText("clipboard answer")
        panel.text_answer.insertFromMimeData(mime)
        self.assertEqual(self.window.exam_attempt.answer_for("text"), "clipboard answer")
        self.assertEqual(worker.pastes, [("context_or_programmatic", False)])

    def test_secure_mode_has_pin_exit_without_stopping_monitoring(self):
        window = self.create_window(secure_mode="FULLSCREEN")
        worker = self.start(window)
        self.assertTrue(window.isFullScreen())
        self.assertFalse(window._q_shortcut.isEnabled())
        window._exit_secure_mode("wrong")
        self.assertTrue(window.isFullScreen())
        window._exit_secure_mode("1234")
        self.assertFalse(window.isFullScreen())
        self.assertEqual(window.state, "EXAM")
        self.assertFalse(worker.stopped)

    def test_close_and_last_resort_shutdown_preserve_answers_and_join(self):
        worker = self.start()
        self.window.exam_page.question_panel._option_widgets[1].click()
        self.window.shutdown_blocking()
        self.assertTrue(worker.joined)
        self.assertEqual(worker.snapshot_at_stop.answers["single"], "B")
        self.assertEqual(worker.snapshot_at_stop.submission_reason, "interrupted")

    def test_fatal_monitoring_failure_preserves_partial_attempt(self):
        worker = self.start()
        self.window.exam_page.question_panel._option_widgets[1].click()
        worker.failed.emit("Camera disconnected")
        self.assertTrue(worker.stopped)
        self.assertEqual(worker.snapshot_at_stop.submission_reason, "interrupted")
        worker.complete(error="Camera disconnected")
        self.assertEqual(self.window.result.exam_result.answers["single"], "B")
        self.assertEqual(self.window.result.error, "Camera disconnected")

    def test_new_session_resets_attempt_report_and_old_submission(self):
        worker = self.start()
        self.window.exam_page.question_panel._option_widgets[1].click()
        self.window._submit_exam()
        previous = worker.snapshot_at_stop
        worker.complete()
        self.window.new_session()
        self.assertIsNone(self.window.exam_attempt)
        self.assertEqual(self.window.report_page.answers_model.rowCount(), 0)
        self.start()
        self.assertEqual(self.window.exam_attempt.answered_count, 0)
        self.assertEqual(previous.answers["single"], "B")


class BuiltinExamWorkerChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 100.0
        self.capture, self.tracker, self.security = FakeCapture(), FakeFace(), FakeSecurity()
        self.encoder = Mock(return_value=b"JPEG fixture")
        self.detector = Mock()
        self.detector.detect.side_effect = self.detect
        self.action = None
        self.config = replace(SessionConfig(), exam_definition=definition(),
                              storage=SessionStorageConfig(Path(self.directory.name)))
        self.worker = VisionWorker(
            self.config, clock=lambda: self.now,
            wall_clock=lambda: TIMESTAMP + timedelta(seconds=self.now - 100),
            camera_opener=lambda cv2, index: (self.capture, usable_frame()),
            detector_factory=lambda *args: self.detector, face_factory=lambda *args: self.tracker,
            security_factory=lambda: self.security,
            evidence_factory=lambda config: EvidenceManager(config, encoder=self.encoder),
        )
        self.capture.worker = self.worker

    def detect(self, frame):
        self.now += 1
        if self.action is not None:
            self.action()
        return [Detection(67, .92, (2, 2, 15, 20))]

    def run_worker(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.worker.run()
        return json.loads((self.worker.result.session_directory / "session.json").read_text(encoding="utf-8"))

    def test_early_failure_preserves_even_unanswered_exam_snapshot(self):
        self.capture.frames = 1
        self.capture.failure = True
        saved = self.run_worker()
        self.assertEqual(saved["exam_result"]["exam_id"], "integration")
        self.assertEqual(saved["exam_result"]["unanswered_count"], 3)
        self.assertEqual(saved["exam_result"]["submission_reason"], "interrupted")
        self.assertEqual(saved["exam_result"]["duration_seconds"], 1)
        self.assertTrue(self.capture.released and self.tracker.closed and self.security.stopped)

    def test_final_submission_saved_with_answers_risk_history_and_evidence(self):
        attempt = ExamAttempt(self.config.exam_definition)
        attempt.start(TIMESTAMP)
        attempt.set_answer("single", "B")
        self.worker.set_exam_result(attempt.submit(submitted_at=TIMESTAMP + timedelta(seconds=4), duration_seconds=4))
        saved = self.run_worker()
        self.assertEqual(saved["exam_result"]["answers"]["single"], "B")
        self.assertEqual(saved["exam_result"]["score"], 1)
        self.assertEqual(saved["exam_result"]["duration_seconds"], 4)
        self.assertEqual(saved["risk_score"], 30)
        self.assertEqual(len(self.worker.result.events), 1)
        self.assertTrue(Path(self.worker.result.events[0].evidence_path).is_file())
        self.encoder.assert_called_once()

    def test_partial_checkpoint_uses_actual_monitoring_end_time(self):
        attempt = ExamAttempt(self.config.exam_definition)
        attempt.start(TIMESTAMP)
        attempt.set_answer("text", "Still writing")
        self.worker.set_exam_result(attempt.snapshot(submitted_at=TIMESTAMP, duration_seconds=0))
        saved = self.run_worker()
        self.assertEqual(saved["exam_result"]["answers"]["text"], "Still writing")
        self.assertEqual(saved["exam_result"]["submitted_at"], saved["ended_at"])
        self.assertEqual(saved["exam_result"]["duration_seconds"], saved["duration_seconds"])
        self.assertFalse(attempt.submitted)

    def test_local_paste_goes_through_history_and_risk_without_evidence(self):
        self.detector.detect.side_effect = lambda frame: []
        self.worker.request_paste_attempt("context_or_programmatic", True)
        saved = self.run_worker()
        self.assertEqual(saved["risk_score"], 5)
        self.assertEqual(len(self.worker.result.events), 1)
        emitted = self.worker.result.events[0]
        self.assertEqual(emitted.type, EventType.PASTE_ATTEMPT)
        self.assertTrue(emitted.metadata["blocked"])
        self.assertIsNone(emitted.evidence_path)
        self.encoder.assert_not_called()

    def test_paste_detection_survives_failed_global_security_initialization(self):
        self.worker._security_factory = Mock(side_effect=RuntimeError("Hook failed"))
        self.detector.detect.side_effect = lambda frame: []
        self.worker.request_paste_attempt("ctrl_v", False)
        self.run_worker()
        self.assertEqual(self.worker.result.events[0].type, EventType.PASTE_ATTEMPT)
        self.assertEqual(self.worker.statuses["Security Monitor"][0], "UNAVAILABLE")
        self.assertTrue(self.capture.released)

    def test_paste_burst_is_bounded_and_shares_event_cooldown(self):
        self.detector.detect.side_effect = lambda frame: []
        accepted = [self.worker.request_paste_attempt("ctrl_v", False) for _ in range(50)]
        self.assertEqual(sum(accepted), 8)
        self.run_worker()
        self.assertEqual(len(self.worker.result.events), 1)
        self.assertFalse(self.worker.request_paste_attempt("ctrl_v", False))

    def test_paste_immediately_followed_by_submission_is_saved_before_cleanup(self):
        def immediately_submit(_started_at):
            self.worker.request_paste_attempt("ctrl_v", False)
            self.worker.request_stop()
        self.worker.session_started.connect(immediately_submit)
        saved = self.run_worker()
        self.detector.detect.assert_not_called()
        self.assertEqual(saved["risk_score"], 5)
        self.assertEqual(len(self.worker.result.events), 1)
        self.assertEqual(self.worker.result.events[0].type, EventType.PASTE_ATTEMPT)
        self.encoder.assert_not_called()
        self.assertTrue(self.capture.released and self.security.stopped)

    def test_snapshot_handoff_rejects_mutable_data(self):
        with self.assertRaises(TypeError):
            self.worker.set_exam_result({"answers": {}})


class ApplicationPasteCooldownChecks(unittest.TestCase):
    def test_local_paste_shares_native_hook_cooldown_and_recurs_at_boundary(self):
        class Backend:
            keyboard_error = None
            def initialize(self, name): pass
            def start_keyboard(self): return True
            def is_focused(self): return True
            def drain_keys(self):
                result, self.keys = self.keys, []
                return result
            def stop(self): pass
        backend = Backend()
        backend.keys = [KeyInput("v", 100, ctrl=True)]
        monitor = SecurityMonitor(backend_factory=lambda config: backend, clock=lambda: 100,
                                  wall_clock=lambda: TIMESTAMP)
        monitor.start("Exam")
        native = monitor.poll()
        self.assertEqual(len(native), 1)
        self.assertIsNone(monitor.record_application_paste(source="ctrl_v", blocked=False, now=100))
        self.assertIsNone(monitor.record_application_paste(source="context", blocked=False, now=101.9))
        again = monitor.record_application_paste(source="context", blocked=False, now=102)
        engine, risk = EventEngine(), RiskEngine()
        recorded = [risk.process(item) for item in engine.record_security_events(native + [again])]
        self.assertEqual([item.risk_delta for item in recorded], [5, 3])
        self.assertEqual(len(engine.history), 2)
        monitor.stop()


class ThreadedBuiltinExamChecks(unittest.TestCase):
    def test_real_qthread_submission_saves_exam_and_security_before_report(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        capture, tracker, security = FakeCapture(), FakeFace(), FakeSecurity()
        gate = Event()
        detector = Mock()
        detector.detect.side_effect = lambda frame: (gate.wait(3), [])[1]
        security.events = [replace(event(EventType.ALT_TAB_ATTEMPT), risk_delta=None)]
        holder = []

        def factory(config, parent):
            config = replace(config, storage=SessionStorageConfig(Path(directory)))
            worker = VisionWorker(
                config, parent, clock=lambda: 100, wall_clock=lambda: TIMESTAMP,
                camera_opener=lambda cv2, index: (capture, usable_frame()),
                detector_factory=lambda *args: detector,
                face_factory=lambda *args: tracker, security_factory=lambda: security,
            )
            capture.worker = worker
            holder.append(worker)
            stop = worker.request_stop
            worker.request_stop = lambda: (stop(), gate.set())
            return worker

        window = MainWindow(replace(SessionConfig(), exam_definition=definition()),
                            worker_factory=factory, discovery_factory=FakeDiscovery,
                            clock=lambda: 100, wall_clock=lambda: TIMESTAMP, auto_discover=False)
        window.setup_page.set_discovery([(0, "Fake camera")], READY)
        loop, watchdog = QEventLoop(), QTimer()
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(loop.quit)
        failures = []

        def submit(_):
            try:
                self.assertEqual(window.state, "EXAM")
                panel = window.exam_page.question_panel
                panel._option_widgets[1].click()
                panel._go_to(2)
                mime = QMimeData()
                mime.setText("Written answer")
                panel.text_answer.insertFromMimeData(mime)
                panel.submit_button.click()
                panel.confirm_button.click()
                self.assertEqual(window.state, "STOPPING")
            except BaseException as exc:
                failures.append(exc)
                window.finish_session()

        try:
            window.start_session("Student", "Threaded exam", 0)
            worker = holder[0]
            worker.session_started.connect(submit)
            worker.finished.connect(loop.quit)
            watchdog.start(5000)
            loop.exec()
            if failures:
                raise failures[0]
            self.assertTrue(watchdog.isActive(), "Submission cleanup exceeded watchdog")
            self.assertTrue(not isValid(worker) or not worker.isRunning())
            self.assertIsNone(window.worker)
            self.assertEqual(window.state, "REPORT")
            self.assertTrue(capture.released and tracker.closed and security.stopped)
            saved = json.loads((window.result.session_directory / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["exam_result"]["submission_reason"], "submitted")
            self.assertEqual(saved["exam_result"]["answers"]["single"], "B")
            self.assertEqual(saved["exam_result"]["answers"]["text"], "Written answer")
            self.assertEqual(saved["exam_result"]["score"], 1)
            self.assertEqual(saved["risk_score"], 20)
            self.assertEqual({item.type for item in window.result.events},
                             {EventType.ALT_TAB_ATTEMPT, EventType.PASTE_ATTEMPT})
            self.assertTrue(all(item.evidence_path is None for item in window.result.events))
        finally:
            gate.set()
            window.shutdown_blocking()
            window.close()
            window.deleteLater()
            watchdog.stop()
            APP.processEvents()


if __name__ == "__main__":
    unittest.main()
