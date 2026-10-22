# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Depreciation, low-value assets, asset pools and Anlage AVEÜR (spec 6.3, 6.4).

Pure Python without any ORM access, so it can be tested without a database.
The ORM layer reads the asset register, converts it into the data classes of
this module and stores the results (spec 7.2).

Rules (§ 7, § 6 (2), (2a), § 7a, § 7g (5) EStG; R 7.4 EStR; H 7.4 EStH):

- linear: annual amount ``J = round(cost × 12 / useful life in months)``; in
  the year of delivery ``J × months / 12`` with the month of delivery counting
  in full (monthly rule); the last year takes the remainder;
- buildings with linear depreciation (§ 7 (4) sentence 1 EStG): a fixed
  percentage (12 / useful life) of the basis. Additional costs and subsidies
  of a later year change the basis, not the book value over the remaining
  useful life (H 7.4 EStH, R 7.3 (4) sentence 2 EStR); after an extraordinary
  depreciation the basis is the cost less that amount from the following
  year (§ 11c (2) EStDV). The depreciation then runs until the book value is
  used up. A remaining useful life entered with the event spreads the book
  value over it instead (§ 7 (4) sentence 2, H 7.4 EStH);
- degressive: book value × rate, monthly rule in the first year, switch to
  linear (book value ÷ remaining useful life) when that is at least the
  degressive amount or in the chosen year;
- electric vehicles (§ 7 (2a)): fixed percentages of the cost, no monthly rule;
- digital assets with a useful life of one year: full cost in the year of
  delivery (BMF 22.02.2022);
- low-value assets and items up to 250 €: full cost in the year of delivery;
- asset pools: one fifth per year from the year of formation, the fifth year
  takes the remainder; disposals have no effect;
- special depreciation § 7g (5): at most 40 % (deliveries from 2024) or 20 %
  of the cost in the year of delivery and the four following years; § 7b:
  up to 5 % per year of the cost of a building in the year of delivery and
  the three following years. Within this period additional costs and
  reductions change the basis of the depreciation, not the book value
  (§ 7a (1)); after it a linear depreciation spreads the book value over the
  remaining useful life (§ 7a (9)), a degressive one may continue
  (R 7a (10) sentence 3 EStR);
- disposal: depreciation up to the disposal (month convention), the book
  value is the residual book value of the year (Kz 135); assets that are not
  depreciable and current assets under § 4 (3) sentence 4 EStG become an
  expense only when the proceeds are received (or at the withdrawal).

