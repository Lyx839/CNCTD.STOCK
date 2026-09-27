"""Buchhaltung (Einnahmen-Überschuss-Rechnung): Buchungsjournal, Eingangsbelege, offene Posten,
Bankabgleich (CSV/CAMT-Import), Kassenbuch und EÜR-Auswertung.

Grundsätze (GoBD):
* Jede Buchung erhält eine fortlaufende Nummer (B-JJJJ-NNNN) und ist nach dem Speichern unveränderbar.
  Korrekturen erfolgen über eine Stornobuchung mit umgekehrtem Vorzeichen.
* Belege (PDF/Bild) und importierte Kontoauszüge werden schreibgeschützt archiviert und per SHA-256 gesichert.
* Die EÜR folgt dem Zufluss-/Abflussprinzip: maßgeblich ist das Zahlungsdatum (booking_date).
* Der Kassenbestand darf zu keinem Zeitpunkt negativ werden.
"""

import csv
import hashlib
import io
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .database import DOC_INVOICE, STATUS_CANCELLED, STATUS_VALID, Database, GoBDError
from .formatting import fmt_money
from .gobd import ARCHIVE_DIR, _set_readonly, _unique, sha256_file
from .pdf_engine import safe_filename

ACCOUNTS = {"BANK": "Bank", "KASSE": "Kasse", "PAYPAL": "PayPal", "OHNE": "ohne Zahlung"}
IMPORT_ACCOUNTS = ("BANK", "PAYPAL")

INCOME, EXPENSE, NEUTRAL = "E", "A", "N"


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    kind: str                 # E = Betriebseinnahme, A = Betriebsausgabe, N = nicht EÜR-wirksam
    only: str = ""            # "KU" nur Kleinunternehmer, "UST" nur Regelbesteuerung


CATEGORIES: dict[str, Category] = {c.key: c for c in [
    Category("einnahmen_ku", "Betriebseinnahmen als Kleinunternehmer (§ 19 UStG)", INCOME, "KU"),
    Category("einnahmen_ust", "Umsatzsteuerpflichtige Betriebseinnahmen", INCOME, "UST"),
    Category("einnahmen_steuerfrei", "Umsatzsteuerfreie / nicht steuerbare Betriebseinnahmen", INCOME, "UST"),
    Category("einnahmen_sonstige", "Sonstige Betriebseinnahmen (z. B. Erstattungen)", INCOME),
    Category("ust_erstattung", "Vom Finanzamt erstattete Umsatzsteuer", INCOME, "UST"),
    Category("wareneinkauf", "Waren, Rohstoffe und Hilfsstoffe (Material, Verpackung)", EXPENSE),
    Category("fremdleistungen", "Bezogene Fremdleistungen", EXPENSE),
    Category("gwg", "Geringwertige Wirtschaftsgüter (bis 800 € netto)", EXPENSE),
    Category("afa", "Absetzung für Abnutzung (AfA)", EXPENSE),
    Category("miete", "Raumkosten (Miete, Nebenkosten)", EXPENSE),
    Category("telekommunikation", "Telekommunikation (Telefon, Internet)", EXPENSE),
    Category("porto", "Porto und Versandkosten", EXPENSE),
    Category("werbung", "Werbekosten", EXPENSE),
    Category("gebuehren", "Gebühren (Marktplatz, Zahlungsdienste, Kontoführung)", EXPENSE),
    Category("software", "Software, Lizenzen, Hosting", EXPENSE),
    Category("buero", "Bürobedarf", EXPENSE),
    Category("fortbildung", "Fortbildung und Fachliteratur", EXPENSE),
    Category("beratung", "Rechts- und Steuerberatung, Buchführung", EXPENSE),
    Category("versicherung", "Betriebliche Versicherungen und Beiträge", EXPENSE),
    Category("kfz", "Kfz- und Fahrtkosten", EXPENSE),
    Category("reise", "Reisekosten", EXPENSE),
    Category("bewirtung", "Bewirtungsaufwendungen (70 % abziehbar)", EXPENSE),
    Category("ust_zahlung", "An das Finanzamt gezahlte Umsatzsteuer", EXPENSE, "UST"),
    Category("sonstige_ausgaben", "Übrige Betriebsausgaben", EXPENSE),
    Category("privatentnahme", "Privatentnahme", NEUTRAL),
    Category("privateinlage", "Privateinlage", NEUTRAL),
    Category("geldtransit", "Geldtransit (zwischen eigenen Konten / Kasse)", NEUTRAL),
    Category("anfangsbestand", "Anfangsbestand", NEUTRAL),
]}

GWG_LIMIT_CENTS = 80_000          # 800 € netto
BEWIRTUNG_DEDUCTIBLE = Decimal("0.7")
KU_LIMIT_PREV_YEAR = 2_500_000    # 25.000 € (Stand 2025)
KU_LIMIT_CURRENT_YEAR = 10_000_000  # 100.000 € (Stand 2025)


