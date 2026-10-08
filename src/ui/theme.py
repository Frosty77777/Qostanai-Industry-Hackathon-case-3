"""Central light academic palette and reusable desktop styles."""

from dataclasses import dataclass
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QVBoxLayout
from .localized_widgets import QLabel

@dataclass(frozen=True)
class LightColors:
    background: str = "#F5F7FA"
    surface: str = "#FFFFFF"
    surface_alt: str = "#EEF2F7"
    text: str = "#172B4D"
    muted: str = "#52647A"
    primary: str = "#1D4ED8"
    primary_hover: str = "#1E40AF"
    border: str = "#CBD5E1"
    success: str = "#15803D"
    success_surface: str = "#EAF7EE"
    warning: str = "#9A5700"
    warning_surface: str = "#FFF4DB"
    critical: str = "#B91C1C"
    critical_surface: str = "#FEEAEA"
    info: str = "#1D4ED8"
    info_surface: str = "#EAF2FF"
    camera: str = "#0B1220"
    camera_text: str = "#E6EDF6"
    disabled: str = "#64748B"
    robot_body: str = "#DCE8F2"
    robot_face: str = "#F7FAFD"

COLORS = LightColors()
MUTED = COLORS.muted
RISK_COLORS = {"LOW": COLORS.success, "MODERATE": COLORS.warning,
               "HIGH": COLORS.warning, "CRITICAL": COLORS.critical}
SEVERITY_COLORS = {"info": COLORS.success, "medium": COLORS.warning,
                   "high": COLORS.warning, "critical": COLORS.critical}
ASSISTANT_COLORS = {"CALM": COLORS.success, "NEUTRAL": COLORS.primary,
                    "ALERT": COLORS.warning, "SERIOUS": COLORS.warning,
                    "CRITICAL": COLORS.critical}

def text_style(color=None, *, font_size=None, bold=False):
    style = f"color: {color or COLORS.text};"
    if font_size is not None:
        style += f" font-size: {font_size}px;"
    if bold:
        style += " font-weight: 700;"
    return style

def panel_style(kind="info", *, padding=7, font_size=None, strong=False, radius=7):
    background, foreground = {
        "info": (COLORS.info_surface, COLORS.info),
        "success": (COLORS.success_surface, COLORS.success),
        "warning": (COLORS.warning_surface, COLORS.warning),
        "critical": (COLORS.critical_surface, COLORS.critical),
        "neutral": (COLORS.surface_alt, COLORS.muted),
    }[kind]
    return (f"background: {background}; border: 1px solid {COLORS.border}; "
            f"border-radius: {radius}px; padding: {padding}px; "
            + text_style(foreground, font_size=font_size, bold=strong))

def progress_style(color):
    return f"QProgressBar::chunk {{ background: {color}; border-radius: 4px; }}"

def navigation_style(active, answered):
    background = COLORS.primary if active else COLORS.success_surface if answered else COLORS.surface_alt
    foreground = COLORS.surface if active else COLORS.text
    return f"background: {background}; color: {foreground}; padding: 5px;"

VIDEO_STYLE = (f"background: {COLORS.camera}; color: {COLORS.camera_text}; "
               f"border: 1px solid {COLORS.border}; border-radius: 10px;")
COMPACT_INPUT_STYLE = "padding: 4px 8px;"
ASSISTANT_MESSAGE_STYLE = ("QLabel#assistantMessage { " + panel_style("info", padding=7, font_size=13, radius=9) + " }")
ASSISTANT_OPTIONS_STYLE = ("QToolButton { " + panel_style("neutral", padding=3, font_size=12, radius=5) + " }")
OPTION_INDICATOR_STYLE = (
    "QRadioButton, QCheckBox { padding: 8px 2px; }"
    f"QRadioButton::indicator, QCheckBox::indicator {{ width: 16px; height: 16px; background: {COLORS.surface}; border: 1px solid {COLORS.muted}; }}"
    "QRadioButton::indicator { border-radius: 8px; } QCheckBox::indicator { border-radius: 3px; }"
    f"QRadioButton::indicator:checked, QCheckBox::indicator:checked {{ background: {COLORS.primary}; border: 2px solid {COLORS.primary}; }}"
)

