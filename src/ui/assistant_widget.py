"""Optional, static assistant confined to a page-owned draggable lane.

The pure model owns prompts and their timing. This module paints the mascot,
uses one single-shot timer for a pending prompt transition, and persists only
explicit user choices. It never creates a floating/native window.
"""

from math import ceil
from time import perf_counter
import weakref

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QCheckBox, QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from .assistant_model import AssistantConfig, AssistantModel, AssistantPreferencesStore, AssistantState


_STATE_COLORS = {
    AssistantState.CALM: "#71d5bb",
    AssistantState.NEUTRAL: "#8bbdf0",
    AssistantState.ALERT: "#f3cc7a",
    AssistantState.SERIOUS: "#f5a777",
    AssistantState.CRITICAL: "#ee9aaa",
}


class AssistantPortrait(QWidget):
    """Transparent PNG when present, otherwise a small code-native robot."""

    def __init__(self, config=None, parent=None):
        super().__init__(parent)
        self.config = config or AssistantConfig()
        self.state = AssistantState.CALM
        self._pixmap = QPixmap()
        self.setAccessibleName("AI Proctor Assistant")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.set_state(self.state, force=True)

    @property
    def uses_placeholder(self):
        return self._pixmap.isNull()

    def set_state(self, state, *, force=False):
        selected = AssistantState(state)
        if selected == self.state and not force:
            return
        self.state = selected
        # Missing or corrupt PNGs yield a null pixmap and a useful placeholder.
        self._pixmap = QPixmap(str(self.config.asset_path(selected)))
        self.setAccessibleDescription(f"{selected.value.title()} assistant. Drag to reposition within the assistant area.")
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self._pixmap.isNull():
            fitted = self._pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap((self.width() - fitted.width()) // 2,
                               (self.height() - fitted.height()) // 2, fitted)
            return
        # Normalized drawing keeps the five static expressions legible at both
        # setup and compact exam sizes, without image assets or animation loops.
        painter.translate((self.width() - min(self.size().width(), self.size().height())) / 2,
                          (self.height() - min(self.size().width(), self.size().height())) / 2)
        scale = min(self.width(), self.height()) / 100
        painter.scale(scale, scale)
        accent = QColor(_STATE_COLORS[self.state])
        painter.setPen(QPen(accent, 3))
        painter.drawLine(QPointF(50, 12), QPointF(50, 23))
        painter.setBrush(accent)
        painter.drawEllipse(QRectF(46, 5, 8, 8))
        painter.setBrush(QColor("#273e50"))
        painter.drawRoundedRect(QRectF(15, 23, 70, 65), 17, 17)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#101c29"))
        painter.drawRoundedRect(QRectF(24, 34, 52, 38), 10, 10)
        painter.setBrush(accent)
        for x in (37, 63):
            if self.state == AssistantState.CALM:
                painter.setPen(QPen(accent, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                painter.drawArc(QRectF(x - 5, 43, 10, 10), 0, 180 * 16)
                painter.setPen(Qt.PenStyle.NoPen)
            else:
                painter.drawEllipse(QRectF(x - 3, 43, 6, 8))
        painter.setPen(QPen(accent, 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        if self.state in (AssistantState.CALM, AssistantState.NEUTRAL):
            painter.drawArc(QRectF(42, 51, 16, 12), 180 * 16, 180 * 16)
        else:
            painter.drawLine(QPointF(43, 62), QPointF(57, 62))
        painter.setBrush(accent)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(QRectF(41, 78, 18, 4), 2, 2)


class AssistantWidget(QFrame):
    """Mascot and message move together within their immediate parent arena."""

    drag_finished = Signal()

    def __init__(self, parent=None, *, compact=True, config=None):
        super().__init__(parent)
        self.compact = bool(compact)
        self._message_visible = True
        self._drag_origin = None
        self._drag_position = None
        self._dock = None
        self.setObjectName("assistantPanel")
        self.setStyleSheet("QFrame#assistantPanel { background: transparent; border: none; }")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setAccessibleName("Draggable AI Proctor Assistant")
        layout = QHBoxLayout(self) if self.compact else QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.portrait = AssistantPortrait(config=config, parent=self)
        self.portrait.setFixedSize(60 if self.compact else 96, 60 if self.compact else 96)
        layout.addWidget(self.portrait, 0, Qt.AlignmentFlag.AlignCenter)
        self.message_label = QLabel(self)
        self.message_label.setObjectName("assistantMessage")
        self.message_label.setTextFormat(Qt.TextFormat.PlainText)
        self.message_label.setWordWrap(True)
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self.message_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.message_label.setAccessibleName("Assistant guidance")
        self.message_label.setStyleSheet(
            "QLabel#assistantMessage { color: #edf5ff; background: #203144; "
            "border: 1px solid #43576e; border-radius: 9px; padding: 7px; font-size: 13px; }")
        layout.addWidget(self.message_label, 1)
        self.installEventFilter(self)
        self.portrait.installEventFilter(self)
        self.message_label.installEventFilter(self)

    @property
    def state(self):
        return self.portrait.state

    def set_state(self, state):
        self.portrait.set_state(state)

    def set_message(self, message):
        text = str(message)
        if self.message_label.text() != text:
            self.message_label.setText(text)
            self.message_label.setAccessibleDescription(text)

    def set_visible(self, visible):
        if self._dock is not None:
            self._dock.set_visible(visible)
        else:
            self.setVisible(bool(visible))

    def set_message_visible(self, visible):
        if self._dock is not None:
            self._dock.set_message_visible(visible)
        else:
            self._apply_message_visible(visible)

    def _apply_message_visible(self, visible):
        self._message_visible = bool(visible)
        self.message_label.setVisible(self._message_visible)

    def save_preferences(self):
        return self._dock.save_preferences() if self._dock is not None else False

    def load_preferences(self):
        return self._dock.load_preferences() if self._dock is not None else None

    def _cancel_drag(self):
        self._drag_origin = self._drag_position = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind in (QEvent.Type.Hide, QEvent.Type.UngrabMouse, QEvent.Type.WindowDeactivate):
            self._cancel_drag()
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.globalPosition().toPoint()
            self._drag_position = self.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return True
        if kind == QEvent.Type.MouseMove and self._drag_origin is not None:
            if not event.buttons() & Qt.MouseButton.LeftButton:
                self._cancel_drag()
                return super().eventFilter(watched, event)
            proposed = self._drag_position + event.globalPosition().toPoint() - self._drag_origin
            parent = self.parentWidget()
            if parent is not None:
                self.move(max(0, min(proposed.x(), parent.width() - self.width())),
                          max(0, min(proposed.y(), parent.height() - self.height())))
            return True
        if (kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton
                and self._drag_origin is not None):
            self._cancel_drag()
            self.drag_finished.emit()
            return True
        return super().eventFilter(watched, event)


class AssistantDock(QWidget):
    """Reusable page-owned lane with permanently available visibility options."""

    def __init__(self, context="exam", *, preferences=None, config=None, clock=perf_counter,
                 height=None, compact=None, parent=None):
        super().__init__(parent)
        self.context = context
        self.compact = context != "setup" if compact is None else bool(compact)
        self._expanded_height = height if height is not None else (96 if self.compact else 230)
        self._options_expanded = False
        self._stopping = False
        self._clock = clock
        self.preferences = preferences or AssistantPreferencesStore()
        self.model = AssistantModel(context=context, config=config, clock=clock)
        self._current_preferences = self.preferences.preferences
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)
        options = QHBoxLayout()
        options.setContentsMargins(0, 0, 0, 0)
        options.addStretch(1)
        self.options_button = QToolButton(self)
        self.options_button.setText("ASSISTANT OPTIONS")
        self.options_button.setToolTip("Show or hide the optional assistant and its messages")
        self.options_button.setAccessibleName("Assistant view options")
        self.options_button.setCheckable(True)
        self.options_button.setFixedHeight(24)
        self.options_button.setStyleSheet("QToolButton { background: #223144; color: #d8e7f8; border: 1px solid #43576e; border-radius: 5px; padding: 2px 7px; font-size: 12px; }")
        self.show_assistant_action = QAction("Show Assistant", self)
        self.show_assistant_action.setCheckable(True)
        self.show_messages_action = QAction("Show Assistant Messages", self)
        self.show_messages_action.setCheckable(True)
        # These controls remain ordinary child widgets: opening assistant
        # options must not create a popup HWND outside the protected exam.
        self.options_panel = QWidget(self)
        inline = QHBoxLayout(self.options_panel)
        inline.setContentsMargins(0, 0, 0, 0)
        inline.setSpacing(10)
        self.show_assistant_checkbox = QCheckBox("Show Assistant", self.options_panel)
        self.show_messages_checkbox = QCheckBox("Show Assistant Messages", self.options_panel)
        for checkbox in (self.show_assistant_checkbox, self.show_messages_checkbox):
            checkbox.setStyleSheet("QCheckBox { color: #d8e7f8; font-size: 12px; }")
            checkbox.setAccessibleName(checkbox.text())
            inline.addWidget(checkbox)
        self.options_panel.setFixedHeight(24)
        self.options_panel.hide()
        self.show_assistant_checkbox.toggled.connect(self.show_assistant_action.setChecked)
        self.show_messages_checkbox.toggled.connect(self.show_messages_action.setChecked)
        self.show_assistant_action.toggled.connect(self.show_assistant_checkbox.setChecked)
        self.show_messages_action.toggled.connect(self.show_messages_checkbox.setChecked)
        self.show_assistant_action.toggled.connect(self.set_visible)
        self.show_messages_action.toggled.connect(self.set_message_visible)
        self.options_button.toggled.connect(self._toggle_options)
        if self.compact:
            options.addWidget(self.options_panel)
        options.addWidget(self.options_button)
        root.addLayout(options)
        if not self.compact:
            root.addWidget(self.options_panel)
        self.arena = QWidget(self)
        self.arena.setObjectName("assistantArena")
        self.arena.setStyleSheet("QWidget#assistantArena { background: transparent; }")
        self.arena.installEventFilter(self)
        root.addWidget(self.arena, 1)
        self.assistant = AssistantWidget(self.arena, compact=self.compact, config=self.model.config)
        self.assistant._dock = self
        self.assistant.drag_finished.connect(self.save_preferences)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.refresh)
        ref = weakref.ref(self)

        def preferences_updated(selected):
            current = ref()
            if current is not None:
                current._apply_preferences(selected)

        unsubscribe = self.preferences.subscribe(preferences_updated)
        self.destroyed.connect(lambda: unsubscribe())
        self._apply_preferences(self._current_preferences)
        self.refresh()

    def minimumSizeHint(self):
        return QSize(0, 28)

    def sizeHint(self):
        return QSize(440 if self.compact else 300, self.height())

    def set_risk(self, score):
        changed = self.model.set_risk(score)
        self.refresh()
        return changed

    def handle_event(self, event):
        accepted = self.model.handle_event(event)
        self.refresh()
        return accepted

    def set_break_active(self, active):
        self.model.set_break_active(active)
        self.refresh()

    def reset(self, context=None):
        if context is not None:
            self.context = context
        self._stopping = False
        self.model.reset(context=self.context)
        self.load_preferences()
        self.refresh()

    def set_stopping(self, stopping=True):
        self._stopping = bool(stopping)
        if self._stopping:
            self.timer.stop()
        else:
            self.refresh()

    def set_visible(self, visible):
        self.preferences.update(visible=bool(visible))

    def set_message_visible(self, visible):
        self.preferences.update(message_visible=bool(visible))

    def _apply_preferences(self, selected):
        self._current_preferences = selected
        for action, checked in ((self.show_assistant_action, selected.visible),
                                (self.show_messages_action, selected.message_visible)):
            previous = action.blockSignals(True)
            action.setChecked(checked)
            action.blockSignals(previous)
        for checkbox, checked in ((self.show_assistant_checkbox, selected.visible),
                                  (self.show_messages_checkbox, selected.message_visible)):
            previous = checkbox.blockSignals(True)
            checkbox.setChecked(checked)
            checkbox.blockSignals(previous)
        self.assistant._apply_message_visible(selected.message_visible)
        self.arena.setVisible(selected.visible)
        self.assistant.setVisible(selected.visible)
        self._update_height()
        self._place_assistant()
        self._schedule()

    def _toggle_options(self, expanded):
        self._options_expanded = bool(expanded)
        self.options_panel.setVisible(self._options_expanded)
        self._update_height()
        self._place_assistant()

    def _update_height(self):
        if self._current_preferences.visible:
            self.setFixedHeight(self._expanded_height)
        else:
            self.setFixedHeight(52 if self._options_expanded and not self.compact else 28)

    def load_preferences(self):
        selected = self.preferences.load()
        self._apply_preferences(selected)
        return selected

    def save_preferences(self):
        available_x = max(0, self.arena.width() - self.assistant.width())
        available_y = max(0, self.arena.height() - self.assistant.height())
        previous = self._current_preferences.position(self.context, default=self._default_position())
        position = (self.assistant.x() / available_x if available_x else previous[0],
                    self.assistant.y() / available_y if available_y else previous[1])
        return self.preferences.update(context=self.context, position=position)

    def _default_position(self):
        return (0.0, 0.5) if self.compact else (0.5, 0.5)

    def _place_assistant(self):
        if not self._current_preferences.visible:
            return
        width = min(self.arena.width(), (420 if self.compact else 290)
                    if self._current_preferences.message_visible else (60 if self.compact else 96))
        height = min(self.arena.height(), 64 if self.compact else
                     (190 if self._current_preferences.message_visible else 96))
        self.assistant.resize(max(0, width), max(0, height))
        x, y = self._current_preferences.position(self.context, default=self._default_position())
        self.assistant.move(round(x * max(0, self.arena.width() - width)),
                            round(y * max(0, self.arena.height() - height)))

    def refresh(self):
        self.model.advance()
        self.assistant.set_state(self.model.state)
        self.assistant.set_message(self.model.current_message())
        self._schedule()

    def _schedule(self):
        self.timer.stop()
        if self._stopping or not self.isVisible() or not self._current_preferences.visible:
            return
        delay = self.model.delay_until_deadline()
        if delay is not None:
            self.timer.start(max(1, ceil(delay * 1000)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_assistant()

    def eventFilter(self, watched, event):
        if watched is self.arena and event.type() == QEvent.Type.Resize:
            self._place_assistant()
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        super().showEvent(event)
        self.load_preferences()
        self.refresh()
        self._place_assistant()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)
