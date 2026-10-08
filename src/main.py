"""Local YOLO and MediaPipe webcam preview. Supports Python 3.12; Q exits."""

import argparse
from collections import deque
from dataclasses import replace
import os
from pathlib import Path
import sys
from time import perf_counter

from evidence import EvidenceConfig, EvidenceManager
from monitoring import EventEngine, EventEngineConfig, EventType, FrameSignals, PhoneDetectionConfig, ProctoringEvent, RiskEngine
from security import SecurityMonitor
from vision.yolo_detector import Detection, PHONE_ID, PERSON_ID, YoloDetector
from vision.face_tracker import DEFAULT_MODEL_PATH, FaceResult, FaceStatus, FaceTracker
from vision.detection_filters import display_detections, secondary_person_confidence
from vision.phone_tracker import PhoneTracker

ROOT = Path(__file__).resolve().parents[1]
WINDOW_NAME = "Local proctoring prototype - Q to quit"
FACE_MISSING_WARNING_TEXT = "WARNING: STUDENT LEFT CAMERA VIEW"
STATUS_FOOTER_HEIGHT = 100
DEFAULT_EVENT_CONFIG = EventEngineConfig()


def parse_args(description: str | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description or __doc__)
    parser.add_argument("--weights", type=Path, default=ROOT / "models" / "yolo11n.pt")
    parser.add_argument("--imgsz", type=int, default=416, help="Inference size, multiple of 32 (default: 416)")
    parser.add_argument("--conf", type=float, default=0.35, help="Detection confidence threshold (default: 0.35)")
    parser.add_argument("--threads", type=int, default=min(4, os.cpu_count() or 1), help="PyTorch CPU threads")
    parser.add_argument("--face-model", type=Path, default=DEFAULT_MODEL_PATH, help="Local MediaPipe face_landmarker.task file")
    parser.add_argument("--head-evidence", action="store_true", help="Also save evidence for emitted head-direction events (disabled by default)")
    parser.add_argument("--phone-conf", type=float, default=DEFAULT_EVENT_CONFIG.phone.minimum_confidence,
                        help="Application minimum phone confidence (default: 0.55)")
    parser.add_argument("--phone-occlusion-grace", type=float,
                        default=DEFAULT_EVENT_CONFIG.phone.occlusion_grace_seconds,
                        help="Seconds to retain a previously observed phone through occlusion (default: 0.6)")
    parser.add_argument("--no-phone-tracking", action="store_true",
                        help="Disable temporal phone continuity and use raw confidence-qualified detections")
    parser.add_argument("--secondary-person-min-area", type=float,
                        default=DEFAULT_EVENT_CONFIG.multiple_persons.minimum_secondary_area_ratio,
                        help="Minimum secondary-person area / frame area (default: 0.02)")
    parser.add_argument("--secondary-person-min-width", type=float,
                        default=DEFAULT_EVENT_CONFIG.multiple_persons.minimum_secondary_width_ratio,
                        help="Minimum secondary-person width / frame width (default: 0.05)")
    parser.add_argument("--secondary-person-min-height", type=float,
                        default=DEFAULT_EVENT_CONFIG.multiple_persons.minimum_secondary_height_ratio,
                        help="Minimum secondary-person height / frame height (default: 0.15)")
    args = parser.parse_args()
    if args.imgsz < 32 or args.imgsz % 32:
        parser.error("--imgsz must be a multiple of 32 and at least 32")
    if not 0 < args.conf <= 1:
        parser.error("--conf must be greater than 0 and at most 1")
    if args.threads < 1:
        parser.error("--threads must be at least 1")
    if not 0 < args.phone_conf <= 1:
        parser.error("--phone-conf must be greater than 0 and at most 1")
    from math import isfinite
    if not isfinite(args.phone_occlusion_grace) or args.phone_occlusion_grace < 0:
        parser.error("--phone-occlusion-grace must be finite and nonnegative")
    for name in ("area", "width", "height"):
        if not 0 <= getattr(args, f"secondary_person_min_{name}") <= 1:
            parser.error(f"--secondary-person-min-{name} must be between 0 and 1")
    return args


