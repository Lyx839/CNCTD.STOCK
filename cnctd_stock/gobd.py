"""GoBD-Funktionen: revisionssicheres Archiv, Belegerstellung, Storno, Integritätsprüfung,
Datensicherung, Datenexport für die Betriebsprüfung und Verfahrensdokumentation.
"""

import csv
import hashlib
import io
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from xml.sax.saxutils import escape

from . import APP_NAME, APP_VERSION
from .database import (
    DOC_CANCEL,
    DOC_DELIVERY,
    DOC_INVOICE,
    DOC_LABELS,
    PRICED_TYPES,
    STATUS_CANCELLED,
    STATUS_LABELS,
    STATUS_VALID,
    Database,
    GoBDError,
    canonical_json,
    now_stamp,
    sha256_text,
)
from .formatting import line_total_cents
from .paths import ASSETS_DIR, DATA_DIR
from .pdf_engine import (
    DocItem,
    DocumentData,
    build_document,
    compute_totals,
    default_filename,
    document_from_dict,
    document_to_dict,
    safe_filename,
)

ARCHIVE_DIR = DATA_DIR / "archiv"
AUTO_BACKUP_DIR = DATA_DIR / "sicherungen"
SUBDIRS = {DOC_INVOICE: "Rechnungen", DOC_DELIVERY: "Lieferscheine", DOC_CANCEL: "Stornorechnungen"}


