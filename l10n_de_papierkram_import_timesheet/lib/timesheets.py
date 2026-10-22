# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Time entries and products of Papierkram exports, without Odoo.

``Zeiteinträge.csv`` has the same columns in the yearly export and in the
export of the list of time entries (Zeiterfassung → Einzelbuchungen → CSV).
Papierkram's list can be filtered by "Noch nicht abgerechnet"; this is the
only place that tells which time entries are not billed yet.
"""

import hashlib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from odoo.addons.l10n_de_papierkram_import.lib.reader import (
    dict_rows,
    parse_amount,
    parse_bool,
    parse_date,
)

#: Papierkram's time entry without project
NO_PROJECT = "Kein Projekt"
#: units of products: Papierkram name → kind of unit
UNITS = {"stunde": "hour", "stunden": "hour", "tag": "day", "tage": "day"}


def parse_duration(value):
    """Duration ``h:mm`` (also ``45m`` or ``2h``) as hours (``Decimal``)."""
    text = (value or "").strip().lower()
    if not text:
        return Decimal(0)
    if ":" in text:
        hours, minutes = text.split(":", 1)
        return Decimal(int(hours or 0)) + Decimal(int(minutes or 0)) / 60
    if text.endswith("m"):
        return Decimal(int(text[:-1] or 0)) / 60
    if text.endswith("h"):
        return Decimal(text[:-1].replace(",", "."))
    raise ValueError(f"Unknown duration: {value!r}")


@dataclass(frozen=True)
class TimeEntry:
    """Line of ``Zeiteinträge.csv``."""

    day: date
    start: str
    end: str
    duration: Decimal  # hours
    billable_duration: Decimal  # hours
    not_billable: bool
    user: str
    customer: str
    project: str
    task: str
    service: str
    rate: Decimal
    amount: Decimal
    comment: str
    key: str

    @property
    def without_project(self):
        return not self.project or self.project == NO_PROJECT


@dataclass(frozen=True)
class Product:
    """Line of ``Waren_oder_Dienstleistungen.csv``."""

    name: str
    code: str
    description: str
    kind: str
    price: Decimal
    unit: str  # "hour" | "day" | "unit"
    unit_name: str
    vat_rate: Decimal


def _key(row, occurrence):
    """Stable identity of a time entry (Papierkram exports no id)."""
    parts = [
        row.get(name, "")
        for name in (
            "Datum",
            "Beginn",
            "Ende",
            "Dauer (hh:mm)",
            "Zeiterfasser",
            "Kunde",
            "Projekt",
            "Aufgabe",
            "Kommentar",
        )
    ]
    parts.append(str(occurrence))
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()


def parse_time_entries(rows):
    """:class:`TimeEntry` of the rows (dicts) in their order.

    Identical rows (same day, times, customer and comment) are kept apart by
    their occurrence, so a second import finds each of them again.
    """
    seen = {}
    entries = []
    for row in rows:
        if not (row.get("Datum") or "").strip():
            continue
        base = _key(row, 0)
        occurrence = seen.get(base, 0)
        seen[base] = occurrence + 1
        duration = parse_duration(row.get("Dauer (hh:mm)"))
        billable = row.get("Dauer (abrechenbar) (hh:mm)")
        entries.append(
            TimeEntry(
                day=parse_date(row["Datum"]),
                start=(row.get("Beginn") or "").strip(),
                end=(row.get("Ende") or "").strip(),
                duration=duration,
                billable_duration=parse_duration(billable) if billable else duration,
                not_billable=parse_bool(row.get("Nicht-Abrechenbar")),
                user=(row.get("Zeiterfasser") or "").strip(),
                customer=(row.get("Kunde") or "").strip(),
                project=(row.get("Projekt") or "").strip() or NO_PROJECT,
                task=(row.get("Aufgabe") or "").strip(),
                service=(row.get("Dienstleistung") or "").strip(),
                rate=parse_amount(row.get("Preis pro Stunde")) or Decimal(0),
                amount=parse_amount(row.get("Betrag ()")) or Decimal(0),
                comment=(row.get("Kommentar") or "").strip(),
                key=_key(row, occurrence),
            )
        )
    return entries


def parse_time_entry_file(text):
    """:class:`TimeEntry` of a CSV file of Papierkram's list of time entries."""
    return parse_time_entries(dict_rows(text))


def parse_products(rows):
    products = []
    for row in rows:
        name = (row.get("Name") or "").strip()
        if not name:
            continue
        unit_name = (row.get("Einheitbezeichnung Einzel") or "").strip()
        products.append(
            Product(
                name=name,
                code=(row.get("Artikelnummer") or "").strip(),
                description=(row.get("Beschreibung") or "").strip(),
                kind=(row.get("Typ") or "").strip(),
                price=parse_amount(row.get("Netto-Preis")) or Decimal(0),
                unit=UNITS.get(unit_name.lower(), "unit"),
                unit_name=unit_name,
                vat_rate=parse_amount(row.get("USt.-Satz")) or Decimal(0),
            )
        )
    return products
