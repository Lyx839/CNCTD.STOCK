"""Tab 1 – Dashboard / Warenbestand."""

import csv
from pathlib import Path

from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..formatting import fmt_money, fmt_qty
from .article_dialog import ArticleDialog
from .common import (
    AppState,
    ArticleFilterProxy,
    ID_ROLE,
    ArticleTableModel,
    card,
    make_article_view,
    selected_article_ids,
)


class StatTile(QWidget):
    def __init__(self, label: str):
        super().__init__()
        frame, layout = card()
        layout.setSpacing(2)
        self.value = QLabel("–")
        self.value.setObjectName("StatValue")
        caption = QLabel(label)
        caption.setObjectName("StatLabel")
        layout.addWidget(self.value)
        layout.addWidget(caption)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)


class InventoryTab(QWidget):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.state = state
        self.db = state.db

        # Kennzahlen
        self.tile_count = StatTile("Artikel im Bestand")
        self.tile_value = StatTile("Gesamter Warenwert (netto)")
        self.tile_empty = StatTile("Artikel ohne Bestand")
        tiles = QHBoxLayout()
        tiles.setSpacing(14)
        for t in (self.tile_count, self.tile_value, self.tile_empty):
            tiles.addWidget(t)

        # Werkzeugleiste
        self.search = QLineEdit()
        self.search.setObjectName("Search")
        self.search.setPlaceholderText("🔍  Bestand durchsuchen (Nr., Bezeichnung, Einheit …)")
        self.search.setClearButtonEnabled(True)

        self.btn_add = QPushButton("＋  Artikel hinzufügen")
        self.btn_add.setObjectName("Primary")
        self.btn_edit = QPushButton("Artikel bearbeiten")
        self.btn_delete = QPushButton("Artikel löschen")
        self.btn_delete.setObjectName("Danger")
        self.btn_export = QPushButton("CSV-Export")

        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.btn_add)
        bar.addWidget(self.btn_edit)
        bar.addWidget(self.btn_delete)
        bar.addWidget(self.btn_export)

        # Tabelle
        self.model = ArticleTableModel(self)
        self.proxy = ArticleFilterProxy(self)
        self.view = make_article_view(self.model, self.proxy)
        self.view.setDragEnabled(False)

        table_card, table_layout = card("Warenbestand")
        table_layout.addLayout(bar)
        table_layout.addWidget(self.view, 1)
        self.hint = QLabel("Tipp: Doppelklick auf einen Artikel öffnet die Bearbeitung. "
                           "Rot markierte Mengen sind aufgebraucht.")
        self.hint.setObjectName("Hint")
        table_layout.addWidget(self.hint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)
        layout.addLayout(tiles)
        layout.addWidget(table_card, 1)

        # Signale
        self.search.textChanged.connect(self.proxy.setFilterFixedString)
        self.btn_add.clicked.connect(self.add_article)
        self.btn_edit.clicked.connect(self.edit_article)
        self.btn_delete.clicked.connect(self.delete_articles)
        self.btn_export.clicked.connect(self.export_csv)
        self.view.doubleClicked.connect(lambda _: self.edit_article())
        self.view.selectionModel().selectionChanged.connect(self._update_buttons)
        self.state.inventoryChanged.connect(self.reload)
        QShortcut(QKeySequence.Delete, self.view, activated=self.delete_articles)
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self.add_article)
        QShortcut(QKeySequence.Find, self, activated=self.search.setFocus)

        self.reload()

    # ------------------------------------------------------------------ data
    def reload(self) -> None:
        selected = set(selected_article_ids(self.view))
        articles = self.db.list_articles()
        self.model.set_articles(articles)
        count, _, value = self.db.inventory_summary()
        empty = sum(1 for a in articles if a.quantity <= 0)
        self.tile_count.value.setText(fmt_qty(count))
        self.tile_value.value.setText(fmt_money(value))
        self.tile_empty.value.setText(fmt_qty(empty))
        if selected:
            for row in range(self.proxy.rowCount()):
                idx = self.proxy.index(row, 0)
                if idx.data(ID_ROLE) in selected:
                    self.view.selectRow(row)
        self._update_buttons()

    def _update_buttons(self) -> None:
        n = len(self.view.selectionModel().selectedRows())
        self.btn_edit.setEnabled(n == 1)
        self.btn_delete.setEnabled(n >= 1)

    # --------------------------------------------------------------- actions
    def add_article(self) -> None:
        dlg = ArticleDialog(self.db, parent=self)
        if dlg.exec():
            self.state.inventoryChanged.emit()
            self._select_id(dlg.article.id)

    def edit_article(self) -> None:
        ids = selected_article_ids(self.view)
        if len(ids) != 1:
            return
        article = self.db.get_article(ids[0])
        if article is None:
            return
        dlg = ArticleDialog(self.db, article, parent=self)
        if dlg.exec():
            self.state.inventoryChanged.emit()

    def delete_articles(self) -> None:
        ids = selected_article_ids(self.view)
        if not ids:
            return
        if len(ids) == 1:
            a = self.db.get_article(ids[0])
            text = f"Artikel „{a.article_no} – {a.name}“ wirklich löschen?"
        else:
            text = f"{len(ids)} Artikel wirklich löschen?"
        answer = QMessageBox.question(self, "Artikel löschen", text + "\n\nDies kann nicht rückgängig gemacht werden.")
        if answer == QMessageBox.Yes:
            self.db.delete_articles(ids)
            self.state.inventoryChanged.emit()

    def _select_id(self, article_id: int) -> None:
        for row in range(self.proxy.rowCount()):
            idx = self.proxy.index(row, 0)
            if idx.data(ID_ROLE) == article_id:
                self.view.selectRow(row)
                self.view.scrollTo(idx)
                return

    def export_csv(self) -> None:
        default = str(Path.home() / "Documents" / "CNCTD_Bestand.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Bestand exportieren", default, "CSV-Datei (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.writer(fh, delimiter=";")
                writer.writerow(["Artikel-Nr.", "Bezeichnung", "Beschreibung", "Einheit", "Menge",
                                 "Stückpreis", "Gesamtpreis"])
                for a in self.db.list_articles():
                    writer.writerow([a.article_no, a.name, a.description, a.unit, fmt_qty(a.quantity),
                                     fmt_money(a.unit_price_cents, ""), fmt_money(a.total_cents, "")])
        except OSError as exc:
            QMessageBox.critical(self, "Export fehlgeschlagen", str(exc))
            return
        self.window().statusBar().showMessage(f"Bestand exportiert: {path}", 6000)
