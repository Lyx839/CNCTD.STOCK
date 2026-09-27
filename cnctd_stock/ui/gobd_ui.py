"""GoBD-Dialoge: Dokumentenarchiv mit Storno, Änderungsprotokoll, Integritätsbericht."""

import json
from datetime import date
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from .. import gobd
from ..database import DOC_CANCEL, DOC_DELIVERY, DOC_LABELS, PRICED_TYPES, STATUS_CANCELLED, GoBDError
from ..formatting import fmt_money
from . import style
from .common import AppState, open_path, print_path, reveal_in_explorer


def _make_table(cols: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(cols))
    t.setHorizontalHeaderLabels(cols)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setAlternatingRowColors(True)
    t.setShowGrid(False)
    t.setWordWrap(False)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(30)
    h = t.horizontalHeader()
    h.setHighlightSections(False)
    h.setSectionResizeMode(QHeaderView.ResizeToContents)
    return t


def _search_field(placeholder: str) -> QLineEdit:
    edit = QLineEdit()
    edit.setObjectName("Search")
    edit.setPlaceholderText(placeholder)
    edit.setClearButtonEnabled(True)
    return edit


def _filter_rows(table: QTableWidget, text: str) -> None:
    text = text.lower().strip()
    for r in range(table.rowCount()):
        hay = " ".join((table.item(r, c).text() + " " + table.item(r, c).toolTip()).lower()
                       for c in range(table.columnCount()) if table.item(r, c))
        table.setRowHidden(r, bool(text) and text not in hay)


def show_integrity_report(parent, report: gobd.IntegrityReport) -> None:
    lines = [f"Änderungsprotokoll: {report.chain_entries} Einträge – "
             + ("Verkettung intakt." if report.chain_ok else f"FEHLER ab Eintrag #{report.first_bad_entry}."),
             f"Geprüfte Belege: {report.documents_checked}"]
    if report.problems:
        lines += ["", "Gefundene Probleme:"] + [f"• {p}" for p in report.problems[:25]]
    if report.notes:
        lines += ["", "Hinweise:"] + [f"• {n}" for n in report.notes[:10]]
    if report.ok:
        QMessageBox.information(parent, "Integrität geprüft",
                                "Alle Belege und das Änderungsprotokoll sind unverändert.\n\n" + "\n".join(lines))
    else:
        QMessageBox.warning(parent, "Integritätsprüfung: Abweichungen gefunden",
                            "\n".join(lines) + "\n\nBitte wenden Sie sich ggf. an Ihren Steuerberater und "
                            "dokumentieren Sie den Vorfall.")


# --------------------------------------------------------------------- Storno
class CancelDialog(QDialog):
    def __init__(self, row, can_restock: bool, parent=None):
        super().__init__(parent)
        label = f"{DOC_LABELS.get(row['doc_type'], row['doc_type'])} {row['doc_number']}"
        self.setWindowTitle(f"{label} stornieren")
        self.setMinimumWidth(520)

        if row["doc_type"] == DOC_DELIVERY:
            text = ("Der Lieferschein wird als <b>storniert</b> gekennzeichnet. Er bleibt unverändert im "
                    "Archiv erhalten.")
        else:
            text = ("Es wird eine <b>Stornorechnung</b> mit negativen Beträgen erstellt, die auf "
                    f"<b>{row['doc_number']}</b> verweist. Die Originalrechnung bleibt unverändert im Archiv und "
                    "wird als „storniert“ gekennzeichnet.<br><br>Danach erstellen Sie bei Bedarf eine neue, "
                    "korrigierte Rechnung.")
        info = QLabel(text)
        info.setWordWrap(True)
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("z. B. falscher Preis, Kunde hat vom Kauf zurückgetreten …")
        self.cb_restock = QCheckBox("Abgebuchten Bestand zurückbuchen")
        self.cb_restock.setEnabled(can_restock)
        self.cb_restock.setChecked(can_restock)
        if not can_restock:
            self.cb_restock.setToolTip("Bei diesem Beleg wurde kein Bestand abgebucht.")

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok = self.buttons.button(QDialogButtonBox.Ok)
        ok.setText("Verbindlich stornieren")
        ok.setObjectName("Primary")
        ok.setEnabled(False)
        self.buttons.button(QDialogButtonBox.Cancel).setText("Abbrechen")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.reason.textChanged.connect(lambda t: ok.setEnabled(bool(t.strip())))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(info)
        layout.addWidget(QLabel("Grund der Stornierung *"))
        layout.addWidget(self.reason)
        layout.addWidget(self.cb_restock)
        layout.addWidget(self.buttons)


