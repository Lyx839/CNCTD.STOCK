"""Tab Buchhaltung – Übersicht, Journal, offene Posten, Bankabgleich, Kassenbuch und EÜR."""

from datetime import date
from pathlib import Path

from PySide6.QtCore import QDate, Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import accounting as acc
from .. import gobd
from ..database import STATUS_CANCELLED, GoBDError, now_stamp
from ..formatting import cents_to_euros, euros_to_cents, fmt_money
from . import style
from .common import AppState, card, open_path
from .gobd_ui import _filter_rows, _make_table, _search_field


# ------------------------------------------------------------------------ helpers
def _money_item(cents: int, colored: bool = True, bold: bool = False) -> QTableWidgetItem:
    item = QTableWidgetItem(fmt_money(cents) if cents is not None else "")
    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    item.setData(Qt.UserRole + 1, cents)
    if colored and cents:
        item.setForeground(QColor(style.SUCCESS if cents > 0 else style.DANGER))
    if bold:
        f = item.font()
        f.setBold(True)
        item.setFont(f)
    return item


def _date_edit(value: date | None = None) -> QDateEdit:
    e = QDateEdit(QDate(value) if value else QDate.currentDate())
    e.setCalendarPopup(True)
    e.setDisplayFormat("dd.MM.yyyy")
    return e


def _amount_spin(value_cents: int = 0, allow_negative: bool = False) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setRange(-10_000_000 if allow_negative else 0, 10_000_000)
    s.setDecimals(2)
    s.setSuffix(" €")
    s.setGroupSeparatorShown(True)
    s.setValue(cents_to_euros(abs(value_cents) if not allow_negative else value_cents))
    return s


def _de(d: str | None) -> str:
    return date.fromisoformat(d[:10]).strftime("%d.%m.%Y") if d else ""


def _buttons(ok_text: str) -> QDialogButtonBox:
    box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    box.button(QDialogButtonBox.Ok).setText(ok_text)
    box.button(QDialogButtonBox.Ok).setObjectName("Primary")
    box.button(QDialogButtonBox.Cancel).setText("Abbrechen")
    return box


def _year_combo(db) -> QComboBox:
    combo = QComboBox()
    for y in acc.booking_years(db):
        combo.addItem(str(y), y)
    return combo


def _refill_years(combo: QComboBox, db) -> None:
    current = combo.currentData()
    combo.blockSignals(True)
    combo.clear()
    for y in acc.booking_years(db):
        combo.addItem(str(y), y)
    idx = combo.findData(current)
    combo.setCurrentIndex(max(idx, 0))
    combo.blockSignals(False)


