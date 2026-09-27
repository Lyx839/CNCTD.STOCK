"""Gemeinsame Test-Fixtures. Alle Tests laufen in temporären Ordnern – echte Daten werden nie berührt."""

import os
import sys
import tempfile
from datetime import date
from pathlib import Path

# Muss vor dem ersten Import von cnctd_stock gesetzt werden (Datenordner wird beim Import bestimmt).
os.environ["CNCTD_STOCK_DATA_DIR"] = tempfile.mkdtemp(prefix="cnctd_stock_test_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from cnctd_stock import accounting, gobd  # noqa: E402
from cnctd_stock.database import DOC_INVOICE, Article, Database  # noqa: E402
from cnctd_stock.pdf_engine import DocItem, DocumentData  # noqa: E402


@pytest.fixture
def archive(tmp_path, monkeypatch):
    folder = tmp_path / "archiv"
    monkeypatch.setattr(gobd, "ARCHIVE_DIR", folder)
    monkeypatch.setattr(accounting, "ARCHIVE_DIR", folder)
    monkeypatch.setattr(gobd, "AUTO_BACKUP_DIR", tmp_path / "sicherungen")
    return folder


@pytest.fixture
def db(tmp_path, archive):
    database = Database(tmp_path / "test.db")
    database.save_settings({"sender_name": "Muster Manufaktur", "sender_street": "Musterweg 1",
                            "sender_zip_city": "12345 Musterstadt", "pdf_output_dir": str(tmp_path / "pdf")})
    yield database
    database.close()


@pytest.fixture
def article(db):
    a = Article(None, "A-00001", "Testartikel", "", "Stk.", 10, 1250)
    db.add_article(a)
    return a


def make_invoice(db, items=None, shipping_cents=0, **kwargs):
    items = items or [DocItem("A-00001", "Testartikel", "Stk.", 2, 1250)]
    data = DocumentData(DOC_INVOICE, "", date.today(), "Erika Musterfrau\nBeispielweg 5\n54321 Beispielstadt",
                        items, shipping_cents=shipping_cents, settings=db.get_settings())
    return gobd.create_document(db, data, **kwargs)
