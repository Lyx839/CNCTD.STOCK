"""Tab 2 – Dokumenten-Erstellung (Warenkorb -> Rechnung / Lieferschein)."""

import json
import tempfile
from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QCompleter,
    QDateEdit,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..database import DOC_DELIVERY, DOC_INVOICE, DOC_LABELS, Customer, GoBDError, now_stamp
from ..formatting import (
    cents_to_euros,
    euros_to_cents,
    fmt_money,
    fmt_qty,
    line_total_cents,
)
from .. import gobd
from ..pdf_engine import DocItem, DocumentData, build_document, compute_totals
from . import style
from .gobd_ui import ArchiveDialog
from .common import (
    ARTICLE_MIME,
    AppState,
    CellSpinBox,
    ArticleFilterProxy,
    ArticleTableModel,
    card,
    make_article_view,
    open_path,
    print_path,
    selected_article_ids,
)


# --------------------------------------------------------------------------- cart
class CartTable(QTableWidget):
    """Warenkorb-Tabelle, die Artikel per Drag & Drop aus der Bestandsliste annimmt."""

    articlesDropped = Signal(list)

    COLS = ["Art.-Nr.", "Bezeichnung", "Menge", "Einheit", "Einzelpreis", "Gesamt", "Lager"]

    def __init__(self, parent=None):
        super().__init__(0, len(self.COLS), parent)
        self.setHorizontalHeaderLabels(self.COLS)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setAlternatingRowColors(True)
        self.setShowGrid(False)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(36)
        h = self.horizontalHeader()
        h.setHighlightSections(False)
        h.setSectionResizeMode(QHeaderView.Interactive)
        h.setSectionResizeMode(1, QHeaderView.Stretch)
        for col, width in {0: 100, 2: 105, 3: 65, 4: 120, 5: 105, 6: 70}.items():
            h.resizeSection(col, width)
        self._drag_active = False

    def _accepts(self, event) -> bool:
        return event.mimeData().hasFormat(ARTICLE_MIME)

    def dragEnterEvent(self, event):
        if self._accepts(event):
            event.acceptProposedAction()
            self._set_highlight(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._accepts(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._set_highlight(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._set_highlight(False)
        if not self._accepts(event):
            event.ignore()
            return
        ids = json.loads(bytes(event.mimeData().data(ARTICLE_MIME)).decode("utf-8"))
        event.acceptProposedAction()
        self.articlesDropped.emit(ids)

    def _set_highlight(self, on: bool) -> None:
        self.setStyleSheet(f"QTableWidget {{ border: 2px dashed {style.PRIMARY}; }}" if on else "")

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.rowCount() == 0:
            p = QPainter(self.viewport())
            p.setPen(QColor(style.MUTED))
            p.drawText(self.viewport().rect(), Qt.AlignCenter,
                       "Warenkorb ist leer\n\nArtikel aus dem Bestand hierher ziehen\n"
                       "oder per Doppelklick hinzufügen")
            p.end()


# --------------------------------------------------------------------------- tab
class DocumentsTab(QWidget):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.state = state
        self.db = state.db
        self.cart: list[dict] = []
        self._auto_number = ""

        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(14)
        splitter.addWidget(self._build_stock_panel())
        splitter.addWidget(self._build_document_panel())
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 6)
        splitter.setSizes([400, 880])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.addWidget(splitter)

        self.state.inventoryChanged.connect(self._reload_stock)
        self.state.settingsChanged.connect(self._settings_changed)
        self.state.customersChanged.connect(self._reload_customers)
        self._reload_stock()
        self._reload_customers()
        self._reload_shipping()
        self._type_changed()

    # ---------------------------------------------------------------- panels
    def _build_stock_panel(self) -> QWidget:
        frame, layout = card("Bestand")
        self.stock_search = QLineEdit()
        self.stock_search.setObjectName("Search")
        self.stock_search.setPlaceholderText("🔍  Artikel suchen …")
        self.stock_search.setClearButtonEnabled(True)

        self.stock_model = ArticleTableModel(self, compact=True)
        self.stock_proxy = ArticleFilterProxy(self)
        self.stock_view = make_article_view(self.stock_model, self.stock_proxy)
        self.stock_view.setDragEnabled(True)
        self.stock_view.setDragDropMode(QAbstractItemView.DragOnly)
        self.stock_view.setDefaultDropAction(Qt.CopyAction)
        self.stock_view.setColumnHidden(2, True)
        self.stock_view.horizontalHeader().resizeSection(0, 78)
        self.stock_view.horizontalHeader().resizeSection(3, 62)
        self.stock_view.horizontalHeader().resizeSection(4, 82)

        btn_add = QPushButton("In den Warenkorb  ➜")
        btn_add.setObjectName("Primary")
        hint = QLabel("Artikel per Drag & Drop in den Warenkorb ziehen, doppelklicken "
                      "oder markieren und „In den Warenkorb“ wählen. Mehrfachauswahl mit Strg/Shift.")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)

        layout.addWidget(self.stock_search)
        layout.addWidget(self.stock_view, 1)
        layout.addWidget(hint)
        layout.addWidget(btn_add)

        self.stock_search.textChanged.connect(self.stock_proxy.setFilterFixedString)
        self.stock_view.doubleClicked.connect(lambda _: self._add_selected())
        btn_add.clicked.connect(self._add_selected)
        return frame

    def _build_document_panel(self) -> QWidget:
        panel = QWidget()
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(14)

        # --- Kopf: Dokumententyp + Stammdaten
        head, head_layout = card()
        head_layout.setSpacing(8)
        type_row = QHBoxLayout()
        self.rb_invoice = QRadioButton("Rechnung")
        self.rb_delivery = QRadioButton("Lieferschein")
        self.rb_invoice.setChecked(True)
        self.type_group = QButtonGroup(self)
        self.type_group.addButton(self.rb_invoice)
        self.type_group.addButton(self.rb_delivery)
        doc_title = QLabel("Dokument")
        doc_title.setObjectName("CardTitle")
        type_row.addWidget(doc_title)
        type_row.addSpacing(16)
        type_row.addWidget(self.rb_invoice)
        type_row.addWidget(self.rb_delivery)
        type_row.addStretch(1)
        self.btn_new = QPushButton("Neues Dokument")
        self.btn_new.setToolTip("Kunde, Adressen, Versand und Warenkorb zurücksetzen")
        self.btn_archive = QPushButton("📁  Dokumentenarchiv")
        type_row.addWidget(self.btn_new)
        type_row.addWidget(self.btn_archive)
        head_layout.addLayout(type_row)

        # Kundenauswahl (durchsuchbar)
        self.customer_combo = QComboBox()
        self.customer_combo.setEditable(True)
        self.customer_combo.setInsertPolicy(QComboBox.NoInsert)
        self.customer_combo.lineEdit().setPlaceholderText("Kunde suchen oder Adresse direkt eintragen …")
        completer = self.customer_combo.completer()
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        self.btn_save_customer = QPushButton("Als Kunde speichern")
        self.btn_save_customer.setToolTip("Adressen und Versandart im Kundenstamm speichern")
        self.current_customer_id: int | None = None

        self.number_edit = QLineEdit()
        self.number_edit.setReadOnly(True)
        self.number_edit.setToolTip("Wird beim Erstellen automatisch und fortlaufend vergeben (GoBD).")
        self.date_edit = self._date_edit()
        self.delivery_date_edit = self._date_edit()
        self.customer_no_edit = QLineEdit()
        self.customer_no_edit.setPlaceholderText("optional")
        self.reference_edit = QLineEdit()
        self.reference_edit.setPlaceholderText("z. B. Bestell-Nr.")
        self.delivery_label = QLabel("Leistungsdatum")
        self.notes_edit = QLineEdit()
        self.notes_edit.setPlaceholderText("Einleitungstext über den Positionen, optional")

        # Versand
        self.ship_combo = QComboBox()
        self.ship_cost = QDoubleSpinBox()
        self.ship_cost.setRange(0, 100_000)
        self.ship_cost.setDecimals(2)
        self.ship_cost.setSuffix(" €")
        self.ship_cost_label = QLabel("Versandkosten")
        self.tracking_edit = QLineEdit()
        self.tracking_edit.setPlaceholderText("Sendungsnummer, optional")

        fields = QGridLayout()
        fields.setHorizontalSpacing(10)
        fields.setVerticalSpacing(6)
        fields.addWidget(QLabel("Kunde"), 0, 0)
        customer_row = QHBoxLayout()
        customer_row.addWidget(self.customer_combo, 1)
        customer_row.addWidget(self.btn_save_customer)
        fields.addLayout(customer_row, 0, 1, 1, 3)
        fields.addWidget(QLabel("Nummer"), 1, 0)
        fields.addWidget(self.number_edit, 1, 1)
        fields.addWidget(QLabel("Kunden-Nr."), 1, 2)
        fields.addWidget(self.customer_no_edit, 1, 3)
        fields.addWidget(QLabel("Datum"), 2, 0)
        fields.addWidget(self.date_edit, 2, 1)
        fields.addWidget(self.delivery_label, 2, 2)
        fields.addWidget(self.delivery_date_edit, 2, 3)
        fields.addWidget(QLabel("Versandart"), 3, 0)
        fields.addWidget(self.ship_combo, 3, 1)
        fields.addWidget(self.ship_cost_label, 3, 2)
        fields.addWidget(self.ship_cost, 3, 3)
        fields.addWidget(QLabel("Sendungs-Nr."), 4, 0)
        fields.addWidget(self.tracking_edit, 4, 1)
        fields.addWidget(QLabel("Referenz"), 4, 2)
        fields.addWidget(self.reference_edit, 4, 3)
        fields.setColumnStretch(1, 1)
        fields.setColumnStretch(3, 1)
        fields.setRowStretch(5, 1)

        self.recipient_edit = QPlainTextEdit()
        self.recipient_edit.setPlaceholderText("Firma / Name\nStraße Nr.\nPLZ Ort")
        self.recipient_edit.setTabChangesFocus(True)
        self.recipient_edit.setFixedHeight(84)
        self.cb_delivery = QCheckBox("Abweichende Lieferadresse")
        self.delivery_edit = QPlainTextEdit()
        self.delivery_edit.setPlaceholderText("Name\nStraße Nr. / Packstation\nPLZ Ort")
        self.delivery_edit.setTabChangesFocus(True)
        self.delivery_edit.setFixedHeight(84)
        self.delivery_edit.setEnabled(False)

        right = QVBoxLayout()
        right.setSpacing(4)
        right.addWidget(QLabel("Rechnungsadresse / Empfänger *"))
        right.addWidget(self.recipient_edit)
        right.addWidget(self.cb_delivery)
        right.addWidget(self.delivery_edit)
        right.addStretch(1)

        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.addLayout(fields, 0, 0)
        grid.addLayout(right, 0, 1)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        head_layout.addLayout(grid)
        outer.addWidget(head)

        # --- Warenkorb
        cart_card, cart_layout = card()
        cart_layout.setSpacing(8)
        cart_head = QHBoxLayout()
        cart_title = QLabel("Warenkorb")
        cart_title.setObjectName("CardTitle")
        cart_head.addWidget(cart_title)
        cart_head.addSpacing(24)
        cart_head.addWidget(QLabel("Einleitung"))
        cart_head.addWidget(self.notes_edit, 1)
        cart_layout.addLayout(cart_head)
        self.cart_table = CartTable()
        cart_layout.addWidget(self.cart_table, 1)
        cart_btns = QHBoxLayout()
        self.btn_remove = QPushButton("Position entfernen")
        self.btn_clear = QPushButton("Warenkorb leeren")
        self.btn_clear.setObjectName("Danger")
        self.stock_warning = QLabel("")
        self.stock_warning.setObjectName("Warning")
        cart_btns.addWidget(self.btn_remove)
        cart_btns.addWidget(self.btn_clear)
        cart_btns.addSpacing(10)
        cart_btns.addWidget(self.stock_warning, 1)
        cart_layout.addLayout(cart_btns)
        outer.addWidget(cart_card, 1)

        # --- Abschluss: Summen + Aktionen (im Warenkorb, spart Platz)
        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setStyleSheet(f"color: {style.BORDER};")
        cart_layout.addWidget(divider)
        foot_layout = cart_layout
        row = QHBoxLayout()
        self.totals_label = QLabel()
        self.totals_label.setObjectName("Muted")
        self.total_big = QLabel()
        self.total_big.setObjectName("TotalBig")
        totals_box = QVBoxLayout()
        totals_box.setSpacing(0)
        totals_box.addWidget(self.totals_label)
        totals_box.addWidget(self.total_big)
        row.addLayout(totals_box, 1)

        self.cb_deduct = QCheckBox("Bestand nach Erstellung abbuchen")
        self.cb_deduct.setToolTip("Zieht die Mengen im Warenkorb vom Lagerbestand ab.")
        self.btn_preview = QPushButton("Vorschau")
        self.btn_preview.setToolTip("Erstellt eine unverbindliche Vorschau ohne Nummernvergabe und Archiv-Eintrag.")
        self.btn_print = QPushButton("🖨  Erstellen && drucken")
        self.btn_create = QPushButton("PDF erstellen && öffnen")
        self.btn_create.setObjectName("Primary")
        actions = QVBoxLayout()
        actions.addWidget(self.cb_deduct, 0, Qt.AlignRight)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.btn_preview)
        btn_row.addWidget(self.btn_print)
        btn_row.addWidget(self.btn_create)
        actions.addLayout(btn_row)
        row.addLayout(actions)
        foot_layout.addLayout(row)

        # Signale
        self.rb_invoice.toggled.connect(self._type_changed)
        self.cart_table.articlesDropped.connect(self.add_to_cart)
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_clear.clicked.connect(self._clear_cart)
        self.btn_archive.clicked.connect(self._open_archive)
        self.btn_new.clicked.connect(self._new_document)
        self.customer_combo.activated.connect(self._customer_chosen)
        self.btn_save_customer.clicked.connect(self._save_customer)
        self.cb_delivery.toggled.connect(self.delivery_edit.setEnabled)
        self.ship_combo.currentIndexChanged.connect(self._shipping_chosen)
        self.ship_cost.valueChanged.connect(self._update_totals)
        self.btn_preview.clicked.connect(self._preview)
        self.btn_create.clicked.connect(lambda: self._create(print_after=False))
        self.btn_print.clicked.connect(lambda: self._create(print_after=True))
        self.date_edit.dateChanged.connect(self._refresh_number)
        QShortcut(QKeySequence.Delete, self.cart_table, activated=self._remove_selected)
        return panel

    @staticmethod
    def _date_edit() -> QDateEdit:
        edit = QDateEdit(QDate.currentDate())
        edit.setCalendarPopup(True)
        edit.setDisplayFormat("dd.MM.yyyy")
        return edit

    # ------------------------------------------------------------ helpers
    def doc_type(self) -> str:
        return DOC_INVOICE if self.rb_invoice.isChecked() else DOC_DELIVERY

    def _reload_stock(self) -> None:
        self.stock_model.set_articles(self.db.list_articles())
        # Artikeldaten im Warenkorb aktualisieren (z. B. geänderter Name), gelöschte behalten.
        self._rebuild_cart()

    def _settings_changed(self) -> None:
        self._reload_shipping()
        self._refresh_number(force=True)
        self._update_totals()

    # ----------------------------------------------------------- customers
    def _reload_customers(self) -> None:
        combo = self.customer_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("", None)
        for cu in self.db.list_customers():
            label = f"{cu.display_name} · {cu.city} ({cu.customer_no})" if cu.city else \
                f"{cu.display_name} ({cu.customer_no})"
            combo.addItem(label, cu.id)
        idx = combo.findData(self.current_customer_id) if self.current_customer_id else -1
        if idx >= 0:
            combo.setCurrentIndex(idx)
        else:
            self.current_customer_id = None
            combo.setCurrentIndex(0)
        combo.blockSignals(False)

    def _customer_chosen(self, index: int) -> None:
        customer_id = self.customer_combo.itemData(index)
        if customer_id:
            self.select_customer(customer_id)
        else:
            self.current_customer_id = None

    def select_customer(self, customer_id: int) -> None:
        """Übernimmt Kundendaten (auch vom Kunden-Tab aus aufrufbar)."""
        cu = self.db.get_customer(customer_id)
        if cu is None:
            return
        self.current_customer_id = cu.id
        idx = self.customer_combo.findData(cu.id)
        if idx >= 0:
            self.customer_combo.setCurrentIndex(idx)
            self.customer_combo.lineEdit().setCursorPosition(0)
        self.customer_no_edit.setText(cu.customer_no)
        self.recipient_edit.setPlainText(cu.billing_address)
        self.delivery_edit.setPlainText(cu.delivery_address)
        self.cb_delivery.setChecked(bool(cu.delivery_address.strip()))
        if cu.shipping_method:
            idx = self.ship_combo.findText(cu.shipping_method)
            if idx >= 0:
                self.ship_combo.setCurrentIndex(idx)

    def _save_customer(self) -> None:
        billing = self.recipient_edit.toPlainText().strip()
        if not billing:
            QMessageBox.warning(self, "Adresse fehlt", "Bitte zuerst die Rechnungsadresse eintragen.")
            self.recipient_edit.setFocus()
            return
        delivery = self.delivery_edit.toPlainText().strip() if self.cb_delivery.isChecked() else ""
        shipping = self._shipping_method()
        existing = self.db.get_customer(self.current_customer_id) if self.current_customer_id else None
        if existing:
            answer = QMessageBox.question(
                self, "Kunde aktualisieren",
                f"Die Daten von „{existing.display_name}“ ({existing.customer_no}) mit den aktuellen "
                "Adressen und der Versandart überschreiben?")
            if answer != QMessageBox.Yes:
                return
            existing.billing_address, existing.delivery_address = billing, delivery
            existing.shipping_method = shipping or existing.shipping_method
            self.db.save_customer(existing)
            customer = existing
        else:
            no = self.customer_no_edit.text().strip()
            if not no or self.db.customer_no_exists(no):
                no = self.db.suggest_customer_no()
            customer = Customer(None, no, billing, delivery, shipping_method=shipping)
            self.db.save_customer(customer)
        self.current_customer_id = customer.id
        self.customer_no_edit.setText(customer.customer_no)
        self.state.customersChanged.emit()
        self.window().statusBar().showMessage(
            f"Kunde „{customer.display_name}“ ({customer.customer_no}) gespeichert.", 6000)

    # ------------------------------------------------------------ shipping
    def _reload_shipping(self) -> None:
        combo = self.ship_combo
        current = self._shipping_method()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("– kein Versand –", None)
        for m in self.db.list_shipping_methods():
            price = f"  ({fmt_money(m.price_cents)})" if m.price_cents else ""
            combo.addItem(m.name, m.price_cents)
            combo.setItemData(combo.count() - 1, f"{m.name}{price}", Qt.ToolTipRole)
        idx = combo.findText(current) if current else 0
        combo.setCurrentIndex(max(idx, 0))
        combo.blockSignals(False)

    def _shipping_chosen(self, index: int) -> None:
        price = self.ship_combo.itemData(index)
        self.ship_cost.setValue(cents_to_euros(price) if price else 0.0)
        self._update_totals()

    def _shipping_cents(self) -> int:
        if self.doc_type() != DOC_INVOICE:
            return 0
        return euros_to_cents(self.ship_cost.value())

    def _shipping_method(self) -> str:
        return self.ship_combo.currentText() if self.ship_combo.currentData() is not None else ""

    def _new_document(self) -> None:
        if self.cart and QMessageBox.question(
                self, "Neues Dokument", "Warenkorb, Kunde und Adressen zurücksetzen?") != QMessageBox.Yes:
            return
        self.cart.clear()
        self._rebuild_cart()
        self.current_customer_id = None
        self.customer_combo.setCurrentIndex(0)
        self.customer_combo.setEditText("")
        for w in (self.customer_no_edit, self.reference_edit, self.notes_edit, self.tracking_edit):
            w.clear()
        self.recipient_edit.clear()
        self.delivery_edit.clear()
        self.cb_delivery.setChecked(False)
        self.ship_combo.setCurrentIndex(0)
        self.cb_deduct.setChecked(False)
        self.date_edit.setDate(QDate.currentDate())
        self.delivery_date_edit.setDate(QDate.currentDate())
        self._refresh_number(force=True)

    def _refresh_number(self, *_, force: bool = False) -> None:
        self.number_edit.setText(self.db.next_doc_number(self.doc_type(), self.date_edit.date().toPython()))

    def _type_changed(self) -> None:
        invoice = self.doc_type() == DOC_INVOICE
        self.delivery_label.setText("Leistungsdatum" if invoice else "Lieferdatum")
        self.ship_cost.setVisible(invoice)
        self.ship_cost_label.setVisible(invoice)
        self.cart_table.setColumnHidden(4, not invoice)
        self.cart_table.setColumnHidden(5, not invoice)
        self._refresh_number(force=True)
        self._update_totals()

    # --------------------------------------------------------------- cart
    def _add_selected(self) -> None:
        ids = selected_article_ids(self.stock_view)
        if ids:
            self.add_to_cart(ids)

    def add_to_cart(self, ids: list[int]) -> None:
        for article_id in ids:
            existing = next((c for c in self.cart if c["article_id"] == article_id), None)
            if existing:
                existing["quantity"] += 1
                continue
            a = self.db.get_article(article_id)
            if a is None:
                continue
            self.cart.append({
                "article_id": a.id, "article_no": a.article_no, "name": a.name,
                "description": a.description, "unit": a.unit, "quantity": 1.0,
                "unit_price_cents": a.unit_price_cents,
            })
        self._rebuild_cart()

    def _rebuild_cart(self) -> None:
        t = self.cart_table
        # Alte Zellen-Widgets sofort verstecken (Qt löscht sie erst verzögert).
        for r in range(t.rowCount()):
            for c in (2, 4):
                w = t.cellWidget(r, c)
                if w is not None:
                    w.hide()
        t.clearContents()
        t.setRowCount(len(self.cart))
        for r, item in enumerate(self.cart):
            a = self.db.get_article(item["article_id"])
            if a:
                item.update(article_no=a.article_no, name=a.name, description=a.description, unit=a.unit)
            item["stock"] = a.quantity if a else None

            for c, text in ((0, item["article_no"]), (1, item["name"]), (3, item["unit"])):
                cell = QTableWidgetItem(text)
                if c == 1 and item["description"]:
                    cell.setToolTip(item["description"])
                t.setItem(r, c, cell)

            qty = CellSpinBox()
            qty.setRange(0.01, 1_000_000_000)
            qty.setDecimals(2)
            qty.setValue(item["quantity"])
            qty.valueChanged.connect(lambda v, it=item: self._set_qty(it, v))
            t.setCellWidget(r, 2, qty)

            price = CellSpinBox()
            price.setRange(0, 100_000_000)
            price.setDecimals(2)
            price.setSuffix(" €")
            price.setValue(cents_to_euros(item["unit_price_cents"]))
            price.valueChanged.connect(lambda v, it=item: self._set_price(it, v))
            t.setCellWidget(r, 4, price)

            total = QTableWidgetItem()
            total.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            t.setItem(r, 5, total)
            stock = QTableWidgetItem()
            stock.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            t.setItem(r, 6, stock)
            self._refresh_row(r)
        self._update_totals()
        t.viewport().update()

    def _refresh_row(self, r: int) -> None:
        item = self.cart[r]
        self.cart_table.item(r, 5).setText(
            fmt_money(line_total_cents(item["quantity"], item["unit_price_cents"])))
        stock_cell = self.cart_table.item(r, 6)
        if item.get("stock") is None:
            stock_cell.setText("gelöscht")
            stock_cell.setForeground(QColor(style.DANGER))
        else:
            stock_cell.setText(fmt_qty(item["stock"]))
            short = item["quantity"] > item["stock"]
            stock_cell.setForeground(QColor(style.DANGER if short else style.SUCCESS))
            stock_cell.setToolTip("Nicht genügend Bestand" if short else "Bestand ausreichend")

    def _set_qty(self, item: dict, value: float) -> None:
        item["quantity"] = round(value, 3)
        self._refresh_row(self.cart.index(item))
        self._update_totals()

    def _set_price(self, item: dict, value: float) -> None:
        item["unit_price_cents"] = euros_to_cents(value)
        self._refresh_row(self.cart.index(item))
        self._update_totals()

    def _remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.cart_table.selectionModel().selectedRows()}, reverse=True)
        if not rows and self.cart_table.currentRow() >= 0:
            rows = [self.cart_table.currentRow()]
        for r in rows:
            del self.cart[r]
        self._rebuild_cart()

    def _clear_cart(self) -> None:
        if self.cart and QMessageBox.question(self, "Warenkorb leeren",
                                              "Alle Positionen entfernen?") == QMessageBox.Yes:
            self.cart.clear()
            self._rebuild_cart()

    def _shortages(self) -> list[dict]:
        return [c for c in self.cart if c.get("stock") is not None and c["quantity"] > c["stock"]]

    def _update_totals(self) -> None:
        settings = self.db.get_settings()
        n = len(self.cart)
        shortages = self._shortages()
        self.stock_warning.setText(
            f"⚠ {len(shortages)} Position(en) übersteigen den Lagerbestand" if shortages else "")
        if self.doc_type() == DOC_INVOICE:
            totals = compute_totals(self._doc_items(), settings, self._shipping_cents())
            parts = [f"{n} Position(en) {fmt_money(totals.goods_cents)}"]
            if totals.shipping_cents:
                parts.append(f"Versand {fmt_money(totals.shipping_cents)}")
            if settings.get("small_business"):
                parts.append("ohne USt. (§ 19 UStG)")
            else:
                parts.append(f"USt. {fmt_qty(totals.vat_rate)} % {fmt_money(totals.vat_cents)}")
            self.totals_label.setText(" · ".join(parts))
            self.total_big.setText(f"Gesamt: {fmt_money(totals.gross_cents)}")
        else:
            qty = sum(c["quantity"] for c in self.cart)
            self.totals_label.setText(f"{n} Position(en) · Lieferschein ohne Preise")
            self.total_big.setText(f"Gesamtmenge: {fmt_qty(qty)}")
        has_items = n > 0
        for b in (self.btn_create, self.btn_print, self.btn_preview, self.btn_clear, self.btn_remove):
            b.setEnabled(has_items)

    def _doc_items(self) -> list[DocItem]:
        return [DocItem(c["article_no"], c["name"], c["unit"], c["quantity"], c["unit_price_cents"],
                        c["description"], c["article_id"]) for c in self.cart]

    def _document_data(self, number: str) -> DocumentData:
        return DocumentData(
            doc_type=self.doc_type(),
            number=number,
            doc_date=self.date_edit.date().toPython(),
            delivery_date=self.delivery_date_edit.date().toPython(),
            recipient=self.recipient_edit.toPlainText().strip(),
            customer_no=self.customer_no_edit.text().strip(),
            reference=self.reference_edit.text().strip(),
            notes=self.notes_edit.text().strip(),
            items=self._doc_items(),
            delivery_address=self._delivery_address(),
            shipping_method=self._shipping_method(),
            shipping_cents=self._shipping_cents(),
            tracking_no=self.tracking_edit.text().strip(),
            settings=self.db.get_settings(),
        )

    def _delivery_address(self) -> str:
        if not self.cb_delivery.isChecked():
            return ""
        text = self.delivery_edit.toPlainText().strip()
        # Identische Adresse nicht doppelt ausweisen.
        return "" if text == self.recipient_edit.toPlainText().strip() else text

    # ------------------------------------------------------------ actions
    def _preview(self) -> None:
        data = self._document_data(self.number_edit.text().strip() or "VORSCHAU")
        data.draft = True
        target = Path(tempfile.gettempdir()) / "CNCTD.STOCK" / f"Vorschau_{now_stamp()}.pdf"
        try:
            build_document(data, target)
        except Exception as exc:  # noqa: BLE001 – Fehler dem Nutzer anzeigen
            QMessageBox.critical(self, "Vorschau fehlgeschlagen", f"Die Vorschau konnte nicht erstellt werden:\n{exc}")
            return
        open_path(target)

    def _create(self, print_after: bool) -> None:
        if not self.cart:
            return
        doc_type = self.doc_type()
        label = DOC_LABELS[doc_type]
        recipient = self.recipient_edit.toPlainText().strip()
        settings = self.db.get_settings()

        if not recipient:
            QMessageBox.warning(self, "Empfänger fehlt", "Bitte die Anschrift des Empfängers eintragen.")
            self.recipient_edit.setFocus()
            return
        if not settings.get("sender_name"):
            answer = QMessageBox.question(
                self, "Absender fehlt",
                "In den Einstellungen ist noch kein Absender (Firmenname) hinterlegt.\n\nTrotzdem erstellen?")
            if answer != QMessageBox.Yes:
                return
        if not self._confirm_date(doc_type):
            return
        deduct = self.cb_deduct.isChecked()
        if deduct and self._shortages():
            names = "\n".join(f"• {c['article_no']} {c['name']} (Bestand {fmt_qty(c['stock'])}, "
                              f"benötigt {fmt_qty(c['quantity'])})" for c in self._shortages())
            answer = QMessageBox.question(
                self, "Bestand nicht ausreichend",
                f"Folgende Positionen übersteigen den Lagerbestand:\n\n{names}\n\n"
                "Trotzdem erstellen und abbuchen? Der Bestand wird dadurch negativ.")
            if answer != QMessageBox.Yes:
                return
        answer = QMessageBox.question(
            self, f"{label} erstellen",
            f"{label} {self.db.next_doc_number(doc_type, self.date_edit.date().toPython())} an "
            f"„{recipient.splitlines()[0]}“ jetzt verbindlich erstellen?\n\n"
            "Nach der Erstellung kann der Beleg nicht mehr geändert oder gelöscht werden "
            "(GoBD). Korrekturen erfolgen über eine Stornierung im Dokumentenarchiv.")
        if answer != QMessageBox.Yes:
            return

        deductions: dict[int, float] = {}
        if deduct:
            for c in self.cart:
                if c.get("stock") is not None:
                    deductions[c["article_id"]] = deductions.get(c["article_id"], 0) + c["quantity"]
        data = self._document_data("")
        out_dir = Path(settings.get("pdf_output_dir") or Path.home() / "Documents" / "CNCTD.STOCK")
        try:
            result = gobd.create_document(self.db, data, customer_id=self.current_customer_id,
                                          deductions=deductions or None, output_dir=out_dir)
        except GoBDError as exc:
            QMessageBox.warning(self, "Nicht möglich", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Fehler bei der PDF-Erstellung", str(exc))
            return

        if deduct:
            # Schutz vor doppelter Abbuchung, wenn z. B. direkt danach der Lieferschein erstellt wird.
            self.cb_deduct.setChecked(False)
            self.state.inventoryChanged.emit()
        self.state.documentsChanged.emit()
        self._refresh_number()

        target = result.work_copy or result.archive_file
        msg = f"{label} {result.number} erstellt und archiviert."
        if deduct:
            msg += " Bestand abgebucht."
        if result.work_copy is None:
            msg += " (Keine Arbeitskopie im PDF-Ordner – bitte Speicherort prüfen.)"
        self.window().statusBar().showMessage(msg, 10000)

        if print_after:
            if not print_path(target):
                open_path(target)
                QMessageBox.information(
                    self, "Drucken",
                    "Der Standard-PDF-Viewer unterstützt keinen Direktdruck.\n"
                    "Das Dokument wurde geöffnet – bitte dort mit Strg+P drucken.")
        else:
            open_path(target)

    def _confirm_date(self, doc_type: str) -> bool:
        """Plausibilitätsprüfung des Belegdatums (zeitgerechte Erfassung)."""
        d = self.date_edit.date().toPython()
        today = date.today()
        hints = []
        if d > today:
            hints.append("Das Belegdatum liegt in der Zukunft.")
        elif (today - d).days > 10:
            hints.append(f"Das Belegdatum liegt {(today - d).days} Tage zurück.")
        last = self.db.last_document_date(doc_type)
        if last and d < last:
            hints.append(f"Der letzte Beleg dieser Art ist vom {last:%d.%m.%Y} – die neue Nummer "
                         "hätte ein früheres Datum.")
        if not hints:
            return True
        return QMessageBox.question(
            self, "Belegdatum prüfen", "\n".join(hints) + "\n\nTrotzdem mit diesem Datum erstellen?"
        ) == QMessageBox.Yes

    def _open_archive(self) -> None:
        ArchiveDialog(self.state, self).exec()
