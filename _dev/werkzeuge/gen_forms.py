#!/usr/bin/env python3
# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Generate the form data of l10n_de_euer (Anlage EÜR, AVEÜR, SZ) per year.

Inputs (research folder, not part of the public repository):

* ``formular_tabellen.csv``: all form lines 2024-2026 with columns
  ``jahr;formular;zeile;kennzahl;bezeichnung;abschnitt;typ;vorzeichen;berechnung``
* ``formular.md``: section 1 (BMF letters, form ids, instructions version,
  SHA-256 of the evaluated PDF) and section 10 (line matrix, cross-check)

Outputs (deterministic, the same input always gives the same files):

* ``l10n_de_euer/data/forms/euer_<year>.xml`` for 2024, 2025 and 2026
* ``l10n_de_euer/tests/data/form_tables.csv``: snapshot of the official
  columns for test FT-01

Codes follow spec 3.1: ``<FORM>.<BOX>.<KZ>``; the profit determination
("Ermittlung des Gewinns") has no printed box and gets ``G``; fields without
a Kennzahl get a speaking code. Formulas (spec 3.3) are derived from the
``berechnung`` column and checked against the "→ target" notes of the
source lines, the formula engine of the module (syntax, unknown codes,
cycles) and the line matrix of formular.md section 10.

Usage::

    python3 -I _dev/werkzeuge/gen_forms.py --spec DIR             # write the files
    python3 -I _dev/werkzeuge/gen_forms.py --spec DIR --check     # compare only
    python3 -I _dev/werkzeuge/gen_forms.py --spec DIR --pdf-dir PDF_DIR

``--pdf-dir`` additionally verifies the SHA-256 of ``bmf<year>.pdf``, the
form ids, every (line, Kennzahl) pair and every official label (the wording
of the line, ``OFFICIAL_LABELS`` where the table paraphrases it) against
``c<year>.txt``. Run it before every release of form data (spec 3.4).