Every amount is rounded to the cent per asset and year (ROUND_HALF_UP). A new
asset never keeps a memo value of 1 €: the full cost is depreciated
(R 7.4 (3) sentence 1 EStR). Only assets taken over from another system may
carry the memo value they already had there (R-49), and the checks report it.
"""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0.00")
TWELVE = Decimal(12)

KINDS = (
    "depreciable",
    "gwg",
    "pool",
    "instant_250",
    "non_depreciable",
    "current_4_3_4",
)
#: Kinds that are expensed in full in the year of delivery (Kz 132)
IMMEDIATE_KINDS = frozenset({"gwg", "instant_250"})
#: Kinds that appear in the groups of Anlage AVEÜR (pools have own lines)
AVEUER_KINDS = frozenset({"depreciable", "non_depreciable", "current_4_3_4"})
#: Kinds without depreciation that become an expense at the receipt of the
#: proceeds or at the withdrawal (§ 4 (3) sentence 4 EStG)
CASH_DISPOSAL_KINDS = frozenset({"non_depreciable", "current_4_3_4"})

METHODS = ("linear", "degressive", "ev_7_2a", "digital_1y", "none", "manual")
DEPRECIATING_METHODS = frozenset(
    {"linear", "degressive", "ev_7_2a", "digital_1y", "manual"}
)

GROUPS = (
    "land",
    "building",
    "land_rights",
    "office_land",
    "office_building",
    "intangible",
    "vehicle",
    "office_equipment",
    "other_movable",
    "fin_tev",
    "fin_other",
    "current",
)
MOVABLE_GROUPS = frozenset({"vehicle", "office_equipment", "other_movable"})
NON_DEPRECIABLE_GROUPS = frozenset({"land", "office_land", "fin_tev", "fin_other"})
#: Groups of real estate: § 15a period of ten years instead of five
REAL_ESTATE_GROUPS = frozenset(
    {"land", "building", "land_rights", "office_land", "office_building"}
)
#: Groups of buildings: linear depreciation is a percentage of the basis
#: (§ 7 (4) sentence 1 EStG)
BUILDING_GROUPS = frozenset({"building", "office_building"})

EVENT_TYPES = (
    "additional_cost",
    "subsidy",
    "sec7g_reduction",
    "sec7g_special",
    "sec7b_special",
    "extraordinary",
    "useful_life_change",
    "business_use_change",
    "manual_depreciation",
)
#: Events that change the book value at the beginning of their year
BASIS_EVENTS = frozenset({"additional_cost", "subsidy"})
#: Special depreciation (AVEÜR column of special depreciation, Kz 403 … 113)
#: with the years of its period of benefit (§ 7a (1) EStG)
SPECIAL_EVENTS = {"sec7g_special": 5, "sec7b_special": 4}
#: Events only depreciable assets can have (§ 7 (1) sentence 7 EStG)
DEPRECIABLE_EVENTS = frozenset({"extraordinary", "useful_life_change"})
#: Events whose amount must be positive (a negative one would be a hidden
#: write-up or an addition)
AMOUNT_EVENTS = frozenset(
    {
        "additional_cost",
        "subsidy",
        "sec7g_reduction",
        "sec7g_special",
        "sec7b_special",
        "extraordinary",
        "manual_depreciation",
    }
)
#: Useful life up to which the cost is no depreciation but an expense of the
#: year (H 7.4 EStH "Nutzungsdauer", BFH 26.08.1993 - IV R 127/91)
SHORT_LIFE_MONTHS = 12

DISPOSAL_KINDS = ("sale", "withdrawal", "scrap", "loss")

#: Kennzahlen of Anlage AVEÜR per group: cost at the beginning, book value at
#: the beginning, additions, special depreciation, depreciation, disposals.
#: ``None``: the group has no such column. The book value at the end is a
#: formula of the form data. Current assets (§ 4 (3) sentence 4) have no
#: opening book value of their own: column 600 holds the cost of the items
#: present at the beginning, which is their book value.
GROUP_KZ = {
    "land": ("100", "101", "102", None, None, "105"),
    "building": ("110", "111", "112", "113", "114", "115"),
    "land_rights": ("120", "121", "122", None, "124", "125"),
    "office_land": ("200", "201", "202", None, None, "205"),
    "office_building": ("210", "211", "212", None, "214", "215"),
    "intangible": ("320", "321", "322", None, "324", "325"),
    "vehicle": ("400", "401", "402", "403", "404", "405"),
    "office_equipment": ("410", "411", "412", "413", "414", "415"),
    "other_movable": ("420", "421", "422", "423", "424", "425"),
    "fin_tev": ("500", "501", "502", None, None, "505"),
    "fin_other": ("510", "511", "512", None, None, "515"),
    "current": ("600", None, "602", None, None, "605"),
}
#: Asset pools relative to the assessment year: formation (or cost),
#: book value at the beginning, release (spec F-05)
POOL_KZ = {
    0: ("432", None, "434"),
    -1: ("440", "441", "444"),
    -2: ("450", "451", "454"),
    -3: ("460", "461", "464"),
    -4: ("470", "471", "474"),
}
POOL_YEARS = 5

AVEUER = "AVEUER.40."
FIELD_LOW_VALUE = "EUER.25.132"
FIELD_GOODS = "EUER.25.100"
FIELD_HOME_OFFICE = "EUER.25.172"
FIELD_HOME_OFFICE_ND = "EUER.25.162"
FIELD_DISPOSAL_REVENUE = "EUER.20.102"
FIELD_WITHDRAWAL_IN_KIND = "EUER.20.108"
FIELD_VAT = "EUER.20.140"
FIELD_WITHDRAWALS = "EUER.29.122"
FIELD_CONTRIBUTIONS = "EUER.29.123"
FIELD_ELECTRIC = "AVEUER.40.407"

#: Transfers Anlage AVEÜR → Anlage EÜR that are formulas of the form data
#: (spec F-04): EÜR field → AVEÜR fields whose sum it must equal
TRANSFERS = {
    "EUER.25.130": ("AVEUER.40.490",),
    "EUER.25.131": ("AVEUER.40.324",),
    "EUER.25.136": ("AVEUER.40.190",),
    "EUER.25.134": ("AVEUER.40.480", "AVEUER.40.113"),
    "EUER.25.137": ("AVEUER.40.499",),
    "EUER.25.135": tuple(
        AVEUER + kz[5] for group, kz in GROUP_KZ.items() if group != "current"
    ),
}
#: Bagatelle limit of § 44 (1) UStDV for the input tax correction (§ 15a UStG)
SEC15A_BAGATELLE = Decimal("1000.00")
#: Cost from which the cost of an asset is spread over the § 15a period in
#: the VAT basis of a use withdrawal (§ 10 (4) sentence 1 no. 2 sentence 3 UStG)
VAT_SPREAD_LIMIT = Decimal("500.00")


class DepreciationError(ValueError):
    """Raised for input the depreciation rules do not allow.

    ``code`` identifies the rule, ``params`` its values; the ORM layer turns
    them into a translated message. ``str()`` gives an English text.
    """

    MESSAGES = {
        "kind": "Unknown asset kind {value!r}",
        "method": "Unknown method {value!r}",
        "disposal_kind": "Unknown disposal kind {value!r}",
        "event_type": "Unknown event type {value!r}",
        "negative_cost": "The cost must not be negative",
        "memo": "The memo value must be below the cost",
        "no_method": "A depreciable asset needs a depreciation method",
        "method_not_allowed": "Only depreciable assets have a depreciation method",
        "no_life": "Linear and degressive depreciation need a useful life",
        "short_life": "A useful life of up to 12 months is not spread (H 7.4 "
        "EStH): the cost is an expense of the year",
        "no_rate": "Degressive depreciation needs a rate",
        "switch_year": "The switch to linear depreciation is possible from the "
        "year after the delivery ({year}) on; linear from the start is the "
        "method linear",
        "no_ev_rates": "No rates of § 7 (2a) EStG for this delivery date",
        "takeover_date": "The takeover date must be a 31 December",
        "takeover_before_delivery": "The takeover date is before the delivery",
        "disposal_before_delivery": "The disposal cannot be before the delivery",
        "disposal_before_takeover": "The asset left before the takeover date",
        "event_before_delivery": "An event cannot be dated before the delivery",
        "event_kind": "Extraordinary depreciation and a new useful life only "
        "for depreciable assets",
        "extraordinary_degressive": "No extraordinary depreciation next to "
        "degressive depreciation (§ 7 (2) sentence 4 EStG)",
        "ev_events": "§ 7 (2a) EStG: only events in the year of delivery",
        "life_change_first_year": "Change the useful life of the asset itself in "
        "the year of delivery",
        "life_change_months": "Enter the remaining useful life in months",
        "manual_method": "Manual depreciation needs the method 'manual'",
        "event_amount": "The amount of the event must be positive",
        "event_life": "The remaining useful life of an event must not be negative",
        "special_movable": "§ 7g (5) EStG applies to movable assets only",
        "special_method": "§ 7g (5) EStG: only next to linear or degressive "
        "depreciation",
        "special_period": "§ 7g (5) EStG: only in the year of delivery and the "
        "four following years",
        "special_limit": "Special depreciation {total} exceeds {percent} % of the "
        "cost (§ 7g (5) EStG)",
        "sec7b_group": "§ 7b EStG applies to buildings only",
        "sec7b_method": "§ 7b EStG: only next to linear or manual depreciation",
        "sec7b_period": "§ 7b EStG: only in the year of delivery and the three "
        "following years",
        "sec7b_limit": "Special depreciation {total} of {year} exceeds {percent} % "
        "of the cost (§ 7b EStG)",
        "exceeds_book_value": "Depreciation of {year} exceeds the book value",
        "subsidy_exceeds_book_value": "The subsidy of {amount} in {year} exceeds "
        "the book value {book_value}",
        "no_end": "The schedule does not end",
        "group_without_depreciation": "Group {group} has no depreciation",
    }

    def __init__(self, code, **params):
        self.code = code
        self.params = params
        super().__init__(self.MESSAGES[code].format(**params))


def cents(value):
    """Round to the cent, half up (spec 5.10)."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def month_index(day):
    """Months since year 0: the month of a date as a running number."""
    return day.year * 12 + day.month - 1


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """Event of an asset (spec 6.2, ``l10n_de_euer.asset.event``).

    ``useful_life_months`` is the remaining useful life counted from 1 January
    of the year of the event (new estimate after additional costs, a subsidy,
    an extraordinary depreciation or a change of the useful life).
    """

    type: str
    date: date
    amount: Decimal = ZERO
    useful_life_months: int = 0
    business_share: Decimal | None = None


