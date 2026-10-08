"""Local question widgets, paste policy and setup loading. No real OS inputs."""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest

import test_desktop as desktop
from PySide6.QtCore import QEvent, QMimeData, Qt
from PySide6.QtGui import QFocusEvent, QKeyEvent
from PySide6.QtWidgets import QApplication

from exams import ExamAttempt, ExamDefinition, Question, QuestionType
from monitoring import EventType
from monitoring.break_manager import BreakConfig
from security import SecurityConfig
from ui.exam_page import ExamPage
from ui.question_panel import AnswerTextEdit, QuestionPanel
from ui.setup_page import SetupPage


def definition(*, timed=False):
    return ExamDefinition("ui-test", "A local test exam", (
        Question("s", QuestionType.SINGLE_CHOICE, "Choose one", ("A", "B"), "B"),
        Question("m", QuestionType.MULTIPLE_CHOICE, "Choose two", ("A", "B", "C"), ("A", "C")),
        Question("t", QuestionType.TEXT, "Explain <b>in plain text</b>"),
    ), 90 if timed else None)


class QuestionPanelChecks(unittest.TestCase):
    def setUp(self):
        self.panel = QuestionPanel()
        self.attempt = ExamAttempt(definition())
        self.panel.set_attempt(self.attempt)

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        desktop.APP.processEvents()

    def test_initial_unanswered_state_and_navigation_bounds(self):
        self.assertEqual(self.panel.question_number.text(), "Question 1 / 3")
        self.assertEqual(self.panel.progress.text(), "0 / 3 answered")
        self.assertFalse(self.panel.previous_button.isEnabled())
        self.assertTrue(self.panel.next_button.isEnabled())
        self.assertEqual(self.panel._navigator[0].toolTip(), "Unanswered")

    def test_single_choice_is_saved_on_click_and_restored(self):
        self.panel._option_widgets[1].click()
        self.panel.next_button.click()
        self.panel.previous_button.click()
        self.assertEqual(self.attempt.answer_for("s"), "B")
        self.assertTrue(self.panel._option_widgets[1].isChecked())
        self.assertEqual(self.panel._navigator[0].toolTip(), "Answered")

    def test_multiple_choice_saved_and_restored(self):
        self.panel.next_button.click()
        self.panel._option_widgets[2].click()
        self.panel._option_widgets[0].click()
        self.panel.next_button.click()
        self.panel.previous_button.click()
        self.assertEqual(self.attempt.answer_for("m"), ("A", "C"))
        self.assertTrue(self.panel._option_widgets[0].isChecked())
        self.assertFalse(self.panel._option_widgets[1].isChecked())
        self.assertTrue(self.panel._option_widgets[2].isChecked())

    def test_text_saved_verbatim_and_restored(self):
        self.panel._navigator[2].click()
        self.panel.text_answer.setPlainText("My answer\n  <b>plain</b>")
        self.panel.previous_button.click()
        self.panel.next_button.click()
        self.assertEqual(self.attempt.answer_for("t"), "My answer\n  <b>plain</b>")
        self.assertEqual(self.panel.text_answer.toPlainText(), "My answer\n  <b>plain</b>")

    def test_question_html_is_not_rendered(self):
        self.panel._navigator[2].click()
        self.assertEqual(self.panel.question_text.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn("<b>", self.panel.question_text.text())

    def test_numeric_navigation_and_previous_next(self):
        self.panel._navigator[2].click()
        self.assertEqual(self.attempt.current_index, 2)
        self.assertFalse(self.panel.next_button.isEnabled())
        self.panel.previous_button.click()
        self.assertEqual(self.attempt.current_index, 1)
        self.panel.previous_button.click()
        self.assertEqual(self.attempt.current_index, 0)

    def test_answer_changed_publishes_after_answer_is_saved(self):
        observed = []
        self.panel.answer_changed.connect(lambda: observed.append(self.attempt.answer_for("s")))
        self.panel._option_widgets[0].click()
        self.assertEqual(observed[-1], "A")

    def test_submit_click_does_not_submit_before_confirmation(self):
        requests, confirmed = [], []
        self.panel.submit_requested.connect(lambda: requests.append(True))
        self.panel.submission_confirmed.connect(lambda: confirmed.append(True))
        self.panel.submit_button.click()
        self.assertEqual(requests, [True])
        self.assertFalse(self.attempt.submitted)
        self.assertEqual(confirmed, [])

    def test_confirmation_and_cancel_use_inline_controls(self):
        confirmed = []
        self.panel.submission_confirmed.connect(lambda: confirmed.append(True))
        self.panel.show_submission_confirmation()
        self.assertFalse(self.panel.confirmation_panel.isHidden())
        self.assertEqual(self.panel.confirmation_message.text(), "Submit exam? You will not be able to change your answers.")
        self.panel.cancel_button.click()
        self.assertTrue(self.panel.confirmation_panel.isHidden())
        self.assertTrue(self.panel.submit_button.isEnabled())
        self.assertEqual(confirmed, [])
        self.panel.show_submission_confirmation()
        self.panel.confirm_button.click()
        self.assertEqual(confirmed, [True])

    def test_inactive_panel_cannot_edit_or_navigate(self):
        self.panel.set_active(False)
        self.panel._option_widgets[0].click()
        self.panel.next_button.click()
        self.panel.show_submission_confirmation()
        self.assertIsNone(self.attempt.answer_for("s"))
        self.assertEqual(self.attempt.current_index, 0)
        self.assertTrue(self.panel.text_answer.isReadOnly())
        self.assertTrue(self.panel.confirmation_panel.isHidden())

    def test_submitted_attempt_remains_locked(self):
        instant = datetime(2026, 10, 8, tzinfo=timezone.utc)
        self.attempt.start(instant)
        self.attempt.submit(submitted_at=instant, duration_seconds=0)
        self.panel.set_active(True)
        self.assertFalse(self.panel.submit_button.isEnabled())
        self.assertTrue(self.panel.text_answer.isReadOnly())

    def test_reset_clears_answers_controls_and_confirmation(self):
        self.panel.show_submission_confirmation()
        self.panel.reset()
        self.assertIsNone(self.panel.attempt)
        self.assertEqual(self.panel._navigator, [])
        self.assertEqual(self.panel._option_widgets, [])
        self.assertTrue(self.panel.confirmation_panel.isHidden())

    def test_large_exam_uses_compact_question_selector(self):
        large = ExamDefinition("large", "Large local exam", tuple(
            Question(f"q{index}", QuestionType.TEXT, f"Question {index}") for index in range(100)))
        self.panel.set_attempt(ExamAttempt(large))
        self.assertEqual(self.panel._navigator, [])
        self.assertFalse(self.panel.navigator_select.isHidden())
        self.assertEqual(self.panel.navigator_select.count(), 100)
        self.panel.navigator_select.setCurrentIndex(99)
        self.assertEqual(self.panel.attempt.current_index, 99)
        self.assertEqual(self.panel.question_number.text(), "Question 100 / 100")

    def test_option_labels_keep_plain_original_values(self):
        option = "A <b>very long plain option</b> " * 20
        exam = ExamDefinition("long", "Long local exam", (
            Question("q", QuestionType.SINGLE_CHOICE, "Choose", (option, "Short"), "Short"),))
        self.panel.set_attempt(ExamAttempt(exam))
        labels = list(self.panel._option_labels)
        self.assertEqual(labels[0].textFormat(), Qt.TextFormat.PlainText)
        self.assertTrue(labels[0].wordWrap())
        self.panel._option_widgets[0].click()
        self.assertEqual(self.panel.attempt.answer_for("q"), option)

    def test_editing_focus_signals(self):
        states = []
        self.panel.editing_changed.connect(states.append)
        self.panel.text_answer.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
        self.panel.text_answer.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
        self.assertEqual(states, [True, False])


class PastePolicyChecks(unittest.TestCase):
    def setUp(self):
        self.panel = QuestionPanel()
        self.attempt = ExamAttempt(definition())

    def tearDown(self):
        self.panel.deleteLater()
        desktop.APP.processEvents()

    def prepare(self, blocked):
        self.panel.set_attempt(self.attempt, SecurityConfig(blocked_events=frozenset({EventType.PASTE_ATTEMPT}) if blocked else frozenset()))
        self.panel._navigator[2].click()
        self.sources = []
        self.panel.local_paste_requested.connect(self.sources.append)
        self.mime = QMimeData()
        self.mime.setText("external content")

    def test_detect_only_paste_is_allowed_and_reported(self):
        self.prepare(False)
        self.panel.text_answer.insertFromMimeData(self.mime)
        self.assertEqual(self.attempt.answer_for("t"), "external content")
        self.assertEqual(self.sources, ["context_or_programmatic"])

    def test_context_or_direct_paste_is_refused_when_suppressed(self):
        self.prepare(True)
        self.panel.text_answer.insertFromMimeData(self.mime)
        self.assertEqual(self.panel.text_answer.toPlainText(), "")
        self.assertEqual(self.sources, ["context_or_programmatic"])
        self.assertIsNone(self.attempt.answer_for("t"))

    def test_ctrl_v_is_refused_and_locally_detectable_without_hooks(self):
        self.prepare(True)
        key = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        self.panel.text_answer.keyPressEvent(key)
        self.assertEqual(self.sources, ["ctrl_v"])
        self.assertEqual(self.panel.text_answer.toPlainText(), "")

    def test_shift_insert_is_refused_and_reported_once(self):
        self.prepare(True)
        key = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Insert, Qt.KeyboardModifier.ShiftModifier)
        self.panel.text_answer.keyPressEvent(key)
        self.assertEqual(self.sources, ["shift_insert"])
        self.assertEqual(self.panel.text_answer.toPlainText(), "")

    def test_drop_is_disabled_and_readonly_cannot_insert(self):
        self.prepare(False)
        self.assertFalse(self.panel.text_answer.acceptDrops())
        self.panel.set_active(False)
        self.panel.text_answer.insertFromMimeData(self.mime)
        self.assertEqual(self.panel.text_answer.toPlainText(), "")
        self.assertEqual(self.sources, [])


