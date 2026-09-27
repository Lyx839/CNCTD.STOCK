"""Erzeugt die Screenshots für README/GitHub mit frei erfundenen Demo-Daten.

Aufruf:  .venv\\Scripts\\python tools\\make_screenshots.py
Die Daten entstehen in einem temporären Ordner – echte Daten werden nicht verwendet.
"""

import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="cnctd_demo_"))
os.environ["CNCTD_STOCK_DATA_DIR"] = str(TMP / "data")
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QLocale, QSize, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from cnctd_stock import accounting as acc  # noqa: E402
from cnctd_stock import gobd  # noqa: E402
from cnctd_stock.database import DOC_INVOICE, Article, Customer, Database  # noqa: E402
from cnctd_stock.pdf_engine import DocItem, DocumentData  # noqa: E402

OUT = ROOT / "docs" / "screenshots"


def demo_database() -> Database:
    db = Database()
    logo = TMP / "logo.png"
    shutil.copyfile(ROOT / "assets" / "icon.png", logo)
    db.save_settings({
        "sender_name": "Muster Manufaktur", "sender_owner": "Max Mustermann", "sender_street": "Musterweg 12",
        "sender_zip_city": "12345 Musterstadt", "sender_country": "Deutschland", "sender_phone": "01234 567890",
        "sender_email": "info@muster-manufaktur.example", "sender_website": "muster-manufaktur.example",
        "tax_number": "123/456/78901", "bank_name": "Musterbank", "iban": "DE00 1234 5678 9012 3456 78",
        "bic": "MUSTDEXXX", "small_business": 1, "logo_path": str(logo), "accounting_enabled": 1,
        "pdf_output_dir": str(TMP / "pdf")})
    articles = [("A-00001", "Kerze Sojawachs 200 g", "Stk.", 48, 1290), ("A-00002", "Keramikbecher handbemalt", "Stk.", 12, 2490),
                ("A-00003", "Schneidebrett Eiche", "Stk.", 7, 3490), ("A-00004", "Geschenkbox klein", "Stk.", 0, 390),
                ("A-00005", "Postkarte A6 Motiv „Herbst“", "Stk.", 250, 150), ("A-00006", "Leinenband natur", "m", 85.5, 80),
                ("A-00007", "Duftöl Lavendel 10 ml", "Stk.", 30, 690), ("A-00008", "Versandkarton M", "Stk.", 140, 95)]
    ids = {}
    for no, name, unit, qty, price in articles:
        ids[no] = db.add_article(Article(None, no, name, "", unit, qty, price))
    db.replace_shipping_methods([acc_method("DHL Paket", 619), acc_method("DHL Päckchen", 419),
                                 acc_method("Deutsche Post Warensendung", 195), acc_method("Abholung", 0)])
    customers = [("Erika Musterfrau\nBeispielweg 5\n54321 Beispielstadt", "Erika Musterfrau\nPackstation 101\n54321 Beispielstadt"),
                 ("Café Sonnenschein\nMarktplatz 3\n98765 Sonnental", ""),
                 ("Jonas Beispiel\nLindenallee 8\n24680 Neudorf", "")]
    cust_ids = [db.save_customer(Customer(None, db.suggest_customer_no(), b, d, email="kontakt@example.org",
                                          shipping_method="DHL Paket")) for b, d in customers]
    return db, ids, cust_ids


def acc_method(name, price):
    from cnctd_stock.database import ShippingMethod
    return ShippingMethod(None, name, price)


def demo_bookings(db, ids, cust_ids):
    s = db.get_settings()
    today = date.today()
    invoices = []
    for i, (cust, items) in enumerate([(0, [("A-00001", 2), ("A-00007", 1)]), (1, [("A-00002", 6)]),
                                       (2, [("A-00003", 1), ("A-00005", 4)])]):
        cu = db.get_customer(cust_ids[cust])
        doc_items = []
        for no, qty in items:
            a = db.get_article(ids[no])
            doc_items.append(DocItem(a.article_no, a.name, a.unit, qty, a.unit_price_cents, article_id=a.id))
        data = DocumentData(DOC_INVOICE, "", today - timedelta(days=20 - i * 6), cu.billing_address, doc_items,
                            delivery_date=today - timedelta(days=20 - i * 6), customer_no=cu.customer_no,
                            delivery_address=cu.delivery_address, shipping_method="DHL Paket", shipping_cents=619,
                            settings=s)
        invoices.append(gobd.create_document(db, data, customer_id=cu.id))
    acc.create_booking(db, acc.BookingInput(today - timedelta(days=25), "KASSE", "anfangsbestand", 15000,
                                            text="Anfangsbestand Kasse"))
    first = acc.open_item(db, invoices[0].doc_id)
    acc.record_invoice_payment(db, first.doc_id, today - timedelta(days=12), "BANK", first.open_cents)
    for d, cat, amount, who, text in [(18, "wareneinkauf", -8990, "Wachs & Docht GmbH", "Sojawachs 5 kg"),
                                      (15, "porto", -1857, "Deutsche Post", "Versandmarken"),
                                      (9, "gebuehren", -490, "Musterbank", "Kontoführung"),
                                      (6, "werbung", -2400, "Druckerei Beispiel", "Flyer")]:
        acc.create_booking(db, acc.BookingInput(today - timedelta(days=d), "BANK", cat, amount, counterparty=who,
                                                text=text))
    acc.create_booking(db, acc.BookingInput(today - timedelta(days=4), "KASSE", "einnahmen_ku", 4870,
                                            text="Barverkauf Wochenmarkt"))
    acc.create_booking(db, acc.BookingInput(today - timedelta(days=4), "KASSE", "buero", -350, text="Quittungsblock"))


def main() -> None:
    QLocale.setDefault(QLocale(QLocale.German, QLocale.Germany))
    app = QApplication([])
    from cnctd_stock.ui.common import AppState
    from cnctd_stock.ui.main_window import MainWindow
    from cnctd_stock.ui.style import STYLESHEET
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)

    db, ids, cust_ids = demo_database()
    demo_bookings(db, ids, cust_ids)
    OUT.mkdir(parents=True, exist_ok=True)

    w = MainWindow(AppState(db))
    w.setAttribute(Qt.WA_DontShowOnScreen)
    w.resize(1320, 840)
    w.show()
    app.processEvents()

    def shot(name):
        app.processEvents()
        w.grab().save(str(OUT / f"{name}.png"))

    w.tabs.setCurrentWidget(w.inventory)
    shot("bestand")
    d = w.documents
    d.select_customer(cust_ids[0])
    d.add_to_cart([ids["A-00001"], ids["A-00007"], ids["A-00008"]])
    w.tabs.setCurrentWidget(d)
    shot("dokumente")
    w.tabs.setCurrentWidget(w.customers)
    shot("kunden")
    w.tabs.setCurrentWidget(w.accounting)
    for i, name in [(0, "buchhaltung"), (1, "journal"), (5, "euer")]:
        w.accounting.tabs.setCurrentIndex(i)
        shot(name)

    from PySide6.QtPdf import QPdfDocument
    first = db.list_documents()[-1]
    pdf = QPdfDocument()
    pdf.load(str(gobd.archive_file(first)))
    pdf.render(0, QSize(900, 1273)).save(str(OUT / "rechnung.png"))
    print("Screenshots gespeichert in", OUT)


if __name__ == "__main__":
    main()
