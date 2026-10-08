"""Paged local evidence thumbnails and an in-app image review dialog."""

from pathlib import Path, PureWindowsPath

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QDialog, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget,
)

from .session import ROOT
from .theme import SEVERITY_COLORS, card, label

PAGE_SIZE = 8
THUMBNAIL_SIZE = QSize(360, 180)
REVIEW_SIZE = QSize(1600, 1000)
SUPPORTED_IMAGES = frozenset({".jpg", ".jpeg", ".png"})


def resolve_evidence_path(value, session_directory=None):
    """Resolve engine paths locally; reject traversal, network paths and URIs."""
    try:
        return _resolve_evidence_path(value, session_directory)
    except (OSError, ValueError, RuntimeError):
        return None


def _resolve_evidence_path(value, session_directory=None):
    if not value:
        return None
    text = str(value)
    if "\x00" in text:
        return None
    windows = PureWindowsPath(text)
    if text.startswith(("//", "\\\\")) or (":" in text and not windows.is_absolute()):
        return None
    path = Path(text)
    if ".." in path.parts or ".." in windows.parts or path.suffix.lower() not in SUPPORTED_IMAGES:
        return None
    directory = Path(session_directory).resolve() if session_directory else None
    if path.is_absolute():
        resolved = path.resolve()
        # Current engine paths are trusted absolute paths. Completed sessions
        # constrain them to their directory, including symlink targets.
        if directory is not None and not resolved.is_relative_to(directory):
            return None
        return resolved
    if windows.drive:
        return None
    if path.parts and path.parts[0] == "sessions":
        base = ROOT
    elif directory is not None:
        base = directory
    else:
        return None
    resolved = (base / path).resolve()
    if directory is not None and not resolved.is_relative_to(directory):
        return None
    return resolved if resolved.is_relative_to(base.resolve()) else None


def read_scaled_image(path, target):
    """Bound decoded thumbnail size before creating a GUI-thread QPixmap."""
    if path is None:
        return None, "Evidence path is unavailable or invalid."
    try:
        if not path.is_file():
            return None, "Evidence image is missing."
        reader = QImageReader(str(path))
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid():
            reader.setScaledSize(size.scaled(target, Qt.AspectRatioMode.KeepAspectRatio))
        image = reader.read()
        if image.isNull():
            return None, "Evidence image cannot be read."
        return image, ""
    except (OSError, ValueError, RuntimeError):
        return None, "Evidence image cannot be read."


def frame_source_note(event):
    metadata = event.metadata or {}
    if metadata.get("evidence_frame_source") == "last_usable":
        age = metadata.get("last_usable_age_seconds")
        if isinstance(age, (int, float)):
            return f"Last usable camera frame · {age:.1f}s before event"
        return "Last usable camera frame before obstruction"
    return ""