# --------------------------------------------------------------------- Archiv
class ArchiveDialog(QDialog):
    COLS = ["Typ", "Nummer", "Datum", "Empfänger", "Betrag", "Status", "Versand", "Sendungs-Nr.", "Erfasst am"]

    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.state = state
        self.db = state.db
        self.setWindowTitle("Dokumentenarchiv (GoBD)")
        self.resize(1180, 600)

        self.search = _search_field("🔍  Archiv durchsuchen (Nummer, Empfänger, Sendungsnummer …)")
        self.table = _make_table(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)

        self.btn_open = QPushButton("Öffnen")
        self.btn_open.setObjectName("Primary")
        self.btn_print = QPushButton("Drucken")
        self.btn_folder = QPushButton("Im Ordner anzeigen")
        self.btn_tracking = QPushButton("Sendungs-Nr. eintragen")
        self.btn_tracking.setToolTip("Sendungsnummer nachträglich vermerken (wird protokolliert, das PDF bleibt "
                                     "unverändert)")
        self.btn_cancel = QPushButton("Stornieren …")
        self.btn_cancel.setObjectName("Danger")
        self.btn_verify = QPushButton("Integrität prüfen")
        btn_close = QPushButton("Schließen")
        hint = QLabel("Belege sind unveränderbar archiviert. Korrekturen erfolgen über „Stornieren“.")
        hint.setObjectName("Hint")

        bar = QHBoxLayout()
        for b in (self.btn_open, self.btn_print, self.btn_folder, self.btn_tracking, self.btn_cancel):
            bar.addWidget(b)
        bar.addStretch(1)
        bar.addWidget(self.btn_verify)
        bar.addWidget(btn_close)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.addWidget(self.search)
        layout.addWidget(self.table, 1)
        layout.addWidget(hint)
        layout.addLayout(bar)

        self.search.textChanged.connect(lambda t: _filter_rows(self.table, t))
        self.table.doubleClicked.connect(lambda _: self._open())
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.btn_open.clicked.connect(self._open)
        self.btn_print.clicked.connect(self._print)
        self.btn_folder.clicked.connect(self._folder)
        self.btn_tracking.clicked.connect(self._edit_tracking)
        self.btn_cancel.clicked.connect(self._cancel)
        self.btn_verify.clicked.connect(lambda: show_integrity_report(self, gobd.verify_integrity(self.db)))
        btn_close.clicked.connect(self.accept)
        self._load()

    def _load(self, select_id: int | None = None) -> None:
        rows = self.db.list_documents()
        numbers = {r["id"]: r["doc_number"] for r in rows}
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            recipient = (row["recipient"] or "").strip().splitlines()
            priced = row["doc_type"] in PRICED_TYPES
            if row["doc_type"] == DOC_CANCEL:
                status = f"Storno zu {numbers.get(row['cancels_id'], '?')}"
            elif row["status"] == STATUS_CANCELLED:
                by = numbers.get(row["cancelled_by_id"])
                status = f"storniert ({by})" if by else "storniert"
            else:
                status = "gültig"
            values = [DOC_LABELS.get(row["doc_type"], row["doc_type"]), row["doc_number"],
                      date.fromisoformat(row["doc_date"]).strftime("%d.%m.%Y"),
                      recipient[0] if recipient else "", fmt_money(row["total_cents"]) if priced else "–",
                      status, row["shipping_method"], row["tracking_no"], row["created_at"][:16]]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c == 0:
                    item.setData(Qt.UserRole, row["id"])
                if c == 1:
                    item.setToolTip(f"Archiv: {row['archive_path'] or '–'}\nSHA-256: {row['pdf_sha256'] or '–'}")
                if c == 4:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if c == 5:
                    if row["status"] == STATUS_CANCELLED:
                        item.setForeground(QColor(style.DANGER))
                        item.setToolTip(f"Grund: {row['cancel_reason']}")
                    elif row["doc_type"] == DOC_CANCEL:
                        item.setForeground(QColor("#B45309"))
                    else:
                        item.setForeground(QColor(style.SUCCESS))
                self.table.setItem(r, c, item)
            if row["id"] == select_id:
                self.table.selectRow(r)
        _filter_rows(self.table, self.search.text())
        self._update_buttons()

    def _current(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.db.get_document(self.table.item(rows[0].row(), 0).data(Qt.UserRole))

    def _file(self, row) -> Path | None:
        f = gobd.archive_file(row)
        if f and f.is_file():
            return f
        if row["file_path"] and Path(row["file_path"]).is_file():
            return Path(row["file_path"])
        return None

    def _update_buttons(self) -> None:
        row = self._current()
        has_file = bool(row) and self._file(row) is not None
        self.btn_open.setEnabled(has_file)
        self.btn_print.setEnabled(has_file)
        self.btn_folder.setEnabled(has_file)
        self.btn_tracking.setEnabled(bool(row))
        self.btn_cancel.setEnabled(bool(row) and row["status"] != STATUS_CANCELLED
                                   and row["doc_type"] != DOC_CANCEL)

    def _open(self) -> None:
        row = self._current()
        if row and self._file(row):
            open_path(self._file(row))

    def _print(self) -> None:
        row = self._current()
        f = self._file(row) if row else None
        if f and not print_path(f):
            open_path(f)

    def _folder(self) -> None:
        row = self._current()
        f = self._file(row) if row else None
        if f:
            reveal_in_explorer(f)

    def _edit_tracking(self) -> None:
        row = self._current()
        if not row:
            return
        text, ok = QInputDialog.getText(self, "Sendungsnummer", f"Sendungsnummer für {row['doc_number']}:",
                                        text=row["tracking_no"])
        if ok and text.strip() != row["tracking_no"]:
            self.db.update_tracking_no(row["id"], text)
            self.state.documentsChanged.emit()
            self._load(row["id"])

    def _cancel(self) -> None:
        row = self._current()
        if not row:
            return
        dlg = CancelDialog(row, gobd.can_restock(row), self)
        if not dlg.exec():
            return
        out_dir = self.db.get_settings().get("pdf_output_dir") or None
        try:
            result = gobd.cancel_document(self.db, row["id"], dlg.reason.text(), dlg.cb_restock.isChecked(),
                                          Path(out_dir) if out_dir else None)
        except GoBDError as exc:
            QMessageBox.warning(self, "Stornierung nicht möglich", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Fehler", str(exc))
            return
        self.state.documentsChanged.emit()
        if dlg.cb_restock.isChecked():
            self.state.inventoryChanged.emit()
        if result:
            self._load(result.doc_id)
            QMessageBox.information(self, "Storniert",
                                    f"{row['doc_number']} wurde storniert. Stornorechnung {result.number} "
                                    "wurde erstellt und archiviert.")
            open_path(result.work_copy or result.archive_file)
        else:
            self._load(row["id"])
            QMessageBox.information(self, "Storniert", f"{row['doc_number']} wurde als storniert gekennzeichnet.")


# ------------------------------------------------------------ Änderungsprotokoll
ACTION_LABELS = {
    "GOBD_MODUS_AKTIVIERT": "GoBD-Modus aktiviert",
    "ARTIKEL_ANGELEGT": "Artikel angelegt", "ARTIKEL_GEAENDERT": "Artikel geändert",
    "ARTIKEL_GELOESCHT": "Artikel gelöscht", "BESTAND_GEBUCHT": "Bestandsbuchung",
    "KUNDE_ANGELEGT": "Kunde angelegt", "KUNDE_GEAENDERT": "Kunde geändert", "KUNDE_GELOESCHT": "Kunde gelöscht",
    "EINSTELLUNGEN_GEAENDERT": "Einstellungen geändert", "VERSANDARTEN_GEAENDERT": "Versandarten geändert",
    "BELEG_ERSTELLT": "Beleg erstellt", "BELEG_STORNIERT": "Beleg storniert",
    "SENDUNGSNR_GEAENDERT": "Sendungsnummer geändert", "ALTBESTAND_ARCHIVIERT": "Altbestand archiviert",
    "INTEGRITAETSPRUEFUNG": "Integritätsprüfung", "DATENSICHERUNG_ERSTELLT": "Datensicherung erstellt",
    "SICHERUNG_WIEDERHERGESTELLT": "Sicherung wiederhergestellt",
    "DATENEXPORT_BETRIEBSPRUEFUNG": "Datenexport Betriebsprüfung",
    "VERFAHRENSDOKUMENTATION_ERSTELLT": "Verfahrensdokumentation erstellt",
}


def _summary(details: str) -> str:
    try:
        d = json.loads(details)
    except ValueError:
        return details
    parts = []
    for k, v in d.items():
        if isinstance(v, dict):
            v = ", ".join(f"{kk}: {vv[0]!s} → {vv[1]!s}" if isinstance(vv, list) and len(vv) == 2 else f"{kk}: {vv}"
                          for kk, vv in v.items())
        elif isinstance(v, list) and len(v) == 2 and not isinstance(v[0], list):
            v = f"{v[0]} → {v[1]}"
        parts.append(f"{k}: {v}")
    return " · ".join(parts).replace("\n", " / ")


class AuditLogDialog(QDialog):
    COLS = ["Nr.", "Zeitpunkt", "Aktion", "Objekt", "Details"]

    def __init__(self, state: AppState, parent=None):
        super().__init__(parent)
        self.db = state.db
        self.setWindowTitle("Änderungsprotokoll (GoBD)")
        self.resize(1180, 640)
        self.search = _search_field("🔍  Protokoll durchsuchen …")
        self.table = _make_table(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)
        info = QLabel("Jeder Eintrag ist mit seinem Vorgänger per SHA-256 verkettet und kann weder geändert "
                      "noch gelöscht werden.")
        info.setObjectName("Hint")
        btn_verify = QPushButton("Integrität prüfen")
        btn_close = QPushButton("Schließen")
        bar = QHBoxLayout()
        bar.addWidget(info, 1)
        bar.addWidget(btn_verify)
        bar.addWidget(btn_close)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.addWidget(self.search)
        layout.addWidget(self.table, 1)
        layout.addLayout(bar)
        self.search.textChanged.connect(lambda t: _filter_rows(self.table, t))
        btn_verify.clicked.connect(self._verify)
        btn_close.clicked.connect(self.accept)
        self._load()

    def _load(self) -> None:
        rows = self.db.list_audit()
        self.table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            obj = f"{row['entity']} {row['entity_id']}".strip()
            values = [str(row["id"]), row["ts"].replace("T", " "), ACTION_LABELS.get(row["action"], row["action"]),
                      obj, _summary(row["details"])]
            for c, v in enumerate(values):
                item = QTableWidgetItem(v)
                if c == 4:
                    try:
                        item.setToolTip(json.dumps(json.loads(row["details"]), ensure_ascii=False, indent=2)
                                        + f"\n\nHash: {row['hash']}")
                    except ValueError:
                        item.setToolTip(row["details"])
                self.table.setItem(r, c, item)
        _filter_rows(self.table, self.search.text())

    def _verify(self) -> None:
        show_integrity_report(self, gobd.verify_integrity(self.db))
        self._load()
