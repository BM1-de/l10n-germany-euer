# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class AccountMove(models.Model):
    _inherit = "account.move"

    papierkram_number = fields.Char(
        copy=False,
        index="btree_not_null",
        readonly=True,
        help="Number of the Papierkram document this entry was imported from "
        "(for example R-00042 or B-00117).",
    )
    papierkram_import_id = fields.Many2one(
        comodel_name="papierkram.import",
        copy=False,
        readonly=True,
        index="btree_not_null",
        ondelete="restrict",
    )

    _papierkram_number_company_uniq = models.UniqueIndex(
        "(company_id, papierkram_number, move_type) "
        "WHERE papierkram_number IS NOT NULL",
        "This Papierkram document has already been imported.",
    )
