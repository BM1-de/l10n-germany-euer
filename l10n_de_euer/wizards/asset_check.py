# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class L10nDeEuerAssetCheckWizard(models.TransientModel):
    """Run the checks of the asset register for a business and year."""

    _name = "l10n_de_euer.asset.check.wizard"
    _description = "EÜR Asset Register Check"

    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company._l10n_de_euer_business_root(),
        domain="['|', ('parent_id', '=', False), "
        "('l10n_de_euer_separate_business', '=', True)]",
    )
    year = fields.Integer(
        required=True,
        default=lambda self: fields.Date.context_today(self).year - 1,
    )

    def action_run(self):
        self.ensure_one()
        checks = self.env["l10n_de_euer.check"]._run_asset_checks(
            self.company_id, self.year
        )
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_check"
        )
        action["domain"] = [("id", "in", checks.ids)]
        action["context"] = {}
        return action
