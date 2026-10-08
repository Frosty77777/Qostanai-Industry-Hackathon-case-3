"""Read completed local reports without modifying the writer or saved sessions.

The repository protocol is the transport boundary: a future authorized remote
repository can return the same summaries and normalized ``SessionResult``.
The repository itself performs no authentication; its caller gates access.
"""

from dataclasses import dataclass
from datetime import datetime
import json
from math import isclose, isfinite
from pathlib import Path, PureWindowsPath
from types import MappingProxyType
from typing import Protocol, TYPE_CHECKING, runtime_checkable

from exams import ExamResult
from monitoring import EventType, ProctoringEvent, Severity
from monitoring.risk_engine import RiskLevel
from .session_store import SessionStorageConfig, SessionStorageError

if TYPE_CHECKING:
    from ui.session import SessionResult

MAX_DIAGNOSTIC_CHARACTERS = 600


def _error_text(error):
    message = str(error)
    return message if len(message) <= MAX_DIAGNOSTIC_CHARACTERS else message[:MAX_DIAGNOSTIC_CHARACTERS] + "…"


@dataclass(frozen=True)
class SessionSummary:
    session_id: str
    student: str
    exam: str
    started_at: datetime
    duration_seconds: float
    exam_score: int | None
    exam_max_score: int | None
    risk_score: int
    risk_level: str


@dataclass(frozen=True)
class RepositoryLimits:
    """Bound local reads and collections without changing the stable writer."""
    session_json_bytes: int = 8 * 1024 * 1024
    events_json_bytes: int = 32 * 1024 * 1024
    max_events: int = 100_000
    max_questions: int = 1000
    max_sessions: int = 2000

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in vars(self).values()):
            raise ValueError("Repository limits must be positive integers")


@runtime_checkable
class SessionRepository(Protocol):
    """Authorization-independent interface suitable for a later transport layer."""
    @property
    def errors(self) -> tuple[str, ...]: ...

    def list_completed(self) -> tuple[SessionSummary, ...]: ...

    def load(self, session_id: str) -> "SessionResult": ...


