# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class AccountAnalyticLine(models.Model):
    _inherit = "account.analytic.line"

    papierkram_key = fields.Char(
        string="Papierkram Time Entry",
        copy=False,
        index="btree_not_null",
        help="Identity of the time entry in Papierkram; a second import skips it.",
    )
    papierkram_import_id = fields.Many2one(
        "papierkram.import", copy=False, index="btree_not_null", ondelete="set null"
    )

    _papierkram_key_uniq = models.UniqueIndex(
        "(company_id, papierkram_key) WHERE papierkram_key IS NOT NULL"
    )
