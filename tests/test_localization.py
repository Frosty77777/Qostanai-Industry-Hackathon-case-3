"""Presentation localization keeps canonical records and arbitrary input intact."""

import ast
import gc
from pathlib import Path
import unittest
import weakref

import test_desktop as desktop
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QIcon, QPixmap
from i18n import (LANGUAGE_NAMES, SUPPORTED_LANGUAGES, LanguageManager, catalogue,
                  language_manager, missing_translations, tr)
from monitoring.event_engine import EventEngineConfig, EventType
from security.security_monitor import SecurityConfig
from ui.assistant_model import AssistantConfig
from ui.localized_widgets import (QAction, QCheckBox, QComboBox, QLabel, QLineEdit,
                                  QPlainTextEdit, QPushButton, QRadioButton, QToolButton)


class TranslationCatalogueChecks(unittest.TestCase):
    def tearDown(self):
        language_manager.set_language("en")

    def test_supported_language_names_are_native(self):
        self.assertEqual(SUPPORTED_LANGUAGES, ("en", "ru", "kk"))
        self.assertEqual(LANGUAGE_NAMES, {"en": "English", "ru": "Русский", "kk": "Қазақша"})

    def test_english_sources_preserve_existing_display(self):
        self.assertEqual(tr("LOW", language="en"), "LOW")
        self.assertEqual(tr("PHONE_DETECTED", language="en"), "PHONE_DETECTED")
        self.assertEqual(tr("Welcome to AI Exam Guard. Good luck on your exam.", language="en"),
                         "Welcome to AI Exam Guard. Good luck on your exam.")

    def test_russian_strings(self):
        self.assertEqual(tr("LOW", language="ru"), "НИЗКИЙ")
        self.assertEqual(tr("PHONE_DETECTED", language="ru"), "ОБНАРУЖЕН ТЕЛЕФОН")
        self.assertEqual(tr("Please put your phone away.", language="ru"), "Уберите телефон.")

    def test_kazakh_strings(self):
        self.assertEqual(tr("LOW", language="kk"), "ТӨМЕН")
        self.assertEqual(tr("PHONE_DETECTED", language="kk"), "ТЕЛЕФОН АНЫҚТАЛДЫ")
        self.assertEqual(tr("Please put your phone away.", language="kk"), "Телефоныңызды алып қойыңыз.")

    def test_all_registered_keys_have_all_languages_and_matching_placeholders(self):
        self.assertGreater(len(catalogue()), 200)
        self.assertEqual(missing_translations(), {})

    def test_student_widget_sources_and_explicit_templates_are_registered(self):
        methods = {"setText", "setPlaceholderText", "setToolTip", "setAccessibleName",
                   "setAccessibleDescription", "show_banner", "set_busy", "addItem",
                   "setMessage", "tr"}
        constructors = {"label", "card", "QLabel", "QPushButton", "QCheckBox",
                        "QRadioButton", "QAction"}
        directory = Path(__file__).resolve().parents[1] / "src" / "ui"
        missing = []
        for name in ("setup_page.py", "exam_page.py", "question_panel.py",
                     "main_window.py", "assistant_widget.py"):
            tree = ast.parse((directory / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                method = (node.func.attr if isinstance(node.func, ast.Attribute)
                          else node.func.id if isinstance(node.func, ast.Name) else "")
                if method not in methods | constructors:
                    continue
                first = node.args[0]
                if (isinstance(first, ast.Constant) and isinstance(first.value, str)
                        and any(character.isalpha() for character in first.value)
                        and first.value not in catalogue()):
                    missing.append((name, node.lineno, first.value))
        self.assertEqual(missing, [])

    def test_all_event_types_and_risk_levels_have_translations(self):
        for source in (*EventType, "LOW", "MODERATE", "HIGH", "CRITICAL"):
            for code in ("ru", "kk"):
                with self.subTest(source=source, language=code):
                    self.assertNotEqual(tr(source, language=code), source)

    def test_all_default_assistant_messages_are_translated(self):
        config = AssistantConfig()
        messages = (*config.state_messages.values(), *config.event_messages.values(),
                    config.setup_message, config.setup_reminder, config.break_message,
                    config.completion_message)
        for source in messages:
            for language in ("ru", "kk"):
                with self.subTest(source=source, language=language):
                    self.assertNotEqual(tr(source, language=language), source)

    def test_all_canonical_cv_and_security_event_messages_have_translations(self):
        messages = [rule.message for config in (EventEngineConfig(), SecurityConfig())
                    for rule in config.rules.values()]
        for source in messages:
            self.assertIn(source, catalogue())

    def test_semantic_template_values_translate_without_changing_input(self):
        values = {"score": 30, "level": "MODERATE"}
        self.assertEqual(tr("{score} / 100 · {level}", language="ru", **values),
                         "30 / 100 · УМЕРЕННЫЙ")
        self.assertEqual(values, {"score": 30, "level": "MODERATE"})

    def test_arbitrary_exam_name_is_preserved_in_template(self):
        source = "{exam} · Only choice questions contribute to the academic score."
        self.assertTrue(tr(source, language="ru", exam="LOW").startswith("LOW · "))
        self.assertTrue(tr(source, language="kk", exam="Teacher Review").startswith("Teacher Review · "))

    def test_registered_rendered_template_translates_only_the_sentence(self):
        self.assertEqual(tr("Saved locally: C:/student/LOW/session.json", language="ru"),
                         "Сохранено локально: C:/student/LOW/session.json")

    def test_event_detail_template_localizes_known_message_and_preserves_path(self):
        source = "{event} · {timestamp} (UTC+5)\n{severity} · {delta} risk\n{message}\n{path}"
        result = tr(source, language="kk", event="PHONE_DETECTED", timestamp="12:00:00",
                    severity="CRITICAL", delta="+30", message="Cell phone detected",
                    path="C:/evidence/PHONE_DETECTED.jpg")
        self.assertIn("Ұялы телефон анықталды", result)
        self.assertIn("C:/evidence/PHONE_DETECTED.jpg", result)

    def test_unknown_strings_and_technical_detail_remain_unchanged(self):
        for code in SUPPORTED_LANGUAGES:
            self.assertEqual(tr("Student's arbitrary answer: LOW", language=code),
                             "Student's arbitrary answer: LOW")
            self.assertEqual(tr("AttributeError: backend failed", language=code),
                             "AttributeError: backend failed")

    def test_invalid_language_rejected(self):
        with self.assertRaises(ValueError):
            tr("LOW", language="fr")
        with self.assertRaises(ValueError):
            LanguageManager("fr")


class LanguageManagerChecks(unittest.TestCase):
    def test_language_switch_notifies_once(self):
        manager = LanguageManager()
        changes = []

        def observe(code):
            changes.append(code)

        manager.subscribe(observe)
        self.assertTrue(manager.set_language("ru"))
        self.assertFalse(manager.set_language("ru"))
        self.assertTrue(manager.set_language("kk"))
        self.assertEqual(changes, ["ru", "kk"])

    def test_unsubscribe_prevents_future_notifications(self):
        manager = LanguageManager()
        changes = []

        def observe(code):
            changes.append(code)

        unsubscribe = manager.subscribe(observe)
        unsubscribe()
        manager.set_language("ru")
        self.assertEqual(changes, [])

    def test_language_observers_do_not_keep_pages_alive(self):
        manager = LanguageManager()

        class Page:
            def changed(self, language):
                pass

        page = Page()
        reference = weakref.ref(page)
        manager.subscribe(page.changed)
        del page
        gc.collect()
        self.assertIsNone(reference())
        manager.set_language("ru")
        self.assertEqual(manager._subscribers, {})

    def test_deleted_observer_is_removed_without_needing_a_language_change(self):
        manager = LanguageManager()

        class Page:
            def changed(self, language):
                pass

        page = Page()
        manager.subscribe(page.changed)
        self.assertEqual(len(manager._subscribers), 1)
        del page
        gc.collect()
        self.assertEqual(manager._subscribers, {})

    def test_observer_can_unsubscribe_another_during_notification(self):
        manager = LanguageManager()
        changes = []

        def first(language):
            changes.append(("first", language))
            unsubscribe_second()

        def second(language):
            changes.append(("second", language))

        manager.subscribe(first)
        unsubscribe_second = manager.subscribe(second)
        manager.set_language("ru")
        self.assertEqual(changes, [("first", "ru")])

    def test_invalid_language_does_not_change_current_setting(self):
        manager = LanguageManager("kk")
        with self.assertRaises(ValueError):
            manager.set_language("fr")
        self.assertEqual(manager.language, "kk")


class LocalizedWidgetChecks(unittest.TestCase):
    def setUp(self):
        language_manager.set_language("en")
        self.widgets = []

    def tearDown(self):
        language_manager.set_language("en")
        for widget in self.widgets:
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        desktop.APP.processEvents()

    def keep(self, widget):
        self.widgets.append(widget)
        return widget

    def test_labels_retranslate_immediately_and_keep_source(self):
        label = self.keep(QLabel("LOW"))
        language_manager.set_language("ru")
        self.assertEqual(label.text(), "НИЗКИЙ")
        self.assertEqual(label.source_text, "LOW")
        language_manager.set_language("kk")
        self.assertEqual(label.text(), "ТӨМЕН")
        language_manager.set_language("en")
        self.assertEqual(label.text(), "LOW")

    def test_buttons_checks_radios_toolbuttons_and_actions_retranslate(self):
        controls = [self.keep(cls("Show Assistant")) for cls in
                    (QPushButton, QCheckBox, QRadioButton, QAction)]
        tool = self.keep(QToolButton())
        tool.setText("Show Assistant")
        controls.append(tool)
        language_manager.set_language("ru")
        self.assertTrue(all(control.text() == "Показать помощника" for control in controls))

    def test_raw_label_preserves_arbitrary_student_content(self):
        label = self.keep(QLabel())
        label.setRawText("LOW")
        language_manager.set_language("ru")
        self.assertEqual(label.text(), "LOW")
        label.setText("LOW")
        self.assertEqual(label.text(), "НИЗКИЙ")

    def test_cleared_label_does_not_restore_stale_notice_on_language_switch(self):
        label = self.keep(QLabel("LOW"))
        label.clear()
        language_manager.set_language("ru")
        self.assertEqual(label.text(), "")
        self.assertEqual(label.source_text, "")

    def test_language_switch_keeps_current_camera_or_evidence_pixels(self):
        label = self.keep(QLabel("LOW"))
        picture = QPixmap(20, 12)
        picture.fill()
        label.setPixmap(picture)
        language_manager.set_language("ru")
        self.assertEqual(label.pixmap().size(), picture.size())
        self.assertEqual(label.text(), "")

    def test_formatted_label_switches_without_replaying_model_update(self):
        label = self.keep(QLabel())
        label.setMessage("{score} / 100 · {level}", score=30, level="MODERATE")
        language_manager.set_language("kk")
        self.assertEqual(label.text(), "30 / 100 · ОРТАША")
        self.assertEqual(label.canonical_text, "30 / 100 · MODERATE")

    def test_text_entry_contents_are_raw_but_placeholder_is_localized(self):
        edit = self.keep(QLineEdit("LOW"))
        edit.setPlaceholderText("Teacher PIN")
        language_manager.set_language("ru")
        self.assertEqual(edit.text(), "LOW")
        self.assertEqual(edit.placeholderText(), "PIN преподавателя")

    def test_written_answer_contents_are_raw_but_placeholder_is_localized(self):
        edit = self.keep(QPlainTextEdit())
        edit.setPlainText("LOW")
        edit.setPlaceholderText("Teacher PIN")
        language_manager.set_language("kk")
        self.assertEqual(edit.toPlainText(), "LOW")
        self.assertEqual(edit.placeholderText(), "Оқытушының PIN коды")

    def test_metadata_is_retranslated(self):
        label = self.keep(QLabel())
        label.setToolTip("Show Assistant")
        label.setAccessibleName("AI Proctor Assistant")
        language_manager.set_language("ru")
        self.assertEqual(label.toolTip(), "Показать помощника")
        self.assertEqual(label.accessibleName(), "ИИ-помощник наблюдения")

    def test_raw_accessibility_preserves_arbitrary_exam_names(self):
        label = self.keep(QLabel())
        label.setRawAccessibleName("LOW")
        label.setRawAccessibleDescription("Teacher Review")
        label.setRawToolTip("LOW")
        language_manager.set_language("ru")
        self.assertEqual(label.accessibleName(), "LOW")
        self.assertEqual(label.accessibleDescription(), "Teacher Review")
        self.assertEqual(label.toolTip(), "LOW")

    def test_combo_preserves_canonical_data_and_selection_without_signals(self):
        combo = self.keep(QComboBox())
        combo.addItem("LOW", "LOW")
        combo.addItem("HIGH", "HIGH")
        combo.setCurrentIndex(1)
        changes = []
        combo.currentIndexChanged.connect(changes.append)
        language_manager.set_language("ru")
        self.assertEqual(combo.currentText(), "ВЫСОКИЙ")
        self.assertEqual(combo.currentData(), "HIGH")
        self.assertEqual(combo.source_items, ("LOW", "HIGH"))
        self.assertEqual(changes, [])

    def test_raw_combo_exam_names_do_not_translate(self):
        combo = self.keep(QComboBox())
        combo.addRawItem("Teacher Review", "exam.json")
        language_manager.set_language("ru")
        self.assertEqual(combo.currentText(), "Teacher Review")
        self.assertEqual(combo.currentData(), "exam.json")

    def test_combo_icon_insert_remove_and_clear_keep_sources_aligned(self):
        combo = self.keep(QComboBox())
        combo.addItem(QIcon(), "LOW", "low")
        combo.insertItem(0, "HIGH", "high")
        combo.removeItem(1)
        language_manager.set_language("kk")
        self.assertEqual(combo.itemText(0), "ЖОҒАРЫ")
        self.assertEqual(combo.itemData(0), "high")
        combo.clear()
        self.assertEqual(combo.source_items, ())


if __name__ == "__main__":
    unittest.main()
