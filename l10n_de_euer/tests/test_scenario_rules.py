# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Additional scenarios of spec 9.5 with real bookings in SKR03.

ST-G (credit notes), ST-K (small business), ST-U (VAT), ST-N (gifts and
entertainment), ST-E (private), ST-B (bookkeeping variants), ST-M (manual),
ST-D (entries without money and open item), ST-R (returned direct debits),
ST-T21 (branches) and the review list of the turn of the year (UT-10T-10 on
the ORM). All amounts are synthetic (D6).
"""

from odoo import Command
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from .scenario_common import SKR03_ACCOUNTS, SKR03_TAXES, ScenarioCommon, euro

KU = "EUER.20.111"
KU_DAVON = "EUER.20.119"
TAXABLE = "EUER.20.112"
EXEMPT = "EUER.20.103"
ASSET_SALE = "EUER.20.102"
PRIVATE_CAR = "EUER.20.106"
OUTPUT_VAT = "EUER.20.140"
REFUND = "EUER.20.141"
GOODS = "EUER.25.100"
INPUT_VAT = "EUER.25.185"
VAT_PAID = "EUER.25.186"
OTHER = "EUER.25.183"
GIFT = "EUER.25.174"
GIFT_ND = "EUER.25.164"
ENTERTAINMENT = "EUER.25.175"
ENTERTAINMENT_ND = "EUER.25.165"
INTEREST = "EUER.25.234"
WITHDRAWAL = "EUER.29.122"
CONTRIBUTION = "EUER.29.123"


class RuleScenarios(ScenarioCommon):
    @property
    def office(self):
        return self.field_of("EXP_WORK_EQUIPMENT")

    # ================================================================ ST-G
    def test_g1_full_credit_note_and_refund_next_year(self):
        self.set_regime("regular")
        invoice = self.invoice(
            "out_invoice", "2026-05-01", [("revenue_19", 1000, ["ust19"])]
        )
        self.bank("2026-05-10", 1190, matches=[(invoice, 1190)])
        credit = self.invoice(
            "out_refund", "2027-02-01", [("revenue_19", 1000, ["ust19"])]
        )
        self.bank("2027-02-05", -1190, matches=[(credit, 1190)])
        self.assertFields(2026, {TAXABLE: 1000, OUTPUT_VAT: 190})
        # negative income of the year moves to Kz 183 (R-12)
        self.assertFields(2027, {OTHER: 1190})

    # ================================================================ ST-K
    def test_k02_change_to_small_business(self):
        self.set_regime("regular", date_to="2026-12-31")
        self.env["l10n_de_euer.vat.period"].create(
            {"company_id": self.company.id, "date_from": "2027-01-01", "regime": "ku"}
        )
        invoice = self.invoice(
            "out_invoice", "2026-12-01", [("revenue_19", 1000, ["ust19"])]
        )
        self.bank("2027-01-15", 1190, matches=[(invoice, 1190)])
        self.assertFields(2027, {TAXABLE: 1000, OUTPUT_VAT: 190})

    def test_k03_threshold_exceeded(self):
        self.set_regime("ku", date_to="2026-06-14")
        self.env["l10n_de_euer.vat.period"].create(
            {
                "company_id": self.company.id,
                "date_from": "2026-06-15",
                "regime": "regular",
                "basis": "receipt_date",
                "vat_return_period": "monthly",
            }
        )
        first = self.sale("2026-02-01", 95000)
        self.bank("2026-03-01", 95000, matches=[(first, 95000)])
        second = self.sale("2026-06-01", 11900)
        self.bank("2026-06-15", 11900, matches=[(second, 11900)])
        self.compute(2026)
        self.assertIn("C-32", self.codes(2026, "blocker"))
        check = self.checks(2026, "C-32").filtered(lambda c: c.severity == "blocker")
        action = check.action_resolve()
        self.assertEqual(action["res_model"], "l10n_de_euer.move.info")
        second.l10n_de_euer_regular_tax_rate = "19"
        self.assertFields(2026, {KU: 95000, TAXABLE: 10000, OUTPUT_VAT: 1900})
        self.assertNotIn("C-32", self.codes(2026, "blocker"))

    def test_k04_date_of_supply_decides(self):
        self.set_regime("ku", date_to="2026-12-31")
        self.env["l10n_de_euer.vat.period"].create(
            {
                "company_id": self.company.id,
                "date_from": "2027-01-01",
                "regime": "regular",
            }
        )
        invoice = self.sale("2027-01-05", 1000, delivery_date="2026-12-20")
        self.bank("2027-02-01", 1000, matches=[(invoice, 1000)])
        self.assertFields(2027, {KU: 1000})

    def test_k05_reverse_charge_small_business(self):
        bill = self.purchase("2026-04-01", 100, taxes=("igE19",))
        self.bank("2026-04-10", -100, matches=[(bill, 100)])
        tax_office = self.tax_office()
        self.bank("2026-05-10", -19, extra=[("vat_payment", 19)], partner=tax_office)
        self.assertFields(2026, {self.office: 100, VAT_PAID: 19})

    def test_k06_k08_small_business_income_types(self):
        interest = self.entry(
            "2026-03-31",
            [("bank", 8.50), ("interest_income", -8.50)],
            journal=self.bank_journal,
        )
        self.assertTrue(interest)
        service = self.sale("2026-04-01", 300, account="exempt_revenue")
        self.bank("2026-04-15", 300, matches=[(service, 300)])
        asset = self.sale("2026-05-01", 400, account="asset_sale")
        self.bank("2026-05-10", 400, matches=[(asset, 400)])
        self.assertFields(2026, {KU: "308.50", KU_DAVON: "308.50", ASSET_SALE: 400})

    def test_k09_small_business_with_vat_shown(self):
        invoice = self.invoice(
            "out_invoice", "2026-04-01", [("revenue_19", 1000, ["ust19"])]
        )
        self.bank("2026-04-10", 1190, matches=[(invoice, 1190)])
        tax_office = self.tax_office()
        self.bank("2026-05-10", -190, extra=[("vat_payment", 190)], partner=tax_office)
        self.assertFields(2026, {KU: 1190, VAT_PAID: 190})

    def test_k10_small_business_input_vat_gross(self):
        bill = self.invoice(
            "in_invoice",
            "2026-04-01",
            [
                (self.template_account("account_3400"), 100, ["vst19"]),
                ("asset", 1000, ["vst19"]),
            ],
        )
        self.bank("2026-04-10", -1309, matches=[(bill, 1309)])
        self.assertFields(2026, {GOODS: 119})

    # ================================================================ ST-U
    def test_u02_supplier_payment_regular(self):
        self.set_regime("regular")
        bill = self.purchase("2026-04-01", 100, taxes=("vst19",))
        self.bank("2026-04-10", -119, matches=[(bill, 119)])
        self.assertFields(2026, {self.office: 100, INPUT_VAT: 19})

    def test_u03_u04_vat_payment_and_refund_not_netted(self):
        self.set_regime("regular")
        tax_office = self.tax_office()
        self.bank("2026-05-10", -300, extra=[("vat_payment", 300)], partner=tax_office)
        self.bank("2026-06-10", 80, extra=[("vat_refund", -80)], partner=tax_office)
        self.assertFields(2026, {VAT_PAID: 300, REFUND: 80})

    def test_u05_direct_debit_of_the_tax_office(self):
        self.set_regime("regular")
        tax_office = self.tax_office()
        bank = self.bank(
            "2027-01-12", -300, extra=[("vat_payment", 300)], partner=tax_office
        )
        bank.move_id.l10n_de_euer_vat_period = "2026-12"
        self.compute(2027)
        self.assertIn("C-26", self.codes(2027, "blocker"))
        item = self.env["l10n_de_euer.ten_day.item"].search(
            [("source_move_id", "=", bank.move_id.id)]
        )
        self.assertEqual(item.kind, "direct_debit_tax")
        item.reason = "VAT return submitted on 8 January"
        item.action_apply()
        self.assertFields(2026, {VAT_PAID: 300})
        self.assertFields(2027, {})

    def test_u06_private_use_of_the_car(self):
        self.set_regime("regular")
        self.entry(
            "2026-12-31",
            [
                ("private_w", 119),
                ("private_car", -100, self.base_line("ust19")),
                ("vat_out", -19, self.tax_line("ust19")),
            ],
        )
        self.assertFields(2026, {PRIVATE_CAR: 100, OUTPUT_VAT: 19, WITHDRAWAL: 119})

    def test_u07_intra_community_acquisition_regular(self):
        self.set_regime("regular")
        bill = self.purchase("2026-04-01", 100, taxes=("igE19",))
        self.bank("2026-04-10", -100, matches=[(bill, 100)])
        self.assertFields(2026, {self.office: 100})

    def test_u08_input_vat_not_deductible(self):
        self.set_regime("regular")
        tax = self.env["account.tax"].create(
            {
                "name": "19 % not deductible",
                "amount": 19,
                "type_tax_use": "purchase",
                "invoice_repartition_line_ids": [
                    Command.create({"repartition_type": "base"}),
                    Command.create({"repartition_type": "tax"}),
                ],
                "refund_repartition_line_ids": [
                    Command.create({"repartition_type": "base"}),
                    Command.create({"repartition_type": "tax"}),
                ],
            }
        )
        bill = self.env["account.move"].create(
            {
                "move_type": "in_invoice",
                "partner_id": self.supplier.id,
                "invoice_date": "2026-04-01",
                "invoice_line_ids": [
                    Command.create(
                        {
                            "name": "Office",
                            "account_id": self.acc("office").id,
                            "price_unit": 100,
                            "tax_ids": [Command.set(tax.ids)],
                        }
                    )
                ],
            }
        )
        bill.action_post()
        self.bank("2026-04-10", -119, matches=[(bill, 119)])
        self.assertFields(2026, {self.office: 119})

    def test_u09_interest_and_late_fees(self):
        self.set_regime("regular")
        tax_office = self.tax_office()
        self.bank(
            "2026-05-10",
            -30,
            extra=[
                (self.template_account("account_2107"), 20),
                (self.template_account("account_2103"), 10),
            ],
            partner=tax_office,
        )
        self.assertFields(2026, {INTEREST: 20, OTHER: 10})

    def test_u10_cash_sale_with_vat_line(self):
        self.set_regime("regular")
        self.bank(
            "2026-05-10",
            119,
            extra=[
                ("revenue_19", -100, self.base_line("ust19")),
                ("vat_out", -19, self.tax_line("ust19")),
            ],
            journal=self.cash_journal,
        )
        self.assertFields(2026, {TAXABLE: 100, OUTPUT_VAT: 19})

    def test_u11_bank_expense_with_input_vat(self):
        self.set_regime("regular")
        self.bank(
            "2026-05-10",
            -119,
            extra=[
                ("office", 100, self.base_line("vst19")),
                ("vat_in", 19, self.tax_line("vst19")),
            ],
        )
        self.assertFields(2026, {self.office: 100, INPUT_VAT: 19})
        self.assertNotIn("C-34", self.codes(2026))

    def test_u11_manual_tax_line(self):
        self.set_regime("regular")
        self.bank("2026-05-10", -119, extra=[("office", 100), ("vat_in", 19)])
        self.assertFields(2026, {self.office: 100, INPUT_VAT: 19})
        self.assertIn("C-34", self.codes(2026))

    def test_u12_discount_received_with_vat_correction(self):
        self.set_regime("regular")
        bill = self.purchase("2026-03-01", 1000, taxes=("vst19",))
        self.bank(
            "2026-03-08",
            -1166.20,
            matches=[(bill, 1190)],
            extra=[
                ("discount_received_19", -20, self.base_line("vst19")),
                ("vat_in", -3.80, self.tax_line("vst19", "refund")),
            ],
        )
        self.assertFields(2026, {self.office: 1000, INPUT_VAT: "186.20", GOODS: -20})

    def test_u13_manual_vat_settlement(self):
        self.set_regime("regular")
        settlement = self.entry(
            "2026-02-10",
            [("vat_out", 500), ("vat_in", -200), ("vat_payment", -300)],
        )
        bank = self.bank(
            "2026-02-12",
            -300,
            matches=[
                (settlement.line_ids.filtered(lambda line: line.balance == -300), 300)
            ],
        )
        self.assertTrue(bank)
        self.bank("2026-03-12", 80, extra=[("vat_refund", -80)])
        self.assertFields(2026, {VAT_PAID: 300, REFUND: 80})

    # ================================================================ ST-N
    def gift(self, date, amount, recipient, taxes=(), supplier=None, **values):
        """Gift bought from a shop (the supplier); the recipient is entered
        in the EÜR information of the invoice (5.7 no. 7)."""
        bill = self.invoice(
            "in_invoice",
            date,
            [("gifts", amount, taxes)],
            partner=supplier or self.supplier,
            **values,
        )
        if recipient:
            bill.l10n_de_euer_gift_recipient_id = recipient
        total = bill.amount_total
        self.bank(date, -total, matches=[(bill, total)])
        return bill

    def test_n01_gifts_to_one_recipient_over_limit(self):
        """Two shops, one recipient: the gifts count together (I2)."""
        recipient = self.env["res.partner"].create({"name": "Recipient"})
        shop = self.env["res.partner"].create({"name": "Other Shop"})
        self.gift("2026-03-01", 30, recipient)
        self.gift("2026-06-01", 30, recipient, supplier=shop)
        self.assertFields(2026, {GIFT_ND: 60})

    def test_n02_gift_below_limit(self):
        """One shop, two recipients: each below the limit (I1a)."""
        first = self.env["res.partner"].create({"name": "Recipient 1"})
        second = self.env["res.partner"].create({"name": "Recipient 2"})
        self.gift("2026-03-01", 45, first)
        self.gift("2026-03-01", 40, second)
        self.assertFields(2026, {GIFT: 85})

    def test_n02b_gift_without_recipient(self):
        """The supplier of the invoice is no recipient: C-51 blocks."""
        self.gift("2026-03-01", 30, None)
        self.compute(2026)
        self.assertIn("C-51", self.codes(2026, "blocker"))

    def test_n03_gifts_regular_net_limit(self):
        self.set_regime("regular")
        first = self.env["res.partner"].create({"name": "Recipient 1"})
        second = self.env["res.partner"].create({"name": "Recipient 2"})
        self.gift("2026-03-01", 50, first, taxes=("vst19",))
        self.gift("2026-03-01", 55, second, taxes=("vst19",))
        self.assertFields(2026, {GIFT: 50, INPUT_VAT: "9.50", GIFT_ND: "65.45"})
        self.assertIn("C-52", self.codes(2026))

    def test_n06_give_aways_and_year_of_the_gift(self):
        bulk = self.invoice(
            "in_invoice",
            "2026-03-01",
            [("gifts", 2, (), 300)],
            partner=self.supplier,
        )
        bulk.l10n_de_euer_gift_bulk = True
        self.bank("2026-03-05", -600, matches=[(bulk, 600)])
        recipient = self.env["res.partner"].create({"name": "Recipient"})
        first = self.invoice(
            "in_invoice",
            "2026-12-20",
            [("gifts", 40)],
            delivery_date="2026-12-20",
        )
        first.l10n_de_euer_gift_recipient_id = recipient
        self.bank("2027-01-05", -40, matches=[(first, 40)])
        second = self.gift("2027-03-15", 20, recipient)
        self.assertTrue(second)
        self.assertFields(2026, {GIFT: 600})
        self.assertNotIn("C-51", self.codes(2026))
        self.assertFields(2027, {GIFT: 60})

    def test_n04_entertainment_separate_accounts(self):
        bill = self.invoice(
            "in_invoice",
            "2026-03-01",
            [("entertainment", 70), ("entertainment_nd", 30)],
        )
        self.bank("2026-03-05", -100, matches=[(bill, 100)])
        self.assertFields(2026, {ENTERTAINMENT: 70, ENTERTAINMENT_ND: 30})
        self.assertNotIn("C-53", self.codes(2026))

    def test_n05_entertainment_split(self):
        self.company.l10n_de_euer_entertainment_mode = "split"
        bill = self.invoice("in_invoice", "2026-03-01", [("entertainment", 100)])
        self.bank("2026-03-05", -100, matches=[(bill, 100)])
        self.assertFields(2026, {ENTERTAINMENT: 70, ENTERTAINMENT_ND: 30})

    def test_n05_entertainment_ratio_warning(self):
        bill = self.invoice("in_invoice", "2026-03-01", [("entertainment", 100)])
        self.bank("2026-03-05", -100, matches=[(bill, 100)])
        self.assertFields(2026, {ENTERTAINMENT: 100})
        self.assertIn("C-53", self.codes(2026))

    # ================================================================ ST-E
    def test_e01_e02_withdrawal_and_contribution(self):
        self.bank("2026-03-01", -500, extra=[("private_w", 500)])
        self.bank("2026-04-01", 200, extra=[("private_c", -200)])
        self.entry("2026-12-31", [("private_c", 100), ("private_w", -100)])
        self.assertFields(2026, {WITHDRAWAL: 500, CONTRIBUTION: 200})

    def private_journal(self):
        return self.env["account.journal"].create(
            {
                "name": "Private Account",
                "code": "PRIV",
                "type": "bank",
                "l10n_de_euer_private_account": True,
            }
        )

    def test_e04_e06_private_account_journal(self):
        journal = self.private_journal()
        invoice = self.sale("2026-03-01", 500)
        self.bank("2026-03-10", 500, matches=[(invoice, 500)], journal=journal)
        bill = self.purchase("2026-03-02", 200)
        self.bank("2026-03-12", -200, matches=[(bill, 200)], journal=journal)
        self.bank("2026-03-20", -80, extra=[("private_w", 80)], journal=journal)
        self.assertFields(
            2026,
            {KU: 500, self.office: 200, WITHDRAWAL: 500, CONTRIBUTION: 200},
        )

    def test_e06_private_purchase_on_business_account(self):
        self.bank("2026-03-20", -80, extra=[("private_w", 80)])
        self.assertFields(2026, {WITHDRAWAL: 80})

    # ================================================================ ST-B
    def test_b01_reconcilable_expense_account(self):
        self.acc("office").reconcile = True
        bill = self.purchase("2026-03-01", 100)
        self.bank("2026-03-05", -100, matches=[(bill, 100)])
        self.assertFields(2026, {self.office: 100})

    def test_b02_credit_card(self):
        card = self.env["account.journal"].create(
            {"name": "Card", "code": "CARD", "type": "credit"}
        )
        self.assertEqual(card.default_account_id.account_type, "liability_credit_card")
        transfer = self.acc("transfer")
        self.bank("2026-12-28", -50, extra=[("office", 50)], journal=card)
        settle_bank = self.bank("2027-01-15", -50, extra=[(transfer, 50)])
        settle_card = self.bank("2027-01-15", 50, extra=[(transfer, -50)], journal=card)
        (settle_bank.move_id | settle_card.move_id).line_ids.filtered(
            lambda line: line.account_id == transfer
        ).reconcile()
        self.assertFields(2026, {self.office: 50})
        self.assertFields(2027, {})

    def test_b03_payment_provider_journal_and_clearing(self):
        paypal = self.env["account.journal"].create(
            {"name": "PayPal", "code": "PP", "type": "bank"}
        )
        transfer = self.acc("transfer")
        invoice = self.sale("2026-11-01", 100)
        self.bank("2026-12-30", 100, matches=[(invoice, 100)], journal=paypal)
        payout_pp = self.bank(
            "2027-01-03", -100, extra=[(transfer, 100)], journal=paypal
        )
        payout = self.bank("2027-01-03", 100, extra=[(transfer, -100)])
        (payout_pp.move_id | payout.move_id).line_ids.filtered(
            lambda line: line.account_id == transfer
        ).reconcile()
        self.assertFields(2026, {KU: 100})
        self.assertFields(2027, {})
        # clearing account: receipt with the payout
        clearing = self.env["account.account"].create(
            {
                "code": "1361",
                "name": "PayPal Clearing",
                "account_type": "asset_current",
                "reconcile": True,
            }
        )
        self.env["l10n_de_euer.account.assignment"].create(
            {
                "company_id": self.company.id,
                "account_id": clearing.id,
                "category_id": self.category("TRANSIT").id,
            }
        )
        second = self.sale("2026-11-02", 70)
        received = self.entry(
            "2026-12-30",
            [(clearing, 70), ("receivable", -70)],
            partner=self.customer,
        )
        self.reconcile(second, received.line_ids.filtered(lambda line: line.credit))
        self.bank(
            "2027-01-03",
            70,
            matches=[(received.line_ids.filtered(lambda line: line.debit), 70)],
        )
        self.assertFields(2026, {KU: 100})
        self.assertFields(2027, {KU: 70})

    def test_b04_advance_invoice_and_final_invoice(self):
        advance = self.sale("2026-03-01", 300)
        self.bank("2026-03-10", 300, matches=[(advance, 300)])
        final = self.invoice(
            "out_invoice",
            "2026-06-01",
            [("ku_revenue", 1000), ("ku_revenue", -300)],
        )
        self.bank("2026-06-10", 700, matches=[(final, 700)])
        self.assertFields(2026, {KU: 1000})

    def test_b05_partial_with_draft(self):
        invoice = self.sale("2026-03-01", 300)
        draft = self.entry(
            "2026-03-10",
            [("bank", 300), ("receivable", -300)],
            partner=self.customer,
            journal=self.bank_journal,
            post=False,
        )
        self.reconcile(invoice, draft.line_ids.filtered(lambda line: line.credit))
        self.assertFields(2026, {})
        self.assertIn("C-14", self.codes(2026))

    def test_b06_instalments(self):
        term = self.env["account.payment.term"].create(
            {
                "name": "50/50",
                "line_ids": [
                    Command.create(
                        {"value": "percent", "value_amount": 50, "nb_days": 0}
                    ),
                    Command.create(
                        {"value": "percent", "value_amount": 50, "nb_days": 30}
                    ),
                ],
            }
        )
        invoice = self.invoice(
            "out_invoice",
            "2026-03-01",
            [("ku_revenue", 1000)],
            invoice_payment_term_id=term.id,
        )
        lines = self.open_lines(invoice).sorted("date_maturity")
        self.assertEqual(len(lines), 2)
        self.bank("2026-03-05", 500, matches=[(lines[0], 500)])
        self.assertFields(2026, {KU: 500})

    def test_b07_revaluation(self):
        usd = self.setup_other_currency("USD", rates=[("2020-01-01", 1.1)])
        self.entry(
            "2026-12-31",
            [
                (
                    "receivable",
                    10,
                    {"currency_id": usd.id, "amount_currency": 0},
                ),
                ("fx_gain", -10),
            ],
            journal=self.company.currency_exchange_journal_id,
            partner=self.customer,
        )
        self.assertFields(2026, {})
        self.assertIn("C-28", self.codes(2026))

    def test_b12_bank_date_differs(self):
        statement = self.bank("2027-01-02", -50, extra=[("bank_fees", 50)])
        statement.transaction_details = {"booking_date": "2026-12-30"}
        self.compute(2027)
        self.assertIn("C-12", self.codes(2027))

    def test_b13_private_share_with_payment(self):
        self.set_regime("regular")
        self.company_data[
            "default_journal_purchase"
        ].non_deductible_account_id = self.acc("private_w")
        bill = self.env["account.move"].create(
            {
                "move_type": "in_invoice",
                "partner_id": self.supplier.id,
                "invoice_date": "2026-12-10",
                "invoice_line_ids": [
                    Command.create(
                        {
                            "name": "Mixed use",
                            "account_id": self.acc("office").id,
                            "price_unit": 100,
                            "deductible_amount": 50,
                            "tax_ids": [Command.set(self.tax("vst19").ids)],
                        }
                    )
                ],
            }
        )
        bill.action_post()
        self.assertEqual(bill.amount_total, 119)
        self.bank("2026-12-15", -60, matches=[(bill, 60)])
        self.bank("2027-02-01", -59, matches=[(bill, 59)])
        totals = {2026: self.fields(2026), 2027: self.fields(2027)}
        self.assertEqual(
            {code: totals[2026][code] + totals[2027][code] for code in totals[2026]},
            {
                self.office: euro(50),
                INPUT_VAT: euro("9.50"),
                WITHDRAWAL: euro("59.50"),
            },
        )
        # the payment of 60 in 2026 realises 60/119 of every line
        self.assertEqual(totals[2026][self.office], euro("25.21"))

    def test_b13b_invoice_line_on_private_account(self):
        bill = self.invoice(
            "in_invoice",
            "2026-12-10",
            [("office", 100), ("private_w", 50)],
        )
        self.assertFields(2026, {})
        self.bank("2027-02-01", -150, matches=[(bill, 150)])
        self.assertFields(2027, {self.office: 100, WITHDRAWAL: 50})

    # ================================================================ ST-M
    def test_m01_cheque_cash_date(self):
        invoice = self.sale("2026-11-01", 200)
        bank = self.bank("2027-01-20", 200, matches=[(invoice, 200)])
        bank.move_id.write(
            {
                "l10n_de_euer_cash_date": "2026-12-29",
                "l10n_de_euer_cash_date_reason": "cheque",
            }
        )
        self.assertFields(2026, {KU: 200})
        slices = self.env["l10n_de_euer.cash.slice"].search(
            [("source_move_id", "=", bank.move_id.id)]
        )
        self.assertTrue(all(slices.mapped("override")))

    # ================================================================ ST-D
    def test_d01_booking_category(self):
        self.entry(
            "2026-06-30",
            [("wages", 50), ("non_cash_benefit", -50)],
        )
        fields = self.fields(2026)
        self.assertEqual(fields[self.field_of("EXP_PERSONNEL")], 50)
        self.assertEqual(fields[KU], 50)

    def test_d02_payroll_paid_next_year(self):
        self.entry("2026-12-31", [("wages", 2000), ("payroll", -2000)])
        self.bank("2027-01-05", -2000, extra=[("payroll", 2000)])
        self.assertFields(2026, {})
        self.assertIn("C-03", self.codes(2026))
        self.assertFields(2027, {self.field_of("EXP_PAYROLL_LIABILITY"): 2000})

    def test_d03_reclassification(self):
        self.entry("2026-06-30", [("leasing", 100), ("office", -100)])
        self.assertFields(2026, {self.field_of("EXP_LEASING"): 100, self.office: -100})

    def test_d04_depreciation_in_the_ledger(self):
        self.entry("2026-12-31", [("depreciation", 300), ("asset", -300)])
        self.assertFields(2026, {})
        self.assertEqual(self.codes(2026, "blocker"), [])

    def test_d05_expense_against_neutral_liability(self):
        self.set_regime("regular")
        self.entry(
            "2026-06-01",
            [("office", 100), ("vat_in", 19), ("liability", -119)],
        )
        self.bank("2026-06-20", -119, extra=[("liability", 119)])
        self.compute(2026)
        blockers = self.codes(2026, "blocker")
        self.assertIn("C-04", blockers)
        self.assertIn("C-18", blockers)

    def test_d06_provision(self):
        self.entry("2026-12-31", [("advice", 500), ("provision", -500)])
        self.compute(2026)
        self.assertIn("C-04", self.codes(2026, "blocker"))

    def test_d07_neutral_reclassification(self):
        self.previous_turnover(2026)
        self.entry("2026-06-30", [("liability", 1000), ("loan", -1000)])
        self.assertFields(2026, {})
        self.assertEqual(self.codes(2026), [])

    # ================================================================ ST-R
    def returned_debit(self, date, invoice_line_or_none, amount=119, fee=3):
        """Returned direct debit: the receivable is open again, fee booked."""
        extra = [("receivable", amount, {"partner_id": self.customer.id})]
        if fee:
            extra.append(("bank_fees", fee))
        return self.bank(date, -(amount + fee), extra=extra, partner=self.customer)

    def returned_line(self, statement):
        return statement.move_id.line_ids.filtered(
            lambda line: line.account_id == self.receivable
        )

    def test_r01_returned_debit_paid_again(self):
        invoice = self.sale("2026-03-01", 119)
        self.bank("2026-03-10", 119, matches=[(invoice, 119)])
        returned = self.returned_debit("2026-03-20", None)
        self.bank("2026-04-05", 119, matches=[(self.returned_line(returned), 119)])
        self.assertFields(2026, {KU: 119, OTHER: 3})

    def test_r02_returned_debit_written_off(self):
        invoice = self.sale("2026-03-01", 119)
        self.bank("2026-03-10", 119, matches=[(invoice, 119)])
        returned = self.returned_debit("2026-03-20", None)
        write_off = self.entry(
            "2026-06-30",
            [("writeoff", 119), ("receivable", -119)],
            partner=self.customer,
        )
        self.reconcile(
            self.returned_line(returned),
            write_off.line_ids.filtered(lambda line: line.credit),
        )
        self.compute(2026)
        check = self.checks(2026, "C-36")
        self.assertEqual(check.severity, "blocker")
        writeoff_line = write_off.line_ids.filtered(lambda line: line.debit)
        self.assertEqual(check.res_ids, writeoff_line.ids)
        self.env["l10n_de_euer.residual.classification"].create(
            {
                "company_id": self.company.id,
                "source_line_id": writeoff_line.id,
                "kind": "negative_revenue",
                "category_id": self.category("REV_KU").id,
                "vat_regime": "ku",
            }
        )
        self.assertFields(2026, {OTHER: 3})
        self.assertNotIn("C-36", self.codes(2026))

    def test_r03_returned_debit_in_next_year(self):
        invoice = self.sale("2026-11-01", 119)
        self.bank("2026-12-15", 119, matches=[(invoice, 119)])
        returned = self.returned_debit("2027-01-12", None, fee=0)
        self.bank("2027-02-10", 119, matches=[(self.returned_line(returned), 119)])
        self.assertFields(2026, {KU: 119})
        self.assertFields(2027, {})

    def test_r04_money_to_money_over_turn_of_year(self):
        invoice = self.sale("2026-11-01", 119)
        self.bank("2026-12-15", 119, matches=[(invoice, 119)])
        returned = self.returned_debit("2026-12-28", None, fee=0)
        again = self.bank(
            "2027-01-15", 119, matches=[(self.returned_line(returned), 119)]
        )
        self.compute(2026)
        self.assertIn("C-37", self.codes(2026, "blocker"))
        Classification = self.env["l10n_de_euer.residual.classification"]
        Classification.create(
            {
                "company_id": self.company.id,
                "source_line_id": self.returned_line(returned).id,
                "kind": "negative_revenue",
                "category_id": self.category("REV_KU").id,
                "vat_regime": "ku",
            }
        )
        Classification.create(
            {
                "company_id": self.company.id,
                "source_line_id": self.returned_line(again).id,
                "kind": "revenue",
                "category_id": self.category("REV_KU").id,
                "vat_regime": "ku",
            }
        )
        self.assertFields(2026, {})
        self.assertNotIn("C-37", self.codes(2026))
        self.assertFields(2027, {KU: 119})

    # ================================================================ ten-day
    def test_ten_day_monthly_rent_without_flags(self):
        """UT-10T-10 with bookings: candidate from the data, decision needed."""
        landlord = self.env["res.partner"].create({"name": "Landlord"})
        for month in range(1, 13):
            bill = self.invoice(
                "in_invoice",
                f"2026-{month:02d}-01",
                [("rent", 800)],
                partner=landlord,
            )
            if month < 12:
                self.bank(f"2026-{month:02d}-03", -800, matches=[(bill, 800)])
        december = bill
        december.invoice_date_due = "2027-01-01"
        self.bank("2027-01-05", -800, matches=[(december, 800)])
        rent = self.field_of("EXP_RENT")
        self.compute(2026)
        self.assertIn("C-26", self.codes(2026, "blocker"))
        item = self.env["l10n_de_euer.ten_day.item"].search(
            [("origin_move_id", "=", december.id)]
        )
        self.assertEqual(item.kind, "recurring")
        self.assertIn("document of the other year", item.candidate_reason)
        self.assertIn("repeated similar payments", item.candidate_reason)
        item.reason = "Rent of December"
        item.action_apply()
        self.assertEqual(self.fields(2026), {rent: 9600})
        self.assertEqual(self.fields(2027), {})


@tagged("post_install_l10n", "post_install", "-at_install")
class TestRuleScenariosSkr03(RuleScenarios, AccountTestInvoicingCommon):
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
