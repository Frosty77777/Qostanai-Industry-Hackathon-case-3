"""Session data exchanged between the monitoring thread and desktop pages."""

from dataclasses import dataclass, field
import os
from pathlib import Path
from time import perf_counter

from evidence import EvidenceConfig
from monitoring import EventEngineConfig, ProctoringEvent, RiskConfig
from monitoring.break_manager import BreakConfig
from security import SecurityConfig
from vision.face_tracker import DEFAULT_MODEL_PATH, FaceResult
from vision.camera_quality import CameraQualityConfig

ROOT = Path(__file__).resolve().parents[2]


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
