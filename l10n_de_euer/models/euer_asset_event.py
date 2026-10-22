# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from decimal import Decimal

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from .engine import depreciation as dep
from .euer_asset import to_decimal

EVENT_TYPES = [
    ("additional_cost", "Additional Cost"),
    ("subsidy", "Subsidy"),
    ("sec7g_reduction", "Reduction § 7g (2) EStG"),
    ("sec7g_special", "Special Depreciation § 7g (5) EStG"),
    ("sec7b_special", "Special Depreciation § 7b EStG"),
    ("extraordinary", "Extraordinary Depreciation (AfaA)"),
    ("useful_life_change", "Change of Useful Life"),
    ("business_use_change", "Change of Business Use"),
    ("manual_depreciation", "Manual Depreciation"),
]
#: Events that need a reason (voucher of one's own, G-15)
REASON_REQUIRED = frozenset(
    {
        "extraordinary",
        "useful_life_change",
        "manual_depreciation",
        "business_use_change",
        "sec7g_special",
        "sec7b_special",
    }
)


class L10nDeEuerAssetEvent(models.Model):
    """Event of an asset: additional cost, subsidy, special depreciation …

    Spec 6.2. Additional costs count from the beginning of their year; a
    subsidy is a negative addition in the year of approval. Both, a change of
    the useful life and an extraordinary depreciation spread the book value
    over the remaining useful life. Events in locked years cannot be changed.
    """

    _name = "l10n_de_euer.asset.event"
    _description = "EÜR Asset Event"
    _order = "date, id"
    _check_company_auto = True

    asset_id = fields.Many2one(
        "l10n_de_euer.asset",
        required=True,
        index=True,
        ondelete="cascade",
        check_company=True,
    )
    company_id = fields.Many2one(related="asset_id.company_id", store=True)
    currency_id = fields.Many2one(related="company_id.currency_id")
    type = fields.Selection(EVENT_TYPES, required=True)
    date = fields.Date(
        required=True,
        help="Subsidy: date of approval; approved before the delivery it reduces "
        "the addition in the year of delivery (approved in an earlier year: "
        "outside the program scope, R 6.5 (4) EStR). Special depreciation: any "
        "date of the year it is taken in.",
    )
    year = fields.Integer(compute="_compute_year", store=True)
    amount = fields.Monetary()
    useful_life_months = fields.Integer(
        string="Remaining Useful Life (Months)",
        help="New estimate of the remaining useful life, counted from 1 January "
        "of the year of the event.",
    )
    business_share = fields.Float(
        digits=(16, 4), help="New share of business use from this year on."
    )
    reason = fields.Text()
    sec7g_profit_confirmed = fields.Boolean(
        string="Profit Limit Met (§ 7g (6) No. 1 EStG)",
        help="The profit of the business year before the year of the "
        "acquisition or production (not before the special depreciation) does "
        "not exceed the limit of § 7g (1) sentence 2 no. 1 EStG (200,000 €). "
        "Required for special depreciation under § 7g (5) EStG. For an "
        "acquisition before 2020 the size limits of that time apply.",
    )
    attachment_ids = fields.Many2many(
        "ir.attachment",
        "l10n_de_euer_asset_event_attachment_rel",
        "event_id",
        "attachment_id",
        string="Vouchers",
    )

    @api.depends("date")
    def _compute_year(self):
        for event in self:
            event.year = event.date.year if event.date else 0

    @api.constrains("type", "reason", "business_share", "sec7g_profit_confirmed")
    def _check_reason(self):
        for event in self:
            if event.type == "sec7g_special" and not event.sec7g_profit_confirmed:
                raise ValidationError(
                    self.env._(
                        "%(asset)s: confirm that the profit of %(year)s, the "
                        "business year before the acquisition or production, does "
                        "not exceed the limit of § 7g (1) sentence 2 no. 1 EStG "
                        "(§ 7g (6) no. 1 EStG).",
                        asset=event.asset_id.display_name,
                        year=event.asset_id.delivery_date.year - 1
                        if event.asset_id.delivery_date
                        else "",
                    )
                )
            if event.type in REASON_REQUIRED and not event.reason:
                raise ValidationError(
                    self.env._(
                        "Enter a reason for the event %(type)s of %(asset)s.",
                        type=dict(EVENT_TYPES)[event.type],
                        asset=event.asset_id.display_name,
                    )
                )
            if event.type == "business_use_change" and not (
                0 <= event.business_share <= 1
            ):
                raise ValidationError(
                    self.env._("The business share must be between 0 and 100 %.")
                )

    def _get_engine_event(self):
        self.ensure_one()
        share = (
            Decimal(str(self.business_share))
            if self.type == "business_use_change"
            else None
        )
        return dep.Event(
            type=self.type,
            date=self.date,
            amount=to_decimal(self.amount),
            useful_life_months=self.useful_life_months,
            business_share=share,
        )

    def _check_year_open(self, years=None):
        """Events of locked years cannot change. Events up to the takeover
        date of an asset from another system are history: those years are not
        calculated by the register, and what they change after the takeover
        is protected by the locked yearly values."""
        locked = self.asset_id._get_locked_year()
        for event in self:
            year = event.date.year if event.date else 0
            opening = event.asset_id.opening_date
            if opening and event.date and event.date <= opening and not years:
                continue
            for number in {year} | set(years or ()):
                if number and number <= locked.get(event.company_id.id, 0):
                    raise UserError(
                        self.env._(
                            "The year %(year)s is locked; events of %(asset)s in "
                            "it cannot be changed.",
                            year=number,
                            asset=event.asset_id.display_name,
                        )
                    )

    def _log(self, action):
        """Change log on the asset (G-06)."""
        labels = dict(EVENT_TYPES)
        for event in self:
            event.asset_id.message_post(
                body=self.env._(
                    "%(action)s: %(type)s on %(date)s, amount %(amount).2f. %(reason)s",
                    action=action,
                    type=labels.get(event.type, event.type),
                    date=event.date,
                    amount=event.amount,
                    reason=event.reason or "",
                )
            )

    def _check_sec7g_use(self):
        """§ 7g (6) no. 2 EStG at the time the special depreciation is entered.

        A later change (use change, disposal) is a blocker of the checks
        (C-40): the special depreciation then has to be reversed.
        """
        for asset in self.filtered(lambda e: e.type == "sec7g_special").asset_id:
            issues = asset._get_sec7g_issues()
            if issues:
                raise ValidationError(
                    self.env._(
                        "%(asset)s: no special depreciation under § 7g (5) EStG: "
                        "%(issues)s.",
                        asset=asset.display_name,
                        issues="; ".join(issues),
                    )
                )

    @api.model_create_multi
    def create(self, vals_list):
        events = super().create(vals_list)
        events._check_year_open()
        events._check_sec7g_use()
        events.asset_id._after_change()
        events._log(self.env._("Event added"))
        return events

    def write(self, vals):
        new_year = [fields.Date.to_date(vals["date"]).year] if vals.get("date") else []
        self._check_year_open(new_year)
        assets = self.asset_id
        result = super().write(vals)
        if {"type", "amount", "date", "asset_id"}.intersection(vals):
            self._check_sec7g_use()
        (assets | self.asset_id)._after_change()
        self._log(self.env._("Event changed"))
        return result

    @api.ondelete(at_uninstall=False)
    def _unlink_except_locked_year(self):
        self._check_year_open()

    def unlink(self):
        assets = self.asset_id
        self._log(self.env._("Event removed"))
        result = super().unlink()
        assets.exists()._after_change()
        return result
