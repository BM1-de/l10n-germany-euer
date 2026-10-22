# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Domain

from .engine import revenue

ROLES = [
    ("geld", "Cash"),
    ("privat", "Private"),
    ("op", "Open Item"),
    ("steuer", "Tax"),
    ("be", "Business Income"),
    ("ba", "Business Expense"),
    ("av", "Fixed Asset"),
    ("afa", "Depreciation"),
    ("eroeffnung", "Opening Balance"),
    ("neutral", "Neutral"),
    ("ungeklaert", "Unclear"),
    ("warn", "Not Provided For"),
]
#: Roles whose amounts never become a form value of their own (spec 5.7 no. 2)
ROLES_WITHOUT_VALUE = (
    "geld",
    "op",
    "av",
    "eroeffnung",
    "neutral",
    "ungeklaert",
    "warn",
)
CONFIDENCE = [("h", "High"), ("m", "Medium"), ("n", "Low")]


class L10nDeEuerCategory(models.Model):
    """Kind of an account for the EÜR, independent of the year (spec 4.1, 4.2)."""

    _name = "l10n_de_euer.category"
    _description = "EÜR Category"
    _order = "sequence, code"
    _rec_names_search = ["code", "name"]

    code = fields.Char(
        required=True,
        index=True,
        help="Stable technical key of the category, e.g. EXP_TELECOM.",
    )
    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    role = fields.Selection(
        ROLES,
        required=True,
        help="Treatment in the cash basis calculation: money accounts, open items, "
        "taxes, income, expenses, private, fixed assets, depreciation, opening "
        "balances, neutral accounts, bank suspense accounts and accounts the "
        "EÜR does not provide for.",
    )
    be_art = fields.Selection(
        [
            ("stpfl", "Taxable"),
            ("frei", "Tax-Exempt"),
            ("frei192", "Tax-Exempt under § 19 (2) UStG"),
            ("nstb", "Not Taxable"),
            ("hilfs", "Ancillary Transaction (Interest)"),
            ("tax", "By Tax of the Line"),
            ("s19a", "§ 19a UStG"),
            ("luf24", "§ 24 UStG"),
            ("ku", "Small Business Revenue Account"),
            ("fixed", "Fixed Field"),
        ],
        string="Revenue Type",
        help="Decides the Kennzahl of business income: taxable 20.112, tax-exempt "
        "and not taxable 20.103, by the UStVA tags of the line, or a fixed field. "
        "Small businesses always use 20.111, plus 20.119 for not taxable, "
        "§ 19 (2) and § 19a revenue and interest.",
    )
    erfassung = fields.Selection(
        [
            ("Z", "Z – at Payment"),
            ("B", "B – at Booking"),
            ("M", "M – Manual Only"),
        ],
        string="Recognition",
        required=True,
        default="Z",
        help="Z: realised with the money (default). B: realised at the booking "
        "date, e.g. depreciation and withdrawals in kind. M: the value comes "
        "from a manual profit correction; booked lines are reported.",
    )
    confidence = fields.Selection(
        CONFIDENCE,
        required=True,
        default="h",
        help="Default confidence of assignments to this category. Accounts with "
        "low confidence must be confirmed before the first finalisation.",
    )
    restricted_column = fields.Selection(
        [("deductible", "Deductible"), ("non_deductible", "Not Deductible")],
        help="Column of restricted business expenses on the form.",
    )
    writeoff = fields.Boolean(
        string="Write-Off",
        help="Bad debt or write-off of receivables. Money reaching such a line "
        "blocks the finalisation until it is classified.",
    )
    gift_bulk_allowed = fields.Boolean(
        string="Bulk Gifts Allowed",
        help="Low-value promotional gifts may be booked collectively without "
        "recipient.",
    )
    recurring_candidate = fields.Boolean(
        string="Recurring Payments",
        help="Payments of this kind are usually recurring. Only a pre-selection "
        "for the review list of the ten-day rule.",
    )
    interim = fields.Boolean(
        string="Interim Account",
        help="Money in transit, bank suspense or interim account; its balance "
        "should be zero at the end of the year.",
    )
    on_payment = fields.Boolean(
        string="Income or Expense at Payment",
        help="Balance sheet account with income or expense role (advance "
        "payments, payroll liabilities): the payment itself is income or expense.",
    )
    separate_record = fields.Selection(
        [("gift", "Gifts"), ("entertainment", "Entertainment")],
        string="Separate Record (§ 4 (7) EStG)",
        help="Expenses that must be recorded separately from other expenses.",
    )
    vat_kind = fields.Selection(
        [
            ("output", "Output VAT"),
            ("input", "Input VAT"),
            ("input_non_deductible", "Input VAT to Split or Not Deductible"),
            ("reverse_charge", "Reverse Charge, Intra-EU Acquisition"),
            ("import", "Import VAT"),
            ("settlement", "VAT Payment and Refund"),
        ],
        string="VAT Kind",
        help="Kind of tax account. Lines with a tax repartition line are assigned "
        "by their repartition line; this kind applies to lines without it.",
    )
    warning = fields.Selection(
        [
            ("provision", "Provision"),
            ("accrual", "Accrual or Deferral"),
            ("valuation", "Valuation Allowance or Write-Down"),
            ("inventory", "Inventory"),
            ("datev", "DATEV Auxiliary Account"),
            ("disposal_balance", "Net Result of Disposal"),
            ("collective", "Collective Account Not Allowed for the EÜR"),
            ("other", "Not Provided for in the EÜR"),
        ],
        help="Bookings on accounts of this category are reported as warning.",
    )
    scope_status = fields.Selection(
        [
            ("supported", "Supported"),
            ("manual", "Manual Input"),
            ("blocked", "Not Supported"),
        ],
        string="Program Scope",
        required=True,
        default="supported",
        help="Not supported: bookings indicate a case outside the program scope "
        "and block the finalisation.",
    )
    scope_reason = fields.Selection(
        [
            ("luf", "Agriculture and Forestry"),
            ("partial_income", "Partial Income Method, InvStG"),
            ("partnership", "Partnership Shares"),
            ("reserves", "Reserves"),
            ("iab", "Investment Deduction"),
            ("interest_limit", "Interest Limitation (Anlage SZ)"),
        ],
        help="Case outside the program scope this category stands for.",
    )
    mapping_ids = fields.One2many(
        "l10n_de_euer.category.mapping", "category_id", string="Form Fields"
    )
    assignment_count = fields.Integer(compute="_compute_assignment_count")

    _code_uniq = models.Constraint(
        "UNIQUE(code)", "The code of an EÜR category must be unique."
    )

    @api.depends("code", "name")
    def _compute_display_name(self):
        for category in self:
            category.display_name = category.name or category.code

    def _compute_assignment_count(self):
        counts = dict(
            self.env["l10n_de_euer.account.assignment"]._read_group(
                Domain("category_id", "in", self.ids), ["category_id"], ["__count"]
            )
        )
        for category in self:
            category.assignment_count = counts.get(category, 0)

    @api.constrains("role", "be_art", "vat_kind")
    def _check_role_flags(self):
        for category in self:
            if (category.role == "be") != bool(category.be_art):
                raise ValidationError(
                    self.env._(
                        "%(category)s: only business income has a revenue type, "
                        "and business income needs one.",
                        category=category.display_name,
                    )
                )
            if category.vat_kind and category.role != "steuer":
                raise ValidationError(
                    self.env._(
                        "%(category)s: only tax categories have a VAT kind.",
                        category=category.display_name,
                    )
                )

    @api.ondelete(at_uninstall=False)
    def _unlink_except_finalised(self):
        """The slices of a finalised statement refer to their category; the
        category stays as long as such a slice exists (G-04)."""
        used = (
            self.env["l10n_de_euer.cash.slice"]
            .sudo()
            .search_count(
                Domain("category_id", "in", self.ids)
                & Domain("run_id.locked", "=", True),
                limit=1,
            )
        )
        if used:
            raise UserError(
                self.env._(
                    "The category is used by a finalised statement and cannot be "
                    "deleted."
                )
            )

    def _get_mappings(self, form):
        """Return the mapping lines of ``self`` for a form year."""
        return self.mapping_ids.filtered(lambda mapping: mapping.form_id == form)

    def _get_default_revenue_field(self, form):
        """Field of business income under standard taxation (category default)."""
        self.ensure_one()
        mappings = self._get_mappings(form).filtered(
            lambda mapping: mapping.condition in ("always", "regular")
        )
        return mappings[:1].field_id

    def _get_revenue_fields(self, form, regime, tag_names=(), taxes=None):
        """Return ``(field, davon_field)`` records of a revenue line (spec 4.4).

        ``regime`` is the VAT treatment of the supply (``ku`` or ``regular``),
        ``tag_names`` the tax tag names and ``taxes`` the taxes
        (``account.tax``) of the base line.
        """
        self.ensure_one()
        if self.role != "be":
            raise ValidationError(
                self.env._(
                    "%(category)s is not business income.",
                    category=self.display_name,
                )
            )
        default = self._get_default_revenue_field(form)
        code, davon = revenue.revenue_fields(
            self.be_art,
            regime,
            tag_names,
            default.code or None,
            ku_tax=self._has_ku_tax(taxes),
        )
        fields_ = form.field_ids
        return (
            fields_.filtered(lambda field: field.code == code)[:1],
            fields_.filtered(lambda field: field.code == davon)[:1],
        )

    @api.model
    def _has_ku_tax(self, taxes):
        """Whether ``taxes`` contain the Odoo tax for small businesses (4.4)."""
        if not taxes:
            return False
        xml_ids = taxes.sudo().get_external_id()
        return any(revenue.is_ku_tax(xml_id) for xml_id in xml_ids.values())

    def action_open_assignments(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_account_assignment"
        )
        action["domain"] = [("category_id", "=", self.id)]
        action["context"] = {"default_category_id": self.id}
        return action