class EvidenceDialog(QDialog):
    def __init__(self, path, event, title, timestamp, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Evidence · {title}")
        self.resize(1020, 720)
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.addWidget(label(title, size=22))
        root.addWidget(label(f"{timestamp} (UTC+5) · {event.severity.value.upper()} · {event.risk_delta or 0:+d} risk", role="muted"))
        if event.confidence is not None:
            root.addWidget(label(f"Confidence: {event.confidence:.0%}", role="muted"))
        source = frame_source_note(event)
        if source:
            root.addWidget(label(source, role="muted"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self._source = None
        image, error = read_scaled_image(path, REVIEW_SIZE)
        self.error = error
        if image is not None:
            self._source = QPixmap.fromImage(image)
        else:
            self.image_label.setText(error)
        scroll.setWidget(self.image_label)
        root.addWidget(scroll, 1)
        message = label(event.message, role="muted")
        message.setWordWrap(True)
        root.addWidget(message)
        close = QPushButton("CLOSE")
        close.clicked.connect(self.accept)
        root.addWidget(close, 0, Qt.AlignmentFlag.AlignRight)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._redraw()

    def showEvent(self, event):
        super().showEvent(event)
        self._redraw()

    def _redraw(self):
        if self._source is not None:
            self.image_label.setPixmap(self._source.scaled(
                self.image_label.size(), Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))


class EvidenceGallery(QWidget):
    def __init__(self, event_labels, time_formatter, parent=None):
        super().__init__(parent)
        self._labels = event_labels
        self._time = time_formatter
        self.events = ()
        self.session_directory = None
        self.page_index = 0
        self.cards = []
        self.dialog = None
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        self.description = label("No evidence images in this session.", role="muted")
        self.description.setWordWrap(True)
        root.addWidget(self.description)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.container = QWidget()
        self.grid = QGridLayout(self.container)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(14)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1)
        self.scroll.setWidget(self.container)
        root.addWidget(self.scroll, 1)
        controls = QHBoxLayout()
        self.previous_button = QPushButton("PREVIOUS")
        self.previous_button.clicked.connect(lambda: self.show_page(self.page_index - 1))
        self.next_button = QPushButton("NEXT")
        self.next_button.clicked.connect(lambda: self.show_page(self.page_index + 1))
        self.page_label = label("Page 0 / 0", role="muted")
        controls.addWidget(self.previous_button)
        controls.addStretch()
        controls.addWidget(self.page_label)
        controls.addStretch()
        controls.addWidget(self.next_button)
        root.addLayout(controls)
        self.clear()

    def clear(self):
        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None
        self.events = ()
        self.session_directory = None
        self.description.setText("No evidence images in this session. Security and authorized-break events have no webcam image.")
        self.show_page(0)

    def set_result(self, result):
        self.clear()
        self.session_directory = result.session_directory
        self.events = tuple(event for event in result.events if event.evidence_path)
        without_image = len(result.events) - len(self.events)
        self.description.setText(
            f"{len(self.events)} evidence image(s) · {without_image} event(s) without an image. "
            "Security and authorized-break events have no webcam image. Select a thumbnail to review."
        )
        self.show_page(0)

    def show_page(self, index):
        pages = (len(self.events) + PAGE_SIZE - 1) // PAGE_SIZE
        self.page_index = max(0, min(index, pages - 1))
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self.cards = []
        start = self.page_index * PAGE_SIZE
        for offset, event in enumerate(self.events[start:start + PAGE_SIZE]):
            panel, layout = card()
            layout.setContentsMargins(16, 14, 16, 14)
            title = self._labels.get(event.type, str(event.type))
            heading = label(title, size=17)
            heading.setWordWrap(True)
            layout.addWidget(heading)
            layout.addWidget(label(self._time(event.timestamp) + " · UTC+5", role="muted"))
            facts = label(f"{event.severity.value.upper()} · {event.risk_delta or 0:+d} risk")
            facts.setStyleSheet(f"color: {SEVERITY_COLORS.get(event.severity.value, '#91a3bb')}; font-weight: 600;")
            layout.addWidget(facts)
            source = frame_source_note(event)
            if source:
                source_label = label(source, role="muted")
                source_label.setWordWrap(True)
                layout.addWidget(source_label)
            button = QPushButton()
            button.setMinimumHeight(160)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            path = resolve_evidence_path(event.evidence_path, self.session_directory)
            image, error = read_scaled_image(path, THUMBNAIL_SIZE)
            if image is not None:
                button.setIcon(QIcon(QPixmap.fromImage(image)))
                button.setIconSize(THUMBNAIL_SIZE)
                button.setToolTip("Open evidence image")
            else:
                button.setText(error)
                button.setToolTip(error)
            button.clicked.connect(lambda checked=False, selected=event: self.open_event(selected))
            layout.addWidget(button)
            if event.confidence is not None:
                layout.addWidget(label(f"Confidence: {event.confidence:.0%}", role="muted"))
            self.cards.append((event, panel, button, error))
            self.grid.addWidget(panel, offset // 2, offset % 2)
        if not self.events:
            empty = label("No evidence to display.", role="muted")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.grid.addWidget(empty, 0, 0, 1, 2)
        self.page_label.setText(f"Page {self.page_index + 1 if pages else 0} / {pages}")
        self.previous_button.setEnabled(self.page_index > 0)
        self.next_button.setEnabled(self.page_index + 1 < pages)
        self.scroll.verticalScrollBar().setValue(0)

    def open_event(self, event):
        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
        path = resolve_evidence_path(event.evidence_path, self.session_directory)
        self.dialog = EvidenceDialog(path, event, self._labels.get(event.type, str(event.type)),
                                     self._time(event.timestamp), self)
        self.dialog.show()
        return self.dialog
