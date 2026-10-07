"""CPU-only YOLO11n inference for COCO persons and cell phones."""

from dataclasses import dataclass
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

PERSON_ID = 0
PHONE_ID = 67
CLASS_NAMES = {PERSON_ID: "person", PHONE_ID: "cell phone"}


@dataclass(frozen=True)
class Detection:
    class_id: int
    confidence: float
    xyxy: tuple[int, int, int, int]

    @property
    def class_name(self) -> str:
        return CLASS_NAMES[self.class_id]


class YoloDetector:
    """Load local pretrained weights once and reuse the predictor per frame."""

    def __init__(
        self,
        weights: Path,
        image_size: int = 416,
        confidence: float = 0.35,
        threads: int = 4,
    ) -> None:
        weights = Path(weights).expanduser().resolve()
        if not weights.is_file():
            raise FileNotFoundError(
                f"YOLO11n weights not found: {weights}. "
                "Place the official pretrained yolo11n.pt file there; "
                "this application does not download models. See README.md."
            )
        if weights.name != "yolo11n.pt":
            raise ValueError("Use the official COCO detection weights named yolo11n.pt.")
        if image_size < 32 or image_size % 32:
            raise ValueError("Image size must be a positive multiple of 32 (at least 32).")
        if not 0 < confidence <= 1:
            raise ValueError("Confidence must be greater than 0 and at most 1.")
        if threads < 1:
            raise ValueError("CPU thread count must be at least 1.")

        # Set before importing Ultralytics: disable its connectivity probe and
        # automatic dependency installation, including on the first launch.
        os.environ["YOLO_OFFLINE"] = "true"
        os.environ["YOLO_AUTOINSTALL"] = "false"
        config_dir = Path(__file__).resolve().parents[2] / ".ultralytics"
        # Ultralytics falls back to a temp directory if this directory is absent.
        config_dir.mkdir(exist_ok=True)
        os.environ["YOLO_CONFIG_DIR"] = str(config_dir)

        import torch
        from ultralytics import YOLO, settings

        settings.update({"sync": False, "hub": False})
        torch.set_num_threads(threads)
        try:
            self._model = YOLO(str(weights), task="detect")
            if self._model.task != "detect" or any(
                self._model.names.get(class_id) != name
                for class_id, name in CLASS_NAMES.items()
            ):
                raise ValueError("Weights must contain the COCO object-detection classes.")
        except Exception as exc:
            raise RuntimeError(f"Cannot load pretrained YOLO11n weights: {exc}") from exc

        self.image_size = image_size
        self.confidence = confidence

    def detect(self, frame: "np.ndarray") -> list[Detection]:
        """Return filtered boxes in the original frame's pixel coordinates."""
        result = self._model.predict(
            source=frame,
            device="cpu",
            imgsz=self.image_size,
            conf=self.confidence,
            classes=list(CLASS_NAMES),
            half=False,
            rect=True,
            verbose=False,
            save=False,
            show=False,
        )[0]
        if result.boxes is None:
            return []

        detections = []
        # Transfer once per frame rather than once per coordinate or box.
        for x1, y1, x2, y2, confidence, class_id in result.boxes.data.cpu().tolist():
            class_id = int(class_id)
            if class_id in CLASS_NAMES:
                detections.append(
                    Detection(class_id, float(confidence), (int(x1), int(y1), int(x2), int(y2)))
                )
        return detections