# ------------------------------------------------------------------------ dialogs
class BookingDialog(QDialog):
    """Einnahme oder Ausgabe erfassen (optional aus einem Bankumsatz)."""

    def __init__(self, db, kind: str, parent=None, *, bank_tx=None, account: str | None = None,
                 category: str | None = None):
        super().__init__(parent)
        self.db = db
        self.kind = kind
        self.bank_tx = bank_tx
        self.booking_id: int | None = None
        self.small = bool(db.get_settings().get("small_business"))
        expense = kind == acc.EXPENSE
        self.setWindowTitle("Ausgabe erfassen" if expense else "Einnahme erfassen")
        self.setMinimumWidth(560)

        self.date_edit = _date_edit()
        self.receipt_date = _date_edit()
        self.account = QComboBox()
        for key, label in acc.ACCOUNTS.items():
            self.account.addItem(label, key)
        self.category = QComboBox()
        for c in acc.categories_for(kind, self.small):
            self.category.addItem(c.label, c.key)
        self.amount = _amount_spin()
        self.vat = QComboBox()
        for rate in (19.0, 7.0, 0.0):
            self.vat.addItem(f"{rate:g} %", rate)
        self.split_label = QLabel()
        self.split_label.setObjectName("Hint")
        self.counterparty = QLineEdit()
        self.counterparty.setPlaceholderText("Lieferant" if expense else "Kunde / Zahler")
        self.text = QLineEdit()
        self.text.setPlaceholderText("z. B. Filament PLA 2 kg" if expense else "z. B. Barverkauf Markt")
        self.receipt = QLineEdit()
        self.receipt.setReadOnly(True)
        self.receipt.setPlaceholderText("PDF oder Foto des Belegs")
        btn_receipt = QPushButton("Beleg wählen …")
        btn_clear = QPushButton("✕")
        btn_clear.setFixedWidth(36)
        receipt_row = QHBoxLayout()
        receipt_row.addWidget(self.receipt, 1)
        receipt_row.addWidget(btn_receipt)
        receipt_row.addWidget(btn_clear)

        form = QFormLayout()
        form.setSpacing(9)
        form.addRow("Zahlungsdatum *", self.date_edit)
        if expense:
            form.addRow("Belegdatum", self.receipt_date)
        form.addRow("Konto *", self.account)
        form.addRow("Kategorie *", self.category)
        form.addRow("Betrag (brutto) *", self.amount)
        if not self.small:
            form.addRow("Umsatzsteuer", self.vat)
        if not self.small:
            form.addRow("", self.split_label)
        form.addRow("Lieferant" if expense else "Von", self.counterparty)
        form.addRow("Beschreibung", self.text)
        form.addRow("Beleg" + (" *" if expense else ""), receipt_row)
        if self.small and expense:
            note = QLabel("Als Kleinunternehmer ist der Bruttobetrag die Betriebsausgabe (kein Vorsteuerabzug).")
            note.setObjectName("Hint")
            note.setWordWrap(True)
            form.addRow("", note)

        buttons = _buttons("Verbindlich buchen")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.addLayout(form)
        layout.addWidget(buttons)

        btn_receipt.clicked.connect(self._choose_receipt)
        btn_clear.clicked.connect(self.receipt.clear)
        self.amount.valueChanged.connect(self._update_split)
        self.vat.currentIndexChanged.connect(self._update_split)
        self.category.currentIndexChanged.connect(self._category_changed)

        if account:
            self.account.setCurrentIndex(self.account.findData(account))
        if category:
            idx = self.category.findData(category)
            if idx >= 0:
                self.category.setCurrentIndex(idx)
        if bank_tx is not None:
            self.date_edit.setDate(QDate(date.fromisoformat(bank_tx["tx_date"])))
            self.receipt_date.setDate(self.date_edit.date())
            self.account.setCurrentIndex(self.account.findData(bank_tx["account"]))
            self.amount.setValue(cents_to_euros(abs(bank_tx["amount_cents"])))
            self.counterparty.setText(bank_tx["counterparty"])
            self.text.setText(bank_tx["purpose"][:200])
            for w in (self.date_edit, self.account, self.amount):
                w.setEnabled(False)
        self._category_changed()
        self._update_split()

    def _category_changed(self) -> None:
        cat = self.category.currentData()
        private = cat in ("privatentnahme", "privateinlage")
        self.vat.setEnabled(not private and not self.small)
        if private:
            self.vat.setCurrentIndex(self.vat.findData(0.0))
        self._update_split()

    def _rate(self) -> float:
        return 0.0 if self.small else float(self.vat.currentData() or 0)

    def _update_split(self) -> None:
        net, vat = acc.split_gross(euros_to_cents(self.amount.value()), self._rate())
        self.split_label.setText(f"Netto {fmt_money(net)} · USt. {fmt_money(vat)}" if vat else "")

    def _choose_receipt(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Beleg auswählen", str(Path.home()),
                                              "Belege (*.pdf *.jpg *.jpeg *.png *.tif *.tiff *.xml);;Alle Dateien (*)")
        if path:
            self.receipt.setText(path)

    def _save(self) -> None:
        gross = euros_to_cents(self.amount.value())
        if gross <= 0:
            QMessageBox.warning(self, "Betrag fehlt", "Bitte einen Betrag größer als 0 eingeben.")
            return
        if not (self.counterparty.text().strip() or self.text.text().strip()):
            QMessageBox.warning(self, "Angaben fehlen", "Bitte Lieferant/Zahler oder eine Beschreibung angeben.")
            return
        cat = self.category.currentData()
        pay_date = self.date_edit.date().toPython()
        expense = self.kind == acc.EXPENSE
        private = cat in ("privatentnahme", "privateinlage")
        if expense and not private and not self.receipt.text():
            if QMessageBox.question(
                    self, "Kein Beleg",
                    "GoBD-Grundsatz: keine Buchung ohne Beleg.\n\nOhne Beleg nur in Ausnahmefällen buchen und dann "
                    "einen Eigenbeleg (Datum, Betrag, Zweck, Unterschrift) erstellen und später anhängen.\n\n"
                    "Trotzdem ohne Beleg buchen?") != QMessageBox.Yes:
                return
        net, _ = acc.split_gross(gross, self._rate())
        if expense and cat in ("gwg", "buero", "sonstige_ausgaben", "software") and net > acc.GWG_LIMIT_CENTS:
            if QMessageBox.question(
                    self, "Anlagegut?",
                    f"Netto {fmt_money(net)} liegt über 800 €. Handelt es sich um ein Wirtschaftsgut, das länger "
                    "genutzt wird (z. B. 3D-Drucker, Computer), ist es über die Nutzungsdauer abzuschreiben (AfA) "
                    "und nicht sofort voll absetzbar.\n\nTrotzdem so buchen?") != QMessageBox.Yes:
                return
        if self.account.currentData() == "KASSE" and pay_date != date.today():
            if QMessageBox.question(
                    self, "Kassenbuchung",
                    "Kassenbuchungen sind täglich zu erfassen. Das gewählte Datum ist nicht heute.\n\n"
                    "Trotzdem mit diesem Datum buchen?") != QMessageBox.Yes:
                return
        booking = acc.BookingInput(
            booking_date=pay_date, account=self.account.currentData(), category=cat,
            gross_cents=-gross if expense else gross, vat_rate=0.0 if private else self._rate(),
            counterparty=self.counterparty.text(), text=self.text.text(),
            receipt_date=self.receipt_date.date().toPython() if expense else None,
            receipt_file=Path(self.receipt.text()) if self.receipt.text() else None,
            bank_tx_id=self.bank_tx["id"] if self.bank_tx is not None else None)
        try:
            self.booking_id = acc.create_booking(self.db, booking)
        except GoBDError as exc:
            QMessageBox.warning(self, "Buchung nicht möglich", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Fehler", str(exc))
            return
        self.accept()


class TransitDialog(QDialog):
    """Geldtransit zwischen Bank, PayPal und Kasse (auch Privat für Bankumsätze)."""

    def __init__(self, db, parent=None, *, bank_tx=None):
        super().__init__(parent)
        self.db = db
        self.bank_tx = bank_tx
        self.setWindowTitle("Privat / Geldtransit" if bank_tx is not None else "Geldtransit")
        self.setMinimumWidth(480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)

        self.rb_transit = QRadioButton("Geldtransit (z. B. Abhebung von der Bank in die Kasse)")
        self.rb_private = QRadioButton()
        group = QButtonGroup(self)
        group.addButton(self.rb_transit)
        group.addButton(self.rb_private)
        self.rb_transit.setChecked(True)

        self.date_edit = _date_edit()
        self.from_acc = QComboBox()
        self.to_acc = QComboBox()
        for key in ("BANK", "KASSE", "PAYPAL"):
            self.from_acc.addItem(acc.ACCOUNTS[key], key)
            self.to_acc.addItem(acc.ACCOUNTS[key], key)
        self.to_acc.setCurrentIndex(1)
        self.amount = _amount_spin()
        self.text = QLineEdit()

        if bank_tx is not None:
            incoming = bank_tx["amount_cents"] > 0
            self.rb_private.setText("Privateinlage (Geld von privat)" if incoming
                                    else "Privatentnahme (Geld für private Zwecke)")
            layout.addWidget(self.rb_transit)
            layout.addWidget(self.rb_private)
            self.date_edit.setDate(QDate(date.fromisoformat(bank_tx["tx_date"])))
            self.amount.setValue(cents_to_euros(abs(bank_tx["amount_cents"])))
            self.text.setText(bank_tx["purpose"][:200])
            own = bank_tx["account"]
            combo_fixed, combo_other = (self.to_acc, self.from_acc) if incoming else (self.from_acc, self.to_acc)
            combo_fixed.setCurrentIndex(combo_fixed.findData(own))
            combo_other.setCurrentIndex(combo_other.findData("KASSE"))
            for w in (self.date_edit, self.amount, combo_fixed):
                w.setEnabled(False)
            # Bei Privatentnahme/-einlage gibt es kein Gegenkonto.
            self.rb_private.toggled.connect(lambda on: combo_other.setEnabled(not on))

        form = QFormLayout()
        form.addRow("Datum", self.date_edit)
        form.addRow("Von", self.from_acc)
        form.addRow("Nach", self.to_acc)
        form.addRow("Betrag", self.amount)
        form.addRow("Text", self.text)
        layout.addLayout(form)
        buttons = _buttons("Verbindlich buchen")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self) -> None:
        amount = euros_to_cents(self.amount.value())
        d = self.date_edit.date().toPython()
        tx_id = self.bank_tx["id"] if self.bank_tx is not None else None
        try:
            if self.bank_tx is not None and self.rb_private.isChecked():
                incoming = self.bank_tx["amount_cents"] > 0
                acc.create_booking(self.db, acc.BookingInput(
                    d, self.bank_tx["account"], "privateinlage" if incoming else "privatentnahme",
                    self.bank_tx["amount_cents"], counterparty=self.bank_tx["counterparty"],
                    text=self.text.text() or ("Privateinlage" if incoming else "Privatentnahme"), bank_tx_id=tx_id))
            else:
                acc.create_transit(self.db, d, self.from_acc.currentData(), self.to_acc.currentData(), amount,
                                   self.text.text(), bank_tx_id=tx_id)
        except GoBDError as exc:
            QMessageBox.warning(self, "Buchung nicht möglich", str(exc))
            return
        self.accept()


