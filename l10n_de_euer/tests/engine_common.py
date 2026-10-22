# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Builder for synthetic engine scenarios (no database).

Categories, mappings, rule parameters and form formulas come from the data
files of the module, read with :mod:`xml.etree`, so the engine tests use the
same data as an installation. Accounts follow SKR03 numbers; amounts are
written in euro and stored in cent. All cases are synthetic (decision D6).
"""

import ast
import dataclasses
import datetime
import functools
import os
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from decimal import Decimal
from fractions import Fraction

from odoo.tests import BaseCase

from ..models.engine import engine as engine_module
from ..models.engine import model as m
from ..models.engine import statement

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
YEARS = (2024, 2025, 2026)

d = datetime.date


def cents(value):
    return int((Decimal(str(value)) * 100).to_integral_value())


def _records(path, model_name):
    tree = ET.parse(os.path.join(MODULE, path))
    for record in tree.getroot().iter("record"):
        if record.get("model") != model_name:
            continue
        values = {}
        for node in record.findall("field"):
            name = node.get("name")
            if node.get("ref"):
                values[name] = node.get("ref")
            elif node.get("eval") is not None:
                values[name] = ast.literal_eval(node.get("eval"))
            else:
                values[name] = node.text
        yield record.get("id"), values


def _function_writes(path, model_name):
    """``(xml id, values)`` of ``<function name="write">`` calls of a file."""
    tree = ET.parse(os.path.join(MODULE, path))
    for node in tree.getroot().iter("function"):
        if node.get("model") != model_name or node.get("name") != "write":
            continue
        ids, values = (child.get("eval") for child in node.findall("value"))
        for xmlid in re.findall(r"ref\('([^']+)'\)", ids):
            yield xmlid, ast.literal_eval(values)


def _bool(value):
    return value in (True, "True", "1")


@functools.cache
def module_data():
    """Categories, mappings per year, parameters and form specs of the module."""
    categories = []
    for _xmlid, values in _records("data/categories.xml", "l10n_de_euer.category"):
        categories.append(
            m.Category(
                code=values["code"],
                role=values["role"],
                be_art=values.get("be_art"),
                erfassung=values.get("erfassung") or "Z",
                writeoff=_bool(values.get("writeoff")),
                on_payment=_bool(values.get("on_payment")),
                vat_kind=values.get("vat_kind"),
                separate_record=values.get("separate_record"),
                restricted_column=values.get("restricted_column"),
                recurring_candidate=_bool(values.get("recurring_candidate")),
                gift_bulk_allowed=_bool(values.get("gift_bulk_allowed")),
                interim=_bool(values.get("interim")),
                scope_status=values.get("scope_status") or "supported",
            )
        )
    category_codes = {
        xmlid: values["code"]
        for xmlid, values in _records("data/categories.xml", "l10n_de_euer.category")
    }
    mappings, specs = {}, {}
    for year in YEARS:
        codes, spec = _form_year(year)
        specs[year] = spec
        lines = []
        for _xmlid, values in _records(
            f"data/category_mapping_{year}.xml", "l10n_de_euer.category.mapping"
        ):
            lines.append(
                m.MappingLine(
                    category=category_codes[values["category_id"]],
                    field=codes[values["field_id"]],
                    share=Fraction(str(values.get("share") or 1)),
                    condition=values.get("condition") or "always",
                    option=values.get("option"),
                    davon_field=codes.get(values.get("davon_field_id")),
                    additional_field=codes.get(values.get("additional_field_id")),
                    sequence=int(values.get("sequence") or 10),
                )
            )
        mappings[year] = tuple(lines)
    return tuple(categories), mappings, _parameters(), specs


def _form_year(year):
    codes, formulas, conditions = {}, {}, {}
    allow_negative, blocked, mandatory = set(), set(), set()
    form_conditions = {}
    path = f"data/forms/euer_{year}.xml"
    for xmlid, values in (
        *_records(path, "l10n_de_euer.form"),
        *_function_writes(path, "l10n_de_euer.form"),
    ):
        if values.get("fill_condition"):
            form_conditions[xmlid] = values["fill_condition"]
    for xmlid, values in _records(f"data/forms/euer_{year}.xml", "l10n_de_euer.field"):
        code = values["code"]
        codes[xmlid] = code
        if values.get("formula"):
            formulas[code] = values["formula"]
        if _bool(values.get("allow_negative")):
            allow_negative.add(code)
        if values.get("scope_status") == "blocked":
            blocked.add(code)
        if _bool(values.get("mandatory")):
            mandatory.add(code)
        if values.get("form_id") in form_conditions:
            conditions[code] = form_conditions[values["form_id"]]
    spec = statement.FormSpec(
        formulas=formulas,
        conditions=conditions,
        allow_negative=frozenset(allow_negative),
        known=frozenset(codes.values()),
        blocked=frozenset(blocked),
        mandatory=frozenset(mandatory),
    )
    return codes, spec


def _parameters():
    values = []
    for _xmlid, record in _records("data/parameters.xml", "l10n_de_euer.parameter"):
        value = record.get("value_json")
        if value is None:
            value = Decimal(record.get("value") or "0")
        values.append(
            m.ParameterValue(
                code=record["code"],
                value=value,
                date_from=_date(record.get("date_from")),
                date_to=_date(record.get("date_to")),
            )
        )
    return tuple(values)


def _date(text):
    return datetime.date.fromisoformat(text) if text else None


# --------------------------------------------------------------------- chart
#: code: (account_type, internal_group, reconcile, category, flags)
CHART = {
    "1000": ("asset_cash", "asset", False, "CASH", {"liquidity"}),
    "1200": ("asset_cash", "asset", False, "CASH", {"liquidity"}),
    "1210": ("asset_cash", "asset", False, "CASH", {"liquidity"}),
    "1730": ("liability_credit_card", "liability", False, "CASH", {"liquidity"}),
    "1260": ("asset_current", "asset", True, "TRANSIT", set()),
    "1360": ("asset_current", "asset", True, "TRANSIT", set()),
    "OR": ("asset_current", "asset", True, "TRANSIT", set()),
    "OP": ("asset_current", "asset", True, "TRANSIT", set()),
    "SUSP": ("asset_current", "asset", False, "SUSPENSE", {"suspense"}),
    "SUSPR": ("asset_current", "asset", True, "SUSPENSE", {"suspense"}),
    "1410": ("asset_receivable", "asset", True, "OPEN_ITEM", set()),
    "1610": ("liability_payable", "liability", True, "OPEN_ITEM", set()),
    "1780": ("liability_payable", "liability", True, "VAT_SETTLEMENT", set()),
    "1545": ("asset_receivable", "asset", True, "VAT_SETTLEMENT", set()),
    "1789": ("liability_current", "liability", False, "VAT_SETTLEMENT", set()),
    "1776": ("liability_current", "liability", False, "VAT_OUTPUT", set()),
    "1771": ("liability_current", "liability", False, "VAT_OUTPUT", set()),
    "1576": ("asset_current", "asset", False, "VAT_INPUT", set()),
    "1571": ("asset_current", "asset", False, "VAT_INPUT", set()),
    "1566": ("asset_current", "asset", False, "VAT_INPUT_NON_DEDUCTIBLE", set()),
    "1577": ("asset_current", "asset", False, "VAT_REVERSE_CHARGE", set()),
    "1787": ("liability_current", "liability", False, "VAT_REVERSE_CHARGE", set()),
    "1588": ("asset_current", "asset", False, "VAT_IMPORT", set()),
    "1800": ("liability_current", "liability", False, "PRIVATE_WITHDRAWAL", set()),
    "1890": ("liability_current", "liability", False, "PRIVATE_CONTRIBUTION", set()),
    "0800": ("equity", "equity", False, "NEUTRAL", set()),
    "0630": ("liability_non_current", "liability", False, "NEUTRAL", set()),
    "1590": ("asset_current", "asset", False, "NEUTRAL", set()),
    "1700": ("liability_current", "liability", False, "NEUTRAL", set()),
    "1710": ("liability_current", "liability", False, "REV_ADVANCE_RECEIVED", set()),
    "1740": ("liability_current", "liability", False, "EXP_PAYROLL_LIABILITY", set()),
    "0490": ("asset_fixed", "asset", False, "FIXED_ASSET", set()),
    "0970": ("liability_current", "liability", False, "WARN_PROVISION", set()),
    "2400": ("expense", "expense", False, "WRITEOFF", set()),
    "2150": ("expense", "expense", False, "EXP_OTHER", set()),
    "2660": ("income", "income", False, "REV_EXEMPT", set()),
    "2650": ("income", "income", False, "REV_INTEREST", set()),
    "2750": ("income", "income", False, "REV_ANCILLARY_REFUND", set()),
    "PP": ("asset_cash", "asset", False, "CASH", {"liquidity"}),
    "PPC": ("asset_current", "asset", True, "TRANSIT", set()),
    "8195": ("income", "income", False, "REV_KU", set()),
    "8400": ("income", "income", False, "REV_TAX", set()),
    "8300": ("income", "income", False, "REV_TAX", set()),
    "8736": ("income", "income", False, "REV_TAX", set()),
    "8730": ("income", "income", False, "REV_TAX", set()),
    "3730": ("expense", "expense", False, "EXP_GOODS", set()),
    "3736": ("expense", "expense", False, "EXP_GOODS", set()),
    "8100": ("income", "income", False, "REV_EXEMPT", set()),
    "8338": ("income", "income", False, "REV_NOT_TAXABLE", set()),
    "8800": ("income", "income", False, "REV_ASSET_DISPOSAL", set()),
    "8590": ("income", "income", False, "REV_TAX_NONCASH", set()),
    "8910": ("income", "income", False, "REV_WITHDRAWAL_OTHER", set()),
    "8921": ("income", "income", False, "REV_PRIVATE_CAR", set()),
    "4930": ("expense", "expense", False, "EXP_WORK_EQUIPMENT", set()),
    "4970": ("expense", "expense", False, "EXP_OTHER", set()),
    "4965": ("expense", "expense", False, "EXP_LEASING", set()),
    "4210": ("expense", "expense", False, "EXP_RENT", set()),
    "4120": ("expense", "expense", False, "EXP_PERSONNEL", set()),
    "4925": ("expense", "expense", False, "EXP_SERVICES", set()),
    "4950": ("expense", "expense", False, "EXP_ADVICE", set()),
    "4630": ("expense", "expense", False, "EXP_GIFTS", set()),
    "4635": ("expense", "expense", False, "EXP_GIFTS_NON_DEDUCTIBLE", set()),
    "4650": ("expense", "expense", False, "EXP_ENTERTAINMENT", set()),
    "4654": (
        "expense",
        "expense",
        False,
        "EXP_ENTERTAINMENT_NON_DEDUCTIBLE",
        set(),
    ),
    "4590": ("expense", "expense", False, "EXP_CAR_PRIVATE_USE", set()),
    "4830": ("expense_depreciation", "expense", False, "DEPR_MOVABLE", set()),
    "4855": ("expense_depreciation", "expense", False, "DEPR_LOW_VALUE", set()),
    "2310": ("expense", "expense", False, "DEPR_BOOK_VALUE", set()),
    "4320": ("expense", "expense", False, "EXP_NON_DEDUCTIBLE", set()),
    "2100": ("expense", "expense", False, "EXP_INTEREST_OTHER", set()),
    "4396": ("expense", "expense", False, "EXP_OTHER", set()),
    "3200": ("expense", "expense", False, "EXP_GOODS", set()),
    "9000": ("equity", "equity", False, "OPENING", set()),
    "9008": ("equity", "equity", False, "OPENING", set()),
    "9009": ("equity", "equity", False, "OPENING", set()),
}

#: name: (type_tax_use, rate, factors, ku_tax)
TAXES = {
    "ust19": ("sale", 19, (("invoice", 100), ("refund", 100)), False),
    "ust7": ("sale", 7, (("invoice", 100), ("refund", 100)), False),
    "vst19": ("purchase", 19, (("invoice", 100), ("refund", 100)), False),
    "vst7": ("purchase", 7, (("invoice", 100), ("refund", 100)), False),
    "rc19": ("purchase", 19, (("invoice", 100), ("invoice", -100)), False),
    "eust": ("purchase", 19, (("invoice", 100), ("invoice", -100)), False),
    "ku0": ("sale", 0, (("invoice", 100),), True),
}
#: tags of the base lines per tax
TAX_TAGS = {"ust19": ("81_BASE",), "ust7": ("86_BASE",), "ku0": ("87_BASE",)}


class Book:
    """Synthetic books of one business (SKR03 numbers)."""

    def __init__(self, regime=m.KU, **options):
        self.categories, self.mappings, self.parameters, self.specs = module_data()
        self.accounts = {}
        for index, (code, spec) in enumerate(sorted(CHART.items()), start=1):
            account_type, group, reconcile, category, flags = spec
            self.accounts[code] = m.Account(
                id=index,
                code=code,
                account_type=account_type,
                internal_group=group,
                reconcile=reconcile,
                liquidity="liquidity" in flags,
                suspense="suspense" in flags,
                assignments=(m.Assignment(category=category),),
            )
        self.taxes, self.repartitions, self.rep_ids = {}, [], {}
        for index, (name, spec) in enumerate(sorted(TAXES.items()), start=1):
            type_tax_use, rate, factors, ku_tax = spec
            self.taxes[name] = m.Tax(
                id=index,
                type_tax_use=type_tax_use,
                amount=Fraction(rate),
                factors=tuple((doc, Fraction(f)) for doc, f in factors),
                ku_tax=ku_tax,
            )
        self.moves, self.lines, self.partials = [], [], []
        self.partners = {}
        self.vat_periods = [m.VatPeriod(date_from=d(2000, 1, 1), regime=regime)]
        self.opening_items, self.classifications, self.decisions = [], [], []
        self.gift_lines = []
        self.confirmations = set()
        self.asset_amounts, self.adjustment_amounts = [], []
        self.turnover_amounts = []
        self.options = options
        self.founding_year = None
        self.activity_start = None
        self._line_id = 0
        self._rep_id = 0
        self._dates = {}

    # ------------------------------------------------------------ master data
    def partner(self, name, **flags):
        if name not in self.partners:
            self.partners[name] = m.Partner(id=len(self.partners) + 1, **flags)
        return self.partners[name].id

    def account(self, code, **changes):
        """Change an account of the chart (e.g. reconcile, assignments)."""
        self.accounts[code] = dataclasses.replace(self.accounts[code], **changes)
        return self.accounts[code]

    def repartition(self, tax, negative=False, **values):
        key = (tax, negative, tuple(sorted(values.items())))
        if key not in self.rep_ids:
            self._rep_id += 1
            self.repartitions.append(
                m.Repartition(
                    id=self._rep_id,
                    tax_id=self.taxes[tax].id,
                    factor_percent=Fraction(-100 if negative else 100),
                    **values,
                )
            )
            self.rep_ids[key] = self._rep_id
        return self.rep_ids[key]

    def set_regimes(self, *periods):
        self.vat_periods = list(periods)
        return self

    def previous_turnover(self, year, cent=0):
        """Enter the turnover of the year before ``year`` on the first
        period: a test without bookings of earlier years and without this
        input gets the warning C-32 'turnover of the previous year
        unknown'."""
        self.vat_periods[0] = dataclasses.replace(
            self.vat_periods[0], prev_year_turnover=cent, prev_year_turnover_year=year
        )
        return self

    # ------------------------------------------------------------ entries
    def move(self, name, date, *lines, **values):
        """Create a posted entry; lines are ``(code, euro)`` or :func:`ln`."""
        move_id = len(self.moves) + 1
        info = values.pop("info", None)
        partner = values.pop("partner", None)
        if partner:
            values["partner_id"] = self.partner(partner)
        if info:
            values["info"] = m.MoveInfo(**info)
        created = []
        for spec in lines:
            if not isinstance(spec, dict):
                spec = ln(*spec)
            created.append(self._line(move_id, spec, values.get("partner_id")))
        assert sum(line.balance for line in created) == 0, f"{name} unbalanced"
        move = m.Move(id=move_id, name=name, date=date, **values)
        self.moves.append(move)
        self._dates[move_id] = date
        self.lines += created
        return Entry(self, move, created)

    def _line(self, move_id, spec, move_partner):
        self._line_id += 1
        tax = spec.get("tax")
        tax_names = (tax,) if isinstance(tax, str) else tuple(tax or ())
        rep = spec.get("rep")
        repartition_id = None
        if rep:
            negative = rep.endswith("-")
            repartition_id = self.repartition(
                rep.rstrip("-"), negative, **spec.get("rep_values", {})
            )
        partner = spec.get("partner")
        tags = spec.get("tags")
        if tags is None:
            tags = tuple(tag for name in tax_names for tag in TAX_TAGS.get(name, ()))
        return m.Line(
            id=self._line_id,
            move_id=move_id,
            account_id=self.accounts[spec["code"]].id,
            balance=cents(spec["amount"]),
            partner_id=self.partner(partner) if partner else move_partner,
            repartition_id=repartition_id,
            tax_ids=tuple(self.taxes[name].id for name in tax_names),
            tag_names=tuple(tags),
            currency_id=spec.get("currency_id"),
            residual_currency_zero=spec.get("residual_currency_zero", False),
            supply_date=spec.get("supply_date"),
            unit_price=cents(spec["unit_price"]) if spec.get("unit_price") else None,
        )

    def rec(self, line_a, line_b, amount=None, exchange=None, created=None):
        """Reconcile two lines (partial); ``created``: day of the
        reconciliation, if it matters (C-60)."""
        debit, credit = (line_a, line_b) if line_a.balance > 0 else (line_b, line_a)
        assert debit.balance > 0 > credit.balance
        value = (
            cents(amount) if amount is not None else min(debit.balance, -credit.balance)
        )
        dates = [self._dates[debit.move_id], self._dates[credit.move_id]]
        partial = m.Partial(
            id=len(self.partials) + 1,
            debit_line_id=debit.id,
            credit_line_id=credit.id,
            amount=value,
            max_date=max(dates),
            exchange_move_id=exchange.move.id if exchange else None,
            created=created,
        )
        self.partials.append(partial)
        return partial

    def opening(self, line, category, regime=m.KU, rate=0):
        self.opening_items.append(
            m.OpeningItem(
                line_id=line.id,
                category=category,
                vat_regime=regime,
                tax_rate=Fraction(rate),
            )
        )

    def classify(self, line, kind, category=None, rate=0, regime=None):
        self.classifications.append(
            m.Classification(
                line_id=line.id,
                kind=kind,
                category=category,
                tax_rate=Fraction(rate),
                vat_regime=regime,
            )
        )

    def gift_line(self, line, recipient, share=1, gift_date=None):
        """Recipient of the gifts of one document line."""
        self.gift_lines.append(
            m.GiftLine(
                line_id=line.id,
                recipient_id=self.partner(recipient),
                share=Fraction(share),
                gift_date=gift_date,
            )
        )

    def decide(self, source, origin, decision="apply", **values):
        self.decisions.append(
            m.TenDayDecision(
                source_move_id=source.move.id,
                origin_move_id=origin.move.id,
                decision=decision,
                **values,
            )
        )

    # ------------------------------------------------------------ run
    def data(self):
        settings = m.Settings(**self.options)
        return m.EngineInput(
            accounts=tuple(self.accounts.values()),
            categories=self.categories,
            mappings=self.mappings,
            moves=tuple(self.moves),
            lines=tuple(self.lines),
            partials=tuple(self.partials),
            taxes=tuple(self.taxes.values()),
            repartitions=tuple(self.repartitions),
            partners=tuple(self.partners.values()),
            vat_periods=tuple(self.vat_periods),
            parameters=self.parameters,
            opening_items=tuple(self.opening_items),
            classifications=tuple(self.classifications),
            ten_day_decisions=tuple(self.decisions),
            gift_lines=tuple(self.gift_lines),
            transfer_confirmations=frozenset(self.confirmations),
            asset_amounts=tuple(self.asset_amounts),
            adjustment_amounts=tuple(self.adjustment_amounts),
            turnover_amounts=tuple(self.turnover_amounts),
            settings=settings,
            founding_year=self.founding_year,
            activity_start=self.activity_start,
        )

    def run(self, year):
        return engine_module.run(self.data(), year)


