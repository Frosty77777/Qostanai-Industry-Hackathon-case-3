"""Validated local exams and immutable academic results. No Qt or CV imports."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import json
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


MAX_EXAM_BYTES = 5 * 1024 * 1024
MAX_QUESTIONS = 1000
DEMO_EXAM_PATH = Path(__file__).resolve().parents[2] / "data" / "demo_exam.json"
Answer = str | tuple[str, ...] | None


class ExamValidationError(ValueError):
    """A local exam cannot be loaded or has an invalid definition."""


class QuestionType(StrEnum):
    SINGLE_CHOICE = "SINGLE_CHOICE"
    MULTIPLE_CHOICE = "MULTIPLE_CHOICE"
    TEXT = "TEXT"


def _nonempty_string(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ExamValidationError(f"{label} must be a nonempty string")
    return value


def _aware_time(value, label):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be a timezone-aware datetime")
    return value


@dataclass(frozen=True)
class Question:
    id: str
    type: QuestionType
    text: str
    options: tuple[str, ...] = ()
    correct_answer: str | tuple[str, ...] | None = None

    def __post_init__(self):
        _nonempty_string(self.id, "Question ID")
        _nonempty_string(self.text, f"Question {self.id}: text")
        try:
            kind = QuestionType(self.type)
        except (ValueError, TypeError):
            raise ExamValidationError(f"Question {self.id}: unsupported question type") from None
        if not isinstance(self.options, (tuple, list)):
            raise ExamValidationError(f"Question {self.id}: options must be a list")
        options = tuple(self.options)
        for option in options:
            _nonempty_string(option, f"Question {self.id}: option")
        if len(set(options)) != len(options):
            raise ExamValidationError(f"Question {self.id}: options must be unique")
        correct = self.correct_answer
        if kind is QuestionType.TEXT:
            if options or correct is not None:
                raise ExamValidationError(f"Question {self.id}: TEXT has no options or automatic answer key")
        elif not options:
            raise ExamValidationError(f"Question {self.id}: choice questions require options")
        elif kind is QuestionType.SINGLE_CHOICE:
            if not isinstance(correct, str) or correct not in options:
                raise ExamValidationError(f"Question {self.id}: correct_answer must be one listed option")
        else:
            if not isinstance(correct, (tuple, list)) or not correct:
                raise ExamValidationError(f"Question {self.id}: correct_answer must be a nonempty list of options")
            if any(not isinstance(value, str) or value not in options for value in correct):
                raise ExamValidationError(f"Question {self.id}: correct_answer contains an unknown option")
            if len(set(correct)) != len(correct):
                raise ExamValidationError(f"Question {self.id}: correct_answer must not contain duplicates")
            correct = tuple(correct)
        object.__setattr__(self, "type", kind)
        object.__setattr__(self, "options", options)
        object.__setattr__(self, "correct_answer", correct)


@dataclass(frozen=True)
class ExamDefinition:
    id: str
    title: str
    questions: tuple[Question, ...]
    time_limit_seconds: int | None = None
    reference: str | None = None

    def __post_init__(self):
        _nonempty_string(self.id, "Exam ID")
        _nonempty_string(self.title, "Exam title")
        if not isinstance(self.questions, (tuple, list)):
            raise ExamValidationError("Exam questions must be a list")
        questions = tuple(self.questions)
        if not questions or len(questions) > MAX_QUESTIONS:
            raise ExamValidationError(f"Exam must contain between 1 and {MAX_QUESTIONS} questions")
        if any(not isinstance(question, Question) for question in questions):
            raise ExamValidationError("Each exam question must be a Question")
        if len({question.id for question in questions}) != len(questions):
            raise ExamValidationError("Question IDs must be unique")
        if self.time_limit_seconds is not None and (
            type(self.time_limit_seconds) is not int or self.time_limit_seconds <= 0
        ):
            raise ExamValidationError("time_limit_seconds must be a positive integer or null")
        if self.reference is not None:
            _nonempty_string(self.reference, "Exam definition reference")
        object.__setattr__(self, "questions", questions)


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExamValidationError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _fields(value, allowed, label):
    if not isinstance(value, dict):
        raise ExamValidationError(f"{label} must be a JSON object")
    unknown = set(value) - allowed
    if unknown:
        raise ExamValidationError(f"{label}: unknown field(s): {', '.join(sorted(unknown))}")


def load_exam(path: str | Path) -> ExamDefinition:
    """Load UTF-8 JSON with a bounded file size and clear, local validation errors."""
    try:
        source = Path(path).expanduser().resolve()
        with source.open("rb") as stream:
            content = stream.read(MAX_EXAM_BYTES + 1)
        if len(content) > MAX_EXAM_BYTES:
            raise ExamValidationError(f"Exam JSON exceeds {MAX_EXAM_BYTES} bytes")
        data = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_json_object)
    except ExamValidationError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise ExamValidationError(f"Cannot load exam JSON: {exc}") from exc
    _fields(data, {"id", "title", "questions", "time_limit_seconds"}, "Exam")
    for required in ("id", "title", "questions"):
        if required not in data:
            raise ExamValidationError(f"Exam is missing {required}")
    if not isinstance(data["questions"], list):
        raise ExamValidationError("Exam questions must be a JSON list")
    if not data["questions"] or len(data["questions"]) > MAX_QUESTIONS:
        raise ExamValidationError(f"Exam must contain between 1 and {MAX_QUESTIONS} questions")
    questions = []
    for index, value in enumerate(data["questions"], 1):
        _fields(value, {"id", "type", "text", "options", "correct_answer"}, f"Question {index}")
        for required in ("id", "type", "text"):
            if required not in value:
                raise ExamValidationError(f"Question {index} is missing {required}")
        if "options" in value and not isinstance(value["options"], list):
            raise ExamValidationError(f"Question {index}: options must be a JSON list")
        questions.append(Question(**value))
    return ExamDefinition(
        data["id"], data["title"], tuple(questions),
        data.get("time_limit_seconds"), str(source),
    )


def load_demo_exam() -> ExamDefinition:
    return load_exam(DEMO_EXAM_PATH)


@dataclass(frozen=True)
class ExamResult:
    exam_id: str
    exam_name: str
    definition_reference: str | None
    question_ids: tuple[str, ...]
    answers: Mapping[str, Answer]
    answer_status: Mapping[str, str]
    started_at: datetime
    submitted_at: datetime
    duration_seconds: float
    submission_reason: str
    correct_count: int
    incorrect_count: int
    unanswered_count: int
    score: int
    max_score: int
    percentage: float | None
    text_pending_count: int
    grading: Mapping[str, str]

    def __post_init__(self):
        object.__setattr__(self, "question_ids", tuple(self.question_ids))
        for name in ("answers", "answer_status", "grading"):
            # Copy before wrapping so future mutation of a caller's dict cannot alter a result.
            values = dict(getattr(self, name))
            if name == "answers":
                values = {key: tuple(value) if isinstance(value, (tuple, list)) else value
                          for key, value in values.items()}
            object.__setattr__(self, name, MappingProxyType(values))

    def to_dict(self) -> dict:
        return {
            "exam_id": self.exam_id,
            "exam_name": self.exam_name,
            "definition_reference": self.definition_reference,
            "question_ids": list(self.question_ids),
            "answers": {key: list(value) if isinstance(value, tuple) else value
                        for key, value in self.answers.items()},
            "answer_status": dict(self.answer_status),
            "started_at": self.started_at.isoformat(),
            "submitted_at": self.submitted_at.isoformat(),
            "duration_seconds": self.duration_seconds,
            "submission_reason": self.submission_reason,
            "correct_count": self.correct_count,
            "incorrect_count": self.incorrect_count,
            "unanswered_count": self.unanswered_count,
            "score": self.score,
            "max_score": self.max_score,
            "percentage": self.percentage,
            "text_pending_count": self.text_pending_count,
            "grading": dict(self.grading),
        }


class ExamAttempt:
    """Answers and navigation on the GUI thread; submission freezes a full snapshot."""

    def __init__(self, exam: ExamDefinition):
        if not isinstance(exam, ExamDefinition):
            raise TypeError("ExamAttempt requires an ExamDefinition")
        self.exam = exam
        self._questions = {question.id: question for question in exam.questions}
        self._answers = {question.id: None for question in exam.questions}
        self._current_index = 0
        self.started_at = None
        self._result = None

    @property
    def answers(self) -> Mapping[str, Answer]:
        return MappingProxyType(self._answers)

    @property
    def submitted(self) -> bool:
        return self._result is not None

    @property
    def result(self) -> ExamResult | None:
        return self._result

    def start(self, started_at: datetime):
        _aware_time(started_at, "Exam start time")
        if self.started_at is not None:
            if self.started_at != started_at:
                raise ValueError("Exam attempt has already started")
            return
        if self.submitted:
            raise ValueError("Exam attempt has already been submitted")
        self.started_at = started_at

    @property
    def current_index(self) -> int:
        return self._current_index

    @property
    def current_question(self) -> Question:
        return self.exam.questions[self._current_index]

    def answer_for(self, question_id: str) -> Answer:
        return self._answers[question_id]

    def set_answer(self, question_id: str, value: Answer):
        if self.submitted:
            raise ValueError("Submitted answers cannot be changed")
        question = self._questions[question_id]
        if value is None:
            normalized = None
        elif question.type is QuestionType.TEXT:
            if not isinstance(value, str):
                raise ValueError("TEXT answers must be strings")
            normalized = value
        elif question.type is QuestionType.SINGLE_CHOICE:
            if not isinstance(value, str) or value not in question.options:
                raise ValueError("SINGLE_CHOICE answer must be one listed option")
            normalized = value
        else:
            if not isinstance(value, (tuple, list)):
                raise ValueError("MULTIPLE_CHOICE answer must be a list or tuple of options")
            if any(not isinstance(option, str) or option not in question.options for option in value):
                raise ValueError("MULTIPLE_CHOICE answer contains an unknown option")
            if len(set(value)) != len(value):
                raise ValueError("MULTIPLE_CHOICE answer must not contain duplicates")
            # Use definition order so checkbox toggling order does not affect persistence.
            selected = set(value)
            normalized = tuple(option for option in question.options if option in selected)
        self._answers[question_id] = normalized

    def is_answered(self, question_id: str) -> bool:
        answer = self.answer_for(question_id)
        return bool(answer.strip()) if isinstance(answer, str) else bool(answer)

    @property
    def answered_count(self) -> int:
        return sum(self.is_answered(question_id) for question_id in self._answers)

    def go_to(self, index: int) -> Question:
        if type(index) is not int or not 0 <= index < len(self.exam.questions):
            raise ValueError("Question index is outside this exam")
        self._current_index = index
        return self.current_question

    def next(self) -> Question:
        return self.go_to(min(self._current_index + 1, len(self.exam.questions) - 1))

    def previous(self) -> Question:
        return self.go_to(max(self._current_index - 1, 0))

    def snapshot(self, *, submitted_at: datetime, duration_seconds: float,
                 reason: str = "interrupted") -> ExamResult:
        """Copy current answers for the worker without locking an ongoing attempt."""
        if self.submitted:
            return self._result
        if self.started_at is None:
            raise ValueError("Exam attempt must start before submission")
        _aware_time(submitted_at, "Exam submission time")
        if submitted_at < self.started_at:
            raise ValueError("Exam submission time cannot precede its start")
        try:
            valid_duration = (not isinstance(duration_seconds, bool)
                              and isinstance(duration_seconds, (int, float))
                              and isfinite(duration_seconds) and duration_seconds >= 0)
        except OverflowError:
            valid_duration = False
        if not valid_duration:
            raise ValueError("Exam duration must be finite and nonnegative")
        if reason not in {"submitted", "expired", "interrupted"}:
            raise ValueError("Unsupported exam submission reason")
        correct = incorrect = unanswered = maximum = pending = 0
        statuses, grading = {}, {}
        for question in self.exam.questions:
            answered = self.is_answered(question.id)
            statuses[question.id] = "answered" if answered else "unanswered"
            if question.type is not QuestionType.TEXT:
                maximum += 1
            if not answered:
                unanswered += 1
                grading[question.id] = "unanswered"
            elif question.type is QuestionType.TEXT:
                pending += 1
                grading[question.id] = "manual_review"
            else:
                answer = self._answers[question.id]
                matches = (set(answer) == set(question.correct_answer)
                           if question.type is QuestionType.MULTIPLE_CHOICE
                           else answer == question.correct_answer)
                correct += int(matches)
                incorrect += int(not matches)
                grading[question.id] = "correct" if matches else "incorrect"
        return ExamResult(
            self.exam.id, self.exam.title, self.exam.reference,
            tuple(self._questions), self._answers, statuses, self.started_at, submitted_at,
            float(duration_seconds), reason, correct, incorrect, unanswered, correct,
            maximum, 100.0 * correct / maximum if maximum else None, pending, grading,
        )

    def submit(self, *, submitted_at: datetime, duration_seconds: float,
               reason: str = "submitted") -> ExamResult:
        """Freeze once; repeated finish signals return the original submission."""
        if not self.submitted:
            self._result = self.snapshot(submitted_at=submitted_at,
                                         duration_seconds=duration_seconds, reason=reason)
        return self._result
