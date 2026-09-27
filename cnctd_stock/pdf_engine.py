"""Dokumenten-Engine: erzeugt Rechnungen und Lieferscheine als PDF (ReportLab)."""

import os
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from .database import DOC_CANCEL, DOC_DELIVERY, DOC_INVOICE, DOC_LABELS, PRICED_TYPES
from .formatting import fmt_money, fmt_qty, line_total_cents, percent_of

BRAND = colors.HexColor("#1D4ED8")
BRAND_LIGHT = colors.HexColor("#EEF3FF")
GREY = colors.HexColor("#5B6475")
LINE = colors.HexColor("#D5DBE7")

PAGE_W, PAGE_H = A4
MARGIN_L = 25 * mm
MARGIN_R = 20 * mm
CONTENT_W = PAGE_W - MARGIN_L - MARGIN_R
HEADER_H = 40 * mm      # reservierter Bereich für Logo + Absender auf Seite 1
FOOTER_H = 26 * mm


@dataclass
class DocItem:
    article_no: str
    name: str
    unit: str
    quantity: float
    unit_price_cents: int
    description: str = ""
    article_id: int | None = None   # Bezug zum Lagerartikel (für Rückbuchung bei Storno)

    @property
    def total_cents(self) -> int:
        return line_total_cents(self.quantity, self.unit_price_cents)


@dataclass
class DocumentData:
    doc_type: str                 # DOC_INVOICE, DOC_DELIVERY oder DOC_CANCEL
    number: str
    doc_date: date
    recipient: str                # mehrzeilige Anschrift
    items: list[DocItem]
    delivery_date: date | None = None
    customer_no: str = ""
    reference: str = ""
    notes: str = ""
    delivery_address: str = ""    # leer = Lieferung an Rechnungsadresse
    shipping_method: str = ""
    shipping_cents: int = 0       # Versandkosten (netto), nur Rechnung
    tracking_no: str = ""
    cancels_number: str = ""      # nur Stornorechnung: Nummer der stornierten Rechnung
    cancels_date: date | None = None
    cancel_reason: str = ""
    draft: bool = False           # Vorschau/Entwurf -> Wasserzeichen, kein gültiger Beleg
    settings: dict = field(default_factory=dict)


@dataclass
class Totals:
    goods_cents: int
    shipping_cents: int
    net_cents: int
    vat_rate: float
    vat_cents: int
    gross_cents: int


def compute_totals(items: list[DocItem], settings: dict, shipping_cents: int = 0) -> Totals:
    goods = sum(i.total_cents for i in items)
    net = goods + int(shipping_cents)
    if settings.get("small_business"):
        rate = 0.0
    else:
        rate = float(settings.get("vat_rate") or 0)
    vat = percent_of(net, rate)
    return Totals(goods, int(shipping_cents), net, rate, vat, net + vat)


