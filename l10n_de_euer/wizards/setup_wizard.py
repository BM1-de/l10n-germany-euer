# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Setup of a business for the EÜR (spec 7.4).

The wizard collects what a business needs before its first calculation:
cash basis accounting enabled with the header values of the form, a VAT
status period, for small businesses the default accounts and taxes without
VAT (C-30), the private share account of the purchase journals (K-11), the
money journals that are private accounts (R-18) and the treatment of
business entertainment. It shows the lock dates with the recommendation of
G-05 but never sets a lock date (B-01).
"""

import datetime

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain

#: default accounts of the chart templates: standard, small business
TEMPLATE_ACCOUNTS = {
    "de_skr03": {
        "income": ("account_8400", "account_8195"),
        "expense": ("account_3400", "account_3200"),
        "private": "account_1800",
    },
    "de_skr04": {
        "income": ("chart_skr04_4400", "chart_skr04_4185"),
        "expense": ("chart_skr04_5400", "chart_skr04_5200"),
        "private": "chart_skr04_2100",
    },
}
MONEY_JOURNAL_TYPES = ("bank", "cash", "credit")


class L10nDeEuerSetupWizard(models.TransientModel):
    _name = "l10n_de_euer.setup.wizard"
    _description = "EÜR Setup Wizard"

    company_id = fields.Many2one(
        "res.company",
        string="Business",
        required=True,
        default=lambda self: self.env.company.root_id,
        domain="[('parent_id', '=', False)]",
        help="Main company of the business; its branches belong to it.",
    )
    chart_template = fields.Char(
        string="Chart Template Code", compute="_compute_company_values"
    )
    chart_template_name = fields.Char(
        string="Chart Template", compute="_compute_company_values"
    )
    # step 1: cash basis accounting and header values (F-08)
    enable = fields.Boolean(
        string="Cash Basis Accounting (EÜR)",
        default=True,
        help="The profit is determined by cash basis accounting (§ 4 (3) EStG).",
    )
    business_type = fields.Char(
        string="Type of Business",
        compute="_compute_business_type",
        store=True,
        readonly=False,
        help="Header of the Anlage EÜR, e.g. photographer.",
    )
    legal_form = fields.Char(
        compute="_compute_legal_form",
        store=True,
        readonly=False,
        help="Header of the Anlage EÜR, e.g. sole proprietorship.",
    )
    income_type = fields.Selection(
        [
            ("1", "1 Agriculture and Forestry"),
            ("2", "2 Trade or Business"),
            ("3", "3 Self-Employment"),
        ],
        string="Type of Income (Kz 103)",
        compute="_compute_income_type",
        store=True,
        readonly=False,
    )
    owner = fields.Selection(
        [
            ("1", "1 Taxpayer / Person A"),
            ("2", "2 Spouse / Person B"),
            ("3", "3 Both"),
        ],
        string="Business Owner (Kz 104)",
        compute="_compute_owner",
        store=True,
        readonly=False,
    )
    entertainment_mode = fields.Selection(
        [
            ("separate", "Separate Accounts (70 % and 30 % Booked Separately)"),
            ("split", "Split 70/30 Automatically"),
        ],
        string="Business Entertainment",
        compute="_compute_entertainment_mode",
        store=True,
        readonly=False,
    )
    # step 2: VAT status period
    vat_period_count = fields.Integer(compute="_compute_company_values")
    create_vat_period = fields.Boolean(
        string="Create VAT Status Period",
        compute="_compute_create_vat_period",
        store=True,
        readonly=False,
        help="Without a VAT status period every year is computed as standard "
        "taxation: each small business invoice then needs a VAT rate (C-33).",
    )
    regime = fields.Selection(
        [("ku", "Small Business (§ 19 UStG)"), ("regular", "Standard Taxation")],
        string="VAT Treatment",
        compute="_compute_regime",
        store=True,
        readonly=False,
        help="Proposed from the VAT status period of the business valid today "
        "(else its latest one), small business without a period. A small "
        "business gets the accounts without VAT (tab Accounts).",
    )
    date_from = fields.Date(
        string="Valid From",
        default=lambda self: datetime.date(fields.Date.context_today(self).year, 1, 1),
    )
    vat_return_period = fields.Selection(
        [("monthly", "Monthly"), ("quarterly", "Quarterly"), ("annual", "Annual")],
        string="VAT Return Period",
        default="annual",
    )
    permanent_extension = fields.Boolean(
        help="Dauerfristverlängerung: VAT returns are due one month later."
    )
    currency_id = fields.Many2one(related="company_id.currency_id")
    prev_year_turnover = fields.Monetary(
        string="Turnover of the Previous Year",
        help="Total turnover of the year before the first year in Odoo (small "
        "business thresholds); needed in the year of a takeover from another "
        "system.",
    )
    prev_year_turnover_year = fields.Integer(
        string="For the Year",
        compute="_compute_prev_year_turnover_year",
        store=True,
        readonly=False,
        help="First year in Odoo: its small business check takes the turnover "
        "entered as that of the previous year. Proposed from the start of the "
        "period.",
    )
    activity_start = fields.Date(
        string="Start of the Activity",
        compute="_compute_activity_start",
        store=True,
        readonly=False,
        help="Day the entrepreneurial activity of the owner started as a whole, "
        "including other businesses outside Odoo (§ 2 (1) sentence 2 UStG). In "
        "the year of the start the small business limit of the current year is "
        "25,000 € instead of 100,000 € (§ 19 (1) UStG from 2025) and there is no "
        "previous year.",
    )
    # step 3: chart of accounts for small businesses (C-30)
    income_account_id = fields.Many2one(
        "account.account",
        string="Current Income Account",
        compute="_compute_current_accounts",
    )
    ku_income_account_id = fields.Many2one(
        "account.account",
        string="Income Account for Small Businesses",
        compute="_compute_accounts",
        store=True,
        readonly=False,
    )
    expense_account_id = fields.Many2one(
        "account.account",
        string="Current Expense Account",
        compute="_compute_current_accounts",
    )
    ku_expense_account_id = fields.Many2one(
        "account.account",
        string="Expense Account without Input Tax",
        compute="_compute_accounts",
        store=True,
        readonly=False,
    )
    switch_accounts = fields.Boolean(
        string="Use Accounts without VAT",
        compute="_compute_accounts",
        store=True,
        readonly=False,
        help="Small businesses: the default income and expense accounts of the "
        "company and of the sales and purchase journals become the accounts "
        "without VAT (e.g. 8195 instead of 8400, 3200 instead of 3400), and "
        "the default taxes are removed. Otherwise the checks report every "
        "booking with VAT (C-30).",
    )
    sale_tax_id = fields.Many2one(related="company_id.account_sale_tax_id")
    purchase_tax_id = fields.Many2one(related="company_id.account_purchase_tax_id")
    # step 4: private share account of the purchase journals (K-11)
    private_share_account_id = fields.Many2one(
        "account.account",
        compute="_compute_accounts",
        store=True,
        readonly=False,
        help="Account for the private share of purchases (Private Share Account "
        "of the purchase journals), e.g. 1800 private withdrawals.",
    )
    purchase_journal_ids = fields.Many2many(
        "account.journal",
        "l10n_de_euer_setup_purchase_journal_rel",
        string="Purchase Journals without Private Share Account",
        compute="_compute_journals",
    )
    private_account_ids = fields.Many2many(
        "account.account",
        string="Private Accounts",
        compute="_compute_journals",
        help="Accounts with the category private withdrawals or contributions.",
    )
    # step 5: money journals that are private accounts (R-18)
    money_journal_ids = fields.Many2many(
        "account.journal",
        "l10n_de_euer_setup_money_journal_rel",
        string="Money Journals",
        compute="_compute_journals",
    )
    private_journal_ids = fields.Many2many(
        "account.journal",
        "l10n_de_euer_setup_private_journal_rel",
        string="Journals of a Private Account",
        compute="_compute_private_journal_ids",
        store=True,
        readonly=False,
        help="Money journals of a private account without a separate business "
        "account (R-18).",
    )
    # step 6: lock dates (G-05)
    fiscalyear_lock_date = fields.Date(related="company_id.fiscalyear_lock_date")
    hard_lock_date = fields.Date(related="company_id.hard_lock_date")
    lock_advice = fields.Text(compute="_compute_lock_advice")

    # ------------------------------------------------------------ computes
    @api.depends("company_id")
    def _compute_company_values(self):
        Period = self.env["l10n_de_euer.vat.period"].sudo()
        templates = dict(
            self.env["res.company"]
            ._fields["chart_template"]
            ._description_selection(self.env)
        )
        for wizard in self:
            company = wizard.company_id.sudo()
            wizard.chart_template = company.chart_template or ""
            wizard.chart_template_name = templates.get(
                company.chart_template, company.chart_template or ""
            )
            wizard.vat_period_count = Period.search_count(
                Domain("company_id", "=", company.id)
            )

    # one compute per field: a wizard created with only some of them (API)
    # still gets the others from the company
    @api.depends("company_id")
    def _compute_business_type(self):
        for wizard in self:
            wizard.business_type = wizard.company_id.sudo().l10n_de_euer_business_type

    @api.depends("company_id")
    def _compute_legal_form(self):
        for wizard in self:
            wizard.legal_form = wizard.company_id.sudo().l10n_de_euer_legal_form

    @api.depends("company_id")
    def _compute_income_type(self):
        for wizard in self:
            wizard.income_type = wizard.company_id.sudo().l10n_de_euer_income_type

    @api.depends("company_id")
    def _compute_owner(self):
        for wizard in self:
            wizard.owner = wizard.company_id.sudo().l10n_de_euer_owner or "1"

    @api.depends("company_id")
    def _compute_entertainment_mode(self):
        for wizard in self:
            wizard.entertainment_mode = (
                wizard.company_id.sudo().l10n_de_euer_entertainment_mode or "separate"
            )

    @api.depends("date_from")
    def _compute_prev_year_turnover_year(self):
        for wizard in self:
            wizard.prev_year_turnover_year = (
                wizard.date_from.year if wizard.date_from else 0
            )

    @api.depends("company_id")
    def _compute_activity_start(self):
        for wizard in self:
            wizard.activity_start = wizard.company_id.sudo().l10n_de_euer_activity_start

    @api.depends("company_id")
    def _compute_create_vat_period(self):
        Period = self.env["l10n_de_euer.vat.period"].sudo()
        for wizard in self:
            company = wizard.company_id.sudo()
            wizard.create_vat_period = bool(company) and not Period.search_count(
                Domain("company_id", "=", company.id)
            )

    @api.depends("company_id")
    def _compute_regime(self):
        """The treatment of the business, never a fixed default: a business
        under standard taxation must not get the accounts of a small
        business by running the setup again."""
        Period = self.env["l10n_de_euer.vat.period"].sudo()
        today = fields.Date.context_today(self)
        for wizard in self:
            company = wizard.company_id.sudo()
            regime = Period._get_regime(company, today) if company else False
            if company and not regime:
                regime = Period.search(
                    Domain("company_id", "=", company.root_id.id),
                    order="date_from desc",
                    limit=1,
                ).regime
            wizard.regime = regime or "ku"

    @api.model
    def _template_account(self, company, xml_id):
        if not xml_id:
            return self.env["account.account"]
        return self.env.ref(f"account.{company.id}_{xml_id}", raise_if_not_found=False)

    @api.depends("company_id")
    def _compute_current_accounts(self):
        for wizard in self:
            company = wizard.company_id.sudo()
            wizard.income_account_id = company.income_account_id
            wizard.expense_account_id = company.expense_account_id

    @api.depends("company_id", "regime")
    def _compute_accounts(self):
        for wizard in self:
            company = wizard.company_id.sudo()
            accounts = TEMPLATE_ACCOUNTS.get(company.chart_template or "", {})
            income = accounts.get("income", (None, None))
            expense = accounts.get("expense", (None, None))
            wizard.ku_income_account_id = wizard._template_account(company, income[1])
            wizard.ku_expense_account_id = wizard._template_account(company, expense[1])
            wizard.private_share_account_id = wizard._template_account(
                company, accounts.get("private")
            )
            standard = wizard._template_account(company, income[0])
            wizard.switch_accounts = bool(
                wizard.regime == "ku"
                and wizard.ku_income_account_id
                and (
                    company.income_account_id == standard
                    or company.account_sale_tax_id
                    or company.account_purchase_tax_id
                )
            )

    def _business_companies(self):
        self.ensure_one()
        return self.company_id._l10n_de_euer_business_companies().sudo()

    @api.depends("company_id")
    def _compute_journals(self):
        Journal = self.env["account.journal"]
        Assignment = self.env["l10n_de_euer.account.assignment"].sudo()
        for wizard in self:
            if not wizard.company_id:
                wizard.purchase_journal_ids = wizard.money_journal_ids = Journal
                wizard.private_account_ids = self.env["account.account"]
                continue
            companies = wizard._business_companies()
            journals = Journal.search(Domain("company_id", "in", companies.ids))
            wizard.purchase_journal_ids = journals.filtered(
                lambda journal: journal.type == "purchase"
                and not journal.non_deductible_account_id
            )
            wizard.money_journal_ids = journals.filtered(
                lambda journal: journal.type in MONEY_JOURNAL_TYPES
            )
            wizard.private_account_ids = Assignment.search(
                Domain("company_id", "=", wizard.company_id.id)
                & Domain("category_id.role", "=", "privat")
            ).account_id

    @api.depends("company_id")
    def _compute_private_journal_ids(self):
        Journal = self.env["account.journal"]
        for wizard in self:
            wizard.private_journal_ids = (
                Journal.search(
                    Domain("company_id", "in", wizard._business_companies().ids)
                    & Domain("type", "in", list(MONEY_JOURNAL_TYPES))
                    & Domain("l10n_de_euer_private_account", "=", True)
                )
                if wizard.company_id
                else Journal
            )

    @api.depends("company_id", "fiscalyear_lock_date", "hard_lock_date")
    def _compute_lock_advice(self):
        for wizard in self:
            company = wizard.company_id.sudo()
            if company.hard_lock_date and not company.fiscalyear_lock_date:
                wizard.lock_advice = self.env._(
                    "Only the hard lock date is set. Use the fiscal year lock date "
                    "after each year instead: unlike the hard lock it allows a "
                    "temporary exception for late December lines (C-70)."
                )
            elif company.fiscalyear_lock_date:
                wizard.lock_advice = self.env._(
                    "The fiscal year lock date is set. Set it to 31 December of "
                    "each year before the statement of that year is finalised."
                )
            else:
                wizard.lock_advice = self.env._(
                    "No lock date is set. Set the fiscal year lock date to 31 "
                    "December of a year before its statement is finalised (G-05); "
                    "the module never sets a lock date itself."
                )

    # ------------------------------------------------------------ apply
    def _check_manager(self):
        if not self.env.user.has_group("account.group_account_manager"):
            raise UserError(
                self.env._("Only an accounting manager can set up a business.")
            )

    def action_apply(self):
        """Write the settings of the business (spec 7.4)."""
        self.ensure_one()
        self._check_manager()
        company = self.company_id
        if (
            self.enable
            and not self.create_vat_period
            and not self.env["l10n_de_euer.vat.period"]
            .sudo()
            .search_count(Domain("company_id", "=", company.id))
        ):
            raise UserError(
                self.env._(
                    "%(company)s has no VAT status period: create one with the "
                    "wizard, otherwise every year is computed as standard taxation.",
                    company=company.display_name,
                )
            )
        changes = []
        company.write(
            {
                "l10n_de_euer_enabled": self.enable,
                "l10n_de_euer_business_type": self.business_type,
                "l10n_de_euer_legal_form": self.legal_form,
                "l10n_de_euer_income_type": self.income_type,
                "l10n_de_euer_owner": self.owner or company.l10n_de_euer_owner or "1",
                "l10n_de_euer_entertainment_mode": self.entertainment_mode
                or "separate",
                "l10n_de_euer_activity_start": self.activity_start,
            }
        )
        changes.append(self.env._("settings of the business"))
        if self.create_vat_period:
            changes.append(self._apply_vat_period())
        if self.switch_accounts and self.regime == "ku":
            changes.append(self._apply_small_business_accounts())
        if self.private_share_account_id and self.purchase_journal_ids:
            self.purchase_journal_ids.write(
                {"non_deductible_account_id": self.private_share_account_id.id}
            )
            changes.append(
                self.env._(
                    "private share account of %(journals)s",
                    journals=", ".join(self.purchase_journal_ids.mapped("name")),
                )
            )
        private = self.private_journal_ids & self.money_journal_ids
        (self.money_journal_ids - private).filtered(
            "l10n_de_euer_private_account"
        ).write({"l10n_de_euer_private_account": False})
        private.filtered(
            lambda journal: not journal.l10n_de_euer_private_account
        ).write({"l10n_de_euer_private_account": True})
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": self.env._("EÜR Setup"),
                "message": self.env._(
                    "Saved: %(changes)s.", changes="; ".join(filter(None, changes))
                ),
                "type": "success",
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    def _apply_vat_period(self):
        Period = self.env["l10n_de_euer.vat.period"]
        if not self.regime or not self.date_from:
            raise UserError(
                self.env._("Enter the VAT treatment and the start of the period.")
            )
        overlapping = Period.search_count(
            Domain("company_id", "=", self.company_id.id)
            & (Domain("date_to", "=", False) | Domain("date_to", ">=", self.date_from))
        )
        if overlapping:
            raise UserError(
                self.env._(
                    "%(company)s has a VAT status period from %(date)s on already; "
                    "change the periods under Configuration › EÜR › VAT Status "
                    "Periods.",
                    company=self.company_id.name,
                    date=self.date_from,
                )
            )
        Period.create(
            {
                "company_id": self.company_id.id,
                "date_from": self.date_from,
                "regime": self.regime,
                "vat_return_period": self.vat_return_period,
                "permanent_extension": self.permanent_extension,
                "prev_year_turnover": self.prev_year_turnover,
                "prev_year_turnover_year": self.prev_year_turnover_year
                if self.prev_year_turnover
                else 0,
                "reason": self.env._("Created by the EÜR setup."),
            }
        )
        return self.env._("VAT status period from %(date)s", date=self.date_from)

    def _apply_small_business_accounts(self):
        """Default accounts and taxes without VAT for small businesses (C-30)."""
        company = self.company_id
        companies = self._business_companies()
        old_income = company.income_account_id
        old_expense = company.expense_account_id
        values = {"account_sale_tax_id": False, "account_purchase_tax_id": False}
        if self.ku_income_account_id:
            values["income_account_id"] = self.ku_income_account_id.id
        if self.ku_expense_account_id:
            values["expense_account_id"] = self.ku_expense_account_id.id
        company.write(values)
        Journal = self.env["account.journal"]
        if self.ku_income_account_id and old_income:
            Journal.search(
                Domain("company_id", "in", companies.ids)
                & Domain("type", "=", "sale")
                & Domain("default_account_id", "=", old_income.id)
            ).write({"default_account_id": self.ku_income_account_id.id})
        if self.ku_expense_account_id and old_expense:
            Journal.search(
                Domain("company_id", "in", companies.ids)
                & Domain("type", "=", "purchase")
                & Domain("default_account_id", "=", old_expense.id)
            ).write({"default_account_id": self.ku_expense_account_id.id})
        return self.env._("default accounts and taxes without VAT")

    # ------------------------------------------------------------ links
    def action_open_account_review(self):
        return self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_account_review"
        )

    def action_open_takeover(self):
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_opening_wizard"
        )
        action["context"] = {"default_company_id": self.company_id.id}
        return action
