# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Unit tests of the depreciation engine (spec 6.3, 9.6; pure Python).

Expected values come from the law, the official example in H 7.4 EStH and
calculations by hand shown in the comments, never from the code itself. All
cases are synthetic (decision D6).
"""

from datetime import date
from decimal import Decimal

from odoo.tests import BaseCase

from ..models.engine import depreciation as dep

D = Decimal
EV_RATES = (0.75, 0.10, 0.05, 0.05, 0.03, 0.02)


def asset(**values):
    defaults = {
        "kind": "depreciable",
        "method": "linear",
        "delivery_date": date(2024, 1, 10),
        "cost": D("1200.00"),
        "useful_life_months": 36,
    }
    defaults.update(values)
    return dep.Asset(**defaults)


def depreciation(rows):
    return {row.year: row.depreciation for row in rows}


class TestLinear(BaseCase):
    def test_at_lin_1_monthly_rule_and_rest_in_last_year(self):
        """AT-LIN-1: J = round(1,999.00 × 12 / 84) = 285.57.

        Delivery 05/2021: 8 months → 285.57 × 8 / 12 = 190.38; last month of
        use 04/2028, the last year takes the rest: 1,999.00 − 190.38 −
        6 × 285.57 = 95.20 (4 months would be 95.19).
        """
        rows = dep.schedule(
            asset(
                delivery_date=date(2021, 5, 3), cost=D("1999.00"), useful_life_months=84
            )
        )
        self.assertEqual(
            depreciation(rows),
            {
                2021: D("190.38"),
                2022: D("285.57"),
                2023: D("285.57"),
                2024: D("285.57"),
                2025: D("285.57"),
                2026: D("285.57"),
                2027: D("285.57"),
                2028: D("95.20"),
            },
        )
        self.assertEqual([row.months for row in rows], [8, 12, 12, 12, 12, 12, 12, 4])
        self.assertEqual(sum(row.depreciation for row in rows), D("1999.00"))
        self.assertTrue(all(row.is_consistent() for row in rows))
        self.assertEqual(rows[-1].book_value_end, D("0.00"))

    def test_at_lin_2_delivery_at_turn_of_month(self):
        """AT-LIN-2: the month of delivery counts in full; 1 January is a new
        year (a delivery at midnight 31 December is entered as 1 January)."""
        december = dep.schedule(asset(delivery_date=date(2026, 12, 31)))
        self.assertEqual(december[0].year, 2026)
        self.assertEqual(december[0].months, 1)
        self.assertEqual(december[0].depreciation, D("33.33"))  # 400 × 1/12
        self.assertEqual(december[-1].year, 2029)
        self.assertEqual(december[-1].depreciation, D("366.67"))  # 11 months, rest
        january = dep.schedule(asset(delivery_date=date(2027, 1, 1)))
        self.assertEqual(january[0].year, 2027)
        self.assertEqual(depreciation(january), {2027: 400, 2028: 400, 2029: 400})

    def test_at_lin_3_rounding_half_up(self):
        """AT-LIN-3: 100.01 × 6 / 12 = 50.005 → 50.01 (ROUND_HALF_UP)."""
        rows = dep.schedule(
            asset(
                delivery_date=date(2026, 7, 1), cost=D("200.02"), useful_life_months=24
            )
        )
        self.assertEqual(
            depreciation(rows), {2026: D("50.01"), 2027: D("100.01"), 2028: D("50.00")}
        )

    def test_at_lin_4_memo_value_only_for_takeover(self):
        """AT-LIN-4: a takeover keeps its memo value of 1.00: the last year is
        6/12 × 159.80 − 1.00 = 78.90 and the asset stays at 1.00. New assets
        never keep one (see test_no_memo_value_for_new_assets)."""
        rows = dep.schedule(
            asset(
                delivery_date=date(2020, 7, 1),
                cost=D("799.00"),
                useful_life_months=60,
                memo_value=D("1.00"),
                opening_date=date(2023, 12, 31),
                opening_book_value=D("239.70"),  # 799 − 79.90 − 3 × 159.80
            )
        )
        self.assertEqual(depreciation(rows), {2024: D("159.80"), 2025: D("78.90")})
        self.assertEqual(rows[-1].book_value_end, D("1.00"))
        disposed = dep.schedule(
            asset(
                delivery_date=date(2020, 7, 1),
                cost=D("799.00"),
                useful_life_months=60,
                memo_value=D("1.00"),
                opening_date=date(2023, 12, 31),
                opening_book_value=D("239.70"),
                disposal_date=date(2026, 3, 1),
                disposal_kind="scrap",
            )
        )
        self.assertEqual(disposed[-1].disposal_book_value, D("1.00"))

    def test_no_memo_value_for_new_assets(self):
        """R 7.4 (3) EStR: the full cost is depreciated."""
        rows = dep.schedule(asset(cost=D("1000.00"), useful_life_months=36))
        self.assertEqual(rows[-1].book_value_end, D("0.00"))
        self.assertEqual(sum(row.depreciation for row in rows), D("1000.00"))


class TestDegressive(BaseCase):
    def test_at_deg_1_official_example_h_7_4(self):
        """AT-DEG-1: H 7.4 EStH, 60,000 €, 04/2024, 6 years, 20 %:
        9,000 / 10,200 / 4 × 9,600 (40,800 ÷ 51 months × 12) / 2,400."""
        rows = dep.schedule(
            asset(
                method="degressive",
                delivery_date=date(2024, 4, 10),
                cost=D("60000.00"),
                useful_life_months=72,
                degressive_rate=D("0.2"),
            )
        )
        self.assertEqual(
            depreciation(rows),
            {
                2024: D("9000.00"),
                2025: D("10200.00"),
                2026: D("9600.00"),
                2027: D("9600.00"),
                2028: D("9600.00"),
                2029: D("9600.00"),
                2030: D("2400.00"),
            },
        )
        self.assertEqual(rows[0].months, 9)

    def test_at_deg_2_rate_limits_by_delivery_date(self):
        """AT-DEG-2: rate ≤ min(factor × linear rate, maximum rate)."""
        # 04–12/2024: 2 × 1/6 = 33.3 %, capped at 20 %
        self.assertEqual(dep.max_degressive_rate(2, 0.2, 72), D("0.2"))
        # 07/2025–2027: 3 × 10 % = 30 %; with 12 years 3 × 8.33 % = 25 %
        self.assertEqual(dep.max_degressive_rate(3, 0.3, 120), D("0.3"))
        self.assertEqual(dep.max_degressive_rate(3, 0.3, 144), D("0.25"))

    def test_at_deg_3_gap_not_allowed(self):
        """AT-DEG-3: 01–06/2025 and 2023–03/2024 have the factor 0."""
        self.assertEqual(dep.max_degressive_rate(0, 0, 72), D("0"))

    def test_at_deg_4_switch_to_linear(self):
        """AT-DEG-4: chosen switch in 2025: 51,000 ÷ 63 months × 12 = 9,714.29;
        the last year (3 months) takes 51,000 − 5 × 9,714.29 = 2,428.55."""
        rows = dep.schedule(
            asset(
                method="degressive",
                delivery_date=date(2024, 4, 10),
                cost=D("60000.00"),
                useful_life_months=72,
                degressive_rate=D("0.2"),
                switch_to_linear_year=2025,
            )
        )
        self.assertEqual(
            depreciation(rows),
            {
                2024: D("9000.00"),
                2025: D("9714.29"),
                2026: D("9714.29"),
                2027: D("9714.29"),
                2028: D("9714.29"),
                2029: D("9714.29"),
                2030: D("2428.55"),
            },
        )

    def test_no_extraordinary_with_degressive(self):
        """§ 7 (2) sentence 4 EStG."""
        with self.assertRaises(dep.DepreciationError):
            dep.schedule(
                asset(
                    method="degressive",
                    degressive_rate=D("0.2"),
                    events=(dep.Event("extraordinary", date(2025, 3, 1), D("100")),),
                )
            )


class TestSpecialMethods(BaseCase):
    def test_at_ekfz_1_electric_vehicle(self):
        """AT-EKFZ-1: 75/10/5/5/3/2 % of 47,999.99 without monthly rule; the
        last year takes the rest 960.00."""
        rows = dep.schedule(
            asset(
                method="ev_7_2a",
                delivery_date=date(2025, 8, 20),
                cost=D("47999.99"),
                useful_life_months=0,
                ev_rates=EV_RATES,
                group="vehicle",
            )
        )
        self.assertEqual(
            depreciation(rows),
            {
                2025: D("35999.99"),
                2026: D("4800.00"),
                2027: D("2400.00"),
                2028: D("2400.00"),
                2029: D("1440.00"),
                2030: D("960.00"),
            },
        )

    def test_at_ekfz_no_special_depreciation(self):
        with self.assertRaises(dep.DepreciationError):
            dep.schedule(
                asset(
                    method="ev_7_2a",
                    ev_rates=EV_RATES,
                    group="vehicle",
                    events=(dep.Event("sec7g_special", date(2024, 2, 1), D("100")),),
                )
            )

    def test_at_dig_1_digital_asset(self):
        """AT-DIG-1: full cost in the year of delivery, no monthly rule; the
        asset stays in Anlage AVEÜR."""
        entry = dep.RegisterAsset(
            asset(
                method="digital_1y",
                delivery_date=date(2026, 11, 5),
                cost=D("1500.00"),
                useful_life_months=0,
                group="office_equipment",
            )
        )
        rows = dep.schedule(entry.asset)
        self.assertEqual(depreciation(rows), {2026: D("1500.00")})
        self.assertEqual(
            dep.field_contributions(2026, [entry]),
            {"AVEUER.40.412": D("1500.00"), "AVEUER.40.414": D("1500.00")},
        )
        # present in 2027 with its cost, book value 0
        self.assertEqual(
            dep.field_contributions(2027, [entry]), {"AVEUER.40.410": D("1500.00")}
        )

    def test_at_man_1_manual(self):
        """AT-MAN-1: yearly amounts as events, at most the book value."""
        building = asset(
            method="manual",
            delivery_date=date(2025, 1, 1),
            cost=D("300000.00"),
            useful_life_months=0,
            group="building",
            events=(
                dep.Event("manual_depreciation", date(2025, 12, 31), D("9000.00")),
                dep.Event("manual_depreciation", date(2026, 12, 31), D("9000.00")),
            ),
        )
        self.assertEqual(
            depreciation(dep.schedule(building)), {2025: D("9000"), 2026: D("9000")}
        )
        too_much = asset(
            method="manual",
            cost=D("100.00"),
            useful_life_months=0,
            events=(dep.Event("manual_depreciation", date(2024, 12, 31), D("100.01")),),
        )
        with self.assertRaises(dep.DepreciationError):
            dep.schedule(too_much)


class TestSpecialDepreciation(BaseCase):
    def special(self, *amounts, delivery=date(2024, 1, 5)):
        return asset(
            delivery_date=delivery,
            cost=D("10000.00"),
            useful_life_months=120,
            events=tuple(
                dep.Event("sec7g_special", date(delivery.year + index, 6, 1), D(amount))
                for index, amount in enumerate(amounts)
                if D(amount)
            ),
        )

    def test_at_7g_1_limit_40_percent(self):
        """AT-7G-1: at most 40 % of the cost from 2024, 20 % before."""
        dep.check_special_limit(self.special("2500", "1500"), 0.4)
        with self.assertRaises(dep.DepreciationError):
            dep.check_special_limit(self.special("2500", "1500.01"), 0.4)
        with self.assertRaises(dep.DepreciationError):
            dep.check_special_limit(
                self.special("2000.01", delivery=date(2023, 1, 5)), 0.2
            )

    def test_at_7g_2_five_years(self):
        """AT-7G-2: year of delivery and the four following years only."""
        dep.schedule(self.special("1000", "0", "0", "0", "1000"))
        with self.assertRaises(dep.DepreciationError):
            dep.schedule(self.special("1000", "0", "0", "0", "0", "1000"))

    def test_at_7g_3_rest_over_remaining_life(self):
        """AT-7G-3: next to linear J = 1,000; after 2028 the book value 1,000
        is spread over the remaining 60 months: 200 per year (§ 7a (9))."""
        rows = dep.schedule(self.special("2500", "1500"))
        self.assertEqual(rows[0].special_depreciation, D("2500"))
        self.assertEqual(rows[1].special_depreciation, D("1500"))
        self.assertEqual(
            depreciation(rows),
            {
                2024: 1000,
                2025: 1000,
                2026: 1000,
                2027: 1000,
                2028: 1000,
                2029: 200,
                2030: 200,
                2031: 200,
                2032: 200,
                2033: 200,
            },
        )
        self.assertEqual(rows[-1].book_value_end, D("0.00"))

    def test_movable_assets_only(self):
        with self.assertRaises(dep.DepreciationError):
            dep.schedule(
                asset(
                    group="building",
                    events=(dep.Event("sec7g_special", date(2024, 6, 1), D("10")),),
                )
            )


class TestEvents(BaseCase):
    def test_at_nak_1_additional_cost(self):
        """AT-NAK-1: additional cost of 600 in 2025 counts from 1 January; new
        remaining life 24 months: (2,400 + 600) ÷ 24 × 12 = 1,500."""
        rows = dep.schedule(
            asset(
                cost=D("3600.00"),
                events=(dep.Event("additional_cost", date(2025, 5, 1), D("600"), 24),),
            )
        )
        self.assertEqual(depreciation(rows), {2024: 1200, 2025: 1500, 2026: 1500})
        self.assertEqual(rows[1].additions, D("600"))
        self.assertEqual(rows[2].cost_start, D("4200.00"))

    def test_at_zus_1_subsidy(self):
        """AT-ZUS-1: subsidy approved 2025 is a negative addition; the basis
        is 8,000 − 1,000 over the remaining 48 months: 1,750 per year."""
        rows = dep.schedule(
            asset(
                cost=D("10000.00"),
                useful_life_months=60,
                events=(dep.Event("subsidy", date(2025, 5, 1), D("1000")),),
            )
        )
        self.assertEqual(
            depreciation(rows),
            {2024: 2000, 2025: 1750, 2026: 1750, 2027: 1750, 2028: 1750},
        )
        self.assertEqual(rows[1].additions, D("-1000.00"))
        self.assertEqual(rows[2].cost_start, D("9000.00"))
        first_year = dep.schedule(
            asset(
                cost=D("10000.00"),
                useful_life_months=60,
                events=(dep.Event("subsidy", date(2024, 2, 1), D("2000")),),
            )
        )
        self.assertEqual(first_year[0].additions, D("8000.00"))
        self.assertEqual(first_year[0].depreciation, D("1600.00"))

    def test_extraordinary_spreads_rest(self):
        """AfaA 400 in 2024; then (1,200 − 400 − 400) over 24 months."""
        rows = dep.schedule(
            asset(events=(dep.Event("extraordinary", date(2024, 9, 1), D("400")),))
        )
        self.assertEqual(depreciation(rows), {2024: 800, 2025: 200, 2026: 200})


class TestDisposal(BaseCase):
    def test_at_abg_1_sale_month_convention(self):
        """AT-ABG-1: J = 400; sale 30.06.2025: 6 months 200, residual book
        value 600 (Kz 135); without the month of disposal 166.67 / 633.33."""
        rows = dep.schedule(
            asset(disposal_date=date(2025, 6, 30), disposal_kind="sale")
        )
        self.assertEqual(rows[-1].depreciation, D("200.00"))
        self.assertEqual(rows[-1].disposal_book_value, D("600.00"))
        rows = dep.schedule(
            asset(
                disposal_date=date(2025, 6, 30),
                disposal_kind="sale",
                disposal_month_counts=False,
            )
        )
        self.assertEqual(rows[-1].depreciation, D("166.67"))
        self.assertEqual(rows[-1].disposal_book_value, D("633.33"))

    def test_at_abg_4_not_depreciable_at_receipt(self):
        """AT-ABG-4: land sold 11/2026, proceeds received 01/2027: the cost is
        an expense in 2027 (§ 4 (3) sentence 4 EStG)."""
        land = asset(
            kind="non_depreciable",
            method="none",
            cost=D("50000.00"),
            useful_life_months=0,
            group="land",
            disposal_date=date(2026, 11, 5),
            disposal_effective_date=date(2027, 1, 15),
            disposal_kind="sale",
        )
        entry = dep.RegisterAsset(land)
        self.assertEqual(
            dep.field_contributions(2026, [entry]),
            {"AVEUER.40.100": D("50000.00"), "AVEUER.40.101": D("50000.00")},
        )
        self.assertEqual(
            dep.field_contributions(2027, [entry]),
            {
                "AVEUER.40.100": D("50000.00"),
                "AVEUER.40.101": D("50000.00"),
                "AVEUER.40.105": D("50000.00"),
            },
        )

    def test_current_assets_into_goods(self):
        """Current assets (§ 4 (3) sentence 4): disposal → Kz 605 and 100."""
        shares = dep.RegisterAsset(
            asset(
                kind="current_4_3_4",
                method="none",
                cost=D("2000.00"),
                useful_life_months=0,
                group="current",
                disposal_date=date(2026, 3, 1),
                disposal_kind="sale",
            )
        )
        self.assertEqual(
            dep.field_contributions(2026, [shares]),
            {
                "AVEUER.40.600": D("2000.00"),
                "AVEUER.40.605": D("2000.00"),
                "EUER.25.100": D("2000.00"),
            },
        )

    def test_at_abg_2_withdrawal(self):
        """AT-ABG-2: withdrawal with fair value 500 and VAT 95 → Kz 102 500,
        140 95, 122 595; the residual book value is a disposal (Kz 135)."""
        entry = dep.RegisterAsset(
            asset(
                cost=D("1000.00"),
                disposal_date=date(2025, 6, 30),
                disposal_kind="withdrawal",
            ),
            disposal_value=D("500.00"),
            withdrawal_vat=D("95.00"),
        )
        values = dep.field_contributions(2025, [entry])
        self.assertEqual(values["EUER.20.102"], D("500.00"))
        self.assertEqual(values["EUER.20.140"], D("95.00"))
        self.assertEqual(values["EUER.29.122"], D("595.00"))
        # J = 333.33, 6 months 166.67, residual 1000 − 333.33 − 166.67 = 500
        self.assertEqual(values["AVEUER.40.424"], D("166.67"))
        self.assertEqual(values["AVEUER.40.425"], D("500.00"))


class TestLowValueAndPools(BaseCase):
    def test_gwg_in_year_of_delivery(self):
        """AT-GWG-4: deduction in the year of delivery (Kz 132), not of payment;
        no entry in Anlage AVEÜR."""
        gwg = dep.RegisterAsset(
            asset(
                kind="gwg",
                method="none",
                delivery_date=date(2026, 12, 20),
                cost=D("940.00"),
                useful_life_months=0,
            )
        )
        self.assertEqual(
            dep.field_contributions(2026, [gwg]), {"EUER.25.132": D("940.00")}
        )
        self.assertEqual(dep.field_contributions(2027, [gwg]), {})

    def test_at_pool_1_2_releases(self):
        """AT-POOL-1/2: 2,345.67 ÷ 5 = 469.13; the fifth year 469.15."""
        self.assertEqual(
            dep.pool_releases(D("2345.67")),
            [D("469.13"), D("469.13"), D("469.13"), D("469.13"), D("469.15")],
        )
        pool = dep.Pool(2024, D("2345.67"))
        self.assertEqual(
            dep.field_contributions(2024, [], [pool]),
            {"AVEUER.40.432": D("2345.67"), "AVEUER.40.434": D("469.13")},
        )
        self.assertEqual(
            dep.field_contributions(2026, [], [pool]),
            {
                "AVEUER.40.450": D("2345.67"),
                "AVEUER.40.451": D("1407.41"),
                "AVEUER.40.454": D("469.13"),
            },
        )
        self.assertEqual(
            dep.field_contributions(2028, [], [pool]),
            {
                "AVEUER.40.470": D("2345.67"),
                "AVEUER.40.471": D("469.15"),
                "AVEUER.40.474": D("469.15"),
            },
        )
        self.assertEqual(dep.field_contributions(2029, [], [pool]), {})

    def test_at_pool_3_disposal_without_effect(self):
        """AT-POOL-3: a pool asset has no schedule of its own; its sale does
        not change the pool."""
        pool_asset = asset(
            kind="pool",
            method="none",
            cost=D("600.00"),
            useful_life_months=0,
            disposal_date=date(2025, 3, 1),
            disposal_kind="sale",
        )
        self.assertEqual(dep.schedule(pool_asset), [])
        self.assertEqual(
            dep.field_contributions(2025, [dep.RegisterAsset(pool_asset)]), {}
        )


class TestContributionPrivateUse(BaseCase):
    def test_at_einl_1_contribution(self):
        """AT-EINL-1: private cost 1,200 (03/2025, life 60 months) contributed
        09/2026: 18 months used = 360, reduced cost 840 < fair value 900."""
        value = dep.contribution_value(
            D("900"), D("1200"), date(2025, 3, 10), date(2026, 9, 1), 60
        )
        self.assertEqual(value, D("840.00"))
        # acquired more than three years before: fair value
        self.assertEqual(
            dep.contribution_value(
                D("900"), D("1200"), date(2023, 8, 31), date(2026, 9, 1), 60
            ),
            D("900.00"),
        )
        entry = dep.RegisterAsset(
            asset(delivery_date=date(2026, 9, 1), cost=value, useful_life_months=42),
            contribution_value=value,
        )
        values = dep.field_contributions(2026, [entry])
        self.assertEqual(values["EUER.29.123"], D("840.00"))
        # 840 ÷ 42 × 12 = 240, 4 months: 80
        self.assertEqual(values["AVEUER.40.424"], D("80.00"))

    def test_at_priv_1_use_withdrawal(self):
        """AT-PRIV-1 (synthetic): (285.57 + 100) × 20 % = 77.11; VAT only on
        expenses with input tax (none for a small business)."""
        self.assertEqual(
            dep.private_use_proposal(D("285.57"), D("100"), 0.8, D(0), D("0.19")),
            (D("77.11"), D("0.00")),
        )
        self.assertEqual(
            dep.private_use_proposal(
                D("285.57"), D("100"), 0.8, D("385.57"), D("0.19")
            ),
            (D("77.11"), D("14.65")),
        )
        entry = dep.RegisterAsset(asset(), private_use=(D("77.11"), D("14.65")))
        values = dep.field_contributions(2024, [entry])
        self.assertEqual(values["EUER.20.108"], D("77.11"))
        self.assertEqual(values["EUER.20.140"], D("14.65"))
        self.assertEqual(values["EUER.29.122"], D("91.76"))

    def test_at_15a_1_formula_help(self):
        """AT-15A-1: 1,900 € input tax, delivery 07/2025, change 01.01.2027:
        42 of 60 months, 380 per year, 190 in 2030; 380 € is a bagatelle."""
        amounts, bagatelle = dep.sec15a_correction(
            D("1900"), date(2025, 7, 1), date(2027, 1, 1)
        )
        self.assertEqual(
            amounts,
            {
                2027: D("380.00"),
                2028: D("380.00"),
                2029: D("380.00"),
                2030: D("190.00"),
            },
        )
        self.assertFalse(bagatelle)
        _amounts, bagatelle = dep.sec15a_correction(
            D("380"), date(2025, 7, 1), date(2027, 1, 1)
        )
        self.assertTrue(bagatelle)


class TestReferenceCase(BaseCase):
    """Synthetic counterpart of the reference case (spec 6.7).

    Two linear assets in the group of other movable assets, rounding per
    asset, AVEÜR book values year by year and a takeover on 31.12.2026 with
    the residual book values that continue unchanged.
    """

    A = asset(delivery_date=date(2021, 5, 3), cost=D("1999.00"), useful_life_months=84)
    B = asset(delivery_date=date(2023, 10, 3), cost=D("869.99"), useful_life_months=60)

    def test_reference_depreciation(self):
        """A: 285.57 per year (rest 95.20 in 2028); B: 869.99 ÷ 5 = 174.00,
        3 months 43.50 in 2023, rest 130.49 in 2028 (9 months)."""
        a, b = depreciation(dep.schedule(self.A)), depreciation(dep.schedule(self.B))
        expected = {
            2024: D("459.57"),
            2025: D("459.57"),
            2026: D("459.57"),
            2027: D("459.57"),
            2028: D("225.69"),
        }
        self.assertEqual({year: a[year] + b[year] for year in expected}, expected)
        self.assertEqual(b[2023], D("43.50"))
        self.assertEqual(b[2028], D("130.49"))

    def test_reference_aveuer_book_values(self):
        """BW beginning 2024: A 1,999 − 190.38 − 2 × 285.57 = 1,237.48;
        B 869.99 − 43.50 = 826.49; sum 2,063.97. End 2024 1,604.40, 2025
        1,144.83, 2026 685.26, 2027 225.69, 2028 0."""
        entries = [dep.RegisterAsset(self.A), dep.RegisterAsset(self.B)]
        starts = {}
        for year in range(2024, 2029):
            values = dep.field_contributions(year, entries)
            starts[year] = values["AVEUER.40.421"]
            self.assertEqual(values["AVEUER.40.420"], D("2868.99"))
        self.assertEqual(
            starts,
            {
                2024: D("2063.97"),
                2025: D("1604.40"),
                2026: D("1144.83"),
                2027: D("685.26"),
                2028: D("225.69"),
            },
        )
        self.assertEqual(
            dep.field_contributions(2029, entries), {"AVEUER.40.420": D("2868.99")}
        )

    def test_reference_takeover_continues(self):
        """Takeover 31.12.2026: A 380.77, B 304.49 (sum 685.26) continue with
        285.57 + 174.00 in 2027 and 95.20 + 130.49 in 2028."""
        took_a = asset(
            delivery_date=date(2021, 5, 3),
            cost=D("1999.00"),
            useful_life_months=84,
            opening_date=date(2026, 12, 31),
            opening_book_value=D("380.77"),
        )
        took_b = asset(
            delivery_date=date(2023, 10, 3),
            cost=D("869.99"),
            useful_life_months=60,
            opening_date=date(2026, 12, 31),
            opening_book_value=D("304.49"),
        )
        self.assertEqual(
            depreciation(dep.schedule(took_a)), {2027: D("285.57"), 2028: D("95.20")}
        )
        self.assertEqual(
            depreciation(dep.schedule(took_b)), {2027: D("174.00"), 2028: D("130.49")}
        )
        self.assertEqual(dep.year_values(took_a, 2026), None)
        entries = [dep.RegisterAsset(took_a), dep.RegisterAsset(took_b)]
        native = [dep.RegisterAsset(self.A), dep.RegisterAsset(self.B)]
        for year in (2027, 2028, 2029):
            self.assertEqual(
                dep.field_contributions(year, entries),
                dep.field_contributions(year, native),
            )


class TestTransfers(BaseCase):
    def test_transfer_differences(self):
        """F-04: 490 = 130; 324 = 131; 190 = 136; 480 + 113 = 134; 499 = 137;
        sum of disposals = 135."""
        values = {
            "AVEUER.40.490": D("100"),
            "EUER.25.130": D("100"),
            "AVEUER.40.425": D("50"),
            "AVEUER.40.105": D("10"),
            "EUER.25.135": D("60"),
        }
        self.assertEqual(dep.transfer_differences(values), {})
        values["EUER.25.135"] = D("50")
        self.assertEqual(
            dep.transfer_differences(values), {"EUER.25.135": (D("50"), D("60"))}
        )

    def test_home_office(self):
        """40.214 → 25.172 (deductible) or 25.162."""
        office = asset(
            method="manual",
            cost=D("20000"),
            useful_life_months=0,
            group="office_building",
            events=(dep.Event("manual_depreciation", date(2024, 12, 31), D("400")),),
        )
        self.assertEqual(
            dep.field_contributions(2024, [dep.RegisterAsset(office)]),
            {
                "AVEUER.40.212": D("20000.00"),
                "AVEUER.40.214": D("400.00"),
                "EUER.25.172": D("400.00"),
            },
        )
        values = dep.field_contributions(
            2024, [dep.RegisterAsset(office, home_office_deductible=False)]
        )
        self.assertEqual(values["EUER.25.162"], D("400.00"))
        self.assertNotIn("EUER.25.172", values)

    def test_total_check(self):
        """C-67: depreciation over the life = cost less reductions."""
        rows = dep.schedule(asset())
        self.assertTrue(dep.total_check(rows, D("1200.00")))
        self.assertFalse(dep.total_check(rows[:-1], D("1200.00")))


class TestBenefitPeriod(BaseCase):
    """§ 7a (1) and (9) EStG: the period of a special depreciation."""

    def h7a_example(self, last_special="40"):
        """H 7a EStH "Nachträgliche AK/HK" with delivery in January 2016 (01):
        cost 10,000, useful life 10 years, § 7g 20 % in 01, additional
        production cost 1,800 in December 04 and 200 in January 05."""
        return asset(
            delivery_date=date(2016, 1, 5),
            cost=D("10000.00"),
            useful_life_months=120,
            events=(
                dep.Event("sec7g_special", date(2016, 6, 1), D("2000")),
                dep.Event("additional_cost", date(2019, 12, 1), D("1800")),
                dep.Event("sec7g_special", date(2019, 12, 31), D("360")),
                dep.Event("additional_cost", date(2020, 1, 15), D("200")),
                dep.Event("sec7g_special", date(2020, 12, 31), D(last_special)),
            ),
        )

    def test_at_7g_4_additional_cost_in_period(self):
        """AT-7G-4: 3 × 1,000; 04: 10 % of 11,800 = 1,180; 05: 10 % of 12,000
        = 1,200; residual value 31.12.05 4,220 (H 7a); then 4,220 over the
        remaining 60 months: 844 per year."""
        rows = dep.schedule(self.h7a_example())
        self.assertEqual(
            depreciation(rows),
            {
                2016: D("1000.00"),
                2017: D("1000.00"),
                2018: D("1000.00"),
                2019: D("1180.00"),
                2020: D("1200.00"),
                2021: D("844.00"),
                2022: D("844.00"),
                2023: D("844.00"),
                2024: D("844.00"),
                2025: D("844.00"),
            },
        )
        self.assertEqual(rows[4].book_value_end, D("4220.00"))
        self.assertTrue(all(row.is_consistent() for row in rows))

    def test_at_7g_limit_follows_the_basis(self):
        """§ 7a (1): 20 % of 11,800 = 2,360 in 04 and of 12,000 = 2,400 in 05
        are allowed; a subsidy lowers the limit (40 % of 8,000 = 3,200)."""
        dep.check_special_limit(self.h7a_example(), 0.2)
        with self.assertRaises(dep.DepreciationError):
            dep.check_special_limit(self.h7a_example("40.01"), 0.2)
        subsidised = asset(
            delivery_date=date(2024, 1, 5),
            cost=D("10000.00"),
            useful_life_months=120,
            events=(
                dep.Event("subsidy", date(2024, 3, 1), D("2000")),
                dep.Event("sec7g_special", date(2024, 6, 1), D("3200")),
            ),
        )
        dep.check_special_limit(subsidised, 0.4)
        too_much = asset(
            delivery_date=date(2024, 1, 5),
            cost=D("10000.00"),
            useful_life_months=120,
            events=(
                dep.Event("subsidy", date(2024, 3, 1), D("2000")),
                dep.Event("sec7g_special", date(2024, 6, 1), D("3200.01")),
            ),
        )
        with self.assertRaises(dep.DepreciationError):
            dep.check_special_limit(too_much, 0.4)

    def test_subsidy_in_period(self):
        """Subsidy 1,000 in the year after the special depreciation: 10 % of
        9,000 = 900 up to the end of the period, then the book value 2,400
        over the remaining 60 months (480)."""
        rows = dep.schedule(
            asset(
                delivery_date=date(2024, 1, 5),
                cost=D("10000.00"),
                useful_life_months=120,
                events=(
                    dep.Event("sec7g_special", date(2024, 6, 1), D("2000")),
                    dep.Event("subsidy", date(2025, 3, 1), D("1000")),
                ),
            )
        )
        values = depreciation(rows)
        self.assertEqual(
            [values[year] for year in range(2025, 2029)], [D("900.00")] * 4
        )
        self.assertEqual(values[2029], D("480.00"))

    def test_degressive_continues_after_period(self):
        """R 7a (10) sentence 3 EStR: 10,000, 07/2025, 15 years, 20 %, § 7g
        4,000: 2025 1,000; 2026 1,000; 2027 800; 2028 640; 2029 512; 2030
        degressive 2,048 × 20 % = 409.60 (linear would be 195.05)."""
        rows = dep.schedule(
            asset(
                method="degressive",
                degressive_rate=D("0.2"),
                delivery_date=date(2025, 7, 1),
                cost=D("10000.00"),
                useful_life_months=180,
                events=(dep.Event("sec7g_special", date(2025, 9, 1), D("4000")),),
            )
        )
        values = depreciation(rows)
        self.assertEqual(values[2029], D("512.00"))
        self.assertEqual(values[2030], D("409.60"))

    def test_takeover_with_events_before(self):
        """Takeover after an extraordinary depreciation or a § 7g special
        depreciation continues as without takeover."""
        afaa = asset(events=(dep.Event("extraordinary", date(2024, 9, 1), D("400")),))
        took = asset(
            events=afaa.events,
            opening_date=date(2024, 12, 31),
            opening_book_value=D("400.00"),
        )
        self.assertEqual(depreciation(dep.schedule(took)), {2025: 200, 2026: 200})
        self.assertEqual(
            depreciation(dep.schedule(took)),
            {k: v for k, v in depreciation(dep.schedule(afaa)).items() if k > 2024},
        )
        special = (dep.Event("sec7g_special", date(2024, 6, 1), D("4000")),)
        native = asset(
            delivery_date=date(2024, 1, 5),
            cost=D("10000.00"),
            useful_life_months=120,
            events=special,
        )
        taken = asset(
            delivery_date=date(2024, 1, 5),
            cost=D("10000.00"),
            useful_life_months=120,
            events=special,
            opening_date=date(2025, 12, 31),
            opening_book_value=D("4000.00"),
        )
        expected = {k: v for k, v in depreciation(dep.schedule(native)).items()}
        self.assertEqual(
            depreciation(dep.schedule(taken)),
            {year: amount for year, amount in expected.items() if year >= 2026},
        )
        self.assertEqual(expected[2028], D("1000.00"))
        self.assertEqual(expected[2029], D("200.00"))

    def test_takeover_degressive_after_switch(self):
        """50,000, 07/2025, 10 years, 30 %: previous system 2025 degressive
        7,500, from 2026 linear 42,500 ÷ 114 × 12 = 4,473.68 (BW 31.12.2026
        38,026.32); the register continues linearly: 38,026.32 ÷ 102 × 12."""
        values = {
            "method": "degressive",
            "degressive_rate": D("0.3"),
            "delivery_date": date(2025, 7, 1),
            "cost": D("50000.00"),
            "useful_life_months": 120,
            "switch_to_linear_year": 2026,
        }
        native = depreciation(dep.schedule(asset(**values)))
        self.assertEqual(native[2026], D("4473.68"))
        taken = depreciation(
            dep.schedule(
                asset(
                    **values,
                    opening_date=date(2026, 12, 31),
                    opening_book_value=D("38026.32"),
                )
            )
        )
        self.assertEqual(taken[2027], D("4473.68"))

    def test_sec7b_buildings(self):
        """§ 7b: 300,000, 01/2024, 400 months (3 %): J = 9,000 and 5 % =
        15,000 special depreciation in 2024–2027 (Kz 113); from 2028 the book
        value 204,000 over the remaining 352 months: 6,954.55 (§ 7a (9))."""
        events = tuple(
            dep.Event("sec7b_special", date(year, 12, 31), D("15000"))
            for year in range(2024, 2028)
        )
        building = asset(
            delivery_date=date(2024, 1, 15),
            cost=D("300000.00"),
            useful_life_months=400,
            group="building",
            events=events,
        )
        dep.check_sec7b_limit(building, 0.05)
        rows = dep.schedule(building)
        self.assertEqual(rows[3].book_value_end, D("204000.00"))
        self.assertEqual(depreciation(rows)[2028], D("6954.55"))
        values = dep.field_contributions(2024, [dep.RegisterAsset(building)])
        self.assertEqual(values["AVEUER.40.113"], D("15000.00"))
        self.assertEqual(values["AVEUER.40.114"], D("9000.00"))
        with self.assertRaises(dep.DepreciationError):
            dep.check_sec7b_limit(
                asset(
                    delivery_date=date(2024, 1, 15),
                    cost=D("300000.00"),
                    useful_life_months=400,
                    group="building",
                    events=(
                        dep.Event("sec7b_special", date(2024, 12, 31), D("15000.01")),
                    ),
                ),
                0.05,
            )
        for group, when, code in (
            ("other_movable", date(2024, 12, 31), "sec7b_group"),
            ("building", date(2028, 12, 31), "sec7b_period"),
        ):
            with self.assertRaises(dep.DepreciationError) as error:
                dep.validate(
                    asset(
                        delivery_date=date(2024, 1, 15),
                        cost=D("300000.00"),
                        useful_life_months=400,
                        group=group,
                        events=(dep.Event("sec7b_special", when, D("100")),),
                    )
                )
            self.assertEqual(error.exception.code, code)


class TestRuleLimits(BaseCase):
    def test_ekfz_with_additional_cost(self):
        """§ 7 (2a): percentages of the cost 62,000 including the additional
        cost of the year of delivery: 46,500 / 6,200 / 3,100 / 3,100 / 1,860 /
        1,240."""
        rows = dep.schedule(
            asset(
                method="ev_7_2a",
                delivery_date=date(2025, 8, 1),
                cost=D("60000.00"),
                useful_life_months=0,
                ev_rates=EV_RATES,
                group="vehicle",
                events=(dep.Event("additional_cost", date(2025, 10, 1), D("2000")),),
            )
        )
        self.assertEqual(
            list(depreciation(rows).values()),
            [
                D("46500.00"),
                D("6200.00"),
                D("3100.00"),
                D("3100.00"),
                D("1860.00"),
                D("1240.00"),
            ],
        )

    def test_short_useful_life(self):
        """H 7.4 "Nutzungsdauer": up to 12 months no depreciation."""
        with self.assertRaises(dep.DepreciationError) as error:
            dep.validate(asset(useful_life_months=12))
        self.assertEqual(error.exception.code, "short_life")
        dep.validate(asset(useful_life_months=13))

    def test_events_of_depreciable_assets_only(self):
        """§ 7 (1) sentence 7: no AfaA and no new useful life for land or
        current assets."""
        for kind, group, event in (
            ("non_depreciable", "other_movable", "extraordinary"),
            ("non_depreciable", "land", "extraordinary"),
            ("current_4_3_4", "current", "useful_life_change"),
        ):
            with self.assertRaises(dep.DepreciationError) as error:
                dep.validate(
                    asset(
                        kind=kind,
                        method="none",
                        useful_life_months=0,
                        group=group,
                        events=(dep.Event(event, date(2025, 3, 1), D("400"), 24),),
                    )
                )
            self.assertEqual(error.exception.code, "event_kind")

    def test_subsidy_before_delivery(self):
        """A subsidy approved before the delivery reduces the addition of the
        year of delivery; other events cannot be dated before it."""
        rows = dep.schedule(
            asset(
                delivery_date=date(2025, 6, 1),
                cost=D("10000.00"),
                useful_life_months=60,
                events=(dep.Event("subsidy", date(2025, 3, 1), D("1000")),),
            )
        )
        self.assertEqual(rows[0].additions, D("9000.00"))
        # 9,000 ÷ 5 = 1,800, 7 months: 1,050
        self.assertEqual(rows[0].depreciation, D("1050.00"))
        with self.assertRaises(dep.DepreciationError):
            dep.validate(
                asset(
                    delivery_date=date(2025, 6, 1),
                    events=(dep.Event("additional_cost", date(2025, 3, 1), D("100")),),
                )
            )

    def test_withdrawal_of_current_assets(self):
        """Withdrawal of current assets: fair value in Kz 108 (Zeile 21), not
        in Kz 102; Kz 122 as before."""
        entry = dep.RegisterAsset(
            asset(
                kind="current_4_3_4",
                method="none",
                cost=D("5000.00"),
                useful_life_months=0,
                group="current",
                delivery_date=date(2024, 3, 1),
                disposal_date=date(2025, 5, 1),
                disposal_kind="withdrawal",
            ),
            disposal_value=D("6000.00"),
        )
        values = dep.field_contributions(2025, [entry])
        self.assertEqual(values["EUER.20.108"], D("6000.00"))
        self.assertEqual(values["EUER.25.100"], D("5000.00"))
        self.assertEqual(values["EUER.29.122"], D("6000.00"))
        self.assertNotIn("EUER.20.102", values)

    def test_at_priv_2_vat_basis_over_correction_period(self):
        """AT-PRIV-2: § 10 (4) sentence 1 no. 2 UStG: 10,000 net spread over
        60 months: 2,000 per year 2024–2028, nothing later; with 20 % private
        use 400 × 19 % = 76.00 VAT per year. Below 500 € in the year of
        delivery only."""
        bases = [
            dep.private_use_vat_base(D("10000"), date(2024, 1, 10), year)
            for year in range(2024, 2030)
        ]
        self.assertEqual(bases, [D("2000.00")] * 5 + [D("0.00")])
        self.assertEqual(
            dep.private_use_proposal(D("1000"), D("0"), 0.8, bases[0], D("0.19")),
            (D("200.00"), D("76.00")),
        )
        self.assertEqual(
            dep.private_use_vat_base(D("499.99"), date(2024, 7, 1), 2024),
            D("499.99"),
        )
        self.assertEqual(
            dep.private_use_vat_base(D("499.99"), date(2024, 7, 1), 2025), D("0")
        )
        total = sum(
            dep.private_use_vat_base(D("999.99"), date(2024, 7, 10), year)
            for year in range(2024, 2031)
        )
        self.assertEqual(total, D("999.99"))
        # shorter useful life: § 15a (5) sentence 2 UStG
        self.assertEqual(
            dep.private_use_vat_base(D("3600"), date(2024, 1, 1), 2025, False, 36),
            D("1200.00"),
        )

    def test_locked_values_replace_the_calculation(self):
        """A locked year keeps its values in the field contributions."""
        frozen = dep.YearValues(
            year=2025,
            cost_start=D("1200.00"),
            book_value_start=D("800.00"),
            additions=D("0.00"),
            special_depreciation=D("0.00"),
            depreciation=D("300.00"),
            disposal_book_value=D("0.00"),
            book_value_end=D("500.00"),
        )
        entry = dep.RegisterAsset(asset(), locked_values=frozen)
        values = dep.field_contributions(2025, [entry])
        self.assertEqual(values["AVEUER.40.424"], D("300.00"))
        self.assertEqual(
            dep.field_contributions(2026, [entry])["AVEUER.40.424"], D("400.00")
        )


class TestBuildings(BaseCase):
    """§ 7 (4) sentence 1 EStG: a percentage of the basis (H 7.4 EStH)."""

    def building(self, **values):
        defaults = {
            "delivery_date": date(2020, 1, 15),
            "cost": D("300000.00"),
            "useful_life_months": 400,  # 3 %
            "group": "building",
        }
        defaults.update(values)
        return asset(**defaults)

    def test_additional_cost_keeps_the_percentage(self):
        """H 7.4 "Nachträgliche AK/HK": 300,000 from 01/2015, additional
        production cost 30,000 in 2025: (300,000 + 30,000) × 3 % = 9,900 in
        2025 and 2026, not the book value 240,000 over the remaining 280
        months (10,285.71). The depreciation runs until the book value is
        used up, beyond the original useful life: 240,000 − 24 × 9,900 =
        2,400 are left for 2049."""
        rows = dep.schedule(
            self.building(
                delivery_date=date(2015, 1, 15),
                events=(dep.Event("additional_cost", date(2025, 6, 1), D("30000")),),
            )
        )
        values = depreciation(rows)
        self.assertEqual(values[2024], D("9000.00"))
        self.assertEqual((values[2025], values[2026]), (D("9900.00"), D("9900.00")))
        self.assertEqual(values[2048], D("9900.00"))
        self.assertEqual(values[2049], D("2400.00"))
        self.assertEqual(rows[-1].year, 2049)
        self.assertEqual(rows[-1].book_value_end, D("0.00"))
        self.assertEqual(sum(values.values()), D("330000.00"))
        self.assertTrue(all(row.is_consistent() for row in rows))

    def test_later_subsidy_reduces_the_basis(self):
        """R 7.3 (4) sentence 2 EStR: subsidy 30,000 approved in 2021:
        (300,000 − 30,000) × 3 % = 8,100 (not 261,000 ÷ 388 × 12)."""
        rows = dep.schedule(
            self.building(
                events=(dep.Event("subsidy", date(2021, 4, 1), D("30000")),),
            )
        )
        values = depreciation(rows)
        self.assertEqual(values[2020], D("9000.00"))
        self.assertEqual(values[2021], D("8100.00"))
        self.assertEqual(values[2022], D("8100.00"))
        self.assertEqual(sum(values.values()), D("270000.00"))

    def test_extraordinary_depreciation_reduces_the_basis(self):
        """§ 11c (2) EStDV: AfaA 50,000 in 2024; from 2025 (300,000 − 50,000)
        × 3 % = 7,500 (not 205,000 ÷ 340 × 12 = 7,235.29). A takeover after
        the AfaA continues the same way."""
        afaa = (dep.Event("extraordinary", date(2024, 8, 1), D("50000")),)
        rows = dep.schedule(self.building(events=afaa))
        values = depreciation(rows)
        self.assertEqual(values[2024], D("59000.00"))
        self.assertEqual(values[2025], D("7500.00"))
        self.assertEqual(rows[4].book_value_end, D("205000.00"))
        taken = dep.schedule(
            self.building(
                events=afaa,
                opening_date=date(2024, 12, 31),
                opening_book_value=D("205000.00"),
            )
        )
        self.assertEqual(
            depreciation(taken),
            {year: amount for year, amount in values.items() if year > 2024},
        )

    def test_remaining_life_with_the_event(self):
        """A remaining useful life entered with the additional cost spreads the
        book value over it (§ 7 (4) sentence 2 EStG, BFH VIII R 105/73 in
        H 7.4): 240,000 ÷ 200 months × 12 = 14,400."""
        rows = dep.schedule(
            self.building(
                delivery_date=date(2015, 1, 15),
                events=(
                    dep.Event("additional_cost", date(2025, 6, 1), D("30000"), 200),
                ),
            )
        )
        self.assertEqual(depreciation(rows)[2025], D("14400.00"))

    def test_movable_assets_keep_the_remaining_life(self):
        """R 7.4 (9) sentence 1: other assets spread the book value over the
        remaining useful life (AT-NAK-1); only buildings keep the rate."""
        rows = dep.schedule(
            self.building(
                group="land_rights",
                delivery_date=date(2015, 1, 15),
                events=(dep.Event("additional_cost", date(2025, 6, 1), D("30000")),),
            )
        )
        self.assertEqual(depreciation(rows)[2025], D("10285.71"))


class TestReviewedLimits(BaseCase):
    def test_electric_vehicle_sold_in_the_last_year(self):
        """R 7.4 (8): 40,000, 09/2025, sold 15.03.2030: 2 % × 3/12 = 200, the
        residual book value 600 goes into Kz 135; sold 06/2027: 5 % × 6/12."""
        values = {
            "method": "ev_7_2a",
            "delivery_date": date(2025, 9, 1),
            "cost": D("40000.00"),
            "useful_life_months": 0,
            "ev_rates": EV_RATES,
            "group": "vehicle",
            "disposal_kind": "sale",
        }
        rows = dep.schedule(asset(disposal_date=date(2030, 3, 15), **values))
        self.assertEqual(rows[-1].year, 2030)
        self.assertEqual(rows[-1].depreciation, D("200.00"))
        self.assertEqual(rows[-1].disposal_book_value, D("600.00"))
        early = dep.schedule(asset(disposal_date=date(2027, 6, 30), **values))
        self.assertEqual(early[-1].depreciation, D("1000.00"))
        kept = dep.schedule(asset(**values))
        self.assertEqual(depreciation(kept)[2030], D("800.00"))

    def test_switch_to_linear_after_the_year_of_delivery(self):
        """§ 7 (3) EStG: a switch in the year of delivery is linear from the
        start."""
        values = {
            "method": "degressive",
            "degressive_rate": D("0.3"),
            "delivery_date": date(2025, 8, 1),
            "cost": D("10000.00"),
            "useful_life_months": 96,
        }
        for year in (2024, 2025):
            with self.assertRaises(dep.DepreciationError) as error:
                dep.validate(asset(switch_to_linear_year=year, **values))
            self.assertEqual(error.exception.code, "switch_year")
        dep.validate(asset(switch_to_linear_year=2026, **values))

    def test_subsidy_above_the_book_value(self):
        """A subsidy approved later reduces the cost at most to zero: a
        digital asset fully depreciated in 2025 or a book value of 333.34."""
        cases = (
            asset(
                method="digital_1y",
                delivery_date=date(2025, 3, 1),
                cost=D("1500.00"),
                useful_life_months=0,
                events=(dep.Event("subsidy", date(2026, 2, 1), D("300")),),
            ),
            asset(
                cost=D("1000.00"),
                events=(dep.Event("subsidy", date(2026, 2, 1), D("500")),),
            ),
        )
        for case, book_value in zip(cases, (D("0.00"), D("333.34")), strict=True):
            with self.assertRaises(dep.DepreciationError) as error:
                dep.schedule(case)
            self.assertEqual(error.exception.code, "subsidy_exceeds_book_value")
            self.assertEqual(error.exception.params["book_value"], book_value)
        allowed = asset(
            cost=D("1000.00"),
            events=(dep.Event("subsidy", date(2026, 2, 1), D("333.34")),),
        )
        self.assertEqual(dep.schedule(allowed)[-1].book_value_end, D("0.00"))

    def test_negative_events(self):
        """No negative extraordinary or special depreciation (a hidden
        write-up) and no negative remaining useful life."""
        cases = (
            (dep.Event("extraordinary", date(2025, 3, 1), D("-100")), "event_amount"),
            (dep.Event("sec7g_special", date(2025, 3, 1), D("-100")), "event_amount"),
            (dep.Event("sec7g_reduction", date(2025, 3, 1), D("-1")), "event_amount"),
            (
                dep.Event("additional_cost", date(2025, 3, 1), D("100"), -5),
                "event_life",
            ),
        )
        for event, code in cases:
            with self.subTest(event=event.type):
                with self.assertRaises(dep.DepreciationError) as error:
                    dep.validate(asset(events=(event,)))
                self.assertEqual(error.exception.code, code)

    def test_takeover_after_special_depreciation(self):
        """§ 7a (9): 10,000, 01/2024, 10 years, § 7g 4,000 in 2024, taken over
        31.12.2026 with 3,000: 1,000 in 2027 and 2028, then 1,000 over the
        remaining 60 months (200). Without the event the register would go on
        with 1,000 per year up to 2029."""
        values = {
            "delivery_date": date(2024, 1, 5),
            "cost": D("10000.00"),
            "useful_life_months": 120,
            "opening_date": date(2026, 12, 31),
            "opening_book_value": D("3000.00"),
        }
        special = (dep.Event("sec7g_special", date(2024, 12, 31), D("4000")),)
        rows = depreciation(dep.schedule(asset(events=special, **values)))
        self.assertEqual((rows[2027], rows[2028]), (D("1000.00"), D("1000.00")))
        self.assertEqual(rows[2029], D("200.00"))
        self.assertEqual(max(rows), 2033)
        without = depreciation(dep.schedule(asset(**values)))
        self.assertEqual(without, {2027: 1000, 2028: 1000, 2029: 1000})

    def test_low_value_asset_of_a_locked_year(self):
        """Kz 132 of a locked year comes from the locked values."""
        gwg = asset(kind="gwg", method="none", cost=D("50.00"), useful_life_months=0)
        frozen = dep.YearValues(
            year=2024,
            cost_start=D("0.00"),
            book_value_start=D("0.00"),
            additions=D("500.00"),
            special_depreciation=D("0.00"),
            depreciation=D("500.00"),
            disposal_book_value=D("0.00"),
            book_value_end=D("0.00"),
        )
        entry = dep.RegisterAsset(gwg, locked_values=frozen)
        self.assertEqual(
            dep.field_contributions(2024, [entry]), {"EUER.25.132": D("500.00")}
        )

    def test_remaining_life_of_a_contribution(self):
        """R 7.4 (10): bought privately 03/2023 with 84 months, contributed
        09/2025: 30 months used, 54 remain; acquired within three years."""
        self.assertEqual(
            dep.contribution_remaining_life(date(2023, 3, 10), date(2025, 9, 1), 84),
            54,
        )
        self.assertTrue(dep.contribution_reduced(date(2023, 3, 10), date(2025, 9, 1)))
        self.assertFalse(dep.contribution_reduced(date(2022, 9, 1), date(2025, 9, 1)))
