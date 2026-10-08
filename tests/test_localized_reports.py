"""Localized review presentation preserves canonical records and student privacy."""

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QLabel

from test_desktop import APP
from test_exam_report_storage import academic_result
from test_hardened_ui import summary
from test_report_page import event, result
from i18n import language_manager, tr
from i18n.report_translations import TRANSLATIONS
from ui.report_page import ReportPage
from ui.teacher_review_page import TeacherReviewPage
from ui.theme import COLORS, RISK_COLORS, STYLESHEET


class LocalizedReportChecks(unittest.TestCase):
    def setUp(self):
        language_manager.set_language("en")
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.page = ReportPage()
        self.page.setStyleSheet(STYLESHEET)
        self.page.resize(1280, 800)

    def tearDown(self):
        self.page.reset()
        self.page.close()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()
        language_manager.set_language("en")
        self.temp.cleanup()

    def image_event(self):
        path = self.directory / "PHONE_DETECTED.jpg"
        image = QImage(80, 60, QImage.Format.Format_RGB888)
        image.fill(0x486078)
        self.assertTrue(image.save(str(path)))
        return replace(event(path=str(path), confidence=0.92), message="Cell phone detected")

    def test_report_catalog_has_russian_and_kazakh_for_every_key(self):
        self.assertGreater(len(TRANSLATIONS), 100)
        for source, translations in TRANSLATIONS.items():
            with self.subTest(source=source):
                self.assertEqual(set(translations), {"ru", "kk"})
                self.assertTrue(all(value.strip() for value in translations.values()))

    def test_english_report_and_completion_labels_are_preserved(self):
        self.page.set_result(result())
        self.assertEqual(self.page.tabs.tabText(0), "Summary")
        self.assertEqual(self.page.timeline_model.headerData(1, Qt.Orientation.Horizontal), "Event")
        self.page.show_completion(result(session_directory=self.directory))
        self.assertEqual(self.page.completion_page.heading.text(), "EXAM SUBMITTED")
        self.assertEqual(self.page.completion_page.teacher_button.text(), "TEACHER REVIEW")

    def test_russian_report_translates_headers_tabs_risk_and_messages(self):
        selected = replace(event(), message="Cell phone detected")
        self.page.set_result(result((selected,)))
        language_manager.set_language("ru")
        self.assertEqual(self.page.tabs.tabText(0), "Сводка")
        model = self.page.timeline_model
        self.assertEqual(model.headerData(1, Qt.Orientation.Horizontal), "Событие")
        self.assertEqual(model.data(model.index(0, 1)), "Обнаружен телефон")
        self.assertEqual(model.data(model.index(0, 4)), "Обнаружен мобильный телефон")
        self.assertEqual(self.page.risk_level.text(), tr("HIGH"))
        self.assertIn("Событий: 1", self.page.timeline_count.text())

    def test_kazakh_report_translates_headers_tabs_risk_and_messages(self):
        selected = replace(event(), message="Cell phone detected")
        self.page.set_result(result((selected,)))
        language_manager.set_language("kk")
        self.assertEqual(self.page.tabs.tabText(0), "Қорытынды")
        model = self.page.timeline_model
        self.assertEqual(model.headerData(1, Qt.Orientation.Horizontal), "Оқиға")
        self.assertEqual(model.data(model.index(0, 1)), "Телефон анықталды")
        self.assertEqual(model.data(model.index(0, 4)), "Ұялы телефон анықталды")
        self.assertEqual(self.page.risk_level.text(), tr("HIGH"))

    def test_language_switch_does_not_change_selected_report_tab(self):
        self.page.set_result(result())
        self.page.tabs.setCurrentIndex(3)
        for language in ("ru", "kk", "en"):
            language_manager.set_language(language)
            self.assertEqual(self.page.tabs.currentIndex(), 3)

    def test_report_model_notifies_views_on_language_switch(self):
        self.page.set_result(result((event(),)))
        changes, headers = [], []
        model = self.page.timeline_model
        model.dataChanged.connect(lambda *_args: changes.append(True))
        model.headerDataChanged.connect(lambda *_args: headers.append(True))
        language_manager.set_language("ru")
        self.assertEqual(changes, [True])
        self.assertEqual(headers, [True])

    def test_academic_status_localizes_while_student_answers_remain_raw(self):
        academic = academic_result()
        self.page.set_result(result(exam_result=academic))
        model = self.page.answers_model
        answer = academic.answers["written"]
        for language in ("ru", "kk"):
            language_manager.set_language(language)
            self.assertEqual(model.data(model.index(3, 1)), answer)
            self.assertEqual(model.data(model.index(3, 2)), tr("Answered"))
            self.assertEqual(model.data(model.index(3, 3)), tr("Manual Review"))
            self.assertEqual(self.page.exam_values["Status"].text(), tr("Submitted"))
            self.assertIn(academic.exam_name, self.page.exam_note.text())

    def test_user_names_matching_translation_keys_are_not_translated(self):
        stored = result(student="LOW", exam="Teacher Review")
        self.page.set_result(stored)
        self.page.show_completion(stored)
        for language in ("ru", "kk"):
            language_manager.set_language(language)
            for values in (self.page.values, self.page.completion_page.values):
                self.assertEqual(values["Student"].text(), "LOW")
                self.assertEqual(values["Exam"].text(), "Teacher Review")

    def test_canonical_risk_events_and_academic_data_never_change(self):
        selected = self.image_event()
        academic = academic_result()
        stored = result((selected,), exam_result=academic, session_directory=self.directory)
        before = (stored, academic.to_dict(), selected.to_dict())
        self.page.set_result(stored)
        for language in ("ru", "kk", "en"):
            language_manager.set_language(language)
            self.assertEqual(self.page.risk_bar.value(), stored.risk_score)
        self.assertEqual(before, (stored, academic.to_dict(), selected.to_dict()))
        self.assertEqual(stored.risk_level, "HIGH")
        self.assertEqual(selected.type.value, "PHONE_DETECTED")

    def test_completion_remains_private_in_all_languages_and_after_switch(self):
        stored = result((event(),), session_directory=self.directory,
                        persistence_error="SECRET teacher error", risk_score=92, risk_level="CRITICAL")
        self.page.show_completion(stored)
        self.page.show()
        for language in ("en", "ru", "kk"):
            language_manager.set_language(language)
            APP.processEvents()
            visible = " ".join(label.text() for label in self.page.findChildren(QLabel) if label.isVisible())
            self.assertIn(tr("EXAM SUBMITTED"), visible)
            for secret in ("92 / 100", tr("CRITICAL"), "SECRET teacher error", "PHONE_DETECTED", tr("SESSION RISK")):
                self.assertNotIn(secret, visible)
            self.assertIs(self.page.presentation.currentWidget(), self.page.completion_page)
            self.assertFalse(self.page.tabs.isVisible())

    def test_evidence_cards_dialog_and_tooltip_switch_without_decoding_again(self):
        selected = self.image_event()
        self.page.set_result(result((selected,), session_directory=self.directory))
        button = self.page.evidence.cards[0][2]
        dialog = self.page.evidence.open_event(selected)
        with patch("ui.evidence_viewer.read_scaled_image") as read:
            for language in ("ru", "kk"):
                language_manager.set_language(language)
                self.assertEqual(dialog.windowTitle(), tr("Evidence · {event}", event="Phone detected"))
                self.assertEqual(button.toolTip(), tr("Open evidence image"))
                self.assertTrue(dialog.isVisible())
                self.assertIsNotNone(dialog._source)
            read.assert_not_called()
        self.assertEqual(dialog.error, "")

    def test_evidence_error_is_localized_but_error_api_stays_canonical(self):
        selected = event(path=str(self.directory / "missing.jpg"))
        self.page.set_result(result((selected,), session_directory=self.directory))
        canonical = self.page.evidence.cards[0][3]
        button = self.page.evidence.cards[0][2]
        language_manager.set_language("kk")
        self.assertEqual(button.text(), tr("Evidence image is missing."))
        self.assertEqual(canonical, "Evidence image is missing.")
        self.assertEqual(self.page.evidence.open_event(selected).error, canonical)

    def test_light_report_uses_centralized_semantic_colors(self):
        self.page.set_result(result())
        self.assertIn(COLORS.background, STYLESHEET)
        self.assertIn(COLORS.surface, STYLESHEET)
        self.assertIn(RISK_COLORS["HIGH"], self.page.risk_level.styleSheet())
        for legacy in ("#101620", "#192230", "#ff955f", "#43c6a4"):
            self.assertNotIn(legacy, self.page.risk_level.styleSheet())
        self.assertNotEqual(COLORS.text, COLORS.surface)