@dataclass(frozen=True)
class Asset:
    """Input of the depreciation of one asset.

    ``cost`` is the cost of acquisition or production or the value of a
    contribution: net if the input tax is deductible, gross otherwise
    (R 9b (1) EStR), never with input tax under § 15 (1a) UStG.
    ``opening_date`` (31 December) and ``opening_book_value`` take over an
    asset from another system: years up to the opening date are not
    calculated again, so years already filed stay unchanged.
    ``disposal_effective_date`` is the date the disposal becomes an expense:
    the disposal date, for a sale of an asset that is not depreciable or of
    current assets the receipt of the proceeds.
    """

    kind: str
    method: str
    delivery_date: date
    cost: Decimal
    useful_life_months: int = 0
    degressive_rate: Decimal | None = None
    switch_to_linear_year: int | None = None
    events: tuple = ()
    disposal_date: date | None = None
    disposal_effective_date: date | None = None
    disposal_kind: str | None = None
    disposal_month_counts: bool = True
    memo_value: Decimal = ZERO
    opening_date: date | None = None
    opening_book_value: Decimal | None = None
    ev_rates: tuple = ()
    group: str = "other_movable"

    @property
    def first_year(self):
        if self.opening_date:
            return self.opening_date.year + 1
        return self.delivery_date.year

    @property
    def disposal_year(self):
        effective = self.disposal_effective_date or self.disposal_date
        return effective.year if effective else None


@dataclass(frozen=True)
class YearValues:
    """Values of one asset in one year (``l10n_de_euer.asset.year``)."""

    year: int
    cost_start: Decimal
    book_value_start: Decimal
    additions: Decimal
    special_depreciation: Decimal
    depreciation: Decimal
    disposal_book_value: Decimal
    book_value_end: Decimal
    months: int = 0

    def is_consistent(self):
        """BW end = BW start + additions − special − depreciation − disposals."""
        return self.book_value_end == (
            self.book_value_start
            + self.additions
            - self.special_depreciation
            - self.depreciation
            - self.disposal_book_value
        )


@dataclass(frozen=True)
class RegisterAsset:
    """An asset as part of the register of a business for one year.

    ``withdrawal_vat``: VAT on a withdrawal (§ 3 (1b) UStG, Kz 140),
    ``contribution_value``: value of a contribution (Kz 123),
    ``private_use``: confirmed use withdrawal ``(amount, vat)`` of the year
    (R-54, Kz 108/140/122),
    ``locked_values``: :class:`YearValues` of a locked year; they replace the
    calculation, so a finalised year keeps its values (spec 7.7).
    """

    asset: Asset
    home_office_deductible: bool = True
    electric: bool = False
    disposal_value: Decimal = ZERO
    withdrawal_vat: Decimal = ZERO
    contribution_value: Decimal = ZERO
    private_use: tuple = (ZERO, ZERO)
    locked_values: YearValues | None = None


@dataclass(frozen=True)
class Pool:
    """Asset pool of one year of formation (§ 6 (2a) EStG)."""

    year: int
    amount: Decimal


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def max_degressive_rate(factor, max_rate, useful_life_months):
    """Highest degressive rate: min(factor × linear rate, maximum rate).

    ``factor`` and ``max_rate`` are the parameters valid on the delivery date
    (spec 3.2, ``degressive_factor`` and ``degressive_max_rate``); a factor of
    0 is a gap in which degressive depreciation is not allowed.
    """
    if not factor or not max_rate or not useful_life_months:
        return ZERO
    linear = TWELVE / Decimal(useful_life_months)
    return min(Decimal(str(factor)) * linear, Decimal(str(max_rate)))


def validate(asset):
    """Raise :class:`DepreciationError` for an asset the rules do not allow."""
    if asset.kind not in KINDS:
        raise DepreciationError("kind", value=asset.kind)
    if asset.method not in METHODS:
        raise DepreciationError("method", value=asset.method)
    if asset.cost < 0:
        raise DepreciationError("negative_cost")
    if asset.memo_value and asset.memo_value >= asset.cost:
        raise DepreciationError("memo")
    if asset.kind == "depreciable" and asset.method == "none":
        raise DepreciationError("no_method")
    if asset.kind != "depreciable" and asset.method != "none":
        raise DepreciationError("method_not_allowed")
    _validate_method(asset)
    if asset.method == "ev_7_2a" and not asset.ev_rates:
        raise DepreciationError("no_ev_rates")
    if asset.disposal_kind and asset.disposal_kind not in DISPOSAL_KINDS:
        raise DepreciationError("disposal_kind", value=asset.disposal_kind)
    if asset.opening_date and (
        asset.opening_date.month != 12 or asset.opening_date.day != 31
    ):
        raise DepreciationError("takeover_date")
    if asset.opening_date and asset.opening_date < asset.delivery_date:
        raise DepreciationError("takeover_before_delivery")
    effective = asset.disposal_effective_date or asset.disposal_date
    if effective and effective < asset.delivery_date:
        raise DepreciationError("disposal_before_delivery")
    if effective and asset.opening_date and effective <= asset.opening_date:
        raise DepreciationError("disposal_before_takeover")
    for event in asset.events:
        _validate_event(asset, event)


