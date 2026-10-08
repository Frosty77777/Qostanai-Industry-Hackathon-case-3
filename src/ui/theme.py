"""Small shared visual vocabulary for the desktop shell."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

RISK_COLORS = {"LOW": "#43c6a4", "MODERATE": "#e5b85c", "HIGH": "#ff955f", "CRITICAL": "#ff667b"}
SEVERITY_COLORS = {"info": "#43c6a4", "medium": "#e5b85c", "high": "#ff955f", "critical": "#ff667b"}

STYLESHEET = """
QWidget { background: #101620; color: #e6ecf5; font-family: 'Segoe UI'; font-size: 14px; }
QMainWindow, QStackedWidget { background: #101620; }
QFrame#card { background: #192230; border: 1px solid #2a3749; border-radius: 12px; }
QFrame#card QLabel { background: transparent; }
QLabel#title { font-size: 32px; font-weight: 700; }
QLabel#heading { color: #a5b6cd; font-size: 12px; font-weight: 600; }
QLabel#muted { color: #91a3bb; }
QLineEdit, QComboBox { background: #101925; border: 1px solid #35465d; border-radius: 7px; padding: 10px; }
QLineEdit:focus, QComboBox:focus { border: 1px solid #5aa9f7; }
QComboBox QAbstractItemView { background: #192230; selection-background-color: #304764; }
QPushButton { background: #28374b; border: 1px solid #3a4d66; border-radius: 7px; padding: 11px 18px; font-weight: 600; }
QPushButton:hover { background: #354b68; }
QPushButton#primary { background: #388ce6; border-color: #388ce6; color: white; }
QPushButton#primary:hover { background: #4da0f4; }
QPushButton#finish { background: #462936; border-color: #875060; color: #ffc3cc; }
QPushButton:disabled { color: #6d7c91; background: #202b3a; border-color: #2e3b4e; }
QListWidget { background: #121b27; border: 1px solid #2a3749; border-radius: 6px; outline: none; }
QListWidget::item { padding: 9px 8px; border-bottom: 1px solid #263345; }
QListWidget::item:selected { background: #293c53; }
QProgressBar { background: #101925; border: none; border-radius: 4px; height: 8px; }
QProgressBar::chunk { border-radius: 4px; background: #43c6a4; }
QScrollArea { border: none; }
QScrollBar:vertical { background: #172130; width: 9px; }
QScrollBar::handle:vertical { background: #42556f; border-radius: 4px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QToolTip { color: #e6ecf5; background: #243247; border: 1px solid #42556f; }
QTabWidget::pane { border: 1px solid #2a3749; border-radius: 8px; }
QTabBar::tab { background: #192230; color: #a5b6cd; padding: 10px 24px; margin-right: 4px; }
QTabBar::tab:selected { background: #283f58; color: white; }
QTableView { background: #121b27; alternate-background-color: #182230; border: 1px solid #2a3749; gridline-color: #263345; selection-background-color: #293c53; }
QHeaderView::section { background: #243247; color: #a5b6cd; padding: 8px; border: none; font-weight: 600; }
"""


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
    color = "#43c6a4" if state in ("READY", "ACTIVE") else "#ff955f" if state in ("ERROR", "UNAVAILABLE") else "#91a3bb"
    widget.setStyleSheet(f"color: {color}; font-weight: 600;")
