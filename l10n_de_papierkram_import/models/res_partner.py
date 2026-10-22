# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    papierkram_number = fields.Char(
        copy=False,
        index="btree_not_null",
        help="Customer or supplier number in Papierkram (K-… or L-…).",
    )
