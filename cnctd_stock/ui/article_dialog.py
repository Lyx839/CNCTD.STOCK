"""Dialog zum Anlegen und Bearbeiten eines Artikels."""

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
)

from ..database import Article, Database
from ..formatting import cents_to_euros, euros_to_cents, fmt_money, line_total_cents

UNITS = ["Stk.", "Paket", "Karton", "Palette", "Set", "kg", "g", "m", "m²", "l", "Std.", "pauschal"]


class ArticleDialog(QDialog):
    def __init__(self, db: Database, article: Article | None = None, parent=None):
        super().__init__(parent)
        self.db = db
        self.article = article
        self.setWindowTitle("Artikel bearbeiten" if article else "Artikel hinzufügen")
        self.setMinimumWidth(460)

        self.no_edit = QLineEdit()
        self.no_edit.setMaxLength(40)
        self.name_edit = QLineEdit()
        self.name_edit.setMaxLength(200)
        self.desc_edit = QPlainTextEdit()
        self.desc_edit.setPlaceholderText("Optional – erscheint klein unter der Bezeichnung im Dokument")
        self.desc_edit.setFixedHeight(70)
        self.unit_combo = QComboBox()
        self.unit_combo.setEditable(True)
        self.unit_combo.addItems(UNITS)

        self.qty_spin = QDoubleSpinBox()
        self.qty_spin.setRange(-1_000_000_000, 1_000_000_000)
        self.qty_spin.setDecimals(2)
        self.qty_spin.setGroupSeparatorShown(True)

        self.price_spin = QDoubleSpinBox()
        self.price_spin.setRange(0, 100_000_000)
        self.price_spin.setDecimals(2)
        self.price_spin.setSuffix(" €")
        self.price_spin.setGroupSeparatorShown(True)

        self.total_label = QLabel()
        self.total_label.setObjectName("TotalBig")

        form = QFormLayout()
        form.setSpacing(10)
        form.addRow("Artikel-Nr. *", self.no_edit)
        form.addRow("Bezeichnung *", self.name_edit)
        form.addRow("Beschreibung", self.desc_edit)
        form.addRow("Einheit", self.unit_combo)
        form.addRow("Menge (Bestand)", self.qty_spin)
        form.addRow("Stückpreis (netto)", self.price_spin)
        form.addRow("Gesamtpreis", self.total_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("Speichern")
        buttons.button(QDialogButtonBox.Save).setObjectName("Primary")
        buttons.button(QDialogButtonBox.Cancel).setText("Abbrechen")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.addLayout(form)
        layout.addSpacing(8)
        layout.addWidget(buttons)

        self.qty_spin.valueChanged.connect(self._update_total)
        self.price_spin.valueChanged.connect(self._update_total)

        if article:
            self.no_edit.setText(article.article_no)
            self.name_edit.setText(article.name)
            self.desc_edit.setPlainText(article.description)
            self.unit_combo.setCurrentText(article.unit)
            self.qty_spin.setValue(article.quantity)
            self.price_spin.setValue(cents_to_euros(article.unit_price_cents))
        else:
            self.no_edit.setText(db.suggest_article_no())
            self.unit_combo.setCurrentText("Stk.")
        self._update_total()
        self.name_edit.setFocus()

    def _update_total(self) -> None:
        cents = line_total_cents(self.qty_spin.value(), euros_to_cents(self.price_spin.value()))
        self.total_label.setText(fmt_money(cents))

    def _save(self) -> None:
        no = self.no_edit.text().strip()
        name = self.name_edit.text().strip()
        if not no or not name:
            QMessageBox.warning(self, "Pflichtfelder", "Bitte Artikel-Nr. und Bezeichnung ausfüllen.")
            return
        exclude = self.article.id if self.article else None
        if self.db.article_no_exists(no, exclude):
            QMessageBox.warning(self, "Artikel-Nr. vergeben",
                                f"Die Artikel-Nr. „{no}“ ist bereits vergeben.")
            return
        result = Article(
            id=self.article.id if self.article else None,
            article_no=no,
            name=name,
            description=self.desc_edit.toPlainText().strip(),
            unit=self.unit_combo.currentText().strip() or "Stk.",
            quantity=round(self.qty_spin.value(), 3),
            unit_price_cents=euros_to_cents(self.price_spin.value()),
        )
        if self.article:
            self.db.update_article(result)
        else:
            result.id = self.db.add_article(result)
        self.article = result
        self.accept()
