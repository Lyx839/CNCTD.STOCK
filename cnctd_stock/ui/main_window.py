"""Hauptfenster mit Kopfleiste und den drei Bereichen Bestand, Dokumente, Einstellungen."""

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMainWindow, QTabWidget, QVBoxLayout, QWidget

from .. import APP_NAME, APP_VERSION
from ..paths import resource_path
from .common import AppState
from .accounting_tab import AccountingTab
from .customers_tab import CustomersTab
from .documents_tab import DocumentsTab
from .inventory_tab import InventoryTab
from .settings_tab import SettingsTab


class MainWindow(QMainWindow):
    def __init__(self, state: AppState):
        super().__init__()
        self.state = state
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(str(resource_path("assets/cnctd_stock.ico"))))
        self.setMinimumSize(1100, 700)

        header = QFrame()
        header.setObjectName("HeaderBar")
        header.setFixedHeight(64)
        logo = QLabel()
        pix = QPixmap(str(resource_path("assets/icon.png")))
        if not pix.isNull():
            logo.setPixmap(pix.scaled(44, 44, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        title = QLabel("CNCTD")
        title.setObjectName("AppTitle")
        dot = QLabel(".")
        dot.setObjectName("AppTitleAccent")
        title2 = QLabel("STOCK")
        title2.setObjectName("AppTitle")
        subtitle = QLabel("Lagerbestand · Rechnungen · Lieferscheine · Buchhaltung")
        subtitle.setObjectName("AppSubtitle")
        h = QHBoxLayout(header)
        h.setContentsMargins(18, 8, 22, 8)
        h.setSpacing(0)
        h.addWidget(logo)
        h.addSpacing(12)
        h.addWidget(title)
        h.addWidget(dot)
        h.addWidget(title2)
        h.addSpacing(18)
        h.addWidget(subtitle)
        h.addStretch(1)
        version = QLabel(f"v{APP_VERSION}")
        version.setObjectName("AppSubtitle")
        h.addWidget(version)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.inventory = InventoryTab(state)
        self.documents = DocumentsTab(state)
        self.customers = CustomersTab(state)
        self.accounting = AccountingTab(state)
        self.settings_tab = SettingsTab(state)
        self.tabs.addTab(self.inventory, "📦  Bestand")
        self.tabs.addTab(self.documents, "🧾  Dokumente erstellen")
        self.tabs.addTab(self.customers, "👥  Kunden")
        self.tabs.addTab(self.accounting, "💶  Buchhaltung")
        self.tabs.addTab(self.settings_tab, "⚙  Einstellungen")
        self.customers.createDocumentRequested.connect(self._document_for_customer)
        state.settingsChanged.connect(self._update_modules)
        self._update_modules()

        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(header)
        v.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage("Bereit.")

        self._qsettings = QSettings("CNCTD", APP_NAME)
        geometry = self._qsettings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        else:
            self.resize(1320, 840)

        # Beim ersten Start direkt zu den Einstellungen, damit der Absender gepflegt wird.
        if not state.db.get_settings().get("sender_name"):
            self.tabs.setCurrentWidget(self.settings_tab)
            self.statusBar().showMessage(
                "Willkommen! Bitte zuerst Absenderdaten und Logo hinterlegen und speichern.")

    def _update_modules(self) -> None:
        enabled = bool(self.state.db.get_settings().get("accounting_enabled"))
        self.tabs.setTabVisible(self.tabs.indexOf(self.accounting), enabled)

    def _document_for_customer(self, customer_id: int) -> None:
        self.documents.select_customer(customer_id)
        self.tabs.setCurrentWidget(self.documents)

    def closeEvent(self, event):
        self._qsettings.setValue("geometry", self.saveGeometry())
        self.state.db.close()
        super().closeEvent(event)