def _validate_method(asset):
    if asset.method in ("linear", "degressive"):
        if asset.useful_life_months <= 0:
            raise DepreciationError("no_life")
        if asset.useful_life_months <= SHORT_LIFE_MONTHS:
            raise DepreciationError("short_life")
    if asset.method == "degressive" and not asset.degressive_rate:
        raise DepreciationError("no_rate")
    if (
        asset.method == "degressive"
        and asset.switch_to_linear_year
        and asset.switch_to_linear_year <= asset.delivery_date.year
    ):
        # the switch takes effect from the beginning of a year (§ 7 (3)
        # sentence 2 EStG); in the year of delivery it is linear from the start
        raise DepreciationError("switch_year", year=asset.delivery_date.year + 1)


def event_year(asset, event):
    """Year in which ``event`` takes effect.

    A subsidy approved before the delivery reduces the addition in the year
    of delivery (spec 6.3); every other event takes effect in its own year.
    """
    return max(event.date.year, asset.delivery_date.year)


def _validate_event(asset, event):
    if event.type not in EVENT_TYPES:
        raise DepreciationError("event_type", value=event.type)
    if event.date < asset.delivery_date and event.type != "subsidy":
        raise DepreciationError("event_before_delivery")
    late = event_year(asset, event) > asset.delivery_date.year
    if event.type in DEPRECIABLE_EVENTS and asset.kind != "depreciable":
        raise DepreciationError("event_kind")
    if event.type == "extraordinary" and asset.method == "degressive":
        raise DepreciationError("extraordinary_degressive")
    if event.type == "sec7g_special":
        _validate_special(asset, event)
    if event.type == "sec7b_special":
        _validate_sec7b(asset, event)
    if asset.method == "ev_7_2a" and late and event.type != "business_use_change":
        raise DepreciationError("ev_events")
    if event.type == "useful_life_change" and not late:
        raise DepreciationError("life_change_first_year")
    if event.type == "useful_life_change" and event.useful_life_months <= 0:
        raise DepreciationError("life_change_months")
    if event.type == "manual_depreciation" and asset.method != "manual":
        raise DepreciationError("manual_method")
    if event.type in AMOUNT_EVENTS and event.amount <= 0:
        raise DepreciationError("event_amount")
    if event.useful_life_months < 0:
        raise DepreciationError("event_life")


def _validate_special(asset, event):
    if asset.group not in MOVABLE_GROUPS:
        raise DepreciationError("special_movable")
    if asset.method not in ("linear", "degressive"):
        raise DepreciationError("special_method")
    period = SPECIAL_EVENTS["sec7g_special"]
    if not 0 <= event.date.year - asset.delivery_date.year < period:
        raise DepreciationError("special_period")


def _validate_sec7b(asset, event):
    """§ 7b (1) EStG: buildings, next to § 7 (4) or (5a), four years."""
    if asset.group != "building":
        raise DepreciationError("sec7b_group")
    if asset.method not in ("linear", "manual"):
        raise DepreciationError("sec7b_method")
    period = SPECIAL_EVENTS["sec7b_special"]
    if not 0 <= event.date.year - asset.delivery_date.year < period:
        raise DepreciationError("sec7b_period")


def basis(asset, year):
    """Cost of ``asset`` with the additional costs and reductions up to ``year``.

    Basis of the special depreciation and, within its period, of the
    depreciation (§ 7a (1) EStG; R 7a (3), (4) EStR: from the beginning of the
    year of the change).
    """
    total = asset.cost
    for event in asset.events:
        if event.type in BASIS_EVENTS and event_year(asset, event) <= year:
            total += event.amount if event.type == "additional_cost" else -event.amount
    return total


def _special_by_year(asset, event_type):
    amounts = {}
    for event in asset.events:
        if event.type == event_type:
            year = event.date.year
            amounts[year] = amounts.get(year, ZERO) + event.amount
    return dict(sorted(amounts.items()))


def check_special_limit(asset, rate):
    """Raise if the special depreciation exceeds ``rate`` of the cost (§ 7g (5)).

    The limit applies to the sum up to each year in which special
    depreciation is taken, measured on the cost with the additional costs and
    reductions up to that year (§ 7a (1) EStG, example in H 7a EStH).
    """
    rate = Decimal(str(rate))
    total = ZERO
    for year, amount in _special_by_year(asset, "sec7g_special").items():
        total += amount
        if amount and total > cents(basis(asset, year) * rate):
            raise DepreciationError(
                "special_limit", total=total, percent=f"{rate * 100:.0f}"
            )


def check_sec7b_limit(asset, rate):
    """Raise if the special depreciation of a year exceeds ``rate`` (§ 7b (1)).

    ``rate`` applies to the cost of the year; the limits of § 7b (3) EStG per
    square metre are the user's responsibility (reason of the event).
    """
    rate = Decimal(str(rate))
    for year, amount in _special_by_year(asset, "sec7b_special").items():
        if amount > cents(basis(asset, year) * rate):
            raise DepreciationError(
                "sec7b_limit", total=amount, year=year, percent=f"{rate * 100:.0f}"
            )


# ---------------------------------------------------------------------------
# Schedule of one asset
# ---------------------------------------------------------------------------