class L10nDeEuerCategoryMapping(models.Model):
    """Form fields of a category in one form year (spec 4.1, 4.5)."""

    _name = "l10n_de_euer.category.mapping"
    _description = "EÜR Category Mapping"
    _order = "year desc, category_id, sequence, id"

    category_id = fields.Many2one(
        "l10n_de_euer.category", required=True, index=True, ondelete="cascade"
    )
    role = fields.Selection(related="category_id.role")
    form_id = fields.Many2one(
        "l10n_de_euer.form",
        string="Form Year",
        required=True,
        index=True,
        ondelete="cascade",
    )
    year = fields.Integer(related="form_id.year", store=True, index=True)
    field_id = fields.Many2one(
        "l10n_de_euer.field",
        required=True,
        ondelete="cascade",
        domain="[('form_id', '=', form_id)]",
    )
    field_code = fields.Char(related="field_id.code", string="Field Code")
    sequence = fields.Integer(default=10)
    share = fields.Float(
        digits=(16, 4),
        default=1.0,
        required=True,
        help="Part of the amount that goes into the field, e.g. 0.7 and 0.3.",
    )
    condition = fields.Selection(
        [
            ("always", "Always"),
            ("debit", "Debit Only"),
            ("credit", "Credit Only"),
            ("ku", "Small Business Only"),
            ("regular", "Standard Taxation Only"),
        ],
        required=True,
        default="always",
        help="Debit and credit refer to the side of the realised amount, small "
        "business and standard taxation to the VAT treatment of the document.",
    )
    option = fields.Selection(
        [
            ("entertainment_separate", "Entertainment on Separate Accounts"),
            ("entertainment_split", "Entertainment Split 70/30"),
        ],
        string="Company Option",
        help="The line applies only if the company uses this option.",
    )
    davon_field_id = fields.Many2one(
        "l10n_de_euer.field",
        string="Of Which Field",
        ondelete="cascade",
        help="Field that receives the same amount as 'of which' (e.g. 20.119).",
    )
    additional_field_id = fields.Many2one(
        "l10n_de_euer.field",
        ondelete="cascade",
        help="Field that receives the same amount in addition, e.g. the private "
        "contribution 29.123 of the business use of a private vehicle.",
    )

    _share_range = models.Constraint(
        "CHECK(share > 0 AND share <= 1)", "The share must be above 0 and at most 1."
    )

    @api.depends("category_id", "field_id")
    def _compute_display_name(self):
        for mapping in self:
            mapping.display_name = (
                f"{mapping.category_id.display_name} → {mapping.field_id.code or ''}"
            )

    @api.constrains("form_id", "field_id", "davon_field_id", "additional_field_id")
    def _check_fields(self):
        for mapping in self:
            if mapping.field_id.form_id != mapping.form_id:
                raise ValidationError(
                    self.env._(
                        "The field %(field)s does not belong to %(form)s.",
                        field=mapping.field_id.code,
                        form=mapping.form_id.name,
                    )
                )
            for field in mapping.davon_field_id | mapping.additional_field_id:
                if field.year != mapping.year:
                    raise ValidationError(
                        self.env._(
                            "The field %(field)s is not a field of %(year)s.",
                            field=field.code,
                            year=mapping.year,
                        )
                    )

    @api.constrains("category_id", "form_id", "condition", "option", "share")
    def _check_share_total(self):
        for mapping in self:
            siblings = mapping.category_id.mapping_ids.filtered(
                lambda other, m=mapping: other.form_id == m.form_id
                and other.condition == m.condition
                and other.option == m.option
            )
            if sum(siblings.mapped("share")) > 1.0001:
                raise ValidationError(
                    self.env._(
                        "The shares of %(category)s in %(year)s exceed 100 %%.",
                        category=mapping.category_id.display_name,
                        year=mapping.year,
                    )
                )

    @api.model_create_multi
    def create(self, vals_list):
        mappings = super().create(vals_list)
        mappings.form_id._check_not_locked()
        return mappings

    def write(self, vals):
        self.form_id._check_not_locked()
        if vals.get("form_id"):
            self.env["l10n_de_euer.form"].browse(vals["form_id"])._check_not_locked()
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_locked_form(self):
        self.form_id._check_not_locked()