class LocalizedTeacherReviewChecks(unittest.TestCase):
    def setUp(self):
        language_manager.set_language("en")
        self.page = TeacherReviewPage()

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()
        language_manager.set_language("en")

    def test_teacher_login_and_listing_switch_immediately(self):
        for language in ("ru", "kk", "en"):
            language_manager.set_language(language)
            self.assertEqual(self.page.pin_input.placeholderText(), tr("Teacher PIN"))
            self.assertEqual(self.page.unlock_button.text(), tr("UNLOCK TEACHER REVIEW"))
        self.page.show_sessions((summary(),))
        for language in ("ru", "kk", "en"):
            language_manager.set_language(language)
            self.assertEqual(self.page.sessions_model.headerData(4, Qt.Orientation.Horizontal), tr("Exam score"))
            self.assertEqual(self.page.sessions_model.data(self.page.sessions_model.index(0, 6)), tr("MODERATE"))
            self.assertIn("1", self.page.message.text())

    def test_teacher_student_names_and_opaque_ids_are_not_translated(self):
        self.page.show_sessions((summary(identity="LOW", student="Teacher Review"),))
        model = self.page.sessions_model
        for language in ("ru", "kk"):
            language_manager.set_language(language)
            self.assertEqual(model.data(model.index(0, 0)), "Teacher Review")
            self.assertEqual(model.data(model.index(0, 0), Qt.ItemDataRole.UserRole), "LOW")

    def test_teacher_pin_value_is_preserved_when_language_changes(self):
        self.page.pin_input.setText("123456")
        language_manager.set_language("kk")
        self.assertEqual(self.page.pin_input.text(), "123456")
        self.assertEqual(self.page.pin_input.echoMode(), self.page.pin_input.EchoMode.Password)

    def test_manual_review_and_omitted_session_diagnostics_localize(self):
        row = summary()
        row.exam_max_score = 0
        self.page.show_sessions((row,), ("diagnostic one", "diagnostic two", "diagnostic three", "diagnostic four"))
        language_manager.set_language("ru")
        self.assertEqual(self.page.sessions_model.data(self.page.sessions_model.index(0, 4)), "Ручная проверка")
        self.assertIn("Не удалось отобразить ещё 1", self.page.list_error.text())
        self.assertIn("diagnostic one", self.page.list_error.text())
        self.assertIn("Примечание к просмотру сеанса", self.page.list_error.text())


if __name__ == "__main__":
    unittest.main()
