"""Responsive live preview, monitoring panel and bounded recent-event timeline."""

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QProgressBar, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from monitoring import EventType
from monitoring.break_manager import BreakConfig
from vision.face_tracker import FaceStatus
from .assistant_widget import AssistantDock
from .session import format_duration
from .theme import RISK_COLORS, SEVERITY_COLORS, apply_status, card, label
from .question_panel import QuestionPanel

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
    secure_exit_requested = Signal(str)

    def __init__(self, parent=None, *, assistant_preferences=None, assistant_clock=None,
                 assistant_config=None):
        super().__init__(parent)
        self.break_config = BreakConfig()
        self._authorization_action = "break"
        self._built_in = False
        self._compact_monitoring = False
        self._secure_mode_active = False
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
        self.timer_heading = label("SESSION TIME", role="heading")
        clock.addWidget(self.timer_heading)
        self.timer_label = label("00:00:00", size=24)
        clock.addWidget(self.timer_label)
        header.addLayout(clock)
        header.addSpacing(24)
        self.secure_session_indicator = label("WINDOWED SESSION", role="heading")
        self.secure_session_indicator.setStyleSheet("background: #283142; color: #a5b6cd; padding: 7px; border-radius: 6px;")
        header.addWidget(self.secure_session_indicator)
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
        self.exit_secure_button = QPushButton("EXIT SECURE MODE")
        self.exit_secure_button.clicked.connect(self._open_secure_authorization)
        self.exit_secure_button.hide()
        header.addWidget(self.exit_secure_button)
        root.addLayout(header)
        assistant_options = {"context": "exam", "preferences": assistant_preferences,
                             "config": assistant_config}
        if assistant_clock is not None:
            assistant_options["clock"] = assistant_clock
        self.assistant = AssistantDock(**assistant_options)
        body = QHBoxLayout()
        self._body = body
        body.setSpacing(20)
        self.monitoring_column = QWidget()
        video_column = QVBoxLayout(self.monitoring_column)
        video_column.setContentsMargins(0, 0, 0, 0)
        self._video_column = video_column
        self.question_panel = QuestionPanel()
        self.question_panel.hide()
        # Keep this optional lane inside the question column so it does not
        # increase the camera/timeline column's minimum height on laptops.
        self.question_column = QWidget()
        questions = QVBoxLayout(self.question_column)
        questions.setContentsMargins(0, 0, 0, 0)
        questions.setSpacing(8)
        questions.addWidget(self.question_panel, 1)
        questions.addWidget(self.assistant)
        body.addWidget(self.question_column)
        self.banner = label("LOCAL MONITORING ACTIVE")
        self.banner.setMinimumHeight(44)
        self.banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("background: #1d3348; border-radius: 7px; color: #a7cff5; font-weight: 600;")
        video_column.addWidget(self.banner)
        self.face_tracking_notice = label("")
        self.face_tracking_notice.setWordWrap(True)
        self.face_tracking_notice.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.face_tracking_notice.setStyleSheet("background: #283142; color: #ffd17c; padding: 8px; border-radius: 7px;")
        self.face_tracking_notice.hide()
        video_column.addWidget(self.face_tracking_notice)
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
        status_grid = QGridLayout()
        self._status_grid = status_grid
        status_grid.setVerticalSpacing(5)
        self.runtime_statuses = {}
        self._component_states = {}
        for index, name in enumerate(("Camera", "AI Detection", "Face Tracking", "Security Monitor", "Evidence", "Session")):
            title = {"AI Detection": "YOLO", "Security Monitor": "SECURITY"}.get(name, name.upper())
            status = label(f"{title}: CHECKING")
            status.setToolTip("Component initialization pending")
            self.runtime_statuses[name] = (title, status)
            status_grid.addWidget(status, index // 3, index % 3)
        video_column.addLayout(status_grid)
        body.addWidget(self.monitoring_column, 1)
        sidebar = QWidget()
        self._sidebar = sidebar
        sidebar.setMinimumWidth(340)
        sidebar.setMaximumWidth(390)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(14)
        monitor, monitor_layout = card("MONITORING")
        self._monitor_card, self._monitor_layout = monitor, monitor_layout
        monitor_layout.setContentsMargins(20, 14, 20, 14)
        monitor_layout.setSpacing(8)
        grid = QGridLayout()
        self._monitor_grid = grid
        grid.setVerticalSpacing(8)
        self.values = {}
        self._monitor_names = {}
        for row, name in enumerate(("Face", "Persons", "Phone", "Head", "Security", "Camera", "AI Detection")):
            caption = label(name, role="muted")
            self._monitor_names[name] = caption
            grid.addWidget(caption, row, 0)
            value = label("—")
            value.setWordWrap(True)
            grid.addWidget(value, row, 1)
            self.values[name] = value
        monitor_layout.addLayout(grid)
        side.addWidget(monitor)
        risk, risk_layout = card("SESSION RISK")
        self._risk_card, self._risk_layout = risk, risk_layout
        risk_layout.setContentsMargins(20, 14, 20, 14)
        risk_layout.setSpacing(8)
        risk_row = QHBoxLayout()
        self._risk_row = risk_row
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
        self._timeline_card, self._timeline_layout = timeline_card, timeline_layout
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

    def set_exam_attempt(self, attempt, security_config=None):
        """Attach local controls without moving any CV work onto the UI thread."""
        self._built_in = True
        self.question_panel.set_attempt(attempt, security_config)
        self.question_panel.show()
        self.finish_button.hide()
        self._body.setStretch(0, 65)
        self._body.setStretch(1, 35)
        self._body.setSpacing(16)
        self.layout().setContentsMargins(20, 16, 20, 16)
        self.layout().setSpacing(10)
        self._video_column.setSpacing(7)
        self.monitoring_column.setMinimumWidth(340)
        self.monitoring_column.setMaximumWidth(590)
        self.video.setMinimumSize(320, 240)
        self.banner.setMinimumHeight(28)
        self.banner.setStyleSheet("background: #1d3348; border-radius: 7px; color: #a7cff5; font-weight: 600; font-size: 12px;")
        self.fps_label.setWordWrap(True)
        self.fps_label.setStyleSheet("color: #91a3bb; font-size: 11px;")
        self.question_panel.question_text.setStyleSheet("font-size: 24px; font-weight: 600;")
        self.face_tracking_notice.setStyleSheet("background: #283142; color: #ffd17c; padding: 6px; border-radius: 7px; font-size: 12px;")
        if not self._compact_monitoring:
            self._compact_monitoring = True
            # The exam owns the main area; diagnostics remain in the camera column.
            self._body.removeWidget(self._sidebar)
            self._sidebar.hide()
            for widget in (self._monitor_card, self._risk_card, self._timeline_card):
                self._sidebar.layout().removeWidget(widget)
                self._video_column.addWidget(widget)
                widget.show()
            for layout in (self._monitor_layout, self._risk_layout, self._timeline_layout):
                layout.setContentsMargins(10, 6, 10, 6)
                layout.setSpacing(4)
                if layout.itemAt(0).widget() is not None:
                    layout.itemAt(0).widget().hide()
            for index, name in enumerate(("Face", "Persons", "Phone", "Head", "Camera", "Security")):
                self._monitor_grid.removeWidget(self._monitor_names[name])
                self._monitor_grid.removeWidget(self.values[name])
                row, column = divmod(index, 2)
                self._monitor_grid.addWidget(self._monitor_names[name], row, column * 2)
                self._monitor_grid.addWidget(self.values[name], row, column * 2 + 1)
                self._monitor_names[name].show()
                self.values[name].show()
                self._monitor_names[name].setStyleSheet("color: #91a3bb; font-size: 12px;")
                self.values[name].setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                self.values[name].setStyleSheet("font-size: 12px;")
            self._monitor_names["AI Detection"].hide()
            self.values["AI Detection"].hide()
            self._monitor_grid.setVerticalSpacing(4)
            self.risk_score.setStyleSheet("font-size: 20px; font-weight: 600;")
            self._risk_row.insertWidget(0, label("RISK", role="heading"))
            self.timeline.setMinimumHeight(48)
            self.timeline.setMaximumHeight(72)
            self.event_count.setStyleSheet("color: #91a3bb; font-size: 11px;")
            self.timeline_toggle = QPushButton("HIDE EVENT TIMELINE")
            self.timeline_toggle.setCheckable(True)
            self.timeline_toggle.setChecked(True)
            self.timeline_toggle.setStyleSheet("padding: 5px; font-size: 11px;")
            self.timeline_toggle.toggled.connect(self._toggle_timeline)
            self._video_column.insertWidget(self._video_column.indexOf(self._timeline_card), self.timeline_toggle)
            for _title, status in self.runtime_statuses.values():
                status.hide()
            self.technical_details_button = QPushButton("TECHNICAL STATUS")
            self.technical_details_button.setCheckable(True)
            self.technical_details_button.setStyleSheet("padding: 4px; font-size: 11px;")
            self.technical_details_button.toggled.connect(self._toggle_technical_status)
            self._video_column.insertWidget(self._video_column.indexOf(self.fps_label) + 1, self.technical_details_button)
            while self._status_grid.count():
                self._status_grid.takeAt(0)
            for index, (_title, status) in enumerate(self.runtime_statuses.values()):
                status.setStyleSheet("color: #91a3bb; font-size: 10px;")
                status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                status.setWordWrap(True)
                self._status_grid.addWidget(status, index // 2, index % 2)
            self._monitor_card.setMaximumHeight(108)
            self._risk_card.setMaximumHeight(58)
            self._timeline_card.setMaximumHeight(112)
        if attempt.exam.time_limit_seconds is not None:
            self.timer_heading.setText("TIME REMAINING")
            self.set_remaining(attempt.exam.time_limit_seconds)
        else:
            self.timer_heading.setText("SESSION TIME")

    def set_remaining(self, seconds):
        self.timer_heading.setText("TIME REMAINING")
        self.timer_label.setText(format_duration(seconds))

    def set_secure_mode_active(self, active):
        self._secure_mode_active = bool(active)
        self.exit_secure_button.setVisible(bool(active))
        self.secure_session_indicator.setText("SECURE SESSION ACTIVE" if active else "WINDOWED SESSION")
        self.secure_session_indicator.setStyleSheet(
            "background: #164e46; color: #79e3c6; padding: 7px; border-radius: 6px;" if active
            else "background: #283142; color: #a5b6cd; padding: 7px; border-radius: 6px;"
        )
        if active and self._built_in:
            self.timeline_toggle.setChecked(False)
        prefix = self.fps_label.text().split(" · ")[0]
        self.fps_label.setText(f"{prefix} · Local CPU · {self._fps_hint()}")

    def _toggle_timeline(self, visible):
        self._timeline_card.setVisible(bool(visible))
        self.timeline_toggle.setText("HIDE EVENT TIMELINE" if visible else "SHOW EVENT TIMELINE")

    def _toggle_technical_status(self, visible):
        for _title, status in self.runtime_statuses.values():
            status.setVisible(bool(visible))

    def _fps_hint(self):
        if self._secure_mode_active:
            return "Secure mode · Instructor PIN to exit"
        return "Q: finish when not typing" if self._built_in else "Q: finish exam"

    def _open_secure_authorization(self):
        self._open_authorization()
        self._authorization_action = "secure_exit"
        self.authorization_message.setText("Enter teacher PIN to exit secure mode. Monitoring will continue.")

    def start(self, student, exam, banner_seconds=3.0, break_config=None):
        self.reset()
        self.break_config = break_config or BreakConfig()
        self.session_label.setText(f"{student} · {exam}")
        self.session_label.setToolTip(f"{student} · {exam}")
        self.banner_seconds = banner_seconds

    def _open_authorization(self):
        self._authorization_action = "break"
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
            self.authorization_message.setText("Invalid teacher PIN. Authorization denied.")
            self.pin_input.clear()
            return
        if self._authorization_action == "secure_exit":
            pin = self.pin_input.text()
            self._close_authorization()
            self.secure_exit_requested.emit(pin)
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
            if self._component_states.get(name) == (state, detail):
                continue
            self._component_states[name] = (state, detail)
            if name in self.runtime_statuses:
                title, widget = self.runtime_statuses[name]
                apply_status(widget, state, detail)
                widget.setText(f"{title}: {state}")
                if self._built_in:
                    widget.setStyleSheet(widget.styleSheet() + " font-size: 10px;")
            if name in mapping:
                apply_status(self.values[mapping[name]], state, detail)
        if statuses.get("Face Tracking", (None,))[0] == "UNAVAILABLE":
            self.values["Face"].setToolTip(statuses["Face Tracking"][1])
        face_state, detail = self._component_states.get("Face Tracking", (None, ""))
        unavailable = face_state in ("UNAVAILABLE", "ERROR")
        self.face_tracking_notice.setVisible(unavailable)
        if unavailable:
            self.face_tracking_notice.setText(
                f"FACE TRACKING {face_state}: {detail or 'No technical details supplied'}\n"
                "Student absence monitoring paused. YOLO monitoring continues."
            )

    def apply_update(self, update):
        self.assistant.set_risk(update.risk_score)
        self.assistant.set_break_active(update.break_active)
        self.video.set_image(update.image)
        self.values["Face"].setText({FaceStatus.FACE_DETECTED: "Detected", FaceStatus.NO_FACE: "Missing",
                                    FaceStatus.MULTIPLE_FACES: "Multiple", FaceStatus.UNAVAILABLE: "Unavailable"}[update.face.status])
        self.values["Persons"].setText(str(update.person_count) if update.person_count < 2 else f"{update.person_count} (2+)")
        phone_state = getattr(update, "phone_state", "NONE")
        self.values["Phone"].setText("Tracked · occluded" if phone_state == "TRACKED_OCCLUDED"
                                      else "Detected" if update.phone_detected or phone_state == "DETECTED" else "None")
        self.values["Phone"].setToolTip("Probable phone track retained during brief occlusion" if phone_state == "TRACKED_OCCLUDED"
                                        else "Current phone detection state")
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
        fps_text = f"FPS: {update.fps:.1f}" if update.fps is not None else "FPS: warming up"
        self.fps_label.setText(f"{fps_text} · Local CPU · {self._fps_hint()}")
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
        self.assistant.handle_event(event)
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
        self.show_banner(banner_text, event.severity.value)

    def show_banner(self, message, severity=None):
        self.banner.setText(message)
        background, foreground = ("#862b3d", "white") if severity in ("high", "critical") else ("#164e46", "white") if severity == "info" else ("#553625", "#ffcd99")
        self.banner.setStyleSheet(f"background: {background}; border-radius: 7px; color: {foreground}; font-weight: 700;")
        self.banner_timer.start(int(self.banner_seconds * 1000))

    def _clear_banner(self):
        self.banner.setText("LOCAL MONITORING ACTIVE")
        self.banner.setStyleSheet("background: #1d3348; border-radius: 7px; color: #a7cff5; font-weight: 600;")

    def set_stopping(self):
        self.assistant.set_stopping()
        self.question_panel.set_active(False)
        self.finish_button.setEnabled(False)
        self.authorize_break_button.setEnabled(False)
        self.end_break_button.setEnabled(False)
        self.exit_secure_button.setEnabled(False)
        self._close_authorization()
        self.banner_timer.stop()
        self.banner.setText("STOPPING MONITORING · Releasing camera and security listener…")

    def reset(self):
        self.assistant.reset()
        self.question_panel.reset()
        self.question_panel.hide()
        self._built_in = False
        self._secure_mode_active = False
        self.secure_session_indicator.setText("WINDOWED SESSION")
        self.secure_session_indicator.setStyleSheet("background: #283142; color: #a5b6cd; padding: 7px; border-radius: 6px;")
        if self._compact_monitoring:
            self.timeline_toggle.setChecked(True)
            self.technical_details_button.setChecked(False)
        self.finish_button.show()
        self.exit_secure_button.hide()
        self.exit_secure_button.setEnabled(True)
        self.banner_timer.stop()
        self._clear_banner()
        self.timeline.clear()
        self._total_events = 0
        self.event_count.setText("0 events · Most recent first")
        self.timer_label.setText("00:00:00")
        self.timer_heading.setText("SESSION TIME")
        self.risk_score.setText("0 / 100")
        self.risk_level.setText("LOW")
        self.risk_level.setStyleSheet(f"color: {RISK_COLORS['LOW']}; font-weight: 700;")
        self.risk_bar.setValue(0)
        self.video.reset()
        self.face_tracking_notice.clear()
        self.face_tracking_notice.hide()
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
        for title, value in self.runtime_statuses.values():
            value.setText(f"{title}: CHECKING")
            value.setStyleSheet("color: #91a3bb;")
            value.setToolTip("Component initialization pending")
        self._component_states.clear()
