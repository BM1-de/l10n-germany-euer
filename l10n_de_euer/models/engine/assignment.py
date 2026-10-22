# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Phase E: slices → category → form field (spec 5.7, 4.4, 4.5).

Each slice is valued with the category of its account on the date of the
slice and the mapping of the form year of its year of attribution:

* revenue by its VAT treatment and revenue type (4.4, :mod:`.revenue`),
  including the split of gross receipts with the rate entered per invoice;
* tax lines by their repartition line (output 140, deductible input 185,
  not deductible input VAT follows its base lines, reverse charge neutral);
* VAT payments and refunds per money movement (186/141, never netted);
* expenses by mapping (shares, "of which", additional fields), gifts per
  recipient and year of the gift (164/174), entertainment;
* withdrawals and contributions (29.122/29.123), including private money
  lines and the journal "private account" (R-17, R-18).

Amounts of contributions have the sign of the form: income and expense
positive.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction

from . import model as m
from . import revenue, vat
from .rounding import apportion, gross_split, split_by_shares

FIELD_KU = "EUER.20.111"
FIELD_KU_DAVON = "EUER.20.119"
FIELD_TAXABLE = "EUER.20.112"
FIELD_EXEMPT = "EUER.20.103"
FIELD_OUTPUT_VAT = "EUER.20.140"
FIELD_REFUND = "EUER.20.141"
FIELD_INPUT_VAT = "EUER.25.185"
FIELD_VAT_PAID = "EUER.25.186"
FIELD_OTHER = "EUER.25.183"
FIELD_GIFT = "EUER.25.174"
FIELD_GIFT_NOT_DEDUCTIBLE = "EUER.25.164"
FIELD_ENTERTAINMENT = "EUER.25.175"
FIELD_ENTERTAINMENT_NOT_DEDUCTIBLE = "EUER.25.165"
FIELD_WITHDRAWAL = "EUER.29.122"
FIELD_CONTRIBUTION = "EUER.29.123"
FIELD_PRIVATE_CAR = "EUER.20.106"
FIELD_IN_KIND = "EUER.20.108"
#: "of which" fields: not part of the sums (F-07)
DAVON_FIELDS = frozenset({FIELD_KU_DAVON, "EUER.25.153"})
#: fields of the total turnover (R-27): received revenue. Tax-free and not
#: taxable revenue (Kz 103) does not count.
TURNOVER_FIELDS = frozenset({FIELD_KU, FIELD_TAXABLE, "EUER.20.104"})
#: free supplies (unentgeltliche Wertabgaben) of the books: their value for
#: income tax is no VAT base; see :meth:`Assigner._free_supply`
FREE_SUPPLY_FIELDS = frozenset({FIELD_PRIVATE_CAR, FIELD_IN_KIND})
#: income fields that carry the sign of the form "income positive"
_INCOME_BOX27 = frozenset({"EUER.27.120", "EUER.27.125"})


def mapping_lines(mappings, category_code, year, option=None):
    """Mapping lines of a category for the form year of ``year``.

    Without a form year of its own the latest earlier one is used (F-13);
    lines bound to another company option are left out.
    """
    if not mappings:
        return []
    candidates = [form_year for form_year in mappings if form_year <= year]
    form_year = max(candidates) if candidates else min(mappings)
    return [
        line
        for line in mappings[form_year]
        if line.category == category_code
        and (line.option is None or line.option == option)
    ]


def form_amount(code, balance):
    """Amount of a balance (debit positive) in the sign of the form field."""
    if code.startswith("EUER.20.") or code in _INCOME_BOX27:
        return -balance
    if code == FIELD_CONTRIBUTION:
        return -balance
    return balance


@dataclass
class _Item:
    """A slice (or a part of it) to be valued."""

    index: int
    amount: int
    category: object = None
    kind: str = "mapping"
    regime: str | None = None
    line: object = None
    tax_id: int | None = None
    base: object = None
    links: list = field(default_factory=list)


