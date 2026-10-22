# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Regression tests of the review of the calculation with real bookings
(stage E2, round 1). All amounts are synthetic (decision D6).

* GT-02: a branch that is a business of its own keeps its own check results,
  review items and runs; a reconciliation across two businesses is C-47.
* Slices and classifications survive when Odoo deletes and recreates the
  journal items of a statement line; runs are only replaced by a new run.
* Record rules bind runs, slices, review items and check results of the
  calculation to the business; decisions are made by the actions only.
* Phase G runs with every calculation (C-71); a statement line moved by the
  lock date is reported (C-12); empty helper fields create no record.
"""

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from .scenario_common import SKR03_ACCOUNTS, SKR03_TAXES, ScenarioCommon, euro

KU = "EUER.20.111"


@tagged("post_install_l10n", "post_install", "-at_install")
class TestReviewFindings(ScenarioCommon, AccountTestInvoicingCommon):
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_euer()

    # ------------------------------------------------------------ helpers
    def separate_branch(self):
        branch = self.env["res.company"].create(
            {
                "name": "Own Business",
                "parent_id": self.company.id,
                "l10n_de_euer_separate_business": True,
            }
        )
        self.env.user.company_ids |= branch
        return branch

    def branch_sale(self, branch, date, amount):
        move = (
            self.env["account.move"]
            .with_company(branch)
            .create(
                {
                    "move_type": "entry",
                    "date": date,
                    "company_id": branch.id,
                    "journal_id": self.misc_journal.id,
                    "line_ids": [
                        Command.create(
                            {
                                "name": "Branch sale",
                                "account_id": self.receivable.id,
                                "partner_id": self.customer.id,
                                "balance": amount,
                                "date_maturity": date,
                            }
                        ),
                        Command.create(
                            {
                                "name": "Branch sale",
                                "account_id": self.acc("ku_revenue").id,
                                "balance": -amount,
                            }
                        ),
                    ],
                }
            )
        )
        move.action_post()
        return move

    def run_of(self, company, year):
        companies = company._l10n_de_euer_business_companies() | self.company
        return self.Run.with_context(allowed_company_ids=companies.ids)._compute_year(
            company, year
        )

    def business_checks(self, company, year, code=None):
        domain = [
            ("company_id", "=", company.id),
            ("year", "=", year),
            ("origin", "=", "calculation"),
        ]
        if code:
            domain.append(("code", "=", code))
        return self.env["l10n_de_euer.check"].sudo().search(domain)

    def rebuild_line(self, st_line, line):
        """Delete and create a journal item of a statement entry again, as
        the reconciliation of Odoo and OCA does."""
        move = st_line.move_id
        values = {
            "name": line.name,
            "account_id": line.account_id.id,
            "partner_id": line.partner_id.id,
            "balance": line.balance,
        }
        move.button_draft()
        move.with_context(skip_account_move_synchronization=True).write(
            {"line_ids": [Command.unlink(line.id), Command.create(values)]}
        )
        move.with_context(skip_account_move_synchronization=True).action_post()

    # ------------------------------------------------------------ GT-02
    def test_separate_business_keeps_its_results(self):
        """s04: the run of one business never deletes the results of the
        other one; each result belongs to its business."""
        branch = self.separate_branch()
        invoice = self.sale("2026-03-15", 500)
        self.bank("2026-04-02", 500, matches=[(invoice, 500)])
        self.bank("2027-01-02", -40, extra=[("office", 40)])
        own = self.branch_sale(branch, "2026-05-01", 300)
        self.assertTrue(own)
        self.run_of(self.company, 2026)
        main_checks = self.business_checks(self.company, 2026)
        self.assertIn("C-16", main_checks.mapped("code"))
        items = (
            self.env["l10n_de_euer.ten_day.item"]
            .sudo()
            .search([("company_id", "=", self.company.id)])
        )
        self.assertEqual(items.mapped("kind"), ["transfer_order"])
        branch_run = self.run_of(branch, 2026)
        self.assertEqual(branch_run.company_id, branch)
        self.assertEqual(self.business_checks(self.company, 2026), main_checks)
        self.assertTrue(items.exists())
        self.assertFalse(
            self.env["l10n_de_euer.ten_day.item"]
            .sudo()
            .search([("company_id", "=", branch.id)])
        )

    def test_reconciliation_with_another_business(self):
        """s04: invoice of the own business paid on the bank of the main
        company: C-47 in both calculations instead of a silent gap."""
        branch = self.separate_branch()
        own = self.branch_sale(branch, "2026-05-01", 595)
        self.bank("2026-05-10", 595, matches=[(own, 595)])
        self.run_of(self.company, 2026)
        self.assertEqual(
            self.business_checks(self.company, 2026, "C-47").severity, "blocker"
        )
        self.assertFalse(self.business_checks(self.company, 2026, "C-20"))
        run = self.run_of(branch, 2026)
        self.assertEqual(run._get_field_amounts(), {})
        self.assertEqual(self.business_checks(branch, 2026, "C-47").severity, "blocker")

    # ------------------------------------------------------------ deletion
    def test_slices_and_classifications_survive_new_lines(self):
        """s04: reconciling a statement line again deletes its journal items;
        the stored run keeps its slices, the classification its snapshot."""
        unclear = self.bank("2026-03-02", 30)
        suspense = unclear.move_id.line_ids.filtered(
            lambda line: line.account_id == self.acc("suspense")
        )
        invoice = self.sale("2026-03-15", 500)
        st_line = self.bank(
            "2026-04-02", 800, matches=[(invoice, 500)], rest="receivable"
        )
        rest = st_line.move_id.line_ids.filtered(
            lambda line: line.account_id == self.receivable and not line.reconciled
        )
        classification = self.env["l10n_de_euer.residual.classification"].create(
            {"source_line_id": rest.id, "kind": "deposit", "reason": "Deposit"}
        )
        run = self.Run._compute_year(self.company, 2026)
        count = run.slice_count
        referenced = run.slice_ids.filtered(lambda one: one.origin_line_id == suspense)
        self.assertTrue(referenced)
        self.rebuild_line(unclear, suspense)
        self.rebuild_line(st_line, rest)
        self.env.invalidate_all()
        self.assertEqual(run.slice_count, count)
        self.assertTrue(referenced.exists())
        self.assertFalse(referenced.origin_line_id)
        self.assertEqual(set(referenced.mapped("origin_line_ref")), {suspense.id})
        self.assertTrue(classification.exists())
        self.assertTrue(classification.obsolete)
        self.assertEqual(classification.source_line_ref, rest.id)
        with self.assertRaises(UserError):
            run.unlink()
        with self.assertRaises(UserError):
            run.sudo().unlink()

    # ------------------------------------------------------------ rights
    def test_rules_bind_results_to_the_business(self):
        branch = self.separate_branch()
        invoice = self.sale("2026-03-15", 500)
        self.bank("2026-04-02", 500, matches=[(invoice, 500)])
        self.branch_sale(branch, "2026-05-01", 300)
        main_run = self.run_of(self.company, 2026)
        user = new_test_user(
            self.env,
            login="euer_own_business",
            groups="account.group_account_user",
            company_id=branch.id,
            company_ids=[Command.set(branch.ids)],
        )
        own_run = self.Run.with_user(user)._compute_year(branch, 2026)
        Run = self.env["l10n_de_euer.run"].with_user(user)
        self.assertEqual(Run.search([]), own_run.with_user(user))
        self.assertFalse(
            self.env["l10n_de_euer.cash.slice"]
            .with_user(user)
            .search([("run_id", "=", main_run.id)])
        )
        checks = self.env["l10n_de_euer.check"].with_user(user).search([])
        self.assertNotIn(
            self.company,
            checks.filtered(lambda check: check.origin == "calculation").company_id,
        )
        with self.assertRaises(AccessError):
            self.Run.with_user(user)._compute_year(self.company, 2026)

    def test_branch_user_cannot_compute_the_business(self):
        branch = self.env["res.company"].create(
            {"name": "Branch", "parent_id": self.company.id}
        )
        user = new_test_user(
            self.env,
            login="euer_branch",
            groups="account.group_account_user",
            company_id=branch.id,
            company_ids=[Command.set(branch.ids)],
        )
        with self.assertRaises(AccessError):
            self.Run.with_user(user)._compute_year(branch, 2026)

    def test_decisions_only_by_actions(self):
        """s05/s09: no decision, decider or date by a plain write."""
        user = new_test_user(
            self.env,
            login="euer_decider",
            groups="account.group_account_user",
            company_id=self.company.id,
            company_ids=[Command.set(self.company.ids)],
        )
        self.bank("2027-01-02", -40, extra=[("office", 40)])
        self.compute(2027)
        item = self.env["l10n_de_euer.ten_day.item"].search([])
        self.assertEqual(item.kind, "transfer_order")
        item = item.with_user(user)
        with self.assertRaises(UserError):
            item.write({"decision": "reject"})
        with self.assertRaises(UserError):
            item.write({"decided_by_id": self.env.uid})
        item.write({"reason": "Order given on 2 January"})
        item.action_apply()
        self.assertEqual(item.decided_by_id, user)
        st_line = self.bank("2026-06-01", 70, rest="receivable")
        rest = st_line.move_id.line_ids.filtered(
            lambda line: line.account_id == self.receivable
        )
        classification = (
            self.env["l10n_de_euer.residual.classification"]
            .with_user(user)
            .create(
                {
                    "source_line_id": rest.id,
                    "kind": "loan",
                    "reason": "Loan of the customer",
                    "decided_by_id": self.env.ref("base.user_admin").id,
                    "decided_on": "2000-01-01 00:00:00",
                }
            )
        )
        self.assertEqual(classification.decided_by_id, user)
        self.assertNotEqual(classification.decided_on.year, 2000)
        classification.write({"decided_by_id": self.env.ref("base.user_admin").id})
        self.assertEqual(classification.decided_by_id, user)

    # ------------------------------------------------------------ phase G
    def test_form_calculation_with_the_run(self):
        """s03: a negative expense field is C-71; the form values are kept."""
        office = self.field_of("EXP_WORK_EQUIPMENT")
        self.bank("2026-05-02", 50, extra=[("office", -50)])
        run = self.compute(2026)
        self.assertEqual(run.totals, {office: -5000})
        self.assertEqual(set(self.checks(2026, "C-71").mapped("severity")), {"blocker"})
        self.assertEqual(euro(run.form_values[office]), euro(-50))
        self.assertTrue(run.form_values_text)

    # ------------------------------------------------------------ B-12
    def test_statement_line_moved_by_the_lock_date(self):
        """s06: statement line of 30.12.2038 posted on 01.01.2039."""
        self.company.fiscalyear_lock_date = "2038-12-31"
        st_line = self.bank("2038-12-30", 620, extra=[("ku_revenue", -620)])
        self.assertEqual(str(st_line.move_id.date), "2039-01-01")
        self.compute(2039)
        check = self.checks(2039, "C-12")
        self.assertEqual(check.severity, "warning")
        self.assertIn(st_line.move_id.id, check.res_ids)

    # ------------------------------------------------------------ helpers
    def test_empty_helper_fields_create_no_information(self):
        Info = self.env["l10n_de_euer.move.info"]
        before = Info.search_count([])
        move = self.env["account.move"].create(
            {
                "move_type": "entry",
                "date": "2026-02-01",
                "journal_id": self.misc_journal.id,
                "l10n_de_euer_economic_year": 0,
                "l10n_de_euer_cash_date": False,
                "l10n_de_euer_gift_bulk": False,
                "line_ids": [
                    Command.create(
                        {"account_id": self.acc("office").id, "balance": 10}
                    ),
                    Command.create({"account_id": self.acc("cash").id, "balance": -10}),
                ],
            }
        )
        self.assertTrue(move)
        self.assertEqual(Info.search_count([]), before)
        documents = self.sale("2026-03-01", 10) | self.purchase("2026-03-01", 10)
        infos = Info._get_or_create(documents)
        self.assertEqual(infos.mapped("vat_regime"), ["ku", "ku"])

    def test_message_of_a_refund_rest(self):
        """Money from a supplier without document: refund, not advance; it
        can be classified as refund of a business expense."""
        payable = self.template_account("account_1610")
        st_line = self.bank("2026-03-05", 50, rest=payable, partner=self.supplier)
        self.compute(2026)
        message = self.checks(2026, "C-20").message
        self.assertIn("refund or repayment", message)
        rest = st_line.move_id.line_ids.filtered(
            lambda line: line.account_id == payable
        )
        self.env["l10n_de_euer.residual.classification"].create(
            {
                "source_line_id": rest.id,
                "kind": "expense_refund",
                "category_id": self.category("EXP_WORK_EQUIPMENT").id,
            }
        )
        office = self.field_of("EXP_WORK_EQUIPMENT")
        self.assertEqual(self.compute(2026).totals, {office: -5000})
        self.assertFalse(self.checks(2026, "C-20"))

    def test_engine_result_of_a_year_without_forms(self):
        """Years before the form data: engine result without storing."""
        self.set_regime("ku", date_from="2010-01-01")
        invoice = self.sale("2018-03-15", 120)
        self.bank("2018-04-02", 120, matches=[(invoice, 120)])
        with self.assertRaises(UserError):
            self.Run._compute_year(self.company, 2018)
        runs = self.Run.search_count([])
        result = self.Run._engine_result(self.company, 2018)
        self.assertEqual(result.totals, {KU: 12000})
        self.assertNotIn("C-32", {finding.code for finding in result.findings})
        self.assertEqual(self.Run.search_count([]), runs)