def _text(value, name, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _integer(value, name, *, maximum=None):
    if type(value) is not int or value < 0 or (maximum is not None and value > maximum):
        raise ValueError(f"{name} is outside its valid integer range")
    return value


def _number(value, name, *, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite and within its valid range")
    try:
        parsed = float(value)
    except OverflowError:
        raise ValueError(f"{name} must be finite and within its valid range") from None
    if not isfinite(parsed) or parsed < 0 or (maximum is not None and parsed > maximum):
        raise ValueError(f"{name} must be finite and within its valid range")
    return parsed


def _time(value, name):
    parsed = datetime.fromisoformat(_text(value, name))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed


def _object(value, name):
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _json_pairs(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"Duplicate JSON field: {name}")
        result[name] = value
    return result


def _json_float(value):
    parsed = float(value)
    if not isfinite(parsed):
        raise ValueError("JSON contains a nonfinite number")
    return parsed


def _json_constant(value):
    raise ValueError(f"JSON contains invalid numeric constant: {value}")


def _link(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


class LocalSessionRepository:
    def __init__(self, config: SessionStorageConfig | None = None, *, limits: RepositoryLimits | None = None):
        self.config = config if config is not None else SessionStorageConfig()
        self.limits = limits if limits is not None else RepositoryLimits()
        self._errors = ()

    @property
    def errors(self) -> tuple[str, ...]:
        return self._errors

    def _directory(self, session_id):
        if (not isinstance(session_id, str) or not session_id or len(session_id) > 255
                or session_id in {".", ".."} or session_id != session_id.rstrip(" .")
                or any(ord(char) < 32 or char in '/\\:<>"|?*' for char in session_id)
                or Path(session_id).name != session_id or PureWindowsPath(session_id).name != session_id):
            raise ValueError("Session ID must be one local directory name")
        candidate = self.config.directory / session_id
        if _link(candidate):
            raise ValueError("Linked session directories are not supported")
        directory = candidate.resolve(strict=True)
        if directory.parent != self.config.directory or not directory.is_dir():
            raise ValueError("Session must be a direct child of the configured directory")
        if (directory / ".save.lock").exists():
            raise ValueError("Session save is still in progress")
        return directory

    def _read_json(self, directory, name, limit):
        path = directory / name
        if _link(path) or path.resolve().parent != directory:
            raise ValueError(f"{name} must be a local session file")
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise ValueError(f"{name} exceeds the configured read limit")
        return json.loads(content.decode("utf-8-sig"), object_pairs_hook=_json_pairs,
                          parse_float=_json_float, parse_constant=_json_constant)

    def _exam(self, payload):
        if payload is None:
            return None
        data = _object(payload, "exam_result")
        ids = data.get("question_ids")
        if not isinstance(ids, list) or not ids or len(ids) > self.limits.max_questions:
            raise ValueError("exam_result question_ids must be a bounded nonempty list")
        ids = tuple(_text(item, "Question ID") for item in ids)
        if len(set(ids)) != len(ids):
            raise ValueError("exam_result has duplicate question IDs")
        answers = _object(data.get("answers"), "answers")
        statuses = _object(data.get("answer_status"), "answer_status")
        grading = _object(data.get("grading"), "grading")
        if any(set(values) != set(ids) for values in (answers, statuses, grading)):
            raise ValueError("Exam answer/status/grading IDs do not match question IDs")
        normalized = {}
        for key in ids:
            value = answers[key]
            if isinstance(value, list):
                value = tuple(_text(item, "Choice answer") for item in value)
                if len(set(value)) != len(value):
                    raise ValueError("Exam choice answer contains duplicates")
            elif value is not None and not isinstance(value, str):
                raise ValueError("Exam answers must be strings, lists of strings, or null")
            normalized[key] = value
            answered = bool(value.strip()) if isinstance(value, str) else bool(value)
            if statuses[key] != ("answered" if answered else "unanswered"):
                raise ValueError("Exam answered status is inconsistent with the answer")
            if grading[key] not in {"correct", "incorrect", "unanswered", "manual_review"}:
                raise ValueError("Exam grading status is invalid")
            if (grading[key] == "unanswered") == answered:
                raise ValueError("Exam grading is inconsistent with answered status")
        counts = {name: _integer(data.get(name), name, maximum=len(ids)) for name in (
            "correct_count", "incorrect_count", "unanswered_count", "text_pending_count", "score", "max_score")}
        expected = {"correct_count": "correct", "incorrect_count": "incorrect",
                    "unanswered_count": "unanswered", "text_pending_count": "manual_review"}
        if any(counts[name] != sum(value == status for value in grading.values()) for name, status in expected.items()):
            raise ValueError("Exam totals do not match per-question grading")
        if (counts["score"] != counts["correct_count"] or counts["score"] > counts["max_score"]
                or counts["correct_count"] + counts["incorrect_count"] > counts["max_score"]
                or counts["max_score"] > len(ids) - counts["text_pending_count"]):
            raise ValueError("Exam score/max_score are inconsistent")
        percentage = data.get("percentage")
        if counts["max_score"]:
            percentage = _number(percentage, "percentage", maximum=100)
            if not isclose(percentage, 100 * counts["score"] / counts["max_score"], abs_tol=1e-6):
                raise ValueError("Exam percentage is inconsistent with score")
        elif percentage is not None:
            raise ValueError("An ungraded exam must have a null percentage")
        started, submitted = _time(data.get("started_at"), "Exam start time"), _time(data.get("submitted_at"), "Exam submission time")
        if submitted < started or data.get("submission_reason") not in {"submitted", "expired", "interrupted"}:
            raise ValueError("Exam submission metadata is invalid")
        return ExamResult(_text(data.get("exam_id"), "Exam ID"), _text(data.get("exam_name"), "Exam name"),
                          _text(data.get("definition_reference"), "Definition reference", nullable=True),
                          ids, normalized, statuses, started, submitted,
                          _number(data.get("duration_seconds"), "Exam duration"), data["submission_reason"],
                          counts["correct_count"], counts["incorrect_count"], counts["unanswered_count"],
                          counts["score"], counts["max_score"], percentage, counts["text_pending_count"], grading)

    def _evidence(self, value, directory):
        if value is None or value == "":
            return None, None
        try:
            if not isinstance(value, str) or "\x00" in value or value.startswith(("//", "\\\\")):
                raise ValueError("Evidence path is not a local image")
            windows, path = PureWindowsPath(value), Path(value)
            if ".." in windows.parts or ".." in path.parts or path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
                raise ValueError("Evidence path is outside the allowed image location")
            if ":" in value and not path.is_absolute():
                raise ValueError("Evidence path contains a drive or URI")
            if path.is_absolute():
                candidate = path
            elif path.parts and path.parts[0] == "evidence":
                candidate = directory / path
            elif len(path.parts) >= 4 and path.parts[:3] == ("sessions", directory.name, "evidence"):
                candidate = directory / Path(*path.parts[2:])
            else:
                raise ValueError("Evidence must belong to the session evidence directory")
            resolved = candidate.resolve()
            if not resolved.is_relative_to(directory / "evidence"):
                raise ValueError("Evidence path leaves the session evidence directory")
            return str(resolved), None
        except (OSError, RuntimeError, ValueError) as exc:
            return None, _error_text(exc)

    def _event(self, payload, directory, index, notices):
        data = _object(payload, f"Event {index}")
        metadata = data.get("metadata")
        if metadata is not None:
            metadata = dict(_object(metadata, "Event metadata"))
        path, error = self._evidence(data.get("evidence_path"), directory)
        if error:
            metadata = {**(metadata or {}), "evidence_unavailable_reason": error}
            notices.append(f"{directory.name}: event {index} evidence rejected: {error}")
        confidence = data.get("confidence")
        if confidence is not None:
            confidence = _number(confidence, "Event confidence", maximum=1)
        delta = data.get("risk_delta")
        if delta is not None:
            delta = _integer(delta, "Event risk_delta", maximum=100)
        return ProctoringEvent(EventType(data.get("type")), _time(data.get("timestamp"), "Event timestamp"),
                               Severity(data.get("severity")), _number(data.get("duration"), "Event duration"),
                               _text(data.get("message"), "Event message"), confidence, metadata, path, delta)

    def _load(self, session_id, notices):
        # Import only after ui.session has initialized its storage configuration.
        from ui.session import SessionResult

        directory = self._directory(session_id)
        data = _object(self._read_json(directory, "session.json", self.limits.session_json_bytes), "Session metadata")
        history = self._read_json(directory, "events.json", self.limits.events_json_bytes)
        if not isinstance(history, list) or len(history) > self.limits.max_events:
            raise ValueError("Event history must be a bounded JSON list")
        counts = _object(data.get("event_counts", {}), "Event counts")
        counts = {EventType(key): _integer(value, "Event count", maximum=self.limits.max_events) for key, value in counts.items()}
        return SessionResult(
            student=_text(data.get("student"), "Student"), exam=_text(data.get("exam"), "Exam"),
            duration_seconds=_number(data.get("duration_seconds"), "Session duration"),
            risk_score=_integer(data.get("risk_score"), "Session risk", maximum=100),
            risk_level=str(RiskLevel(data.get("risk_level"))),
            events=tuple(self._event(item, directory, index, notices) for index, item in enumerate(history, 1)),
            event_counts=MappingProxyType(counts), error=_text(data.get("error"), "Component error", nullable=True),
            started_at=_time(data.get("started_at"), "Session start time"),
            ended_at=_time(data.get("ended_at"), "Session end time"), session_directory=directory,
            persistence_error=_text(data.get("persistence_error"), "Persistence error", nullable=True),
            exam_result=self._exam(data.get("exam_result")),
        )

    def load(self, session_id: str) -> "SessionResult":
        notices = []
        try:
            result = self._load(session_id, notices)
        except (OSError, ValueError, TypeError, RuntimeError, RecursionError) as exc:
            identifier = str(session_id)[:255]
            notices.append(f"{identifier}: cannot open completed session: {_error_text(exc)}")
            self._errors = tuple(notices)
            raise SessionStorageError(f"Cannot open completed session: {_error_text(exc)}") from exc
        self._errors = tuple(notices)
        return result

    def list_completed(self) -> tuple[SessionSummary, ...]:
        notices, summaries = [], []
        try:
            if not self.config.directory.exists():
                self._errors = ()
                return ()
            for count, child in enumerate(self.config.directory.iterdir(), 1):
                if count > self.limits.max_sessions:
                    notices.append("Session directory scan reached the configured limit")
                    break
                if not child.is_dir():
                    continue
                try:
                    result = self._load(child.name, notices)
                    exam = result.exam_result
                    summaries.append(SessionSummary(child.name, result.student, result.exam, result.started_at,
                                                    result.duration_seconds, exam.score if exam else None,
                                                    exam.max_score if exam else None, result.risk_score, result.risk_level))
                except (OSError, ValueError, TypeError, RuntimeError, RecursionError) as exc:
                    notices.append(f"{child.name}: skipped incomplete/invalid session: {_error_text(exc)}")
        except (OSError, ValueError, RuntimeError) as exc:
            notices.append(f"Cannot list local sessions: {_error_text(exc)}")
        self._errors = tuple(notices)
        return tuple(sorted(summaries, key=lambda item: (item.started_at, item.session_id), reverse=True))
