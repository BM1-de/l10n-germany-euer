# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Regression tests of the review of the calculation with real bookings
(stage E2, round 3). All amounts are synthetic (decision D6).

* private payments with a discount next to the open items, in SKR03 and
  SKR04 (5.2 c); private accounts as default account of a money journal;
  the private account journal per money movement (R-18);
* a manual input VAT line of a gift above the limit; a write-off in the
  statement matched with a registered payment; a payment on the bank
  account matched with statement lines of two years; a payment entry
  reversed after the bank reconciliation; a credit note reconciled after
  the end of its year; a single invoice with an entered economic year;
* branches: the EÜR fields of an entry for a user of the branch only,
  archived branches, lock dates and their exceptions of the main company;
* rights of the accounting manager (Odoo 19: no longer a bookkeeper);
* open items taken over belong to an opening entry of the business.
"""

import datetime

from freezegun import freeze_time

from odoo import Command
from odoo.exceptions import ValidationError
from odoo.tests import Form, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from .scenario_common import (
    SKR03_ACCOUNTS,
    SKR03_TAXES,
    SKR04_ACCOUNTS,
    SKR04_TAXES,
    ScenarioCommon,
)

KU = "EUER.20.111"
TAXABLE = "EUER.20.112"
OUTPUT_VAT = "EUER.20.140"
GOODS = "EUER.25.100"
GIFT_NOT_DEDUCTIBLE = "EUER.25.164"
INPUT_VAT = "EUER.25.185"
WITHDRAWAL = "EUER.29.122"
CONTRIBUTION = "EUER.29.123"


class PrivatePaymentCases(ScenarioCommon):
    """5.2 (c): the owner or the customer pays with private money and books
    the discount in the same journal entry (N01 to N03, S04)."""

    def settle(self, document, lines, partner):
        item = self.open_line(document)
        entry = self.entry("2026-03-10", lines, partner=partner)
        (
            item
            | entry.line_ids.filtered(lambda line: line.account_id == item.account_id)
        ).reconcile()
        return entry

    def test_n01_customer_pays_to_the_private_account(self):
        invoice = self.sale("2026-03-01", 1000)
        self.settle(
            invoice,
            [
                ("private_w", 980),
                ("discount_given", 20),
                (self.open_line(invoice).account_id, -1000),
            ],
            self.customer,
        )
        self.assertFields(2026, {KU: 980, WITHDRAWAL: 980})
        self.assertNotIn("C-25", self.codes(2026))

    def test_n02_owner_pays_a_bill_privately(self):
        bill = self.purchase("2026-03-01", 1000)
        self.settle(
            bill,
            [
                (self.open_line(bill).account_id, 1000),
                ("private_c", -980),
                ("discount_received", -20),
            ],
            self.supplier,
        )
        self.assertFields(
            2026,
            {self.field_of("EXP_WORK_EQUIPMENT"): 1000, GOODS: -20, CONTRIBUTION: 980},
        )

    def test_n03_with_the_correction_of_the_input_vat(self):
        self.set_regime("regular")
        bill = self.purchase("2026-03-01", 1000, taxes=["vst19"])
        self.settle(
            bill,
            [
                (self.open_line(bill).account_id, 1190),
                ("private_c", -1166.2),
                ("discount_received_19", -20, self.base_line("vst19")),
                ("vat_in", -3.8, self.tax_line("vst19")),
            ],
            self.supplier,
        )
        self.assertFields(
            2026,
            {
                self.field_of("EXP_WORK_EQUIPMENT"): 1000,
                GOODS: -20,
                INPUT_VAT: 186.2,
                CONTRIBUTION: 1166.2,
            },
        )


@tagged("post_install_l10n", "post_install", "-at_install")
class TestPrivatePaymentSkr03(PrivatePaymentCases, AccountTestInvoicingCommon):
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_euer()


@tagged("post_install_l10n", "post_install", "-at_install")
class TestPrivatePaymentSkr04(PrivatePaymentCases, AccountTestInvoicingCommon):
    CHART = "de_skr04"
    ACCOUNTS = SKR04_ACCOUNTS
    TAXES = SKR04_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr04")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_euer()


@tagged("post_install_l10n", "post_install", "-at_install")
class TestReviewRound3(ScenarioCommon, AccountTestInvoicingCommon):
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_euer()

    def journal(self, code, journal_type, account_key=None, **values):
        if account_key:
            values["default_account_id"] = self.acc(account_key).id
        return self.env["account.journal"].create(
            {"name": code, "code": code, "type": journal_type, **values}
        )

    def branch(self, name="Branch", **values):
        branch = self.env["res.company"].create(
            {"name": name, "parent_id": self.company.id, **values}
        )
        self.env.user.company_ids |= branch
        return branch

    def make_user(self, login, groups, companies, company=None):
        return new_test_user(
            self.env,
            login=login,
            groups=groups,
            company_id=(company or companies[:1]).id,
            company_ids=[Command.set(companies.ids)],
        )

    # ------------------------------------------------------------ 5.2
    def test_n04_journal_paid_privately(self):
        """Cash journal "paid privately" on 1890 without the mark of R-18."""
        self.set_regime("regular")
        journal = self.journal("PRVB", "cash", "private_c")
        bill = self.purchase("2026-03-01", 100, taxes=["vst19"])
        self.bank("2026-03-05", -119, matches=[(bill, 119)], journal=journal)
        self.assertFields(
            2026,
            {
                self.field_of("EXP_WORK_EQUIPMENT"): 100,
                INPUT_VAT: 19,
                CONTRIBUTION: 119,
            },
        )

    def test_p02_transfer_to_a_private_journal(self):
        journal = self.journal("PRVE", "cash", "private_w")
        self.bank("2026-05-10", -1000, extra=[(journal.default_account_id, 1000)])
        self.assertFields(2026, {WITHDRAWAL: 1000})

    # ------------------------------------------------------------ R-18
    def test_p01_private_account_receives_with_discount(self):
        self.set_regime("regular")
        private = self.journal("PRP1", "bank", l10n_de_euer_private_account=True)
        invoice = self.invoice(
            "out_invoice", "2026-05-01", [("revenue_19", 1000, ["ust19"])]
        )
        self.bank(
            "2026-05-10",
            1166.2,
            matches=[(invoice, 1190)],
            extra=[
                ("discount_19", 20, self.base_line("ust19")),
                ("vat_out", 3.8, self.tax_line("ust19")),
            ],
            journal=private,
        )
        self.assertFields(2026, {TAXABLE: 980, OUTPUT_VAT: 186.2, WITHDRAWAL: 1166.2})

    # ------------------------------------------------------------ 5.7 no. 7
    def test_i08_gift_with_a_manual_input_vat_line(self):
        self.set_regime("regular")
        shop = self.env["res.partner"].create({"name": "Shop"})
        st_line = self.bank(
            "2026-03-01",
            -71.4,
            extra=[("gifts", 60), ("vat_in", 11.4)],
            partner=shop,
        )
        st_line.move_id.l10n_de_euer_gift_recipient_id = self.env["res.partner"].create(
            {"name": "Customer A"}
        )
        self.assertFields(2026, {GIFT_NOT_DEDUCTIBLE: 71.4})
        self.assertIn("C-52", self.codes(2026))

    # ------------------------------------------------------------ 5.5
    def test_write_off_in_the_statement_of_a_payment(self):
        """f1: payment registered, statement matched with it, difference
        written off in the statement entry."""
        self.set_regime("regular")
        invoice = self.invoice(
            "out_invoice",
            "2026-03-01",
            [("revenue_19", 1000, ["ust19"]), ("revenue_7", 1000, ["ust7"])],
        )
        payment = self.pay(invoice, "2026-03-10")
        outstanding = payment.move_id.line_ids.filtered(
            lambda line: line.account_id == payment.outstanding_account_id
        )
        self.bank(
            "2026-03-12",
            2000,
            matches=[(outstanding, 2260)],
            extra=[("writeoff", 260)],
        )
        self.assertFields(2026, {TAXABLE: "1769.91", OUTPUT_VAT: "230.09"})
        self.assertNotIn("C-36", self.codes(2026))

    # ------------------------------------------------------------ C-24
    def test_payment_on_the_bank_account_in_two_years(self):
        self.set_regime("regular")
        methods = (
            self.bank_journal.inbound_payment_method_line_ids
            | self.bank_journal.outbound_payment_method_line_ids
        )
        methods.payment_account_id = self.bank_journal.default_account_id
        invoice = self.invoice(
            "out_invoice", "2026-11-01", [("revenue_19", 1000, ["ust19"])]
        )
        payment = self.pay(invoice, "2026-12-20")
        money = payment.move_id.line_ids.filtered(
            lambda line: line.account_id == self.bank_journal.default_account_id
        )
        self.bank("2026-12-30", 595, matches=[(money, 595)])
        self.bank("2027-01-20", 595, matches=[(money, 595)])
        for year in (2026, 2027):
            self.assertFields(year, {TAXABLE: 500, OUTPUT_VAT: 95})
            self.assertIn("C-24", self.codes(year, "warning"))

    # ------------------------------------------------------------ 5.4 no. 3
    def test_payment_entry_reversed_after_the_bank_reconciliation(self):
        self.set_regime("regular")
        invoice = self.invoice(
            "out_invoice", "2026-03-01", [("revenue_19", 1000, ["ust19"])]
        )
        payment = self.pay(invoice, "2026-03-10")
        self.bank_for_payment("2026-03-12", payment)
        self.assertFields(2026, {TAXABLE: 1000, OUTPUT_VAT: 190})
        payment.move_id._reverse_moves(
            [{"date": datetime.date(2026, 3, 20), "ref": "Reversal"}], cancel=True
        )
        self.assertFields(2026, {})
        checks = self.checks(2026, "C-20")
        self.assertEqual(checks.mapped("severity"), ["blocker"])
        self.assertIn("outstanding", checks.message)

    # ------------------------------------------------------------ C-60
    def test_credit_note_reconciled_after_the_year(self):
        self.set_regime("regular")
        invoice = self.invoice(
            "out_invoice",
            "2025-11-03",
            [("revenue_19", 1000, ["ust19"]), ("exempt_revenue", 1000)],
        )
        credit = self.invoice(
            "out_refund", "2025-12-15", [("revenue_19", 1000, ["ust19"])]
        )
        self.bank("2025-12-20", 1000, matches=[(invoice, 1000)])
        self.reconcile(invoice, credit)
        self.env.flush_all()
        self.env.cr.execute(
            """
            UPDATE account_partial_reconcile SET create_date = '2026-02-01'
             WHERE debit_move_id = ANY(%s) AND credit_move_id = ANY(%s)
            """,
            [invoice.line_ids.ids, credit.line_ids.ids],
        )
        self.compute(2025)
        self.assertEqual(self.checks(2025, "C-60").severity, "warning")

    # ------------------------------------------------------------ 5.6.1
    def test_single_invoice_with_an_entered_economic_year(self):
        """A single invoice of December paid in
        January; the entered economic year alone decides nothing."""
        invoice = self.sale(
            "2026-12-13",
            80,
            invoice_payment_term_id=False,
            invoice_date_due="2026-12-28",
        )
        invoice.l10n_de_euer_economic_year = 2026
        self.bank("2027-01-10", 80, matches=[(invoice, 80)])
        self.assertFields(2027, {KU: 80})
        self.assertIn("C-26", self.codes(2027, "blocker"))
        item = self.env["l10n_de_euer.ten_day.item"].search(
            [("origin_move_id", "=", invoice.id)]
        )
        self.assertEqual((item.proposal, item.decision), ("apply", False))

    # ------------------------------------------------------------ branches
    def test_entry_of_a_branch_for_a_user_of_the_branch(self):
        """s09: only the branch selected, the form of an entry of the branch
        opens; the setting of the main company is read as superuser."""
        branch = self.branch()
        move = (
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
                            {"account_id": self.receivable.id, "balance": 400}
                        ),
                        Command.create(
                            {"account_id": self.acc("ku_revenue").id, "balance": -400}
                        ),
                    ],
                }
            )
        )
        move.action_post()
        for login, groups in (
            ("euer_branch_billing", "account.group_account_invoice"),
            ("euer_branch_bookkeeper", "account.group_account_user"),
            ("euer_branch_reader", "account.group_account_readonly"),
        ):
            user = self.make_user(login, groups, self.company | branch, company=branch)
            branch_move = move.with_user(user).with_context(
                allowed_company_ids=branch.ids
            )
            self.env.invalidate_all()
            self.assertTrue(branch_move.l10n_de_euer_enabled)
            self.env.invalidate_all()
            Form(branch_move)

    def test_archived_branch(self):
        branch = self.branch("Old Branch")
        user = self.make_user(
            "euer_archived_branch", "account.group_account_user", self.company | branch
        )
        invoice = self.sale("2026-03-15", 500)
        self.bank("2026-04-02", 500, matches=[(invoice, 500)])
        branch.active = False
        run = (
            self.Run.with_user(user)
            .with_context(allowed_company_ids=self.company.ids)
            ._compute_year(self.company, 2026)
        )
        self.assertEqual(run.totals, {KU: 50000})

    def test_own_business_computed_by_its_own_user(self):
        """The settings of the main company are read as superuser; the user
        selected only the branch that is a business of its own."""
        branch = self.branch("Own Business", l10n_de_euer_separate_business=True)
        user = self.make_user("euer_own_only", "account.group_account_user", branch)
        self.env.invalidate_all()
        run = (
            self.Run.with_user(user)
            .with_context(allowed_company_ids=branch.ids)
            ._compute_year(branch, 2026)
        )
        self.assertEqual(run.company_id, branch)

    def test_lock_date_of_the_main_company(self):
        """s05: Odoo moves a statement line of a branch journal by the lock
        date of the main company (C-12)."""
        branch = self.branch()
        self.env = self.env(
            context=dict(
                self.env.context, allowed_company_ids=(self.company | branch).ids
            )
        )
        journal = self.journal(
            "BFIL",
            "bank",
            company_id=branch.id,
            suspense_account_id=self.bank_journal.suspense_account_id.id,
        )
        with freeze_time("2026-10-18"):
            self.company.fiscalyear_lock_date = "2026-06-30"
            st_line = self.bank(
                "2026-05-15", 620, extra=[("ku_revenue", -620)], journal=journal
            )
            self.assertNotEqual(str(st_line.move_id.date), "2026-05-15")
            self.compute(2026)
        self.assertIn(st_line.move_id.id, self.checks(2026, "C-12").res_ids)

    def test_lock_date_exception_of_the_main_company(self):
        """s13: an exception of the main company opens a branch that is a
        business of its own; one of the tax lock date does not open a year
        whose fiscal year stays locked."""
        branch = self.branch("Own Business", l10n_de_euer_separate_business=True)
        Exception_ = self.env["account.lock_exception"]
        Run = self.Run.with_context(allowed_company_ids=(self.company | branch).ids)
        self.company.fiscalyear_lock_date = "2026-12-31"
        exception = Exception_.create(
            {
                "company_id": self.company.id,
                "fiscalyear_lock_date": "2025-12-31",
                "reason": "late bank statement",
            }
        )
        Run._compute_year(branch, 2026)
        checks = self.env["l10n_de_euer.check"].search(
            [("company_id", "=", branch.id), ("year", "=", 2026), ("code", "=", "C-39")]
        )
        self.assertEqual(checks.severity, "warning")
        exception.action_revoke()
        self.company.tax_lock_date = "2026-12-31"
        Exception_.create(
            {
                "company_id": self.company.id,
                "tax_lock_date": "2025-12-31",
                "reason": "VAT correction",
            }
        )
        self.compute(2026)
        self.assertFalse(self.checks(2026, "C-39"))

    # ------------------------------------------------------------ 7.10
    def test_accounting_manager(self):
        """Odoo 19: the manager group no longer implies the bookkeeper."""
        manager = self.make_user(
            "euer_manager_only", "account.group_account_manager", self.company
        )
        self.assertFalse(manager.has_group("account.group_account_user"))
        invoice = self.sale("2026-03-15", 500)
        self.bank("2026-04-02", 800, matches=[(invoice, 500)], rest="receivable")
        wizard = (
            self.env["l10n_de_euer.compute.year"]
            .with_user(manager)
            .create({"company_id": self.company.id, "year": 2026})
        )
        run = self.env["l10n_de_euer.run"].browse(wizard.action_compute()["res_id"])
        self.assertTrue(run.with_user(manager).slice_ids)
        self.env["l10n_de_euer.cash.slice"].with_user(manager).search([])
        self.env["l10n_de_euer.ten_day.item"].with_user(manager).search([])
        rest = self.checks(2026, "C-20").res_ids
        self.env["l10n_de_euer.residual.classification"].with_user(manager).create(
            {
                "company_id": self.company.id,
                "source_line_id": rest[0],
                "kind": "deposit",
                "reason": "Deposit",
            }
        )
        bill = self.purchase("2026-05-01", 40, account="gifts")
        info = self.env["l10n_de_euer.move.info"]._get_or_create(bill)
        self.env["l10n_de_euer.gift.line"].with_user(manager).create(
            {
                "info_id": info.id,
                "move_line_id": bill.invoice_line_ids.id,
                "recipient_id": self.customer.id,
            }
        )
        arch = (
            self.env["account.move"]
            .with_user(manager)
            .get_view(self.env.ref("account.view_move_form").id)["arch"]
        )
        self.assertIn("l10n_de_euer_tab", arch)
        self.assertIn("action_l10n_de_euer_slices", arch)

    # ------------------------------------------------------------ B-08
    def test_open_item_of_an_opening_entry_only(self):
        opening = self.entry(
            "2027-01-01",
            [
                ("receivable", 120, {"partner_id": self.customer.id}),
                ("opening_debtors", -120),
            ],
        )
        invoice = self.sale("2027-01-05", 50)
        Item = self.env["l10n_de_euer.opening.item"]
        values = {
            "company_id": self.company.id,
            "category_id": self.category("REV_KU").id,
            "vat_regime": "ku",
        }
        with self.assertRaises(ValidationError):
            Item.create({**values, "move_line_id": self.open_line(invoice).id})
        with self.assertRaises(ValidationError):
            Item.create(
                {
                    **values,
                    "move_line_id": opening.line_ids.filtered(
                        lambda line: line.balance < 0
                    ).id,
                }
            )
        other = self.setup_other_company(name="Other Business")["company"]
        self.env.user.company_ids |= other
        with self.assertRaises(ValidationError):
            Item.with_context(allowed_company_ids=(self.company | other).ids).create(
                {
                    **values,
                    "company_id": other.id,
                    "move_line_id": self.open_line(opening).id,
                }
            )
        self.assertTrue(
            Item.create({**values, "move_line_id": self.open_line(opening).id})
        )

    # ------------------------------------------------------------ jit
    def test_extraction_restores_jit(self):
        self.env.cr.execute("SHOW jit")
        before = self.env.cr.fetchone()[0]
        self.env["l10n_de_euer.adapter"]._engine_input(self.company, 2026)
        self.env.cr.execute("SHOW jit")
        self.assertEqual(self.env.cr.fetchone()[0], before)
