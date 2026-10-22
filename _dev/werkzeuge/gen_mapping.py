#!/usr/bin/env python3
# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Generate the EÜR categories, mappings and SKR03/SKR04 defaults of l10n_de_euer.

Inputs:

* research folder (not part of the public repository):
  ``kontenzuordnung_skr03.csv`` and ``kontenzuordnung_skr04.csv`` with one row
  per account of the Odoo chart templates (role, Kennzahl, BE type, extra
  Kennzahl, recognition, confidence, ambiguity case)
* form data of the module (``data/forms/euer_<year>.xml``) to resolve the XML
  ids of the form fields per year
* optional ``--templates``: ``odoo/addons/l10n_de/data/template`` to verify the
  template XML ids, codes and German names (recommended; extract it with
  ``docker compose exec -T odoo tar cf - -C /usr/lib/python3/dist-packages/odoo/addons
  l10n_de/data/template | tar xf - -C <dir>``)

Outputs (deterministic, the same input always gives the same files):

* ``data/categories.xml``: categories (spec 4.2)
* ``data/category_mapping_<year>.xml`` for 2024, 2025 and 2026 (spec 4.1)
* ``data/mapping/category_de_skr03.csv`` and ``category_de_skr04.csv``: default
  category per template XML id without company prefix (spec 4.3), plus the
  utility accounts Odoo creates outside the template CSV and, by code only,
  DATEV accounts the templates lack (8192/4184, K-07)
* ``data/mapping/range_rules.csv``: code ranges for proposals on accounts
  created by the user (K-03); each range runs from a template account to the
  next template account with another category

The corrections of spec 4.3 are applied here, not in the research data:
entertainment 100 % to 175/165 with an optional 70/30 split, private accounts
by side (debit 29.122, credit 29.123), § 19a revenue and refunded ancillary
tax payments, 1585 as tax account, 9000-9009/9090 as opening, input VAT only
for standard taxation, write-off flag, balance sheet accounts with income or
expense role recognised at payment. Tags 76/49 and the KU warning for the
standard accounts 3400/8400 (5400/4400) are implemented in the module.

The corrections of the review of 18.10.2026 (``ACCOUNT_CORRECTIONS``, open
items only on receivable/payable accounts, dividends to be confirmed, fixed
range rules for DATEV auxiliary accounts, opening codes 9000-9009, 9060-9089
and 9090) are applied after the classification. With ``--templates`` the
script also checks that accounts with the same name in SKR03 and SKR04 get
the same category (``ALLOWED_PAIR_DIFFERENCES`` lists the justified
exceptions) and that no income or expense account with money flow is neutral
with high or medium confidence.

Corrections after a release do not reach installed databases (the data is
loaded with ``noupdate``): ship them with a migration script (spec 3.4).

Usage::

    python3 -I _dev/werkzeuge/gen_mapping.py --spec DIR \
        --templates <dir>/l10n_de/data/template
    python3 -I _dev/werkzeuge/gen_mapping.py --spec DIR --check
