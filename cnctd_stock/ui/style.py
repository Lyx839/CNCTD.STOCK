"""Zentrales Stylesheet – Farben angelehnt an das CNCTD.STOCK-Icon."""

from ..paths import resource_path


def _asset(name: str) -> str:
    return resource_path(f"assets/{name}").as_posix()


PRIMARY = "#1D4ED8"
PRIMARY_DARK = "#1E3A8A"
ACCENT = "#22D3EE"
NAVY = "#0B1F4B"
BG = "#F3F6FB"
SURFACE = "#FFFFFF"
BORDER = "#D9E0EC"
TEXT = "#1B2333"
MUTED = "#5B6475"
DANGER = "#DC2626"
SUCCESS = "#059669"

STYLESHEET = f"""
* {{
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 10pt;
    color: {TEXT};
}}
QMainWindow, QDialog {{ background: {BG}; }}
QWidget#Page {{ background: {BG}; }}

/* ---------- Kopfleiste ---------- */
QFrame#HeaderBar {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {NAVY}, stop:1 {PRIMARY});
    border: none;
}}
QLabel#AppTitle {{ color: white; font-size: 17pt; font-weight: 700; letter-spacing: 1px; }}
QLabel#AppTitleAccent {{ color: {ACCENT}; font-size: 17pt; font-weight: 700; }}
QLabel#AppSubtitle {{ color: #C7D6FF; font-size: 9pt; }}

/* ---------- Tabs ---------- */
QTabWidget::pane {{ border: none; background: {BG}; }}
QTabBar {{ background: {NAVY}; }}
QTabBar::tab {{
    background: transparent;
    color: #AFC3F5;
    padding: 10px 26px;
    font-weight: 600;
    border: none;
    border-bottom: 3px solid transparent;
    min-width: 140px;
}}
QTabBar::tab:selected {{ color: white; border-bottom: 3px solid {ACCENT}; }}
QTabBar::tab:hover:!selected {{ color: white; }}

/* ---------- Unter-Reiter (z. B. Buchhaltung) ---------- */
QTabWidget#SubTabs QTabBar {{ background: transparent; border-bottom: 1px solid {BORDER}; }}
QTabWidget#SubTabs QTabBar::tab {{
    color: {MUTED}; padding: 8px 18px; min-width: 90px; border-bottom: 3px solid transparent;
}}
QTabWidget#SubTabs QTabBar::tab:selected {{ color: {PRIMARY}; border-bottom: 3px solid {PRIMARY}; }}
QTabWidget#SubTabs QTabBar::tab:hover:!selected {{ color: {TEXT}; }}

/* ---------- Karten / Gruppen ---------- */
QFrame#Card, QGroupBox {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QGroupBox {{ margin-top: 14px; padding: 16px 12px 12px 12px; font-weight: 600; }}
QGroupBox::title {{
    subcontrol-origin: margin; left: 12px; padding: 0 4px;
    color: {PRIMARY_DARK}; font-weight: 700;
}}
QLabel#CardTitle {{ font-size: 11pt; font-weight: 700; color: {PRIMARY_DARK}; }}
QLabel#StatValue {{ font-size: 16pt; font-weight: 700; color: {PRIMARY}; }}
QLabel#StatLabel {{ color: {MUTED}; font-size: 9pt; }}
QLabel#Muted {{ color: {MUTED}; }}
QLabel#Hint {{ color: {MUTED}; font-size: 9pt; }}
QLabel#TotalBig {{ font-size: 15pt; font-weight: 700; color: {PRIMARY}; }}
QLabel#Warning {{ color: {DANGER}; font-weight: 600; }}

/* ---------- Buttons ---------- */
QPushButton {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 7px 14px;
    font-weight: 600;
}}
QPushButton:hover {{ border-color: {PRIMARY}; color: {PRIMARY}; }}
QPushButton:pressed {{ background: #E6ECF8; }}
QPushButton:disabled {{ color: #A0A8B8; border-color: #E4E8F0; }}
QPushButton#Primary {{ background: {PRIMARY}; border: 1px solid {PRIMARY}; color: white; }}
QPushButton#Primary:hover {{ background: {PRIMARY_DARK}; color: white; }}
QPushButton#Primary:disabled {{ background: #9DB3EE; border-color: #9DB3EE; color: white; }}
QPushButton#Danger:hover {{ border-color: {DANGER}; color: {DANGER}; }}

/* ---------- Eingaben ---------- */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QDateEdit, QComboBox {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 7px;
    selection-background-color: {PRIMARY};
    selection-color: white;
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QDateEdit:focus, QComboBox:focus {{ border: 1px solid {PRIMARY}; }}
QLineEdit#Search {{ padding: 7px 10px; border-radius: 8px; }}

QRadioButton, QCheckBox {{ spacing: 7px; background: transparent; }}
QRadioButton::indicator, QCheckBox::indicator {{
    width: 14px; height: 14px; background: {SURFACE}; border: 2px solid #AEB8CC;
}}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator:hover, QCheckBox::indicator:hover {{ border-color: {PRIMARY}; }}
QRadioButton::indicator:checked {{ border: 5px solid {PRIMARY}; width: 8px; height: 8px; }}
QCheckBox::indicator:checked {{
    background: {PRIMARY}; border-color: {PRIMARY}; image: url({_asset("check.png")});
}}
QCheckBox::indicator:disabled {{ background: #EEF1F6; border-color: #D5DBE7; }}

/* ---------- Auswahllisten, Datums- und Zahlenfelder ---------- */
QComboBox::drop-down, QDateEdit::drop-down {{
    subcontrol-origin: padding; subcontrol-position: center right;
    width: 24px; border: none; background: transparent;
}}
QComboBox::down-arrow, QDateEdit::down-arrow {{ image: url({_asset("chevron_down.png")}); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE}; border: 1px solid {BORDER}; selection-background-color: #DCE6FF;
    selection-color: {TEXT}; outline: none;
}}
QSpinBox, QDoubleSpinBox {{ padding-right: 22px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border; subcontrol-position: top right;
    width: 20px; border: none; border-left: 1px solid {BORDER}; border-top-right-radius: 6px;
    background: #F4F6FB;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border; subcontrol-position: bottom right;
    width: 20px; border: none; border-left: 1px solid {BORDER}; border-bottom-right-radius: 6px;
    background: #F4F6FB;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background: #E3EAF8; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({_asset("chevron_up.png")}); width: 10px; height: 10px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({_asset("chevron_down.png")}); width: 10px; height: 10px; }}
QTableWidget QDoubleSpinBox {{ border-radius: 4px; padding: 2px 6px; margin: 2px 3px; }}

/* ---------- Kalender-Popup ---------- */
QCalendarWidget QWidget#qt_calendar_navigationbar {{ background: {PRIMARY}; }}
QCalendarWidget QToolButton {{ color: white; background: transparent; font-weight: 600; padding: 4px 8px; }}
QCalendarWidget QAbstractItemView {{ selection-background-color: {PRIMARY}; selection-color: white; }}

/* ---------- Tabellen ---------- */
QTableView, QTableWidget {{
    background: {SURFACE};
    alternate-background-color: #F6F8FD;
    border: 1px solid {BORDER};
    border-radius: 8px;
    gridline-color: transparent;
    selection-background-color: #DCE6FF;
    selection-color: {TEXT};
}}
QTableView::item, QTableWidget::item {{ padding: 4px 6px; border: none; }}
QHeaderView::section {{
    background: #EEF2FA;
    color: {PRIMARY_DARK};
    font-weight: 700;
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 7px 6px;
}}
QTableCornerButton::section {{ background: #EEF2FA; border: none; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #C5CEDE; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:horizontal {{ background: #C5CEDE; border-radius: 3px; min-width: 30px; }}
QScrollBar::handle:hover {{ background: #9FADC6; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; color: {MUTED}; }}
QSplitter::handle {{ background: transparent; }}
QToolTip {{ background: {NAVY}; color: white; border: none; padding: 5px; }}
"""
