# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Exact amounts and the largest remainder method (spec 5.10, B-06).

Amounts are integers in cent. Intermediate values are
:class:`fractions.Fraction`, so a split never loses or invents a cent:
:func:`apportion` rounds a set of exact shares to whole cent so that their sum
is exactly the given total (Hamilton, largest remainders), normalised to the
sign of the total. Ties go to the smaller key (the smaller ``aml.id``).
"""

import math
from fractions import Fraction


def to_fraction(value):
    """Return ``value`` (int, str, Decimal, Fraction) as exact fraction."""
    if isinstance(value, Fraction):
        return value
    if isinstance(value, float):
        return Fraction(str(value))
    return Fraction(value)


def sign(value):
    """Return -1, 0 or 1."""
    return (value > 0) - (value < 0)


def apportion(raw, total, caps=None):
    """Round the exact shares ``raw`` (key → Fraction) to integer cent.

    The result sums to ``total`` exactly. Normally ``sum(raw) == total``; then
    every value is rounded down or up by less than one cent (Hamilton). If the
    sums differ (e.g. a final share was fixed exactly), the difference is
    spread over the keys in the order of their remainders.

    ``caps`` (key → int) limits the absolute value of single keys, e.g. the
    amount that can still pass a partial reconciliation. Excess cents move to
    keys with spare room. Keys must be sortable; ties go to the smaller key.
    """
    if not raw:
        if total:
            raise ValueError(f"Cannot apportion {total} to nothing")
        return {}
    direction = -1 if total < 0 else 1
    scaled = {key: to_fraction(value) * direction for key, value in raw.items()}
    result = {key: math.floor(value) for key, value in scaled.items()}
    order = sorted(scaled, key=lambda key: (result[key] - scaled[key], key))
    _spread(result, order, total * direction - sum(result.values()))
    if caps:
        _apply_caps(result, scaled, caps)
    return {key: value * direction for key, value in result.items()}


def _spread(result, order, rest):
    """Add ``rest`` cent to ``result`` in ``order`` (negative: from the end)."""
    count = len(order)
    if rest >= 0:
        keys = order
    else:
        keys = order[::-1]
    quotient, remainder = divmod(abs(rest), count)
    step = 1 if rest >= 0 else -1
    for index, key in enumerate(keys):
        result[key] += step * (quotient + (1 if index < remainder else 0))


def _apply_caps(result, scaled, caps):
    """Move cent above the caps to keys that still have room.

    Works on the values normalised to a non-negative total; capped keys carry
    values with the sign of the total.
    """
    excess = 0
    for key, cap in caps.items():
        if key in result and result[key] > cap:
            excess += result[key] - cap
            result[key] = cap
    if not excess:
        return
    order = sorted(scaled, key=lambda key: (result[key] - scaled[key], key))
    for key in order:
        cap = caps.get(key)
        take = excess if cap is None else min(excess, max(cap - result[key], 0))
        result[key] += take
        excess -= take
        if not excess:
            return
    raise ValueError(f"Caps leave {excess} cent without a target")


def apportion_bounded(raw, total, bounds):
    """Like :func:`apportion`, with per key bounds.

    ``bounds``: key → ``(low, high)`` in cent with the sign of the values
    (``None`` for no bound). Cents outside a bound move to the keys with
    room, first to those with the largest remainders. Raises
    :class:`ValueError` if the bounds cannot be kept.
    """
    result = apportion(raw, total)
    if not bounds:
        return result
    direction = -1 if total < 0 else 1
    scaled = {key: to_fraction(value) * direction for key, value in raw.items()}
    low, high = {}, {}
    for key, (lower, upper) in bounds.items():
        values = [v * direction for v in (lower, upper) if v is not None]
        if lower is not None and upper is not None:
            low[key], high[key] = min(values), max(values)
        elif lower is not None:
            (low if direction > 0 else high)[key] = values[0]
        elif upper is not None:
            (high if direction > 0 else low)[key] = values[0]
    normal = {key: value * direction for key, value in result.items()}
    excess = 0
    for key in normal:
        if key in high and normal[key] > high[key]:
            excess += normal[key] - high[key]
            normal[key] = high[key]
        elif key in low and normal[key] < low[key]:
            excess -= low[key] - normal[key]
            normal[key] = low[key]
    order = sorted(scaled, key=lambda key: (normal[key] - scaled[key], key))
    if excess < 0:
        order.reverse()
    for key in order:
        if not excess:
            break
        if excess > 0:
            room = high[key] - normal[key] if key in high else excess
            take = min(excess, max(room, 0))
        else:
            room = normal[key] - low[key] if key in low else -excess
            take = -min(-excess, max(room, 0))
        normal[key] += take
        excess -= take
    if excess:
        raise ValueError(f"Bounds leave {excess} cent without a target")
    return {key: value * direction for key, value in normal.items()}


def split_by_shares(amount, shares):
    """Split ``amount`` (cent) by ``shares`` (key → Fraction, sum 1)."""
    raw = {key: to_fraction(share) * amount for key, share in shares.items()}
    return apportion(raw, amount)


def gross_split(gross, rate):
    """Split a gross amount into ``(net, tax)`` for a VAT rate in percent.

    Both parts are whole cent and add up to ``gross`` (Hamilton, net first on
    a tie).
    """
    rate = to_fraction(rate)
    if not rate:
        return gross, 0
    raw = {
        0: Fraction(gross) * 100 / (100 + rate),
        1: Fraction(gross) * rate / (100 + rate),
    }
    parts = apportion(raw, gross)
    return parts[0], parts[1]
