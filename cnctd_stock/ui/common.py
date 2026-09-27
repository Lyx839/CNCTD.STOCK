"""Gemeinsame UI-Bausteine: App-Zustand, Artikel-Tabellenmodell, Hilfsfunktionen."""

import json
import os
import subprocess
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QMimeData,
    QModelIndex,
    QObject,
    QSortFilterProxyModel,
    Qt,
    Signal,
)
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDoubleSpinBox,
    QFrame,
    QHeaderView,
    QLabel,
    QTableView,
    QVBoxLayout,
)

from ..database import Article, Database
from ..formatting import fmt_money, fmt_qty
from . import style

ARTICLE_MIME = "application/x-cnctd-article-ids"
SORT_ROLE = Qt.UserRole + 1
ID_ROLE = Qt.UserRole + 2


class AppState(QObject):
    """Hält die Datenbank und verteilt Änderungs-Signale an alle Tabs."""

    inventoryChanged = Signal()
    settingsChanged = Signal()
    documentsChanged = Signal()
    customersChanged = Signal()
    accountingChanged = Signal()

    def __init__(self, db: Database):
        super().__init__()
        self.db = db


class ArticleTableModel(QAbstractTableModel):
    COLUMNS = ["Art.-Nr.", "Bezeichnung", "Einheit", "Menge", "Stückpreis", "Gesamtpreis"]
    RIGHT_ALIGNED = {3, 4, 5}

    def __init__(self, parent=None, compact: bool = False):
        super().__init__(parent)
        self._rows: list[Article] = []
        # Kompakte Variante (Dokumente-Tab) ohne Gesamtpreis-Spalte.
        self.columns = self.COLUMNS[:-1] if compact else list(self.COLUMNS)

    def set_articles(self, articles: list[Article]) -> None:
        self.beginResetModel()
        self._rows = list(articles)
        self.endResetModel()

    def article_at(self, row: int) -> Article:
        return self._rows[row]

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.columns[section]
        if orientation == Qt.Horizontal and role == Qt.TextAlignmentRole and section in self.RIGHT_ALIGNED:
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        a = self._rows[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            return [a.article_no, a.name, a.unit, fmt_qty(a.quantity),
                    fmt_money(a.unit_price_cents), fmt_money(a.total_cents)][col]
        if role == SORT_ROLE:
            return [a.article_no.lower(), a.name.lower(), a.unit.lower(), a.quantity,
                    a.unit_price_cents, a.total_cents][col]
        if role == ID_ROLE:
            return a.id
        if role == Qt.TextAlignmentRole:
            if col in self.RIGHT_ALIGNED:
                return int(Qt.AlignRight | Qt.AlignVCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        if role == Qt.ForegroundRole and col == 3 and a.quantity <= 0:
            return QBrush(QColor(style.DANGER))
        if role == Qt.ToolTipRole and a.description:
            return a.description
        return None

    def flags(self, index):
        base = super().flags(index)
        if index.isValid():
            base |= Qt.ItemIsDragEnabled
        return base

    def mimeTypes(self):
        return [ARTICLE_MIME]

    def mimeData(self, indexes):
        ids = sorted({self._rows[i.row()].id for i in indexes if i.isValid()})
        mime = QMimeData()
        mime.setData(ARTICLE_MIME, json.dumps(ids).encode("utf-8"))
        return mime

    def supportedDragActions(self):
        return Qt.CopyAction


class ArticleFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(SORT_ROLE)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.setFilterKeyColumn(-1)


def make_article_view(model: ArticleTableModel, proxy: ArticleFilterProxy) -> QTableView:
    proxy.setSourceModel(model)
    view = QTableView()
    view.setModel(proxy)
    view.setSortingEnabled(True)
    view.sortByColumn(0, Qt.AscendingOrder)
    view.setSelectionBehavior(QAbstractItemView.SelectRows)
    view.setAlternatingRowColors(True)
    view.setShowGrid(False)
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(32)
    view.setWordWrap(False)
    header = view.horizontalHeader()
    header.setHighlightSections(False)
    header.setSectionResizeMode(QHeaderView.Interactive)
    header.setSectionResizeMode(1, QHeaderView.Stretch)
    header.resizeSection(0, 110)
    header.resizeSection(2, 70)
    for col in range(3, model.columnCount()):
        header.resizeSection(col, 110)
    return view


def selected_article_ids(view: QTableView) -> list[int]:
    ids = []
    for idx in view.selectionModel().selectedRows():
        ids.append(idx.data(ID_ROLE))
    return ids


def card(title: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("Card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(10)
    if title:
        lbl = QLabel(title)
        lbl.setObjectName("CardTitle")
        layout.addWidget(lbl)
    return frame, layout


def open_path(path: str | Path) -> None:
    """Öffnet eine Datei/einen Ordner mit dem Windows-Standardprogramm."""
    path = str(path)
    if hasattr(os, "startfile"):
        os.startfile(path)  # noqa: S606 – bewusst Windows-Standardviewer
    else:  # Entwicklungs-Fallback für andere Systeme
        subprocess.Popen(["xdg-open", path])


def print_path(path: str | Path) -> bool:
    """Versucht, die Datei direkt über das Standardprogramm zu drucken."""
    try:
        os.startfile(str(path), "print")
        return True
    except (OSError, AttributeError):
        return False


def reveal_in_explorer(path: str | Path) -> None:
    path = Path(path)
    if path.is_file():
        subprocess.Popen(["explorer", "/select,", str(path)])
    else:
        open_path(path if path.exists() else path.parent)


class CellSpinBox(QDoubleSpinBox):
    """Zahlenfeld für Tabellenzellen: ohne Pfeiltasten und ohne versehentliches Ändern per Mausrad."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setButtonSymbols(QDoubleSpinBox.NoButtons)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()