class _Run:
    """Year by year calculation of one asset."""

    def __init__(self, asset):
        self.asset = asset
        self.events = sorted(asset.events, key=lambda event: (event.date, event.type))
        self.mode = asset.method
        self.annual = None  # linear annual amount J
        self.rebase = False  # J = book value ÷ remaining life (R 7.4 (9))
        self.rebase_cost = False  # J = basis of the year ÷ useful life (§ 7a (1))
        # § 7 (4) sentence 1 EStG: J is a percentage of the basis; ``rate``
        # ends when the book value is spread over a remaining useful life
        self.rate = asset.method == "linear" and asset.group in BUILDING_GROUPS
        self.rate_changed = False  # basis changed: the end follows the book value
        self.extraordinary = ZERO  # AfaA of earlier years (§ 11c (2) EStDV)
        self.special_end = self._special_end()
        delivery = month_index(asset.delivery_date)
        life = asset.useful_life_months
        self.end_index = delivery + life - 1 if life else None
        if asset.opening_date:
            self.book_value = asset.opening_book_value or ZERO
            self.cost_present = basis(asset, asset.first_year - 1)
            self._init_takeover()
        else:
            self.book_value = ZERO
            self.cost_present = ZERO

    # ---------------------------------------------------------------- helpers
    def _year_of(self, event):
        return event_year(self.asset, event)

    def _events(self, year, *types):
        return [
            event
            for event in self.events
            if self._year_of(event) == year and (not types or event.type in types)
        ]

    def _basis_changes(self, year):
        total = ZERO
        for event in self._events(year, *BASIS_EVENTS):
            sign = 1 if event.type == "additional_cost" else -1
            total += sign * event.amount
        return total

    def _special_end(self):
        """First year after the period of the special depreciation, or None.

        The period of benefit (§ 7a (1) EStG) matters only if special
        depreciation is taken in it; it is derived from the events of all
        years, so a takeover knows it as well.
        """
        periods = [
            SPECIAL_EVENTS[event.type]
            for event in self.events
            if event.type in SPECIAL_EVENTS and event.amount
        ]
        if not periods:
            return None
        return self.asset.delivery_date.year + max(periods)

    def _in_special_period(self, year):
        return self.special_end is not None and year < self.special_end

    def _init_takeover(self):
        """Continue a takeover from the book value at the takeover date.

        The years up to the takeover date are not calculated again, but their
        events decide how the depreciation continues: an extraordinary
        depreciation, a new remaining useful life or an additional cost after
        the year of delivery spread the book value over the remaining useful
        life (R 7.4 (9) EStR), as does the end of the period of a special
        depreciation (§ 7a (9) EStG). Within that period the depreciation is
        measured on the cost with its changes (§ 7a (1) EStG), otherwise on the
        cost of the year of delivery. A building under § 7 (4) sentence 1 EStG
        keeps its percentage of the basis with the changes (H 7.4 EStH,
        § 11c (2) EStDV).
        """
        asset = self.asset
        first = self.first
        rebase = self.special_end is not None and self.special_end <= first
        for event in self.events:
            year = self._year_of(event)
            if year >= first:
                continue
            late = year > asset.delivery_date.year
            if event.type == "extraordinary":
                if self.rate:
                    self.extraordinary += event.amount
                    self.rate_changed = True
                else:
                    rebase = True
            elif event.type == "useful_life_change" or (
                event.type in BASIS_EVENTS and late
            ):
                if event.useful_life_months:
                    self.end_index = year * 12 + event.useful_life_months - 1
                if self._in_special_period(year) and event.type in BASIS_EVENTS:
                    self.rate_changed = self.rate_changed or self.rate
                elif self._keeps_rate(event):
                    self.rate_changed = True
                else:
                    rebase = True
        if rebase:
            self.rate = False
        if self.mode == "degressive":
            if asset.switch_to_linear_year and asset.switch_to_linear_year <= first:
                self.mode = "linear"
                self.rebase = True
        elif self.mode == "linear":
            if rebase:
                self.rebase = True
            else:
                self.annual = cents(self._basis() * TWELVE / asset.useful_life_months)

    def _keeps_rate(self, event):
        """Whether ``event`` of a later year keeps the percentage of a building:
        a change of the basis without a new remaining useful life."""
        return self.rate and event.type in BASIS_EVENTS and not event.useful_life_months

    def _basis(self):
        """Basis of the linear amount: cost with its changes, for a building
        less the extraordinary depreciation of earlier years (§ 11c (2) EStDV)."""
        return self.cost_present - self.extraordinary

    @property
    def first(self):
        return self.asset.first_year

    def _remaining_months(self, year):
        if self.end_index is None:
            return 0
        return max(self.end_index - year * 12 + 1, 1)

    def _months(self, year):
        """Months of depreciation in ``year`` and whether it is the last year."""
        asset = self.asset
        delivery_year = asset.delivery_date.year == year and not asset.opening_date
        start = asset.delivery_date.month if delivery_year else 1
        if self.mode in ("ev_7_2a", "digital_1y"):
            start = 1
        end = 12
        last = False
        # a building whose basis changed depreciates its percentage until the
        # book value is used up, also after the original useful life (H 7.4)
        open_end = self.rate and self.rate_changed
        if (
            self.end_index is not None
            and self.end_index <= year * 12 + 11
            and not open_end
        ):
            end = self.end_index - year * 12 + 1
            last = True
        if asset.disposal_year == year and asset.kind == "depreciable":
            month = asset.disposal_date.month
            month = month if asset.disposal_month_counts else month - 1
            if month < end:
                end, last = month, False
        return max(end - start + 1, 0), last

    # ---------------------------------------------------------------- per year
    def year(self, year):
        asset = self.asset
        cost_start = self.cost_present
        start = self.book_value
        additions = self._basis_changes(year)
        if year == asset.delivery_date.year and not asset.opening_date:
            additions += asset.cost
        if self.book_value + additions < 0:
            # a subsidy reduces the cost at most to zero; the rest is business
            # income (R 6.5 (2), (3) EStR)
            subsidies = sum(
                (event.amount for event in self._events(year, "subsidy")), ZERO
            )
            raise DepreciationError(
                "subsidy_exceeds_book_value",
                year=year,
                amount=subsidies,
                book_value=self.book_value + additions + subsidies,
            )
        self.book_value += additions
        self.cost_present += additions
        self._apply_life_events(year)
        special = sum(
            (event.amount for event in self._events(year, *SPECIAL_EVENTS)), ZERO
        )
        months, last = self._months(year)
        depreciation = self._depreciation(year, months, last, special)
        depreciation += self._extraordinary(year, special + depreciation)
        if special + depreciation > self.book_value:
            raise DepreciationError("exceeds_book_value", year=year)
        self.book_value -= special + depreciation
        disposal = ZERO
        if asset.disposal_year == year:
            disposal = self.book_value
            self.book_value = ZERO
            self.cost_present = ZERO
        return YearValues(
            year=year,
            cost_start=cost_start,
            book_value_start=start,
            additions=additions,
            special_depreciation=special,
            depreciation=depreciation,
            disposal_book_value=disposal,
            book_value_end=self.book_value,
            months=months if self.mode in DEPRECIATING_METHODS else 0,
        )

    def _apply_life_events(self, year):
        """New basis at the beginning of ``year`` (R 7.4 (9) EStR, § 7a EStG)."""
        late = year > self.asset.delivery_date.year
        for event in self._events(year, *BASIS_EVENTS, "useful_life_change"):
            if not late:
                continue
            if event.useful_life_months:
                self.end_index = year * 12 + event.useful_life_months - 1
            if event.type in BASIS_EVENTS and self._in_special_period(year):
                # § 7a (1): within the period of a special depreciation the
                # depreciation is measured on the cost with the change, as if
                # it happened on 1 January (R 7a (3), (4) EStR)
                self.rebase_cost = True
                self.rate_changed = self.rate_changed or self.rate
            elif self._keeps_rate(event):
                # § 7 (4) sentence 1: previous basis with the change × the
                # percentage, from 1 January (H 7.4, R 7.3 (4), R 7.4 (9) s. 3)
                self.rebase_cost = True
                self.rate_changed = True
            else:
                self.rebase = True
                self.rate = False
        if year == self.special_end and self.mode == "linear":
            # end of the period: book value ÷ remaining useful life (§ 7a (9));
            # a degressive depreciation may continue (R 7a (10) sentence 3)
            self.rebase = True
            self.rate = False

    def _depreciation(self, year, months, last, special):
        mode = self.mode
        if mode == "degressive":
            self._maybe_switch(year)
            mode = self.mode
        available = max(self.book_value - special - self.asset.memo_value, ZERO)
        if mode == "linear":
            return self._linear(year, months, last, available)
        if mode == "degressive":
            if last:
                return available
            amount = cents(self.book_value * self.asset.degressive_rate * months / 12)
            return min(amount, available)
        if mode == "ev_7_2a":
            return self._electric(year, months, last, available)
        if mode == "digital_1y":
            return available
        if mode == "manual":
            amount = sum(
                (event.amount for event in self._events(year, "manual_depreciation")),
                ZERO,
            )
            if amount > available:
                raise DepreciationError("exceeds_book_value", year=year)
            return amount
        return ZERO

    def _linear(self, year, months, last, available):
        life = self.asset.useful_life_months
        if self.rebase and year > self.asset.delivery_date.year:
            remaining = self._remaining_months(year)
            self.annual = cents(self.book_value * TWELVE / remaining)
        elif self.rebase_cost:
            self.annual = cents(self._basis() * TWELVE / life)
        elif self.annual is None or self.rebase:
            self.annual = cents(self.book_value * TWELVE / life)
        self.rebase = self.rebase_cost = False
        if last or (self.rate and self.rate_changed and self.annual <= 0):
            return available
        return min(cents(self.annual * months / 12), available)

    def _maybe_switch(self, year):
        """Switch from degressive to linear (§ 7 (3) EStG)."""
        asset = self.asset
        if year <= asset.delivery_date.year:
            return
        chosen = asset.switch_to_linear_year
        if chosen:
            switch = year >= chosen
        else:
            remaining = self._remaining_months(year)
            linear = self.book_value * TWELVE / remaining
            switch = linear >= self.book_value * asset.degressive_rate
        if switch:
            self.mode = "linear"
            self.rebase = True

    def _electric(self, year, months, last, available):
        """§ 7 (2a): percentages of the cost including the changes of the year
        of delivery (later events are not allowed)."""
        asset = self.asset
        rates = asset.ev_rates
        index = year - asset.delivery_date.year
        if index >= len(rates):
            return available
        cost = basis(asset, asset.delivery_date.year)
        amount = cents(cost * Decimal(str(rates[index])))
        if months < 12 and not last:
            # year of a disposal, also in the last year of the scale (R 7.4 (8))
            return min(cents(amount * months / 12), available)
        if index == len(rates) - 1:
            return available
        return min(amount, available)

    def _extraordinary(self, year, taken):
        amount = sum(
            (event.amount for event in self._events(year, "extraordinary")), ZERO
        )
        if amount:
            if amount > self.book_value - taken:
                raise DepreciationError("exceeds_book_value", year=year)
            if self.rate and self.mode == "linear":
                # § 11c (2) EStDV: from the following year the percentage of
                # the cost less the extraordinary depreciation
                self.extraordinary += amount
                self.rebase_cost = True
                self.rate_changed = True
            else:
                self.rebase = True
        return amount

    def finished(self, year):
        """Whether nothing changes any more after ``year``."""
        asset = self.asset
        if asset.disposal_year is not None:
            return year >= asset.disposal_year
        later_events = any(self._year_of(event) > year for event in self.events)
        if later_events:
            return False
        if asset.method in DEPRECIATING_METHODS - {"manual"}:
            return self.book_value <= asset.memo_value
        return True


