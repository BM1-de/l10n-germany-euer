# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Depreciation schedule as computed by Papierkram."""

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def papierkram_schedule(asset, keep_value=Decimal("1.00")):
    """Yearly straight-line depreciation exactly as Papierkram computes it.

    Papierkram depreciates monthly from the month of acquisition on (German
    ``Monatsregel``, § 7 (1) sentence 4 EStG): ``acquisition value / useful
    life in months`` per month, rounded to cents per year. The last year takes
    the remainder and keeps a memo value of 1 €. Assets without a monthly rate
    (immediate write-off) are fully written off in the year of acquisition.

    :param asset: :class:`~.reader.Asset`
    :return: list of ``(year, amount)``
    """
    value = Decimal(asset.acquisition_value)
    start = asset.acquisition_date
    months = asset.useful_life_months
    if not months:
        return [(start.year, value)]
    monthly = value / months
    schedule = []
    remaining = months
    booked = Decimal(0)
    year = start.year
    first_months = 12 - start.month + 1
    while remaining > 0:
        n = min(first_months if year == start.year else 12, remaining)
        remaining -= n
        if remaining == 0:
            amount = value - keep_value - booked
        else:
            amount = (monthly * n).quantize(CENT, rounding=ROUND_HALF_UP)
        booked += amount
        schedule.append((year, amount))
        year += 1
    return schedule
