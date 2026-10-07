"""In-memory completion summary; full reports are intentionally deferred."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFormLayout, QPushButton, QVBoxLayout, QWidget

from .session import format_duration
from .theme import RISK_COLORS, card, label


class ReportPage(QWidget):
    new_session_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.addStretch()
        panel, layout = card()
        panel.setMinimumWidth(600)
        panel.setMaximumWidth(650)
        layout.setContentsMargins(36, 32, 36, 32)
        layout.setSpacing(20)
        layout.addWidget(label("SESSION COMPLETE", role="title"))
        layout.addWidget(label("Monitoring stopped. Session details remain available in this window.", role="muted"))
        form = QFormLayout()
        form.setSpacing(18)
        self.values = {}
        for name in ("Student", "Exam", "Duration", "Final Risk", "Total Events"):
            value = label("—")
            value.setWordWrap(True)
            form.addRow(name, value)
            self.values[name] = value
        layout.addLayout(form)
        self.message = label("The full report and evidence viewer will be available in a later update.", role="muted")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.view_button = QPushButton("VIEW REPORT")
        self.view_button.setEnabled(False)
        self.view_button.setToolTip("Full report UI is not implemented yet")
        layout.addWidget(self.view_button)
        self.new_button = QPushButton("NEW SESSION")
        self.new_button.setObjectName("primary")
        self.new_button.clicked.connect(self.new_session_requested)
        layout.addWidget(self.new_button)
        root.addWidget(panel, 0, Qt.AlignmentFlag.AlignHCenter)
        root.addStretch()

    def set_result(self, result):
        self.values["Student"].setText(result.student)
        self.values["Exam"].setText(result.exam)
        self.values["Duration"].setText(format_duration(result.duration_seconds))
        self.values["Final Risk"].setText(f"{result.risk_score} / 100 · {result.risk_level}")
        self.values["Final Risk"].setStyleSheet(f"color: {RISK_COLORS[result.risk_level]}; font-weight: 600;")
        self.values["Total Events"].setText(str(len(result.events)))
        if result.error:
            self.message.setText(f"Session ended with a component error: {result.error}\nEvents and saved evidence were retained.")
        else:
            self.message.setText("The full report and evidence viewer will be available in a later update.")

    def reset(self):
        for value in self.values.values():
            value.setText("—")
        self.message.setText("The full report and evidence viewer will be available in a later update.")
