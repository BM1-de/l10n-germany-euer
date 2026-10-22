# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import base64
import csv
import datetime
import io
import re
from decimal import Decimal, InvalidOperation

from psycopg2 import DataError, IntegrityError

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from ..models.engine import depreciation as dep
from ..models.euer_asset import (
    ASSET_KINDS,
    AVEUER_GROUPS,
    METHODS,
    VAT_TREATMENTS,
    to_decimal,
)

#: column → accepted headers (lower case), spec 6.6
COLUMNS = {
    "name": ("name", "bezeichnung"),
    "code": ("code", "inventarnummer"),
    "aveuer_group": ("group", "gruppe"),
    "asset_kind": ("kind", "art"),
    "delivery_date": ("delivery_date", "lieferdatum"),
    "cost": ("cost", "ak"),
    "cost_net": ("cost_net", "ak netto"),
    "vat_treatment": ("vat_treatment", "vst-status"),
    "useful_life_months": ("useful_life_months", "nd (monate)", "nd"),
    "method": ("method", "methode"),
    "degressive_rate": ("rate", "satz"),
    "switch_to_linear_year": (
        "switch_to_linear_year",
        "wechsel zu linear",
        "wechsel zu linear (jahr)",
    ),
    "accumulated": ("accumulated", "kumulierte afa bis stichtag", "kumulierte afa"),
    "book_value": ("book_value", "restbuchwert"),
    "memo_value": ("memo_value", "erinnerungswert"),
    "pool_year": ("pool_year", "sammelposten-jahrgang"),
    "pool_remaining": ("pool_remaining", "sammelposten-restbetrag"),
    "sec7g_special": ("sec7g_special", "sonder-afa § 7g", "sonder-afa"),
    "sec7g_year": ("sec7g_year", "jahr sonder-afa § 7g", "jahr sonder-afa"),
}
REQUIRED = ("name", "aveuer_group", "asset_kind", "cost")
ALIASES = {
    "asset_kind": {
        "abnutzbar": "depreciable",
        "gwg": "gwg",
        "sammelposten": "pool",
        "sofortabzug": "instant_250",
        "nicht abnutzbar": "non_depreciable",
        "umlaufvermögen": "current_4_3_4",
    },
    "aveuer_group": {
        "grund und boden": "land",
        "gebäude": "building",
        "grundstücksrechte": "land_rights",
        "arbeitszimmer grund und boden": "office_land",
        "arbeitszimmer gebäude": "office_building",
        "immaterielle wg": "intangible",
        "kfz": "vehicle",
        "büroausstattung": "office_equipment",
        "andere": "other_movable",
        "andere bewegliche wg": "other_movable",
        "finanzanlagen teileinkünfte": "fin_tev",
        "finanzanlagen": "fin_other",
        "umlaufvermögen": "current",
    },
    "method": {
        "linear": "linear",
        "degressiv": "degressive",
        "e-kfz": "ev_7_2a",
        "digital": "digital_1y",
        "keine": "none",
        "manuell": "manual",
    },
    "vat_treatment": {
        "abziehbar": "deductible",
        "nicht abziehbar": "not_deductible",
        "§ 15 abs. 1a": "sec15_1a",
    },
}
SELECTIONS = {
    "asset_kind": ASSET_KINDS,
    "aveuer_group": AVEUER_GROUPS,
    "method": METHODS,
    "vat_treatment": VAT_TREATMENTS,
}
AMOUNTS = (
    "cost",
    "cost_net",
    "accumulated",
    "book_value",
    "memo_value",
    "pool_remaining",
    "sec7g_special",
)
#: whole numbers and their range (the database stores 32-bit integers)
INTEGERS = {
    "useful_life_months": (0, 1200),
    "pool_year": (1900, 2100),
    "switch_to_linear_year": (1900, 2100),
    "sec7g_year": (1900, 2100),
}
#: difference to the linear depreciation that is more than rounding
LINEAR_TOLERANCE = Decimal("1.00")


