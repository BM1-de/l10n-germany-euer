# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Core scenarios ST-T01 to ST-T31 with real bookings (spec 9.5).

The cases run on SKR03 and SKR04 (the same bookings with the accounts of
each chart). Amounts are synthetic (D6). The business is a small business
(``KU``) unless a case sets standard taxation.
"""

from odoo import Command
from odoo.exceptions import ValidationError
from odoo.tests import tagged

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
REFUND = "EUER.20.141"
INPUT_VAT = "EUER.25.185"
VAT_PAID = "EUER.25.186"
OTHER = "EUER.25.183"
WITHDRAWAL = "EUER.29.122"
CONTRIBUTION = "EUER.29.123"


class CoreScenarios(ScenarioCommon):
    """Scenarios of ``odoo_technik.md`` 7 as adapted by spec 9.5."""

    def office_field(self, year=2026):
        """Form field of the office supplies category (mapping of the year)."""
        mapping = self.env["l10n_de_euer.category.mapping"].search(
            [
                ("category_id.code", "=", "EXP_WORK_EQUIPMENT"),
                ("year", "=", min(year, 2026)),
            ],
            limit=1,
        )
        return mapping.field_id.code

    # ------------------------------------------------------------ T01-T05
    def test_t01_invoice_paid_by_bank(self):
        invoice = self.sale("2026-03-15", 500)
        self.bank("2026-04-02", 500, matches=[(invoice, 500)])
        self.assertFields(2026, {KU: 500})
        self.assertEqual(self.codes(2026, "blocker"), [])

    def test_t02_receipt_in_next_year(self):
        invoice = self.sale("2025-11-15", 500)
        self.bank("2026-01-15", 500, matches=[(invoice, 500)])
        self.assertFields(2025, {})
        self.assertFields(2026, {KU: 500})

    def test_t03_partial_payments_regular(self):
        """500 on 1,404: 356.13 / 67.66 / 71.22 / 4.99 (spec 5.10)."""
        self.set_regime("regular")
        invoice = self.invoice(
            "out_invoice",
            "2025-11-10",
            [("revenue_19", 1000, ["ust19"]), ("revenue_7", 200, ["ust7"])],
        )
        self.assertEqual(invoice.amount_total, 1404)
        self.bank("2025-12-01", 500, matches=[(invoice, 500)])
        self.bank("2026-01-15", 904, matches=[(invoice, 904)])
        self.assertFields(2025, {TAXABLE: "427.35", OUTPUT_VAT: "72.65"})
        self.assertFields(2026, {TAXABLE: "772.65", OUTPUT_VAT: "131.35"})

    def test_t04_payment_registered_bank_next_year(self):
        invoice = self.sale("2025-12-28", 300)
        payment = self.pay(invoice, "2025-12-30")
        self.assertIn(invoice.payment_state, ("paid", "in_payment"))
        self.bank_for_payment("2026-01-02", payment)
        self.assertFields(2025, {})
        # invoice of 2025 paid in the window: candidate of the ten-day rule
        self.assertIn("C-26", self.codes(2025, "blocker"))
        self.assertFields(2026, {KU: 300})

    def test_t04_payment_without_bank(self):
        invoice = self.sale("2026-06-28", 300)
        self.pay(invoice, "2026-06-30")
        self.assertFields(2026, {})
        self.assertIn("C-27", self.codes(2026))

    def test_t04b_transfer_order_override(self):
        bill = self.purchase("2026-12-15", 100)
        payment = self.pay(bill, "2026-12-30")
        statement = self.bank_for_payment("2027-01-02", payment)
        self.assertFields(2026, {})
        self.assertFields(2027, {self.office_field(): 100})
        self.assertIn("C-23", self.codes(2027))
        item = self.env["l10n_de_euer.ten_day.item"].search(
            [("source_move_id", "=", statement.move_id.id)]
        )
        self.assertEqual(item.kind, "transfer_order")
        statement.move_id.write(
            {
                "l10n_de_euer_cash_date": "2026-12-30",
                "l10n_de_euer_cash_date_reason": "transfer_order",
            }
        )
        self.assertFields(2026, {self.office_field(): 100})
        self.assertFields(2027, {})
        self.assertNotIn("C-23", self.codes(2027))

    def test_t05_collective_payment(self):
        invoices = [self.sale("2026-05-01", amount) for amount in (100, 200, 300)]
        moves = invoices[0] | invoices[1] | invoices[2]
        payments = self.pay(moves, "2026-05-30", group_payment=True)
        self.bank_for_payment("2026-06-01", payments)
        self.assertFields(2026, {KU: 600})
        self.assertNotIn("C-27", self.codes(2026))

    def test_t05b_collective_payment_partly_matched(self):
        invoices = [self.sale("2026-05-01", amount) for amount in (100, 200, 300)]
        moves = invoices[0] | invoices[1] | invoices[2]
        payments = self.pay(moves, "2026-05-30", group_payment=True)
        self.bank_for_payment("2026-06-01", payments, amount=300)
        self.assertFields(2026, {KU: 300})
        self.assertIn("C-27", self.codes(2026))

    # ------------------------------------------------------------ credit notes
    def test_t06_full_reversal(self):
        invoice = self.sale("2026-11-05", 1190)
        reversal = invoice._reverse_moves(cancel=True)
        self.assertEqual(reversal.state, "posted")
        self.assertFields(2026, {})

    def test_g2_credit_note_without_money(self):
        invoice = self.sale("2026-11-05", 500)
        credit = self.invoice("out_refund", "2026-11-20", [("ku_revenue", 500)])
        self.reconcile(invoice, credit)
        self.assertFields(2026, {})

    def test_t07_partial_credit_note(self):
        self.set_regime("regular")
        invoice = self.invoice(
            "out_invoice",
            "2026-11-10",
            [("revenue_19", 100, ["ust19"]), ("revenue_7", 100, ["ust7"])],
        )
        credit = self.invoice(
            "out_refund", "2026-11-20", [("revenue_19", 100, ["ust19"])]
        )
        self.reconcile(invoice, credit)
        self.bank("2027-01-25", 107, matches=[(invoice, 107)])
        self.assertFields(2026, {})
        self.assertFields(2027, {TAXABLE: 100, OUTPUT_VAT: 7})

    def test_t08_credit_note_after_partial_payment(self):
        """Payments before the credit note stay unchanged (R-11)."""
        self.set_regime("regular")
        invoice = self.invoice(
            "out_invoice",
            "2026-12-01",
            [("revenue_19", 100, ["ust19"]), ("revenue_7", 100, ["ust7"])],
        )
        self.bank("2026-12-15", 50, matches=[(invoice, 50)])
        credit = self.invoice(
            "out_refund", "2027-01-12", [("revenue_19", 100, ["ust19"])]
        )
        self.reconcile(invoice, credit)
        self.bank("2027-01-20", 57, matches=[(invoice, 57)])
        # 8400 22.13, 1776 4.20, 8300 22.12, 1771 1.55
        self.assertFields(2026, {TAXABLE: "44.25", OUTPUT_VAT: "5.75"})
        # 8400 -22.13, 1776 -4.20, 8300 77.88, 1771 5.45
        self.assertFields(2027, {TAXABLE: "55.75", OUTPUT_VAT: "1.25"})

    def test_t26_credit_note_paid_back(self):
        credit = self.invoice("out_refund", "2026-08-01", [("ku_revenue", 50)])
        self.bank("2026-08-05", -50, matches=[(credit, 50)])
        # negative income moves to Kz 183 (R-12)
        self.assertFields(2026, {OTHER: 50})

    def test_t27_one_payment_for_invoice_and_credit_note(self):
        self.set_regime("regular")
        invoice = self.invoice(
            "out_invoice", "2026-04-01", [("revenue_19", 100, ["ust19"])]
        )
        credit = self.invoice(
            "out_refund", "2026-04-05", [("revenue_7", 18.69, ["ust7"])]
        )
        self.assertEqual(credit.amount_total, 20)
        self.bank("2026-04-10", 99, matches=[(invoice, 119), (credit, 20)])
        self.assertFields(2026, {TAXABLE: "81.31", OUTPUT_VAT: "17.69"})

    # ------------------------------------------------------------ discount
    def test_t09_discount_in_bank_entry(self):
        self.set_regime("regular")
        invoice = self.invoice(
            "out_invoice", "2026-03-01", [("revenue_19", 1000, ["ust19"])]
        )
        self.bank(
            "2026-03-08",
            1166.20,
            matches=[(invoice, 1190)],
            extra=[
                ("discount_19", 20, self.base_line("ust19")),
                ("vat_out", 3.80, self.tax_line("ust19", "refund")),
            ],
        )
        self.assertFields(2026, {TAXABLE: 980, OUTPUT_VAT: "186.20"})

    def test_t09b_discount_in_payment(self):
        self.set_regime("regular")
        term = self.env["account.payment.term"].create(
            {
                "name": "2 % discount",
                "early_discount": True,
                "discount_percentage": 2,
                "discount_days": 10,
                "early_pay_discount_computation": "included",
                "line_ids": [
                    Command.create(
                        {"value": "percent", "value_amount": 100, "nb_days": 30}
                    )
                ],
            }
        )
        invoice = self.invoice(
            "out_invoice",
            "2026-03-01",
            [("revenue_19", 1000, ["ust19"])],
            invoice_payment_term_id=term.id,
        )
        payment = self.pay(invoice, "2026-03-08")
        self.assertEqual(payment.amount, 1166.20)
        self.bank_for_payment("2026-03-10", payment)
        self.assertFields(2026, {TAXABLE: 980, OUTPUT_VAT: "186.20"})

    # ------------------------------------------------------------ write-off
    def test_t10_write_off(self):
        invoice = self.sale("2026-10-01", 400)
        write_off = self.entry(
            "2027-02-01",
            [("writeoff", 400), ("receivable", -400)],
            partner=self.customer,
        )
        self.reconcile(invoice, write_off)
        self.assertFields(2026, {})
        self.assertFields(2027, {})

    def test_t10b_partial_payment_and_write_off(self):
        invoice = self.sale("2026-10-01", 400)
        self.bank("2026-11-01", 150, matches=[(invoice, 150)])
        write_off = self.entry(
            "2027-02-01",
            [("writeoff", 250), ("receivable", -250)],
            partner=self.customer,
        )
        self.reconcile(invoice, write_off)
        self.assertFields(2026, {KU: 150})
        self.assertFields(2027, {})

    # ------------------------------------------------------------ currency
    def _usd(self):
        return self.setup_other_currency(
            "USD", rates=[("2020-01-01", 1.1), ("2026-04-01", 1.0 / 0.9)]
        )

    def test_t11_exchange_difference_entry(self):
        usd = self._usd()
        invoice = self.sale("2026-03-01", 1000, currency_id=usd.id)
        self.assertEqual(invoice.amount_total_signed, 909.09)
        self.bank("2026-04-01", 900, matches=[(invoice, 900, 1000)])
        self.assertTrue(
            self.env["account.partial.reconcile"].search(
                [("exchange_move_id", "!=", False)]
            )
        )
        self.assertFields(2026, {KU: "909.09", OTHER: "9.09"})

    def test_t11b_exchange_difference_in_bank_entry(self):
        usd = self._usd()
        invoice = self.sale("2026-03-01", 1000, currency_id=usd.id)
        self.bank(
            "2026-04-01",
            900,
            matches=[(invoice, 909.09, 1000)],
            extra=[("fx_loss", 9.09)],
        )
        self.assertFields(2026, {KU: "909.09", OTHER: "9.09"})

    def test_f01_exchange_difference_missing(self):
        usd = self._usd()
        invoice = self.sale("2026-03-01", 1000, currency_id=usd.id)
        bank = self.bank("2026-04-01", 900, rest="receivable", partner=self.customer)
        line = bank.move_id.line_ids.filtered(
            lambda line: line.account_id == self.receivable
        )
        # the bank item in the currency of the invoice, reconciled without
        # exchange difference entry
        bank.move_id.button_draft()
        line.with_context(skip_account_move_synchronization=True).write(
            {"currency_id": usd.id, "amount_currency": -1000}
        )
        bank.move_id.with_context(skip_account_move_synchronization=True).action_post()
        (line | self.open_line(invoice)).with_context(
            no_exchange_difference=True
        ).reconcile()
        self.compute(2026)
        self.assertIn("C-19", self.codes(2026, "blocker"))

    # ------------------------------------------------------------ direct bank
    def test_t12_bank_fees_and_withdrawal(self):
        self.bank("2026-06-30", -9.90, extra=[("bank_fees", 9.90)])
        self.bank("2026-07-01", -500, extra=[("private_w", 500)])
        self.assertFields(2026, {OTHER: "9.90", WITHDRAWAL: 500})

    def test_t13_fixed_asset_purchase(self):
        bill = self.purchase("2026-07-15", 2150, account="asset")
        self.bank("2026-07-20", -2150, matches=[(bill, 2150)])
        self.entry("2026-12-31", [("depreciation", 153.57), ("asset", -153.57)])
        # acquisition is neutral, depreciation comes from the asset register
        self.assertFields(2026, {})
        self.assertEqual(self.codes(2026, "blocker"), [])

    def test_t15_paid_privately(self):
        bill = self.purchase("2026-03-01", 50)
        private = self.entry(
            "2026-03-01",
            [(self.open_line(bill).account_id, 50), ("private_c", -50)],
            partner=self.supplier,
        )
        self.reconcile(bill, private)
        self.assertFields(2026, {self.office_field(): 50, CONTRIBUTION: 50})

    def test_t16_offsetting(self):
        invoice = self.sale("2026-05-01", 500)
        bill = self.purchase("2026-05-03", 200, partner=self.customer)
        offset = self.entry(
            "2026-05-10",
            [
                (self.open_line(bill).account_id, 200),
                (self.open_line(invoice).account_id, -200),
            ],
            partner=self.customer,
        )
        self.reconcile(bill, offset.line_ids.filtered(lambda line: line.debit))
        self.reconcile(invoice, offset.line_ids.filtered(lambda line: line.credit))
        self.bank("2026-05-20", 300, matches=[(invoice, 300)])
        self.assertFields(2026, {KU: 500, self.office_field(): 200})

    def test_t17_unclear_statement_line(self):
        self.bank("2026-12-31", 250)
        self.compute(2026)
        self.assertIn("C-10", self.codes(2026, "blocker"))

    # ------------------------------------------------------------ rests
    def test_t18_advance_payment_classified(self):
        bank = self.bank("2026-12-20", 300, rest="receivable", partner=self.customer)
        self.compute(2026)
        check = self.checks(2026, "C-20")
        self.assertEqual(check.severity, "blocker")
        rest_line = bank.move_id.line_ids.filtered(
            lambda line: line.account_id == self.receivable
        )
        self.assertEqual(check.res_ids, rest_line.ids)
        action = check.action_resolve()
        self.assertEqual(action["res_model"], "l10n_de_euer.residual.classification")
        self.env["l10n_de_euer.residual.classification"].create(
            {
                "company_id": self.company.id,
                "source_line_id": rest_line.id,
                "kind": "revenue",
                "category_id": self.env["l10n_de_euer.category"]
                .search([("code", "=", "REV_KU")])
                .id,
                "vat_regime": "ku",
            }
        )
        self.assertFields(2026, {KU: 300})
        self.assertNotIn("C-20", self.codes(2026))
        # the invoice of 2027 settles the rest: same cash date, its composition
        invoice = self.sale("2027-01-10", 300)
        self.reconcile(invoice, rest_line)
        self.assertFields(2026, {KU: 300})
        self.assertFields(2027, {})

    def test_t19_reversal_over_turn_of_year(self):
        move = self.entry(
            "2026-12-28",
            [("office", 100), ("bank", -100)],
            journal=self.bank_journal,
        )
        move._reverse_moves([{"date": "2027-01-03"}], cancel=True)
        self.assertFields(2026, {})
        self.assertFields(2027, {})
        # left out, but reported in both years (C-48)
        self.assertEqual(self.checks(2026, "C-48").severity, "warning")
        self.assertEqual(self.checks(2027, "C-48").severity, "warning")

    def test_t20_drafts_ignored(self):
        self.sale("2026-05-01", 500, post=False)
        bill = self.purchase("2026-05-01", 80)
        bill.button_cancel()
        self.assertFields(2026, {})
        self.assertIn("C-13", self.codes(2026))

    def test_t22_opening_items(self):
        customer_a = self.env["res.partner"].create({"name": "Customer A"})
        customer_b = self.env["res.partner"].create({"name": "Customer B"})
        opening = self.entry(
            "2027-01-01",
            [
                ("receivable", 120, {"partner_id": customer_a.id}),
                ("receivable", 39, {"partner_id": customer_b.id}),
                ("opening_debtors", -159),
                ("bank", 5000),
                ("opening", -5000),
            ],
        )
        item = opening.line_ids.filtered(lambda line: line.partner_id == customer_a)
        self.bank("2027-01-15", 120, matches=[(item, 120)])
        self.compute(2027)
        check = self.checks(2027, "C-35")
        self.assertEqual(check.severity, "blocker")
        self.assertEqual(check.res_ids, item.ids)
        self.env["l10n_de_euer.opening.item"].create(
            {
                "company_id": self.company.id,
                "move_line_id": item.id,
                "category_id": self.env["l10n_de_euer.category"]
                .search([("code", "=", "REV_KU")])
                .id,
                "vat_regime": "ku",
            }
        )
        # the opening bank balance is no receipt
        self.assertFields(2027, {KU: 120})
        self.assertNotIn("C-35", self.codes(2027))

    def test_t22_opening_item_rate_is_a_percentage(self):
        """The VAT rate of an opening item is a percentage below 100."""
        self.set_regime("regular")
        opening = self.entry(
            "2027-01-01",
            [
                ("receivable", 119, {"partner_id": self.customer.id}),
                ("opening_debtors", -119),
            ],
        )
        receivable = opening.line_ids.filtered(lambda line: line.balance == 119)
        category = self.env["l10n_de_euer.category"].search(
            [("code", "=", "REV_TAXABLE")]
        )
        with self.assertRaises(ValidationError):
            self.env["l10n_de_euer.opening.item"].create(
                {
                    "company_id": self.company.id,
                    "move_line_id": receivable.id,
                    "category_id": category.id,
                    "vat_regime": "regular",
                    "tax_rate": 100,
                }
            )

    def test_t22c_opening_items_regular(self):
        self.set_regime("regular")
        opening = self.entry(
            "2027-01-01",
            [
                ("receivable", 119, {"partner_id": self.customer.id}),
                ("opening_debtors", -119),
                ("payable", -238, {"partner_id": self.supplier.id}),
                ("opening_creditors", 238),
            ],
        )
        receivable = opening.line_ids.filtered(lambda line: line.balance == 119)
        payable = opening.line_ids.filtered(lambda line: line.balance == -238)
        Category = self.env["l10n_de_euer.category"]
        for line, code in (
            (receivable, "REV_TAXABLE"),
            (payable, "EXP_WORK_EQUIPMENT"),
        ):
            self.env["l10n_de_euer.opening.item"].create(
                {
                    "company_id": self.company.id,
                    "move_line_id": line.id,
                    "category_id": Category.search([("code", "=", code)]).id,
                    "vat_regime": "regular",
                    "tax_rate": 19,
                }
            )
        self.bank("2027-02-03", 119, matches=[(receivable, 119)])
        self.bank("2027-02-05", -238, matches=[(payable, 238)])
        self.assertFields(
            2027,
            {
                TAXABLE: 100,
                OUTPUT_VAT: 19,
                self.office_field(): 200,
                INPUT_VAT: 38,
            },
        )

    # ------------------------------------------------------------ VAT
    def test_t23_vat_payment_ten_day_rule(self):
        self.set_regime("regular")
        tax_office = self.tax_office()
        bank = self.bank(
            "2027-01-08", -300, extra=[("vat_payment", 300)], partner=tax_office
        )
        bank.move_id.l10n_de_euer_vat_period = "2026-12"
        self.assertFields(2026, {VAT_PAID: 300})
        self.assertFields(2027, {})
        item = self.env["l10n_de_euer.ten_day.item"].search(
            [("source_move_id", "=", bank.move_id.id)]
        )
        self.assertEqual(item.decision, "auto")
        self.assertTrue(item.applied)

    def test_t23_vat_payment_permanent_extension(self):
        self.set_regime("regular", permanent_extension=True)
        tax_office = self.tax_office()
        bank = self.bank(
            "2027-01-08", -300, extra=[("vat_payment", 300)], partner=tax_office
        )
        bank.move_id.l10n_de_euer_vat_period = "2026-12"
        self.assertFields(2026, {})
        self.assertFields(2027, {VAT_PAID: 300})

    def test_t24_transitory_item(self):
        self.bank("2026-03-01", 100, extra=[("transitory", -100)])
        self.assertFields(2026, {})

    def test_t25_money_in_transit(self):
        transfer = self.acc("transfer")
        bank = self.bank("2026-12-31", -200, extra=[(transfer, 200)])
        cash = self.bank(
            "2027-01-02", 200, extra=[(transfer, -200)], journal=self.cash_journal
        )
        lines = (bank.move_id | cash.move_id).line_ids.filtered(
            lambda line: line.account_id == transfer
        )
        lines.reconcile()
        self.assertFields(2026, {})
        self.assertFields(2027, {})
        self.assertEqual(self.codes(2026, "blocker"), [])

    # ------------------------------------------------------------ reconciliation
    def test_t29_keep_mode_cash_date(self):
        """OCA keep mode: the reconciliation entry has a later date (T29)."""
        suspense = self.bank_journal.suspense_account_id
        suspense.reconcile = True
        invoice = self.sale("2026-11-15", 500)
        statement = self.bank("2026-12-30", 500)
        keep = self.entry(
            "2027-01-05",
            [(suspense, 500), ("receivable", -500)],
            partner=self.customer,
        )
        (
            statement.move_id.line_ids.filtered(
                lambda line: line.account_id == suspense
            )
            | keep.line_ids.filtered(lambda line: line.account_id == suspense)
        ).reconcile()
        self.reconcile(invoice, keep.line_ids.filtered(lambda line: line.credit))
        self.assertFields(2026, {KU: 500})
        self.assertFields(2027, {})

    def test_t30_cash_date_override(self):
        bill = self.purchase("2026-12-01", 100)
        bank = self.bank("2027-01-02", -100, matches=[(bill, 100)])
        bank.move_id.write(
            {
                "l10n_de_euer_cash_date": "2026-12-30",
                "l10n_de_euer_cash_date_reason": "transfer_order",
            }
        )
        self.assertFields(2026, {self.office_field(): 100})
        self.assertFields(2027, {})
        info = bank.move_id.l10n_de_euer_info_id
        self.assertEqual(info.cash_date_reason, "transfer_order")

    def test_t31_small_business_invoice_paid_under_standard_taxation(self):
        self.set_regime("ku", date_to="2026-12-31")
        self.env["l10n_de_euer.vat.period"].create(
            {
                "company_id": self.company.id,
                "date_from": "2027-01-01",
                "regime": "regular",
                "vat_return_period": "monthly",
            }
        )
        invoice = self.sale("2026-12-15", 1000)
        self.bank("2027-02-03", 1000, matches=[(invoice, 1000)])
        self.assertFields(2027, {KU: 1000})
        self.assertEqual(invoice.l10n_de_euer_vat_regime, "ku")


@tagged("post_install_l10n", "post_install", "-at_install")
class TestCoreScenariosSkr03(CoreScenarios, AccountTestInvoicingCommon):
    allow_inherited_tests_method = True
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
class TestCoreScenariosSkr04(CoreScenarios, AccountTestInvoicingCommon):
    allow_inherited_tests_method = True
    CHART = "de_skr04"
    ACCOUNTS = SKR04_ACCOUNTS
    TAXES = SKR04_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr04")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_euer()
