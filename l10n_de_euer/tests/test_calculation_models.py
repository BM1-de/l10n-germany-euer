# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Models of the calculation: runs, slices, EÜR information of entries,
checks of the calculation, branches, rights (spec 7.3, 7.8, 9.7).

* GT-16: calculation, overrides and recomputation of the VAT treatment never
  write into ``account_move`` or ``account_move_line``, also not on hashed
  entries.
* UT-KU-8: changed VAT status periods recompute the VAT treatment of all
  documents not set by hand, logged once in the chatter of the period.
* ST-T21: a run covers the main company and its branches, not other
  companies; a branch marked as separate business has its own EÜR.
"""

import time

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from .scenario_common import SKR03_ACCOUNTS, SKR03_TAXES, ScenarioCommon, euro

KU = "EUER.20.111"


@tagged("post_install_l10n", "post_install", "-at_install")
class TestCalculationModels(ScenarioCommon, AccountTestInvoicingCommon):
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_euer()

    # ------------------------------------------------------------ GT-16
    def _accounting_state(self):
        self.env.flush_all()
        self.env.cr.execute(
            """
            SELECT (SELECT count(*) FROM account_move),
                   (SELECT max(write_date) FROM account_move),
                   (SELECT count(*) FROM account_move_line),
                   (SELECT max(write_date) FROM account_move_line),
                   (SELECT md5(string_agg(id || ':' || write_date::text, ','
                                          ORDER BY id)) FROM account_move)
            """
        )
        return self.env.cr.fetchone()

    def test_gt16_no_write_on_journal_entries(self):
        self.company_data["default_journal_sale"].restrict_mode_hash_table = True
        invoice = self.sale("2026-03-15", 500)
        self.assertTrue(invoice.inalterable_hash)
        bank = self.bank("2026-04-02", 500, matches=[(invoice, 500)])
        before = self._accounting_state()
        # calculation
        self.compute(2026)
        # overrides through the helper fields of account.move, on a hashed entry
        invoice.write(
            {
                "l10n_de_euer_economic_year": 2026,
                "l10n_de_euer_due_date": "2026-04-01",
                "l10n_de_euer_regular_tax_rate": "19",
            }
        )
        bank.move_id.write(
            {
                "l10n_de_euer_cash_date": "2026-04-01",
                "l10n_de_euer_cash_date_reason": "other",
            }
        )
        # recomputation of the VAT treatment
        self.env["l10n_de_euer.vat.period"].search(
            [("company_id", "=", self.company.id)]
        ).regime = "regular"
        self.assertEqual(invoice.l10n_de_euer_vat_regime, "regular")
        self.compute(2026)
        self.assertEqual(self._accounting_state(), before)
        info = self.env["l10n_de_euer.move.info"].search([("move_id", "=", invoice.id)])
        self.assertEqual(info.economic_year, 2026)
        self.assertEqual(info.regular_tax_rate, "19")
        self.assertEqual(invoice.l10n_de_euer_economic_year, 2026)

    def test_helper_fields_mixed_with_other_values(self):
        invoice = self.sale("2026-03-15", 500, post=False)
        invoice.write({"ref": "R-1", "l10n_de_euer_economic_year": 2025})
        self.assertEqual(invoice.ref, "R-1")
        self.assertEqual(invoice.l10n_de_euer_info_id.economic_year, 2025)

    def test_cash_date_needs_reason(self):
        invoice = self.sale("2026-03-15", 500)
        with self.assertRaises(ValidationError):
            invoice.write({"l10n_de_euer_cash_date": "2026-03-20"})

    def test_vat_period_format(self):
        invoice = self.sale("2026-03-15", 500)
        with self.assertRaises(ValidationError):
            invoice.l10n_de_euer_vat_period = "December"
        invoice.l10n_de_euer_vat_period = "2026-Q4"
        self.assertEqual(invoice.l10n_de_euer_vat_period, "2026-Q4")

    # ------------------------------------------------------------ UT-KU-8
    def test_ut_ku_8_regime_recomputed(self):
        first = self.sale("2026-03-15", 500)
        second = self.sale("2026-08-15", 300)
        self.compute(2026)
        Info = self.env["l10n_de_euer.move.info"]
        info_first = Info.search([("move_id", "=", first.id)])
        info_second = Info.search([("move_id", "=", second.id)])
        self.assertEqual(info_first.vat_regime, "ku")
        info_second.write(
            {
                "vat_regime": "ku",
                "vat_regime_manual": True,
                "vat_regime_reason": "Confirmed by the tax office",
            }
        )
        messages = len(info_first.message_ids)
        period = self.env["l10n_de_euer.vat.period"].search(
            [("company_id", "=", self.company.id)]
        )
        period.date_to = "2025-12-31"
        new_period = self.env["l10n_de_euer.vat.period"].create(
            {
                "company_id": self.company.id,
                "date_from": "2026-01-01",
                "regime": "regular",
            }
        )
        self.assertEqual(info_first.vat_regime, "regular")
        self.assertEqual(info_second.vat_regime, "ku")
        # logged once on the status period, not on every document (s12)
        self.assertEqual(len(info_first.message_ids), messages)
        self.assertIn("1 documents", " ".join(new_period.message_ids.mapped("body")))
        # back to automatic: recomputed at once
        info_second.vat_regime_manual = False
        self.assertEqual(info_second.vat_regime, "regular")

    def test_manual_regime_needs_reason(self):
        invoice = self.sale("2026-03-15", 500)
        info = self.env["l10n_de_euer.move.info"]._get_or_create(invoice)
        with self.assertRaises(ValidationError):
            info.write({"vat_regime_manual": True, "vat_regime": "regular"})

    # ------------------------------------------------------------ runs
    def test_run_replaces_previous_run(self):
        invoice = self.sale("2026-03-15", 500)
        self.bank("2026-04-02", 500, matches=[(invoice, 500)])
        first = self.compute(2026)
        second = self.compute(2026)
        self.assertFalse(first.exists())
        self.assertEqual(second.hash, first.hash if first.exists() else second.hash)
        self.assertEqual(second.year_from, 2026)
        slices = second.slice_ids.filtered("field_code")
        self.assertEqual(slices.mapped("field_code"), [KU])
        self.assertEqual(sum(slices.mapped("amount")), 500)
        self.assertEqual(second._get_field_amounts(), {KU: euro(500)})
        self.assertTrue(second.totals_text)
        self.assertTrue(second.log_text)
        self.assertEqual(
            second.action_open_slices()["domain"], [("run_id", "=", second.id)]
        )

    def test_determinism(self):
        """UT-DET-1 on the ORM: two runs of the same data, the same hash."""
        invoice = self.sale("2026-03-15", 500)
        self.bank("2026-04-02", 200, matches=[(invoice, 200)])
        self.bank("2026-05-02", 300, matches=[(invoice, 300)])
        first = self.compute(2026).hash
        second = self.compute(2026).hash
        self.assertEqual(first, second)

    def test_slices_are_read_only(self):
        invoice = self.sale("2026-03-15", 500)
        self.bank("2026-04-02", 500, matches=[(invoice, 500)])
        run = self.compute(2026)
        one = run.slice_ids[:1]
        with self.assertRaises(UserError):
            one.write({"amount": 1})
        with self.assertRaises(UserError):
            one.unlink()
        with self.assertRaises(UserError):
            self.env["l10n_de_euer.cash.slice"].create(
                {
                    "run_id": run.id,
                    "company_id": self.company.id,
                    "date": "2026-01-01",
                    "attribution_year": 2026,
                    "kind": "cash_direct",
                }
            )

    def test_drilldown_from_the_entry(self):
        """Smart button EÜR: slices of the money entry and of the document."""
        invoice = self.sale("2026-03-15", 500)
        bank = self.bank("2026-04-02", 500, matches=[(invoice, 500)])
        self.compute(2026)
        for move in (invoice, bank.move_id):
            self.assertEqual(move.l10n_de_euer_slice_count, 1)
            action = move.action_l10n_de_euer_slices()
            slices = self.env["l10n_de_euer.cash.slice"].search(action["domain"])
            self.assertEqual(slices.field_code, KU)
            self.assertEqual(slices.source_move_id, bank.move_id)
            self.assertEqual(slices.origin_move_id, invoice)
        self.assertTrue(invoice.l10n_de_euer_enabled)
        info_action = invoice.action_l10n_de_euer_info()
        self.assertEqual(info_action["res_model"], "l10n_de_euer.move.info")

    def test_compute_year_wizard(self):
        invoice = self.sale("2026-03-15", 500)
        self.bank("2026-04-02", 500, matches=[(invoice, 500)])
        wizard = self.env["l10n_de_euer.compute.year"].create(
            {"company_id": self.company.id, "year": 2026}
        )
        action = wizard.action_compute()
        run = self.env["l10n_de_euer.run"].browse(action["res_id"])
        self.assertEqual(run._get_field_amounts(), {KU: euro(500)})
        self.assertEqual(run.company_id, self.company)

    def test_disabled_company(self):
        self.company.l10n_de_euer_enabled = False
        with self.assertRaises(UserError):
            self.Run._compute_year(self.company, 2026)

    # ------------------------------------------------------------ checks
    def test_c15_c16_c17_checks(self):
        invoice = self.sale("2026-03-15", 500)
        self.bank("2026-04-02", 500, matches=[(invoice, 500)])
        self.bank("2026-05-02", 70)
        self.entry(
            "2026-06-01",
            [("bank", -20, {"name": False}), ("bank_fees", 20, {"name": False})],
            journal=self.bank_journal,
        )
        self.compute(2026)
        self.assertEqual(self.checks(2026, "C-15").severity, "info")
        c16 = self.checks(2026, "C-16")
        self.assertEqual(c16.res_ids, invoice.ids)
        self.assertTrue(self.checks(2026, "C-17"))
        # a file attached to the invoice resolves C-16
        self.env["ir.attachment"].create(
            {
                "name": "invoice.pdf",
                "res_model": "account.move",
                "res_id": invoice.id,
                "raw": b"%PDF-1.4",
            }
        )
        self.compute(2026)
        self.assertFalse(self.checks(2026, "C-16"))

    def test_c24_payment_on_bank_account(self):
        """Payment booked on the bank account: the statement date counts."""
        invoice = self.sale("2026-12-10", 100)
        method_line = self.bank_journal.inbound_payment_method_line_ids[:1]
        method_line.payment_account_id = self.acc("bank")
        payment = self.pay(invoice, "2026-12-30", payment_method_line_id=method_line.id)
        bank_line = payment.move_id.line_ids.filtered(
            lambda line: line.account_id == self.acc("bank")
        )
        self.bank("2027-01-04", 100, matches=[(bank_line, 100)])
        self.assertFields(2026, {})
        self.assertFields(2027, {KU: 100})
        self.assertIn("C-24", self.codes(2027))

    def test_c39_lock_date_exception(self):
        self.company.fiscalyear_lock_date = "2026-12-31"
        self.env["account.lock_exception"].create(
            {
                "company_id": self.company.id,
                "fiscalyear_lock_date": "2025-12-31",
                "reason": "late bank statement",
            }
        )
        self.compute(2026)
        self.assertEqual(self.checks(2026, "C-39").severity, "warning")

    def test_acknowledged_warning_kept(self):
        """A warning acknowledged stays acknowledged while it is unchanged."""
        invoice = self.sale("2026-03-15", 500)
        self.bank("2026-04-02", 500, matches=[(invoice, 500)])
        self.compute(2026)
        check = self.checks(2026, "C-16")
        check.acknowledge_reason = "Paper invoice in the folder"
        check.action_acknowledge()
        self.compute(2026)
        self.assertTrue(self.checks(2026, "C-16").acknowledged_on)
        other = self.sale("2026-05-15", 200)
        self.bank("2026-06-02", 200, matches=[(other, 200)])
        self.compute(2026)
        self.assertFalse(self.checks(2026, "C-16").acknowledged_on)

    def test_user_rights(self):
        """O-09: bookkeeper computes, decides and classifies; readonly reads."""
        user = new_test_user(
            self.env,
            login="euer_bookkeeper",
            groups="account.group_account_user",
            company_id=self.company.id,
            company_ids=[Command.set(self.company.ids)],
        )
        reader = new_test_user(
            self.env,
            login="euer_reader",
            groups="account.group_account_readonly",
            company_id=self.company.id,
            company_ids=[Command.set(self.company.ids)],
        )
        invoice = self.sale("2026-03-15", 500)
        bank = self.bank("2026-04-02", 800, matches=[(invoice, 500)], rest="receivable")
        run = self.Run.with_user(user)._compute_year(self.company, 2026)
        self.assertEqual(run._get_field_amounts(), {KU: euro(500)})
        # a second run replaces the first one, also for a bookkeeper
        again = self.Run.with_user(user)._compute_year(self.company, 2026)
        self.assertFalse(run.exists())
        self.assertTrue(again.with_user(reader).slice_ids)
        rest = bank.move_id.line_ids.filtered(
            lambda line: line.account_id == self.receivable
            and line.balance < 0
            and not line.reconciled
        )
        self.env["l10n_de_euer.residual.classification"].with_user(user).create(
            {
                "company_id": self.company.id,
                "source_line_id": rest.id,
                "kind": "deposit",
                "reason": "Deposit for equipment",
            }
        )
        invoice.with_user(user).write({"l10n_de_euer_economic_year": 2026})
        self.env["l10n_de_euer.cash.slice"].with_user(reader).search([])
        with self.assertRaises(AccessError):
            self.Run.with_user(reader)._compute_year(self.company, 2026)
        with self.assertRaises(AccessError):
            invoice.with_user(reader).write({"l10n_de_euer_economic_year": 2025})

    # ------------------------------------------------------------ ST-T21
    def test_t21_branches_and_other_company(self):
        branch = self.env["res.company"].create(
            {"name": "Branch", "parent_id": self.company.id}
        )
        self.env.user.company_ids |= branch
        invoice = (
            self.env["account.move"]
            .with_company(branch)
            .create(
                {
                    "move_type": "entry",
                    "date": "2026-03-15",
                    "company_id": branch.id,
                    "journal_id": self.misc_journal.id,
                    "line_ids": [
                        Command.create(
                            {
                                "name": "Branch sale",
                                "account_id": self.receivable.id,
                                "partner_id": self.customer.id,
                                "balance": 400,
                                "date_maturity": "2026-03-15",
                            }
                        ),
                        Command.create(
                            {
                                "name": "Branch sale",
                                "account_id": self.acc("ku_revenue").id,
                                "balance": -400,
                            }
                        ),
                    ],
                }
            )
        )
        invoice.action_post()
        self.bank("2026-04-02", 400, matches=[(invoice, 400)])
        other = self.setup_other_company(name="Other Business")["company"]
        self.assertFalse(other.l10n_de_euer_enabled)
        # the user must have selected all companies of the business
        with self.assertRaises(AccessError):
            self.Run.with_context(allowed_company_ids=self.company.ids)._compute_year(
                self.company, 2026
            )
        Run = self.Run.with_context(allowed_company_ids=(self.company | branch).ids)
        run = Run._compute_year(self.company, 2026)
        self.assertEqual(run._get_field_amounts(), {KU: euro(400)})
        # the branch as separate business has its own calculation
        branch.l10n_de_euer_separate_business = True
        self.assertEqual(branch._l10n_de_euer_business_root(), branch)
        self.assertEqual(self.company._l10n_de_euer_business_companies(), self.company)
        run = Run._compute_year(branch, 2026)
        self.assertEqual(run.company_id, branch)
        # the money came to the main company: in the branch, nothing is paid
        self.assertEqual(run._get_field_amounts(), {})

    # ------------------------------------------------------------ resolve
    def test_resolve_actions(self):
        tax_office = self.tax_office()
        bank = self.bank("2027-01-02", -300, extra=[("vat_payment", 300)])
        self.assertTrue(bank)
        self.bank("2026-12-20", 100, rest="receivable", partner=self.customer)
        bill = self.purchase("2026-12-01", 50, partner=tax_office)
        self.bank("2027-01-03", -50, matches=[(bill, 50)])
        self.compute(2027)
        c23 = self.checks(2027, "C-23")
        self.assertTrue(c23)
        action = c23.action_resolve()
        self.assertEqual(action["res_model"], "l10n_de_euer.ten_day.item")
        items = self.env["l10n_de_euer.ten_day.item"].search(action["domain"])
        self.assertTrue(items)
        with self.assertRaises(UserError):
            items[:1].action_apply()
        items.write({"reason": "Order given on 2 January"})
        items.action_apply()
        self.compute(2027)
        self.assertFalse(self.checks(2027, "C-23"))

    # ------------------------------------------------------------ UT-PERF-1
    def test_ut_perf_1_small(self):
        """Run time of a business with 100 paid invoices (logged, B-15)."""
        invoices = self.env["account.move"].create(
            [
                {
                    "move_type": "out_invoice",
                    "partner_id": self.customer.id,
                    "invoice_date": "2026-03-01",
                    "invoice_line_ids": [
                        Command.create(
                            {
                                "name": "Sale",
                                "account_id": self.acc("ku_revenue").id,
                                "price_unit": 10 + index,
                                "tax_ids": [Command.clear()],
                            }
                        )
                    ],
                }
                for index in range(100)
            ]
        )
        invoices.action_post()
        for invoice in invoices:
            self.bank(
                "2026-04-01",
                invoice.amount_total,
                matches=[(invoice, invoice.amount_total)],
            )
        start = time.perf_counter()
        run = self.compute(2026)
        duration = time.perf_counter() - start
        self.assertLess(duration, 30)
        self.assertEqual(
            run._get_field_amounts()[KU], euro(sum(10 + index for index in range(100)))
        )