class Assigner:
    """Phase E over the slices of a run."""

    def __init__(self, graph, slices, params, cash_dates, notes=()):
        self.graph = graph
        self.data = graph.data
        self.slices = slices
        self.params = params
        self.cash_dates = cash_dates
        self.notes = notes
        self.settings = self.data.settings
        self.contributions = []
        self.findings = []
        self.turnover = []
        #: free supplies booked in small business periods that the total
        #: turnover leaves out: ``(date, move id, cent)``
        self.free_supplies_left_out = []
        self.asset_slices = []
        self._settlement = defaultdict(list)
        self._gift_items = []
        self._manual_tax_moves = set()
        self._open_rests = []
        self._gift_lines = defaultdict(list)
        for gift_line in self.data.gift_lines:
            self._gift_lines[gift_line.line_id].append(gift_line)
        self._private_sources = {
            move.id for move in graph.moves_of_class(m.SOURCE) if move.private_journal
        }
        self._option = f"entertainment_{self.settings.entertainment_mode}"
        self._last_event = max((one.event for one in slices), default=0)

    def _next_event(self):
        self._last_event += 1
        return self._last_event

    # ------------------------------------------------------------ helpers
    def mapping_lines(self, category_code, year):
        return mapping_lines(self.data.mappings, category_code, year, self._option)

    def fields_of(self, category, day):
        """All fields a category can reach in the year of ``day`` (C-03)."""
        return {line.field for line in self.mapping_lines(category.code, day.year)}

    def _category(self, code):
        return self.graph.categories.get(code) if code else None

    def _origin(self, one):
        return self.graph.moves.get(one.origin_move_id)

    def _line(self, one):
        if one.key and not one.key[1]:
            return self.graph.lines.get(one.key[0])
        return None

    def _add(self, one_index, code, amount, rule):
        """Contribution of a slice in the year of attribution.

        Withdrawals and contributions happen on their actual day (§ 4 (1)
        S. 2, S. 8 EStG): the ten-day rule moves income and expenses only
        (§ 11 (1) S. 2, (2) S. 2 EStG), so they keep the year of the cash
        date.
        """
        if not amount:
            return
        one = self.slices[one_index]
        year = one.attribution_year
        if code in (FIELD_WITHDRAWAL, FIELD_CONTRIBUTION):
            year = one.date.year
        self.contributions.append(
            m.Contribution(
                field=code,
                amount=amount,
                year=year,
                slice_index=one_index,
                rule=rule,
            )
        )

    def _finding(self, code, severity, key, one, **values):
        self.findings.append(
            m.Finding(
                code=code,
                severity=severity,
                key=key,
                years=(one.attribution_year,),
                move_ids=tuple(
                    sorted({x for x in (one.source_move_id, one.origin_move_id) if x})
                ),
                line_ids=(one.origin_line_id,) if one.origin_line_id else (),
                amount=values.pop("amount", one.amount),
                params=values,
            )
        )

    # ------------------------------------------------------------ run
    def run(self):
        events = defaultdict(list)
        for index, one in enumerate(self.slices):
            events[one.event].append(index)
        for event in sorted(events):
            self._event(events[event])
        self._settle()
        self._gifts()
        self._rest_findings()
        self._private_lines()
        self._private_journal()
        self._private_transfers()
        self._check_booking_categories()
        return self.contributions

    def _event(self, indexes):
        items, tax_items = [], []
        for index in indexes:
            item = self._item(index)
            if item is None:
                continue
            if item.kind in ("tax_input", "tax_base"):
                tax_items.append(item)
            else:
                items.append(item)
        for tax in tax_items:
            self._link_tax(tax, items)
        for item in items + tax_items:
            self._value(item)

    # ------------------------------------------------------------ items
    def _item(self, index):
        """Classify a slice; returns an :class:`_Item` or ``None``."""
        one = self.slices[index]
        if one.kind == m.UNCLEAR:
            self._finding("C-10", m.BLOCKER, "unclear", one)
            return None
        if one.kind == m.CLASSIFIED and not one.category and not one.virtual_tax:
            return None
        origin = self._origin(one)
        if one.virtual_tax:
            return self._virtual_tax_item(index, one)
        category = self._category(one.category)
        if one.repartition_id and not (category and category.role == "privat"):
            return self._tax_item(index, one, origin, category)
        if category is None:
            key = (
                "opening_item_missing" if one.kind == m.KIND_OPENING else "no_category"
            )
            code = "C-35" if one.kind == m.KIND_OPENING else "C-01"
            self._finding(code, m.BLOCKER, key, one)
            return None
        return self._category_item(index, one, origin, category)

    def _virtual_tax_item(self, index, one):
        regime = one.regime or m.REGULAR
        if one.virtual_tax == "sale":
            return _Item(index, one.amount, kind="tax_output", regime=regime)
        kind = "tax_base" if regime == m.KU else "tax_input"
        return _Item(index, one.amount, kind=kind, regime=regime)

    def _tax_item(self, index, one, origin, category):
        repartition = self.graph.repartitions.get(one.repartition_id)
        tax = self.graph.taxes.get(repartition.tax_id) if repartition else None
        if repartition is None:
            return _Item(index, one.amount, kind="tax_input", regime=m.REGULAR)
        if tax is not None and tax.type_tax_use == "sale":
            regime = vat.revenue_regime(
                origin, one.date, self.data.vat_periods, self.settings.default_regime
            )
        else:
            regime = vat.document_regime(
                origin, self.data.vat_periods, self.settings.default_regime
            )
        kind = vat.tax_line_kind(repartition, tax, category, regime)
        if kind == vat.TAX_NEUTRAL:
            return None
        return _Item(
            index,
            one.amount,
            category=category,
            kind=f"tax_{kind}",
            regime=regime,
            line=self._line(one),
            tax_id=repartition.tax_id,
        )

    def _category_item(self, index, one, origin, category):
        role = category.role
        if role in ("av", "afa"):
            self.asset_slices.append(index)
            return _Item(index, one.amount, category, "asset", None, self._line(one))
        if role == "steuer":
            return self._tax_account_item(index, one, origin, category)
        if category.writeoff and one.kind in m.MONEY_KINDS:
            self._writeoff(one)
            return None
        if one.kind == m.UNALLOCATED and role not in ("be", "ba"):
            self._unallocated(one, category)
            return None
        if role == "ungeklaert" and one.kind in m.MONEY_KINDS:
            # money realised on a suspense account (B-05), e.g. a document
            # line on the bank suspense account
            self._finding("C-10", m.BLOCKER, "unclear", one)
            return None
        if (
            role == "privat"
            and one.kind in m.MONEY_KINDS
            and one.source_move_id in self._private_sources
        ):
            # private money pays a private share: it never left the private
            # sphere, the account journal rule counts the rest (R-18)
            return None
        if role not in ("be", "ba", "privat"):
            return None
        regime = (
            vat.revenue_regime(
                origin, one.date, self.data.vat_periods, self.settings.default_regime
            )
            if role == "be"
            else vat.document_regime(
                origin, self.data.vat_periods, self.settings.default_regime
            )
        )
        if one.regime:
            regime = one.regime
        kind = {"be": "revenue", "ba": "expense", "privat": "private"}[role]
        return _Item(
            index,
            one.amount,
            category=category,
            kind=kind,
            regime=regime,
            line=self._line(one),
        )

    def _tax_account_item(self, index, one, origin, category):
        """Tax account line without repartition line (5.7 no. 4 last, no. 5)."""
        line = self._line(one)
        regime = vat.document_regime(
            origin, self.data.vat_periods, self.settings.default_regime
        )
        if category.vat_kind == "import":
            # import VAT paid to customs follows its category: 185, small
            # business 183 (5.7 no. 4, no. 5 does not apply)
            return _Item(index, one.amount, category, "expense", regime, line)
        if (
            category.vat_kind == "settlement"
            or one.kind == m.UNALLOCATED
            or self._only_tax_accounts(origin)
        ):
            self._settlement[self._settlement_key(one, category)].append(index)
            return None
        if category.vat_kind in ("output", "input") and self._has_profit_lines(origin):
            self._manual_tax_moves.add(origin.id)
        if category.vat_kind == "output":
            return _Item(index, one.amount, category, "tax_output", regime, line)
        if category.vat_kind == "input" and regime == m.REGULAR:
            return _Item(index, one.amount, category, "tax_input", regime, line)
        if category.vat_kind in ("input", "input_non_deductible"):
            return _Item(index, one.amount, category, "tax_base", regime, line)
        if category.vat_kind == "reverse_charge":
            return None
        return _Item(index, one.amount, category, "expense", regime, line)

    @staticmethod
    def _settlement_key(one, category):
        """Money movement of a VAT payment or refund (5.7 no. 5, R-20).

        The slices of one document reached by the money form one movement
        (e.g. "1776/1576 an 1780": the balance is the payment). Lines on
        payment or refund accounts booked on the money entry itself are
        movements of their own: a payment and a refund set off by the tax
        office in one bank entry are not netted.
        """
        source = one.source_move_id or one.origin_move_id
        document = one.document_move_id or one.origin_move_id
        if category.vat_kind == "settlement" and one.kind != m.DOC_VIA_PAYMENT:
            return (source, document, one.origin_line_id or 0)
        return (source, document, 0)

    def _only_tax_accounts(self, origin):
        """Entry whose non-reconcilable lines are all tax accounts (AB-04)."""
        lines = self.graph.lines_of(origin.id, m.N)
        if not lines:
            return False
        for line in lines:
            category = self.graph.category(line, origin.date)
            if (
                line.repartition_id
                or category is None
                or category.role != "steuer"
                or category.vat_kind == "import"
            ):
                return False
        return True

    def _has_profit_lines(self, origin):
        for line in self.graph.lines_of(origin.id):
            category = self.graph.category(line, origin.date)
            if category is not None and category.role in ("be", "ba", "av"):
                return True
        return False

    def _unallocated(self, one, category):
        """C-20: rest on a receivable/payable (blocker) or other account.

        A rest on the outstanding account of a payment method is a blocker,
        too: the payment entry it belonged to is missing (reversed after the
        bank reconciliation), so a receipt or payment would be lost. A rest
        of a line reconciled with another business is reported by C-47
        instead.
        """
        if one.origin_line_id in self.graph.foreign_lines:
            return
        account = self.graph.accounts.get(one.account_id)
        if category.role == "op" or (account is not None and account.outstanding):
            self._open_rests.append(one)
        elif category.role not in ("ungeklaert",):
            self._finding("C-20", m.WARNING, "unallocated_other", one)

    def _rest_findings(self):
        """C-20 for rests on receivables and payables (5.7 no. 2).

        Rests of the same partner and account that cancel each other in the
        same year (a payment and its return) do not change the year: they
        are a warning that proposes to reconcile the two payments; every
        other rest is a blocker until it is classified.
        """
        groups = defaultdict(list)
        for one in self._open_rests:
            if one.partner_id:
                groups[(one.partner_id, one.account_id, one.attribution_year)].append(
                    one
                )
        paired = set()
        for rests in groups.values():
            if len(rests) > 1 and not sum(one.amount for one in rests):
                paired.update(id(one) for one in rests)
                continue
            for one in rests:
                if id(one) in paired:
                    continue
                match = next(
                    (
                        other
                        for other in rests
                        if id(other) not in paired
                        and other is not one
                        and other.amount == -one.amount
                    ),
                    None,
                )
                if match is not None:
                    paired.update({id(one), id(match)})
        for one in self._open_rests:
            if id(one) in paired:
                self._finding("C-20", m.WARNING, "unallocated_pair", one)
                continue
            account = self.graph.accounts.get(one.account_id)
            if account is not None and account.outstanding:
                self._finding("C-20", m.BLOCKER, "unallocated_outstanding", one)
                continue
            receivable = account is not None and account.account_type == (
                "asset_receivable"
            )
            # money in on a receivable or out on a payable: paid before the
            # invoice; otherwise money returned (refund, repayment)
            advance = (one.amount < 0) == receivable
            self._finding(
                "C-20",
                m.BLOCKER,
                "unallocated_open_item" if advance else "unallocated_refund",
                one,
            )

    def _writeoff(self, one):
        """C-36: money reaches a write-off (returned direct debit)."""
        proposal = self._writeoff_proposal(one)
        self._finding(
            "C-36",
            m.BLOCKER,
            "money_reaches_writeoff",
            one,
            proposal_move_ids=proposal,
        )

    def _writeoff_proposal(self, one):
        """Documents the returned payment realised (same partner, amount, 60 days)."""
        result = set()
        for other in self.slices:
            if (
                other.kind == m.DOC_VIA_PAYMENT
                and other.partner_id == one.partner_id
                and other.source_move_id != one.source_move_id
                and 0 <= (one.date - other.date).days <= 60
                and self._category(other.category)
                and self._category(other.category).role == "be"
                and abs(other.amount) == abs(one.amount)
            ):
                result.add(other.origin_move_id)
        return tuple(sorted(result))

    # ------------------------------------------------------------ tax links
    def _link_tax(self, tax, items):
        """Base items of the same event that carry the tax of ``tax``.

        A tax line without tax (manual line on an input VAT account, a
        classified tax part) follows all base items of the event, so a gift
        above the limit takes its input VAT along (5.7 nos. 4 and 7).
        """
        bases = [
            item
            for item in items
            if item.line is not None
            and tax.tax_id is not None
            and tax.tax_id in item.line.tax_ids
            and item.kind in _BASE_KINDS
        ]
        if not bases and (tax.kind == "tax_base" or tax.tax_id is None):
            bases = [item for item in items if item.kind in _BASE_KINDS]
        tax.links = bases

    # ------------------------------------------------------------ valuation
    def _value(self, item):
        handler = {
            "revenue": self._revenue,
            "expense": self._expense,
            "private": self._mapped,
            "tax_output": self._tax_output,
            "tax_input": self._tax_input,
            "tax_base": self._tax_base,
            "asset": _ignore,
        }[item.kind]
        handler(item)

    def _mapped(self, item, rule="mapping"):
        """Generic mapping: shares, conditions, "of which" and additional field."""
        one = self.slices[item.index]
        lines = [
            line
            for line in self.mapping_lines(item.category.code, one.attribution_year)
            if self._condition(line.condition, item)
        ]
        if not lines:
            return
        lines.sort(key=lambda line: (line.sequence, line.field))
        parts = split_by_shares(
            item.amount, {index: line.share for index, line in enumerate(lines)}
        )
        for index, line in enumerate(lines):
            value = form_amount(line.field, parts[index])
            self._add(item.index, line.field, value, rule)
            if line.davon_field:
                self._add(item.index, line.davon_field, value, rule)
            if line.additional_field and not self._private_counted(
                line.additional_field, one
            ):
                self._add(item.index, line.additional_field, value, rule)

    def _private_counted(self, code, one):
        """No additional withdrawal or contribution where it does not apply.

        The additional private field of a category realised on booking
        (use of a private car, 4590 → 29.123) counts only for a booking
        (5.5.1 a): paid with money of the business it is no contribution.
        An entry that books the contribution on a private line counts it
        there (no double count).
        """
        if code not in (FIELD_WITHDRAWAL, FIELD_CONTRIBUTION):
            return False
        if one.kind != m.BOOKING:
            return True
        return self._private_line_in(one)

    @staticmethod
    def _condition(condition, item):
        return (
            condition == "always"
            or (condition == "debit" and item.amount > 0)
            or (condition == "credit" and item.amount < 0)
            or condition == item.regime
        )

    def _private_line_in(self, one):
        origin = self.graph.moves.get(one.origin_move_id)
        return origin is not None and any(
            line.id in self.graph.private_lines
            for line in self.graph.lines_of(origin.id)
        )

    def _revenue(self, item):
        """Revenue field by VAT treatment and revenue type (4.4)."""
        one = self.slices[item.index]
        category = item.category
        default_lines = [
            line
            for line in self.mapping_lines(category.code, one.attribution_year)
            if line.condition in ("always", m.REGULAR)
        ]
        default = default_lines[0].field if default_lines else None
        line = item.line
        tags = line.tag_names if line else ()
        taxes = (
            [self.graph.taxes.get(tax_id) for tax_id in line.tax_ids] if line else []
        )
        ku_tax = any(tax is not None and tax.ku_tax for tax in taxes)
        real_tax = any(
            tax is not None
            and tax.type_tax_use == "sale"
            and tax.amount > 0
            and not tax.ku_tax
            for tax in taxes
        )
        if line is None and item.regime == m.REGULAR and category.be_art == "ku":
            # classified or opening net amount: the rate was given there
            self._add_revenue(item, FIELD_TAXABLE, None)
            return
        code, davon = revenue.revenue_fields(
            category.be_art,
            item.regime,
            tags,
            default,
            ku_tax=ku_tax,
            real_tax=real_tax,
        )
        if line is not None and revenue.needs_tax_rate_input(
            category.be_art,
            item.regime,
            tags,
            bool(line.tax_ids),
            ku_tax,
            real_tax=real_tax,
        ):
            self._gross_revenue(item, one)
            return
        if code in FREE_SUPPLY_FIELDS:
            self._free_supply(item, code, real_tax)
            return
        if code:
            self._add_revenue(item, code, davon)

    def _add_revenue(self, item, code, davon, amount=None):
        value = form_amount(code, item.amount if amount is None else amount)
        self._add(item.index, code, value, "revenue")
        if davon:
            self._add(item.index, davon, value, "revenue")
        self._turnover(item.index, code, value)
        if davon:
            self._turnover(item.index, davon, value)

    def _gross_revenue(self, item, one):
        """Standard taxation on gross receipts: split with the entered rate."""
        origin = self._origin(one)
        rate = origin.info.regular_tax_rate if origin else None
        if rate is None:
            crossing = vat.receipt_regime_at(self.data.vat_periods, one.date)
            code = "C-32" if crossing else "C-33"
            key = "tax_rate_missing_limit" if crossing else "tax_rate_missing"
            self._finding(code, m.BLOCKER, key, one)
            self._add_revenue(item, FIELD_TAXABLE, None)
            return
        if not rate:
            self._add_revenue(item, FIELD_EXEMPT, None)
            return
        net, tax = gross_split(item.amount, rate)
        self._add(item.index, FIELD_TAXABLE, form_amount(FIELD_TAXABLE, net), "revenue")
        self._add(
            item.index, FIELD_OUTPUT_VAT, form_amount(FIELD_OUTPUT_VAT, tax), "tax"
        )
        # the limit counts what was received for the small business invoice
        self._turnover(
            item.index, FIELD_TAXABLE, form_amount(FIELD_TAXABLE, item.amount)
        )

    def _free_supply(self, item, code, real_tax):
        """Free supply booked on Kz 106 or 108 (R-27, UStAE 19.2 (2)).

        Its value for income tax never counts in the total turnover; a
        taxable free supply counts at its VAT base (§ 10 (4) UStG). Under
        standard taxation a booking with output VAT is taxable: the net
        amount of the line is the base and counts. Without output VAT it is
        not taxable. A small business books no VAT, and whether its supply
        is taxable (the good or its costs gave input tax deduction) is not
        in the books: it does not count. A taxable one is entered with its
        VAT base as profit correction (:class:`~.model.TurnoverAmount`); the
        run lists the left out ones (C-32 ``ku_free_supply_not_counted``).
        """
        one = self.slices[item.index]
        value = form_amount(code, item.amount)
        self._add(item.index, code, value, "revenue")
        if item.regime == m.KU:
            if value:
                move_id = one.origin_move_id or one.source_move_id
                self.free_supplies_left_out.append((one.date, move_id, value))
        elif real_tax:
            self.turnover.append((one.date, one.source_move_id, value))

    def _turnover(self, index, code, value):
        one = self.slices[index]
        if code in TURNOVER_FIELDS or code == FIELD_KU_DAVON:
            amount = -value if code == FIELD_KU_DAVON else value
            self.turnover.append((one.date, one.source_move_id, amount))

    def _expense(self, item):
        if item.category.separate_record == "gift":
            self._gift_items.append(item)
            return
        self._mapped(item)

    def _tax_output(self, item):
        code = FIELD_KU if item.regime == m.KU else FIELD_OUTPUT_VAT
        self._add(item.index, code, form_amount(code, item.amount), "tax")

    def _tax_input(self, item):
        gift_links = [link for link in item.links if self._is_gift(link)]
        if not gift_links:
            self._add(item.index, FIELD_INPUT_VAT, item.amount, "tax")
            return
        for link, part in self._split_over(item, item.links):
            if self._is_gift(link):
                self._gift_items.append(
                    _Item(
                        item.index,
                        part,
                        category=link.category,
                        kind="gift_tax",
                        regime=item.regime,
                        line=link.line,
                    )
                )
            else:
                self._add(item.index, FIELD_INPUT_VAT, part, "tax")

    def _tax_base(self, item):
        """Input VAT that is not deductible follows its base lines (5.7 no. 4)."""
        if not item.links:
            self._add(item.index, FIELD_OTHER, item.amount, "tax")
            self._finding(
                "C-30",
                m.WARNING,
                "input_vat_without_base",
                self.slices[item.index],
            )
            return
        for link, part in self._split_over(item, item.links):
            derived = _Item(
                item.index,
                part,
                category=link.category,
                kind=link.kind,
                regime=link.regime,
                line=link.line,
            )
            if link.kind == "asset":
                continue
            if self._is_gift(link):
                self._gift_items.append(derived)
            else:
                self._mapped(derived, "tax")

    @staticmethod
    def _split_over(item, links):
        weights = {index: abs(link.amount) for index, link in enumerate(links)}
        total = sum(weights.values())
        if not total:
            weights = {index: 1 for index in weights}
            total = len(weights)
        raw = {
            index: Fraction(item.amount * weight, total)
            for index, weight in weights.items()
        }
        parts = apportion(raw, item.amount)
        return [(links[index], parts[index]) for index in sorted(parts)]

    @staticmethod
    def _is_gift(item):
        return item.category is not None and item.category.separate_record == "gift"

    # ------------------------------------------------------------ settlement
    def _settle(self):
        """VAT paid (186) or refunded (141) per money movement (5.7 no. 5)."""
        for _key, indexes in sorted(self._settlement.items()):
            per_year = defaultdict(list)
            for index in indexes:
                per_year[self.slices[index].attribution_year].append(index)
            for _year, year_indexes in sorted(per_year.items()):
                total = sum(self.slices[index].amount for index in year_indexes)
                code = FIELD_VAT_PAID if total > 0 else FIELD_REFUND
                for index in year_indexes:
                    amount = form_amount(code, self.slices[index].amount)
                    self._add(index, code, amount, "settlement")

    # ------------------------------------------------------------ gifts
    def _gift_recipient(self, move):
        """Recipient of the gifts of an entry (5.7 no. 7).

        Entered in the EÜR information of the entry (a credit note takes it
        from the reversed document). The partner of an entry is never the
        recipient: on an invoice it is the supplier, on a bank or cash entry
        the shop that was paid. Without an entered recipient the gift is
        blocked (C-51) unless it is a give-away of a category that allows
        them.
        """
        if move.info.gift_recipient_id:
            return move.info.gift_recipient_id
        reversed_move = self.graph.moves.get(move.reversed_entry_id)
        if reversed_move is not None and reversed_move.info.gift_recipient_id:
            return reversed_move.info.gift_recipient_id
        return None

    def _gift_day(self, move, line=None):
        """Date the gift is handed over: entered date, else date of supply."""
        reversed_move = self.graph.moves.get(move.reversed_entry_id)
        return (
            move.info.gift_date
            or (reversed_move.info.gift_date if reversed_move is not None else None)
            or (line.supply_date if line is not None else None)
            or vat.supply_date(move)
        )

    def _gift_receivers(self, move, line):
        """``[(recipient, day, share)]`` of the gifts of a line.

        Recipients entered per line (several recipients on one invoice)
        split the line by their shares; otherwise the recipient of the
        entry receives the whole line.
        """
        gift_lines = self._gift_lines.get(line.id) if line is not None else None
        default_day = self._gift_day(move, line)
        if not gift_lines:
            return [(self._gift_recipient(move), default_day, Fraction(1))]
        total = sum(gift_line.share for gift_line in gift_lines)
        return [
            (
                gift_line.recipient_id,
                gift_line.gift_date or default_day,
                Fraction(gift_line.share) / total,
            )
            for gift_line in gift_lines
        ]

    def _bulk(self, move, line, category):
        """Give-aways without recipient (R-41): marked on the entry, allowed
        by the category and no recipients entered for the line."""
        return (
            move.info.gift_bulk
            and category is not None
            and category.gift_bulk_allowed
            and not (line is not None and self._gift_lines.get(line.id))
        )

    def _non_deductible_tax_share(self, line, move, regime):
        """Input VAT of ``line`` that is not deductible for other reasons than
        § 15 (1a) UStG: small business, not in the tax closing, without
        account, account for non-deductible input VAT (R 9b (2) EStR)."""
        share = Fraction(0)
        move_lines = self.graph.lines_of(move.id)
        for tax_line in move_lines:
            repartition = self.graph.repartitions.get(tax_line.repartition_id)
            if repartition is not None:
                if repartition.tax_id not in line.tax_ids:
                    continue
                tax = self.graph.taxes.get(repartition.tax_id)
                category = self.graph.category(tax_line, move.date)
                kind = vat.tax_line_kind(repartition, tax, category, regime)
                if kind != vat.TAX_BASE:
                    continue
                bases = [x for x in move_lines if repartition.tax_id in x.tax_ids]
            else:
                # manual line on an input VAT account without repartition:
                # follows all base lines of the entry (5.7 no. 4)
                category = self.graph.category(tax_line, move.date)
                if (
                    tax_line.repartition_id
                    or category is None
                    or category.role != "steuer"
                    or category.vat_kind not in ("input", "input_non_deductible")
                    or (category.vat_kind == "input" and regime != m.KU)
                ):
                    continue
                bases = [
                    x
                    for x in move_lines
                    if not x.repartition_id
                    and (cat := self.graph.category(x, move.date)) is not None
                    and cat.role in ("be", "ba", "av")
                ]
            total = sum(x.balance for x in bases)
            if total and any(x.id == line.id for x in bases):
                share += Fraction(tax_line.balance * line.balance, total)
        return share

    def _gift_measure(self, line, move):
        """Amount of a gift line for the 50 € limit (R 4.10 (3) EStR): net
        plus input VAT that is not deductible (small business: gross)."""
        regime = vat.document_regime(
            move, self.data.vat_periods, self.settings.default_regime
        )
        return line.balance + self._non_deductible_tax_share(line, move, regime)

    def _gift_groups(self):
        """Gifts per recipient and year of the gift, paid or not (5.7 no. 7)."""
        groups = defaultdict(Fraction)
        for move in self.graph.sorted_moves():
            if self.graph.cls[move.id] == m.OPENING:
                continue
            for line in self.graph.lines_of(move.id, m.N):
                if line.repartition_id:
                    continue
                category = self.graph.category(line, move.date)
                if category is None or category.separate_record != "gift":
                    continue
                if self._bulk(move, line, category):
                    continue
                measure = self._gift_measure(line, move)
                for recipient, day, share in self._gift_receivers(move, line):
                    if recipient:
                        groups[(recipient, day.year)] += measure * share
        return groups

    def _gift_parts(self, item):
        """Parts of a gift item per recipient: ``[(item, recipient, day,
        bulk)]``."""
        one = self.slices[item.index]
        line = item.line
        move = self.graph.move_of(line) if line is not None else self._origin(one)
        if move is None:
            return [(item, None, one.date, False)]
        receivers = self._gift_receivers(move, line)
        bulk = self._bulk(move, line, item.category)
        if len(receivers) == 1:
            recipient, day, _share = receivers[0]
            return [(item, recipient, day, bulk)]
        amounts = split_by_shares(
            item.amount,
            {index: share for index, (_r, _d, share) in enumerate(receivers)},
        )
        return [
            (
                _Item(
                    item.index,
                    amounts[index],
                    category=item.category,
                    kind=item.kind,
                    regime=item.regime,
                    line=item.line,
                ),
                recipient,
                day,
                False,
            )
            for index, (recipient, day, _share) in enumerate(receivers)
        ]

    def _gifts(self):
        """Gifts: limit per recipient and year of the gift (5.7 no. 7).

        The contributions of a gift keep its recipient, the day it is handed
        over and whether it is a give-away: the register of gifts per
        recipient lists them (§ 4 (7) EStG, G-08).
        """
        groups = self._gift_groups()
        for item in self._gift_items:
            for part, recipient, day, bulk in self._gift_parts(item):
                start = len(self.contributions)
                self._gift(part, recipient, day, bulk, groups)
                for contribution in self.contributions[start:]:
                    contribution.recipient = recipient or None
                    contribution.gift_date = day
                    contribution.gift_bulk = bool(bulk)

    def _gift(self, item, recipient, day, bulk, groups):
        one = self.slices[item.index]
        limit = self.params.cents("gift_limit", day.year, 5000)
        not_deductible = (
            item.category is not None
            and item.category.restricted_column == "non_deductible"
        )
        if bulk or not recipient:
            if bulk and item.line is not None and (item.line.unit_price or 0) > limit:
                self._finding("C-51", m.WARNING, "bulk_gift_unit_price", one)
            if not_deductible:
                # booked as not deductible: the limit does not matter, the
                # input VAT is not deductible either (§ 15 (1a) UStG)
                if item.kind == "gift_tax":
                    self._finding("C-52", m.WARNING, "gift_input_vat", one)
                    code = FIELD_GIFT_NOT_DEDUCTIBLE
                else:
                    self._mapped(item)
                    return
            else:
                if not bulk and item.kind != "gift_tax":
                    self._finding("C-51", m.BLOCKER, "gift_without_recipient", one)
                code = FIELD_INPUT_VAT if item.kind == "gift_tax" else FIELD_GIFT
        elif groups.get((recipient, day.year), 0) > limit:
            code = FIELD_GIFT_NOT_DEDUCTIBLE
            if item.kind == "gift_tax":
                self._finding("C-52", m.WARNING, "gift_input_vat", one)
        elif item.kind == "gift_tax":
            code = FIELD_INPUT_VAT
        else:
            self._mapped(item)
            return
        self._add(item.index, code, form_amount(code, item.amount), "gift")

    # ------------------------------------------------------------ private
    def _private_lines(self):
        """Withdrawals and contributions of private money lines (5.7 no. 9)."""
        for move in self.graph.moves_of_class(m.SOURCE):
            if move.private_journal or self._private_transfer(move):
                continue
            lines = [
                line
                for line in self.graph.lines_of(move.id, m.L)
                if line.id in self.graph.private_lines
            ]
            if not lines:
                continue
            cash = self.cash_dates.get(move.id)
            event = self._next_event()
            for line in lines:
                category = self.graph.category(line, cash.date)
                self.slices.append(
                    m.Slice(
                        kind=m.PRIVATE,
                        date=cash.date,
                        amount=line.balance,
                        event=event,
                        key=(line.id, 0),
                        source_move_id=move.id,
                        source_line_id=line.id,
                        origin_move_id=move.id,
                        origin_line_id=line.id,
                        document_move_id=move.id,
                        account_id=line.account_id,
                        partner_id=line.partner_id,
                        category=category.code if category else None,
                        attribution_year=cash.date.year,
                        override=cash.override,
                    )
                )
                code = FIELD_WITHDRAWAL if line.balance > 0 else FIELD_CONTRIBUTION
                self._add(
                    len(self.slices) - 1,
                    code,
                    form_amount(code, line.balance),
                    "private",
                )

    def _private_transfer(self, move):
        """Transfer between private and equity accounts only: not counted."""
        for line in self.graph.lines_of(move.id):
            account = self.graph.accounts[line.account_id]
            if line.id not in self.graph.private_lines and (
                account.internal_group != "equity"
            ):
                return False
        return True

    def _private_journal(self):
        """R-18: the journal is a private account. Money of the business that
        comes in there is withdrawn, money paid from it is contributed
        (gross), whatever it pays: expenses, VAT, fixed assets, loans or
        deposits. Lines on private accounts are private money anyway and
        count by neither rule.

        It counts per money movement (source and cash date), not per slice:
        a discount or a VAT correction in the same statement line is no
        money of its own, only the amount received or paid is withdrawn or
        contributed (5.7 no. 9).
        """
        private_sources = self._private_sources
        if not private_sources:
            return
        totals = defaultdict(int)
        overrides = defaultdict(bool)
        for one in self.slices:
            if (
                one.source_move_id not in private_sources
                or one.kind not in m.MONEY_KINDS
                or one.kind == m.UNCLEAR
                or not one.amount
            ):
                continue
            category = self._category(one.category)
            if category is not None and category.role == "privat":
                continue
            totals[(one.source_move_id, one.date)] += one.amount
            overrides[(one.source_move_id, one.date)] |= one.override
        for (source_id, day), total in sorted(totals.items()):
            money = self.graph.lines_of(source_id, m.L)
            self._private_slice(
                source_id,
                source_id,
                money[0] if money else None,
                day,
                -total,
                overrides[(source_id, day)],
                "private_journal",
            )

    def _private_transfers(self):
        """R-18: money between the private account journal and business money.

        A transfer over a money transit account (money to money) and an entry
        whose money lines lie on both (credit card statement paid from the
        private account) move money between the private sphere and the
        business: from the business it is a withdrawal, into it a
        contribution, on the cash date of the business side.
        """
        for note in self.notes:
            if (
                note.kind == "money_to_money"
                and note.source_move_id not in self._private_sources
                and note.move_id in self._private_sources
            ):
                line = self.graph.lines.get(note.line_id)
                self._private_slice(
                    note.source_move_id,
                    note.move_id,
                    line,
                    note.date,
                    note.amount,
                    self.cash_dates.get(note.source_move_id).override,
                )
        for move in self.graph.moves_of_class(m.SOURCE):
            lines = self.graph.lines_of(move.id)
            if any(self.graph.role[line.id] != m.L for line in lines):
                continue
            private = [
                line
                for line in lines
                if self.graph.accounts[line.account_id].private_money
            ]
            business = [
                line
                for line in lines
                if line.id not in self.graph.private_lines
                and not self.graph.accounts[line.account_id].private_money
            ]
            if not private or not business:
                continue
            amount = -sum(line.balance for line in business)
            cash = self.cash_dates.get(move.id)
            self._private_slice(
                move.id, move.id, private[0], cash.date, amount, cash.override
            )

    def _private_slice(
        self, source_id, origin_id, line, day, amount, override, rule="private"
    ):
        """Withdrawal (``amount`` > 0) or contribution of private money."""
        if not amount:
            return
        self.slices.append(
            m.Slice(
                kind=m.PRIVATE,
                date=day,
                amount=amount,
                event=self._next_event(),
                key=(line.id, 0) if line is not None else None,
                source_move_id=source_id,
                source_line_id=line.id if line is not None else None,
                origin_move_id=origin_id,
                origin_line_id=line.id if line is not None else None,
                document_move_id=origin_id,
                account_id=line.account_id if line is not None else None,
                partner_id=line.partner_id if line is not None else None,
                attribution_year=day.year,
                override=override,
            )
        )
        code = FIELD_WITHDRAWAL if amount > 0 else FIELD_CONTRIBUTION
        self._add(len(self.slices) - 1, code, form_amount(code, amount), rule)

    # ------------------------------------------------------------ checks
    def _check_booking_categories(self):
        """C-29: category realised at booking inside a document or transit.

        Reported in the year of the entry and in the years in which money
        realised the line (paid in the next year).
        """
        money_years = defaultdict(set)
        for one in self.slices:
            if one.kind in m.MONEY_KINDS and one.origin_line_id:
                money_years[one.origin_line_id] |= {
                    one.date.year,
                    one.attribution_year or one.date.year,
                }
        for move in self.graph.sorted_moves():
            if self.graph.cls[move.id] not in (m.DOC, m.TRANSIT):
                continue
            for line in self.graph.lines_of(move.id, m.N):
                category = self.graph.category(line, move.date)
                if (
                    category is not None
                    and category.erfassung == "B"
                    and category.role != "afa"
                ):
                    self.findings.append(
                        m.Finding(
                            code="C-29",
                            severity=m.BLOCKER,
                            key="booking_category_in_document",
                            years=tuple(
                                sorted({move.date.year} | money_years[line.id])
                            ),
                            move_ids=(move.id,),
                            line_ids=(line.id,),
                            amount=line.balance,
                        )
                    )
                    break
        for move_id in sorted(self._manual_tax_moves):
            move = self.graph.moves[move_id]
            self.findings.append(
                m.Finding(
                    code="C-34",
                    severity=m.WARNING,
                    key="tax_line_without_repartition",
                    years=(move.date.year,),
                    move_ids=(move_id,),
                )
            )


_BASE_KINDS = frozenset({"expense", "revenue", "asset"})


def _ignore(_item):
    """Fixed assets and depreciation: value from the asset register (6.5)."""
