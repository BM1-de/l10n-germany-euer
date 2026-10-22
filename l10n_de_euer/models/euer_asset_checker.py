# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import datetime
from collections import defaultdict

from odoo import api, fields, models
from odoo.fields import Domain

from .engine import depreciation as dep
from .euer_asset import MIN_BUSINESS_SHARE, to_decimal

#: Assets whose sale is an expense at the receipt of the proceeds
CASH_SALE_KINDS = dep.CASH_DISPOSAL_KINDS


class L10nDeEuerCheck(models.Model):
    _inherit = "l10n_de_euer.check"

    origin = fields.Selection(
        selection_add=[("asset", "Asset Register")], ondelete={"asset": "cascade"}
    )

    @api.model
    def _run_asset_checks(self, company, year, form_values=None):
        """Run the checks of the asset register and store the results.

        :param form_values: final form values of the statement, see
            :meth:`L10nDeEuerAssetChecker._run`
        """
        self.check_access("write")
        results = self.env["l10n_de_euer.asset.checker"]._run(
            company, year, form_values
        )
        self._store_results(company, year, "asset", results)
        return self.search(
            Domain("company_id", "=", company._l10n_de_euer_business_root().id)
            & Domain("year", "=", year)
            & Domain("origin", "=", "asset")
        )


