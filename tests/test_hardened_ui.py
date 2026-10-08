"""Question-focused layout and private student/instructor screens, offscreen."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

import test_desktop as desktop
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QLineEdit

from exams import ExamAttempt, load_demo_exam
from monitoring import EventType
from security import SecurityConfig
from ui.completion_page import CompletionPage
from ui.exam_page import ExamPage
from ui.report_page import ReportPage
from ui.session import SessionResult
from ui.setup_page import SetupPage
from ui.teacher_review_page import TeacherReviewPage
from ui.theme import STYLESHEET
from vision.face_tracker import FaceStatus


def session_result(**changes):
    timestamp = datetime(2026, 10, 8, 10, 30, tzinfo=timezone.utc)
    return replace(SessionResult("Student A", "Demo Exam", 62, 92, "CRITICAL",
                                 (desktop.event(),), {EventType.PHONE_DETECTED: 1},
                                 started_at=timestamp, ended_at=timestamp,
                                 session_directory=Path("sessions/20261008_student")), **changes)


def summary(identity="session-a", student="Student A", risk=42):
    return SimpleNamespace(session_id=identity, student=student, exam="Demo Exam",
                           started_at=datetime(2026, 10, 8, 10, 30, tzinfo=timezone.utc),
                           duration_seconds=62, exam_score=5, exam_max_score=7,
                           risk_score=risk, risk_level="CRITICAL" if risk > 70 else "HIGH" if risk > 45 else "MODERATE" if risk > 20 else "LOW")


def visible_text(widget):
    return "\n".join(child.text() for child in widget.findChildren(QLabel) if child.isVisible())


class CompletionPrivacyChecks(unittest.TestCase):
    def setUp(self):
        self.page = ReportPage()
        self.page.setStyleSheet(STYLESHEET)
        self.page.resize(1280, 800)
        self.page.show()

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        desktop.APP.processEvents()

    def test_completion_contains_only_submission_metadata(self):
        self.page.show_completion(session_result())
        desktop.APP.processEvents()
        text = visible_text(self.page)
        self.assertIn("EXAM SUBMITTED", text)
        self.assertIn("Your answers have been saved.", text)
        self.assertIn("Student A", text)
        self.assertIn("Demo Exam", text)
        self.assertIn("00:01:02", text)
        self.assertNotIn("92 / 100", text)
        self.assertNotIn("CRITICAL", text)
        self.assertNotIn("PHONE_DETECTED", text)
        self.assertNotIn("SESSION RISK", text)
        self.assertFalse(self.page.tabs.isVisible())
        self.assertFalse(self.page.view_button.isVisible())
        self.assertFalse(self.page.back_to_sessions_button.isVisible())

    def test_full_report_model_is_retained_while_student_view_hidden(self):
        self.page.show_completion(session_result())
        self.assertEqual(self.page.timeline_model.rowCount(), 1)
        self.assertEqual(self.page.values["Final Risk"].text(), "92 / 100 · CRITICAL")
        self.assertIs(self.page.presentation.currentWidget(), self.page.completion_page)
        self.assertTrue(self.page.detail_widget.isHidden())

    def test_completion_offers_teacher_login_not_direct_evidence_access(self):
        requested = []
        self.page.teacher_review_requested.connect(lambda: requested.append(True))
        self.page.show_completion(session_result())
        self.page.completion_page.teacher_button.click()
        self.assertEqual(requested, [True])
        self.assertIs(self.page.presentation.currentWidget(), self.page.completion_page)

    def test_completion_new_session_signal_is_preserved(self):
        requested = []
        self.page.new_session_requested.connect(lambda: requested.append(True))
        self.page.show_completion(session_result())
        self.page.completion_page.new_button.click()
        self.assertEqual(requested, [True])

    def test_save_failure_is_generic_and_does_not_expose_error_details(self):
        result = session_result(persistence_error="Secret filesystem path C:/private/student/events.json",
                                error="Sensitive security exception")
        self.page.show_completion(result)
        desktop.APP.processEvents()
        text = visible_text(self.page)
        self.assertNotIn("Your answers have been saved.", text)
        self.assertIn("local save could not complete", text)
        self.assertNotIn("C:/private", text)
        self.assertNotIn("Sensitive security", text)

    def test_controller_can_open_instructor_details_and_return_to_sessions(self):
        requested = []
        self.page.back_to_sessions_requested.connect(lambda: requested.append(True))
        self.page.show_completion(session_result())
        self.page.show_instructor_report()
        desktop.APP.processEvents()
        self.assertIs(self.page.presentation.currentWidget(), self.page.detail_widget)
        self.assertTrue(self.page.tabs.isVisible())
        self.assertTrue(self.page.view_button.isVisible())
        self.assertTrue(self.page.back_to_sessions_button.isVisible())
        self.assertFalse(self.page.new_button.isVisible())
        self.page.back_to_sessions_button.click()
        self.assertEqual(requested, [True])

    def test_standalone_set_result_stays_detailed_for_legacy_api(self):
        self.page.set_result(session_result())
        self.assertIs(self.page.presentation.currentWidget(), self.page.detail_widget)
        self.assertEqual(self.page.risk_score.text(), "92 / 100")

    def test_return_to_sessions_closes_larger_evidence_dialog(self):
        selected = replace(desktop.event(), evidence_path="evidence/missing.jpg")
        self.page.set_result(session_result(events=(selected,)))
        self.page.show_instructor_report()
        dialog = self.page.evidence.open_event(selected)
        self.assertTrue(dialog.isVisible())
        self.page.back_to_sessions_button.click()
        self.assertFalse(dialog.isVisible())
        self.assertIsNone(self.page.evidence.dialog)

    def test_reset_clears_hidden_student_and_instructor_data(self):
        self.page.show_completion(session_result())
        self.page.reset()
        self.assertEqual(self.page.timeline_model.rowCount(), 0)
        self.assertEqual(self.page.completion_page.values["Student"].text(), "—")
        self.assertEqual(self.page.values["Student"].text(), "—")
        self.assertTrue(self.page.back_to_sessions_button.isHidden())


class TeacherReviewWidgetChecks(unittest.TestCase):
    def setUp(self):
        self.page = TeacherReviewPage()
        self.page.resize(1280, 800)
        self.page.show()

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        desktop.APP.processEvents()

    def test_locked_view_masks_pin_and_exposes_no_sessions(self):
        self.assertEqual(self.page.pin_input.echoMode(), QLineEdit.EchoMode.Password)
        self.assertIs(self.page.stack.currentWidget(), self.page.locked_page)
        self.assertEqual(self.page.sessions_model.rowCount(), 0)
        self.assertFalse(self.page.table.isVisible())
        self.assertFalse(self.page.open_button.isEnabled())

    def test_pin_is_forwarded_to_controller_and_cleared(self):
        pins = []
        self.page.pin_requested.connect(pins.append)
        self.page.pin_input.setText("wrong")
        self.page.unlock_button.click()
        self.assertEqual(pins, ["wrong"])
        self.assertEqual(self.page.pin_input.text(), "")
        self.assertIs(self.page.stack.currentWidget(), self.page.locked_page)
        self.page.set_error("Invalid Teacher PIN")
        self.assertFalse(self.page.pin_error.isHidden())
        self.assertEqual(self.page.sessions_model.rowCount(), 0)

    def test_authorized_list_shows_score_and_risk_separately(self):
        self.page.show_sessions((summary(),))
        model = self.page.sessions_model
        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.data(model.index(0, 4)), "5 / 7")
        self.assertEqual(model.data(model.index(0, 5)), "42 / 100")
        self.assertEqual(model.data(model.index(0, 6)), "MODERATE")
        self.assertEqual(model.data(model.index(0, 2)), "2026-10-08 15:30:00")

    def test_text_only_score_is_manual_review_and_legacy_score_is_unavailable(self):
        text_only = summary()
        text_only.exam_score = 0
        text_only.exam_max_score = 0
        legacy = summary("legacy")
        legacy.exam_score = None
        legacy.exam_max_score = None
        self.page.show_sessions((text_only, legacy))
        self.assertEqual(self.page.sessions_model.data(self.page.sessions_model.index(0, 4)), "Manual review")
        self.assertEqual(self.page.sessions_model.data(self.page.sessions_model.index(1, 4)), "—")
        desktop.APP.processEvents()
        self.assertLessEqual(self.page.table.horizontalHeader().length(), self.page.table.viewport().width())

    def test_open_correct_session_emits_opaque_id(self):
        ids = []
        self.page.session_open_requested.connect(ids.append)
        self.page.show_sessions((summary(), summary("session-b", "Student B", 65)))
        self.page.table.setCurrentIndex(self.page.sessions_model.index(1, 0))
        self.page.open_button.click()
        self.assertEqual(ids, ["session-b"])

    def test_double_click_opens_correct_row(self):
        ids = []
        self.page.session_open_requested.connect(ids.append)
        self.page.show_sessions((summary(),))
        self.page.table.doubleClicked.emit(self.page.sessions_model.index(0, 2))
        self.assertEqual(ids, ["session-a"])

    def test_locked_page_refuses_direct_open_action(self):
        ids = []
        self.page.session_open_requested.connect(ids.append)
        self.page._open_session(0)
        self.assertEqual(ids, [])

    def test_lock_clears_summary_data_and_error_text(self):
        self.page.show_sessions((summary(),), errors=("Skipped unreadable session",))
        self.page.show_locked()
        self.assertEqual(self.page.sessions_model.rowCount(), 0)
        self.assertEqual(self.page.list_error.text(), "")
        self.assertFalse(self.page.table.isVisible())

    def test_empty_sessions_and_corrupt_file_diagnostics_are_readable(self):
        self.page.show_sessions((), errors=("Skipped corrupted session",))
        self.assertFalse(self.page.open_button.isEnabled())
        self.assertIn("0 completed", self.page.message.text())
        self.assertIn("corrupted", self.page.list_error.text())

    def test_refresh_logout_and_back_signals(self):
        requested = []
        self.page.refresh_requested.connect(lambda: requested.append("refresh"))
        self.page.logout_requested.connect(lambda: requested.append("logout"))
        self.page.back_requested.connect(lambda: requested.append("back"))
        self.page.show_sessions((summary(),))
        self.page.refresh_button.click()
        self.page.logout_button.click()
        self.page.back_button.click()
        self.assertEqual(requested, ["refresh", "logout", "back"])


class HardenedSetupChecks(unittest.TestCase):
    def setUp(self):
        self.page = SetupPage()

    def tearDown(self):
        self.page.deleteLater()
        desktop.APP.processEvents()

    def test_standalone_default_mode_is_maximized(self):
        self.assertEqual(self.page.selected_secure_mode, "MAXIMIZED")

    def test_required_secure_mode_disables_windowed_choice(self):
        self.page.set_secure_mode("WINDOWED")
        self.page.set_secure_window_required(True)
        self.assertEqual(self.page.selected_secure_mode, "MAXIMIZED")
        index = self.page.secure_mode_select.findData("WINDOWED")
        self.assertFalse(self.page.secure_mode_select.model().item(index).isEnabled())
        self.page.set_secure_mode("WINDOWED")
        self.assertEqual(self.page.selected_secure_mode, "MAXIMIZED")

    def test_legacy_windowed_config_remains_available_when_not_required(self):
        self.page.set_secure_window_required(True)
        self.page.set_secure_window_required(False)
        self.page.set_secure_mode("WINDOWED")
        self.assertEqual(self.page.selected_secure_mode, "WINDOWED")

    def test_setup_teacher_review_control_emits_login_request(self):
        requested = []
        self.page.teacher_review_requested.connect(lambda: requested.append(True))
        self.page.teacher_review_button.click()
        self.assertEqual(requested, [True])
        self.page.set_busy(True)
        self.assertFalse(self.page.teacher_review_button.isEnabled())


class CentralExamLayoutChecks(unittest.TestCase):
    def setUp(self):
        self.page = ExamPage()
        self.page.setStyleSheet(STYLESHEET)
        self.page.start("Student", "Demo Exam")
        self.page.set_exam_attempt(ExamAttempt(load_demo_exam()), SecurityConfig())
        self.page.set_secure_mode_active(True)
        self.page.show()

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        desktop.APP.processEvents()

    def test_question_dominates_requested_resolutions_with_visible_camera(self):
        for width, height in ((1280, 800), (1366, 768), (1920, 1080)):
            with self.subTest(resolution=(width, height)):
                self.page.resize(width, height)
                desktop.APP.processEvents()
                self.assertGreater(self.page.question_panel.width(), self.page.monitoring_column.width() * 1.4)
                self.assertGreaterEqual(self.page.video.width(), 320)
                self.assertGreaterEqual(self.page.video.height(), 240)
                self.assertLessEqual(self.page.minimumSizeHint().height(), height)
                self.assertLessEqual(self.page.question_panel.geometry().right(), self.page.monitoring_column.geometry().left())

    def test_existing_monitoring_packet_populates_compact_diagnostics(self):
        self.page.apply_update(desktop.update(events=[desktop.event()], score=30, level="MODERATE"))
        self.assertEqual(self.page.values["Face"].text(), "Detected")
        self.assertEqual(self.page.values["Persons"].text(), "1")
        self.assertEqual(self.page.values["Phone"].text(), "None")
        self.assertEqual(self.page.values["Head"].text(), "UNKNOWN")
        self.assertEqual(self.page.values["Security"].text(), "ACTIVE")
        self.assertEqual(self.page.values["Camera"].text(), "READY")
        self.assertEqual(self.page.risk_score.text(), "30 / 100")
        self.assertEqual(self.page.timeline.count(), 1)

    def test_phone_occlusion_state_is_rendered(self):
        values = vars(desktop.update()).copy()
        values.update(phone_detected=True, phone_state="TRACKED_OCCLUDED")
        self.page.apply_update(SimpleNamespace(**values))
        self.assertEqual(self.page.values["Phone"].text(), "Tracked · occluded")
        self.assertIn("brief occlusion", self.page.values["Phone"].toolTip())

    def test_secure_indicator_and_low_priority_timeline(self):
        self.assertEqual(self.page.secure_session_indicator.text(), "SECURE SESSION ACTIVE")
        self.assertTrue(self.page._timeline_card.isHidden())
        self.page.timeline_toggle.click()
        self.assertFalse(self.page._timeline_card.isHidden())
        self.page.timeline_toggle.click()
        self.assertTrue(self.page._timeline_card.isHidden())

    def test_technical_diagnostics_can_be_expanded_without_backend_change(self):
        self.assertTrue(self.page.runtime_statuses["Evidence"][1].isHidden())
        self.page.technical_details_button.click()
        self.assertFalse(self.page.runtime_statuses["Evidence"][1].isHidden())
        self.page.technical_details_button.click()
        self.assertTrue(self.page.runtime_statuses["Evidence"][1].isHidden())

    def test_persistent_warnings_still_render_in_camera(self):
        self.page.apply_update(desktop.update(face=FaceStatus.NO_FACE, warning=True))
        self.assertFalse(self.page.face_warning.isHidden())
        self.page.banner_timer.timeout.emit()
        self.assertFalse(self.page.face_warning.isHidden())
        self.page.apply_update(desktop.update())
        self.assertTrue(self.page.face_warning.isHidden())


if __name__ == "__main__":
    unittest.main()
