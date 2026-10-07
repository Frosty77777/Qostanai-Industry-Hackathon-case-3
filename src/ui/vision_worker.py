"""Reuse the existing backend off the GUI thread, with bounded frame delivery."""

from collections import deque
from dataclasses import replace
import importlib.util
import sys
from queue import Empty, Full, Queue
from threading import Event, Lock
from time import perf_counter

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QImage

from evidence import EvidenceManager
from main import draw_preview, format_event, frame_signals, open_webcam, STATUS_FOOTER_HEIGHT
from monitoring import EventEngine, EventType, RiskEngine
from monitoring.break_manager import BreakManager
from security import SecurityMonitor, WindowsSecurityBackend
from vision.face_tracker import FaceResult, FaceStatus, FaceTracker
from vision.camera_quality import RecentUsableFrame, analyze_camera_frame
from vision.yolo_detector import YoloDetector
from .session import FrameUpdate, SessionConfig, SessionResult


def frame_to_image(frame) -> QImage:
    """Deep-copy BGR pixels so Qt owns memory after the OpenCV frame is reused."""
    import numpy as np

    frame = np.ascontiguousarray(frame)
    height, width = frame.shape[:2]
    return QImage(frame.data, width, height, frame.strides[0], QImage.Format.Format_BGR888).copy()


class UpdateMailbox:
    """Retain the newest frame and every event without an unbounded Qt queue."""

    def __init__(self):
        self._lock = Lock()
        self._pending = None

    def publish(self, update: FrameUpdate) -> bool:
        with self._lock:
            notify = self._pending is None
            if self._pending is not None:
                update = replace(update, events=self._pending.events + update.events)
            self._pending = update
            return notify

    def take(self) -> FrameUpdate | None:
        with self._lock:
            update, self._pending = self._pending, None
            return update


class CameraDiscoveryWorker(QThread):
    """Probe actual OpenCV indices in the background; release every probe."""

    discovered = Signal(object, object)

    def __init__(self, config: SessionConfig, parent=None, *, indices=range(5)):
        super().__init__(parent)
        self.config, self.indices = config, indices

    def run(self):
        cameras = []
        statuses = {
            "Camera": ("UNAVAILABLE", "No usable camera found"),
            "AI Detection": ("UNAVAILABLE", "YOLO dependencies or local weights missing"),
            "Face Tracking": ("UNAVAILABLE", "MediaPipe or local face model missing"),
            "Security Monitor": ("READY" if sys.platform == "win32" else "UNAVAILABLE",
                                 "Windows hooks and focus are checked when the session starts"),
        }
        try:
            if self.config.weights.is_file() and all(importlib.util.find_spec(name) for name in ("torch", "ultralytics")):
                statuses["AI Detection"] = ("READY", "Dependencies and local model found; inference checked at start")
            if self.config.face_model.is_file() and importlib.util.find_spec("mediapipe"):
                statuses["Face Tracking"] = ("READY", "Local face model found; tracker checked at start")
            import cv2

            for index in self.indices:
                if self.isInterruptionRequested():
                    break
                capture = None
                try:
                    capture, _ = open_webcam(cv2, index)
                    cameras.append((index, f"Camera {index}" + (" · Default" if index == 0 else "")))
                except Exception:
                    continue
                finally:
                    if capture is not None:
                        capture.release()
            if cameras:
                statuses["Camera"] = ("READY", "Camera opened and returned a usable frame")
        except Exception as exc:
            statuses["Camera"] = ("ERROR", str(exc))
        self.discovered.emit(cameras, statuses)


