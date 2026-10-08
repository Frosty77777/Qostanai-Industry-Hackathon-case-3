"""Flush immutable session results into isolated local session directories.

The final ``session.json`` is the completion marker. Both JSON payloads are
prepared and flushed before publication; a failed save leaves the caller's
in-memory result untouched. Existing completed sessions are never overwritten.
"""

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile


ROOT = Path(__file__).resolve().parents[2]
_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED_NAME = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)
_MAX_STUDENT_NAME_LENGTH = 64


class SessionStorageError(RuntimeError):
    """A local storage failure, safe for the application to catch and report."""


@dataclass(frozen=True)
class SessionStorageConfig:
    directory: Path = ROOT / "sessions"
    enabled: bool = True

    def __post_init__(self):
        if not isinstance(self.enabled, bool):
            raise ValueError("Session storage enabled must be a boolean")
        object.__setattr__(self, "directory", Path(self.directory).expanduser().resolve())


def _student_component(student: str) -> str:
    """Make one readable Windows-safe component; user input never supplies paths."""
    name = _INVALID_NAME.sub("_", str(student)).strip(" .")
    name = re.sub(r"\s+", "_", name)[:_MAX_STUDENT_NAME_LENGTH].rstrip(" .")
    if not name:
        name = "Student"
    if _RESERVED_NAME.match(name):
        name = "Student_" + name
    return name


def _aware_time(value, field: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.isoformat()


class SessionStore:
    def __init__(self, config: SessionStorageConfig | None = None):
        self.config = config if config is not None else SessionStorageConfig()

    def create_session(self, student: str, started_at: datetime) -> Path:
        """Reserve a unique timestamp/student folder and its evidence directory."""
        if not self.config.enabled:
            raise SessionStorageError("Local session storage is disabled")
        try:
            _aware_time(started_at, "started_at")
            base = f"{started_at:%Y%m%d_%H%M%S}_{started_at.microsecond // 1000:03d}_{_student_component(student)}"
            self.config.directory.mkdir(parents=True, exist_ok=True)
            suffix = 0
            while True:
                candidate = self.config.directory / (base if suffix == 0 else f"{base}_{suffix:03d}")
                try:
                    candidate.mkdir()
                    break
                except FileExistsError:
                    suffix += 1
            try:
                (candidate / "evidence").mkdir()
            except OSError:
                candidate.rmdir()
                raise
            return candidate
        except (OSError, ValueError, TypeError) as exc:
            raise SessionStorageError(f"Could not create local session directory: {exc}") from exc

    def _event_dict(self, event, directory: Path) -> dict:
        data = dict(event.to_dict())
        evidence = data.get("evidence_path")
        if evidence:
            path = Path(evidence)
            # EvidenceManager uses project-relative paths for project sessions.
            # A previously portable path is already relative to this session.
            if not path.is_absolute() and path.parts and path.parts[0] == "evidence":
                candidate = directory / path
            else:
                candidate = path if path.is_absolute() else ROOT / path
            try:
                data["evidence_path"] = candidate.resolve().relative_to(directory).as_posix()
            except ValueError:
                # Preserve external paths instead of inventing a relocated image.
                data["evidence_path"] = str(evidence)
        return data

    @staticmethod
    def _write_json_temp(directory: Path, payload) -> Path:
        descriptor, name = tempfile.mkstemp(prefix=".session-", suffix=".tmp", dir=directory)
        path = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
                json.dump(payload, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            return path
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def save_session(self, result) -> None:
        """Publish full metadata/history once; storage errors never mutate result."""
        if not self.config.enabled:
            return
        temporary = []
        lock = None
        owns_lock = False
        directory = None
        published_events = False
        try:
            if result.session_directory is None:
                raise ValueError("The result has no reserved session directory")
            directory = Path(result.session_directory).resolve()
            directory.relative_to(self.config.directory)
            if directory == self.config.directory or not directory.is_dir():
                raise ValueError("The result must belong to an existing session subdirectory")
            metadata = {
                "student": result.student,
                "exam": result.exam,
                "started_at": _aware_time(result.started_at, "started_at"),
                "ended_at": _aware_time(result.ended_at, "ended_at"),
                "duration_seconds": result.duration_seconds,
                "risk_score": result.risk_score,
                "risk_level": str(result.risk_level),
                "event_counts": {str(key): value for key, value in result.event_counts.items()},
                "error": result.error,
                "persistence_error": getattr(result, "persistence_error", None),
                "schema_version": 1,
            }
            history = [self._event_dict(event, directory) for event in result.events]
            lock = directory / ".save.lock"
            # This reservation prevents two writers from publishing the same result.
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(descriptor)
            owns_lock = True
            if (directory / "session.json").exists() or (directory / "events.json").exists():
                raise FileExistsError("Session JSON already exists; refusing to overwrite it")
            events_temp = self._write_json_temp(directory, history)
            temporary.append(events_temp)
            session_temp = self._write_json_temp(directory, metadata)
            temporary.append(session_temp)
            os.replace(events_temp, directory / "events.json")
            published_events = True
            os.replace(session_temp, directory / "session.json")
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            # An interrupted publication must not look like a completed session.
            if published_events and directory is not None and not (directory / "session.json").exists():
                try:
                    (directory / "events.json").unlink(missing_ok=True)
                except OSError:
                    pass
            raise SessionStorageError(f"Could not save local session JSON: {exc}") from exc
        finally:
            for path in temporary:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            if owns_lock:
                try:
                    lock.unlink(missing_ok=True)
                except OSError:
                    pass
