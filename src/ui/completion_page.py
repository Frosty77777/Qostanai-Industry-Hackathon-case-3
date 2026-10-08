"""Student-facing completion screen. No proctoring details are rendered here."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QVBoxLayout, QWidget

from i18n import language_manager, tr
from .localized_widgets import QPushButton

from .session import DISPLAY_TIMEZONE, format_duration
from .theme import card, label


class CompletionPage(QWidget):
    new_session_requested = Signal()
    teacher_review_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._result = None
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 28, 32, 28)
        root.addStretch()
        panel, layout = card()
        panel.setMaximumWidth(720)
        panel.setMinimumWidth(560)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(20)
        self.heading = label("EXAM SUBMITTED", role="title")
        layout.addWidget(self.heading)
        self.message = label("Your answers have been saved.\nThe proctoring report is available for instructor review.")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.values = {}
        form = QFormLayout()
        form.setSpacing(12)
        for name in ("Student", "Exam", "Submitted", "Duration"):
            value = label("—")
            value.setWordWrap(True)
            self.values[name] = value
            form.addRow(label(name), value)
        layout.addLayout(form)
        self.notice = label("", role="muted")
        self.notice.setWordWrap(True)
        self.notice.hide()
        layout.addWidget(self.notice)
        buttons = QHBoxLayout()
        self.new_button = QPushButton("NEW SESSION")
        self.new_button.setObjectName("primary")
        self.new_button.clicked.connect(self.new_session_requested)
        buttons.addWidget(self.new_button, 1)
        self.teacher_button = QPushButton("TEACHER REVIEW")
        self.teacher_button.clicked.connect(self.teacher_review_requested)
        buttons.addWidget(self.teacher_button, 1)
        layout.addLayout(buttons)
        root.addWidget(panel, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addStretch()
        unsubscribe = language_manager.subscribe(self._language_changed)
        self.destroyed.connect(lambda *_args: unsubscribe())

    def set_result(self, result):
        self._result = result
        self._refresh_result()

    def _language_changed(self, _language):
        if self._result is not None:
            self._refresh_result()

    def _refresh_result(self):
        result = self._result
        academic = getattr(result, "exam_result", None)
        interrupted = academic is not None and academic.submission_reason == "interrupted"
        self.heading.setText("SESSION ENDED" if interrupted else "EXAM SUBMITTED")
        saved = bool(result.session_directory) and not result.persistence_error
        self.message.setRawText("\n".join((
            tr("Your answers have been saved." if saved else "The exam session has ended."),
            tr("The proctoring report is available for instructor review."),
        )))
        self.values["Student"].setRawText(result.student)
        self.values["Exam"].setRawText(result.exam)
        submitted = academic.submitted_at if academic is not None else result.ended_at
        if submitted is not None and submitted.tzinfo is not None:
            submitted = submitted.astimezone(DISPLAY_TIMEZONE)
        self.values["Submitted"].setRawText(submitted.strftime("%Y-%m-%d %H:%M:%S") if submitted else "—")
        self.values["Duration"].setRawText(format_duration(result.duration_seconds))
        notes = []
        if result.persistence_error:
            notes.append("The local save could not complete. Please contact your instructor before starting another session.")
        elif not result.session_directory:
            notes.append("Please check with your instructor that your submission has been retained.")
        if interrupted:
            notes.append("This session ended before normal submission. Please contact your instructor about the current answers.")
        self.notice.setRawText("\n".join(tr(note) for note in notes))
        self.notice.setVisible(bool(notes))

    def reset(self):
        self._result = None
        self.heading.setText("EXAM SUBMITTED")
        self.message.setText("Your answers have been saved.\nThe proctoring report is available for instructor review.")
        for value in self.values.values():
            value.setText("—")
        self.notice.clear()
        self.notice.hide()
