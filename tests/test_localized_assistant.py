"""Actual assistant views translate without changing monitoring or preferences."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_desktop as desktop
from test_assistant_widget import Clock
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QMenu, QWidget

from i18n import language_manager
from ui.assistant_model import AssistantConfig, AssistantPreferencesStore, AssistantState
from ui.assistant_widget import AssistantDock, AssistantPortrait
from ui.theme import ASSISTANT_MESSAGE_STYLE, ASSISTANT_OPTIONS_STYLE, COLORS


class LocalizedAssistantChecks(unittest.TestCase):
    def setUp(self):
        self.previous_language = language_manager.language
        language_manager.set_language("en")
        self.temporary = TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.preferences = AssistantPreferencesStore(self.directory / "assistant.json")
        self.clock = Clock()
        self.config = AssistantConfig(asset_directory=self.directory / "assets")
        self.widgets = []

    def tearDown(self):
        for widget in reversed(self.widgets):
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        desktop.APP.processEvents()
        language_manager.set_language(self.previous_language)
        self.temporary.cleanup()

    def dock(self, context="exam", parent=None):
        dock = AssistantDock(context=context, preferences=self.preferences,
                             config=self.config, clock=self.clock, parent=parent)
        self.widgets.append(dock)
        dock.resize(440, dock.height())
        dock.show()
        desktop.APP.processEvents()
        return dock

    def test_english_setup_greeting_is_preserved(self):
        dock = self.dock("setup")
        self.assertEqual(dock.assistant.message_label.text(),
                         "Welcome to AI Exam Guard. Good luck on your exam.")
        self.assertEqual(dock.assistant.state, AssistantState.CALM)

    def test_existing_setup_greeting_changes_immediately_to_russian(self):
        dock = self.dock("setup")
        language_manager.set_language("ru")
        self.assertEqual(dock.assistant.message_label.text(),
                         "Добро пожаловать в AI Exam Guard. Удачи на экзамене.")
        self.assertEqual(dock.model.current_message(), self.config.setup_message)

    def test_existing_setup_greeting_changes_immediately_to_kazakh(self):
        dock = self.dock("setup")
        language_manager.set_language("kk")
        self.assertEqual(dock.assistant.message_label.text(),
                         "AI Exam Guard жүйесіне қош келдіңіз. Емтиханда сәттілік.")
        self.assertEqual(dock.model.current_message(), self.config.setup_message)

    def test_event_prompt_switches_language_without_extending_deadline_or_altering_event(self):
        dock = self.dock()
        event = SimpleNamespace(type="PHONE_DETECTED", message="Teacher-only report details",
                                risk_delta=30, evidence_path="private/evidence.jpg")
        before = dict(vars(event))
        dock.handle_event(event)
        deadline = dock.model.next_deadline
        expected = {"ru": "Уберите телефон.", "kk": "Телефоныңызды алып қойыңыз.",
                    "en": "Please put your phone away."}
        for language, prompt in expected.items():
            language_manager.set_language(language)
            self.assertEqual(dock.assistant.message_label.text(), prompt)
            self.assertEqual(dock.model.next_deadline, deadline)
            self.assertEqual(dock.model.current_message(), "Please put your phone away.")
            self.assertNotIn("Teacher-only", dock.assistant.message_label.text())
        self.assertEqual(vars(event), before)

    def test_risk_states_and_score_stay_canonical_under_localization(self):
        dock = self.dock()
        language_manager.set_language("kk")
        for score, state in ((0, AssistantState.CALM), (35, AssistantState.NEUTRAL),
                             (60, AssistantState.ALERT), (80, AssistantState.SERIOUS),
                             (90, AssistantState.CRITICAL)):
            with self.subTest(score=score):
                dock.set_risk(score)
                self.assertEqual(dock.model.risk_score, score)
                self.assertEqual(dock.assistant.state, state)
                self.assertEqual(dock.model.current_message(), self.config.state_messages[state])
                self.assertNotEqual(dock.assistant.message_label.text(), dock.model.current_message())

    def test_authorized_break_text_localizes_and_still_suppresses_absence_prompt(self):
        dock = self.dock()
        dock.set_break_active(True)
        language_manager.set_language("ru")
        self.assertEqual(dock.assistant.message_label.text(),
                         "Разрешённый перерыв. Немного отдохните.")
        self.assertFalse(dock.handle_event("FACE_MISSING"))
        language_manager.set_language("kk")
        self.assertEqual(dock.assistant.message_label.text(),
                         "Рұқсат етілген үзіліс. Аздап демалыңыз.")
        self.assertTrue(dock.model.break_active)

    def test_expired_event_returns_to_localized_current_generic_state(self):
        dock = self.dock()
        dock.handle_event("FACE_MISSING")
        dock.set_risk(60)
        language_manager.set_language("ru")
        self.clock.advance(self.config.event_message_seconds)
        dock.refresh()
        self.assertEqual(dock.assistant.message_label.text(), "Обнаружена подозрительная активность.")
        self.assertEqual(dock.assistant.state, AssistantState.ALERT)

    def test_hidden_assistant_stays_hidden_during_language_switch(self):
        dock = self.dock()
        dock.set_visible(False)
        height = dock.height()
        for language in ("ru", "kk", "en"):
            language_manager.set_language(language)
            self.assertTrue(dock.assistant.isHidden())
            self.assertTrue(dock.arena.isHidden())
            self.assertFalse(self.preferences.preferences.visible)
            self.assertEqual(dock.height(), height)

    def test_hidden_messages_translate_without_becoming_visible(self):
        dock = self.dock()
        dock.set_message_visible(False)
        dock.handle_event("PHONE_DETECTED")
        language_manager.set_language("kk")
        self.assertEqual(dock.assistant.message_label.text(), "Телефоныңызды алып қойыңыз.")
        self.assertTrue(dock.assistant.message_label.isHidden())
        self.assertFalse(self.preferences.preferences.message_visible)

    def test_controls_created_in_russian_restore_canonical_english_sources(self):
        language_manager.set_language("ru")
        dock = self.dock()
        self.assertEqual(dock.show_assistant_checkbox.text(), "Показать помощника")
        self.assertEqual(dock.show_assistant_checkbox.accessibleName(), "Показать помощника")
        language_manager.set_language("en")
        self.assertEqual(dock.options_button.text(), "ASSISTANT OPTIONS")
        self.assertEqual(dock.show_assistant_checkbox.text(), "Show Assistant")
        self.assertEqual(dock.show_assistant_checkbox.accessibleName(), "Show Assistant")
        self.assertEqual(dock.show_messages_action.text(), "Show Assistant Messages")

    def test_portrait_accessibility_localizes_current_state_without_changing_it(self):
        dock = self.dock()
        dock.set_risk(60)
        language_manager.set_language("ru")
        self.assertIn("Внимательный", dock.assistant.portrait.accessibleDescription())
        self.assertEqual(dock.assistant.portrait.accessibleName(), "ИИ-помощник наблюдения")
        language_manager.set_language("kk")
        self.assertIn("Сақ", dock.assistant.portrait.accessibleDescription())
        self.assertEqual(dock.assistant.portrait.state.value, "ALERT")

    def test_message_and_container_accessibility_update_with_language(self):
        dock = self.dock()
        dock.handle_event("PHONE_DETECTED")
        language_manager.set_language("ru")
        self.assertEqual(dock.assistant.accessibleName(), "Перемещаемый ИИ-помощник")
        self.assertEqual(dock.assistant.message_label.accessibleName(), "Подсказки помощника")
        self.assertEqual(dock.assistant.message_label.accessibleDescription(), "Уберите телефон.")
        self.assertIn("помощника", dock.options_button.toolTip())

    def test_language_and_inline_options_preserve_parent_hwnd(self):
        page = QWidget()
        self.widgets.append(page)
        page.resize(640, 300)
        page.show()
        dock = self.dock(parent=page)
        handle = int(page.winId())
        for language in ("ru", "kk", "en"):
            language_manager.set_language(language)
            dock.options_button.click()
            desktop.APP.processEvents()
            self.assertEqual(int(page.winId()), handle)
            self.assertFalse(dock.isWindow())
            self.assertFalse(dock.assistant.isWindow())
            self.assertEqual(dock.findChildren(QMenu), [])

    def test_light_message_and_options_use_central_palette_styles(self):
        dock = self.dock()
        self.assertEqual(dock.assistant.message_label.styleSheet(), ASSISTANT_MESSAGE_STYLE)
        self.assertEqual(dock.options_button.styleSheet(), ASSISTANT_OPTIONS_STYLE)
        self.assertIn(COLORS.text, dock.show_assistant_checkbox.styleSheet())
        self.assertIn(COLORS.info_surface, dock.assistant.message_label.styleSheet())

    def test_placeholder_robot_body_uses_light_central_palette(self):
        portrait = AssistantPortrait(config=self.config)
        self.widgets.append(portrait)
        portrait.resize(100, 100)
        portrait.show()
        image = portrait.grab().toImage()
        self.assertEqual(image.pixelColor(50, 85), QColor(COLORS.robot_body))
        self.assertEqual(image.pixelColor(50, 37), QColor(COLORS.robot_face))

    def test_view_translation_does_not_write_preferences_or_change_position(self):
        dock = self.dock()
        position = dock.assistant.pos()
        with patch.object(self.preferences, "save") as save:
            for language in ("ru", "kk", "en"):
                language_manager.set_language(language)
        save.assert_not_called()
        self.assertEqual(dock.assistant.pos(), position)

    def test_language_switch_does_not_restart_a_stopped_message_timer(self):
        dock = self.dock()
        dock.handle_event("PHONE_DETECTED")
        self.assertTrue(dock.timer.isSingleShot())
        dock.set_stopping(True)
        deadline = dock.model.next_deadline
        language_manager.set_language("kk")
        self.assertFalse(dock.timer.isActive())
        self.assertEqual(dock.model.next_deadline, deadline)


if __name__ == "__main__":
    unittest.main()