def schedule(asset, until=None):
    """Return the :class:`YearValues` of ``asset`` from its first year.

    Without ``until`` the schedule ends with the year after which nothing
    changes any more (end of depreciation, disposal, last event). With
    ``until`` it runs up to that year as long as the asset is present, so the
    cost column of Anlage AVEÜR also covers fully depreciated assets. Low-value
    assets and items up to 250 € have a single year; pool assets are
    calculated per pool (:func:`pool_releases`).
    """
    validate(asset)
    if asset.kind == "pool":
        return []
    if asset.kind in IMMEDIATE_KINDS:
        year = asset.delivery_date.year
        if asset.opening_date or (until is not None and until < year):
            return []
        return [
            YearValues(
                year=year,
                cost_start=ZERO,
                book_value_start=ZERO,
                additions=asset.cost,
                special_depreciation=ZERO,
                depreciation=asset.cost,
                disposal_book_value=ZERO,
                book_value_end=ZERO,
                months=0,
            )
        ]
    run = _Run(asset)
    result = []
    year = asset.first_year
    while until is None or year <= until:
        if asset.disposal_year is not None and year > asset.disposal_year:
            break
        result.append(run.year(year))
        if until is None and run.finished(year):
            break
        year += 1
        if year > asset.first_year + 200:  # pragma: no cover
            raise DepreciationError("no_end")
    return result