class Entry:
    """Entry of a :class:`Book` with access to its lines by account code."""

    def __init__(self, book, move, lines):
        self.book = book
        self.move = move
        self.lines = lines

    def __call__(self, code, index=0):
        account_id = self.book.accounts[code].id
        return [line for line in self.lines if line.account_id == account_id][index]


class EngineCase(BaseCase):
    """Base class of engine tests: invariants are checked on every run."""

    def assertClean(self, result):
        """No algorithm error: invariants I-1 to I-3 hold (no C-11)."""
        self.assertEqual(
            result.invariants, {"money": {}, "documents": {}, "transit": {}}
        )
        self.assertNotIn("C-11", codes(result))
        # the contributions explain the totals of the statement year
        self.assertEqual(result.totals_for(result.year), result.totals)

    def run_year(self, book, year):
        result = book.run(year)
        self.assertClean(result)
        return result

    def sale(self, book, name, day, amount, account="8195", **values):
        """Customer invoice of a small business (gross on 8195)."""
        return book.move(
            name,
            day,
            ("1410", amount),
            (account, -amount),
            move_type="out_invoice",
            **values,
        )

    def receipt(self, book, name, day, amount, invoice=None, **values):
        bank = book.move(name, day, ("1200", amount), ("1410", -amount), **values)
        if invoice is not None:
            book.rec(invoice("1410"), bank("1410"))
        return bank

    def regular_sale(self, book, day, net, tax=19, **values):
        """Invoice under standard taxation with tax line."""
        vat = round(net * tax) / 100
        name = "ust19" if tax == 19 else "ust7"
        account = "8400" if tax == 19 else "8300"
        tax_account = "1776" if tax == 19 else "1771"
        return book.move(
            "RE",
            day,
            ln("1410", net + vat),
            ln(account, -net, tax=name),
            ln(tax_account, -vat, rep=name),
            move_type="out_invoice",
            **values,
        )


def ln(code, amount, **values):
    """Line specification: account code, amount in euro, options."""
    return {"code": code, "amount": amount, **values}


def euro(value):
    """Cent as Decimal euro for readable assertions."""
    return Decimal(value) / 100


def fields(result, year=None):
    """Field totals of a year in euro (``Decimal``), zero fields left out."""
    year = year or result.year
    totals = result.totals if year == result.year else result.totals_for(year)
    return {code: euro(value) for code, value in sorted(totals.items()) if value}


def codes(result, severity=None):
    """Sorted codes of the findings (optionally of one severity)."""
    return sorted(
        {f.code for f in result.findings if severity is None or f.severity == severity}
    )


def by_account(result, book, year):
    """Realised slices per account code and year (cent)."""
    names = {account.id: code for code, account in book.accounts.items()}
    totals = defaultdict(int)
    for one in result.slices:
        if one.attribution_year == year and one.kind in m.MONEY_KINDS | {m.BOOKING}:
            totals[names.get(one.account_id, "?")] += one.amount
    return {code: euro(value) for code, value in sorted(totals.items()) if value}
