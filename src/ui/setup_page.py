"""Setup fields and asynchronous preflight status."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QFormLayout, QGridLayout, QHBoxLayout, QLineEdit, QPushButton, QVBoxLayout, QWidget

from .theme import apply_status, card, label


class SetupPage(QWidget):
    start_requested = Signal(str, str, int)
    refresh_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = False
        self._camera_ready = False
        self._ai_ready = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 24, 32, 24)
        outer.addStretch()
        panel, layout = card()
        panel.setMinimumWidth(620)
        panel.setMaximumWidth(700)
        layout.setContentsMargins(32, 26, 32, 26)
        layout.setSpacing(16)
        layout.addWidget(label("AI Exam Guard", role="title"))
        layout.addWidget(label("Local AI-Assisted Proctoring", role="muted"))
        form = QFormLayout()
        form.setSpacing(14)
        self.student_input = QLineEdit()
        self.student_input.setPlaceholderText("Student")
        self.student_input.setMaxLength(100)
        self.exam_input = QLineEdit()
        self.exam_input.setPlaceholderText("Practice Exam")
        self.exam_input.setMaxLength(100)
        self.camera_select = QComboBox()
        self.camera_select.addItem("Checking available cameras…", None)
        form.addRow("Student Name", self.student_input)
        form.addRow("Exam Name", self.exam_input)
        form.addRow("Camera", self.camera_select)
        layout.addLayout(form)
        layout.addWidget(label("SYSTEM STATUS", role="heading"))
        self.status_labels = {}
        status_grid = QGridLayout()
        status_grid.setHorizontalSpacing(24)
        status_grid.setVerticalSpacing(10)
        names = ("Camera", "AI Detection", "Face Tracking", "Security Monitor", "Evidence", "Session")
        for index, name in enumerate(names):
            row = QHBoxLayout()
            row.addWidget(label("YOLO" if name == "AI Detection" else name))
            row.addStretch()
            status = label("CHECKING", role="muted")
            self.status_labels[name] = status
            row.addWidget(status)
            status_grid.addLayout(row, index // 2, index % 2)
        layout.addLayout(status_grid)
        self.message = label("Checking cameras and local model availability…", role="muted")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("RECHECK SYSTEM")
        self.refresh_button.clicked.connect(self.refresh_requested)
        buttons.addWidget(self.refresh_button)
        self.start_button = QPushButton("START SECURE SESSION")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self._start)
        self.start_button.setEnabled(False)
        buttons.addWidget(self.start_button, 1)
        layout.addLayout(buttons)
        outer.addWidget(panel, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch()

    def _start(self):
        index = self.camera_select.currentData()
        if self._busy or not self._camera_ready or not self._ai_ready or index is None:
            self.message.setText("A usable camera and AI detection are required. Recheck the system.")
            return
        self.start_requested.emit(self.student_input.text().strip() or "Student",
                                  self.exam_input.text().strip() or "Practice Exam", index)

    def set_discovery(self, cameras, statuses):
        self.camera_select.clear()
        for index, description in cameras:
            self.camera_select.addItem(description, index)
        if not cameras:
            self.camera_select.addItem("No available camera", None)
        self.set_statuses(statuses)
        if self._camera_ready and self._ai_ready:
            self.message.setText("Ready to start. Component initialization is verified again on start.")
        elif cameras:
            self.message.setText("AI detection is unavailable. Check local YOLO weights and dependencies, then recheck.")
        else:
            self.message.setText("No camera returned a valid frame. Connect a camera and recheck.")

    def set_statuses(self, statuses):
        for name, (state, detail) in statuses.items():
            if name in self.status_labels:
                apply_status(self.status_labels[name], state, detail)
        if "Camera" in statuses:
            self._camera_ready = statuses["Camera"][0] == "READY"
        if "AI Detection" in statuses:
            self._ai_ready = statuses["AI Detection"][0] == "READY"
        self._update_controls()

    def _update_controls(self):
        self.start_button.setEnabled(not self._busy and self._camera_ready and self._ai_ready)
        for widget in (self.student_input, self.exam_input, self.camera_select, self.refresh_button):
            widget.setEnabled(not self._busy)

    def set_busy(self, busy, message=None):
        self._busy = busy
        self._update_controls()
        if message:
            self.message.setText(message)

    def reset(self):
        self.student_input.clear()
        self.exam_input.clear()
        self.set_busy(False)
        self.message.setText("New session. Previous evidence remains saved locally.")