#: Dots as thousands separators without decimals, e.g. 2.400 or 1.234.567
THOUSANDS = re.compile(r"^-?\d{1,3}(\.\d{3})+$")


def parse_number(text):
    """Number in German (1.234,56) or technical (1234.56) notation.

    A dot followed by groups of three digits and no comma is a thousands
    separator (German "2.400" is 2,400, not 2.4). Returns ``None`` for an
    empty text and raises :class:`ValueError` for anything else.
    """
    text = (text or "").strip().replace(" ", "").replace("€", "").replace("%", "")
    if not text:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    elif THOUSANDS.match(text):
        text = text.replace(".", "")
    try:
        return Decimal(text)
    except InvalidOperation as error:
        raise ValueError(text) from error


def parse_amount(text):
    """Amount in euros with at most two decimals (spec 6.6)."""
    number = parse_number(text)
    if number is None:
        return None
    if number != number.quantize(dep.CENT):
        raise ValueError(text)
    return dep.cents(number)


def parse_rate(text):
    """Degressive rate as fraction: "0,25", "25" and "25 %" are 25 %."""
    number = parse_number(text)
    if number is None:
        return None
    return number / 100 if number > 1 else number


def parse_date(text):
    text = (text or "").strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise ValueError(text)


def parse_choice(column, text):
    text = (text or "").strip()
    if not text:
        return None
    lower = text.lower()
    for key, label in SELECTIONS[column]:
        if lower in (key, label.lower()):
            return key
    if lower in ALIASES[column]:
        return ALIASES[column][lower]
    raise ValueError(text)


