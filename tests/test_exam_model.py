"""Local exam validation, preserved answers and academic grading with fake times."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from exams import (
    ExamAttempt, ExamDefinition, ExamValidationError, Question, QuestionType,
    load_demo_exam, load_exam,
)


START = datetime(2026, 10, 8, 17, 30, tzinfo=timezone(timedelta(hours=5)))


def definition():
    return ExamDefinition("test", "Test Exam", (
        Question("single", QuestionType.SINGLE_CHOICE, "Choose one", ("A", "B"), "B"),
        Question("multi", QuestionType.MULTIPLE_CHOICE, "Choose several", ("A", "B", "C"), ("A", "C")),
        Question("text", QuestionType.TEXT, "Explain"),
    ), reference="local-test.json")


def json_definition():
    return {
        "id": "sample",
        "title": "Sample Exam",
        "questions": [
            {"id": "q1", "type": "SINGLE_CHOICE", "text": "Choose", "options": ["A", "B"], "correct_answer": "A"},
            {"id": "q2", "type": "MULTIPLE_CHOICE", "text": "Select", "options": ["A", "B"], "correct_answer": ["A", "B"]},
            {"id": "q3", "type": "TEXT", "text": "Describe"},
        ],
    }


class ExamModelChecks(unittest.TestCase):
    def setUp(self):
        self.attempt = ExamAttempt(definition())
        self.attempt.start(START)

    def submit(self, **kwargs):
        return self.attempt.submit(submitted_at=START + timedelta(seconds=10),
                                   duration_seconds=10, **kwargs)

    def test_demo_loads_nine_questions_and_all_types(self):
        exam = load_demo_exam()
        self.assertEqual(len(exam.questions), 9)
        self.assertEqual({q.type for q in exam.questions}, set(QuestionType))
        self.assertTrue(Path(exam.reference).is_file())
        self.assertIsNone(exam.time_limit_seconds)

    def test_single_choice_preserved_across_navigation(self):
        self.attempt.set_answer("single", "B")
        self.attempt.next()
        self.attempt.previous()
        self.assertEqual(self.attempt.answer_for("single"), "B")

    def test_multiple_choice_preserved_and_uses_definition_order(self):
        selected = ["C", "A"]
        self.attempt.set_answer("multi", selected)
        selected.clear()
        self.attempt.go_to(2)
        self.attempt.go_to(1)
        self.assertEqual(self.attempt.answer_for("multi"), ("A", "C"))

    def test_text_preserved_verbatim_across_navigation(self):
        text = "  First line\nSecond line: café.  "
        self.attempt.set_answer("text", text)
        self.attempt.go_to(0)
        self.attempt.go_to(2)
        self.assertEqual(self.attempt.answer_for("text"), text)

    def test_next_previous_and_boundary_navigation(self):
        self.assertEqual(self.attempt.current_index, 0)
        self.assertEqual(self.attempt.previous().id, "single")
        self.assertEqual(self.attempt.next().id, "multi")
        self.assertEqual(self.attempt.next().id, "text")
        self.assertEqual(self.attempt.next().id, "text")
        self.assertEqual(self.attempt.previous().id, "multi")

    def test_invalid_navigation_rejected(self):
        for index in (-1, 3, True, 1.5):
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.attempt.go_to(index)

    def test_initial_unanswered_state(self):
        self.assertEqual(self.attempt.answered_count, 0)
        self.assertFalse(self.attempt.is_answered("single"))
        self.assertEqual(dict(self.attempt.answers), {"single": None, "multi": None, "text": None})

    def test_blank_text_and_empty_choices_are_unanswered(self):
        self.attempt.set_answer("text", " \n\t ")
        self.attempt.set_answer("multi", [])
        self.assertEqual(self.attempt.answered_count, 0)
        result = self.submit()
        self.assertEqual(result.unanswered_count, 3)
        self.assertEqual(result.text_pending_count, 0)

    def test_answers_can_be_cleared_before_submission(self):
        self.attempt.set_answer("single", "B")
        self.assertEqual(self.attempt.answered_count, 1)
        self.attempt.set_answer("single", None)
        self.assertFalse(self.attempt.is_answered("single"))

    def test_automatic_grading_exact_matches_and_text_manual(self):
        self.attempt.set_answer("single", "B")
        self.attempt.set_answer("multi", ["C", "A"])
        self.attempt.set_answer("text", "A useful explanation")
        result = self.submit()
        self.assertEqual((result.correct_count, result.incorrect_count, result.unanswered_count), (2, 0, 0))
        self.assertEqual((result.score, result.max_score, result.percentage), (2, 2, 100))
        self.assertEqual(result.text_pending_count, 1)
        self.assertEqual(result.grading["text"], "manual_review")

    def test_multiple_choice_requires_exact_set_not_partial_credit(self):
        self.attempt.set_answer("multi", ["A"])
        result = self.submit()
        self.assertEqual(result.score, 0)
        self.assertEqual(result.incorrect_count, 1)
        self.assertEqual(result.grading["multi"], "incorrect")

    def test_extra_multiple_choice_option_is_incorrect(self):
        self.attempt.set_answer("multi", ["A", "B", "C"])
        self.assertEqual(self.submit().grading["multi"], "incorrect")

    def test_unanswered_counts_are_separate_from_answered_incorrect(self):
        self.attempt.set_answer("single", "A")
        result = self.submit()
        self.assertEqual((result.correct_count, result.incorrect_count, result.unanswered_count), (0, 1, 2))
        self.assertEqual(result.max_score, 2)

    def test_text_does_not_change_choice_denominator(self):
        self.attempt.set_answer("single", "B")
        self.attempt.set_answer("text", "Stored for instructor")
        result = self.submit()
        self.assertEqual((result.score, result.max_score, result.percentage), (1, 2, 50))

    def test_all_text_exam_has_no_automatic_percentage(self):
        attempt = ExamAttempt(ExamDefinition("essay", "Essay", (Question("q", QuestionType.TEXT, "Explain"),)))
        attempt.start(START)
        attempt.set_answer("q", "Answer")
        result = attempt.submit(submitted_at=START, duration_seconds=0)
        self.assertEqual((result.score, result.max_score), (0, 0))
        self.assertIsNone(result.percentage)
        self.assertEqual(result.text_pending_count, 1)

    def test_result_captures_ids_status_times_reference_and_duration(self):
        self.attempt.set_answer("single", "B")
        result = self.submit()
        self.assertEqual(result.question_ids, ("single", "multi", "text"))
        self.assertEqual(result.exam_id, "test")
        self.assertEqual(result.exam_name, "Test Exam")
        self.assertEqual(result.definition_reference, "local-test.json")
        self.assertEqual(result.answer_status["single"], "answered")
        self.assertEqual(result.answer_status["multi"], "unanswered")
        self.assertEqual(result.started_at, START)
        self.assertEqual(result.submitted_at, START + timedelta(seconds=10))
        self.assertEqual(result.duration_seconds, 10)

    def test_result_serializes_complete_json_snapshot(self):
        self.attempt.set_answer("multi", ["A", "C"])
        result = self.submit(reason="expired")
        snapshot = json.loads(json.dumps(result.to_dict()))
        self.assertEqual(snapshot["answers"]["multi"], ["A", "C"])
        self.assertEqual(snapshot["started_at"], START.isoformat())
        self.assertEqual(snapshot["submission_reason"], "expired")
        self.assertEqual(snapshot["grading"]["multi"], "correct")
        self.assertNotIn("risk_score", snapshot)

    def test_result_and_answer_maps_immutable(self):
        result = self.submit()
        with self.assertRaises(FrozenInstanceError):
            result.score = 20
        with self.assertRaises(TypeError):
            result.answers["single"] = "A"
        with self.assertRaises(TypeError):
            result.answer_status["single"] = "answered"
        with self.assertRaises(TypeError):
            result.grading["single"] = "correct"

    def test_readonly_attempt_answers_do_not_bypass_submission(self):
        self.submit()
        with self.assertRaises(TypeError):
            self.attempt.answers["single"] = "B"
        with self.assertRaises(ValueError):
            self.attempt.set_answer("single", "B")

    def test_submission_is_idempotent_and_preserves_first_snapshot(self):
        first = self.submit()
        second = self.attempt.submit(submitted_at=START + timedelta(seconds=40),
                                     duration_seconds=40, reason="interrupted")
        self.assertIs(first, second)
        self.assertTrue(self.attempt.submitted)
        self.assertIs(self.attempt.result, first)
        self.assertEqual(first.submission_reason, "submitted")
        self.assertEqual(first.duration_seconds, 10)

    def test_snapshot_is_immutable_copy_without_freezing_attempt(self):
        self.attempt.set_answer("single", "A")
        snapshot = self.attempt.snapshot(submitted_at=START + timedelta(seconds=4), duration_seconds=4)
        self.assertFalse(self.attempt.submitted)
        self.assertIsNone(self.attempt.result)
        self.assertEqual(snapshot.submission_reason, "interrupted")
        self.attempt.set_answer("single", "B")
        self.assertEqual(snapshot.answers["single"], "A")
        self.assertEqual(snapshot.score, 0)
        self.assertEqual(self.submit().score, 1)

    def test_snapshot_of_submitted_attempt_uses_locked_result(self):
        result = self.submit()
        self.assertIs(self.attempt.snapshot(submitted_at=START, duration_seconds=0), result)

    def test_unknown_question_answer_rejected(self):
        with self.assertRaises(KeyError):
            self.attempt.set_answer("missing", "A")

    def test_invalid_answer_values_rejected(self):
        cases = [("single", "unknown"), ("single", ["A"]), ("multi", "A"),
                 ("multi", ["unknown"]), ("multi", ["A", "A"]), ("text", 42)]
        for question_id, value in cases:
            with self.subTest(question_id=question_id, value=value), self.assertRaises(ValueError):
                self.attempt.set_answer(question_id, value)

    def test_start_and_submit_require_aware_datetimes(self):
        attempt = ExamAttempt(definition())
        with self.assertRaises(ValueError):
            attempt.start(datetime(2026, 10, 8))
        with self.assertRaises(ValueError):
            attempt.submit(submitted_at=START, duration_seconds=0)
        with self.assertRaises(ValueError):
            self.attempt.submit(submitted_at=START.replace(tzinfo=None), duration_seconds=0)

    def test_submission_before_start_rejected(self):
        with self.assertRaises(ValueError):
            self.attempt.submit(submitted_at=START - timedelta(seconds=1), duration_seconds=0)

    def test_invalid_duration_and_reason_rejected(self):
        for duration in (-1, float("inf"), float("nan"), True, "10", 10 ** 1000):
            with self.subTest(duration=repr(duration)[:40]), self.assertRaises(ValueError):
                self.attempt.submit(submitted_at=START, duration_seconds=duration)
        with self.assertRaises(ValueError):
            self.submit(reason="unknown")

    def test_start_is_idempotent_but_cannot_change_time(self):
        self.attempt.start(START)
        with self.assertRaises(ValueError):
            self.attempt.start(START + timedelta(seconds=1))


class ExamLoadingChecks(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.directory / "exam.json"

    def load(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return load_exam(self.path)

    def test_valid_local_json_loaded_with_reference_and_limit(self):
        data = json_definition()
        data["time_limit_seconds"] = 120
        exam = self.load(data)
        self.assertEqual(exam.title, "Sample Exam")
        self.assertEqual(exam.time_limit_seconds, 120)
        self.assertEqual(exam.reference, str(self.path.resolve()))
        self.assertEqual(exam.questions[1].correct_answer, ("A", "B"))

    def test_invalid_json_clear_error(self):
        self.path.write_text('{"id":', encoding="utf-8")
        with self.assertRaisesRegex(ExamValidationError, "Cannot load exam JSON"):
            load_exam(self.path)

    def test_missing_file_clear_error(self):
        with self.assertRaisesRegex(ExamValidationError, "Cannot load exam JSON"):
            load_exam(self.path)

    def test_invalid_encoding_clear_error(self):
        self.path.write_bytes(b"\xff\xfe invalid")
        with self.assertRaisesRegex(ExamValidationError, "Cannot load exam JSON"):
            load_exam(self.path)

    def test_utf8_bom_supported(self):
        self.path.write_text(json.dumps(json_definition()), encoding="utf-8-sig")
        self.assertEqual(load_exam(self.path).id, "sample")

    def test_duplicate_json_keys_rejected(self):
        self.path.write_text('{"id":"a","id":"b"}', encoding="utf-8")
        with self.assertRaisesRegex(ExamValidationError, "Duplicate JSON field"):
            load_exam(self.path)

    def test_oversize_file_rejected_before_json_parse(self):
        self.path.write_bytes(b" " * 20)
        with patch("exams.exam_model.MAX_EXAM_BYTES", 10):
            with self.assertRaisesRegex(ExamValidationError, "exceeds"):
                load_exam(self.path)

    def test_deep_invalid_json_is_validation_error(self):
        self.path.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")
        with self.assertRaises(ExamValidationError):
            load_exam(self.path)

    def test_root_missing_unknown_empty_invalid_questions_rejected(self):
        cases = [None, [], {}, {"id": "a", "title": "A", "questions": []},
                 {"id": "a", "title": "A", "questions": "bad"},
                 {**json_definition(), "unexpected": True}]
        for data in cases:
            with self.subTest(data=data), self.assertRaises(ExamValidationError):
                self.load(data)

    def test_duplicate_question_ids_rejected(self):
        data = json_definition()
        data["questions"][1]["id"] = "q1"
        with self.assertRaisesRegex(ExamValidationError, "IDs must be unique"):
            self.load(data)

    def test_unsupported_question_type_rejected(self):
        data = json_definition()
        data["questions"][0]["type"] = "VIDEO"
        with self.assertRaisesRegex(ExamValidationError, "unsupported"):
            self.load(data)

    def test_missing_question_fields_rejected(self):
        for name in ("id", "type", "text"):
            data = json_definition()
            del data["questions"][0][name]
            with self.subTest(name=name), self.assertRaises(ExamValidationError):
                self.load(data)

    def test_empty_question_id_text_or_option_rejected(self):
        for name, value in (("id", " "), ("text", ""), ("options", ["A", " "])):
            data = json_definition()
            data["questions"][0][name] = value
            with self.subTest(name=name), self.assertRaises(ExamValidationError):
                self.load(data)

    def test_invalid_options_or_answer_key_rejected(self):
        cases = [("options", "A"), ("options", []), ("options", ["A", "A"]),
                 ("options", ["A", 3]), ("correct_answer", "unknown"), ("correct_answer", ["A"])]
        for name, value in cases:
            data = json_definition()
            data["questions"][0][name] = value
            with self.subTest(name=name, value=value), self.assertRaises(ExamValidationError):
                self.load(data)

    def test_invalid_multiple_answer_key_rejected(self):
        for value in (None, [], "A", ["A", "A"], ["A", "unknown"], [1]):
            data = json_definition()
            data["questions"][1]["correct_answer"] = value
            with self.subTest(value=value), self.assertRaises(ExamValidationError):
                self.load(data)

    def test_text_does_not_allow_automatic_key_or_options(self):
        for name, value in (("correct_answer", "expected"), ("options", ["A"])):
            data = json_definition()
            data["questions"][2][name] = value
            with self.subTest(name=name), self.assertRaises(ExamValidationError):
                self.load(data)

    def test_invalid_time_limit_rejected(self):
        for value in (0, -1, True, 3.5, "60"):
            data = json_definition()
            data["time_limit_seconds"] = value
            with self.subTest(value=value), self.assertRaises(ExamValidationError):
                self.load(data)

    def test_null_time_limit_is_unlimited(self):
        data = json_definition()
        data["time_limit_seconds"] = None
        self.assertIsNone(self.load(data).time_limit_seconds)

    def test_definition_and_questions_are_frozen(self):
        exam = self.load(json_definition())
        with self.assertRaises(FrozenInstanceError):
            exam.title = "Changed"
        with self.assertRaises(FrozenInstanceError):
            exam.questions[0].text = "Changed"


if __name__ == "__main__":
    unittest.main()
