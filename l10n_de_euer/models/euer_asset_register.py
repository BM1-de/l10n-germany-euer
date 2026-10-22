# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Asset register as a whole: field contributions, Anlage AVEÜR, ledger.

Interface of the asset register to the form calculation (phase F, spec 5.8)
and to the general ledger (spec 6.5):

- :meth:`_get_field_contributions` returns the amounts of the register per
  field code for a business and year. They go into the form calculation
  together with the amounts of the payment calculation; the transfers into
  Anlage EÜR (Kz 130, 131, 134, 135, 136, 137) are formulas of the form data.
- :meth:`_get_key_values` returns key fields (Kz 407).
- :meth:`_get_aveuer_details` returns the voluntary details per asset.
- :meth:`_get_ledger_comparison` compares bookings on depreciation accounts
  with the register (check C-68); such bookings are never realised from
  money, so booking depreciation in the ledger does not count twice.
- :meth:`_lock_years` freezes the years of a finalised statement.
"""

from collections import defaultdict
from decimal import Decimal

from odoo import api, models
from odoo.exceptions import UserError
from odoo.fields import Domain

from .engine import depreciation as dep
from .euer_asset import to_decimal

#: Roles of accounts whose additions need an entry in the register (C-64)
REGISTER_ROLES = ("av", "afa")


class L10nDeEuerAsset(models.Model):
    _inherit = "l10n_de_euer.asset"

    # ------------------------------------------------------------ register
    @api.model
    def _get_register_assets(self, company):
        """Assets of the business of ``company`` that are in the register."""
        return self.sudo().search(
            Domain("company_id", "=", company._l10n_de_euer_business_root().id)
            & Domain("state", "!=", "draft")
        )

    def _get_register_entry(self, year, include_private_use=True):
        """:class:`~.engine.depreciation.RegisterAsset` of the asset for ``year``."""
        self.ensure_one()
        private_use = (dep.ZERO, dep.ZERO)
        line = self.year_ids.filtered(lambda line: line.year == year)
        if include_private_use and line.private_use_decision == "confirmed":
            private_use = (
                to_decimal(line.private_use_confirmed_amount),
                to_decimal(line.private_use_confirmed_vat),
            )
        disposed = self.state == "disposed"
        withdrawal = disposed and self.disposal_kind == "withdrawal"
        locked = line.filtered(lambda line: line.locked and not line.is_opening)
        return dep.RegisterAsset(
            asset=self._get_engine_asset(),
            home_office_deductible=self.home_office_deductible,
            electric=self.is_electric_or_plugin_hybrid,
            disposal_value=to_decimal(self.disposal_value) if withdrawal else dep.ZERO,
            withdrawal_vat=to_decimal(self.disposal_vat_amount)
            if withdrawal
            else dep.ZERO,
            contribution_value=to_decimal(self.contribution_value)
            if self.origin == "contribution"
            else dep.ZERO,
            private_use=private_use,
            locked_values=locked._get_engine_values() if locked else None,
        )

    @api.model
    def _get_register_entries(
        self, company, year, include_private_use=True, exclude_private_use=()
    ):
        assets = self._get_register_assets(company)
        pools = (
            self.env["l10n_de_euer.asset.pool"]
            .sudo()
            .search(Domain("company_id", "=", company._l10n_de_euer_business_root().id))
        )
        excluded = set(getattr(exclude_private_use, "ids", exclude_private_use))
        entries = [
            asset._get_register_entry(
                year, include_private_use and asset.id not in excluded
            )
            for asset in assets
        ]
        return assets, entries, [pool._get_engine_pool() for pool in pools]

    @api.model
    def _get_field_contributions(
        self, company, year, include_private_use=True, exclude_private_use=()
    ):
        """Field contributions of the asset register (phase F, spec 5.8).

        :param company: company of the business (its main company is used)
        :param year: assessment year
        :param include_private_use: include confirmed use withdrawals of
            private use (Kz 108, 140, 122). A later profit correction that
            takes them over passes ``False`` to avoid counting them twice.
        :param exclude_private_use: assets (records or ids) whose private use
            a profit correction of the statement takes over: their confirmed
            use withdrawal is left out, the other assets keep theirs
        :return: dict field code → :class:`~decimal.Decimal`, input fields
            only (columns of Anlage AVEÜR, Kz 132, 100, 172/162, 102, 140,
            122, 123, 108); empty and zero values are left out
        """
        assets, entries, pools = self._get_register_entries(
            company, year, include_private_use, exclude_private_use
        )
        try:
            return dep.field_contributions(year, entries, pools)
        except dep.DepreciationError as error:
            culprits = self._get_error_assets(error, assets, entries, year)
            raise UserError(
                "\n".join(asset._engine_message(error) for asset in culprits)
            ) from error

    @api.model
    def _get_error_assets(self, error, assets, entries, year):
        """Assets that cause ``error`` of the field contributions of ``year``.

        For a group without depreciation the assets of the group with
        depreciation in the year; otherwise the assets whose own calculation
        fails. Falls back to all assets so the message is never empty.
        """
        culprits = self.browse()
        group = error.params.get("group")
        for asset, entry in zip(assets, entries, strict=True):
            try:
                values = dep.entry_values(entry, year)
            except dep.DepreciationError:
                culprits |= asset
                continue
            if (
                group
                and asset.aveuer_group == group
                and values is not None
                and (values.depreciation or values.special_depreciation)
            ):
                culprits |= asset
        return culprits or assets

    @api.model
    def _get_key_values(self, company, year):
        """Key fields of Anlage AVEÜR: Kz 407 (1 yes, 2 no) from 2025 on."""
        try:
            forms, _provisional = self.env["l10n_de_euer.form"]._get_forms_for_year(
                year
            )
        except UserError:
            return {}
        if dep.FIELD_ELECTRIC not in forms.field_ids.mapped("code"):
            return {}
        _assets, entries, _pools = self._get_register_entries(company, year)
        vehicles = [
            entry
            for entry, _values in dep.register_values(year, entries)
            if entry.asset.group == "vehicle" and entry.asset.kind in dep.AVEUER_KINDS
        ]
        if not vehicles:
            return {}
        present = dep.electric_vehicle_present(year, vehicles)
        return {dep.FIELD_ELECTRIC: "1" if present else "2"}

    @api.model
    def _get_aveuer_details(self, company, year):
        """Voluntary details per asset of Anlage AVEÜR (spec 6.4).

        Returns a list of dicts with the asset and its values of ``year``,
        ordered by group and delivery date.
        """
        assets, entries, _pools = self._get_register_entries(company, year)
        result = []
        for asset, entry in zip(assets, entries, strict=True):
            if entry.asset.kind not in dep.AVEUER_KINDS:
                continue
            values = dep.entry_values(entry, year)
            if values is None:
                continue
            result.append({"asset": asset, "group": asset.aveuer_group, **vars(values)})
        result.sort(key=lambda row: (row["group"], row["asset"].delivery_date))
        return result

    @api.model
    def _get_field_sources(self, company, year):
        """Assets and asset pools behind the field contributions of ``year``
        (drilldown, spec 7.6 no. 3).

        The contributions of the register add up per asset and pool, so the
        contributions of one asset alone show which fields it reaches.
        Returns ``{code: (assets, pools)}``.
        """
        statement = self.env["l10n_de_euer.statement"]._get_for_calculation(
            company._l10n_de_euer_business_root(), year
        )
        assets, entries, pools = self._get_register_entries(
            company, year, exclude_private_use=statement.adjustment_ids.asset_id
        )
        Pool = self.env["l10n_de_euer.asset.pool"].sudo()
        pool_records = Pool.search(
            Domain("company_id", "=", company._l10n_de_euer_business_root().id)
        )
        sources = defaultdict(lambda: (self.browse(), Pool.browse()))
        for asset, entry in zip(assets, entries, strict=True):
            try:
                codes = dep.field_contributions(year, [entry])
            except dep.DepreciationError:
                continue
            for code in codes:
                found, found_pools = sources[code]
                sources[code] = (found | asset, found_pools)
        for record, pool in zip(pool_records, pools, strict=True):
            for code in dep.field_contributions(year, [], [pool]):
                found, found_pools = sources[code]
                sources[code] = (found, found_pools | record)
        return dict(sources)

    @api.model
    def _action_open_field_sources(self, company, year, codes):
        """Yearly values of the assets (and the asset pools) behind ``codes``
        of ``year``: asset → yearly values → journal items (spec 7.6 no. 3)."""
        sources = self._get_field_sources(company, year)
        assets = self.browse()
        pools = self.env["l10n_de_euer.asset.pool"]
        for code in codes:
            found, found_pools = sources.get(code, (self.browse(), pools))
            assets |= found
            pools |= found_pools
        if pools and not assets:
            return {
                "type": "ir.actions.act_window",
                "domain": [("id", "in", pools.ids)],
                "name": self.env._("Asset Pools"),
                "res_model": "l10n_de_euer.asset.pool",
                "view_mode": "list,form",
            }
        years = assets.year_ids.filtered(lambda line: line.year == year)
        return {
            "type": "ir.actions.act_window",
            "domain": [("id", "in", years.ids)],
            "name": self.env._("Assets of %(year)s", year=year),
            "res_model": "l10n_de_euer.asset.year",
            "view_mode": "list,form",
            "context": {"create": False},
        }

    @api.model
    def _get_form_values(self, company, year, contributions=None):
        """Form values of ``year`` calculated from the register alone."""
        forms, _provisional = self.env["l10n_de_euer.form"]._get_forms_for_year(year)
        if contributions is None:
            contributions = self._get_field_contributions(company, year)
        return forms._calculate(contributions)

    # ------------------------------------------------------------ ledger
    @api.model
    def _get_ledger_comparison(self, company, year):
        """Bookings on depreciation accounts against the register (C-68).

        Returns ``{field code: (ledger amount, register amount)}`` for every
        field that has bookings on accounts of role depreciation in ``year``.
        """
        forms, _provisional = self.env["l10n_de_euer.form"]._get_forms_for_year(year)
        euer = forms.filtered(lambda form: form.form_type == "euer")
        checker = self.env["l10n_de_euer.assignment.checker"]
        context = checker._prepare(company, year)
        ledger = defaultdict(Decimal)
        for _account, usages in context["segments"].items():
            for assignment, usage in usages.items():
                category = assignment.category_id
                if category.role != "afa" or not usage["count"]:
                    continue
                mapping = category._get_mappings(euer)[:1]
                code = mapping.field_id.code or category.code
                ledger[code] += to_decimal(usage["balance"])
        if not ledger:
            return {}
        values = self._get_form_values(company, year)
        return {
            code: (amount, values.get(code) or dep.ZERO)
            for code, amount in ledger.items()
        }

    @api.model
    def _get_register_lines(self, company, year):
        """Posted lines of ``year`` on accounts with role fixed asset or
        depreciation that add to the register (check C-64).

        Left out are bookings that consist only of such lines (depreciation or
        disposals booked in the ledger, 5.5.1 d) and opening entries.
        """
        # assignments belong to the main company, the lines to the business
        root = company.sudo().root_id
        Assignment = self.env["l10n_de_euer.account.assignment"].sudo()
        assignments = Assignment.search(
            Domain("company_id", "=", root.id)
            & Domain("category_id.role", "in", REGISTER_ROLES)
        )
        if not assignments:
            return self.env["account.move.line"]
        companies = company._l10n_de_euer_business_companies().sudo()
        lines = (
            self.env["account.move.line"]
            .sudo()
            .search(
                Domain("company_id", "in", companies.ids)
                & Domain("parent_state", "=", "posted")
                & Domain("date", ">=", f"{year}-01-01")
                & Domain("date", "<=", f"{year}-12-31")
                & Domain("account_id", "in", assignments.account_id.ids)
                & Domain("balance", ">", 0)
            )
        )
        periods = defaultdict(list)
        for assignment in assignments:
            periods[assignment.account_id].append(assignment)
        opening_roles = self._get_opening_accounts(root)

        def valid(line):
            return any(
                (not item.date_from or item.date_from <= line.date)
                and (not item.date_to or item.date_to >= line.date)
                for item in periods[line.account_id]
            )

        def addition(line):
            move = line.move_id
            if move in companies.account_opening_move_id or (
                move.line_ids.account_id & opening_roles
            ):
                return False
            return bool(move.line_ids.account_id - assignments.account_id)

        return lines.filtered(lambda line: valid(line) and addition(line))

    @api.model
    def _get_opening_accounts(self, root):
        return (
            self.env["l10n_de_euer.account.assignment"]
            .sudo()
            .search(
                Domain("company_id", "=", root.id)
                & Domain("category_id.role", "=", "eroeffnung")
            )
            .account_id
        )

    # ------------------------------------------------------------ locking
    @api.model
    def _lock_years(self, company, year):
        """Freeze the asset years and pools of the business up to ``year``.

        Called when the statement of ``year`` is finalised (E4, spec 7.7):
        locked years can no longer be changed, also not by a later change of
        the asset or one of its events, nor by assets entered later (the
        business keeps the year in ``l10n_de_euer_asset_lock_year``).
        """
        assets = self._get_register_assets(company)
        # lines up to the year also for assets that only fill the cost column
        # there, so a later change in an open year never needs a locked line
        assets._compute_years(until=year)
        years = assets.year_ids.filtered(lambda line: line.year <= year)
        years.filtered(lambda line: not line.locked)._write_internal({"locked": True})
        pools = (
            self.env["l10n_de_euer.asset.pool"]
            .sudo()
            .search(
                Domain("company_id", "=", company._l10n_de_euer_business_root().id)
                & Domain("year", "<=", year)
                & Domain("locked", "=", False)
            )
        )
        for pool in pools:
            pool._write_internal({"locked": True, "locked_amount": pool.amount})
        business = company._l10n_de_euer_business_root().sudo()
        if year > business.l10n_de_euer_asset_lock_year:
            business.l10n_de_euer_asset_lock_year = year
        return True
