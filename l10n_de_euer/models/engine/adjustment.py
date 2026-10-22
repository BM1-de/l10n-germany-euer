# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Profit corrections without bookings (phase F, spec 5.8, R-44).

Pure Python: the field amounts of one profit correction from its inputs and
the rule parameters of the year (:class:`.parameters.Parameters`). The codes
are the stable field codes (``EUER.<box>.<Kz>``, spec 3.1); the line and Kz
of the year come from the form data.

Effect on withdrawals and contributions (Kz 29.122/29.123, R-18), computed
here and never entered by hand:

* withdrawals in kind, private use of vehicles and VAT on them are
  withdrawals at their value plus VAT (Anleitung, lines 107/108 with line 19);
* the use of a private vehicle for business trips (Kz 147) is a
  contribution of the use;
* flat rates for expenses the business did not pay (daily home office
  allowance, home office, additional meal expenses, commuting allowance
  without actual costs in the books) are expenses paid privately: a
  contribution of the deductible amount (R-17). Commuting with actual costs
  booked in the business (Kz 142) is no contribution;
* the flat rate of business expenses (Kz 195), income in kind and tax-free
  income and § 3c expenses (G.240–G.245) have no effect.

Yearly maximums (daily home office allowance, yearly flat rate of a home
office, flat rate of business expenses per group, commuting allowance
without an own vehicle) hold for the year, not for each correction: the
corrections of a year share them in their order (:func:`limits_used`).

