import os
import sqlite3
import stat
import zipfile
from datetime import date

import pytest

from cnctd_stock import gobd
from cnctd_stock.database import DOC_DELIVERY, STATUS_CANCELLED, Database, GoBDError
from cnctd_stock.pdf_engine import DocItem, DocumentData

from conftest import make_invoice


def test_numbering_archive_and_readonly(tmp_path, db, archive):
    first = make_invoice(db, output_dir=tmp_path / "pdf")
    second = make_invoice(db)
    assert (first.number[-4:], second.number[-4:]) == ("0001", "0002")
    assert first.archive_file.is_file() and not os.access(first.archive_file, os.W_OK)
    assert first.work_copy is not None and first.work_copy.is_file()


def test_documents_and_log_are_immutable(db):
    doc = make_invoice(db)
    for sql in (f"DELETE FROM documents WHERE id = {doc.doc_id}",
                f"UPDATE documents SET total_cents = 1 WHERE id = {doc.doc_id}",
                "UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log"):
        with pytest.raises(sqlite3.DatabaseError):
            db.conn.execute(sql)
    db.conn.rollback()
    with pytest.raises(GoBDError):
        db.save_settings({"next_invoice_no": 1})


def test_cancel_invoice_with_restock(db, article):
    items = [DocItem(article.article_no, article.name, "Stk.", 3, article.unit_price_cents, article_id=article.id)]
    doc = make_invoice(db, items, shipping_cents=499, deductions={article.id: 3})
    assert db.get_article(article.id).quantity == 7
    storno = gobd.cancel_document(db, doc.doc_id, "Falscher Preis", restock=True)
    orig, st = db.get_document(doc.doc_id), db.get_document(storno.doc_id)
    assert orig["status"] == STATUS_CANCELLED and st["total_cents"] == -orig["total_cents"]
    assert db.get_article(article.id).quantity == 10
    with pytest.raises(GoBDError):
        gobd.cancel_document(db, doc.doc_id, "nochmal", restock=False)


def test_cancel_delivery_note(db):
    data = DocumentData(DOC_DELIVERY, "", date.today(), "Kunde", [DocItem("A", "X", "Stk.", 1, 0)],
                        settings=db.get_settings())
    ls = gobd.create_document(db, data)
    assert gobd.cancel_document(db, ls.doc_id, "doppelt", restock=False) is None
    assert db.get_document(ls.doc_id)["status"] == STATUS_CANCELLED


def test_integrity_detects_manipulation(db):
    doc = make_invoice(db)
    assert gobd.verify_integrity(db, log=False).ok
    os.chmod(doc.archive_file, stat.S_IWRITE)
    doc.archive_file.write_bytes(doc.archive_file.read_bytes() + b"x")
    assert not gobd.verify_integrity(db, log=False).ok
    doc.archive_file.write_bytes(doc.archive_file.read_bytes()[:-1])
    db.conn.execute("DROP TRIGGER audit_log_no_update")
    db.conn.execute("UPDATE audit_log SET details = '{}' WHERE id = 2")
    db.conn.commit()
    assert not gobd.verify_integrity(db, log=False).chain_ok


def test_backup_restore_rules(tmp_path, db, archive):
    make_invoice(db)
    old = gobd.create_backup(db, tmp_path / "alt.zip")
    make_invoice(db)
    new = gobd.create_backup(db, tmp_path / "neu.zip")
    with pytest.raises(GoBDError):
        gobd.restore_backup(db, old)          # würde einen Beleg entfernen
    gobd.restore_backup(db, new)
    # Neuer Rechner: leere Datenbank, Archiv verloren
    archive.rename(tmp_path / "archiv_alt")
    fresh = Database(tmp_path / "frisch.db")
    gobd.restore_backup(fresh, new)
    assert len(fresh.list_documents()) == 2
    assert gobd.verify_integrity(fresh, log=False).ok
    fresh.close()


def test_tax_audit_export(tmp_path, db):
    make_invoice(db)
    target, count = gobd.export_tax_audit(db, tmp_path / "z3.zip", date.today().year)
    names = zipfile.ZipFile(target).namelist()
    assert count == 1
    assert {"index.xml", "belege.csv", "buchungen.csv", "protokoll.csv"} <= set(names)
    assert any(n.startswith("pdf/") for n in names)


def test_procedure_documentation(tmp_path, db):
    target = gobd.write_procedure_documentation(db, tmp_path / "vd.docx", {"taetigkeit": "Test"}, log=False)
    assert zipfile.is_zipfile(target)
