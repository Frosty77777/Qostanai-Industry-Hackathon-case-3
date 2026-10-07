"""Responsive live preview, monitoring panel and bounded recent-event timeline."""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QProgressBar, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from monitoring import EventType
from monitoring.break_manager import BreakConfig
from vision.face_tracker import FaceStatus
from .session import format_duration
from .theme import RISK_COLORS, SEVERITY_COLORS, apply_status, card, label

TIMELINE_LIMIT = 80


class VideoPreview(QLabel):
    def __init__(self):
        super().__init__("Camera starting…")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(460, 280)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.setStyleSheet("background: #080d14; border: 1px solid #2a3749; border-radius: 10px;")
        self._source = None

    def set_image(self, image):
        self._source = QPixmap.fromImage(image)
        self._redraw()

    def _redraw(self):
        if self._source is not None:
            self.setPixmap(self._source.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                              Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._redraw()

    def reset(self):
        self._source = None
        self.clear()
        self.setText("Camera starting…")


class ExamPage(QWidget):
    finish_requested = Signal()
    break_requested = Signal(str, int)
    end_break_requested = Signal()
    authorization_mode_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.break_config = BreakConfig()
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(16)
        header = QHBoxLayout()
        names = QVBoxLayout()
        names.addWidget(label("AI Exam Guard", size=24))
        self.session_label = label("", role="muted")
        self.session_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        names.addWidget(self.session_label)
        header.addLayout(names, 1)
        clock = QVBoxLayout()
        clock.addWidget(label("SESSION TIME", role="heading"))
        self.timer_label = label("00:00:00", size=24)
        clock.addWidget(self.timer_label)
        header.addLayout(clock)
        header.addSpacing(24)
        self.authorize_break_button = QPushButton("AUTHORIZE BREAK")
        self.authorize_break_button.clicked.connect(self._open_authorization)
        header.addWidget(self.authorize_break_button)
        self.end_break_button = QPushButton("END BREAK")
        self.end_break_button.clicked.connect(self.end_break_requested)
        self.end_break_button.hide()
        header.addWidget(self.end_break_button)
        self.finish_button = QPushButton("FINISH EXAM")
        self.finish_button.setObjectName("finish")
        self.finish_button.clicked.connect(self.finish_requested)
        header.addWidget(self.finish_button)
        root.addLayout(header)
        body = QHBoxLayout()
        body.setSpacing(20)
        video_column = QVBoxLayout()
        self.banner = label("LOCAL MONITORING ACTIVE")
        self.banner.setMinimumHeight(44)
        self.banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("background: #1d3348; border-radius: 7px; color: #a7cff5; font-weight: 600;")
        video_column.addWidget(self.banner)
        self.video = VideoPreview()
        # Warning widgets share the video grid, so they stay prominent without
        # pushing the timeline off a laptop screen. Evidence pixels omit them.
        video_container = QWidget()
        overlay = QGridLayout(video_container)
        overlay.setContentsMargins(0, 0, 0, 0)
        overlay.addWidget(self.video, 0, 0)
        self.head_badge = label("HEAD: UNKNOWN")
        self.head_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        overlay.addWidget(self.head_badge, 0, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        warning_container = QWidget()
        warning_container.setStyleSheet("background: transparent;")
        warnings = QVBoxLayout(warning_container)
        warnings.setContentsMargins(12, 12, 12, 12)
        warnings.setSpacing(8)
        self.face_warning = label("WARNING\nSTUDENT LEFT CAMERA VIEW")
        self.face_warning.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.face_warning.setStyleSheet("background: #862b3d; border-radius: 8px; color: white; padding: 14px; font-size: 20px; font-weight: 700;")
        self.face_warning.hide()
        warnings.addWidget(self.face_warning)
        self.obstruction_warning = label("WARNING\nCAMERA VIEW OBSTRUCTED")
        self.obstruction_warning.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.obstruction_warning.setStyleSheet(self.face_warning.styleSheet())
        self.obstruction_warning.hide()
        warnings.addWidget(self.obstruction_warning)
        self.break_banner = label("AUTHORIZED BREAK\n00:00 remaining")
        self.break_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.break_banner.setStyleSheet("background: #164e46; border-radius: 8px; color: white; padding: 12px; font-size: 20px; font-weight: 700;")
        self.break_banner.hide()
        warnings.addWidget(self.break_banner)
        overlay.addWidget(warning_container, 0, 0, Qt.AlignmentFlag.AlignBottom)
        self.authorization_panel = QFrame()
        self.authorization_panel.setObjectName("card")
        authorization = QVBoxLayout(self.authorization_panel)
        authorization.setContentsMargins(20, 16, 20, 16)
        authorization.addWidget(label("INSTRUCTOR AUTHORIZATION", size=18))
        self.authorization_message = label("Enter teacher PIN to choose a break duration.")
        self.authorization_message.setWordWrap(True)
        authorization.addWidget(self.authorization_message)
        pin_row = QHBoxLayout()
        self.pin_input = QLineEdit()
        self.pin_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.pin_input.setPlaceholderText("Teacher PIN")
        pin_row.addWidget(self.pin_input, 1)
        self.verify_pin_button = QPushButton("VERIFY PIN")
        self.verify_pin_button.clicked.connect(self._verify_pin)
        self.pin_input.returnPressed.connect(self._verify_pin)
        pin_row.addWidget(self.verify_pin_button)
        authorization.addLayout(pin_row)
        controls = QHBoxLayout()
        self.break_duration = QComboBox()
        self.break_duration.hide()
        controls.addWidget(self.break_duration, 1)
        self.start_break_button = QPushButton("START BREAK")
        self.start_break_button.hide()
        self.start_break_button.clicked.connect(self._submit_break)
        controls.addWidget(self.start_break_button)
        cancel = QPushButton("CANCEL")
        cancel.clicked.connect(self._close_authorization)
        controls.addWidget(cancel)
        authorization.addLayout(controls)
        self.authorization_panel.hide()
        overlay.addWidget(self.authorization_panel, 0, 0, Qt.AlignmentFlag.AlignCenter)
        video_column.addWidget(video_container, 1)
        self.fps_label = label("FPS: warming up · Local CPU processing · Q: finish exam", role="muted")
        video_column.addWidget(self.fps_label)
        body.addLayout(video_column, 1)
        sidebar = QWidget()
        sidebar.setMinimumWidth(340)
        sidebar.setMaximumWidth(390)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(14)
        monitor, monitor_layout = card("MONITORING")
        monitor_layout.setContentsMargins(20, 14, 20, 14)
        monitor_layout.setSpacing(8)
        grid = QGridLayout()
        grid.setVerticalSpacing(8)
        self.values = {}
        for row, name in enumerate(("Face", "Persons", "Phone", "Head", "Security", "Camera", "AI Detection")):
            grid.addWidget(label(name, role="muted"), row, 0)
            value = label("—")
            value.setWordWrap(True)
            grid.addWidget(value, row, 1)
            self.values[name] = value
        monitor_layout.addLayout(grid)
        side.addWidget(monitor)
        risk, risk_layout = card("SESSION RISK")
        risk_layout.setContentsMargins(20, 14, 20, 14)
        risk_layout.setSpacing(8)
        risk_row = QHBoxLayout()
        self.risk_score = label("0 / 100", size=30)
        self.risk_level = label("LOW")
        risk_row.addWidget(self.risk_score, 1)
        risk_row.addWidget(self.risk_level)
        risk_layout.addLayout(risk_row)
        self.risk_bar = QProgressBar()
        self.risk_bar.setRange(0, 100)
        self.risk_bar.setTextVisible(False)
        risk_layout.addWidget(self.risk_bar)
        side.addWidget(risk)
        timeline_card, timeline_layout = card("EVENT TIMELINE")
        self.timeline = QListWidget()
        self.timeline.setMinimumHeight(100)
        timeline_layout.addWidget(self.timeline, 1)
        self.event_count = label("0 events · Most recent first", role="muted")
        timeline_layout.addWidget(self.event_count)
        side.addWidget(timeline_card, 1)
        body.addWidget(sidebar)
        root.addLayout(body, 1)
        self._total_events = 0
        self.banner_timer = QTimer(self)
        self.banner_timer.setSingleShot(True)
        self.banner_timer.timeout.connect(self._clear_banner)
        self.banner_seconds = 3.0

    def start(self, student, exam, banner_seconds=3.0, break_config=None):
        self.reset()
        self.break_config = break_config or BreakConfig()
        self.session_label.setText(f"{student} · {exam}")
        self.session_label.setToolTip(f"{student} · {exam}")
        self.banner_seconds = banner_seconds

    def _open_authorization(self):
        self.authorization_message.setText("Enter teacher PIN to choose a break duration.")
        self.pin_input.clear()
        self.pin_input.setEnabled(True)
        self.verify_pin_button.setEnabled(True)
        self.break_duration.hide()
        self.start_break_button.hide()
        self.authorization_panel.show()
        self.authorization_mode_changed.emit(True)
        self.pin_input.setFocus()

    def _verify_pin(self):
        if not self.break_config.accepts_pin(self.pin_input.text()):
            self.authorization_message.setText("Invalid teacher PIN. Break was not authorized.")
            self.pin_input.clear()
            return
        self.pin_input.setEnabled(False)
        self.verify_pin_button.setEnabled(False)
        self.authorization_message.setText("PIN verified. Select the authorized break duration.")
        self.break_duration.clear()
        for seconds in self.break_config.durations_seconds:
            self.break_duration.addItem(f"{seconds // 60} minute(s)", seconds)
        self.break_duration.show()
        self.start_break_button.show()

    def _submit_break(self):
        # Recheck even when invoked directly; the worker independently rechecks
        # again before changing monitoring state. PIN never enters the timeline.
        if (not self.pin_input.isEnabled() and self.break_config.accepts_pin(self.pin_input.text())
                and self.break_duration.currentData() in self.break_config.durations_seconds):
            self.break_requested.emit(self.pin_input.text(), self.break_duration.currentData())
            self._close_authorization()

    def _close_authorization(self):
        self.pin_input.clear()
        self.authorization_panel.hide()
        self.authorization_mode_changed.emit(False)

    def set_elapsed(self, seconds):
        self.timer_label.setText(format_duration(seconds))

    def set_statuses(self, statuses):
        mapping = {"Camera": "Camera", "AI Detection": "AI Detection", "Security Monitor": "Security"}
        for name, (state, detail) in statuses.items():
            if name in mapping:
                apply_status(self.values[mapping[name]], state, detail)
        if statuses.get("Face Tracking", (None,))[0] == "UNAVAILABLE":
            self.values["Face"].setToolTip(statuses["Face Tracking"][1])

    def apply_update(self, update):
        self.video.set_image(update.image)
        self.values["Face"].setText({FaceStatus.FACE_DETECTED: "Detected", FaceStatus.NO_FACE: "Missing",
                                    FaceStatus.MULTIPLE_FACES: "Multiple", FaceStatus.UNAVAILABLE: "Unavailable"}[update.face.status])
        self.values["Persons"].setText(str(update.person_count) if update.person_count < 2 else f"{update.person_count} (2+)")
        self.values["Phone"].setText("Detected" if update.phone_detected else "None")
        self.values["Head"].setText(update.face.head_direction.value)
        direction = update.face.head_direction.value
        color = "#43c6a4" if direction == "CENTER" else "#91a3bb" if direction == "UNKNOWN" else "#ffd17c"
        progress = (f" · {update.head_duration_seconds:.1f}/{update.head_threshold_seconds:g}s"
                    if update.head_threshold_seconds is not None else "")
        self.head_badge.setText(f"HEAD: {direction}{progress}")
        self.head_badge.setStyleSheet(f"background: #182535; color: {color}; border-radius: 7px; padding: 8px; font-size: 18px; font-weight: 700;")
        detail = ("Head pose unavailable: tracker unavailable or no single valid face/pose matrix."
                  if direction == "UNKNOWN" else "Live head pose; a sustained deviation emits an event after the configured threshold.")
        self.head_badge.setToolTip(detail)
        self.values["Head"].setToolTip(detail)
        self.values["Head"].setStyleSheet(f"color: {color}; font-weight: 700;")
        self.set_statuses(update.statuses)
        self.risk_score.setText(f"{update.risk_score} / 100")
        self.risk_level.setText(update.risk_level)
        color = RISK_COLORS[update.risk_level]
        self.risk_level.setStyleSheet(f"color: {color}; font-weight: 700;")
        self.risk_bar.setValue(update.risk_score)
        self.risk_bar.setStyleSheet(f"QProgressBar::chunk {{ background: {color}; border-radius: 4px; }}")
        self.fps_label.setText(f"FPS: {update.fps:.1f}" if update.fps is not None else "FPS: warming up")
        self.face_warning.setVisible(update.face_missing_warning and update.face.status == FaceStatus.NO_FACE
                                     and not update.break_active and not update.camera_obstructed_warning)
        self.obstruction_warning.setVisible(update.camera_obstructed_warning)
        self.break_banner.setVisible(update.break_active)
        remaining = update.break_remaining_seconds
        self.break_banner.setText(f"AUTHORIZED BREAK\n{remaining // 60:02d}:{remaining % 60:02d} remaining")
        self.authorize_break_button.setVisible(not update.break_active)
        self.end_break_button.setVisible(update.break_active)
        for event in update.events:
            self.add_event(event)

    def add_event(self, event):
        self._total_events += 1
        name = event.type.value.replace("_", " ")
        item = QListWidgetItem(f"{event.timestamp:%H:%M:%S}  {name}\n{event.severity.value.upper()}  ·  +{event.risk_delta or 0} risk")
        item.setForeground(QColor(SEVERITY_COLORS[event.severity.value]))
        item.setData(Qt.ItemDataRole.UserRole, event)
        item.setToolTip(event.message)
        self.timeline.insertItem(0, item)
        while self.timeline.count() > TIMELINE_LIMIT:
            self.timeline.takeItem(self.timeline.count() - 1)
        self.event_count.setText(f"{self._total_events} events · Showing latest {self.timeline.count()}")
        banner_text = {
            EventType.PHONE_DETECTED: "CELL PHONE DETECTED",
            EventType.MULTIPLE_PERSONS: "MULTIPLE PERSONS DETECTED",
            EventType.MULTIPLE_FACES: "MULTIPLE FACES DETECTED",
            EventType.ALT_TAB_ATTEMPT: "WINDOW SWITCH ATTEMPT",
            EventType.WINDOW_FOCUS_LOST: "EXAM WINDOW LOST FOCUS",
            EventType.CAMERA_OBSTRUCTED: "CAMERA VIEW OBSTRUCTED",
        }.get(event.type, name)
        self.show_banner(banner_text)

    def show_banner(self, message):
        self.banner.setText(message)
        self.banner.setStyleSheet("background: #553625; border-radius: 7px; color: #ffcd99; font-weight: 700;")
        self.banner_timer.start(int(self.banner_seconds * 1000))

    def _clear_banner(self):
        self.banner.setText("LOCAL MONITORING ACTIVE")
        self.banner.setStyleSheet("background: #1d3348; border-radius: 7px; color: #a7cff5; font-weight: 600;")

    def set_stopping(self):
        self.finish_button.setEnabled(False)
        self.authorize_break_button.setEnabled(False)
        self.end_break_button.setEnabled(False)
        self._close_authorization()
        self.banner_timer.stop()
        self.banner.setText("STOPPING MONITORING · Releasing camera and security listener…")

    def reset(self):
        self.banner_timer.stop()
        self._clear_banner()
        self.timeline.clear()
        self._total_events = 0
        self.event_count.setText("0 events · Most recent first")
        self.timer_label.setText("00:00:00")
        self.risk_score.setText("0 / 100")
        self.risk_level.setText("LOW")
        self.risk_level.setStyleSheet(f"color: {RISK_COLORS['LOW']}; font-weight: 700;")
        self.risk_bar.setValue(0)
        self.video.reset()
        self.face_warning.hide()
        self.obstruction_warning.hide()
        self.break_banner.hide()
        self._close_authorization()
        self.authorize_break_button.show()
        self.authorize_break_button.setEnabled(True)
        self.end_break_button.hide()
        self.end_break_button.setEnabled(True)
        self.head_badge.setText("HEAD: UNKNOWN")
        self.head_badge.setStyleSheet("background: #182535; color: #91a3bb; border-radius: 7px; padding: 8px; font-size: 18px; font-weight: 700;")
        self.finish_button.setEnabled(True)
        for value in self.values.values():
            value.setText("—")
            value.setStyleSheet("")
            value.setToolTip("")
