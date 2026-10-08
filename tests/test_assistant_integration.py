"""Assistant UX through real pages/controller; no cameras, hooks, or real sleeps."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_desktop as desktop
from test_product_hardening import FakeReader, FakeRepository
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QLabel

from exams import ExamAttempt, load_demo_exam
from monitoring import EventType
from security import SecurityConfig
from ui.assistant_model import AssistantPreferencesStore, AssistantState
from ui.assistant_widget import AssistantDock
from ui.exam_page import ExamPage
from ui.main_window import MainWindow
from ui.session import SessionConfig
from ui.setup_page import SetupPage
from ui.theme import STYLESHEET
from vision.face_tracker import FaceStatus


def visible_text(page):
    return "\n".join(label.text() for label in page.findChildren(QLabel) if label.isVisible())


class AssistantPageChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = AssistantPreferencesStore(Path(self.directory.name) / "assistant.json")
        self.now = 100.0
        self.pages = []

    def make_page(self, kind=ExamPage):
        page = kind(assistant_preferences=self.store, assistant_clock=lambda: self.now)
        page.setStyleSheet(STYLESHEET)
        page.resize(1366, 768)
        if isinstance(page, ExamPage):
            page.start("Student", "Demo Exam")
            page.set_exam_attempt(ExamAttempt(load_demo_exam()), SecurityConfig())
            page.set_secure_mode_active(True)
        page.show()
        desktop.APP.processEvents()
        self.pages.append(page)
        return page

    def tearDown(self):
        for page in self.pages:
            page.close()
            page.deleteLater()
        desktop.APP.processEvents()

    def test_setup_default_greeting_is_visible_beside_controls(self):
        page = self.make_page(SetupPage)
        self.assertEqual(page.assistant.model.state, AssistantState.CALM)
        self.assertIn("Welcome to AI Exam Guard", visible_text(page))
        self.assertTrue(page.start_button.isVisible())
        self.assertTrue(page.teacher_review_button.isVisible())

    def test_current_risk_packet_drives_all_five_visual_states(self):
        page = self.make_page()
        for score, level, state in ((20, "LOW", AssistantState.CALM),
                                    (21, "MODERATE", AssistantState.NEUTRAL),
                                    (46, "HIGH", AssistantState.ALERT),
                                    (71, "CRITICAL", AssistantState.SERIOUS),
                                    (86, "CRITICAL", AssistantState.CRITICAL)):
            with self.subTest(score=score):
                page.apply_update(desktop.update(score=score, level=level))
                self.assertEqual(page.assistant.model.state, state)
                self.assertEqual(page.risk_score.text(), f"{score} / 100")

    def test_event_prompt_does_not_change_event_or_risk(self):
        page = self.make_page()
        event = replace(desktop.event(), message="Private report detail", confidence=0.91,
                        evidence_path="private/evidence.jpg", metadata={"private": "report"})
        before = vars(event).copy()
        page.apply_update(desktop.update(events=(event,), score=30, level="MODERATE"))
        self.assertIn("put your phone away", page.assistant.model.current_message())
        self.assertEqual(vars(event), before)
        self.assertNotIn("private/evidence.jpg", page.assistant.assistant.message_label.text())
        self.assertNotIn("Private report detail", page.assistant.assistant.message_label.text())
        self.assertEqual(page.timeline.count(), 1)
        self.assertEqual(page.risk_bar.value(), 30)

    def test_assistant_expiry_does_not_clear_persistent_absence_warning(self):
        page = self.make_page()
        page.apply_update(desktop.update(events=(desktop.event(EventType.FACE_MISSING),),
                                         face=FaceStatus.NO_FACE, warning=True, score=15))
        self.assertIn("camera view", page.assistant.model.current_message())
        self.now += 10
        page.assistant.refresh()
        page.banner_timer.timeout.emit()
        self.assertTrue(page.face_warning.isVisible())
        self.assertIn("Good luck", page.assistant.model.current_message())

    def test_break_tone_is_informational_without_changing_risk_visual(self):
        page = self.make_page()
        packet = replace(desktop.update(score=92, level="CRITICAL"), break_active=True,
                         break_remaining_seconds=120,
                         events=(desktop.event(EventType.AUTHORIZED_BREAK_STARTED, 0),))
        page.apply_update(packet)
        self.assertEqual(page.assistant.model.state, AssistantState.CRITICAL)
        self.assertIn("break", page.assistant.model.current_message().lower())
        self.now += 10
        page.assistant.refresh()
        self.assertIn("break", page.assistant.model.current_message().lower())
        self.assertTrue(page.break_banner.isVisible())

    def test_hidden_assistant_stays_hidden_across_updates_and_reset(self):
        page = self.make_page()
        page.assistant.set_visible(False)
        page.apply_update(desktop.update(events=(desktop.event(),), score=100, level="CRITICAL"))
        page.reset()
        self.assertTrue(page.assistant.arena.isHidden())
        self.assertTrue(page.assistant.options_button.isVisible())
        self.assertFalse(self.store.preferences.visible)

    def test_messages_can_be_hidden_while_portrait_updates(self):
        page = self.make_page()
        page.assistant.set_message_visible(False)
        page.apply_update(desktop.update(events=(desktop.event(),), score=71, level="CRITICAL"))
        self.assertTrue(page.assistant.assistant.message_label.isHidden())
        self.assertTrue(page.assistant.assistant.portrait.isVisible())
        self.assertEqual(page.assistant.model.state, AssistantState.SERIOUS)

    def test_setup_preference_changes_apply_to_new_exam_and_reload(self):
        setup = self.make_page(SetupPage)
        setup.assistant.set_visible(False)
        setup.assistant.set_message_visible(False)
        exam = self.make_page()
        self.assertTrue(exam.assistant.arena.isHidden())
        reloaded = AssistantPreferencesStore(self.store.path)
        self.assertFalse(reloaded.preferences.visible)
        self.assertFalse(reloaded.preferences.message_visible)

    def test_save_failure_keeps_user_hidden_choice_across_page_transitions(self):
        setup = self.make_page(SetupPage)
        with patch("ui.assistant_model.os.replace", side_effect=PermissionError("Read-only settings")):
            with self.assertLogs("ui.assistant_model", level="WARNING"):
                setup.assistant.set_visible(False)
        exam = self.make_page()
        self.assertTrue(exam.assistant.arena.isHidden())
        self.assertFalse(self.store.preferences.visible)
        setup.hide()
        setup.show()
        desktop.APP.processEvents()
        self.assertTrue(setup.assistant.arena.isHidden())

    def test_exam_safe_lane_does_not_overlap_essential_content_at_target_sizes(self):
        page = self.make_page()
        for width, height in ((1280, 800), (1366, 768), (1920, 1080)):
            with self.subTest(resolution=(width, height)):
                page.resize(width, height)
                desktop.APP.processEvents()
                lane = page.assistant.geometry()
                self.assertFalse(lane.intersects(page.question_panel.geometry()))
                self.assertFalse(lane.intersects(page.monitoring_column.geometry()))
                self.assertGreaterEqual(page.video.height(), 240)
                self.assertLessEqual(page.minimumSizeHint().height(), height)
                self.assertTrue(page.assistant.arena.rect().contains(page.assistant.assistant.geometry()))

    def test_setup_safe_lane_does_not_overlap_inputs_at_target_sizes(self):
        page = self.make_page(SetupPage)
        for width, height in ((1280, 800), (1366, 768), (1920, 1080)):
            with self.subTest(resolution=(width, height)):
                page.resize(width, height)
                desktop.APP.processEvents()
                origin = page.assistant.mapTo(page, QPoint(0, 0))
                lane = page.assistant.rect().translated(origin)
                for control in (page.student_input, page.start_button, page.teacher_review_button):
                    rect = control.rect().translated(control.mapTo(page, QPoint(0, 0)))
                    self.assertFalse(lane.intersects(rect))
                    self.assertTrue(page.rect().contains(rect))

    def test_stopping_cannot_restart_assistant_timer_from_final_packet(self):
        page = self.make_page()
        page.set_stopping()
        page.apply_update(desktop.update(events=(desktop.event(),), score=30, level="MODERATE"))
        self.assertFalse(page.assistant.timer.isActive())
        page.start("Next student", "Exam")
        page.apply_update(desktop.update(events=(desktop.event(),), score=30, level="MODERATE"))
        self.assertTrue(page.assistant.timer.isActive())


class AssistantControllerChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = AssistantPreferencesStore(Path(self.directory.name) / "assistant.json")
        self.now = 100.0
        self.window = MainWindow(replace(SessionConfig(), secure_mode="MAXIMIZED"),
                                 worker_factory=desktop.FakeWorker,
                                 discovery_factory=desktop.FakeDiscovery,
                                 auto_discover=False, clock=lambda: self.now,
                                 assistant_preferences=self.store,
                                 session_repository=FakeRepository(), review_worker_factory=FakeReader)
        self.window.setup_page.set_discovery([(0, "Camera 0")], desktop.READY)
        self.window.show()
        desktop.APP.processEvents()

    def tearDown(self):
        if self.window.worker is not None:
            self.window.finish_session()
            self.window.worker.complete()
        self.window.close()
        self.window.deleteLater()
        desktop.APP.processEvents()

    def start(self):
        self.window.setup_page.start_button.click()
        worker = self.window.worker
        worker.session_started.emit(self.now)
        desktop.APP.processEvents()
        return worker

    def test_shared_visibility_survives_setup_exam_and_new_session(self):
        self.window.setup_page.assistant.set_visible(False)
        worker = self.start()
        self.assertTrue(self.window.exam_page.assistant.arena.isHidden())
        self.window.finish_session()
        worker.complete()
        self.window.new_session()
        desktop.APP.processEvents()
        self.assertTrue(self.window.setup_page.assistant.arena.isHidden())

    def test_monitoring_packet_updates_assistant_without_changing_secure_window(self):
        worker = self.start()
        handle = int(self.window.winId())
        worker.pending = desktop.update(events=(desktop.event(),), score=92, level="CRITICAL")
        worker.update_ready.emit(worker.pending)
        self.assertEqual(self.window.exam_page.assistant.model.state, AssistantState.CRITICAL)
        self.assertEqual(int(self.window.winId()), handle)
        self.assertTrue(self.window._secure_mode_active)
        self.assertFalse(self.window._q_shortcut.isEnabled())

    def test_completion_and_teacher_review_have_no_visible_assistant_or_student_report_leak(self):
        worker = self.start()
        worker.pending = desktop.update(events=(desktop.event(),), score=92, level="CRITICAL")
        worker.update_ready.emit(worker.pending)
        self.window.finish_session()
        worker.complete(events=(desktop.event(),))
        desktop.APP.processEvents()
        current = self.window.pages.currentWidget()
        self.assertIs(current, self.window.report_page)
        self.assertNotIn("risk", visible_text(current).lower())
        self.assertEqual(current.findChildren(AssistantDock), [])
        self.assertFalse(self.window.exam_page.assistant.isVisible())
        self.window.open_teacher_review()
        self.assertEqual(self.window.state, "TEACHER_LOGIN")
        self.assertEqual(self.window.teacher_review_page.findChildren(AssistantDock), [])
        self.window._authorize_teacher(self.window.config.breaks.teacher_pin)
        self.assertEqual(self.window.state, "TEACHER_REVIEW")

    def test_external_shutdown_stops_both_assistant_timers_and_worker(self):
        worker = self.start()
        self.window.exam_page.add_event(desktop.event())
        self.window.shutdown_blocking()
        self.assertFalse(self.window.exam_page.assistant.timer.isActive())
        self.assertFalse(self.window.setup_page.assistant.timer.isActive())
        self.assertTrue(worker.stopped)
        self.assertTrue(worker.joined)


if __name__ == "__main__":
    unittest.main()
