# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Unit tests of the profit corrections and the questionnaire, no database.

UT-PAU-1 to UT-PAU-8 (spec 9.2, R-44) on the pure Python engine
(:mod:`..models.engine.adjustment`) with the rule parameters of the years
2024 to 2026, and the evaluation of the questionnaire (R-52). Expected
values are computed by hand from the law and the official instructions; all
cases are synthetic (D6).
"""

import datetime
from decimal import Decimal as D

from odoo.tests import BaseCase

from ..models.engine import adjustment as adj
from ..models.engine import model as m
from ..models.engine import scope
from ..models.engine.parameters import Parameters


def _value(code, value, date_from="2024-01-01", date_to=None):
    return m.ParameterValue(
        code=code,
        value=D(value),
        date_from=datetime.date.fromisoformat(date_from),
        date_to=datetime.date.fromisoformat(date_to) if date_to else None,
    )


#: rule parameters as in data/parameters.xml (spec 3.2, steuerrecht.md 12)
PARAMS = Parameters(
    [
        _value("homeoffice_day", "6"),
        _value("homeoffice_max", "1260"),
        _value("study_flat", "1260"),
        _value("meal_8h", "14"),
        _value("meal_24h", "28"),
        _value("commute_rate_1_20", "0.30", date_to="2025-12-31"),
        _value("commute_rate_1_20", "0.38", date_from="2026-01-01"),
        _value("commute_rate_21", "0.38"),
        _value("commute_max", "4500"),
        _value("private_car_km", "0.30"),
        _value("flat_writer_rate", "0.30"),
        _value("flat_writer_max", "3600"),
        _value("flat_side_rate", "0.25"),
        _value("flat_side_max", "900"),
        _value("flat_childcare_month", "400"),
        _value("flat_childcare_free_place", "50"),
        _value("tax_free_trainer", "3000", date_to="2025-12-31"),
        _value("tax_free_trainer", "3300", date_from="2026-01-01"),
    ]
)


def run(kind, year=2026, **values):
    return adj.compute(adj.AdjustmentInput(kind=kind, year=year, **values), PARAMS)


class TestAdjustmentEngine(BaseCase):
    def assertAmounts(self, result, expected):
        self.assertEqual(
            result.amounts(), {code: D(value) for code, value in expected.items()}
        )

    # ------------------------------------------------------------ UT-PAU-1
    def test_pau_1_daily_allowance_limit(self):
        """6 € per day, at most 1,260 € (§ 4 (5) sentence 1 no. 6c EStG); the
        allowance is a contribution (expenses paid privately)."""
        result = run(adj.HOME_OFFICE_DAYS, days=100)
        self.assertAmounts(result, {"EUER.25.163": "600.00", "EUER.29.123": "600.00"})
        result = run(adj.HOME_OFFICE_DAYS, days=230)
        self.assertEqual(result.amount, D("1260.00"))
        self.assertEqual(result.notes, ("capped",))
        # the limit holds for all activities: 300 € claimed for wages
        result = run(adj.HOME_OFFICE_DAYS, days=230, other_amount=D("300"))
        self.assertEqual(result.amount, D("960.00"))
        with self.assertRaises(adj.AdjustmentError):
            run(adj.HOME_OFFICE_DAYS, days=367)

    # ------------------------------------------------------------ UT-PAU-2
    def test_pau_2_daily_allowance_exclusions(self):
        """No daily allowance with accommodation costs of a double household
        (sentence 3); with a home office in the same year check C-55."""
        result = run(adj.HOME_OFFICE_DAYS, days=100, double_household=True)
        self.assertAmounts(result, {})
        self.assertEqual(result.notes, ("double_household",))
        findings = adj.plausibility(
            {"EUER.25.163": D("600.00"), "EUER.25.172": D("1260.00")}
        )
        self.assertEqual([f[0] for f in findings], ["C-55"])
        self.assertFalse(adj.plausibility({"EUER.25.163": D("600.00")}))
        # the non-deductible column alone is no deduction
        self.assertFalse(
            adj.plausibility({"EUER.25.163": D("600.00"), "EUER.25.162": D("900")})
        )

    # ------------------------------------------------------------ UT-PAU-3
    def test_pau_3_home_office_pro_rata(self):
        """Home office as centre for 9 months: flat rate 1,260 × 9/12 = 945;
        the actual costs not deducted go to Kz 162. Actual costs 2,400 × 9/12
        = 1,800 deductible, 600 not. Without centre nothing is deductible."""
        result = run(
            adj.HOME_OFFICE_ROOM,
            center_of_activity=True,
            months=9,
            costs=D("2400"),
        )
        self.assertAmounts(
            result,
            {
                "EUER.25.172": "945.00",
                "EUER.25.162": "2400.00",
                "EUER.29.123": "945.00",
            },
        )
        result = run(
            adj.HOME_OFFICE_ROOM,
            center_of_activity=True,
            months=9,
            costs=D("2400"),
            method="actual",
        )
        self.assertAmounts(
            result,
            {
                "EUER.25.172": "1800.00",
                "EUER.25.162": "600.00",
                "EUER.29.123": "1800.00",
            },
        )
        result = run(adj.HOME_OFFICE_ROOM, months=12, costs=D("2400"))
        self.assertAmounts(result, {"EUER.25.162": "2400.00"})
        with self.assertRaises(adj.AdjustmentError):
            run(adj.HOME_OFFICE_ROOM, center_of_activity=True, months=13)

    # ------------------------------------------------------------ UT-PAU-4
    def test_pau_4_meals(self):
        """14 € for more than 8 hours or arrival/departure, 28 € for 24 hours:
        3 × 14 + 2 × 28 = 98; meals provided reduce, flat rates abroad add."""
        result = run(adj.MEALS, days_8h=3, days_24h=2)
        self.assertAmounts(result, {"EUER.25.171": "98.00", "EUER.29.123": "98.00"})
        result = run(
            adj.MEALS,
            days_8h=3,
            days_24h=2,
            reduction=D("11.20"),
            other_amount=D("39"),
        )
        self.assertEqual(result.amount, D("125.80"))
        with self.assertRaises(adj.AdjustmentError):
            run(adj.MEALS, days_8h=1, reduction=D("15"))

    # ------------------------------------------------------------ UT-PAU-5
    def test_pau_5_commuting_2025(self):
        """2025: 0.30 € for km 1 to 20, 0.38 € from km 21 (full km): 220 days
        × 20 km = 1,320.00; 21 km = 220 × 6.38 = 1,403.60; 21.9 km count as
        21. Actual costs booked go to Kz 142 and make no contribution."""
        result = run(adj.COMMUTING, year=2025, days=220, distance_km=D("20"))
        self.assertAmounts(result, {"EUER.25.176": "1320.00", "EUER.29.123": "1320.00"})
        result = run(adj.COMMUTING, year=2025, days=220, distance_km=D("21.9"))
        self.assertEqual(result.amount, D("1403.60"))
        result = run(
            adj.COMMUTING,
            year=2025,
            days=220,
            distance_km=D("21"),
            costs=D("2100"),
            own_car=True,
        )
        self.assertAmounts(result, {"EUER.25.176": "1403.60", "EUER.25.142": "2100.00"})
        # 2024 has the same rates
        self.assertEqual(
            run(adj.COMMUTING, year=2024, days=220, distance_km=D("21")).amount,
            D("1403.60"),
        )

    # ------------------------------------------------------------ UT-PAU-6
    def test_pau_6_commuting_2026_and_limit(self):
        """From 2026 0.38 € from the first km: 220 × 21 × 0.38 = 1,755.60.
        At most 4,500 € without own vehicle: 230 × 60 × 0.38 = 5,244.00."""
        result = run(adj.COMMUTING, days=220, distance_km=D("21"))
        self.assertEqual(result.amount, D("1755.60"))
        result = run(adj.COMMUTING, days=230, distance_km=D("60"))
        self.assertEqual(result.amount, D("4500.00"))
        self.assertEqual(result.notes, ("capped",))
        result = run(adj.COMMUTING, days=230, distance_km=D("60"), own_car=True)
        self.assertEqual(result.amount, D("5244.00"))

    # ------------------------------------------------------------ UT-PAU-7
    def test_pau_7_use_contribution(self):
        """Business trips with a private vehicle: 0.30 € per full km, 1,234.6
        km = 1,234 × 0.30 = 370.20 in Kz 147 and as contribution (Kz 123);
        or the actual share of the costs."""
        result = run(adj.USE_CONTRIBUTION, km=D("1234.6"))
        self.assertAmounts(result, {"EUER.25.147": "370.20", "EUER.29.123": "370.20"})
        result = run(adj.USE_CONTRIBUTION, method="actual", amount=D("512.34"))
        self.assertAmounts(result, {"EUER.25.147": "512.34", "EUER.29.123": "512.34"})

    # ------------------------------------------------------------ UT-PAU-8
    def test_pau_8_flat_rate_of_expenses(self):
        """Writers 30 % of 15,000 = 4,500, capped at 3,600; side activity
        25 % of 2,000 = 500; child day care 24 child months × 400 × 30/40 +
        3 × 50 = 7,350. Next to actual expenses: C-56."""
        result = run(adj.FLAT_RATE, flat_kind=adj.FLAT_WRITER, revenue=D("15000"))
        self.assertAmounts(result, {"EUER.25.195": "3600.00"})
        self.assertEqual(result.notes, ("capped",))
        result = run(adj.FLAT_RATE, flat_kind=adj.FLAT_SIDE_ACTIVITY, revenue=D("2000"))
        self.assertEqual(result.amount, D("500.00"))
        result = run(
            adj.FLAT_RATE,
            flat_kind=adj.FLAT_CHILDCARE,
            child_months=D("24"),
            hours_per_week=D("30"),
            free_place_months=D("3"),
        )
        self.assertEqual(result.amount, D("7350.00"))
        with self.assertRaises(adj.AdjustmentError):
            run(adj.FLAT_RATE, revenue=D("100"))
        values = {
            "EUER.25.195": D("500.00"),
            "EUER.25.183": D("12.00"),
            "EUER.25.164": D("80.00"),
        }
        findings = adj.plausibility(values)
        self.assertEqual(findings[0][0], "C-56")
        self.assertEqual(findings[0][2]["fields"], ["EUER.25.183"])
        # "of which" fields and the subtotals are no actual expenses
        self.assertFalse(
            adj.plausibility(
                {"EUER.25.195": D("500.00"), "EUER.25.164": D("80.00")},
                expense_codes={"EUER.25.195", "EUER.25.164", "EUER.25.183"},
            )
        )

    # ------------------------------------------------------------ withdrawals
    def test_withdrawals_with_vat(self):
        """Kz 106 and 108 with VAT (Kz 140) are withdrawals at value plus VAT
        (Anleitung lines 107/108 with line 19); VAT on free supplies alone is
        a withdrawal, too. The VAT base (1 % rule: 80 % of 3,600 = 2,880,
        × 19 % = 547.20) is the part of the total turnover (R-27); a VAT
        needs its base."""
        result = run(
            adj.CAR_PRIVATE_USE,
            amount=D("3600"),
            vat=D("547.20"),
            vat_base=D("2880"),
        )
        self.assertAmounts(
            result,
            {
                "EUER.20.106": "3600.00",
                "EUER.20.140": "547.20",
                "EUER.29.122": "4147.20",
            },
        )
        self.assertEqual(result.turnover, D("2880.00"))
        with self.assertRaises(adj.AdjustmentError) as error:
            run(adj.CAR_PRIVATE_USE, amount=D("3600"), vat=D("547.20"))
        self.assertEqual(error.exception.key, "vat_base_missing")
        result = run(adj.WITHDRAWAL_IN_KIND, amount=D("81.43"))
        self.assertAmounts(result, {"EUER.20.108": "81.43", "EUER.29.122": "81.43"})
        # no input tax deduction: not taxable, no part of the total turnover
        self.assertEqual(result.turnover, D("0"))
        result = run(adj.WITHDRAWAL_VAT, amount=D("19"))
        self.assertAmounts(result, {"EUER.20.140": "19.00", "EUER.29.122": "19.00"})

    def test_tax_free(self):
        """G.240 to G.245 without withdrawal or contribution; a field is
        required."""
        result = run(adj.TAX_FREE, amount=D("840"), tax_free_field="EUER.G.240")
        self.assertAmounts(result, {"EUER.G.240": "840.00"})
        with self.assertRaises(adj.AdjustmentError):
            run(adj.TAX_FREE, amount=D("840"))

    def test_errors(self):
        with self.assertRaises(adj.AdjustmentError) as error:
            run(adj.CAR_PRIVATE_USE, amount=D("-1"))
        self.assertEqual(error.exception.key, "negative_input")
        with self.assertRaises(adj.AdjustmentError) as error:
            run(adj.HOME_OFFICE_DAYS, year=2023, days=10)
        self.assertEqual(error.exception.key, "missing_parameter")
        with self.assertRaises(adj.AdjustmentError) as error:
            run("unknown")
        self.assertEqual(error.exception.key, "unknown_kind")

    def test_kind_fields(self):
        """Every kind names the fields it can fill."""
        self.assertEqual(set(adj.KIND_FIELDS), set(adj.KINDS))
        total = adj.total_amounts(
            [
                run(adj.MEALS, days_8h=1),
                run(adj.HOME_OFFICE_DAYS, days=1),
            ]
        )
        self.assertEqual(
            total,
            {
                "EUER.25.171": D("14.00"),
                "EUER.25.163": D("6.00"),
                "EUER.29.123": D("20.00"),
            },
        )


class TestScopeEngine(BaseCase):
    def answers(self, confirmed=True, **answers):
        return [
            scope.Answer(
                question=question.code,
                answer=answers.get(question.code, scope.NO),
                confirmed=confirmed,
            )
            for question in scope.QUESTIONS
        ]

    def codes(self, findings):
        return sorted({finding.code for finding in findings})

    def test_confirmed_supported_answers(self):
        findings = scope.evaluate(self.answers(Q01_LEGAL_FORM=scope.YES))
        self.assertEqual(findings, [])

    def test_unconfirmed_is_c45(self):
        """GT-14: an answer that is not confirmed does not count."""
        findings = scope.evaluate(self.answers(confirmed=False, Q01_LEGAL_FORM="yes"))
        self.assertEqual(self.codes(findings), ["C-45"])
        self.assertEqual(findings[0].questions, scope.QUESTION_CODES)
        findings = scope.evaluate([])
        self.assertEqual(self.codes(findings), ["C-45"])

    def test_each_blocking_answer(self):
        """UT-SCOPE: each case outside the program scope is C-40."""
        for question in scope.QUESTIONS:
            with self.subTest(question=question.code):
                answers = {"Q01_LEGAL_FORM": scope.YES}
                if question.blocking:
                    answers[question.code] = question.blocking
                findings = scope.evaluate(self.answers(**answers))
                expected = ["C-40"] if question.blocking else []
                self.assertEqual(self.codes(findings), expected)

    def test_land_and_tax_free(self):
        answers = self.answers(
            Q01_LEGAL_FORM=scope.YES, Q04_LAND=scope.YES, Q12_TAX_FREE=scope.YES
        )
        self.assertEqual(self.codes(scope.evaluate(answers)), ["C-43", "C-44"])
        self.assertEqual(
            scope.evaluate(answers, tax_free_correction=True, land_disposal=True), []
        )
        # the data contradicts a "no" (Kz 15.120 is mandatory, § 3 income)
        answers = self.answers(Q01_LEGAL_FORM=scope.YES)
        findings = scope.evaluate(answers, tax_free_correction=True, land_disposal=True)
        self.assertEqual(
            sorted((f.code, f.severity, f.key) for f in findings),
            [
                ("C-43", m.WARNING, "disposal_without_land"),
                ("C-44", m.WARNING, "correction_without_tax_free"),
            ],
        )
        self.assertEqual(scope.land_value(scope.YES), "1")
        self.assertEqual(scope.land_value(scope.NO), "2")
        self.assertEqual(scope.land_value(None), "")


class TestAdjustmentReview(BaseCase):
    """Findings of the review of stage E4 on the profit corrections (spec
    5.8, R-27, R-28, R-44, R-52). Synthetic cases (D6)."""

    def series(self, *inputs, year=2026):
        return adj.compute_series(
            [adj.AdjustmentInput(year=year, **values) for values in inputs], PARAMS
        )

    def test_yearly_maximums_are_shared(self):
        """The maximums hold for the year: two daily allowances of 115 days
        (690 + 690) give 1,260 (§ 4 (5) sentence 1 no. 6c EStG); two flat
        rates of a side activity on 4,000 € each 900 in all; two commuting
        allowances of 115 days × 60 km (2,622 each) 4,500 without own
        vehicle, the own vehicle has no maximum; two yearly flat rates of a
        home office 1,260."""
        results = self.series(
            {"kind": adj.HOME_OFFICE_DAYS, "days": 115},
            {"kind": adj.HOME_OFFICE_DAYS, "days": 115},
        )
        self.assertEqual([r.amount for r in results], [D("690.00"), D("570.00")])
        self.assertEqual(results[1].notes, ("capped",))
        self.assertEqual(adj.total_amounts(results)["EUER.25.163"], D("1260.00"))
        # the allowance claimed for other income counts once for the year
        results = self.series(
            {"kind": adj.HOME_OFFICE_DAYS, "days": 115, "other_amount": D("300")},
            {"kind": adj.HOME_OFFICE_DAYS, "days": 115, "other_amount": D("300")},
        )
        self.assertEqual(sum(r.amount for r in results), D("960.00"))
        side = {
            "kind": adj.FLAT_RATE,
            "flat_kind": adj.FLAT_SIDE_ACTIVITY,
            "revenue": D("4000"),
        }
        results = self.series(side, side)
        self.assertEqual([r.amount for r in results], [D("900.00"), D("0.00")])
        # the groups have maximums of their own
        writer = {
            "kind": adj.FLAT_RATE,
            "flat_kind": adj.FLAT_WRITER,
            "revenue": D("1000"),
        }
        results = self.series(side, writer)
        self.assertEqual([r.amount for r in results], [D("900.00"), D("300.00")])
        trip = {"kind": adj.COMMUTING, "days": 115, "distance_km": D("60")}
        results = self.series(trip, trip)
        self.assertEqual([r.amount for r in results], [D("2622.00"), D("1878.00")])
        self.assertEqual(adj.total_amounts(results)["EUER.25.176"], D("4500.00"))
        results = self.series(trip, {**trip, "own_car": True})
        self.assertEqual([r.amount for r in results], [D("2622.00"), D("2622.00")])
        room = {
            "kind": adj.HOME_OFFICE_ROOM,
            "center_of_activity": True,
            "months": 12,
        }
        results = self.series(room, room)
        self.assertEqual([r.amount for r in results], [D("1260.00"), D("0.00")])
        half = {**room, "months": 6}
        results = self.series(half, half)
        self.assertEqual([r.amount for r in results], [D("630.00"), D("630.00")])
        # meals have no yearly maximum
        meals = {"kind": adj.MEALS, "days_24h": 50}
        self.assertEqual(
            [r.amount for r in self.series(meals, meals)],
            [D("1400.00"), D("1400.00")],
        )
        self.assertIsNone(adj.limit_key(adj.AdjustmentInput(year=2026, **meals)))

    def test_small_business_has_no_vat(self):
        """R-28, § 19 (1) UStG: no VAT on the free supplies of a small
        business, and no VAT on free supplies of its own; a taxable free
        supply (input tax deducted before) counts at its base."""
        with self.assertRaises(adj.AdjustmentError) as error:
            run(
                adj.WITHDRAWAL_IN_KIND,
                amount=D("500"),
                vat=D("95"),
                small_business=True,
            )
        self.assertEqual(error.exception.key, "small_business_vat")
        with self.assertRaises(adj.AdjustmentError):
            run(adj.WITHDRAWAL_VAT, amount=D("19"), small_business=True)
        with self.assertRaises(adj.AdjustmentError):
            run(
                adj.INCOME_IN_KIND,
                amount=D("100"),
                vat=D("19"),
                small_business=True,
            )
        result = run(
            adj.WITHDRAWAL_IN_KIND,
            amount=D("600"),
            vat_base=D("400"),
            small_business=True,
        )
        self.assertAmounts(result, {"EUER.20.108": "600.00", "EUER.29.122": "600.00"})
        self.assertEqual(result.turnover, D("400.00"))

    def test_income_in_kind(self):
        """Income in kind or barter (spec 5.8): small business Kz 111;
        standard taxation Kz 112 with VAT or Kz 103; no withdrawal; the
        consideration counts in the total turnover."""
        result = run(adj.INCOME_IN_KIND, amount=D("250"), small_business=True)
        self.assertAmounts(result, {"EUER.20.111": "250.00"})
        self.assertEqual(result.turnover, D("250.00"))
        result = run(adj.INCOME_IN_KIND, amount=D("250"), vat=D("47.50"))
        self.assertAmounts(result, {"EUER.20.112": "250.00", "EUER.20.140": "47.50"})
        result = run(adj.INCOME_IN_KIND, amount=D("250"), revenue_field="EUER.20.103")
        self.assertAmounts(result, {"EUER.20.103": "250.00"})
        with self.assertRaises(adj.AdjustmentError) as error:
            run(
                adj.INCOME_IN_KIND,
                amount=D("250"),
                vat=D("1"),
                revenue_field="EUER.20.103",
            )
        self.assertEqual(error.exception.key, "vat_on_exempt")

    def test_income_in_kind_exempt_not_in_turnover(self):
        """R-27, § 19 (2) UStG: tax-free or not taxable income in kind (Kz
        103) is no part of the total turnover, like booked revenue of Kz 103;
        taxable income in kind (Kz 112) counts at its net value."""
        result = run(adj.INCOME_IN_KIND, amount=D("1000"), revenue_field="EUER.20.103")
        self.assertAmounts(result, {"EUER.20.103": "1000.00"})
        self.assertEqual(result.turnover, D("0"))
        result = run(
            adj.INCOME_IN_KIND,
            amount=D("1000"),
            vat=D("190"),
            revenue_field="EUER.20.112",
        )
        self.assertEqual(result.turnover, D("1000.00"))

    def test_c55_only_so_far_as_the_home_office_is_deducted(self):
        """§ 4 (5) sentence 1 no. 6c sentence 3 EStG ("soweit"): a home
        office for 6 months and the daily allowance for the other days is a
        warning; for all 12 months a blocker."""
        values = {"EUER.25.163": D("360.00"), "EUER.25.172": D("630.00")}
        self.assertEqual(
            [(code, key) for code, key, _p in adj.plausibility(values, room_months=6)],
            [("C-55", "home_office_part_year")],
        )
        self.assertEqual(
            [(code, key) for code, key, _p in adj.plausibility(values)],
            [("C-55", "home_office_twice")],
        )

    def test_tax_free_limit(self):
        """Kz 240 above § 3 no. 26 and 26a EStG together (2026: 3,300 + 960):
        warning C-44."""
        values = {"EUER.G.240": D("4300.00")}
        findings = adj.plausibility(values, tax_free_limit=D("4260"))
        self.assertEqual(
            [(f[0], f[1]) for f in findings], [("C-44", "tax_free_above_limit")]
        )
        self.assertFalse(adj.plausibility(values, tax_free_limit=D("4300")))
        self.assertFalse(adj.plausibility(values))

    def test_questionnaire_cross_checks(self):
        """R-52: sole proprietorship with both spouses as owners (Kz 104 = 3)
        is a warning C-40; question Q13 (advance payments for more than five
        years, § 11 (2) sentence 3 EStG) is outside the program scope."""
        answers = [
            scope.Answer(
                question=question.code,
                answer=scope.YES if question.code == "Q01_LEGAL_FORM" else scope.NO,
                confirmed=True,
            )
            for question in scope.QUESTIONS
        ]
        findings = scope.evaluate(answers, joint_owners=True)
        self.assertEqual(
            [(f.code, f.severity, f.key) for f in findings],
            [("C-40", m.WARNING, "joint_owners")],
        )
        self.assertIn("Q13_PREPAYMENT", scope.QUESTION_CODES)
        self.assertEqual(scope.QUESTIONS_BY_CODE["Q13_PREPAYMENT"].blocking, scope.YES)

    def assertAmounts(self, result, expected):
        self.assertEqual(
            result.amounts(), {code: D(value) for code, value in expected.items()}
        )
