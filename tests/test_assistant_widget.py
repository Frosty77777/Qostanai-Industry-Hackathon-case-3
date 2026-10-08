"""Offscreen assistant painting, controls, safe dragging and timer lifecycle."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import test_desktop as desktop
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QMouseEvent, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMenu, QWidget

from ui.assistant_model import AssistantConfig, AssistantPreferencesStore, AssistantState
from ui.assistant_widget import AssistantDock, AssistantWidget


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class AssistantWidgetChecks(unittest.TestCase):
    def setUp(self):
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
        desktop.APP.processEvents()
        self.temporary.cleanup()

    def dock(self, context="exam", width=800, **options):
        widget = AssistantDock(context=context, preferences=self.preferences,
                               config=self.config, clock=self.clock, **options)
        widget.resize(width, widget.height())
        widget.show()
        desktop.APP.processEvents()
        self.widgets.append(widget)
        return widget

    def assert_contained(self, widget):
        self.assertGreaterEqual(widget.assistant.x(), 0)
        self.assertGreaterEqual(widget.assistant.y(), 0)
        self.assertLessEqual(widget.assistant.geometry().right(), widget.arena.width() - 1)
        self.assertLessEqual(widget.assistant.geometry().bottom(), widget.arena.height() - 1)

    def test_setup_greeting_is_calm_and_readable(self):
        dock = self.dock("setup", width=300)
        self.assertEqual(dock.assistant.state, AssistantState.CALM)
        self.assertIn("Welcome to AI Exam Guard", dock.assistant.message_label.text())
        self.assertIn("Good luck", dock.assistant.message_label.text())
        self.assertTrue(dock.assistant.message_label.wordWrap())
        self.assertTrue(dock.assistant.message_label.accessibleName())
        self.assert_contained(dock)

    def test_missing_png_uses_static_placeholder(self):
        dock = self.dock()
        self.assertTrue(dock.assistant.portrait.uses_placeholder)
        self.assertFalse(dock.assistant.grab().isNull())

    def test_corrupt_png_uses_placeholder_without_crashing(self):
        self.config.asset_directory.mkdir()
        self.config.asset_path(AssistantState.CALM).write_bytes(b"not a PNG")
        dock = self.dock()
        self.assertTrue(dock.assistant.portrait.uses_placeholder)

    def test_transparent_local_png_can_replace_placeholder(self):
        self.config.asset_directory.mkdir()
        image = QPixmap(80, 80)
        image.fill(QColor("#90d9c0"))
        self.assertTrue(image.save(str(self.config.asset_path(AssistantState.CALM)), "PNG"))
        dock = self.dock()
        self.assertFalse(dock.assistant.portrait.uses_placeholder)

    def test_visual_state_follows_risk_while_event_prompt_is_active(self):
        dock = self.dock()
        dock.handle_event("PHONE_DETECTED")
        dock.set_risk(90)
        self.assertEqual(dock.assistant.state, AssistantState.CRITICAL)
        self.assertEqual(dock.assistant.message_label.text(), "Please put your phone away.")

    def test_event_message_expires_to_current_generic_message(self):
        dock = self.dock()
        dock.handle_event("FACE_MISSING")
        dock.set_risk(60)
        self.clock.advance(self.config.event_message_seconds)
        dock.refresh()
        self.assertEqual(dock.assistant.state, AssistantState.ALERT)
        self.assertEqual(dock.assistant.message_label.text(), self.config.state_messages[AssistantState.ALERT])

    def test_rapid_events_do_not_flicker_immediately(self):
        dock = self.dock()
        dock.handle_event("PHONE_DETECTED")
        self.clock.advance(0.1)
        dock.handle_event("FACE_MISSING")
        self.assertEqual(dock.assistant.message_label.text(), "Please put your phone away.")
        self.clock.advance(self.config.event_debounce_seconds)
        dock.refresh()
        self.assertEqual(dock.assistant.message_label.text(), "Please return to the camera view.")

    def test_unknown_event_does_not_schedule_updates(self):
        dock = self.dock()
        original = dock.assistant.message_label.text()
        self.assertFalse(dock.handle_event("UNKNOWN"))
        self.assertEqual(dock.assistant.message_label.text(), original)
        self.assertFalse(dock.timer.isActive())

    def test_static_generic_state_has_no_periodic_timer(self):
        dock = self.dock()
        dock.set_risk(50)
        self.assertFalse(dock.timer.isActive())
        self.assertTrue(dock.timer.isSingleShot())

    def test_event_uses_one_shot_timer_only_until_expiry(self):
        dock = self.dock()
        dock.handle_event("CAMERA_OBSTRUCTED")
        self.assertTrue(dock.timer.isActive())
        self.assertTrue(dock.timer.isSingleShot())
        self.clock.advance(self.config.event_message_seconds)
        dock.refresh()
        self.assertFalse(dock.timer.isActive())

    def test_hide_assistant_retains_accessible_options_and_reclaims_space(self):
        dock = self.dock()
        dock.show_assistant_action.setChecked(False)
        self.assertTrue(dock.arena.isHidden())
        self.assertTrue(dock.assistant.isHidden())
        self.assertTrue(dock.options_button.isVisible())
        self.assertEqual(dock.height(), 28)
        dock.show_assistant_action.setChecked(True)
        desktop.APP.processEvents()
        self.assertTrue(dock.assistant.isVisible())
        self.assertEqual(dock.height(), 96)
        self.assert_contained(dock)

    def test_options_are_inline_children_without_native_popup(self):
        dock = self.dock()
        self.assertIsNone(dock.options_button.menu())
        self.assertEqual(dock.findChildren(QMenu), [])
        dock.options_button.click()
        self.assertTrue(dock.options_panel.isVisible())
        self.assertFalse(dock.options_panel.isWindow())
        self.assertFalse(dock.show_assistant_checkbox.isWindow())
        self.assertEqual(dock.height(), 96)

    def test_hidden_assistant_can_be_restored_using_inline_options(self):
        dock = self.dock("setup", width=300)
        dock.set_visible(False)
        dock.options_button.click()
        self.assertTrue(dock.show_assistant_checkbox.isVisible())
        self.assertTrue(dock.show_messages_checkbox.isVisible())
        self.assertEqual(dock.height(), 52)
        dock.show_assistant_checkbox.setChecked(True)
        desktop.APP.processEvents()
        self.assertTrue(dock.assistant.isVisible())
        self.assertTrue(dock.show_assistant_action.isChecked())
        self.assertEqual(dock.height(), 230)
        self.assert_contained(dock)

    def test_inline_checkboxes_and_programmatic_actions_stay_synchronized(self):
        dock = self.dock()
        dock.show_messages_checkbox.setChecked(False)
        self.assertFalse(dock.show_messages_action.isChecked())
        self.assertTrue(dock.assistant.message_label.isHidden())
        dock.show_messages_action.setChecked(True)
        self.assertTrue(dock.show_messages_checkbox.isChecked())
        self.assertTrue(dock.assistant.message_label.isVisible())

    def test_hidden_assistant_stays_hidden_during_risk_and_events(self):
        dock = self.dock()
        dock.set_visible(False)
        dock.set_risk(100)
        dock.handle_event("PHONE_DETECTED")
        dock.set_break_active(True)
        dock.refresh()
        self.assertTrue(dock.assistant.isHidden())
        self.assertFalse(dock.show_assistant_action.isChecked())
        self.assertFalse(dock.timer.isActive())

    def test_message_can_be_hidden_independently(self):
        dock = self.dock()
        dock.assistant.set_message_visible(False)
        self.assertTrue(dock.assistant.isVisible())
        self.assertTrue(dock.assistant.message_label.isHidden())
        self.assertFalse(dock.show_messages_action.isChecked())
        self.assertEqual(dock.assistant.width(), 60)
        dock.handle_event("PHONE_DETECTED")
        self.assertTrue(dock.assistant.message_label.isHidden())

    def test_show_message_again_retains_latest_guidance(self):
        dock = self.dock()
        dock.set_message_visible(False)
        dock.handle_event("PHONE_DETECTED")
        dock.set_message_visible(True)
        self.assertTrue(dock.assistant.message_label.isVisible())
        self.assertEqual(dock.assistant.message_label.text(), "Please put your phone away.")
        self.assert_contained(dock)

    def test_preferences_reload_restores_hidden_assistant_and_message(self):
        dock = self.dock()
        dock.set_visible(False)
        dock.set_message_visible(False)
        reloaded = AssistantPreferencesStore(self.preferences.path)
        second = AssistantDock(context="exam", preferences=reloaded, config=self.config, clock=self.clock)
        self.widgets.append(second)
        second.show()
        desktop.APP.processEvents()
        self.assertTrue(second.assistant.isHidden())
        self.assertFalse(second.show_assistant_action.isChecked())
        self.assertFalse(second.show_messages_action.isChecked())

    def test_shared_preferences_immediately_update_both_page_widgets(self):
        setup = self.dock("setup", width=300)
        exam = self.dock()
        setup.set_message_visible(False)
        self.assertTrue(exam.assistant.message_label.isHidden())
        exam.set_visible(False)
        self.assertTrue(setup.assistant.isHidden())

    def test_page_hide_is_not_persisted_as_user_hiding_assistant(self):
        dock = self.dock()
        dock.hide()
        self.assertTrue(self.preferences.preferences.visible)
        self.assertFalse(self.preferences.path.exists())
        dock.show()
        self.assertTrue(dock.assistant.isVisible())

    def test_page_hide_stops_timer_and_show_expires_old_message(self):
        dock = self.dock()
        dock.handle_event("PHONE_DETECTED")
        dock.hide()
        self.assertFalse(dock.timer.isActive())
        self.clock.advance(self.config.event_message_seconds + 1)
        dock.show()
        desktop.APP.processEvents()
        self.assertEqual(dock.assistant.message_label.text(), self.config.state_messages[AssistantState.CALM])
        self.assertFalse(dock.timer.isActive())

    def test_pending_prompt_does_not_restart_after_hidden_page_resumes(self):
        dock = self.dock()
        dock.handle_event("PHONE_DETECTED")
        self.clock.advance(0.1)
        dock.handle_event("FACE_MISSING")
        dock.hide()
        self.clock.advance(self.config.event_message_seconds + self.config.event_debounce_seconds + 1)
        dock.show()
        desktop.APP.processEvents()
        self.assertEqual(dock.assistant.message_label.text(), self.config.state_messages[AssistantState.CALM])
        self.assertFalse(dock.timer.isActive())

    def test_failed_preference_save_does_not_reshow_hidden_assistant(self):
        blocker = self.directory / "blocked-parent"
        blocker.write_text("file prevents directory creation", encoding="utf-8")
        store = AssistantPreferencesStore(blocker / "assistant.json")
        dock = AssistantDock(context="exam", preferences=store, config=self.config, clock=self.clock)
        self.widgets.append(dock)
        dock.show()
        dock.set_visible(False)
        self.assertIsNotNone(store.error)
        dock.hide()
        dock.show()
        desktop.APP.processEvents()
        self.assertTrue(dock.assistant.isHidden())
        self.assertFalse(dock.show_assistant_action.isChecked())

    def test_stopping_cannot_be_rearmed_by_final_frame(self):
        dock = self.dock()
        dock.handle_event("PHONE_DETECTED")
        dock.set_stopping()
        dock.set_risk(90)
        dock.handle_event("FACE_MISSING")
        self.assertFalse(dock.timer.isActive())
        dock.reset()
        dock.handle_event("PHONE_DETECTED")
        self.assertTrue(dock.timer.isActive())

    def test_reset_preserves_preferences_and_clears_session_risk(self):
        dock = self.dock()
        dock.set_visible(False)
        dock.set_message_visible(False)
        dock.set_risk(90)
        dock.handle_event("PHONE_DETECTED")
        dock.reset()
        self.assertEqual(dock.assistant.state, AssistantState.CALM)
        self.assertTrue(dock.assistant.isHidden())
        self.assertTrue(dock.assistant.message_label.isHidden())
        self.assertFalse(dock.timer.isActive())

    def test_break_uses_supportive_tone_without_erasing_risk(self):
        dock = self.dock()
        dock.set_risk(90)
        dock.handle_event("FACE_MISSING")
        dock.set_break_active(True)
        self.assertEqual(dock.assistant.state, AssistantState.CRITICAL)
        self.assertIn("break", dock.assistant.message_label.text().lower())
        self.assertNotIn("camera view", dock.assistant.message_label.text())
        dock.set_break_active(False)
        self.assertEqual(dock.model.risk_score, 90)

    def test_drag_moves_image_and_message_together_and_saves_position(self):
        dock = self.dock(width=1000)
        original = dock.assistant.pos()
        point = QPoint(20, 20)
        QTest.mousePress(dock.assistant.portrait, Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseMove(dock.assistant.portrait, point + QPoint(160, 0))
        QTest.mouseRelease(dock.assistant.portrait, Qt.MouseButton.LeftButton, pos=point + QPoint(160, 0))
        self.assertGreater(dock.assistant.x(), original.x())
        self.assertIs(dock.assistant.message_label.parentWidget(), dock.assistant)
        self.assertTrue(self.preferences.path.exists())
        self.assertGreater(self.preferences.preferences.position("exam")[0], 0)
        self.assert_contained(dock)

    def test_drag_far_outside_lane_is_clamped(self):
        dock = self.dock(width=800)
        QTest.mousePress(dock.assistant.portrait, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(dock.assistant.portrait, QPoint(10000, 10000))
        QTest.mouseRelease(dock.assistant.portrait, Qt.MouseButton.LeftButton, pos=QPoint(10000, 10000))
        self.assert_contained(dock)

    def test_interrupted_drag_cancels_without_saving_or_stale_movement(self):
        dock = self.dock(width=1000)
        finished = []
        dock.assistant.drag_finished.connect(lambda: finished.append(True))
        for kind in (QEvent.Type.Hide, QEvent.Type.UngrabMouse, QEvent.Type.WindowDeactivate):
            with self.subTest(event=kind):
                original = dock.assistant.pos()
                QTest.mousePress(dock.assistant.portrait, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
                desktop.APP.sendEvent(dock.assistant.portrait, QEvent(kind))
                self.assertIsNone(dock.assistant._drag_origin)
                self.assertEqual(dock.assistant.cursor().shape(), Qt.CursorShape.OpenHandCursor)
                QTest.mouseMove(dock.assistant.portrait, QPoint(200, 20))
                QTest.mouseRelease(dock.assistant.portrait, Qt.MouseButton.LeftButton, pos=QPoint(200, 20))
                self.assertEqual(dock.assistant.pos(), original)
        self.assertEqual(finished, [])
        self.assertFalse(self.preferences.path.exists())

    def test_move_without_left_button_cancels_drag(self):
        dock = self.dock(width=1000)
        # Qt normally drops unpressed moves for non-tracking widgets. Enable
        # delivery here to exercise the lost-button cancellation path itself.
        dock.assistant.portrait.setMouseTracking(True)
        original = dock.assistant.pos()
        QTest.mousePress(dock.assistant.portrait, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        event = QMouseEvent(QEvent.Type.MouseMove, QPointF(200, 20), QPointF(200, 20),
                            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier)
        desktop.APP.sendEvent(dock.assistant.portrait, event)
        self.assertIsNone(dock.assistant._drag_origin)
        self.assertEqual(dock.assistant.pos(), original)
        QTest.mouseRelease(dock.assistant.portrait, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        self.assertFalse(self.preferences.path.exists())

    def test_right_button_release_does_not_finish_left_drag(self):
        dock = self.dock(width=1000)
        finished = []
        dock.assistant.drag_finished.connect(lambda: finished.append(True))
        QTest.mousePress(dock.assistant.portrait, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        event = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(20, 20), QPointF(20, 20),
                            Qt.MouseButton.RightButton, Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
        desktop.APP.sendEvent(dock.assistant.portrait, event)
        self.assertIsNotNone(dock.assistant._drag_origin)
        self.assertEqual(finished, [])
        QTest.mouseRelease(dock.assistant.portrait, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        self.assertEqual(finished, [True])

    def test_normalized_position_survives_reload_at_different_width(self):
        dock = self.dock(width=1000)
        dock.assistant.move(290, dock.assistant.y())
        dock.save_preferences()
        position = self.preferences.preferences.position("exam")
        reloaded = AssistantPreferencesStore(self.preferences.path)
        second = AssistantDock(context="exam", preferences=reloaded, config=self.config, clock=self.clock)
        second.resize(800, second.height())
        second.show()
        desktop.APP.processEvents()
        self.widgets.append(second)
        expected = round(position[0] * (second.arena.width() - second.assistant.width()))
        self.assertEqual(second.assistant.x(), expected)
        self.assert_contained(second)

    def test_setup_and_exam_positions_are_saved_separately(self):
        self.preferences.update(context="setup", position=(0.3, 0.7))
        dock = self.dock()
        dock.assistant.move(200, 0)
        dock.save_preferences()
        self.assertEqual(self.preferences.preferences.position("setup"), (0.3, 0.7))
        self.assertNotEqual(self.preferences.preferences.position("exam"), (0.3, 0.7))

    def test_resize_and_bubble_changes_keep_panel_in_reserved_area(self):
        dock = self.dock(width=1000)
        self.preferences.update(context="exam", position=(1.0, 1.0))
        dock.set_message_visible(False)
        dock.resize(600, dock.height())
        desktop.APP.processEvents()
        self.assert_contained(dock)
        dock.set_message_visible(True)
        self.assert_contained(dock)

    def test_dock_is_a_regular_child_widget_without_floating_window(self):
        page = QWidget()
        self.widgets.append(page)
        dock = AssistantDock(context="exam", parent=page, preferences=self.preferences, config=self.config)
        self.assertIs(dock.parentWidget(), page)
        self.assertFalse(dock.isWindow())
        self.assertFalse(dock.assistant.isWindow())
        self.assertFalse(dock.assistant.portrait.isWindow())

    def test_standalone_widget_exposes_reusable_display_methods(self):
        widget = AssistantWidget(config=self.config)
        self.widgets.append(widget)
        widget.set_state(AssistantState.ALERT)
        widget.set_message("Please stay focused.")
        widget.set_message_visible(False)
        widget.set_visible(False)
        self.assertEqual(widget.state, AssistantState.ALERT)
        self.assertEqual(widget.message_label.text(), "Please stay focused.")
        self.assertTrue(widget.message_label.isHidden())
        self.assertTrue(widget.isHidden())


if __name__ == "__main__":
    unittest.main()