class PaymentDialog(QDialog):
    """Zahlungseingang (bzw. Erstattung) zu einer Rechnung."""

    def __init__(self, db, item: acc.OpenItem, parent=None):
        super().__init__(parent)
        self.db = db
        self.item = item
        refund = item.open_cents < 0
        self.setWindowTitle(f"{'Erstattung' if refund else 'Zahlung'} zu {item.number}")
        self.setMinimumWidth(440)
        self.date_edit = _date_edit()
        self.account = QComboBox()
        for key in ("BANK", "PAYPAL", "KASSE"):
            self.account.addItem(acc.ACCOUNTS[key], key)
        self.amount = _amount_spin(abs(item.open_cents))
        info = QLabel(f"{item.customer} · Rechnungsbetrag {fmt_money(item.total_cents)} · bereits bezahlt "
                      f"{fmt_money(item.paid_cents)} · offen <b>{fmt_money(item.open_cents)}</b>")
        info.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Zahlungsdatum", self.date_edit)
        form.addRow("Konto", self.account)
        form.addRow("Erstattet" if refund else "Betrag", self.amount)
        buttons = _buttons("Verbindlich buchen")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.addWidget(info)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _save(self) -> None:
        cents = euros_to_cents(self.amount.value())
        if cents <= 0:
            return
        if self.item.open_cents < 0:
            cents = -cents
        try:
            acc.record_invoice_payment(self.db, self.item.doc_id, self.date_edit.date().toPython(),
                                       self.account.currentData(), cents)
        except GoBDError as exc:
            QMessageBox.warning(self, "Buchung nicht möglich", str(exc))
            return
        self.accept()


