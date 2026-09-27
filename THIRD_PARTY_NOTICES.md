# Verwendete Software von Drittanbietern

CNCTD.STOCK selbst steht unter der [MIT-Lizenz](LICENSE). Die ausgelieferte `.exe` enthält zusätzlich
folgende Bibliotheken, die unter ihren eigenen Lizenzen stehen:

| Komponente | Zweck | Lizenz |
|---|---|---|
| [Python](https://www.python.org/) | Laufzeitumgebung | PSF License |
| [Qt for Python / PySide6](https://doc.qt.io/qtforpython/) | Benutzeroberfläche | LGPL-3.0 |
| [ReportLab](https://www.reportlab.com/opensource/) | PDF-Erzeugung | BSD-3-Clause |
| [Pillow](https://python-pillow.org/) | Bildverarbeitung (Icon, Logo) | MIT-CMU (HPND) |
| [python-docx](https://github.com/python-openxml/python-docx) | Verfahrensdokumentation (Word) | MIT |
| [SQLite](https://www.sqlite.org/) | Datenbank | Public Domain |
| [PyInstaller](https://pyinstaller.org/) | Erstellung der `.exe` | GPL-2.0 mit Bootloader-Ausnahme (die erzeugte `.exe` darf unter beliebiger Lizenz verteilt werden) |
| [Inno Setup](https://jrsoftware.org/isinfo.php) | Erstellung des Installers | Inno Setup License (kostenlos für nicht-kommerzielle Nutzung) |

**Hinweis zu Qt/PySide6 (LGPL-3.0):** Qt wird unverändert verwendet. Der vollständige Quellcode von
CNCTD.STOCK liegt in diesem Repository; die Anwendung kann mit einer anderen Qt-/PySide6-Version neu
gebaut werden (siehe README, Abschnitt „Aus dem Quellcode bauen“). Den Quellcode von Qt erhalten Sie
unter https://download.qt.io/.
