# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Data classes of the cash basis engine (spec 5, 7.2).

The input is what phase A (extraction) delivers: accounts with their
category assignments, moves, lines and partial reconciliations, taxes, the
VAT status periods and the user decisions stored in the module (opening
items, classifications of unallocated rests, ten-day decisions). Everything
is plain data; the engine never touches the ORM.

Amounts are integers in cent with the sign of the balance (debit positive).
Rates and shares are :class:`fractions.Fraction` (or values convertible to
it). Dates are :class:`datetime.date`.
"""

import datetime
from dataclasses import dataclass, field
from fractions import Fraction

# ---------------------------------------------------------------- constants
#: Odoo account types of money accounts (role L)
CASH_TYPES = frozenset({"asset_cash", "liability_credit_card"})
#: Odoo account types of receivables and payables
OPEN_ITEM_TYPES = frozenset({"asset_receivable", "liability_payable"})
#: ``internal_group`` of balance sheet accounts (B-04)
BALANCE_GROUPS = frozenset({"asset", "liability"})

#: line roles (spec 5.2)
L = "L"
R = "R"
N = "N"

#: move classes (spec 5.2)
OPENING = "opening"
SOURCE = "source"
DOC = "doc"
TRANSIT = "transit"
DIRECT = "direct"

#: slice kinds (spec 7.3 ``l10n_de_euer.cash.slice.kind``)
DOC_VIA_PAYMENT = "doc_via_payment"
CASH_DIRECT = "cash_direct"
KIND_TRANSIT = "transit"
UNALLOCATED = "unallocated"
CLASSIFIED = "classified"
UNCLEAR = "unclear"
KIND_OPENING = "opening"
BOOKING = "booking"
PRIVATE = "private"
#: kinds realised from money (geldabstimmung I-1)
MONEY_KINDS = frozenset(
    {
        DOC_VIA_PAYMENT,
        CASH_DIRECT,
        KIND_TRANSIT,
        UNALLOCATED,
        CLASSIFIED,
        UNCLEAR,
        KIND_OPENING,
    }
)

BLOCKER = "blocker"
WARNING = "warning"
INFO = "info"

KU = "ku"
REGULAR = "regular"


# --------------------------------------------------------------- master data
@dataclass(frozen=True, kw_only=True)
class Category:
    """EÜR category (``l10n_de_euer.category``, spec 4.2)."""

    code: str
    role: str
    be_art: str | None = None
    erfassung: str = "Z"
    writeoff: bool = False
    on_payment: bool = False
    vat_kind: str | None = None
    separate_record: str | None = None
    restricted_column: str | None = None
    recurring_candidate: bool = False
    gift_bulk_allowed: bool = False
    interim: bool = False
    scope_status: str = "supported"


@dataclass(frozen=True, kw_only=True)
class MappingLine:
    """Field of a category in a form year (``l10n_de_euer.category.mapping``)."""

    category: str
    field: str
    share: Fraction = Fraction(1)
    condition: str = "always"
    option: str | None = None
    davon_field: str | None = None
    additional_field: str | None = None
    sequence: int = 10


@dataclass(frozen=True, kw_only=True)
class Assignment:
    """Category of an account in a period (``l10n_de_euer.account.assignment``)."""

    category: str
    date_from: datetime.date | None = None
    date_to: datetime.date | None = None


@dataclass(frozen=True, kw_only=True)
class Account:
    """Account of the business.

    ``liquidity``: default account of a bank, cash or credit card journal or
    a payment provider account configured as money account. ``suspense``:
    suspense account of such a journal (B-05). ``private_money``: default
    account of a journal kept as private account (R-18). ``outstanding``:
    outstanding receipts or payments account of a payment method (the
    payment wizard books on it, 5.4 no. 3).
    """

    id: int
    code: str
    account_type: str
    internal_group: str
    reconcile: bool = False
    liquidity: bool = False
    suspense: bool = False
    private_money: bool = False
    outstanding: bool = False
    assignments: tuple = ()


@dataclass(frozen=True, kw_only=True)
class Tax:
    """Tax with its type and the factors of its tax repartition lines.

    ``factors`` holds ``(document_type, factor_percent)`` of the repartition
    lines of type ``tax``; a tax with positive and negative factors for the
    same document type is reverse charge, an intra-EU acquisition or import
    VAT (spec 5.7 no. 4). For taxes in a group pass the group's type.
    ``ku_tax``: the Odoo tax "0 % USt (Pflichtbefreit z.B. als Kleinunt.
    oder bei med. Leistg.)" (spec 4.4 no. 5, :func:`.revenue.is_ku_tax`).
    """

    id: int
    type_tax_use: str
    amount: Fraction = Fraction(0)
    factors: tuple = ()
    ku_tax: bool = False


@dataclass(frozen=True, kw_only=True)
class Repartition:
    """Tax repartition line (``account.tax.repartition.line``)."""

    id: int
    tax_id: int
    document_type: str = "invoice"
    factor_percent: Fraction = Fraction(100)
    use_in_tax_closing: bool = True
    has_account: bool = True


@dataclass(frozen=True, kw_only=True)
class Partner:
    id: int
    recurring: bool = False
    tax_office: bool = False


@dataclass(frozen=True, kw_only=True)
class MoveInfo:
    """Additional data of a move (``l10n_de_euer.move.info``, B-01).

    ``gift_recipient_id``: partner who receives the gifts of the document
    (5.7 no. 7); the partner of an invoice is the supplier, never the
    recipient. ``gift_date``: date the gift is handed over, if it differs
    from the date of supply (R 4.10 (2) S. 3 EStR).
    """

    vat_regime: str | None = None
    cash_date: datetime.date | None = None
    cash_date_reason: str | None = None
    economic_year: int | None = None
    due_date: datetime.date | None = None
    vat_period: str | None = None
    regular_tax_rate: Fraction | None = None
    gift_bulk: bool = False
    gift_recipient_id: int | None = None
    gift_date: datetime.date | None = None


NO_INFO = MoveInfo()


@dataclass(frozen=True, kw_only=True)
class Move:
    """Journal entry.

    ``supply_date``: ``taxable_supply_date``, otherwise ``delivery_date``.
    ``statement``: the move of a bank statement line. ``bank_date``: bank
    date from the statement data, if known (C-12). ``payment_order_date``:
    date the payment order was handed to the bank (C-23 proposal).
    ``revaluation``: valuation of foreign currency holdings (unrealised
    exchange difference); such entries are left out (C-28).
    ``lock_date_shift``: lock date of a statement entry dated where Odoo
    puts an entry of a locked period (first day after the lock date, its
    month end, the year end or the day of posting); Odoo may have moved it
    there (B-12, warning C-12 until the bank reconciliation C-38).
    """

    id: int
    name: str = ""
    date: datetime.date
    state: str = "posted"
    company_id: int | None = None
    move_type: str = "entry"
    partner_id: int | None = None
    reversed_entry_id: int | None = None
    caba: bool = False
    opening: bool = False
    statement: bool = False
    invoice_date: datetime.date | None = None
    invoice_date_due: datetime.date | None = None
    supply_date: datetime.date | None = None
    private_journal: bool = False
    revaluation: bool = False
    bank_date: datetime.date | None = None
    payment_order_date: datetime.date | None = None
    lock_date_shift: datetime.date | None = None
    info: MoveInfo = NO_INFO


@dataclass(frozen=True, kw_only=True)
class Line:
    """Journal item. ``balance`` in cent, debit positive.

    ``residual_currency_zero``: the line is in a foreign currency and fully
    reconciled in that currency (C-19). ``supply_date``: delivery date of the
    line, if it differs from the move (gifts). ``unit_price``: price per
    piece in cent (bulk gifts, C-51).
    """

    id: int
    move_id: int
    account_id: int
    balance: int
    partner_id: int | None = None
    repartition_id: int | None = None
    tax_ids: tuple = ()
    tag_names: tuple = ()
    display_type: str = "product"
    currency_id: int | None = None
    amount_currency: int = 0
    residual_currency_zero: bool = False
    supply_date: datetime.date | None = None
    unit_price: int | None = None


@dataclass(frozen=True, kw_only=True)
class Partial:
    """Partial reconciliation; ``amount`` in cent, company currency.

    ``created``: day the reconciliation was made (``create_date``); a credit
    note reconciled after the end of the year of ``max_date`` can change a
    year that was already computed (C-60).
    """

    id: int
    debit_line_id: int
    credit_line_id: int
    amount: int
    max_date: datetime.date
    exchange_move_id: int | None = None
    created: datetime.date | None = None


@dataclass(frozen=True, kw_only=True)
class VatPeriod:
    """VAT status period (``l10n_de_euer.vat.period``).

    ``prev_year_turnover``: total turnover of the previous year entered
    (cent) for the check of the year ``prev_year_turnover_year``; without
    that year it holds for the first year of the period.
    """

    date_from: datetime.date
    date_to: datetime.date | None = None
    regime: str = REGULAR
    basis: str = "supply_date"
    vat_return_period: str | None = None
    permanent_extension: bool = False
    prev_year_turnover: int | None = None
    prev_year_turnover_year: int | None = None


@dataclass(frozen=True, kw_only=True)
class ParameterValue:
    """Rule parameter with validity (``l10n_de_euer.parameter``)."""

    code: str
    value: object
    date_from: datetime.date | None = None
    date_to: datetime.date | None = None


@dataclass(frozen=True, kw_only=True)
class GiftLine:
    """Recipient of the gifts of a document line (``l10n_de_euer.gift.line``).

    One invoice can hold gifts for several recipients; the line is split
    by ``share`` (number or share of the gifts) among its recipients.
    """

    line_id: int
    recipient_id: int
    gift_date: datetime.date | None = None
    share: Fraction = Fraction(1)


@dataclass(frozen=True, kw_only=True)
class OpeningItem:
    """Open item of an opening entry (``l10n_de_euer.opening.item``, B-08)."""

    line_id: int
    category: str
    vat_regime: str = KU
    tax_rate: Fraction = Fraction(0)


@dataclass(frozen=True, kw_only=True)
class Classification:
    """Classification of a rest (``l10n_de_euer.residual.classification``).

    ``line_id`` identifies the case: for an unallocated rest (C-20) the
    reconcilable line of the source or transit entry that carries the rest;
    for money reaching a write-off (C-36) the write-off line; for money to
    money between two years (C-37) the reconcilable line of the source.
    ``kind``: ``revenue``, ``expense``, ``negative_revenue`` (realised like
    a document line with category, rate and regime) or ``loan``,
    ``deposit``, ``refund_obligation`` (neutral).
    """

    line_id: int
    kind: str
    category: str | None = None
    tax_rate: Fraction = Fraction(0)
    vat_regime: str | None = None


NEUTRAL_CLASSIFICATIONS = frozenset({"loan", "deposit", "refund_obligation"})


@dataclass(frozen=True, kw_only=True)
class TenDayDecision:
    """Decision of the ten-day review list (``l10n_de_euer.ten_day.item``).

    ``kind`` ``recurring`` (ten-day rule, ``decision`` apply/reject) or
    ``direct_debit_tax`` (apply: the due date becomes the cash date, R-06).
    The key is the money event: source entry and origin entry.
    """

    source_move_id: int
    origin_move_id: int
    decision: str
    kind: str = "recurring"
    economic_year: int | None = None
    due_date: datetime.date | None = None


@dataclass(frozen=True, kw_only=True)
class FieldAmount:
    """Field value that does not come from money (phase F, spec 5.8).

    ``amount`` in cent with the sign of the form (income and expense
    positive). ``source``: ``asset`` (fixed asset register, stage E3) or
    ``adjustment`` (profit correction, stage E4).
    """

    year: int
    field: str
    amount: int
    source: str = "adjustment"
    reference: str = ""


@dataclass(frozen=True, kw_only=True)
class TurnoverAmount:
    """Part of the total turnover without money (phase F, R-27).

    The small business limit counts the taxable free supplies (§ 3 (1b),
    (9a) UStG) at their VAT base (§ 10 (4) UStG) and income in kind at its
    consideration (not when tax-free or not taxable, Kz 103); never the
    value for income tax, and never a free supply that is not taxable
    because the good or its costs gave no input tax deduction (UStAE 19.2
    (2)). ``amount`` in cent, positive; ``date``: the day it counts (default
    31 December of ``year``).
    """

    year: int
    amount: int
    date: datetime.date | None = None
    source: str = "adjustment"
    reference: str = ""

    def day(self):
        return self.date or datetime.date(self.year, 12, 31)


@dataclass(frozen=True, kw_only=True)
class Settings:
    """Company options (spec 7.3 ``res.company``)."""

    offsetting_is_payment: bool = True
    fx_mode: str = "separate"
    entertainment_mode: str = "separate"
    default_regime: str = REGULAR


@dataclass(frozen=True, kw_only=True)
class EngineInput:
    """Everything a run needs (phase 0 and A).

    ``mappings``: form year → mapping lines of all categories.
    ``company_ids``: companies of the business; moves of other companies
    are ignored (empty: all). ``founding_year``: year the business started
    (small business threshold), ``activity_start`` the day (conversion into
    a full year until 2024). ``transfer_confirmations``: sources the user
    confirmed as "order given in the new year" (C-23). ``turnover_amounts``:
    :class:`TurnoverAmount` records of the year and the previous year.
    """

    accounts: tuple = ()
    categories: tuple = ()
    mappings: dict = field(default_factory=dict)
    moves: tuple = ()
    lines: tuple = ()
    partials: tuple = ()
    taxes: tuple = ()
    repartitions: tuple = ()
    partners: tuple = ()
    vat_periods: tuple = ()
    parameters: tuple = ()
    opening_items: tuple = ()
    classifications: tuple = ()
    ten_day_decisions: tuple = ()
    gift_lines: tuple = ()
    transfer_confirmations: frozenset = frozenset()
    asset_amounts: tuple = ()
    adjustment_amounts: tuple = ()
    turnover_amounts: tuple = ()
    settings: Settings = Settings()
    company_ids: frozenset = frozenset()
    founding_year: int | None = None
    activity_start: datetime.date | None = None


# ------------------------------------------------------------------- output
@dataclass(kw_only=True)
class Slice:
    """Realised amount of one line at one date (spec 7.3 ``cash.slice``).

    ``amount`` in cent with the sign of the balance of the realised line.
    ``event`` groups the slices realised together (one money event at one
    document, one booking ...). ``document_move_id`` is the entry the money
    reached (the document of the open item, the source or the transit
    entry); the line itself may belong to another entry after a credit note.
    ``key`` is the key of the realised line in
    the composition: ``(line id, 0)`` for real lines, ``(line id, 1|2)`` for
    the net and tax part of an opening item or a classification.
    ``money_date``: cash date of the money at its source before a cash date
    entered on a payment entry in the path replaced it (``date``); it ties
    the slice to the money of its source (I-1).
    """

    kind: str
    date: datetime.date
    amount: int
    event: int
    key: tuple | None = None
    source_move_id: int | None = None
    source_line_id: int | None = None
    origin_move_id: int | None = None
    origin_line_id: int | None = None
    document_move_id: int | None = None
    account_id: int | None = None
    partner_id: int | None = None
    repartition_id: int | None = None
    category: str | None = None
    path: tuple = ()
    attribution_year: int | None = None
    ten_day_applied: bool = False
    override: bool = False
    virtual_tax: str | None = None
    regime: str | None = None
    tax_rate: Fraction | None = None
    money_date: datetime.date | None = None

    def sort_key(self):
        return (
            self.date,
            self.event,
            self.key or (0, 0),
            self.kind,
            self.amount,
        )


@dataclass(kw_only=True)
class Contribution:
    """Amount of a slice (or of phase F) in a form field.

    ``amount`` in cent with the sign of the form. ``slice_index`` points
    into :attr:`Result.slices`; ``None`` for phase F values. The other
    attributes only describe where the value comes from (drilldown and
    registers, spec 7.6 and G-08); they never change an amount.
    """

    field: str
    amount: int
    year: int
    slice_index: int | None = None
    rule: str = "mapping"
    #: phase F: record the value comes from, e.g. ``l10n_de_euer.adjustment,7``
    #: (drilldown to the own receipt, spec 7.6 no. 4)
    reference: str = ""
    #: gifts (5.7 no. 7): recipient, day the gift is handed over and whether
    #: it is a give-away without recipient (register of gifts, G-08)
    recipient: int | None = None
    gift_date: datetime.date | None = None
    gift_bulk: bool = False


@dataclass(kw_only=True)
class Finding:
    """Check result of the engine (spec 7.8).

    ``key`` names the case for the message; ``params`` holds its values.
    ``years`` are the statement years the finding belongs to (empty: all).
    """

    code: str
    severity: str
    key: str
    years: tuple = ()
    move_ids: tuple = ()
    line_ids: tuple = ()
    amount: int = 0
    params: dict = field(default_factory=dict)

    def applies_to(self, year):
        return not self.years or year in self.years


@dataclass(kw_only=True)
class MoneyNote:
    """Money that is not realised: money to money, cycle, capped transit.

    ``money_date``: cash date of the money at its source (see
    :class:`Slice`).
    """

    kind: str
    date: datetime.date
    amount: int
    source_move_id: int
    move_id: int | None = None
    line_id: int | None = None
    money_date: datetime.date | None = None


@dataclass(kw_only=True)
class TenDayItem:
    """Entry of the ten-day review list (spec 5.6.1, 7.3).

    ``proposal``: ``apply`` (flag, tax office or repeated payments and an
    economic year that differs from the year of payment), ``review`` (the
    user has to judge, e.g. only a document of the other year), ``reject``
    (the known due date lies outside the window: the rule cannot apply) or
    ``confirm`` (transfer at the turn of the year).
    """

    kind: str
    source_move_id: int
    origin_move_id: int
    cash_date: datetime.date
    reasons: tuple = ()
    economic_year: int | None = None
    due_date: datetime.date | None = None
    proposal: str = "apply"
    proposed_cash_date: datetime.date | None = None
    decision: str | None = None
    applied: bool = False
    slice_indexes: tuple = ()


@dataclass(kw_only=True)
class Turnover:
    """Total turnover of a calendar year for the small business limit (R-27)."""

    year: int
    amount: int = 0
    limit: int | None = None
    crossing_date: datetime.date | None = None
    crossing_source_id: int | None = None
    entries: list = field(default_factory=list)