def _esc(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _lines(text: str) -> str:
    return "<br/>".join(_esc(line) for line in (text or "").strip().splitlines())


def _de_date(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def safe_filename(text: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text).strip(" .") or "Dokument"


class _Styles:
    def __init__(self):
        base = dict(fontName="Helvetica", fontSize=9.5, leading=13, textColor=colors.black)
        self.body = ParagraphStyle("body", **base)
        self.small = ParagraphStyle("small", **{**base, "fontSize": 7.5, "leading": 9.5, "textColor": GREY})
        self.footer = ParagraphStyle("footer", **{**base, "fontSize": 7, "leading": 8.8, "textColor": GREY})
        self.sender_line = ParagraphStyle("sender_line", **{**base, "fontSize": 7, "leading": 9, "textColor": GREY})
        self.title = ParagraphStyle("title", **{**base, "fontName": "Helvetica-Bold", "fontSize": 17,
                                                "leading": 21, "textColor": BRAND})
        self.th = ParagraphStyle("th", **{**base, "fontName": "Helvetica-Bold", "fontSize": 8.5,
                                          "textColor": colors.white})
        self.th_r = ParagraphStyle("th_r", parent=self.th, alignment=TA_RIGHT)
        self.td = ParagraphStyle("td", **{**base, "fontSize": 9})
        self.td_r = ParagraphStyle("td_r", parent=self.td, alignment=TA_RIGHT)
        self.td_desc = ParagraphStyle("td_desc", **{**base, "fontSize": 7.5, "leading": 9.5, "textColor": GREY})
        self.meta_l = ParagraphStyle("meta_l", **{**base, "fontSize": 9, "textColor": GREY, "alignment": TA_LEFT})
        self.meta_r = ParagraphStyle("meta_r", **{**base, "fontSize": 9, "alignment": TA_RIGHT})
        self.right = ParagraphStyle("right", **{**base, "alignment": TA_RIGHT})
        self.right_bold = ParagraphStyle("right_bold", **{**base, "fontName": "Helvetica-Bold",
                                                          "fontSize": 11, "alignment": TA_RIGHT})


class DocumentRenderer:
    def __init__(self, data: DocumentData):
        self.d = data
        self.s = data.settings
        self.st = _Styles()
        self.totals = (compute_totals(data.items, self.s, data.shipping_cents)
                       if data.doc_type in PRICED_TYPES else None)
        self._logo = self._load_logo()

    # ------------------------------------------------------------ page frame
    def _load_logo(self):
        logo = self.s.get("logo_path") or ""
        if not logo or not os.path.isfile(logo):
            return None
        try:
            img = ImageReader(logo)
            img.getSize()
            return img
        except Exception:
            return None

    def _draw_logo(self, canvas, box_x: float, box_top: float, align_right: bool) -> None:
        if self._logo is None:
            return
        iw, ih = self._logo.getSize()
        max_w, max_h = 60 * mm, 26 * mm
        scale = min(max_w / iw, max_h / ih)
        w, h = iw * scale, ih * scale
        x = box_x - w if align_right else box_x
        canvas.drawImage(self._logo, x, box_top - h, width=w, height=h, mask="auto", preserveAspectRatio=True)

    def _sender_lines(self) -> list[str]:
        s = self.s
        lines = [s.get("sender_owner"), s.get("sender_street"), s.get("sender_zip_city"),
                 s.get("sender_country")]
        contact = []
        if s.get("sender_phone"):
            contact.append(f"Tel.: {s['sender_phone']}")
        if s.get("sender_email"):
            contact.append(s["sender_email"])
        if s.get("sender_website"):
            contact.append(s["sender_website"])
        return [x for x in lines if x] + contact

    def _watermark(self, canvas, doc=None) -> None:
        if not self.d.draft:
            return
        canvas.saveState()
        canvas.setFillColor(colors.Color(0.85, 0.15, 0.15, alpha=0.16))
        canvas.setFont("Helvetica-Bold", 64)
        canvas.translate(PAGE_W / 2, PAGE_H / 2)
        canvas.rotate(35)
        canvas.drawCentredString(0, 20, "ENTWURF")
        canvas.setFont("Helvetica-Bold", 22)
        canvas.drawCentredString(0, -20, "Vorschau – kein gültiger Beleg")
        canvas.restoreState()

    def _first_page(self, canvas, doc) -> None:
        canvas.saveState()
        top = PAGE_H - 15 * mm
        logo_right = (self.s.get("logo_position") or "rechts") == "rechts"

        # Logo auf der einen Seite, Absenderblock auf der anderen.
        if logo_right:
            self._draw_logo(canvas, PAGE_W - MARGIN_R, top, align_right=True)
            text_x, align = MARGIN_L, "left"
        else:
            self._draw_logo(canvas, MARGIN_L, top, align_right=False)
            text_x, align = PAGE_W - MARGIN_R, "right"

        draw = canvas.drawString if align == "left" else canvas.drawRightString
        y = top - 4 * mm
        name = self.s.get("sender_name") or ""
        # Mit Logo steht der Firmenname nur auf Wunsch zusätzlich im Kopf.
        if name and (self._logo is None or self.s.get("show_company_name")):
            canvas.setFont("Helvetica-Bold", 13)
            canvas.setFillColor(BRAND)
            draw(text_x, y, name)
            y -= 5 * mm
        canvas.setFont("Helvetica", 8.5)
        canvas.setFillColor(GREY)
        for line in self._sender_lines():
            draw(text_x, y, line)
            y -= 3.8 * mm

        # Akzentlinie unter dem Kopf
        canvas.setStrokeColor(BRAND)
        canvas.setLineWidth(1.2)
        canvas.line(MARGIN_L, PAGE_H - 15 * mm - HEADER_H + 6 * mm,
                    PAGE_W - MARGIN_R, PAGE_H - 15 * mm - HEADER_H + 6 * mm)
        canvas.restoreState()
        self._footer(canvas, doc)

    def _later_pages(self, canvas, doc) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica-Bold", 9)
        canvas.setFillColor(BRAND)
        canvas.drawString(MARGIN_L, PAGE_H - 15 * mm, f"{DOC_LABELS[self.d.doc_type]} {self.d.number}")
        canvas.setFont("Helvetica", 8.5)
        canvas.setFillColor(GREY)
        canvas.drawRightString(PAGE_W - MARGIN_R, PAGE_H - 15 * mm, self.s.get("sender_name") or "")
        canvas.setStrokeColor(LINE)
        canvas.line(MARGIN_L, PAGE_H - 17 * mm, PAGE_W - MARGIN_R, PAGE_H - 17 * mm)
        canvas.restoreState()
        self._footer(canvas, doc)

    def _footer(self, canvas, doc) -> None:
        s = self.s
        canvas.saveState()
        y_line = FOOTER_H - 4 * mm
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.6)
        canvas.line(MARGIN_L, y_line, PAGE_W - MARGIN_R, y_line)

        col1 = [s.get("sender_name"), s.get("sender_street"), s.get("sender_zip_city")]
        col2 = []
        if s.get("sender_phone"):
            col2.append(f"Tel.: {s['sender_phone']}")
        if s.get("sender_email"):
            col2.append(s["sender_email"])
        if s.get("tax_number"):
            col2.append(f"St.-Nr.: {s['tax_number']}")
        if s.get("vat_id"):
            col2.append(f"USt-IdNr.: {s['vat_id']}")
        col3 = []
        if s.get("bank_name"):
            col3.append(s["bank_name"])
        if s.get("iban"):
            col3.append(f"IBAN: {s['iban']}")
        if s.get("bic"):
            col3.append(f"BIC: {s['bic']}")

        # Spalten als Absätze setzen, damit lange Einträge (z. B. Bankname) umbrechen
        # statt über den Seitenrand zu laufen.
        col_w = CONTENT_W / 3
        gap = 3 * mm
        for idx, col in enumerate([[x for x in col1 if x], col2, col3]):
            if not col:
                continue
            para = Paragraph("<br/>".join(_esc(x) for x in col[:4]), self.st.footer)
            _, h = para.wrap(col_w - gap, y_line)
            para.drawOn(canvas, MARGIN_L + idx * col_w, y_line - 1.5 * mm - h)

        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(GREY)
        canvas.drawRightString(PAGE_W - MARGIN_R, 5 * mm, f"Seite {doc.page}")
        canvas.restoreState()

    # --------------------------------------------------------------- content
    def _address_block(self) -> Table:
        st = self.st
        s = self.s
        sender_short = " · ".join(x for x in [s.get("sender_name"), s.get("sender_street"),
                                               s.get("sender_zip_city")] if x)
        left = []
        if sender_short:
            left.append(Paragraph(_esc(sender_short), st.sender_line))
            left.append(Spacer(1, 1.5 * mm))
        # Der Lieferschein geht an die Lieferadresse, die Rechnung an die Rechnungsadresse.
        window = self.d.recipient
        if self.d.doc_type == DOC_DELIVERY and self.d.delivery_address.strip():
            window = self.d.delivery_address
        left.append(Paragraph(_lines(window) or "&nbsp;", st.body))

        meta_rows = [("Datum", _de_date(self.d.doc_date))]
        label_no = {DOC_INVOICE: "Rechnungs-Nr.", DOC_DELIVERY: "Lieferschein-Nr.",
                    DOC_CANCEL: "Storno-Nr."}[self.d.doc_type]
        meta_rows.insert(0, (label_no, self.d.number))
        if self.d.customer_no:
            meta_rows.append(("Kunden-Nr.", self.d.customer_no))
        if self.d.delivery_date:
            label = "Lieferdatum" if self.d.doc_type == DOC_DELIVERY else "Leistungsdatum"
            meta_rows.append((label, _de_date(self.d.delivery_date)))
        if self.d.reference:
            meta_rows.append(("Ihre Referenz", self.d.reference))
        if self.d.shipping_method:
            meta_rows.append(("Versandart", self.d.shipping_method))
        if self.d.tracking_no:
            meta_rows.append(("Sendungs-Nr.", self.d.tracking_no))
        if self.d.doc_type == DOC_CANCEL and self.d.cancels_number:
            meta_rows.append(("zu Rechnung", self.d.cancels_number))
            if self.d.cancels_date:
                meta_rows.append(("vom", _de_date(self.d.cancels_date)))
        if self.d.doc_type == DOC_INVOICE and int(s.get("payment_days") or 0) > 0:
            due = self.d.doc_date + timedelta(days=int(s["payment_days"]))
            meta_rows.append(("Fällig am", _de_date(due)))

        meta = Table(
            [[Paragraph(_esc(k), st.meta_l), Paragraph(_esc(v), st.meta_r)] for k, v in meta_rows],
            colWidths=[30 * mm, 40 * mm],
        )
        meta.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ]))

        block = Table([[left, meta]], colWidths=[CONTENT_W - 72 * mm, 72 * mm])
        block.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ]))
        return block

    def _item_cell(self, item: DocItem) -> list:
        cell = [Paragraph(_esc(item.name), self.st.td)]
        if item.description:
            cell.append(Paragraph(_lines(item.description), self.st.td_desc))
        return cell

    def _items_table(self) -> Table:
        st = self.st
        invoice = self.d.doc_type in PRICED_TYPES
        if invoice:
            header = ["Pos.", "Art.-Nr.", "Bezeichnung", "Menge", "Einzelpreis", "Gesamtpreis"]
            widths = [11 * mm, 24 * mm, None, 22 * mm, 25 * mm, 27 * mm]
        else:
            header = ["Pos.", "Art.-Nr.", "Bezeichnung", "Menge", "Einheit"]
            widths = [11 * mm, 28 * mm, None, 24 * mm, 20 * mm]
        fixed = sum(w for w in widths if w)
        widths = [w if w else CONTENT_W - fixed for w in widths]

        right_cols = {3, 4, 5} if invoice else {3}
        rows = [[Paragraph(h, st.th_r if i in right_cols else st.th) for i, h in enumerate(header)]]
        for pos, item in enumerate(self.d.items, start=1):
            if invoice:
                qty = f"{fmt_qty(item.quantity)} {_esc(item.unit)}".strip()
                rows.append([
                    Paragraph(str(pos), st.td),
                    Paragraph(_esc(item.article_no), st.td),
                    self._item_cell(item),
                    Paragraph(qty, st.td_r),
                    Paragraph(fmt_money(item.unit_price_cents), st.td_r),
                    Paragraph(fmt_money(item.total_cents), st.td_r),
                ])
            else:
                rows.append([
                    Paragraph(str(pos), st.td),
                    Paragraph(_esc(item.article_no), st.td),
                    self._item_cell(item),
                    Paragraph(fmt_qty(item.quantity), st.td_r),
                    Paragraph(_esc(item.unit), st.td),
                ])

        table = Table(rows, colWidths=widths, repeatRows=1)
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), BRAND),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("LINEBELOW", (0, 1), (-1, -1), 0.4, LINE),
        ]
        for r in range(2, len(rows), 2):
            style.append(("BACKGROUND", (0, r), (-1, r), BRAND_LIGHT))
        table.setStyle(TableStyle(style))
        return table

    def _totals_table(self) -> Table:
        st = self.st
        t = self.totals
        small = bool(self.s.get("small_business"))
        rows = []
        if t.shipping_cents:
            method = f" ({_esc(self.d.shipping_method)})" if self.d.shipping_method else ""
            rows.append([Paragraph("Summe Positionen", st.right), Paragraph(fmt_money(t.goods_cents), st.right)])
            rows.append([Paragraph(f"Versandkosten{method}", st.right),
                         Paragraph(fmt_money(t.shipping_cents), st.right)])
        if not small or not t.shipping_cents:
            rows.append([Paragraph("Zwischensumme (netto)" if not small else "Summe Positionen", st.right),
                         Paragraph(fmt_money(t.net_cents), st.right)])
        if not small:
            rate = fmt_qty(t.vat_rate)
            rows.append([Paragraph(f"zzgl. {rate} % USt.", st.right), Paragraph(fmt_money(t.vat_cents), st.right)])
        label = "Rechnungsbetrag" if not self.s.get("small_business") else "Gesamtbetrag"
        if self.d.doc_type == DOC_CANCEL:
            label = "Stornobetrag"
        rows.append([Paragraph(label, st.right_bold), Paragraph(fmt_money(t.gross_cents), st.right_bold)])
        table = Table(rows, colWidths=[CONTENT_W - 35 * mm, 35 * mm])
        table.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LINEABOVE", (0, -1), (-1, -1), 1.0, BRAND),
            ("TOPPADDING", (0, -1), (-1, -1), 5),
        ]))
        return table

    def _closing(self) -> list:
        st = self.st
        s = self.s
        out = []
        if self.d.doc_type == DOC_CANCEL:
            ref = _esc(self.d.cancels_number)
            if self.d.cancels_date:
                ref += f" vom {_de_date(self.d.cancels_date)}"
            out.append(Paragraph(f"Hiermit stornieren wir die Rechnung <b>{ref}</b> vollständig. "
                                 "Die Rechnung ist damit gegenstandslos; bereits geleistete Zahlungen "
                                 "werden erstattet bzw. verrechnet.", st.body))
            if self.d.cancel_reason:
                out.append(Paragraph(f"Grund: {_esc(self.d.cancel_reason)}", st.body))
            if s.get("small_business"):
                out.append(Paragraph("Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.", st.body))
        elif self.d.doc_type == DOC_INVOICE:
            if s.get("small_business"):
                out.append(Paragraph("Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.", st.body))
            days = int(s.get("payment_days") or 0)
            if days > 0:
                due = _de_date(self.d.doc_date + timedelta(days=days))
                text = (f"Bitte überweisen Sie den Rechnungsbetrag ohne Abzug bis zum <b>{due}</b> "
                        f"unter Angabe der Rechnungsnummer <b>{_esc(self.d.number)}</b>")
                text += f" auf das Konto IBAN {_esc(s['iban'])}." if s.get("iban") else "."
                out.append(Paragraph(text, st.body))
            if s.get("invoice_closing_text"):
                out.append(Spacer(1, 2 * mm))
                out.append(Paragraph(_lines(s["invoice_closing_text"]), st.body))
        else:
            if s.get("delivery_closing_text"):
                out.append(Paragraph(_lines(s["delivery_closing_text"]), st.body))
            out.append(Spacer(1, 16 * mm))
            sig = Table(
                [["", "", ""], ["Datum, Ort", "", "Unterschrift Empfänger"]],
                colWidths=[65 * mm, CONTENT_W - 130 * mm, 65 * mm],
            )
            sig.setStyle(TableStyle([
                ("LINEABOVE", (0, 1), (0, 1), 0.6, colors.black),
                ("LINEABOVE", (2, 1), (2, 1), 0.6, colors.black),
                ("FONT", (0, 1), (-1, 1), "Helvetica", 7.5),
                ("TEXTCOLOR", (0, 1), (-1, 1), GREY),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ]))
            out.append(sig)
        return out

    def _delivery_note_line(self) -> Paragraph | None:
        """Hinweis auf die jeweils andere Adresse, wenn Liefer- und Rechnungsadresse abweichen."""
        if not self.d.delivery_address.strip():
            return None
        if self.d.doc_type in PRICED_TYPES:
            label, text = "Lieferanschrift", self.d.delivery_address
        else:
            label, text = "Rechnungsanschrift", self.d.recipient
        parts = [x.strip() for x in text.splitlines() if x.strip()]
        return Paragraph(f"<b>{label}:</b> {_esc(', '.join(parts))}", self.st.small)

    def build(self, target: Path) -> Path:
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        label = DOC_LABELS[self.d.doc_type]
        doc = BaseDocTemplate(
            str(target), pagesize=A4,
            leftMargin=MARGIN_L, rightMargin=MARGIN_R,
            topMargin=15 * mm + HEADER_H, bottomMargin=FOOTER_H + 4 * mm,
            title=f"{label} {self.d.number}",
            author=self.s.get("sender_name") or "CNCTD.STOCK",
            creator="CNCTD.STOCK",
        )
        bottom = FOOTER_H + 4 * mm
        first_frame = Frame(MARGIN_L, bottom, CONTENT_W, PAGE_H - 15 * mm - HEADER_H - bottom,
                            id="first", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        later_frame = Frame(MARGIN_L, bottom, CONTENT_W, PAGE_H - 22 * mm - bottom,
                            id="later", leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
        doc.addPageTemplates([
            PageTemplate(id="First", frames=[first_frame], onPage=self._first_page,
                         onPageEnd=self._watermark, autoNextPageTemplate="Later"),
            PageTemplate(id="Later", frames=[later_frame], onPage=self._later_pages,
                         onPageEnd=self._watermark),
        ])
        st = self.st
        story = [self._address_block(), Spacer(1, 12 * mm),
                 Paragraph(f"{label} {_esc(self.d.number)}", st.title), Spacer(1, 4 * mm)]
        delivery = self._delivery_note_line()
        if delivery:
            story += [delivery, Spacer(1, 3 * mm)]
        if self.d.notes:
            story += [Paragraph(_lines(self.d.notes), st.body), Spacer(1, 4 * mm)]
        story.append(self._items_table())
        if self.totals:
            story += [Spacer(1, 3 * mm), KeepTogether(self._totals_table())]
        story += [Spacer(1, 8 * mm), KeepTogether(self._closing())]

        doc.build(story)
        return target


def build_document(data: DocumentData, target: Path) -> Path:
    return DocumentRenderer(data).build(target)


def document_to_dict(data: DocumentData) -> dict:
    """Vollständiger Datensatz eines Belegs (für Archiv-Snapshot und Reproduzierbarkeit)."""
    d = asdict(data)
    for key in ("doc_date", "delivery_date", "cancels_date"):
        if d.get(key):
            d[key] = d[key].isoformat()
    return d


def document_from_dict(d: dict) -> DocumentData:
    d = dict(d)
    for key in ("doc_date", "delivery_date", "cancels_date"):
        if d.get(key):
            d[key] = date.fromisoformat(d[key])
    d["items"] = [DocItem(**i) for i in d.get("items", [])]
    return DocumentData(**d)


def default_filename(data: DocumentData) -> str:
    label = DOC_LABELS[data.doc_type]
    first_line = (data.recipient or "").strip().splitlines()[0] if (data.recipient or "").strip() else ""
    parts = [label, data.number]
    if first_line:
        parts.append(first_line[:40])
    return safe_filename("_".join(parts)) + ".pdf"