"""

# pylint: disable=print-used

import argparse
import csv
import io
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from xml.sax.saxutils import escape

REPO = Path(__file__).resolve().parents[2]
MODULE = REPO / "l10n_de_euer"
YEARS = (2024, 2025, 2026)
PRINT_WIDTH = 88
CHARTS = {
    # chart: (research file, template file, xml id prefix, expected accounts)
    "de_skr03": ("kontenzuordnung_skr03.csv", "account.account-de_skr03.csv", 1274),
    "de_skr04": ("kontenzuordnung_skr04.csv", "account.account-de_skr04.csv", 1192),
}
INCOME_EXPENSE = ("income", "income_other", "expense", "expense_depreciation")


class GenError(Exception):
    pass


def cat(code, name, name_de, role, mappings=(), **flags):
    """One category of the catalogue."""
    return {
        "code": code,
        "name": name,
        "name_de": name_de,
        "role": role,
        "mappings": list(mappings),
        **flags,
    }


def m(field, condition="always", share=1, option=False, davon=False, extra=False):
    """One mapping line: field code without the EUER prefix."""
    return {
        "field": field,
        "condition": condition,
        "share": share,
        "option": option,
        "davon": davon,
        "extra": extra,
    }


def revenue(regular, ku="20.111", ku_davon=False):
    return [m(regular, "regular"), m(ku, "ku", davon=ku_davon)]


def fixed(field):
    return [m(field)]


# ------------------------------------------------------------------ catalogue
# Order = sequence in the module. Flags not given keep the model default.
CATEGORIES = [
    # money, open items, opening, suspense
    cat("CASH", "Cash and bank", "Kasse und Bank", "geld"),
    cat(
        "SUSPENSE",
        "Bank suspense account",
        "Bank-Zwischenkonto (ungeklärt)",
        "ungeklaert",
        interim=True,
    ),
    cat(
        "OPEN_ITEM",
        "Receivables and payables",
        "Forderungen und Verbindlichkeiten",
        "op",
    ),
    cat("OPENING", "Opening balance", "Saldenvortrag (Eröffnung)", "eroeffnung"),
    # private
    cat(
        "PRIVATE_WITHDRAWAL",
        "Private withdrawals",
        "Privatentnahmen",
        "privat",
        [m("29.122", "debit"), m("29.123", "credit")],
    ),
    cat(
        "PRIVATE_CONTRIBUTION",
        "Private contributions",
        "Privateinlagen",
        "privat",
        [m("29.122", "debit"), m("29.123", "credit")],
    ),
    # taxes
    cat(
        "VAT_OUTPUT",
        "Output VAT",
        "Umsatzsteuer",
        "steuer",
        [m("20.140", "regular"), m("20.111", "ku")],
        vat_kind="output",
    ),
    cat(
        "VAT_INPUT",
        "Deductible input VAT",
        "Abziehbare Vorsteuer",
        "steuer",
        [m("25.185", "regular")],
        vat_kind="input",
    ),
    cat(
        "VAT_INPUT_NON_DEDUCTIBLE",
        "Input VAT to be split or not deductible",
        "Aufzuteilende oder nicht abziehbare Vorsteuer",
        "steuer",
        vat_kind="input_non_deductible",
        confidence="m",
    ),
    cat(
        "VAT_REVERSE_CHARGE",
        "VAT on reverse charge and intra-EU acquisitions",
        "Umsatz- und Vorsteuer aus Reverse Charge und innergemeinschaftlichem Erwerb",
        "steuer",
        vat_kind="reverse_charge",
        confidence="m",
    ),
    cat(
        "VAT_IMPORT",
        "Import VAT",
        "Einfuhrumsatzsteuer",
        "steuer",
        # small business: import VAT paid to customs is not deductible; it is
        # part of the expense (goods: 25.100, fixed assets: acquisition cost
        # in the register). Default 25.183 with warning C-30.
        [m("25.185", "regular"), m("25.183", "ku")],
        vat_kind="import",
        confidence="m",
    ),
    cat(
        "VAT_SETTLEMENT",
        "VAT payments and refunds",
        "Umsatzsteuer-Zahlungen und -Erstattungen",
        "steuer",
        [m("25.186", "debit"), m("20.141", "credit")],
        vat_kind="settlement",
        recurring_candidate=True,
    ),
    # business income
    cat(
        "REV_TAX",
        "Revenue by tax of the line",
        "Erlöse nach Steuer der Buchungszeile",
        "be",
        revenue("20.112"),
        be_art="tax",
    ),
    cat(
        "REV_TAX_NONCASH",
        "Benefits in kind (by tax of the line)",
        "Sachbezüge (nach Steuer der Buchungszeile)",
        "be",
        revenue("20.112"),
        be_art="tax",
        erfassung="B",
        confidence="n",
    ),
    cat(
        "REV_ADVANCE_RECEIVED",
        "Advance payments received",
        "Erhaltene Anzahlungen",
        "be",
        revenue("20.112"),
        be_art="tax",
        on_payment=True,
        confidence="m",
    ),
    cat(
        "REV_TAXABLE",
        "Taxable revenue",
        "Umsatzsteuerpflichtige Erlöse",
        "be",
        revenue("20.112"),
        be_art="stpfl",
    ),
    cat(
        "REV_EXEMPT",
        "Tax-exempt revenue",
        "Steuerfreie Erlöse",
        "be",
        revenue("20.103"),
        be_art="frei",
    ),
    cat(
        "REV_EXEMPT_19_2",
        "Tax-exempt revenue under § 19 (2) UStG",
        "Steuerfreie Umsätze nach § 19 Abs. 2 UStG",
        "be",
        revenue("20.103", ku_davon="20.119"),
        be_art="frei192",
    ),
    cat(
        "REV_NOT_TAXABLE",
        "Non-taxable income",
        "Nicht steuerbare Einnahmen",
        "be",
        revenue("20.103", ku_davon="20.119"),
        be_art="nstb",
    ),
    cat(
        "REV_INTEREST",
        "Interest income (ancillary transaction)",
        "Zinserträge (Hilfsumsatz)",
        "be",
        revenue("20.103", ku_davon="20.119"),
        be_art="hilfs",
    ),
    cat(
        "REV_ANCILLARY_REFUND",
        "Refunded ancillary tax payments",
        "Erstattete steuerliche Nebenleistungen",
        "be",
        revenue("20.103", ku_davon="20.119"),
        be_art="nstb",
    ),
    cat(
        "REV_S19A",
        "Revenue under § 19a UStG (EU small business scheme)",
        "Umsätze nach § 19a UStG (EU-Kleinunternehmerregelung)",
        "be",
        revenue("20.103", ku_davon="20.119"),
        be_art="s19a",
    ),
    cat(
        "REV_TAX_FREE_3",
        "Tax-free income under § 3 EStG",
        "Steuerfreie Einnahmen nach § 3 EStG",
        "be",
        revenue("20.103", ku_davon="20.119"),
        be_art="nstb",
        confidence="n",
        scope_status="manual",
    ),
    cat(
        "REV_PARTIAL_INCOME",
        "Income under the partial income method",
        "Erträge im Teileinkünfteverfahren",
        "be",
        revenue("20.103", ku_davon="20.119"),
        be_art="nstb",
        confidence="n",
        scope_status="blocked",
        scope_reason="partial_income",
    ),
    cat(
        "REV_KU",
        "Revenue as small business (§ 19 UStG)",
        "Erlöse als Kleinunternehmer (§ 19 UStG)",
        "be",
        revenue("20.112"),
        be_art="ku",
    ),
    cat(
        "REV_LUF",
        "Revenue under § 24 UStG (agriculture and forestry)",
        "Umsätze nach § 24 UStG (Land- und Forstwirtschaft)",
        "be",
        revenue("20.104", ku="20.104"),
        be_art="luf24",
        scope_status="blocked",
        scope_reason="luf",
    ),
    cat(
        "REV_ASSET_DISPOSAL",
        "Sale or withdrawal of fixed assets",
        "Veräußerung oder Entnahme von Anlagevermögen",
        "be",
        fixed("20.102"),
        be_art="fixed",
    ),
    cat(
        "REV_ASSET_DISPOSAL_PARTIAL_INCOME",
        "Sale of shares under the partial income method",
        "Veräußerung von Anteilen im Teileinkünfteverfahren",
        "be",
        fixed("20.102"),
        be_art="fixed",
        confidence="n",
        scope_status="blocked",
        scope_reason="partial_income",
    ),
    cat(
        "REV_PRIVATE_CAR",
        "Private use of business vehicles",
        "Private Kfz-Nutzung",
        "be",
        fixed("20.106"),
        be_art="fixed",
        erfassung="B",
    ),
    cat(
        "REV_WITHDRAWAL_OTHER",
        "Other withdrawals in kind, of use and of services",
        "Sonstige Sach-, Nutzungs- und Leistungsentnahmen",
        "be",
        fixed("20.108"),
        be_art="fixed",
        erfassung="B",
    ),
    cat(
        "RESERVE_RELEASE",
        "Release of reserves",
        "Auflösung von Rücklagen",
        "be",
        fixed("27.120"),
        be_art="fixed",
        erfassung="M",
        scope_status="blocked",
        scope_reason="reserves",
    ),
    cat(
        "RESERVE_RELEASE_COMPENSATION",
        "Release of compensating items (§ 4g EStG)",
        "Auflösung von Ausgleichsposten (§ 4g EStG)",
        "be",
        fixed("27.125"),
        be_art="fixed",
        erfassung="M",
        scope_status="blocked",
        scope_reason="reserves",
    ),
    # business expenses
    cat(
        "EXP_GOODS",
        "Goods, raw materials and supplies",
        "Waren, Roh- und Hilfsstoffe",
        "ba",
        fixed("25.100"),
    ),
    cat(
        "EXP_GOODS_ON_SALE",
        "Current assets under § 4 (3) sentence 4 EStG, on sale",
        "Umlaufvermögen nach § 4 Abs. 3 Satz 4 EStG, bei Veräußerung",
        "afa",
        fixed("25.100"),
        erfassung="B",
        comment=(
            "the cost becomes an expense from the asset register at the receipt",
            "of the proceeds (Kz 605 → 100), not from the booking (spec 6.5)",
        ),
    ),
    cat(
        "EXP_GOODS_ON_SALE_PARTIAL_INCOME",
        "Current assets under the partial income method, on sale",
        "Umlaufvermögen im Teileinkünfteverfahren, bei Veräußerung",
        "afa",
        fixed("25.100"),
        erfassung="B",
        confidence="n",
        scope_status="blocked",
        scope_reason="partial_income",
    ),
    cat(
        "EXP_ADVANCE_PAID",
        "Advance payments on inventories",
        "Geleistete Anzahlungen auf Vorräte",
        "ba",
        fixed("25.100"),
        on_payment=True,
        confidence="m",
    ),
    cat(
        "EXP_SERVICES",
        "Services purchased",
        "Bezogene Fremdleistungen",
        "ba",
        fixed("25.110"),
    ),
    cat(
        "EXP_CONSTRUCTION_WITHHOLDING",
        "Construction withholding tax",
        "Bauabzugsteuer",
        "ba",
        fixed("25.110"),
        on_payment=True,
        confidence="n",
    ),
    cat(
        "EXP_PERSONNEL",
        "Personnel expenses",
        "Ausgaben für eigenes Personal",
        "ba",
        fixed("25.120"),
        recurring_candidate=True,
    ),
    cat(
        "EXP_PAYROLL_LIABILITY",
        "Payroll liabilities",
        "Verbindlichkeiten aus Lohn und Gehalt",
        "ba",
        fixed("25.120"),
        on_payment=True,
        recurring_candidate=True,
        confidence="m",
    ),
    cat(
        "EXP_RENT",
        "Rent and lease of business premises",
        "Miete und Pacht für Geschäftsräume",
        "ba",
        fixed("25.150"),
        recurring_candidate=True,
    ),
    cat(
        "EXP_DOUBLE_HOUSEHOLD",
        "Double household (business)",
        "Doppelte Haushaltsführung",
        "ba",
        fixed("25.152"),
    ),
    cat(
        "EXP_PROPERTY",
        "Other property expenses",
        "Sonstige Aufwendungen für betrieblich genutzte Grundstücke",
        "ba",
        fixed("25.151"),
    ),
    cat(
        "EXP_PROPERTY_MAINTENANCE",
        "Maintenance of business property",
        "Erhaltungsaufwendungen für Grundstücke",
        "ba",
        [m("25.151", davon="25.153")],
    ),
    cat(
        "EXP_TELECOM",
        "Telecommunication",
        "Telekommunikation",
        "ba",
        fixed("25.280"),
        recurring_candidate=True,
    ),
    cat(
        "EXP_TRAVEL",
        "Accommodation and incidental travel costs",
        "Übernachtungs- und Reisenebenkosten",
        "ba",
        fixed("25.221"),
    ),
    cat(
        "EXP_TRAVEL_COLLECTIVE",
        "Travel costs on a DATEV collective account",
        "Reisekosten auf DATEV-Sammelkonto",
        "ba",
        fixed("25.221"),
        warning="collective",
        confidence="n",
    ),
    cat(
        "EXP_TRAINING", "Further training", "Fortbildungskosten", "ba", fixed("25.281")
    ),
    cat(
        "EXP_ADVICE",
        "Legal and tax advice, bookkeeping",
        "Rechts- und Steuerberatung, Buchführung",
        "ba",
        fixed("25.194"),
    ),
    cat(
        "EXP_LEASING",
        "Rent and leasing of movable assets",
        "Miete und Leasing beweglicher Wirtschaftsgüter",
        "ba",
        fixed("25.222"),
        recurring_candidate=True,
    ),
    cat(
        "EXP_MAINTENANCE",
        "Repairs and maintenance",
        "Erhaltungsaufwendungen",
        "ba",
        fixed("25.225"),
    ),
    cat(
        "EXP_FEES_INSURANCE",
        "Contributions, fees, levies and insurance",
        "Beiträge, Gebühren, Abgaben und Versicherungen",
        "ba",
        fixed("25.223"),
        recurring_candidate=True,
    ),
    cat(
        "EXP_IT",
        "Current IT costs",
        "Laufende EDV-Kosten",
        "ba",
        fixed("25.228"),
        recurring_candidate=True,
    ),
    cat(
        "EXP_WORK_EQUIPMENT",
        "Work equipment, office supplies and postage",
        "Arbeitsmittel, Büromaterial und Porto",
        "ba",
        fixed("25.229"),
    ),
    cat("EXP_DISPOSAL", "Waste disposal", "Abfallbeseitigung", "ba", fixed("25.226")),
    cat(
        "EXP_PACKAGING",
        "Packaging and transport",
        "Verpackung und Transport",
        "ba",
        fixed("25.227"),
    ),
    cat("EXP_ADVERTISING", "Advertising", "Werbekosten", "ba", fixed("25.224")),
    cat(
        "EXP_INTEREST_FIXED_ASSETS",
        "Interest on loans for fixed assets",
        "Schuldzinsen für Anlagevermögen",
        "ba",
        fixed("25.232"),
        recurring_candidate=True,
    ),
    cat(
        "EXP_INTEREST_OTHER",
        "Other interest on debt",
        "Übrige Schuldzinsen",
        "ba",
        fixed("25.234"),
        recurring_candidate=True,
    ),
    cat(
        "EXP_INTEREST_PARTIAL_INCOME",
        "Interest under the partial income method",
        "Schuldzinsen im Teileinkünfteverfahren",
        "ba",
        fixed("25.234"),
        confidence="n",
        scope_status="blocked",
        scope_reason="partial_income",
    ),
    cat(
        "EXP_OTHER",
        "Other unrestricted business expenses",
        "Übrige unbeschränkt abziehbare Betriebsausgaben",
        "ba",
        fixed("25.183"),
    ),
    cat(
        "EXP_OTHER_PARTIAL_INCOME",
        "Expenses under the partial income method",
        "Aufwendungen im Teileinkünfteverfahren",
        "ba",
        fixed("25.183"),
        confidence="n",
        scope_status="blocked",
        scope_reason="partial_income",
    ),
    cat(
        "EXP_EXCISE_LIABILITY",
        "Excise duty liabilities",
        "Verbindlichkeiten für Verbrauchsteuern",
        "ba",
        # same field as the expense account (SKR03 4350): "4350 an 1747" is
        # ignored, the payment "1747 an Bank" realises the expense (5.5.1 b)
        fixed("25.223"),
        on_payment=True,
        confidence="n",
    ),
    cat(
        "EXP_GIFTS",
        "Gifts (deductible)",
        "Geschenke (abziehbar)",
        "ba",
        fixed("25.174"),
        restricted_column="deductible",
        separate_record="gift",
        gift_bulk_allowed=True,
    ),
    cat(
        "EXP_GIFTS_NON_DEDUCTIBLE",
        "Gifts (not deductible)",
        "Geschenke (nicht abziehbar)",
        "ba",
        fixed("25.164"),
        restricted_column="non_deductible",
        separate_record="gift",
    ),
    cat(
        "EXP_ENTERTAINMENT",
        "Business entertainment (deductible part)",
        "Bewirtungskosten (abziehbarer Teil)",
        "ba",
        [
            m("25.175", option="entertainment_separate"),
            m("25.175", share=0.7, option="entertainment_split"),
            m("25.165", share=0.3, option="entertainment_split"),
        ],
        restricted_column="deductible",
        separate_record="entertainment",
    ),
    cat(
        "EXP_ENTERTAINMENT_NON_DEDUCTIBLE",
        "Business entertainment (not deductible)",
        "Bewirtungskosten (nicht abziehbar)",
        "ba",
        fixed("25.165"),
        restricted_column="non_deductible",
        separate_record="entertainment",
    ),
    cat(
        "EXP_MEALS",
        "Additional meal expenses",
        "Verpflegungsmehraufwendungen",
        "ba",
        fixed("25.171"),
    ),
    cat(
        "EXP_HOME_OFFICE",
        "Home office (deductible)",
        "Häusliches Arbeitszimmer (abziehbar)",
        "ba",
        fixed("25.172"),
        restricted_column="deductible",
    ),
    cat(
        "EXP_HOME_OFFICE_NON_DEDUCTIBLE",
        "Home office (not deductible)",
        "Häusliches Arbeitszimmer (nicht abziehbar)",
        "ba",
        fixed("25.162"),
        restricted_column="non_deductible",
    ),
    cat(
        "EXP_RESTRICTED",
        "Other restricted business expenses (deductible)",
        "Sonstige beschränkt abziehbare Betriebsausgaben (abziehbar)",
        "ba",
        fixed("25.177"),
        restricted_column="deductible",
    ),
    cat(
        "EXP_NON_DEDUCTIBLE",
        "Non-deductible business expenses (trade tax, fines)",
        "Nicht abziehbare Betriebsausgaben (Gewerbesteuer, Geldbußen)",
        "ba",
        fixed("25.168"),
        restricted_column="non_deductible",
    ),
    cat(
        "EXP_CAR_LEASING",
        "Vehicle leasing",
        "Leasingkosten Kfz",
        "ba",
        fixed("25.144"),
        recurring_candidate=True,
    ),
    cat(
        "EXP_CAR_TAX_INSURANCE",
        "Vehicle tax, insurance and tolls",
        "Kfz-Steuer, -Versicherung und Maut",
        "ba",
        fixed("25.145"),
        recurring_candidate=True,
    ),
    cat(
        "EXP_CAR_OTHER",
        "Other vehicle and travel costs",
        "Sonstige Fahrtkosten",
        "ba",
        fixed("25.146"),
    ),
    cat(
        "EXP_CAR_COLLECTIVE",
        "Vehicle costs on a DATEV collective account",
        "Fahrzeugkosten auf DATEV-Sammelkonto",
        "ba",
        fixed("25.146"),
        warning="collective",
        confidence="n",
    ),
    cat(
        "EXP_CAR_PRIVATE_USE",
        "Business use of a private vehicle",
        "Nutzungseinlage Privatfahrzeug",
        "ba",
        [m("25.147", extra="29.123")],
        erfassung="B",
    ),
    cat(
        "EXP_COMMUTE",
        "Trips between home and business (non-deductible part)",
        "Fahrten Wohnung–Betriebsstätte (nicht abziehbarer Teil)",
        "ba",
        fixed("25.142"),
        erfassung="M",
        confidence="n",
    ),
    cat(
        "EXP_COMMUTE_FLAT_RATE",
        "Distance allowance home to business",
        "Entfernungspauschale Wohnung–Betriebsstätte",
        "ba",
        fixed("25.176"),
        erfassung="M",
        confidence="n",
    ),
    cat(
        "RESERVE_FORMATION",
        "Formation of reserves",
        "Bildung von Rücklagen",
        "ba",
        fixed("27.187"),
        erfassung="M",
        scope_status="blocked",
        scope_reason="reserves",
    ),
    cat(
        "RESERVE_FORMATION_COMPENSATION",
        "Formation of compensating items (§ 4g EStG)",
        "Bildung von Ausgleichsposten (§ 4g EStG)",
        "ba",
        fixed("27.191"),
        erfassung="M",
        scope_status="blocked",
        scope_reason="reserves",
    ),
    # fixed assets and depreciation (values from the asset register)
    cat(
        "FIXED_ASSET",
        "Fixed assets (asset register)",
        "Anlagevermögen (Anlagenverzeichnis)",
        "av",
    ),
    cat(
        "CURRENT_ASSET_4_3_4",
        "Current assets under § 4 (3) sentence 4 EStG",
        "Umlaufvermögen nach § 4 Abs. 3 Satz 4 EStG",
        "av",
    ),
    cat(
        "DEPR_BUILDING",
        "Depreciation of buildings",
        "AfA auf Gebäude",
        "afa",
        fixed("25.136"),
        erfassung="B",
    ),
    cat(
        "DEPR_INTANGIBLE",
        "Depreciation of intangible assets",
        "AfA auf immaterielle Wirtschaftsgüter",
        "afa",
        fixed("25.131"),
        erfassung="B",
    ),
    cat(
        "DEPR_MOVABLE",
        "Depreciation of movable assets",
        "AfA auf bewegliche Wirtschaftsgüter",
        "afa",
        fixed("25.130"),
        erfassung="B",
    ),
    cat(
        "DEPR_SPECIAL",
        "Special depreciation",
        "Sonderabschreibungen",
        "afa",
        fixed("25.134"),
        erfassung="B",
    ),
    cat(
        "DEPR_7G_REDUCTION",
        "Reduction of acquisition costs (§ 7g (2) EStG)",
        "Herabsetzungsbeträge nach § 7g Abs. 2 EStG",
        "afa",
        fixed("25.138"),
        erfassung="B",
        scope_status="blocked",
        scope_reason="iab",
    ),
    cat(
        "DEPR_LOW_VALUE",
        "Low-value assets",
        "Geringwertige Wirtschaftsgüter",
        "afa",
        fixed("25.132"),
        erfassung="B",
    ),
    cat(
        "DEPR_POOL", "Asset pool", "Sammelposten", "afa", fixed("25.137"), erfassung="B"
    ),
    cat(
        "DEPR_BOOK_VALUE",
        "Residual book value of disposed assets",
        "Restbuchwert ausgeschiedener Anlagegüter",
        "afa",
        fixed("25.135"),
        erfassung="B",
    ),
    cat(
        "DEPR_BOOK_VALUE_PARTIAL_INCOME",
        "Residual book value under the partial income method",
        "Restbuchwert im Teileinkünfteverfahren",
        "afa",
        fixed("25.135"),
        erfassung="B",
        confidence="n",
        scope_status="blocked",
        scope_reason="partial_income",
    ),
    cat(
        "DEPR_HOME_OFFICE",
        "Depreciation of the home office",
        "AfA häusliches Arbeitszimmer",
        "afa",
        fixed("25.172"),
        erfassung="B",
        restricted_column="deductible",
        confidence="m",
    ),
    # neutral
    cat("NEUTRAL", "No income or expense", "Ohne Gewinnauswirkung", "neutral"),
    cat(
        "TRANSIT",
        "Money in transit and interim accounts",
        "Geldtransit und Interimskonten",
        "neutral",
        interim=True,
    ),
    cat(
        "WRITEOFF",
        "Bad debts and write-off of receivables",
        "Forderungsverluste und Ausbuchungen",
        "neutral",
        writeoff=True,
        confidence="m",
    ),
    cat(
        "PARTNERSHIP",
        "Shares in partnerships",
        "Beteiligungen an Personengesellschaften",
        "neutral",
        confidence="n",
        scope_status="blocked",
        scope_reason="partnership",
    ),
    # not provided for in the EÜR
    cat("WARN_PROVISION", "Provisions", "Rückstellungen", "warn", warning="provision"),
    cat(
        "WARN_ACCRUAL",
        "Accruals and deferrals",
        "Rechnungsabgrenzung",
        "warn",
        warning="accrual",
    ),
    cat(
        "WARN_VALUATION",
        "Valuation allowances and write-downs",
        "Wertberichtigungen und Teilwertabschreibungen",
        "warn",
        warning="valuation",
    ),
    cat(
        "WARN_INVENTORY",
        "Inventories and changes in inventories",
        "Bestände und Bestandsveränderungen",
        "warn",
        warning="inventory",
    ),
    cat(
        "WARN_DATEV",
        "DATEV auxiliary accounts for the EÜR",
        "DATEV-Hilfskonten für die EÜR",
        "warn",
        warning="datev",
        confidence="m",
    ),
    cat(
        "WARN_DISPOSAL_BALANCE",
        "Net gain or loss on disposals",
        "Saldo aus Anlagenabgängen",
        "warn",
        warning="disposal_balance",
        confidence="m",
    ),
    cat(
        "WARN_IAB",
        "Investment deduction (§ 7g EStG)",
        "Investitionsabzugsbetrag (§ 7g EStG)",
        "warn",
        warning="other",
        scope_status="blocked",
        scope_reason="iab",
    ),
    cat(
        "WARN_INTEREST_LIMIT",
        "Addition of interest under § 4 (4a) EStG",
        "Hinzurechnung von Schuldzinsen nach § 4 Abs. 4a EStG",
        "warn",
        warning="other",
        scope_status="blocked",
        scope_reason="interest_limit",
    ),
    cat(
        "WARN_OTHER",
        "Not provided for in the EÜR",
        "In der EÜR nicht vorgesehen",
        "warn",
        warning="other",
        confidence="m",
    ),
]
CATEGORY_BY_CODE = {c["code"]: c for c in CATEGORIES}

# Categories without a template account (assigned by the user or by structure)
MANUAL_ONLY = {"SUSPENSE", "REV_ANCILLARY_REFUND", "REV_S19A"}

# Utility accounts created by Odoo outside the template CSV (chart_template.py,
# _setup_utility_bank_accounts, get_unaffected_earnings_account). Bank and
# cash journal accounts are assigned by the module from the journals.
UTILITY = [
    ("account_journal_suspense_account_id", "SUSPENSE", "h"),
    ("account_journal_payment_debit_account_id", "TRANSIT", "h"),
    ("account_journal_payment_credit_account_id", "TRANSIT", "h"),
    ("transfer_account_id", "TRANSIT", "h"),
    # a cash surplus is usually unrecorded taxable revenue: by tax of the line
    # (standard taxation 112, small business 111 without 119), confirmation
    ("default_cash_difference_income_account_id", "REV_TAX", "n"),
    ("default_cash_difference_expense_account_id", "EXP_OTHER", "m"),
    ("unaffected_earnings_account", "NEUTRAL", "h"),
]

# DATEV accounts missing in the Odoo chart templates that get a default when
# the user creates them (K-07: small business revenue 8192/4184 since 2025/2026)
EXTRA_ACCOUNTS = {
    "de_skr03": [("8192", "REV_KU", "h")],
    "de_skr04": [("4184", "REV_KU", "h")],
}

BA_FIELDS = {
    "25.100": "EXP_GOODS",
    "25.110": "EXP_SERVICES",
    "25.120": "EXP_PERSONNEL",
    "25.142": "EXP_COMMUTE",
    "25.144": "EXP_CAR_LEASING",
    "25.145": "EXP_CAR_TAX_INSURANCE",
    "25.146": "EXP_CAR_OTHER",
    "25.147": "EXP_CAR_PRIVATE_USE",
    "25.150": "EXP_RENT",
    "25.151": "EXP_PROPERTY",
    "25.152": "EXP_DOUBLE_HOUSEHOLD",
    "25.162": "EXP_HOME_OFFICE_NON_DEDUCTIBLE",
    "25.164": "EXP_GIFTS_NON_DEDUCTIBLE",
    "25.165": "EXP_ENTERTAINMENT_NON_DEDUCTIBLE",
    "25.168": "EXP_NON_DEDUCTIBLE",
    "25.171": "EXP_MEALS",
    "25.172": "EXP_HOME_OFFICE",
    "25.174": "EXP_GIFTS",
    "25.175": "EXP_ENTERTAINMENT",
    "25.176": "EXP_COMMUTE_FLAT_RATE",
    "25.177": "EXP_RESTRICTED",
    "25.183": "EXP_OTHER",
    "25.194": "EXP_ADVICE",
    "25.221": "EXP_TRAVEL",
    "25.222": "EXP_LEASING",
    "25.223": "EXP_FEES_INSURANCE",
    "25.224": "EXP_ADVERTISING",
    "25.225": "EXP_MAINTENANCE",
    "25.226": "EXP_DISPOSAL",
    "25.227": "EXP_PACKAGING",
    "25.228": "EXP_IT",
    "25.229": "EXP_WORK_EQUIPMENT",
    "25.232": "EXP_INTEREST_FIXED_ASSETS",
    "25.234": "EXP_INTEREST_OTHER",
    "25.280": "EXP_TELECOM",
    "25.281": "EXP_TRAINING",
    "27.187": "RESERVE_FORMATION",
    "27.191": "RESERVE_FORMATION_COMPENSATION",
}
AFA_FIELDS = {
    "25.130": "DEPR_MOVABLE",
    "25.131": "DEPR_INTANGIBLE",
    "25.132": "DEPR_LOW_VALUE",
    "25.134": "DEPR_SPECIAL",
    "25.135": "DEPR_BOOK_VALUE",
    "25.136": "DEPR_BUILDING",
    "25.137": "DEPR_POOL",
    "25.138": "DEPR_7G_REDUCTION",
    "25.172": "DEPR_HOME_OFFICE",
}
BE_ARTS = {
    "STPFL": "REV_TAXABLE",
    "FREI": "REV_EXEMPT",
    "FREI192": "REV_EXEMPT_19_2",
    "NSTB": "REV_NOT_TAXABLE",
    "HILFS": "REV_INTEREST",
}
WARN_RULES = [
    # (pattern on research hint or German name, category)
    (r"Rückstellung", "WARN_PROVISION"),
    (r"Rechnungsabgrenzung|latente Steuern", "WARN_ACCRUAL"),
    (r"HGB-Saldo", "WARN_DISPOSAL_BALANCE"),
    (r"§ 4 Abs\. 4a", "WARN_INTEREST_LIMIT"),
    (r"IAB|§ 7g", "WARN_IAB"),
    (r"DATEV-Hilfskont|§ ?4 ?Abs\. ?3 EStG|§ ?4/3", "WARN_DATEV"),
    (r"Bestand|aktivierte Eigenleistung", "WARN_INVENTORY"),
    (r"Wertberichtigung|Teilwert|Abschreibung", "WARN_VALUATION"),
]
SIMPLE_ROLES = {
    "GELD": "CASH",
    "OP": "OPEN_ITEM",
    "UV": "CURRENT_ASSET_4_3_4",
    "AV": "FIXED_ASSET",
}
BE_FIXED_FIELDS = {
    "20.106": "REV_PRIVATE_CAR",
    "20.108": "REV_WITHDRAWAL_OTHER",
    "20.111": "REV_KU",
    "20.104": "REV_LUF",
    "27.120": "RESERVE_RELEASE",
    "27.125": "RESERVE_RELEASE_COMPENSATION",
}
#: balance sheet accounts with expense role: expense at payment (correction 10)
BA_BALANCE_SHEET = {
    "25.100": "EXP_ADVANCE_PAID",
    "25.110": "EXP_CONSTRUCTION_WITHHOLDING",
    "25.120": "EXP_PAYROLL_LIABILITY",
    "25.183": "EXP_EXCISE_LIABILITY",
}
BA_PARTIAL_INCOME = {
    "25.100": "EXP_GOODS_ON_SALE_PARTIAL_INCOME",
    "25.234": "EXP_INTEREST_PARTIAL_INCOME",
    "25.183": "EXP_OTHER_PARTIAL_INCOME",
}
#: partial income method recognised by the account name (research "extra"
#: is not always set, e.g. SKR03 2328)
PARTIAL_INCOME_NAME = re.compile(r"§ 3 Nr\. 40")
# corrections of spec 4.3 that are tied to single accounts; opening balances
# K-13: Saldenvorträge 9000-9009, open items of earlier years 9060-9089 and
# the carry-forward account 9090
OPENING_CODES = {*range(9000, 9010), *range(9060, 9090), 9090}
COLLECTIVE = {"de_skr03": {"4500", "4670"}, "de_skr04": {"6500", "6670"}}
ENTERTAINMENT = {"de_skr03": ("4650", "4654"), "de_skr04": ("6640", "6644")}
WRITEOFF_NAME = re.compile(
    r"^Einstellung(en)? in die (Pauschal|Einzel)wertberichtigung auf Forderungen"
)


def _classify_tax(row):
    kz = row["kz_regel"]
    if kz == "20.140":
        return "VAT_OUTPUT"
    if kz == "25.185":
        return "VAT_INPUT_NON_DEDUCTIBLE" if row["mehrdeutig"] == "A13" else "VAT_INPUT"
    if kz == "25.186":
        return "VAT_SETTLEMENT"
    if "Zoll" in kz:
        return "VAT_IMPORT"
    if kz.startswith("neutral"):
        return "VAT_REVERSE_CHARGE"
    return None


def _classify_depreciation(row):
    if row["kz_regel"] == "25.135" and row["extra"].startswith("G.26"):
        return "DEPR_BOOK_VALUE_PARTIAL_INCOME"
    return AFA_FIELDS[row["kz_regel"]]


def _classify_neutral(row):
    if row["extra"] == "G.255":
        return "PARTNERSHIP"
    if row["mehrdeutig"] == "A07" or WRITEOFF_NAME.match(row["name_de"]):
        return "WRITEOFF"  # correction 9 (K-15)
    if re.search(r"Interimskont|Geldtransit", row["name_de"] + " " + row["hinweis"]):
        return "TRANSIT"
    return "NEUTRAL"


def _classify_warning(row):
    text = row["hinweis"] + " " + row["name_de"]
    for pattern, category in WARN_RULES:
        if re.search(pattern, text, re.IGNORECASE):
            return category
    return "WARN_OTHER"


def _classify_income(row):
    kz = row["kz_regel"]
    extra = row["extra"]
    if kz == "20.102":
        if extra.startswith("G.26"):
            return "REV_ASSET_DISPOSAL_PARTIAL_INCOME"
        return "REV_ASSET_DISPOSAL"
    if kz in BE_FIXED_FIELDS:
        return BE_FIXED_FIELDS[kz]
    if row["odoo_account_type"] not in INCOME_EXPENSE:
        return "REV_ADVANCE_RECEIVED"  # correction 10
    if row["be_art"] == "TAX":
        return "REV_TAX_NONCASH" if row["erfassung"] == "B" else "REV_TAX"
    if extra.startswith("G.26"):
        return "REV_PARTIAL_INCOME"
    if extra.startswith("G.241"):
        return "REV_TAX_FREE_3"
    return BE_ARTS[row["be_art"]]


def _classify_expense(chart, row):
    kz = row["kz_regel"]
    extra = row["extra"]
    if row["odoo_account_type"] not in INCOME_EXPENSE:
        return BA_BALANCE_SHEET[kz]  # correction 10
    if extra.startswith("G.2") or PARTIAL_INCOME_NAME.search(row["name_de"]):
        return BA_PARTIAL_INCOME[kz]
    if kz == "25.100" and row["erfassung"] == "B":
        return "EXP_GOODS_ON_SALE"
    if row["code"] in COLLECTIVE[chart]:
        return "EXP_CAR_COLLECTIVE" if kz == "25.146" else "EXP_TRAVEL_COLLECTIVE"
    if kz == "25.151" and extra == "25.153":
        return "EXP_PROPERTY_MAINTENANCE"
    return BA_FIELDS[kz]


def classify(chart, row):
    """Return the category code of one research row (spec 4.2/4.3)."""
    code = row["code"]
    role = row["rolle"]
    if int(code) in OPENING_CODES:
        return "OPENING"  # correction 7 (K-13)
    if code in ENTERTAINMENT[chart]:  # correction 1 (R-42)
        if code == ENTERTAINMENT[chart][0]:
            return "EXP_ENTERTAINMENT"
        return "EXP_ENTERTAINMENT_NON_DEDUCTIBLE"
    if role in SIMPLE_ROLES:
        return SIMPLE_ROLES[role]
    if role == "PRIVAT":
        if row["kz_regel"] == "29.123":
            return "PRIVATE_CONTRIBUTION"
        return "PRIVATE_WITHDRAWAL"
    handlers = {
        "STEUER": _classify_tax,
        "AFA": _classify_depreciation,
        "NEUTRAL": _classify_neutral,
        "WARN": _classify_warning,
        "BE": _classify_income,
        "BA": lambda item: _classify_expense(chart, item),
    }
    category = handlers[role](row) if role in handlers else None
    if not category:
        raise GenError(
            f"{chart} {code}: no category for role {role!r}, Kz {row['kz_regel']!r}"
        )
    return category


def confidence(chart, row):
    """Confidence of the default assignment (K-05)."""
    if row["code"] in ENTERTAINMENT[chart]:
        return "h"  # 100 % convention 4650/4654, spec 4.3 correction 1
    return row["konfidenz"]


# ------------------------------------------------------------------ review
# Corrections of single accounts after the review of 18.10.2026: (chart, code)
# → (category, confidence, ambiguity case). They take precedence over the
# research data; check_corrections() asserts them.
_DATEV = ("WARN_DATEV", "m", "A20")
_DISPOSAL = ("WARN_DISPOSAL_BALANCE", "m", "A06")
_BILLS = ("WARN_OTHER", "m", "")
_SECURITIES = ("CURRENT_ASSET_4_3_4", "h", "")
_PENSION_ASSETS = ("NEUTRAL", "h", "")
_ADVANCE_FIXED = ("NEUTRAL", "m", "A30")
_EXEMPT_19_2 = ("REV_EXEMPT_19_2", "n", "A01")
ACCOUNT_CORRECTIONS = {
    # DATEV SKR03 4980 is "Sonstiger Betriebsbedarf" (= SKR04 6850, English
    # name "Operating supplies"); the German name of the Odoo template is a
    # copy of 4810 ("Mietleasing ..."), hence ambiguous
    ("de_skr03", "4980"): ("EXP_WORK_EQUIPMENT", "n", "A08"),
    # restructuring costs are paid: expense, to be confirmed (SKR03 = SKR04)
    ("de_skr03", "2007"): ("EXP_OTHER", "n", ""),
    ("de_skr04", "7553"): ("EXP_OTHER", "n", ""),
    # net gains or losses of disposals: proceeds 20.102 and book value 25.135
    # are needed separately (A06)
    ("de_skr03", "2008"): _DISPOSAL,
    ("de_skr03", "2506"): _DISPOSAL,
    ("de_skr03", "2507"): _DISPOSAL,
    ("de_skr03", "2508"): _DISPOSAL,
    ("de_skr04", "7452"): _DISPOSAL,
    ("de_skr04", "7453"): _DISPOSAL,
    ("de_skr04", "7454"): _DISPOSAL,
    ("de_skr04", "7554"): _DISPOSAL,
    # DATEV auxiliary accounts of the EÜR (K-08, C-05)
    ("de_skr03", "8589"): _DATEV,
    ("de_skr03", "1449"): _DATEV,
    ("de_skr03", "1609"): _DATEV,
    ("de_skr03", "1371"): _DATEV,
    ("de_skr04", "1219"): _DATEV,
    ("de_skr04", "3309"): _DATEV,
    ("de_skr04", "1486"): _DATEV,
    # VAT owed by the recipient (§ 13b, intra-EU acquisition): neutral (R-22)
    ("de_skr03", "1725"): ("VAT_REVERSE_CHARGE", "m", "A11"),
    ("de_skr04", "3865"): ("VAT_REVERSE_CHARGE", "m", "A11"),
    # § 14c UStG: output VAT (regular 140, small business 111, R-28)
    ("de_skr03", "1783"): ("VAT_OUTPUT", "n", "A11"),
    ("de_skr04", "3851"): ("VAT_OUTPUT", "n", "A11"),
    # bills of exchange: inflow only when the bill is honoured
    **{("de_skr04", code): _BILLS for code in ("1230", "1231", "1232", "1235")},
    **{("de_skr04", code): _BILLS for code in ("1266", "1267", "1268", "1269")},
    **{("de_skr04", code): _BILLS for code in ("1286", "1287", "1288", "1289")},
    # trade payables to affiliated companies: open items as in SKR03 1630/1640
    ("de_skr04", "3420"): ("OPEN_ITEM", "h", ""),
    ("de_skr04", "3470"): ("OPEN_ITEM", "h", ""),
    # securities and shares in current assets (§ 4 (3) sentence 4 EStG)
    ("de_skr03", "1327"): _SECURITIES,
    ("de_skr03", "1329"): _SECURITIES,
    ("de_skr04", "1390"): _SECURITIES,
    ("de_skr04", "1393"): _SECURITIES,
    ("de_skr04", "1395"): _SECURITIES,
    # reinsurance and pension assets: neutral as in SKR04 1378-1383
    **{("de_skr03", str(code)): _PENSION_ASSETS for code in range(1353, 1358)},
    # advance payments received: income at payment as SKR03 1722 (A22)
    ("de_skr04", "1190"): ("REV_ADVANCE_RECEIVED", "m", "A22"),
    # insurance recoveries and compensation: not taxable as SKR03 2742
    ("de_skr04", "4970"): ("REV_NOT_TAXABLE", "m", ""),
    # cash differences: see UTILITY
    ("de_skr04", "9991"): ("REV_TAX", "n", ""),
    # advance payments on intangible assets: as all advance payments (A30)
    ("de_skr03", "0038"): _ADVANCE_FIXED,
    ("de_skr03", "0039"): _ADVANCE_FIXED,
    ("de_skr04", "0170"): _ADVANCE_FIXED,
    ("de_skr04", "0179"): _ADVANCE_FIXED,
    # tax-exempt under § 4 no. 8 ff. UStG: small business 111 and of which 119
    # unless main transaction of § 4 no. 8a-h, 9a, 10 (§ 19 (2) UStG)
    ("de_skr03", "8100"): _EXEMPT_19_2,
    ("de_skr03", "8514"): _EXEMPT_19_2,
    ("de_skr03", "8574"): _EXEMPT_19_2,
    ("de_skr03", "8609"): _EXEMPT_19_2,
    ("de_skr03", "8701"): ("REV_EXEMPT_19_2", "n", "A09"),
    ("de_skr04", "4100"): _EXEMPT_19_2,
    ("de_skr04", "4564"): _EXEMPT_19_2,
    ("de_skr04", "4574"): _EXEMPT_19_2,
    ("de_skr04", "4841"): _EXEMPT_19_2,
    ("de_skr04", "4701"): ("REV_EXEMPT_19_2", "n", "A09"),
    # partial income method recognised by the name (§ 3 Nr. 40 EStG)
    ("de_skr03", "2328"): ("EXP_GOODS_ON_SALE_PARTIAL_INCOME", "n", "A28"),
    # typically silent partner: the profit share paid is an expense
    ("de_skr03", "2493"): ("EXP_OTHER", "n", ""),
    ("de_skr04", "7399"): ("EXP_OTHER", "n", ""),
    # equal names, equal treatment (check_pairs)
    ("de_skr03", "0005"): ("NEUTRAL", "h", ""),  # = SKR04 0090, equity item
    ("de_skr03", "1522"): _SECURITIES,  # Genussrechte = SKR04 1393
    ("de_skr03", "2504"): ("NEUTRAL", "m", ""),  # merger gains = SKR04 7451
    # loss shares of partnerships: as the profit shares 2618/7008 (G.255)
    ("de_skr03", "4872"): ("PARTNERSHIP", "n", ""),
    ("de_skr04", "7208"): ("PARTNERSHIP", "n", ""),
}
#: accounts with the same German or English name in SKR03 and SKR04 whose
#: categories may differ (check_pairs), with the reason
ALLOWED_PAIR_DIFFERENCES = {
    # the account types of the templates differ: only the SKR04 account is a
    # receivable (open item, B-04), the SKR03 account is not reconcilable
    ("1460", "1240"): "account type",
    ("1594", "1260"): "account type",
    ("1597", "1280"): "account type",
    # SKR03 3000 is an expense (purchase), SKR04 1000 the inventory account
    ("3000", "1000"): "account type",
    # BilMoG transition entries in equity: both without form value
    ("0987", "2969"): "no form value",
    ("0988", "2968"): "no form value",
    ("0989", "2959"): "no form value",
    # foreign VAT refund claims: both neutral tax or balance sheet accounts
    ("1542", "1440"): "no form value",
    # only the German name of SKR03 4980 matches 6498 (Odoo translation error)
    ("4980", "6498"): "wrong German name of SKR03 4980",
}
#: income from investments, dividends: partial income method for a sole
#: proprietor (G.261/262, outside the scope) - confirm before finalising (A28)
DIVIDEND_RANGES = {"de_skr03": range(2600, 2650), "de_skr04": range(7000, 7031)}
#: open items only on receivable/payable accounts (B-04); other accounts with
#: open item category in the research are neutral (C-18 covers the money)
OPEN_ITEM_TYPES = ("asset_receivable", "liability_payable")


def correct(chart, row, category, conf, case):
    """Apply the review corrections to one classified account."""
    code = row["code"]
    if (chart, code) in ACCOUNT_CORRECTIONS:
        return ACCOUNT_CORRECTIONS[chart, code]
    if category == "OPEN_ITEM" and row["odoo_account_type"] not in OPEN_ITEM_TYPES:
        return "NEUTRAL", "m", case
    if category == "REV_NOT_TAXABLE" and int(code) in DIVIDEND_RANGES[chart]:
        return category, "n", "A28"
    return category, conf, case


#: fixed range rules before the runs of template accounts (K-03, K-08): DATEV
#: auxiliary accounts of the EÜR the templates lack, by chart
RANGE_EXCEPTIONS = {
    "de_skr03": [
        (1391, 1391, "WARN_DATEV"),
        (1692, 1693, "WARN_DATEV"),
        (8580, 8589, "WARN_DATEV"),
        (9287, 9289, "WARN_DATEV"),
        (9893, 9902, "WARN_DATEV"),
    ],
    "de_skr04": [
        (4580, 4589, "WARN_DATEV"),
        (9287, 9289, "WARN_DATEV"),
        (9893, 9902, "WARN_DATEV"),
    ],
}
#: codes that must be DATEV auxiliary accounts (WARN_DATEV), template account
#: or range proposal (K-08); also asserted by the module tests
DATEV_AUXILIARY = {
    "de_skr03": (
        1370, 1371, 1391, 1449, *range(1580, 1584), 1609, 1692, 1693,
        *range(8580, 8590), 9287, 9289, 9893, 9902,
    ),
    "de_skr04": (
        1219, *range(1480, 1484), 1485, 1486, 3309, *range(4580, 4590),
        9287, 9289, 9893, 9902,
    ),
}  # fmt: skip


# ------------------------------------------------------------------ inputs
def read_research(path):
    with open(path, encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))
    if not rows:
        raise GenError(f"{path}: empty")
    return rows


def template_xml_id(chart, code):
    if chart == "de_skr03":
        return f"account_{code}"
    return f"chart_skr04_{int(code)}"


def read_templates(path, chart):
    """XML id, German and English name per template account (by code)."""
    with open(path / CHARTS[chart][1], encoding="utf-8", newline="") as handle:
        return {
            row["code"]: (row["id"], row.get("name@de", ""), row["name"])
            for row in csv.DictReader(handle)
        }


def read_field_ids(module):
    """XML id per (year, code) of the form fields."""
    result = {}
    pattern = re.compile(
        r'<record id="([^"]+)" model="l10n_de_euer.field">.*?'
        r'<field name="code">([^<]+)</field>',
        re.S,
    )
    for year in YEARS:
        text = (module / "data" / "forms" / f"euer_{year}.xml").read_text("utf-8")
        for xml_id, code in pattern.findall(text):
            result[year, code] = xml_id
        form = re.search(r'<record id="(form_euer_\d+_v\d+)"', text)
        result[year, "FORM"] = form.group(1)
    return result


# ------------------------------------------------------------------ outputs
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


def xml_ref(name, ref, indent=8):
    line = " " * indent + f'<field name="{name}" ref="{ref}" />'
    if len(line) <= PRINT_WIDTH:
        return [line]
    return [
        " " * indent + "<field",
        " " * (indent + 4) + f'name="{name}"',
        " " * (indent + 4) + f'ref="{ref}"',
        " " * indent + "/>",
    ]


def xml_record(record_id, model, indent=4):
    line = " " * indent + f'<record id="{record_id}" model="{model}">'
    if len(line) <= PRINT_WIDTH:
        return [line]
    return [
        " " * indent + "<record",
        " " * (indent + 4) + f'id="{record_id}"',
        " " * (indent + 4) + f'model="{model}"',
        " " * indent + ">",
    ]


HEADER = """<?xml version="1.0" encoding="utf-8" ?>
<!-- Copyright 2026 Baumgärtner Marketing GmbH
     License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl). -->
