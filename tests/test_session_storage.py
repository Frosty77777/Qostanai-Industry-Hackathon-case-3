"""Deterministic local JSON tests; no camera, listener, or elapsed-time sleeps."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from monitoring import EventType, ProctoringEvent, Severity
from session_storage import SessionStorageConfig, SessionStorageError, SessionStore
import session_storage.session_store as storage_module


START = datetime(2026, 10, 8, 14, 32, 0, 481000, tzinfo=timezone(timedelta(hours=5)))


class SessionStoreChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config = SessionStorageConfig(self.root / "sessions")
        self.store = SessionStore(self.config)

    def result(self, directory=None, events=()):
        return SimpleNamespace(
            student="Demo Student", exam="Local Exam", started_at=START,
            ended_at=START + timedelta(seconds=123), duration_seconds=123.25,
            risk_score=43, risk_level="MODERATE", events=tuple(events),
            event_counts={EventType.PHONE_DETECTED: 1, EventType.AUTHORIZED_BREAK_STARTED: 1},
            error=None, persistence_error=None,
            session_directory=directory or self.store.create_session("Demo Student", START),
        )

    def event(self, kind=EventType.PHONE_DETECTED, *, path=None, delta=30):
        severity = Severity.INFO if kind == EventType.AUTHORIZED_BREAK_STARTED else Severity.HIGH
        return ProctoringEvent(
            kind, START, severity, 1.2, "Suspicious event — review required",
            confidence=0.92 if kind == EventType.PHONE_DETECTED else None,
            metadata={"source": "synthetic", "nested": {"value": 2}},
            evidence_path=path, risk_delta=delta,
        )

    def read(self, result, filename):
        return json.loads((result.session_directory / filename).read_text(encoding="utf-8"))

    def test_readable_timestamp_student_directory_created(self):
        directory = self.store.create_session("Demo Student", START)
        self.assertEqual(directory.name, "20261008_143200_481_Demo_Student")
        self.assertEqual(directory.parent, self.config.directory)
        self.assertTrue((directory / "evidence").is_dir())

    def test_collision_safe_directories_and_preserved_previous_content(self):
        first = self.store.create_session("Student", START)
        (first / "evidence" / "old.jpg").write_bytes(b"previous evidence")
        second = self.store.create_session("Student", START)
        third = SessionStore(self.config).create_session("Student", START)
        self.assertEqual(second.name, first.name + "_001")
        self.assertEqual(third.name, first.name + "_002")
        self.assertEqual((first / "evidence" / "old.jpg").read_bytes(), b"previous evidence")

    def test_student_names_cannot_escape_root_or_use_windows_unsafe_components(self):
        for student in ("../..", "C:\\evil\\path", "../../other", "CON", "NUL.txt", "COM1",
                        "LPT9", "a<>:\"/\\|?*b\x00\n", ". .", "x" * 300):
            with self.subTest(student=student):
                directory = self.store.create_session(student, START)
                self.assertEqual(directory.parent, self.config.directory)
                self.assertNotRegex(directory.name, r'[<>:"/\\|?*\x00-\x1f]')
                self.assertEqual(directory.name, directory.name.rstrip(" ."))
                self.assertLess(len(directory.name), 110)

    def test_blank_student_gets_readable_fallback(self):
        directory = self.store.create_session(" ... ", START)
        self.assertTrue(directory.name.endswith("_Student"))

    def test_unicode_student_and_event_text_are_preserved(self):
        result = self.result(directory=self.store.create_session("Әлия Иванова", START),
                             events=[self.event()])
        self.store.save_session(result)
        self.assertIn("Әлия_Иванова", result.session_directory.name)
        self.assertIn("— review required", self.read(result, "events.json")[0]["message"])

    def test_session_json_contains_complete_metadata(self):
        result = self.result()
        self.store.save_session(result)
        metadata = self.read(result, "session.json")
        self.assertEqual(metadata["student"], result.student)
        self.assertEqual(metadata["exam"], result.exam)
        self.assertEqual(metadata["started_at"], START.isoformat())
        self.assertEqual(metadata["ended_at"], result.ended_at.isoformat())
        self.assertEqual(metadata["duration_seconds"], 123.25)
        self.assertEqual(metadata["risk_score"], 43)
        self.assertEqual(metadata["risk_level"], "MODERATE")
        self.assertEqual(metadata["event_counts"], {"PHONE_DETECTED": 1, "AUTHORIZED_BREAK_STARTED": 1})
        self.assertEqual(metadata["schema_version"], 1)
        self.assertIsNone(metadata["error"])

    def test_events_json_preserves_all_fields_and_full_security_break_history(self):
        events = [self.event(), self.event(EventType.ALT_TAB_ATTEMPT, delta=15),
                  self.event(EventType.AUTHORIZED_BREAK_STARTED, delta=0)]
        result = self.result(events=events)
        self.store.save_session(result)
        saved = self.read(result, "events.json")
        self.assertEqual(saved, [event.to_dict() for event in events])
        self.assertEqual(saved[-1]["risk_delta"], 0)
        self.assertIsNone(saved[1]["evidence_path"])
        self.assertEqual(result.events, tuple(events))

    def test_empty_history_saved_as_json_list(self):
        result = self.result()
        self.store.save_session(result)
        self.assertEqual(self.read(result, "events.json"), [])

    def test_managed_absolute_evidence_path_is_session_relative(self):
        directory = self.store.create_session("Student", START)
        path = directory / "evidence" / "PHONE_DETECTED.jpg"
        original = self.event(path=path.as_posix())
        result = self.result(directory, [original])
        self.store.save_session(result)
        self.assertEqual(self.read(result, "events.json")[0]["evidence_path"], "evidence/PHONE_DETECTED.jpg")
        self.assertEqual(original.evidence_path, path.as_posix())

    def test_project_relative_evidence_path_is_session_relative(self):
        directory = self.store.create_session("Student", START)
        path = (directory / "evidence" / "snapshot.jpg").relative_to(self.root).as_posix()
        result = self.result(directory, [self.event(path=path)])
        with patch.object(storage_module, "ROOT", self.root):
            self.store.save_session(result)
        self.assertEqual(self.read(result, "events.json")[0]["evidence_path"], "evidence/snapshot.jpg")

    def test_already_portable_evidence_path_remains_portable(self):
        result = self.result(events=[self.event(path="evidence/snapshot.jpg")])
        self.store.save_session(result)
        self.assertEqual(self.read(result, "events.json")[0]["evidence_path"], "evidence/snapshot.jpg")

    def test_external_evidence_path_is_preserved(self):
        external = (self.root / "elsewhere" / "snapshot.jpg").as_posix()
        result = self.result(events=[self.event(path=external)])
        self.store.save_session(result)
        self.assertEqual(self.read(result, "events.json")[0]["evidence_path"], external)

    def test_completed_session_cannot_be_overwritten(self):
        result = self.result(events=[self.event()])
        self.store.save_session(result)
        original = (result.session_directory / "session.json").read_bytes()
        result.student = "Different Student"
        with self.assertRaisesRegex(SessionStorageError, "refusing to overwrite"):
            self.store.save_session(result)
        self.assertEqual((result.session_directory / "session.json").read_bytes(), original)
        self.assertFalse((result.session_directory / ".save.lock").exists())

    def test_distinct_completed_sessions_have_independent_json_and_evidence(self):
        first = self.result(events=[self.event()])
        second = self.result(events=[])
        self.store.save_session(first)
        self.store.save_session(second)
        self.assertNotEqual(first.session_directory, second.session_directory)
        self.assertEqual(len(self.read(first, "events.json")), 1)
        self.assertEqual(self.read(second, "events.json"), [])

    def test_failed_creation_is_clean_storage_exception(self):
        with patch.object(Path, "mkdir", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(SessionStorageError, "denied"):
                self.store.create_session("Student", START)

    def test_failed_image_subdirectory_creation_removes_only_new_empty_folder(self):
        actual_mkdir = Path.mkdir

        def mkdir(path, *args, **kwargs):
            if path.name == "evidence":
                raise OSError("evidence directory unavailable")
            return actual_mkdir(path, *args, **kwargs)

        with patch.object(Path, "mkdir", new=mkdir):
            with self.assertRaises(SessionStorageError):
                self.store.create_session("Student", START)
        self.assertEqual(list(self.config.directory.iterdir()), [])

    def test_write_failure_preserves_in_memory_result_and_no_completed_json(self):
        result = self.result(events=[self.event()])
        original_events = result.events
        with patch.object(json, "dump", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(SessionStorageError, "disk full"):
                self.store.save_session(result)
        self.assertIs(result.events, original_events)
        self.assertEqual(list(result.session_directory.iterdir()), [result.session_directory / "evidence"])

    def test_json_files_are_flushed_before_publish_with_session_marker_last(self):
        result = self.result()
        calls = []
        actual_fsync = os.fsync
        actual_replace = os.replace

        def fsync(descriptor):
            calls.append("flush")
            return actual_fsync(descriptor)

        def publish(source, target):
            calls.append(Path(target).name)
            self.assertFalse((result.session_directory / "session.json").exists())
            return actual_replace(source, target)

        with patch.object(os, "fsync", side_effect=fsync), patch.object(os, "replace", side_effect=publish):
            self.store.save_session(result)
        self.assertEqual(calls, ["flush", "flush", "events.json", "session.json"])

    def test_failed_final_publish_removes_partial_history_and_allows_retry(self):
        result = self.result(events=[self.event()])
        actual_replace = os.replace

        def publish(source, target):
            if Path(target).name == "session.json":
                raise OSError("final metadata unavailable")
            return actual_replace(source, target)

        with patch.object(os, "replace", side_effect=publish):
            with self.assertRaises(SessionStorageError):
                self.store.save_session(result)
        self.assertFalse((result.session_directory / "events.json").exists())
        self.assertFalse((result.session_directory / "session.json").exists())
        self.store.save_session(result)
        self.assertEqual(len(self.read(result, "events.json")), 1)

    def test_other_writer_lock_is_never_removed(self):
        result = self.result()
        lock = result.session_directory / ".save.lock"
        lock.write_text("another writer owns this", encoding="utf-8")
        with self.assertRaises(SessionStorageError):
            self.store.save_session(result)
        self.assertEqual(lock.read_text(encoding="utf-8"), "another writer owns this")

    def test_storage_root_or_external_directory_rejected(self):
        for directory in (self.config.directory, self.root):
            result = self.result(directory=directory)
            with self.subTest(directory=directory), self.assertRaises(SessionStorageError):
                self.store.save_session(result)

    def test_naive_or_missing_timestamps_rejected(self):
        with self.assertRaisesRegex(SessionStorageError, "timezone-aware"):
            self.store.create_session("Student", datetime(2026, 10, 8))
        result = self.result()
        result.ended_at = None
        with self.assertRaisesRegex(SessionStorageError, "timezone-aware"):
            self.store.save_session(result)
        self.assertFalse((result.session_directory / "session.json").exists())

    def test_nonfinite_json_values_fail_without_committing(self):
        result = self.result()
        result.duration_seconds = float("nan")
        with self.assertRaises(SessionStorageError):
            self.store.save_session(result)
        self.assertFalse((result.session_directory / "session.json").exists())
        self.assertFalse((result.session_directory / "events.json").exists())

    def test_session_error_is_preserved(self):
        result = self.result()
        result.error = "Camera read failed"
        self.store.save_session(result)
        self.assertEqual(self.read(result, "session.json")["error"], "Camera read failed")

    def test_disabled_storage_does_not_write(self):
        disabled = SessionStore(replace(self.config, enabled=False))
        with self.assertRaisesRegex(SessionStorageError, "disabled"):
            disabled.create_session("Student", START)
        disabled.save_session(SimpleNamespace())
        self.assertFalse(self.config.directory.exists())

    def test_config_frozen_and_directory_normalized(self):
        config = SessionStorageConfig(str(self.root / "sessions"))
        self.assertIsInstance(config.directory, Path)
        with self.assertRaises(FrozenInstanceError):
            config.enabled = False
        with self.assertRaises(ValueError):
            SessionStorageConfig(enabled="yes")


if __name__ == "__main__":
    unittest.main()
