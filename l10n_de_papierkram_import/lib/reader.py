# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Reader for the Papierkram full export (CSV, PDF).

Papierkram exports one ZIP file per year. Master data (contacts, bank
transactions, products) is complete in every export, while invoices, vouchers
and payments are cut by the *creation date* of their document. A payment is
therefore contained in the export of the year its document was created, not in
the year it was paid. All yearly exports must be merged to get the full
picture; this is what :class:`PapierkramExport` does.

This module does not depend on Odoo and can be tested on its own.
"""

import csv
import io
import os
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

CSV_DIR = "CSV"
MAX_CSV_BYTES = 50 * 1024 * 1024

# Categories missing from the expense category list: legacy expense categories
# Papierkram no longer offers and the categories of income vouchers. SKR03
# account as shown in Papierkram's category tooltip.
LEGACY_CATEGORIES = {
    "Geschenke bis 35 Euro": "4630",
    "Geschenke über 35 Euro": "4635",
    "Sonstige Einnahme (Erlöse)": "8200",
}


class PapierkramFormatError(ValueError):
    """The file is not a readable Papierkram export."""


@dataclass(frozen=True)
class Note:
    """Something the import must report. ``code`` selects the message."""

    code: str
    params: dict

    def __str__(self):
        return f"{self.code}: " + ", ".join(f"{k}={v}" for k, v in self.params.items())


def parse_amount(value):
    """Convert ``1.234,56`` (German) or ``-40.0`` to ``Decimal``.

    Empty values give ``None``.
    """
    value = (value or "").strip()
    if not value:
        return None
    if "," in value:
        value = value.replace(".", "").replace(",", ".")
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise PapierkramFormatError(f"Not an amount: {value!r}") from exc


def parse_date(value):
    """Read ``DD.MM.YYYY`` or ``YYYY-MM-DD``. Empty values give ``None``."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise PapierkramFormatError(f"Not a date: {value!r}")


def parse_bool(value):
    return (value or "").strip().lower() in ("ja", "yes", "true", "1")


def dict_rows(text):
    """Semicolon CSV text as a list of dicts.

    Removes a BOM and blank lines before the header; the fixed asset CSV for
    example starts with a blank line.
    """
    text = text.lstrip("﻿").lstrip("\r\n")
    return list(csv.DictReader(io.StringIO(text, newline=""), delimiter=";"))


@dataclass(frozen=True)
class Contact:
    number: str
    name: str
    kind: str  # "customer" | "supplier"
    street: str = ""
    zip: str = ""
    city: str = ""
    country: str = ""
    email: str = ""
    phone: str = ""
    website: str = ""
    vat: str = ""
    iban: str = ""
    bic: str = ""
    note: str = ""
    salutation: str = ""
    first_name: str = ""
    last_name: str = ""


@dataclass(frozen=True)
class Invoice:
    """Customer invoice, cancellation invoice or credit note."""

    number: str  # empty for drafts
    kind: str  # "invoice" | "cancellation" | "credit_note"
    state: str  # "draft" | "open" | "paid" | "cancelled"
    subject: str
    customer_name: str
    customer_number: str
    country: str
    date: date
    due_date: date | None
    net: Decimal
    tax: Decimal
    gross: Decimal
    cash_discount: Decimal
    paid: Decimal
    last_payment_date: date | None
    note: str
    project: str
    export_year: int

    @property
    def is_draft(self):
        return self.state == "draft"

    @property
    def base_number(self):
        """Number of the original invoice.

        ``R-00077-S`` (cancellation) and ``R-00077-2`` (re-issue) belong to
        ``R-00077``; ``R-00076-2-S`` cancels ``R-00076-2``.
        """
        if self.kind == "cancellation" and self.number.endswith("-S"):
            return self.number[:-2]
        return self.number


@dataclass(frozen=True)
class VoucherLine:
    category: str
    gross: Decimal | None  # None if the export does not contain the split
    title: str = ""


@dataclass(frozen=True)
class Voucher:
    """Expense or income voucher (supplier invoice, receipt)."""

    number: str
    subject: str
    supplier: str
    date: date
    due_date: date | None
    paid_on: date | None
    state: str  # "paid" | "unpaid" | "canceled" | "deleted"
    net: Decimal  # positive
    tax: Decimal  # positive
    gross: Decimal  # positive
    private_share: Decimal
    origin: str  # "domestic" | "foreign" | ...
    incoming: bool  # True: money comes in (refund, income voucher)
    categories: tuple
    lines: tuple
    project: str
    note: str
    export_year: int

    @property
    def needs_split(self):
        """Several categories but no amount per category known."""
        return len(self.categories) > 1 and any(
            line.gross is None for line in self.lines
        )


