"""Local question controls. Navigation never touches the monitoring worker."""

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFrame, QGridLayout,
                               QHBoxLayout, QPlainTextEdit, QPushButton,
                               QRadioButton, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from monitoring import EventType
from security import SecurityConfig
from .theme import label

MAX_NAVIGATOR_BUTTONS = 25


class AnswerTextEdit(QPlainTextEdit):
    """Honor configured paste suppression for every Qt paste entry point.

    Local attempts and native keyboard attempts enter the same security cooldown,
    so paste stays detectable even if the global hook is unavailable.
    """

    editing_changed = Signal(bool)
    local_paste_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.suppress_paste = False
        self._keyboard_paste = False
        self.setAcceptDrops(False)
        self.setPlaceholderText("Type your answer here…")
        self.setMinimumHeight(150)
        self.setStyleSheet("QPlainTextEdit { background: #101925; border: 1px solid #35465d; border-radius: 7px; padding: 10px; }")

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.editing_changed.emit(True)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.editing_changed.emit(False)

    def keyPressEvent(self, event):
        paste = event.matches(QKeySequence.StandardKey.Paste)
        # Every paste path is forwarded to the same normalized security queue.
        shift_insert = event.key() == Qt.Key.Key_Insert and bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if paste or shift_insert:
            self.local_paste_requested.emit("shift_insert" if shift_insert else "ctrl_v")
            self._keyboard_paste = True
            try:
                if self.suppress_paste:
                    event.accept()
                    return
                super().keyPressEvent(event)
            finally:
                self._keyboard_paste = False
            return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source):
        if self.isReadOnly():
            return
        if not self._keyboard_paste:
            self.local_paste_requested.emit("context_or_programmatic")
        if not self.suppress_paste:
            super().insertFromMimeData(source)