class AssignInvoiceDialog(QDialog):
    """Bankumsatz einer offenen Rechnung zuordnen."""

    def __init__(self, db, tx, parent=None):
        super().__init__(parent)
        self.db = db
        self.tx = tx
        self.setWindowTitle("Rechnung zuordnen")
        self.resize(760, 420)
        info = QLabel(f"<b>{fmt_money(tx['amount_cents'])}</b> am {_de(tx['tx_date'])} von "
                      f"<b>{tx['counterparty'] or '–'}</b><br>{tx['purpose']}")
        info.setWordWrap(True)
        self.table = _make_table(["Treffer", "Rechnung", "Datum", "Kunde", "Offen"])
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.suggestions = acc.suggest_invoices(db, tx)
        self.table.setRowCount(len(self.suggestions))
        for r, (score, item) in enumerate(self.suggestions):
            quality = "sehr gut" if score >= 100 else "gut" if score >= 50 else "möglich" if score else "–"
            for c, v in enumerate([quality, item.number, item.doc_date.strftime("%d.%m.%Y"), item.customer]):
                self.table.setItem(r, c, QTableWidgetItem(v))
            self.table.setItem(r, 4, _money_item(item.open_cents, colored=False))
        if self.suggestions:
            self.table.selectRow(0)
        buttons = _buttons("Zuordnen und buchen")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.addWidget(info)
        if not self.suggestions:
            layout.addWidget(QLabel("Keine offene Rechnung gefunden."))
        layout.addWidget(self.table, 1)
        layout.addWidget(buttons)

    def _save(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        item = self.suggestions[rows[0].row()][1]
        if item.open_cents != self.tx["amount_cents"]:
            kind = "Teilzahlung" if abs(self.tx["amount_cents"]) < abs(item.open_cents) else "Überzahlung"
            if QMessageBox.question(
                    self, kind, f"Der Betrag {fmt_money(self.tx['amount_cents'])} weicht vom offenen Betrag "
                                f"{fmt_money(item.open_cents)} ab ({kind}).\n\nTrotzdem zuordnen?") != QMessageBox.Yes:
                return
        try:
            acc.record_invoice_payment(self.db, item.doc_id, date.fromisoformat(self.tx["tx_date"]),
                                       self.tx["account"], self.tx["amount_cents"], self.tx["id"])
        except GoBDError as exc:
            QMessageBox.warning(self, "Buchung nicht möglich", str(exc))
            return
        self.accept()


class CsvMappingDialog(QDialog):
    """Spaltenzuordnung, falls das CSV-Format nicht automatisch erkannt wurde."""

    FIELDS = [("date", "Datum *"), ("amount", "Betrag *"), ("debit", "Soll (Ausgang)"), ("credit", "Haben (Eingang)"),
              ("counterparty", "Name / Gegenpartei"), ("purpose", "Verwendungszweck"), ("iban", "IBAN")]

    def __init__(self, header: list[str], rows: list[list[str]], mapping: dict[str, int], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Spalten zuordnen")
        self.resize(900, 520)
        preview = QTableWidget(min(len(rows), 8), len(header))
        preview.setHorizontalHeaderLabels(header)
        for r, row in enumerate(rows[:8]):
            for c, v in enumerate(row[:len(header)]):
                preview.setItem(r, c, QTableWidgetItem(v))
        preview.setEditTriggers(QTableWidget.NoEditTriggers)
        self.combos: dict[str, QComboBox] = {}
        grid = QGridLayout()
        for i, (key, label) in enumerate(self.FIELDS):
            combo = QComboBox()
            combo.addItem("–", None)
            for idx, h in enumerate(header):
                combo.addItem(h, idx)
            if key in mapping:
                combo.setCurrentIndex(combo.findData(mapping[key]))
            self.combos[key] = combo
            grid.addWidget(QLabel(label), i // 2, (i % 2) * 2)
            grid.addWidget(combo, i // 2, (i % 2) * 2 + 1)
        hint = QLabel("Betrag: eine Spalte mit Vorzeichen – oder getrennte Spalten für Soll und Haben.")
        hint.setObjectName("Hint")
        buttons = _buttons("Importieren")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Das Format wurde nicht vollständig erkannt. Bitte die Spalten zuordnen:"))
        layout.addWidget(preview, 1)
        layout.addLayout(grid)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def mapping(self) -> dict[str, int]:
        return {k: c.currentData() for k, c in self.combos.items() if c.currentData() is not None}


# -------------------------------------------------------------------------- pages
class _Page(QWidget):
    def __init__(self, tab: "AccountingTab"):
        super().__init__()
        self.setObjectName("Page")
        self.tab = tab
        self.db = tab.db

    def reload(self) -> None:  # pragma: no cover - von Unterklassen überschrieben
        pass


class _Tile(QWidget):
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


class OverviewPage(_Page):
    def __init__(self, tab):
        super().__init__(tab)
        self.year = _year_combo(self.db)
        self.tiles = {k: _Tile(label) for k, label in [
            ("income", "Betriebseinnahmen"), ("expenses", "Betriebsausgaben"), ("profit", "Gewinn / Verlust"),
            ("open", "Offene Rechnungen"), ("cash", "Kassenbestand"), ("bank", "Bankkonto (laut Buchungen)")]}
        grid = QGridLayout()
        grid.setSpacing(14)
        for i, t in enumerate(self.tiles.values()):
            grid.addWidget(t, i // 3, i % 3)

        actions = QHBoxLayout()
        for text, slot, primary in [("＋ Ausgabe erfassen", tab.add_expense, True),
                                    ("＋ Einnahme erfassen", tab.add_income, False),
                                    ("Barverkauf (Kasse)", tab.add_cash_sale, False),
                                    ("Kontoauszug importieren", tab.import_bank, False),
                                    ("Geldtransit", tab.add_transit, False)]:
            b = QPushButton(text)
            if primary:
                b.setObjectName("Primary")
            b.clicked.connect(slot)
            actions.addWidget(b)
        actions.addStretch(1)

        todo_card, todo_layout = card("Zu erledigen")
        self.todo = QLabel()
        self.todo.setWordWrap(True)
        self.todo.setTextFormat(Qt.RichText)
        todo_layout.addWidget(self.todo)

        head = QHBoxLayout()
        head.addWidget(QLabel("Geschäftsjahr"))
        head.addWidget(self.year)
        head.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 14, 0, 0)
        layout.setSpacing(14)
        layout.addLayout(head)
        layout.addLayout(grid)
        layout.addLayout(actions)
        layout.addWidget(todo_card)
        layout.addStretch(1)
        self.year.currentIndexChanged.connect(self.reload)

    def reload(self) -> None:
        _refill_years(self.year, self.db)
        year = self.year.currentData() or date.today().year
        rep = acc.euer(self.db, year)
        open_items = acc.invoice_items(self.db)
        open_sum = sum(i.open_cents for i in open_items if i.open_cents > 0)
        values = {"income": rep.total_income, "expenses": rep.total_expenses, "profit": rep.profit,
                  "open": open_sum, "cash": acc.account_balance(self.db, "KASSE"),
                  "bank": acc.account_balance(self.db, "BANK")}
        for k, v in values.items():
            self.tiles[k].value.setText(fmt_money(v))
        self.tiles["profit"].value.setStyleSheet(f"color: {style.SUCCESS if rep.profit >= 0 else style.DANGER};")

        todo = []
        unbooked = len(acc.list_bank_transactions(self.db, only_open=True))
        if unbooked:
            todo.append(f"<b>{unbooked}</b> importierte Bankumsätze sind noch nicht gebucht (Reiter „Bankabgleich“).")
        overdue_days = int(self.db.get_settings().get("payment_days") or 14)
        overdue = [i for i in open_items if i.open_cents > 0 and (date.today() - i.doc_date).days > overdue_days]
        if overdue:
            todo.append(f"<b>{len(overdue)}</b> Rechnung(en) sind überfällig (Reiter „Offene Posten“).")
        refunds = [i for i in open_items if i.open_cents < 0]
        if refunds:
            todo.append(f"<b>{len(refunds)}</b> stornierte bzw. überzahlte Rechnung(en) mit offener Erstattung.")
        if not self.db.get_settings().get("tax_number") and not self.db.get_settings().get("vat_id"):
            todo.append("In den Einstellungen ist keine <b>Steuernummer</b> hinterlegt – Pflichtangabe auf Rechnungen.")
        todo += [n for n in rep.notes if "Bankums" not in n]   # Bankumsätze stehen schon oben
        self.todo.setText("<br>".join(f"• {t}" for t in todo) if todo else "Alles erledigt. 👍")


class JournalPage(_Page):
    COLS = ["Nr.", "Zahlungsdatum", "Konto", "Kategorie", "Gegenpartei", "Text", "Betrag", "USt.", "Beleg", "Status"]

    def __init__(self, tab):
        super().__init__(tab)
        self.year = _year_combo(self.db)
        self.account = QComboBox()
        self.account.addItem("Alle Konten", None)
        for key, label in acc.ACCOUNTS.items():
            self.account.addItem(label, key)
        self.search = _search_field("🔍  Buchungen durchsuchen …")
        self.table = _make_table(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.setSortingEnabled(False)

        head = QHBoxLayout()
        head.addWidget(QLabel("Jahr"))
        head.addWidget(self.year)
        head.addWidget(QLabel("Konto"))
        head.addWidget(self.account)
        head.addWidget(self.search, 1)

        bar = QHBoxLayout()
        for text, slot, name in [("＋ Ausgabe", tab.add_expense, "Primary"), ("＋ Einnahme", tab.add_income, ""),
                                 ("Geldtransit", tab.add_transit, ""), ("Beleg öffnen", self._open_receipt, ""),
                                 ("Stornieren …", self._cancel, "Danger")]:
            b = QPushButton(text)
            if name:
                b.setObjectName(name)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        self.sum_label = QLabel()
        self.sum_label.setObjectName("Muted")
        bar.addWidget(self.sum_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 14, 0, 0)
        layout.addLayout(head)
        layout.addWidget(self.table, 1)
        layout.addLayout(bar)
        self.year.currentIndexChanged.connect(self.reload)
        self.account.currentIndexChanged.connect(self.reload)
        self.search.textChanged.connect(lambda t: _filter_rows(self.table, t))
        self.table.doubleClicked.connect(lambda _: self._open_receipt())

    def reload(self) -> None:
        _refill_years(self.year, self.db)
        rows = acc.list_bookings(self.db, self.year.currentData(), self.account.currentData())
        self.table.setRowCount(len(rows))
        total = 0
        for r, b in enumerate(rows):
            total += b["gross_cents"]
            status = ("storniert" if b["status"] == STATUS_CANCELLED else
                      "Storno" if b["cancels_id"] else "gültig")
            values = [b["booking_no"], _de(b["booking_date"]), acc.ACCOUNTS.get(b["account"], b["account"]),
                      acc.category_label(b["category"]), b["counterparty"], b["text"]]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c == 0:
                    item.setData(Qt.UserRole, b["id"])
                self.table.setItem(r, c, item)
            self.table.setItem(r, 6, _money_item(b["gross_cents"]))
            self.table.setItem(r, 7, _money_item(b["vat_cents"], colored=False) if b["vat_cents"] else
                               QTableWidgetItem(""))
            receipt = QTableWidgetItem("📎" if b["receipt_path"] else "")
            receipt.setToolTip(b["receipt_original_name"])
            receipt.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(r, 8, receipt)
            st = QTableWidgetItem(status)
            if status != "gültig":
                st.setForeground(QColor(style.DANGER if status == "storniert" else "#B45309"))
                st.setToolTip(b["cancel_reason"] or b["text"])
            self.table.setItem(r, 9, st)
        self.sum_label.setText(f"{len(rows)} Buchung(en) · Saldo {fmt_money(total)}")
        _filter_rows(self.table, self.search.text())

    def _current(self):
        rows = self.table.selectionModel().selectedRows()
        return acc.get_booking(self.db, self.table.item(rows[0].row(), 0).data(Qt.UserRole)) if rows else None

    def _open_receipt(self) -> None:
        b = self._current()
        if b and b["receipt_path"]:
            open_path(gobd.ARCHIVE_DIR / b["receipt_path"])
        elif b:
            QMessageBox.information(self, "Kein Beleg", f"Zur Buchung {b['booking_no']} ist kein Beleg hinterlegt.")

    def _cancel(self) -> None:
        b = self._current()
        if not b:
            return
        if b["status"] == STATUS_CANCELLED or b["cancels_id"]:
            QMessageBox.information(self, "Nicht möglich", "Diese Buchung ist bereits storniert bzw. selbst ein Storno.")
            return
        reason, ok = QInputDialog.getText(
            self, "Buchung stornieren",
            f"{b['booking_no']} ({fmt_money(b['gross_cents'])}) wird durch eine Gegenbuchung storniert.\n"
            "Grund der Stornierung:")
        if not ok or not reason.strip():
            return
        try:
            acc.cancel_booking(self.db, b["id"], reason)
        except GoBDError as exc:
            QMessageBox.warning(self, "Stornierung nicht möglich", str(exc))
            return
        self.tab.changed()


class OpenItemsPage(_Page):
    COLS = ["Rechnung", "Datum", "Kunde", "Rechnungsbetrag", "Bezahlt", "Offen", "Alter"]

    def __init__(self, tab):
        super().__init__(tab)
        self.cb_all = QCheckBox("Auch bezahlte Rechnungen anzeigen")
        self.table = _make_table(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        btn_pay = QPushButton("Zahlung erfassen …")
        btn_pay.setObjectName("Primary")
        btn_open = QPushButton("Rechnung öffnen")
        self.sum_label = QLabel()
        self.sum_label.setObjectName("Muted")
        bar = QHBoxLayout()
        bar.addWidget(btn_pay)
        bar.addWidget(btn_open)
        bar.addStretch(1)
        bar.addWidget(self.sum_label)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 14, 0, 0)
        layout.addWidget(self.cb_all)
        layout.addWidget(self.table, 1)
        layout.addLayout(bar)
        self.cb_all.toggled.connect(self.reload)
        btn_pay.clicked.connect(self._pay)
        btn_open.clicked.connect(self._open)
        self.table.doubleClicked.connect(lambda _: self._pay())
        self.items: list[acc.OpenItem] = []

    def reload(self) -> None:
        self.items = acc.invoice_items(self.db, only_open=not self.cb_all.isChecked())
        days_due = int(self.db.get_settings().get("payment_days") or 14)
        self.table.setRowCount(len(self.items))
        for r, it in enumerate(self.items):
            age = (date.today() - it.doc_date).days
            for c, v in enumerate([it.number, it.doc_date.strftime("%d.%m.%Y"), it.customer]):
                self.table.setItem(r, c, QTableWidgetItem(v))
            self.table.setItem(r, 3, _money_item(it.total_cents if it.status != STATUS_CANCELLED else 0, False))
            self.table.setItem(r, 4, _money_item(it.paid_cents, False))
            open_item = _money_item(it.open_cents, colored=False, bold=True)
            if it.open_cents < 0:
                open_item.setToolTip("Erstattung an den Kunden offen (Rechnung storniert oder überzahlt)")
                open_item.setForeground(QColor("#B45309"))
            self.table.setItem(r, 5, open_item)
            age_item = QTableWidgetItem(f"{age} Tage" if it.open_cents > 0 else "")
            if it.open_cents > 0 and age > days_due:
                age_item.setForeground(QColor(style.DANGER))
                age_item.setText(f"{age} Tage – überfällig")
            self.table.setItem(r, 6, age_item)
        total = sum(i.open_cents for i in self.items if i.open_cents > 0)
        self.sum_label.setText(f"Offen gesamt: {fmt_money(total)}")

    def _current(self) -> acc.OpenItem | None:
        rows = self.table.selectionModel().selectedRows()
        return self.items[rows[0].row()] if rows else None

    def _pay(self) -> None:
        item = self._current()
        if item is None:
            return
        if item.open_cents == 0:
            QMessageBox.information(self, "Bezahlt", f"{item.number} ist vollständig bezahlt.")
            return
        if PaymentDialog(self.db, item, self).exec():
            self.tab.changed()

    def _open(self) -> None:
        item = self._current()
        if item:
            f = gobd.archive_file(self.db.get_document(item.doc_id))
            if f and f.is_file():
                open_path(f)


class BankPage(_Page):
    COLS = ["Datum", "Konto", "Betrag", "Gegenpartei", "Verwendungszweck", "Gebucht als"]

    def __init__(self, tab):
        super().__init__(tab)
        self.import_account = QComboBox()
        for key in acc.IMPORT_ACCOUNTS:
            self.import_account.addItem(acc.ACCOUNTS[key], key)
        btn_import = QPushButton("Kontoauszug importieren …")
        btn_import.setObjectName("Primary")
        self.cb_open = QCheckBox("Nur ungebuchte")
        self.cb_open.setChecked(True)
        self.search = _search_field("🔍  Umsätze durchsuchen …")
        head = QHBoxLayout()
        head.addWidget(QLabel("Konto"))
        head.addWidget(self.import_account)
        head.addWidget(btn_import)
        head.addSpacing(16)
        head.addWidget(self.cb_open)
        head.addWidget(self.search, 1)

        self.table = _make_table(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.setWordWrap(False)

        bar = QHBoxLayout()
        self.btn_invoice = QPushButton("Rechnung zuordnen …")
        self.btn_expense = QPushButton("Als Ausgabe buchen …")
        self.btn_income = QPushButton("Als Einnahme buchen …")
        self.btn_other = QPushButton("Privat / Geldtransit …")
        for b in (self.btn_invoice, self.btn_expense, self.btn_income, self.btn_other):
            bar.addWidget(b)
        bar.addStretch(1)
        hint = QLabel("Unterstützt: CSV-Export des Online-Bankings (Sparkasse, Volksbank, DKB, ING, Commerzbank …), "
                      "CAMT.053/.052 (XML/ZIP) und PayPal-CSV. Die Originaldatei wird archiviert, doppelte Umsätze "
                      "werden erkannt.")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 14, 0, 0)
        layout.addLayout(head)
        layout.addWidget(self.table, 1)
        layout.addLayout(bar)
        layout.addWidget(hint)

        btn_import.clicked.connect(lambda: tab.import_bank(self.import_account.currentData()))
        self.cb_open.toggled.connect(self.reload)
        self.search.textChanged.connect(lambda t: _filter_rows(self.table, t))
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.btn_invoice.clicked.connect(self._assign)
        self.btn_expense.clicked.connect(lambda: self._book(acc.EXPENSE))
        self.btn_income.clicked.connect(lambda: self._book(acc.INCOME))
        self.btn_other.clicked.connect(self._other)

    def reload(self) -> None:
        rows = acc.list_bank_transactions(self.db, only_open=self.cb_open.isChecked())
        numbers = {r["id"]: r["booking_no"] for r in self.db.conn.execute("SELECT id, booking_no FROM bookings")}
        self.table.setRowCount(len(rows))
        for r, t in enumerate(rows):
            date_item = QTableWidgetItem(_de(t["tx_date"]))
            date_item.setData(Qt.UserRole, t["id"])
            self.table.setItem(r, 0, date_item)
            self.table.setItem(r, 1, QTableWidgetItem(acc.ACCOUNTS.get(t["account"], t["account"])))
            self.table.setItem(r, 2, _money_item(t["amount_cents"]))
            self.table.setItem(r, 3, QTableWidgetItem(t["counterparty"]))
            purpose = QTableWidgetItem(t["purpose"])
            purpose.setToolTip(t["purpose"])
            self.table.setItem(r, 4, purpose)
            booked = QTableWidgetItem(numbers.get(t["booking_id"], "offen"))
            booked.setForeground(QColor(style.SUCCESS if t["booking_id"] else "#B45309"))
            self.table.setItem(r, 5, booked)
        _filter_rows(self.table, self.search.text())
        self._update_buttons()

    def _current(self):
        rows = self.table.selectionModel().selectedRows()
        return acc.get_bank_tx(self.db, self.table.item(rows[0].row(), 0).data(Qt.UserRole)) if rows else None

    def _update_buttons(self) -> None:
        t = self._current()
        free = t is not None and t["booking_id"] is None
        self.btn_invoice.setEnabled(free)
        self.btn_expense.setEnabled(free and t["amount_cents"] < 0)
        self.btn_income.setEnabled(free and t["amount_cents"] > 0)
        self.btn_other.setEnabled(free)

    def _assign(self) -> None:
        t = self._current()
        if t is not None and AssignInvoiceDialog(self.db, t, self).exec():
            self.tab.changed()

    def _book(self, kind: str) -> None:
        t = self._current()
        if t is not None and BookingDialog(self.db, kind, self, bank_tx=t).exec():
            self.tab.changed()

    def _other(self) -> None:
        t = self._current()
        if t is not None and TransitDialog(self.db, self, bank_tx=t).exec():
            self.tab.changed()


class CashPage(_Page):
    COLS = ["Datum", "Nr.", "Text", "Einnahme", "Ausgabe", "Bestand"]

    def __init__(self, tab):
        super().__init__(tab)
        self.year = _year_combo(self.db)
        self.balance = QLabel()
        self.balance.setObjectName("TotalBig")
        head = QHBoxLayout()
        head.addWidget(QLabel("Jahr"))
        head.addWidget(self.year)
        head.addStretch(1)
        head.addWidget(self.balance)
        self.table = _make_table(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        bar = QHBoxLayout()
        self.btn_opening = QPushButton("Anfangsbestand erfassen …")
        for text, slot, name in [("＋ Kasseneinnahme (Barverkauf)", tab.add_cash_sale, "Primary"),
                                 ("＋ Kassenausgabe", tab.add_cash_expense, ""),
                                 ("Geldtransit", tab.add_transit, "")]:
            b = QPushButton(text)
            if name:
                b.setObjectName(name)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addWidget(self.btn_opening)
        bar.addStretch(1)
        hint = QLabel("Kassenbuch: Bareinnahmen und -ausgaben täglich erfassen. Ein negativer Kassenbestand ist nicht "
                      "möglich. Korrekturen nur per Storno im Journal.")
        hint.setObjectName("Hint")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 14, 0, 0)
        layout.addLayout(head)
        layout.addWidget(self.table, 1)
        layout.addLayout(bar)
        layout.addWidget(hint)
        self.year.currentIndexChanged.connect(self.reload)
        self.btn_opening.clicked.connect(self._opening)

    def reload(self) -> None:
        _refill_years(self.year, self.db)
        year = self.year.currentData() or date.today().year
        opening, rows = acc.cash_book(self.db, year)
        self.table.setRowCount(len(rows) + 1)
        first = QTableWidgetItem(f"01.01.{year}")
        self.table.setItem(0, 0, first)
        self.table.setItem(0, 1, QTableWidgetItem(""))
        self.table.setItem(0, 2, QTableWidgetItem("Übertrag / Bestand zum Jahresbeginn"))
        self.table.setItem(0, 3, QTableWidgetItem(""))
        self.table.setItem(0, 4, QTableWidgetItem(""))
        self.table.setItem(0, 5, _money_item(opening, colored=False, bold=True))
        for r, entry in enumerate(rows, start=1):
            b = entry["row"]
            text = b["text"] or acc.category_label(b["category"])
            if b["counterparty"]:
                text = f"{b['counterparty']} – {text}"
            self.table.setItem(r, 0, QTableWidgetItem(_de(b["booking_date"])))
            self.table.setItem(r, 1, QTableWidgetItem(b["booking_no"]))
            self.table.setItem(r, 2, QTableWidgetItem(text))
            self.table.setItem(r, 3, _money_item(b["gross_cents"], colored=False) if b["gross_cents"] > 0
                               else QTableWidgetItem(""))
            self.table.setItem(r, 4, _money_item(-b["gross_cents"], colored=False) if b["gross_cents"] < 0
                               else QTableWidgetItem(""))
            self.table.setItem(r, 5, _money_item(entry["balance"], colored=False, bold=True))
        self.balance.setText(f"Kassenbestand: {fmt_money(acc.account_balance(self.db, 'KASSE'))}")
        has_cash = self.db.conn.execute("SELECT 1 FROM bookings WHERE account = 'KASSE' LIMIT 1").fetchone()
        self.btn_opening.setEnabled(not has_cash)

    def _opening(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Anfangsbestand der Kasse")
        d = _date_edit()
        amount = _amount_spin()
        form = QFormLayout()
        form.addRow("Datum", d)
        form.addRow("Gezählter Bestand", amount)
        buttons = _buttons("Buchen")
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("Bargeldbestand der Geschäftskasse zu Beginn der Aufzeichnungen:"))
        lay.addLayout(form)
        lay.addWidget(buttons)
        if not dlg.exec() or amount.value() <= 0:
            return
        try:
            acc.create_booking(self.db, acc.BookingInput(d.date().toPython(), "KASSE", "anfangsbestand",
                                                         euros_to_cents(amount.value()), text="Anfangsbestand Kasse"))
        except GoBDError as exc:
            QMessageBox.warning(self, "Nicht möglich", str(exc))
            return
        self.tab.changed()


class EuerPage(_Page):
    def __init__(self, tab):
        super().__init__(tab)
        self.year = _year_combo(self.db)
        btn_pdf = QPushButton("Als PDF speichern …")
        btn_pdf.setObjectName("Primary")
        head = QHBoxLayout()
        head.addWidget(QLabel("Jahr"))
        head.addWidget(self.year)
        head.addStretch(1)
        head.addWidget(btn_pdf)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Position", "Betrag"])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().resizeSection(1, 160)
        self.profit = QLabel()
        self.profit.setObjectName("TotalBig")
        self.notes = QLabel()
        self.notes.setWordWrap(True)
        self.notes.setObjectName("Hint")
        info = QLabel("Zufluss-/Abflussprinzip: maßgeblich ist das Zahlungsdatum. Die Werte in „Mein ELSTER“ in die "
                      "Anlage EÜR übertragen (Zuordnung laut aktuellem Vordruck). Keine Steuerberatung.")
        info.setWordWrap(True)
        info.setObjectName("Hint")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 14, 0, 0)
        layout.addLayout(head)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.profit)
        layout.addWidget(self.notes)
        layout.addWidget(info)
        self.year.currentIndexChanged.connect(self.reload)
        btn_pdf.clicked.connect(self._pdf)
        self.report: acc.EuerReport | None = None

    def reload(self) -> None:
        _refill_years(self.year, self.db)
        self.report = rep = acc.euer(self.db, self.year.currentData() or date.today().year)
        rows: list[tuple[str, int | None, bool]] = [("Betriebseinnahmen", None, True)]
        rows += [(f"   {x.label}" + (f"  ({x.note})" if x.note else ""), x.cents, False) for x in rep.income]
        rows.append(("Summe Betriebseinnahmen", rep.total_income, True))
        rows.append(("", None, False))
        rows.append(("Betriebsausgaben", None, True))
        rows += [(f"   {x.label}" + (f"  ({x.note})" if x.note else ""), x.cents, False) for x in rep.expenses]
        rows.append(("Summe Betriebsausgaben", rep.total_expenses, True))
        self.table.setRowCount(len(rows))
        bold = QFont()
        bold.setBold(True)
        for r, (label, cents, is_bold) in enumerate(rows):
            item = QTableWidgetItem(label)
            if is_bold:
                item.setFont(bold)
            self.table.setItem(r, 0, item)
            self.table.setItem(r, 1, _money_item(cents, colored=False, bold=is_bold) if cents is not None
                               else QTableWidgetItem(""))
        color = style.SUCCESS if rep.profit >= 0 else style.DANGER
        self.profit.setText(f"Gewinn / Verlust {rep.year}: <span style='color:{color}'>{fmt_money(rep.profit)}</span>")
        self.notes.setText("<br>".join(f"⚠ {n}" for n in rep.notes))

    def _pdf(self) -> None:
        if self.report is None:
            return
        default = str(Path.home() / "Documents" / f"EUER_{self.report.year}_{now_stamp()}.pdf")
        path, _ = QFileDialog.getSaveFileName(self, "EÜR speichern", default, "PDF (*.pdf)")
        if not path:
            return
        try:
            acc.euer_pdf(self.db, self.report, Path(path))
        except PermissionError:
            QMessageBox.warning(self, "Speichern nicht möglich", "Die Datei ist vermutlich noch geöffnet.")
            return
        open_path(path)


# ---------------------------------------------------------------------------- tab
class AccountingTab(QWidget):
    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.state = state
        self.db = state.db
        self.tabs = QTabWidget()
        self.tabs.setObjectName("SubTabs")
        self.tabs.setDocumentMode(True)
        self.pages: list[_Page] = [OverviewPage(self), JournalPage(self), OpenItemsPage(self), BankPage(self),
                                   CashPage(self), EuerPage(self)]
        for page, title in zip(self.pages, ["Übersicht", "Journal", "Offene Posten", "Bankabgleich", "Kassenbuch",
                                            "EÜR"]):
            self.tabs.addTab(page, title)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 10, 18, 18)
        layout.addWidget(self.tabs)
        self.tabs.currentChanged.connect(lambda i: self.pages[i].reload())
        for sig in (state.accountingChanged, state.documentsChanged, state.settingsChanged):
            sig.connect(self.reload)
        self.reload()

    def reload(self) -> None:
        self.pages[self.tabs.currentIndex()].reload()

    def changed(self) -> None:
        self.state.accountingChanged.emit()

    # --- gemeinsame Aktionen
    def add_expense(self) -> None:
        if BookingDialog(self.db, acc.EXPENSE, self).exec():
            self.changed()

    def add_income(self) -> None:
        small = bool(self.db.get_settings().get("small_business"))
        if BookingDialog(self.db, acc.INCOME, self, category=acc.default_income_category(small)).exec():
            self.changed()

    def add_cash_sale(self) -> None:
        small = bool(self.db.get_settings().get("small_business"))
        if BookingDialog(self.db, acc.INCOME, self, account="KASSE",
                         category=acc.default_income_category(small)).exec():
            self.changed()

    def add_cash_expense(self) -> None:
        if BookingDialog(self.db, acc.EXPENSE, self, account="KASSE").exec():
            self.changed()

    def add_transit(self) -> None:
        if TransitDialog(self.db, self).exec():
            self.changed()

    def import_bank(self, account: str | None = None) -> None:
        account = account or "BANK"
        path, _ = QFileDialog.getOpenFileName(
            self, f"Kontoauszug importieren ({acc.ACCOUNTS[account]})", str(Path.home() / "Downloads"),
            "Kontoauszüge (*.csv *.txt *.xml *.zip);;Alle Dateien (*)")
        if not path:
            return
        try:
            if acc.is_camt(Path(path)):
                txs = acc.read_camt_file(Path(path))
            else:
                header, rows = acc.read_csv(Path(path))
                mapping = acc.auto_mapping(header)
                if "date" not in mapping or not ("amount" in mapping or {"debit", "credit"} <= mapping.keys()):
                    dlg = CsvMappingDialog(header, rows, mapping, self)
                    if not dlg.exec():
                        return
                    mapping = dlg.mapping()
                txs = acc.transactions_from_csv(header, rows, mapping)
            if not txs:
                QMessageBox.warning(self, "Keine Umsätze", "In der Datei wurden keine Umsätze erkannt.")
                return
            result = acc.import_transactions(self.db, Path(path), account, txs)
        except (ValueError, GoBDError) as exc:
            QMessageBox.warning(self, "Import nicht möglich", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Fehler beim Import", str(exc))
            return
        self.changed()
        self.tabs.setCurrentWidget(self.pages[3])
        QMessageBox.information(self, "Kontoauszug importiert",
                                f"{result.new} neue Umsätze übernommen, {result.duplicates} bereits vorhanden.\n\n"
                                "Bitte jetzt jeden Umsatz zuordnen: Rechnung, Ausgabe, Einnahme oder Privat/Transit.")
