# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    l10n_de_euer_recurring = fields.Boolean(
        string="Recurring Payments (EÜR)",
        tracking=True,
        help="Payments to or from this partner are regularly recurring (rent, "
        "insurance). Payments around the turn of the year are proposed for the "
        "ten-day rule (§ 11 EStG); the rule is checked without this flag, too.",
    )
    l10n_de_euer_tax_office = fields.Boolean(
        string="Tax Office (EÜR)",
        tracking=True,
        help="Tax office, Federal Central Tax Office or customs: payments are tax "
        "payments or refunds (ten-day rule for VAT returns).",
    )
