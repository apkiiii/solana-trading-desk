"""Thème sombre de l'application."""

BG = "#0b0f14"
PANEL = "#121922"
PANEL_2 = "#18212d"
BORDER = "#243041"
TEXT = "#e6edf3"
MUTED = "#8b98a9"
GREEN = "#14f195"
PURPLE = "#9945ff"
RED = "#ff4d6a"
AMBER = "#ffb547"
BLUE = "#4da3ff"

STYLESHEET = f"""
* {{ font-family: "Segoe UI", "Inter", "Helvetica Neue", Arial, sans-serif; font-size: 10pt; color: {TEXT}; }}
QMainWindow, QWidget#root {{ background: {BG}; }}
QWidget#sidebar {{ background: {PANEL}; border-right: 1px solid {BORDER}; }}
QLabel#brand {{ font-size: 13pt; font-weight: 700; color: {TEXT}; padding: 18px 16px 2px 16px; }}
QLabel#brandSub {{ color: {MUTED}; padding: 0 16px 14px 16px; font-size: 8.5pt; }}
QListWidget#nav {{ background: transparent; border: none; outline: none; padding: 4px 8px; }}
QListWidget#nav::item {{ padding: 9px 12px; border-radius: 8px; margin: 2px 0; color: {MUTED}; }}
QListWidget#nav::item:selected {{ background: {PANEL_2}; color: {TEXT}; border-left: 3px solid {GREEN}; }}
QListWidget#nav::item:hover {{ background: {PANEL_2}; }}
QWidget#topbar {{ background: {PANEL}; border-bottom: 1px solid {BORDER}; }}
QLabel#pageTitle {{ font-size: 15pt; font-weight: 700; }}
QLabel#muted {{ color: {MUTED}; }}
QLabel#badge {{ border-radius: 10px; padding: 3px 10px; font-weight: 700; font-size: 8.5pt; }}
QFrame#card {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 12px; }}
QLabel#cardLabel {{ color: {MUTED}; font-size: 8.5pt; text-transform: uppercase; letter-spacing: 1px; }}
QLabel#cardValue {{ font-size: 17pt; font-weight: 700; }}
QLabel#cardSub {{ color: {MUTED}; font-size: 8.5pt; }}
QFrame#banner {{ background: rgba(153, 69, 255, 0.12); border: 1px solid {PURPLE}; border-radius: 10px; }}
QPushButton {{ background: {PANEL_2}; border: 1px solid {BORDER}; border-radius: 8px; padding: 8px 16px;
              font-weight: 600; }}
QPushButton:hover {{ border-color: {BLUE}; }}
QPushButton:disabled {{ color: #4f5b6b; border-color: #1c2532; background: #10161e; }}
QPushButton#start {{ background: rgba(20, 241, 149, 0.15); border-color: {GREEN}; color: {GREEN}; }}
QPushButton#stop {{ background: rgba(255, 181, 71, 0.12); border-color: {AMBER}; color: {AMBER}; }}
QPushButton#emergency {{ background: rgba(255, 77, 106, 0.15); border-color: {RED}; color: {RED}; }}
QPushButton#primary {{ background: {PURPLE}; border-color: {PURPLE}; color: white; }}
QPushButton#primary:disabled {{ background: #2a1f42; color: #7a6a99; }}
QPushButton:disabled#start, QPushButton:disabled#stop, QPushButton:disabled#emergency {{
    background: #10161e; color: #4f5b6b; border-color: #1c2532; }}
QLineEdit, QPlainTextEdit, QTextBrowser {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 8px;
              padding: 7px; selection-background-color: {PURPLE}; }}
QPlainTextEdit#console {{ font-family: "Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace; font-size: 9pt; }}
QTableWidget {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 10px; gridline-color: {BORDER};
               alternate-background-color: {PANEL_2}; }}
QHeaderView::section {{ background: {PANEL_2}; color: {MUTED}; border: none; padding: 7px;
                        border-bottom: 1px solid {BORDER}; font-weight: 600; }}
QProgressBar {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 6px; text-align: center; height: 16px; }}
QProgressBar::chunk {{ background: {GREEN}; border-radius: 6px; }}
QStatusBar {{ background: {PANEL}; color: {MUTED}; border-top: 1px solid {BORDER}; }}
QScrollArea {{ border: none; background: transparent; }}
QToolTip {{ background: {PANEL_2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; }}
"""
