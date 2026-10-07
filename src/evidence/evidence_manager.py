"""Save one local overlayed JPEG for an eligible emitted event."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from itertools import count
from pathlib import Path
import sys
from typing import Any

from monitoring import EventType, ProctoringEvent

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVIDENCE_DIRECTORY = PROJECT_ROOT / "sessions" / "current" / "evidence"
IMPORTANT_EVENTS = frozenset({
    EventType.PHONE_DETECTED, EventType.MULTIPLE_PERSONS,
    EventType.MULTIPLE_FACES, EventType.FACE_MISSING,
    EventType.CAMERA_OBSTRUCTED,
})
HEAD_EVENTS = frozenset({
    EventType.LOOK_LEFT, EventType.LOOK_RIGHT, EventType.LOOK_UP, EventType.LOOK_DOWN,
})


@dataclass(frozen=True)
class EvidenceConfig:
    directory: Path = DEFAULT_EVIDENCE_DIRECTORY
    head_direction_enabled: bool = False
    jpeg_quality: int = 90

    def __post_init__(self) -> None:
        if not isinstance(self.jpeg_quality, int) or not 1 <= self.jpeg_quality <= 100:
            raise ValueError("JPEG quality must be an integer between 1 and 100")
        object.__setattr__(self, "directory", Path(self.directory))


def _encode_jpeg(frame: Any, quality: int) -> bytes:
    # Import only when saving, so importing the manager needs no native runtime.
    import cv2

    ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise OSError("OpenCV failed to encode the evidence JPEG")
    return encoded.tobytes()


class EvidenceManager:
    """Enrich immutable events without changing their other fields.

    Directory creation is lazy and failures are caught by capture. The caller
    passes only newly emitted events and replaces their entries in history with
    the returned events. An already attached snapshot is never saved again.
    """

    def __init__(
        self, config: EvidenceConfig | None = None, *,
        encoder: Callable[[Any, int], bytes] = _encode_jpeg,
    ) -> None:
        self.config = config if config is not None else EvidenceConfig()
        self._encoder = encoder

    def should_capture(self, event: ProctoringEvent) -> bool:
        return event.type in IMPORTANT_EVENTS or (
            self.config.head_direction_enabled and event.type in HEAD_EVENTS
        )

    def capture(self, event: ProctoringEvent, frame: Any) -> ProctoringEvent:
        """Return an event with a saved path; the path is None on save failure.

        Exclusive binary file creation prevents overwrites, including collisions
        across manager instances or process restarts. Same-millisecond names use
        readable _1, _2, ... suffixes. No automatic retries after a save failure.
        """
        if event.evidence_path is not None or not self.should_capture(event):
            return event
        reserved_path = None
        try:
            jpeg = self._encoder(frame, self.config.jpeg_quality)
            if not jpeg:
                raise OSError("JPEG encoder returned empty evidence")
            directory = self.config.directory
            directory.mkdir(parents=True, exist_ok=True)
            stem = (
                f"{event.type.value}_{event.timestamp:%Y%m%d_%H%M%S}_"
                f"{event.timestamp.microsecond // 1000:03d}"
            )
            for suffix in count():
                filename = f"{stem}{'_' + str(suffix) if suffix else ''}.jpg"
                path = directory / filename
                try:
                    stream = path.open("xb")
                except FileExistsError:
                    continue
                reserved_path = path
                with stream:
                    if stream.write(jpeg) != len(jpeg):
                        raise OSError("Incomplete evidence write")
                break
            saved_path = path.resolve()
            try:
                evidence_path = saved_path.relative_to(PROJECT_ROOT).as_posix()
            except ValueError:
                evidence_path = saved_path.as_posix()
            enriched = replace(event, evidence_path=evidence_path)
            print(f"[EVIDENCE] Saved: {evidence_path}")
            return enriched
        except Exception as exc:
            print(f"[EVIDENCE] Error saving {event.type.value}: {exc}", file=sys.stderr)
            if reserved_path is not None:
                try:
                    reserved_path.unlink(missing_ok=True)
                except OSError as cleanup_exc:
                    print(f"[EVIDENCE] Cannot remove incomplete snapshot: {cleanup_exc}", file=sys.stderr)
            return replace(event, evidence_path=None)
