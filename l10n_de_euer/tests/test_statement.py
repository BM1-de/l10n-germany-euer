# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Statement of a business and year (spec 7.3, 7.7 steps 1 and 2).

GT-01 (only for companies with EÜR), GT-03 (mandatory header values), R-02
(one valid statement per business and year), the field values with their
sources, overrides with reason (G-15), the drilldown to the slices (G-03,
GT-08 forward) and the checks of the statement. SKR03 and SKR04; synthetic
cases (D6).
"""

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from .scenario_common import (
    SKR03_ACCOUNTS,
    SKR03_TAXES,
    SKR04_ACCOUNTS,
    SKR04_TAXES,
)
from .statement_common import D, StatementCommon

KU = "EUER.20.111"
INCOME = "EUER.20.159"
EXPENSES = "EUER.25.199"
PROFIT = "EUER.G.219"


class StatementScenarios(StatementCommon):
    def year_2026(self):
        """KU business: 1,000.00 received, 200.00 paid in 2026."""
        sale = self.sale("2026-03-10", 1000)
        self.bank("2026-03-20", 1000, matches=[(sale, 1000)])
        bill = self.purchase("2026-04-10", 200)
        self.bank("2026-04-15", -200, matches=[(bill, 200)])
        return sale, bill

    # ------------------------------------------------------------ GT-01
    def test_gt01_only_for_enabled_company(self):
        """GT-01: a statement only for a company with cash basis accounting."""
        other = self.env["res.company"].create({"name": "Without EÜR"})
        self.env.user.company_ids |= other
        with self.assertRaises(ValidationError):
            self.statement(company=other)
        statement = self.statement()
        self.company.l10n_de_euer_enabled = False
        with self.assertRaises(UserError):
            statement.action_compute()

    # ------------------------------------------------------------ compute
    def test_compute_values_and_control_values(self):
        """Compute: the run with the field values of the forms; zero fields
        stay empty (R-46); control values Kz 159, 199, 219."""
        self.year_2026()
        statement = self.statement()
        self.assertEqual(statement.state, "draft")
        statement.action_compute()
        self.assertEqual(statement.state, "computed")
        self.assertTrue(statement.run_id)
        self.assertFalse(statement.provisional_form)
        self.assertEqual(
            set(statement.form_ids.mapped("form_type")), {"euer", "aveuer", "sz"}
        )
        self.assertEqual(self.value(statement, KU), D("1000.00"))
        self.assertEqual(self.value(statement, INCOME), D("1000.00"))
        self.assertEqual(self.value(statement, EXPENSES), D("200.00"))
        self.assertEqual(self.value(statement, PROFIT), D("800.00"))
        self.assertEqual(statement.profit, 800.0)
        self.assertEqual(statement.total_income, 1000.0)
        self.assertEqual(statement.total_expense, 200.0)
        self.assertIsNone(self.value(statement, "EUER.20.112"))
        line = statement.line_ids.filtered(lambda line: line.code == KU)
        self.assertEqual((line.line, line.kz, line.source), (12, "111", "computed"))
        # values match the run
        self.assertEqual(
            {code: D(value) for code, value in statement.run_id.form_values.items()},
            {
                line.code: D(str(line.amount)).quantize(D("0.01"))
                for line in statement.line_ids
                if line.has_value and line.field_type not in ("text", "key", "date")
            },
        )

    def test_previous_year_column(self):
        """The previous year comes from the statement of the previous year."""
        sale = self.sale("2025-05-10", 300)
        self.bank("2025-05-20", 300, matches=[(sale, 300)])
        previous = self.statement(2025)
        previous.action_compute()
        self.year_2026()
        statement = self.statement()
        statement.action_compute()
        line = statement.line_ids.filtered(lambda line: line.code == KU)
        self.assertEqual(line.amount_prev_year, 300.0)

    def test_provisional_without_form_year(self):
        """F-13: 2027 has no form data yet; the statement uses the forms of
        2026, is provisional and C-72 blocks its finalisation."""
        statement = self.statement(2027)
        statement.action_compute()
        self.assertTrue(statement.provisional_form)
        self.assertEqual(set(statement.form_ids.mapped("year")), {2026})
        self.answer(statement)
        statement.action_check()
        self.assertIn("C-72", self.result_codes(statement, "blocker"))

    # ------------------------------------------------------------ GT-03
    def test_gt03_mandatory_header_values(self):
        """GT-03 (F-08): header values of the company; Kz 15.120 from the
        questionnaire; a missing mandatory value blocks."""
        self.company.state_id = self.env.ref("base.state_de_by")
        self.company.l10n_de_stnr = "181/815/08155"
        statement = self.statement()
        statement.action_compute()
        self.assertEqual(self.value(statement, "EUER.15.100"), "Fotografie")
        self.assertEqual(
            self.value(statement, "EUER.15.RECHTSFORM"), "Einzelunternehmen"
        )
        self.assertEqual(self.value(statement, "EUER.15.103"), "2")
        self.assertEqual(self.value(statement, "EUER.15.104"), "1")
        self.assertEqual(self.value(statement, "EUER.00.STNR"), "181/815/08155")
        self.assertEqual(self.value(statement, "EUER.15.120"), "")
        self.answer(statement, Q04_LAND="no")
        self.assertEqual(self.value(statement, "EUER.15.120"), "2")
        statement.action_check()
        self.assertFalse(self.results(statement, "F-08"))
        self.company.l10n_de_euer_income_type = False
        statement.action_check()
        result = self.results(statement, "F-08")
        self.assertEqual(result.severity, "blocker")
        self.assertIn("Kz 103", result.message)

    # ------------------------------------------------------------ R-02
    def test_r02_one_statement_per_business_and_year(self):
        """R-02: one valid statement per business and year; a branch counts
        for its business, a branch that is a business of its own has its
        own statement."""
        statement = self.statement()
        with self.assertRaises(ValidationError):
            self.statement()
        branch = self.env["res.company"].create(
            {"name": "Branch", "parent_id": self.company.id}
        )
        own = self.env["res.company"].create(
            {
                "name": "Own Business",
                "parent_id": self.company.id,
                "l10n_de_euer_separate_business": True,
            }
        )
        self.env.user.company_ids |= branch | own
        with self.assertRaises(ValidationError):
            self.statement(company=branch)
        own_statement = self.statement(company=own)
        self.assertEqual(own_statement.company_id, own)
        self.assertNotEqual(own_statement, statement)
        self.assertTrue(self.statement(2025))

    # ------------------------------------------------------------ override
    def test_override_with_reason(self):
        """G-15: a value is overridden only with a reason; the formulas use
        the override; back to the computed value."""
        self.year_2026()
        statement = self.statement()
        statement.action_compute()
        line = statement.line_ids.filtered(lambda line: line.code == KU)
        with self.assertRaises(ValidationError):
            line.write({"source": "override", "override_amount": 1100.0})
        line.write(
            {
                "override_amount": 1100.0,
                "override_reason": "Cash receipt missing in the books",
            }
        )
        line.action_apply_override()
        self.assertEqual(line.source, "override")
        self.assertEqual(self.value(statement, KU), D("1100.00"))
        self.assertEqual(line.amount_computed, 1000.0)
        self.assertEqual(self.value(statement, PROFIT), D("900.00"))
        self.assertEqual(statement.profit, 900.0)
        # the override stays with a new computation
        statement.action_compute()
        self.assertEqual(self.value(statement, PROFIT), D("900.00"))
        line.action_reset()
        self.assertEqual(self.value(statement, PROFIT), D("800.00"))
        with self.assertRaises(UserError):
            line.write({"amount": 5.0})

    def test_override_of_a_total(self):
        """A total (formula) can be overridden as well."""
        self.year_2026()
        statement = self.statement()
        statement.action_compute()
        line = statement.line_ids.filtered(lambda line: line.code == EXPENSES)
        line.write(
            {
                "source": "override",
                "override_amount": 250.0,
                "override_reason": "Correction of the tax advisor",
            }
        )
        self.assertEqual(self.value(statement, PROFIT), D("750.00"))

    # ------------------------------------------------------------ drilldown
    def test_drilldown_to_the_slices(self):
        """G-03: a value shows its slices, a total those of its inputs."""
        sale, bill = self.year_2026()
        statement = self.statement()
        statement.action_compute()
        line = statement.line_ids.filtered(lambda line: line.code == KU)
        action = line.action_open_slices()
        slices = self.env["l10n_de_euer.cash.slice"].search(action["domain"])
        self.assertEqual(slices.origin_move_id, sale)
        self.assertEqual(line.slice_count, len(slices))
        total = statement.line_ids.filtered(lambda line: line.code == PROFIT)
        slices = self.env["l10n_de_euer.cash.slice"].search(
            total.action_open_slices()["domain"]
        )
        self.assertEqual(slices.origin_move_id, sale | bill)
        action = statement.action_open_slices()
        self.assertTrue(self.env["l10n_de_euer.cash.slice"].search(action["domain"]))

    # ------------------------------------------------------------ check
    def test_check_runs_all_checks(self):
        """Check: account assignment, calculation, asset register and the
        statement; results visible on the statement."""
        self.year_2026()
        statement = self.statement()
        with self.assertRaises(UserError):
            statement.action_check()
        statement.action_compute()
        statement.action_check()
        self.assertTrue(statement.checked_on)
        origins = set(statement.check_ids.mapped("origin"))
        self.assertIn("statement", origins)
        self.assertIn("calculation", origins)
        # questionnaire not confirmed, bank not reconciled, year not locked
        self.assertIn("C-45", self.result_codes(statement, "blocker"))
        self.assertIn("C-38", self.result_codes(statement, "blocker"))
        self.assertIn("C-70", self.result_codes(statement, "blocker"))
        self.answer(statement)
        self.reconcile_banks(statement)
        self.company.fiscalyear_lock_date = "2026-12-31"
        statement.action_check()
        codes = self.result_codes(statement, "blocker")
        for code in ("C-45", "C-38", "C-70"):
            self.assertNotIn(code, codes)
        self.assertTrue(statement.blocker_count >= 1)  # C-72: forms not verified

    def test_check_needs_current_computation(self):
        """A booking after the computation: the statement has to be computed
        again before the checks."""
        self.year_2026()
        statement = self.statement()
        statement.action_compute()
        sale = self.sale("2026-06-10", 50)
        self.bank("2026-06-20", 50, matches=[(sale, 50)])
        with self.assertRaises(UserError):
            statement.action_check()
        statement.action_compute()
        statement.action_check()
        self.assertEqual(self.value(statement, KU), D("1050.00"))

    def test_business_and_year_stay(self):
        """Business and year of a statement never change (its answers and
        bank reconciliation belong to them); the reference of the
        submission can be entered."""
        statement = self.statement()
        with self.assertRaises(UserError):
            statement.year = 2025
        statement.action_compute()
        with self.assertRaises(UserError):
            statement.company_id = self.env["res.company"].create({"name": "Other"})
        statement.submission_reference = "TT-1"


@tagged("post_install_l10n", "post_install", "-at_install")
class TestStatementSkr03(StatementScenarios, AccountTestInvoicingCommon):
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
class TestStatementSkr04(StatementScenarios, AccountTestInvoicingCommon):
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
