# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Setup and takeover wizards (spec 7.4, B-08, C-30, C-35, G-05, R-18).

Setup: cash basis accounting with the header values, a VAT status period,
for small businesses the default accounts and taxes without VAT, the
private share account of the purchase journals, the journals of a private
account and the business entertainment; lock dates are only recommended,
never set (B-01). Takeover: the open items of the opening entries with
category, VAT treatment and rate, the turnover of the previous year in the
VAT status period, the link to the takeover of the asset register; only
managers. SKR03 and SKR04; synthetic cases (D6).
"""

from decimal import Decimal

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from .scenario_common import (
    SKR03_ACCOUNTS,
    SKR03_TAXES,
    SKR04_ACCOUNTS,
    SKR04_TAXES,
    ScenarioCommon,
)

KU = "EUER.20.111"
#: template accounts per chart: KU revenue, standard revenue, goods with
#: and without input tax, private withdrawals
CHART_ACCOUNTS = {
    "de_skr03": ("8195", "8400", "3400", "3200", "1800"),
    "de_skr04": ("4185", "4400", "5400", "5200", "2100"),
}


class SetupScenarios(ScenarioCommon):
    @classmethod
    def setup_wizard_company(cls):
        cls.company = cls.env.company
        cls.Wizard = cls.env["l10n_de_euer.setup.wizard"]
        cls.Takeover = cls.env["l10n_de_euer.opening.wizard"]
        cls.bookkeeper = new_test_user(
            cls.env,
            login="setup_bookkeeper",
            groups="account.group_account_user",
            company_id=cls.company.id,
            company_ids=[Command.set(cls.company.ids)],
        )

    def chart_accounts(self):
        return CHART_ACCOUNTS[self.CHART]

    def account_code(self, account):
        return account.with_company(self.company).code

    # ------------------------------------------------------------ setup
    def test_setup_small_business(self):
        """Small business: cash basis accounting, header values, a VAT status
        period, accounts and taxes without VAT for the company and its
        journals (C-30), the private share account (K-11), a journal of a
        private account (R-18); no lock date is set (B-01)."""
        ku, standard, goods_vat, goods, private = self.chart_accounts()
        bank = self.company_data["default_journal_bank"]
        sale_journal = self.company_data["default_journal_sale"]
        purchase_journal = self.company_data["default_journal_purchase"]
        purchase_journal.non_deductible_account_id = False
        self.assertFalse(self.company.l10n_de_euer_enabled)
        self.assertEqual(self.account_code(self.company.income_account_id), standard)
        wizard = self.Wizard.create({"company_id": self.company.id})
        self.assertEqual(wizard.chart_template, self.CHART)
        # the name of the chart template, not its code (E5 review)
        self.assertIn(self.CHART[-5:].upper(), wizard.chart_template_name)
        self.assertNotEqual(wizard.chart_template_name, self.CHART)
        self.assertTrue(wizard.create_vat_period)
        self.assertEqual(wizard.regime, "ku")
        self.assertTrue(wizard.switch_accounts)
        self.assertEqual(self.account_code(wizard.ku_income_account_id), ku)
        self.assertEqual(self.account_code(wizard.ku_expense_account_id), goods)
        self.assertEqual(self.account_code(wizard.private_share_account_id), private)
        self.assertIn(purchase_journal, wizard.purchase_journal_ids)
        self.assertIn(bank, wizard.money_journal_ids)
        self.assertFalse(wizard.private_journal_ids)
        self.assertIn("No lock date", wizard.lock_advice)
        wizard.write(
            {
                "business_type": "Fotografie",
                "legal_form": "Einzelunternehmen",
                "income_type": "2",
                "date_from": "2026-01-01",
                "prev_year_turnover": 12000.0,
                "entertainment_mode": "split",
                "private_journal_ids": [Command.set(bank.ids)],
            }
        )
        result = wizard.action_apply()
        self.assertEqual(result["params"]["type"], "success")
        company = self.company
        self.assertTrue(company.l10n_de_euer_enabled)
        self.assertEqual(company.l10n_de_euer_business_type, "Fotografie")
        self.assertEqual(company.l10n_de_euer_income_type, "2")
        self.assertEqual(company.l10n_de_euer_owner, "1")
        self.assertEqual(company.l10n_de_euer_entertainment_mode, "split")
        period = self.env["l10n_de_euer.vat.period"].search(
            [("company_id", "=", company.id)]
        )
        self.assertEqual(
            (period.regime, str(period.date_from), period.prev_year_turnover),
            ("ku", "2026-01-01", 12000.0),
        )
        self.assertEqual(self.account_code(company.income_account_id), ku)
        self.assertEqual(self.account_code(company.expense_account_id), goods)
        self.assertFalse(company.account_sale_tax_id)
        self.assertFalse(company.account_purchase_tax_id)
        self.assertEqual(self.account_code(sale_journal.default_account_id), ku)
        self.assertEqual(self.account_code(purchase_journal.default_account_id), goods)
        self.assertEqual(
            self.account_code(purchase_journal.non_deductible_account_id), private
        )
        self.assertTrue(bank.l10n_de_euer_private_account)
        self.assertFalse(company.fiscalyear_lock_date)
        self.assertFalse(company.hard_lock_date)
        self.assertNotEqual(goods_vat, goods)
        # an invoice on the default account is small business revenue
        self.setup_euer_journals()
        sale = self.env["account.move"].create(
            {
                "move_type": "out_invoice",
                "partner_id": self.customer.id,
                "invoice_date": "2026-03-10",
                "invoice_line_ids": [
                    Command.create({"name": "Fotos", "price_unit": 1000})
                ],
            }
        )
        sale.action_post()
        self.assertEqual(self.account_code(sale.invoice_line_ids.account_id), ku)
        self.bank("2026-03-20", 1000, matches=[(sale, 1000)], journal=bank)
        values = self.fields(2026)
        self.assertEqual(values[KU], 1000)
        # received on a private account: also a withdrawal (R-18)
        self.assertEqual(values["EUER.29.122"], 1000)
        self.assertNotIn("C-30", self.codes(2026))

    def test_setup_regular_and_existing_period(self):
        """Standard taxation keeps the accounts with VAT; a second run keeps
        the existing VAT status period and refuses an overlapping one; a
        journal no longer marked is not a private account any more."""
        _ku, standard, *_rest = self.chart_accounts()
        bank = self.company_data["default_journal_bank"]
        bank.l10n_de_euer_private_account = True
        wizard = self.Wizard.create(
            {"company_id": self.company.id, "regime": "regular"}
        )
        self.assertFalse(wizard.switch_accounts)
        self.assertEqual(wizard.private_journal_ids, bank)
        wizard.write(
            {
                "date_from": "2026-01-01",
                "private_journal_ids": [Command.clear()],
                "business_type": "Beratung",
            }
        )
        wizard.action_apply()
        self.assertEqual(self.account_code(self.company.income_account_id), standard)
        self.assertTrue(self.company.account_sale_tax_id)
        self.assertFalse(bank.l10n_de_euer_private_account)
        period = self.env["l10n_de_euer.vat.period"].search(
            [("company_id", "=", self.company.id)]
        )
        self.assertEqual(period.regime, "regular")
        again = self.Wizard.create({"company_id": self.company.id})
        self.assertFalse(again.create_vat_period)
        self.assertEqual(again.vat_period_count, 1)
        self.assertEqual(again.business_type, "Beratung")
        again.action_apply()
        self.assertEqual(
            self.env["l10n_de_euer.vat.period"].search_count(
                [("company_id", "=", self.company.id)]
            ),
            1,
        )
        again.write({"create_vat_period": True, "date_from": "2027-01-01"})
        with self.assertRaises(UserError):
            again.action_apply()
        with self.assertRaises(UserError):
            again.write({"regime": False, "date_from": False})
            again._apply_vat_period()

    def test_setup_keeps_standard_taxation(self):
        """Regression (E5 review of the user interface): run again for a
        business under standard taxation, the setup proposes standard
        taxation from its VAT status period and keeps the accounts and taxes
        with VAT; it proposed small business before and switched them."""
        _ku, standard, *_rest = self.chart_accounts()
        Period = self.env["l10n_de_euer.vat.period"]
        Period.create(
            {
                "company_id": self.company.id,
                "date_from": "2019-01-01",
                "date_to": "2020-12-31",
                "regime": "ku",
                "vat_return_period": "annual",
            }
        )
        Period.create(
            {
                "company_id": self.company.id,
                "date_from": "2021-01-01",
                "regime": "regular",
                "vat_return_period": "monthly",
            }
        )
        wizard = self.Wizard.create({"company_id": self.company.id})
        self.assertFalse(wizard.create_vat_period)
        self.assertEqual(wizard.regime, "regular")
        self.assertFalse(wizard.switch_accounts)
        wizard.action_apply()
        self.assertEqual(self.account_code(self.company.income_account_id), standard)
        self.assertTrue(self.company.account_sale_tax_id)
        self.assertTrue(self.company.account_purchase_tax_id)
        # without a period valid today the latest one counts
        Period.search([("company_id", "=", self.company.id)]).unlink()
        Period.create(
            {
                "company_id": self.company.id,
                "date_from": "2019-01-01",
                "date_to": "2020-12-31",
                "regime": "regular",
                "vat_return_period": "monthly",
            }
        )
        self.assertEqual(
            self.Wizard.create({"company_id": self.company.id}).regime, "regular"
        )
        # a new business without a period is proposed as small business
        Period.search([("company_id", "=", self.company.id)]).unlink()
        self.assertEqual(
            self.Wizard.create({"company_id": self.company.id}).regime, "ku"
        )

    def test_setup_lock_date_advice(self):
        """G-05: the wizard recommends the fiscal year lock date; with only
        the hard lock date it points to C-70; it never sets one."""
        self.company.hard_lock_date = "2025-12-31"
        wizard = self.Wizard.create({"company_id": self.company.id})
        self.assertIn("hard lock date", wizard.lock_advice)
        self.company.fiscalyear_lock_date = "2025-12-31"
        wizard.invalidate_recordset(["lock_advice"])
        self.assertIn("fiscal year lock date is set", wizard.lock_advice)
        wizard.write({"date_from": "2026-01-01"})
        wizard.action_apply()
        self.assertEqual(str(self.company.fiscalyear_lock_date), "2025-12-31")
        self.assertEqual(str(self.company.hard_lock_date), "2025-12-31")

    def test_setup_links_and_rights(self):
        """Only managers set up a business (spec 7.10); the wizard leads to
        the check of the account assignment and to the takeover."""
        wizard = self.Wizard.create({"company_id": self.company.id})
        review = wizard.action_open_account_review()
        self.assertEqual(review["res_model"], "l10n_de_euer.account.review")
        takeover = wizard.action_open_takeover()
        self.assertEqual(takeover["res_model"], "l10n_de_euer.opening.wizard")
        self.assertEqual(takeover["context"]["default_company_id"], self.company.id)
        with self.assertRaises(AccessError):
            self.Wizard.with_user(self.bookkeeper).create(
                {"company_id": self.company.id}
            )
        with self.assertRaises(UserError):
            wizard.with_user(self.bookkeeper).action_apply()

    # ------------------------------------------------------------ takeover
    def opening_entry(self):
        customer = self.env["res.partner"].create({"name": "Customer A"})
        supplier = self.env["res.partner"].create({"name": "Supplier B"})
        opening = self.entry(
            "2026-01-01",
            [
                ("receivable", 120, {"partner_id": customer.id}),
                ("opening_debtors", -120),
                ("payable", -39, {"partner_id": supplier.id}),
                ("opening_creditors", 39),
                ("transfer", 50),
                ("bank", 5000),
                ("opening", -5050),
            ],
        )
        receivable = opening.line_ids.filtered(lambda line: line.partner_id == customer)
        payable = opening.line_ids.filtered(lambda line: line.partner_id == supplier)
        return opening, receivable, payable

    def test_takeover_open_items(self):
        """B-08, C-35: the wizard lists the open receivable and payable of
        the opening entry (not the money in transit nor the bank balance),
        asks for category, VAT treatment and rate and stores them; the
        payment of a taken over item counts with its category."""
        self.setup_euer()
        opening, receivable, payable = self.opening_entry()
        self.bank("2026-02-01", 120, matches=[(receivable, 120)])
        self.compute(2026)
        self.assertEqual(self.checks(2026, "C-35").severity, "blocker")
        wizard = self.Takeover.create({"company_id": self.company.id})
        action = wizard.action_load()
        # the dialog keeps the title of the menu (E5 review)
        self.assertEqual(
            action["name"],
            self.env.ref("l10n_de_euer.action_l10n_de_euer_opening_wizard").name,
        )
        self.assertEqual(wizard.move_count, 1)
        self.assertEqual(wizard.line_ids.move_line_id, receivable | payable)
        self.assertEqual(wizard.missing_count, 2)
        self.assertEqual(set(wizard.line_ids.mapped("vat_regime")), {"ku"})
        self.assertEqual(set(wizard.line_ids.mapped("tax_rate")), {0.0})
        self.assertEqual(wizard.line_ids.move_id, opening)
        revenue = self.category("REV_KU")
        expense = self.category("EXP_WORK_EQUIPMENT")
        line_r = wizard.line_ids.filtered(lambda line: line.move_line_id == receivable)
        line_r.write({"category_id": revenue.id, "origin_reference": "RE-2025-17"})
        result = wizard.action_apply()
        self.assertEqual(result["params"]["type"], "warning")
        self.assertIn("C-35", result["params"]["message"])
        items = self.env["l10n_de_euer.opening.item"].search(
            [("company_id", "=", self.company.id)]
        )
        self.assertEqual(items.move_line_id, receivable)
        self.assertEqual(
            (items.category_id, items.vat_regime, items.origin_reference),
            (revenue, "ku", "RE-2025-17"),
        )
        self.assertFields(2026, {KU: 120})
        self.assertNotIn("C-35", self.codes(2026))
        # read again: the stored takeover is shown and can be completed
        again = self.Takeover.create({"company_id": self.company.id})
        again.action_load()
        shown = again.line_ids.filtered(lambda line: line.move_line_id == receivable)
        self.assertEqual(shown.item_id, items)
        self.assertEqual(shown.category_id, revenue)
        again.line_ids.filtered(lambda line: not line.category_id).category_id = expense
        result = again.action_apply()
        self.assertEqual(result["params"]["type"], "success")
        self.assertEqual(
            self.env["l10n_de_euer.opening.item"].search_count(
                [("company_id", "=", self.company.id)]
            ),
            2,
        )

    def test_takeover_regular_rate_and_turnover(self):
        """Standard taxation: the rate of the original invoice is proposed
        (19 %); the turnover of the previous year goes into the VAT status
        period (§ 19 UStG thresholds); the asset register is taken over by
        its own wizard; only managers take over."""
        self.setup_euer()
        self.set_regime("regular")
        _opening, receivable, _payable = self.opening_entry()
        wizard = self.Takeover.create({"company_id": self.company.id})
        self.assertTrue(wizard.vat_period_id)
        wizard.action_load()
        self.assertEqual(set(wizard.line_ids.mapped("vat_regime")), {"regular"})
        self.assertEqual(set(wizard.line_ids.mapped("tax_rate")), {19.0})
        wizard.prev_year_turnover = 18000.0
        wizard.line_ids.filtered(
            lambda line: line.move_line_id == receivable
        ).category_id = self.category("REV_TAXABLE")
        wizard.action_apply()
        self.assertEqual(wizard.vat_period_id.prev_year_turnover, 18000.0)
        item = self.env["l10n_de_euer.opening.item"].search(
            [("move_line_id", "=", receivable.id)]
        )
        self.assertEqual(item.tax_rate, 19.0)
        self.bank("2026-02-03", 120, matches=[(receivable, 120)])
        values = self.fields(2026)
        # 120.00 gross with 19 %: 100.84 revenue, 19.16 VAT (B-08)
        self.assertEqual(values["EUER.20.112"], Decimal("100.84"))
        self.assertEqual(values["EUER.20.140"], Decimal("19.16"))
        action = wizard.action_open_asset_import()
        self.assertEqual(action["res_model"], "l10n_de_euer.asset.import")
        with self.assertRaises(AccessError):
            self.Takeover.with_user(self.bookkeeper).create(
                {"company_id": self.company.id}
            )
        with self.assertRaises(UserError):
            wizard.with_user(self.bookkeeper).action_load()

    def setup_euer_journals(self):
        """Helpers of the scenarios after the setup by the wizard."""
        self.bank_journal = self.company_data["default_journal_bank"]
        self.cash_journal = self.company_data["default_journal_cash"]
        self.misc_journal = self.company_data["default_journal_misc"]
        self.customer = self.partner_a
        self.supplier = self.partner_b
        self.receivable = self.customer.with_company(
            self.company
        ).property_account_receivable_id
        self.payable = self.supplier.with_company(
            self.company
        ).property_account_payable_id
        self.Run = self.env["l10n_de_euer.run"]


@tagged("post_install_l10n", "post_install", "-at_install")
class TestSetupWizardSkr03(SetupScenarios, AccountTestInvoicingCommon):
    allow_inherited_tests_method = True
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_wizard_company()


@tagged("post_install_l10n", "post_install", "-at_install")
class TestSetupWizardSkr04(SetupScenarios, AccountTestInvoicingCommon):
    allow_inherited_tests_method = True
    CHART = "de_skr04"
    ACCOUNTS = SKR04_ACCOUNTS
    TAXES = SKR04_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr04")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_wizard_company()
