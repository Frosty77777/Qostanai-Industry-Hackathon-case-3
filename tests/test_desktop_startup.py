"""Desktop optional-runtime startup order, without cameras or native tasks."""

import builtins
import contextlib
from dataclasses import dataclass
import io
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import desktop
from vision import face_tracker


@dataclass
class StartupConfig:
    weights: object = None
    face_model: object = None
    image_size: int = 416
    confidence: float = 0.35
    threads: int = 1
    events: object = None
    evidence: object = None


class StartupHarness:
    """Trace the real entry point while replacing only the desktop surfaces."""

    def __init__(self):
        self.order = []
        self.app = Mock()
        self.app.exec.return_value = 0
        self.window = Mock()
        self.application = Mock(return_value=self.app)
        self.window_factory = Mock(side_effect=self.create_window)
        self.modules = {
            "PySide6.QtGui": SimpleNamespace(QFont=Mock()),
            "PySide6.QtWidgets": SimpleNamespace(QApplication=self.application),
            "ui.main_window": SimpleNamespace(MainWindow=self.window_factory),
            "ui.session": SimpleNamespace(SessionConfig=StartupConfig),
        }
        self.original_import = builtins.__import__

    def create_window(self, config):
        self.order.append("window")
        return self.window

    def import_module(self, name, globals=None, locals=None, fromlist=(), level=0):
        if name in self.modules:
            self.order.append(name)
            return self.modules[name]
        return self.original_import(name, globals, locals, fromlist, level)

    @contextlib.contextmanager
    def installed(self, prepare, argv=None):
        with (
            patch.object(sys, "argv", argv or ["desktop.py"]),
            patch.object(desktop, "prepare_face_tracking_runtime", side_effect=prepare) as warmup,
            patch.object(desktop.signal, "signal"),
            patch("builtins.__import__", side_effect=self.import_module),
        ):
            yield warmup


class DesktopStartupChecks(unittest.TestCase):
    def test_optional_runtime_import_precedes_qt_and_window_creation(self):
        harness = StartupHarness()
        with harness.installed(lambda: harness.order.append("mediapipe")) as warmup:
            self.assertEqual(desktop.main(), 0)

        self.assertEqual(harness.order[0], "mediapipe")
        self.assertLess(harness.order.index("mediapipe"), harness.order.index("PySide6.QtGui"))
        self.assertLess(harness.order.index("mediapipe"), harness.order.index("ui.main_window"))
        warmup.assert_called_once_with()
        harness.window.show.assert_called_once_with()
        harness.app.exec.assert_called_once_with()
        harness.app.aboutToQuit.connect.assert_called_once_with(harness.window.shutdown_blocking)

    def test_feature_import_error_logs_technical_warning_and_still_starts_desktop(self):
        harness = StartupHarness()

        def unavailable():
            harness.order.append("mediapipe")
            raise AttributeError("'_SixMetaPathImporter' object has no attribute '_path'")

        output = io.StringIO()
        with harness.installed(unavailable), contextlib.redirect_stderr(output):
            self.assertEqual(desktop.main(), 0)

        self.assertEqual(harness.order[0], "mediapipe")
        self.assertIn("Face tracking startup unavailable", output.getvalue())
        self.assertIn("AttributeError", output.getvalue())
        self.assertIn("_SixMetaPathImporter", output.getvalue())
        harness.window.show.assert_called_once_with()
        harness.app.exec.assert_called_once_with()

    def test_missing_optional_runtime_does_not_prevent_desktop_startup(self):
        harness = StartupHarness()
        output = io.StringIO()
        with harness.installed(Mock(side_effect=ImportError("mediapipe unavailable"))), contextlib.redirect_stderr(output):
            self.assertEqual(desktop.main(), 0)

        self.assertIn("mediapipe unavailable", output.getvalue())
        harness.window_factory.assert_called_once()
        harness.app.exec.assert_called_once_with()

    def test_help_exits_before_loading_optional_runtime_or_qt(self):
        harness = StartupHarness()
        output = io.StringIO()
        with harness.installed(Mock(), ["desktop.py", "--help"]) as warmup, contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as stopped:
                desktop.main()

        self.assertEqual(stopped.exception.code, 0)
        warmup.assert_not_called()
        self.assertEqual(harness.order, [])
        harness.application.assert_not_called()
        self.assertIn("--face-model", output.getvalue())

    def test_invalid_arguments_exit_before_runtime_or_qt_initialization(self):
        harness = StartupHarness()
        with harness.installed(Mock(), ["desktop.py", "--imgsz", "31"]) as warmup, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stopped:
                desktop.main()

        self.assertEqual(stopped.exception.code, 2)
        warmup.assert_not_called()
        self.assertEqual(harness.order, [])
        harness.application.assert_not_called()

    def test_runtime_preparation_imports_package_without_creating_face_model(self):
        runtime = ModuleType("mediapipe")
        model_factory = Mock()
        runtime.tasks = SimpleNamespace(vision=SimpleNamespace(
            FaceLandmarker=SimpleNamespace(create_from_options=model_factory),
        ))
        with patch.dict(sys.modules, {"mediapipe": runtime}), patch.object(face_tracker, "FaceTracker") as tracker:
            self.assertIs(face_tracker.prepare_face_tracking_runtime(), runtime)

        model_factory.assert_not_called()
        tracker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
