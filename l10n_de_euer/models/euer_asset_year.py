# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from decimal import Decimal

from odoo import api, fields, models
from odoo.exceptions import UserError

from .engine import depreciation as dep
from .euer_asset import AVEUER_GROUPS, STANDARD_VAT_RATE, to_decimal

#: context key of the register: only it creates, changes and deletes years,
#: always as superuser (a client cannot set ``env.su`` through RPC)
INTERNAL = "l10n_de_euer_asset_year_internal"
#: fields a user may write: the decision on the use withdrawal
USER_FIELDS = frozenset({"private_use_reason"})
#: values compared when a locked year is calculated again
LOCKED_VALUES = (
    "cost_start",
    "book_value_start",
    "additions",
    "special_depreciation",
    "depreciation",
    "disposal_book_value",
    "book_value_end",
)


class L10nDeEuerAssetYear(models.Model):
    """Values of an asset in one year (spec 6.2).

    Calculated by the register; frozen with ``locked`` when the statement of
    the year is finalised. The year also carries the proposal of a use
    withdrawal for private use (R-54) and its confirmation or rejection.
    """

    _name = "l10n_de_euer.asset.year"
    _description = "EÜR Asset Year"
    _order = "year desc, asset_id"
    _check_company_auto = True

    asset_id = fields.Many2one(
        "l10n_de_euer.asset", required=True, index=True, ondelete="cascade"
    )
    company_id = fields.Many2one(related="asset_id.company_id", store=True, index=True)
    currency_id = fields.Many2one(related="company_id.currency_id")
    year = fields.Integer(required=True, index=True)
    asset_kind = fields.Selection(related="asset_id.asset_kind", store=True)
    aveuer_group = fields.Selection(
        AVEUER_GROUPS,
        string="AVEÜR Group",
        readonly=True,
        help="Group of the asset when the year was calculated; a locked year keeps it.",
    )
    is_opening = fields.Boolean(
        string="Takeover",
        readonly=True,
        help="Book value at the takeover date from the previous system; the "
        "years before are not calculated again.",
    )
    cost_start = fields.Monetary(
        string="Cost at Beginning",
        help="Historical cost of the asset if it was present at the beginning "
        "of the year (AVEÜR column of cost).",
    )
    book_value_start = fields.Monetary()
    additions = fields.Monetary()
    special_depreciation = fields.Monetary()
    depreciation = fields.Monetary()
    disposal_book_value = fields.Monetary(string="Disposals (Residual Value)")
    book_value_end = fields.Monetary()
    months_in_year = fields.Integer()
    locked = fields.Boolean(readonly=True, copy=False)
    business_share = fields.Float(digits=(16, 4), readonly=True)
    private_cost = fields.Monetary(
        string="Running Costs",
        readonly=True,
        help="Running costs assigned to the asset with a date in the year.",
    )
    private_use_amount = fields.Monetary(
        string="Proposed Use Withdrawal",
        readonly=True,
        help="(Depreciation + running costs) × private share → Kz 108 and 122.",
    )
    private_use_vat = fields.Monetary(
        string="Proposed VAT", readonly=True, help="Kz 140, only with input tax."
    )
    private_use_vat_base = fields.Monetary(
        string="VAT Base of the Use",
        readonly=True,
        help="Expenses that gave input tax deduction × private share (§ 10 (4) "
        "sentence 1 no. 2 UStG). The use is a taxable supply only with such "
        "expenses (§ 3 (9a) no. 1 UStG); then this base counts in the total "
        "turnover of the small business limit (R-27).",
    )
    private_use_decision = fields.Selection(
        [("confirmed", "Confirmed"), ("rejected", "Rejected")],
        readonly=True,
        copy=False,
    )
    private_use_confirmed_amount = fields.Monetary(readonly=True, copy=False)
    private_use_confirmed_vat = fields.Monetary(readonly=True, copy=False)
    private_use_confirmed_base = fields.Monetary(
        string="Confirmed VAT Base", readonly=True, copy=False
    )
    private_use_reason = fields.Text(copy=False)
    private_use_decided_by_id = fields.Many2one(
        "res.users", readonly=True, copy=False, ondelete="restrict"
    )
    private_use_decided_on = fields.Datetime(readonly=True, copy=False)

    _asset_year_uniq = models.Constraint(
        "UNIQUE(asset_id, year)", "There is one line per asset and year."
    )

    @api.depends("asset_id", "year")
    def _compute_display_name(self):
        for line in self:
            line.display_name = f"{line.asset_id.display_name} {line.year}"

    # ------------------------------------------------------------ engine
    @api.model
    def _values_from_engine(self, row, group=False):
        return {
            "year": row.year,
            "aveuer_group": group,
            "cost_start": float(row.cost_start),
            "book_value_start": float(row.book_value_start),
            "additions": float(row.additions),
            "special_depreciation": float(row.special_depreciation),
            "depreciation": float(row.depreciation),
            "disposal_book_value": float(row.disposal_book_value),
            "book_value_end": float(row.book_value_end),
            "months_in_year": row.months,
        }

    def _get_engine_values(self):
        self.ensure_one()
        return dep.YearValues(
            year=self.year,
            cost_start=to_decimal(self.cost_start),
            book_value_start=to_decimal(self.book_value_start),
            additions=to_decimal(self.additions),
            special_depreciation=to_decimal(self.special_depreciation),
            depreciation=to_decimal(self.depreciation),
            disposal_book_value=to_decimal(self.disposal_book_value),
            book_value_end=to_decimal(self.book_value_end),
            months=self.months_in_year,
        )

    def _check_unchanged(self, values):
        """Raise if a new calculation would change a locked year."""
        self.ensure_one()
        values = values or {}
        currency = self.currency_id
        changed = values.get("aveuer_group", self.aveuer_group) != self.aveuer_group
        for name in LOCKED_VALUES:
            if changed or currency.compare_amounts(values.get(name, 0.0), self[name]):
                raise UserError(
                    self.env._(
                        "The year %(year)s of %(asset)s is locked; the change "
                        "would alter its values. Record it as a correction.",
                        year=self.year,
                        asset=self.asset_id.display_name,
                    )
                )

    # ------------------------------------------------------------ private use
    @api.model
    def _is_internal(self):
        return self.env.su and bool(self.env.context.get(INTERNAL))

    def _get_business_share(self):
        """Share of business use valid at the end of the year."""
        self.ensure_one()
        asset = self.asset_id
        changes = asset.event_ids.filtered(
            lambda event: event.type == "business_use_change"
            and event.date.year <= self.year
        ).sorted("date")
        return changes[-1:].business_share if changes else asset.business_share

    def _compute_private_use(self):
        """Proposal of a use withdrawal for private use (R-54, spec 6.3).

        Vehicles get no proposal: their private use is the private use value
        of Zeile 20 (Kz 106, 1 % rule or logbook) of the profit corrections
        (spec 5.8), not a use withdrawal of Zeile 21 (Kz 108).
        """
        for line in self.filtered(lambda line: not line.locked and not line.is_opening):
            asset = line.asset_id
            share = line._get_business_share()
            if asset.aveuer_group == "vehicle":
                line._write_internal(
                    {
                        "business_share": share,
                        "private_cost": 0.0,
                        "private_use_amount": 0.0,
                        "private_use_vat": 0.0,
                        "private_use_vat_base": 0.0,
                    }
                )
                continue
            costs = asset.cost_line_ids.filtered(
                lambda aml, y=line.year: aml.date.year == y
                and aml.parent_state == "posted"
            )
            cost_total = sum(costs.mapped("balance"))
            regime = asset.company_id._l10n_de_euer_get_vat_regime(
                fields.Date.to_date(f"{line.year}-12-31")
            )
            vat_base = Decimal(0)
            if asset.vat_treatment == "deductible" and asset.input_tax:
                # § 10 (4) sentence 1 no. 2 UStG: the cost spread over the
                # § 15a period, not the depreciation for income tax; it
                # counts also in a later small business year (the use is
                # taxable, but tax-free under § 19 (1) UStG)
                vat_base += dep.private_use_vat_base(
                    to_decimal(asset.acquisition_cost),
                    asset.delivery_date,
                    line.year,
                    real_estate=asset.aveuer_group in dep.REAL_ESTATE_GROUPS,
                    useful_life_months=asset.useful_life_months
                    if asset.method in ("linear", "degressive")
                    else 0,
                )
            if regime != "ku":
                # running costs give input tax deduction only under standard
                # taxation
                vat_base += sum(
                    (
                        to_decimal(aml.balance)
                        for aml in costs
                        if aml.tax_ids.filtered(
                            lambda tax: tax.type_tax_use == "purchase"
                            and tax.amount > 0
                        )
                    ),
                    Decimal(0),
                )
            amount, vat = dep.private_use_proposal(
                to_decimal(line.depreciation),
                to_decimal(cost_total),
                share,
                vat_base if regime != "ku" else Decimal(0),
                STANDARD_VAT_RATE,
            )
            private = Decimal(1) - Decimal(str(share))
            base = dep.cents(vat_base * private) if private > 0 else Decimal(0)
            line._write_internal(
                {
                    "business_share": share,
                    "private_cost": cost_total,
                    "private_use_amount": float(amount),
                    "private_use_vat": float(vat),
                    "private_use_vat_base": float(base),
                }
            )

    def _private_use_open(self):
        """Whether the proposal still needs a decision (check C-69)."""
        self.ensure_one()
        if not self.private_use_amount and not self.private_use_vat:
            return False
        if self.private_use_decision == "rejected":
            return False
        currency = self.currency_id
        return self.private_use_decision != "confirmed" or bool(
            currency.compare_amounts(
                self.private_use_confirmed_amount, self.private_use_amount
            )
            or currency.compare_amounts(
                self.private_use_confirmed_vat, self.private_use_vat
            )
            or currency.compare_amounts(
                self.private_use_confirmed_base, self.private_use_vat_base
            )
        )

    def action_confirm_private_use(self):
        """Confirm the proposed use withdrawal as profit correction."""
        self.check_access("write")
        self._check_decision_open()
        for line in self:
            line._write_internal(
                {
                    "private_use_decision": "confirmed",
                    "private_use_confirmed_amount": line.private_use_amount,
                    "private_use_confirmed_vat": line.private_use_vat,
                    "private_use_confirmed_base": line.private_use_vat_base,
                    "private_use_decided_by_id": self.env.uid,
                    "private_use_decided_on": fields.Datetime.now(),
                }
            )
            line.asset_id.message_post(
                body=self.env._(
                    "Use withdrawal %(year)s confirmed: %(amount).2f, VAT %(vat).2f.",
                    year=line.year,
                    amount=line.private_use_amount,
                    vat=line.private_use_vat,
                )
            )
        return True

    def action_reject_private_use(self):
        """Reject the proposal with a reason (e.g. private use compensated)."""
        self.check_access("write")
        self._check_decision_open()
        for line in self:
            if not line.private_use_reason:
                raise UserError(
                    self.env._(
                        "Enter a reason to reject the use withdrawal of %(line)s.",
                        line=line.display_name,
                    )
                )
            line._write_internal(
                {
                    "private_use_decision": "rejected",
                    "private_use_confirmed_amount": 0.0,
                    "private_use_confirmed_vat": 0.0,
                    "private_use_confirmed_base": 0.0,
                    "private_use_decided_by_id": self.env.uid,
                    "private_use_decided_on": fields.Datetime.now(),
                }
            )
            line.asset_id.message_post(
                body=self.env._(
                    "Use withdrawal %(year)s rejected: %(reason)s",
                    year=line.year,
                    reason=line.private_use_reason,
                )
            )
        return True

    def action_open_move_lines(self):
        """Journal items of the asset of the yearly values (spec 7.6 no. 3)."""
        self.ensure_one()
        return self.asset_id.action_open_move_lines()

    def _check_decision_open(self):
        if self.filtered("locked"):
            raise UserError(self.env._("The year is locked."))

    # ------------------------------------------------------------ protection
    @api.model
    def _create_internal(self, values):
        return self.sudo().with_context(**{INTERNAL: True}).create(values)

    def _write_internal(self, values):
        return self.sudo().with_context(**{INTERNAL: True}).write(values)

    def _unlink_internal(self):
        return self.sudo().with_context(**{INTERNAL: True}).unlink()

    @api.model_create_multi
    def create(self, vals_list):
        if not self._is_internal():
            raise UserError(self.env._("Yearly values are calculated by the register."))
        return super().create(vals_list)

    def write(self, vals):
        internal = self._is_internal()
        if "locked" in vals and not vals["locked"] and self.filtered("locked"):
            raise UserError(self.env._("A locked year cannot be unlocked."))
        if self.filtered("locked") and not internal:
            raise UserError(self.env._("Locked yearly values cannot be changed."))
        if not internal:
            if set(vals) - USER_FIELDS:
                raise UserError(
                    self.env._("Yearly values are calculated by the register.")
                )
            if self.filtered("private_use_decision"):
                raise UserError(
                    self.env._("The reason of a decided use withdrawal is fixed.")
                )
        elif self.filtered("locked") and set(vals) - {"locked"}:
            raise UserError(self.env._("Locked yearly values cannot be changed."))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_locked(self):
        if self.filtered("locked"):
            raise UserError(self.env._("Locked yearly values cannot be deleted."))
        if not self._is_internal():
            raise UserError(self.env._("Yearly values are calculated by the register."))
