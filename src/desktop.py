"""Launch AI Exam Guard's local PySide6 desktop shell."""

from dataclasses import replace
import signal
import sys

from evidence import EvidenceConfig
from main import event_config_from_args, parse_args
from security.secure_window import SecureWindowConfig
from vision.face_tracker import prepare_face_tracking_runtime


def main() -> int:
    args = parse_args(__doc__)
    # Help exits during parsing. Import only the optional runtime here, before
    # Qt's feature hook; camera and model initialization stay on VisionWorker.
    try:
        prepare_face_tracking_runtime()
    except Exception as exc:
        print(f"Face tracking startup unavailable ({type(exc).__name__}: {exc}). "
              "Desktop monitoring will continue with technical status details.", file=sys.stderr)
    try:
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QApplication
        from ui.main_window import MainWindow
        from ui.session import SessionConfig
    except (ImportError, OSError) as exc:
        print(f"Desktop UI unavailable ({exc}). Install requirements.txt in this environment.", file=sys.stderr)
        return 1
    app = QApplication([sys.argv[0]])
    app.setApplicationName("AI Exam Guard")
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    config = replace(SessionConfig(), weights=args.weights, face_model=args.face_model,
                     image_size=args.imgsz, confidence=args.conf, threads=args.threads,
                     events=event_config_from_args(args),
                     evidence=EvidenceConfig(head_direction_enabled=args.head_evidence))
    # Keep older programmatic configuration adapters usable while the current
    # desktop always requires a maximized/fullscreen secure student window.
    secure_overrides = {}
    if hasattr(config, "secure_mode"):
        secure_overrides["secure_mode"] = "MAXIMIZED"
    if hasattr(config, "secure_window"):
        secure_overrides["secure_window"] = SecureWindowConfig(required=True)
    if secure_overrides:
        config = replace(config, **secure_overrides)
    window = MainWindow(config)
    app.aboutToQuit.connect(window.shutdown_blocking)
    signal.signal(signal.SIGINT, lambda *_: window.close())
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
