"""SQLite-Backend: Warenbestand, Kunden, Einstellungen, Belege und GoBD-Änderungsprotokoll.

GoBD-relevante Grundsätze, die hier technisch abgesichert werden:
* Unveränderbarkeit: erstellte Belege können weder gelöscht noch inhaltlich geändert werden
  (SQLite-Trigger). Korrekturen erfolgen ausschließlich über Stornobelege.
* Nachvollziehbarkeit: jede Änderung an Stamm- und Bewegungsdaten wird im Änderungsprotokoll
  (audit_log) festgehalten. Die Einträge sind per SHA-256 verkettet, nachträgliche Manipulation
  wird bei der Integritätsprüfung erkannt. Das Protokoll selbst ist ebenfalls unveränderbar.
* Vollständigkeit / Ordnung: Belegnummern werden automatisch, fortlaufend und eindeutig vergeben;
  Nummernzähler können nur erhöht werden.
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .paths import DB_PATH, DEFAULT_PDF_DIR

# Spalten der Einstellungs-Tabelle (eine einzige Zeile mit id = 1).
# Neue Spalten werden bei bestehenden Datenbanken automatisch ergänzt.
SETTINGS_COLUMNS: dict[str, tuple[str, object]] = {
    "logo_path": ("TEXT", ""),                 # Firmen_Logo_Pfad
    "logo_position": ("TEXT", "rechts"),
    "show_company_name": ("INTEGER", 0),      # Firmenname zusätzlich zum Logo im Briefkopf
    "sender_name": ("TEXT", ""),               # Absender_Name
    "sender_owner": ("TEXT", ""),
    "sender_street": ("TEXT", ""),
    "sender_zip_city": ("TEXT", ""),
    "sender_country": ("TEXT", ""),
    "sender_phone": ("TEXT", ""),
    "sender_email": ("TEXT", ""),
    "sender_website": ("TEXT", ""),
    "tax_number": ("TEXT", ""),
    "vat_id": ("TEXT", ""),
    "bank_name": ("TEXT", ""),
    "iban": ("TEXT", ""),
    "bic": ("TEXT", ""),
    "pdf_output_dir": ("TEXT", str(DEFAULT_PDF_DIR)),  # Standard_Speicherort_PDFs
    "vat_rate": ("REAL", 19.0),
    "small_business": ("INTEGER", 0),
    "payment_days": ("INTEGER", 14),
    "invoice_prefix": ("TEXT", "RE"),
    "next_invoice_no": ("INTEGER", 1),
    "delivery_prefix": ("TEXT", "LS"),
    "next_delivery_no": ("INTEGER", 1),
    "cancel_prefix": ("TEXT", "ST"),
    "next_cancel_no": ("INTEGER", 1),
    "invoice_closing_text": ("TEXT", "Vielen Dank für Ihren Auftrag!"),
    "delivery_closing_text": ("TEXT", "Bitte prüfen Sie die Ware bei Erhalt auf Vollständigkeit und Unversehrtheit."),
    "accounting_enabled": ("INTEGER", 0),     # Buchhaltungsmodul (EÜR, Kassenbuch, Bankabgleich)
}

DOC_INVOICE = "RE"
DOC_DELIVERY = "LS"
DOC_CANCEL = "ST"
DOC_LABELS = {DOC_INVOICE: "Rechnung", DOC_DELIVERY: "Lieferschein", DOC_CANCEL: "Stornorechnung"}
PRICED_TYPES = {DOC_INVOICE, DOC_CANCEL}
COUNTERS = {
    DOC_INVOICE: ("invoice_prefix", "next_invoice_no"),
    DOC_DELIVERY: ("delivery_prefix", "next_delivery_no"),
    DOC_CANCEL: ("cancel_prefix", "next_cancel_no"),
}
COUNTER_COLUMNS = {counter for _, counter in COUNTERS.values()}

STATUS_VALID = "gueltig"
STATUS_CANCELLED = "storniert"
STATUS_LABELS = {STATUS_VALID: "gültig", STATUS_CANCELLED: "storniert"}

# Zusätzliche Spalten der Dokumenten-Tabelle (werden bei bestehenden Datenbanken nachgerüstet).
DOCUMENT_EXTRA_COLUMNS: dict[str, str] = {
    "customer_id": "INTEGER",
    "delivery_address": "TEXT NOT NULL DEFAULT ''",
    "shipping_method": "TEXT NOT NULL DEFAULT ''",
    "shipping_cents": "INTEGER NOT NULL DEFAULT 0",
    "tracking_no": "TEXT NOT NULL DEFAULT ''",
    "net_cents": "INTEGER NOT NULL DEFAULT 0",
    "vat_cents": "INTEGER NOT NULL DEFAULT 0",
    "status": "TEXT NOT NULL DEFAULT 'gueltig'",
    "cancel_reason": "TEXT NOT NULL DEFAULT ''",
    "cancels_id": "INTEGER",
    "cancelled_by_id": "INTEGER",
    "cancelled_at": "TEXT",
    "stock_deducted": "INTEGER NOT NULL DEFAULT 0",
    "stock_returned": "INTEGER NOT NULL DEFAULT 0",
    "archive_path": "TEXT NOT NULL DEFAULT ''",   # relativ zum Archivordner
    "pdf_sha256": "TEXT",
    "snapshot_json": "TEXT",
    "snapshot_sha256": "TEXT",
    "legacy": "INTEGER NOT NULL DEFAULT 0",
}

# Diese Felder eines Belegs sind nach der Erstellung gesperrt.
IMMUTABLE_DOCUMENT_FIELDS = [
    "doc_type", "doc_number", "doc_date", "recipient", "total_cents", "net_cents", "vat_cents",
    "items_json", "customer_id", "delivery_address", "shipping_method", "shipping_cents",
    "archive_path", "pdf_sha256", "snapshot_json", "snapshot_sha256", "created_at", "cancels_id",
    "stock_deducted", "legacy",
]

# Diese Felder einer Buchung sind nach dem Speichern gesperrt (nur Storno-Status darf sich ändern).
IMMUTABLE_BOOKING_FIELDS = [
    "booking_no", "booking_date", "receipt_date", "account", "category", "gross_cents", "net_cents",
    "vat_cents", "vat_rate", "counterparty", "text", "document_id", "bank_tx_id", "receipt_path",
    "receipt_sha256", "receipt_original_name", "cancels_id", "created_at",
]

DEFAULT_SHIPPING_METHODS = [
    "DHL Paket", "DHL Päckchen", "Deutsche Post Warensendung", "Hermes Paket", "DPD Paket", "Abholung",
]

GENESIS_HASH = "0" * 64


@dataclass
class Article:
    id: int | None
    article_no: str
    name: str
    description: str
    unit: str
    quantity: float
    unit_price_cents: int
    total_cents: int = 0


@dataclass
class Customer:
    id: int | None
    customer_no: str
    billing_address: str          # mehrzeilig, erste Zeile = Name/Firma
    delivery_address: str = ""    # leer = wie Rechnungsadresse
    email: str = ""
    phone: str = ""
    shipping_method: str = ""     # bevorzugte Versandart
    notes: str = ""

    def _lines(self) -> list[str]:
        return [x.strip() for x in self.billing_address.splitlines() if x.strip()]

    @property
    def display_name(self) -> str:
        lines = self._lines()
        return lines[0] if lines else "(ohne Namen)"

    @property
    def city(self) -> str:
        lines = self._lines()
        return lines[-1] if len(lines) > 1 else ""


@dataclass
class ShippingMethod:
    id: int | None
    name: str
    price_cents: int = 0


class DuplicateArticleNo(Exception):
    pass


class GoBDError(Exception):
    """Eine Aktion würde GoBD-Grundsätze verletzen (z. B. Zähler verringern)."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chain_hash(prev_hash: str, ts: str, action: str, entity: str, entity_id: str, details: str) -> str:
    return sha256_text("|".join([prev_hash, ts, action, entity, entity_id, details]))


