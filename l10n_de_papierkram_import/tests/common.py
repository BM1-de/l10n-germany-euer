# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Fictitious Papierkram exports for tests."""

import os
import zipfile

H_RECHNUNGEN = (
    "Rechnungsnr.;Betreff;Kunde;Land;Projekt;Kundennummer;Datum;Fälligkeitsdatum;"
    "Status;Typ;Netto;USt.;Brutto;Skonto;Bezahlt;letztes Bezahldatum;Notiz;"
    "Mahnungsdatum;Waren & Dienstleistungen"
)
H_BELEGE = (
    "Belegnummer;Gegenstand;Lieferant;Datum;Zahlungsfrist;Zahlung am;Status;Netto;"
    "USt.;Brutto;Privatanteil;Herkunftsort;Kategorien;Projekte;Projektkunden;"
    "Projektkunden-Nr;Zusatz;Notizen"
)
H_ZAHLUNGEN = "Buchung;Kundennummer;Rechnungsnummer;Belegnummer;Datum;Betrag"
H_KONTAKTE = (
    "Name;Kontaktart;Kunden-/Lieferantennr.;Straße (Post);PLZ (Post);Stadt (Post);"
    "Land (Post);Straße (Haus);PLZ (Haus);Stadt (Haus);Land;Website;E-Mail;Telefon;"
    "Fax;UStID;Notiz;Kontonummer;Bankname;BLZ;IBAN;BIC;SEPA-Mandatsreferenz;"
    "SEPA-Lastschrift akzeptiert;Anrede (Standardempfänger);Titel (Standardempfänger);"
    "Vorname (Standardempfänger);Nachname (Standardempfänger);"
    "E-Mail (Standardempfänger);Telefonnummer (Standardempfänger);"
    "Fax (Standardempfänger);Handynummer (Standardempfänger);"
    "Skype (Standardempfänger);Notiz (Standardempfänger)"
)
H_KONTOUMSAETZE = (
    "Status;Zahlungswert;Zahlungswährung;Storniert;Kunden-Referenz;Instref;GV-Code;"
    "Kategorie;Verwendungszweck;Primanota;SEPA?;Absender-IBAN;Absender-BIC;"
    "Absender-Kontonummer;Absender-BLZ;Absender-Währung;Absender-Name;Saldo-Wert;"
    "Saldo-Währung;Saldo-Zeitpunkt;Bankverbindung-ID;Valuta;Buchungsdatum;"
    "Importierungsdatum"
)

KATEGORIEN = (
    "name;skr03;konto_name;konto_typ;abschreibungsart;typ\n"
    "Porto;4910;Porto;EXPENSE;;EXPENSE\n"
    "Zeitschriften, Bücher;4940;Zeitschriften, Bücher;EXPENSE;;EXPENSE\n"
    "Betriebsausstattung (Anlage);400;Betriebsausstattung;NEUTRAL;AFA;ASSET\n"
    "Sonstige betriebliche Aufwendungen;4900;Sonstige;EXPENSE;;EXPENSE\n"
)


def contact_row(name, kind, number, country=""):
    """Line of ``Kontakte.csv``; ``country`` is the postal "Land (Post)"."""
    cells = [name, kind, number, "", "", "", country] + [""] * 27
    return ";".join(cells)


def make_export(
    directory,
    year,
    rechnungen=(),
    belege=(),
    zahlungen=(),
    pdfs=(),
    customer_country="",
):
    path = os.path.join(
        directory, f"export_firma_papierkram_2026-01-15_120000_csv_and_pdf_{year}.zip"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("CSV/Rechnungen.csv", "\n".join((H_RECHNUNGEN, *rechnungen)) + "\n")
        zf.writestr("CSV/Belege.csv", "\n".join((H_BELEGE, *belege)) + "\n")
        zf.writestr(
            "CSV/Zahlungseingänge.csv", "\n".join((H_ZAHLUNGEN, *zahlungen)) + "\n"
        )
        zf.writestr(
            "CSV/Kontakte.csv",
            "\n".join(
                (
                    H_KONTAKTE,
                    contact_row("Muster GmbH", "Kunde", "K-00001", customer_country),
                    contact_row("Porto AG", "Lieferant", "L-00001"),
                )
            )
            + "\n",
        )
        zf.writestr(
            "CSV/Kontoumsätze.csv",
            H_KONTOUMSAETZE
            + '\ncleared;-5,00;EUR;nein;;;;;"Porto\nzweite Zeile";;nein;;;;;;'
            "Porto AG;;;;10;;03.02.2025;\n",
        )
        for name, content in pdfs:
            zf.writestr(name, content)
    return path
