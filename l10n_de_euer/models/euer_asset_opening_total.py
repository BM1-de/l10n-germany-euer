# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models

from .euer_asset import AVEUER_GROUPS


class L10nDeEuerAssetOpeningTotal(models.Model):
    """Book value at the end of a year per group of the filed Anlage AVEÜR.

    Entered with the takeover of a register from another system (spec 6.6,
    U-05): the checks compare the book values at the beginning of the
    following year with these filed values (C-61), so a different group of an
    asset in the takeover is noticed.
    """

    _name = "l10n_de_euer.asset.opening.total"
    _description = "EÜR Filed AVEÜR Book Value per Group"
    _order = "year desc, aveuer_group"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        default=lambda self: self.env.company._l10n_de_euer_business_root(),
        domain="['|', ('parent_id', '=', False), "
        "('l10n_de_euer_separate_business', '=', True)]",
    )
    currency_id = fields.Many2one(related="company_id.currency_id")
    year = fields.Integer(
        required=True, help="Year of the filed Anlage AVEÜR (the takeover year)."
    )
    aveuer_group = fields.Selection(AVEUER_GROUPS, string="AVEÜR Group", required=True)
    book_value_end = fields.Monetary(
        string="Book Value at End",
        help="Book value at the end of the year of the group in the filed "
        "Anlage AVEÜR.",
    )

    _company_year_group_uniq = models.Constraint(
        "UNIQUE(company_id, year, aveuer_group)",
        "There is one filed book value per business, year and group.",
    )