def year_values(asset, year):
    """Values of ``asset`` in ``year`` or ``None`` if it is not present."""
    if asset.kind == "pool" or year < asset.first_year:
        return None
    rows = schedule(asset, until=year)
    return rows[-1] if rows and rows[-1].year == year else None


# ---------------------------------------------------------------------------
# Pools, contributions, private use, § 15a
# ---------------------------------------------------------------------------


def pool_releases(amount):
    """Releases of a pool: one fifth per year, the fifth year takes the rest."""
    regular = cents(Decimal(amount) / POOL_YEARS)
    releases = [regular] * (POOL_YEARS - 1)
    releases.append(cents(Decimal(amount) - sum(releases, ZERO)))
    return releases


def pool_year(pool, year):
    """``(book value at the beginning, release)`` of a pool in ``year``."""
    offset = year - pool.year
    if not 0 <= offset < POOL_YEARS:
        return None
    releases = pool_releases(pool.amount)
    start = cents(pool.amount - sum(releases[:offset], ZERO))
    return start, releases[offset]


def contribution_value(
    fair_value,
    private_cost=None,
    private_date=None,
    contribution_date=None,
    private_life_months=0,
):
    """Value of a contribution (§ 6 (1) no. 5 EStG, R-38).

    The fair value (Teilwert), at most the cost reduced by the depreciation
    for the time between acquisition and contribution, if the asset was
    acquired within the last three years before the contribution.
    """
    fair_value = cents(fair_value)
    if private_cost is None or not contribution_reduced(
        private_date, contribution_date
    ):
        return fair_value
    cost = cents(private_cost)
    if private_life_months:
        months = max(month_index(contribution_date) - month_index(private_date), 0)
        used = min(cents(cost * months / private_life_months), cost)
        cost -= used
    return min(fair_value, cost)


def _years_before(day, years):
    """The same day ``years`` earlier (28 February for 29 February)."""
    if day.month == 2 and day.day == 29:
        return date(day.year - years, 2, 28)
    return date(day.year - years, day.month, day.day)


def contribution_reduced(private_date, contribution_date):
    """Whether the value of a contribution is limited to the reduced cost:
    acquired within the last three years (§ 6 (1) no. 5 sentence 1 (a) EStG)."""
    if not private_date or not contribution_date:
        return False
    return private_date > _years_before(contribution_date, 3)


def contribution_remaining_life(private_date, contribution_date, total_months):
    """Remaining useful life of a contributed asset in months (R 7.4 (10) EStR).

    The total useful life less the months of private use from the month of
    the acquisition up to the month before the contribution; at least 0.
    """
    if not private_date or not contribution_date or not total_months:
        return 0
    used = max(month_index(contribution_date) - month_index(private_date), 0)
    return max(total_months - used, 0)


def private_use_proposal(depreciation, costs, business_share, vat_base, vat_rate):
    """Use withdrawal for the private use of an asset (R-54, spec 6.3).

    ``(depreciation + running costs) × (1 − business share)`` → Kz 108; VAT
    only on expenses with input tax deduction (``vat_base``, § 10 (4)
    sentence 1 no. 2 UStG) → Kz 140. Returns ``(amount, vat)``.
    """
    private = Decimal(1) - Decimal(str(business_share))
    if private <= 0:
        return ZERO, ZERO
    amount = cents((Decimal(depreciation) + Decimal(costs)) * private)
    vat = cents(cents(Decimal(vat_base) * private) * Decimal(str(vat_rate)))
    return amount, vat


def private_use_vat_base(
    cost, delivery_date, year, real_estate=False, useful_life_months=0
):
    """Share of the cost in the VAT basis of a use withdrawal in ``year``.

    § 10 (4) sentence 1 no. 2 sentences 2 and 3 UStG: a cost of at least
    500 € is spread evenly over the § 15a period (5 years, 10 for real
    estate, a shorter useful life if the asset is used for less, § 15a (5)
    sentence 2 UStG) from the month of delivery; a lower cost counts in the
    year of delivery. Independent of the depreciation for income tax. The
    shares of all years add up to the cost.
    """
    cost = Decimal(cost)
    if cost <= 0:
        return ZERO
    if cost < VAT_SPREAD_LIMIT:
        return cost if year == delivery_date.year else ZERO
    period = (10 if real_estate else POOL_YEARS) * 12
    if useful_life_months and useful_life_months < period:
        period = useful_life_months
    first = month_index(delivery_date)
    end = first + period

    def share(index):
        months = min(max(index - first, 0), period)
        return cents(cost * months / period)

    return share(min((year + 1) * 12, end)) - share(max(year * 12, first))


def sec15a_correction(input_tax, delivery_date, change_date, real_estate=False):
    """Formula help for the input tax correction of § 15a UStG (spec 6.3).

    For a change between small business and standard taxation on
    ``change_date``: the correction period is 5 years (10 for real estate)
    from the first use; each remaining month of the period carries
    ``input_tax / months of the period``. Returns ``{year: amount}`` and
    whether the bagatelle limit of § 44 (1) UStDV (input tax up to 1,000 €)
    applies, in which case there is no correction. The cost of the asset
    stays unchanged (§ 9b (2) EStG); the correction affects the EÜR through
    the VAT payment or refund (Kz 186/141).
    """
    period = (10 if real_estate else POOL_YEARS) * 12
    first = month_index(delivery_date)
    change = month_index(change_date)
    end = first + period
    if change >= end or input_tax <= 0:
        return {}, Decimal(input_tax) <= SEC15A_BAGATELLE
    per_month = Decimal(input_tax) / period
    result = {}
    for index in range(max(change, first), end):
        year = index // 12
        result[year] = result.get(year, ZERO) + per_month
    rounded = {year: cents(amount) for year, amount in result.items()}
    total = cents(per_month * (end - max(change, first)))
    last = max(rounded)
    rounded[last] += total - sum(rounded.values(), ZERO)
    return rounded, Decimal(input_tax) <= SEC15A_BAGATELLE


