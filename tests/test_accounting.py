import sqlite3
from datetime import date

import pytest

from cnctd_stock import accounting as acc
from cnctd_stock.database import GoBDError

from conftest import make_invoice

CSV = """Umsatzanzeige;Konto DE00
"Auftragskonto";"Buchungstag";"Valutadatum";"Buchungstext";"Verwendungszweck";"Beguenstigter/Zahlungspflichtiger";"Kontonummer/IBAN";"Betrag";"Waehrung"
"DE00";"02.03.26";"02.03.26";"GUTSCHRIFT";"Rechnung {nr} vielen Dank";"Erika Musterfrau";"DE11";"25,00";"EUR"
"DE00";"03.03.26";"03.03.26";"LASTSCHRIFT";"Material Bestellung 4711";"Material GmbH";"DE22";"-1.234,50";"EUR"
"DE00";"03.03.26";"03.03.26";"AUSZAHLUNG";"Geldautomat";"";"";"-50,00";"EUR"
"""

CAMT = b"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02"><BkToCstmrStmt><Stmt>
<Ntry><Amt Ccy="EUR">12.50</Amt><CdtDbtInd>DBIT</CdtDbtInd><BookgDt><Dt>2026-03-04</Dt></BookgDt>
<NtryDtls><TxDtls><RltdPties><Cdtr><Nm>Deutsche Post</Nm></Cdtr></RltdPties>
<RmtInf><Ustrd>Porto</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>
</Stmt></BkToCstmrStmt></Document>"""


@pytest.mark.parametrize("text,cents", [("1.234,56", 123456), ("-12,00", -1200), ("5,00 S", -500),
                                        ("1234.56", 123456), ("-0,99 EUR", -99), ("1.000", 100000)])
def test_parse_amount(text, cents):
    assert acc.parse_amount(text) == cents


def test_split_gross():
    assert acc.split_gross(11900, 19) == (10000, 1900)
    assert acc.split_gross(1000, 0) == (1000, 0)


def test_csv_import_matching_and_payment(tmp_path, db):
    db.save_settings({"small_business": 1})
    inv = make_invoice(db)                                  # 25,00 € (Kleinunternehmer, ohne USt.)
    path = tmp_path / "umsaetze.csv"
    path.write_text(CSV.format(nr=inv.number.replace("-", " ")), encoding="cp1252")
    header, rows = acc.read_csv(path)
    mapping = acc.auto_mapping(header)
    txs = acc.transactions_from_csv(header, rows, mapping)
    assert [t.amount_cents for t in txs] == [2500, -123450, -5000]
    assert acc.import_transactions(db, path, "BANK", txs).new == 3
    again = acc.import_transactions(db, path, "BANK", txs)
    assert (again.new, again.duplicates) == (0, 3)

    incoming = next(t for t in acc.list_bank_transactions(db) if t["amount_cents"] > 0)
    score, item = acc.suggest_invoices(db, incoming)[0]
    assert item.number == inv.number and score >= 100
    acc.record_invoice_payment(db, item.doc_id, date(2026, 3, 2), "BANK", 2500, incoming["id"])
    assert acc.invoice_items(db) == []
    assert acc.get_bank_tx(db, incoming["id"])["booking_id"] is not None


def test_camt_import(tmp_path, db):
    path = tmp_path / "auszug.xml"
    path.write_bytes(CAMT)
    txs = acc.read_camt_file(path)
    assert [(t.amount_cents, t.counterparty, t.purpose) for t in txs] == [(-1250, "Deutsche Post", "Porto")]


def test_expense_with_receipt_and_storno(tmp_path, db, archive):
    receipt = tmp_path / "beleg.pdf"
    receipt.write_bytes(b"%PDF-1.4 test")
    bid = acc.create_booking(db, acc.BookingInput(date(2026, 3, 3), "BANK", "wareneinkauf", -2380, 19,
                                                  "Material GmbH", "Filament", receipt_file=receipt))
    b = acc.get_booking(db, bid)
    assert (b["net_cents"], b["vat_cents"]) == (-2000, -380)
    assert (archive / b["receipt_path"]).is_file()
    for sql in ("DELETE FROM bookings", "UPDATE bookings SET gross_cents = 1"):
        with pytest.raises(sqlite3.DatabaseError):
            db.conn.execute(sql)
    db.conn.rollback()
    sid = acc.cancel_booking(db, bid, "falsch")
    assert acc.get_booking(db, sid)["gross_cents"] == 2380
    with pytest.raises(GoBDError):
        acc.cancel_booking(db, bid, "nochmal")


def test_cash_book_never_negative(db):
    with pytest.raises(GoBDError):
        acc.create_booking(db, acc.BookingInput(date(2026, 1, 5), "KASSE", "buero", -100, text="Stifte"))
    acc.create_transit(db, date(2026, 1, 5), "BANK", "KASSE", 5000)
    acc.create_booking(db, acc.BookingInput(date(2026, 1, 6), "KASSE", "einnahmen_ku", 1200, text="Barverkauf"))
    with pytest.raises(GoBDError):
        acc.create_booking(db, acc.BookingInput(date(2026, 1, 6), "KASSE", "buero", -10000, text="zu viel"))
    opening, rows = acc.cash_book(db, 2026)
    assert opening == 0 and rows[-1]["balance"] == 6200


def test_euer(db):
    db.save_settings({"small_business": 1})
    acc.create_booking(db, acc.BookingInput(date(2026, 2, 1), "BANK", "einnahmen_ku", 10000, text="Verkauf"))
    acc.create_booking(db, acc.BookingInput(date(2026, 2, 2), "BANK", "wareneinkauf", -3000, text="Material"))
    acc.create_booking(db, acc.BookingInput(date(2026, 2, 3), "BANK", "bewirtung", -1000, text="Essen"))
    acc.create_booking(db, acc.BookingInput(date(2026, 2, 4), "BANK", "privatentnahme", -5000, text="privat"))
    rep = acc.euer(db, 2026)
    assert rep.total_income == 10000
    assert rep.total_expenses == 3000 + 700          # Bewirtung nur zu 70 %
    assert rep.profit == 6300
