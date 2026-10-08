"""Setup fields and asynchronous preflight status."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from i18n import LANGUAGE_NAMES, language_manager, tr
from .localized_widgets import QComboBox, QLineEdit, QPushButton

from exams import ExamDefinition, ExamValidationError, load_demo_exam, load_exam

from .assistant_widget import AssistantDock
from .theme import COLORS, COMPACT_INPUT_STYLE, apply_status, card, label, text_style


class SetupPage(QWidget):
    start_requested = Signal(str, str, int)
    refresh_requested = Signal()
    teacher_review_requested = Signal()

    def __init__(self, parent=None, *, assistant_preferences=None, assistant_clock=None,
                 assistant_config=None):
        super().__init__(parent)
        self._busy = False
        self._camera_ready = False
        self._ai_ready = False
        self._selected_exam = None
        self._secure_window_required = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 24, 32, 24)
        outer.addStretch()
        panel, layout = card()
        panel.setMinimumWidth(620)
        panel.setMaximumWidth(700)
        layout.setContentsMargins(32, 26, 32, 26)
        layout.setSpacing(14)
        title = QHBoxLayout()
        title.addWidget(label("AI Exam Guard", role="title"), 1)
        title.addWidget(label("Language"))
        self.language_select = QComboBox()
        self.language_select.setAccessibleName("Language")
        self.language_select.setStyleSheet(COMPACT_INPUT_STYLE)
        self.language_select.setMaximumWidth(140)
        for code, name in LANGUAGE_NAMES.items():
            self.language_select.addRawItem(name, code)
        title.addWidget(self.language_select)
        layout.addLayout(title)
        layout.addWidget(label("Local AI-Assisted Proctoring", role="muted"))
        form = QFormLayout()
        form.setSpacing(12)
        self.student_input = QLineEdit()
        self.student_input.setPlaceholderText("Student")
        self.student_input.setMaxLength(100)
        self.exam_input = QLineEdit()
        self.exam_input.setPlaceholderText("Practice Exam")
        self.exam_input.setMaxLength(100)
        self.camera_select = QComboBox()
        self.camera_select.addItem("Checking available cameras…", None)
        form.addRow(label("Student Name"), self.student_input)
        form.addRow(label("Exam Name"), self.exam_input)
        exam_row = QHBoxLayout()
        self.exam_select = QComboBox()
        self.exam_select.currentIndexChanged.connect(self._exam_selected)
        exam_row.addWidget(self.exam_select, 1)
        self.load_exam_button = QPushButton("LOAD EXAM JSON")
        self.load_exam_button.clicked.connect(self._choose_exam_file)
        exam_row.addWidget(self.load_exam_button)
        form.addRow(label("Exam"), exam_row)
        self.secure_mode_select = QComboBox()
        self.secure_mode_select.addItem("Maximized (recommended)", "MAXIMIZED")
        self.secure_mode_select.addItem("Fullscreen", "FULLSCREEN")
        self.secure_mode_select.addItem("Windowed", "WINDOWED")
        form.addRow(label("Exam window"), self.secure_mode_select)
        form.addRow(label("Camera"), self.camera_select)
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
        self.exam_error = label("", role="muted")
        self.exam_error.setWordWrap(True)
        self.exam_error.setStyleSheet(text_style(COLORS.warning))
        self.exam_error.hide()
        layout.addWidget(self.exam_error)
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
        self.teacher_review_button = QPushButton("TEACHER REVIEW")
        self.teacher_review_button.clicked.connect(self.teacher_review_requested)
        layout.addWidget(self.teacher_review_button)
        assistant_options = {"context": "setup", "preferences": assistant_preferences,
                             "config": assistant_config}
        if assistant_clock is not None:
            assistant_options["clock"] = assistant_clock
        self.assistant = AssistantDock(**assistant_options)
        self.language_select.setCurrentIndex(self.language_select.findData(self.assistant.preferences.preferences.language))
        language_manager.set_language(self.assistant.preferences.preferences.language)
        self.language_select.currentIndexChanged.connect(self._language_selected)
        self.assistant.setFixedWidth(300)
        # A separate lane keeps the greeting away from setup inputs/statuses.
        content = QWidget()
        content.setMaximumWidth(1024)
        row = QHBoxLayout(content)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(24)
        row.addWidget(panel, 1, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.assistant, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(content, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch()
        try:
            demo = load_demo_exam()
            self.exam_select.addRawItem(demo.title, demo)
        except ExamValidationError as exc:
            self.exam_error.setText(f"Demo exam unavailable: {exc}. Load a valid local exam JSON.")
            self.exam_error.show()

    @property
    def selected_exam(self):
        return self._selected_exam

    def _language_selected(self, index):
        code = self.language_select.itemData(index)
        if code in LANGUAGE_NAMES:
            self.assistant.preferences.update(language=code)
            language_manager.set_language(code)

    @property
    def selected_secure_mode(self):
        return self.secure_mode_select.currentData() or "WINDOWED"

    def set_secure_mode(self, mode):
        if self._secure_window_required and mode == "WINDOWED":
            mode = "MAXIMIZED"
        index = self.secure_mode_select.findData(mode)
        if index < 0:
            raise ValueError("Secure mode must be WINDOWED, MAXIMIZED or FULLSCREEN")
        self.secure_mode_select.setCurrentIndex(index)

    def set_secure_window_required(self, required):
        self._secure_window_required = bool(required)
        index = self.secure_mode_select.findData("WINDOWED")
        if index >= 0:
            item = self.secure_mode_select.model().item(index)
            if item is not None:
                item.setEnabled(not self._secure_window_required)
        if self._secure_window_required and self.selected_secure_mode == "WINDOWED":
            self.set_secure_mode("MAXIMIZED")

    def _exam_selected(self, index):
        self._selected_exam = self.exam_select.itemData(index) if index >= 0 else None

    def set_exam_definition(self, exam):
        if not isinstance(exam, ExamDefinition):
            raise TypeError("Exam selection requires a validated ExamDefinition")
        for index in range(self.exam_select.count()):
            previous = self.exam_select.itemData(index)
            if previous == exam:
                self.exam_select.setCurrentIndex(index)
                self._selected_exam = exam
                return
        self.exam_select.addRawItem(exam.title, exam)
        self.exam_select.setCurrentIndex(self.exam_select.count() - 1)

    def _choose_exam_file(self):
        if self._busy:
            return
        path, _filter = QFileDialog.getOpenFileName(self, tr("Load local exam"), "", tr("Exam JSON (*.json)"))
        if path:
            self.load_exam_file(path)

    def load_exam_file(self, path):
        if self._busy:
            return False
        try:
            exam = load_exam(path)
        except ExamValidationError as exc:
            self.exam_error.setText(f"Invalid exam JSON: {exc}. The previous exam remains selected.")
            self.exam_error.show()
            return False
        for index in range(self.exam_select.count()):
            previous = self.exam_select.itemData(index)
            if previous is not None and previous.reference == exam.reference:
                self.exam_select.setItemData(index, exam)
                self.exam_select.setRawItemText(index, exam.title)
                self.exam_select.setCurrentIndex(index)
                self._selected_exam = exam
                break
        else:
            self.exam_select.addRawItem(exam.title, exam)
            self.exam_select.setCurrentIndex(self.exam_select.count() - 1)
        self.exam_error.clear()
        self.exam_error.hide()
        self.exam_input.setText(exam.title[:100])
        return True

    def _start(self):
        index = self.camera_select.currentData()
        if self._busy or not self._camera_ready or not self._ai_ready or index is None:
            self.message.setText("A usable camera and AI detection are required. Recheck the system.")
            return
        if self.selected_exam is None:
            self.exam_error.setText("Select or load a valid local exam before starting.")
            self.exam_error.show()
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
        for widget in (self.student_input, self.exam_input, self.camera_select, self.refresh_button,
                       self.exam_select, self.load_exam_button, self.secure_mode_select):
            widget.setEnabled(not self._busy)
        self.teacher_review_button.setEnabled(not self._busy)

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
        self.assistant.reset()
