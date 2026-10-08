"""Session data exchanged between the monitoring thread and desktop pages."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from time import perf_counter

from evidence import EvidenceConfig
from exams import ExamDefinition, ExamResult
from monitoring import EventEngineConfig, ProctoringEvent, RiskConfig
from monitoring.break_manager import BreakConfig
from security import SecurityConfig
from security.secure_window import SecureWindowConfig
from session_storage import SessionStorageConfig
from vision.face_tracker import DEFAULT_MODEL_PATH, FaceResult
from vision.camera_quality import CameraQualityConfig

ROOT = Path(__file__).resolve().parents[2]
DISPLAY_TIMEZONE = timezone(timedelta(hours=5), "Asia/Qyzylorda")


def session_wall_time():
    return datetime.now(DISPLAY_TIMEZONE)


@dataclass(frozen=True)
class SessionConfig:
    student: str = "Student"
    exam: str = "Practice Exam"
    camera_index: int = 0
    window_handle: int | None = None
    weights: Path = ROOT / "models" / "yolo11n.pt"
    face_model: Path = DEFAULT_MODEL_PATH
    image_size: int = 416
    confidence: float = 0.35
    threads: int = min(4, os.cpu_count() or 1)
    events: EventEngineConfig = field(default_factory=EventEngineConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    evidence: EvidenceConfig = field(default_factory=EvidenceConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    camera_quality: CameraQualityConfig = field(default_factory=CameraQualityConfig)
    breaks: BreakConfig = field(default_factory=BreakConfig)
    storage: SessionStorageConfig = field(default_factory=SessionStorageConfig)
    exam_definition: ExamDefinition | None = None
    secure_mode: str = "WINDOWED"
    secure_window: SecureWindowConfig = field(default_factory=SecureWindowConfig)

    def __post_init__(self):
        if self.secure_mode not in {"WINDOWED", "MAXIMIZED", "FULLSCREEN"}:
            raise ValueError("Secure mode must be WINDOWED, MAXIMIZED, or FULLSCREEN")
        if self.exam_definition is not None and not isinstance(self.exam_definition, ExamDefinition):
            raise TypeError("exam_definition must be an ExamDefinition or None")
        if not isinstance(self.secure_window, SecureWindowConfig):
            raise TypeError("secure_window must be a SecureWindowConfig")


@dataclass(frozen=True)
class FrameUpdate:
    image: object  # Detached QImage; QPixmap is created only on the UI thread.
    face: FaceResult
    person_count: int
    phone_detected: bool
    fps: float | None
    events: tuple[ProctoringEvent, ...]
    risk_score: int
    risk_level: str
    face_missing_warning: bool
    statuses: dict[str, tuple[str, str]]
    camera_obstructed_warning: bool = False
    break_active: bool = False
    break_remaining_seconds: int = 0
    head_duration_seconds: float = 0.0
    head_threshold_seconds: float | None = None
    phone_state: str = "NONE"
    window_focus_lost: bool = False


@dataclass(frozen=True)
class SessionResult:
    student: str
    exam: str
    duration_seconds: float
    risk_score: int
    risk_level: str
    events: tuple[ProctoringEvent, ...]
    event_counts: dict
    error: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    session_directory: Path | None = None
    persistence_error: str | None = None
    exam_result: ExamResult | None = None


class SessionTimer:
    """Monotonic elapsed time, independent of repaint frequency."""

    def __init__(self, clock=perf_counter):
        self._clock = clock
        self.reset()

    def reset(self):
        self._started = None
        self._elapsed = 0.0

    def start(self, started_at: float | None = None):
        if self._started is None:
            self._started = self._clock() if started_at is None else started_at

    def stop(self):
        self._elapsed = self.elapsed
        self._started = None

    @property
    def elapsed(self) -> float:
        return self._elapsed if self._started is None else max(0.0, self._clock() - self._started)


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}"
