# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""VAT rules of the engine (spec 4.4, 5.6.2, 5.7 no. 4/5, R-25 to R-28).

* VAT treatment (small business ``ku`` or standard ``regular``) per supply:
  stored on the document, otherwise from the status periods on the supply
  date; a period "standard from receipt" (threshold exceeded) applies to all
  revenue received from its start (R-25).
* Tax lines are assigned by their repartition line, never by the direction
  of the booking (B-17).
* Due dates of VAT returns (§ 18 (1) UStG, § 46 UStDV) for the ten-day rule.
* Total turnover of a calendar year for the small business limit (R-27).
"""

import datetime
import re
from fractions import Fraction

from . import model as m

#: tax line kinds
TAX_NEUTRAL = "neutral"
TAX_OUTPUT = "output"
TAX_INPUT = "input"
TAX_BASE = "base"

_PERIOD_RE = re.compile(r"^(\d{4})(?:-(?:(\d{1,2})|Q([1-4])))?$")

#: until 2024 the turnover of a year with activity in part of the year only is
#: converted into a turnover of the full year (§ 19 (3) sentences 3 and 4 UStG
#: in the version until 31 December 2024); the version from 2025 has no
#: conversion (section 19.1 (3) UStAE)
LAST_YEAR_OF_ANNUAL_CONVERSION = 2024


# ------------------------------------------------------------ status periods
def period_at(vat_periods, on_date):
    """Return the status period covering ``on_date`` (latest start wins)."""
    best = None
    for period in vat_periods:
        if period.date_from > on_date:
            continue
        if period.date_to and period.date_to < on_date:
            continue
        if best is None or period.date_from > best.date_from:
            best = period
    return best


def regime_at(vat_periods, on_date, default=m.REGULAR):
    period = period_at(vat_periods, on_date)
    return period.regime if period else default


def receipt_regime_at(vat_periods, on_date):
    """Regime of a period "from receipt" (threshold exceeded) at a cash date."""
    period = period_at(vat_periods, on_date)
    if period is not None and period.basis == "receipt_date":
        return period.regime
    return None


def supply_date(move):
    """Date of supply of a document: supply date, invoice date or date."""
    return move.supply_date or move.invoice_date or move.date


def document_regime(move, vat_periods, default=m.REGULAR):
    """VAT treatment stored on the document or valid on its supply date."""
    if move.info.vat_regime:
        return move.info.vat_regime
    return regime_at(vat_periods, supply_date(move), default)


def revenue_regime(move, cash_date, vat_periods, default=m.REGULAR):
    """VAT treatment of revenue received on ``cash_date`` (R-25)."""
    return receipt_regime_at(vat_periods, cash_date) or document_regime(
        move, vat_periods, default
    )


# ------------------------------------------------------------ tax lines
def is_reverse_charge(tax, document_type):
    """Tax with a positive and a negative factor for the same document type.

    Reverse charge, intra-EU acquisition and import VAT: the tax and its
    counter entry neutralise each other in the document (R-22).
    """
    factors = [factor for doc_type, factor in tax.factors if doc_type == document_type]
    return any(factor > 0 for factor in factors) and any(
        factor < 0 for factor in factors
    )


def tax_line_kind(repartition, tax, category, regime):
    """Kind of a tax line with repartition line (spec 5.7 no. 4).

    ``neutral`` (reverse charge, import VAT in the document), ``output``,
    ``input`` (deductible input VAT) or ``base`` (input VAT that is not
    deductible: it follows its base lines).
    """
    if tax is not None and is_reverse_charge(tax, repartition.document_type):
        return TAX_NEUTRAL
    type_tax_use = tax.type_tax_use if tax is not None else "purchase"
    if type_tax_use == "sale":
        return TAX_OUTPUT
    if (
        not repartition.use_in_tax_closing
        or not repartition.has_account
        or (category is not None and category.vat_kind == "input_non_deductible")
        or regime == m.KU
    ):
        return TAX_BASE
    return TAX_INPUT


# ------------------------------------------------------------ VAT returns
def _add_months(day, months):
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    return day.replace(year=year, month=month)


def parse_vat_period(text):
    """Return ``(year, first day, last day, kind)`` of a VAT return period.

    Formats: ``2026-12`` (month), ``2026-Q4`` (quarter), ``2026`` (year).
    """
    match = _PERIOD_RE.match((text or "").strip())
    if not match:
        return None
    year = int(match.group(1))
    if match.group(2):
        month = int(match.group(2))
        if not 1 <= month <= 12:
            return None
        first = datetime.date(year, month, 1)
        last = _add_months(first, 1) - datetime.timedelta(days=1)
        return year, first, last, "month"
    if match.group(3):
        quarter = int(match.group(3))
        first = datetime.date(year, 3 * quarter - 2, 1)
        last = _add_months(first, 3) - datetime.timedelta(days=1)
        return year, first, last, "quarter"
    return year, datetime.date(year, 1, 1), datetime.date(year, 12, 31), "year"


def vat_due_date(text, permanent_extension):
    """Legal due date of a VAT advance return (R-08, 5.6.2).

    The 10th day after the end of the period; with permanent extension one
    month later. No shift for weekends or public holidays: § 108 (3) AO only
    extends the period for the payment, the ten-day rule takes the due date
    of the law, and a payment after the 10th belongs to the year of payment
    (BFH X R 44/16, VIII R 34/12; H 11 EStH "Allgemeines"). Annual returns
    have no fixed due date here (``None``).
    """
    parsed = parse_vat_period(text)
    if parsed is None or parsed[3] == "year":
        return None
    due = parsed[2] + datetime.timedelta(days=10)
    return _add_months(due, 1) if permanent_extension else due


def permanent_extension_for(text, vat_periods):
    """Whether the period of the return has a permanent extension (DFV)."""
    parsed = parse_vat_period(text)
    if parsed is None:
        return False
    period = period_at(vat_periods, parsed[2])
    return bool(period and period.permanent_extension)


# ------------------------------------------------------------ KU limit
def is_small_business_year(vat_periods, year):
    """Whether a small business period overlaps ``year``."""
    start, end = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
    return any(
        period.regime == m.KU
        and period.date_from <= end
        and (period.date_to is None or period.date_to >= start)
        for period in vat_periods
    )


def turnover(entries, year):
    """Return :class:`~.model.Turnover` of ``year`` from ``(date, source, cent)``.

    The turnover follows the actual cash date, without the ten-day rule
    (R-27). Entries are summed per payment in chronological order.
    """
    result = m.Turnover(year=year)
    per_payment = {}
    for on_date, source_id, amount in entries:
        if on_date.year == year:
            key = (on_date, source_id or 0)
            per_payment[key] = per_payment.get(key, 0) + amount
    result.entries = sorted(per_payment.items())
    result.amount = sum(per_payment.values())
    return result


def check_limit(
    entries,
    year,
    limits,
    vat_periods,
    founding_year=None,
    previous_data=True,
    activity_start=None,
    books_start=None,
):
    """Findings C-32 for the small business limit (R-27, UT-KU).

    ``limits``: dict with ``ku_limit_prev``, ``ku_limit_cur`` and
    ``ku_limit_founding`` in cent for ``year``; ``None`` if the parameter is
    missing for the year, then that check is left out. ``founding_year``:
    year the activity started; in that year the limit of the year of
    foundation holds and there is no previous year (section 19.1 (3)
    UStAE). ``previous_data``: whether the books hold entries of an earlier
    year than ``year``; without them and without a turnover entered for the
    year the turnover of the previous year is unknown (warning).
    ``activity_start``: day the activity started, for the conversion into a
    full year until 2024 (:func:`annual_turnover`). ``books_start``: the
    books start on this day during the previous year, so their turnover of
    that year is incomplete. Returns ``(turnover, findings)``.
    """
    current = turnover(entries, year)
    findings = []
    if not is_small_business_year(vat_periods, year):
        return current, findings
    founded = founding_year == year
    limit = limits["ku_limit_founding" if founded else "ku_limit_cur"]
    current.limit = limit
    if limits["ku_limit_prev"] is not None and not founded:
        findings += _check_previous_year(
            entries,
            year,
            limits,
            vat_periods,
            previous_data,
            activity_start,
            books_start,
        )
    if limit is None:
        return current, findings
    # until 2024 the turnover expected for the year of foundation is a
    # turnover of the full year (§ 19 (3) sentences 3 and 4 UStG old)
    start = activity_start if founded else None
    total = 0
    for (on_date, source_id), amount in current.entries:
        total += amount
        if (
            annual_turnover(total, start, year) > limit
            and current.crossing_date is None
        ):
            current.crossing_date = on_date
            current.crossing_source_id = source_id or None
    if current.crossing_date is not None:
        findings.append(_crossing_finding(current, year, vat_periods, start))
    elif limit and annual_turnover(current.amount, start, year) * 5 >= limit * 4:
        findings.append(
            m.Finding(
                code="C-32",
                severity=m.WARNING,
                key="ku_limit_near",
                years=(year,),
                amount=current.amount,
                params={"limit": limit, **_annual_params(current.amount, start, year)},
            )
        )
    return current, findings


def annual_turnover(amount, start, year, checked_year=None):
    """Turnover of ``year`` converted into a turnover of the full year.

    A business that started its activity in ``year`` converts the actual
    turnover of that year: begun calendar months count as full months,
    unless the conversion by days gives a lower total turnover (§ 19 (3)
    sentences 3 and 4 UStG in the version until 2024). The law of the year
    checked (``checked_year``, default ``year``) decides: only checks until
    :data:`LAST_YEAR_OF_ANNUAL_CONVERSION` convert. Otherwise, and for a
    start on 1 January, the amount is returned unchanged. The result is
    exact (a fraction of cents).
    """
    if (
        start is None
        or start.year != year
        or (checked_year or year) > LAST_YEAR_OF_ANNUAL_CONVERSION
        or amount <= 0
    ):
        return amount
    first, last = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
    if start == first:
        return amount
    by_months = Fraction(amount * 12, 13 - start.month)
    by_days = Fraction(amount * ((last - first).days + 1), (last - start).days + 1)
    return min(by_months, by_days)


def _annual_params(amount, start, year, checked_year=None):
    """Parameters of a finding on a converted turnover (empty without)."""
    annual = annual_turnover(amount, start, year, checked_year)
    if annual == amount:
        return {}
    return {"annual": round(annual), "start": start}


def free_supplies_hint(left_out, year):
    """C-32 hint: free supplies booked in small business periods (Kz 106,
    108) of ``year`` that the total turnover leaves out (R-27).

    ``left_out``: ``(date, move id, cent)``. Without input tax deduction they
    are not taxable; a taxable one is entered with its VAT base as profit
    correction.
    """
    entries = [entry for entry in left_out if entry[0].year == year]
    amount = sum(entry[2] for entry in entries)
    if not amount:
        return []
    return [
        m.Finding(
            code="C-32",
            severity=m.INFO,
            key="ku_free_supply_not_counted",
            years=(year,),
            move_ids=tuple(sorted({entry[1] for entry in entries if entry[1]})),
            amount=amount,
        )
    ]


def entered_turnover(vat_periods, year):
    """Turnover of the previous year entered for the check of ``year``.

    The status period holds it for one year: the year entered with it (the
    first year in Odoo after a takeover from another system), otherwise the
    first year of the period. ``None``: nothing entered for ``year``.
    """
    for period in sorted(vat_periods, key=lambda period: period.date_from):
        if period.prev_year_turnover is None:
            continue
        if (period.prev_year_turnover_year or period.date_from.year) == year:
            return period.prev_year_turnover
    return None


def _check_previous_year(
    entries,
    year,
    limits,
    vat_periods,
    previous_data=True,
    activity_start=None,
    books_start=None,
):
    start_regime = regime_at(vat_periods, datetime.date(year, 1, 1), None)
    if start_regime != m.KU:
        return []
    amount = entered_turnover(vat_periods, year)
    if amount is None:
        if not previous_data:
            # no entries before the year and nothing entered: the check of
            # the previous year would silently compare 0 (takeover); books
            # that start during the previous year hold only a part of it
            params = {"limit": limits["ku_limit_prev"]}
            if books_start:
                params["books_start"] = books_start
            return [
                m.Finding(
                    code="C-32",
                    severity=m.WARNING,
                    key="ku_previous_year_unknown",
                    years=(year,),
                    params=params,
                )
            ]
        # entered for another year: later years take the data
        amount = turnover(entries, year - 1).amount
    # until 2024 the turnover of the year of foundation is converted
    annual = annual_turnover(amount, activity_start, year - 1, year)
    if annual > limits["ku_limit_prev"]:
        return [
            m.Finding(
                code="C-32",
                severity=m.WARNING,
                key="ku_limit_previous_year",
                years=(year,),
                amount=round(annual),
                params={
                    "limit": limits["ku_limit_prev"],
                    **_annual_params(amount, activity_start, year - 1, year),
                },
            )
        ]
    return []


def _crossing_finding(current, year, vat_periods, start=None):
    """Exceeding payment: a period "standard from receipt" must start on it."""
    params = {
        "limit": current.limit,
        "date": current.crossing_date,
        **_annual_params(current.amount, start, year),
    }
    move_ids = (current.crossing_source_id,) if current.crossing_source_id else ()
    if year < 2025:
        # until 2024 the current year limit was a forecast (§ 19 (1) UStG old)
        return m.Finding(
            code="C-32",
            severity=m.WARNING,
            key="ku_limit_forecast_exceeded",
            years=(year,),
            move_ids=move_ids,
            amount=current.amount,
            params=params,
        )
    period = period_at(vat_periods, current.crossing_date)
    if (
        period is not None
        and period.basis == "receipt_date"
        and period.regime == m.REGULAR
        and period.date_from == current.crossing_date
    ):
        return m.Finding(
            code="C-32",
            severity=m.WARNING,
            key="ku_limit_exceeded_period_set",
            years=(year,),
            move_ids=move_ids,
            amount=current.amount,
            params=params,
        )
    return m.Finding(
        code="C-32",
        severity=m.BLOCKER,
        key="ku_limit_exceeded",
        years=(year,),
        move_ids=move_ids,
        amount=current.amount,
        params=params,
    )
