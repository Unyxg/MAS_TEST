"""
theme.py - Visual design for MAS-QA-Bridge (dark "workbench" theme).

Colours live in one place so the stylesheet and the painted widgets (status
dots, outcome chips) always agree.
"""
from __future__ import annotations

COLORS = {
    "bg": "#16181d",
    "panel": "#1d2027",
    "panel_alt": "#232730",
    "border": "#2e333d",
    "text": "#e6e8eb",
    "muted": "#9199a5",
    "accent": "#4c8dff",
    "accent_hover": "#6aa1ff",
    "pass": "#2ea043",
    "fail": "#e5534b",
    "blocked": "#d29922",
    "na": "#8b949e",
    "rec": "#ff4d4f",
}

OUTCOME_COLORS = {
    "Passed": COLORS["pass"],
    "Failed": COLORS["fail"],
    "Blocked": COLORS["blocked"],
    "NotApplicable": COLORS["na"],
    "Unspecified": "#4b5260",
}

STYLESHEET = """
* {{ font-family: "Segoe UI", "Inter", "Helvetica Neue", sans-serif; font-size: 13px; }}
QMainWindow, QWidget#root {{ background: {bg}; color: {text}; }}
QWidget {{ color: {text}; }}
QToolTip {{ background: {panel_alt}; color: {text}; border: 1px solid {border}; padding: 4px; }}

/* ---------- header ---------- */
QFrame#header {{ background: {panel}; border-bottom: 1px solid {border}; }}
QLabel#brand {{ font-size: 15px; font-weight: 700; color: {text}; }}
QLabel#brandAccent {{ font-size: 15px; font-weight: 700; color: {accent}; }}
QLabel#exePath {{
    background: {bg}; border: 1px solid {border}; border-radius: 6px;
    padding: 5px 10px; color: {muted};
}}

/* ---------- panels ---------- */
QFrame#panel {{ background: {panel}; border: 1px solid {border}; border-radius: 10px; }}
QLabel#panelTitle {{ font-size: 11px; font-weight: 700; color: {muted}; letter-spacing: 1px; }}
QLabel#muted {{ color: {muted}; }}
QLabel#caseTitle {{ font-size: 15px; font-weight: 700; }}
QLabel#caseId {{ color: {accent}; font-weight: 700; }}

/* ---------- inputs ---------- */
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {{
    background: {bg}; border: 1px solid {border}; border-radius: 6px; padding: 5px 8px;
    selection-background-color: {accent};
}}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{ border-color: {accent}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{ background: {panel_alt}; border: 1px solid {border}; selection-background-color: {accent}; }}

/* ---------- buttons ---------- */
QPushButton, QToolButton {{
    background: {panel_alt}; border: 1px solid {border}; border-radius: 6px;
    padding: 6px 12px; color: {text};
}}
QPushButton:hover, QToolButton:hover {{ border-color: {accent}; }}
QPushButton:disabled, QToolButton:disabled {{ color: #5c6370; border-color: #262a32; }}
QPushButton#primary {{ background: {accent}; border-color: {accent}; color: white; font-weight: 600; }}
QPushButton#primary:hover {{ background: {accent_hover}; }}
QPushButton#primary:disabled {{ background: #2b3a55; border-color: #2b3a55; color: #8aa0c8; }}
QPushButton#record[recording="true"] {{ background: {rec}; border-color: {rec}; color: white; font-weight: 600; }}
QPushButton#record {{ color: {rec}; font-weight: 600; }}
QPushButton#publish {{ background: {accent}; border-color: {accent}; color: white; font-weight: 700; padding: 9px; }}
QPushButton#publish:disabled {{ background: #2b3a55; border-color: #2b3a55; color: #8aa0c8; }}

QToolButton#stepPass:checked {{ background: {pass}; border-color: {pass}; color: white; }}
QToolButton#stepFail:checked {{ background: {fail}; border-color: {fail}; color: white; }}
QToolButton#stepPass, QToolButton#stepFail, QToolButton#stepShot {{ padding: 3px 8px; min-width: 22px; }}

/* ---------- step cards ---------- */
QFrame#stepCard {{ background: {panel_alt}; border: 1px solid {border}; border-radius: 8px; }}
QFrame#stepCard[current="true"] {{ border: 1px solid {accent}; }}
QFrame#stepCard[outcome="Passed"] {{ border-left: 4px solid {pass}; }}
QFrame#stepCard[outcome="Failed"] {{ border-left: 4px solid {fail}; }}
QLabel#stepNo {{
    background: {bg}; border-radius: 11px; min-width: 22px; max-width: 22px;
    min-height: 22px; max-height: 22px; qproperty-alignment: AlignCenter; font-weight: 700; color: {muted};
}}
QLabel#expected {{ color: {muted}; }}
QLabel#sharedTag {{ color: {blocked}; font-size: 11px; }}

/* ---------- lists / trees ---------- */
QTreeWidget, QListWidget {{
    background: transparent; border: none; outline: none;
}}
QTreeWidget::item, QListWidget::item {{ padding: 5px 4px; border-radius: 5px; }}
QTreeWidget::item:selected, QListWidget::item:selected {{ background: #25406e; color: white; }}
QTreeWidget::item:hover, QListWidget::item:hover {{ background: {panel_alt}; }}

/* ---------- host ---------- */
QFrame#hostFrame {{ background: #0f1115; border: 1px solid {border}; border-radius: 10px; }}
QLabel#placeholder {{ color: {muted}; font-size: 14px; }}

/* ---------- pills / progress ---------- */
QLabel#pill {{ border-radius: 10px; padding: 3px 10px; font-size: 12px; font-weight: 600; background: {panel_alt}; color: {muted}; }}
QLabel#pill[kind="ok"] {{ background: #16361f; color: #56d364; }}
QLabel#pill[kind="warn"] {{ background: #3a2e12; color: #e3b341; }}
QLabel#pill[kind="err"] {{ background: #42191a; color: #ff7b72; }}
QLabel#pill[kind="rec"] {{ background: {rec}; color: white; }}
QLabel#pill[kind="info"] {{ background: #1b2d4d; color: #79b0ff; }}
QProgressBar {{ background: {bg}; border: none; border-radius: 3px; max-height: 6px; }}
QProgressBar::chunk {{ background: {pass}; border-radius: 3px; }}

/* ---------- dialogs / tabs / tables ---------- */
QDialog {{ background: {bg}; }}
QLineEdit#bugTitle {{ font-size: 15px; font-weight: 600; padding: 8px 10px; }}
QTabWidget::pane {{ border: 1px solid {border}; border-radius: 8px; background: {panel}; top: -1px; }}
QTabBar::tab {{ background: transparent; color: {muted}; padding: 7px 14px; border: none; margin-right: 2px; }}
QTabBar::tab:selected {{ color: {text}; border-bottom: 2px solid {accent}; }}
QTabBar::tab:hover {{ color: {text}; }}
QTableWidget {{ background: {bg}; border: 1px solid {border}; gridline-color: {border}; border-radius: 6px; }}
QHeaderView::section {{ background: {panel_alt}; color: {muted}; border: none; padding: 5px; }}
QTextBrowser {{ background: #fbfbfc; color: #1f2328; border: none; border-radius: 6px; padding: 10px; }}
QPushButton#bugLink {{ background: transparent; border: 1px solid {fail}; color: #ff7b72; padding: 3px 8px; }}
QPushButton#bugLink:hover {{ background: #42191a; }}

QSplitter::handle {{ background: {bg}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: #3a404b; border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: #3a404b; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QCheckBox::indicator {{ width: 15px; height: 15px; }}
QStatusBar {{ background: {panel}; color: {muted}; border-top: 1px solid {border}; }}
QDockWidget {{ color: {muted}; }}
QDockWidget::title {{ background: {panel}; padding: 4px 8px; }}
""".format(**COLORS)