class ExamSetupChecks(unittest.TestCase):
    def setUp(self):
        self.page = SetupPage()
        self.directory = self.enterContext(tempfile.TemporaryDirectory())

    def tearDown(self):
        self.page.deleteLater()
        desktop.APP.processEvents()

    def write_exam(self, data):
        path = Path(self.directory) / "exam.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_demo_exam_is_selected(self):
        self.assertIsNotNone(self.page.selected_exam)
        self.assertGreaterEqual(len(self.page.selected_exam.questions), 8)

    def test_configured_definition_becomes_visible_selection(self):
        exam = definition(timed=True)
        self.page.set_exam_definition(exam)
        self.assertIs(self.page.selected_exam, exam)
        self.assertEqual(self.page.exam_select.currentText(), exam.title)
        count = self.page.exam_select.count()
        self.page.set_exam_definition(exam)
        self.assertEqual(self.page.exam_select.count(), count)

    def test_missing_exam_selection_prevents_start(self):
        recorded = []
        self.page.start_requested.connect(lambda *args: recorded.append(args))
        self.page.set_discovery([(0, "Fake camera")], desktop.READY)
        self.page.exam_select.clear()
        self.page.start_button.click()
        self.assertEqual(recorded, [])
        self.assertIn("valid local exam", self.page.exam_error.text())

    def test_valid_local_exam_selected(self):
        path = self.write_exam({"id": "loaded", "title": "Loaded exam", "questions": [
            {"id": "q", "type": "TEXT", "text": "Explain"}]})
        self.assertTrue(self.page.load_exam_file(path))
        self.assertEqual(self.page.selected_exam.id, "loaded")
        self.assertEqual(self.page.exam_input.text(), "Loaded exam")
        self.assertTrue(self.page.exam_error.isHidden())

    def test_invalid_local_exam_retains_valid_demo_and_shows_error(self):
        demo = self.page.selected_exam
        self.assertFalse(self.page.load_exam_file(self.write_exam({"questions": []})))
        self.assertIs(self.page.selected_exam, demo)
        self.assertFalse(self.page.exam_error.isHidden())
        self.assertIn("Invalid exam JSON", self.page.exam_error.text())

    def test_missing_local_file_is_handled(self):
        self.assertFalse(self.page.load_exam_file(Path(self.directory) / "missing.json"))
        self.assertIn("Cannot load exam JSON", self.page.exam_error.text())

    def test_busy_setup_disables_exam_controls(self):
        self.page.set_busy(True)
        self.assertFalse(self.page.exam_select.isEnabled())
        self.assertFalse(self.page.load_exam_button.isEnabled())
        self.assertFalse(self.page.secure_mode_select.isEnabled())
        self.assertFalse(self.page.load_exam_file(Path(self.directory) / "missing.json"))

    def test_secure_modes_and_invalid_mode(self):
        for mode in ("WINDOWED", "MAXIMIZED", "FULLSCREEN"):
            self.page.set_secure_mode(mode)
            self.assertEqual(self.page.selected_secure_mode, mode)
        with self.assertRaises(ValueError):
            self.page.set_secure_mode("lock_os")

    def test_existing_start_signal_has_three_arguments(self):
        recorded = []
        self.page.start_requested.connect(lambda *args: recorded.append(args))
        self.page.set_discovery([(0, "Fake camera")], desktop.READY)
        self.page.start_button.click()
        self.assertEqual(recorded, [("Student", "Practice Exam", 0)])


