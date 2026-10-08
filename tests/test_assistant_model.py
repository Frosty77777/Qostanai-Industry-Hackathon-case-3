"""Mascot presentation/preferences never alter the monitoring backend."""

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ui.assistant_model import (AssistantConfig, AssistantModel, AssistantPreferences,
                                AssistantPreferencesStore, AssistantState,
                                MAX_PREFERENCES_BYTES, default_preferences_path)


class FakeClock:
    def __init__(self):
        self.time = 0.0

    def __call__(self):
        return self.time


class AssistantModelTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.model = AssistantModel("exam", clock=self.clock)

    def test_calm_range(self):
        for score in (0, 1, 20):
            with self.subTest(score=score):
                self.model.set_risk(score)
                self.assertEqual(self.model.state, AssistantState.CALM)

    def test_neutral_range(self):
        for score in (21, 30, 45):
            self.model.set_risk(score)
            self.assertEqual(self.model.state, AssistantState.NEUTRAL)

    def test_alert_range(self):
        for score in (46, 60, 70):
            self.model.set_risk(score)
            self.assertEqual(self.model.state, AssistantState.ALERT)

    def test_serious_range(self):
        for score in (71, 80, 85):
            self.model.set_risk(score)
            self.assertEqual(self.model.state, AssistantState.SERIOUS)

    def test_critical_range(self):
        for score in (86, 99, 100):
            self.model.set_risk(score)
            self.assertEqual(self.model.state, AssistantState.CRITICAL)

    def test_risk_presentation_clamps_out_of_range(self):
        self.model.set_risk(-20)
        self.assertEqual(self.model.risk_score, 0)
        self.model.set_risk(150)
        self.assertEqual(self.model.risk_score, 100)

    def test_non_integer_risk_rejected(self):
        for value in (True, 1.5, "45", None):
            with self.subTest(value=value), self.assertRaises(TypeError):
                self.model.set_risk(value)

    def test_default_setup_greeting(self):
        model = AssistantModel(clock=self.clock)
        self.assertEqual(model.state, AssistantState.CALM)
        self.assertEqual(model.current_message(), "Welcome to AI Exam Guard. Good luck on your exam.")
        self.assertIsNone(model.next_deadline)

    def test_setup_ignores_proctoring_events(self):
        self.model.reset("setup")
        greeting = self.model.current_message()
        self.assertFalse(self.model.handle_event("PHONE_DETECTED"))
        self.assertEqual(self.model.current_message(), greeting)

    def test_completion_message_contains_no_proctoring_details(self):
        self.model.reset("completion")
        self.model.set_risk(100)
        self.model.handle_event("PHONE_DETECTED")
        self.assertEqual(self.model.current_message(), "Your exam has been submitted successfully.")

    def test_phone_event_message(self):
        self.assertTrue(self.model.handle_event("PHONE_DETECTED"))
        self.assertEqual(self.model.current_message(), "Please put your phone away.")

    def test_face_missing_message(self):
        self.model.handle_event("FACE_MISSING")
        self.assertEqual(self.model.current_message(), "Please return to the camera view.")

    def test_camera_obstruction_message(self):
        self.model.handle_event("CAMERA_OBSTRUCTED")
        self.assertEqual(self.model.current_message(), "Please clear the camera view.")

    def test_focus_event_aliases_use_same_prompt(self):
        for kind in ("ALT_TAB_ATTEMPT", "WINDOW_FOCUS_LOST"):
            self.model.reset("exam")
            self.model.handle_event(kind)
            self.assertEqual(self.model.current_message(), "Leaving the exam window is not allowed.")

    def test_break_started_message(self):
        self.model.set_break_active(True)
        self.model.handle_event("AUTHORIZED_BREAK_STARTED")
        self.assertEqual(self.model.current_message(), "Authorized break started.")

    def test_break_ended_and_expired_messages(self):
        for kind in ("AUTHORIZED_BREAK_ENDED", "AUTHORIZED_BREAK_EXPIRED"):
            self.model.reset("exam")
            self.model.handle_event(kind)
            self.assertEqual(self.model.current_message(),
                             "Authorized break ended. Monitoring is active again.")

    def test_unknown_raw_states_cannot_select_prompt(self):
        for kind in ("CENTER", "UNKNOWN", "NO_FACE", "UNAVAILABLE", None, 2):
            self.assertFalse(self.model.handle_event(kind))
        self.assertIsNone(self.model.next_deadline)
        self.assertEqual(self.model.current_message(), self.model.config.state_messages[AssistantState.CALM])

    def test_event_object_uses_only_type_not_private_contents(self):
        event = SimpleNamespace(type="FACE_MISSING", message="PRIVATE INSTRUCTOR COMMENT",
                                evidence_path="private/session/image.jpg",
                                metadata={"answers": "secret"}, risk_delta=99)
        before = dict(vars(event))
        self.model.handle_event(event)
        self.assertEqual(self.model.current_message(), "Please return to the camera view.")
        self.assertEqual(vars(event), before)
        self.assertEqual(self.model.risk_score, 0)

    def test_event_prompt_expires_at_configured_deadline(self):
        self.model.set_risk(50)
        self.model.handle_event("PHONE_DETECTED")
        self.clock.time = 3.999
        self.assertEqual(self.model.current_message(), "Please put your phone away.")
        self.clock.time = 4.0
        self.assertTrue(self.model.advance())
        self.assertEqual(self.model.current_message(), self.model.config.state_messages[AssistantState.ALERT])
        self.assertIsNone(self.model.next_deadline)

    def test_risk_change_keeps_temporary_message_but_changes_visual(self):
        self.model.handle_event("PHONE_DETECTED")
        self.model.set_risk(90)
        self.assertEqual(self.model.state, AssistantState.CRITICAL)
        self.assertEqual(self.model.current_message(), "Please put your phone away.")
        self.clock.time = 4
        self.assertEqual(self.model.current_message(), self.model.config.state_messages[AssistantState.CRITICAL])

    def test_generic_text_does_not_change_within_same_risk_state(self):
        self.assertFalse(self.model.set_risk(10))
        self.assertFalse(self.model.set_risk(20))
        self.assertTrue(self.model.set_risk(21))
        self.assertFalse(self.model.set_risk(40))

    def test_rapid_events_coalesce_latest_pending_prompt(self):
        self.model.handle_event("PHONE_DETECTED")
        self.clock.time = .2
        self.assertFalse(self.model.handle_event("FACE_MISSING"))
        self.clock.time = .4
        self.assertFalse(self.model.handle_event("CAMERA_OBSTRUCTED"))
        self.assertEqual(self.model.current_message(), "Please put your phone away.")
        self.assertEqual(self.model.next_deadline, .75)
        self.clock.time = .75
        self.assertTrue(self.model.advance())
        self.assertEqual(self.model.current_message(), "Please clear the camera view.")
        self.assertEqual(self.model.next_deadline, 4.75)

    def test_identical_events_do_not_repeatedly_extend_prompt(self):
        self.model.handle_event("PHONE_DETECTED")
        for seconds in (.2, .8, 2.0, 3.5):
            self.clock.time = seconds
            self.assertFalse(self.model.handle_event("PHONE_DETECTED"))
            self.assertEqual(self.model.next_deadline, 4)
        self.clock.time = 4
        self.assertEqual(self.model.current_message(), self.model.config.state_messages[AssistantState.CALM])

    def test_later_event_after_debounce_displays_immediately(self):
        self.model.handle_event("PHONE_DETECTED")
        self.clock.time = .75
        self.assertTrue(self.model.handle_event("FACE_MISSING"))
        self.assertEqual(self.model.current_message(), "Please return to the camera view.")

    def test_latest_event_matching_visible_prompt_cancels_older_pending(self):
        self.model.handle_event("PHONE_DETECTED")
        self.clock.time = .2
        self.model.handle_event("FACE_MISSING")
        self.clock.time = .4
        self.model.handle_event("PHONE_DETECTED")
        self.assertEqual(self.model.next_deadline, 4)
        self.clock.time = .75
        self.assertFalse(self.model.advance())
        self.assertEqual(self.model.current_message(), "Please put your phone away.")

    def test_delayed_pending_event_expires_instead_of_reappearing_after_pause(self):
        self.model.set_risk(60)
        self.model.handle_event("PHONE_DETECTED")
        self.clock.time = .1
        self.model.handle_event("WINDOW_FOCUS_LOST")
        self.clock.time = 100
        self.model.advance()
        self.assertEqual(self.model.current_message(), self.model.config.state_messages[AssistantState.ALERT])
        self.assertEqual(self.model.risk_score, 60)
        self.assertIsNone(self.model.next_deadline)

    def test_delayed_pending_event_keeps_original_deadline_if_still_current(self):
        self.model.handle_event("PHONE_DETECTED")
        self.clock.time = .1
        self.model.handle_event("WINDOW_FOCUS_LOST")
        self.clock.time = 2
        self.model.advance()
        self.assertEqual(self.model.current_message(), "Leaving the exam window is not allowed.")
        self.assertEqual(self.model.next_deadline, 4.75)
        self.clock.time = 4.75
        self.assertEqual(self.model.current_message(), self.model.config.state_messages[AssistantState.CALM])

    def test_set_risk_reports_due_message_expiry_even_if_state_same(self):
        self.model.handle_event("PHONE_DETECTED")
        self.clock.time = 4
        self.assertTrue(self.model.set_risk(0))
        self.assertEqual(self.model.current_message(), self.model.config.state_messages[AssistantState.CALM])

    def test_deadline_helper_uses_injected_clock(self):
        self.assertIsNone(self.model.delay_until_deadline())
        self.model.handle_event("PHONE_DETECTED")
        self.clock.time = 1.25
        self.assertEqual(self.model.delay_until_deadline(), 2.75)
        self.clock.time = 6
        self.assertEqual(self.model.delay_until_deadline(), 0)

    def test_reset_clears_risk_break_and_pending_events(self):
        self.model.set_risk(80)
        self.model.handle_event("PHONE_DETECTED")
        self.clock.time = .1
        self.model.handle_event("FACE_MISSING")
        self.model.set_break_active(True)
        self.model.reset("exam")
        self.assertEqual(self.model.state, AssistantState.CALM)
        self.assertFalse(self.model.break_active)
        self.assertIsNone(self.model.next_deadline)
        self.assertEqual(self.model.current_message(), self.model.config.state_messages[AssistantState.CALM])

    def test_break_tone_is_informational_without_reducing_visual_risk(self):
        self.model.set_risk(90)
        self.model.handle_event("PHONE_DETECTED")
        self.model.set_break_active(True)
        self.assertEqual(self.model.state, AssistantState.CRITICAL)
        self.assertEqual(self.model.current_message(), self.model.config.break_message)
        self.assertFalse(self.model.handle_event("FACE_MISSING"))
        self.assertEqual(self.model.current_message(), self.model.config.break_message)

    def test_break_start_prompt_returns_to_break_generic(self):
        self.model.set_break_active(True)
        self.model.handle_event("AUTHORIZED_BREAK_STARTED")
        self.clock.time = 4
        self.assertEqual(self.model.current_message(), self.model.config.break_message)

    def test_break_ending_returns_to_current_risk_generic(self):
        self.model.set_risk(80)
        self.model.set_break_active(True)
        self.model.set_break_active(False)
        self.model.handle_event("AUTHORIZED_BREAK_EXPIRED")
        self.clock.time = 4
        self.assertEqual(self.model.state, AssistantState.SERIOUS)
        self.assertEqual(self.model.current_message(), self.model.config.state_messages[AssistantState.SERIOUS])

    def test_break_boolean_and_context_validation(self):
        with self.assertRaises(TypeError):
            self.model.set_break_active(1)
        with self.assertRaises(ValueError):
            self.model.reset("teacher")

    def test_clock_validation(self):
        self.model.advance()
        self.clock.time = -1
        with self.assertRaises(ValueError):
            self.model.advance()
        self.clock.time = float("nan")
        with self.assertRaises(ValueError):
            self.model.advance()


