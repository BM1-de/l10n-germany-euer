# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Regression tests of the review of the calculation with real bookings
(stage E2, round 2). All amounts are synthetic (decision D6).

* private lines next to VAT items ("1780 an 1890", "1800 an 1545");
* due dates of bank entries (``invoice_date_due`` of journal entries is the
  day of creation), entered economic years, entered review values;
* gift recipients of bank entries and per document line;
* small business revenue account with real VAT; write-offs in the payment
  wizard, the bank reconciliation and before the payment;
* cash date on a payment entry; opening items without invoice; lock date
  shifts; payments without journal entry; receipts without file;
* the private account journal with the ten-day rule and transfers;
* the extraction reads only the years the run needs.
"""

import datetime
from unittest.mock import patch

from freezegun import freeze_time

from odoo.exceptions import ValidationError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from .scenario_common import SKR03_ACCOUNTS, SKR03_TAXES, ScenarioCommon

KU = "EUER.20.111"
TAXABLE = "EUER.20.112"
OUTPUT_VAT = "EUER.20.140"
REFUND = "EUER.20.141"
GIFT_NOT_DEDUCTIBLE = "EUER.25.164"
GIFT = "EUER.25.174"
INPUT_VAT = "EUER.25.185"
VAT_PAID = "EUER.25.186"
WITHDRAWAL = "EUER.29.122"
CONTRIBUTION = "EUER.29.123"


@tagged("post_install_l10n", "post_install", "-at_install")
class TestReviewRound2(ScenarioCommon, AccountTestInvoicingCommon):
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_euer()

    def ten_day_item(self, move):
        return self.env["l10n_de_euer.ten_day.item"].search(
            [("source_move_id", "=", move.id)]
        )

    # ------------------------------------------------------------ 5.2 (b)
    def test_vat_paid_privately(self):
        """B13: "1780 an 1890" with VAT return period: payment and
        contribution."""
        self.set_regime("regular")
        tax_office = self.tax_office()
        move = self.entry(
            "2026-05-10",
            [("vat_payment", 300), ("private_c", -300)],
            partner=tax_office,
        )
        move.l10n_de_euer_vat_period = "2026-04"
        self.assertFields(2026, {VAT_PAID: 300, CONTRIBUTION: 300})

    def test_refund_set_off_against_private_tax(self):
        """B12: "1800 an 1545": refund and withdrawal."""
        self.set_regime("regular")
        self.entry(
            "2026-06-01",
            [("private_w", 500), ("vat_refund", -500)],
            partner=self.tax_office(),
        )
        self.assertFields(2026, {REFUND: 500, WITHDRAWAL: 500})

    # ------------------------------------------------------------ 5.6.2
    def test_rent_paid_by_bank_in_december(self):
        """B05: no due date of its own: C-26, not decided automatically."""
        rent = self.field_of("EXP_RENT")
        st_line = self.bank("2026-12-28", -800, extra=[("rent", 800)])
        self.compute(2026)
        self.assertIn("C-26", self.codes(2026, "blocker"))
        item = self.ten_day_item(st_line.move_id)
        self.assertFalse(item.decision)
        self.assertFalse(item.due_date)
        self.assertIn("flag on partner or category", item.candidate_reason)
        # entered values survive the next calculation (G-06)
        item.write({"economic_year": 2027, "due_date": "2027-01-01"})
        self.compute(2026)
        self.assertEqual(
            (item.economic_year, item.due_date, item.values_entered),
            (2027, datetime.date(2027, 1, 1), True),
        )
        item.reason = "Rent of January"
        item.action_apply()
        self.assertFields(2026, {})
        self.assertFields(2027, {rent: 800})

    def test_vat_paid_in_january_without_period(self):
        """B09: VAT payment without return period: C-26 with readable
        reasons."""
        self.set_regime("regular")
        self.bank(
            "2027-01-08",
            -186,
            extra=[("vat_payment", 186)],
            partner=self.tax_office(),
        )
        self.compute(2027)
        check = self.checks(2027, "C-26")
        self.assertEqual(check.severity, "blocker")
        self.assertIn("tax office", check.message)
        self.assertNotIn("tax_office", check.message)

    def test_b06_entered_economic_year(self):
        """Maintenance contract for the next year, paid in December; the
        supplier is marked as recurring (an entered year alone is no sign of
        a regularly recurring payment)."""
        self.set_regime("regular")
        self.supplier.l10n_de_euer_recurring = True
        advice = self.field_of("EXP_ADVICE")
        bill = self.invoice(
            "in_invoice",
            "2026-12-15",
            [("advice", 1000, ["vst19"])],
            invoice_payment_term_id=False,
            invoice_date_due="2027-01-02",
        )
        self.assertEqual(str(bill.invoice_date_due), "2027-01-02")
        bill.l10n_de_euer_economic_year = 2027
        self.bank("2026-12-30", -1190, matches=[(bill, 1190)])
        self.assertFields(2026, {})
        self.assertNotIn("C-26", self.codes(2026))
        self.assertFields(2027, {advice: 1000, INPUT_VAT: 190})

    # ------------------------------------------------------------ gifts
    def test_gift_paid_at_a_shop(self):
        """I03/s90: the partner of a bank entry is the shop, no recipient."""
        shop = self.env["res.partner"].create({"name": "Shop"})
        st_lines = [
            self.bank(day, -40, extra=[("gifts", 40)], partner=shop)
            for day in ("2026-05-02", "2026-06-03")
        ]
        self.compute(2026)
        self.assertIn("C-51", self.codes(2026, "blocker"))
        for st_line, name in zip(st_lines, ("Customer A", "Customer B"), strict=True):
            st_line.move_id.l10n_de_euer_gift_recipient_id = self.env[
                "res.partner"
            ].create({"name": name})
        self.assertFields(2026, {GIFT: 80})
        self.assertNotIn("C-51", self.codes(2026))

    def test_gift_lines_of_one_invoice(self):
        """I04: one invoice with gift baskets for two customers."""
        customer_a = self.env["res.partner"].create({"name": "Customer A"})
        customer_b = self.env["res.partner"].create({"name": "Customer B"})
        bill = self.invoice("in_invoice", "2026-11-20", [("gifts", 40), ("gifts", 40)])
        bill.l10n_de_euer_gift_recipient_id = customer_a
        self.bank("2026-11-25", -80, matches=[(bill, 80)])
        self.assertFields(2026, {GIFT_NOT_DEDUCTIBLE: 80})
        info = self.env["l10n_de_euer.move.info"]._get_or_create(bill)
        lines = bill.invoice_line_ids.sorted("id")
        GiftLine = self.env["l10n_de_euer.gift.line"]
        for line, recipient in zip(lines, (customer_a, customer_b), strict=True):
            GiftLine.create(
                {
                    "info_id": info.id,
                    "move_line_id": line.id,
                    "recipient_id": recipient.id,
                }
            )
        self.assertFields(2026, {GIFT: 80})
        other = self.purchase("2026-11-20", 10)
        with self.assertRaises(ValidationError):
            GiftLine.create(
                {
                    "info_id": info.id,
                    "move_line_id": other.invoice_line_ids.id,
                    "recipient_id": customer_a.id,
                }
            )

    # ------------------------------------------------------------ 4.4 no. 5
    def test_small_business_account_with_vat(self):
        """F04: 8195 with 19 % VAT under standard taxation."""
        self.set_regime("regular")
        invoice = self.sale("2026-05-01", 100, taxes=["ust19"])
        self.bank("2026-05-10", 119, matches=[(invoice, 119)])
        self.assertFields(2026, {TAXABLE: 100, OUTPUT_VAT: 19})
        self.assertNotIn("C-33", self.codes(2026))

    # ------------------------------------------------------------ write-offs
    def mixed_invoice(self):
        self.set_regime("regular")
        return self.invoice(
            "out_invoice",
            "2026-03-01",
            [("revenue_19", 1000, ["ust19"]), ("revenue_7", 1000, ["ust7"])],
        )

    def assert_written_off(self):
        self.assertFields(2026, {TAXABLE: "1769.91", OUTPUT_VAT: "230.09"})
        self.assertNotIn("C-36", self.codes(2026))

    def test_write_off_in_the_payment_wizard(self):
        invoice = self.mixed_invoice()
        payment = self.pay(
            invoice,
            "2026-03-10",
            amount=2000,
            payment_difference_handling="reconcile",
            writeoff_account_id=self.acc("writeoff").id,
            writeoff_label="Write-off",
        )
        self.bank_for_payment("2026-03-12", payment)
        self.assert_written_off()

    def test_write_off_in_the_bank_reconciliation(self):
        invoice = self.mixed_invoice()
        self.bank(
            "2026-03-12",
            2000,
            matches=[(invoice, 2260)],
            extra=[("writeoff", 260)],
        )
        self.assert_written_off()

    def test_partial_write_off_before_the_payment(self):
        invoice = self.mixed_invoice()
        write_off = self.entry(
            "2026-03-05",
            [("writeoff", 260), ("receivable", -260)],
            partner=self.customer,
        )
        self.reconcile(invoice, write_off)
        self.bank("2026-03-12", 2000, matches=[(invoice, 2000)])
        self.assert_written_off()

    # ------------------------------------------------------------ 5.6
    def test_cheque_registered_as_payment(self):
        """O1: the cash date of the payment entry counts."""
        invoice = self.sale("2026-11-01", 200)
        payment = self.pay(invoice, "2026-12-29")
        payment.move_id.write(
            {
                "l10n_de_euer_cash_date": "2026-12-29",
                "l10n_de_euer_cash_date_reason": "cheque",
            }
        )
        self.bank_for_payment("2027-01-05", payment)
        self.assertFields(2026, {KU: 200})
        self.assertFields(2027, {})

    # ------------------------------------------------------------ B-08
    def test_opening_items_without_invoice(self):
        """Money in transit and VAT of the previous year need no takeover."""
        self.set_regime("regular")
        tax_office = self.tax_office()
        opening = self.entry(
            "2026-01-01",
            [
                ("bank", 5000),
                ("transfer", 500),
                ("vat_payment", -1000, {"partner_id": tax_office.id}),
                ("opening", -4500),
            ],
        )
        transit = opening.line_ids.filtered(
            lambda line: line.account_id == self.acc("transfer")
        )
        vat = opening.line_ids.filtered(
            lambda line: line.account_id == self.acc("vat_payment")
        )
        self.bank("2026-01-02", 500, matches=[(transit, 500)])
        st_line = self.bank(
            "2026-01-08", -1000, matches=[(vat, 1000)], partner=tax_office
        )
        st_line.move_id.l10n_de_euer_vat_period = "2025-12"
        self.assertFields(2026, {})
        self.assertNotIn("C-35", self.codes(2026))
        self.assertEqual(
            self.Run._engine_result(self.company, 2025).totals, {VAT_PAID: 100000}
        )

    # ------------------------------------------------------------ B-12
    def test_statement_line_moved_to_the_month_end(self):
        """s20: lock date in the past; Odoo posts on the month end."""
        journal = self.env["account.journal"].create(
            {"name": "Bank 2", "code": "BNK2", "type": "bank"}
        )
        with freeze_time("2026-10-18"):
            self.company.fiscalyear_lock_date = "2026-06-30"
            st_line = self.bank(
                "2026-05-15", 620, extra=[("ku_revenue", -620)], journal=journal
            )
            self.assertEqual(str(st_line.move_id.date), "2026-07-31")
            self.compute(2026)
        check = self.checks(2026, "C-12")
        self.assertEqual(check.severity, "warning")
        self.assertIn(st_line.move_id.id, check.res_ids)

    # ------------------------------------------------------------ B-13
    def test_payment_without_journal_entry(self):
        """s71: payment without outstanding account, bank not matched."""
        methods = (
            self.bank_journal.inbound_payment_method_line_ids
            | self.bank_journal.outbound_payment_method_line_ids
        )
        methods.payment_account_id = False
        invoice = self.sale("2026-12-10", 100)
        # as with account_in_payment: the invoice is "in payment"
        with patch.object(
            type(self.env["account.move"]),
            "_get_invoice_in_payment_state",
            lambda _self: "in_payment",
        ):
            payment = self.pay(invoice, "2026-12-20")
            self.assertFalse(payment.move_id)
            self.compute(2026)
        check = self.checks(2026, "C-27")
        self.assertEqual(
            (check.res_model, check.res_ids), ("account.payment", payment.ids)
        )

    # ------------------------------------------------------------ G-14
    def test_receipt_without_file(self):
        receipt = self.invoice("in_receipt", "2026-04-01", [("office", 30)])
        self.bank("2026-04-02", -30, matches=[(receipt, 30)])
        self.compute(2026)
        self.assertIn(receipt.id, self.checks(2026, "C-16").res_ids)

    # ------------------------------------------------------------ R-18
    def private_journal(self):
        return self.env["account.journal"].create(
            {
                "name": "Private Account",
                "code": "PRIV",
                "type": "bank",
                "l10n_de_euer_private_account": True,
            }
        )

    def test_private_account_and_ten_day_rule(self):
        """H08: the expense moves to January, the contribution stays."""
        rent = self.field_of("EXP_RENT")
        st_line = self.bank(
            "2026-12-28", -800, extra=[("rent", 800)], journal=self.private_journal()
        )
        st_line.move_id.write(
            {"l10n_de_euer_economic_year": 2027, "l10n_de_euer_due_date": "2027-01-01"}
        )
        run = self.compute(2026)
        self.assertEqual(run.totals, {CONTRIBUTION: 80000})
        contribution = run.slice_ids.filtered(
            lambda one: one.field_code == CONTRIBUTION
        )
        self.assertEqual(
            (contribution.attribution_year, contribution.amount), (2026, 800)
        )
        self.assertFields(2027, {rent: 800})

    def test_private_money_to_the_business(self):
        """H07: transfer from the private account over money in transit."""
        transfer = self.acc("transfer")
        private = self.bank(
            "2026-07-01",
            -1000,
            extra=[(transfer, 1000)],
            journal=self.private_journal(),
        )
        line = private.move_id.line_ids.filtered(
            lambda line: line.account_id == transfer
        )
        self.bank("2026-07-02", 1000, matches=[(line, 1000)])
        self.assertFields(2026, {CONTRIBUTION: 1000})

    # ------------------------------------------------------------ 5.1
    def test_extraction_reads_the_years_of_the_run(self):
        self.set_regime("ku", date_from="2010-01-01")
        old_invoice = self.sale("2018-03-01", 120)
        unrelated = self.bank("2018-04-01", 50, extra=[("ku_revenue", -50)])
        self.bank("2026-03-01", 120, matches=[(old_invoice, 120)])
        data = self.env["l10n_de_euer.adapter"]._engine_input(self.company, 2026)
        move_ids = {move.id for move in data.moves}
        self.assertIn(old_invoice.id, move_ids)
        self.assertNotIn(unrelated.move_id.id, move_ids)
        self.assertFields(2026, {KU: 120})

    # ------------------------------------------------------------ checks
    def test_messages_in_the_language_of_the_user(self):
        """Amounts are formatted for the language and currency."""
        self.env["res.lang"]._activate_lang("de_DE")
        supplier = self.supplier
        payable = self.template_account("account_1610")
        self.bank("2026-03-05", 92.4, rest=payable, partner=supplier)
        self.Run.with_context(lang="de_DE")._compute_year(self.company, 2026)
        message = self.checks(2026, "C-20").message
        self.assertIn("92,40", message)
        self.assertIn("€", message)

    def test_rests_that_cancel_each_other(self):
        """+75 / −75 on the payable of one supplier: a warning."""
        payable = self.template_account("account_1610")
        self.bank("2026-03-01", -75, rest=payable, partner=self.supplier)
        self.bank("2026-03-09", 75, rest=payable, partner=self.supplier)
        self.compute(2026)
        self.assertEqual(set(self.checks(2026, "C-20").mapped("severity")), {"warning"})

    def test_field_labels_are_unique(self):
        run_fields = self.env["l10n_de_euer.run"]._fields
        self.assertNotEqual(
            run_fields["form_values"].string, run_fields["form_values_text"].string
        )

    def test_classification_of_another_business(self):
        branch = self.env["res.company"].create(
            {
                "name": "Own Business",
                "parent_id": self.company.id,
                "l10n_de_euer_separate_business": True,
            }
        )
        self.env.user.company_ids |= branch
        move = (
            self.env["account.move"]
            .with_company(branch)
            .create(
                {
                    "move_type": "entry",
                    "date": "2026-05-01",
                    "company_id": branch.id,
                    "journal_id": self.misc_journal.id,
                    "line_ids": [
                        (0, 0, {"account_id": self.receivable.id, "balance": 50}),
                        (
                            0,
                            0,
                            {"account_id": self.acc("ku_revenue").id, "balance": -50},
                        ),
                    ],
                }
            )
        )
        move.action_post()
        line = move.line_ids.filtered(lambda line: line.balance > 0)
        with self.assertRaises(ValidationError):
            self.env["l10n_de_euer.residual.classification"].create(
                {
                    "company_id": self.company.id,
                    "source_line_id": line.id,
                    "kind": "loan",
                    "reason": "Loan",
                }
            )
