# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class L10nDeEuerStatementCorrection(models.TransientModel):
    """Create a correction of a finalised statement (spec 7.7 step 6, G-07)."""

    _name = "l10n_de_euer.statement.correction"
    _description = "EÜR Statement Correction"

    statement_id = fields.Many2one(
        "l10n_de_euer.statement",
        required=True,
        readonly=True,
        domain="[('state', '=', 'final')]",
    )
    reason = fields.Text(
        required=True,
        help="Why the finalised statement is corrected, e.g. an entry posted "
        "later or a value found wrong.",
    )

    def action_create(self):
        self.ensure_one()
        correction = self.statement_id._create_correction(self.reason)
        return {
            "type": "ir.actions.act_window",
            "res_model": correction._name,
            "res_id": correction.id,
            "view_mode": "form",
            "target": "current",
        }