class VisionWorker(QThread):
    """All capture, inference, event/risk scoring and evidence I/O live here."""

    session_started = Signal(float)
    components_changed = Signal(object)
    update_ready = Signal(object)
    failed = Signal(str)
    notice = Signal(str)

    def __init__(self, config: SessionConfig, parent=None, *, clock=perf_counter,
                 camera_opener=open_webcam, detector_factory=YoloDetector,
                 face_factory=FaceTracker, security_factory=None, evidence_factory=EvidenceManager):
        super().__init__(parent)
        self.config, self._clock = config, clock
        self._camera_opener, self._detector_factory = camera_opener, detector_factory
        self._face_factory, self._evidence_factory = face_factory, evidence_factory
        self._security_factory = security_factory or self._make_security
        self._stop_requested = Event()
        self._mailbox = UpdateMailbox()
        self._break_commands = Queue(maxsize=8)
        self.result: SessionResult | None = None
        self.statuses = {name: ("CHECKING", "Initializing") for name in (
            "Camera", "AI Detection", "Face Tracking", "Security Monitor",
        )}

    def _make_security(self):
        return SecurityMonitor(self.config.security, backend_factory=lambda config: WindowsSecurityBackend(
            config, window_handle=self.config.window_handle,
        ))

    def request_stop(self):
        self._stop_requested.set()
        self.requestInterruption()

    def take_update(self):
        return self._mailbox.take()

    def request_authorized_break(self, pin, duration_seconds):
        """Queue a command; only the worker's BreakManager may authorize it."""
        return self._queue_break_command(("start", pin, duration_seconds))

    def request_end_break(self):
        return self._queue_break_command(("end", None, None))

    def _queue_break_command(self, command):
        if self._stopping():
            return False
        try:
            self._break_commands.put_nowait(command)
            return True
        except Full:
            self.notice.emit("Break request pending; please wait")
            return False

    def _update_break(self, manager, engine, now):
        transitions = []
        expired = manager.expire(now=now)
        if expired is not None:
            transitions.append(expired)
        while True:
            try:
                command, pin, duration = self._break_commands.get_nowait()
            except Empty:
                break
            if command == "start":
                transition = manager.start(pin, duration, now=now)
                if transition is None:
                    self.notice.emit("Break not authorized: invalid PIN, duration, or an existing break")
            else:
                transition = manager.end(now=now)
            if transition is not None:
                transitions.append(transition)
        if transitions:
            engine.reset_condition(EventType.FACE_MISSING)
        return engine.record_session_events(transitions)

    def _stopping(self):
        return self._stop_requested.is_set() or self.isInterruptionRequested()

    def _status(self, name, state, detail=""):
        value = (state, detail)
        if self.statuses.get(name) != value:
            self.statuses[name] = value
            self.components_changed.emit(dict(self.statuses))

    def _cleanup(self, resource, method):
        try:
            getattr(resource, method)()
        except Exception as exc:
            self.notice.emit(f"Component cleanup failed: {exc}")
            return str(exc)
        return None

    def _security_status(self, monitor):
        if monitor.focus_available and monitor.keyboard_available:
            self._status("Security Monitor", "ACTIVE", "Focus and keyboard detection active; blocking disabled by default")
        elif monitor.focus_available or monitor.keyboard_available:
            self._status("Security Monitor", "ACTIVE", "Partial monitoring: " + (
                "focus only; keyboard unavailable" if monitor.focus_available else "keyboard only; focus unavailable"))
        else:
            self._status("Security Monitor", "UNAVAILABLE", "Security monitoring unavailable; CV continues")

    def run(self):
        capture = tracker = security = None
        events = EventEngine(self.config.events, clock=self._clock)
        risk = RiskEngine(self.config.risk)
        breaks = BreakManager(self.config.breaks)
        recent_frame = RecentUsableFrame(self.config.camera_quality)
        started_at = None
        error = None
        try:
            if self._stopping():
                return
            import cv2

            cv2.setNumThreads(1)
            try:
                capture, first_frame = self._camera_opener(cv2, self.config.camera_index)
                self._status("Camera", "READY", f"Camera {self.config.camera_index} initialized")
            except Exception as exc:
                self._status("Camera", "ERROR", str(exc))
                raise
            if self._stopping():
                return
            try:
                detector = self._detector_factory(self.config.weights, self.config.image_size,
                                                   self.config.confidence, self.config.threads)
                self._status("AI Detection", "READY", "Local YOLO11n loaded on CPU")
            except Exception as exc:
                self._status("AI Detection", "ERROR", str(exc))
                raise
            if self._stopping():
                return
            try:
                tracker = self._face_factory(self.config.face_model)
                self._status("Face Tracking", "READY" if tracker.available else "UNAVAILABLE",
                             "MediaPipe initialized" if tracker.available else str(tracker.error))
            except Exception as exc:
                self._status("Face Tracking", "UNAVAILABLE", str(exc))
                self.notice.emit(f"Face tracking unavailable: {exc}")
            if self._stopping():
                return
            try:
                security = self._security_factory()
                security.start("AI Exam Guard")
                self._security_status(security)
            except Exception as exc:
                self._status("Security Monitor", "UNAVAILABLE", str(exc))
                self.notice.emit(f"Security monitoring unavailable: {exc}")
                if security is not None:
                    self._cleanup(security, "stop")
                    security = None
            evidence = self._evidence_factory(self.config.evidence)
            if self._stopping():
                return
            started_at = self._clock()
            self.session_started.emit(started_at)
            durations = deque(maxlen=30)
            frame_count = 0
            while not self._stopping():
                tick = self._clock()
                if frame_count == 0:
                    frame = first_frame
                else:
                    ok, frame = capture.read()
                    if not ok or frame is None or not frame.size:
                        self._status("Camera", "ERROR", "Camera stopped delivering frames")
                        raise RuntimeError("Camera disconnected or stopped delivering frames. Session stopped.")
                if self._stopping():
                    break
                # Quality must be measured before the renderer mutates pixels.
                quality = analyze_camera_frame(frame, self.config.camera_quality)
                try:
                    detections = detector.detect(frame)
                except Exception as exc:
                    self._status("AI Detection", "ERROR", str(exc))
                    raise RuntimeError(f"AI detection failed. Session stopped: {exc}") from exc
                face = FaceResult(FaceStatus.UNAVAILABLE, None)
                if tracker is not None and tracker.available:
                    try:
                        face = tracker.process(frame)
                    except Exception as exc:
                        self._status("Face Tracking", "UNAVAILABLE", str(exc))
                        self._cleanup(tracker, "close")
                        tracker = None
                if face.status == FaceStatus.UNAVAILABLE:
                    detail = str(tracker.error) if tracker is not None and tracker.error else self.statuses["Face Tracking"][1]
                    self._status("Face Tracking", "UNAVAILABLE", detail)
                now = self._clock()
                new_events = self._update_break(breaks, events, now)
                signals = replace(frame_signals(detections, face, frame.shape, events.config),
                                  camera_obstructed=quality.obstructed, camera_quality=quality.to_dict(),
                                  authorized_break=breaks.active)
                new_events.extend(events.update(signals, now=now))
                missing_warning = events.condition_qualified(EventType.FACE_MISSING)
                obstruction_warning = events.condition_qualified(EventType.CAMERA_OBSTRUCTED)
                if security is not None:
                    try:
                        new_events.extend(events.record_security_events(security.poll()))
                        self._security_status(security)
                    except Exception as exc:
                        self._status("Security Monitor", "UNAVAILABLE", str(exc))
                        self.notice.emit(f"Security monitoring unavailable: {exc}")
                        self._cleanup(security, "stop")
                        security = None
                fps = len(durations) / sum(durations) if durations else None
                # Existing renderer draws once: its permanent footer goes only
                # into evidence, while the desktop receives the annotated image.
                annotated = draw_preview(cv2, frame, detections, fps, face, event_config=events.config)
                evidence_frame = annotated[:frame.shape[0] + STATUS_FOOTER_HEIGHT]
                recent_frame.remember(evidence_frame, quality, now)
                enriched = []
                for event in new_events:
                    event = risk.process(event)
                    print(format_event(event, risk.current_score))
                    snapshot = evidence_frame
                    if event.type == EventType.CAMERA_OBSTRUCTED:
                        snapshot, source = recent_frame.evidence_frame(evidence_frame, now)
                        event = replace(event, metadata={**(event.metadata or {}), **source})
                    event = evidence.capture(event, snapshot)
                    if evidence.should_capture(event) and event.evidence_path is None:
                        self.notice.emit(f"Evidence save failed for {event.type.value}; event retained")
                    enriched.append(event)
                if enriched:
                    events.history[-len(enriched):] = enriched
                head_type = EventType.__members__.get(f"LOOK_{face.head_direction.value}")
                update = FrameUpdate(
                    frame_to_image(annotated[:frame.shape[0]]), face, signals.person_count,
                    signals.phone_detected, fps, tuple(enriched), risk.current_score,
                    risk.current_level.value, missing_warning, dict(self.statuses),
                    camera_obstructed_warning=obstruction_warning,
                    break_active=breaks.active, break_remaining_seconds=breaks.remaining_seconds(now),
                    head_duration_seconds=events.condition_duration(head_type) if head_type else 0.0,
                    head_threshold_seconds=events.config.rules[head_type].threshold_seconds if head_type else None,
                )
                if self._mailbox.publish(update):
                    self.update_ready.emit(update)
                frame_count += 1
                if frame_count > 5:
                    durations.append(max(self._clock() - tick, 1e-9))
        except Exception as exc:
            error = str(exc)
            self.failed.emit(error)
        finally:
            self._stop_requested.set()
            # Drop any queued PIN values as soon as the session stops.
            while not self._break_commands.empty():
                try:
                    self._break_commands.get_nowait()
                except Empty:
                    break
            # Independent cleanup ensures one component failure cannot leak the
            # camera or hook. Finished is emitted by QThread only after run exits.
            for resource, cleanup in ((security, "stop"), (tracker, "close"), (capture, "release")):
                if resource is not None:
                    try:
                        getattr(resource, cleanup)()
                    except Exception as exc:
                        detail = f"Cleanup failed: {exc}"
                        self.notice.emit(detail)
                        error = error or detail
            self.result = SessionResult(
                self.config.student, self.config.exam,
                0.0 if started_at is None else max(0.0, self._clock() - started_at),
                risk.current_score, risk.current_level.value, tuple(events.history),
                dict(risk.event_counts), error,
            )
