"""Pfad-Auflösung für Entwicklungsmodus und gepackte PyInstaller-.exe."""

import os
import sys
from pathlib import Path

from . import APP_NAME


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def resource_path(relative: str) -> Path:
    """Pfad zu gebündelten Ressourcen (Icon usw.), auch innerhalb der --onefile-.exe."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / relative


def app_dir() -> Path:
    """Ordner, in dem die .exe (bzw. main.py) liegt."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _is_writable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _resolve_data_dir() -> Path:
    # Explizit vorgegebener Datenordner (Tests, Demo-Daten, mehrere Mandanten).
    override = os.environ.get("CNCTD_STOCK_DATA_DIR")
    if override:
        folder = Path(override)
        folder.mkdir(parents=True, exist_ok=True)
        return folder
    # Portabler Modus: Daten liegen im Unterordner "data" neben der .exe,
    # solange dieser beschreibbar ist (z. B. USB-Stick, Desktop-Ordner).
    portable = app_dir() / "data"
    if _is_writable(portable):
        return portable
    # Installierte Variante (z. B. unter "Programme"): Benutzerprofil verwenden.
    base = Path(os.environ.get("APPDATA", Path.home()))
    fallback = base / APP_NAME
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


DATA_DIR = _resolve_data_dir()
DB_PATH = DATA_DIR / "cnctd_stock.db"
ASSETS_DIR = DATA_DIR / "assets"
DEFAULT_PDF_DIR = Path.home() / "Documents" / APP_NAME
