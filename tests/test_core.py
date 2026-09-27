from datetime import date

import pytest

from cnctd_stock.database import DOC_DELIVERY, DOC_INVOICE, Article, DuplicateArticleNo
from cnctd_stock.formatting import fmt_money, fmt_qty, line_total_cents, percent_of
from cnctd_stock.pdf_engine import DocItem, DocumentData, build_document, compute_totals


def test_german_formatting():
    assert fmt_money(123456) == "1.234,56 €"
    assert fmt_money(-5) == "-0,05 €"
    assert fmt_qty(2.5) == "2,5"
    assert fmt_qty(1000) == "1.000"
    assert line_total_cents(480.5, 95) == 45648
    assert percent_of(1000, 19) == 190


def test_totals_with_vat_and_shipping():
    items = [DocItem("A", "X", "Stk.", 2, 1000)]
    t = compute_totals(items, {"vat_rate": 19, "small_business": 0}, shipping_cents=500)
    assert (t.goods_cents, t.net_cents, t.vat_cents, t.gross_cents) == (2000, 2500, 475, 2975)
    t = compute_totals(items, {"vat_rate": 19, "small_business": 1}, shipping_cents=500)
    assert (t.vat_cents, t.gross_cents) == (0, 2500)


def test_articles_are_logged(db):
    a = Article(None, "A-1", "Schraube", "", "Stk.", 5, 10)
    db.add_article(a)
    a.quantity = 7
    db.update_article(a)
    with pytest.raises(DuplicateArticleNo):
        db.add_article(Article(None, "a-1", "Doppelt", "", "Stk.", 1, 1))
    db.delete_articles([a.id])
    actions = [r["action"] for r in db.list_audit()]
    assert actions[:3] == ["ARTIKEL_GELOESCHT", "ARTIKEL_GEAENDERT", "ARTIKEL_ANGELEGT"]
    ok, _, _ = db.verify_audit_chain()
    assert ok


@pytest.mark.parametrize("doc_type", [DOC_INVOICE, DOC_DELIVERY])
def test_pdf_is_rendered(tmp_path, db, doc_type):
    data = DocumentData(doc_type, "X-1", date.today(), "Kunde\nStraße 1\n12345 Ort",
                        [DocItem("A", f"Artikel {i}", "Stk.", 1, 100) for i in range(60)],
                        delivery_address="Packstation 1\n12345 Ort", shipping_method="DHL Paket",
                        shipping_cents=499, settings=db.get_settings(), draft=True)
    target = build_document(data, tmp_path / "doc.pdf")
    content = target.read_bytes()
    assert content.startswith(b"%PDF") and content.count(b"/Type /Page\n") + content.count(b"/Type /Page ") >= 2
