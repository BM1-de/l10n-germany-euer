# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Rule parameters with period of validity (spec 3.2)."""

from datetime import date

from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged

SPEC_CODES = {
    "ku_limit_prev",
    "ku_limit_cur",
    "ku_limit_founding",
    "gwg_limit",
    "gwg_register_limit",
    "pool_min",
    "pool_max",
    "gift_limit",
    "entertain_share",
    "meal_8h",
    "meal_24h",
    "homeoffice_day",
    "homeoffice_max",
    "study_flat",
    "commute_rate_1_20",
    "commute_rate_21",
    "commute_max",
    "private_car_km",
    "debt_interest_base",
    "degressive_factor",
    "degressive_max_rate",
    "sec7g_special_rate",
    "ten_day_from",
    "ten_day_to",
}


@tagged("post_install", "-at_install")
class TestParameters(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Parameter = cls.env["l10n_de_euer.parameter"]

    def value(self, code, on_date):
        return self.Parameter._get_value(code, on_date)

    def test_all_parameters_of_the_spec(self):
        parameters = self.Parameter.search([("code", "!=", False)])
        self.assertTrue(SPEC_CODES <= set(parameters.mapped("code")))
        for parameter in parameters:
            with self.subTest(code=parameter.code):
                self.assertTrue(parameter.source)
                self.assertTrue(parameter.name)

    def test_values_per_year(self):
        expected = {
            ("ku_limit_prev", 2024): 22000,
            ("ku_limit_prev", 2025): 25000,
            ("ku_limit_prev", 2026): 25000,
            ("ku_limit_cur", 2024): 50000,
            ("ku_limit_cur", 2026): 100000,
            ("ku_limit_founding", 2024): 22000,
            ("ku_limit_founding", 2025): 25000,
            ("gift_limit", 2024): 50,
            ("entertain_share", 2025): 0.7,
            ("meal_8h", 2026): 14,
            ("meal_24h", 2026): 28,
            ("homeoffice_day", 2026): 6,
            ("homeoffice_max", 2026): 1260,
            ("study_flat", 2024): 1260,
            ("commute_rate_1_20", 2024): 0.30,
            ("commute_rate_1_20", 2025): 0.30,
            ("commute_rate_1_20", 2026): 0.38,
            ("commute_rate_21", 2025): 0.38,
            ("commute_max", 2026): 4500,
            ("private_car_km", 2026): 0.30,
            ("debt_interest_base", 2026): 2050,
            ("tax_free_trainer", 2025): 3000,
            ("tax_free_trainer", 2026): 3300,
            ("tax_free_volunteer", 2025): 840,
            ("tax_free_volunteer", 2026): 960,
        }
        for (code, year), value in expected.items():
            with self.subTest(code=code, year=year):
                self.assertAlmostEqual(self.value(code, year), value)
        self.assertEqual(self.value("ten_day_from", 2026), "12-22")
        self.assertEqual(self.value("ten_day_to", 2026), "01-10")
        with self.assertRaises(UserError):
            self.value("ku_limit_founding", 2023)

    def test_values_by_acquisition_date(self):
        expected = [
            (date(2021, 5, 1), 2.5, 0.25),
            (date(2023, 6, 1), 0, 0),
            (date(2024, 3, 31), 0, 0),
            (date(2024, 4, 1), 2, 0.20),
            (date(2024, 12, 31), 2, 0.20),
            (date(2025, 6, 30), 0, 0),
            (date(2025, 7, 1), 3, 0.30),
            (date(2027, 12, 31), 3, 0.30),
        ]
        for day, factor, rate in expected:
            with self.subTest(day=day):
                self.assertAlmostEqual(self.value("degressive_factor", day), factor)
                self.assertAlmostEqual(self.value("degressive_max_rate", day), rate)
        with self.assertRaises(UserError):
            self.value("degressive_factor", date(2028, 1, 1))
        with self.assertRaises(UserError):
            self.value("degressive_factor", date(2019, 12, 31))
        self.assertAlmostEqual(self.value("sec7g_special_rate", date(2019, 3, 1)), 0.20)
        self.assertAlmostEqual(
            self.value("sec7g_special_rate", date(2023, 12, 31)), 0.20
        )
        self.assertAlmostEqual(self.value("sec7g_special_rate", date(2024, 1, 1)), 0.40)
        rates = self.value("ev_rates", date(2025, 7, 1))
        self.assertEqual(rates, [0.75, 0.1, 0.05, 0.05, 0.03, 0.02])
        self.assertAlmostEqual(sum(rates), 1.0)
        with self.assertRaises(UserError):
            self.value("ev_rates", date(2025, 6, 30))
        acquisition = self.Parameter.search([("date_basis", "=", "acquisition_date")])
        self.assertTrue(
            {"degressive_factor", "sec7g_special_rate", "gwg_limit"}
            <= set(acquisition.mapped("code"))
        )

    def test_asset_limits_by_acquisition_date(self):
        """GWG and pool limits since 2018, older ones 2010-2017 (§ 52 (12) EStG).

        The forms 2024-2026 still show asset pools of 2020-2023.
        """
        expected = {
            date(2022, 6, 1): (800, 250, 250, 1000, 5),
            date(2018, 1, 1): (800, 250, 250, 1000, 5),
            date(2017, 12, 31): (410, 150, 150, 1000, 5),
            date(2010, 1, 1): (410, 150, 150, 1000, 5),
        }
        codes = ("gwg_limit", "gwg_register_limit", "pool_min", "pool_max")
        codes += ("pool_years",)
        for day, values in expected.items():
            for code, value in zip(codes, values, strict=True):
                with self.subTest(day=day, code=code):
                    self.assertAlmostEqual(self.value(code, day), value)
        with self.assertRaises(UserError):
            self.value("gwg_limit", date(2009, 12, 31))

    def test_sz_base_matches_parameter(self):
        """The pre-printed 2,050 € of Anlage SZ equals the rule parameter."""
        for year in (2024, 2025, 2026):
            field = self.env["l10n_de_euer.field"].search(
                [("year", "=", year), ("code", "=", "SZ.43.SOCKEL")]
            )
            self.assertEqual(
                float(field.formula), self.value("debt_interest_base", year)
            )
            # Anlage SZ is filled in only above this base amount (F-14)
            self.assertEqual(
                field.form_id.fill_condition, f"EUER.25.234 - {field.formula}"
            )

    def test_display(self):
        parameter = self.Parameter.search([("code", "=", "commute_rate_1_20")], limit=1)
        self.assertEqual(parameter.value_display, "0.3")
        parameter = self.Parameter.search([("code", "=", "ev_rates")])
        self.assertEqual(parameter.value_display, "[0.75, 0.1, 0.05, 0.05, 0.03, 0.02]")

    def test_periods_must_not_overlap(self):
        vals = {
            "code": "test_parameter",
            "name": "Test",
            "source": "Test",
            "value": 1,
        }
        self.Parameter.create({**vals, "date_to": date(2024, 12, 31)})
        self.Parameter.create({**vals, "date_from": date(2025, 1, 1)})
        self.assertEqual(self.value("test_parameter", date(1990, 1, 1)), 1)
        with self.assertRaises(ValidationError):
            self.Parameter.create({**vals, "date_from": date(2025, 6, 1)})
        with self.assertRaises(ValidationError):
            self.Parameter.create(
                {
                    **vals,
                    "code": "gwg_limit",
                    "date_basis": "acquisition_date",
                    "date_from": date(2023, 1, 1),
                    "date_to": date(2024, 1, 1),
                }
            )
        with self.assertRaises(ValidationError):
            self.Parameter.create(
                {
                    **vals,
                    "code": "gwg_limit",
                    "date_basis": "fiscal_year",
                    "date_from": date(2020, 1, 1),
                    "date_to": date(2020, 12, 31),
                }
            )
        with self.assertRaises(UserError):
            self.value("unknown_parameter", 2026)
