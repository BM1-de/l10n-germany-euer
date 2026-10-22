# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from markupsafe import Markup, escape

from odoo import api, fields, models
from odoo.exceptions import UserError

from .engine import depreciation as dep
from .euer_asset import to_decimal

#: context key of the register: only it creates pools and changes their
#: year, business, members and lock, always as superuser (a client cannot set
#: ``env.su`` through RPC)
INTERNAL = "l10n_de_euer_asset_pool_internal"
#: fields only the register writes
PROTECTED_FIELDS = frozenset(
    {"year", "company_id", "asset_ids", "locked", "locked_amount"}
)


class L10nDeEuerAssetPool(models.Model):
    """Asset pool of one year of formation (§ 6 (2a) EStG, spec 6.2).

    The pool holds the assets of one business with a net cost above 250 € up
    to 1,000 € delivered in its year. It is released by one fifth per year
    without regard to the month of delivery; the fifth year takes the
    remainder. A disposal of a pool asset does not reduce the pool.
    """

    _name = "l10n_de_euer.asset.pool"
    _description = "EÜR Asset Pool"
    _order = "year desc"
    _check_company_auto = True

    name = fields.Char(compute="_compute_name", store=True)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        default=lambda self: self.env.company._l10n_de_euer_business_root(),
    )
    currency_id = fields.Many2one(related="company_id.currency_id")
    year = fields.Integer(string="Year of Formation", required=True, index=True)
    asset_ids = fields.One2many(
        "l10n_de_euer.asset", "pool_id", string="Assets", readonly=True
    )
    amount = fields.Monetary(
        compute="_compute_amount",
        store=True,
        help="Sum of the cost of the pool assets (gross for small businesses).",
    )
    release_amount = fields.Monetary(
        string="Yearly Release", compute="_compute_releases"
    )
    last_release_amount = fields.Monetary(
        string="Release in the Fifth Year", compute="_compute_releases"
    )
    end_year = fields.Integer(compute="_compute_releases")
    schedule_html = fields.Html(
        string="Releases", compute="_compute_releases", sanitize=False
    )
    locked = fields.Boolean(
        readonly=True,
        copy=False,
        help="Set when the statement of the year of formation is finalised.",
    )
    locked_amount = fields.Monetary(readonly=True, copy=False)

    _company_year_uniq = models.Constraint(
        "UNIQUE(company_id, year)", "There is one asset pool per business and year."
    )

    @api.depends("year")
    def _compute_name(self):
        for pool in self:
            pool.name = self.env._("Asset Pool %(year)s", year=pool.year)

    @api.depends("asset_ids.acquisition_cost", "asset_ids.state")
    def _compute_amount(self):
        for pool in self:
            assets = pool.asset_ids.filtered(lambda asset: asset.state != "draft")
            pool.amount = float(
                sum((to_decimal(a.acquisition_cost) for a in assets), dep.ZERO)
            )

    @api.depends("amount", "year")
    def _compute_releases(self):
        for pool in self:
            releases = dep.pool_releases(to_decimal(pool.amount))
            pool.release_amount = float(releases[0])
            pool.last_release_amount = float(releases[-1])
            pool.end_year = pool.year + dep.POOL_YEARS - 1
            pool.schedule_html = pool._schedule_html(releases)

    def _schedule_html(self, releases):
        rows = []
        remaining = to_decimal(self.amount)
        for offset, release in enumerate(releases):
            remaining -= release
            rows.append(
                Markup(
                    "<tr><th scope='row'>%s</th><td class='text-end'>%s</td>"
                    "<td class='text-end'>%s</td></tr>"
                )
                % (self.year + offset, f"{release:.2f}", f"{remaining:.2f}")
            )
        caption = escape(self.env._("Releases of the asset pool"))
        head = Markup(
            "<thead><tr><th scope='col'>%s</th><th scope='col'>%s</th>"
            "<th scope='col'>%s</th></tr></thead>"
        ) % (
            self.env._("Year"),
            self.env._("Release"),
            self.env._("Book Value at End"),
        )
        return (
            Markup("<table class='table table-sm'><caption>%s</caption>") % caption
            + head
            + Markup("<tbody>")
            + Markup("").join(rows)
            + Markup("</tbody></table>")
        )

    def _get_engine_pool(self):
        """A locked pool keeps the amount of its finalised statement."""
        self.ensure_one()
        amount = self.locked_amount if self.locked else self.amount
        return dep.Pool(year=self.year, amount=to_decimal(amount))

    # ------------------------------------------------------------ protection
    @api.model
    def _is_internal(self):
        return self.env.su and bool(self.env.context.get(INTERNAL))

    @api.model
    def _create_internal(self, values):
        return self.sudo().with_context(**{INTERNAL: True}).create(values)

    def _write_internal(self, values):
        return self.sudo().with_context(**{INTERNAL: True}).write(values)

    @api.model_create_multi
    def create(self, vals_list):
        if not self._is_internal():
            raise UserError(self.env._("Asset pools are formed by the register."))
        return super().create(vals_list)

    def write(self, vals):
        """Like a locked year (spec 7.7): never unlocked, also not by an
        administrator; a locked pool does not change at all."""
        locked = self.filtered("locked")
        if locked and "locked" in vals and not vals["locked"]:
            raise UserError(self.env._("A locked asset pool cannot be unlocked."))
        if PROTECTED_FIELDS.intersection(vals) and not self._is_internal():
            raise UserError(self.env._("Asset pools are formed by the register."))
        if locked and set(vals) - {"locked"}:
            raise UserError(
                self.env._(
                    "%(pool)s is locked and cannot change.",
                    pool=", ".join(locked.mapped("name")),
                )
            )
        return super().write(vals)

    def _check_amount_unchanged(self):
        """A locked pool keeps its amount (no new members, no removals)."""
        for pool in self.filtered("locked"):
            assets = pool.asset_ids.filtered(lambda asset: asset.state != "draft")
            amount = sum((to_decimal(a.acquisition_cost) for a in assets), dep.ZERO)
            if amount != to_decimal(pool.locked_amount):
                raise UserError(
                    self.env._("%(pool)s is locked and cannot change.", pool=pool.name)
                )

    @api.ondelete(at_uninstall=False)
    def _unlink_except_used(self):
        for pool in self:
            if pool.locked or pool.asset_ids:
                raise UserError(
                    self.env._(
                        "%(pool)s holds assets and cannot be deleted.", pool=pool.name
                    )
                )