def event_config_from_args(args) -> EventEngineConfig:
    return replace(DEFAULT_EVENT_CONFIG, phone=replace(DEFAULT_EVENT_CONFIG.phone,
        minimum_confidence=args.phone_conf,
        occlusion_grace_seconds=getattr(args, "phone_occlusion_grace", DEFAULT_EVENT_CONFIG.phone.occlusion_grace_seconds),
        tracking_enabled=not getattr(args, "no_phone_tracking", False)), multiple_persons=replace(
        DEFAULT_EVENT_CONFIG.multiple_persons,
        minimum_secondary_area_ratio=args.secondary_person_min_area,
        minimum_secondary_width_ratio=args.secondary_person_min_width,
        minimum_secondary_height_ratio=args.secondary_person_min_height,
    ))


def open_webcam(cv2, camera_index: int = 0):
    """Open a selected camera; try backends and verify a usable first frame."""
    backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY] if sys.platform == "win32" else [cv2.CAP_ANY]
    for backend in backends:
        capture = None
        try:
            capture = cv2.VideoCapture(camera_index, backend)
            if capture.isOpened():
                capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                capture.set(cv2.CAP_PROP_FPS, 30)
                # Best effort: some Windows camera drivers ignore this setting.
                capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                ok, frame = capture.read()
                if ok and frame is not None and frame.size:
                    return capture, frame
        except cv2.error:
            # A backend error may still allow the next Windows backend to work.
            pass
        if capture is not None:
            capture.release()
    raise RuntimeError(
        f"Cannot open the webcam (index {camera_index}) or read a frame. "
        "Connect/enable the camera, close other camera apps, and enable "
        "Windows Settings > Privacy & security > Camera > "
        "Let desktop apps access your camera."
    )


def frame_signals(
    detections: list[Detection], face: FaceResult, frame_shape: tuple[int, ...],
    config: EventEngineConfig = DEFAULT_EVENT_CONFIG,
) -> FrameSignals:
    """Adapt existing CV outputs without changing either detector."""
    person_count = sum(d.class_id == PERSON_ID for d in detections)
    phones = [d.confidence for d in display_detections(detections, config.phone) if d.class_id == PHONE_ID]
    return FrameSignals(
        phone_detected=bool(phones),
        person_count=person_count,
        face_status=face.status,
        face_count=face.face_count,
        head_direction=face.head_direction,
        phone_confidence=max(phones) if phones else None,
        persons_confidence=secondary_person_confidence(detections, frame_shape, config.multiple_persons),
    )


def format_event(event: ProctoringEvent, session_score: int | None = None) -> str:
    line = (
        f"[{event.timestamp:%H:%M:%S}] {event.severity.value.upper()} | "
        f"{event.type.value} | {event.message} | duration={event.duration:.2f}s"
    )
    if event.risk_delta is not None:
        line += f" | +{event.risk_delta} risk"
        if session_score is not None:
            line += f" | Session risk: {session_score}/100"
    return line


