"""Student UI localization, persisted settings and light-theme regressions."""

from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import test_desktop as desktop
from PySide6.QtCore import QCoreApplication, QEvent, Qt

from exams import ExamAttempt, ExamDefinition, Question, QuestionType, load_demo_exam
from i18n import language_manager, missing_translations, tr
from monitoring import EventType
from security import SecurityConfig
from ui.assistant_model import AssistantPreferences, AssistantPreferencesStore
from ui.exam_page import ExamPage
from ui.main_window import MainWindow
from ui.question_panel import QuestionPanel
from ui.setup_page import SetupPage
from ui.theme import COLORS, STYLESHEET, VIDEO_STYLE
from vision.face_tracker import FaceStatus, HeadDirection


class ProductPolishChecks(unittest.TestCase):
    def setUp(self):
        language_manager.set_language("en")
        self.temporary = TemporaryDirectory()
        self.path = Path(self.temporary.name) / "assistant.json"
        self.store = AssistantPreferencesStore(self.path)
        self.widgets = []

    def tearDown(self):
        for widget in reversed(self.widgets):
            if isinstance(widget, MainWindow):
                widget.shutdown_blocking()
                widget.worker = None
                widget.state = "SETUP"
                widget._secure_mode_active = False
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        desktop.APP.processEvents()
        language_manager.set_language("en")
        self.temporary.cleanup()

    def setup_page(self):
        page = SetupPage(assistant_preferences=self.store)
        page.setStyleSheet(STYLESHEET)
        page.set_discovery([(0, "Camera 0")], desktop.READY)
        self.widgets.append(page)
        return page

    def exam_page(self):
        page = ExamPage(assistant_preferences=self.store)
        page.setStyleSheet(STYLESHEET)
        page.start("LOW", "PHONE_DETECTED")
        self.widgets.append(page)
        return page

    def test_setup_selector_has_native_names_and_canonical_language_codes(self):
        page = self.setup_page()
        self.assertEqual([page.language_select.itemText(i) for i in range(3)],
                         ["English", "Русский", "Қазақша"])
        self.assertEqual([page.language_select.itemData(i) for i in range(3)], ["en", "ru", "kk"])

    def test_russian_selection_updates_ui_and_persists_in_existing_settings(self):
        page = self.setup_page()
        page.language_select.setCurrentIndex(page.language_select.findData("ru"))
        self.assertEqual(page.start_button.text(), tr("START SECURE SESSION", language="ru"))
        self.assertNotEqual(page.start_button.text(), "START SECURE SESSION")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["language"], "ru")
        self.assertEqual(AssistantPreferencesStore(self.path).preferences.language, "ru")

    def test_kazakh_preference_reused_when_application_constructed(self):
        self.store.update(language="kk")
        window = MainWindow(auto_discover=False, assistant_preferences=AssistantPreferencesStore(self.path))
        self.widgets.append(window)
        self.assertEqual(window.setup_page.language_select.currentData(), "kk")
        self.assertEqual(window.exam_page.finish_button.text(), tr("FINISH EXAM", language="kk"))
        self.assertEqual(language_manager.language, "kk")

    def test_language_update_preserves_assistant_preferences_and_positions(self):
        self.store.update(visible=False, message_visible=False, context="exam", position=(0.7, 0.2))
        self.store.update(language="ru")
        loaded = AssistantPreferencesStore(self.path).preferences
        self.assertFalse(loaded.visible)
        self.assertFalse(loaded.message_visible)
        self.assertEqual(loaded.positions["exam"], (0.7, 0.2))

    def test_english_selection_round_trips_after_other_language(self):
        page = self.setup_page()
        page.language_select.setCurrentIndex(1)
        page.language_select.setCurrentIndex(0)
        self.assertEqual(page.start_button.text(), "START SECURE SESSION")
        self.assertEqual(AssistantPreferencesStore(self.path).preferences.language, "en")

    def test_invalid_saved_language_falls_back_to_english(self):
        self.path.write_text(json.dumps({"version": 1, "language": "invalid"}), encoding="utf-8")
        self.assertEqual(AssistantPreferencesStore(self.path).preferences.language, "en")
        with self.assertRaises(ValueError):
            AssistantPreferences(language="invalid")

    def test_language_save_failure_keeps_live_choice_and_existing_preferences(self):
        page = self.setup_page()
        with patch("ui.assistant_model.os.replace", side_effect=OSError("Unavailable settings folder")):
            page.language_select.setCurrentIndex(2)
        self.assertEqual(language_manager.language, "kk")
        self.assertEqual(self.store.preferences.language, "kk")
        self.assertTrue(self.store.preferences.visible)
        self.assertEqual(page.start_button.text(), tr("START SECURE SESSION"))

    def test_setup_names_and_selected_exam_are_never_translated(self):
        page = self.setup_page()
        definition = replace(load_demo_exam(), title="LOW", reference="local.json")
        page.set_exam_definition(definition)
        page.student_input.setText("PHONE_DETECTED")
        page.exam_input.setText("LOW")
        page.language_select.setCurrentIndex(1)
        self.assertEqual(page.student_input.text(), "PHONE_DETECTED")
        self.assertEqual(page.exam_input.text(), "LOW")
        self.assertEqual(page.exam_select.currentText(), "LOW")
        self.assertIs(page.selected_exam, definition)
        self.assertEqual(page.selected_secure_mode, "MAXIMIZED")
        self.assertEqual(page.camera_select.currentData(), 0)

    def test_warning_and_runtime_status_switch_without_replaying_update(self):
        page = self.exam_page()
        page.apply_update(desktop.update(face=FaceStatus.NO_FACE, warning=True, score=55, level="HIGH"))
        for language in ("ru", "kk", "en"):
            language_manager.set_language(language)
            self.assertEqual(page.face_warning.text(), tr("WARNING\nSTUDENT LEFT CAMERA VIEW"))
            self.assertFalse(page.face_warning.isHidden())
            self.assertEqual(page.risk_level.text(), tr("HIGH"))
            self.assertEqual(page.risk_score.text(), "55 / 100")
            self.assertEqual(page.runtime_statuses["Camera"][1].text(), tr("{component}: {state}", component="CAMERA", state="READY"))

    def test_obstruction_warning_is_localized_and_has_visual_precedence(self):
        page = self.exam_page()
        page.apply_update(replace(desktop.update(face=FaceStatus.NO_FACE, warning=True), camera_obstructed_warning=True))
        language_manager.set_language("kk")
        self.assertTrue(page.face_warning.isHidden())
        self.assertFalse(page.obstruction_warning.isHidden())
        self.assertEqual(page.obstruction_warning.text(), tr("WARNING\nCAMERA VIEW OBSTRUCTED"))

    def test_tracker_error_keeps_raw_diagnostic_inside_localized_notice(self):
        page = self.exam_page()
        diagnostic = "AttributeError: tracker._path"
        page.apply_update(replace(desktop.update(face=FaceStatus.UNAVAILABLE),
                                  statuses={"Face Tracking": ("UNAVAILABLE", diagnostic)}))
        language_manager.set_language("ru")
        self.assertTrue(page.face_warning.isHidden())
        self.assertIn(diagnostic, page.face_tracking_notice.text())
        self.assertNotIn("Student absence monitoring paused", page.face_tracking_notice.text())

    def test_tracker_without_details_switches_all_notice_text(self):
        page = self.exam_page()
        page.set_statuses({"Face Tracking": ("UNAVAILABLE", "")})
        for language in ("ru", "kk"):
            language_manager.set_language(language)
            self.assertNotIn("No technical details supplied", page.face_tracking_notice.text())
            self.assertIn(tr("No technical details supplied"), page.face_tracking_notice.text())

    def test_persistent_warning_wraps_long_translations_inside_camera_column(self):
        page = self.exam_page()
        page.set_exam_attempt(ExamAttempt(load_demo_exam()), SecurityConfig())
        page.set_secure_mode_active(True)
        page.resize(1280, 800)
        page.show()
        page.apply_update(desktop.update(face=FaceStatus.NO_FACE, warning=True))
        language_manager.set_language("ru")
        desktop.APP.processEvents()
        self.assertTrue(page.face_warning.wordWrap())
        self.assertTrue(page.obstruction_warning.wordWrap())
        self.assertLessEqual(page.face_warning.width(), page.video.width())
        self.assertEqual(page.width(), 1280)
        self.assertNotEqual(page.secure_session_indicator.text(), "SECURE SESSION ACTIVE")

    def test_language_change_preserves_current_camera_image(self):
        page = self.exam_page()
        page.apply_update(desktop.update())
        source_key = page.video._source.cacheKey()
        for language in ("ru", "kk", "en"):
            language_manager.set_language(language)
            self.assertFalse(page.video.pixmap().isNull())
            self.assertEqual(page.video._source.cacheKey(), source_key)

    def test_timeline_translates_without_duplicate_history_or_new_prompt_deadline(self):
        page = self.exam_page()
        emitted = desktop.event(EventType.PHONE_DETECTED)
        original = emitted.to_dict()
        page.add_event(emitted)
        initial_deadline = page.assistant.model.next_deadline
        for language in ("ru", "kk", "en"):
            language_manager.set_language(language)
            self.assertEqual(page.timeline.count(), 1)
            self.assertEqual(page._total_events, 1)
            self.assertIs(page.timeline.item(0).data(Qt.ItemDataRole.UserRole), emitted)
            self.assertEqual(page.assistant.model.next_deadline, initial_deadline)
            self.assertEqual(emitted.to_dict(), original)

    def test_language_switch_keeps_raw_header_names(self):
        page = self.exam_page()
        language_manager.set_language("ru")
        self.assertEqual(page.session_label.text(), "LOW · PHONE_DETECTED")
        self.assertEqual(page.session_label.toolTip(), "LOW · PHONE_DETECTED")

    def test_exam_content_options_and_answers_are_verbatim_in_all_languages(self):
        question = Question("q", QuestionType.SINGLE_CHOICE, "PHONE_DETECTED", ("Detected", "None"), "Detected")
        attempt = ExamAttempt(ExamDefinition("exam", "LOW", (question,)))
        panel = QuestionPanel()
        self.widgets.append(panel)
        panel.set_attempt(attempt, SecurityConfig())
        panel._option_widgets[0].click()
        for language in ("ru", "kk", "en"):
            language_manager.set_language(language)
            self.assertEqual(panel.exam_title.text(), "LOW")
            self.assertEqual(panel.question_text.text(), "PHONE_DETECTED")
            self.assertEqual([text.text() for text in panel._option_labels], ["Detected", "None"])
            self.assertEqual(panel._option_widgets[0].accessibleName(), "Detected")
            self.assertEqual(attempt.answer_for("q"), "Detected")
            self.assertEqual(panel.next_button.text(), tr("NEXT"))

    def test_head_direction_progress_and_timer_remain_correct_after_switch(self):
        page = self.exam_page()
        packet = replace(desktop.update(), face=replace(desktop.update().face, head_direction=HeadDirection.LEFT),
                         head_duration_seconds=2.5, head_threshold_seconds=4)
        page.apply_update(packet)
        page.set_elapsed(65)
        language_manager.set_language("kk")
        self.assertEqual(page.head_badge.text(), tr("HEAD: {direction} · {duration}/{threshold}s", direction="LEFT", duration="2.5", threshold="4"))
        self.assertEqual(page.values["Head"].text(), tr("LEFT"))
        self.assertEqual(page.timer_label.text(), "00:01:05")

    def test_break_controls_translate_without_losing_authorization_or_duration_data(self):
        page = self.exam_page()
        page._open_authorization()
        page.pin_input.setText(page.break_config.teacher_pin)
        page._verify_pin()
        original = [page.break_duration.itemData(i) for i in range(page.break_duration.count())]
        language_manager.set_language("ru")
        self.assertFalse(page.pin_input.isEnabled())
        self.assertEqual([page.break_duration.itemData(i) for i in range(page.break_duration.count())], original)
        self.assertEqual(page.start_break_button.text(), tr("START BREAK"))

    def test_light_stylesheet_applied_to_main_window_and_warning_components(self):
        window = MainWindow(auto_discover=False, assistant_preferences=self.store)
        self.widgets.append(window)
        self.assertEqual(window.styleSheet(), STYLESHEET)
        self.assertIn(COLORS.background, window.styleSheet())
        self.assertIn(COLORS.primary, window.styleSheet())
        self.assertIn(COLORS.critical, window.exam_page.face_warning.styleSheet())
        self.assertIn(COLORS.critical_surface, window.exam_page.face_warning.styleSheet())
        self.assertEqual(window.exam_page.video.styleSheet(), VIDEO_STYLE)

    def test_text_and_semantic_panels_have_readable_contrast(self):
        def luminance(color):
            values = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            values = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in values]
            return sum(value * weight for value, weight in zip(values, (0.2126, 0.7152, 0.0722)))
        for foreground, background in ((COLORS.text, COLORS.background), (COLORS.muted, COLORS.surface),
                                       (COLORS.primary, COLORS.surface), (COLORS.success, COLORS.success_surface),
                                       (COLORS.warning, COLORS.warning_surface), (COLORS.critical, COLORS.critical_surface)):
            lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
            self.assertGreaterEqual((lighter + 0.05) / (darker + 0.05), 4.5)

    def test_complete_catalog_includes_all_student_surface_templates(self):
        self.assertFalse(missing_translations())
        for source in ("START SECURE SESSION", "WARNING\nSTUDENT LEFT CAMERA VIEW", "WARNING\nCAMERA VIEW OBSTRUCTED",
                       "FINISH EXAM", "SESSION TIME", "Type your answer here…", "AUTHORIZE BREAK"):
            for language in ("ru", "kk"):
                self.assertNotEqual(tr(source, language=language), source)


if __name__ == "__main__":
    unittest.main()
