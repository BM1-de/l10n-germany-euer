# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Profit corrections in the statement (phase F, spec 5.8, R-44, R-54).

GT-12 (no correction without reason), the corrections in the calculation of
the year like the amounts of the asset register, the field codes resolved
per form year, the checks C-54, C-55 and C-56, and the use withdrawal of an
asset that never counts twice (proposal of the register taken over as
correction, C-69). SKR03 and SKR04; synthetic cases (D6).
"""

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from ..models.engine import adjustment as adj
from .scenario_common import (
    SKR03_ACCOUNTS,
    SKR03_TAXES,
    SKR04_ACCOUNTS,
    SKR04_TAXES,
)
from .statement_common import D, StatementCommon

DAYS = "EUER.25.163"
ROOM = "EUER.25.172"
FLAT = "EUER.25.195"
CAR = "EUER.20.106"
IN_KIND = "EUER.20.108"
WITHDRAWALS = "EUER.29.122"
CONTRIBUTIONS = "EUER.29.123"
PROFIT = "EUER.G.219"


class AdjustmentScenarios(StatementCommon):
    def sale_2026(self, amount=5000):
        sale = self.sale("2026-03-10", amount)
        self.bank("2026-03-20", amount, matches=[(sale, amount)])

    def register_asset(self, **values):
        asset = self.env["l10n_de_euer.asset"].create(
            {
                "name": "Kamera",
                "company_id": self.company.id,
                "delivery_date": "2026-01-10",
                "cost_net": 1200.0,
                "method": "linear",
                "useful_life_months": 36,
                "aveuer_group": "other_movable",
                "business_share": 0.8,
                **values,
            }
        )
        asset.action_activate()
        asset.action_compute_years()
        return asset

    # ------------------------------------------------------------ GT-12
    def test_gt12_reason_required(self):
        """GT-12: a profit correction without reason is refused."""
        statement = self.statement()
        with self.assertRaises(ValidationError):
            self.correction(statement, "home_office_days", days=10, reason=False)
        with self.assertRaises(ValidationError):
            self.correction(statement, "home_office_days", days=10, reason="  ")
        correction = self.correction(statement, "home_office_days", days=10)
        with self.assertRaises(ValidationError):
            correction.reason = ""

    # ------------------------------------------------------------ phase F
    def test_corrections_in_the_calculation(self):
        """The corrections count in the run (source "adjustment") with their
        withdrawals and contributions: home office days 100 × 6 = 600 (Kz
        163, 123), private use of a vehicle 1,200 (Kz 106, 122)."""
        self.sale_2026()
        statement = self.statement()
        self.correction(statement, "home_office_days", days=100)
        self.correction(statement, "car_private_use", amount=1200.0)
        statement.action_compute()
        self.assertEqual(self.value(statement, DAYS), D("600.00"))
        self.assertEqual(self.value(statement, CONTRIBUTIONS), D("600.00"))
        self.assertEqual(self.value(statement, CAR), D("1200.00"))
        self.assertEqual(self.value(statement, WITHDRAWALS), D("1200.00"))
        self.assertEqual(self.value(statement, PROFIT), D("5600.00"))
        line = statement.line_ids.filtered(lambda line: line.code == DAYS)
        self.assertEqual(line.source, "manual")
        slices = self.env["l10n_de_euer.cash.slice"].search(
            line.action_open_slices()["domain"]
        )
        self.assertEqual(set(slices.mapped("rule")), {"adjustment"})
        # the compute wizard without statement context uses the statement, too
        run = self.Run._compute_year(self.company, 2026)
        self.assertEqual(run._get_field_amounts()[DAYS], D("600"))

    def test_change_needs_computation(self):
        """A changed correction sets the statement back to draft."""
        statement = self.statement()
        correction = self.correction(statement, "meals", days_8h=2)
        statement.action_compute()
        self.assertEqual(statement.state, "computed")
        correction.days_8h = 3
        self.assertEqual(statement.state, "draft")
        self.assertEqual(correction.result_amount, 42.0)
        statement.action_compute()
        self.assertEqual(self.value(statement, "EUER.25.171"), D("42.00"))
        correction.unlink()
        self.assertEqual(statement.state, "draft")

    def test_field_codes_per_form_year(self):
        """The codes are resolved per form year: Kz 163 is line 66 in 2025
        and line 67 in 2026; a year without form data is refused."""
        for year, line in ((2025, 66), (2026, 67)):
            statement = self.statement(year)
            correction = self.correction(statement, "home_office_days", days=10)
            self.assertEqual(correction.field_codes, f"{DAYS}, {CONTRIBUTIONS}")
            self.assertIn(f"Line {line} (Kz 163): 60.00", correction.field_text)
        statement = self.statement(2023)
        with self.assertRaises(ValidationError):
            self.correction(statement, "home_office_days", days=10)

    def test_input_errors(self):
        statement = self.statement()
        with self.assertRaises(ValidationError):
            self.correction(statement, "flat_rate", revenue=1000.0)
        with self.assertRaises(ValidationError):
            self.correction(statement, "home_office_days", days=400)
        with self.assertRaises(ValidationError):
            self.correction(statement, "car_private_use", amount=-5.0)

    # ------------------------------------------------------------ checks
    def test_c54_private_use_of_car_twice(self):
        """C-54: private use of the car booked and entered as correction."""
        self.entry("2026-12-31", [("private_w", 1200), ("private_car", -1200)])
        statement = self.statement()
        self.correction(statement, "car_private_use", amount=1200.0)
        statement.action_compute()
        self.assertEqual(self.results(statement, "C-54").severity, "blocker")

    def test_c55_daily_allowance_and_home_office(self):
        """C-55: daily allowance and home office in the same year."""
        statement = self.statement()
        self.correction(statement, "home_office_days", days=50)
        self.correction(
            statement, "home_office_room", center_of_activity=True, months=12
        )
        statement.action_compute()
        self.assertEqual(self.value(statement, ROOM), D("1260.00"))
        statement.action_check()
        self.assertEqual(self.results(statement, "C-55").severity, "blocker")

    def test_c56_flat_rate_and_actual_expenses(self):
        """C-56: the flat rate of business expenses next to actual expenses."""
        self.sale_2026(2000)
        statement = self.statement()
        self.correction(
            statement, "flat_rate", flat_kind="side_activity", revenue=2000.0
        )
        statement.action_compute()
        self.assertEqual(self.value(statement, FLAT), D("500.00"))
        statement.action_check()
        self.assertFalse(self.results(statement, "C-56"))
        bill = self.purchase("2026-04-10", 80)
        self.bank("2026-04-15", -80, matches=[(bill, 80)])
        statement.action_compute()
        statement.action_check()
        self.assertEqual(self.results(statement, "C-56").severity, "blocker")

    # ------------------------------------------------------------ R-54
    def test_use_withdrawal_counts_once(self):
        """R-54: the proposal of the register (AfA 400 × 20 % = 80) taken over
        as correction counts once; the register leaves out the private use
        of the asset, C-69 is resolved."""
        asset = self.register_asset()
        line = asset.year_ids.filtered(lambda line: line.year == 2026)
        self.assertEqual(line.private_use_amount, 80.0)
        statement = self.statement()
        statement.action_compute()
        statement.action_check()
        self.assertTrue(self.results(statement, "C-69"))
        self.assertIn(line, statement.private_use_ids)
        statement.action_take_over_private_use()
        correction = statement.adjustment_ids
        self.assertEqual(
            (correction.kind, correction.asset_id, correction.result_amount),
            ("withdrawal_in_kind", asset, 80.0),
        )
        self.assertTrue(correction.reason)
        # taking over twice does not add a second correction
        statement.action_take_over_private_use()
        self.assertEqual(len(statement.adjustment_ids), 1)
        statement.action_compute()
        self.assertEqual(self.value(statement, IN_KIND), D("80.00"))
        self.assertEqual(self.value(statement, WITHDRAWALS), D("80.00"))
        statement.action_check()
        self.assertFalse(self.results(statement, "C-69"))
        # confirmed in the register as well: still once
        line.action_confirm_private_use()
        statement.action_compute()
        self.assertEqual(self.value(statement, IN_KIND), D("80.00"))
        correction.amount = 90.0
        statement.action_compute()
        self.assertEqual(self.value(statement, IN_KIND), D("90.00"))
        with self.assertRaises(ValidationError):
            self.correction(
                statement, "withdrawal_in_kind", amount=10.0, asset_id=asset.id
            )

    def test_register_without_taken_over_assets(self):
        """Regression of the asset register: ``exclude_private_use`` leaves
        out the confirmed use withdrawal of the given assets only."""
        first = self.register_asset()
        second = self.register_asset(name="Objektiv", cost_net=600.0)
        for asset in first | second:
            asset.year_ids.filtered(
                lambda line: line.year == 2026
            ).action_confirm_private_use()
        Asset = self.env["l10n_de_euer.asset"]
        values = Asset._get_field_contributions(self.company, 2026)
        self.assertEqual(values[IN_KIND], D("120.00"))
        values = Asset._get_field_contributions(
            self.company, 2026, exclude_private_use=first
        )
        self.assertEqual(values[IN_KIND], D("40.00"))
        values = Asset._get_field_contributions(
            self.company, 2026, exclude_private_use=first.ids
        )
        self.assertEqual(values[WITHDRAWALS], D("40.00"))
        values = Asset._get_field_contributions(
            self.company, 2026, include_private_use=False
        )
        self.assertNotIn(IN_KIND, values)

    def test_c69_vehicle_decided_by_correction(self):
        """C-69 for a vehicle used privately is resolved by a correction of
        the private use of vehicles (Kz 106)."""
        self.register_asset(
            name="Kfz",
            aveuer_group="vehicle",
            cost_net=24000.0,
            useful_life_months=72,
            business_share=0.6,
        )
        statement = self.statement()
        statement.action_compute()
        statement.action_check()
        vehicle_results = self.results(statement, "C-69").filtered(
            lambda check: check.res_model == "l10n_de_euer.asset"
        )
        self.assertTrue(vehicle_results)
        self.correction(statement, "car_private_use", amount=3000.0)
        statement.action_compute()
        statement.action_check()
        self.assertFalse(
            self.results(statement, "C-69").filtered(
                lambda check: check.res_model == "l10n_de_euer.asset"
            )
        )

    def test_error_messages_of_the_calculation(self):
        """Every input error of the calculation has its own message for the
        user; a case without one still names its key."""
        Adjustment = self.Adjustment.with_context(lang="en_US")
        messages = {
            "unknown_field": "Choose the field of the tax-free income or expense.",
            "invalid_months": "Enter between 0 and 12 months.",
            "reduction_too_high": "The reduction is higher than the meal allowances.",
            "vat_on_exempt": "Tax-free income in kind (Kz 103) has no VAT.",
            "unknown_kind": "The profit correction cannot be computed: unknown_kind",
        }
        for key, message in messages.items():
            with self.subTest(key=key):
                self.assertEqual(
                    Adjustment._error_message(adj.AdjustmentError(key)), message
                )
        self.assertIn(
            "§ 10 (4) UStG",
            Adjustment._error_message(adj.AdjustmentError("vat_base_missing")),
        )

    def test_asset_only_for_a_withdrawal_of_use(self):
        """Only the use withdrawal (Kz 108) refers to an asset of the register;
        a meal allowance with an asset is refused."""
        asset = self.register_asset()
        statement = self.statement()
        with self.assertRaises(ValidationError):
            self.correction(statement, "meals", days_8h=1, asset_id=asset.id)

    def test_finalised_statement_is_protected(self):
        """Corrections of a statement that is no longer open cannot change."""
        statement = self.statement()
        correction = self.correction(statement, "meals", days_8h=1)
        statement._write_internal({"state": "final"})
        with self.assertRaises(UserError):
            correction.days_8h = 2
        with self.assertRaises(UserError):
            self.correction(statement, "meals", days_8h=1)
        with self.assertRaises(UserError):
            statement.unlink()


@tagged("post_install_l10n", "post_install", "-at_install")
class TestAdjustmentSkr03(AdjustmentScenarios, AccountTestInvoicingCommon):
    allow_inherited_tests_method = True
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_statement()


@tagged("post_install_l10n", "post_install", "-at_install")
class TestAdjustmentSkr04(AdjustmentScenarios, AccountTestInvoicingCommon):
    allow_inherited_tests_method = True
    CHART = "de_skr04"
    ACCOUNTS = SKR04_ACCOUNTS
    TAXES = SKR04_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr04")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_statement()
