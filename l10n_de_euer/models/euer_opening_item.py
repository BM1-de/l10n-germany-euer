# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .euer_move_info import VAT_REGIMES


class L10nDeEuerOpeningItem(models.Model):
    """Open item taken over from an opening entry (spec 7.3, B-08).

    Opening entries are never money; their open receivables and payables
    become virtual documents ("Altbestand"). When they are paid, the amount
    is split with the category and VAT rate entered here (standard taxation:
    net to the category, VAT to Kz 140/185; small business: gross). Without
    this information the payment is blocked (C-35).
    """

    _name = "l10n_de_euer.opening.item"
    _inherit = ["mail.thread"]
    _description = "EÜR Open Item Taken Over"
    _rec_name = "move_line_id"
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
    move_line_id = fields.Many2one(
        "account.move.line",
        string="Open Item",
        required=True,
        index=True,
        ondelete="cascade",
        tracking=True,
        domain="[('parent_state', '=', 'posted'), ('account_id.reconcile', '=', True),"
        " ('account_id.internal_group', 'in', ('asset', 'liability'))]",
        help="Open receivable or payable of the opening entry.",
    )
    move_id = fields.Many2one(related="move_line_id.move_id", string="Opening Entry")
    partner_id = fields.Many2one(related="move_line_id.partner_id")
    account_id = fields.Many2one(related="move_line_id.account_id")
    balance = fields.Monetary(
        related="move_line_id.balance", currency_field="currency_id"
    )
    currency_id = fields.Many2one(related="company_id.currency_id")
    category_id = fields.Many2one(
        "l10n_de_euer.category",
        required=True,
        tracking=True,
        domain="[('role', 'in', ('be', 'ba'))]",
        help="Kind of income or expense of the original invoice.",
    )
    vat_regime = fields.Selection(
        VAT_REGIMES, string="VAT Treatment", required=True, tracking=True
    )
    tax_rate = fields.Float(
        string="VAT Rate (%)",
        digits=(5, 2),
        tracking=True,
        help="VAT rate of the original invoice under standard taxation.",
    )
    origin_reference = fields.Char(
        string="Reference in the Previous System",
        tracking=True,
        help="Invoice number in the previous accounting system.",
    )
    reason = fields.Text(tracking=True)

    _line_unique = models.Constraint(
        "UNIQUE(move_line_id)",
        "An open item is taken over only once.",
    )

    @api.constrains("move_line_id", "company_id")
    def _check_open_item(self):
        """The item is an open item (receivable, payable or other
        reconcilable balance sheet line) of an opening entry of the business
        (spec 5.2, B-08); the calculation reads only those."""
        for item in self:
            line = item.move_line_id.sudo()
            business = item.company_id.sudo()
            companies = business._l10n_de_euer_business_companies().sudo()
            if line.company_id not in companies:
                raise ValidationError(
                    self.env._(
                        "%(line)s does not belong to the business %(company)s.",
                        line=line.display_name,
                        company=business.display_name,
                    )
                )
            if not line.account_id._l10n_de_euer_is_open_item():
                raise ValidationError(
                    self.env._(
                        "%(line)s is not an open item (receivable, payable or "
                        "reconcilable balance sheet account).",
                        line=line.display_name,
                    )
                )
            if not self._is_opening_entry(line.move_id, companies):
                raise ValidationError(
                    self.env._(
                        "%(move)s is no opening entry (opening move of the company "
                        "or entry with an opening balance account).",
                        move=line.move_id.display_name,
                    )
                )

    @api.model
    def _is_opening_entry(self, move, companies):
        """Opening entry of the business (class ERÖFFNUNG, spec 5.2)."""
        if move in companies.account_opening_move_id:
            return True
        root = companies[:1].root_id
        assignments = (
            self.env["l10n_de_euer.account.assignment"]
            .sudo()
            ._get_assignments(root, move.line_ids.account_id, move.date)
        )
        return any(
            assignment.category_id.role == "eroeffnung"
            for assignment in assignments.values()
        )

    @api.constrains("tax_rate", "vat_regime")
    def _check_tax_rate(self):
        for item in self:
            if not 0 <= item.tax_rate < 100:
                raise ValidationError(self.env._("The VAT rate must be a percentage."))