Anlage SZ is filled in only if the other interest on debt exceeds the
pre-printed base amount (``FILL_CONDITIONS``, spec F-14); the line for
partnerships (``EUER.G.PERSGES``) has no formula (outside the program scope).
"""

# pylint: disable=print-used

import argparse
import csv
import hashlib
import importlib.util
import io
import re
import sys
from decimal import Decimal
from heapq import heappop, heappush
from pathlib import Path
from xml.sax.saxutils import escape

REPO = Path(__file__).resolve().parents[2]
MODULE = REPO / "l10n_de_euer"
YEARS = (2024, 2025, 2026)
VERSION = 1
PRINT_WIDTH = 88

FORMS = {
    # csv name: (form_type, code prefix, form id key in formular.md)
    "Anlage EÜR": ("euer", "EUER", "EÜR"),
    "Anlage AVEÜR": ("aveuer", "AVEUER", "AVEÜR"),
    "Anlage SZ": ("sz", "SZ", "SZ"),
}
FORM_ORDER = ("euer", "aveuer", "sz")

FIELD_TYPES = {
    "Text": "text",
    "Schlüssel": "key",
    "Datum": "date",
    "Betrag": "amount",
    "Betrag-davon": "amount_davon",
    "Übernahme": "transfer",
    "Zwischensumme": "subtotal",
    "Summe": "total",
    "Konstante": "constant",
}
SIGNS = {"+": "+", "−": "-", "±": "±", "=": "=", "0": "0", "−/+": "±", "": ""}

# ---------------------------------------------------------------------------
# Speaking codes for lines without Kennzahl (spec 3.1)
# ---------------------------------------------------------------------------
NO_KZ_CODES = {
    "euer": [
        (r"^Name des Steuerpflichtigen", "00", "NAME"),
        (r"^Vorname$", "00", "VORNAME"),
        (r"^\(Betriebs-\)Steuernummer$", "00", "STNR"),
        (r"^Rechtsform des Betriebs$", "15", "RECHTSFORM"),
        (r"^Auflösung von Rücklagen und/oder Ausgleichsposten", "20", "Z22_RUECKL"),
        (r"^Sachlicher Bebauungskostenrichtbetrag", "25", "LUF_WEINBAU"),
        (r"^Betriebsausgabenpauschale für Forstwirte", "25", "LUF_FORST"),
        (r"^Bildung Rücklage Tierbewertung", "25", "Z27_TIERBEW"),
        (r"^Übertrag \(Summe Zeilen 24 bis 27\)$", "25", "UEB1"),
        (r"^Übertrag \(Summe Zeilen 24 bis 27 und Zeilen 29 bis", "25", "UEB2"),
        (r"^Rücklagen, stille Reserven und/oder Ausgleichsposten", "25", "RUECKL"),
        (r"^Summe der Betriebseinnahmen \(Übertrag", "G", "BE"),
        (r"^abzüglich Summe der Betriebsausgaben \(Übertrag", "G", "BA"),
        (r"^Übertrag \(Summe/Differenz Zeilen", "G", "UEB"),
        (r"^Nur PersGes/gesonderte Feststellung", "G", "PERSGES"),
    ],
    "aveuer": [
        (r"^Name des Steuerpflichtigen", "00", "NAME"),
        (r"^\(Betriebs-\)Steuernummer$", "00", "STNR"),
        (r"^Wirtschafts-Identifikationsnummer$", "00", "WIDNR"),
    ],
    "sz": [
        (r"^Name des Steuerpflichtigen$", "00", "NAME"),
        (r"^Vorname$", "00", "VORNAME"),
        (r"^\(Betriebs-\)Steuernummer$", "00", "STNR"),
        (r"^Wirtschafts-Identifikationsnummer$", "00", "WIDNR"),
        (r"^Betriebseinnahmen \(Übertrag", "43", "BE"),
        (r"^abzüglich Betriebsausgaben \(Übertrag", "43", "BA"),
        (r"^Maßgeblicher Gewinn/Verlust", "43", "GEWINN"),
        (r"^abzüglich Gewinn/zuzüglich Verlust", "43", "GEWINN_ABZ"),
        (r"^Über-/Unterentnahme des laufenden", "43", "UEBERENTN"),
        (r"^Kumulierte Über-/Unterentnahmen$", "43", "UEBERENTN_KUM"),
        (r"^Entnahmen des laufenden Wj\.", "43", "ENTN_LFD"),
        (r"^Kumulierte Entnahmen$", "43", "ENTN_KUM"),
        (r"^Einlagen des laufenden Wj\.", "43", "EINL_LFD"),
        (r"^Kumulierte Einlagen$", "43", "EINL_KUM"),
        (r"^Kumulierter Entnahmenüberschuss$", "43", "ENTN_UEBERSCHUSS"),
        (r"^6 % des niedrigeren Betrags", "43", "ZINS_6PROZ"),
        (r"^Übrige Schuldzinsen \(Übertrag", "43", "ZINS_UEBRIG"),
        (r"^Kürzungsbetrag gem\.", "43", "SOCKEL"),
        (r"^Höchstbetrag der nicht abziehbaren", "43", "HOECHSTBETRAG"),
        (r"^Niedrigerer Betrag aus Zeile 25 oder 29", "43", "HINZURECHNUNG"),
    ],
}

# ---------------------------------------------------------------------------
# Short names (English source text, German translation), spec 3.2 ``name``
# ---------------------------------------------------------------------------
EUER_NAMES = {
    "00.NAME": ("Name", "Name"),
    "00.VORNAME": ("First name", "Vorname"),
    "00.STNR": ("Tax number", "Steuernummer"),
    "15.131": ("Start of deviating fiscal year", "Beginn abweichendes Wj."),
    "15.132": ("End of deviating fiscal year", "Ende abweichendes Wj."),
    "15.101": ("Business identification number", "Wirtschafts-IdNr."),
    "15.100": ("Type of business", "Art des Betriebs"),
    "15.RECHTSFORM": ("Legal form", "Rechtsform"),
    "15.103": ("Type of income", "Einkunftsart"),
    "15.104": ("Business owner", "Betriebsinhaber"),
    "15.111": ("Business closed", "Betrieb beendet"),
    "15.120": ("Real estate withdrawn or sold", "Grundstücke entnommen/veräußert"),
    "20.111": ("Revenue as small business", "BE Kleinunternehmer"),
    "20.119": (
        "Of which non-taxable or exempt small business turnover",
        "davon nicht steuerbar oder § 19 Abs. 2 UStG",
    ),
    "20.104": ("Revenue of farmers (§ 24 UStG)", "BE Land- und Forstwirt § 24 UStG"),
    "20.112": ("Taxable revenue, net", "USt-pflichtige BE, netto"),
    "20.103": (
        "Tax-exempt or non-taxable revenue",
        "USt-freie oder nicht steuerbare BE",
    ),
    "20.140": ("VAT received", "Vereinnahmte USt"),
    "20.141": ("VAT refunded by the tax office", "Vom Finanzamt erstattete USt"),
    "20.102": (
        "Sale or withdrawal of fixed assets",
        "Veräußerung oder Entnahme von Anlagevermögen",
    ),
    "20.106": ("Private use of vehicles", "Private Kfz-Nutzung"),
    "20.108": ("Other withdrawals in kind", "Sonstige Sach- und Nutzungsentnahmen"),
    "20.Z22_RUECKL": (
        "Release of reserves (transfer)",
        "Auflösung Rücklagen (Übertrag)",
    ),
    "20.159": ("Total business income", "Summe Betriebseinnahmen"),
    "25.195": ("Flat-rate business expenses", "Betriebsausgabenpauschale"),
    "25.LUF_WEINBAU": (
        "Standard amounts for vineyards (Anlage LuF)",
        "Richtbeträge Weinbau (Anlage LuF)",
    ),
    "25.LUF_FORST": (
        "Flat-rate expenses of foresters (Anlage LuF)",
        "BA-Pauschale Forstwirte (Anlage LuF)",
    ),
    "25.Z27_TIERBEW": (
        "Reserve for livestock valuation (Anlage LuF)",
        "Rücklage Tierbewertung (Anlage LuF)",
    ),
    "25.UEB1": ("Subtotal 1 of business expenses", "Übertrag 1 Betriebsausgaben"),
    "25.100": ("Goods, raw materials and supplies", "Waren, Roh- und Hilfsstoffe"),
    "25.110": ("Third-party services", "Bezogene Fremdleistungen"),
    "25.120": ("Staff costs", "Personalkosten"),
    "25.136": ("Depreciation of real estate", "AfA Grundstücke"),
    "25.131": (
        "Depreciation of intangible assets",
        "AfA immaterielle Wirtschaftsgüter",
    ),
    "25.130": ("Depreciation of movable assets", "AfA bewegliche Wirtschaftsgüter"),
    "25.134": ("Special depreciation (§ 7b, § 7g EStG)", "Sonderabschreibungen"),
    "25.138": ("Reduction amounts (§ 7g (2) EStG)", "Herabsetzungsbeträge § 7g Abs. 2"),
    "25.132": ("Low-value assets", "Geringwertige Wirtschaftsgüter"),
    "25.137": ("Release of asset pools", "Auflösung Sammelposten"),
    "25.135": ("Residual book values of disposals", "Restbuchwerte Abgänge"),
    "25.150": ("Rent for business premises", "Miete/Pacht Geschäftsräume"),
    "25.152": ("Double housekeeping", "Doppelte Haushaltsführung"),
    "25.151": ("Other real estate expenses", "Sonstige Grundstücksaufwendungen"),
    "25.153": (
        "Of which maintenance of real estate",
        "davon Erhaltungsaufwendungen Grundstücke",
    ),
    "25.280": ("Telecommunications", "Telekommunikation"),
    "25.221": (
        "Accommodation and travel expenses",
        "Übernachtungs- und Reisenebenkosten",
    ),
    "25.281": ("Training costs", "Fortbildungskosten"),
    "25.194": (
        "Legal and tax advice, bookkeeping",
        "Rechts- und Steuerberatung, Buchführung",
    ),
    "25.222": ("Rent and leasing of movable assets", "Miete/Leasing bewegliche WG"),
    "25.225": ("Maintenance and repairs", "Erhaltungsaufwendungen"),
    "25.223": (
        "Contributions, fees and insurance",
        "Beiträge, Gebühren und Versicherungen",
    ),
    "25.228": ("Running IT costs", "Laufende EDV-Kosten"),
    "25.229": ("Work equipment", "Arbeitsmittel"),
    "25.226": ("Waste disposal", "Abfallbeseitigung"),
    "25.227": ("Packaging and transport", "Verpackung und Transport"),
    "25.224": ("Advertising", "Werbekosten"),
    "25.232": ("Interest for fixed asset financing", "Schuldzinsen Anlagevermögen"),
    "25.234": ("Other interest", "Übrige Schuldzinsen"),
    "25.185": ("Input VAT paid", "Gezahlte Vorsteuer"),
    "25.186": ("VAT paid to the tax office", "An das Finanzamt gezahlte USt"),
    "25.RUECKL": ("Reserves formed (transfer)", "Rücklagen (Übertrag)"),
    "25.183": ("Other business expenses", "Übrige Betriebsausgaben"),
    "25.UEB2": ("Subtotal 2 of business expenses", "Übertrag 2 Betriebsausgaben"),
    "25.164": ("Gifts, non-deductible", "Geschenke, nicht abziehbar"),
    "25.174": ("Gifts, deductible", "Geschenke, abziehbar"),
    "25.165": ("Entertainment, non-deductible", "Bewirtung, nicht abziehbar"),
    "25.175": ("Entertainment, deductible", "Bewirtung, abziehbar"),
    "25.171": ("Additional meal expenses", "Verpflegungsmehraufwendungen"),
    "25.162": ("Home office, non-deductible", "Arbeitszimmer, nicht abziehbar"),
    "25.172": ("Home office, deductible", "Arbeitszimmer, abziehbar"),
    "25.163": ("Daily home office allowance", "Tagespauschale häusliche Wohnung"),
    "25.168": (
        "Other limited expenses, non-deductible",
        "Sonstige beschränkt abziehbare BA, nicht abziehbar",
    ),
    "25.177": (
        "Other limited expenses, deductible",
        "Sonstige beschränkt abziehbare BA, abziehbar",
    ),
    "25.144": ("Vehicle leasing", "Kfz-Leasing"),
    "25.145": (
        "Vehicle taxes, insurance and tolls",
        "Kfz-Steuer, Versicherung und Maut",
    ),
    "25.146": ("Other vehicle costs", "Sonstige Fahrtkosten"),
    "25.147": (
        "Trips with vehicles outside the business",
        "Fahrtkosten betriebsfremde Fahrzeuge",
    ),
    "25.142": ("Commuting costs", "Fahrtkosten Wohnung–Betriebsstätte"),
    "25.176": ("Commuting allowance", "Entfernungspauschale"),
    "25.139": ("Non-deductible amounts", "Nicht abziehbare Beträge"),
    "25.199": ("Total business expenses", "Summe Betriebsausgaben"),
    "G.BE": ("Total business income (transfer)", "Summe Betriebseinnahmen (Übertrag)"),
    "G.BA": ("Total business expenses (transfer)", "Summe Betriebsausgaben (Übertrag)"),
    "G.240": (
        "Tax-free income (§ 3 no. 26, 26a, 26b EStG)",
        "Steuerfreie Einnahmen § 3 Nr. 26, 26a, 26b",
    ),
    "G.241": ("Other tax-free income (§ 3 EStG)", "Übrige steuerfreie Einnahmen § 3"),
    "G.242": ("Tax-free income (§ 3a EStG)", "Steuerfreie Einnahmen § 3a"),
    "G.243": (
        "Non-deductible expenses (§ 3 no. 26, 26a, 26b EStG)",
        "Nicht abziehbare BA § 3 Nr. 26, 26a, 26b",
    ),
    "G.244": (
        "Non-deductible expenses (§ 3c (1) EStG)",
        "Nicht abziehbare BA § 3c Abs. 1",
    ),
    "G.245": (
        "Non-deductible expenses (§ 3c (4) EStG)",
        "Nicht abziehbare BA § 3c Abs. 4",
    ),
    "G.180": (
        "Investment deduction added back (year -3)",
        "Hinzurechnung Investitionsabzugsbetrag (Jahr -3)",
    ),
    "G.181": (
        "Investment deduction added back (year -2)",
        "Hinzurechnung Investitionsabzugsbetrag (Jahr -2)",
    ),
    "G.182": (
        "Investment deduction added back (year -1)",
        "Hinzurechnung Investitionsabzugsbetrag (Jahr -1)",
    ),
    "G.123": ("Profit surcharge (§ 6c EStG)", "Gewinnzuschlag § 6c"),
    "G.187": (
        "Investment deduction (§ 7g (1) EStG)",
        "Investitionsabzugsbetrag § 7g Abs. 1",
    ),
    "G.250": ("Change of profit method", "Wechsel der Gewinnermittlungsart"),
    "G.UEB": ("Subtotal of profit determination", "Übertrag Gewinnermittlung"),
    "G.255": ("Shares in partnerships", "Ergebnisanteile Personengesellschaften"),
    "G.290": ("Adjusted profit or loss", "Korrigierter Gewinn/Verlust"),
    "G.263": ("Investment tax amounts, total", "InvStG-Beträge, Gesamtbetrag"),
    "G.264": ("Investment tax amounts, correction", "InvStG-Beträge, Korrekturbetrag"),
    "G.261": ("Partial income method, total", "Teileinkünfteverfahren, Gesamtbetrag"),
    "G.262": (
        "Partial income method, correction",
        "Teileinkünfteverfahren, Korrekturbetrag",
    ),
    "G.293": ("Taxable profit before § 4 (4a) EStG", "Gewinn vor § 4 Abs. 4a EStG"),
    "G.271": ("Addition (§ 4 (4a) EStG)", "Hinzurechnungsbetrag § 4 Abs. 4a"),
    "G.219": ("Taxable profit or loss", "Steuerpflichtiger Gewinn/Verlust"),
    "G.PERSGES": (
        "Partnerships only: profit (§ 4 (4a) EStG)",
        "Nur PersGes: Gewinn § 4 Abs. 4a",
    ),
    "27.187": ("Reserves formed (§ 6c EStG)", "Bildung Rücklagen § 6c"),
    "27.170": ("Transfer of hidden reserves", "Übertragung stiller Reserven"),
    "27.191": (
        "Compensating item formed (§ 4g EStG)",
        "Bildung Ausgleichsposten § 4g",
    ),
    "27.190": ("Total formed or transferred", "Summe Bildung/Übertragung"),
    "27.120": ("Reserves released (§ 6c EStG)", "Auflösung Rücklagen § 6c"),
    "27.125": (
        "Compensating item released (§ 4g EStG)",
        "Auflösung Ausgleichsposten § 4g",
    ),
    "27.124": ("Total released", "Summe Auflösung"),
    "29.122": ("Withdrawals", "Entnahmen"),
    "29.123": ("Contributions", "Einlagen"),
}

SZ_NAMES = {
    "00.NAME": ("Name", "Name"),
    "00.VORNAME": ("First name", "Vorname"),
    "00.STNR": ("Tax number", "Steuernummer"),
    "00.WIDNR": ("Business identification number", "Wirtschafts-IdNr."),
    "43.BE": ("Business income (transfer)", "Betriebseinnahmen (Übertrag)"),
    "43.BA": ("Business expenses (transfer)", "Betriebsausgaben (Übertrag)"),
    "43.151": (
        "Tax-free profits outside Anlage EÜR",
        "Steuerfreie Gewinne außerhalb EÜR",
    ),
    "43.152": ("Non-deductible business expenses", "Nicht abziehbare Betriebsausgaben"),
    "43.153": (
        "Results of asset-managing partnerships",
        "Ergebnisse vermögensverwaltender PersGes",
    ),
    "43.154": (
        "Gain on sale or closure, change of method",
        "Veräußerungsgewinn, Wechsel Gewinnermittlung",
    ),
    "43.GEWINN": ("Relevant profit or loss", "Maßgeblicher Gewinn/Verlust"),
    "43.100": ("Withdrawals", "Entnahmen"),
    "43.GEWINN_ABZ": ("Less profit, plus loss", "Abzüglich Gewinn, zuzüglich Verlust"),
    "43.210": ("Contributions", "Einlagen"),
    "43.UEBERENTN": (
        "Over- or under-withdrawal of the year",
        "Über-/Unterentnahme des Jahres",
    ),
    "43.315": (
        "Over- or under-withdrawals of previous years",
        "Über-/Unterentnahmen der Vorjahre",
    ),
    "43.UEBERENTN_KUM": (
        "Cumulated over- or under-withdrawals",
        "Kumulierte Über-/Unterentnahmen",
    ),
    "43.ENTN_LFD": ("Withdrawals of the year", "Entnahmen des Jahres"),
    "43.325": ("Withdrawals of previous years", "Entnahmen der Vorjahre"),
    "43.ENTN_KUM": ("Cumulated withdrawals", "Kumulierte Entnahmen"),
    "43.EINL_LFD": ("Contributions of the year", "Einlagen des Jahres"),
    "43.335": ("Contributions of previous years", "Einlagen der Vorjahre"),
    "43.EINL_KUM": ("Cumulated contributions", "Kumulierte Einlagen"),
    "43.ENTN_UEBERSCHUSS": (
        "Cumulated excess of withdrawals",
        "Kumulierter Entnahmenüberschuss",
    ),
    "43.ZINS_6PROZ": ("6 % of the lower amount", "6 % des niedrigeren Betrags"),
    "43.ZINS_UEBRIG": ("Other interest (transfer)", "Übrige Schuldzinsen (Übertrag)"),
    "43.405": ("Correction of other interest", "Korrektur übrige Schuldzinsen"),
    "43.SOCKEL": ("Deductible base amount", "Kürzungsbetrag"),
    "43.HOECHSTBETRAG": (
        "Maximum non-deductible interest",
        "Höchstbetrag nicht abziehbarer Schuldzinsen",
    ),
    "43.HINZURECHNUNG": ("Addition amount", "Hinzurechnungsbetrag"),
}

AVEUER_HEAD_NAMES = {
    "00.NAME": ("Name", "Name"),
    "00.STNR": ("Tax number", "Steuernummer"),
    "00.WIDNR": ("Business identification number", "Wirtschafts-IdNr."),
}

# Asset groups by section text (without the year of an asset pool)
AVEUER_GROUPS = [
    (r"^40 Grundstücke – Grund und Boden$", ("Land", "Grund und Boden")),
    (r"^40 Grundstücke – Gebäude$", ("Buildings", "Gebäude")),
    (r"^40 Grundstücke – Andere", ("Other real estate", "Andere Grundstücke")),
    (r"^40 Grundstücke – Summe$", ("Real estate", "Grundstücke")),
    (
        r"^40 Häusliches Arbeitszimmer – Anteil Grund und Boden$",
        ("Home office, land", "Arbeitszimmer Grund und Boden"),
    ),
    (
        r"^40 Häusliches Arbeitszimmer – Gebäudeteil$",
        ("Home office, building", "Arbeitszimmer Gebäudeteil"),
    ),
    (r"^40 Immaterielle Wirtschaftsgüter$", ("Intangible assets", "Immaterielle WG")),
    (r"^40 Bewegliche WG – Kfz$", ("Vehicles", "Kfz")),
    (r"^40 Bewegliche WG – Büroausstattung$", ("Office equipment", "Büroausstattung")),
    (r"^40 Bewegliche WG – Andere$", ("Other movable assets", "Andere bewegliche WG")),
    (r"^40 Bewegliche WG – Summe$", ("Movable assets", "Bewegliche WG")),
    (r"^40 Sammelposten .*– Summe$", ("Asset pools", "Sammelposten")),
    (r"^40 Finanzanlagen – Anteile", ("Shares", "Anteile")),
    (
        r"^40 Finanzanlagen – Andere$",
        ("Other financial assets", "Andere Finanzanlagen"),
    ),
    (r"^40 Umlaufvermögen", ("Current assets (§ 4 (3) EStG)", "Umlaufvermögen")),
]
POOL_GROUPS = {
    0: ("Asset pool of the year", "Sammelposten lfd. Jahr"),
    -1: ("Asset pool year -1", "Sammelposten Jahr -1"),
    -2: ("Asset pool year -2", "Sammelposten Jahr -2"),
    -3: ("Asset pool year -3", "Sammelposten Jahr -3"),
    -4: ("Asset pool year -4", "Sammelposten Jahr -4"),
}
AVEUER_ROLES = [
    (r"Summe Anschaffungs-/Herstellungskosten/Einlagewerte$", ("cost", "AK/HK")),
    (r"Anschaffungs-/Herstellungskosten/Einlagewert$", ("cost", "AK/HK")),
    (r"Summe Buchwerte zu Beginn", ("opening book value", "Buchwert Beginn")),
    (r"Buchwert zu Beginn", ("opening book value", "Buchwert Beginn")),
    (r"^Summe Zugänge$", ("additions", "Zugänge")),
    (
        r"Summe Sonderabschreibungen nach § 7b",
        ("special depreciation § 7b", "Sonder-AfA § 7b"),
    ),
    (
        r"^Summe Sonderabschreibungen nach § 7g Abs\. 5 und 6 EStG aller",
        ("special depreciation § 7g, total", "Sonder-AfA § 7g gesamt"),
    ),
    (
        r"Summe Sonderabschreibungen nach § 7g",
        ("special depreciation § 7g", "Sonder-AfA § 7g"),
    ),
    (r"^Summe AfA aller", ("depreciation, total", "AfA gesamt")),
    (r"^Summe AfA", ("depreciation", "AfA")),
    (r"^Summe Abgänge", ("disposals", "Abgänge")),
    (r"Summe Buchwerte am Ende", ("closing book value", "Buchwert Ende")),
    (r"Buchwert am Ende", ("closing book value", "Buchwert Ende")),
    (r": Bildung$", ("formation", "Bildung")),
    (r"Auflösungsbetrag", ("release", "Auflösung")),
    (r"^Summe Auflösungsbeträge", ("releases, total", "Auflösungen gesamt")),
    (
        r"^Es ist mindestens ein Elektro",
        ("electric vehicle present", "E-Fahrzeug vorhanden"),
    ),
]

# ---------------------------------------------------------------------------
# Program scope per field (spec 1.2)
# ---------------------------------------------------------------------------
SCOPE_BLOCKED = {
    "EUER.15.131",
    "EUER.15.132",
    "EUER.15.111",
    "EUER.20.104",
    "EUER.20.Z22_RUECKL",
    "EUER.25.LUF_WEINBAU",
    "EUER.25.LUF_FORST",
    "EUER.25.Z27_TIERBEW",
    "EUER.25.138",
    "EUER.25.RUECKL",
    "EUER.G.180",
    "EUER.G.181",
    "EUER.G.182",
    "EUER.G.123",
    "EUER.G.187",
    "EUER.G.250",
    "EUER.G.255",
    "EUER.G.263",
    "EUER.G.264",
    "EUER.G.261",
    "EUER.G.262",
    "EUER.G.271",
    "EUER.G.PERSGES",
}
SCOPE_MANUAL = {
    "EUER.15.101",
    "EUER.15.100",
    "EUER.15.RECHTSFORM",
    "EUER.15.103",
    "EUER.15.104",
    "EUER.15.120",
    "EUER.20.106",
    "EUER.20.108",
    "EUER.25.195",
    "EUER.25.171",
    "EUER.25.162",
    "EUER.25.172",
    "EUER.25.163",
    "EUER.25.147",
    "EUER.25.142",
    "EUER.25.176",
    "EUER.25.139",
    "EUER.G.240",
    "EUER.G.241",
    "EUER.G.242",
    "EUER.G.243",
    "EUER.G.244",
    "EUER.G.245",
    # buildings: depreciation method "manual" (§ 7 (4), § 7b EStG)
    "AVEUER.40.113",
    "AVEUER.40.114",
    "AVEUER.40.124",
}
MANDATORY = {"EUER.15.120"}
# Profit and loss results and cumulated SZ values may be negative (F-06)
NEGATIVE_RESULTS = {
    "EUER.G.UEB",
    "EUER.G.290",
    "EUER.G.293",
    "EUER.G.219",
    "EUER.G.PERSGES",
    "SZ.43.GEWINN",
    "SZ.43.UEBERENTN",
    "SZ.43.UEBERENTN_KUM",
    "SZ.43.ENTN_UEBERSCHUSS",
}
# Lines that refer to a target whose formula legitimately uses another line
TARGET_EXCEPTIONS = {
    # Z13 shows -Z11 for information; Z15 is defined as Kz 100 - Z11 - Kz 210
    ("SZ.43.GEWINN_ABZ", "SZ.43.UEBERENTN"),
}
# Sums kept without formula: partnerships only, outside the program scope
# (spec 1.2); the line must stay empty for a sole proprietorship
TOTALS_WITHOUT_FORMULA = {"EUER.G.PERSGES"}
# Forms that are filled in only under a condition (spec F-14): Anlage SZ only
# if the other interest on debt (Kz 25.234) exceeds the pre-printed base
# amount of line 28. The condition is "code - constant field" > 0.
FILL_CONDITIONS = {"sz": ("EUER.25.234", "SZ.43.SOCKEL")}
# Lines whose sign column describes the effect on another value than the target
SIGN_EXCEPTIONS = {
    # last fifth of the pool: "-" reduces the (not printed) book value
    ("AVEUER.40.474", "AVEUER.40.499"),
}

# ---------------------------------------------------------------------------
# Official wording (spec 3.2, F-09): the line text of the form, hyphenation
# joined, without line number, Kennzahl, sign and footnote marks. Column
# headings (deductible/non-deductible, total/correction) and block headings
# (asset pool of a year, vehicle costs) are not part of the line text; the
# short name and the relative year carry them. Where formular_tabellen.csv
# paraphrases the form, the wording is given here, per year if it differs.
# --pdf-dir checks every label against the extracted text of the form.
# ---------------------------------------------------------------------------
_RESERVES = "Rücklagen nach § 6c i. V. m. § 6b EStG, R 6.6 EStR"
_POOL_LABELS = {
    "432": "Bildung",
    "434": "Auflösungsbetrag",
    "436": "Buchwert am Ende des Gewinnermittlungszeitraums",
    "440": "Anschaffungs-/Herstellungskosten/Einlagewert",
    "441": "Buchwert zu Beginn des Gewinnermittlungszeitraums",
    "444": "Auflösungsbetrag",
    "446": "Buchwert am Ende des Gewinnermittlungszeitraums",
}
OFFICIAL_LABELS = {
    "EUER.15.131": "davon abweichender Beginn",
    "EUER.15.132": "davon abweichendes Ende",
    "EUER.15.101": "Wirtschafts-Identifikationsnummer",
    "EUER.15.111": "Wurde im Kalenderjahr/Wirtschaftsjahr der Betrieb beendet?",
    "EUER.20.111": {
        2024: "Betriebseinnahmen als umsatzsteuerlicher Kleinunternehmer (nach "
        "§ 19 Abs. 1 UStG)",
        2025: "Betriebseinnahmen als umsatzsteuerlicher Kleinunternehmer (nach "
        "§ 19 Abs. 1 UStG) ohne Beträge laut Zeilen 18 bis 22",
    },
    "EUER.20.104": "Betriebseinnahmen als Land- und Forstwirt, soweit die "
    "Durchschnittssatzbesteuerung nach § 24 UStG angewandt wird",
    "EUER.20.112": "Umsatzsteuerpflichtige Betriebseinnahmen (ohne solche zum "
    "ermäßigten Steuersatz von 0 %)",
    "EUER.20.103": {
        2024: "Betriebseinnahmen, die umsatzsteuerfrei oder nicht "
        "umsatzsteuerbar sind oder nach § 12 Abs. 3 UStG dem ermäßigten "
        "Steuersatz von 0 % unterliegen oder für die der Leistungsempfänger die "
        "Umsatzsteuer nach § 13b UStG schuldet",
        2025: "Betriebseinnahmen, die umsatzsteuerfrei (§ 4 UStG) oder nicht "
        "umsatzsteuerbar sind oder nach § 12 Abs. 3 UStG dem ermäßigten "
        "Steuersatz von 0 % unterliegen oder für die der Leistungsempfänger die "
        "Umsatzsteuer nach § 13b UStG schuldet",
    },
    "EUER.20.140": "Vereinnahmte Umsatzsteuer sowie Umsatzsteuer auf "
    "unentgeltliche Wertabgaben",
    "EUER.20.141": "Vom Finanzamt erstattete und ggf. verrechnete Umsatzsteuer "
    "(Die Regelung zum 10-Tageszeitraum - § 11 Abs. 1 Satz 2 EStG - ist zu "
    "beachten.)",
    "EUER.25.120": "Ausgaben für eigenes Personal (z. B. Gehälter, Löhne und "
    "Versicherungsbeiträge)",
    "EUER.25.135": {
        2024: "Restbuchwerte der ausgeschiedenen Anlagegüter (Übertrag der Summe "
        "der Beträge aus Zeilen 7, 14, 20, 26, 32, 38, 45, 52, 59, 85, 90 der "
        "Anlage AVEÜR)",
        2025: "Restbuchwerte der ausgeschiedenen Anlagegüter (Übertrag der Summe "
        "der Beträge aus Zeilen 7, 14, 20, 26, 32, 38, 45, 53, 60, 86, 91 der "
        "Anlage AVEÜR)",
    },
    "EUER.25.225": "Erhaltungsaufwendungen (z. B. Instandhaltung, Wartung, "
    "Reparatur; ohne solche für Gebäude und Kfz)",
    "EUER.25.223": "Beiträge, Gebühren, Abgaben und Versicherungen (ohne solche "
    "für Gebäude und Kfz)",
    "EUER.25.232": "Schuldzinsen zur Finanzierung von Anschaffungs- und "
    "Herstellungskosten von Wirtschaftsgütern des Anlagevermögens (ohne "
    "häusliches Arbeitszimmer)",
    "EUER.25.186": "An das Finanzamt gezahlte und ggf. verrechnete Umsatzsteuer "
    "(Die Regelung zum 10-Tageszeitraum - § 11 Abs. 2 Satz 2 EStG - ist zu "
    "beachten.)",
    "EUER.25.183": "Übrige unbeschränkt abziehbare Betriebsausgaben (auch "
    "zurückgezahlte Hilfen/Zuschüsse aufgrund der Corona-Pandemie)",
    "EUER.25.164": "Geschenke",
    "EUER.25.174": "Geschenke",
    "EUER.25.165": "Bewirtungsaufwendungen",
    "EUER.25.175": "Bewirtungsaufwendungen",
    "EUER.25.171": "Verpflegungsmehraufwendungen",
    "EUER.25.162": "Aufwendungen für ein häusliches Arbeitszimmer (siehe Anleitung)",
    "EUER.25.172": "Aufwendungen für ein häusliches Arbeitszimmer (siehe Anleitung)",
    "EUER.25.163": "Tagespauschale für die Tätigkeit in der häuslichen Wohnung "
    "(siehe Anleitung)",
    "EUER.25.168": "Sonstige beschränkt abziehbare Betriebsausgaben",
    "EUER.25.177": "Sonstige beschränkt abziehbare Betriebsausgaben",
    "EUER.25.144": "Leasingkosten",
    "EUER.25.145": "Steuern, Versicherungen und Maut",
    "EUER.25.146": "Sonstige tatsächliche Fahrtkosten ohne AfA und Zinsen (z. B. "
    "Reparaturen, Wartungen, Treibstoff, Kosten für Flugstrecken, Kosten für "
    "öffentliche Verkehrsmittel)",
    "EUER.25.142": "Fahrtkosten für Wege zwischen Wohnung und erster "
    "Betriebsstätte; Familienheimfahrten (pauschaliert oder tatsächlich)",
    "EUER.25.176": "Mindestens abziehbare Fahrtkosten für Wege zwischen Wohnung "
    "und erster Betriebsstätte (Entfernungspauschale); Familienheimfahrten",
    **{
        f"EUER.G.{kz}": (
            lambda year, back=back: "zuzüglich Hinzurechnung der "
            "Investitionsabzugsbeträge nach § 7g Abs. 2 Satz 1 EStG aus "
            f"{year - back} (Erläuterungen auf gesondertem Blatt)"
        )
        for kz, back in (("180", 3), ("181", 2), ("182", 1))
    },
    "EUER.G.250": "Hinzurechnungen und Abrechnungen bei Wechsel der "
    "Gewinnermittlungsart (Erläuterungen auf gesondertem Blatt)",
    "EUER.G.263": {
        2024: "Bereits berücksichtigte Beträge, für die Steuerbefreiungen nach "
        "InvStG gelten (ohne Beträge laut Zeile 94; Erläuterungen auf "
        "gesondertem Blatt)",
        2025: "Bereits berücksichtigte Beträge, für die Steuerbefreiungen nach "
        "InvStG gelten (ohne Beträge lt. Zeile 94; Erläuterungen auf "
        "gesondertem Blatt)",
        2026: "Bereits berücksichtigte Beträge, für die Steuerbefreiungen nach "
        "InvStG gelten (ohne Beträge lt. Zeile 95; Erläuterungen auf "
        "gesondertem Blatt)",
    },
    "EUER.G.261": "Bereits berücksichtigte Beträge, für die das "
    "Teileinkünfteverfahren bzw. § 8b KStG gilt",
    "EUER.G.PERSGES": {
        2024: "Anzusetzender steuerpflichtiger Gewinn/Verlust nach Anwendung des "
        "§ 4 Abs. 4a EStG ohne Berücksichtigung des InvStG, des "
        "Teileinkünfteverfahrens bzw. § 8b KStG (Betrag laut Zeile 92 zuzüglich "
        "Betrag laut Zeile 96)",
        2025: "Anzusetzender steuerpflichtiger Gewinn/Verlust nach Anwendung des "
        "§ 4 Abs. 4a EStG ohne Berücksichtigung des InvStG, des "
        "Teileinkünfteverfahrens bzw. § 8b KStG (Betrag lt. Zeile 92 zuzüglich "
        "Betrag lt. Zeile 96)",
        2026: "Anzusetzender steuerpflichtiger Gewinn/Verlust nach Anwendung des "
        "§ 4 Abs. 4a EStG ohne Berücksichtigung des InvStG, des "
        "Teileinkünfteverfahrens bzw. § 8b KStG (Betrag lt. Zeile 93 zuzüglich "
        "Betrag lt. Zeile 97)",
    },
    "EUER.27.187": _RESERVES,
    "EUER.27.120": _RESERVES,
    "EUER.27.191": "Ausgleichsposten nach § 4g EStG",
    "EUER.27.125": "Ausgleichsposten nach § 4g EStG",
    "EUER.27.190": {
        2024: "Gesamtsumme (Übertrag in Zeile 59)",
        2026: "Gesamtsumme (Übertrag in Zeile 60)",
    },
    "EUER.27.124": "Gesamtsumme (Übertrag in Zeile 22)",
    "EUER.29.122": "Entnahmen einschließlich Sach-, Leistungs- und Nutzungsentnahmen",
    "EUER.29.123": "Einlagen einschließlich Sach-, Leistungs- und Nutzungseinlagen",
    "AVEUER.00.NAME": "Name des Steuerpflichtigen bzw. der "
    "Gesellschaft/Gemeinschaft/Körperschaft",
    "AVEUER.40.190": {
        2024: "Summe AfA aller Grundstücke/grundstücksgleichen Rechte (Summe der "
        "Zeilen 13 und 19; Übertrag in Zeile 31 der Anlage EÜR)",
        2026: "Summe AfA aller Grundstücke/grundstücksgleichen Rechte (Summe der "
        "Zeilen 13 und 19; Übertrag in Zeile 32 der Anlage EÜR)",
    },
    "AVEUER.40.480": {
        2024: "Summe Sonderabschreibungen nach § 7g Abs. 5 und 6 EStG aller "
        "beweglichen Wirtschaftsgüter (Summe der Zeilen 43, 50 und 57; zu "
        "erfassen in Zeile 34 der Anlage EÜR)",
        2025: "Summe Sonderabschreibungen nach § 7g Abs. 5 und 6 EStG aller "
        "beweglichen Wirtschaftsgüter (Summe der Zeilen 43, 51 und 58; zu "
        "erfassen in Zeile 34 der Anlage EÜR)",
        2026: "Summe Sonderabschreibungen nach § 7g Abs. 5 und 6 EStG aller "
        "beweglichen Wirtschaftsgüter (Summe der Zeilen 43, 51 und 58; zu "
        "erfassen in Zeile 35 der Anlage EÜR)",
    },
    "AVEUER.40.490": {
        2024: "Summe AfA aller beweglichen Wirtschaftsgüter (Summe der Zeilen 44, "
        "51 und 58; Übertrag in Zeile 33 der Anlage EÜR)",
        2025: "Summe AfA aller beweglichen Wirtschaftsgüter (Summe der Zeilen 44, "
        "52 und 59; Übertrag in Zeile 33 der Anlage EÜR)",
        2026: "Summe AfA aller beweglichen Wirtschaftsgüter (Summe der Zeilen 44, "
        "52 und 59; Übertrag in Zeile 34 der Anlage EÜR)",
    },
    **{f"AVEUER.40.{kz}": label for kz, label in _POOL_LABELS.items()},
    **{
        f"AVEUER.40.{group}{suffix}": _POOL_LABELS[f"44{suffix}"]
        for group in (45, 46, 47)
        for suffix in ("0", "1", "4", "6")
        if (group, suffix) != (47, "6")  # the last fifth has no closing value
    },
    "AVEUER.40.499": {
        2024: "Summe Auflösungsbeträge (Summe der Zeilen 64, 68, 72, 76, 80; "
        "Übertrag in Zeile 37 der Anlage EÜR)",
        2025: "Summe Auflösungsbeträge (Summe der Zeilen 65, 69, 73, 77, 81; "
        "Übertrag in Zeile 37 der Anlage EÜR)",
        2026: "Summe Auflösungsbeträge (Summe der Zeilen 65, 69, 73, 77, 81; "
        "Übertrag in Zeile 38 der Anlage EÜR)",
    },
    "SZ.43.152": "abzüglich nicht abziehbare Betriebsausgaben",
    "SZ.43.153": {
        2024: "zuzüglich Gewinne bzw. abzüglich Verluste aus Beteiligungen an "
        "vermögensverwaltenden Personengesellschaften und abzüglich "
        "Ergebnisanteile aus Kostenträgergemeinschaften (in Zeile 91 der Anlage "
        "EÜR enthalten)",
        2026: "zuzüglich Gewinne bzw. abzüglich Verluste aus Beteiligungen an "
        "vermögensverwaltenden Personengesellschaften und abzüglich "
        "Ergebnisanteile aus Kostenträgergemeinschaften (in Zeile 92 der Anlage "
        "EÜR enthalten)",
    },
    "SZ.43.154": "zuzüglich Veräußerungs-/Aufgabegewinn bzw. abzüglich "
    "Veräußerungs-/Aufgabeverlust und zuzüglich Hinzu- bzw. abzüglich "
    "Abrechnungen beim Wechsel der Gewinnermittlungsart",
    "SZ.43.315": {
        2024: "zuzüglich Über-/abzüglich Unterentnahmen der vorangegangenen "
        "Wirtschaftsjahre (Betrag aus Zeile 16 der Anlage SZ des Vorjahres)",
        2025: "zuzüglich Über-/abzüglich Unterentnahmen der vorangegangenen "
        "Wirtschaftsjahre (Betrag aus Zeile 17 der Anlage SZ des Vorjahres)",
    },
    "SZ.43.ENTN_LFD": "Entnahmen des laufenden Wirtschaftsjahres (Betrag aus Zeile 12)",
    "SZ.43.325": {
        2024: "Entnahmen der vorangegangenen Wirtschaftsjahre (Betrag aus Zeile "
        "19 der Anlage SZ des Vorjahres)",
        2025: "Entnahmen der vorangegangenen Wirtschaftsjahre (Betrag aus Zeile "
        "20 der Anlage SZ des Vorjahres)",
    },
    "SZ.43.EINL_LFD": "Einlagen des laufenden Wirtschaftsjahres (Betrag aus Zeile 14)",
    "SZ.43.335": {
        2024: "Einlagen der vorangegangenen Wirtschaftsjahre (Betrag aus Zeile 22 "
        "der Anlage SZ des Vorjahres)",
        2025: "Einlagen der vorangegangenen Wirtschaftsjahre (Betrag aus Zeile 23 "
        "der Anlage SZ des Vorjahres)",
    },
    "SZ.43.ZINS_6PROZ": "6 % des niedrigeren Betrags aus Zeile 17 oder 24 "
    "(Ergibt sich in Zeile 17 oder 24 ein negativer Betrag, ist hier der Wert "
    "„0“ einzutragen.)",
    "SZ.43.405": "Korrekturbetrag zu den übrigen Schuldzinsen (siehe Anleitung "
    "zur Anlage EÜR)",
    "SZ.43.SOCKEL": "Kürzungsbetrag gem. § 4 Abs. 4a Satz 4 EStG",
    "SZ.43.HOECHSTBETRAG": "Höchstbetrag der nicht abziehbaren Schuldzinsen "
    "(Ergibt sich ein negativer Betrag, ist hier der Wert „0“ einzutragen.)",
}
OFFICIAL_LABELS["EUER.G.264"] = OFFICIAL_LABELS["EUER.G.263"]
OFFICIAL_LABELS["EUER.G.262"] = OFFICIAL_LABELS["EUER.G.261"]
# Lines that share the printed line with other fields, answer keys, header
# texts or pre-printed amounts: the label must occur in the text of the line
# (with the lines before it), but need not be all of it.
INLINE_LABELS = {
    "EUER.00.VORNAME",
    "EUER.15.131",
    "EUER.15.132",
    "EUER.15.101",
    "EUER.15.103",
    "EUER.15.104",
    "EUER.15.111",
    "EUER.15.120",
    "AVEUER.00.STNR",
    "AVEUER.00.WIDNR",
    "AVEUER.40.407",
    "SZ.00.VORNAME",
    "SZ.00.WIDNR",
    "SZ.43.SOCKEL",
}


def official_label(code, year, default):
    """Wording of the form for ``code`` in ``year`` (latest earlier variant)."""
    label = OFFICIAL_LABELS.get(code, default)
    if isinstance(label, dict):
        label = label[max(y for y in label if y <= year)]
    if callable(label):
        label = label(year)
    return label


class GenError(Exception):
    pass


def load_engine():
    path = MODULE / "models" / "engine" / "form_calc.py"
    spec = importlib.util.spec_from_file_location("l10n_de_euer_form_calc", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def read_rows(csv_path):
    with open(csv_path, encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))
    expected = [
        "jahr",
        "formular",
        "zeile",
        "kennzahl",
        "bezeichnung",
        "abschnitt",
        "typ",
        "vorzeichen",
        "berechnung",
    ]
    if list(rows[0].keys()) != expected:
        raise GenError(f"Unexpected CSV columns {list(rows[0].keys())}")
    return [row for row in rows if row["formular"] in FORMS]


def read_sources(md_text):
    """Section 1 of formular.md: BMF letters and form ids per year."""
    section = md_text.split("## 1. Quellen", 1)[1].split("\n## 2.", 1)[0]
    sources = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 6 or not re.fullmatch(r"20\d\d", cells[0]):
            continue
        year = int(cells[0])
        if year not in YEARS:
            continue
        day, month, yr = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", cells[1]).groups()
        reference, dok = (part.strip() for part in cells[2].split("/ DOK", 1))
        url = re.search(r"https://[^\s)]+", cells[4]).group(0)
        sha = cells[5]
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise GenError(f"Invalid SHA-256 for {year}: {sha}")
        sources[year] = {
            "bmf_date": f"{yr}-{month}-{day}",
            "bmf_reference": reference,
            "bmf_dok": dok,
            "source_url": url,
            "source_sha256": sha,
            "vordruck": {},
        }
    for line in section.splitlines():
        if not line.startswith("- `20"):
            continue
        ids = re.findall(r"`(\d{4})Anl(EÜR|AVEÜR|SZ|LuF)(\d{3}(?:–\d{3})?)`", line)
        year = int(ids[0][0])
        if year not in sources:
            continue
        for _year, form, pages in ids:
            sources[year]["vordruck"][form] = f"{year}Anl{form}{pages}"
        version = re.search(r"Anleitung Version ([\d.]+)", line)
        sources[year]["instructions_version"] = version.group(1)
    for year in YEARS:
        if year not in sources or "instructions_version" not in sources[year]:
            raise GenError(f"formular.md section 1: no data for {year}")
        # formular.md abbreviates the ids of later years with "…": the page
        # numbers are the same every year (checked with --pdf-dir)
        for form in ("EÜR", "AVEÜR", "SZ"):
            if form not in sources[year]["vordruck"]:
                pages = sources[YEARS[0]]["vordruck"][form].split(f"Anl{form}")[1]
                sources[year]["vordruck"][form] = f"{year}Anl{form}{pages}"
    return sources


def read_line_matrix(md_text):
    """Section 10 of formular.md: Anlage EÜR line per year by box.kz."""
    section = md_text.split("## 10. Kennungs-Stabilitätsmatrix", 1)[1]
    section = section.split("\n## 11.", 1)[0]
    matrix = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 6 and re.fullmatch(r"\d{2}\.\d{3}", cells[0]):
            matrix[cells[0]] = dict(
                zip(YEARS, (int(c) for c in cells[3:6]), strict=True)
            )
    if len(matrix) < 100:
        raise GenError("formular.md section 10: line matrix not found")
    return matrix


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------


def box_of(section):
    if section.startswith("00"):
        return "00"
    if "Ermittlung des Gewinns" in section:
        return "G"
    match = re.match(r"(\d{2}) ", section)
    if not match:
        raise GenError(f"No box in section {section!r}")
    return match.group(1)


def code_of(form_type, prefix, box, kz, label):
    if kz:
        return f"{prefix}.{box}.{kz}"
    for pattern, code_box, key in NO_KZ_CODES[form_type]:
        if re.search(pattern, label):
            if code_box != box and not (code_box == "00" and box == "00"):
                raise GenError(f"Box mismatch for {label!r}: {box} vs {code_box}")
            return f"{prefix}.{code_box}.{key}"
    raise GenError(f"No speaking code for {form_type} line {label!r}")


def relative_year(field):
    match = re.search(r"\baus (20\d\d)\b", field["section"] + " " + field["label"])
    return int(match.group(1)) - field["year"] if match else 0


def aveuer_name(field):
    key = field["code"].split(".", 1)[1]
    if key in AVEUER_HEAD_NAMES:
        return AVEUER_HEAD_NAMES[key]
    section = field["section"]
    if section.startswith("40 Sammelposten") and not section.endswith("Summe"):
        group = POOL_GROUPS[field["relative_year"]]
    else:
        group = next(
            (names for pattern, names in AVEUER_GROUPS if re.search(pattern, section)),
            None,
        )
    role = next(
        (
            names
            for pattern, names in AVEUER_ROLES
            if re.search(pattern, field["label"])
        ),
        None,
    )
    if not group or not role:
        raise GenError(f"No name for {field['code']} ({section} / {field['label']})")
    return (f"{group[0]}: {role[0]}", f"{group[1]}: {role[1]}")


def name_of(field):
    key = field["code"].split(".", 1)[1]
    if field["form"] == "euer":
        names = EUER_NAMES.get(key)
    elif field["form"] == "sz":
        names = SZ_NAMES.get(key)
    else:
        names = aveuer_name(field)
    if not names:
        raise GenError(f"No short name for {field['code']}")
    return names


def scope_of(field):
    code = field["code"]
    if field["box"] == "00":
        return "manual"
    if field["form"] == "sz":
        return "blocked"
    if code in SCOPE_BLOCKED or code.startswith("EUER.27."):
        return "blocked"
    if code in SCOPE_MANUAL:
        return "manual"
    if field["form"] == "aveuer":
        section = field["section"]
        if "Finanzanlagen – Anteile" in section:
            return "blocked"
        if "Häusliches Arbeitszimmer" in section:
            return "manual"
    return "supported"


def allow_negative(field):
    calc = field["calc"]
    return bool(
        field["sign"] == "±"
        or field["code"] in NEGATIVE_RESULTS
        or re.search(r"negativ(er Wert)? zulässig|Minderungen negativ", calc)
        or "beide Vorzeichen" in calc
    )


def build_fields(rows):
    fields = {year: [] for year in YEARS}
    counters = {}
    for row in rows:
        year = int(row["jahr"])
        if year not in fields:
            continue
        form_type, prefix, _key = FORMS[row["formular"]]
        box = box_of(row["abschnitt"])
        kz = row["kennzahl"]
        label = row["bezeichnung"].strip()
        if row["typ"] not in FIELD_TYPES or row["vorzeichen"] not in SIGNS:
            raise GenError(f"Unknown type or sign in {row}")
        index = counters.get((year, form_type), 0) + 1
        counters[(year, form_type)] = index
        field = {
            "year": year,
            "form": form_type,
            "prefix": prefix,
            "csv_form": row["formular"],
            "line": int(row["zeile"]),
            "kz": kz,
            "label": label,
            "section": row["abschnitt"],
            "csv_type": row["typ"],
            "csv_sign": row["vorzeichen"],
            "calc": row["berechnung"],
            "box": box,
            "code": code_of(form_type, prefix, box, kz, label),
            "field_type": FIELD_TYPES[row["typ"]],
            "sign": SIGNS[row["vorzeichen"]],
            "sequence": index * 10,
            "formula": "",
            "order": len(fields[year]),
        }
        fields[year].append(field)
    for year, items in fields.items():
        seen = {}
        for field in items:
            key = (field["form"], field["code"])
            if key in seen:
                raise GenError(f"Duplicate code {field['code']} in {year}")
            seen[key] = field
            field["relative_year"] = relative_year(field)
            field["scope"] = scope_of(field)
            field["allow_negative"] = allow_negative(field)
            field["mandatory"] = field["code"] in MANDATORY
            field["name_en"], field["name_de"] = name_of(field)
            # codes and names follow the research text; the label is the form's
            field["label"] = official_label(field["code"], year, field["label"])
    codes = {f["code"] for items in fields.values() for f in items}
    unknown = sorted(set(OFFICIAL_LABELS) - codes)
    if unknown:
        raise GenError(f"Official labels for unknown codes: {unknown}")
    return fields


# ---------------------------------------------------------------------------
# Formulas
# ---------------------------------------------------------------------------

GERMAN_NUMBER_RE = re.compile(r"(?<![\d.,])(\d\.\d{3}(?:,\d+)?|\d+,\d+)(?![\d.,])")
TOKEN_RE = re.compile(
    r"\s*(?:"
    r"(?P<range>Summe\s+Z(?P<r1>\d+)\s+bis\s+Z(?P<r2>\d+))"
    r"|(?P<szline>Anlage\s+SZ\s+Zeile\s+(?P<szl>\d+))"
    r"|(?P<form>AVEÜR|EÜR)"
    r"|(?P<kzword>Kz)"
    r"|(?P<line>Z(?P<ln>\d+))"
    r"|(?P<lit>LIT(?P<litv>\d+(?:\.\d+)?))"
    r"|(?P<boxkz>(?P<bx>\d{2})\.(?P<bkz>\d{3}))"
    r"|(?P<num>\d+)"
    r"|(?P<func>min|max)"
    r"|(?P<op>[+−±(),×-])"
    r")"
)


class Resolver:
    """Look up fields of one year by line, box/kz or code."""

    def __init__(self, items):
        self.items = items
        self.by_code = {f["code"]: f for f in items}

    def by_line(self, form, line):
        found = [f for f in self.items if f["form"] == form and f["line"] == line]
        if len(found) != 1:
            raise GenError(f"Line {line} of {form} is ambiguous or missing: {found}")
        return found[0]

    def by_box_kz(self, form, box, kz):
        prefix = {"euer": "EUER", "aveuer": "AVEUER", "sz": "SZ"}[form]
        candidates = [f"{prefix}.{box}.{kz}"]
        if form == "euer" and box == "25":
            candidates.append(f"{prefix}.G.{kz}")
        for code in candidates:
            if code in self.by_code:
                return self.by_code[code]
        raise GenError(f"Unknown reference {form} {box}.{kz}")


def german_numbers(text):
    def replace(match):
        number = match.group(1).replace(".", "").replace(",", ".")
        return f"LIT{Decimal(number)}"

    return GERMAN_NUMBER_RE.sub(replace, text)


def strip_parentheses(text):
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"\([^()]*\)", " ", text)
    return text


def prepare_rhs(calc):
    """Return the computable right-hand side of a ``berechnung`` text or ``""``.

    Values of the previous year, a fifth of a pool and transfers from Anlage
    LuF (not generated) cannot be expressed.
    """
    if not calc.startswith("="):
        return ""
    rhs = calc[1:]
    has_function = bool(re.search(r"\b(min|max)\(", rhs))
    rhs = german_numbers(rhs)
    if has_function:
        rhs = rhs.split("→", 1)[0].rstrip()
    else:
        rhs = strip_parentheses(rhs)
        rhs = re.split(r"[,→]", rhs, maxsplit=1)[0].rstrip()
    if any(marker in rhs for marker in ("Vorjahr", "1/5", "LuF")):
        return ""
    return rhs


class FormulaTranslator:
    """Translate one ``berechnung`` text into an engine formula."""

    def __init__(self, field, resolver, constants):
        self.field = field
        self.resolver = resolver
        self.constants = constants
        self.form = field["form"]
        self.box = None
        self.out = []

    def translate(self):
        calc = self.field["calc"].strip()
        if self.field["field_type"] == "constant":
            value = re.match(r"=\s*(\d\.\d{3},\d{2})", calc)
            if not value:
                raise GenError(f"Constant without value: {self.field['code']}")
            return german_numbers(value.group(1))[3:]
        rhs = prepare_rhs(calc)
        pos = 0
        while pos < len(rhs):
            match = TOKEN_RE.match(rhs, pos)
            if not match or match.end() == pos:
                code = self.field["code"]
                raise GenError(f"{code}: cannot read {rhs[pos:]!r} in {calc!r}")
            pos = match.end()
            kind = next(k for k in TOKEN_KINDS if match.group(k))
            getattr(self, f"_token_{kind}")(match)
        formula = " ".join(self.out)
        for pattern, replacement in (
            (r"\( ", "("),
            (r" \)", ")"),
            (r" ,", ","),
            (r"^- ", "-"),
            (r"\b(min|max) \(", r"\1("),
        ):
            formula = re.sub(pattern, replacement, formula)
        return formula

    def _token_range(self, match):
        first, last = int(match.group("r1")), int(match.group("r2"))
        terms = [
            ("-" if item["sign"] == "-" else "+", item["code"])
            for item in self.resolver.items
            if item["form"] == self.form
            and first <= item["line"] <= last
            and item["field_type"] != "amount_davon"
            and item["sign"] in ("+", "-", "±")
        ]
        if not terms:
            raise GenError(f"{self.field['code']}: empty range {first}-{last}")
        if self.out and self.out[-1] == "+":
            self.out[-1] = terms[0][0]
            self.out.append(terms[0][1])
            for sign, code in terms[1:]:
                self.out += [sign, code]
            return
        inner = ("-" if terms[0][0] == "-" else "") + terms[0][1]
        for sign, code in terms[1:]:
            inner += f" {sign} {code}"
        self.out.append(f"({inner})")

    def _token_szline(self, match):
        self.out.append(self.resolver.by_line("sz", int(match.group("szl")))["code"])

    def _token_form(self, match):
        self.form = {"EÜR": "euer", "AVEÜR": "aveuer"}[match.group("form")]

    def _token_kzword(self, match):
        return

    def _token_line(self, match):
        line = int(match.group("ln"))
        self.out.append(self.resolver.by_line(self.field["form"], line)["code"])

    def _token_lit(self, match):
        value = Decimal(match.group("litv"))
        key = (self.field["form"], value)
        self.out.append(self.constants.get(key, f"{value:.2f}"))

    def _token_boxkz(self, match):
        self.box = match.group("bx")
        field = self.resolver.by_box_kz(self.form, self.box, match.group("bkz"))
        self.out.append(field["code"])

    def _token_num(self, match):
        number = match.group("num")
        if len(number) == 3 and self.box:
            number = self.resolver.by_box_kz(self.form, self.box, number)["code"]
        self.out.append(number)

    def _token_func(self, match):
        self.out.append(match.group("func"))

    def _token_op(self, match):
        op = match.group("op")
        self.out.append({"−": "-", "±": "+", "×": "*"}.get(op, op))


TOKEN_KINDS = (
    "range",
    "szline",
    "form",
    "kzword",
    "line",
    "lit",
    "boxkz",
    "num",
    "func",
    "op",
)


def translate_formula(field, resolver, constants):
    return FormulaTranslator(field, resolver, constants).translate()


def build_formulas(fields, engine):
    for year, items in fields.items():
        resolver = Resolver(items)
        constants = {}
        for field in items:
            if field["field_type"] == "constant":
                field["formula"] = translate_formula(field, resolver, {})
                constants[(field["form"], Decimal(field["formula"]))] = field["code"]
        for field in items:
            if field["code"] in TOTALS_WITHOUT_FORMULA:
                field["formula"] = ""
            elif field["field_type"] != "constant":
                field["formula"] = translate_formula(field, resolver, constants)
        formulas = {f["code"]: f["formula"] for f in items if f["formula"]}
        missing = [
            f["code"]
            for f in items
            if f["field_type"] in ("total", "subtotal", "constant")
            and not f["formula"]
            and f["code"] not in TOTALS_WITHOUT_FORMULA
        ]
        if missing:
            raise GenError(f"{year}: sums without formula: {', '.join(missing)}")
        errors = engine.validate(formulas, resolver.by_code)
        if errors:
            raise GenError(f"{year}: " + "; ".join(errors))
        check_targets(year, items, resolver, engine)


TARGET_RE = re.compile(
    r"(?:→|mindert)\s*(?:(?P<form>EÜR|AVEÜR|Anlage SZ)\s+)?"
    r"(?:Kz\s*(?P<boxkz>\d{2}\.\d{3})|Z(?P<line>\d+)|Zeile\s+(?P<zeile>\d+))"
    r"(?P<more>(?:\s+und\s+\d{2}\.\d{3})*)"
)


def check_targets(year, items, resolver, engine):
    """Each "→ target" note must match the formula of the target field."""
    problems = []
    for field in items:
        first = True
        for match in TARGET_RE.finditer(field["calc"]):
            form = {"EÜR": "euer", "AVEÜR": "aveuer", "Anlage SZ": "sz", None: None}[
                match.group("form")
            ] or field["form"]
            targets = []
            if match.group("boxkz"):
                box, kz = match.group("boxkz").split(".")
                targets.append(resolver.by_box_kz(form, box, kz))
            else:
                targets.append(
                    resolver.by_line(
                        form, int(match.group("line") or match.group("zeile"))
                    )
                )
            for extra in re.findall(r"\d{2}\.\d{3}", match.group("more") or ""):
                box, kz = extra.split(".")
                targets.append(resolver.by_box_kz(form, box, kz))
            for target in targets:
                same_form = target["form"] == field["form"]
                check_sign = first and same_form and field["sign"] in ("+", "-", "±")
                first = False
                if not target["formula"]:
                    continue
                if (field["code"], target["code"]) in TARGET_EXCEPTIONS:
                    continue
                refs = engine.references(target["formula"])
                if field["code"] not in refs:
                    problems.append(
                        f"{field['code']} -> {target['code']}: not in formula"
                    )
                    continue
                if (
                    check_sign
                    and (field["code"], target["code"]) not in SIGN_EXCEPTIONS
                ):
                    try:
                        signs = engine.signed_references(target["formula"])
                    except engine.FormulaError:
                        continue
                    expected = -1 if field["sign"] == "-" else 1
                    if signs[field["code"]] != expected:
                        problems.append(f"{field['code']} -> {target['code']}: sign")
    if problems:
        raise GenError(
            f"{year}: target notes do not match formulas: " + "; ".join(problems)
        )


# ---------------------------------------------------------------------------
# Cross-checks
# ---------------------------------------------------------------------------


def check_line_matrix(fields, matrix):
    for box_kz, lines in matrix.items():
        box, kz = box_kz.split(".")
        for year in YEARS:
            resolver = Resolver(fields[year])
            field = resolver.by_box_kz("euer", box, kz)
            if field["line"] != lines[year]:
                code, line = field["code"], field["line"]
                raise GenError(f"{code} {year}: line {line}, matrix {lines[year]}")


def check_pdf(fields, sources, pdf_dir):
    """Optional check against the BMF PDFs and their extracted text."""
    report = []
    for year in YEARS:
        pdf = pdf_dir / f"bmf{year}.pdf"
        digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
        if digest != sources[year]["source_sha256"]:
            raise GenError(f"SHA-256 of {pdf} differs from formular.md")
        letter = (pdf_dir / f"bmf{year}.txt").read_text(encoding="utf-8")
        for form, form_id in sources[year]["vordruck"].items():
            match = re.fullmatch(
                rf"(\d{{4}}Anl{form})(\d{{3}})(?:–(\d{{3}}))?", form_id
            )
            first, last = int(match.group(2)), int(match.group(3) or match.group(2))
            for page in range(first, last + 1):
                if f"{match.group(1)}{page}" not in letter:
                    raise GenError(
                        f"Form id {match.group(1)}{page} not in bmf{year}.txt"
                    )
        for marker in (sources[year]["bmf_reference"], sources[year]["bmf_dok"]):
            if marker.replace(" ", "") not in letter.replace(" ", ""):
                raise GenError(f"{marker!r} not in bmf{year}.txt")
        if f"Version {sources[year]['instructions_version']}" not in letter:
            raise GenError(f"Instructions version not in bmf{year}.txt")
        text = (pdf_dir / f"c{year}.txt").read_text(encoding="utf-8").splitlines()
        checked, missing = 0, []
        for field in fields[year]:
            if not field["kz"]:
                continue
            checked += 1
            if not kz_in_text(text, field):
                missing.append(f"{field['code']} (Z. {field['line']})")
        report.append(
            f"{year}: {checked} pairs (line, Kz) checked, not found: {missing}"
        )
    return report


NOISE_LINE = re.compile(r"^[\d\s,+−=/()-]*$|^(?:\d{4}Anl)")
LINE_NUMBER = re.compile(r"^(?:\d{12} )?\d{1,3}(?: |$)")
BARCODE = re.compile(r"^\d{12} ")
# column headings printed into the lines of a label
COLUMN_HEADINGS = re.compile(r"\s*(?:EUR Ct|Gesamtbetrag Korrekturbetrag)\s*")
TRAILER = re.compile(r"(?:\s+(?:\d{3}|[+−=,]|\+/−|−/\+))+\s*$")
FOOTNOTE = re.compile(r"(?<=[A-Za-zäöüß)])[1-9]\)")


def normalized(text):
    """Text without whitespace, hyphens and soft hyphens (joins hyphenation)."""
    text = text.replace("\u00ad", "").replace("–", "-").replace("−", "-")
    return re.sub(r"[\s-]+", "", text)


def check_labels(fields, pdf_dir):
    """Every official label is the wording of its line in the form (F-09).

    The label must equal the text of its line, joined with up to seven lines
    before it (labels printed over several lines), without line number,
    Kennzahl, sign, footnote mark, barcode and empty columns. Fields of
    ``INLINE_LABELS`` share their line with other fields; their label must
    occur in that text. Comparison ignores whitespace and hyphens.
    """
    problems = []
    for year in YEARS:
        text = (pdf_dir / f"c{year}.txt").read_text(encoding="utf-8").splitlines()
        for field in fields[year]:
            section = form_section(text, field["csv_form"])
            pattern = re.compile(rf"^(?:\d{{12}} )?{field['line']}(?: |$)")
            target = normalized(field["label"])
            found = False
            for index, line in enumerate(section):
                if not pattern.match(line):
                    continue
                block = [
                    BARCODE.sub("", item)
                    for item in section[max(0, index - 7) : index]
                    if not NOISE_LINE.match(item)
                ]
                block.append(TRAILER.sub("", LINE_NUMBER.sub("", line)))
                block = [
                    FOOTNOTE.sub("", COLUMN_HEADINGS.sub(" ", item)) for item in block
                ]
                candidates = [
                    normalized(" ".join(block[start:])) for start in range(len(block))
                ]
                if target in candidates or (
                    field["code"] in INLINE_LABELS
                    and any(target in candidate for candidate in candidates)
                ):
                    found = True
                    break
            if not found:
                problems.append(f"{year} {field['code']} (Z. {field['line']})")
    if problems:
        raise GenError("Labels differ from the form: " + "; ".join(problems))
    return f"{sum(len(items) for items in fields.values())} labels checked"


def form_section(lines, csv_form):
    """Lines of one form in the extracted text (from shortly before its title)."""
    start_marker, end_marker = SECTION_MARKERS[csv_form]
    start = None
    for index, line in enumerate(lines):
        if start is None and line.strip() == start_marker and index > 20:
            start = index
        elif (
            start is not None
            and line.strip().startswith(end_marker)
            and index > start + 5
        ):
            return lines[max(0, start - 3) : index]
    return lines[max(0, (start or 0) - 3) :]


SECTION_MARKERS = {
    "Anlage EÜR": ("Anlage EÜR", "Anlage AVEÜR"),
    "Anlage AVEÜR": ("Anlage AVEÜR", "Anlage SZ"),
    "Anlage SZ": ("Anlage SZ", "Anlage LuF"),
}


def kz_in_text(lines, field):
    start_marker, end_marker = SECTION_MARKERS[field["csv_form"]]
    start = None
    section = lines
    for index, line in enumerate(lines):
        if start is None and line.strip() == start_marker and index > 20:
            start = index
        elif (
            start is not None
            and line.strip().startswith(end_marker)
            and index > start + 5
        ):
            section = lines[start:index]
            break
    pattern = re.compile(rf"^(?:\d{{12}} )?{field['line']}(?: |$)")
    kz = re.compile(rf"(?<![\d,.]){field['kz']}(?![\d,])")
    for index, line in enumerate(section):
        if pattern.match(line):
            rest = (
                pattern.sub("", line, count=1)
                + " "
                + " ".join(section[index + 1 : index + 3])
            )
            if kz.search(rest):
                return True
    # the SZ text puts Kz 405 before its line number (formular.md section 8 no. 7)
    return field["code"] == "SZ.43.405"


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def xml_id(field):
    suffix = field["code"].split(".", 1)[1].replace(".", "_").lower()
    return f"field_{field['form']}_{field['year']}_v{VERSION}_{suffix}"


def form_xml_id(form_type, year):
    return f"form_{form_type}_{year}_v{VERSION}"


def xml_field(name, value, indent=8):
    text = escape(str(value))
    line = " " * indent + f'<field name="{name}">{text}</field>'
    if len(line) <= PRINT_WIDTH:
        return [line]
    return [
        " " * indent + "<field",
        " " * (indent + 4) + f'name="{name}"',
        " " * indent + f">{text}</field>",
    ]


def ordered(items):
    """Fields in form order, each after the fields its formula uses."""
    by_code = {f["code"]: f for f in items}
    rank = {f["code"]: (FORM_ORDER.index(f["form"]), f["order"]) for f in items}
    deps = {}
    users = {code: [] for code in by_code}
    for field in items:
        refs = set()
        if field["formula"]:
            refs = {
                ref for ref in ENGINE.references(field["formula"]) if ref in by_code
            }
        deps[field["code"]] = refs
        for ref in refs:
            users[ref].append(field["code"])
    heap = [(rank[code], code) for code, refs in deps.items() if not refs]
    result = []
    while heap:
        _rank, code = heappop(heap)
        result.append(by_code[code])
        for user in users[code]:
            deps[user].discard(code)
            if not deps[user]:
                heappush(heap, (rank[user], user))
    if len(result) != len(items):
        raise GenError("Cyclic formulas while ordering")
    return result


def render_year(year, items, source):
    out = [
        '<?xml version="1.0" encoding="utf-8" ?>',
        "<!-- Copyright 2026 Baumgärtner Marketing GmbH",
        "     License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl). -->",
        f"<!-- Official forms {year}: Anlage EÜR, AVEÜR and SZ.",
        "     Generated by _dev/werkzeuge/gen_forms.py; do not edit by hand.",
        "     Fields are ordered so that every formula only uses fields defined",
        "     before it. -->",
        '<odoo noupdate="1">',
    ]
    for form_type in FORM_ORDER:
        key = next(k for t, _p, k in FORMS.values() if t == form_type)
        record_id = form_xml_id(form_type, year)
        out.append(f'    <record id="{record_id}" model="l10n_de_euer.form">')
        out += xml_field("year", year)
        out += xml_field("form_type", form_type)
        out += xml_field("version", VERSION)
        out += xml_field("state", "draft")
        out += xml_field("vordruck_ids", source["vordruck"][key])
        out += xml_field("bmf_date", source["bmf_date"])
        out += xml_field("bmf_reference", source["bmf_reference"])
        out += xml_field("bmf_dok", source["bmf_dok"])
        out += xml_field("instructions_version", source["instructions_version"])
        out += xml_field("source_url", source["source_url"])
        out += xml_field("source_sha256", source["source_sha256"])
        out.append("    </record>")
    for field in ordered(items):
        out.append(f'    <record id="{xml_id(field)}" model="l10n_de_euer.field">')
        out.append(
            f'        <field name="form_id" ref="{form_xml_id(field["form"], year)}" />'
        )
        out += xml_field("code", field["code"])
        out += xml_field("box", field["box"])
        if field["kz"]:
            out += xml_field("kz", field["kz"])
        out += xml_field("line", field["line"])
        out += xml_field("sequence", field["sequence"])
        out += xml_field("official_label", field["label"])
        out += xml_field("name", field["name_en"])
        out += xml_field("name@de", field["name_de"])
        out += xml_field("field_type", field["field_type"])
        if field["sign"]:
            out += xml_field("sign", field["sign"])
        if field["formula"]:
            out += xml_field("formula", field["formula"])
        if field["allow_negative"]:
            out.append('        <field name="allow_negative" eval="True" />')
        if field["relative_year"]:
            out += xml_field("relative_year", field["relative_year"])
        if field["mandatory"]:
            out.append('        <field name="mandatory" eval="True" />')
        out += xml_field("scope_status", field["scope"])
        out.append("    </record>")
    # conditions refer to fields of other forms: set after all fields exist
    # (a function call: a second record with the same id is not allowed)
    for form_type in FORM_ORDER:
        if form_type in FILL_CONDITIONS:
            condition = fill_condition(form_type, items)
            out.append('    <function model="l10n_de_euer.form" name="write">')
            out.append(
                f"        <value eval=\"[ref('{form_xml_id(form_type, year)}')]\" />"
            )
            out.append(
                f"        <value eval=\"{{'fill_condition': '{condition}'}}\" />"
            )
            out.append("    </function>")
    out.append("</odoo>")
    return "\n".join(out) + "\n"


def fill_condition(form_type, items):
    """Condition "code - pre-printed amount" of a form filled in conditionally."""
    code, constant = FILL_CONDITIONS[form_type]
    by_code = {field["code"]: field for field in items}
    if code not in by_code or by_code[constant]["field_type"] != "constant":
        raise GenError(f"Fill condition of {form_type}: {code} or {constant} missing")
    return f"{code} - {by_code[constant]['formula']}"


def render_snapshot(fields):
    """Official columns per line; ``bezeichnung`` is the wording of the form."""
    handle = io.StringIO()
    writer = csv.writer(handle, delimiter=";", lineterminator="\n")
    columns = [
        "jahr",
        "formular",
        "zeile",
        "kennzahl",
        "bezeichnung",
        "abschnitt",
        "typ",
        "vorzeichen",
    ]
    writer.writerow(columns)
    for year in YEARS:
        for field in fields[year]:
            writer.writerow(
                [
                    year,
                    field["csv_form"],
                    field["line"],
                    field["kz"],
                    field["label"],
                    field["section"],
                    field["csv_type"],
                    field["csv_sign"],
                ]
            )
    return handle.getvalue()


def check_names(fields):
    """One English name must always have the same German translation."""
    seen = {}
    for items in fields.values():
        for field in items:
            german = seen.setdefault(field["name_en"], field["name_de"])
            if german != field["name_de"]:
                raise GenError(
                    f"{field['name_en']!r}: {german!r} vs {field['name_de']!r}"
                )


ENGINE = None


def main(argv=None):
    global ENGINE
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--spec",
        type=Path,
        required=True,
        help="research folder with formular_tabellen.csv and formular.md",
    )
    parser.add_argument("--out", type=Path, default=MODULE, help="module folder")
    parser.add_argument("--check", action="store_true", help="compare, do not write")
    parser.add_argument(
        "--pdf-dir", type=Path, help="folder with bmf<year>.pdf/.txt, c<year>.txt"
    )
    args = parser.parse_args(argv)

    ENGINE = load_engine()
    rows = read_rows(args.spec / "formular_tabellen.csv")
    md_text = (args.spec / "formular.md").read_text(encoding="utf-8")
    sources = read_sources(md_text)
    fields = build_fields(rows)
    build_formulas(fields, ENGINE)
    check_line_matrix(fields, read_line_matrix(md_text))
    check_names(fields)
    if args.pdf_dir:
        for line in check_pdf(fields, sources, args.pdf_dir):
            print(line)
        print(check_labels(fields, args.pdf_dir))

    outputs = {
        args.out / "data" / "forms" / f"euer_{year}.xml": render_year(
            year, fields[year], sources[year]
        )
        for year in YEARS
    }
    outputs[args.out / "tests" / "data" / "form_tables.csv"] = render_snapshot(fields)
    differences = []
    for path, content in outputs.items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == content:
            continue
        differences.append(path)
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    for year in YEARS:
        counts = {t: sum(1 for f in fields[year] if f["form"] == t) for t in FORM_ORDER}
        formulas = sum(1 for f in fields[year] if f["formula"])
        print(f"{year}: {counts} fields, {formulas} formulas")
    if args.check:
        for path in differences:
            print(f"differs: {path.relative_to(REPO)}")
        return 1 if differences else 0
    for path in differences:
        print(f"written: {path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GenError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