def draw_preview(
    cv2, frame, detections: list[Detection], fps: float | None,
    face: FaceResult | None = None, latest_event: ProctoringEvent | None = None,
    *, face_missing_warning: bool = False, risk_engine: RiskEngine | None = None,
    event_config: EventEngineConfig = DEFAULT_EVENT_CONFIG,
    phone_state: str | None = None,
):
    """Draw boxes, confidence labels, and current-frame detection counts."""
    height, width = frame.shape[:2]
    detections = display_detections(detections, event_config.phone)
    for detection in detections:
        x1, y1, x2, y2 = detection.xyxy
        x1, x2 = (max(0, min(width - 1, x)) for x in (x1, x2))
        y1, y2 = (max(0, min(height - 1, y)) for y in (y1, y2))
        color = (0, 200, 0) if detection.class_id == PERSON_ID else (0, 140, 255)
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        cv2.putText(
            frame, f"{detection.class_name} {detection.confidence:.2f}",
            (x1, max(18, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA,
        )

    person_count = sum(d.class_id == PERSON_ID for d in detections)
    phone_detected = any(d.class_id == PHONE_ID for d in detections)
    fps_text = f"{fps:.1f}" if fps is not None else "warming up"
    lines = [f"Persons: {person_count}", f"Phone detected: {'YES' if phone_detected else 'NO'}", f"FPS: {fps_text} | Q: quit"]
    face_lines = []
    if face is not None:
        if face.status == FaceStatus.UNAVAILABLE:
            face_lines = ["Face: unavailable", "Head: UNKNOWN", "Faces: unavailable"]
        else:
            detected = face.status != FaceStatus.NO_FACE
            face_lines = [
                f"Face: {'detected' if detected else 'not detected'}",
                f"Head: {face.head_direction.value}",
                f"Faces: {face.face_count} ({face.status.value})",
            ]
    # Keep text outside the image so the status area cannot hide YOLO labels.
    show_face_warning = face_missing_warning and face is not None and face.status == FaceStatus.NO_FACE
    risk_footer_height = 60 if risk_engine is not None else 0
    footer_height = STATUS_FOOTER_HEIGHT + risk_footer_height + (60 if latest_event else 0)
    preview = cv2.copyMakeBorder(
        frame, 0, footer_height + (60 if show_face_warning else 0), 0, 0,
        cv2.BORDER_CONSTANT, value=(20, 20, 20),
    )
    for column, column_lines in enumerate((lines, face_lines)):
        for index, line in enumerate(column_lines):
            cv2.putText(preview, line, (10 + column * (width // 2), height + 25 + index * 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    if phone_state is not None:
        # Keep the original permanent labels and 100px evidence footer intact.
        # The desktop also displays the state in its compact diagnostics.
        cv2.putText(preview, f"Phone: {phone_state}", (10, height + STATUS_FOOTER_HEIGHT - 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 180, 255), 1, cv2.LINE_AA)
    if risk_engine is not None:
        risk_lines = [
            f"SESSION RISK: {risk_engine.current_score} / 100",
            f"LEVEL: {risk_engine.current_level.value}",
        ]
        for index, line in enumerate(risk_lines):
            cv2.putText(
                preview, line, (10, height + STATUS_FOOTER_HEIGHT + 20 + index * 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA,
            )
    if latest_event is not None:
        color = (0, 140, 255)
        event_lines = [
            f"Latest: {latest_event.severity.value.upper()} | {latest_event.type.value}",
            f"{latest_event.message} | duration={latest_event.duration:.2f}s",
        ]
        for index, line in enumerate(event_lines):
            cv2.putText(preview, line, (10, height + risk_footer_height + 115 + index * 25), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    if show_face_warning:
        top = height + footer_height
        cv2.rectangle(preview, (0, top), (width - 1, top + 59), (0, 0, 180), -1)
        text_width = cv2.getTextSize(FACE_MISSING_WARNING_TEXT, cv2.FONT_HERSHEY_SIMPLEX, 1.0, 2)[0][0]
        scale = min(0.7, (width - 20) / text_width)
        cv2.putText(
            preview, FACE_MISSING_WARNING_TEXT, (10, top + 38),
            cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 2, cv2.LINE_AA,
        )
    return preview


def main() -> int:
    args = parse_args()
    if sys.version_info < (3, 11):
        print(
            f"Error: Python 3.11 or newer is required; found {sys.version.split()[0]}.",
            file=sys.stderr,
        )
        return 1
    try:
        import cv2
    except (ImportError, OSError) as exc:
        print(
            f"Error: OpenCV is unavailable ({exc}). Install requirements.txt in your Python environment.",
            file=sys.stderr,
        )
        return 1
    
    capture = None
    face_tracker = None
    window_created = False
    frame_count = 0
    durations: deque[float] = deque(maxlen=30)
    measured_frames = 0
    measured_seconds = 0.0
    event_engine = EventEngine(event_config_from_args(args))
    phone_tracker = PhoneTracker(event_engine.config.phone) if event_engine.config.phone.tracking_enabled else None
    risk_engine = RiskEngine()
    security_monitor = SecurityMonitor()
    evidence_manager = EvidenceManager(EvidenceConfig(head_direction_enabled=args.head_evidence))
    latest_event = None
    latest_event_at = None
    face_missing_warning = False
    try:
        # Avoid competing OpenCV and PyTorch thread pools during inference.
        cv2.setNumThreads(1)
        detector = YoloDetector(args.weights, args.imgsz, args.conf, args.threads)
        face_tracker = FaceTracker(args.face_model)
        if not face_tracker.available:
            print(f"Warning: Face tracking unavailable ({face_tracker.error}). YOLO continues.", file=sys.stderr)
        capture, first_frame = open_webcam(cv2)
        cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_AUTOSIZE)
        window_created = True
        security_monitor.start(WINDOW_NAME)
        print("Webcam opened. Focus the preview and press Q to quit.")
        while True:
            started = perf_counter()
            if frame_count == 0:
                frame = first_frame
            else:
                ok, frame = capture.read()
                if not ok or frame is None or not frame.size:
                    raise RuntimeError("Webcam stopped delivering frames. Check the camera connection and other camera apps.")
            detections = detector.detect(frame)
            face_was_available = face_tracker.available
            face = face_tracker.process(frame)
            if face_was_available and not face_tracker.available:
                print(f"Warning: Face tracking stopped ({face_tracker.error}). YOLO continues.", file=sys.stderr)
            event_now = perf_counter()
            # The warning belongs to the emitted absence episode, not its
            # expiring latest-event banner. Returned faces and failures clear it.
            if face.status != FaceStatus.NO_FACE:
                face_missing_warning = False
            signals = frame_signals(detections, face, frame.shape, event_engine.config)
            phone_tracking = phone_tracker.update(detections, frame.shape, now=event_now) if phone_tracker else None
            if phone_tracking is not None:
                signals = phone_tracking.apply_to(signals)
            new_events = event_engine.update(signals, now=event_now)
            face_missing_warning = event_engine.condition_qualified(EventType.FACE_MISSING)
            new_events.extend(event_engine.record_security_events(security_monitor.poll()))
            for index, event in enumerate(new_events):
                event = risk_engine.process(event)
                new_events[index] = event
                print(format_event(event, risk_engine.current_score))
                latest_event = event
                latest_event_at = event_now
                if event.type == EventType.FACE_MISSING:
                    face_missing_warning = True
            if latest_event_at is not None and event_now - latest_event_at >= event_engine.config.latest_event_display_seconds:
                latest_event = None
                latest_event_at = None
            fps = len(durations) / sum(durations) if durations else None
            preview = draw_preview(
                cv2, frame, detections, fps, face, latest_event,
                face_missing_warning=face_missing_warning, risk_engine=risk_engine, event_config=event_engine.config,
                phone_state=phone_tracking.state.value if phone_tracking is not None else None,
            )
            if new_events:
                # The first footer contains permanent CV labels. Both temporary
                # banners sit below this crop; reuse the drawing already done.
                evidence_frame = preview[:frame.shape[0] + STATUS_FOOTER_HEIGHT]
                new_events = [evidence_manager.capture(event, evidence_frame) for event in new_events]
                # Events are immutable: keep enriched objects in session history
                # and the latest-event reference without changing engine state.
                event_engine.history[-len(new_events):] = new_events
                latest_event = new_events[-1]
            cv2.imshow(WINDOW_NAME, preview)
            key = cv2.waitKey(1) & 0xFF
            elapsed = max(perf_counter() - started, 1e-9)
            frame_count += 1
            # Exclude the predictor's first-call setup and initial warmup.
            if frame_count > 5:
                durations.append(elapsed)
                measured_frames += 1
                measured_seconds += elapsed
            if key in (ord("q"), ord("Q")) or cv2.getWindowProperty(WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
                break
        return 0
    except KeyboardInterrupt:
        return 0
    except ImportError as exc:
        print(f"Error: Missing vision dependency ({exc}). Install requirements.txt in your Python environment.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        security_monitor.stop()
        if face_tracker is not None:
            face_tracker.close()
        if capture is not None:
            capture.release()
        if window_created:
            cv2.destroyAllWindows()
        if measured_frames:
            print(f"Average preview FPS (excluding first 5 frames): {measured_frames / measured_seconds:.1f}")


if __name__ == "__main__":
    raise SystemExit(main())