# --------------------------------------------------------------------------- helpers
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _set_readonly(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    except OSError:
        pass


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def archive_file(row) -> Path | None:
    rel = row["archive_path"] if row is not None else ""
    return ARCHIVE_DIR / rel if rel else None


def _now_sql() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ----------------------------------------------------------------------- erstellen
@dataclass
class CreatedDocument:
    doc_id: int
    number: str
    archive_file: Path
    work_copy: Path | None


def create_document(db: Database, data: DocumentData, *, customer_id: int | None = None,
                    deductions: dict[int, float] | None = None, output_dir: Path | None = None,
                    cancels: dict | None = None) -> CreatedDocument:
    """Vergibt die nächste Belegnummer, erzeugt das PDF im Archiv und schreibt den Beleg fest."""
    data.number = db.next_doc_number(data.doc_type, data.doc_date)
    data.draft = False
    target = _unique(ARCHIVE_DIR / str(data.doc_date.year) / SUBDIRS[data.doc_type] / default_filename(data))
    build_document(data, target)
    try:
        pdf_sha = sha256_file(target)
        snapshot = canonical_json(document_to_dict(data))
        totals = compute_totals(data.items, data.settings, data.shipping_cents) \
            if data.doc_type in PRICED_TYPES else None
        rec = {
            "doc_type": data.doc_type,
            "doc_number": data.number,
            "doc_date": data.doc_date.isoformat(),
            "recipient": data.recipient,
            "total_cents": totals.gross_cents if totals else 0,
            "net_cents": totals.net_cents if totals else 0,
            "vat_cents": totals.vat_cents if totals else 0,
            "file_path": "",
            "items_json": canonical_json([document_to_dict_item(i) for i in data.items]),
            "customer_id": customer_id,
            "delivery_address": data.delivery_address,
            "shipping_method": data.shipping_method,
            "shipping_cents": data.shipping_cents if data.doc_type in PRICED_TYPES else 0,
            "tracking_no": data.tracking_no,
            "status": STATUS_VALID,
            "cancels_id": cancels["id"] if cancels else None,
            "stock_deducted": 1 if deductions else 0,
            "archive_path": target.relative_to(ARCHIVE_DIR).as_posix(),
            "pdf_sha256": pdf_sha,
            "snapshot_json": snapshot,
            "snapshot_sha256": sha256_text(snapshot),
            "created_at": _now_sql(),
        }
        doc_id = db.insert_document(rec, deductions, cancels)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    _set_readonly(target)

    work_copy = None
    if output_dir:
        try:
            work_copy = _unique(Path(output_dir) / SUBDIRS[data.doc_type] / target.name)
            work_copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(target, work_copy)
            db.update_file_path(doc_id, str(work_copy))
        except OSError:
            work_copy = None  # Arbeitskopie ist optional, das Original liegt sicher im Archiv
    return CreatedDocument(doc_id, data.number, target, work_copy)


def document_to_dict_item(item: DocItem) -> dict:
    return {"article_no": item.article_no, "name": item.name, "unit": item.unit,
            "quantity": item.quantity, "unit_price_cents": item.unit_price_cents,
            "description": item.description, "article_id": item.article_id}


# -------------------------------------------------------------------------- storno
def original_document_data(db: Database, row) -> DocumentData:
    """Rekonstruiert die Belegdaten aus dem Archiv-Snapshot (bzw. Altbestand)."""
    snap = json.loads(row["snapshot_json"] or "{}")
    if snap and not snap.get("legacy"):
        return document_from_dict(snap)
    items = [DocItem(i.get("article_no", ""), i.get("name", ""), i.get("unit", ""), i.get("quantity", 0),
                     i.get("unit_price_cents", 0), i.get("description", ""))
             for i in json.loads(row["items_json"] or "[]")]
    return DocumentData(row["doc_type"], row["doc_number"], date.fromisoformat(row["doc_date"]),
                        row["recipient"], items, delivery_address=row["delivery_address"] or "",
                        shipping_method=row["shipping_method"] or "", shipping_cents=row["shipping_cents"] or 0,
                        settings=db.get_settings())


def _restock_map(row) -> dict[int, float]:
    result: dict[int, float] = {}
    for item in json.loads(row["items_json"] or "[]"):
        aid = item.get("article_id")
        if aid:
            result[aid] = result.get(aid, 0) + float(item.get("quantity") or 0)
    return result


def can_restock(row) -> bool:
    return bool(row["stock_deducted"]) and bool(_restock_map(row))


def cancel_document(db: Database, doc_id: int, reason: str, restock: bool,
                    output_dir: Path | None = None) -> CreatedDocument | None:
    row = db.get_document(doc_id)
    if row is None:
        raise GoBDError("Beleg nicht gefunden.")
    if row["status"] == STATUS_CANCELLED:
        raise GoBDError("Der Beleg ist bereits storniert.")
    if row["doc_type"] == DOC_CANCEL:
        raise GoBDError("Eine Stornorechnung kann nicht erneut storniert werden.")
    if not reason.strip():
        raise GoBDError("Bitte einen Grund für die Stornierung angeben.")
    restock_map = _restock_map(row) if restock and row["stock_deducted"] else None

    if row["doc_type"] == DOC_DELIVERY:
        db.cancel_without_document(doc_id, reason.strip(), restock_map)
        return None

    orig = original_document_data(db, row)
    settings = db.get_settings()
    # Steuerliche Behandlung wie in der Originalrechnung, damit sich die Beträge exakt aufheben.
    settings["vat_rate"] = orig.settings.get("vat_rate", settings["vat_rate"])
    settings["small_business"] = orig.settings.get("small_business", settings["small_business"])
    storno = DocumentData(
        doc_type=DOC_CANCEL, number="", doc_date=date.today(), recipient=orig.recipient,
        items=[DocItem(i.article_no, i.name, i.unit, -i.quantity, i.unit_price_cents, i.description)
               for i in orig.items],
        delivery_date=orig.delivery_date, customer_no=orig.customer_no, reference=orig.reference,
        delivery_address=orig.delivery_address, shipping_method=orig.shipping_method,
        shipping_cents=-int(orig.shipping_cents or 0), cancels_number=row["doc_number"],
        cancels_date=date.fromisoformat(row["doc_date"]), cancel_reason=reason.strip(), settings=settings,
    )
    return create_document(db, storno, customer_id=row["customer_id"], output_dir=output_dir,
                           cancels={"id": doc_id, "reason": reason.strip(), "restock": restock_map})


# -------------------------------------------------------------------- Altbestand
def migrate_legacy(db: Database) -> int:
    """Übernimmt Belege aus Versionen vor dem GoBD-Modus einmalig ins Archiv."""
    rows = db.conn.execute("SELECT * FROM documents WHERE snapshot_sha256 IS NULL ORDER BY id").fetchall()
    for row in rows:
        src = Path(row["file_path"]) if row["file_path"] else None
        rel, pdf_sha = "", None
        if src and src.is_file():
            year = row["doc_date"][:4]
            target = _unique(ARCHIVE_DIR / year / SUBDIRS.get(row["doc_type"], "Sonstige") / src.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            _set_readonly(target)
            rel, pdf_sha = target.relative_to(ARCHIVE_DIR).as_posix(), sha256_file(target)
            note = "Altbestand (vor Aktivierung des GoBD-Modus erstellt), PDF aus dem Arbeitsordner übernommen"
        else:
            note = "Altbestand (vor Aktivierung des GoBD-Modus erstellt), PDF-Datei war nicht mehr vorhanden"
        snapshot = canonical_json({"legacy": True, "doc_type": row["doc_type"], "doc_number": row["doc_number"],
                                   "doc_date": row["doc_date"], "recipient": row["recipient"],
                                   "total_cents": row["total_cents"], "items": json.loads(row["items_json"] or "[]")})
        db.backfill_legacy(row["id"], rel, pdf_sha, snapshot, sha256_text(snapshot), note)
    return len(rows)


# ------------------------------------------------------------------ Integrität
@dataclass
class IntegrityReport:
    chain_ok: bool
    chain_entries: int
    first_bad_entry: int | None
    documents_checked: int
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.chain_ok and not self.problems


def verify_integrity(db: Database, log: bool = True) -> IntegrityReport:
    chain_ok, entries, bad = db.verify_audit_chain()
    report = IntegrityReport(chain_ok, entries, bad, 0)
    if not chain_ok:
        report.problems.append(f"Änderungsprotokoll: Verkettung ab Eintrag #{bad} beschädigt oder manipuliert.")

    # PDF-Prüfsummen laut Protokoll (unabhängige Gegenprobe zur Belegtabelle)
    logged_sha: dict[str, str] = {}
    for a in db.conn.execute(
            "SELECT entity_id, details FROM audit_log WHERE action IN ('BELEG_ERSTELLT', 'ALTBESTAND_ARCHIVIERT')"):
        sha = json.loads(a["details"]).get("pdf_sha256")
        if sha:
            logged_sha[a["entity_id"]] = sha

    for row in db.conn.execute("SELECT * FROM documents ORDER BY id"):
        report.documents_checked += 1
        label = f"{DOC_LABELS.get(row['doc_type'], row['doc_type'])} {row['doc_number']}"
        if row["snapshot_sha256"] is None:
            report.problems.append(f"{label}: nicht archiviert.")
            continue
        if sha256_text(row["snapshot_json"] or "") != row["snapshot_sha256"]:
            report.problems.append(f"{label}: Belegdaten wurden verändert (Prüfsumme stimmt nicht).")
        if not row["pdf_sha256"]:
            report.notes.append(f"{label}: Altbestand ohne archiviertes PDF.")
            continue
        file = archive_file(row)
        if file is None or not file.is_file():
            report.problems.append(f"{label}: PDF fehlt im Archiv ({row['archive_path']}).")
        elif sha256_file(file) != row["pdf_sha256"]:
            report.problems.append(f"{label}: PDF im Archiv wurde verändert.")
        if logged_sha.get(str(row["id"])) not in (None, row["pdf_sha256"]):
            report.problems.append(f"{label}: Prüfsumme weicht vom Änderungsprotokoll ab.")
    for b in db.conn.execute("SELECT * FROM bookings WHERE receipt_path != '' ORDER BY id"):
        report.documents_checked += 1
        f = ARCHIVE_DIR / b["receipt_path"]
        if not f.is_file():
            report.problems.append(f"Buchung {b['booking_no']}: Beleg fehlt im Archiv ({b['receipt_path']}).")
        elif sha256_file(f) != b["receipt_sha256"]:
            report.problems.append(f"Buchung {b['booking_no']}: Beleg im Archiv wurde verändert.")
    for imp in db.conn.execute("SELECT * FROM bank_imports ORDER BY id"):
        report.documents_checked += 1
        f = ARCHIVE_DIR / imp["archive_path"]
        if not f.is_file():
            report.problems.append(f"Kontoauszug-Import {imp['file_name']}: Datei fehlt im Archiv.")
        elif sha256_file(f) != imp["sha256"]:
            report.problems.append(f"Kontoauszug-Import {imp['file_name']}: Datei im Archiv wurde verändert.")
    if log:
        db.log_event("INTEGRITAETSPRUEFUNG", {"ergebnis": "ok" if report.ok else "FEHLER",
                                              "protokolleintraege": entries, "belege": report.documents_checked,
                                              "probleme": report.problems[:20]})
    return report


# -------------------------------------------------------------- Datensicherung
def create_backup(db: Database, target: Path) -> Path:
    """ZIP mit Datenbank, Belegarchiv (PDFs) und Logo."""
    target = Path(target)
    n_docs = db.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    db.log_event("DATENSICHERUNG_ERSTELLT", {"datei": target.name, "belege": n_docs})
    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / "cnctd_stock.db"
        db.backup_to(tmp_db)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp_db, "cnctd_stock.db")
            for folder, arc_root in ((ARCHIVE_DIR, "archiv"), (ASSETS_DIR, "assets")):
                if folder.is_dir():
                    for f in folder.rglob("*"):
                        if f.is_file():
                            zf.write(f, f"{arc_root}/{f.relative_to(folder).as_posix()}")
            zf.writestr("sicherung_info.json", json.dumps({
                "programm": APP_NAME, "version": APP_VERSION, "erstellt": datetime.now().isoformat(timespec="seconds"),
                "belege": n_docs, "letzter_protokoll_hash": db.last_audit_hash()}, ensure_ascii=False, indent=2))
    return target


def auto_backup(db: Database, reason: str) -> Path:
    AUTO_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return create_backup(db, AUTO_BACKUP_DIR / f"auto_{safe_filename(reason)}_{now_stamp()}.zip")


def restore_backup(db: Database, source: Path) -> str:
    """Spielt eine Sicherung ein. Ältere Stände, die Belege verlieren würden, werden abgelehnt."""
    source = Path(source)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_db = Path(tmp) / "restore.db"
        zf = None
        if zipfile.is_zipfile(source):
            zf = zipfile.ZipFile(source)
            if "cnctd_stock.db" not in zf.namelist():
                raise GoBDError("Die ZIP-Datei enthält keine CNCTD.STOCK-Datenbank.")
            tmp_db.write_bytes(zf.read("cnctd_stock.db"))
        else:
            shutil.copyfile(source, tmp_db)
        try:
            src = sqlite3.connect(tmp_db)
            src.execute("SELECT COUNT(*) FROM articles").fetchone()
            src.execute("SELECT COUNT(*) FROM settings").fetchone()
        except sqlite3.DatabaseError as exc:
            raise GoBDError("Die Datei ist keine gültige CNCTD.STOCK-Sicherung.") from exc

        try:
            current_docs = db.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            if current_docs:
                has_audit = src.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_log'").fetchone()
                contains_current = has_audit and src.execute(
                    "SELECT 1 FROM audit_log WHERE hash = ?", (db.last_audit_hash(),)).fetchone()
                if not contains_current:
                    raise GoBDError(
                        "Die Sicherung ist älter als der aktuelle Datenbestand. Ein Zurückspielen würde "
                        "bereits erstellte Belege bzw. Protokolleinträge entfernen und ist nach GoBD nicht "
                        "zulässig.\n\nWiederherstellen lässt sich nur eine Sicherung, die den aktuellen Stand "
                        "enthält (z. B. nach Datenverlust auf einem neuen Rechner).")
            pre = auto_backup(db, "vor_wiederherstellung")
            src.backup(db.conn)
        finally:
            src.close()

        db._migrate()
        restored_files = 0
        if zf is not None:
            for name in zf.namelist():
                for arc_root, folder in (("archiv/", ARCHIVE_DIR), ("assets/", ASSETS_DIR)):
                    if name.startswith(arc_root) and not name.endswith("/"):
                        dest = folder / name[len(arc_root):]
                        if not dest.exists():  # vorhandene Archivdateien werden nie überschrieben
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_bytes(zf.read(name))
                            if arc_root == "archiv/":
                                _set_readonly(dest)
                            restored_files += 1
            zf.close()

    # Logo-Pfad an den aktuellen Datenordner anpassen (z. B. nach Umzug auf einen anderen PC).
    logo = db.get_settings().get("logo_path") or ""
    if logo and not Path(logo).is_file() and (ASSETS_DIR / Path(logo).name).is_file():
        db.save_settings({"logo_path": str(ASSETS_DIR / Path(logo).name)})
    db.log_event("SICHERUNG_WIEDERHERGESTELLT", {"quelle": source.name, "dateien": restored_files,
                                                 "vorher_gesichert_als": pre.name})
    return f"Wiederhergestellt. Der vorherige Stand wurde zusätzlich gesichert:\n{pre}"


# ------------------------------------------------ Datenexport Betriebsprüfung (Z3)
def _num(cents: int | None) -> str:
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    euros, rest = divmod(abs(cents), 100)
    return f"{sign}{euros},{rest:02d}"


def _qty(q) -> str:
    return f"{float(q or 0):.3f}".replace(".", ",")


def _de(d: str | None) -> str:
    if not d:
        return ""
    return date.fromisoformat(d[:10]).strftime("%d.%m.%Y")


# (Spaltenname, Typ) – Typ: "A" alphanumerisch, "N" Betrag (2 Nachkommastellen), "Q" Menge (3), "D" Datum
EXPORT_TABLES = {
    "belege.csv": ("Belege", "Rechnungen, Stornorechnungen und Lieferscheine", [
        ("Belegart", "A"), ("Belegnummer", "A"), ("Belegdatum", "D"), ("Erfasst_am", "A"), ("Status", "A"),
        ("Storno_Bezug", "A"), ("Stornogrund", "A"), ("Kunden_Nr", "A"), ("Empfaenger", "A"),
        ("Lieferadresse", "A"), ("Versandart", "A"), ("Sendungsnummer", "A"), ("Nettobetrag", "N"),
        ("Umsatzsteuer", "N"), ("Bruttobetrag", "N"), ("Versandkosten", "N"), ("PDF_Datei", "A"),
        ("PDF_SHA256", "A")]),
    "belegpositionen.csv": ("Belegpositionen", "Positionen der Belege", [
        ("Belegart", "A"), ("Belegnummer", "A"), ("Belegdatum", "D"), ("Position", "A"), ("Artikel_Nr", "A"),
        ("Bezeichnung", "A"), ("Menge", "Q"), ("Einheit", "A"), ("Einzelpreis", "N"), ("Gesamtpreis", "N")]),
    "kunden.csv": ("Kunden", "Kundenstamm (aktueller Stand)", [
        ("Kunden_Nr", "A"), ("Rechnungsadresse", "A"), ("Lieferadresse", "A"), ("EMail", "A"),
        ("Telefon", "A"), ("Versandart", "A")]),
    "artikel.csv": ("Artikel", "Artikelstamm mit Bestand (aktueller Stand)", [
        ("Artikel_Nr", "A"), ("Bezeichnung", "A"), ("Einheit", "A"), ("Bestand", "Q"), ("Stueckpreis", "N"),
        ("Warenwert", "N")]),
    "buchungen.csv": ("Buchungen", "Buchungsjournal der Einnahmen-Überschuss-Rechnung (Zahlungsdatum)", [
        ("Buchungsnummer", "A"), ("Zahlungsdatum", "D"), ("Belegdatum", "D"), ("Konto", "A"), ("Kategorie", "A"),
        ("Brutto", "N"), ("Netto", "N"), ("Umsatzsteuer", "N"), ("Steuersatz", "A"), ("Gegenpartei", "A"),
        ("Buchungstext", "A"), ("Rechnung", "A"), ("Status", "A"), ("Storno_Bezug", "A"), ("Beleg_Datei", "A"),
        ("Beleg_SHA256", "A"), ("Erfasst_am", "A")]),
    "bankumsaetze.csv": ("Bankumsaetze", "Importierte Bank- und PayPal-Umsätze mit Buchungszuordnung", [
        ("ID", "A"), ("Konto", "A"), ("Datum", "D"), ("Betrag", "N"), ("Gegenpartei", "A"), ("IBAN", "A"),
        ("Verwendungszweck", "A"), ("Buchungsnummer", "A"), ("Importdatei", "A")]),
    "protokoll.csv": ("Aenderungsprotokoll", "Verkettetes Änderungsprotokoll (SHA-256)", [
        ("Nr", "A"), ("Zeitpunkt", "A"), ("Aktion", "A"), ("Objekt", "A"), ("Objekt_ID", "A"), ("Details", "A"),
        ("Hash_Vorgaenger", "A"), ("Hash", "A")]),
}


def _index_xml(supplier: str, period: tuple[date, date] | None) -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<!DOCTYPE DataSet SYSTEM "gdpdu-01-09-2004.dtd">',
             "<DataSet>", "  <Version>1.0</Version>",
             "  <DataSupplier>", f"    <Name>{escape(supplier)}</Name>",
             "    <Location></Location>",
             f"    <Comment>Export aus {APP_NAME} {APP_VERSION}</Comment>", "  </DataSupplier>",
             "  <Media>", "    <Name>CNCTD.STOCK Export</Name>"]
    for file, (name, desc, cols) in EXPORT_TABLES.items():
        parts += ["    <Table>", f"      <URL>{file}</URL>", f"      <Name>{name}</Name>",
                  f"      <Description>{escape(desc)}</Description>"]
        if period:
            parts += ["      <Validity>", "        <Range>", f"          <From>{period[0]:%d.%m.%Y}</From>",
                      f"          <To>{period[1]:%d.%m.%Y}</To>", "        </Range>",
                      "        <Format>DD.MM.YYYY</Format>", "      </Validity>"]
        parts += ["      <UTF8/>", "      <DecimalSymbol>,</DecimalSymbol>",
                  "      <DigitGroupingSymbol>.</DigitGroupingSymbol>", "      <SkipNumBytes>0</SkipNumBytes>",
                  "      <VariableLength>", "        <ColumnDelimiter>;</ColumnDelimiter>",
                  "        <RecordDelimiter>&#13;&#10;</RecordDelimiter>",
                  '        <TextEncapsulator>"</TextEncapsulator>']
        for i, (col, typ) in enumerate(cols):
            tag = "VariablePrimaryKey" if i == 0 and file not in ("belegpositionen.csv",) else "VariableColumn"
            if typ == "N":
                kind = "<Numeric><Accuracy>2</Accuracy></Numeric>"
            elif typ == "Q":
                kind = "<Numeric><Accuracy>3</Accuracy></Numeric>"
            elif typ == "D":
                kind = "<Date><Format>DD.MM.YYYY</Format></Date>"
            else:
                kind = "<AlphaNumeric/>"
            parts.append(f"        <{tag}><Name>{col}</Name>{kind}</{tag}>")
        parts += ["      </VariableLength>", "    </Table>"]
    parts += ["  </Media>", "</DataSet>", ""]
    return "\n".join(parts)