class L10nDeEuerAssetImport(models.TransientModel):
    """Take over an asset register from another system (spec 6.6, R-48).

    Each row becomes an asset with the takeover date (31 December of the last
    year of the previous system) and its book value at that date. Years up
    to the takeover date are not calculated again, so years already filed
    stay as they are. Rows whose book value is not cost less accumulated
    depreciation are rejected; nothing is imported while a row has an error.
    """

    _name = "l10n_de_euer.asset.import"
    _description = "EÜR Asset Register Import"

    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company._l10n_de_euer_business_root(),
        domain="['|', ('parent_id', '=', False), "
        "('l10n_de_euer_separate_business', '=', True)]",
    )
    opening_date = fields.Date(
        string="Takeover Date",
        required=True,
        default=lambda self: datetime.date(
            fields.Date.context_today(self).year - 1, 12, 31
        ),
        help="31 December of the last year calculated in the previous system.",
    )
    file = fields.Binary(string="CSV File", required=True)
    filename = fields.Char()
    line_ids = fields.One2many("l10n_de_euer.asset.import.line", "wizard_id")
    total_ids = fields.One2many(
        "l10n_de_euer.asset.import.total",
        "wizard_id",
        string="Filed AVEÜR",
        help="Book values at the end of the takeover year per group of the Anlage "
        "AVEÜR filed with the previous system (optional). The checks of the "
        "following year compare them with the register (C-61).",
    )
    error_count = fields.Integer(compute="_compute_error_count")
    state = fields.Selection(
        [("upload", "Upload"), ("preview", "Preview"), ("done", "Done")],
        default="upload",
        required=True,
    )

    @api.constrains("opening_date")
    def _check_opening_date(self):
        for wizard in self:
            date = wizard.opening_date
            if date and (date.month, date.day) != (12, 31):
                raise ValidationError(
                    self.env._("The takeover date must be a 31 December.")
                )

    @api.depends("line_ids.error")
    def _compute_error_count(self):
        for wizard in self:
            wizard.error_count = len(wizard.line_ids.filtered("error"))

    def _reopen(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    # ------------------------------------------------------------ parsing
    def _read_rows(self):
        data = base64.b64decode(self.file or b"")
        not_csv = self.env._(
            "The file is no CSV file. Save the asset register as CSV in UTF-8 "
            "or Windows-1252 (e.g. not as an Excel workbook)."
        )
        if b"\x00" in data:
            raise UserError(not_csv)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = data.decode("cp1252")
            except UnicodeDecodeError as error:
                raise UserError(not_csv) from error
        delimiter = ";" if text.split("\n", 1)[0].count(";") else ","
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = [row for row in reader if any(cell.strip() for cell in row)]
        if not rows:
            raise UserError(self.env._("The file is empty."))
        header = [cell.strip().lower() for cell in rows[0]]
        columns = {}
        for column, names in COLUMNS.items():
            for index, cell in enumerate(header):
                if cell in names:
                    columns[column] = index
        missing = [column for column in REQUIRED if column not in columns]
        if missing:
            raise UserError(
                self.env._("Missing columns: %(columns)s", columns=", ".join(missing))
            )
        for number, row in enumerate(rows[1:], start=2):
            yield (
                number,
                {
                    column: row[index] if index < len(row) else ""
                    for column, index in columns.items()
                },
            )

    def action_parse(self):
        """Read the file and show every error before anything is imported."""
        self.ensure_one()
        self.line_ids.unlink()
        values = [self._parse_row(number, row) for number, row in self._read_rows()]
        self._check_codes(values)
        self.write({"line_ids": [fields.Command.create(line) for line in values]})
        self.state = "preview"
        self._try_import()
        return self._reopen()

    def _check_codes(self, values):
        """Inventory numbers are unique per business (in the file and the
        register), reported per row instead of a database error."""
        codes = [row.get("code") for row in values if row.get("code")]
        existing = set(
            self.env["l10n_de_euer.asset"]
            .sudo()
            .search(
                [
                    (
                        "company_id",
                        "=",
                        self.company_id._l10n_de_euer_business_root().id,
                    ),
                    ("code", "in", codes),
                ]
            )
            .mapped("code")
        )
        for row in values:
            code = row.get("code")
            if not code:
                continue
            error = None
            if codes.count(code) > 1:
                error = self.env._(
                    "The inventory number %(code)s appears more than once.",
                    code=code,
                )
            elif code in existing:
                error = self.env._(
                    "The inventory number %(code)s is already in the register.",
                    code=code,
                )
            if error:
                row["error"] = "\n".join(filter(None, [row.get("error"), error]))

    def _try_import(self):
        """Import every row without errors on trial and roll it back, so the
        preview also shows the errors of the register (limits, rates,
        depreciation rules)."""
        errors = {}
        for line in self.line_ids.filtered(lambda line: not line.error):
            try:
                with self.env.cr.savepoint():
                    line._create_asset()
                    raise _TrialDone
            except _TrialDone:
                continue
            except (UserError, ValidationError) as error:
                errors[line.id] = error.args[0] if error.args else str(error)
            except (IntegrityError, DataError) as error:
                errors[line.id] = str(error.pgerror or error).strip()
        for line in self.line_ids.browse(list(errors)):
            line.error = errors[line.id]

    def _parse_row(self, number, row):
        values = {"sequence": number, "name": (row.get("name") or "").strip()}
        errors = []
        parsers = {
            **dict.fromkeys(SELECTIONS, "choice"),
            **dict.fromkeys(AMOUNTS, "amount"),
            "delivery_date": "date",
            **dict.fromkeys(INTEGERS, "int"),
            "degressive_rate": "rate",
            "code": "text",
        }
        for column, kind in parsers.items():
            text = row.get(column) or ""
            try:
                values[column] = self._parse_value(kind, column, text)
            except ValueError:
                errors.append(
                    self.env._(
                        "Invalid %(column)s: %(value)s", column=column, value=text
                    )
                )
        if not values["name"]:
            errors.append(self.env._("The name is missing."))
        try:
            errors += self._plausibility(values)
        except ValueError as error:
            errors.append(self.env._("Invalid row: %(error)s", error=error))
        values["error"] = "\n".join(str(error) for error in errors) or False
        values["warning"] = self._warning(values)
        return {key: value for key, value in values.items() if value is not None}

    def _warning(self, values):
        """Hints that do not prevent the import."""
        hints = []
        if values.get("method") == "degressive" and not values.get(
            "switch_to_linear_year"
        ):
            hints.append(
                self.env._(
                    "Degressive without a year of the switch to linear: the "
                    "register continues degressively. If the previous system "
                    "already depreciates linearly, enter the year of the switch; "
                    "a return to degressive depreciation is not allowed (§ 7 (3) "
                    "sentence 3 EStG)."
                )
            )
        expected = self._linear_book_value(values)
        if (
            expected is not None
            and not values.get("sec7g_special")
            and to_decimal(values.get("book_value")) < expected - LINEAR_TOLERANCE
        ):
            hints.append(
                self.env._(
                    "The book value is below the linear depreciation (%(expected).2f). "
                    "If the previous system took special depreciation under § 7g "
                    "EStG, enter it in the column Sonder-AfA § 7g; after an "
                    "extraordinary depreciation record it as an event before the "
                    "takeover date. Otherwise the depreciation after the takeover "
                    "is not spread over the remaining useful life (§ 7a (9), "
                    "§ 7 (1) sentence 7 EStG).",
                    expected=expected,
                )
            )
        return "\n".join(hints) or False

    def _linear_book_value(self, values):
        """Book value at the takeover date with linear depreciation alone, or
        ``None`` if the row is not depreciated linearly."""
        if (
            values.get("asset_kind") != "depreciable"
            or (values.get("method") or "linear") != "linear"
            or not values.get("delivery_date")
            or values["delivery_date"] > self.opening_date
            or (values.get("useful_life_months") or 0) <= dep.SHORT_LIFE_MONTHS
        ):
            return None
        try:
            rows = dep.schedule(
                dep.Asset(
                    kind="depreciable",
                    method="linear",
                    delivery_date=values["delivery_date"],
                    cost=to_decimal(values.get("cost")),
                    useful_life_months=values["useful_life_months"],
                    group=values.get("aveuer_group") or "other_movable",
                ),
                until=self.opening_date.year,
            )
        except dep.DepreciationError:
            return None
        return rows[-1].book_value_end if rows else None

    def _parse_value(self, kind, column, text):
        if kind == "choice":
            return parse_choice(column, text)
        if kind == "amount":
            amount = parse_amount(text)
            return None if amount is None else float(amount)
        if kind == "date":
            return parse_date(text)
        if kind == "int":
            if not text.strip():
                return None
            number = int(text.strip())
            low, high = INTEGERS[column]
            if not low <= number <= high:
                raise ValueError(text)
            return number
        if kind == "rate":
            rate = parse_rate(text)
            return None if rate is None else float(rate)
        return text.strip() or None

    def _plausibility(self, values):
        """Book value = cost − accumulated depreciation (spec 6.6)."""
        errors = []
        cost = to_decimal(values.get("cost"))
        if values.get("asset_kind") == "pool":
            year = values.get("pool_year") or (
                values.get("delivery_date") and values["delivery_date"].year
            )
            if not year:
                return [self.env._("The year of the asset pool is missing.")]
            values["pool_year"] = year
            if not values.get("delivery_date"):
                values["delivery_date"] = datetime.date(year, 12, 31)
            elif values["delivery_date"].year != year:
                # the pool is the one of the year of delivery (§ 6 (2a) EStG)
                return [
                    self.env._(
                        "The delivery date %(date)s is not in the year of the "
                        "asset pool %(year)s.",
                        date=values["delivery_date"],
                        year=year,
                    )
                ]
            remaining = to_decimal(values.get("pool_remaining"))
            expected = dep.pool_year(dep.Pool(year, cost), self.opening_date.year + 1)
            expected = expected[0] if expected else dep.ZERO
            if remaining != expected:
                errors.append(
                    self.env._(
                        "Remaining pool %(remaining).2f differs from the releases "
                        "of the pool (%(expected).2f).",
                        remaining=remaining,
                        expected=expected,
                    )
                )
            return errors
        if not values.get("delivery_date"):
            return [self.env._("The delivery date is missing.")]
        if values["delivery_date"] > self.opening_date:
            errors.append(self.env._("Delivered after the takeover date."))
        book_value = to_decimal(values.get("book_value"))
        accumulated = to_decimal(values.get("accumulated"))
        if cost - accumulated != book_value:
            errors.append(
                self.env._(
                    "Book value %(book).2f is not cost %(cost).2f less accumulated "
                    "depreciation %(acc).2f.",
                    book=book_value,
                    cost=cost,
                    acc=accumulated,
                )
            )
        memo = to_decimal(values.get("memo_value"))
        if memo and memo > book_value:
            errors.append(self.env._("The memo value exceeds the book value."))
        errors += self._check_sec7g(values)
        return errors

    def _check_sec7g(self, values):
        """Special depreciation under § 7g taken by the previous system: year
        of delivery up to four years later, not after the takeover date."""
        if not values.get("sec7g_special"):
            if values.get("sec7g_year"):
                return [
                    self.env._("The amount of the special depreciation is missing.")
                ]
            return []
        delivery = values["delivery_date"].year
        year = values.get("sec7g_year") or delivery
        values["sec7g_year"] = year
        if not delivery <= year <= min(delivery + 4, self.opening_date.year):
            return [
                self.env._(
                    "Special depreciation under § 7g in %(year)s: only in the year "
                    "of delivery and the four following years up to the takeover "
                    "date.",
                    year=year,
                )
            ]
        return []

    # ------------------------------------------------------------ import
    def action_import(self):
        self.ensure_one()
        if self.state != "preview" or not self.line_ids:
            raise UserError(self.env._("Read the file first."))
        if self.error_count:
            raise UserError(
                self.env._(
                    "Correct the %(count)s rows with errors.", count=self.error_count
                )
            )
        assets = self.env["l10n_de_euer.asset"]
        for line in self.line_ids:
            try:
                with self.env.cr.savepoint():
                    asset = line._create_asset()
            except (UserError, ValidationError, IntegrityError, DataError) as error:
                raise UserError(
                    self.env._(
                        "Row %(row)s (%(name)s): %(error)s",
                        row=line.sequence,
                        name=line.name,
                        error=error.args[0] if error.args else error,
                    )
                ) from error
            line.asset_id = asset
            assets |= asset
        self._store_totals()
        self.state = "done"
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_asset"
        )
        action["domain"] = [("id", "in", assets.ids)]
        return action

    def _store_totals(self):
        """Keep the filed book values per group for the checks (C-61)."""
        Total = self.env["l10n_de_euer.asset.opening.total"]
        company = self.company_id._l10n_de_euer_business_root()
        year = self.opening_date.year
        for total in self.total_ids:
            existing = Total.search(
                [
                    ("company_id", "=", company.id),
                    ("year", "=", year),
                    ("aveuer_group", "=", total.aveuer_group),
                ]
            )
            values = {"book_value_end": total.book_value_end}
            if existing:
                existing.write(values)
            else:
                Total.create(
                    {
                        "company_id": company.id,
                        "year": year,
                        "aveuer_group": total.aveuer_group,
                        **values,
                    }
                )