STYLESHEET = """
QWidget { background: %(background)s; color: %(text)s; font-family: 'Segoe UI'; font-size: 14px; }
QMainWindow, QStackedWidget { background: %(background)s; }
QFrame#card { background: %(surface)s; border: 1px solid %(border)s; border-radius: 12px; }
QFrame#card QLabel { background: transparent; }
QLabel#title { font-size: 32px; font-weight: 700; }
QLabel#heading { color: %(muted)s; font-size: 12px; font-weight: 600; }
QLabel#muted { color: %(muted)s; }
QLineEdit, QComboBox, QPlainTextEdit { background: %(surface)s; color: %(text)s; border: 1px solid %(border)s; border-radius: 7px; padding: 10px; }
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus { border: 1px solid %(primary)s; }
QComboBox QAbstractItemView { background: %(surface)s; color: %(text)s; selection-background-color: %(info_surface)s; selection-color: %(text)s; }
QPushButton { background: %(surface_alt)s; border: 1px solid %(border)s; border-radius: 7px; padding: 11px 18px; font-weight: 600; }
QPushButton:hover { background: %(info_surface)s; }
QPushButton#primary { background: %(primary)s; border-color: %(primary)s; color: %(surface)s; }
QPushButton#primary:hover { background: %(primary_hover)s; }
QPushButton#finish { background: %(critical_surface)s; border-color: %(critical)s; color: %(critical)s; }
QPushButton:disabled { color: %(disabled)s; background: %(surface_alt)s; border-color: %(border)s; }
QPushButton#primary:disabled, QPushButton#finish:disabled { color: %(disabled)s; background: %(surface_alt)s; border-color: %(border)s; }
QListWidget { background: %(surface)s; border: 1px solid %(border)s; border-radius: 6px; outline: none; }
QListWidget::item { padding: 9px 8px; border-bottom: 1px solid %(border)s; }
QListWidget::item:selected { background: %(info_surface)s; }
QProgressBar { background: %(surface_alt)s; border: none; border-radius: 4px; height: 8px; }
QProgressBar::chunk { border-radius: 4px; background: %(success)s; }
QScrollArea { border: none; }
QScrollBar:vertical { background: %(surface_alt)s; width: 9px; }
QScrollBar::handle:vertical { background: %(border)s; border-radius: 4px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QToolTip { color: %(text)s; background: %(surface)s; border: 1px solid %(border)s; }
QTabWidget::pane { border: 1px solid %(border)s; border-radius: 8px; }
QTabBar::tab { background: %(surface_alt)s; color: %(muted)s; padding: 10px 24px; margin-right: 4px; }
QTabBar::tab:selected { background: %(info_surface)s; color: %(primary)s; }
QTableView { background: %(surface)s; alternate-background-color: %(background)s; border: 1px solid %(border)s; gridline-color: %(border)s; selection-background-color: %(info_surface)s; selection-color: %(text)s; }
QHeaderView::section { background: %(surface_alt)s; color: %(text)s; padding: 8px; border: none; font-weight: 600; }
""" % vars(COLORS)


def label(text, *, role=None, size=None):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        widget.setObjectName(role)
    if size:
        widget.setStyleSheet(f"font-size: {size}px; font-weight: 600;")
    return widget


def card(title=None):
    widget = QFrame()
    widget.setObjectName("card")
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(12)
    if title:
        layout.addWidget(label(title, role="heading"))
    return widget, layout


def apply_status(widget, state, detail=""):
    widget.setText(state)
    widget.setToolTip(detail)
    color = COLORS.success if state in ("READY", "ACTIVE") else COLORS.warning if state in ("ERROR", "UNAVAILABLE") else COLORS.muted
    widget.setStyleSheet(f"color: {color}; font-weight: 600;")
