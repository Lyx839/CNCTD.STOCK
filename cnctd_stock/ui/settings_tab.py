"""Tab 3 – Einstellungen: Logo, Absender, Steuer/Bank, Dokumente, Datensicherung."""

import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path

from PySide6.QtCore import QMargins, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, APP_VERSION
from .. import gobd
from ..database import DOC_INVOICE, GoBDError, ShippingMethod, now_stamp
from ..formatting import cents_to_euros, euros_to_cents
from ..paths import ASSETS_DIR, DATA_DIR, DB_PATH
from ..pdf_engine import DocItem, DocumentData, build_document
from .common import AppState, CellSpinBox, open_path
from .gobd_ui import AuditLogDialog, show_integrity_report


class SettingsTab(QWidget):
    TEXT_FIELDS = [
        # key, label, placeholder
        ("sender_name", "Firmenname / Absender *", "z. B. CNCTD GmbH"),
        ("sender_owner", "Inhaber / Ansprechpartner", "z. B. Max Mustermann"),
        ("sender_street", "Straße, Nr.", "Musterstraße 1"),
        ("sender_zip_city", "PLZ, Ort", "12345 Musterstadt"),
        ("sender_country", "Land", "optional"),
        ("sender_phone", "Telefon", ""),
        ("sender_email", "E-Mail", ""),
        ("sender_website", "Website", ""),
    ]
    TAX_FIELDS = [
        ("tax_number", "Steuernummer", "z. B. 12/345/67890"),
        ("vat_id", "USt-IdNr.", "z. B. DE123456789"),
        ("bank_name", "Bank", ""),
        ("iban", "IBAN", "DE00 0000 0000 0000 0000 00"),
        ("bic", "BIC", ""),
    ]

    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.state = state
        self.db = state.db
        self.edits: dict[str, QLineEdit] = {}
        self.logo_path = ""

        content = QWidget()
        content.setObjectName("Page")
        grid = QGridLayout(content)
        grid.setContentsMargins(18, 8, 18, 18)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(6)
        grid.addWidget(self._logo_group(), 0, 0)
        grid.addWidget(self._sender_group(), 1, 0)
        grid.addWidget(self._tax_group(), 0, 1)
        grid.addWidget(self._documents_group(), 1, 1)
        grid.addWidget(self._shipping_group(), 2, 0)
        grid.addWidget(self._data_group(), 2, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(3, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)

        # Aktionsleiste
        self.btn_save = QPushButton("Einstellungen speichern")
        self.btn_save.setObjectName("Primary")
        self.btn_test = QPushButton("Test-Rechnung (Layout-Vorschau)")
        self.btn_reset = QPushButton("Änderungen verwerfen")
        bar = QHBoxLayout()
        bar.setContentsMargins(18, 10, 18, 12)
        bar.addWidget(self.btn_test)
        bar.addStretch(1)
        bar.addWidget(self.btn_reset)
        bar.addWidget(self.btn_save)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(scroll, 1)
        layout.addLayout(bar)

        self.btn_save.clicked.connect(self.save)
        self.btn_reset.clicked.connect(self.load)
        self.btn_test.clicked.connect(self.test_pdf)
        self.load()

    # ---------------------------------------------------------------- groups
    def _line(self, key: str, placeholder: str = "") -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        self.edits[key] = edit
        return edit

    def _logo_group(self) -> QGroupBox:
        box = QGroupBox("Firmenlogo")
        self.logo_preview = QLabel("Kein Logo hinterlegt")
        self.logo_preview.setAlignment(Qt.AlignCenter)
        self.logo_preview.setFixedSize(260, 110)
        self.logo_preview.setStyleSheet("border: 1px dashed #C3CCDD; border-radius: 8px; background: #FAFBFE;"
                                        "color: #8A93A5;")
        btn_upload = QPushButton("Logo hochladen …")
        btn_remove = QPushButton("Entfernen")
        btn_remove.setObjectName("Danger")
        self.logo_pos = QComboBox()
        self.logo_pos.addItem("Logo rechts, Absender links", "rechts")
        self.logo_pos.addItem("Logo links, Absender rechts", "links")
        hint = QLabel("PNG oder JPG. Transparente PNGs sehen am besten aus.")
        hint.setObjectName("Hint")

        side = QVBoxLayout()
        side.addWidget(btn_upload)
        side.addWidget(btn_remove)
        side.addStretch(1)
        row = QHBoxLayout()
        row.addWidget(self.logo_preview)
        row.addLayout(side)
        row.addStretch(1)
        layout = QVBoxLayout(box)
        layout.addLayout(row)
        form = QFormLayout()
        form.addRow("Position im Dokument", self.logo_pos)
        self.cb_show_name = QCheckBox("Firmenname zusätzlich zum Logo im Briefkopf anzeigen")
        self.cb_show_name.setToolTip("Ohne Logo wird der Firmenname immer angezeigt.")
        form.addRow("", self.cb_show_name)
        layout.addLayout(form)
        layout.addWidget(hint)

        btn_upload.clicked.connect(self._choose_logo)
        btn_remove.clicked.connect(lambda: self._set_logo(""))
        return box

    def _sender_group(self) -> QGroupBox:
        box = QGroupBox("Absender / Rechnungskopf")
        form = QFormLayout(box)
        form.setSpacing(8)
        for key, label, ph in self.TEXT_FIELDS:
            form.addRow(label, self._line(key, ph))
        return box

    def _tax_group(self) -> QGroupBox:
        box = QGroupBox("Steuer && Bankverbindung")
        form = QFormLayout(box)
        form.setSpacing(8)
        for key, label, ph in self.TAX_FIELDS:
            form.addRow(label, self._line(key, ph))
        self.vat_spin = QDoubleSpinBox()
        self.vat_spin.setRange(0, 100)
        self.vat_spin.setDecimals(1)
        self.vat_spin.setSuffix(" %")
        self.small_business = QCheckBox("Kleinunternehmer nach § 19 UStG (keine USt. ausweisen)")
        self.payment_days = QSpinBox()
        self.payment_days.setRange(0, 365)
        self.payment_days.setSuffix(" Tage")
        self.payment_days.setSpecialValueText("sofort fällig (kein Zahlungsziel)")
        form.addRow("Umsatzsteuer", self.vat_spin)
        form.addRow("", self.small_business)
        form.addRow("Zahlungsziel", self.payment_days)
        self.cb_accounting = QCheckBox("Buchhaltung aktivieren (EÜR, Kassenbuch, Bankabgleich)")
        self.cb_accounting.setToolTip("Blendet den Bereich „Buchhaltung“ ein: Ausgaben mit Beleg, Zahlungseingänge, "
                                      "Kontoauszug-Import, Kassenbuch und Einnahmen-Überschuss-Rechnung.")
        form.addRow("Module", self.cb_accounting)
        self.small_business.toggled.connect(lambda on: self.vat_spin.setEnabled(not on))
        return box

    def _documents_group(self) -> QGroupBox:
        box = QGroupBox("Dokumente && Speicherorte")
        form = QFormLayout(box)
        form.setSpacing(8)
        self.pdf_dir = self._line("pdf_output_dir")
        btn_browse = QPushButton("…")
        btn_browse.setFixedWidth(40)
        btn_open = QPushButton("Öffnen")
        row = QHBoxLayout()
        row.addWidget(self.pdf_dir, 1)
        row.addWidget(btn_browse)
        row.addWidget(btn_open)
        form.addRow("PDF-Speicherort", row)

        self.counter_spins: dict[str, QSpinBox] = {}
        for label, prefix_key, counter_key in (("Rechnungsnummern", "invoice_prefix", "next_invoice_no"),
                                               ("Stornorechnungen", "cancel_prefix", "next_cancel_no"),
                                               ("Lieferscheinnummern", "delivery_prefix", "next_delivery_no")):
            prefix = self._line(prefix_key)
            prefix.setMaximumWidth(80)
            spin = QSpinBox()
            spin.setRange(1, 999_999)
            spin.setToolTip("Kann nur erhöht werden (GoBD) – so ist jede Nummer eindeutig.")
            self.counter_spins[counter_key] = spin
            row_layout = QHBoxLayout()
            row_layout.addWidget(QLabel("Präfix"))
            row_layout.addWidget(prefix)
            row_layout.addSpacing(10)
            row_layout.addWidget(QLabel("nächste Nr."))
            row_layout.addWidget(spin)
            row_layout.addStretch(1)
            form.addRow(label, row_layout)

        self.inv_text = QPlainTextEdit()
        self.inv_text.setFixedHeight(54)
        self.del_text = QPlainTextEdit()
        self.del_text.setFixedHeight(54)
        form.addRow("Schlusstext Rechnung", self.inv_text)
        form.addRow("Schlusstext Lieferschein", self.del_text)
        hint = QLabel("Format: PRÄFIX-JAHR-NUMMER, z. B. RE-2026-0001. Nummern werden automatisch und "
                      "fortlaufend vergeben; Zähler lassen sich nur erhöhen.")
        hint.setWordWrap(True)
        hint.setObjectName("Hint")
        form.addRow("", hint)

        btn_browse.clicked.connect(self._choose_pdf_dir)
        btn_open.clicked.connect(self._open_pdf_dir)
        return box

    def _data_group(self) -> QGroupBox:
        box = QGroupBox("Datensicherung && GoBD")
        layout = QVBoxLayout(box)
        info = QLabel(f"Datenbank: {DB_PATH}<br>Belegarchiv: {gobd.ARCHIVE_DIR}")
        info.setWordWrap(True)
        info.setObjectName("Muted")
        info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        text = QLabel("Belege werden unveränderbar archiviert, alle Änderungen protokolliert. Erstellen Sie "
                      "regelmäßig eine Datensicherung auf einem externen Datenträger. Aufbewahrungsfrist für "
                      "Rechnungen: 8 Jahre.")
        text.setWordWrap(True)
        text.setObjectName("Hint")
        version = QLabel(f"{APP_NAME} v{APP_VERSION}")
        version.setObjectName("Hint")

        buttons = {
            "Datensicherung erstellen …": self._backup,
            "Sicherung wiederherstellen …": self._restore,
            "Datenordner öffnen": lambda: open_path(DATA_DIR),
            "Integrität prüfen": lambda: show_integrity_report(self, gobd.verify_integrity(self.db)),
            "Änderungsprotokoll …": lambda: AuditLogDialog(self.state, self).exec(),
            "Datenexport Betriebsprüfung …": self._export_audit,
            "Verfahrensdokumentation …": self._procedure_doc,
        }
        grid = QGridLayout()
        grid.setSpacing(8)
        for i, (label, slot) in enumerate(buttons.items()):
            btn = QPushButton(label)
            if label.startswith("Datensicherung"):
                btn.setObjectName("Primary")
            btn.clicked.connect(slot)
            grid.addWidget(btn, i // 2, i % 2)
        layout.addWidget(info)
        layout.addWidget(text)
        layout.addLayout(grid)
        layout.addWidget(version)
        layout.addStretch(1)
        return box

    def _shipping_group(self) -> QGroupBox:
        box = QGroupBox("Versandarten")
        self.ship_table = QTableWidget(0, 2)
        self.ship_table.setHorizontalHeaderLabels(["Versandart", "Preis (netto)"])
        self.ship_table.verticalHeader().setVisible(False)
        self.ship_table.verticalHeader().setDefaultSectionSize(34)
        self.ship_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.ship_table.setShowGrid(False)
        self.ship_table.setMinimumHeight(180)
        h = self.ship_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        h.setSectionResizeMode(1, QHeaderView.Fixed)
        h.resizeSection(1, 130)
        btn_add = QPushButton("＋ Versandart")
        btn_remove = QPushButton("Entfernen")
        btn_remove.setObjectName("Danger")
        hint = QLabel("Der Preis wird bei Auswahl der Versandart als Versandkosten vorgeschlagen und "
                      "kann im Dokument angepasst werden. Doppelklick auf den Namen zum Umbenennen.")
        hint.setObjectName("Hint")
        hint.setWordWrap(True)
        row = QHBoxLayout()
        row.addWidget(btn_add)
        row.addWidget(btn_remove)
        row.addStretch(1)
        layout = QVBoxLayout(box)
        layout.addWidget(self.ship_table, 1)
        layout.addLayout(row)
        layout.addWidget(hint)
        btn_add.clicked.connect(lambda: self._add_shipping_row("Neue Versandart", 0, edit=True))
        btn_remove.clicked.connect(self._remove_shipping_rows)
        return box

    def _add_shipping_row(self, name: str, price_cents: int, edit: bool = False) -> None:
        r = self.ship_table.rowCount()
        self.ship_table.insertRow(r)
        self.ship_table.setItem(r, 0, QTableWidgetItem(name))
        spin = CellSpinBox()
        spin.setRange(0, 100_000)
        spin.setDecimals(2)
        spin.setSuffix(" €")
        spin.setValue(cents_to_euros(price_cents))
        self.ship_table.setCellWidget(r, 1, spin)
        if edit:
            self.ship_table.setCurrentCell(r, 0)
            self.ship_table.editItem(self.ship_table.item(r, 0))

    def _remove_shipping_rows(self) -> None:
        for r in sorted({i.row() for i in self.ship_table.selectedIndexes()}, reverse=True):
            self.ship_table.removeRow(r)

    def _load_shipping(self) -> None:
        self.ship_table.setRowCount(0)
        for m in self.db.list_shipping_methods():
            self._add_shipping_row(m.name, m.price_cents)

    def _collect_shipping(self) -> list[ShippingMethod]:
        methods = []
        for r in range(self.ship_table.rowCount()):
            item = self.ship_table.item(r, 0)
            name = item.text().strip() if item else ""
            if name:
                methods.append(ShippingMethod(None, name, euros_to_cents(self.ship_table.cellWidget(r, 1).value())))
        return methods

    # ------------------------------------------------------------- load/save
    def load(self) -> None:
        s = self.db.get_settings()
        for key, edit in self.edits.items():
            edit.setText(str(s.get(key) or ""))
        self.vat_spin.setValue(float(s["vat_rate"] or 0))
        self.small_business.setChecked(bool(s["small_business"]))
        self.payment_days.setValue(int(s["payment_days"] or 0))
        for key, spin in self.counter_spins.items():
            spin.setMinimum(1)
            spin.setValue(int(s[key] or 1))
            spin.setMinimum(int(s[key] or 1))   # nur erhöhen
        self.inv_text.setPlainText(s["invoice_closing_text"] or "")
        self.del_text.setPlainText(s["delivery_closing_text"] or "")
        idx = self.logo_pos.findData(s["logo_position"] or "rechts")
        self.logo_pos.setCurrentIndex(max(idx, 0))
        self._set_logo(s["logo_path"] or "")
        self.cb_show_name.setChecked(bool(s["show_company_name"]))
        self.cb_accounting.setChecked(bool(s["accounting_enabled"]))
        self._load_shipping()

    def collect(self) -> dict:
        values = {key: edit.text().strip() for key, edit in self.edits.items()}
        values.update(
            logo_path=self.logo_path,
            logo_position=self.logo_pos.currentData(),
            show_company_name=int(self.cb_show_name.isChecked()),
            accounting_enabled=int(self.cb_accounting.isChecked()),
            vat_rate=self.vat_spin.value(),
            small_business=int(self.small_business.isChecked()),
            payment_days=self.payment_days.value(),
            **{key: spin.value() for key, spin in self.counter_spins.items()},
            invoice_closing_text=self.inv_text.toPlainText().strip(),
            delivery_closing_text=self.del_text.toPlainText().strip(),
        )
        values["invoice_prefix"] = values["invoice_prefix"] or "RE"
        values["delivery_prefix"] = values["delivery_prefix"] or "LS"
        values["cancel_prefix"] = values["cancel_prefix"] or "ST"
        return values

    def save(self) -> None:
        values = self.collect()
        pdf_dir = Path(values["pdf_output_dir"]) if values["pdf_output_dir"] else None
        if pdf_dir is None:
            QMessageBox.warning(self, "PDF-Speicherort", "Bitte einen Speicherort für die PDFs festlegen.")
            return
        try:
            pdf_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "PDF-Speicherort", f"Ordner kann nicht angelegt werden:\n{exc}")
            return
        try:
            self.db.save_settings(values)
        except GoBDError as exc:
            QMessageBox.warning(self, "Nicht zulässig", str(exc))
            return
        self.db.replace_shipping_methods(self._collect_shipping())
        self.load()
        self.state.settingsChanged.emit()
        self.window().statusBar().showMessage("Einstellungen gespeichert.", 5000)

    # ----------------------------------------------------------------- logo
    def _set_logo(self, path: str) -> None:
        self.logo_path = path
        pix = QPixmap(path) if path and Path(path).is_file() else QPixmap()
        if pix.isNull():
            self.logo_preview.setPixmap(QPixmap())
            self.logo_preview.setText("Kein Logo hinterlegt" if not path else "Logo-Datei nicht gefunden")
        else:
            area = self.logo_preview.size().shrunkBy(QMargins(10, 10, 10, 10))
            self.logo_preview.setPixmap(pix.scaled(area, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _choose_logo(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Logo auswählen", str(Path.home()),
                                              "Bilder (*.png *.jpg *.jpeg)")
        if not path:
            return
        if QPixmap(path).isNull():
            QMessageBox.warning(self, "Ungültiges Bild", "Die Datei konnte nicht als Bild gelesen werden.")
            return
        # Logo in den Datenordner kopieren, damit es auch nach Verschieben des Originals verfügbar bleibt.
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        target = ASSETS_DIR / f"logo_{now_stamp()}{Path(path).suffix.lower()}"
        shutil.copy2(path, target)
        self._set_logo(str(target))
        self.window().statusBar().showMessage("Logo übernommen – zum Übernehmen bitte speichern.", 5000)

    # ------------------------------------------------------------- folders
    def _choose_pdf_dir(self) -> None:
        start = self.pdf_dir.text() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "PDF-Speicherort wählen", start)
        if path:
            self.pdf_dir.setText(str(Path(path)))

    def _open_pdf_dir(self) -> None:
        path = Path(self.pdf_dir.text().strip() or Path.home())
        path.mkdir(parents=True, exist_ok=True)
        open_path(path)

    # --------------------------------------------------------------- backup
    def _backup(self) -> None:
        default = str(Path.home() / "Documents" / f"CNCTD_STOCK_Sicherung_{now_stamp()}.zip")
        path, _ = QFileDialog.getSaveFileName(self, "Datensicherung erstellen", default, "ZIP-Archiv (*.zip)")
        if not path:
            return
        try:
            gobd.create_backup(self.db, Path(path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Sicherung fehlgeschlagen", str(exc))
            return
        QMessageBox.information(self, "Sicherung erstellt",
                                f"Datenbank und Belegarchiv wurden gesichert:\n{path}\n\n"
                                "Bitte zusätzlich auf einem externen Datenträger aufbewahren.")

    def _restore(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Sicherung wiederherstellen", str(Path.home() / "Documents"),
                                              "Sicherungen (*.zip *.db)")
        if not path:
            return
        answer = QMessageBox.warning(
            self, "Sicherung wiederherstellen",
            "Der Datenbestand wird durch die Sicherung ersetzt. Fehlende Archiv-PDFs werden ergänzt, vorhandene "
            "nie überschrieben.\n\nVorher wird der aktuelle Stand automatisch gesichert.\n\nFortfahren?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        try:
            message = gobd.restore_backup(self.db, Path(path))
        except GoBDError as exc:
            QMessageBox.warning(self, "Wiederherstellung nicht möglich", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Fehler", str(exc))
            return
        for signal in (self.state.inventoryChanged, self.state.settingsChanged, self.state.documentsChanged,
                       self.state.customersChanged):
            signal.emit()
        self.load()
        QMessageBox.information(self, "Wiederhergestellt", message)

    def _export_audit(self) -> None:
        years = sorted({r["doc_date"][:4] for r in self.db.list_documents()}, reverse=True)
        options = [f"Jahr {y}" for y in years] + ["Gesamter Datenbestand"]
        choice, ok = QInputDialog.getItem(self, "Datenexport für die Betriebsprüfung",
                                          "Zeitraum:", options, 0, False)
        if not ok:
            return
        year = int(choice.split()[-1]) if choice.startswith("Jahr") else None
        default = str(Path.home() / "Documents" / f"Betriebspruefung_{year or 'gesamt'}_{now_stamp()}.zip")
        path, _ = QFileDialog.getSaveFileName(self, "Datenexport speichern", default, "ZIP-Archiv (*.zip)")
        if not path:
            return
        try:
            _, count = gobd.export_tax_audit(self.db, Path(path), year)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Export fehlgeschlagen", str(exc))
            return
        QMessageBox.information(
            self, "Datenexport erstellt",
            f"{count} Beleg(e) exportiert:\n{path}\n\nEnthalten: Belege, Positionen, Kunden, Artikel und "
            "Änderungsprotokoll als CSV, Beschreibung index.xml (GDPdU-Standard) sowie alle Beleg-PDFs.")

    def _procedure_doc(self) -> None:
        default = str(Path.home() / "Documents" / "Verfahrensdokumentation_CNCTD_STOCK.docx")
        path, _ = QFileDialog.getSaveFileName(self, "Verfahrensdokumentation speichern", default,
                                              "Word-Dokument (*.docx)")
        if not path:
            return
        try:
            gobd.write_procedure_documentation(self.db, Path(path))
        except PermissionError:
            QMessageBox.warning(self, "Speichern nicht möglich",
                                "Die Datei ist vermutlich noch in Word geöffnet. Bitte schließen und erneut "
                                "versuchen.")
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Fehler", str(exc))
            return
        if QMessageBox.question(
                self, "Verfahrensdokumentation erstellt",
                "Die vorausgefüllte Verfahrensdokumentation wurde erstellt. Gelb markierte Stellen bitte "
                "ergänzen und das Dokument zusammen mit den Belegen aufbewahren.\n\nJetzt öffnen?"
        ) == QMessageBox.Yes:
            open_path(path)

    # ----------------------------------------------------------- test PDF
    def test_pdf(self) -> None:
        settings = self.collect()
        items = [
            DocItem("A-00001", "Beispielartikel Premium", "Stk.", 3, 1999, "Mit optionaler Beschreibungszeile"),
            DocItem("A-00002", "Versandkarton groß", "Karton", 10, 245),
            DocItem("A-00003", "Montage vor Ort", "Std.", 1.5, 6500),
        ]
        data = DocumentData(
            doc_type=DOC_INVOICE, number="MUSTER-0001", doc_date=date.today(),
            delivery_date=date.today() - timedelta(days=1),
            recipient="Musterkunde GmbH\nErika Musterfrau\nBeispielweg 5\n54321 Beispielstadt",
            customer_no="K-1001", items=items, settings=settings,
            shipping_method="DHL Paket", shipping_cents=619, tracking_no="00340434161234567890",
            draft=True,
        )
        target = Path(tempfile.gettempdir()) / "CNCTD.STOCK" / f"Test_Rechnung_{now_stamp()}.pdf"
        try:
            build_document(data, target)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Fehler", f"Test-PDF konnte nicht erstellt werden:\n{exc}")
            return
        open_path(target)