class AssistantConfigTests(unittest.TestCase):
    def test_all_asset_paths_are_centralized_and_files_optional(self):
        config = AssistantConfig(asset_directory=Path("missing-mascot-assets"))
        expected = ("calm.png", "neutral.png", "warning.png", "serious.png", "critical.png")
        for state, filename in zip(AssistantState, expected):
            self.assertEqual(config.asset_path(state), Path("missing-mascot-assets") / filename)

    def test_mapping_configuration_is_immutable(self):
        config = AssistantConfig()
        with self.assertRaises(TypeError):
            config.asset_files[AssistantState.CALM] = "other.png"
        with self.assertRaises(TypeError):
            config.state_messages[AssistantState.CALM] = "other"

    def test_invalid_configurations_are_rejected(self):
        for changes in ({"risk_boundaries": (20, 20, 70, 85)},
                        {"risk_boundaries": (20, 45, 70, 100)},
                        {"risk_boundaries": (20, 45, 70)},
                        {"event_message_seconds": 0}, {"event_debounce_seconds": -1},
                        {"event_message_seconds": float("inf")},
                        {"event_debounce_seconds": True}, {"asset_files": {}},
                        {"state_messages": {}}, {"setup_message": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                AssistantConfig(**changes)

    def test_unsafe_asset_filenames_rejected(self):
        for filename in ("../calm.png", "C:\\fake\\calm.png", "folder/calm.png", ""):
            files = dict(AssistantConfig().asset_files)
            files[AssistantState.CALM] = filename
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                AssistantConfig(asset_files=files)

    def test_customized_thresholds_and_timing(self):
        config = AssistantConfig(risk_boundaries=(10, 30, 60, 90),
                                 event_message_seconds=2, event_debounce_seconds=.1)
        clock = FakeClock()
        model = AssistantModel("exam", clock=clock, config=config)
        model.set_risk(11)
        self.assertEqual(model.state, AssistantState.NEUTRAL)
        model.handle_event("PHONE_DETECTED")
        self.assertEqual(model.next_deadline, 2)


class AssistantPreferencesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "settings" / "assistant.json"
        self.store = AssistantPreferencesStore(self.path)

    def write_raw(self, raw):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(raw), encoding="utf-8")

    def test_default_visibility_and_loading_do_not_create_files(self):
        self.assertTrue(self.store.preferences.visible)
        self.assertTrue(self.store.preferences.message_visible)
        self.store.load()
        self.assertFalse(self.path.exists())
        self.assertFalse(self.path.parent.exists())

    def test_visibility_and_messages_persist_independently(self):
        self.assertTrue(self.store.update(message_visible=False))
        self.assertTrue(self.store.preferences.visible)
        self.assertFalse(self.store.preferences.message_visible)
        self.assertTrue(self.store.update(visible=False))
        reloaded = AssistantPreferencesStore(self.path).preferences
        self.assertFalse(reloaded.visible)
        self.assertFalse(reloaded.message_visible)

    def test_normalized_positions_survive_reload(self):
        self.store.update(context="setup", position=(.25, .75))
        self.store.update(context="exam", position=(.8, .1))
        reloaded = AssistantPreferencesStore(self.path).preferences
        self.assertEqual(reloaded.position("setup"), (.25, .75))
        self.assertEqual(reloaded.position("exam"), (.8, .1))

    def test_context_positions_do_not_overwrite_each_other(self):
        self.store.update(context="setup", position=(0, 1))
        self.store.update(context="exam", position=(1, 0))
        self.store.update(visible=False)
        self.assertEqual(dict(self.store.preferences.positions), {"setup": (0, 1), "exam": (1, 0)})

    def test_position_default_does_not_write_preferences(self):
        self.assertEqual(self.store.preferences.position("exam", default=(.5, .5)), (.5, .5))
        self.assertFalse(self.path.exists())

    def test_hidden_preference_remains_hidden_after_model_events_and_reset(self):
        self.store.update(visible=False)
        model = AssistantModel("exam", clock=FakeClock())
        model.set_risk(100)
        model.handle_event("PHONE_DETECTED")
        model.reset("setup")
        self.assertFalse(self.store.preferences.visible)
        self.assertFalse(AssistantPreferencesStore(self.path).preferences.visible)

    def test_corrupt_json_loads_safe_defaults_without_overwriting_file(self):
        self.path.parent.mkdir()
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertLogs("ui.assistant_model", level="WARNING"):
            store = AssistantPreferencesStore(self.path)
        self.assertEqual(store.preferences, AssistantPreferences())
        self.assertIn("load failed", store.error)
        self.assertEqual(self.path.read_text(), "{not json")

    def test_invalid_field_values_fall_back_safely(self):
        self.write_raw({"visible": "false", "message_visible": 0,
                        "positions": {"setup": [3, -1], "exam": [.4, .7],
                                      "teacher": [.5, .5], "completion": [True, 0]}})
        preferences = self.store.load()
        self.assertTrue(preferences.visible)
        self.assertTrue(preferences.message_visible)
        self.assertEqual(dict(preferences.positions), {"exam": (.4, .7)})

    def test_nonfinite_positions_ignored(self):
        self.write_raw({"positions": {"exam": [float("nan"), .2],
                                      "setup": [float("inf"), .2]}})
        self.assertEqual(dict(self.store.load().positions), {})

    def test_unknown_json_keys_cannot_enter_preferences(self):
        self.write_raw({"visible": False, "risk": 100, "events": ["secret"], "evidence": "private"})
        self.assertEqual(self.store.load(), AssistantPreferences(visible=False))

    def test_oversized_settings_ignored(self):
        self.path.parent.mkdir()
        self.path.write_text(" " * (MAX_PREFERENCES_BYTES + 1))
        with self.assertLogs("ui.assistant_model", level="WARNING"):
            self.store.load()
        self.assertIn("size limit", self.store.error)

    def test_unsupported_version_ignored(self):
        self.write_raw({"version": 99, "visible": False})
        with self.assertLogs("ui.assistant_model", level="WARNING"):
            preferences = self.store.load()
        self.assertTrue(preferences.visible)

    def test_unwritable_path_keeps_runtime_preference_without_crash(self):
        observer = Mock()
        self.store.subscribe(observer)
        with patch.object(Path, "mkdir", side_effect=PermissionError("read only")):
            with self.assertLogs("ui.assistant_model", level="WARNING"):
                self.assertFalse(self.store.update(visible=False))
        self.assertFalse(self.store.preferences.visible)
        observer.assert_called_once_with(self.store.preferences)
        self.assertIn("PermissionError", self.store.error)

    def test_failed_save_preserves_hidden_preferences_on_same_store_reload(self):
        with patch.object(Path, "mkdir", side_effect=PermissionError("read only")):
            with self.assertLogs("ui.assistant_model", level="WARNING"):
                self.assertFalse(self.store.update(visible=False, message_visible=False))
        error = self.store.error
        self.assertFalse(self.store.load().visible)
        self.assertFalse(self.store.load().message_visible)
        self.assertEqual(self.store.error, error)
        self.assertFalse(self.path.exists())
        self.assertTrue(AssistantPreferencesStore(self.path).preferences.visible)

    def test_successful_retry_persists_pending_preferences_and_allows_reload(self):
        with patch.object(Path, "mkdir", side_effect=PermissionError("read only")):
            with self.assertLogs("ui.assistant_model", level="WARNING"):
                self.assertFalse(self.store.update(visible=False))
        self.assertTrue(self.store.save())
        self.assertIsNone(self.store.error)
        self.assertFalse(AssistantPreferencesStore(self.path).preferences.visible)
        self.write_raw({"visible": True})
        self.assertTrue(self.store.load().visible)

    def test_atomic_write_failure_preserves_previous_file_and_cleans_temp(self):
        self.store.update(visible=True)
        previous = self.path.read_bytes()
        with patch("ui.assistant_model.os.replace", side_effect=OSError("replace unavailable")):
            with self.assertLogs("ui.assistant_model", level="WARNING"):
                self.assertFalse(self.store.update(visible=False))
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_observers_notify_only_on_changes_and_can_unsubscribe(self):
        callback = Mock()
        unsubscribe = self.store.subscribe(callback)
        self.store.update(visible=True)
        callback.assert_not_called()
        self.store.update(visible=False)
        callback.assert_called_once_with(self.store.preferences)
        unsubscribe()
        self.store.update(visible=True)
        self.assertEqual(callback.call_count, 1)

    def test_observer_failure_does_not_prevent_preference_save(self):
        self.store.subscribe(Mock(side_effect=RuntimeError("closed widget")))
        other = Mock()
        self.store.subscribe(other)
        with self.assertLogs("ui.assistant_model", level="ERROR"):
            self.assertTrue(self.store.update(visible=False))
        other.assert_called_once()
        self.assertFalse(AssistantPreferencesStore(self.path).preferences.visible)

    def test_loading_updates_shared_observers_without_writing(self):
        callback = Mock()
        self.store.subscribe(callback)
        self.write_raw({"visible": False, "message_visible": False})
        previous = self.path.read_bytes()
        self.store.load()
        callback.assert_called_once_with(self.store.preferences)
        self.assertEqual(self.path.read_bytes(), previous)

    def test_invalid_preferences_and_positions_rejected(self):
        for changes in ({"visible": 0}, {"message_visible": "true"},
                        {"positions": {"teacher": (0, 1)}},
                        {"positions": {"exam": (1.2, 0)}},
                        {"positions": {"exam": (float("nan"), 0)}},
                        {"positions": {"exam": (True, 0)}},
                        {"positions": {"exam": (0,)}}, {"positions": []}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                AssistantPreferences(**changes)

    def test_store_update_requires_context_with_position(self):
        for changes in ({"context": "exam"}, {"position": (0, 1)},
                        {"context": "teacher", "position": (0, 1)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.update(**changes)

    def test_default_path_uses_local_application_data(self):
        with patch.dict("ui.assistant_model.os.environ", {"LOCALAPPDATA": str(Path(self.temp.name))}):
            self.assertEqual(default_preferences_path(),
                             Path(self.temp.name) / "AIExamGuard" / "assistant.json")

    def test_save_format_contains_only_assistant_settings(self):
        self.store.update(visible=False, context="exam", position=(.2, .3))
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(raw), {"version", "visible", "message_visible", "positions"})
        self.assertEqual(raw["positions"], {"exam": [.2, .3]})

    def test_preferences_mapping_cannot_be_mutated(self):
        preferences = AssistantPreferences(positions={"exam": (.1, .2)})
        with self.assertRaises(TypeError):
            preferences.positions["exam"] = (.3, .4)


if __name__ == "__main__":
    unittest.main()
