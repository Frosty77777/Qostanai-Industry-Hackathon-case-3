"""PIN entry and completed-session listing. Authorization belongs to the controller."""

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView,
                               QLineEdit, QPushButton, QStackedWidget,
                               QTableView, QVBoxLayout, QWidget)

from .report_page import display_time
from .session import format_duration
from .theme import RISK_COLORS, card, label


class CompletedSessionsModel(QAbstractTableModel):
    HEADERS = ("Student", "Exam", "Date / time (UTC+5)", "Duration", "Exam score", "Risk", "Level")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.summaries = ()

    def set_sessions(self, summaries):
        self.beginResetModel()
        self.summaries = tuple(summaries)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.summaries)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return super().headerData(section, orientation, role)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.summaries):
            return None
        summary = self.summaries[index.row()]
        score = ("Manual review" if summary.exam_max_score == 0
                 else f"{summary.exam_score} / {summary.exam_max_score}"
                 if summary.exam_score is not None and summary.exam_max_score is not None else "—")
        values = (summary.student, summary.exam, display_time(summary.started_at),
                  format_duration(summary.duration_seconds), score,
                  f"{summary.risk_score} / 100", summary.risk_level)
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return values[index.column()]
        if role == Qt.ItemDataRole.UserRole:
            return summary.session_id
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 6:
            return QColor(RISK_COLORS.get(summary.risk_level, "#91a3bb"))
        return None


class TeacherReviewPage(QWidget):
    pin_requested = Signal(str)
    session_open_requested = Signal(object)
    back_requested = Signal()
    refresh_requested = Signal()
    logout_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        self.stack = QStackedWidget()
        root.addWidget(self.stack)
        self.locked_page = QWidget()
        locked = QVBoxLayout(self.locked_page)
        locked.addStretch()
        panel, content = card("INSTRUCTOR AUTHORIZATION")
        panel.setMaximumWidth(620)
        panel.setMinimumWidth(480)
        content.setSpacing(18)
        content.addWidget(label("Teacher Review", role="title"))
        explanation = label("Enter the configured Teacher PIN to review completed sessions and proctoring evidence.")
        explanation.setWordWrap(True)
        content.addWidget(explanation)
        self.pin_input = QLineEdit()
        self.pin_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.pin_input.setPlaceholderText("Teacher PIN")
        self.pin_input.returnPressed.connect(self._submit_pin)
        content.addWidget(self.pin_input)
        self.pin_error = label("", role="muted")
        self.pin_error.setWordWrap(True)
        self.pin_error.setStyleSheet("color: #ff955f;")
        self.pin_error.hide()
        content.addWidget(self.pin_error)
        pin_buttons = QHBoxLayout()
        self.pin_back_button = QPushButton("BACK")
        self.pin_back_button.clicked.connect(self.back_requested)
        pin_buttons.addWidget(self.pin_back_button)
        self.unlock_button = QPushButton("UNLOCK TEACHER REVIEW")
        self.unlock_button.setObjectName("primary")
        self.unlock_button.clicked.connect(self._submit_pin)
        pin_buttons.addWidget(self.unlock_button, 1)
        content.addLayout(pin_buttons)
        locked.addWidget(panel, 0, Qt.AlignmentFlag.AlignHCenter)
        locked.addStretch()
        self.stack.addWidget(self.locked_page)
        self.sessions_page = QWidget()
        sessions = QVBoxLayout(self.sessions_page)
        sessions.setContentsMargins(0, 0, 0, 0)
        sessions.setSpacing(14)
        header = QHBoxLayout()
        header.addWidget(label("COMPLETED SESSIONS", role="title"), 1)
        self.refresh_button = QPushButton("REFRESH")
        self.refresh_button.clicked.connect(self.refresh_requested)
        header.addWidget(self.refresh_button)
        self.logout_button = QPushButton("LOCK TEACHER REVIEW")
        self.logout_button.clicked.connect(self.logout_requested)
        header.addWidget(self.logout_button)
        sessions.addLayout(header)
        self.message = label("0 completed sessions", role="muted")
        self.message.setWordWrap(True)
        sessions.addWidget(self.message)
        self.sessions_model = CompletedSessionsModel(self)
        self.table = QTableView()
        self.table.setModel(self.sessions_model)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(54)
        for index, width in enumerate((150, 250, 190, 110, 120, 110, 120)):
            self.table.setColumnWidth(index, width)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.doubleClicked.connect(lambda index: self._open_session(index.row()))
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        sessions.addWidget(self.table, 1)
        self.list_error = label("", role="muted")
        self.list_error.setWordWrap(True)
        self.list_error.setStyleSheet("color: #ff955f;")
        self.list_error.hide()
        sessions.addWidget(self.list_error)
        actions = QHBoxLayout()
        self.back_button = QPushButton("BACK")
        self.back_button.clicked.connect(self.back_requested)
        actions.addWidget(self.back_button)
        actions.addStretch()
        self.open_button = QPushButton("OPEN INSTRUCTOR REPORT")
        self.open_button.setObjectName("primary")
        self.open_button.clicked.connect(lambda: self._open_session(self.table.currentIndex().row()))
        actions.addWidget(self.open_button)
        sessions.addLayout(actions)
        self.stack.addWidget(self.sessions_page)
        self.show_locked()

    def _submit_pin(self):
        pin = self.pin_input.text()
        self.pin_input.clear()
        self.pin_requested.emit(pin)

    def _selection_changed(self, *_args):
        self.open_button.setEnabled(self.stack.currentWidget() is self.sessions_page
                                    and self.table.currentIndex().isValid())

    def _open_session(self, row):
        if self.stack.currentWidget() is self.sessions_page and 0 <= row < len(self.sessions_model.summaries):
            self.session_open_requested.emit(self.sessions_model.summaries[row].session_id)

    def show_locked(self):
        self.sessions_model.set_sessions(())
        self.table.clearSelection()
        self.open_button.setEnabled(False)
        self.message.setText("0 completed sessions")
        self.list_error.clear()
        self.list_error.hide()
        self.pin_input.clear()
        self.pin_error.clear()
        self.pin_error.hide()
        self.stack.setCurrentWidget(self.locked_page)
        self.pin_input.setFocus()

    def show_sessions(self, summaries, errors=()):
        self.pin_input.clear()
        self.pin_error.clear()
        self.pin_error.hide()
        self.sessions_model.set_sessions(summaries)
        self.message.setText(f"{len(self.sessions_model.summaries)} completed session(s) · Select a session to review its full report")
        errors = tuple(errors)
        displayed = [str(error)[:250] for error in errors[:3]]
        if len(errors) > 3:
            displayed.append(f"{len(errors) - 3} additional session(s) could not be listed.")
        self.list_error.setText("\n".join(displayed))
        self.list_error.setVisible(bool(errors))
        self.stack.setCurrentWidget(self.sessions_page)
        self.open_button.setEnabled(False)

    def set_error(self, message):
        target = self.pin_error if self.stack.currentWidget() is self.locked_page else self.list_error
        target.setText(str(message))
        target.setVisible(bool(message))
