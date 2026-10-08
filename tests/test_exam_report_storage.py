"""Built-in academic report/storage checks without camera, keys, or sleeps."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from test_desktop import APP
from exams import ExamAttempt, ExamDefinition, Question, QuestionType
from monitoring import EventType, ProctoringEvent, Severity
from session_storage import SessionStorageConfig, SessionStorageError, SessionStore
from ui.report_page import ReportPage
from ui.session import SessionResult


START = datetime(2026, 10, 8, 14, 32, tzinfo=timezone(timedelta(hours=5)))
END = START + timedelta(seconds=123)


def definition():
    return ExamDefinition("academic-demo", "Local Academic Demo", (
        Question("single", QuestionType.SINGLE_CHOICE, "Pick one", ("A", "B"), "A"),
        Question("multiple", QuestionType.MULTIPLE_CHOICE, "Pick several", ("A", "B", "C"), ("A", "B")),
        Question("wrong", QuestionType.SINGLE_CHOICE, "Pick one more", ("A", "B"), "A"),
        Question("written", QuestionType.TEXT, "Explain your reasoning"),
    ), reference="data/local_exam.json")


def academic_result(*, empty=False, reason="submitted", all_text=False):
    exam = (ExamDefinition("written-only", "Written Exam", (
        Question("written", QuestionType.TEXT, "Explain your reasoning"),
    ), reference="data/written.json") if all_text else definition())
    attempt = ExamAttempt(exam)
    attempt.start(START)
    if not empty:
        if not all_text:
            attempt.set_answer("single", "A")
            attempt.set_answer("multiple", ("A", "B"))
            attempt.set_answer("wrong", "B")
        attempt.set_answer("written", "My explanation\nPreserved in full. Әлия")
    return attempt.submit(submitted_at=END, duration_seconds=123, reason=reason)


def session(exam_result=None, **changes):
    values = dict(student="Demo Student", exam="Local Academic Demo", duration_seconds=123,
                  risk_score=42, risk_level="MODERATE", events=(), event_counts={},
                  started_at=START, ended_at=END, exam_result=exam_result)
    values.update(changes)
    return SessionResult(**values)


class ExamReportChecks(unittest.TestCase):
    def setUp(self):
        self.page = ReportPage()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.page.reset()
        self.page.close()
        self.page.deleteLater()
        APP.processEvents()

    def test_academic_score_percentage_and_risk_are_separate(self):
        self.page.set_result(session(academic_result()))
        self.assertEqual(self.page.exam_score.text(), "2 / 3")
        self.assertEqual(self.page.exam_percentage.text(), "66.7%")
        self.assertEqual(self.page.risk_score.text(), "42 / 100")
        self.assertEqual(self.page.risk_level.text(), "MODERATE")
        titles = {item.text() for item in self.page.findChildren(QLabel)}
        self.assertTrue({"EXAM RESULT", "EXAM SCORE", "PROCTORING RESULT", "SESSION RISK"} <= titles)

    def test_changed_proctoring_risk_never_changes_academic_score(self):
        academic = academic_result()
        for risk, level in ((0, "LOW"), (100, "CRITICAL")):
            with self.subTest(risk=risk):
                self.page.set_result(session(academic, risk_score=risk, risk_level=level))
                self.assertEqual(self.page.exam_score.text(), "2 / 3")
                self.assertEqual(self.page.risk_score.text(), f"{risk} / 100")

    def test_written_answers_are_pending_and_not_graded_as_incorrect(self):
        self.page.set_result(session(academic_result()))
        self.assertEqual(self.page.exam_values["Text pending review"].text(), "1")
        self.assertEqual(self.page.exam_values["Correct"].text(), "2")
        self.assertEqual(self.page.exam_values["Incorrect"].text(), "1")
        self.assertIn("manual review", self.page.exam_note.text())

    def test_all_text_exam_has_no_misleading_percentage(self):
        self.page.set_result(session(academic_result(all_text=True)))
        self.assertEqual(self.page.exam_score.text(), "0 / 0")
        self.assertEqual(self.page.exam_percentage.text(), "No automatically graded questions")
        self.assertEqual(self.page.exam_values["Text pending review"].text(), "1")
        self.assertEqual(self.page.exam_values["Incorrect"].text(), "0")

    def test_report_shows_submitted_time_duration_and_answer_counts(self):
        self.page.set_result(session(academic_result()))
        self.assertEqual(self.page.exam_values["Submitted"].text(), "2026-10-08 14:34:03")
        self.assertEqual(self.page.exam_values["Duration"].text(), "00:02:03")
        self.assertEqual(self.page.exam_values["Answered"].text(), "4")
        self.assertEqual(self.page.exam_values["Unanswered"].text(), "0")
        self.assertEqual(self.page.exam_values["Status"].text(), "Submitted")

    def test_unanswered_attempt_is_visible_and_review_rows_are_retained(self):
        self.page.set_result(session(academic_result(empty=True)))
        self.assertEqual(self.page.exam_values["Answered"].text(), "0")
        self.assertEqual(self.page.exam_values["Unanswered"].text(), "4")
        self.assertEqual(self.page.answers_model.rowCount(), 4)
        self.assertEqual(self.page.answers_model.data(self.page.answers_model.index(0, 1)), "—")
        self.assertEqual(self.page.answers_model.data(self.page.answers_model.index(0, 2)), "Unanswered")

    def test_answer_review_retains_single_multiple_and_full_written_answer(self):
        academic = academic_result()
        self.page.set_result(session(academic))
        model = self.page.answers_model
        self.assertEqual(model.data(model.index(0, 1)), "A")
        self.assertEqual(model.data(model.index(1, 1)), "A, B")
        self.assertEqual(model.data(model.index(2, 3)), "Incorrect")
        self.assertEqual(model.data(model.index(3, 3)), "Manual Review")
        self.assertEqual(model.data(model.index(3, 1), Qt.ItemDataRole.ToolTipRole), academic.answers["written"])

    def test_interrupted_session_is_clearly_provisional(self):
        self.page.set_result(session(academic_result(reason="interrupted")))
        self.assertEqual(self.page.exam_values["Status"].text(), "Interrupted · provisional")
        self.assertIn("provisional", self.page.exam_note.text())

    def test_expired_submission_is_identified(self):
        self.page.set_result(session(academic_result(reason="expired")))
        self.assertEqual(self.page.exam_values["Status"].text(), "Time limit reached")

    def test_existing_timeline_and_evidence_tab_indices_remain_unchanged(self):
        selected = ProctoringEvent(EventType.ALT_TAB_ATTEMPT, START, Severity.HIGH, 0,
                                  "Alt+Tab attempt detected", risk_delta=15)
        self.page.set_result(session(academic_result(), events=(selected,)))
        self.assertEqual(self.page.tabs.tabText(1), "Full timeline")
        self.assertEqual(self.page.tabs.tabText(2), "Evidence")
        self.assertEqual(self.page.timeline_model.rowCount(), 1)
        self.page.view_button.click()
        self.assertEqual(self.page.tabs.currentIndex(), 2)
        self.assertEqual(self.page.evidence.events, ())

    def test_legacy_result_without_academic_field_remains_readable(self):
        legacy = SimpleNamespace(**{key: value for key, value in vars(session()).items() if key != "exam_result"})
        self.page.set_result(legacy)
        self.assertEqual(self.page.exam_percentage.text(), "No built-in exam result")
        self.assertEqual(self.page.answers_model.rowCount(), 0)
        self.assertEqual(self.page.risk_score.text(), "42 / 100")

    def test_new_session_reset_clears_academic_answers_and_risk(self):
        self.page.set_result(session(academic_result()))
        self.page.reset()
        self.assertEqual(self.page.exam_score.text(), "—")
        self.assertEqual(self.page.exam_percentage.text(), "No built-in exam result")
        self.assertEqual(self.page.exam_values["Status"].text(), "—")
        self.assertEqual(self.page.answers_model.rowCount(), 0)
        self.assertEqual(self.page.risk_score.text(), "0 / 100")


class ExamStorageChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SessionStore(SessionStorageConfig(Path(self.temp.name) / "sessions"))

    def saved_result(self, exam=None, **changes):
        directory = self.store.create_session("Student", START)
        result = session(exam, session_directory=directory, **changes)
        self.store.save_session(result)
        return result, json.loads((directory / "session.json").read_text(encoding="utf-8"))

    def test_session_json_preserves_complete_answer_result_and_definition_reference(self):
        academic = academic_result()
        _, payload = self.saved_result(academic)
        self.assertEqual(payload["exam_result"], academic.to_dict())
        self.assertEqual(payload["exam_result"]["definition_reference"], "data/local_exam.json")
        self.assertEqual(payload["exam_result"]["exam_name"], "Local Academic Demo")
        self.assertEqual(payload["exam_result"]["answers"]["multiple"], ["A", "B"])
        self.assertEqual(payload["exam_result"]["answers"]["written"], academic.answers["written"])

    def test_academic_score_and_percentage_are_separate_from_risk_in_json(self):
        _, payload = self.saved_result(academic_result())
        self.assertEqual(payload["risk_score"], 42)
        self.assertEqual(payload["risk_level"], "MODERATE")
        self.assertEqual(payload["exam_result"]["score"], 2)
        self.assertEqual(payload["exam_result"]["max_score"], 3)
        self.assertAlmostEqual(payload["exam_result"]["percentage"], 200 / 3)

    def test_json_preserves_question_status_and_timezone_aware_submission_times(self):
        _, payload = self.saved_result(academic_result(empty=True))
        academic = payload["exam_result"]
        self.assertEqual(academic["question_ids"], ["single", "multiple", "wrong", "written"])
        self.assertEqual(set(academic["answer_status"].values()), {"unanswered"})
        self.assertEqual(academic["started_at"], START.isoformat())
        self.assertEqual(academic["submitted_at"], END.isoformat())
        self.assertEqual(academic["duration_seconds"], 123)

    def test_exam_storage_preserves_event_history_and_portable_evidence(self):
        directory = self.store.create_session("Student", START)
        evidence = directory / "evidence" / "PHONE_DETECTED.jpg"
        evidence.write_bytes(b"unchanged JPEG content")
        event = ProctoringEvent(EventType.PHONE_DETECTED, START, Severity.CRITICAL, 1.2,
                               "Cell phone detected", evidence_path=str(evidence), risk_delta=30)
        result = session(academic_result(), session_directory=directory, events=(event,))
        self.store.save_session(result)
        history = json.loads((directory / "events.json").read_text(encoding="utf-8"))
        self.assertEqual(history[0]["type"], "PHONE_DETECTED")
        self.assertEqual(history[0]["evidence_path"], "evidence/PHONE_DETECTED.jpg")
        self.assertEqual(history[0]["risk_delta"], 30)
        self.assertEqual(evidence.read_bytes(), b"unchanged JPEG content")

    def test_null_exam_result_and_legacy_objects_save_without_changes_to_risk(self):
        result, payload = self.saved_result()
        self.assertIsNone(payload["exam_result"])
        legacy = SimpleNamespace(**{key: value for key, value in vars(result).items() if key != "exam_result"})
        legacy.session_directory = self.store.create_session("Legacy", START)
        self.store.save_session(legacy)
        saved = json.loads((legacy.session_directory / "session.json").read_text(encoding="utf-8"))
        self.assertIsNone(saved["exam_result"])
        self.assertEqual(saved["risk_score"], result.risk_score)

    def test_multiple_exam_sessions_never_overwrite_prior_answers_or_evidence(self):
        previous, payload = self.saved_result(academic_result())
        before = (previous.session_directory / "session.json").read_bytes()
        next_result, next_payload = self.saved_result(academic_result(empty=True))
        self.assertNotEqual(previous.session_directory, next_result.session_directory)
        self.assertEqual((previous.session_directory / "session.json").read_bytes(), before)
        self.assertNotEqual(payload["exam_result"]["answers"], next_payload["exam_result"]["answers"])

    def test_failed_exam_serialization_keeps_immutable_in_memory_result(self):
        academic = academic_result()
        directory = self.store.create_session("Student", START)
        result = session(academic, session_directory=directory)
        with patch("session_storage.session_store.json.dump", side_effect=OSError("disk full")):
            with self.assertRaises(SessionStorageError):
                self.store.save_session(result)
        self.assertIs(result.exam_result, academic)
        self.assertEqual(result.exam_result.score, 2)
        self.assertFalse((directory / "session.json").exists())
        self.assertFalse((directory / "events.json").exists())


if __name__ == "__main__":
    unittest.main()