def categories_for(kind: str, small_business: bool, include_private: bool = True) -> list[Category]:
    """Kategorien für Einnahmen bzw. Ausgaben passend zur Besteuerungsart."""
    result = []
    for c in CATEGORIES.values():
        if c.only == "KU" and not small_business or c.only == "UST" and small_business:
            continue
        if c.kind == kind:
            result.append(c)
    if include_private:
        result.append(CATEGORIES["privateinlage" if kind == INCOME else "privatentnahme"])
    return result


def default_income_category(small_business: bool) -> str:
    return "einnahmen_ku" if small_business else "einnahmen_ust"


def category_label(key: str) -> str:
    return CATEGORIES[key].label if key in CATEGORIES else key


# ------------------------------------------------------------------------------ helpers
def _round(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def split_gross(gross_cents: int, vat_rate: float) -> tuple[int, int]:
    """Brutto -> (netto, USt)."""
    if not vat_rate:
        return gross_cents, 0
    net = _round(Decimal(gross_cents) * 100 / (Decimal(100) + Decimal(str(vat_rate))))
    return net, gross_cents - net


def _now_sql() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def next_booking_no(db: Database, year: int) -> str:
    prefix = f"B-{year}-"
    row = db.conn.execute("SELECT booking_no FROM bookings WHERE booking_no LIKE ? ORDER BY booking_no DESC LIMIT 1",
                          (prefix + "%",)).fetchone()
    n = int(row["booking_no"].rsplit("-", 1)[-1]) + 1 if row else 1
    return f"{prefix}{n:04d}"


def is_enabled(db: Database) -> bool:
    return bool(db.get_settings().get("accounting_enabled"))


# ------------------------------------------------------------------------------ buchen
@dataclass
class BookingInput:
    booking_date: date
    account: str
    category: str
    gross_cents: int                  # + Zufluss, - Abfluss
    vat_rate: float = 0.0
    counterparty: str = ""
    text: str = ""
    receipt_date: date | None = None
    receipt_file: Path | None = None
    document_id: int | None = None
    bank_tx_id: int | None = None


def _archive_receipt(src: Path, booking_no: str, year: int) -> tuple[str, str]:
    target = _unique(ARCHIVE_DIR / str(year) / "Eingangsbelege" /
                     f"{booking_no}_{safe_filename(src.stem)[:60]}{src.suffix.lower()}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, target)
    _set_readonly(target)
    return target.relative_to(ARCHIVE_DIR).as_posix(), sha256_file(target)


def _check_cash(db: Database) -> None:
    balance = 0
    for row in db.conn.execute(
            "SELECT booking_date, gross_cents FROM bookings WHERE account = 'KASSE' ORDER BY booking_date, id"):
        balance += row["gross_cents"]
        if balance < 0:
            raise GoBDError(
                f"Der Kassenbestand wäre am {date.fromisoformat(row['booking_date']):%d.%m.%Y} negativ "
                f"({fmt_money(balance)}). Ein negativer Kassenbestand ist nicht möglich – bitte Datum prüfen "
                "oder zuerst den Anfangsbestand bzw. die Kasseneinnahmen erfassen.")


def _insert_booking(db: Database, b: BookingInput, *, cancels_id: int | None = None,
                    receipt: tuple[str, str, str] | None = None) -> int:
    """Fügt eine Buchung ein (ohne Transaktionssteuerung)."""
    if b.category not in CATEGORIES:
        raise GoBDError(f"Unbekannte Kategorie: {b.category}")
    if b.account not in ACCOUNTS:
        raise GoBDError(f"Unbekanntes Konto: {b.account}")
    if b.gross_cents == 0:
        raise GoBDError("Der Betrag darf nicht 0 sein.")
    net, vat = split_gross(b.gross_cents, b.vat_rate)
    number = next_booking_no(db, b.booking_date.year)
    rel, sha, original = receipt or ("", None, "")
    cur = db.conn.execute(
        """INSERT INTO bookings (booking_no, booking_date, receipt_date, account, category, gross_cents,
               net_cents, vat_cents, vat_rate, counterparty, text, document_id, bank_tx_id, receipt_path,
               receipt_sha256, receipt_original_name, status, cancels_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (number, b.booking_date.isoformat(), b.receipt_date.isoformat() if b.receipt_date else None, b.account,
         b.category, b.gross_cents, net, vat, b.vat_rate, b.counterparty.strip(), b.text.strip(), b.document_id,
         b.bank_tx_id, rel, sha, original, STATUS_VALID, cancels_id, _now_sql()))
    booking_id = cur.lastrowid
    if b.bank_tx_id:
        updated = db.conn.execute(
            "UPDATE bank_transactions SET booking_id = ? WHERE id = ? AND booking_id IS NULL",
            (booking_id, b.bank_tx_id)).rowcount
        if not updated:
            raise GoBDError("Dieser Bankumsatz ist bereits gebucht.")
    db._audit("BUCHUNG_ERSTELLT", "buchung", booking_id, {
        "nummer": number, "datum": b.booking_date.isoformat(), "konto": b.account, "kategorie": b.category,
        "brutto_cent": b.gross_cents, "ust_cent": vat, "gegenpartei": b.counterparty.strip(),
        "text": b.text.strip(), "beleg": rel, "beleg_sha256": sha, "rechnung_id": b.document_id,
        "bankumsatz_id": b.bank_tx_id, "storno_zu": cancels_id})
    if b.account == "KASSE":
        _check_cash(db)
    return booking_id


def create_booking(db: Database, b: BookingInput) -> int:
    receipt = None
    archived: Path | None = None
    if b.receipt_file:
        src = Path(b.receipt_file)
        if not src.is_file():
            raise GoBDError(f"Beleg nicht gefunden: {src}")
        # Die Nummer wird hier nur für den Dateinamen reserviert; eingefügt wird in derselben Sitzung.
        rel, sha = _archive_receipt(src, next_booking_no(db, b.booking_date.year), b.booking_date.year)
        archived = ARCHIVE_DIR / rel
        receipt = (rel, sha, src.name)
    try:
        with db.conn:
            return _insert_booking(db, b, receipt=receipt)
    except Exception:
        if archived and archived.exists():
            archived.chmod(0o666)
            archived.unlink(missing_ok=True)
        raise


def create_transit(db: Database, booking_date: date, from_account: str, to_account: str, amount_cents: int,
                   text: str = "", bank_tx_id: int | None = None) -> tuple[int, int]:
    """Geldtransit (z. B. Abhebung Bank -> Kasse) als Buchungspaar in einer Transaktion."""
    if amount_cents <= 0:
        raise GoBDError("Der Betrag muss größer als 0 sein.")
    if from_account == to_account:
        raise GoBDError("Quell- und Zielkonto müssen verschieden sein.")
    label = f"Geldtransit {ACCOUNTS[from_account]} → {ACCOUNTS[to_account]}"
    with db.conn:
        out_id = _insert_booking(db, BookingInput(
            booking_date, from_account, "geldtransit", -amount_cents, text=text or label,
            bank_tx_id=bank_tx_id if from_account in IMPORT_ACCOUNTS else None))
        in_id = _insert_booking(db, BookingInput(
            booking_date, to_account, "geldtransit", amount_cents, text=text or label,
            bank_tx_id=bank_tx_id if to_account in IMPORT_ACCOUNTS and from_account not in IMPORT_ACCOUNTS
            else None))
    return out_id, in_id


def get_booking(db: Database, booking_id: int):
    return db.conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()


def cancel_booking(db: Database, booking_id: int, reason: str) -> int:
    """Storniert eine Buchung durch eine Gegenbuchung mit umgekehrtem Vorzeichen."""
    orig = get_booking(db, booking_id)
    if orig is None:
        raise GoBDError("Buchung nicht gefunden.")
    if orig["status"] == STATUS_CANCELLED:
        raise GoBDError("Die Buchung ist bereits storniert.")
    if orig["cancels_id"]:
        raise GoBDError("Eine Stornobuchung kann nicht erneut storniert werden.")
    if not reason.strip():
        raise GoBDError("Bitte einen Grund für die Stornierung angeben.")
    # Storno frühestens am Datum der Originalbuchung (sonst entstünde zwischenzeitlich ein falscher Bestand).
    reversal = BookingInput(
        booking_date=max(date.today(), date.fromisoformat(orig["booking_date"])), account=orig["account"], category=orig["category"],
        gross_cents=-orig["gross_cents"], vat_rate=orig["vat_rate"], counterparty=orig["counterparty"],
        text=f"Storno {orig['booking_no']}: {reason.strip()}", document_id=orig["document_id"])
    with db.conn:
        new_id = _insert_booking(db, reversal, cancels_id=booking_id)
        db.conn.execute("UPDATE bookings SET status = ?, cancel_reason = ?, cancelled_by_id = ? WHERE id = ?",
                        (STATUS_CANCELLED, reason.strip(), new_id, booking_id))
        if orig["bank_tx_id"]:
            # Bankumsatz wird wieder frei und kann neu gebucht werden.
            db.conn.execute("UPDATE bank_transactions SET booking_id = NULL WHERE id = ? AND booking_id = ?",
                            (orig["bank_tx_id"], booking_id))
        db._audit("BUCHUNG_STORNIERT", "buchung", booking_id, {
            "nummer": orig["booking_no"], "grund": reason.strip(),
            "stornobuchung": get_booking(db, new_id)["booking_no"]})
    return new_id


def list_bookings(db: Database, year: int | None = None, account: str | None = None) -> list:
    sql, args = "SELECT * FROM bookings WHERE 1 = 1", []
    if year:
        sql += " AND substr(booking_date, 1, 4) = ?"
        args.append(str(year))
    if account:
        sql += " AND account = ?"
        args.append(account)
    return db.conn.execute(sql + " ORDER BY booking_date DESC, id DESC", args).fetchall()


def booking_years(db: Database) -> list[int]:
    years = {int(r[0]) for r in db.conn.execute("SELECT DISTINCT substr(booking_date, 1, 4) FROM bookings")}
    years |= {int(r[0]) for r in db.conn.execute("SELECT DISTINCT substr(doc_date, 1, 4) FROM documents")}
    years.add(date.today().year)
    return sorted(years, reverse=True)


def account_balance(db: Database, account: str, until: date | None = None) -> int:
    sql, args = "SELECT COALESCE(SUM(gross_cents), 0) FROM bookings WHERE account = ?", [account]
    if until:
        sql += " AND booking_date <= ?"
        args.append(until.isoformat())
    return db.conn.execute(sql, args).fetchone()[0]


def cash_book(db: Database, year: int) -> tuple[int, list[dict]]:
    """Kassenbuch eines Jahres: (Anfangsbestand zum 1.1., Zeilen mit laufendem Bestand)."""
    opening = db.conn.execute(
        "SELECT COALESCE(SUM(gross_cents), 0) FROM bookings WHERE account = 'KASSE' AND booking_date < ?",
        (f"{year}-01-01",)).fetchone()[0]
    rows, balance = [], opening
    for r in db.conn.execute(
            "SELECT * FROM bookings WHERE account = 'KASSE' AND substr(booking_date, 1, 4) = ? "
            "ORDER BY booking_date, id", (str(year),)):
        balance += r["gross_cents"]
        rows.append({"row": r, "balance": balance})
    return opening, rows


# ------------------------------------------------------------------------ offene Posten
@dataclass
class OpenItem:
    doc_id: int
    number: str
    doc_date: date
    customer: str
    total_cents: int
    paid_cents: int
    status: str

    @property
    def open_cents(self) -> int:
        due = self.total_cents if self.status != STATUS_CANCELLED else 0
        return due - self.paid_cents


def invoice_items(db: Database, only_open: bool = True) -> list[OpenItem]:
    paid = {r[0]: r[1] for r in db.conn.execute(
        "SELECT document_id, SUM(gross_cents) FROM bookings WHERE document_id IS NOT NULL GROUP BY document_id")}
    items = []
    for d in db.conn.execute("SELECT * FROM documents WHERE doc_type = ? ORDER BY doc_date, id", (DOC_INVOICE,)):
        recipient = (d["recipient"] or "").strip().splitlines()
        item = OpenItem(d["id"], d["doc_number"], date.fromisoformat(d["doc_date"]),
                        recipient[0] if recipient else "", d["total_cents"], paid.get(d["id"], 0), d["status"])
        if not only_open or item.open_cents != 0:
            items.append(item)
    return items


def open_item(db: Database, doc_id: int) -> OpenItem | None:
    return next((i for i in invoice_items(db, only_open=False) if i.doc_id == doc_id), None)


def record_invoice_payment(db: Database, doc_id: int, pay_date: date, account: str, amount_cents: int,
                           bank_tx_id: int | None = None) -> int:
    """Zahlungseingang (oder bei negativem Betrag: Erstattung) zu einer Rechnung buchen."""
    doc = db.get_document(doc_id)
    if doc is None or doc["doc_type"] != DOC_INVOICE:
        raise GoBDError("Rechnung nicht gefunden.")
    settings = db.get_settings()
    small = bool(settings.get("small_business"))
    # USt-Anteil entsprechend der Rechnung aufteilen (bei Regelbesteuerung).
    rate = 0.0
    if not small and doc["net_cents"]:
        rate = round((doc["total_cents"] / doc["net_cents"] - 1) * 100, 2)
    recipient = (doc["recipient"] or "").strip().splitlines()
    text = f"{'Zahlung' if amount_cents > 0 else 'Erstattung'} Rechnung {doc['doc_number']}"
    return create_booking(db, BookingInput(
        pay_date, account, default_income_category(small), amount_cents, rate,
        counterparty=recipient[0] if recipient else "", text=text, document_id=doc_id, bank_tx_id=bank_tx_id))


# ------------------------------------------------------------------------ Bankimport
@dataclass
class BankTx:
    tx_date: date
    amount_cents: int
    counterparty: str = ""
    purpose: str = ""
    iban: str = ""


def parse_amount(text: str) -> int:
    """'1.234,56' / '-1234.56' / '12,00 EUR' / '5,00 S' -> Cent."""
    t = (text or "").strip().replace(" ", "").replace(" ", "")
    t = re.sub(r"(EUR|€)$", "", t, flags=re.I)
    negative = False
    if t.endswith(("S", "s")) and not t[:-1].isalpha():   # Soll-Kennzeichen
        negative, t = True, t[:-1]
    elif t.endswith(("H", "h")):
        t = t[:-1]
    if t.endswith("-"):
        negative, t = True, t[:-1]
    if t.startswith("-"):
        negative, t = not negative, t[1:]
    elif t.startswith("+"):
        t = t[1:]
    if "," in t and "." in t:
        if t.rfind(",") > t.rfind("."):
            t = t.replace(".", "").replace(",", ".")
        else:
            t = t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".")
    elif t.count(".") == 1 and len(t.split(".")[1]) != 2:
        t = t.replace(".", "")   # Tausendertrennzeichen
    elif t.count(".") > 1:
        t = t.replace(".", "")
    if not re.fullmatch(r"\d+(\.\d+)?", t):
        raise ValueError(f"Kein Betrag: {text!r}")
    cents = _round(Decimal(t) * 100)
    return -cents if negative else cents


def parse_date(text: str) -> date:
    t = (text or "").strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(t[:10] if fmt == "%Y-%m-%d" else t, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Kein Datum: {text!r}")


COLUMN_KEYWORDS = {
    "date": ["buchungstag", "buchungsdatum", "datum", "booking date", "date", "wertstellung", "valuta"],
    "amount": ["betrag (eur)", "betrag", "umsatz", "brutto", "amount"],
    "debit": ["soll"],
    "credit": ["haben"],
    "counterparty": ["beguenstigter/zahlungspflichtiger", "begünstigter/zahlungspflichtiger",
                     "name zahlungsbeteiligter", "zahlungsempfänger", "zahlungspflichtiger", "auftraggeber/empfänger",
                     "empfänger/auftraggeber", "auftraggeber", "empfänger", "name", "payee", "gegenseite"],
    "purpose": ["verwendungszweck", "purpose", "beschreibung", "buchungstext", "hinweis", "betreff"],
    "iban": ["iban zahlungsbeteiligter", "kontonummer/iban", "iban", "gegenkonto"],
}


def _norm(h: str) -> str:
    return (h or "").strip().strip('"').lower()


def read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    """Liest eine Bank-CSV und sucht die Kopfzeile (Banken schreiben oft Infozeilen davor)."""
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    sample = "\n".join(text.splitlines()[:40])
    delimiter = max([";", ",", "\t"], key=sample.count)
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    for i, row in enumerate(rows[:40]):
        cells = [_norm(c) for c in row]
        has_date = any(any(k == c or c.startswith(k) for k in COLUMN_KEYWORDS["date"]) for c in cells)
        has_amount = any(any(k in c for k in COLUMN_KEYWORDS["amount"] + ["soll", "haben"]) for c in cells)
        if has_date and has_amount and len(row) >= 3:
            header = [c.strip() for c in row]
            body = [r for r in rows[i + 1:] if any(x.strip() for x in r)]
            return header, body
    width = max((len(r) for r in rows), default=0)
    return [f"Spalte {n + 1}" for n in range(width)], [r for r in rows if any(x.strip() for x in r)]


def auto_mapping(header: list[str]) -> dict[str, int]:
    cells = [_norm(h) for h in header]
    mapping: dict[str, int] = {}
    for key, words in COLUMN_KEYWORDS.items():
        for word in words:                      # Reihenfolge = Priorität
            for idx, cell in enumerate(cells):
                if idx in mapping.values():
                    continue
                if cell == word or (key != "iban" and cell.startswith(word)):
                    mapping[key] = idx
                    break
            if key in mapping:
                break
    return mapping


def transactions_from_csv(header: list[str], rows: list[list[str]], mapping: dict[str, int]) -> list[BankTx]:
    if "date" not in mapping or not ("amount" in mapping or ("debit" in mapping and "credit" in mapping)):
        raise ValueError("Datum und Betrag müssen zugeordnet sein.")

    def cell(row, key):
        idx = mapping.get(key)
        return row[idx].strip() if idx is not None and idx < len(row) else ""

    result = []
    for row in rows:
        try:
            d = parse_date(cell(row, "date"))
        except ValueError:
            continue                            # Summen-/Fußzeilen überspringen
        try:
            if "amount" in mapping:
                amount = parse_amount(cell(row, "amount"))
            else:
                debit = parse_amount(cell(row, "debit")) if cell(row, "debit") else 0
                credit = parse_amount(cell(row, "credit")) if cell(row, "credit") else 0
                amount = credit - abs(debit)
        except ValueError:
            continue
        if amount == 0:
            continue
        result.append(BankTx(d, amount, cell(row, "counterparty"), cell(row, "purpose"), cell(row, "iban")))
    return result


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def parse_camt(data: bytes) -> list[BankTx]:
    """CAMT.052/.053 (ISO 20022) – Standardformat deutscher Banken."""
    root = _strip_ns(ET.fromstring(data))
    result = []
    for ntry in root.iter("Ntry"):
        amt = ntry.find("Amt")
        if amt is None or not (amt.text or "").strip():
            continue
        cents = _round(Decimal(amt.text.strip()) * 100)
        if (ntry.findtext("CdtDbtInd") or "").strip() == "DBIT":
            cents = -cents
        d = ntry.findtext("BookgDt/Dt") or ntry.findtext("BookgDt/DtTm") or ntry.findtext("ValDt/Dt") or ""
        try:
            tx_date = date.fromisoformat(d[:10])
        except ValueError:
            continue
        party_path = "Dbtr" if cents > 0 else "Cdtr"
        name = (ntry.findtext(f".//RltdPties/{party_path}/Nm") or ntry.findtext(f".//RltdPties/{party_path}/Pty/Nm")
                or "")
        iban = ntry.findtext(f".//RltdPties/{party_path}Acct/Id/IBAN") or ""
        purpose = " ".join(x.text.strip() for x in ntry.iter("Ustrd") if x.text) or \
            (ntry.findtext("AddtlNtryInf") or "")
        result.append(BankTx(tx_date, cents, name.strip(), purpose.strip(), iban.strip()))
    return result


def is_camt(path: Path) -> bool:
    p = Path(path)
    if p.suffix.lower() == ".xml":
        return True
    if zipfile.is_zipfile(p):
        return any(n.lower().endswith(".xml") for n in zipfile.ZipFile(p).namelist())
    return False


def read_camt_file(path: Path) -> list[BankTx]:
    p = Path(path)
    if zipfile.is_zipfile(p):
        txs = []
        with zipfile.ZipFile(p) as zf:
            for name in sorted(zf.namelist()):
                if name.lower().endswith(".xml"):
                    txs += parse_camt(zf.read(name))
        return txs
    return parse_camt(p.read_bytes())


@dataclass
class ImportResult:
    new: int
    duplicates: int
    import_id: int


def _dedupe_keys(account: str, txs: list[BankTx]) -> list[str]:
    seen: dict[str, int] = {}
    keys = []
    for t in txs:
        base = "|".join([account, t.tx_date.isoformat(), str(t.amount_cents), " ".join(t.counterparty.lower().split()),
                         " ".join(t.purpose.lower().split())])
        n = seen.get(base, 0)
        seen[base] = n + 1
        keys.append(hashlib.sha256(f"{base}|{n}".encode("utf-8")).hexdigest())
    return keys


def import_transactions(db: Database, source: Path, account: str, txs: list[BankTx]) -> ImportResult:
    """Archiviert die Originaldatei und übernimmt neue Umsätze (Duplikate werden erkannt)."""
    if account not in IMPORT_ACCOUNTS:
        raise GoBDError("Import nur für Bank- und PayPal-Konto möglich.")
    source = Path(source)
    target = _unique(ARCHIVE_DIR / str(date.today().year) / "Kontoauszuege" /
                     f"{datetime.now():%Y%m%d_%H%M%S}_{safe_filename(source.stem)[:60]}{source.suffix.lower()}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    _set_readonly(target)
    sha = sha256_file(target)
    keys = _dedupe_keys(account, txs)
    existing = {r[0] for r in db.conn.execute(
        f"SELECT dedupe_key FROM bank_transactions WHERE dedupe_key IN ({','.join('?' * len(keys))})", keys)} \
        if keys else set()
    fresh = [(t, k) for t, k in zip(txs, keys) if k not in existing]
    new = len(fresh)
    with db.conn:
        cur = db.conn.execute(
            """INSERT INTO bank_imports (account, file_name, archive_path, sha256, new_count, dup_count, imported_at)
               VALUES (?,?,?,?,?,?,?)""",
            (account, source.name, target.relative_to(ARCHIVE_DIR).as_posix(), sha, new, len(txs) - new, _now_sql()))
        import_id = cur.lastrowid
        for t, key in fresh:
            db.conn.execute(
                """INSERT INTO bank_transactions (account, tx_date, amount_cents, counterparty, iban,
                       purpose, import_id, dedupe_key, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                (account, t.tx_date.isoformat(), t.amount_cents, t.counterparty, t.iban, t.purpose, import_id, key,
                 _now_sql()))
        db._audit("KONTOAUSZUG_IMPORTIERT", "bankimport", import_id, {
            "konto": account, "datei": source.name, "archiv": target.relative_to(ARCHIVE_DIR).as_posix(),
            "sha256": sha, "neu": new, "doppelt": len(txs) - new})
    return ImportResult(new, len(txs) - new, import_id)


def list_bank_transactions(db: Database, only_open: bool = False, account: str | None = None) -> list:
    sql, args = "SELECT * FROM bank_transactions WHERE 1 = 1", []
    if only_open:
        sql += " AND booking_id IS NULL"
    if account:
        sql += " AND account = ?"
        args.append(account)
    return db.conn.execute(sql + " ORDER BY tx_date DESC, id DESC", args).fetchall()


def get_bank_tx(db: Database, tx_id: int):
    return db.conn.execute("SELECT * FROM bank_transactions WHERE id = ?", (tx_id,)).fetchone()


def _alnum(text: str) -> str:
    return re.sub(r"[^0-9A-ZÄÖÜ]", "", (text or "").upper())


def suggest_invoices(db: Database, tx) -> list[tuple[int, OpenItem]]:
    """Offene Rechnungen, sortiert nach Passgenauigkeit zum Bankumsatz (höher = besser)."""
    purpose = _alnum(f"{tx['purpose']} {tx['counterparty']}")
    scored = []
    for item in invoice_items(db):
        score = 0
        if _alnum(item.number) in purpose:
            score += 100
        if item.open_cents == tx["amount_cents"]:
            score += 50
        name = _alnum(item.customer.split()[-1]) if item.customer.split() else ""
        if name and len(name) > 2 and name in purpose:
            score += 20
        if (item.open_cents > 0) == (tx["amount_cents"] > 0):
            scored.append((score, item))
    return sorted(scored, key=lambda x: (-x[0], x[1].doc_date))


# ------------------------------------------------------------------------------- EÜR
@dataclass
class EuerLine:
    key: str
    label: str
    cents: int
    note: str = ""


@dataclass
class EuerReport:
    year: int
    small_business: bool
    income: list[EuerLine] = field(default_factory=list)
    expenses: list[EuerLine] = field(default_factory=list)
    total_income: int = 0
    total_expenses: int = 0
    notes: list[str] = field(default_factory=list)
    booking_count: int = 0

    @property
    def profit(self) -> int:
        return self.total_income - self.total_expenses


def euer(db: Database, year: int) -> EuerReport:
    small = bool(db.get_settings().get("small_business"))
    report = EuerReport(year, small)
    sums: dict[str, int] = {}
    vat_in = vat_out = 0
    rows = db.conn.execute("SELECT * FROM bookings WHERE substr(booking_date, 1, 4) = ?", (str(year),)).fetchall()
    report.booking_count = len(rows)
    for r in rows:
        cat = CATEGORIES.get(r["category"])
        if cat is None or cat.kind == NEUTRAL:
            continue
        if cat.kind == INCOME:
            sums[cat.key] = sums.get(cat.key, 0) + r["net_cents"]
            vat_in += r["vat_cents"]
        else:
            sums[cat.key] = sums.get(cat.key, 0) - r["net_cents"]
            vat_out -= r["vat_cents"]

    for cat in CATEGORIES.values():
        if cat.key not in sums or sums[cat.key] == 0:
            continue
        amount, note = sums[cat.key], ""
        if cat.key == "bewirtung":
            full = amount
            amount = _round(Decimal(full) * BEWIRTUNG_DEDUCTIBLE)
            note = f"gesamt {fmt_money(full)}, davon 70 % abziehbar"
        line = EuerLine(cat.key, cat.label, amount, note)
        (report.income if cat.kind == INCOME else report.expenses).append(line)
    if vat_in:
        report.income.append(EuerLine("ust_vereinnahmt", "Vereinnahmte Umsatzsteuer", vat_in))
    if vat_out:
        report.expenses.append(EuerLine("vorsteuer", "Gezahlte Vorsteuerbeträge", vat_out))
    report.total_income = sum(x.cents for x in report.income)
    report.total_expenses = sum(x.cents for x in report.expenses)

    if small:
        prev = db.conn.execute(
            "SELECT COALESCE(SUM(net_cents), 0) FROM bookings WHERE category IN ('einnahmen_ku', 'einnahmen_sonstige') "
            "AND substr(booking_date, 1, 4) = ?", (str(year - 1),)).fetchone()[0]
        current = sums.get("einnahmen_ku", 0)
        if current > KU_LIMIT_CURRENT_YEAR or prev > KU_LIMIT_PREV_YEAR:
            report.notes.append("Achtung: Die Umsatzgrenzen für Kleinunternehmer (Vorjahr 25.000 €, laufendes Jahr "
                                "100.000 €, Stand 2025) sind möglicherweise überschritten – bitte steuerlich prüfen.")
    if sums.get("afa"):
        report.notes.append("AfA wurde manuell gebucht. Ein Anlagenverzeichnis (Anschaffung, Nutzungsdauer, "
                            "Restwert) ist zusätzlich zu führen.")
    big = db.conn.execute(
        "SELECT COUNT(*) FROM bookings WHERE substr(booking_date, 1, 4) = ? AND category IN "
        "('gwg', 'buero', 'sonstige_ausgaben', 'software') AND -net_cents > ? AND status = ?",
        (str(year), GWG_LIMIT_CENTS, STATUS_VALID)).fetchone()[0]
    if big:
        report.notes.append(f"{big} Ausgabe(n) über 800 € netto als Sofortaufwand gebucht – handelt es sich um "
                            "Anlagegüter, sind sie über die Nutzungsdauer abzuschreiben (AfA).")
    open_bank = db.conn.execute(
        "SELECT COUNT(*) FROM bank_transactions WHERE booking_id IS NULL AND substr(tx_date, 1, 4) = ?",
        (str(year),)).fetchone()[0]
    if open_bank:
        report.notes.append(f"{open_bank} importierte(r) Bankumsatz/-umsätze aus {year} sind noch nicht gebucht.")
    return report


def euer_pdf(db: Database, report: EuerReport, target: Path) -> Path:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from .pdf_engine import BRAND, BRAND_LIGHT, GREY

    s = db.get_settings()
    base = ParagraphStyle("b", fontName="Helvetica", fontSize=9.5, leading=13)
    small = ParagraphStyle("s", parent=base, fontSize=8, leading=10.5, textColor=GREY)
    title = ParagraphStyle("t", parent=base, fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=BRAND)
    head = ParagraphStyle("h", parent=base, fontName="Helvetica-Bold", fontSize=11, textColor=BRAND)
    right = ParagraphStyle("r", parent=base, alignment=2)
    bold_r = ParagraphStyle("br", parent=right, fontName="Helvetica-Bold")
    bold = ParagraphStyle("bb", parent=base, fontName="Helvetica-Bold")

    def section(lines: list[EuerLine], total: int, label: str):
        rows = [[Paragraph(x.label + (f"<br/><font size=7.5 color='#5B6475'>{x.note}</font>" if x.note else ""),
                           base), Paragraph(fmt_money(x.cents), right)] for x in lines]
        if not rows:
            rows = [[Paragraph("– keine –", small), Paragraph(fmt_money(0), right)]]
        rows.append([Paragraph(label, bold), Paragraph(fmt_money(total), bold_r)])
        t = Table(rows, colWidths=[125 * mm, 40 * mm])
        t.setStyle(TableStyle([("LINEABOVE", (0, -1), (-1, -1), 0.8, BRAND),
                               ("ROWBACKGROUNDS", (0, 0), (-1, -2), [colors.white, BRAND_LIGHT]),
                               ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        return t

    company = ", ".join(x for x in [s.get("sender_name"), s.get("sender_owner"), s.get("sender_zip_city")] if x)
    story = [Paragraph(f"Einnahmen-Überschuss-Rechnung {report.year}", title),
             Paragraph(company, base),
             Paragraph(("Kleinunternehmer nach § 19 UStG" if report.small_business else "Regelbesteuerung")
                       + f" · Steuernummer: {s.get('tax_number') or '–'} · Stand {date.today():%d.%m.%Y} · "
                       f"{report.booking_count} Buchungen", small),
             Spacer(1, 8 * mm), Paragraph("Betriebseinnahmen", head), Spacer(1, 2 * mm),
             section(report.income, report.total_income, "Summe Betriebseinnahmen"), Spacer(1, 6 * mm),
             Paragraph("Betriebsausgaben", head), Spacer(1, 2 * mm),
             section(report.expenses, report.total_expenses, "Summe Betriebsausgaben"), Spacer(1, 6 * mm)]
    result = Table([[Paragraph("Gewinn / Verlust", bold),
                     Paragraph(fmt_money(report.profit), ParagraphStyle("g", parent=bold_r, fontSize=13))]],
                   colWidths=[125 * mm, 40 * mm])
    result.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), BRAND_LIGHT), ("BOX", (0, 0), (-1, -1), 1, BRAND),
                                ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story += [result, Spacer(1, 6 * mm)]
    for n in report.notes:
        story.append(Paragraph(f"Hinweis: {n}", small))
    story += [Spacer(1, 4 * mm), Paragraph(
        "Die Werte sind nach dem Zufluss-/Abflussprinzip (Zahlungsdatum) ermittelt und in die Anlage EÜR in "
        "„Mein ELSTER“ zu übertragen. Die Zuordnung zu den Formularzeilen richtet sich nach dem jeweils gültigen "
        "Vordruck. Diese Auswertung ersetzt keine steuerliche Beratung.", small)]
    target = Path(target)
    SimpleDocTemplate(str(target), pagesize=A4, leftMargin=22 * mm, rightMargin=20 * mm, topMargin=20 * mm,
                      bottomMargin=18 * mm, title=f"EÜR {report.year}", creator="CNCTD.STOCK").build(story)
    db.log_event("EUER_ERSTELLT", {"jahr": report.year, "datei": target.name, "gewinn_cent": report.profit})
    return target