def canonical_json(data) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def format_doc_number(prefix: str, year: int, counter: int) -> str:
    return f"{prefix}-{year}-{int(counter):04d}"


class Database:
    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.migration_warnings: list[str] = []
        self._migrate()

    # ------------------------------------------------------------------ schema
    def _migrate(self) -> None:
        c = self.conn
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS articles (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                article_no       TEXT    NOT NULL UNIQUE COLLATE NOCASE,
                name             TEXT    NOT NULL,
                description      TEXT    NOT NULL DEFAULT '',
                unit             TEXT    NOT NULL DEFAULT 'Stk.',
                quantity         REAL    NOT NULL DEFAULT 0,
                unit_price_cents INTEGER NOT NULL DEFAULT 0,
                total_cents      INTEGER GENERATED ALWAYS AS
                                 (CAST(ROUND(quantity * unit_price_cents) AS INTEGER)) VIRTUAL,
                created_at       TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at       TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
            """
        )
        c.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK (id = 1))")
        self._ensure_columns("settings", {col: sql_type for col, (sql_type, _) in SETTINGS_COLUMNS.items()})
        c.execute("INSERT OR IGNORE INTO settings (id) VALUES (1)")
        # Fehlende Werte mit Standardwerten auffüllen.
        row = c.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        for col, (_, default) in SETTINGS_COLUMNS.items():
            if row[col] is None:
                c.execute(f"UPDATE settings SET {col} = ? WHERE id = 1", (default,))
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_type    TEXT    NOT NULL,
                doc_number  TEXT    NOT NULL,
                doc_date    TEXT    NOT NULL,
                recipient   TEXT    NOT NULL DEFAULT '',
                total_cents INTEGER NOT NULL DEFAULT 0,
                file_path   TEXT    NOT NULL DEFAULT '',
                items_json  TEXT    NOT NULL DEFAULT '[]',
                created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
            """
        )
        self._ensure_columns("documents", DOCUMENT_EXTRA_COLUMNS)
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS customers (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_no      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
                billing_address  TEXT    NOT NULL DEFAULT '',
                delivery_address TEXT    NOT NULL DEFAULT '',
                email            TEXT    NOT NULL DEFAULT '',
                phone            TEXT    NOT NULL DEFAULT '',
                shipping_method  TEXT    NOT NULL DEFAULT '',
                notes            TEXT    NOT NULL DEFAULT '',
                created_at       TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
            """
        )
        shipping_exists = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'shipping_methods'").fetchone()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS shipping_methods (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL,
                price_cents INTEGER NOT NULL DEFAULT 0,
                sort        INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        if not shipping_exists:  # nur beim allerersten Anlegen mit Beispielen füllen
            c.executemany("INSERT INTO shipping_methods (name, price_cents, sort) VALUES (?, 0, ?)",
                          [(n, i) for i, n in enumerate(DEFAULT_SHIPPING_METHODS)])
        audit_exists = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'audit_log'").fetchone()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        TEXT NOT NULL,
                action    TEXT NOT NULL,
                entity    TEXT NOT NULL,
                entity_id TEXT NOT NULL DEFAULT '',
                details   TEXT NOT NULL DEFAULT '{}',
                prev_hash TEXT NOT NULL,
                hash      TEXT NOT NULL
            )
            """
        )
        self._create_accounting_tables()
        self._create_protection()
        if not audit_exists:
            n_docs = c.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            n_art = c.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            self._audit("GOBD_MODUS_AKTIVIERT", "system", "",
                        {"bestehende_belege": n_docs, "bestehende_artikel": n_art,
                         "hinweis": "Ab hier werden alle Änderungen protokolliert."})
        c.commit()

    def _create_protection(self) -> None:
        """Trigger, die Belege und Protokoll auf Datenbankebene vor Änderungen schützen."""
        c = self.conn
        for name in ("audit_log_no_update", "audit_log_no_delete", "documents_no_delete",
                     "documents_immutable", "bookings_no_delete", "bookings_immutable",
                     "bank_imports_no_delete", "bank_imports_no_update", "bank_tx_no_delete", "bank_tx_immutable"):
            c.execute(f"DROP TRIGGER IF EXISTS {name}")
        c.execute("""CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
                     BEGIN SELECT RAISE(ABORT, 'GoBD: Protokolleinträge sind unveränderbar'); END""")
        c.execute("""CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
                     BEGIN SELECT RAISE(ABORT, 'GoBD: Protokolleinträge können nicht gelöscht werden'); END""")
        c.execute("""CREATE TRIGGER documents_no_delete BEFORE DELETE ON documents
                     BEGIN SELECT RAISE(ABORT, 'GoBD: Belege können nicht gelöscht werden - bitte stornieren');
                     END""")
        changed = " OR ".join(f"NEW.{f} IS NOT OLD.{f}" for f in IMMUTABLE_DOCUMENT_FIELDS)
        c.execute(f"""CREATE TRIGGER documents_immutable BEFORE UPDATE ON documents
                      WHEN OLD.snapshot_sha256 IS NOT NULL AND (
                          {changed}
                          OR (OLD.status = '{STATUS_CANCELLED}' AND (
                              NEW.status IS NOT OLD.status OR NEW.cancel_reason IS NOT OLD.cancel_reason
                              OR NEW.cancelled_by_id IS NOT OLD.cancelled_by_id
                              OR NEW.cancelled_at IS NOT OLD.cancelled_at
                              OR NEW.stock_returned IS NOT OLD.stock_returned)))
                      BEGIN SELECT RAISE(ABORT, 'GoBD: Belege sind nach der Erstellung unveränderbar'); END""")
        c.execute("""CREATE TRIGGER bookings_no_delete BEFORE DELETE ON bookings
                     BEGIN SELECT RAISE(ABORT, 'GoBD: Buchungen können nicht gelöscht werden - bitte stornieren');
                     END""")
        changed = " OR ".join(f"NEW.{f} IS NOT OLD.{f}" for f in IMMUTABLE_BOOKING_FIELDS)
        c.execute(f"""CREATE TRIGGER bookings_immutable BEFORE UPDATE ON bookings
                      WHEN ({changed})
                          OR (OLD.status = '{STATUS_CANCELLED}' AND (
                              NEW.status IS NOT OLD.status OR NEW.cancel_reason IS NOT OLD.cancel_reason
                              OR NEW.cancelled_by_id IS NOT OLD.cancelled_by_id))
                      BEGIN SELECT RAISE(ABORT, 'GoBD: Buchungen sind unveränderbar'); END""")
        c.execute("""CREATE TRIGGER bank_imports_no_delete BEFORE DELETE ON bank_imports
                     BEGIN SELECT RAISE(ABORT, 'GoBD: Kontoauszug-Importe können nicht gelöscht werden'); END""")
        c.execute("""CREATE TRIGGER bank_imports_no_update BEFORE UPDATE ON bank_imports
                     BEGIN SELECT RAISE(ABORT, 'GoBD: Kontoauszug-Importe sind unveränderbar'); END""")
        c.execute("""CREATE TRIGGER bank_tx_no_delete BEFORE DELETE ON bank_transactions
                     BEGIN SELECT RAISE(ABORT, 'GoBD: Bankumsätze können nicht gelöscht werden'); END""")
        changed = " OR ".join(f"NEW.{f} IS NOT OLD.{f}" for f in
                              ("account", "tx_date", "amount_cents", "counterparty", "iban", "purpose",
                               "import_id", "dedupe_key", "created_at"))
        c.execute(f"""CREATE TRIGGER bank_tx_immutable BEFORE UPDATE ON bank_transactions WHEN {changed}
                      BEGIN SELECT RAISE(ABORT, 'GoBD: Bankumsätze sind unveränderbar'); END""")
        try:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_documents_number ON documents (doc_type, doc_number)")
        except sqlite3.IntegrityError:
            self.migration_warnings.append(
                "Im Altbestand gibt es doppelt vergebene Belegnummern. Neue Nummern werden trotzdem "
                "eindeutig vergeben; die Dubletten sind im Archiv sichtbar.")

    def _create_accounting_tables(self) -> None:
        """Buchhaltung: Buchungsjournal, Kontoauszug-Importe und Bankumsätze."""
        c = self.conn
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                booking_no            TEXT    NOT NULL UNIQUE,
                booking_date          TEXT    NOT NULL,          -- Zahlungsdatum (Zu-/Abfluss)
                receipt_date          TEXT,                      -- Belegdatum
                account               TEXT    NOT NULL,          -- BANK, KASSE, PAYPAL, OHNE
                category              TEXT    NOT NULL,
                gross_cents           INTEGER NOT NULL,          -- + Zufluss, - Abfluss
                net_cents             INTEGER NOT NULL,
                vat_cents             INTEGER NOT NULL,
                vat_rate              REAL    NOT NULL DEFAULT 0,
                counterparty          TEXT    NOT NULL DEFAULT '',
                text                  TEXT    NOT NULL DEFAULT '',
                document_id           INTEGER,                   -- zugehörige Rechnung (Zahlungseingang)
                bank_tx_id            INTEGER,
                receipt_path          TEXT    NOT NULL DEFAULT '',
                receipt_sha256        TEXT,
                receipt_original_name TEXT    NOT NULL DEFAULT '',
                status                TEXT    NOT NULL DEFAULT 'gueltig',
                cancel_reason         TEXT    NOT NULL DEFAULT '',
                cancels_id            INTEGER,
                cancelled_by_id       INTEGER,
                created_at            TEXT    NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS bank_imports (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                account      TEXT    NOT NULL,
                file_name    TEXT    NOT NULL,
                archive_path TEXT    NOT NULL,
                sha256       TEXT    NOT NULL,
                new_count    INTEGER NOT NULL DEFAULT 0,
                dup_count    INTEGER NOT NULL DEFAULT 0,
                imported_at  TEXT    NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS bank_transactions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                account      TEXT    NOT NULL,
                tx_date      TEXT    NOT NULL,
                amount_cents INTEGER NOT NULL,
                counterparty TEXT    NOT NULL DEFAULT '',
                iban         TEXT    NOT NULL DEFAULT '',
                purpose      TEXT    NOT NULL DEFAULT '',
                import_id    INTEGER NOT NULL,
                dedupe_key   TEXT    NOT NULL UNIQUE,
                booking_id   INTEGER,
                created_at   TEXT    NOT NULL
            )
            """
        )

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        for col, definition in columns.items():
            if col not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- protokoll
    def _last_hash(self) -> str:
        row = self.conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        return row["hash"] if row else GENESIS_HASH

    def _audit(self, action: str, entity: str, entity_id="", details: dict | None = None) -> None:
        """Schreibt einen verketteten Protokolleintrag (ohne Commit – läuft in der Transaktion des Aufrufers)."""
        ts = datetime.now().isoformat(timespec="seconds")
        det = canonical_json(details or {})
        prev = self._last_hash()
        h = chain_hash(prev, ts, action, entity, str(entity_id), det)
        self.conn.execute(
            "INSERT INTO audit_log (ts, action, entity, entity_id, details, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
            (ts, action, entity, str(entity_id), det, prev, h))

    def log_event(self, action: str, details: dict | None = None, entity: str = "system", entity_id="") -> None:
        with self.conn:
            self._audit(action, entity, entity_id, details)

    def list_audit(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM audit_log ORDER BY id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.conn.execute(sql).fetchall()

    def last_audit_hash(self) -> str:
        return self._last_hash()

    def verify_audit_chain(self) -> tuple[bool, int, int | None]:
        """Prüft die Hash-Kette. Rückgabe: (ok, Anzahl Einträge, erste fehlerhafte ID)."""
        prev = GENESIS_HASH
        count = 0
        for row in self.conn.execute("SELECT * FROM audit_log ORDER BY id"):
            count += 1
            expected = chain_hash(prev, row["ts"], row["action"], row["entity"], row["entity_id"], row["details"])
            if row["prev_hash"] != prev or row["hash"] != expected:
                return False, count, row["id"]
            prev = row["hash"]
        return True, count, None

    # --------------------------------------------------------------- articles
    @staticmethod
    def _row_to_article(row: sqlite3.Row) -> Article:
        return Article(
            id=row["id"],
            article_no=row["article_no"],
            name=row["name"],
            description=row["description"],
            unit=row["unit"],
            quantity=row["quantity"],
            unit_price_cents=row["unit_price_cents"],
            total_cents=row["total_cents"],
        )

    @staticmethod
    def _article_dict(a: Article) -> dict:
        return {"artikel_nr": a.article_no, "bezeichnung": a.name, "beschreibung": a.description,
                "einheit": a.unit, "menge": a.quantity, "stueckpreis_cent": a.unit_price_cents}

    def list_articles(self) -> list[Article]:
        rows = self.conn.execute("SELECT * FROM articles ORDER BY article_no COLLATE NOCASE")
        return [self._row_to_article(r) for r in rows]

    def get_article(self, article_id: int) -> Article | None:
        row = self.conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
        return self._row_to_article(row) if row else None

    def article_no_exists(self, article_no: str, exclude_id: int | None = None) -> bool:
        row = self.conn.execute(
            "SELECT id FROM articles WHERE article_no = ? AND id IS NOT ?",
            (article_no.strip(), exclude_id),
        ).fetchone()
        return row is not None

    def suggest_article_no(self) -> str:
        n = self.conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] + 1
        while self.article_no_exists(f"A-{n:05d}"):
            n += 1
        return f"A-{n:05d}"

    def add_article(self, a: Article) -> int:
        try:
            with self.conn:
                cur = self.conn.execute(
                    """INSERT INTO articles (article_no, name, description, unit, quantity, unit_price_cents)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (a.article_no.strip(), a.name.strip(), a.description.strip(), a.unit.strip(),
                     a.quantity, a.unit_price_cents),
                )
                a.id = cur.lastrowid
                self._audit("ARTIKEL_ANGELEGT", "artikel", a.id, self._article_dict(a))
        except sqlite3.IntegrityError as exc:
            raise DuplicateArticleNo(a.article_no) from exc
        return a.id

    def update_article(self, a: Article) -> None:
        old = self.get_article(a.id)
        try:
            with self.conn:
                self.conn.execute(
                    """UPDATE articles SET article_no = ?, name = ?, description = ?, unit = ?,
                           quantity = ?, unit_price_cents = ?, updated_at = datetime('now', 'localtime')
                       WHERE id = ?""",
                    (a.article_no.strip(), a.name.strip(), a.description.strip(), a.unit.strip(),
                     a.quantity, a.unit_price_cents, a.id),
                )
                if old:
                    before, after = self._article_dict(old), self._article_dict(self.get_article(a.id))
                    diff = {k: [before[k], after[k]] for k in before if before[k] != after[k]}
                    if diff:
                        self._audit("ARTIKEL_GEAENDERT", "artikel", a.id,
                                    {"artikel_nr": after["artikel_nr"], "aenderungen": diff})
        except sqlite3.IntegrityError as exc:
            raise DuplicateArticleNo(a.article_no) from exc

    def delete_articles(self, ids: list[int]) -> None:
        with self.conn:
            for article_id in ids:
                old = self.get_article(article_id)
                if old is None:
                    continue
                self.conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
                self._audit("ARTIKEL_GELOESCHT", "artikel", article_id, self._article_dict(old))

    def _book_stock(self, changes: dict[int, float], reason: str, doc_number: str = "") -> None:
        """Bestandsbuchung (article_id -> Mengenänderung, negativ = Abgang). Ohne Commit."""
        for article_id, delta in changes.items():
            old = self.get_article(article_id)
            if old is None:
                continue
            self.conn.execute(
                """UPDATE articles SET quantity = quantity + ?,
                       updated_at = datetime('now', 'localtime') WHERE id = ?""",
                (delta, article_id),
            )
            self._audit("BESTAND_GEBUCHT", "artikel", article_id, {
                "artikel_nr": old.article_no, "alt": old.quantity, "neu": round(old.quantity + delta, 3),
                "aenderung": delta, "grund": reason, "beleg": doc_number})

    def inventory_summary(self) -> tuple[int, float, int]:
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(quantity), 0), COALESCE(SUM(total_cents), 0) FROM articles"
        ).fetchone()
        return row[0], row[1], row[2]

    # --------------------------------------------------------------- settings
    def get_settings(self) -> dict:
        row = self.conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        return {k: row[k] for k in SETTINGS_COLUMNS}

    def save_settings(self, values: dict) -> None:
        cols = [k for k in values if k in SETTINGS_COLUMNS]
        if not cols:
            return
        old = self.get_settings()
        for col in cols:
            if col in COUNTER_COLUMNS and int(values[col]) < int(old[col]):
                raise GoBDError("Nummernzähler dürfen nicht verringert werden – sonst könnten Belegnummern "
                                "doppelt vergeben werden.")
        diff = {c: [old[c], values[c]] for c in cols if str(old[c]) != str(values[c])}
        if not diff:
            return
        with self.conn:
            assignments = ", ".join(f"{c} = ?" for c in diff)
            self.conn.execute(f"UPDATE settings SET {assignments} WHERE id = 1", [values[c] for c in diff])
            self._audit("EINSTELLUNGEN_GEAENDERT", "einstellungen", 1, diff)

    # -------------------------------------------------------------- documents
    def next_doc_number(self, doc_type: str, on: date | None = None) -> str:
        s = self.get_settings()
        prefix_col, counter_col = COUNTERS[doc_type]
        year = (on or date.today()).year
        counter = int(s[counter_col])
        number = format_doc_number(s[prefix_col], year, counter)
        # Falls die Nummer (z. B. aus dem Altbestand) schon existiert, weiterzählen.
        while self.doc_number_used(doc_type, number):
            counter += 1
            number = format_doc_number(s[prefix_col], year, counter)
        return number

    # Alter Name, bleibt für bestehende Aufrufer erhalten.
    preview_doc_number = next_doc_number

    def doc_number_used(self, doc_type: str, number: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM documents WHERE doc_type = ? AND doc_number = ?", (doc_type, number)
        ).fetchone()
        return row is not None

    def last_document_date(self, doc_type: str) -> date | None:
        row = self.conn.execute(
            "SELECT MAX(doc_date) FROM documents WHERE doc_type = ?", (doc_type,)).fetchone()
        return date.fromisoformat(row[0]) if row and row[0] else None

    def insert_document(self, rec: dict, deductions: dict[int, float] | None = None,
                        cancels: dict | None = None) -> int:
        """Legt einen finalen Beleg an (eine Transaktion inkl. Zähler, Bestand und Protokoll).

        rec: Spaltenwerte des Belegs (doc_type, doc_number, ... snapshot_sha256).
        deductions: Bestandsabgänge (article_id -> Menge).
        cancels: {"id", "reason", "restock": {article_id: Menge}} wenn dies ein Stornobeleg ist.
        """
        doc_type = rec["doc_type"]
        expected = self.next_doc_number(doc_type, date.fromisoformat(rec["doc_date"]))
        if rec["doc_number"] != expected:
            raise GoBDError(f"Belegnummer {rec['doc_number']} ist nicht die nächste freie Nummer ({expected}).")
        _, counter_col = COUNTERS[doc_type]
        n_in_number = int(rec["doc_number"].rsplit("-", 1)[-1])
        cols = list(rec)
        with self.conn:
            cur = self.conn.execute(
                f"INSERT INTO documents ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [rec[c] for c in cols])
            doc_id = cur.lastrowid
            self.conn.execute(f"UPDATE settings SET {counter_col} = ? WHERE id = 1", (n_in_number + 1,))
            self._audit("BELEG_ERSTELLT", "beleg", doc_id, {
                "typ": DOC_LABELS[doc_type], "nummer": rec["doc_number"], "datum": rec["doc_date"],
                "brutto_cent": rec.get("total_cents", 0), "pdf_sha256": rec.get("pdf_sha256"),
                "daten_sha256": rec.get("snapshot_sha256"), "archiv": rec.get("archive_path")})
            if deductions:
                self._book_stock({k: -v for k, v in deductions.items()}, "Abgang laut Beleg", rec["doc_number"])
            if cancels:
                self._mark_cancelled(cancels["id"], cancels["reason"], cancels.get("restock"), doc_id)
        return doc_id

    def _mark_cancelled(self, doc_id: int, reason: str, restock: dict[int, float] | None,
                        cancelled_by_id: int | None) -> None:
        orig = self.get_document(doc_id)
        if orig is None or orig["status"] == STATUS_CANCELLED:
            raise GoBDError("Der Beleg ist bereits storniert.")
        self.conn.execute(
            """UPDATE documents SET status = ?, cancel_reason = ?, cancelled_by_id = ?,
                   cancelled_at = datetime('now', 'localtime'), stock_returned = ? WHERE id = ?""",
            (STATUS_CANCELLED, reason, cancelled_by_id, 1 if restock else 0, doc_id))
        storno_no = self.get_document(cancelled_by_id)["doc_number"] if cancelled_by_id else ""
        self._audit("BELEG_STORNIERT", "beleg", doc_id, {
            "nummer": orig["doc_number"], "typ": DOC_LABELS.get(orig["doc_type"], orig["doc_type"]),
            "grund": reason, "stornobeleg": storno_no, "bestand_zurueckgebucht": bool(restock)})
        if restock:
            self._book_stock(restock, "Rückbuchung wegen Storno", orig["doc_number"])

    def cancel_without_document(self, doc_id: int, reason: str, restock: dict[int, float] | None) -> None:
        """Storno ohne eigenen Stornobeleg (Lieferscheine)."""
        with self.conn:
            self._mark_cancelled(doc_id, reason, restock, None)

    def get_document(self, doc_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()

    def list_documents(self, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM documents ORDER BY created_at DESC, id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.conn.execute(sql).fetchall()

    def update_tracking_no(self, doc_id: int, tracking_no: str) -> None:
        old = self.get_document(doc_id)
        with self.conn:
            self.conn.execute("UPDATE documents SET tracking_no = ? WHERE id = ?", (tracking_no.strip(), doc_id))
            self._audit("SENDUNGSNR_GEAENDERT", "beleg", doc_id, {
                "nummer": old["doc_number"], "alt": old["tracking_no"], "neu": tracking_no.strip()})

    def update_file_path(self, doc_id: int, file_path: str) -> None:
        """Arbeitskopie im PDF-Ordner (nicht geschützt – das Original liegt im Archiv)."""
        with self.conn:
            self.conn.execute("UPDATE documents SET file_path = ? WHERE id = ?", (file_path, doc_id))

    def backfill_legacy(self, doc_id: int, archive_path: str, pdf_sha: str | None,
                        snapshot_json: str, snapshot_sha: str, note: str) -> None:
        """Übernimmt einen Beleg aus der Zeit vor dem GoBD-Modus einmalig ins Archiv."""
        with self.conn:
            self.conn.execute(
                """UPDATE documents SET archive_path = ?, pdf_sha256 = ?, snapshot_json = ?,
                       snapshot_sha256 = ?, legacy = 1 WHERE id = ? AND snapshot_sha256 IS NULL""",
                (archive_path, pdf_sha, snapshot_json, snapshot_sha, doc_id))
            row = self.get_document(doc_id)
            self._audit("ALTBESTAND_ARCHIVIERT", "beleg", doc_id, {
                "nummer": row["doc_number"], "pdf_sha256": pdf_sha, "hinweis": note})

    # -------------------------------------------------------------- customers
    @staticmethod
    def _row_to_customer(row: sqlite3.Row) -> Customer:
        return Customer(row["id"], row["customer_no"], row["billing_address"], row["delivery_address"],
                        row["email"], row["phone"], row["shipping_method"], row["notes"])

    @staticmethod
    def _customer_dict(cu: Customer) -> dict:
        return {"kunden_nr": cu.customer_no, "rechnungsadresse": cu.billing_address,
                "lieferadresse": cu.delivery_address, "email": cu.email, "telefon": cu.phone,
                "versandart": cu.shipping_method, "notizen": cu.notes}

    def list_customers(self) -> list[Customer]:
        rows = self.conn.execute("SELECT * FROM customers ORDER BY customer_no COLLATE NOCASE")
        return [self._row_to_customer(r) for r in rows]

    def get_customer(self, customer_id: int) -> Customer | None:
        row = self.conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        return self._row_to_customer(row) if row else None

    def customer_no_exists(self, customer_no: str, exclude_id: int | None = None) -> bool:
        row = self.conn.execute("SELECT id FROM customers WHERE customer_no = ? AND id IS NOT ?",
                                (customer_no.strip(), exclude_id)).fetchone()
        return row is not None

    def suggest_customer_no(self) -> str:
        n = self.conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] + 1
        while self.customer_no_exists(f"K-{n:05d}"):
            n += 1
        return f"K-{n:05d}"

    def save_customer(self, cu: Customer) -> int:
        values = (cu.customer_no.strip(), cu.billing_address.strip(), cu.delivery_address.strip(),
                  cu.email.strip(), cu.phone.strip(), cu.shipping_method.strip(), cu.notes.strip())
        with self.conn:
            if cu.id is None:
                cur = self.conn.execute(
                    """INSERT INTO customers (customer_no, billing_address, delivery_address, email, phone,
                                              shipping_method, notes) VALUES (?, ?, ?, ?, ?, ?, ?)""", values)
                cu.id = cur.lastrowid
                self._audit("KUNDE_ANGELEGT", "kunde", cu.id, self._customer_dict(self.get_customer(cu.id)))
            else:
                old = self.get_customer(cu.id)
                self.conn.execute(
                    """UPDATE customers SET customer_no = ?, billing_address = ?, delivery_address = ?,
                           email = ?, phone = ?, shipping_method = ?, notes = ? WHERE id = ?""",
                    (*values, cu.id))
                before, after = self._customer_dict(old), self._customer_dict(self.get_customer(cu.id))
                diff = {k: [before[k], after[k]] for k in before if before[k] != after[k]}
                if diff:
                    self._audit("KUNDE_GEAENDERT", "kunde", cu.id, {"kunden_nr": after["kunden_nr"],
                                                                    "aenderungen": diff})
        return cu.id

    def delete_customers(self, ids: list[int]) -> None:
        with self.conn:
            for customer_id in ids:
                old = self.get_customer(customer_id)
                if old is None:
                    continue
                self.conn.execute("DELETE FROM customers WHERE id = ?", (customer_id,))
                self._audit("KUNDE_GELOESCHT", "kunde", customer_id, self._customer_dict(old))

    def customer_document_counts(self) -> dict[int, int]:
        rows = self.conn.execute(
            "SELECT customer_id, COUNT(*) FROM documents WHERE customer_id IS NOT NULL GROUP BY customer_id")
        return {r[0]: r[1] for r in rows}

    # -------------------------------------------------------- shipping methods
    def list_shipping_methods(self) -> list[ShippingMethod]:
        rows = self.conn.execute("SELECT * FROM shipping_methods ORDER BY sort, id")
        return [ShippingMethod(r["id"], r["name"], r["price_cents"]) for r in rows]

    def replace_shipping_methods(self, methods: list[ShippingMethod]) -> None:
        old = [(m.name, m.price_cents) for m in self.list_shipping_methods()]
        new = [(m.name.strip(), m.price_cents) for m in methods if m.name.strip()]
        if old == new:
            return
        with self.conn:
            self.conn.execute("DELETE FROM shipping_methods")
            self.conn.executemany(
                "INSERT INTO shipping_methods (name, price_cents, sort) VALUES (?, ?, ?)",
                [(n, p, i) for i, (n, p) in enumerate(new)])
            self._audit("VERSANDARTEN_GEAENDERT", "einstellungen", "versand",
                        {"alt": [list(x) for x in old], "neu": [list(x) for x in new]})

    # ----------------------------------------------------------------- backup
    def backup_to(self, target: Path) -> None:
        dest = sqlite3.connect(target)
        try:
            self.conn.backup(dest)
        finally:
            dest.close()


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")