@dataclass(frozen=True)
class Payment:
    """Payment assignment from ``Zahlungseingänge.csv``.

    Positive amounts are receipts (invoices), negative ones disbursements
    (vouchers).
    """

    reference: str
    customer_number: str
    invoice_number: str
    voucher_number: str
    date: date
    amount: Decimal


@dataclass(frozen=True)
class CashEvent:
    """Money flow of a document, the basis of cash-basis accounting.

    ``source`` tells where the date comes from:

    - ``payment``: payment assignment from ``Zahlungseingänge.csv``
    - ``last_payment_date``: remainder of an invoice that Papierkram only knows
      from a bank transaction (date = "letztes Bezahldatum")
    - ``voucher_paid_on``: paid voucher without payment assignment
      (date = "Zahlung am")

    The last two are derived and marked for review by the import.
    """

    document: str
    date: date
    amount: Decimal  # positive = receipt, negative = disbursement
    source: str


@dataclass(frozen=True)
class BankTransaction:
    account_id: str
    booking_date: date | None
    value_date: date | None
    amount: Decimal
    currency: str
    purpose: str
    counterpart_name: str
    counterpart_iban: str
    status: str
    cancelled: bool


@dataclass(frozen=True)
class Asset:
    """Line of the fixed asset CSV (Ausgaben → Anlagegüter → CSV, filter "Alle")."""

    voucher_number: str
    name: str
    residual_value: Decimal
    acquisition_date: date
    acquisition_value: Decimal
    monthly_depreciation: Decimal

    @property
    def useful_life_months(self):
        """Useful life in months; 0 for immediately written-off assets."""
        if not self.monthly_depreciation:
            return 0
        return int(
            (self.acquisition_value / self.monthly_depreciation).to_integral_value()
        )


@dataclass(frozen=True)
class Category:
    name: str
    account_skr03: str
    kind: str  # "AFA" | "GWG" | "INSTANT" | ""
    category_type: str  # "ASSET" | "EXPENSE" | "PRIV_COL" | "TAX_EXPENSE" ...


def split_categories(text, known):
    """Split the field ``Kategorien`` into single category names.

    Papierkram joins several categories with ``", "``, but some names contain a
    comma themselves (``"Zeitschriften, Bücher"``). The longest known name is
    therefore matched greedily from the left. Unknown parts stay single.
    """
    parts = [p.strip() for p in (text or "").split(",")]
    parts = [p for p in parts if p]
    result = []
    i = 0
    while i < len(parts):
        for j in range(len(parts), i, -1):
            candidate = ", ".join(parts[i:j])
            if candidate in known:
                result.append(candidate)
                i = j
                break
        else:
            result.append(parts[i])
            i += 1
    return tuple(result)


class _Source:
    """A single export, as ZIP file or extracted directory."""

    _year_re = re.compile(r"(?:_|^)(\d{4})(?:\.zip)?$")

    def __init__(self, path):
        self.path = path
        self.is_zip = zipfile.is_zipfile(path) if os.path.isfile(path) else False
        if not self.is_zip and not os.path.isdir(path):
            raise PapierkramFormatError(f"Neither ZIP nor directory: {path}")
        self._zip = zipfile.ZipFile(path) if self.is_zip else None
        self.names = self._list()
        if not any(n.startswith(CSV_DIR + "/") for n in self.names):
            raise PapierkramFormatError(
                "Not a Papierkram export (CSV folder missing): "
                f"{os.path.basename(path)}"
            )
        base = os.path.basename(os.path.normpath(path))
        stem = base[:-4] if base.lower().endswith(".zip") else base
        match = self._year_re.search(stem)
        self.year = int(match.group(1)) if match else 0

    def _list(self):
        if self._zip:
            return [i.filename for i in self._zip.infolist() if not i.is_dir()]
        out = []
        for root, _dirs, files in os.walk(self.path):
            for name in files:
                rel = os.path.relpath(os.path.join(root, name), self.path)
                out.append(rel.replace(os.sep, "/"))
        return out

    def read_bytes(self, member):
        if member not in self.names:
            raise KeyError(member)
        if self._zip:
            info = self._zip.getinfo(member)
            if info.file_size > MAX_CSV_BYTES and member.endswith(".csv"):
                raise PapierkramFormatError(f"CSV too large: {member}")
            return self._zip.read(member)
        with open(os.path.join(self.path, *member.split("/")), "rb") as fh:
            return fh.read()

    def rows(self, csv_name):
        member = f"{CSV_DIR}/{csv_name}"
        if member not in self.names:
            # file names with umlauts may be decomposed (macOS)
            wanted = unicodedata.normalize("NFC", member)
            member = next(
                (n for n in self.names if unicodedata.normalize("NFC", n) == wanted),
                None,
            )
            if not member:
                return []
        return dict_rows(self.read_bytes(member).decode("utf-8-sig"))

    def close(self):
        if self._zip:
            self._zip.close()