class L10nDeEuerAssetChecker(models.AbstractModel):
    """Checks of the asset register (spec 6.4, 7.8).

    C-30 (input tax treatment against the VAT status), C-40 (outside the
    program scope: reduction under § 7g (2) EStG, § 7g (6) not met, use for
    other income before a contribution, subsidy before the year of delivery,
    sale against instalments), C-42 (§ 15a UStG), C-61 (AVEÜR carried
    forward, after a takeover against the filed AVEÜR), C-62 (transfers
    AVEÜR → EÜR), C-63 (memo value), C-64 (addition without register entry),
    C-65 (business share below 10 %), C-66 (choice of the asset pool not
    uniform), C-67 (depreciation over the life ≠ cost), C-68 (ledger ≠
    register: depreciation accounts, journal items of the purchase, payment
    of the sale), C-69 (use withdrawal not decided, private use of a vehicle).
    """

    _name = "l10n_de_euer.asset.checker"
    _description = "EÜR Asset Register Checks"

    @api.model
    def _run(self, company, year, form_values=None):
        """Results of the checks of the asset register for ``year``.

        :param form_values: final form values of the statement (phase G with
            the amounts of the payment calculation and the overrides),
            ``code → Decimal``. C-62 compares the transfers AVEÜR → EÜR in
            them; without them it can only check the form values of the
            register alone, which match by construction of the formulas.
        """
        context = self._prepare(company, year)
        context["form_values"] = form_values
        results = []
        for check in (
            self._check_c30,
            self._check_c40,
            self._check_c42,
            self._check_c61,
            self._check_c62,
            self._check_c63,
            self._check_c64,
            self._check_c65,
            self._check_c66,
            self._check_c67,
            self._check_c68,
            self._check_c69,
        ):
            results += check(context)
        return results

    @api.model
    def _prepare(self, company, year):
        Asset = self.env["l10n_de_euer.asset"]
        assets, entries, pools = Asset._get_register_entries(company, year)
        present = {}
        errors = []
        kept, kept_entries = Asset.browse(), []
        for asset, entry in zip(assets, entries, strict=True):
            try:
                values = dep.entry_values(entry, year)
                dep.entry_values(entry, year - 1)
            except dep.DepreciationError as error:
                # reported by C-62; the other checks run without the asset
                errors.append((asset, error))
                continue
            kept |= asset
            kept_entries.append(entry)
            if values is not None:
                present[asset] = values
        return {
            "company": company._l10n_de_euer_business_root(),
            "year": year,
            "assets": kept,
            "entries": kept_entries,
            "pools": pools,
            "present": present,
            "errors": errors,
        }

    def _result(self, code, severity, message, records, amount=0.0):
        return {
            "code": code,
            "severity": severity,
            "message": message,
            "res_model": records._name,
            "res_ids": sorted(records.ids),
            "amount": float(amount),
        }

    # ------------------------------------------------------------ C-30
    def _check_c30(self, context):
        """Input tax treatment against the VAT status on the delivery date."""
        company, year = context["company"], context["year"]
        results = []
        for asset in context["assets"]:
            if asset.delivery_date.year != year or not asset.input_tax:
                continue
            if asset.origin not in ("purchase", "production"):
                continue
            regime = company._l10n_de_euer_get_vat_regime(asset.delivery_date)
            if not regime:
                message = self.env._(
                    "%(asset)s: no VAT status period on the delivery date "
                    "%(date)s; the input tax is treated as %(treatment)s. Enter "
                    "the VAT status periods of the business.",
                    asset=asset.display_name,
                    date=asset.delivery_date,
                    treatment=dict(
                        asset._fields["vat_treatment"]._description_selection(self.env)
                    ).get(asset.vat_treatment, asset.vat_treatment),
                )
            elif regime == "ku" and asset.vat_treatment != "not_deductible":
                message = self.env._(
                    "%(asset)s: small business on %(date)s, but the input tax is "
                    "treated as deductible; it is part of the cost (R 9b (1) "
                    "EStR).",
                    asset=asset.display_name,
                    date=asset.delivery_date,
                )
            else:
                continue
            results.append(
                self._result(
                    "C-30", "warning", message, asset, to_decimal(asset.input_tax)
                )
            )
        return results

    # ------------------------------------------------------------ C-40
    def _check_c40(self, context):
        return (
            self._check_c40_reduction(context)
            + self._check_c40_sec7g(context)
            + self._check_c40_contribution(context)
            + self._check_c40_subsidy(context)
            + self._check_c40_instalments(context)
        )

    def _check_c40_sec7g(self, context):
        """§ 7g (6) no. 2 EStG not met: the special depreciation of the year
        of delivery and the following year has to be reversed (§ 7g (4))."""
        year = context["year"]
        results = []
        for asset in context["assets"]:
            specials = asset.event_ids.filtered(lambda e: e.type == "sec7g_special")
            if not specials:
                continue
            years = {e.date.year for e in specials} | {asset.delivery_date.year + 1}
            issues = asset._get_sec7g_issues()
            if year in years and issues:
                results.append(
                    self._result(
                        "C-40",
                        "blocker",
                        self.env._(
                            "%(asset)s: the special depreciation under § 7g (5) "
                            "EStG is not allowed: %(issues)s. Remove it; if its "
                            "year is finalised, correct that statement (§ 7g (4) "
                            "EStG).",
                            asset=asset.display_name,
                            issues="; ".join(issues),
                        ),
                        asset,
                        sum(to_decimal(e.amount) for e in specials),
                    )
                )
        return results

    def _check_c40_contribution(self, context):
        """§ 7 (1) sentence 5 EStG: contribution after use for other income."""
        return [
            self._result(
                "C-40",
                "blocker",
                self.env._(
                    "%(asset)s was used to earn other income before the "
                    "contribution: the basis of the depreciation is reduced by "
                    "the depreciation taken there (§ 7 (1) sentence 5 EStG). This "
                    "case is outside the program scope.",
                    asset=asset.display_name,
                ),
                asset,
                to_decimal(asset.contribution_value),
            )
            for asset in context["present"]
            if asset.origin == "contribution" and asset.contribution_prior_income_use
        ]

    def _check_c40_subsidy(self, context):
        """Subsidy approved in a year before the delivery (R 6.5 (4) EStR)."""
        year = context["year"]
        results = []
        for asset in context["assets"]:
            if asset.delivery_date.year != year:
                continue
            early = asset.event_ids.filtered(
                lambda e, a=asset: e.type == "subsidy"
                and e.date.year < a.delivery_date.year
            )
            if early:
                results.append(
                    self._result(
                        "C-40",
                        "blocker",
                        self.env._(
                            "%(asset)s: subsidy approved in %(year)s, before the "
                            "year of delivery. A subsidy before the acquisition "
                            "(R 6.5 (4) EStR) is outside the program scope.",
                            asset=asset.display_name,
                            year=min(e.date.year for e in early),
                        ),
                        asset,
                        sum(to_decimal(e.amount) for e in early),
                    )
                )
        return results

    def _check_c40_instalments(self, context):
        """Sale of an asset under § 4 (3) sentence 4 EStG paid in instalments
        (R 4.5 (5) EStR): part of the cost is an expense in each year of a
        payment. Not supported: the register expenses the cost at once."""
        year = context["year"]
        results = []
        for asset in context["assets"]:
            payments = asset._get_disposal_payments()
            if (
                asset.asset_kind not in CASH_SALE_KINDS
                or asset.disposal_kind != "sale"
                or not payments
            ):
                continue
            years = {day.year for day, _amount in payments}
            receipt = asset.disposal_receipt_date
            if receipt:
                years.add(receipt.year)
            open_rest = asset.disposal_invoice_id.amount_residual
            several = len({day.year for day, _amount in payments}) > 1
            if year in years and (several or open_rest):
                results.append(
                    self._result(
                        "C-40",
                        "blocker",
                        self.env._(
                            "%(asset)s: the sales invoice is paid in instalments "
                            "or only in part. The cost of an asset under § 4 (3) "
                            "sentence 4 EStG is then an expense in the amount of "
                            "each payment (R 4.5 (5) EStR); this is outside the "
                            "program scope.",
                            asset=asset.display_name,
                        ),
                        asset,
                        to_decimal(asset.acquisition_cost),
                    )
                )
        return results

    def _check_c40_reduction(self, context):
        events = context["assets"].event_ids.filtered(
            lambda event: event.type == "sec7g_reduction"
            and event.date.year == context["year"]
        )
        return [
            self._result(
                "C-40",
                "blocker",
                self.env._(
                    "%(asset)s: the reduction under § 7g (2) sentence 3 EStG "
                    "(investment deduction) is outside the program scope.",
                    asset=event.asset_id.display_name,
                ),
                event.asset_id,
                to_decimal(event.amount),
            )
            for event in events
        ]

    # ------------------------------------------------------------ C-42
    def _check_c42(self, context):
        """Change of the VAT treatment with assets in the § 15a period."""
        company, year = context["company"], context["year"]
        changes = self._regime_changes(company, year)
        results = []
        for change_date in changes:
            for asset in context["assets"]:
                text = self._sec15a_text(asset, change_date)
                if text:
                    results.append(
                        self._result(
                            "C-42",
                            "warning",
                            self.env._(
                                "%(asset)s: check the input tax correction under "
                                "§ 15a UStG.\n%(help)s",
                                asset=asset.display_name,
                                help=text,
                            ),
                            asset,
                            to_decimal(asset.input_tax),
                        )
                    )
        return results

    def _regime_changes(self, company, year):
        periods = (
            self.env["l10n_de_euer.vat.period"]
            .sudo()
            .search(
                Domain("company_id", "=", company.id)
                & Domain("date_from", ">=", f"{year}-01-01")
                & Domain("date_from", "<=", f"{year}-12-31")
            )
        )
        changes = []
        for period in periods:
            before = company._l10n_de_euer_get_vat_regime(
                period.date_from - datetime.timedelta(days=1)
            )
            if before and before != period.regime:
                changes.append(period.date_from)
        return changes

    def _sec15a_text(self, asset, change_date):
        if not asset.input_tax or asset.asset_kind not in dep.AVEUER_KINDS:
            return False
        if asset.delivery_date >= change_date:
            return False
        if asset.state == "disposed" and asset.disposal_date < change_date:
            return False
        return asset._get_sec15a_text(change_date)

    # ------------------------------------------------------------ C-61
    def _check_c61(self, context):
        """Book value at the end of the previous year = at the beginning.

        After a takeover the previous year is the Anlage AVEÜR filed with the
        previous system, if its book values per group were entered (U-05).
        """
        year = context["year"]
        previous = defaultdict(lambda: dep.ZERO)
        current = defaultdict(lambda: dep.ZERO)
        records = defaultdict(lambda: self.env["l10n_de_euer.asset"])
        filed = (
            self.env["l10n_de_euer.asset.opening.total"]
            .sudo()
            .search(
                Domain("company_id", "=", context["company"].id)
                & Domain("year", "=", year - 1)
            )
        )
        for total in filed:
            previous[total.aveuer_group] += to_decimal(total.book_value_end)
        for asset, entry in zip(context["assets"], context["entries"], strict=True):
            if entry.asset.kind not in dep.AVEUER_KINDS:
                continue
            stored = asset.year_ids.filtered(lambda line: line.year == year - 1)
            if filed:
                records[asset.aveuer_group] |= asset
            elif stored:
                previous[stored.aveuer_group] += to_decimal(stored.book_value_end)
                records[stored.aveuer_group] |= asset
            else:
                values = dep.entry_values(entry, year - 1)
                if values is not None:
                    previous[asset.aveuer_group] += values.book_value_end
                    records[asset.aveuer_group] |= asset
            values = context["present"].get(asset)
            if values is not None:
                current[asset.aveuer_group] += values.book_value_start
                records[asset.aveuer_group] |= asset
        groups = dict(self.env["l10n_de_euer.asset"]._fields["aveuer_group"].selection)
        results = []
        for group in sorted(set(previous) | set(current)):
            if previous[group] != current[group]:
                results.append(
                    self._result(
                        "C-61",
                        "blocker",
                        self.env._(
                            "%(group)s: the book value at the end of %(prev)s "
                            "(%(end).2f) differs from the book value at the "
                            "beginning of %(year)s (%(start).2f).",
                            group=groups.get(group, group),
                            prev=year - 1,
                            end=previous[group],
                            start=current[group],
                            year=year,
                        ),
                        records[group],
                        current[group] - previous[group],
                    )
                )
        return results

    # ------------------------------------------------------------ C-62
    def _check_c62(self, context):
        """Transfers AVEÜR → EÜR (spec F-04) and assets that cannot be calculated.

        With the final form values of the statement (``form_values``) the
        transfers are checked as filed, including overrides. Kz 100 and
        Kz 172/162 also hold payments (goods, rooms): they must be at least
        the amounts of Anlage AVEÜR (Kz 605, Kz 214).
        """
        company, year = context["company"], context["year"]
        Asset = self.env["l10n_de_euer.asset"]
        results = [
            self._result(
                "C-62",
                "blocker",
                self.env._(
                    "%(error)s The asset cannot be entered in Anlage AVEÜR.",
                    error=asset._engine_message(error),
                ),
                asset,
            )
            for asset, error in context["errors"]
        ]
        Form = self.env["l10n_de_euer.form"]
        if not Form.search_count(Domain("year", "<=", year), limit=1):
            return results
        try:
            contributions = dep.field_contributions(
                year, context["entries"], context["pools"]
            )
        except dep.DepreciationError as error:
            culprits = Asset._get_error_assets(
                error, context["assets"], context["entries"], year
            )
            return results + [
                self._result(
                    "C-62",
                    "blocker",
                    "\n".join(asset._engine_message(error) for asset in culprits),
                    culprits,
                )
            ]
        values = context.get("form_values")
        if values is None:
            values = Asset._get_form_values(company, year, contributions)
        differences = dep.transfer_differences(values)
        for codes, source in (
            ((dep.FIELD_GOODS,), "AVEUER.40.605"),
            ((dep.FIELD_HOME_OFFICE, dep.FIELD_HOME_OFFICE_ND), "AVEUER.40.214"),
        ):
            total = sum((values.get(code) or dep.ZERO for code in codes), dep.ZERO)
            expected = values.get(source) or dep.ZERO
            if total < expected:
                differences[codes[0]] = (total, expected)
        return results + [
            self._result(
                "C-62",
                "blocker",
                self.env._(
                    "Transfer to %(code)s (%(value).2f) differs from Anlage AVEÜR "
                    "(%(aveuer).2f).",
                    code=code,
                    value=target,
                    aveuer=aveuer,
                ),
                context["assets"],
                target - aveuer,
            )
            for code, (target, aveuer) in sorted(differences.items())
        ]

    # ------------------------------------------------------------ C-63
    def _check_c63(self, context):
        assets = [
            asset for asset in context["present"] if asset.memo_value and asset.origin
        ]
        return [
            self._result(
                "C-63",
                "warning",
                self.env._(
                    "%(asset)s keeps the memo value %(value).2f of the previous "
                    "system; by law the full cost is depreciated (R 7.4 (3) EStR).",
                    asset=asset.display_name,
                    value=asset.memo_value,
                ),
                asset,
                to_decimal(asset.memo_value),
            )
            for asset in assets
        ]

    # ------------------------------------------------------------ C-64
    def _check_c64(self, context):
        company, year = context["company"], context["year"]
        Asset = self.env["l10n_de_euer.asset"]
        lines = Asset._get_register_lines(company, year)
        if not lines:
            return []
        linked = (
            Asset.sudo()
            .search(
                Domain("move_line_ids", "in", lines.ids)
                & Domain("state", "!=", "draft")
            )
            .move_line_ids
        )
        missing = lines - linked
        results = []
        for account in missing.account_id:
            account_lines = missing.filtered(
                lambda line, a=account: line.account_id == a
            )
            results.append(
                self._result(
                    "C-64",
                    "blocker",
                    self.env._(
                        "%(count)s addition(s) on %(account)s without entry in the "
                        "asset register; create the asset from the journal item.",
                        count=len(account_lines),
                        account=account.display_name,
                    ),
                    account_lines,
                    sum(to_decimal(line.balance) for line in account_lines),
                )
            )
        return results

    # ------------------------------------------------------------ C-65
    def _check_c65(self, context):
        results = []
        for asset in context["present"]:
            line = asset.year_ids.filtered(lambda line: line.year == context["year"])
            share = line.business_share if line else asset.business_share
            if share < MIN_BUSINESS_SHARE:
                results.append(
                    self._result(
                        "C-65",
                        "warning",
                        self.env._(
                            "%(asset)s is used for business by %(share).0f %% only: "
                            "below 10 %% it is private property (R 4.2 (1) EStR).",
                            asset=asset.display_name,
                            share=share * 100,
                        ),
                        asset,
                    )
                )
        return results

    # ------------------------------------------------------------ C-66
    def _check_c66(self, context):
        """§ 6 (2a) sentence 5 EStG: in a year with an asset pool every movable
        asset above 250 € up to 1,000 € goes into the pool."""
        year = context["year"]
        delivered = context["assets"].filtered(
            lambda asset: asset.delivery_date.year == year and not asset.opening_date
        )
        pool = delivered.filtered(lambda asset: asset.asset_kind == "pool")
        if not pool:
            return []
        gwg = delivered.filtered(
            lambda asset: asset.asset_kind == "gwg" and asset.in_gwg_register
        )
        single = delivered.filtered(self._belongs_into_pool)
        results = []
        if single:
            results.append(
                self._result(
                    "C-66",
                    "blocker",
                    self.env._(
                        "%(year)s has an asset pool: every movable, independently "
                        "usable asset above 250 € up to 1,000 € net of the year "
                        "goes into the pool (§ 6 (2a) sentence 5 EStG), also "
                        "%(assets)s.",
                        year=year,
                        assets=", ".join(single.mapped("display_name")),
                    ),
                    pool | single,
                    sum(to_decimal(asset.acquisition_cost) for asset in single),
                )
            )
        if not gwg:
            return results
        return results + [
            self._result(
                "C-66",
                "blocker",
                self.env._(
                    "%(year)s has an asset pool and low-value assets above 250 € "
                    "net. The choice applies to all additions of the year "
                    "(§ 6 (2a) sentence 5 EStG): in a pool year low-value assets "
                    "are allowed up to 250 € only.",
                    year=year,
                ),
                pool | gwg,
                sum(to_decimal(asset.acquisition_cost) for asset in gwg),
            )
        ]

    def _belongs_into_pool(self, asset):
        """A depreciable asset within the limits of the asset pool."""
        if asset.asset_kind != "depreciable" or not asset.independent_use:
            return False
        if asset.aveuer_group not in dep.MOVABLE_GROUPS and not (
            asset.aveuer_group == "intangible" and asset.is_trivial_program
        ):
            return False
        currency = asset.currency_id
        lower = asset._get_parameter("pool_min")
        upper = asset._get_parameter("pool_max")
        return (
            currency.compare_amounts(asset.limit_value, lower) > 0
            and currency.compare_amounts(asset.limit_value, upper) <= 0
        )

    # ------------------------------------------------------------ C-67
    def _check_c67(self, context):
        year = context["year"]
        results = []
        for asset, values in context["present"].items():
            if asset.asset_kind != "depreciable" or asset.state == "disposed":
                continue
            if values.depreciation + values.special_depreciation == 0:
                continue
            if values.book_value_end != to_decimal(asset.memo_value):
                continue
            lines = asset.year_ids.filtered(
                lambda line: not line.is_opening and line.year <= year
            )
            rows = [line._get_engine_values() for line in lines]
            engine_asset = asset._get_engine_asset()
            cost_total = to_decimal(asset.acquisition_cost) + sum(
                (
                    event.amount if event.type == "additional_cost" else -event.amount
                    for event in engine_asset.events
                    if event.type in dep.BASIS_EVENTS
                ),
                dep.ZERO,
            )
            accumulated = (
                to_decimal(asset.opening_accumulated)
                if asset.opening_date
                else dep.ZERO
            )
            if not dep.total_check(
                rows, cost_total, to_decimal(asset.memo_value), accumulated
            ):
                results.append(
                    self._result(
                        "C-67",
                        "blocker",
                        self.env._(
                            "%(asset)s: the depreciation over the useful life does "
                            "not add up to the cost less reductions.",
                            asset=asset.display_name,
                        ),
                        asset,
                        cost_total,
                    )
                )
        return results

    # ------------------------------------------------------------ C-68
    def _check_c68(self, context):
        company, year = context["company"], context["year"]
        Form = self.env["l10n_de_euer.form"]
        if not Form.search_count(Domain("year", "<=", year), limit=1):
            return []
        comparison = self.env["l10n_de_euer.asset"]._get_ledger_comparison(
            company, year
        )
        return (
            self._check_c68_lines(context)
            + self._check_c68_shared(context)
            + self._check_c68_receipt(context)
        ) + [
            self._result(
                "C-68",
                "warning",
                self.env._(
                    "%(code)s: bookings on depreciation accounts %(ledger).2f, "
                    "asset register %(register).2f. The form uses the register; "
                    "correct the ledger or the register.",
                    code=code,
                    ledger=ledger,
                    register=register,
                ),
                context["assets"],
                ledger - register,
            )
            for code, (ledger, register) in sorted(comparison.items())
            if ledger != register
        ]

    @api.model
    def _purchase_cost(self, asset, base):
        """Cost of ``asset`` its purchase items ``base`` (without tax items)
        must show.

        The net cost, unless the input tax is not deductible (small
        business, § 9b (1) EStG) and is part of the items themselves: a
        small business books the purchase gross without a tax item, so the
        items show the acquisition cost (net cost and input tax). An input
        tax booked on a tax item of its own is not part of ``base``.
        """
        if asset.vat_treatment == "not_deductible" and not any(
            line.tax_ids and line.price_total != line.price_subtotal for line in base
        ):
            return to_decimal(asset.cost_net) + to_decimal(asset.input_tax)
        return to_decimal(asset.cost_net)

    def _check_c68_lines(self, context):
        """Journal items of the purchase against the asset (G-10): items no
        longer posted, or a cost that differs from them (the net cost, for a
        small business booking gross the acquisition cost with the input tax
        that is not deductible, :meth:`_purchase_cost`)."""
        year = context["year"]
        results = []
        for asset in context["assets"]:
            lines = asset.move_line_ids
            if not lines or (
                asset not in context["present"]
                and not (asset.delivery_date.year == year)
            ):
                continue
            unposted = lines.filtered(lambda line: line.parent_state != "posted")
            base = lines.filtered(lambda line: not line.tax_line_id)
            amount = sum((to_decimal(line.balance) for line in base), dep.ZERO)
            cost = self._purchase_cost(asset, base)
            # items shared with other assets: compared together (shared check)
            shared = bool(
                self.env["l10n_de_euer.asset"]
                .sudo()
                .search_count(
                    Domain("move_line_ids", "in", base.ids)
                    & Domain("state", "!=", "draft")
                    & Domain("id", "!=", asset.id),
                    limit=1,
                )
            )
            if unposted:
                message = self.env._(
                    "%(asset)s: the journal items %(moves)s of the purchase are "
                    "not posted (cancelled or reset to draft).",
                    asset=asset.display_name,
                    moves=", ".join(unposted.move_id.mapped("display_name")),
                )
            elif (
                asset.delivery_date.year == year
                and asset.origin == "purchase"
                and not shared
                and amount != cost
            ):
                if cost != to_decimal(asset.cost_net):
                    message = self.env._(
                        "%(asset)s: acquisition cost %(cost).2f (net cost and "
                        "input tax that is not deductible), journal items of the "
                        "purchase %(lines).2f. Correct the asset or the items.",
                        asset=asset.display_name,
                        cost=cost,
                        lines=amount,
                    )
                else:
                    message = self.env._(
                        "%(asset)s: net cost %(cost).2f, journal items of the "
                        "purchase %(lines).2f. Correct the asset or the items.",
                        asset=asset.display_name,
                        cost=asset.cost_net,
                        lines=amount,
                    )
            else:
                continue
            results.append(
                self._result(
                    "C-68",
                    "warning",
                    message,
                    asset,
                    amount - cost,
                )
            )
        return results

    def _check_c68_shared(self, context):
        """Journal items of the purchase linked to several assets: together
        the assets must not cost more than the items (the cost would count
        twice)."""
        year = context["year"]
        Asset = self.env["l10n_de_euer.asset"]
        delivered = context["assets"].filtered(
            lambda asset: asset.delivery_date.year == year and asset.move_line_ids
        )
        results = []
        seen = set()
        for asset in delivered:
            base = asset.move_line_ids.filtered(lambda line: not line.tax_line_id)
            group = Asset.sudo().search(
                Domain("move_line_ids", "in", base.ids) & Domain("state", "!=", "draft")
            )
            if len(group) < 2 or frozenset(group.ids) in seen:
                continue
            seen.add(frozenset(group.ids))
            lines = group.move_line_ids.filtered(lambda line: not line.tax_line_id)
            cost = sum(
                (
                    self._purchase_cost(
                        item,
                        item.move_line_ids.filtered(lambda line: not line.tax_line_id),
                    )
                    for item in group
                ),
                dep.ZERO,
            )
            amount = sum((to_decimal(line.balance) for line in lines), dep.ZERO)
            if cost == amount:
                continue
            results.append(
                self._result(
                    "C-68",
                    "warning",
                    self.env._(
                        "%(assets)s share journal items of the purchase: cost "
                        "together %(cost).2f, journal items %(lines).2f. Correct "
                        "the assets or the items (the cost must not count twice).",
                        assets=", ".join(group.mapped("display_name")),
                        cost=cost,
                        lines=amount,
                    ),
                    group,
                    cost - amount,
                )
            )
        return results

    def _check_c68_receipt(self, context):
        """Receipt of the proceeds against the payment of the sales invoice."""
        year = context["year"]
        results = []
        for asset in context["assets"]:
            if asset.asset_kind not in CASH_SALE_KINDS or asset.disposal_kind != (
                "sale"
            ):
                continue
            payments = asset._get_disposal_payments()
            receipt = asset.disposal_receipt_date
            if not payments or asset.disposal_invoice_id.amount_residual:
                continue
            paid = max(day for day, _amount in payments)
            if len({day.year for day, _amount in payments}) > 1:
                continue  # instalments: C-40
            if (receipt and receipt.year == paid.year) or year not in (
                paid.year,
                receipt and receipt.year,
            ):
                continue
            results.append(
                self._result(
                    "C-68",
                    "warning",
                    self.env._(
                        "%(asset)s: the proceeds were received on %(paid)s "
                        "(payment of %(invoice)s), the register expenses the "
                        "cost on %(receipt)s.",
                        asset=asset.display_name,
                        paid=paid,
                        invoice=asset.disposal_invoice_id.display_name,
                        receipt=receipt or self.env._("no date"),
                    ),
                    asset,
                    to_decimal(asset.acquisition_cost),
                )
            )
        return results

    # ------------------------------------------------------------ C-69
    def _check_c69(self, context):
        """Use withdrawal neither confirmed nor rejected, private use of a
        vehicle (R-54). A profit correction of the statement decides it: a
        use withdrawal of the asset, or the private use of vehicles
        (Kz 106) for the vehicles."""
        statement = self.env["l10n_de_euer.statement"]._get_for_calculation(
            context["company"], context["year"]
        )
        corrections = statement.adjustment_ids
        taken = corrections.asset_id
        lines = context["assets"].year_ids.filtered(
            lambda line: line.year == context["year"]
            and line._private_use_open()
            and line.asset_id not in taken
        )
        car_corrected = any(
            correction.kind == "car_private_use" for correction in corrections
        )
        vehicles = [
            asset
            for asset in context["present"]
            if not car_corrected
            and asset.aveuer_group == "vehicle"
            and asset.asset_kind in dep.AVEUER_KINDS
            and asset.year_ids.filtered(
                lambda line, y=context["year"]: line.year == y
                and line.business_share < 1
            )
        ]
        vehicle_results = [
            self._result(
                "C-69",
                "warning",
                self.env._(
                    "%(asset)s is a vehicle used privately: enter the private use "
                    "value as profit correction in Kz 106 (1 %% rule above 50 %% "
                    "business use, otherwise the logbook or the private share of "
                    "the costs), not as use withdrawal of the register.",
                    asset=asset.display_name,
                ),
                asset,
            )
            for asset in vehicles
        ]
        return vehicle_results + [
            self._result(
                "C-69",
                "warning",
                self.env._(
                    "%(asset)s is used privately: confirm the proposed use "
                    "withdrawal of %(amount).2f (VAT %(vat).2f) or reject it with "
                    "a reason (R-54).",
                    asset=line.asset_id.display_name,
                    amount=line.private_use_amount,
                    vat=line.private_use_vat,
                ),
                line,
                to_decimal(line.private_use_amount),
            )
            for line in lines
        ]
