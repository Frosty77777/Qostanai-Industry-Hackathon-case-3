"""Read-only completed-report round trips; no webcam, keyboard or network."""

from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_exam_report_storage import START, academic_result
from monitoring import EventType, ProctoringEvent, Severity
from session_storage import SessionStorageConfig, SessionStorageError, SessionStore
from session_storage.session_repository import (
    LocalSessionRepository, RepositoryLimits, SessionRepository,
)
from ui.session import SessionResult


class SessionRepositoryChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = SessionStorageConfig(self.root / "sessions")
        self.store = SessionStore(self.config)
        self.repository = LocalSessionRepository(self.config)

    def make(self, *, student="Student", started=START, academic=True, events=None):
        directory = self.store.create_session(student, started)
        image = directory / "evidence" / "PHONE_DETECTED.jpg"
        image.write_bytes(b"preserved evidence image")
        if events is None:
            events = (
                ProctoringEvent(EventType.PHONE_DETECTED, started + timedelta(seconds=1), Severity.CRITICAL,
                                0.82, "Cell phone detected", 0.92, {"nested": {"value": 1}}, str(image), 30),
                ProctoringEvent(EventType.ALT_TAB_ATTEMPT, started + timedelta(seconds=2), Severity.HIGH,
                                0, "Alt+Tab attempt detected", metadata={"detected": True}, risk_delta=15),
                ProctoringEvent(EventType.AUTHORIZED_BREAK_STARTED, started + timedelta(seconds=3), Severity.INFO,
                                0, "Authorized break started", metadata={"minutes": 1}, risk_delta=0),
            )
        result = SessionResult(student, "Local Academic Demo", 123.0, 45, "MODERATE", tuple(events),
                               {event.type: 1 for event in events}, started_at=started,
                               ended_at=started + timedelta(seconds=123), session_directory=directory,
                               exam_result=academic_result() if academic else None)
        self.store.save_session(result)
        return result

    def metadata(self, result):
        return json.loads((result.session_directory / "session.json").read_text(encoding="utf-8"))

    def history(self, result):
        return json.loads((result.session_directory / "events.json").read_text(encoding="utf-8"))

    def edit(self, result, filename, data):
        (result.session_directory / filename).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_real_writer_round_trip_recreates_full_normalized_report(self):
        source = self.make()
        loaded = self.repository.load(source.session_directory.name)
        self.assertIsInstance(loaded, SessionResult)
        self.assertEqual(loaded.student, source.student)
        self.assertEqual(loaded.exam, source.exam)
        self.assertEqual(loaded.started_at, source.started_at)
        self.assertEqual(loaded.ended_at, source.ended_at)
        self.assertEqual(loaded.duration_seconds, source.duration_seconds)
        self.assertEqual(loaded.risk_score, source.risk_score)
        self.assertEqual(loaded.risk_level, source.risk_level)
        self.assertEqual(loaded.event_counts, source.event_counts)
        self.assertEqual(loaded.exam_result.to_dict(), source.exam_result.to_dict())
        self.assertEqual([event.to_dict() for event in loaded.events], [event.to_dict() for event in source.events])
        self.assertEqual(self.repository.errors, ())

    def test_completed_summary_keeps_academic_score_separate_from_risk(self):
        source = self.make(student="Әлия")
        summary, = self.repository.list_completed()
        self.assertEqual(summary.session_id, source.session_directory.name)
        self.assertEqual(summary.student, "Әлия")
        self.assertEqual(summary.exam, source.exam)
        self.assertEqual((summary.exam_score, summary.exam_max_score), (2, 3))
        self.assertEqual((summary.risk_score, summary.risk_level), (45, "MODERATE"))
        self.assertEqual(summary.started_at, START)
        self.assertEqual(summary.duration_seconds, 123)
        with self.assertRaises(FrozenInstanceError):
            summary.risk_score = 100

    def test_completed_sessions_sorted_newest_first_and_open_correct_student(self):
        older = self.make(student="Older", started=START)
        newer = self.make(student="Newer", started=START + timedelta(days=1))
        summaries = self.repository.list_completed()
        self.assertEqual([summary.student for summary in summaries], ["Newer", "Older"])
        self.assertEqual(self.repository.load(summaries[0].session_id).session_directory, newer.session_directory)
        self.assertEqual(self.repository.load(summaries[1].session_id).student, older.student)

    def test_reader_never_modifies_previous_json_evidence_or_directory_contents(self):
        first = self.make(student="First")
        self.make(student="Second", academic=False)
        before = {path.relative_to(self.config.directory): path.read_bytes()
                  for path in self.config.directory.rglob("*") if path.is_file()}
        with patch.object(SessionStore, "save_session", side_effect=AssertionError("reader must not save")):
            self.repository.list_completed()
            self.repository.load(first.session_directory.name)
        after = {path.relative_to(self.config.directory): path.read_bytes()
                 for path in self.config.directory.rglob("*") if path.is_file()}
        self.assertEqual(after, before)

    def test_legacy_session_without_exam_result_remains_available(self):
        source = self.make(academic=False)
        metadata = self.metadata(source)
        metadata.pop("exam_result")
        metadata.pop("persistence_error")
        self.edit(source, "session.json", metadata)
        summary, = self.repository.list_completed()
        self.assertIsNone(summary.exam_score)
        self.assertIsNone(summary.exam_max_score)
        loaded = self.repository.load(summary.session_id)
        self.assertIsNone(loaded.exam_result)
        self.assertEqual(len(loaded.events), 3)

    def test_missing_storage_root_is_empty_without_creating_directories(self):
        self.assertEqual(self.repository.list_completed(), ())
        self.assertEqual(self.repository.errors, ())
        self.assertFalse(self.config.directory.exists())

    def test_storage_disabled_for_writing_can_still_review_prior_completed_sessions(self):
        self.make()
        reader = LocalSessionRepository(replace(self.config, enabled=False))
        self.assertEqual(len(reader.list_completed()), 1)

    def test_corrupt_or_incomplete_sessions_skipped_with_diagnostics(self):
        good = self.make(student="Good")
        corrupt = self.make(student="Corrupt")
        (corrupt.session_directory / "session.json").write_text("{broken", encoding="utf-8")
        incomplete = self.store.create_session("Incomplete", START)
        summaries = self.repository.list_completed()
        self.assertEqual([item.session_id for item in summaries], [good.session_directory.name])
        self.assertTrue(any(corrupt.session_directory.name in text for text in self.repository.errors))
        self.assertTrue(any(incomplete.name in text for text in self.repository.errors))

    def test_missing_events_json_is_incomplete_and_cannot_be_loaded(self):
        source = self.make()
        (source.session_directory / "events.json").unlink()
        self.assertEqual(self.repository.list_completed(), ())
        with self.assertRaises(SessionStorageError):
            self.repository.load(source.session_directory.name)
        self.assertIn("cannot open", self.repository.errors[-1])

    def test_locked_session_is_not_opened_while_publication_in_progress(self):
        source = self.make()
        (source.session_directory / ".save.lock").write_text("writer active", encoding="utf-8")
        self.assertEqual(self.repository.list_completed(), ())
        self.assertIn("in progress", self.repository.errors[0])
        self.assertTrue((source.session_directory / ".save.lock").exists())

    def test_arbitrary_absolute_traversal_network_and_drive_ids_rejected(self):
        self.make()
        for session_id in ("", ".", "..", "../other", "a/b", "a\\b", "C:\\other",
                           "C:other", "\\\\server\\share", str(self.root), "bad\x00name", "bad\nname",
                           "name.", "name ", "bad*name", "bad?name", None):
            with self.subTest(session_id=session_id), self.assertRaises(SessionStorageError):
                self.repository.load(session_id)

    def test_unrelated_files_do_not_appear_as_completed_sessions(self):
        source = self.make()
        (self.config.directory / "notes.txt").write_text("local notes", encoding="utf-8")
        self.assertEqual([item.session_id for item in self.repository.list_completed()], [source.session_directory.name])

    def test_linked_session_directory_cannot_load_other_saved_content(self):
        source = self.make()
        link = self.config.directory / "alias"
        try:
            link.symlink_to(source.session_directory, target_is_directory=True)
        except OSError:
            with patch("session_storage.session_repository._link", return_value=True):
                with self.assertRaisesRegex(SessionStorageError, "Linked"):
                    self.repository.load(source.session_directory.name)
            return
        with self.assertRaisesRegex(SessionStorageError, "Linked"):
            self.repository.load(link.name)
        self.assertEqual(len(self.repository.list_completed()), 1)

    def test_linked_json_file_rejected_before_read(self):
        source = self.make()
        actual = (source.session_directory / "session.json").read_text(encoding="utf-8")
        external = self.root / "outside.json"
        external.write_text(actual, encoding="utf-8")
        metadata = source.session_directory / "session.json"
        with patch("session_storage.session_repository._link", side_effect=lambda path: path == metadata):
            with self.assertRaisesRegex(SessionStorageError, "local session file"):
                self.repository.load(source.session_directory.name)

    def test_unsafe_evidence_paths_are_removed_without_discarding_event(self):
        source = self.make()
        for value in ("../outside.jpg", "evidence/../../outside.jpg", "https://example.test/image.jpg",
                      "\\\\server\\share\\image.jpg", "C:relative.jpg", "image.exe", str(self.root / "outside.jpg"),
                      "image.jpg", "bad\x00.jpg", 123):
            history = self.history(source)
            history[0]["evidence_path"] = value
            self.edit(source, "events.json", history)
            with self.subTest(value=value):
                loaded = self.repository.load(source.session_directory.name)
                self.assertIsNone(loaded.events[0].evidence_path)
                self.assertEqual(loaded.events[0].risk_delta, 30)
                self.assertIn("evidence_unavailable_reason", loaded.events[0].metadata)
                self.assertTrue(self.repository.errors)

    def test_missing_and_corrupt_managed_evidence_remain_available_for_viewer_placeholder(self):
        source = self.make()
        image = source.session_directory / "evidence" / "PHONE_DETECTED.jpg"
        loaded = self.repository.load(source.session_directory.name)
        self.assertEqual(Path(loaded.events[0].evidence_path), image)
        image.unlink()
        loaded = self.repository.load(source.session_directory.name)
        self.assertEqual(Path(loaded.events[0].evidence_path), image)
        self.assertEqual(len(self.repository.list_completed()), 1)

    def test_project_relative_legacy_evidence_path_is_rebased_to_current_session(self):
        source = self.make()
        history = self.history(source)
        history[0]["evidence_path"] = f"sessions/{source.session_directory.name}/evidence/PHONE_DETECTED.jpg"
        self.edit(source, "events.json", history)
        loaded = self.repository.load(source.session_directory.name)
        self.assertEqual(Path(loaded.events[0].evidence_path), source.session_directory / "evidence" / "PHONE_DETECTED.jpg")

    def test_evidence_link_outside_session_is_rejected(self):
        source = self.make()
        image = source.session_directory / "evidence" / "PHONE_DETECTED.jpg"
        external = self.root / "outside.jpg"
        external.write_bytes(b"outside")
        actual_resolve = Path.resolve
        def resolve(path, *args, **kwargs):
            return external if path == image else actual_resolve(path, *args, **kwargs)
        with patch.object(Path, "resolve", resolve):
            loaded = self.repository.load(source.session_directory.name)
        self.assertIsNone(loaded.events[0].evidence_path)
        self.assertEqual(external.read_bytes(), b"outside")

    def test_json_reads_and_event_question_counts_are_bounded(self):
        source = self.make()
        cases = (
            RepositoryLimits(session_json_bytes=10), RepositoryLimits(events_json_bytes=10),
            RepositoryLimits(max_events=2), RepositoryLimits(max_questions=2),
        )
        for limits in cases:
            with self.subTest(limits=limits), self.assertRaises(SessionStorageError):
                LocalSessionRepository(self.config, limits=limits).load(source.session_directory.name)

    def test_session_directory_scan_is_bounded_with_readable_message(self):
        self.make(student="First")
        self.make(student="Second")
        reader = LocalSessionRepository(self.config, limits=RepositoryLimits(max_sessions=1))
        self.assertEqual(len(reader.list_completed()), 1)
        self.assertTrue(any("scan reached" in message for message in reader.errors))

    def test_nonfinite_json_numbers_including_nested_metadata_rejected(self):
        source = self.make()
        file = source.session_directory / "events.json"
        for literal in ("NaN", "Infinity", "-Infinity", "1e999"):
            file.write_text('[{"metadata":{"bad":' + literal + '}}]', encoding="utf-8")
            with self.subTest(literal=literal), self.assertRaises(SessionStorageError):
                self.repository.load(source.session_directory.name)

    def test_duplicate_json_fields_and_wrong_top_level_types_rejected(self):
        source = self.make()
        for text in ('{"student":"A","student":"B"}', "[]", "null"):
            (source.session_directory / "session.json").write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(SessionStorageError):
                self.repository.load(source.session_directory.name)

    def test_invalid_event_enums_timestamps_and_numeric_fields_rejected(self):
        source = self.make()
        original = self.history(source)
        for key, value in (("type", "CENTER"), ("severity", "unknown"), ("timestamp", "2026-10-08T14:32:00"),
                           ("duration", -1), ("confidence", 1.2), ("risk_delta", "30"), ("metadata", [])):
            history = [dict(item) for item in original]
            history[0][key] = value
            self.edit(source, "events.json", history)
            with self.subTest(key=key), self.assertRaises(SessionStorageError):
                self.repository.load(source.session_directory.name)

    def test_invalid_session_numeric_fields_and_naive_time_rejected(self):
        source = self.make()
        original = self.metadata(source)
        for key, value in (("risk_score", 101), ("risk_score", -1), ("risk_score", True),
                           ("risk_level", "UNKNOWN"), ("duration_seconds", "123"),
                           ("duration_seconds", 10 ** 400), ("started_at", "2026-10-08T14:32:00")):
            metadata = dict(original)
            metadata[key] = value
            self.edit(source, "session.json", metadata)
            with self.subTest(key=key, value=value), self.assertRaises(SessionStorageError):
                self.repository.load(source.session_directory.name)

    def test_inconsistent_exam_answers_status_counts_or_percentage_rejected(self):
        source = self.make()
        original = self.metadata(source)
        for key, value in (("question_ids", ["duplicate", "duplicate"]), ("answers", {"single": "A"}),
                           ("score", 3), ("max_score", 1), ("percentage", 99), ("submission_reason", "unknown")):
            metadata = json.loads(json.dumps(original))
            metadata["exam_result"][key] = value
            self.edit(source, "session.json", metadata)
            with self.subTest(key=key), self.assertRaises(SessionStorageError):
                self.repository.load(source.session_directory.name)

    def test_unknown_additive_fields_do_not_break_existing_known_schema(self):
        source = self.make()
        metadata = self.metadata(source)
        metadata["future_field"] = {"ignored": "compatible"}
        metadata["exam_result"]["future_academic_field"] = 1
        self.edit(source, "session.json", metadata)
        history = self.history(source)
        history[0]["future_event_field"] = {"value": "compatible"}
        self.edit(source, "events.json", history)
        self.assertEqual(self.repository.load(source.session_directory.name).exam_result.score, 2)

    def test_reloaded_exam_snapshot_events_and_counts_are_immutable(self):
        source = self.make()
        loaded = self.repository.load(source.session_directory.name)
        with self.assertRaises(FrozenInstanceError):
            loaded.risk_score = 100
        with self.assertRaises(FrozenInstanceError):
            loaded.events[0].type = EventType.FACE_MISSING
        with self.assertRaises(TypeError):
            loaded.exam_result.answers["single"] = "B"
        with self.assertRaises(TypeError):
            loaded.event_counts[EventType.PHONE_DETECTED] = 10

    def test_all_text_exam_and_empty_event_history_round_trip(self):
        source = self.make(events=())
        metadata = self.metadata(source)
        metadata["exam_result"] = academic_result(all_text=True).to_dict()
        self.edit(source, "session.json", metadata)
        loaded = self.repository.load(source.session_directory.name)
        self.assertEqual(loaded.events, ())
        self.assertIsNone(loaded.exam_result.percentage)
        self.assertEqual(loaded.exam_result.text_pending_count, 1)

    def test_component_errors_preserved_without_creating_new_violation_or_rescoring(self):
        source = self.make()
        metadata = self.metadata(source)
        metadata["error"] = "Camera read failed"
        metadata["persistence_error"] = "Final flush failed"
        self.edit(source, "session.json", metadata)
        loaded = self.repository.load(source.session_directory.name)
        self.assertEqual(loaded.error, "Camera read failed")
        self.assertEqual(loaded.persistence_error, "Final flush failed")
        self.assertEqual(loaded.risk_score, 45)
        self.assertEqual(len(loaded.events), 3)

    def test_successful_operation_resets_previous_diagnostics(self):
        source = self.make()
        with self.assertRaises(SessionStorageError):
            self.repository.load("missing")
        self.assertTrue(self.repository.errors)
        self.repository.load(source.session_directory.name)
        self.assertEqual(self.repository.errors, ())

    def test_repository_protocol_exposes_transport_compatible_contract(self):
        self.assertIsInstance(self.repository, SessionRepository)
        with self.assertRaises(ValueError):
            RepositoryLimits(max_events=0)
        with self.assertRaises(FrozenInstanceError):
            RepositoryLimits().max_events = 1


if __name__ == "__main__":
    unittest.main()
