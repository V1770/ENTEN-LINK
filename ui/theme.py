"""Dark DJ-booth theme — palette, colours, and global stylesheet for PyQt6."""
from __future__ import annotations
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QPalette, QColor

# ── Colour tokens ──────────────────────────────────────────────────────────────
C_BG         = "#0d0d0d"
C_BG_PANEL   = "#1a1a1a"
C_BG_WIDGET  = "#242424"
C_BORDER     = "#333333"
C_TEXT       = "#e0e0e0"
C_TEXT_DIM   = "#606060"

C_ACCENT     = "#00c8ff"   # cyan    — active / playing
C_BEAT_1     = "#ffffff"   # white   — downbeat flash
C_BEAT_N     = "#00c8ff"   # cyan    — beats 2-4

C_PLAY       = "#00e676"   # green   — playing
C_PAUSE      = "#ffee58"   # yellow  — paused / cued
C_STOP       = "#ef5350"   # red     — stopped / end-of-track
C_MASTER     = "#ff8800"   # orange  — tempo master
C_SYNC       = "#ab47bc"   # purple  — sync active

# ── Global stylesheet ──────────────────────────────────────────────────────────
STYLESHEET = f"""
QMainWindow, QDialog, QWidget {{
    background-color: {C_BG};
    color: {C_TEXT};
    font-family: "Helvetica Neue", "Arial", sans-serif;
    font-size: 12px;
}}

QGroupBox {{
    background-color: {C_BG_PANEL};
    border: 1px solid {C_BORDER};
    border-radius: 6px;
    margin-top: 8px;
    padding: 8px 6px 6px 6px;
    font-weight: bold;
    color: {C_ACCENT};
    font-size: 11px;
    letter-spacing: 2px;
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
}}

QLabel {{
    background: transparent;
    color: {C_TEXT};
}}

QStatusBar {{
    background-color: {C_BG_PANEL};
    color: {C_TEXT_DIM};
    border-top: 1px solid {C_BORDER};
    font-size: 11px;
}}

QToolTip {{
    background-color: {C_BG_WIDGET};
    color: {C_TEXT};
    border: 1px solid {C_BORDER};
}}

QSplitter::handle {{
    background: {C_BORDER};
    width: 1px;
    height: 1px;
}}

QFrame[frameShape="4"],
QFrame[frameShape="5"] {{
    color: {C_BORDER};
}}
"""


def apply_dark_theme(app: QApplication) -> None:
    app.setStyleSheet(STYLESHEET)
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window,          QColor(C_BG))
    pal.setColor(QPalette.ColorRole.WindowText,      QColor(C_TEXT))
    pal.setColor(QPalette.ColorRole.Base,            QColor(C_BG_WIDGET))
    pal.setColor(QPalette.ColorRole.AlternateBase,   QColor(C_BG_PANEL))
    pal.setColor(QPalette.ColorRole.Text,            QColor(C_TEXT))
    pal.setColor(QPalette.ColorRole.Button,          QColor(C_BG_PANEL))
    pal.setColor(QPalette.ColorRole.ButtonText,      QColor(C_TEXT))
    pal.setColor(QPalette.ColorRole.Highlight,       QColor(C_ACCENT))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#000000"))
    pal.setColor(QPalette.ColorRole.Link,            QColor(C_ACCENT))
    app.setPalette(pal)