Small businesses (§ 19 UStG) have no VAT on free supplies or income in
kind: their supplies are tax-free (§ 19 (1) UStG new version). A free supply
is part of the total turnover of the small business limit only if it is
taxable, at its VAT base (``vat_base``, § 10 (4) UStG), never at the value
for income tax (R-27, UStAE 19.2 (2)).
"""

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal("0.00")
CENT = Decimal("0.01")

CAR_PRIVATE_USE = "car_private_use"
WITHDRAWAL_IN_KIND = "withdrawal_in_kind"
WITHDRAWAL_VAT = "withdrawal_vat"
USE_CONTRIBUTION = "use_contribution"
COMMUTING = "commuting"
HOME_OFFICE_DAYS = "home_office_days"
HOME_OFFICE_ROOM = "home_office_room"
MEALS = "meals"
FLAT_RATE = "flat_rate"
TAX_FREE = "tax_free"
INCOME_IN_KIND = "income_in_kind"

KINDS = (
    CAR_PRIVATE_USE,
    WITHDRAWAL_IN_KIND,
    WITHDRAWAL_VAT,
    USE_CONTRIBUTION,
    COMMUTING,
    HOME_OFFICE_DAYS,
    HOME_OFFICE_ROOM,
    MEALS,
    FLAT_RATE,
    TAX_FREE,
    INCOME_IN_KIND,
)

FIELD_CAR = "EUER.20.106"
FIELD_IN_KIND = "EUER.20.108"
FIELD_VAT = "EUER.20.140"
FIELD_USE_CONTRIBUTION = "EUER.25.147"
FIELD_COMMUTING_COSTS = "EUER.25.142"
FIELD_COMMUTING = "EUER.25.176"
FIELD_HOME_OFFICE_DAYS = "EUER.25.163"
FIELD_HOME_OFFICE = "EUER.25.172"
FIELD_HOME_OFFICE_ND = "EUER.25.162"
FIELD_MEALS = "EUER.25.171"
FIELD_FLAT_RATE = "EUER.25.195"
FIELD_KU = "EUER.20.111"
FIELD_TAXABLE = "EUER.20.112"
FIELD_EXEMPT = "EUER.20.103"
#: fields of income in kind under standard taxation (taxable, tax-free)
REVENUE_FIELDS = (FIELD_TAXABLE, FIELD_EXEMPT)
FIELD_WITHDRAWALS = "EUER.29.122"
FIELD_CONTRIBUTIONS = "EUER.29.123"
TAX_FREE_FIELDS = (
    "EUER.G.240",
    "EUER.G.241",
    "EUER.G.242",
    "EUER.G.243",
    "EUER.G.244",
    "EUER.G.245",
)
#: fields of each kind, in the order of the form (resolved per year)
KIND_FIELDS = {
    CAR_PRIVATE_USE: (FIELD_CAR, FIELD_VAT),
    WITHDRAWAL_IN_KIND: (FIELD_IN_KIND, FIELD_VAT),
    WITHDRAWAL_VAT: (FIELD_VAT,),
    USE_CONTRIBUTION: (FIELD_USE_CONTRIBUTION,),
    COMMUTING: (FIELD_COMMUTING_COSTS, FIELD_COMMUTING),
    HOME_OFFICE_DAYS: (FIELD_HOME_OFFICE_DAYS,),
    HOME_OFFICE_ROOM: (FIELD_HOME_OFFICE_ND, FIELD_HOME_OFFICE),
    MEALS: (FIELD_MEALS,),
    FLAT_RATE: (FIELD_FLAT_RATE,),
    TAX_FREE: TAX_FREE_FIELDS,
    INCOME_IN_KIND: (FIELD_EXEMPT, FIELD_KU, FIELD_TAXABLE, FIELD_VAT),
}
#: kinds with VAT on the value; a small business has none (§ 19 (1) UStG)
VAT_KINDS = (CAR_PRIVATE_USE, WITHDRAWAL_IN_KIND, INCOME_IN_KIND)
#: free supplies (unentgeltliche Wertabgaben) with a VAT base (§ 10 (4) UStG)
FREE_SUPPLY_KINDS = (CAR_PRIVATE_USE, WITHDRAWAL_IN_KIND)
#: tax-free income of § 3 no. 26, 26a, 26b EStG (limited per year)
FIELD_TAX_FREE_26 = "EUER.G.240"

#: flat rates of business expenses (BMF 06.04.2023, BStBl I S. 671 and 669)
FLAT_WRITER = "writer"
FLAT_SIDE_ACTIVITY = "side_activity"
FLAT_CHILDCARE = "childcare"
FLAT_KINDS = (FLAT_WRITER, FLAT_SIDE_ACTIVITY, FLAT_CHILDCARE)
#: (rate parameter, maximum parameter) of the flat rates in percent
FLAT_PARAMETERS = {
    FLAT_WRITER: ("flat_writer_rate", "flat_writer_max"),
    FLAT_SIDE_ACTIVITY: ("flat_side_rate", "flat_side_max"),
}
FULL_CARE_HOURS = 40


class AdjustmentError(ValueError):
    """Inputs of a profit correction that cannot be calculated.

    ``key`` names the case for the message, ``params`` its values.
    """

    def __init__(self, key, **params):
        super().__init__(key)
        self.key = key
        self.params = params


@dataclass(frozen=True, kw_only=True)
class AdjustmentInput:
    """Inputs of one profit correction (``l10n_de_euer.adjustment``).

    Amounts are ``Decimal`` in euro. Only the inputs of the kind are used.
    """

    kind: str
    year: int
    amount: Decimal = ZERO
    vat: Decimal = ZERO
    days: int = 0
    days_8h: int = 0
    days_24h: int = 0
    distance_km: Decimal = ZERO
    km: Decimal = ZERO
    months: int = 12
    costs: Decimal = ZERO
    revenue: Decimal = ZERO
    other_amount: Decimal = ZERO
    reduction: Decimal = ZERO
    child_months: Decimal = ZERO
    hours_per_week: Decimal = Decimal(FULL_CARE_HOURS)
    free_place_months: Decimal = ZERO
    method: str = "flat"
    center_of_activity: bool = False
    double_household: bool = False
    own_car: bool = False
    flat_kind: str = ""
    tax_free_field: str = ""
    vat_base: Decimal = ZERO
    revenue_field: str = ""
    small_business: bool = False


@dataclass(frozen=True, kw_only=True)
class AdjustmentResult:
    """Field amounts of a profit correction.

    ``fields``: code → ``Decimal`` without the effect on Kz 29.122/29.123,
    zero amounts left out. ``withdrawal`` and ``contribution`` are that
    effect; :meth:`amounts` adds it. ``notes``: keys of hints, e.g. a capped
    amount. ``turnover``: part of the total turnover of the small business
    limit (taxable free supply at its VAT base, income in kind except Kz
    103; R-27).
    """

    kind: str
    amount: Decimal = ZERO
    fields: dict = field(default_factory=dict)
    withdrawal: Decimal = ZERO
    contribution: Decimal = ZERO
    notes: tuple = ()
    turnover: Decimal = ZERO

    def amounts(self):
        """All field amounts including the withdrawals and contributions."""
        result = dict(self.fields)
        if self.withdrawal:
            result[FIELD_WITHDRAWALS] = result.get(FIELD_WITHDRAWALS, ZERO) + (
                self.withdrawal
            )
        if self.contribution:
            result[FIELD_CONTRIBUTIONS] = result.get(FIELD_CONTRIBUTIONS, ZERO) + (
                self.contribution
            )
        return {code: value for code, value in result.items() if value}


def to_cent(value):
    """Commercial rounding to cent."""
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _parameter(params, code, year):
    value = params.get(code, year)
    if value is None:
        raise AdjustmentError("missing_parameter", parameter=code, year=year)
    return Decimal(str(value))


def _not_negative(**values):
    for name, value in values.items():
        if value is not None and Decimal(value) < 0:
            raise AdjustmentError("negative_input", input=name)


def compute(data, params, used=ZERO):
    """Amounts of the profit correction ``data`` (:class:`AdjustmentInput`).

    :param params: :class:`~.parameters.Parameters` of the rule set
    :param used: part of the yearly maximum of the correction
        (:func:`limit_key`) that earlier corrections of the year already use
    :raises AdjustmentError: unknown kind, negative input, missing parameter
        of the year, VAT of a small business or a VAT without its base
    """
    _not_negative(
        amount=data.amount,
        vat=data.vat,
        vat_base=data.vat_base,
        days=data.days,
        days_8h=data.days_8h,
        days_24h=data.days_24h,
        distance_km=data.distance_km,
        km=data.km,
        costs=data.costs,
        revenue=data.revenue,
        other_amount=data.other_amount,
        reduction=data.reduction,
        child_months=data.child_months,
        hours_per_week=data.hours_per_week,
        free_place_months=data.free_place_months,
    )
    function = _KIND_FUNCTIONS.get(data.kind)
    if function is None:
        raise AdjustmentError("unknown_kind", kind=data.kind)
    if data.small_business and (
        data.kind == WITHDRAWAL_VAT or (data.kind in VAT_KINDS and data.vat)
    ):
        # § 19 (1) UStG: no VAT on the supplies of a small business (R-28)
        raise AdjustmentError("small_business_vat")
    if (
        data.kind in FREE_SUPPLY_KINDS
        and not data.small_business
        and data.vat
        and not data.vat_base
    ):
        raise AdjustmentError("vat_base_missing")
    return function(data, params, Decimal(used or 0))


def limit_key(data):
    """Key of the yearly maximum ``data`` shares with the other corrections
    of the year, ``None`` for a correction without one.

    Daily home office allowance (1,260 € per year, § 4 (5) sentence 1 no. 6c
    EStG), yearly flat rate of a home office (no. 6b), flat rate of business
    expenses per group (BMF 06.04.2023) and commuting allowance without an
    own or provided vehicle (4,500 € per calendar year, § 9 (1) sentence 3
    no. 4 sentence 2 EStG).
    """
    if data.kind == HOME_OFFICE_DAYS:
        return (HOME_OFFICE_DAYS,)
    if data.kind == HOME_OFFICE_ROOM and data.method != "actual":
        return (HOME_OFFICE_ROOM,)
    if data.kind == FLAT_RATE and data.flat_kind in FLAT_PARAMETERS:
        return (FLAT_RATE, data.flat_kind)
    if data.kind == COMMUTING and not data.own_car:
        return (COMMUTING,)
    return None


def limits_used(inputs, params):
    """Parts of the yearly maximums the corrections ``inputs`` use, in their
    order: ``{limit key: Decimal}``. A correction that cannot be computed
    uses nothing."""
    used = {}
    for data in inputs:
        key = limit_key(data)
        if key is None:
            continue
        try:
            result = compute(data, params, used.get(key, ZERO))
        except AdjustmentError:
            continue
        used[key] = used.get(key, ZERO) + result.amount
    return used


def compute_series(inputs, params):
    """Results of the corrections of one year in their order; the yearly
    maximums are shared, earlier corrections first."""
    results = []
    for index, data in enumerate(inputs):
        key = limit_key(data)
        used = limits_used(inputs[:index], params).get(key, ZERO) if key else ZERO
        results.append(compute(data, params, used))
    return results


def _capped(amount, limit):
    """``(amount, notes)`` with ``amount`` limited to ``limit`` (not below 0)."""
    if amount > limit:
        return max(to_cent(limit), ZERO), ("capped",)
    return amount, ()


def _withdrawal(data, main_field):
    amount, vat = to_cent(data.amount), to_cent(data.vat)
    return AdjustmentResult(
        kind=data.kind,
        amount=amount,
        fields={main_field: amount, FIELD_VAT: vat},
        withdrawal=amount + vat,
        turnover=to_cent(data.vat_base),
    )


def _car_private_use(data, params, used=ZERO):
    """Kz 106: value of the private use of a vehicle of the business, plus
    the VAT on it (Kz 140); a withdrawal (Anleitung, line 20). Its VAT base
    counts in the total turnover if the use is taxable (R-27)."""
    return _cleaned(_withdrawal(data, FIELD_CAR))


def _withdrawal_in_kind(data, params, used=ZERO):
    """Kz 108: withdrawal of goods, use or services at value or cost, plus
    the VAT on it (Kz 140); a withdrawal (Anleitung, line 21). Its VAT base
    counts in the total turnover if the supply is taxable (R-27)."""
    return _cleaned(_withdrawal(data, FIELD_IN_KIND))


def _income_in_kind(data, params, used=ZERO):
    """Income in kind and barter (Sach- und Tauscheinnahmen): the value of
    what the business receives instead of money is income when it is
    received (§ 8 (1), (2) EStG, Anleitung 2026 lines 12 to 17). A small
    business shows it in Kz 111; under standard taxation Kz 112 (taxable)
    or Kz 103 (tax-free) with its VAT in Kz 140. No withdrawal or
    contribution. The consideration counts in the total turnover (R-27),
    except tax-free or not taxable income in Kz 103: like booked revenue of
    Kz 103 it is no part of it (§ 19 (2) UStG)."""
    amount, vat = to_cent(data.amount), to_cent(data.vat)
    turnover = amount
    if data.small_business:
        fields = {FIELD_KU: amount}
    else:
        code = data.revenue_field or FIELD_TAXABLE
        if code not in REVENUE_FIELDS:
            raise AdjustmentError("unknown_field", field=code)
        if code == FIELD_EXEMPT and vat:
            raise AdjustmentError("vat_on_exempt")
        fields = {code: amount, FIELD_VAT: vat}
        if code == FIELD_EXEMPT:
            turnover = ZERO
    return _cleaned(
        AdjustmentResult(
            kind=data.kind, amount=amount, fields=fields, turnover=turnover
        )
    )


def _withdrawal_vat(data, params, used=ZERO):
    """Kz 140: VAT on free supplies (unentgeltliche Wertabgaben) entered on
    their own; part of the withdrawal (Anleitung, line 17)."""
    amount = to_cent(data.amount)
    return _cleaned(
        AdjustmentResult(
            kind=data.kind,
            amount=amount,
            fields={FIELD_VAT: amount},
            withdrawal=amount,
        )
    )


def _use_contribution(data, params, used=ZERO):
    """Kz 147: business trips with a private vehicle, 0.30 € per full km or
    the actual share of the costs (Anleitung 2026, line 72); a contribution
    of the use."""
    if data.method == "actual":
        amount = to_cent(data.amount)
    else:
        rate = _parameter(params, "private_car_km", data.year)
        amount = to_cent(int(data.km) * rate)
    return _cleaned(
        AdjustmentResult(
            kind=data.kind,
            amount=amount,
            fields={FIELD_USE_CONTRIBUTION: amount},
            contribution=amount,
        )
    )


def commuting_rate(params, year, distance_km):
    """Commuting allowance per working day (§ 9 (1) sentence 3 no. 4 EStG).

    Full km only. 2024 and 2025: 0.30 € for km 1 to 20, 0.38 € from km 21;
    from 2026: 0.38 € from the first km (rule parameters).
    """
    km = int(distance_km)
    first = _parameter(params, "commute_rate_1_20", year)
    further = _parameter(params, "commute_rate_21", year)
    return min(km, 20) * first + max(km - 20, 0) * further


def _commuting(data, params, used=ZERO):
    """Kz 176: commuting allowance; Kz 142: actual costs of the trips booked
    as expenses, deducted (§ 4 (5) sentence 1 no. 6 EStG, Anleitung 2026
    lines 73 and 74). At most 4,500 € per calendar year unless an own or
    provided vehicle is used (``used``: allowance of the year already
    claimed without own vehicle)."""
    allowance = to_cent(data.days * commuting_rate(params, data.year, data.distance_km))
    notes = ()
    if not data.own_car:
        limit = _parameter(params, "commute_max", data.year) - used
        allowance, notes = _capped(allowance, limit)
    costs = to_cent(data.costs)
    return _cleaned(
        AdjustmentResult(
            kind=data.kind,
            amount=allowance,
            fields={FIELD_COMMUTING_COSTS: costs, FIELD_COMMUTING: allowance},
            contribution=allowance if not costs else ZERO,
            notes=notes,
        )
    )


def _home_office_days(data, params, used=ZERO):
    """Kz 163: daily home office allowance, 6 € per day, at most 1,260 € per
    year for all business and professional activities together (§ 4 (5)
    sentence 1 no. 6c EStG). ``other_amount``: allowance already claimed
    for other income in the year; ``used``: allowance of the other
    corrections of the year. Not with deductible costs of a double
    household (sentence 3); with a home office in the same year: check
    C-55."""
    if data.days > 366:
        raise AdjustmentError("too_many_days", days=data.days)
    if data.double_household:
        return AdjustmentResult(kind=data.kind, notes=("double_household",))
    per_day = _parameter(params, "homeoffice_day", data.year)
    limit = (
        _parameter(params, "homeoffice_max", data.year)
        - to_cent(data.other_amount)
        - used
    )
    amount, notes = _capped(to_cent(data.days * per_day), limit)
    return _cleaned(
        AdjustmentResult(
            kind=data.kind,
            amount=amount,
            fields={FIELD_HOME_OFFICE_DAYS: amount},
            contribution=amount,
            notes=notes,
        )
    )


def _home_office_room(data, params, used=ZERO):
    """Kz 172 deductible, Kz 162 not deductible: home office (§ 4 (5)
    sentence 1 no. 6b EStG, Anleitung 2026 line 66).

    Only as centre of all activities: actual costs, or the yearly flat rate
    of 1,260 € reduced by a twelfth for each full month without the centre.
    ``months``: months with the centre. ``costs``: actual costs not booked;
    their part that is not deducted goes to Kz 162 (also with the flat rate).
    ``used``: flat rate of the other corrections of the year.
    """
    if not 0 <= data.months <= 12:
        raise AdjustmentError("invalid_months", months=data.months)
    costs = to_cent(data.costs)
    notes = ()
    if not data.center_of_activity or not data.months:
        deductible = ZERO
    elif data.method == "actual":
        deductible = to_cent(costs * data.months / 12)
    else:
        flat = _parameter(params, "study_flat", data.year)
        deductible, notes = _capped(to_cent(flat * data.months / 12), flat - used)
    # costs not deducted: the months without the centre, or all costs when
    # the flat rate is used instead of them
    not_deductible = costs - deductible if data.method == "actual" else costs
    return _cleaned(
        AdjustmentResult(
            kind=data.kind,
            amount=deductible,
            fields={
                FIELD_HOME_OFFICE_ND: not_deductible,
                FIELD_HOME_OFFICE: deductible,
            },
            contribution=deductible,
            notes=notes,
        )
    )


def _meals(data, params, used=ZERO):
    """Kz 171: additional meal expenses only at the flat rates (§ 4 (5)
    sentence 1 no. 5, § 9 (4a) EStG): 14 € for more than 8 hours or the day
    of arrival or departure, 28 € for 24 hours. ``other_amount``: flat rates
    abroad (BMF table); ``reduction``: meals provided."""
    amount = (
        data.days_8h * _parameter(params, "meal_8h", data.year)
        + data.days_24h * _parameter(params, "meal_24h", data.year)
        + to_cent(data.other_amount)
        - to_cent(data.reduction)
    )
    if amount < 0:
        raise AdjustmentError("reduction_too_high")
    amount = to_cent(amount)
    return _cleaned(
        AdjustmentResult(
            kind=data.kind,
            amount=amount,
            fields={FIELD_MEALS: amount},
            contribution=amount,
        )
    )


def _flat_rate(data, params, used=ZERO):
    """Kz 195: flat rate instead of all actual business expenses (BMF
    06.04.2023): writers and journalists 30 % of the income, at most
    3,600 € per year; scientific, artistic or literary side activity and
    part-time teaching and examining 25 %, at most 900 € per year for all
    of them; child day care 400 € per child and month for 40 hours a week
    (less pro rata) plus 50 € per month of a reserved place. ``used``: flat
    rate of the group already claimed in the year. Actual expenses at the
    same time: check C-56."""
    notes = ()
    if data.flat_kind == FLAT_CHILDCARE:
        per_month = _parameter(params, "flat_childcare_month", data.year)
        reserved = _parameter(params, "flat_childcare_free_place", data.year)
        hours = min(Decimal(data.hours_per_week), Decimal(FULL_CARE_HOURS))
        amount = (
            Decimal(data.child_months) * per_month * hours / FULL_CARE_HOURS
            + Decimal(data.free_place_months) * reserved
        )
    elif data.flat_kind in FLAT_PARAMETERS:
        rate_code, max_code = FLAT_PARAMETERS[data.flat_kind]
        amount = to_cent(data.revenue) * _parameter(params, rate_code, data.year)
        limit = _parameter(params, max_code, data.year) - used
        amount, notes = _capped(amount, limit)
    else:
        raise AdjustmentError("unknown_flat_kind", flat_kind=data.flat_kind)
    amount = to_cent(amount)
    return _cleaned(
        AdjustmentResult(
            kind=data.kind,
            amount=amount,
            fields={FIELD_FLAT_RATE: amount},
            notes=notes,
        )
    )


def _tax_free(data, params, used=ZERO):
    """G.240–G.242: tax-free income contained in the income (§ 3, § 3a
    EStG), deducted; G.243–G.245: expenses not deductible under § 3c EStG,
    added (Anleitung 2026, lines 79 to 84)."""
    if data.tax_free_field not in TAX_FREE_FIELDS:
        raise AdjustmentError("unknown_field", field=data.tax_free_field)
    amount = to_cent(data.amount)
    return _cleaned(
        AdjustmentResult(
            kind=data.kind, amount=amount, fields={data.tax_free_field: amount}
        )
    )


def _cleaned(result):
    return AdjustmentResult(
        kind=result.kind,
        amount=result.amount,
        fields={code: value for code, value in result.fields.items() if value},
        withdrawal=result.withdrawal,
        contribution=result.contribution,
        notes=result.notes,
        turnover=result.turnover,
    )


_KIND_FUNCTIONS = {
    CAR_PRIVATE_USE: _car_private_use,
    WITHDRAWAL_IN_KIND: _withdrawal_in_kind,
    WITHDRAWAL_VAT: _withdrawal_vat,
    USE_CONTRIBUTION: _use_contribution,
    COMMUTING: _commuting,
    HOME_OFFICE_DAYS: _home_office_days,
    HOME_OFFICE_ROOM: _home_office_room,
    MEALS: _meals,
    FLAT_RATE: _flat_rate,
    TAX_FREE: _tax_free,
    INCOME_IN_KIND: _income_in_kind,
}


def total_amounts(results):
    """Sum of the field amounts of several profit corrections."""
    totals = {}
    for result in results:
        for code, value in result.amounts().items():
            totals[code] = totals.get(code, ZERO) + value
    return {code: value for code, value in totals.items() if value}


# ------------------------------------------------------------ plausibility
#: "of which" and "not deductible" columns of box 25; they are not part of
#: the expenses (F-07)
INFORMATIVE_EXPENSES = frozenset(
    {"EUER.25.153", "EUER.25.164", "EUER.25.165", "EUER.25.162", "EUER.25.168"}
)


def plausibility(values, expense_codes=None, room_months=12, tax_free_limit=None):
    """Checks C-55, C-56 and the limit of Kz 240 on the form values of a
    year (spec 5.8).

    :param values: code → ``Decimal`` (form values after the formulas)
    :param expense_codes: codes of the expense fields of box 25 that hold
        amounts (inputs and transfers); without them every ``EUER.25.*``
        code except subtotals counts
    :param room_months: months in which a home office is deducted (Kz 172);
        the daily allowance is excluded only so far (§ 4 (5) sentence 1 no.
        6c sentence 3 EStG: "soweit"): for 12 months the key is
        ``home_office_twice`` (blocker), else ``home_office_part_year``
        (warning, the days must lie in the other months)
    :param tax_free_limit: maximum of Kz 240 in the year (§ 3 no. 26 and
        26a EStG together), ``None``: not checked
    :return: list of ``(code, key, params)``
    """
    results = []
    days = values.get(FIELD_HOME_OFFICE_DAYS) or ZERO
    room = values.get(FIELD_HOME_OFFICE) or ZERO
    if days > 0 and room > 0:
        key = "home_office_twice" if room_months >= 12 else "home_office_part_year"
        results.append(
            ("C-55", key, {"days": days, "room": room, "months": room_months})
        )
    tax_free = values.get(FIELD_TAX_FREE_26) or ZERO
    if tax_free_limit is not None and tax_free > tax_free_limit:
        results.append(
            (
                "C-44",
                "tax_free_above_limit",
                {"amount": tax_free, "limit": Decimal(tax_free_limit)},
            )
        )
    flat = values.get(FIELD_FLAT_RATE) or ZERO
    if flat > 0:
        if expense_codes is None:
            expense_codes = {
                code
                for code in values
                if code.startswith("EUER.25.") and code[8:].isdigit()
            }
        actual = sorted(
            code
            for code in set(expense_codes) - INFORMATIVE_EXPENSES - {FIELD_FLAT_RATE}
            if values.get(code)
        )
        if actual:
            results.append(
                ("C-56", "flat_rate_and_actual", {"flat": flat, "fields": actual})
            )
    return results