class _TrialDone(Exception):
    """Ends a trial import inside a savepoint (rolled back)."""


class L10nDeEuerAssetImportTotal(models.TransientModel):
    _name = "l10n_de_euer.asset.import.total"
    _description = "EÜR Asset Register Import Filed AVEÜR"

    wizard_id = fields.Many2one(
        "l10n_de_euer.asset.import", required=True, ondelete="cascade"
    )
    currency_id = fields.Many2one(related="wizard_id.company_id.currency_id")
    aveuer_group = fields.Selection(AVEUER_GROUPS, string="AVEÜR Group", required=True)
    book_value_end = fields.Monetary(string="Book Value at End")


class L10nDeEuerAssetImportLine(models.TransientModel):
    _name = "l10n_de_euer.asset.import.line"
    _description = "EÜR Asset Register Import Row"
    _order = "sequence"

    wizard_id = fields.Many2one(
        "l10n_de_euer.asset.import", required=True, ondelete="cascade"
    )
    currency_id = fields.Many2one(related="wizard_id.company_id.currency_id")
    sequence = fields.Integer(string="Row")
    name = fields.Char()
    code = fields.Char()
    aveuer_group = fields.Selection(AVEUER_GROUPS, string="AVEÜR Group")
    asset_kind = fields.Selection(ASSET_KINDS, string="Kind")
    delivery_date = fields.Date()
    cost = fields.Monetary()
    cost_net = fields.Monetary(string="Net Cost")
    vat_treatment = fields.Selection(VAT_TREATMENTS, string="Input Tax Treatment")
    useful_life_months = fields.Integer(string="Useful Life (Months)")
    method = fields.Selection(METHODS)
    degressive_rate = fields.Float(digits=(16, 6))
    switch_to_linear_year = fields.Integer(string="Switch to Linear in")
    accumulated = fields.Monetary(string="Accumulated Depreciation")
    book_value = fields.Monetary()
    memo_value = fields.Monetary()
    pool_year = fields.Integer()
    pool_remaining = fields.Monetary()
    sec7g_special = fields.Monetary(
        string="Special Depreciation § 7g",
        help="Special depreciation under § 7g (5) EStG the previous system took "
        "(total). It becomes an event before the takeover date, so the "
        "depreciation after the period of benefit follows the book value and "
        "the remaining useful life (§ 7a (9) EStG).",
    )
    sec7g_year = fields.Integer(string="Year of the Special Depreciation")
    error = fields.Text()
    warning = fields.Text(string="Note")
    asset_id = fields.Many2one("l10n_de_euer.asset", readonly=True)

    def _create_asset(self):
        """Create the asset of the row and enter it in the register."""
        self.ensure_one()
        asset = self.env["l10n_de_euer.asset"].create(self._asset_values())
        if self.sec7g_special:
            self.env["l10n_de_euer.asset.event"].create(
                {
                    "asset_id": asset.id,
                    "type": "sec7g_special",
                    "date": datetime.date(self.sec7g_year, 12, 31),
                    "amount": self.sec7g_special,
                    "reason": self.env._("Taken over from the previous system."),
                    "sec7g_profit_confirmed": True,
                }
            )
        asset.action_activate()
        return asset

    def _asset_values(self):
        self.ensure_one()
        wizard = self.wizard_id
        cost = to_decimal(self.cost)
        net = to_decimal(self.cost_net) if self.cost_net else cost
        vat_treatment = self.vat_treatment or "deductible"
        tax = cost - net if vat_treatment == "not_deductible" else dep.ZERO
        kind = self.asset_kind
        pool = kind == "pool"
        return {
            "name": self.name,
            "code": self.code or False,
            "company_id": wizard.company_id._l10n_de_euer_business_root().id,
            "asset_kind": kind,
            "aveuer_group": self.aveuer_group,
            "delivery_date": self.delivery_date,
            "origin": "legacy_import",
            "cost_net": float(net if tax else cost),
            "input_tax": float(tax),
            "vat_treatment": vat_treatment if tax else "deductible",
            "method": (self.method or "linear") if kind == "depreciable" else "none",
            "useful_life_months": self.useful_life_months,
            "degressive_rate": self.degressive_rate,
            "switch_to_linear_year": self.switch_to_linear_year,
            "memo_value": self.memo_value,
            "opening_date": wizard.opening_date,
            "opening_book_value": self.pool_remaining if pool else self.book_value,
            "opening_accumulated": self.cost - self.pool_remaining
            if pool
            else self.accumulated,
        }