@dataclass
class PapierkramExport:
    """Merged content of one or more yearly exports."""

    contacts: dict = field(default_factory=dict)
    invoices: dict = field(default_factory=dict)
    vouchers: dict = field(default_factory=dict)
    payments: list = field(default_factory=list)
    bank_transactions: list = field(default_factory=list)
    assets: list = field(default_factory=list)
    categories: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    _sources: list = field(default_factory=list, repr=False)
    _invoice_pdfs: dict = field(default_factory=dict, repr=False)
    _voucher_pdfs: dict = field(default_factory=dict, repr=False)

    INVOICE_KIND = {
        "Rechnung": "invoice",
        "Stornorechnung": "cancellation",
        "Gutschrift": "credit_note",
    }
    INVOICE_STATE = {
        "Entwurf": "draft",
        "Unbezahlt": "open",
        "Überfällig": "open",
        "Teilweise bezahlt": "open",
        "Bezahlt": "paid",
        "Überbezahlt": "paid",
        "Storniert": "cancelled",
    }

    @classmethod
    def load(cls, paths, categories_csv=None, assets_csv=None, splits_csv=None):
        """Read exports (ZIP or directory) and optional additional files.

        :param paths: paths of the yearly exports, any order
        :param categories_csv: category list with SKR03 accounts (text)
        :param assets_csv: fixed asset CSV with filter "Alle" (text)
        :param splits_csv: split of multi-category vouchers
            (``belegnummer;position;kategorie;betrag_brutto``, text)
        """
        export = cls()
        if categories_csv:
            export._load_categories(categories_csv)
        sources = sorted((_Source(p) for p in paths), key=lambda s: s.year)
        export._sources = sources
        known = set(export.categories) | set(LEGACY_CATEGORIES)
        for src in sources:
            export._load_documents(src, known)
        if sources:
            newest = sources[-1]
            export._load_contacts(newest)
            export._load_bank(newest)
        if splits_csv:
            export._apply_splits(splits_csv)
        if assets_csv:
            export._load_assets(assets_csv)
        export._check()
        return export

    @property
    def years(self):
        """Years of the loaded exports, ascending."""
        return [src.year for src in self._sources]

    def table(self, csv_name, newest=False):
        """Rows of a further CSV file of the exports as ``(year, row)``.

        :param csv_name: file name below ``CSV/``, e.g. ``Zeiteinträge.csv``
        :param newest: only the newest export (master data such as products
            are complete in every export)
        """
        sources = self._sources[-1:] if newest else self._sources
        return [(src.year, row) for src in sources for row in src.rows(csv_name)]

    def _note(self, code, **params):
        self.notes.append(Note(code, params))

    # -- Loading ---------------------------------------------------------

    def _load_categories(self, text):
        for row in dict_rows(text):
            name = row["name"].strip()
            self.categories[name] = Category(
                name=name,
                account_skr03=str(row.get("skr03") or "").strip(),
                kind=(row.get("abschreibungsart") or "").strip(),
                category_type=(row.get("typ") or "").strip(),
            )

    def _load_documents(self, src, known):
        for row in src.rows("Rechnungen.csv"):
            inv = self._parse_invoice(row, src.year)
            key = (
                inv.number
                or f"DRAFT:{src.year}:{inv.date}:{inv.customer_number}:{inv.gross}"
            )
            self.invoices[key] = inv
        for member in src.names:
            if member.startswith("Rechnungen/") and member.endswith(".pdf"):
                match = re.search(r"Rechnung_(R-[0-9]+(?:-[0-9A-Z]+)*)_", member)
                if match:
                    self._invoice_pdfs[match.group(1)] = (src, member)
            elif member.startswith("Belege/") and member.endswith(".pdf"):
                match = re.match(r"Belege/(B-[0-9]+(?:-[0-9]+)?)_(\d+)\.pdf$", member)
                if match:
                    self._voucher_pdfs.setdefault(match.group(1), []).append(
                        (int(match.group(2)), src, member)
                    )
        for row in src.rows("Belege.csv"):
            voucher = self._parse_voucher(row, src.year, known)
            self.vouchers[voucher.number] = voucher
        seen = {(p.reference, p.date, p.amount) for p in self.payments}
        for row in src.rows("Zahlungseingänge.csv"):
            pay = Payment(
                reference=row["Buchung"],
                customer_number=row["Kundennummer"],
                invoice_number=row["Rechnungsnummer"],
                voucher_number=row["Belegnummer"],
                date=parse_date(row["Datum"]),
                amount=parse_amount(row["Betrag"]),
            )
            key = (pay.reference, pay.date, pay.amount)
            if key not in seen:
                seen.add(key)
                self.payments.append(pay)

    def _parse_invoice(self, row, year):
        kind = self.INVOICE_KIND.get(row["Typ"])
        state = self.INVOICE_STATE.get(row["Status"])
        if not kind or not state:
            raise PapierkramFormatError(
                f"Unknown invoice type/state: {row['Typ']!r}/{row['Status']!r}"
            )
        return Invoice(
            number=row["Rechnungsnr."].strip(),
            kind=kind,
            state=state,
            subject=row["Betreff"],
            customer_name=row["Kunde"],
            customer_number=row["Kundennummer"],
            country=row["Land"],
            date=parse_date(row["Datum"]),
            due_date=parse_date(row["Fälligkeitsdatum"]),
            net=parse_amount(row["Netto"]) or Decimal(0),
            tax=parse_amount(row["USt."]) or Decimal(0),
            gross=parse_amount(row["Brutto"]) or Decimal(0),
            cash_discount=parse_amount(row["Skonto"]) or Decimal(0),
            paid=parse_amount(row["Bezahlt"]) or Decimal(0),
            last_payment_date=parse_date(row["letztes Bezahldatum"]),
            note=row["Notiz"],
            project=row["Projekt"],
            export_year=year,
        )

    def _parse_voucher(self, row, year, known):
        categories = split_categories(row["Kategorien"], known)
        signed = parse_amount(row["Brutto"]) or Decimal(0)
        gross = abs(signed)
        if len(categories) <= 1:
            lines = (
                VoucherLine(category=categories[0] if categories else "", gross=gross),
            )
        else:
            lines = tuple(VoucherLine(category=c, gross=None) for c in categories)
        return Voucher(
            number=row["Belegnummer"].strip(),
            subject=row["Gegenstand"],
            supplier=row["Lieferant"],
            date=parse_date(row["Datum"]),
            due_date=parse_date(row["Zahlungsfrist"]),
            paid_on=parse_date(row["Zahlung am"]),
            state=row["Status"].strip(),
            net=abs(parse_amount(row["Netto"]) or Decimal(0)),
            tax=abs(parse_amount(row["USt."]) or Decimal(0)),
            gross=gross,
            private_share=parse_amount(row["Privatanteil"]) or Decimal(0),
            origin=row["Herkunftsort"],
            incoming=signed > 0,
            categories=categories,
            lines=lines,
            project=row["Projekte"],
            note=row["Notizen"],
            export_year=year,
        )

    def _load_contacts(self, src):
        for row in src.rows("Kontakte.csv"):
            number = row["Kunden-/Lieferantennr."].strip()
            self.contacts[number] = Contact(
                number=number,
                name=row["Name"].strip(),
                kind="supplier" if row["Kontaktart"] == "Lieferant" else "customer",
                street=row["Straße (Post)"],
                zip=row["PLZ (Post)"],
                city=row["Stadt (Post)"],
                country=row["Land (Post)"] or row["Land"],
                email=row["E-Mail"] or row["E-Mail (Standardempfänger)"],
                phone=row["Telefon"] or row["Telefonnummer (Standardempfänger)"],
                website=row["Website"],
                vat=row["UStID"],
                iban=row["IBAN"],
                bic=row["BIC"],
                note=row["Notiz"],
                salutation=row["Anrede (Standardempfänger)"],
                first_name=row["Vorname (Standardempfänger)"],
                last_name=row["Nachname (Standardempfänger)"],
            )

    def _load_bank(self, src):
        for row in src.rows("Kontoumsätze.csv"):
            self.bank_transactions.append(
                BankTransaction(
                    account_id=row["Bankverbindung-ID"],
                    booking_date=parse_date(row["Buchungsdatum"]),
                    value_date=parse_date(row["Valuta"]),
                    amount=parse_amount(row["Zahlungswert"]) or Decimal(0),
                    currency=row["Zahlungswährung"] or "EUR",
                    purpose=" ".join(row["Verwendungszweck"].split()),
                    counterpart_name=row["Absender-Name"],
                    counterpart_iban=row["Absender-IBAN"],
                    status=row["Status"],
                    cancelled=parse_bool(row["Storniert"]),
                )
            )

    def _apply_splits(self, text):
        by_voucher = {}
        for row in dict_rows(text):
            by_voucher.setdefault(row["belegnummer"].strip(), []).append(row)
        for number, rows in by_voucher.items():
            voucher = self.vouchers.get(number)
            if not voucher:
                self._note("split_unknown_voucher", voucher=number)
                continue
            rows.sort(key=lambda r: int(r["position"]))
            lines = tuple(
                VoucherLine(
                    category=r["kategorie"].strip(),
                    gross=Decimal(r["betrag_brutto"].strip()),
                    title=(r.get("titel") or "").strip(),
                )
                for r in rows
            )
            total = sum(line.gross for line in lines)
            if total != voucher.gross:
                self._note(
                    "split_sum_mismatch",
                    voucher=number,
                    total=total,
                    gross=voucher.gross,
                )
                continue
            self.vouchers[number] = replace(voucher, lines=lines)

    def _load_assets(self, text):
        for row in dict_rows(text):
            self.assets.append(
                Asset(
                    voucher_number=row["Belegnummer"].strip(),
                    name=row["Beleg"].strip(),
                    residual_value=parse_amount(row["Restwert Jahresende"])
                    or Decimal(0),
                    acquisition_date=parse_date(row["Anschaffungsdatum"]),
                    acquisition_value=parse_amount(row["Anschaffungswert"]),
                    monthly_depreciation=parse_amount(row["monatl. Abschreibung"])
                    or Decimal(0),
                )
            )

    def _check(self):
        for voucher in self.vouchers.values():
            if voucher.state in ("paid", "unpaid") and voucher.needs_split:
                self._note(
                    "split_missing",
                    voucher=voucher.number,
                    categories=", ".join(voucher.categories),
                )
            unknown = [
                c
                for c in voucher.categories
                if self.categories
                and c not in self.categories
                and c not in LEGACY_CATEGORIES
            ]
            if unknown:
                self._note(
                    "unknown_category",
                    voucher=voucher.number,
                    categories=", ".join(unknown),
                )
        for pay in self.payments:
            if pay.invoice_number and pay.invoice_number not in self.invoices:
                self._note("payment_unknown_invoice", reference=pay.reference)
            if pay.voucher_number and pay.voucher_number not in self.vouchers:
                self._note("payment_unknown_voucher", reference=pay.reference)

    # -- Access ----------------------------------------------------------

    def invoice_pdf(self, number):
        """PDF content of an invoice or ``None``."""
        found = self._invoice_pdfs.get(number)
        return found[0].read_bytes(found[1]) if found else None

    def voucher_pdfs(self, number):
        """List of ``(file name, content)`` of a voucher, in page order."""
        files = sorted(self._voucher_pdfs.get(number, []), key=lambda x: x[0])
        return [(os.path.basename(m), s.read_bytes(m)) for _i, s, m in files]

    def account_for_category(self, name):
        """SKR03 account of a category or ``None``."""
        if name in self.categories:
            return self.categories[name].account_skr03 or None
        return LEGACY_CATEGORIES.get(name)

    def cash_events(self):
        """All money flows per document, completed by unassigned payments.

        Cancellation invoices create no money flow; they only offset the
        cancelled invoice. Drafts and cancelled or deleted vouchers without
        payment are ignored.
        """
        events = []
        by_doc = {}
        for pay in self.payments:
            doc = pay.invoice_number or pay.voucher_number
            events.append(CashEvent(doc, pay.date, pay.amount, "payment"))
            by_doc[doc] = by_doc.get(doc, Decimal(0)) + pay.amount
        for inv in self.invoices.values():
            if inv.is_draft or inv.kind == "cancellation" or not inv.paid:
                continue
            rest = inv.paid - by_doc.get(inv.number, Decimal(0))
            if not rest or (rest > 0) != (inv.paid > 0):
                continue
            if not inv.last_payment_date:
                self._note("invoice_paid_without_date", invoice=inv.number, amount=rest)
                continue
            events.append(
                CashEvent(inv.number, inv.last_payment_date, rest, "last_payment_date")
            )
        for voucher in self.vouchers.values():
            if voucher.state != "paid" or voucher.number in by_doc:
                continue
            if not voucher.paid_on:
                self._note("voucher_paid_without_date", voucher=voucher.number)
                continue
            amount = voucher.gross if voucher.incoming else -voucher.gross
            events.append(
                CashEvent(voucher.number, voucher.paid_on, amount, "voucher_paid_on")
            )
        return events

    def payments_for(self, number):
        return [
            p for p in self.payments if number in (p.invoice_number, p.voucher_number)
        ]

    def close(self):
        for src in self._sources:
            src.close()
