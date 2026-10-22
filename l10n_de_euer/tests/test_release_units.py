# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Unit tests of engine paths without a test before the release (stage E5,
O-06), no database. Expected values are worked out by hand in the
docstrings."""

import dataclasses
from datetime import date
from decimal import Decimal as D
from fractions import Fraction

from odoo.tests import BaseCase

from ..models.engine import depreciation as dep
from ..models.engine import rounding


class TestRoundingBounds(BaseCase):
    """``apportion_bounded``: the cents outside a bound move to the keys with
    room, the sign of the total is kept (spec 5.10, B-06)."""

    def test_without_bounds_like_apportion(self):
        raw = {1: Fraction(10, 3), 2: Fraction(20, 3)}
        self.assertEqual(
            rounding.apportion_bounded(raw, 10, {}), rounding.apportion(raw, 10)
        )
        self.assertEqual(rounding.apportion_bounded(raw, 10, None), {1: 3, 2: 7})

    def test_upper_bound_moves_cents(self):
        """5/5 with key 1 at most 3: the two cents above go to key 2."""
        raw = {1: Fraction(5), 2: Fraction(5)}
        self.assertEqual(
            rounding.apportion_bounded(raw, 10, {1: (None, 3)}), {1: 3, 2: 7}
        )

    def test_lower_bound_takes_cents(self):
        """1/9 with key 1 at least 4: three cents come from key 2."""
        raw = {1: Fraction(1), 2: Fraction(9)}
        self.assertEqual(
            rounding.apportion_bounded(raw, 10, {1: (4, None)}), {1: 4, 2: 6}
        )

    def test_negative_total(self):
        """Bounds carry the sign of the values: -5/-5 with key 1 not below -3
        gives -3/-7; key 2 at most -6 gives -4/-6; key 1 between -4 and -3
        gives -4/-6."""
        raw = {1: Fraction(-5), 2: Fraction(-5)}
        self.assertEqual(
            rounding.apportion_bounded(raw, -10, {1: (-3, None)}), {1: -3, 2: -7}
        )
        self.assertEqual(
            rounding.apportion_bounded(raw, -10, {2: (None, -6)}), {1: -4, 2: -6}
        )
        self.assertEqual(
            rounding.apportion_bounded(raw, -10, {1: (-4, -3), 2: (-9, 0)}),
            {1: -4, 2: -6},
        )

    def test_bounds_within_need_no_move(self):
        raw = {1: Fraction(5), 2: Fraction(5)}
        self.assertEqual(
            rounding.apportion_bounded(raw, 10, {1: (0, 6), 2: (4, None)}),
            {1: 5, 2: 5},
        )

    def test_bounds_that_cannot_be_kept(self):
        """Key 1 at most 3 and key 2 at most 6: one cent has no target."""
        raw = {1: Fraction(5), 2: Fraction(5)}
        with self.assertRaisesRegex(ValueError, "Bounds leave 1 cent"):
            rounding.apportion_bounded(raw, 10, {1: (2, 3), 2: (0, 6)})
        with self.assertRaisesRegex(ValueError, "Bounds leave -2 cent"):
            rounding.apportion_bounded(raw, 10, {1: (6, None), 2: (6, None)})


class TestRoundingBuildingBlocks(BaseCase):
    def test_spread_of_a_negative_rest(self):
        """Shares above the total: the missing cents come off the keys with
        the smallest remainders, the larger key first on a tie."""
        raw = {1: Fraction(5), 2: Fraction(5)}
        self.assertEqual(rounding.apportion(raw, 8), {1: 4, 2: 4})
        self.assertEqual(rounding.apportion(raw, 9), {1: 5, 2: 4})

    def test_caps_without_excess(self):
        raw = {1: Fraction(5), 2: Fraction(5)}
        self.assertEqual(rounding.apportion(raw, 10, caps={1: 6}), {1: 5, 2: 5})
        self.assertEqual(rounding.apportion(raw, 10, caps={3: 0}), {1: 5, 2: 5})

    def test_to_fraction_and_sign(self):
        self.assertEqual(rounding.to_fraction(0.1), Fraction(1, 10))
        self.assertEqual(rounding.to_fraction("1.25"), Fraction(5, 4))
        half = Fraction(1, 2)
        self.assertIs(rounding.to_fraction(half), half)
        self.assertEqual([rounding.sign(value) for value in (-3, 0, 2)], [-1, 0, 1])


class TestDepreciationValidation(BaseCase):
    """Input the depreciation rules do not allow is refused with the code of
    the rule (spec 6.3); the ORM layer translates the code into a message."""

    BASE = dep.Asset(
        kind="depreciable",
        method="linear",
        delivery_date=date(2024, 1, 10),
        cost=D("1200.00"),
        useful_life_months=36,
    )

    def assertRefused(self, code, **changes):
        asset = dataclasses.replace(self.BASE, **changes)
        with self.assertRaises(dep.DepreciationError) as caught:
            dep.validate(asset)
        self.assertEqual(caught.exception.code, code, changes)
        self.assertTrue(str(caught.exception))

    def test_valid_asset(self):
        self.assertIsNone(dep.validate(self.BASE))

    def test_asset_values(self):
        self.assertRefused("kind", kind="building")
        self.assertRefused("method", method="sum_of_years")
        self.assertRefused("negative_cost", cost=D("-1"))
        self.assertRefused("memo", memo_value=D("1200.00"))
        self.assertRefused("no_method", method="none")
        self.assertRefused("method_not_allowed", kind="gwg")
        self.assertRefused("no_rate", method="degressive")
        self.assertRefused("disposal_kind", disposal_kind="gift")

    def test_takeover_and_disposal_dates(self):
        self.assertRefused("takeover_date", opening_date=date(2025, 6, 30))
        self.assertRefused("takeover_before_delivery", opening_date=date(2023, 12, 31))
        self.assertRefused("disposal_before_delivery", disposal_date=date(2023, 12, 1))
        self.assertRefused(
            "disposal_before_takeover",
            opening_date=date(2024, 12, 31),
            disposal_date=date(2024, 6, 1),
        )

    def test_events(self):
        def event(kind, day, **values):
            return (dep.Event(type=kind, date=day, **values),)

        self.assertRefused("event_type", events=event("revaluation", date(2025, 1, 1)))
        self.assertRefused(
            "ev_events",
            method="ev_7_2a",
            ev_rates=(D("0.75"), D("0.1"), D("0.05"), D("0.05"), D("0.03"), D("0.02")),
            events=event("additional_cost", date(2025, 3, 1), amount=D("100")),
        )
        self.assertRefused(
            "life_change_first_year",
            events=event("useful_life_change", date(2024, 6, 1), useful_life_months=24),
        )
        self.assertRefused(
            "life_change_months",
            events=event("useful_life_change", date(2025, 6, 1)),
        )
        self.assertRefused(
            "manual_method",
            events=event("manual_depreciation", date(2025, 6, 1), amount=D("100")),
        )
        self.assertRefused(
            "sec7b_method",
            group="building",
            method="degressive",
            degressive_rate=D("0.05"),
            useful_life_months=600,
            events=event("sec7b_special", date(2024, 6, 1), amount=D("50")),
        )
