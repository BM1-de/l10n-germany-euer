# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    l10n_de_euer_enabled = fields.Boolean(
        related="company_id.root_id.l10n_de_euer_enabled", readonly=False
    )
    l10n_de_euer_entertainment_mode = fields.Selection(
        related="company_id.root_id.l10n_de_euer_entertainment_mode", readonly=False
    )
    l10n_de_euer_offsetting_is_payment = fields.Boolean(
        related="company_id.root_id.l10n_de_euer_offsetting_is_payment",
        readonly=False,
    )
    l10n_de_euer_fx_mode = fields.Selection(
        related="company_id.root_id.l10n_de_euer_fx_mode", readonly=False
    )
    l10n_de_euer_disposal_month = fields.Boolean(
        related="company_id.root_id.l10n_de_euer_disposal_month", readonly=False
    )