class QuestionPanel(QFrame):
    submit_requested = Signal()
    submission_confirmed = Signal()
    editing_changed = Signal(bool)
    local_paste_requested = Signal(str)
    answer_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setMinimumWidth(400)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.attempt = None
        self._active = False
        self._rendering = False
        self._security = SecurityConfig()
        self._option_widgets = []
        self._option_labels = {}
        self._navigator = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        self.exam_title = label("BUILT-IN EXAM", role="heading")
        self.exam_title.setWordWrap(True)
        layout.addWidget(self.exam_title)
        self.question_number = label("Question 0 / 0", size=20)
        layout.addWidget(self.question_number)
        self.progress = label("0 answered", role="muted")
        layout.addWidget(self.progress)
        self.navigation = QGridLayout()
        self.navigation.setSpacing(5)
        layout.addLayout(self.navigation)
        self.navigator_select = QComboBox()
        self.navigator_select.currentIndexChanged.connect(self._go_to)
        self.navigator_select.hide()
        layout.addWidget(self.navigator_select)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        content = QWidget()
        self.answers_layout = QVBoxLayout(content)
        self.answers_layout.setContentsMargins(0, 8, 0, 8)
        self.answers_layout.setSpacing(12)
        self.question_text = label("", size=18)
        self.question_text.setWordWrap(True)
        self.question_text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.answers_layout.addWidget(self.question_text)
        self.question_hint = label("", role="muted")
        self.question_hint.setWordWrap(True)
        self.answers_layout.addWidget(self.question_hint)
        self.option_area = QWidget()
        self.option_layout = QVBoxLayout(self.option_area)
        self.option_layout.setContentsMargins(0, 0, 0, 0)
        self.option_layout.setSpacing(10)
        self.answers_layout.addWidget(self.option_area)
        self.text_answer = AnswerTextEdit()
        self.text_answer.textChanged.connect(self._text_changed)
        self.text_answer.editing_changed.connect(self.editing_changed)
        self.text_answer.local_paste_requested.connect(self.local_paste_requested)
        self.answers_layout.addWidget(self.text_answer)
        self.answers_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        controls = QHBoxLayout()
        self.previous_button = QPushButton("PREVIOUS")
        self.previous_button.clicked.connect(self._previous)
        self.next_button = QPushButton("NEXT")
        self.next_button.clicked.connect(self._next)
        controls.addWidget(self.previous_button)
        controls.addWidget(self.next_button)
        layout.addLayout(controls)
        self.submit_button = QPushButton("SUBMIT EXAM")
        self.submit_button.setObjectName("primary")
        self.submit_button.clicked.connect(self.submit_requested)
        layout.addWidget(self.submit_button)
        self.confirmation_panel = QFrame()
        self.confirmation_panel.setStyleSheet("background: #28374b; border-radius: 7px;")
        confirm = QVBoxLayout(self.confirmation_panel)
        self.confirmation_message = label("Submit exam? You will not be able to change your answers.")
        self.confirmation_message.setWordWrap(True)
        confirm.addWidget(self.confirmation_message)
        confirm_row = QHBoxLayout()
        self.confirm_button = QPushButton("CONFIRM SUBMISSION")
        self.confirm_button.setObjectName("finish")
        self.confirm_button.clicked.connect(self._confirm)
        self.cancel_button = QPushButton("CANCEL")
        self.cancel_button.clicked.connect(self._cancel)
        confirm_row.addWidget(self.confirm_button)
        confirm_row.addWidget(self.cancel_button)
        confirm.addLayout(confirm_row)
        layout.addWidget(self.confirmation_panel)
        self.reset()

    @staticmethod
    def _kind(question):
        return getattr(question.type, "value", question.type)

    def set_attempt(self, attempt, security_config=None):
        self.reset()
        self.attempt = attempt
        self._security = security_config or SecurityConfig()
        self.text_answer.suppress_paste = EventType.PASTE_ATTEMPT in self._security.blocked_events
        self.exam_title.setText(attempt.exam.title)
        if len(attempt.exam.questions) > MAX_NAVIGATOR_BUTTONS:
            self.navigator_select.blockSignals(True)
            for index in range(len(attempt.exam.questions)):
                self.navigator_select.addItem(f"Question {index + 1}")
            self.navigator_select.blockSignals(False)
            self.navigator_select.show()
        else:
            for index, _question in enumerate(attempt.exam.questions):
                button = QPushButton(str(index + 1))
                button.setFixedHeight(34)
                button.setStyleSheet("padding: 5px;")
                button.clicked.connect(lambda checked=False, i=index: self._go_to(i))
                self.navigation.addWidget(button, index // 5, index % 5)
                self._navigator.append(button)
        self.set_active(not attempt.submitted)
        self._render()

    def set_active(self, active):
        self._active = bool(active) and self.attempt is not None and not self.attempt.submitted
        self.text_answer.setReadOnly(not self._active)
        self.option_area.setEnabled(self._active)
        self.submit_button.setEnabled(self._active)
        self.confirm_button.setEnabled(self._active)
        self.cancel_button.setEnabled(self._active)
        self.navigator_select.setEnabled(self._active)
        if not self._active:
            self.confirmation_panel.hide()
            self.editing_changed.emit(False)
        self._refresh_navigation()

    def _refresh_navigation(self):
        if self.attempt is None:
            self.previous_button.setEnabled(False)
            self.next_button.setEnabled(False)
            return
        total = len(self.attempt.exam.questions)
        current = self.attempt.current_index
        self.question_number.setText(f"Question {current + 1} / {total}")
        self.progress.setText(f"{self.attempt.answered_count} / {total} answered")
        self.previous_button.setEnabled(self._active and current > 0)
        self.next_button.setEnabled(self._active and current + 1 < total)
        self.navigator_select.blockSignals(True)
        self.navigator_select.setCurrentIndex(current)
        self.navigator_select.blockSignals(False)
        for index, button in enumerate(self._navigator):
            answered = self.attempt.is_answered(self.attempt.exam.questions[index].id)
            button.setEnabled(self._active)
            color = "#388ce6" if index == current else "#164e46" if answered else "#28374b"
            button.setStyleSheet(f"background: {color}; padding: 5px;")
            button.setToolTip("Answered" if answered else "Unanswered")

    def _clear_options(self):
        while self.option_layout.count():
            item = self.option_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        self._option_widgets.clear()
        self._option_labels.clear()
        if getattr(self, "_button_group", None) is not None:
            self._button_group.deleteLater()
        self._button_group = None

    def _render(self):
        if self.attempt is None:
            return
        self._rendering = True
        try:
            question = self.attempt.current_question
            kind = self._kind(question)
            self.question_text.setText(question.text)
            self._clear_options()
            self.text_answer.hide()
            self.option_area.hide()
            answer = self.attempt.answer_for(question.id)
            if kind == "TEXT":
                self.question_hint.setText("Text answer · Reviewed separately from the automatic exam score")
                self.text_answer.setPlainText(answer or "")
                self.text_answer.show()
            else:
                self.question_hint.setText("Select one answer" if kind == "SINGLE_CHOICE" else "Select all answers that apply")
                self._button_group = QButtonGroup(self)
                self._button_group.setExclusive(kind == "SINGLE_CHOICE")
                for option in question.options:
                    widget = QRadioButton() if kind == "SINGLE_CHOICE" else QCheckBox()
                    widget.setProperty("option_value", option)
                    widget.setAccessibleName(option)
                    widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
                    widget.setToolTip(option)
                    widget.setStyleSheet("QRadioButton, QCheckBox { padding: 8px 2px; }"
                                        "QRadioButton::indicator, QCheckBox::indicator { width: 16px; height: 16px; background: #101925; border: 1px solid #91a3bb; }"
                                        "QRadioButton::indicator { border-radius: 8px; } QCheckBox::indicator { border-radius: 3px; }"
                                        "QRadioButton::indicator:checked, QCheckBox::indicator:checked { background: #388ce6; border: 2px solid #a7cff5; }")
                    self._button_group.addButton(widget)
                    widget.setChecked(answer == option if kind == "SINGLE_CHOICE" else option in (answer or ()))
                    widget.toggled.connect(self._choice_changed)
                    row = QWidget()
                    row_layout = QHBoxLayout(row)
                    row_layout.setContentsMargins(0, 0, 0, 0)
                    row_layout.setSpacing(8)
                    row_layout.addWidget(widget)
                    text = label(option)
                    text.setStyleSheet("font-size: 18px;")
                    text.setWordWrap(True)
                    text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                    text.installEventFilter(self)
                    self._option_labels[text] = widget
                    row_layout.addWidget(text, 1)
                    self.option_layout.addWidget(row)
                    self._option_widgets.append(widget)
                self.option_area.show()
            self._refresh_navigation()
        finally:
            self._rendering = False

    def _choice_changed(self):
        if self._rendering or not self._active or self.attempt is None:
            return
        question = self.attempt.current_question
        checked = [widget.property("option_value") for widget in self._option_widgets if widget.isChecked()]
        answer = (checked[0] if checked else None) if self._kind(question) == "SINGLE_CHOICE" else checked
        self.attempt.set_answer(question.id, answer)
        self._refresh_navigation()
        self.answer_changed.emit()

    def eventFilter(self, watched, event):
        button = self._option_labels.get(watched)
        if (button is not None and event.type() == QEvent.Type.MouseButtonRelease
                and event.button() == Qt.MouseButton.LeftButton and button.isEnabled()):
            button.click()
            return True
        return super().eventFilter(watched, event)

    def _text_changed(self):
        if not self._rendering and self._active and self.attempt is not None and self._kind(self.attempt.current_question) == "TEXT":
            self.attempt.set_answer(self.attempt.current_question.id, self.text_answer.toPlainText())
            self._refresh_navigation()
            self.answer_changed.emit()

    def _go_to(self, index):
        if self._active and not self._rendering and self.attempt is not None and index >= 0:
            self.attempt.go_to(index)
            self._render()

    def _previous(self):
        if self._active and self.attempt is not None:
            self.attempt.previous()
            self._render()

    def _next(self):
        if self._active and self.attempt is not None:
            self.attempt.next()
            self._render()

    def show_submission_confirmation(self):
        if self._active:
            self.confirmation_panel.show()
            self.submit_button.setEnabled(False)
            self.editing_changed.emit(False)
            self.confirm_button.setFocus()

    def _confirm(self):
        if self._active and not self.confirmation_panel.isHidden():
            self.set_active(False)
            self.submission_confirmed.emit()

    def _cancel(self):
        self.confirmation_panel.hide()
        self.submit_button.setEnabled(self._active)

    def reset(self):
        self.attempt = None
        self._active = False
        self._rendering = True
        self._clear_options()
        while self.navigation.count():
            item = self.navigation.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        self._navigator.clear()
        self.navigator_select.blockSignals(True)
        self.navigator_select.clear()
        self.navigator_select.blockSignals(False)
        self.navigator_select.hide()
        self.text_answer.clear()
        self.text_answer.hide()
        self.text_answer.setReadOnly(True)
        self.text_answer.suppress_paste = False
        self.question_text.clear()
        self.question_hint.clear()
        self.question_number.setText("Question 0 / 0")
        self.progress.setText("0 answered")
        self.exam_title.setText("BUILT-IN EXAM")
        self.confirmation_panel.hide()
        self.submit_button.setEnabled(False)
        self._refresh_navigation()
        self._rendering = False
        self.editing_changed.emit(False)
