"""Completed-session summary and full event timeline for human review."""
from collections import Counter

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QFormLayout, QGridLayout, QHBoxLayout, QHeaderView,
    QProgressBar, QPushButton, QScrollArea, QStackedWidget, QTableView, QTabWidget, QVBoxLayout, QWidget,
)
from monitoring import EventType
from .evidence_viewer import EvidenceGallery
from .completion_page import CompletionPage
from .session import DISPLAY_TIMEZONE, format_duration
from .theme import RISK_COLORS, SEVERITY_COLORS, card, label

EVENT_LABELS = {
    EventType.PHONE_DETECTED: "Phone detected",
    EventType.MULTIPLE_PERSONS: "Multiple persons",
    EventType.MULTIPLE_FACES: "Multiple faces",
    EventType.FACE_MISSING: "Face missing",
    EventType.CAMERA_OBSTRUCTED: "Camera obstructed",
    EventType.LOOK_LEFT: "Head left",
    EventType.LOOK_RIGHT: "Head right",
    EventType.LOOK_UP: "Head up",
    EventType.LOOK_DOWN: "Head down",
    EventType.ALT_TAB_ATTEMPT: "Alt+Tab attempts",
    EventType.WINDOW_FOCUS_LOST: "Window focus lost",
    EventType.COPY_ATTEMPT: "Copy attempts",
    EventType.PASTE_ATTEMPT: "Paste attempts",
    EventType.PRINTSCREEN_ATTEMPT: "PrintScreen attempts",
    EventType.ESCAPE_ATTEMPT: "Escape attempts",
    EventType.AUTHORIZED_BREAK_STARTED: "Authorized break started",
    EventType.AUTHORIZED_BREAK_ENDED: "Authorized break ended",
    EventType.AUTHORIZED_BREAK_EXPIRED: "Authorized break expired",
}
SUMMARY_TYPES = tuple(kind for kind in EVENT_LABELS if not kind.value.startswith("AUTHORIZED_BREAK"))


def display_time(timestamp):
    if timestamp is None:
        return "—"
    # Older imported naive values stay readable without assuming host timezone.
    if timestamp.tzinfo is not None:
        timestamp = timestamp.astimezone(DISPLAY_TIMEZONE)
    return timestamp.strftime("%Y-%m-%d %H:%M:%S")