"""
BOOL_FLAGS = (
    "recurring_candidate",
    "writeoff",
    "gift_bulk_allowed",
    "interim",
    "on_payment",
)
SELECTION_FLAGS = (
    "be_art",
    "restricted_column",
    "separate_record",
    "vat_kind",
    "warning",
    "scope_reason",
)


def category_xml_id(code):
    return f"category_{code.lower()}"


def xml_comment(text_lines):
    """An XML comment of several lines inside a record (empty without text)."""
    if not text_lines:
        return []
    lines = [f"        <!-- {text_lines[0]}"]
    lines += [f"             {line}" for line in text_lines[1:]]
    lines[-1] += " -->"
    return lines


def render_categories():
    lines = [
        HEADER.rstrip("\n"),
        "<!-- EÜR categories (spec 4.2).",
        "     Generated by _dev/werkzeuge/gen_mapping.py; do not edit by hand. -->",
        '<odoo noupdate="1">',
    ]
    for sequence, category in enumerate(CATEGORIES, start=1):
        lines += xml_record(category_xml_id(category["code"]), "l10n_de_euer.category")
        lines += xml_field("code", category["code"])
        lines += xml_field("name", category["name"])
        lines += xml_field("name@de", category["name_de"])
        lines += xml_field("sequence", sequence * 10)
        lines += xml_comment(category.get("comment", ()))
        lines += xml_field("role", category["role"])
        if category.get("erfassung", "Z") != "Z":
            lines += xml_field("erfassung", category["erfassung"])
        if category.get("confidence", "h") != "h":
            lines += xml_field("confidence", category["confidence"])
        for flag in SELECTION_FLAGS:
            if category.get(flag):
                lines += xml_field(flag, category[flag])
        for flag in BOOL_FLAGS:
            if category.get(flag):
                lines += xml_field(flag, "True")
        if category.get("scope_status", "supported") != "supported":
            lines += xml_field("scope_status", category["scope_status"])
        lines.append("    </record>")
    lines.append("</odoo>")
    return "\n".join(lines) + "\n"


def render_mappings(year, field_ids):
    lines = [
        HEADER.rstrip("\n"),
        f"<!-- Mapping of the EÜR categories to the form fields {year} (spec 4.1).",
        "     Generated by _dev/werkzeuge/gen_mapping.py; do not edit by hand. -->",
        '<odoo noupdate="1">',
    ]
    form_ref = field_ids[year, "FORM"]

    def field_ref(code):
        key = (year, f"EUER.{code}")
        if key not in field_ids:
            raise GenError(f"{year}: field EUER.{code} is missing in the form data")
        return field_ids[key]

    for category in CATEGORIES:
        for index, mapping in enumerate(category["mappings"], start=1):
            record_id = f"mapping_{year}_{category['code'].lower()}_{index}"
            lines += xml_record(record_id, "l10n_de_euer.category.mapping")
            lines += xml_ref("category_id", category_xml_id(category["code"]))
            lines += xml_ref("form_id", form_ref)
            lines += xml_ref("field_id", field_ref(mapping["field"]))
            lines += xml_field("sequence", index * 10)
            if mapping["share"] != 1:
                lines += xml_field("share", mapping["share"])
            if mapping["condition"] != "always":
                lines += xml_field("condition", mapping["condition"])
            if mapping["option"]:
                lines += xml_field("option", mapping["option"])
            if mapping["davon"]:
                lines += xml_ref("davon_field_id", field_ref(mapping["davon"]))
            if mapping["extra"]:
                lines += xml_ref("additional_field_id", field_ref(mapping["extra"]))
            lines.append("    </record>")
    lines.append("</odoo>")
    return "\n".join(lines) + "\n"


def render_csv(header, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue()


def range_rules(chart, assignments, templates=()):
    """Code ranges for proposals on accounts created by the user (K-03).

    Runs of equal categories over the template codes; within a run of opening
    balances only the opening codes (K-13) get a proposal, the codes between
    get none (empty category). Fixed exceptions (DATEV auxiliary accounts the
    templates lack, K-08) take precedence over the runs, never over a template
    account. The result covers 0-9999 without gaps.
    """
    items = sorted((int(code), category) for code, category, *_rest in assignments)
    per_code = [""] * 10000
    for index, (code, category) in enumerate(items):
        start = 0 if index == 0 else code
        end = items[index + 1][0] - 1 if index + 1 < len(items) else 9999
        for number in range(start, end + 1):
            per_code[number] = category
    for number, category in enumerate(per_code):
        if category == "OPENING" and number not in OPENING_CODES:
            per_code[number] = ""
    fixed = {int(code) for code in templates}
    for first, last, category in RANGE_EXCEPTIONS[chart]:
        for number in range(first, last + 1):
            if number not in fixed:
                per_code[number] = category
    rules = []
    for number, category in enumerate(per_code):
        if rules and rules[-1][2] == category:
            rules[-1][1] = number
        else:
            rules.append([number, number, category])
    return [
        (chart, f"{start:04d}", f"{end:04d}", category)
        for start, end, category in rules
    ]


# ------------------------------------------------------------------ checks
def check_catalogue():
    codes = Counter(c["code"] for c in CATEGORIES)
    duplicates = [code for code, count in codes.items() if count > 1]
    if duplicates:
        raise GenError(f"duplicate category codes: {duplicates}")
    for category in CATEGORIES:
        role = category["role"]
        if role in ("be", "ba", "steuer", "privat", "afa") and not category["mappings"]:
            if category.get("vat_kind") not in (
                "input_non_deductible",
                "reverse_charge",
            ):
                raise GenError(f"{category['code']}: role {role} needs a mapping")
        if role in ("geld", "op", "av", "neutral", "eroeffnung", "ungeklaert", "warn"):
            if category["mappings"]:
                raise GenError(f"{category['code']}: role {role} has no form value")
        groups = defaultdict(float)
        for mapping in category["mappings"]:
            groups[mapping["condition"], mapping["option"]] += mapping["share"]
        for key, total in groups.items():
            if abs(total - 1) > 1e-9:
                raise GenError(f"{category['code']}: shares {key} sum to {total}")


def check_templates(chart, rows, templates):
    research = {row["code"] for row in rows}
    template = set(templates)
    if research != template:
        raise GenError(
            f"{chart}: research and template differ: "
            f"only research {sorted(research - template)[:10]}, "
            f"only template {sorted(template - research)[:10]}"
        )
    for row in rows:
        xml_id, name_de, _name = templates[row["code"]]
        if xml_id != template_xml_id(chart, row["code"]):
            raise GenError(f"{chart} {row['code']}: unexpected XML id {xml_id}")
        if name_de != row["name_de"]:
            raise GenError(f"{chart} {row['code']}: German name differs")
    if chart == "de_skr03":
        # SKR03 4810 has no German name. 4980 is DATEV "Sonstiger
        # Betriebsbedarf" (English "Operating supplies", = SKR04 6850); its
        # German Odoo name wrongly repeats "Mietleasing ..." of 4810. If Odoo
        # corrects one of them, review ACCOUNT_CORRECTIONS.
        if (
            templates["4810"][1]
            or "Mietleasing" not in templates["4980"][1]
            or templates["4980"][2] != "Operating supplies"
        ):
            raise GenError("de_skr03 4810/4980: names changed, check the mapping")


def check_corrections(assigned):
    """Spec 4.3 corrections must be visible in the result."""
    expect = {
        ("de_skr03", "4650"): "EXP_ENTERTAINMENT",
        ("de_skr03", "4654"): "EXP_ENTERTAINMENT_NON_DEDUCTIBLE",
        ("de_skr04", "6640"): "EXP_ENTERTAINMENT",
        ("de_skr04", "6644"): "EXP_ENTERTAINMENT_NON_DEDUCTIBLE",
        ("de_skr03", "1585"): "VAT_REVERSE_CHARGE",
        ("de_skr03", "1576"): "VAT_INPUT",
        ("de_skr04", "1406"): "VAT_INPUT",
        ("de_skr03", "1800"): "PRIVATE_WITHDRAWAL",
        ("de_skr04", "2100"): "PRIVATE_WITHDRAWAL",
        ("de_skr03", "1890"): "PRIVATE_CONTRIBUTION",
        ("de_skr04", "2180"): "PRIVATE_CONTRIBUTION",
        ("de_skr03", "8195"): "REV_KU",
        ("de_skr04", "4185"): "REV_KU",
        ("de_skr03", "4810"): "EXP_LEASING",
        ("de_skr03", "4980"): "EXP_WORK_EQUIPMENT",
        ("de_skr04", "6850"): "EXP_WORK_EQUIPMENT",
        ("de_skr03", "2328"): "EXP_GOODS_ON_SALE_PARTIAL_INCOME",
        ("de_skr03", "9089"): "OPENING",
        ("de_skr03", "1740"): "EXP_PAYROLL_LIABILITY",
        ("de_skr03", "1755"): "EXP_PAYROLL_LIABILITY",
        ("de_skr04", "3790"): "EXP_PAYROLL_LIABILITY",
        ("de_skr03", "1710"): "REV_ADVANCE_RECEIVED",
        ("de_skr04", "3250"): "REV_ADVANCE_RECEIVED",
        ("de_skr03", "4500"): "EXP_CAR_COLLECTIVE",
        ("de_skr03", "4670"): "EXP_TRAVEL_COLLECTIVE",
        ("de_skr04", "6500"): "EXP_CAR_COLLECTIVE",
        ("de_skr04", "6670"): "EXP_TRAVEL_COLLECTIVE",
        ("de_skr03", "1360"): None,  # created by Odoo, not in the template
    }
    for chart in CHARTS:
        for code in (*(f"{c}" for c in OPENING_CODES),):
            category = assigned[chart].get(code)
            if category not in (None, "OPENING"):
                raise GenError(f"{chart} {code}: opening expected, got {category}")
        writeoff = range(2400, 2451) if chart == "de_skr03" else range(6930, 6939)
        for code, category in assigned[chart].items():
            if int(code) in writeoff and category != "WRITEOFF":
                raise GenError(f"{chart} {code}: write-off expected, got {category}")
    for (chart, code), (category, _conf, _case) in ACCOUNT_CORRECTIONS.items():
        expect[chart, code] = category
    for (chart, code), category in expect.items():
        if assigned[chart].get(code) != category:
            raise GenError(
                f"{chart} {code}: {category} expected, got {assigned[chart].get(code)}"
            )
    for chart, codes in DATEV_AUXILIARY.items():
        for code in codes:
            category = assigned[chart].get(f"{code:04d}")
            if category not in (None, "WARN_DATEV"):
                raise GenError(f"{chart} {code}: DATEV auxiliary account, {category}")


def check_pairs(templates, assigned):
    """Accounts with the same name in SKR03 and SKR04 get the same category.

    Names that occur once per chart, German or English. Differences need a
    reason in ALLOWED_PAIR_DIFFERENCES.
    """
    pairs = set()
    for column in (1, 2):
        names = {}
        for chart in CHARTS:
            index = defaultdict(list)
            for code, values in templates[chart].items():
                if values[column].strip():
                    index[values[column].strip()].append(code)
            names[chart] = index
        for name in set(names["de_skr03"]) & set(names["de_skr04"]):
            codes03, codes04 = names["de_skr03"][name], names["de_skr04"][name]
            if len(codes03) == 1 and len(codes04) == 1:
                pairs.add((codes03[0], codes04[0]))
    problems = [
        f"{code03}/{code04}: {assigned['de_skr03'][code03]} "
        f"vs {assigned['de_skr04'][code04]}"
        for code03, code04 in sorted(pairs)
        if assigned["de_skr03"][code03] != assigned["de_skr04"][code04]
        and (code03, code04) not in ALLOWED_PAIR_DIFFERENCES
    ]
    unused = sorted(set(ALLOWED_PAIR_DIFFERENCES) - pairs)
    if problems or unused:
        raise GenError(
            f"Equal names, different categories: {problems}; unused: {unused}"
        )
    return len(pairs)


def check_rules(rules):
    """DATEV auxiliary accounts the templates lack get a warning (K-08)."""
    for chart, codes in DATEV_AUXILIARY.items():
        chart_rules = [rule for rule in rules if rule[0] == chart]
        for code in codes:
            found = [
                category
                for _chart, first, last, category in chart_rules
                if int(first) <= code <= int(last)
            ]
            if found != ["WARN_DATEV"]:
                raise GenError(f"{chart} {code}: range rule {found}, WARN_DATEV wanted")
        for number in (9010, 9059, 9091, 9500):
            found = [
                category
                for _chart, first, last, category in chart_rules
                if int(first) <= number <= int(last)
            ]
            if found != [""]:
                raise GenError(f"{chart} {number}: no opening proposal wanted, {found}")


#: income and expense accounts that may be neutral with high or medium
#: confidence: no money flow (imputed costs, write-ups, provisions, deferred
#: taxes, contra and statistical accounts) or a case outside the program scope
#: (corporations, partnerships, cooperatives, group accounting)
NEUTRAL_PL_NAMES = re.compile(
    "|".join(
        (
            r"[Kk]alkulatorisch",
            r"Zuschreibung",
            r"[Rr]ückstellung",
            r"Abzinsung",
            r"Übergangs(vorschrift|bestimmung)",
            r"[Rr]ücklage",
            r"latenten? Steuern",
            r"Gegenkonto",
            r"Überleitungskonto",
            r"[Ww]ertberichtigung",
            r"Bewertung",
            r"nicht (zahlungs|finanzierungs)wirksam",
            r"nur Bilanzierer",
            r"Verschmelzung",
            r"Aktivierung unentgeltlich",
            r"Gewinnvortrag|Verlustvortrag",
            r"Verlustübernahme|Gewinngemeinschaft|[Gg]ewinnabführ|Teilgewinnabführ",
            r"[Aa]bgeführte Gewinne|Erhaltene Gewinne",
            r"eigene[rn]? Anteile",
            r"Aufsichtsrat",
            r"Körperschaftsteuer|§ ?38",
            r"Vorabausschüttung",
            r"Sonderbetriebseinnahmen",
            r"Innenumsätze",
            r"Genossenschaftliche Rückvergütung",
            r"Ausgleichsposten",
            r"Herstellungskosten|Verwaltungskosten|Vertriebskosten",
            r"§ 246 Abs\. 2 HGB",
            r"Saldenvortr|Summenvortrag",
        )
    )
)


def check_neutral_pl(chart, rows, result):
    """No income or expense account with money flow is silently neutral."""
    names = {row["code"]: row for row in rows}
    problems = []
    for code, category, conf, _case in result:
        row = names[code]
        if (
            category == "NEUTRAL"
            and conf in ("h", "m")
            and row["odoo_account_type"] in INCOME_EXPENSE + ("expense_direct_cost",)
            and not NEUTRAL_PL_NAMES.search(row["name_de"])
        ):
            problems.append(f"{code} {row['name_de']}")
    if problems:
        raise GenError(f"{chart}: neutral income or expense accounts: {problems}")


# ------------------------------------------------------------------ main
def classify_chart(chart, rows):
    """``(code, category, confidence, case)`` per account, sorted by code."""
    result = [
        (row["code"], *correct(chart, row, *classify_row(chart, row))) for row in rows
    ]
    return sorted(result, key=lambda item: int(item[0]))


def classify_row(chart, row):
    return classify(chart, row), confidence(chart, row), row["mehrdeutig"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--spec",
        type=Path,
        required=True,
        help="research folder with kontenzuordnung_skr03.csv and _skr04.csv",
    )
    parser.add_argument("--out", type=Path, default=MODULE, help="module folder")
    parser.add_argument(
        "--templates", type=Path, help="l10n_de/data/template of Odoo 19 (verify)"
    )
    parser.add_argument("--check", action="store_true", help="compare, do not write")
    args = parser.parse_args(argv)

    check_catalogue()
    field_ids = read_field_ids(args.out)
    outputs = {
        args.out / "data" / "categories.xml": render_categories(),
    }
    for year in YEARS:
        outputs[args.out / "data" / f"category_mapping_{year}.xml"] = render_mappings(
            year, field_ids
        )
    assigned = {}
    templates = {}
    rules = []
    used = Counter()
    for chart, (research_file, _template_file, expected) in CHARTS.items():
        rows = read_research(args.spec / research_file)
        if len(rows) != expected:
            raise GenError(f"{chart}: {len(rows)} accounts, {expected} expected")
        if args.templates:
            templates[chart] = read_templates(args.templates, chart)
            check_templates(chart, rows, templates[chart])
        result = classify_chart(chart, rows)
        used.update(category for _code, category, *_rest in result)
        assigned[chart] = {code: category for code, category, *_rest in result}
        check_neutral_pl(chart, rows, result)
        table = [
            (template_xml_id(chart, code), code, category, conf, case)
            for code, category, conf, case in result
        ]
        table += [
            (xml_id, "", category, conf, "") for xml_id, category, conf in UTILITY
        ]
        table += [
            ("", code, category, conf, "")
            for code, category, conf in EXTRA_ACCOUNTS[chart]
        ]
        for _xml_id, category, _conf in UTILITY:
            used[category] += 1
        outputs[args.out / "data" / "mapping" / f"category_{chart}.csv"] = render_csv(
            ("xml_id", "code", "category", "confidence", "case"), table
        )
        extra = [(code, category) for code, category, _conf in EXTRA_ACCOUNTS[chart]]
        if {code for code, _category in extra} & set(assigned[chart]):
            raise GenError(f"{chart}: extra DATEV account is a template account")
        rules += range_rules(chart, result + extra, assigned[chart])
    outputs[args.out / "data" / "mapping" / "range_rules.csv"] = render_csv(
        ("chart", "code_from", "code_to", "category"), rules
    )
    check_corrections(assigned)
    check_rules(rules)
    if templates:
        print(f"{check_pairs(templates, assigned)} account pairs with equal names")
    unused = [
        c["code"]
        for c in CATEGORIES
        if not used[c["code"]] and c["code"] not in MANUAL_ONLY
    ]
    if unused:
        raise GenError(f"categories without account: {unused}")

    differences = []
    for path, content in outputs.items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == content:
            continue
        differences.append(path)
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    mappings = sum(len(c["mappings"]) for c in CATEGORIES)
    print(f"{len(CATEGORIES)} categories, {mappings} mappings per year")
    for chart in CHARTS:
        print(f"{chart}: {len(assigned[chart])} template accounts")
    print(f"{len(rules)} range rules")
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