class ExamPageIntegrationChecks(unittest.TestCase):
    def setUp(self):
        self.page = ExamPage()
        self.page.start("Student", "Exam", break_config=BreakConfig(teacher_pin="1234"))

    def tearDown(self):
        self.page.deleteLater()
        desktop.APP.processEvents()

    def test_attempt_attached_and_monitoring_packet_still_renders(self):
        attempt = ExamAttempt(definition())
        self.page.set_exam_attempt(attempt, SecurityConfig())
        self.page.question_panel.next_button.click()
        self.page.apply_update(desktop.update(events=[desktop.event()], score=30, level="MODERATE"))
        self.assertEqual(attempt.current_index, 1)
        self.assertEqual(self.page.timeline.count(), 1)
        self.assertEqual(self.page.risk_score.text(), "30 / 100")
        self.assertIsNotNone(self.page.video.pixmap())
        self.assertTrue(self.page.finish_button.isHidden())

    def test_timed_attempt_uses_remaining_heading(self):
        self.page.set_exam_attempt(ExamAttempt(definition(timed=True)))
        self.assertEqual(self.page.timer_heading.text(), "TIME REMAINING")
        self.assertEqual(self.page.timer_label.text(), "00:01:30")
        self.page.set_remaining(30)
        self.assertEqual(self.page.timer_label.text(), "00:00:30")

    def test_secure_mode_exit_requires_pin_and_does_not_finish(self):
        exits, finishes = [], []
        self.page.secure_exit_requested.connect(exits.append)
        self.page.finish_requested.connect(lambda: finishes.append(True))
        self.page.set_secure_mode_active(True)
        self.page.exit_secure_button.click()
        self.page.pin_input.setText("wrong")
        self.page.verify_pin_button.click()
        self.assertEqual(exits, [])
        self.page.pin_input.setText("1234")
        self.page.verify_pin_button.click()
        self.assertEqual(exits, ["1234"])
        self.assertEqual(finishes, [])
        self.assertTrue(self.page.authorization_panel.isHidden())
        self.page.apply_update(desktop.update())
        self.assertIn("Secure mode", self.page.fps_label.text())
        self.assertNotIn("Q: finish", self.page.fps_label.text())

    def test_stopping_locks_question_controls(self):
        self.page.set_exam_attempt(ExamAttempt(definition()))
        self.page.set_stopping()
        self.assertFalse(self.page.question_panel.submit_button.isEnabled())
        self.assertTrue(self.page.question_panel.text_answer.isReadOnly())

    def test_repeated_sessions_reuse_one_compact_monitoring_layout(self):
        self.page.set_exam_attempt(ExamAttempt(definition()))
        count = self.page._video_column.count()
        self.page.reset()
        self.page.start("Second", "Second exam")
        self.page.set_exam_attempt(ExamAttempt(definition()))
        self.assertEqual(self.page._video_column.count(), count)


if __name__ == "__main__":
    unittest.main()