class EventTimelineModel(QAbstractTableModel):
    """Qt requests visible rows; long sessions need no per-event widgets."""
    HEADERS = ("Time (UTC+5)", "Event", "Severity", "Risk delta", "Message")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.events = ()

    def set_events(self, events):
        self.beginResetModel()
        self.events = tuple(events)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.events)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return super().headerData(section, orientation, role)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.events):
            return None
        event = self.events[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            values = (display_time(event.timestamp), EVENT_LABELS.get(event.type, str(event.type)),
                      event.severity.value.upper(), f"{event.risk_delta or 0:+d}", event.message)
            return values[index.column()]
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 2:
            return QColor(SEVERITY_COLORS.get(event.severity.value, "#91a3bb"))
        if role == Qt.ItemDataRole.ToolTipRole:
            return (f"{event.type.value} · {display_time(event.timestamp)} (UTC+5)\n"
                    f"{event.severity.value.upper()} · {event.risk_delta or 0:+d} risk\n"
                    f"{event.message}\n{event.evidence_path or 'No webcam evidence'}")
        if role == Qt.ItemDataRole.TextAlignmentRole and index.column() in (2, 3):
            return Qt.AlignmentFlag.AlignCenter
        return None


class ExamAnswersModel(QAbstractTableModel):
    """Read-only answer review, independent of proctoring events and risk."""
    HEADERS = ("Question ID", "Student answer", "Status", "Grading")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = ()

    def set_result(self, result):
        self.beginResetModel()
        self.rows = () if result is None else tuple(
            (question_id, result.answers.get(question_id),
             result.answer_status.get(question_id, "unanswered"), result.grading.get(question_id, "unanswered"))
            for question_id in result.question_ids
        )
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return super().headerData(section, orientation, role)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            value = self.rows[index.row()][index.column()]
            if index.column() in (2, 3):
                return str(value).replace("_", " ").title()
            if value is None or value == "" or value == () or value == []:
                return "—"
            if isinstance(value, (list, tuple)):
                return ", ".join(str(item) for item in value)
            return str(value)
        return None


class ReportPage(QWidget):
    new_session_requested = Signal()
    teacher_review_requested = Signal()
    back_to_sessions_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.presentation = QStackedWidget()
        outer.addWidget(self.presentation)
        self.detail_widget = QWidget()
        self.completion_page = CompletionPage()
        self.presentation.addWidget(self.detail_widget)
        self.presentation.addWidget(self.completion_page)
        self.completion_page.new_session_requested.connect(self.new_session_requested)
        self.completion_page.teacher_review_requested.connect(self.teacher_review_requested)
        root = QVBoxLayout(self.detail_widget)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)
        header = QHBoxLayout()
        header.addWidget(label("SESSION REPORT", role="title"), 1)
        self.view_button = QPushButton("VIEW EVIDENCE")
        self.view_button.clicked.connect(lambda: self.tabs.setCurrentIndex(2))
        header.addWidget(self.view_button)
        self.new_button = QPushButton("NEW SESSION")
        self.new_button.setObjectName("primary")
        self.new_button.clicked.connect(self.new_session_requested)
        header.addWidget(self.new_button)
        self.back_to_sessions_button = QPushButton("BACK TO SESSIONS")
        self.back_to_sessions_button.clicked.connect(self._return_to_sessions)
        self.back_to_sessions_button.hide()
        header.addWidget(self.back_to_sessions_button)
        root.addLayout(header)
        self.message = label("Review suspicious events and supporting images before drawing conclusions.", role="muted")
        self.message.setWordWrap(True)
        root.addWidget(self.message)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        summary = QWidget()
        summary_layout = QVBoxLayout(summary)
        summary_layout.setContentsMargins(16, 16, 16, 16)
        summary_layout.setSpacing(16)
        exam_panel, exam_layout = card("EXAM RESULT")
        exam_overview = QHBoxLayout()
        academic = QVBoxLayout()
        academic.addWidget(label("EXAM SCORE", role="heading"))
        self.exam_score = label("—", size=32)
        academic.addWidget(self.exam_score)
        self.exam_percentage = label("No built-in exam result", role="muted")
        self.exam_percentage.setWordWrap(True)
        academic.addWidget(self.exam_percentage)
        academic.addStretch()
        exam_overview.addLayout(academic, 2)
        self.exam_values = {"Score": self.exam_score, "Percentage": self.exam_percentage}
        for names in (("Answered", "Unanswered", "Correct", "Incorrect"),
                      ("Text pending review", "Submitted", "Duration", "Status")):
            details = QFormLayout()
            details.setSpacing(8)
            for name in names:
                value = label("—")
                value.setWordWrap(True)
                details.addRow(name, value)
                self.exam_values[name] = value
            exam_overview.addLayout(details, 3)
        exam_layout.addLayout(exam_overview)
        self.exam_note = label("Academic performance and session risk are separate results.", role="muted")
        self.exam_note.setWordWrap(True)
        exam_layout.addWidget(self.exam_note)
        summary_layout.addWidget(exam_panel)
        overview = QHBoxLayout()
        metadata, metadata_layout = card("SESSION DETAILS · UTC+5")
        form = QFormLayout()
        form.setSpacing(12)
        self.values = {}
        for name in ("Student", "Exam", "Start time", "End time", "Duration"):
            value = label("—")
            value.setWordWrap(True)
            form.addRow(name, value)
            self.values[name] = value
        metadata_layout.addLayout(form)
        overview.addWidget(metadata, 3)
        risk_panel, risk_layout = card("PROCTORING RESULT")
        risk_layout.addWidget(label("SESSION RISK", role="heading"))
        self.risk_score = label("0 / 100", size=44)
        self.risk_level = label("LOW", size=22)
        self.risk_bar = QProgressBar()
        self.risk_bar.setRange(0, 100)
        self.risk_bar.setTextVisible(False)
        risk_layout.addWidget(self.risk_score)
        risk_layout.addWidget(self.risk_level)
        risk_layout.addWidget(self.risk_bar)
        self.values["Final Risk"] = label("—")
        risk_layout.addWidget(self.values["Final Risk"])
        risk_layout.addStretch()
        overview.addWidget(risk_panel, 2)
        summary_layout.addLayout(overview)

        totals_panel, totals_layout = card()
        totals = QHBoxLayout()
        self.severity_values = {}
        for text, key in (("TOTAL EVENTS", "total"), ("CRITICAL", "critical"),
                          ("HIGH", "high"), ("MEDIUM", "medium"), ("INFO / BREAK", "info")):
            column = QVBoxLayout()
            column.addWidget(label(text, role="heading"))
            value = label("0", size=28)
            if key != "total":
                value.setStyleSheet(f"font-size: 28px; font-weight: 600; color: {SEVERITY_COLORS[key]};")
            self.severity_values[key] = value
            column.addWidget(value)
            totals.addLayout(column, 1)
        self.values["Total Events"] = self.severity_values["total"]
        totals_layout.addLayout(totals)
        summary_layout.addWidget(totals_panel)

        counts_panel, counts_layout = card("SUMMARY")
        grid = QGridLayout()
        grid.setHorizontalSpacing(40)
        grid.setVerticalSpacing(9)
        self.summary_values = {}
        for i, kind in enumerate(SUMMARY_TYPES):
            row, column = i % 8, i // 8 * 2
            grid.addWidget(label(EVENT_LABELS[kind]), row, column)
            value = label("0")
            value.setAlignment(Qt.AlignmentFlag.AlignRight)
            self.summary_values[kind] = value
            grid.addWidget(value, row, column + 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)
        counts_layout.addLayout(grid)
        note = label("Totals include authorized-break entries. These informational entries add no risk.", role="muted")
        note.setWordWrap(True)
        counts_layout.addWidget(note)
        summary_layout.addWidget(counts_panel)
        summary_layout.addStretch()
        scroll.setWidget(summary)
        self.tabs.addTab(scroll, "Summary")

        timeline_page = QWidget()
        timeline_layout = QVBoxLayout(timeline_page)
        timeline_layout.setContentsMargins(16, 16, 16, 16)
        self.timeline_count = label("0 events · chronological order · UTC+5", role="muted")
        timeline_layout.addWidget(self.timeline_count)
        self.timeline_model = EventTimelineModel(self)
        self.timeline = QTableView()
        self.timeline.setModel(self.timeline_model)
        self.timeline.setAlternatingRowColors(True)
        self.timeline.setWordWrap(True)
        self.timeline.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.timeline.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.timeline.verticalHeader().hide()
        self.timeline.verticalHeader().setDefaultSectionSize(60)
        for column, width in enumerate((180, 215, 95, 90)):
            self.timeline.setColumnWidth(column, width)
            self.timeline.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        self.timeline.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.timeline.horizontalHeader().setMinimumSectionSize(70)
        timeline_layout.addWidget(self.timeline, 1)
        self.tabs.addTab(timeline_page, "Full timeline")
        self.evidence = EvidenceGallery(EVENT_LABELS, display_time, self)
        self.tabs.addTab(self.evidence, "Evidence")
        answers_page = QWidget()
        answers_layout = QVBoxLayout(answers_page)
        answers_layout.setContentsMargins(16, 16, 16, 16)
        self.answers_note = label("No built-in exam result", role="muted")
        self.answers_note.setWordWrap(True)
        answers_layout.addWidget(self.answers_note)
        self.answers_model = ExamAnswersModel(self)
        self.answers_table = QTableView()
        self.answers_table.setModel(self.answers_model)
        self.answers_table.setAlternatingRowColors(True)
        self.answers_table.setWordWrap(True)
        self.answers_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.answers_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.answers_table.verticalHeader().hide()
        self.answers_table.verticalHeader().setDefaultSectionSize(64)
        self.answers_table.setColumnWidth(0, 150)
        self.answers_table.setColumnWidth(2, 180)
        self.answers_table.setColumnWidth(3, 180)
        self.answers_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        answers_layout.addWidget(self.answers_table, 1)
        self.tabs.addTab(answers_page, "Answers")

    def _set_exam_result(self, result):
        self.answers_model.set_result(result)
        for value in self.exam_values.values():
            value.setText("—")
        if result is None:
            self.exam_percentage.setText("No built-in exam result")
            self.answers_note.setText("No built-in exam result")
            self.exam_note.setText("Academic performance and session risk are separate results.")
            return
        self.exam_score.setText(f"{result.score} / {result.max_score}")
        self.exam_percentage.setText(
            f"{result.percentage:.1f}%" if result.percentage is not None
            else "No automatically graded questions"
        )
        self.exam_values["Answered"].setText(str(len(result.question_ids) - result.unanswered_count))
        self.exam_values["Unanswered"].setText(str(result.unanswered_count))
        self.exam_values["Correct"].setText(str(result.correct_count))
        self.exam_values["Incorrect"].setText(str(result.incorrect_count))
        self.exam_values["Text pending review"].setText(str(result.text_pending_count))
        self.exam_values["Submitted"].setText(display_time(result.submitted_at))
        self.exam_values["Duration"].setText(format_duration(result.duration_seconds))
        interrupted = result.submission_reason == "interrupted"
        self.exam_values["Status"].setText(
            "Interrupted · provisional" if interrupted else
            "Time limit reached" if result.submission_reason == "expired" else "Submitted"
        )
        note = f"{result.exam_name} · Only choice questions contribute to the academic score."
        if result.text_pending_count:
            note += " Written answers await manual review."
        if interrupted:
            note += " The session ended before normal submission; these answers are provisional."
        self.exam_note.setText(note)
        self.answers_note.setText(
            f"{result.exam_name} · {len(result.question_ids)} questions · "
            "Written answers are stored for instructor review."
        )

    def set_result(self, result):
        self._set_exam_result(getattr(result, "exam_result", None))
        self.values["Student"].setText(result.student)
        self.values["Exam"].setText(result.exam)
        self.values["Start time"].setText(display_time(result.started_at))
        self.values["End time"].setText(display_time(result.ended_at))
        self.values["Duration"].setText(format_duration(result.duration_seconds))
        color = RISK_COLORS.get(result.risk_level, "#91a3bb")
        self.risk_score.setText(f"{result.risk_score} / 100")
        self.risk_score.setStyleSheet(f"font-size: 44px; font-weight: 700; color: {color};")
        self.risk_level.setText(result.risk_level)
        self.risk_level.setStyleSheet(f"font-size: 22px; font-weight: 600; color: {color};")
        self.risk_bar.setValue(result.risk_score)
        self.risk_bar.setStyleSheet(f"QProgressBar::chunk {{ background: {color}; }}")
        self.values["Final Risk"].setText(f"{result.risk_score} / 100 · {result.risk_level}")
        counts = Counter(event.type for event in result.events)
        severities = Counter(event.severity.value for event in result.events)
        self.values["Total Events"].setText(str(len(result.events)))
        for severity, value in self.severity_values.items():
            if severity != "total":
                value.setText(str(severities[severity]))
        for kind, value in self.summary_values.items():
            value.setText(str(counts[kind]))
        self.timeline_model.set_events(result.events)
        self.timeline_count.setText(f"{len(result.events)} events · chronological order · UTC+5")
        self.evidence.set_result(result)
        messages = []
        if result.error:
            messages.append(f"Session ended with a component error: {result.error}")
        if result.persistence_error:
            messages.append(f"Local save failed: {result.persistence_error}. The in-memory report remains available.")
        elif result.session_directory:
            messages.append(f"Saved locally: {result.session_directory}")
        messages.append("Review suspicious events and supporting images before drawing conclusions.")
        self.message.setText("\n".join(messages))
        self.tabs.setCurrentIndex(0)
        self.presentation.setCurrentWidget(self.detail_widget)

    def show_completion(self, result):
        # Keep the trusted report model available for legacy/controller APIs,
        # while the student sees only the separate completion widget.
        self.set_result(result)
        self.set_teacher_review(False)
        self.completion_page.set_result(result)
        self.presentation.setCurrentWidget(self.completion_page)

    def show_instructor_report(self):
        self.set_teacher_review(True)
        self.presentation.setCurrentWidget(self.detail_widget)

    def set_teacher_review(self, active):
        self.back_to_sessions_button.setVisible(bool(active))
        self.new_button.setVisible(not active)
        if not active:
            self._close_evidence_dialog()

    def _close_evidence_dialog(self):
        if self.evidence.dialog is not None:
            self.evidence.dialog.close()
            self.evidence.dialog.deleteLater()
            self.evidence.dialog = None

    def _return_to_sessions(self):
        self._close_evidence_dialog()
        self.back_to_sessions_requested.emit()

    def reset(self):
        self.completion_page.reset()
        self.set_teacher_review(False)
        self.presentation.setCurrentWidget(self.detail_widget)
        self._set_exam_result(None)
        for value in self.values.values():
            value.setText("—")
        for value in self.severity_values.values():
            value.setText("0")
        for value in self.summary_values.values():
            value.setText("0")
        self.risk_score.setText("0 / 100")
        self.risk_score.setStyleSheet("font-size: 44px; font-weight: 700; color: #43c6a4;")
        self.risk_level.setText("LOW")
        self.risk_level.setStyleSheet("font-size: 22px; font-weight: 600; color: #43c6a4;")
        self.risk_bar.setValue(0)
        self.timeline_model.set_events(())
        self.timeline_count.setText("0 events · chronological order · UTC+5")
        self.evidence.clear()
        self.message.setText("Review suspicious events and supporting images before drawing conclusions.")
        self.tabs.setCurrentIndex(0)

