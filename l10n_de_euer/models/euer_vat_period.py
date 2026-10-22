# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.fields import Domain

from .euer_move_info import REGIME_FIELDS


class L10nDeEuerVatPeriod(models.Model):
    """VAT status period of a business: small business or standard (spec 7.3).

    The VAT treatment is never derived from taxes or tax tags (K-06).
    """

    _name = "l10n_de_euer.vat.period"
    _inherit = ["mail.thread"]
    _description = "EÜR VAT Status Period"
    _order = "company_id, date_from desc"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        default=lambda self: self.env.company.root_id,
        domain="[('parent_id', '=', False)]",
        tracking=True,
        help="Business (main company with its branches).",
    )
    date_from = fields.Date(required=True, tracking=True)
    date_to = fields.Date(tracking=True, help="Empty: valid until changed.")
    regime = fields.Selection(
        [("ku", "Small Business (§ 19 UStG)"), ("regular", "Standard Taxation")],
        string="VAT Treatment",
        required=True,
        tracking=True,
    )
    basis = fields.Selection(
        [
            ("supply_date", "Date of Supply"),
            ("receipt_date", "Date of Receipt (Threshold Exceeded)"),
        ],
        required=True,
        default="supply_date",
        tracking=True,
        help="Date of supply: the treatment follows the supply date of each sale. "
        "Date of receipt: the period starts with the payment that exceeds the "
        "small business threshold.",
    )
    vat_return_period = fields.Selection(
        [("monthly", "Monthly"), ("quarterly", "Quarterly"), ("annual", "Annual")],
        string="VAT Return Period",
        tracking=True,
    )
    permanent_extension = fields.Boolean(
        tracking=True,
        help="Dauerfristverlängerung: VAT returns are due one month later.",
    )
    prev_year_turnover = fields.Monetary(
        string="Turnover of the Previous Year",
        currency_field="currency_id",
        tracking=True,
        help="Input after a takeover from another system: the total turnover of "
        "the year before the year entered next to it (the first year in Odoo), "
        "used for the small business thresholds. Later years take the turnover "
        "from the data. A business founded in the year has no previous year: "
        "enter the start of the activity at the company instead.",
    )
    prev_year_turnover_year = fields.Integer(
        string="For the Year",
        tracking=True,
        help="Year whose small business check takes the turnover of the "
        "previous year entered here, normally the first year in Odoo (for 2025 "
        "enter the turnover of 2024). Empty: the first year of the period. A "
        "small business year without bookings of the previous year in Odoo and "
        "without this input is reported (C-32).",
    )
    currency_id = fields.Many2one(related="company_id.currency_id")
    reason = fields.Text(tracking=True)

    _date_range = models.Constraint(
        "CHECK(date_to IS NULL OR date_from <= date_to)",
        "The start of a VAT status period must not be after its end.",
    )

    @api.depends("regime", "date_from", "date_to")
    def _compute_display_name(self):
        labels = dict(self._fields["regime"]._description_selection(self.env))
        for period in self:
            end = period.date_to or "…"
            period.display_name = (
                f"{labels.get(period.regime, '')} {period.date_from or ''} – {end}"
            )

    @api.constrains("company_id", "date_from", "date_to")
    def _check_overlap(self):
        for period in self:
            domain = (
                Domain("company_id", "=", period.company_id.id)
                & Domain("id", "!=", period.id)
                & (
                    Domain("date_to", "=", False)
                    | Domain("date_to", ">=", period.date_from)
                )
            )
            if period.date_to:
                domain &= Domain("date_from", "<=", period.date_to)
            if self.search_count(domain, limit=1):
                raise ValidationError(
                    self.env._(
                        "VAT status periods of %(company)s must not overlap.",
                        company=period.company_id.name,
                    )
                )

    @api.model_create_multi
    def create(self, vals_list):
        periods = super().create(vals_list)
        periods._recompute_move_regimes(periods.company_id)
        return periods

    def write(self, vals):
        companies = self.company_id
        result = super().write(vals)
        if any(name in vals for name in REGIME_FIELDS):
            self._recompute_move_regimes(companies | self.company_id)
        return result

    def unlink(self):
        companies = self.company_id
        result = super().unlink()
        self.browse()._recompute_move_regimes(companies)
        return result

    def _recompute_move_regimes(self, companies):
        """VAT treatment of the documents after a change of the periods
        (UT-KU-8). The change is logged once on the changed periods (after a
        deletion on the remaining periods of the business), not on every
        document."""
        if not companies:
            return
        changed = self.env["l10n_de_euer.move.info"]._recompute_regimes(companies)
        if not changed:
            return
        periods = self.exists() or self.search(
            Domain("company_id", "in", companies.root_id.ids)
        )
        days = [changed._regime_date(info.move_id) for info in changed.sudo()]
        body = self.env._(
            "The VAT treatment of %(count)s documents was recomputed (dates of "
            "supply %(first)s to %(last)s).",
            count=len(changed),
            first=min(days),
            last=max(days),
        )
        for period in periods:
            period.message_post(body=body)

    @api.model
    def _get_regime(self, company, on_date):
        """Return ``ku``, ``regular`` or ``False`` for a company on a date."""
        period = self.search(
            Domain("company_id", "=", company.root_id.id)
            & Domain("date_from", "<=", on_date)
            & (Domain("date_to", "=", False) | Domain("date_to", ">=", on_date)),
            limit=1,
        )
        return period.regime or False
