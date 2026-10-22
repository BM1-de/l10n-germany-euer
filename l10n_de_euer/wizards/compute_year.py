# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from decimal import Decimal

from odoo import api, fields, models
from odoo.exceptions import UserError


class L10nDeEuerComputeYear(models.TransientModel):
    """Compute the cash basis profit of a year (method "Jahr berechnen")."""

    _name = "l10n_de_euer.compute.year"
    _description = "EÜR Compute Year"

    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        help="Company of the business; a branch computes the business it belongs to.",
    )
    year = fields.Integer(
        required=True,
        default=lambda self: fields.Date.context_today(self).year - 1,
    )
    run_assignment_checks = fields.Boolean(
        string="Check Account Assignment, Too",
        default=True,
        help="Also run the checks of the account assignment (C-01 to C-09).",
    )
    run_asset_checks = fields.Boolean(
        string="Check Asset Register, Too",
        default=True,
        help="Also run the checks of the asset register (C-30, C-40, C-42, "
        "C-61 to C-69) against the computed form values.",
    )

    @api.constrains("year")
    def _check_year(self):
        for wizard in self:
            if not 1900 < wizard.year < 3000:
                raise UserError(self.env._("Enter a valid year."))

    def action_compute(self):
        self.ensure_one()
        business = self.company_id._l10n_de_euer_business_root()
        if self.run_assignment_checks:
            self.env["l10n_de_euer.check"]._run_assignment_checks(
                business.root_id, self.year
            )
        run = self.env["l10n_de_euer.run"]._compute_year(business, self.year)
        if self.run_asset_checks:
            self.env["l10n_de_euer.check"]._run_asset_checks(
                business,
                self.year,
                form_values={
                    code: Decimal(value)
                    for code, value in (run.sudo().form_values or {}).items()
                },
            )
        return {
            "type": "ir.actions.act_window",
            "res_model": "l10n_de_euer.run",
            "res_id": run.id,
            "view_mode": "form",
            "target": "current",
        }