# ---------------------------------------------------------------------------
# Field contributions (phase F, spec 5.8) and Anlage AVEÜR (spec 6.4)
# ---------------------------------------------------------------------------


def _add(result, code, amount):
    if amount:
        result[code] = result.get(code, ZERO) + amount


def entry_values(entry, year):
    """Values of a register entry in ``year``: locked values if there are."""
    locked = entry.locked_values
    if locked is not None and locked.year == year:
        return locked
    return year_values(entry.asset, year)


def register_values(year, entries):
    """``[(entry, values of year)]`` of the entries present in ``year``."""
    rows = []
    for entry in entries:
        values = entry_values(entry, year)
        if values is not None:
            rows.append((entry, values))
    return rows


def aveuer_group_values(rows):
    """Sum of the AVEÜR columns per group: ``{group: [six amounts]}``.

    ``rows`` come from :func:`register_values`. Low-value assets and pool
    assets are not entered in Anlage AVEÜR.
    """
    groups = {}
    for entry, values in rows:
        if entry.asset.kind not in AVEUER_KINDS:
            continue
        sums = groups.setdefault(entry.asset.group, [ZERO] * 6)
        columns = (
            values.cost_start,
            values.book_value_start,
            values.additions,
            values.special_depreciation,
            values.depreciation,
            values.disposal_book_value,
        )
        for index, amount in enumerate(columns):
            sums[index] += amount
    return groups


def field_contributions(year, entries, pools=()):
    """Field contributions of the asset register for ``year`` (phase F).

    Returns ``{code: Decimal}`` for input fields only: the columns of
    Anlage AVEÜR (the transfers into Anlage EÜR such as Kz 130, 131, 134,
    135, 136 and 137 are formulas of the form data, spec F-04), low-value
    assets (Kz 132), disposals of current assets (Kz 100), depreciation of a
    home office (Kz 172 or 162), withdrawals of assets (Kz 102, 140, 122),
    contributions (Kz 123) and confirmed use withdrawals (Kz 108, 140, 122).
    Zero amounts are left out (R-46).
    """
    result = {}
    rows = register_values(year, entries)
    for group, sums in aveuer_group_values(rows).items():
        kz = GROUP_KZ[group]
        if group == "current":
            sums[0] = sums[1]
        for index, amount in enumerate(sums):
            if kz[index] is not None:
                _add(result, AVEUER + kz[index], amount)
            elif index in (3, 4) and amount:
                raise DepreciationError("group_without_depreciation", group=group)
        if group == "current":
            _add(result, FIELD_GOODS, sums[5])
    for entry, values in rows:
        if entry.asset.group == "office_building" and values.depreciation:
            code = (
                FIELD_HOME_OFFICE
                if entry.home_office_deductible
                else FIELD_HOME_OFFICE_ND
            )
            _add(result, code, values.depreciation)
    for entry in entries:
        _entry_contributions(result, entry, year)
    for pool in pools:
        _pool_contributions(result, pool, year)
    return {code: amount for code, amount in result.items() if amount}


def _entry_contributions(result, entry, year):
    asset = entry.asset
    if asset.kind in IMMEDIATE_KINDS and not asset.opening_date:
        if asset.delivery_date.year == year:
            locked = entry.locked_values
            amount = (
                locked.additions
                if locked is not None and locked.year == year
                else asset.cost
            )
            _add(result, FIELD_LOW_VALUE, amount)
    if entry.contribution_value and asset.delivery_date.year == year:
        _add(result, FIELD_CONTRIBUTIONS, entry.contribution_value)
    if asset.disposal_kind == "withdrawal" and asset.disposal_date.year == year:
        # current assets: withdrawal of goods (Kz 108, Anleitung Zeile 21),
        # assets of the fixed assets: Kz 102 (Zeile 19)
        revenue = (
            FIELD_WITHDRAWAL_IN_KIND
            if asset.kind == "current_4_3_4"
            else FIELD_DISPOSAL_REVENUE
        )
        _add(result, revenue, entry.disposal_value)
        _add(result, FIELD_VAT, entry.withdrawal_vat)
        _add(result, FIELD_WITHDRAWALS, entry.disposal_value + entry.withdrawal_vat)
    amount, vat = entry.private_use
    if amount or vat:
        _add(result, FIELD_WITHDRAWAL_IN_KIND, amount)
        _add(result, FIELD_VAT, vat)
        _add(result, FIELD_WITHDRAWALS, amount + vat)


def _pool_contributions(result, pool, year):
    values = pool_year(pool, year)
    if values is None:
        return
    start, release = values
    kz = POOL_KZ[pool.year - year]
    _add(result, AVEUER + kz[0], pool.amount)
    if kz[1] is not None:
        _add(result, AVEUER + kz[1], start)
    _add(result, AVEUER + kz[2], release)


def electric_vehicle_present(year, entries):
    """Kz 407: at least one electric or plug-in hybrid vehicle is present."""
    return any(
        entry.electric and entry.asset.group == "vehicle"
        for entry, _values in register_values(year, entries)
    )


def transfer_differences(values):
    """Differences of the transfers AVEÜR → EÜR (spec F-04, check C-62).

    ``values`` are the calculated form values (code → Decimal or None).
    Returns ``{eür code: (eür value, sum of the AVEÜR fields)}`` for every
    transfer that does not match.
    """
    differences = {}
    for code, sources in TRANSFERS.items():
        target = values.get(code) or ZERO
        total = sum((values.get(source) or ZERO for source in sources), ZERO)
        if target != total:
            differences[code] = (target, total)
    return differences


def total_check(entry_years, cost_total, memo_value=ZERO, accumulated=ZERO):
    """Whether the depreciation over the life equals the cost (check C-67).

    ``entry_years`` are all :class:`YearValues` of a fully depreciated asset,
    ``cost_total`` its cost including additions and minus reductions,
    ``accumulated`` the depreciation taken over from another system.
    """
    taken = accumulated + sum(
        (values.depreciation + values.special_depreciation for values in entry_years),
        ZERO,
    )
    return taken == cost_total - memo_value
