"""Tab Kunden – Kundenstamm mit Rechnungs- und Lieferadresse."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..database import Customer, Database
from .common import AppState, card


class CustomerDialog(QDialog):
    def __init__(self, db: Database, customer: Customer | None = None, parent=None):
        super().__init__(parent)
        self.db = db
        self.customer = customer
        self.setWindowTitle("Kunde bearbeiten" if customer and customer.id else "Kunde anlegen")
        self.setMinimumWidth(520)

        self.no_edit = QLineEdit()
        self.billing_edit = QPlainTextEdit()
        self.billing_edit.setPlaceholderText("Firma / Name\nStraße Nr.\nPLZ Ort")
        self.billing_edit.setFixedHeight(96)
        self.billing_edit.setTabChangesFocus(True)
        self.cb_delivery = QCheckBox("Abweichende Lieferadresse")
        self.delivery_edit = QPlainTextEdit()
        self.delivery_edit.setPlaceholderText("Name\nStraße Nr. / Packstation\nPLZ Ort")
        self.delivery_edit.setFixedHeight(96)
        self.delivery_edit.setTabChangesFocus(True)
        self.email_edit = QLineEdit()
        self.phone_edit = QLineEdit()
        self.shipping_combo = QComboBox()
        self.shipping_combo.addItem("– keine –", "")
        for m in db.list_shipping_methods():
            self.shipping_combo.addItem(m.name, m.name)
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setFixedHeight(56)

        form = QFormLayout()
        form.setSpacing(9)
        form.addRow("Kunden-Nr. *", self.no_edit)
        form.addRow("Rechnungsadresse *", self.billing_edit)
        form.addRow("", self.cb_delivery)
        form.addRow("Lieferadresse", self.delivery_edit)
        form.addRow("E-Mail", self.email_edit)
        form.addRow("Telefon", self.phone_edit)
        form.addRow("Bevorzugte Versandart", self.shipping_combo)
        form.addRow("Notizen", self.notes_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Speichern")
        buttons.button(QDialogButtonBox.Save).setObjectName("Primary")
        buttons.button(QDialogButtonBox.Cancel).setText("Abbrechen")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self.cb_delivery.toggled.connect(self.delivery_edit.setEnabled)
        cu = customer or Customer(None, db.suggest_customer_no(), "")
        self.no_edit.setText(cu.customer_no or db.suggest_customer_no())
        self.billing_edit.setPlainText(cu.billing_address)
        self.delivery_edit.setPlainText(cu.delivery_address)
        self.cb_delivery.setChecked(bool(cu.delivery_address.strip()))
        self.delivery_edit.setEnabled(self.cb_delivery.isChecked())
        self.email_edit.setText(cu.email)
        self.phone_edit.setText(cu.phone)
        idx = self.shipping_combo.findData(cu.shipping_method)
        if idx < 0 and cu.shipping_method:
            self.shipping_combo.addItem(cu.shipping_method, cu.shipping_method)
            idx = self.shipping_combo.count() - 1
        self.shipping_combo.setCurrentIndex(max(idx, 0))
        self.notes_edit.setPlainText(cu.notes)
        self.billing_edit.setFocus()

    def _save(self) -> None:
        no = self.no_edit.text().strip()
        billing = self.billing_edit.toPlainText().strip()
        if not no or not billing:
            QMessageBox.warning(self, "Pflichtfelder", "Bitte Kunden-Nr. und Rechnungsadresse ausfüllen.")
            return
        cid = self.customer.id if self.customer else None
        if self.db.customer_no_exists(no, cid):
            QMessageBox.warning(self, "Kunden-Nr. vergeben", f"Die Kunden-Nr. „{no}“ ist bereits vergeben.")
            return
        self.customer = Customer(
            id=cid,
            customer_no=no,
            billing_address=billing,
            delivery_address=self.delivery_edit.toPlainText().strip() if self.cb_delivery.isChecked() else "",
            email=self.email_edit.text().strip(),
            phone=self.phone_edit.text().strip(),
            shipping_method=self.shipping_combo.currentData() or "",
            notes=self.notes_edit.toPlainText().strip(),
        )
        self.db.save_customer(self.customer)
        self.accept()


class CustomersTab(QWidget):
    COLS = ["Kunden-Nr.", "Name / Firma", "Ort", "Lieferadresse", "E-Mail", "Telefon", "Versandart", "Belege"]

    createDocumentRequested = Signal(int)   # Kunden-ID

    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.state = state
        self.db = state.db

        self.search = QLineEdit()
        self.search.setObjectName("Search")
        self.search.setPlaceholderText("🔍  Kunden durchsuchen (Name, Ort, Nr., E-Mail …)")
        self.search.setClearButtonEnabled(True)
        self.btn_add = QPushButton("＋  Kunde anlegen")
        self.btn_add.setObjectName("Primary")
        self.btn_edit = QPushButton("Kunde bearbeiten")
        self.btn_delete = QPushButton("Kunde löschen")
        self.btn_delete.setObjectName("Danger")
        self.btn_doc = QPushButton("Dokument für Kunde erstellen  ➜")

        bar = QHBoxLayout()
        bar.setSpacing(8)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.btn_add)
        bar.addWidget(self.btn_edit)
        bar.addWidget(self.btn_delete)
        bar.addWidget(self.btn_doc)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSortingEnabled(True)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(32)
        h = self.table.horizontalHeader()
        h.setHighlightSections(False)
        h.setSectionResizeMode(QHeaderView.Interactive)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        for col, width in {0: 100, 2: 170, 3: 110, 4: 200, 5: 130, 6: 130, 7: 70}.items():
            h.resizeSection(col, width)

        table_card, table_layout = card("Kunden")
        table_layout.addLayout(bar)
        table_layout.addWidget(self.table, 1)
        self.count_label = QLabel()
        self.count_label.setObjectName("Hint")
        table_layout.addWidget(self.count_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.addWidget(table_card, 1)

        self.search.textChanged.connect(self._apply_filter)
        self.btn_add.clicked.connect(self.add_customer)
        self.btn_edit.clicked.connect(self.edit_customer)
        self.btn_delete.clicked.connect(self.delete_customers)
        self.btn_doc.clicked.connect(self._request_document)
        self.table.doubleClicked.connect(lambda _: self.edit_customer())
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.state.customersChanged.connect(self.reload)
        self.state.documentsChanged.connect(self.reload)
        QShortcut(QKeySequence.Delete, self.table, activated=self.delete_customers)
        self.reload()

    def reload(self) -> None:
        selected = set(self._selected_ids())
        counts = self.db.customer_document_counts()
        customers = self.db.list_customers()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(customers))
        for r, cu in enumerate(customers):
            values = [cu.customer_no, cu.display_name, cu.city,
                      "abweichend" if cu.delivery_address.strip() else "wie Rechnung",
                      cu.email, cu.phone, cu.shipping_method, str(counts.get(cu.id, 0))]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c == 0:
                    item.setData(Qt.UserRole, cu.id)
                if c == 7:
                    item.setData(Qt.DisplayRole, counts.get(cu.id, 0))
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 1:
                    item.setToolTip(cu.billing_address)
                if c == 3 and cu.delivery_address.strip():
                    item.setToolTip(cu.delivery_address)
                self.table.setItem(r, c, item)
            if cu.id in selected:
                self.table.selectRow(r)
        self.table.setSortingEnabled(True)
        self.count_label.setText(f"{len(customers)} Kunde(n) gespeichert. Doppelklick öffnet die Bearbeitung.")
        self._apply_filter(self.search.text())
        self._update_buttons()

    def _apply_filter(self, text: str) -> None:
        text = text.lower().strip()
        for r in range(self.table.rowCount()):
            hay = " ".join(self.table.item(r, c).text().lower() for c in range(self.table.columnCount()))
            tip = (self.table.item(r, 1).toolTip() + " " + self.table.item(r, 3).toolTip()).lower()
            self.table.setRowHidden(r, bool(text) and text not in hay and text not in tip)

    def _selected_ids(self) -> list[int]:
        return [self.table.item(i.row(), 0).data(Qt.UserRole)
                for i in self.table.selectionModel().selectedRows()]

    def _update_buttons(self) -> None:
        n = len(self._selected_ids())
        self.btn_edit.setEnabled(n == 1)
        self.btn_doc.setEnabled(n == 1)
        self.btn_delete.setEnabled(n >= 1)

    def add_customer(self) -> None:
        if CustomerDialog(self.db, parent=self).exec():
            self.state.customersChanged.emit()

    def edit_customer(self) -> None:
        ids = self._selected_ids()
        if len(ids) != 1:
            return
        cu = self.db.get_customer(ids[0])
        if cu and CustomerDialog(self.db, cu, parent=self).exec():
            self.state.customersChanged.emit()

    def delete_customers(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        text = (f"Kunde „{self.db.get_customer(ids[0]).display_name}“ wirklich löschen?" if len(ids) == 1
                else f"{len(ids)} Kunden wirklich löschen?")
        text += "\n\nBereits erstellte Rechnungen und Lieferscheine bleiben erhalten."
        if QMessageBox.question(self, "Kunde löschen", text) == QMessageBox.Yes:
            self.db.delete_customers(ids)
            self.state.customersChanged.emit()

    def _request_document(self) -> None:
        ids = self._selected_ids()
        if len(ids) == 1:
            self.createDocumentRequested.emit(ids[0])
