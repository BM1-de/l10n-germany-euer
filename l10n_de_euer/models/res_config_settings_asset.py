# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


# the settings of the asset register stay in a file of their own
class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"  # pylint: disable=consider-merging-classes-inherited

    l10n_de_euer_disposal_month = fields.Boolean(
        related="company_id.root_id.l10n_de_euer_disposal_month", readonly=False
    )