def _csv_bytes(header: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", quotechar='"', quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    w.writerow(header)
    for r in rows:
        w.writerow(["" if v is None else str(v).replace("\r\n", " | ").replace("\n", " | ") for v in r])
    return buf.getvalue().encode("utf-8")


def export_tax_audit(db: Database, target: Path, year: int | None = None) -> tuple[Path, int]:
    """Datenträgerüberlassung (Z3): CSV-Dateien + index.xml (Beschreibungsstandard) + Beleg-PDFs."""
    target = Path(target)
    docs = db.conn.execute("SELECT * FROM documents ORDER BY doc_date, doc_type, doc_number").fetchall()
    if year:
        docs = [d for d in docs if d["doc_date"].startswith(str(year))]
    by_id = {r["id"]: r for r in db.conn.execute("SELECT id, doc_number FROM documents")}
    customers = {c.id: c for c in db.list_customers()}

    beleg_rows, pos_rows = [], []
    for d in docs:
        ref = ""
        if d["cancels_id"]:
            ref = f"storniert {by_id.get(d['cancels_id'], {'doc_number': '?'})['doc_number']}"
        elif d["cancelled_by_id"]:
            ref = f"storniert durch {by_id.get(d['cancelled_by_id'], {'doc_number': '?'})['doc_number']}"
        cust = customers.get(d["customer_id"])
        pdf_name = f"pdf/{d['archive_path']}" if d["archive_path"] else ""
        beleg_rows.append([
            DOC_LABELS.get(d["doc_type"], d["doc_type"]), d["doc_number"], _de(d["doc_date"]), d["created_at"],
            STATUS_LABELS.get(d["status"], d["status"]), ref, d["cancel_reason"], cust.customer_no if cust else "",
            d["recipient"], d["delivery_address"], d["shipping_method"], d["tracking_no"],
            "" if d["legacy"] else _num(d["net_cents"]), "" if d["legacy"] else _num(d["vat_cents"]),
            _num(d["total_cents"]), _num(d["shipping_cents"]), pdf_name, d["pdf_sha256"]])
        snap = json.loads(d["snapshot_json"] or "{}")
        items = snap.get("items") if snap else json.loads(d["items_json"] or "[]")
        for pos, it in enumerate(items or [], start=1):
            unit_price = int(it.get("unit_price_cents") or 0)
            priced = d["doc_type"] in PRICED_TYPES
            pos_rows.append([
                DOC_LABELS.get(d["doc_type"], d["doc_type"]), d["doc_number"], _de(d["doc_date"]), pos,
                it.get("article_no", ""), it.get("name", ""), _qty(it.get("quantity")), it.get("unit", ""),
                _num(unit_price) if priced else "", _num(line_total_cents(it.get("quantity") or 0, unit_price))
                if priced else ""])

    kunden_rows = [[c.customer_no, c.billing_address, c.delivery_address, c.email, c.phone, c.shipping_method]
                   for c in customers.values()]
    artikel_rows = [[a.article_no, a.name, a.unit, _qty(a.quantity), _num(a.unit_price_cents), _num(a.total_cents)]
                    for a in db.list_articles()]
    db.log_event("DATENEXPORT_BETRIEBSPRUEFUNG", {"datei": target.name, "jahr": year or "alle",
                                                  "belege": len(docs)})
    proto_rows = [[r["id"], r["ts"], r["action"], r["entity"], r["entity_id"], r["details"], r["prev_hash"],
                   r["hash"]] for r in db.conn.execute("SELECT * FROM audit_log ORDER BY id")]

    period = (date(year, 1, 1), date(year, 12, 31)) if year else None
    supplier = db.get_settings().get("sender_name") or APP_NAME
    from .accounting import ACCOUNTS, category_label

    bookings = db.conn.execute("SELECT * FROM bookings ORDER BY booking_date, id").fetchall()
    if year:
        bookings = [b for b in bookings if b["booking_date"].startswith(str(year))]
    numbers = {r["id"]: r["booking_no"] for r in db.conn.execute("SELECT id, booking_no FROM bookings")}
    doc_numbers = {r["id"]: r["doc_number"] for r in by_id.values()}
    buchung_rows = [[
        b["booking_no"], _de(b["booking_date"]), _de(b["receipt_date"]), ACCOUNTS.get(b["account"], b["account"]),
        category_label(b["category"]), _num(b["gross_cents"]), _num(b["net_cents"]), _num(b["vat_cents"]),
        f"{b['vat_rate']:g} %", b["counterparty"], b["text"], doc_numbers.get(b["document_id"], ""),
        STATUS_LABELS.get(b["status"], b["status"]),
        f"storniert {numbers.get(b['cancels_id'], '')}" if b["cancels_id"] else
        (f"storniert durch {numbers.get(b['cancelled_by_id'], '')}" if b["cancelled_by_id"] else ""),
        f"belege_eingang/{b['receipt_path']}" if b["receipt_path"] else "", b["receipt_sha256"] or "",
        b["created_at"]] for b in bookings]
    imports = {r["id"]: r for r in db.conn.execute("SELECT * FROM bank_imports")}
    txs = db.conn.execute("SELECT * FROM bank_transactions ORDER BY tx_date, id").fetchall()
    if year:
        txs = [t for t in txs if t["tx_date"].startswith(str(year))]
    bank_rows = [[t["id"], ACCOUNTS.get(t["account"], t["account"]), _de(t["tx_date"]), _num(t["amount_cents"]),
                  t["counterparty"], t["iban"], t["purpose"], numbers.get(t["booking_id"], ""),
                  f"kontoauszuege/{imports[t['import_id']]['archive_path']}" if t["import_id"] in imports else ""]
                 for t in txs]

    data = {"belege.csv": beleg_rows, "belegpositionen.csv": pos_rows, "kunden.csv": kunden_rows,
            "artikel.csv": artikel_rows, "buchungen.csv": buchung_rows, "bankumsaetze.csv": bank_rows,
            "protokoll.csv": proto_rows}
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for file, rows in data.items():
            header = [c for c, _ in EXPORT_TABLES[file][2]]
            zf.writestr(file, _csv_bytes(header, rows))
        zf.writestr("index.xml", _index_xml(supplier, period))
        for d in docs:
            f = archive_file(d)
            if f and f.is_file():
                zf.write(f, f"pdf/{d['archive_path']}")
        for b in bookings:
            f = ARCHIVE_DIR / b["receipt_path"] if b["receipt_path"] else None
            if f and f.is_file():
                zf.write(f, f"belege_eingang/{b['receipt_path']}")
        for imp_id in {t["import_id"] for t in txs}:
            imp = imports.get(imp_id)
            f = ARCHIVE_DIR / imp["archive_path"] if imp else None
            if f and f.is_file():
                zf.write(f, f"kontoauszuege/{imp['archive_path']}")
        zf.writestr("LIESMICH.txt", (
            f"Datenexport für die Betriebsprüfung (Datenträgerüberlassung, Z3)\r\n"
            f"Erstellt mit {APP_NAME} {APP_VERSION} am {datetime.now():%d.%m.%Y %H:%M}\r\n"
            f"Zeitraum: {year or 'gesamter Datenbestand'}\r\n\r\n"
            "index.xml     Beschreibung der Tabellen nach dem Beschreibungsstandard (GDPdU)\r\n"
            "belege.csv    Belegkopfdaten (Rechnungen, Stornorechnungen, Lieferscheine)\r\n"
            "belegpositionen.csv, kunden.csv, artikel.csv, protokoll.csv\r\n"
            "buchungen.csv Buchungsjournal (Einnahmen-Überschuss-Rechnung), bankumsaetze.csv\r\n"
            "pdf/          Archivierte Original-PDFs; Prüfsumme (SHA-256) je Beleg in belege.csv\r\n"
            "belege_eingang/  Eingangsbelege zu den Buchungen; kontoauszuege/  importierte Originaldateien\r\n\r\n"
            "Zeichensatz UTF-8, Trennzeichen Semikolon, Dezimalzeichen Komma.\r\n"
            "Die DTD gdpdu-01-09-2004.dtd wird vom Prüfprogramm (z. B. IDEA) bereitgestellt.\r\n"))
    return target, len(docs)


# ------------------------------------------------------ Verfahrensdokumentation
# Betriebsspezifische Angaben der Verfahrensdokumentation. Ohne eigene Angaben bleiben die
# Platzhalter [in eckigen Klammern] stehen und werden im Dokument gelb markiert.
PROCEDURE_DEFAULTS: dict = {
    "taetigkeit": "[kurze Beschreibung der Geschäftstätigkeit, z. B. Herstellung und Versand von …]",
    "zustaendig": "[Name der Person, die Belege und Büro verantwortet]",
    "buchhaltung": "[Name der Kanzlei bzw. wer die Buchhaltung führt]",
    "buchhaltung_satz": "Die Finanzbuchhaltung selbst erfolgt [mit … / durch …].",
    "rechnungserstellung": ["[Organisatorisch: Rechnungen werden spätestens … Tage nach Lieferung/Leistung "
                            "erstellt.]"],
    "versand_zahlung": [
        "[Wie werden Rechnungen an Kunden übermittelt (E-Mail, Post, Beilage zur Sendung)?]",
        "[Wie werden Zahlungseingänge kontrolliert und in der Buchhaltung erfasst?]",
        "[Wie werden Eingangsrechnungen (z. B. Material, Versandkosten) aufbewahrt? – nicht Teil dieses "
        "Programms]",
    ],
    "barverkauf": [],
    "rechner": "[z. B. Windows-PC im Büro, Benutzerkonto …]",
    "db_path": None,
    "archive_path": None,
    "pdf_path": None,
    "sicherung": [
        "[Häufigkeit: z. B. wöchentlich sowie nach jedem Rechnungslauf]",
        "[Speicherort: z. B. externe Festplatte/USB-Stick, getrennt vom PC aufbewahrt; zusätzlich …]",
        "[Verantwortlich: …]  [Kontrolle der Wiederherstellbarkeit: z. B. jährlich]",
    ],
    "zugriff": [
        "[Wer hat Zugriff auf den PC und das Programm? Benutzerkonto mit Passwort, Bildschirmsperre …]",
        "[Virenschutz und Betriebssystem-Updates: …]",
    ],
    "iks": [
        "[Regelmäßige Kontrolle: z. B. monatlich „Integrität prüfen“ ausführen und Ergebnis ablegen]",
        "[Abgleich der Rechnungen mit Zahlungseingängen und Buchhaltung: …]",
        "[Vier-Augen-Prinzip / Freigaben, falls mehrere Personen beteiligt sind: …]",
    ],
    "hinweise": [],
    "historie_name": "[Name]",
}


def write_procedure_documentation(db: Database, target: Path, details: dict | None = None,
                                  log: bool = True) -> Path:
    """Erzeugt eine vorausgefüllte Verfahrensdokumentation (Word) nach GoBD Rz. 151 ff."""
    info = {**PROCEDURE_DEFAULTS, **(details or {})}
    from docx import Document
    from docx.enum.text import WD_COLOR_INDEX
    from docx.shared import Pt, RGBColor

    s = db.get_settings()
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(10.5)

    def _highlighted(p, text: str, bold: bool = False):
        """Text in [eckigen Klammern] wird gelb markiert (vom Unternehmen auszufüllen)."""
        for chunk in text.replace("]", "]\x00").replace("[", "\x00[").split("\x00"):
            if not chunk:
                continue
            run = p.add_run(chunk)
            run.bold = bold
            if chunk.startswith("["):
                run.font.highlight_color = WD_COLOR_INDEX.YELLOW
        return p

    def para(text: str, bold: bool = False):
        return _highlighted(doc.add_paragraph(), text, bold)

    def bullets(items: list[str]):
        for item in items:
            _highlighted(doc.add_paragraph(style="List Bullet"), item)

    def table(rows: list[tuple[str, str]]):
        t = doc.add_table(rows=0, cols=2)
        t.style = "Light Grid Accent 1"
        for k, v in rows:
            cells = t.add_row().cells
            cells[0].text = k
            _highlighted(cells[1].paragraphs[0], v or "[bitte ergänzen]")
        doc.add_paragraph()

    company = s.get("sender_name") or "[Firmenname]"
    title = doc.add_heading("Verfahrensdokumentation", level=0)
    title.runs[0].font.color.rgb = RGBColor(0x1D, 0x4E, 0xD8)
    para(f"zur Erstellung, Archivierung und Aufbewahrung von Rechnungen, Stornorechnungen und Lieferscheinen "
         f"mit {APP_NAME} – {company}")
    para(f"Stand: {date.today():%d.%m.%Y} · Programmversion {APP_VERSION} · Dokumentversion 1.0")
    para("Hinweis: Gelb markierte Stellen sind vom Unternehmen zu ergänzen bzw. zu prüfen. Die "
         "Verfahrensdokumentation ist bei Änderungen am Verfahren fortzuschreiben und wie die Belege "
         "aufzubewahren.")
    if info["hinweise"]:
        doc.add_heading("Offene Punkte", level=2)
        bullets(info["hinweise"])

    doc.add_heading("1. Allgemeine Beschreibung", level=1)
    table([
        ("Unternehmen", company),
        ("Inhaber / Verantwortliche", s.get("sender_owner")),
        ("Anschrift", ", ".join(x for x in [s.get("sender_street"), s.get("sender_zip_city"),
                                            s.get("sender_country")] if x)),
        ("Steuernummer / USt-IdNr.", " / ".join(x for x in [s.get("tax_number"), s.get("vat_id")] if x)),
        ("Umsatzsteuer", "Kleinunternehmer nach § 19 UStG" if s.get("small_business")
         else f"Regelbesteuerung, Standardsatz {s.get('vat_rate')} %"),
        ("Tätigkeit", info["taetigkeit"]),
        ("Zuständig für Büro und Belege", info["zustaendig"]),
        ("Steuerberatung / Buchhaltung", info["buchhaltung"]),
    ])
    para("Gegenstand dieser Dokumentation ist das Verfahren zur Fakturierung (Rechnungen, Stornorechnungen), zur "
         "Erstellung von Lieferscheinen, zur Bestandsführung sowie zur elektronischen Archivierung dieser Belege "
         f"mit der Software {APP_NAME}. " + info["buchhaltung_satz"])

    doc.add_heading("2. Anwenderdokumentation (Ablauf)", level=1)
    para("2.1 Stammdaten", bold=True)
    bullets([
        "Artikel werden im Bereich „Bestand“ angelegt, geändert oder gelöscht. Jede Änderung (inkl. Bestandsmenge "
        "und Preis) wird mit Zeitpunkt sowie altem und neuem Wert im Änderungsprotokoll festgehalten.",
        "Kunden werden im Bereich „Kunden“ gepflegt; Änderungen werden ebenfalls protokolliert.",
        "Absenderdaten, Steuerangaben, Versandarten und Nummernkreise werden unter „Einstellungen“ gepflegt; "
        "jede Änderung wird protokolliert.",
    ])
    para("2.2 Erstellung von Belegen", bold=True)
    bullets([
        "Im Bereich „Dokumente erstellen“ werden Kunde, Positionen, Versandart und -kosten erfasst.",
        "Mit „PDF erstellen“ vergibt das Programm automatisch die nächste fortlaufende Belegnummer "
        f"(Rechnungen {s.get('invoice_prefix')}-JJJJ-NNNN, Stornorechnungen {s.get('cancel_prefix')}-JJJJ-NNNN, "
        f"Lieferscheine {s.get('delivery_prefix')}-JJJJ-NNNN). Eine manuelle Nummernvergabe ist nicht möglich; "
        "die Zähler können nur erhöht werden (jede Erhöhung wird protokolliert).",
        "Vorschauen sind mit „ENTWURF – kein gültiger Beleg“ gekennzeichnet, erhalten keine Belegnummer und "
        "werden nicht archiviert.",
        "Optional wird der Lagerbestand beim Erstellen abgebucht; jede Bestandsbuchung wird protokolliert.",
        "Belegdatum und Erfassungszeitpunkt werden getrennt gespeichert (zeitgerechte Erfassung).",
        *info["rechnungserstellung"],
    ])
    para("2.3 Korrekturen und Stornierungen", bold=True)
    bullets([
        "Erstellte Belege können weder geändert noch gelöscht werden.",
        "Fehlerhafte Rechnungen werden im Dokumentenarchiv storniert. Dabei entsteht eine eigene Stornorechnung "
        "mit negativen Beträgen und Verweis auf die Originalrechnung; der Stornogrund wird erfasst. Anschließend "
        "wird bei Bedarf eine neue, korrigierte Rechnung erstellt.",
        "Lieferscheine werden als storniert gekennzeichnet (mit Grund). Abgebuchte Bestände können dabei "
        "zurückgebucht werden.",
        "Sendungsnummern können nachträglich im Archiv vermerkt werden; dies wird protokolliert, das archivierte "
        "PDF bleibt unverändert.",
    ])
    para("2.4 Übermittlung, Zahlung und Eingangsbelege", bold=True)
    bullets(info["versand_zahlung"])
    if info["barverkauf"]:
        para("2.5 Direktverkauf und Barzahlungen", bold=True)
        bullets(info["barverkauf"])

    if s.get("accounting_enabled"):
        para("2.6 Buchhaltung (Einnahmen-Überschuss-Rechnung)", bold=True)
        bullets([
            "Alle Zahlungsvorgänge werden im Bereich „Buchhaltung“ als Buchungen erfasst. Jede Buchung erhält "
            "eine fortlaufende Nummer (B-JJJJ-NNNN) und ist nach dem Speichern unveränderbar; Korrekturen "
            "erfolgen über eine Stornobuchung mit Grund.",
            "Maßgeblich ist das Zahlungsdatum (Zufluss-/Abflussprinzip). Zusätzlich wird das Belegdatum erfasst.",
            "Eingangsbelege (PDF oder Foto) werden der Buchung zugeordnet und schreibgeschützt mit Prüfsumme "
            "archiviert. Buchungen ohne Beleg sind die Ausnahme und werden mit einem Eigenbeleg dokumentiert "
            "[Vorgehen für Eigenbelege festlegen].",
            "Zahlungseingänge werden den Rechnungen zugeordnet (offene Posten). Kontoauszüge werden als CSV- "
            "oder CAMT-Datei importiert; die Originaldatei wird archiviert, doppelte Umsätze werden erkannt. "
            "Jeder Umsatz wird einer Buchung zugeordnet (Rechnung, Ausgabe, Privatentnahme/-einlage oder "
            "Geldtransit).",
            "Kassenbuch: Bareinnahmen und -ausgaben werden täglich im Konto „Kasse“ erfasst; das Programm "
            "verhindert einen negativen Kassenbestand. Bargeldbewegungen zwischen Bank und Kasse werden als "
            "Geldtransit gebucht. [Kassensturz: z. B. täglich bei Verkaufstagen, Ergebnis notieren]",
            "Die Auswertung „EÜR“ fasst die Buchungen eines Jahres nach den Positionen der Anlage EÜR zusammen. "
            "Die Werte werden manuell in „Mein ELSTER“ übertragen; die Auswertung wird als PDF abgelegt.",
        ])

    doc.add_heading("3. Technische Systemdokumentation", level=1)
    table([
        ("Software", f"{APP_NAME} Version {APP_VERSION} (Windows-Desktopanwendung)"),
        ("Datenhaltung", "SQLite-Datenbank (eine Datei), keine Cloud, keine Übermittlung an Dritte"),
        ("Speicherort Datenbank", info["db_path"] or str(db.path)),
        ("Belegarchiv (Original-PDFs)", info["archive_path"] or str(ARCHIVE_DIR)),
        ("Arbeitskopien der PDFs", info["pdf_path"] or s.get("pdf_output_dir")),
        ("Rechner / Betriebssystem", info["rechner"]),
    ])
    para("Maßnahmen zur Unveränderbarkeit und Nachvollziehbarkeit:", bold=True)
    bullets([
        "Datenbank-Trigger verhindern das Löschen von Belegen und jede inhaltliche Änderung erstellter Belege.",
        "Zu jedem Beleg werden das Original-PDF im Archiv (schreibgeschützt) sowie ein vollständiger Datensatz "
        "aller Belegangaben gespeichert; für beides wird eine SHA-256-Prüfsumme hinterlegt.",
        "Alle Änderungen an Artikeln, Beständen, Kunden, Einstellungen und Belegen werden in einem "
        "Änderungsprotokoll erfasst. Jeder Eintrag enthält die Prüfsumme seines Vorgängers (Hash-Kette); "
        "Protokolleinträge können nicht geändert oder gelöscht werden.",
        "Die Funktion „Integrität prüfen“ kontrolliert Hash-Kette, Belegdaten und PDF-Prüfsummen und meldet "
        "jede Abweichung. Die Prüfung selbst wird protokolliert.",
        "Das Wiederherstellen einer Datensicherung ist nur möglich, wenn diese den aktuellen Datenstand "
        "vollständig enthält (kein Zurücksetzen auf ältere Stände). Vor jeder Wiederherstellung wird der "
        "aktuelle Stand automatisch gesichert.",
        "Grenze: Die Sicherungen schützen gegen versehentliche Änderungen und machen Manipulationen erkennbar. "
        "Gegen vorsätzliche Eingriffe mit Administratorrechten auf Dateiebene schützen organisatorische "
        "Maßnahmen (Zugriffsschutz, externe Datensicherung, siehe Abschnitt 4 und 5).",
    ])
    para("Datenzugriff der Finanzverwaltung:", bold=True)
    bullets([
        "Unmittelbarer/mittelbarer Zugriff (Z1/Z2): Nur-Lese-Einsicht in Programm, Archiv und Protokoll am "
        "Arbeitsplatz.",
        "Datenträgerüberlassung (Z3): Einstellungen → „Datenexport Betriebsprüfung“ erzeugt ein ZIP mit "
        "CSV-Dateien, index.xml nach dem Beschreibungsstandard und allen Beleg-PDFs.",
    ])

    doc.add_heading("4. Betriebsdokumentation", level=1)
    para("4.1 Datensicherung", bold=True)
    bullets([
        "Die Datensicherung (Einstellungen → „Datensicherung erstellen“) umfasst Datenbank, Belegarchiv und Logo "
        "in einer ZIP-Datei.",
        *info["sicherung"],
    ])
    para("4.2 Aufbewahrung", bold=True)
    bullets([
        "Rechnungen und Stornorechnungen (Buchungsbelege): 8 Jahre (§ 147 AO, § 14b UStG, Stand 2025).",
        "Lieferscheine: soweit keine Buchungsbelege, bis zum Erhalt bzw. Versand der Rechnung; das Programm "
        "bewahrt sie dennoch dauerhaft auf.",
        "Datenbank und Protokoll werden mindestens für die längste Frist der enthaltenen Belege aufbewahrt. "
        "Die Frist beginnt mit Ablauf des Kalenderjahres, in dem der Beleg entstanden ist.",
        "Bei einem Wechsel der Software bleiben Datenbank, Archiv und Export für die Aufbewahrungsdauer "
        "lesbar zu halten.",
    ])
    para("4.3 Zugriffsschutz", bold=True)
    bullets(info["zugriff"])

    doc.add_heading("5. Internes Kontrollsystem (IKS)", level=1)
    bullets([
        "Programmseitig: automatische Nummernvergabe, Lösch- und Änderungssperre, Pflichtfelder "
        "(Empfänger), Warnung bei unplausiblem Belegdatum, Warnung bei unzureichendem Bestand.",
        *info["iks"],
    ])

    doc.add_heading("6. Änderungshistorie dieser Dokumentation", level=1)
    t = doc.add_table(rows=1, cols=3)
    t.style = "Light Grid Accent 1"
    t.rows[0].cells[0].text, t.rows[0].cells[1].text, t.rows[0].cells[2].text = "Datum", "Änderung", "Name"
    cells = t.add_row().cells
    cells[0].text, cells[1].text, cells[2].text = f"{date.today():%d.%m.%Y}", \
        f"Erstfassung (Programmversion {APP_VERSION})", info["historie_name"]

    doc.save(target)
    if log:
        db.log_event("VERFAHRENSDOKUMENTATION_ERSTELLT", {"datei": Path(target).name})
    return Path(target)
