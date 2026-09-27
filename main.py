"""Startpunkt von CNCTD.STOCK."""

import sys
import traceback

from PySide6.QtCore import QLocale
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from cnctd_stock import APP_NAME, APP_VERSION, gobd
from cnctd_stock.database import Database
from cnctd_stock.paths import DATA_DIR, resource_path
from cnctd_stock.ui.common import AppState
from cnctd_stock.ui.main_window import MainWindow
from cnctd_stock.ui.style import STYLESHEET


def _set_windows_app_id() -> None:
    # Eigene AppUserModelID, damit Windows das App-Icon (statt des Python-Icons) in der Taskleiste zeigt.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"CNCTD.STOCK.{APP_VERSION}")
        except Exception:  # noqa: BLE001
            pass


def _excepthook(exc_type, exc, tb):
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        (DATA_DIR / "error.log").open("a", encoding="utf-8").write(text + "\n")
    except OSError:
        pass
    QMessageBox.critical(None, f"{APP_NAME} – Unerwarteter Fehler",
                         f"Es ist ein unerwarteter Fehler aufgetreten:\n\n{exc}\n\n"
                         f"Details wurden in {DATA_DIR / 'error.log'} protokolliert.")


def _selftest(target: str) -> int:
    """`CNCTD.STOCK.exe --selftest <datei.pdf>` – prüft die PDF-Engine ohne GUI (für den Build)."""
    from datetime import date

    from cnctd_stock.database import DOC_INVOICE, SETTINGS_COLUMNS
    from cnctd_stock.pdf_engine import DocItem, DocumentData, build_document

    settings = {k: v for k, (_, v) in SETTINGS_COLUMNS.items()}
    settings.update(sender_name="Selbsttest", logo_path=str(resource_path("assets/icon.png")))
    data = DocumentData(DOC_INVOICE, "TEST-0001", date.today(), "Test\nStraße 1\n12345 Ort",
                        [DocItem("A-1", "Prüfartikel äöü €", "Stk.", 2, 1999)], settings=settings)
    build_document(data, target)

    # Verfahrensdokumentation (prüft, ob python-docx samt Vorlagen gebündelt ist)
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "selftest.db")
        gobd.write_procedure_documentation(db, Path(target).with_suffix(".docx"))
        db.close()
    return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--selftest":
        return _selftest(sys.argv[2])
    _set_windows_app_id()
    QLocale.setDefault(QLocale(QLocale.German, QLocale.Germany))
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("CNCTD")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    app.setWindowIcon(QIcon(str(resource_path("assets/cnctd_stock.ico"))))
    sys.excepthook = _excepthook

    try:
        db = Database()
    except Exception as exc:  # noqa: BLE001
        QMessageBox.critical(None, APP_NAME, f"Die Datenbank konnte nicht geöffnet werden:\n{exc}")
        return 1

    # Belege aus Versionen vor dem GoBD-Modus einmalig ins revisionssichere Archiv übernehmen.
    try:
        migrated = gobd.migrate_legacy(db)
    except Exception as exc:  # noqa: BLE001
        migrated = 0
        QMessageBox.warning(None, APP_NAME, f"Altbestand konnte nicht vollständig archiviert werden:\n{exc}")
    if migrated or db.migration_warnings:
        text = ""
        if migrated:
            text += (f"GoBD-Modus aktiviert: {migrated} bestehende(r) Beleg(e) wurden ins revisionssichere "
                     "Archiv übernommen.\n\nAb jetzt sind Belege unveränderbar, Korrekturen erfolgen per Storno, "
                     "und alle Änderungen werden protokolliert.")
        if db.migration_warnings:
            text += "\n\n" + "\n".join(db.migration_warnings)
        QMessageBox.information(None, APP_NAME, text.strip())

    window = MainWindow(AppState(db))
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
