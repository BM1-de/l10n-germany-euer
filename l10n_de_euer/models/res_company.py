# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    l10n_de_euer_enabled = fields.Boolean(
        string="Cash Basis Accounting (EÜR)",
        tracking=True,
        help="The profit is determined by cash basis accounting according to "
        "§ 4 (3) EStG. Do not submit the opening balance or a closing of Odoo as "
        "tax balance sheet.",
    )
    l10n_de_euer_entertainment_mode = fields.Selection(
        [
            ("separate", "Separate Accounts (70 % and 30 % Booked Separately)"),
            ("split", "Split 70/30 Automatically"),
        ],
        string="Business Entertainment",
        default="separate",
        tracking=True,
        help="Separate accounts: the deductible 70 % are booked on 4650 (SKR04 "
        "6640) and the non-deductible 30 % on 4654 (6644). Split automatically: "
        "the full amount is booked on 4650 (6640) and divided 70/30.",
    )
    l10n_de_euer_vat_period_ids = fields.One2many(
        "l10n_de_euer.vat.period", "company_id", string="VAT Status Periods"
    )
    l10n_de_euer_offsetting_is_payment = fields.Boolean(
        string="Offsetting Is Payment",
        default=True,
        tracking=True,
        help="A set-off of a receivable against a payable of the same partner "
        "without money counts as receipt and payment on its date (H 11 "
        "'Aufrechnung'). Income and expense are shown gross, not netted.",
    )
    l10n_de_euer_fx_mode = fields.Selection(
        [
            ("separate", "Separate Position"),
            ("allocate", "Allocate to the Original Categories"),
        ],
        string="Realised Exchange Differences",
        default="separate",
        tracking=True,
        help="Separate position: realised exchange differences of a payment are "
        "shown as other income or expense. Allocate: they correct the categories "
        "of the document, so only the euro amount received counts.",
    )
    l10n_de_euer_disposal_month = fields.Boolean(
        string="Depreciation in the Month of Disposal",
        default=True,
        tracking=True,
        help="The month of the disposal of a fixed asset counts for its "
        "depreciation (R 7.4 (8) EStR).",
    )
    l10n_de_euer_separate_business = fields.Boolean(
        string="Separate Business (EÜR)",
        tracking=True,
        help="This branch is a business of its own with its own EÜR. Otherwise "
        "branches belong to the business of their main company.",
    )
    l10n_de_euer_business_id = fields.Many2one(
        "res.company",
        string="EÜR Business",
        compute="_compute_l10n_de_euer_business_id",
        store=True,
        recursive=True,
        index=True,
        help="Company that represents the business of this company: the main "
        "company, or the branch that is a business of its own (spec 7.9).",
    )
    l10n_de_euer_member_ids = fields.One2many(
        "res.company",
        "l10n_de_euer_business_id",
        string="Companies of the EÜR Business",
        help="Companies whose business this company is (record rules).",
    )
    l10n_de_euer_business_type = fields.Char(
        string="Type of Business",
        tracking=True,
        help="Header of the Anlage EÜR, e.g. photographer.",
    )
    l10n_de_euer_legal_form = fields.Char(
        string="Legal Form (EÜR)",
        tracking=True,
        help="Header of the Anlage EÜR, e.g. sole proprietorship.",
    )
    l10n_de_euer_income_type = fields.Selection(
        [
            ("1", "1 Agriculture and Forestry"),
            ("2", "2 Trade or Business"),
            ("3", "3 Self-Employment"),
        ],
        string="Type of Income (Kz 103)",
        tracking=True,
    )
    l10n_de_euer_owner = fields.Selection(
        [
            ("1", "1 Taxpayer / Person A"),
            ("2", "2 Spouse / Person B"),
            ("3", "3 Both"),
        ],
        string="Business Owner (Kz 104)",
        default="1",
        tracking=True,
    )
    l10n_de_euer_activity_start = fields.Date(
        string="Start of the Activity",
        tracking=True,
        help="Day the entrepreneurial activity of the owner started as a whole, "
        "including other businesses outside Odoo (e.g. a photovoltaic system "
        "feeding into the grid): § 2 (1) sentence 2 UStG, one enterprise. In the "
        "year of the start the small business limit of the current year is "
        "25,000 € instead of 100,000 € (§ 19 (1) UStG from 2025, section 19.1 "
        "(3) UStAE), and there is no previous year to check; until 2024 the "
        "turnover of that year is converted into a full year. It belongs to the "
        "main company like the VAT status periods: the limits apply to the whole "
        "enterprise.",
    )

    @api.depends(
        "parent_id",
        "parent_id.l10n_de_euer_business_id",
        "l10n_de_euer_separate_business",
    )
    def _compute_l10n_de_euer_business_id(self):
        for company in self:
            parent = company.parent_id
            if parent and not company.l10n_de_euer_separate_business:
                company.l10n_de_euer_business_id = (
                    parent.l10n_de_euer_business_id or parent
                )
            else:
                company.l10n_de_euer_business_id = company

    def _l10n_de_euer_get_vat_regime(self, on_date):
        """VAT treatment of the business on a date: ``ku``, ``regular`` or False."""
        self.ensure_one()
        return self.env["l10n_de_euer.vat.period"]._get_regime(self, on_date)

    def _l10n_de_euer_get_mapping_option(self):
        """Company option that selects mapping lines (spec 4.5, 5.7 no. 8)."""
        self.ensure_one()
        mode = self.root_id.l10n_de_euer_entertainment_mode or "separate"
        return f"entertainment_{mode}"

    def _l10n_de_euer_business_root(self):
        """Company that represents the business of ``self`` (spec 5.2).

        The main company, unless ``self`` or a parent between them is a branch
        marked as separate business.
        """
        self.ensure_one()
        company = self.sudo()
        while company.parent_id and not company.l10n_de_euer_separate_business:
            company = company.parent_id
        return company.with_env(self.env)

    def _l10n_de_euer_business_companies(self):
        """Companies of the business: its root and the branches below it.

        Branches marked as separate business and their branches are left out.
        """
        root = self._l10n_de_euer_business_root().sudo()
        result = root
        todo = root.with_context(active_test=False).child_ids
        while todo:
            kept = todo.filtered(lambda c: not c.l10n_de_euer_separate_business)
            result |= kept
            todo = kept.with_context(active_test=False).child_ids
        return result.with_env(self.env)
