# Änderungen

## 1.3.0
- Neues optionales Modul **Buchhaltung** (Einnahmen-Überschuss-Rechnung): Übersicht, Buchungsjournal,
  offene Posten, Bankabgleich (CSV, CAMT.053/.052, PayPal-CSV) mit Zuordnungsvorschlägen, Kassenbuch,
  Geldtransit, Privatentnahmen/-einlagen und EÜR-Auswertung als PDF.
- Buchungen sind unveränderbar (Storno per Gegenbuchung), Belege und Kontoauszüge werden mit Prüfsumme
  archiviert, der Kassenbestand kann nicht negativ werden.
- Integritätsprüfung, Datenexport für die Betriebsprüfung und Verfahrensdokumentation um die
  Buchhaltung erweitert.
- Automatische Tests (pytest) und GitHub-Workflows für Tests und Releases.

## 1.2.0
- GoBD: unveränderbare Belege, Stornorechnungen, automatische fortlaufende Nummern, revisionssicheres
  Archiv mit SHA-256, verkettetes Änderungsprotokoll, Integritätsprüfung.
- Datensicherung als ZIP inkl. Archiv, geschützte Wiederherstellung.
- Datenexport für die Betriebsprüfung (GDPdU-Beschreibungsstandard) und Verfahrensdokumentation (Word).
- Wasserzeichen „ENTWURF“ für Vorschauen, Plausibilitätsprüfung des Belegdatums.

## 1.1.0
- Kundenstamm mit Rechnungs- und Lieferadresse.
- Versandarten mit Standardpreisen, Versandkosten, Versandart und Sendungsnummer auf den Belegen.
- Firmenname im Briefkopf optional, Fußzeile mit Zeilenumbruch.

## 1.0.0
- Erste Version: Bestand, Rechnungen und Lieferscheine als PDF mit Logo, Einstellungen, Installer.
