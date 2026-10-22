# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain


# the settings of the asset register stay in a file of their own
class ResCompany(models.Model):
    _inherit = "res.company"  # pylint: disable=consider-merging-classes-inherited

    l10n_de_euer_asset_lock_year = fields.Integer(
        string="Asset Register Locked Up To",
        readonly=True,
        copy=False,
        help="Last year whose statement is finalised: the asset register "
        "cannot change it any more, also not by assets entered later.",
    )
    l10n_de_euer_disposal_month = fields.Boolean(
        string="Month of Disposal Counts",
        default=True,
        tracking=True,
        help="Depreciation in the year of a disposal includes the month of the "
        "disposal (R 7.4 (8) EStR does not say; the profit is the same, only "
        "the split between depreciation and residual book value changes).",
    )

    def write(self, vals):
        self._l10n_de_euer_check_asset_lock(vals)
        changed = self._l10n_de_euer_disposal_month_changed(vals)
        result = super().write(vals)
        if changed:
            # the stored yearly values of the disposals follow the setting
            self.env["l10n_de_euer.asset"].sudo().search(
                Domain("company_id", "in", changed.ids)
                & Domain("state", "=", "disposed")
            )._compute_years()
        return result

    def _l10n_de_euer_disposal_month_changed(self, vals):
        if "l10n_de_euer_disposal_month" not in vals:
            return self.browse()
        return self.filtered(
            lambda company: company.l10n_de_euer_disposal_month
            != bool(vals["l10n_de_euer_disposal_month"])
        )

    def _l10n_de_euer_check_asset_lock(self, vals):
        """The lock only grows; the month convention of disposals stays as
        long as a locked year has a disposal (spec 7.7)."""
        if "l10n_de_euer_asset_lock_year" in vals:
            for company in self:
                if (vals["l10n_de_euer_asset_lock_year"] or 0) < (
                    company.l10n_de_euer_asset_lock_year
                ):
                    raise UserError(
                        self.env._(
                            "The asset register of %(company)s is locked up to "
                            "%(year)s; the lock cannot be lifted.",
                            company=company.display_name,
                            year=company.l10n_de_euer_asset_lock_year,
                        )
                    )
        changed = self._l10n_de_euer_disposal_month_changed(vals)
        if not changed:
            return
        lines = (
            self.env["l10n_de_euer.asset.year"]
            .sudo()
            .search(
                Domain("company_id", "in", changed.ids)
                & Domain("locked", "=", True)
                & Domain("asset_id.state", "=", "disposed")
            )
        )
        disposals = lines.filtered(
            lambda line: line.asset_id._get_engine_asset().disposal_year == line.year
        )
        if disposals:
            raise UserError(
                self.env._(
                    "The locked year %(year)s has disposals (%(assets)s): the "
                    "month convention of disposals cannot change any more.",
                    year=max(disposals.mapped("year")),
                    assets=", ".join(disposals.asset_id.mapped("display_name")),
                )
            )
