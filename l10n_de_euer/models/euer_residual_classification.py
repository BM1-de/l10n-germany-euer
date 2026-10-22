# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from .euer_move_info import VAT_REGIMES

CLASSIFICATION_KINDS = [
    ("revenue", "Business Income (e.g. Advance Payment Received)"),
    ("expense", "Business Expense (e.g. Advance Payment Made)"),
    ("expense_refund", "Refund of a Business Expense"),
    ("negative_revenue", "Reduction of Business Income (Refund, Returned Debit)"),
    ("loan", "Loan"),
    ("deposit", "Deposit"),
    ("refund_obligation", "Repayment Obligation"),
]
#: kinds realised like a document line with category, rate and treatment
REALISED_KINDS = ("revenue", "expense", "expense_refund", "negative_revenue")
#: fields of the decision: who decided and when is set by the record itself
DECISION_FIELDS = frozenset(
    {"kind", "category_id", "tax_rate", "vat_regime", "reason", "source_line_id"}
)
#: neutral kinds need a reason (spec 5.7 no. 2)
NEUTRAL_KINDS = ("loan", "deposit", "refund_obligation")


class L10nDeEuerResidualClassification(models.Model):
    """Classification of money the calculation cannot assign (spec 5.7 no. 2).

    Unallocated rests of a payment on a receivable or payable (C-20), money
    reaching a write-off (returned direct debit, C-36) and money to money
    over a receivable or payable between two years (C-37). The slices are
    computed anew on every run, so the decision is stored here; it applies to
    the rest still open at the time of the run.
    """

    _name = "l10n_de_euer.residual.classification"
    _inherit = ["mail.thread"]
    _description = "EÜR Classification of an Unallocated Rest"
    _rec_name = "source_line_name"
    _order = "id desc"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        default=lambda self: self.env.company._l10n_de_euer_business_root(),
        help="Business (main company with its branches, or a branch that is a "
        "business of its own).",
    )
    # A reconciliation of the statement line deletes and creates its journal
    # items again: the classification stays with its snapshot and is marked
    # as obsolete instead of disappearing with the line.
    source_line_id = fields.Many2one(
        "account.move.line",
        string="Journal Item",
        index=True,
        ondelete="set null",
        tracking=True,
        help="Reconcilable line of the money entry that carries the rest, or the "
        "write-off line the money reached.",
    )
    source_line_ref = fields.Integer(
        string="Journal Item ID", readonly=True, help="Snapshot of the journal item."
    )
    source_line_name = fields.Char(
        string="Journal Item Name", readonly=True, help="Snapshot of the journal item."
    )
    obsolete = fields.Boolean(
        compute="_compute_obsolete",
        search="_search_obsolete",
        help="The journal item no longer exists (e.g. the statement line was "
        "reconciled again); the classification has no effect.",
    )
    move_id = fields.Many2one(related="source_line_id.move_id", string="Entry")
    partner_id = fields.Many2one(related="source_line_id.partner_id")
    account_id = fields.Many2one(related="source_line_id.account_id")
    date = fields.Date(related="source_line_id.date")
    check_code = fields.Selection(
        [
            ("C-20", "C-20 Unallocated Rest"),
            ("C-36", "C-36 Money Reaches a Write-Off"),
            ("C-37", "C-37 Money to Money Between Two Years"),
        ],
        string="Check",
        tracking=True,
    )
    partial_path = fields.Json(
        help="Partial reconciliations the money passed (C-36, C-37)."
    )
    kind = fields.Selection(CLASSIFICATION_KINDS, required=True, tracking=True)
    category_id = fields.Many2one(
        "l10n_de_euer.category",
        tracking=True,
        domain="[('role', 'in', ('be', 'ba'))]",
        help="Kind of income or expense (e.g. the revenue account of the invoice "
        "to come).",
    )
    tax_rate = fields.Float(
        string="VAT Rate (%)",
        digits=(5, 2),
        tracking=True,
        help="VAT rate under standard taxation; the amount is split into net and "
        "VAT. Not used for small businesses.",
    )
    vat_regime = fields.Selection(
        VAT_REGIMES,
        string="VAT Treatment",
        tracking=True,
        help="Empty: the treatment valid on the cash date.",
    )
    amount_classified = fields.Monetary(
        string="Classified Rest",
        currency_field="currency_id",
        tracking=True,
        help="Rest at the time of the classification (information).",
    )
    currency_id = fields.Many2one(related="company_id.currency_id")
    reason = fields.Text(tracking=True)
    decided_by_id = fields.Many2one("res.users", readonly=True)
    decided_on = fields.Datetime(readonly=True)

    _line_unique = models.Constraint(
        "UNIQUE(source_line_id)",
        "A journal item is classified only once.",
    )

    @api.depends("source_line_id")
    def _compute_obsolete(self):
        for item in self:
            item.obsolete = not item.source_line_id

    def _search_obsolete(self, operator, value):
        if operator not in ("=", "!=") or not isinstance(value, bool):
            return NotImplemented
        obsolete = (operator == "=") == value
        return [("source_line_id", "=" if obsolete else "!=", False)]

    @api.constrains("kind", "category_id", "reason")
    def _check_kind(self):
        for item in self:
            if item.kind in REALISED_KINDS:
                if not item.category_id:
                    raise ValidationError(
                        self.env._(
                            "Choose the category of the income or expense of %(line)s.",
                            line=item.source_line_id.display_name,
                        )
                    )
                wanted = "ba" if item.kind in ("expense", "expense_refund") else "be"
                if item.category_id.role != wanted:
                    raise ValidationError(
                        self.env._(
                            "%(category)s does not fit the classification %(kind)s.",
                            category=item.category_id.display_name,
                            kind=dict(CLASSIFICATION_KINDS)[item.kind],
                        )
                    )
            elif not item.reason:
                raise ValidationError(
                    self.env._(
                        "Give a reason why %(line)s is a loan, deposit or repayment "
                        "obligation.",
                        line=item.source_line_id.display_name,
                    )
                )

    @api.constrains("source_line_id", "company_id")
    def _check_business(self):
        """The journal item belongs to the business of the classification;
        the calculation reads only the classifications of its business."""
        for item in self:
            line = item.source_line_id.sudo()
            companies = item.company_id.sudo()._l10n_de_euer_business_companies()
            if line and line.company_id not in companies:
                raise ValidationError(
                    self.env._(
                        "%(line)s does not belong to the business %(company)s.",
                        line=line.display_name,
                        company=item.company_id.display_name,
                    )
                )

    @api.constrains("tax_rate")
    def _check_tax_rate(self):
        for item in self:
            if not 0 <= item.tax_rate < 100:
                raise ValidationError(self.env._("The VAT rate must be a percentage."))

    @api.model_create_multi
    def create(self, vals_list):
        """Who decided and when is always the current user and time (G-06)."""
        lines = self.env["account.move.line"].sudo()
        for vals in vals_list:
            if not vals.get("source_line_id"):
                raise UserError(self.env._("Choose the journal item to classify."))
            line = lines.browse(vals["source_line_id"])
            vals.update(
                decided_by_id=self.env.uid,
                decided_on=fields.Datetime.now(),
                source_line_ref=line.id,
                source_line_name=line.display_name,
            )
        return super().create(vals_list)

    def write(self, vals):
        if "source_line_id" in vals and not vals["source_line_id"]:
            raise UserError(self.env._("Choose the journal item to classify."))
        vals = {
            name: value
            for name, value in vals.items()
            if name
            not in (
                "decided_by_id",
                "decided_on",
                "source_line_ref",
                "source_line_name",
            )
        }
        if set(vals) & DECISION_FIELDS:
            vals.update(decided_by_id=self.env.uid, decided_on=fields.Datetime.now())
        if vals.get("source_line_id"):
            line = self.env["account.move.line"].sudo().browse(vals["source_line_id"])
            vals.update(source_line_ref=line.id, source_line_name=line.display_name)
        return super().write(vals)
