# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class ProjectProject(models.Model):
    _inherit = "project.project"

    papierkram_key = fields.Char(
        string="Papierkram Project",
        copy=False,
        index="btree_not_null",
        help="Customer and project in Papierkram; a second import uses the project.",
    )
