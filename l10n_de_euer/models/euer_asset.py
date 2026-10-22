# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from decimal import Decimal

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Domain

from .engine import depreciation as dep

ASSET_KINDS = [
    ("depreciable", "Depreciable Asset"),
    ("gwg", "Low-Value Asset (§ 6 (2) EStG)"),
    ("pool", "Asset Pool (§ 6 (2a) EStG)"),
    ("instant_250", "Immediate Expense up to 250 €"),
    ("non_depreciable", "Not Depreciable (§ 4 (3) Sentence 4 EStG)"),
    ("current_4_3_4", "Current Asset (§ 4 (3) Sentence 4 EStG)"),
]
AVEUER_GROUPS = [
    ("land", "Land"),
    ("building", "Buildings"),
    ("land_rights", "Other Land Rights"),
    ("office_land", "Home Office: Land"),
    ("office_building", "Home Office: Building"),
    ("intangible", "Intangible Assets"),
    ("vehicle", "Vehicles"),
    ("office_equipment", "Office Equipment"),
    ("other_movable", "Other Movable Assets"),
    ("fin_tev", "Financial Assets (Partial Income Method)"),
    ("fin_other", "Other Financial Assets"),
    ("current", "Current Assets (§ 4 (3) Sentence 4 EStG)"),
]
METHODS = [
    ("linear", "Linear"),
    ("degressive", "Degressive (§ 7 (2) EStG)"),
    ("ev_7_2a", "Electric Vehicle (§ 7 (2a) EStG)"),
    ("digital_1y", "Digital Asset, One Year"),
    ("none", "None"),
    ("manual", "Manual"),
]
ORIGINS = [
    ("purchase", "Purchase"),
    ("production", "Production"),
    ("contribution", "Contribution"),
    ("opening", "Opening Balance"),
    ("legacy_import", "Takeover from Another System"),
]
DISPOSAL_KINDS = [
    ("sale", "Sale"),
    ("withdrawal", "Withdrawal"),
    ("scrap", "Scrapping"),
    ("loss", "Loss"),
]
VAT_TREATMENTS = [
    ("deductible", "Input Tax Deductible (Cost Net)"),
    ("not_deductible", "Input Tax Not Deductible (Cost Gross)"),
    ("sec15_1a", "Not Deductible under § 15 (1a) UStG (Cost Net)"),
]
#: Fields of the asset that change its schedule
SCHEDULE_FIELDS = frozenset(
    {
        "asset_kind",
        "aveuer_group",
        "delivery_date",
        "origin",
        "cost_net",
        "input_tax",
        "vat_treatment",
        "contribution_fair_value",
        "private_acquisition_date",
        "private_acquisition_cost",
        "private_useful_life_months",
        "method",
        "useful_life_months",
        "degressive_rate",
        "switch_to_linear_year",
        "memo_value",
        "opening_date",
        "opening_book_value",
        "opening_accumulated",
        "disposal_date",
        "disposal_kind",
        "disposal_value",
        "disposal_receipt_date",
        "disposal_vat_amount",
        "business_share",
        "home_office_deductible",
        "company_id",
        "cost_line_ids",
        "degressive_rate_max",
    }
)
#: Fields outside the yearly values that go into the form values of a year
#: (Kz 102/140/122 of a withdrawal, Kz 407, Kz 172/162): they cannot change
#: once that year is locked
LOCKED_INPUT_FIELDS = frozenset(
    {
        "disposal_value",
        "disposal_vat_amount",
        "is_electric_or_plugin_hybrid",
        "is_battery_electric",
        "home_office_deductible",
    }
)
#: Fields the register calculates itself: never written from outside, also
#: not by an administrator through the ORM (spec 7.7)
COMPUTED_FIELDS = frozenset(
    {
        "acquisition_cost",
        "cost_gross",
        "limit_value",
        "contribution_value",
        "in_gwg_register",
        "pool_id",
    }
)
#: context key of the register for the fields above, only as superuser (a
#: client cannot set ``env.su`` through RPC)
INTERNAL = "l10n_de_euer_asset_internal"
#: Fields of a takeover from another system, released by a manager (7.10)
TAKEOVER_FIELDS = frozenset(
    {
        "origin",
        "opening_date",
        "opening_book_value",
        "opening_accumulated",
        "memo_value",
    }
)
TAKEOVER_ORIGINS = frozenset({"opening", "legacy_import"})
#: Fields that never change the form values of a year; every other change of
#: an asset with locked years is compared with the form values of these years
NEUTRAL_FIELDS = frozenset({"name", "code", "note", "pool_id"})
#: Groups of computer hardware and software, the scope of the useful life of
#: one year (BMF 22.02.2022, BStBl I S. 187)
DIGITAL_GROUPS = frozenset({"office_equipment", "other_movable", "intangible"})
#: Origins of an acquired asset (§ 7 (2a) EStG: "angeschafft")
ACQUIRED_ORIGINS = frozenset({"purchase", "legacy_import"})
#: Business share required for § 7g (5) EStG (§ 7g (6) no. 2: "ausschließlich
#: oder fast ausschließlich", at least 90 %)
SEC7G_MIN_BUSINESS_SHARE = 0.9
#: Standard VAT rate proposed for the VAT on a withdrawal (§ 12 (1) UStG)
STANDARD_VAT_RATE = Decimal("0.19")
#: Business share below which an asset is private property (R 4.2 (1) EStR)
MIN_BUSINESS_SHARE = 0.1


def to_decimal(value):
    """Monetary value of the ORM (float) as :class:`~decimal.Decimal` in cents."""
    return dep.cents(Decimal(str(value or 0.0)))


class L10nDeEuerAsset(models.Model):
    """Asset of the EÜR asset register (spec 6.2).

    The register is the only source of depreciation, low-value assets, asset
    pools and residual book values in Anlage EÜR and of Anlage AVEÜR (6.5).
    The calculation is pure Python (:mod:`.engine.depreciation`); this model
    holds the input, stores the yearly values and protects locked years.
    """

    _name = "l10n_de_euer.asset"
    _inherit = ["mail.thread"]
    _description = "EÜR Asset"
    _order = "delivery_date desc, id desc"
    _check_company_auto = True
    _rec_names_search = ["name", "code"]

    name = fields.Char(required=True, tracking=True)
    code = fields.Char(string="Inventory Number", tracking=True, copy=False)
    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        default=lambda self: self.env.company._l10n_de_euer_business_root(),
        domain="['|', ('parent_id', '=', False), "
        "('l10n_de_euer_separate_business', '=', True)]",
        tracking=True,
        help="Business: the main company with its branches or a branch that is "
        "a business of its own.",
    )
    currency_id = fields.Many2one(related="company_id.currency_id")
    state = fields.Selection(
        [("draft", "Draft"), ("active", "In Register"), ("disposed", "Disposed")],
        required=True,
        default="draft",
        tracking=True,
        copy=False,
    )
    asset_kind = fields.Selection(
        ASSET_KINDS,
        string="Kind",
        required=True,
        default="depreciable",
        tracking=True,
        help="Low-value assets (net cost up to 800 €) and items up to 250 € are "
        "expensed in the year of delivery (Kz 132). Asset pools (250 € to "
        "1,000 €) are released by one fifth per year. Assets that are not "
        "depreciable and current assets under § 4 (3) sentence 4 EStG become "
        "an expense only when the proceeds are received.",
    )
    aveuer_group = fields.Selection(
        AVEUER_GROUPS,
        string="AVEÜR Group",
        required=True,
        default="other_movable",
        tracking=True,
        help="Group of Anlage AVEÜR. The account is only a proposal: a camera "
        "is an other movable asset, not office equipment.",
    )
    is_electric_or_plugin_hybrid = fields.Boolean(
        string="Electric or Plug-in Hybrid Vehicle",
        compute="_compute_is_electric_or_plugin_hybrid",
        store=True,
        readonly=False,
        precompute=True,
        tracking=True,
        help="At least one such vehicle present: AVEÜR Kz 407, independent of "
        "the depreciation method.",
    )
    is_battery_electric = fields.Boolean(
        string="Purely Electric Vehicle",
        tracking=True,
        help="Driven exclusively by electric motors (electric vehicle under "
        "§ 9 (2) KraftStG): required for the depreciation under § 7 (2a) EStG. "
        "A plug-in hybrid is an electric or plug-in hybrid vehicle for Kz 407, "
        "but not purely electric.",
    )
    is_trivial_program = fields.Boolean(
        string="Trivial Program (R 5.5 (1) EStR)",
        tracking=True,
        help="Computer program up to the limit of R 5.5 (1) sentence 3 EStR "
        "(800 € from 2018): a movable asset that can be a low-value asset, go "
        "into the asset pool or be expensed up to 250 €. Other intangible "
        "assets cannot.",
    )
    independent_use = fields.Boolean(
        string="Independently Usable",
        default=True,
        tracking=True,
        help="The asset can be used on its own (§ 6 (2) sentence 2 EStG); a "
        "lens without camera body cannot. Required for low-value assets and "
        "asset pools.",
    )
    business_share = fields.Float(
        digits=(16, 4),
        default=1.0,
        tracking=True,
        help="Share of business use. Changes in later years are events. Below "
        "100 % the register proposes a use withdrawal; below 10 % the asset is "
        "private property (R 4.2 (1) EStR).",
    )
    home_office_deductible = fields.Boolean(
        default=True,
        tracking=True,
        help="Depreciation of a home office goes into Kz 172 (deductible) or "
        "Kz 162 (not deductible).",
    )
    delivery_date = fields.Date(
        required=True,
        tracking=True,
        help="Delivery, completion, contribution or opening of the business: "
        "start of the depreciation, not the invoice or payment date. A "
        "delivery at the turn of two months (31 December, 24:00) is the first "
        "day of the second month.",
    )
    origin = fields.Selection(ORIGINS, required=True, default="purchase", tracking=True)
    cost_net = fields.Monetary(
        string="Net Cost",
        tracking=True,
        help="Cost of acquisition or production without input tax; decides the "
        "limits of low-value assets and asset pools, also for small businesses.",
    )
    input_tax = fields.Monetary(tracking=True, help="Input tax of the purchase.")
    vat_treatment = fields.Selection(
        VAT_TREATMENTS,
        string="Input Tax Treatment",
        compute="_compute_vat_treatment",
        store=True,
        readonly=False,
        precompute=True,
        tracking=True,
        help="Proposal from the VAT status period on the delivery date. Small "
        "businesses cannot deduct input tax: it is part of the cost "
        "(R 9b (1) EStR). Input tax not deductible under § 15 (1a) UStG is "
        "never part of the cost.",
    )
    cost_gross = fields.Monetary(compute="_compute_cost", store=True)
    contribution_fair_value = fields.Monetary(
        string="Fair Value (Teilwert)",
        tracking=True,
        help="Fair value of a contribution from private property.",
    )
    private_acquisition_date = fields.Date(
        tracking=True, help="Private acquisition of a contributed asset."
    )
    private_acquisition_cost = fields.Monetary(tracking=True)
    private_useful_life_months = fields.Integer(
        tracking=True,
        help="Total useful life to reduce the private cost by the depreciation "
        "up to the contribution (§ 6 (1) no. 5 sentence 2 EStG); required if the "
        "asset was acquired within the last three years. The useful life of the "
        "asset is then its remaining useful life.",
    )
    contribution_prior_income_use = fields.Boolean(
        string="Used for Other Income Before",
        tracking=True,
        help="The contributed asset was used to earn income under § 2 (1) "
        "sentence 1 no. 4 to 7 EStG before (e.g. as an employee): the basis of "
        "the depreciation is reduced by the depreciation taken there (§ 7 (1) "
        "sentence 5 EStG). This case is outside the program scope.",
    )
    contribution_value = fields.Monetary(
        compute="_compute_cost",
        store=True,
        help="Fair value, at most the reduced private cost if the asset was "
        "acquired within the last three years (§ 6 (1) no. 5 EStG).",
    )
    acquisition_cost = fields.Monetary(
        string="Cost (Basis)",
        compute="_compute_cost",
        store=True,
        help="Cost of acquisition or production or value of the contribution: "
        "basis of the depreciation.",
    )
    limit_value = fields.Monetary(
        compute="_compute_cost",
        store=True,
        help="Net value compared with the limits of § 6 (2) and (2a) EStG.",
    )
    method = fields.Selection(
        METHODS,
        required=True,
        compute="_compute_method",
        store=True,
        readonly=False,
        precompute=True,
        tracking=True,
        help="Manual: depreciation per year as events with a reason, e.g. "
        "buildings under § 7 (5a) EStG. Special depreciation under § 7b EStG is "
        "an event of its own next to linear or manual depreciation.",
    )
    useful_life_months = fields.Integer(
        string="Useful Life (Months)",
        tracking=True,
        help="Useful life according to the official depreciation tables, e.g. "
        "84 months for cameras (AfA-Tabelle AV 6.14.4). Contribution: the "
        "remaining useful life from the contribution (R 7.4 (10) EStR), proposed "
        "from the private acquisition. Buildings with linear depreciation "
        "(§ 7 (4) sentence 1 EStG): 12 ÷ percentage, e.g. 400 months for 3 %, "
        "480 for 2.5 %, 600 for 2 %; later costs, subsidies and extraordinary "
        "depreciation change the basis of the percentage. Enter a remaining "
        "useful life with such an event only if the depreciation follows the "
        "remaining useful life (§ 7 (4) sentence 2 EStG).",
    )
    degressive_rate = fields.Float(
        digits=(16, 4),
        compute="_compute_degressive_rate",
        store=True,
        readonly=False,
        precompute=True,
        tracking=True,
        help="Degressive rate, at most the factor times the linear rate and the "
        "maximum rate valid on the delivery date.",
    )
    degressive_rate_max = fields.Boolean(
        string="Highest Degressive Rate",
        tracking=True,
        help="Depreciate with the highest rate allowed on the delivery date, "
        "exactly min(factor × 12 / useful life, maximum rate). A rate such as "
        "1/6 cannot be entered with four decimals.",
    )
    switch_to_linear_year = fields.Integer(
        string="Switch to Linear in",
        tracking=True,
        help="Year of the switch from degressive to linear depreciation. Empty: "
        "the switch happens in the first year in which the linear amount is "
        "at least the degressive one.",
    )
    memo_value = fields.Monetary(
        tracking=True,
        help="Memo value of 1 € that a takeover from another system brings "
        "along. New assets are depreciated in full (R 7.4 (3) EStR).",
    )
    opening_date = fields.Date(
        string="Takeover Date",
        tracking=True,
        help="31 December of the last year calculated in the previous system. "
        "Years up to this date are not calculated again.",
    )
    opening_book_value = fields.Monetary(string="Book Value at Takeover", tracking=True)
    opening_accumulated = fields.Monetary(
        string="Depreciation up to Takeover", tracking=True
    )
    move_line_ids = fields.Many2many(
        "account.move.line",
        "l10n_de_euer_asset_move_line_rel",
        "asset_id",
        "move_line_id",
        string="Journal Items",
        copy=False,
        help="Lines of the purchase (additions, input tax not deductible).",
    )
    cost_line_ids = fields.Many2many(
        "account.move.line",
        "l10n_de_euer_asset_cost_line_rel",
        "asset_id",
        "move_line_id",
        string="Running Costs",
        copy=False,
        help="Running costs of the asset (repair, insurance) for the use "
        "withdrawal of private use.",
    )
    disposal_date = fields.Date(tracking=True, copy=False)
    disposal_kind = fields.Selection(DISPOSAL_KINDS, tracking=True, copy=False)
    disposal_value = fields.Monetary(
        string="Proceeds or Fair Value",
        tracking=True,
        copy=False,
        help="Sale: proceeds (Kz 102 at receipt, from the payments). "
        "Withdrawal: fair value, Kz 102 and 122 in the year of withdrawal.",
    )
    disposal_invoice_id = fields.Many2one(
        "account.move",
        string="Sales Invoice",
        copy=False,
        domain="[('move_type', 'in', ('out_invoice', 'out_receipt'))]",
        help="Sales invoice of the business or one of its branches.",
    )
    disposal_receipt_date = fields.Date(
        string="Receipt of Proceeds",
        tracking=True,
        copy=False,
        help="Assets that are not depreciable and current assets become an "
        "expense when the proceeds are received (§ 4 (3) sentence 4 EStG). "
        "Proposal from the payment of the sales invoice; a sale against "
        "instalments (R 4.5 (5) EStR) is outside the program scope.",
    )
    disposal_vat_allowed = fields.Boolean(compute="_compute_disposal_vat_allowed")
    disposal_vat_amount = fields.Monetary(
        string="VAT on Withdrawal",
        compute="_compute_disposal_vat_amount",
        store=True,
        readonly=False,
        tracking=True,
        copy=False,
        help="VAT under § 3 (1b) UStG (Kz 140), only if input tax was deducted "
        "on the purchase. Proposal: fair value × 19 %; once the disposal is "
        "recorded the amount is no longer proposed again.",
    )
    pool_id = fields.Many2one(
        "l10n_de_euer.asset.pool",
        string="Asset Pool",
        readonly=True,
        copy=False,
        index=True,
        ondelete="restrict",
    )
    in_gwg_register = fields.Boolean(
        string="In Register of Low-Value Assets",
        compute="_compute_in_gwg_register",
        store=True,
        help="Low-value assets with a net cost above 250 € (§ 6 (2) sentence 4 EStG).",
    )
    event_ids = fields.One2many("l10n_de_euer.asset.event", "asset_id", copy=False)
    year_ids = fields.One2many(
        "l10n_de_euer.asset.year", "asset_id", string="Yearly Values", copy=False
    )
    book_value = fields.Monetary(
        string="Current Book Value", compute="_compute_book_value"
    )
    sec15a_help = fields.Text(
        string="§ 15a UStG", compute="_compute_sec15a_help", compute_sudo=True
    )
    note = fields.Text()

    _code_uniq = models.Constraint(
        "UNIQUE(company_id, code)",
        "The inventory number must be unique per business.",
    )
    _business_share_range = models.Constraint(
        "CHECK(business_share >= 0 AND business_share <= 1)",
        "The business share must be between 0 and 100 %.",
    )

    # ------------------------------------------------------------ computes
    @api.depends("is_battery_electric")
    def _compute_is_electric_or_plugin_hybrid(self):
        for asset in self:
            asset.is_electric_or_plugin_hybrid = (
                asset.is_electric_or_plugin_hybrid or asset.is_battery_electric
            )

    @api.depends("asset_kind")
    def _compute_method(self):
        """Only depreciable assets have a method; others get ``none``."""
        for asset in self:
            if asset.asset_kind != "depreciable":
                asset.method = "none"
            elif not asset.method or asset.method == "none":
                asset.method = "linear"

    @api.depends("degressive_rate_max", "method", "useful_life_months", "delivery_date")
    def _compute_degressive_rate(self):
        for asset in self:
            if asset.degressive_rate_max and asset.method == "degressive":
                asset.degressive_rate = float(asset._get_max_degressive_rate())
            else:
                asset.degressive_rate = asset.degressive_rate

    @api.depends("company_id", "delivery_date")
    def _compute_vat_treatment(self):
        for asset in self:
            regime = False
            if asset.company_id and asset.delivery_date:
                regime = asset.company_id._l10n_de_euer_get_vat_regime(
                    asset.delivery_date
                )
            asset.vat_treatment = "not_deductible" if regime == "ku" else "deductible"

    @api.depends(
        "cost_net",
        "input_tax",
        "vat_treatment",
        "origin",
        "contribution_fair_value",
        "private_acquisition_date",
        "private_acquisition_cost",
        "private_useful_life_months",
        "delivery_date",
    )
    def _compute_cost(self):
        for asset in self:
            net = to_decimal(asset.cost_net)
            tax = to_decimal(asset.input_tax)
            asset.cost_gross = float(net + tax)
            if asset.origin == "contribution":
                value = dep.contribution_value(
                    to_decimal(asset.contribution_fair_value),
                    to_decimal(asset.private_acquisition_cost)
                    if asset.private_acquisition_date
                    else None,
                    asset.private_acquisition_date,
                    asset.delivery_date,
                    asset.private_useful_life_months,
                )
                asset.contribution_value = float(value)
                asset.acquisition_cost = float(value)
                asset.limit_value = float(value)
                continue
            asset.contribution_value = 0.0
            cost = net + tax if asset.vat_treatment == "not_deductible" else net
            asset.acquisition_cost = float(cost)
            asset.limit_value = float(net)

    @api.depends("input_tax", "vat_treatment", "company_id", "disposal_date")
    def _compute_disposal_vat_allowed(self):
        for asset in self:
            allowed = asset.vat_treatment == "deductible" and asset.input_tax > 0
            if allowed and asset.disposal_date:
                regime = asset.company_id._l10n_de_euer_get_vat_regime(
                    asset.disposal_date
                )
                allowed = regime != "ku"
            asset.disposal_vat_allowed = allowed

    @api.depends("disposal_kind", "disposal_value", "disposal_vat_allowed")
    def _compute_disposal_vat_amount(self):
        for asset in self:
            if asset.disposal_kind != "withdrawal" or not asset.disposal_vat_allowed:
                asset.disposal_vat_amount = 0.0
            elif asset.state == "disposed":
                # the VAT of a recorded withdrawal is the user's amount; a
                # change of the input tax does not replace it by the proposal
                asset.disposal_vat_amount = asset.disposal_vat_amount
            else:
                asset.disposal_vat_amount = float(
                    dep.cents(to_decimal(asset.disposal_value) * STANDARD_VAT_RATE)
                )

    @api.depends("asset_kind", "limit_value", "delivery_date")
    def _compute_in_gwg_register(self):
        for asset in self:
            asset.in_gwg_register = (
                asset.asset_kind == "gwg"
                and bool(asset.delivery_date)
                and asset.limit_value > asset._get_parameter("gwg_register_limit")
            )

    @api.depends("year_ids.book_value_end", "year_ids.year")
    def _compute_book_value(self):
        today = fields.Date.context_today(self)
        for asset in self:
            years = asset.year_ids.filtered(lambda year: year.year <= today.year)
            last = max(years, key=lambda year: year.year, default=None)
            asset.book_value = last.book_value_end if last else 0.0

    @api.depends("input_tax", "vat_treatment", "delivery_date", "aveuer_group")
    def _compute_sec15a_help(self):
        for asset in self:
            asset.sec15a_help = asset._get_sec15a_text()

    def _get_sec15a_text(self, change_date=None):
        """Formula help for § 15a UStG (spec 6.3): amount per year."""
        self.ensure_one()
        if not self.input_tax or not self.delivery_date:
            return False
        change_date = change_date or self._get_regime_change_date()
        if not change_date:
            return False
        amounts, bagatelle = dep.sec15a_correction(
            to_decimal(self.input_tax),
            self.delivery_date,
            change_date,
            real_estate=self.aveuer_group in dep.REAL_ESTATE_GROUPS,
        )
        if not amounts:
            return False
        lines = [
            self.env._("Change of the VAT treatment on %(date)s.", date=change_date),
            self.env._(
                "Input tax correction (§ 15a UStG) for the VAT return, the cost "
                "stays unchanged (§ 9b (2) EStG):"
            ),
        ]
        lines += [f"{year}: {amount:.2f}" for year, amount in sorted(amounts.items())]
        if bagatelle:
            lines.append(
                self.env._(
                    "Input tax up to 1,000 €: bagatelle limit of § 44 (1) UStDV, "
                    "probably no correction."
                )
            )
        return "\n".join(lines)

    def _get_regime_change_date(self):
        """First change of the VAT treatment after the delivery, or False."""
        self.ensure_one()
        periods = self.env["l10n_de_euer.vat.period"].search(
            Domain("company_id", "=", self.company_id.id)
            & Domain("date_from", ">", self.delivery_date),
            order="date_from",
        )
        before = self.company_id._l10n_de_euer_get_vat_regime(self.delivery_date)
        for period in periods:
            if period.regime != before:
                return period.date_from
        return False

    # ------------------------------------------------------------ parameters
    def _get_parameter(self, code, on_date=None):
        """Rule parameter valid on the delivery date, ``0.0`` if there is none."""
        self.ensure_one()
        on_date = on_date or self.delivery_date
        try:
            return self.env["l10n_de_euer.parameter"].sudo()._get_value(code, on_date)
        except UserError:
            return 0.0

    def _get_max_degressive_rate(self):
        """Highest degressive rate on the delivery date (exact Decimal)."""
        self.ensure_one()
        if not self.delivery_date:
            return dep.ZERO
        return dep.max_degressive_rate(
            self._get_parameter("degressive_factor"),
            self._get_parameter("degressive_max_rate"),
            self.useful_life_months,
        )

    def _get_degressive_rate(self):
        """Rate for the engine: exact for the highest rate, else as entered."""
        self.ensure_one()
        if self.method != "degressive":
            return None
        if self.degressive_rate_max:
            return self._get_max_degressive_rate() or None
        return Decimal(str(self.degressive_rate)) if self.degressive_rate else None

    def _get_ev_rates(self):
        if self.method != "ev_7_2a":
            return ()
        rates = self._get_parameter("ev_rates")
        return tuple(rates) if isinstance(rates, list) else ()

    # ------------------------------------------------------------ engine input
    def _get_disposal_effective_date(self):
        self.ensure_one()
        if not self.disposal_date:
            return None
        if self.asset_kind in dep.CASH_DISPOSAL_KINDS and self.disposal_kind == "sale":
            return self.disposal_receipt_date or None
        return self.disposal_date

    def _get_engine_asset(self):
        """Input of :mod:`.engine.depreciation` for this asset."""
        self.ensure_one()
        disposal_date = self.disposal_date if self.state == "disposed" else None
        effective = self._get_disposal_effective_date() if disposal_date else None
        if disposal_date and not effective:
            # sale of an asset that is not depreciable: proceeds not received yet
            disposal_date = None
        return dep.Asset(
            kind=self.asset_kind,
            method=self.method,
            delivery_date=self.delivery_date,
            cost=to_decimal(self.acquisition_cost),
            useful_life_months=self.useful_life_months,
            degressive_rate=self._get_degressive_rate(),
            switch_to_linear_year=self.switch_to_linear_year or None,
            events=tuple(event._get_engine_event() for event in self.event_ids),
            disposal_date=disposal_date,
            disposal_effective_date=effective,
            disposal_kind=self.disposal_kind if disposal_date else None,
            disposal_month_counts=self.company_id.sudo().root_id.l10n_de_euer_disposal_month,
            memo_value=to_decimal(self.memo_value),
            opening_date=self.opening_date or None,
            opening_book_value=to_decimal(self.opening_book_value)
            if self.opening_date
            else None,
            ev_rates=self._get_ev_rates(),
            group=self.aveuer_group,
        )

    # ------------------------------------------------------------ messages
    def _engine_message(self, error):
        """Translated text of a :class:`~.engine.depreciation.DepreciationError`."""
        params = error.params
        texts = {
            "negative_cost": lambda: self.env._("The cost must not be negative."),
            "memo": lambda: self.env._("The memo value must be below the cost."),
            "no_method": lambda: self.env._(
                "A depreciable asset needs a depreciation method."
            ),
            "method_not_allowed": lambda: self.env._(
                "Only depreciable assets have a depreciation method."
            ),
            "no_life": lambda: self.env._(
                "Linear and degressive depreciation need a useful life."
            ),
            "short_life": lambda: self.env._(
                "With a useful life of up to 12 months the cost is not spread but "
                "an expense of the year (H 7.4 EStH); such an item is no asset of "
                "the register. Computer hardware and software: use the method "
                "Digital Asset, One Year."
            ),
            "no_rate": lambda: self.env._("Degressive depreciation needs a rate."),
            "switch_year": lambda: self.env._(
                "The switch to linear depreciation takes effect from the year "
                "after the delivery (%(year)s) on. A switch in the year of "
                "delivery means linear depreciation: choose the method Linear.",
                **params,
            ),
            "no_ev_rates": lambda: self.env._(
                "§ 7 (2a) EStG applies to electric vehicles delivered from "
                "1 July 2025 to 31 December 2027 only."
            ),
            "takeover_date": lambda: self.env._(
                "The takeover date must be a 31 December."
            ),
            "takeover_before_delivery": lambda: self.env._(
                "The takeover date is before the delivery."
            ),
            "disposal_before_delivery": lambda: self.env._(
                "The disposal cannot be before the delivery."
            ),
            "disposal_before_takeover": lambda: self.env._(
                "The asset left before the takeover date."
            ),
            "event_before_delivery": lambda: self.env._(
                "An event cannot be dated before the delivery."
            ),
            "event_kind": lambda: self.env._(
                "Extraordinary depreciation and a new useful life are for "
                "depreciable assets only (§ 7 (1) sentence 7 EStG); land, "
                "financial and current assets become an expense at the sale or "
                "withdrawal."
            ),
            "extraordinary_degressive": lambda: self.env._(
                "No extraordinary depreciation next to degressive depreciation "
                "(§ 7 (2) sentence 4 EStG)."
            ),
            "ev_events": lambda: self.env._(
                "§ 7 (2a) EStG: only events in the year of delivery are supported."
            ),
            "life_change_first_year": lambda: self.env._(
                "Change the useful life of the asset itself in the year of delivery."
            ),
            "life_change_months": lambda: self.env._(
                "Enter the remaining useful life in months."
            ),
            "manual_method": lambda: self.env._(
                "Manual depreciation needs the depreciation method Manual."
            ),
            "event_amount": lambda: self.env._(
                "The amount of the event must be positive."
            ),
            "event_life": lambda: self.env._(
                "The remaining useful life of an event must not be negative."
            ),
            "special_movable": lambda: self.env._(
                "§ 7g (5) EStG applies to movable assets only."
            ),
            "special_method": lambda: self.env._(
                "§ 7g (5) EStG: special depreciation only next to linear or "
                "degressive depreciation."
            ),
            "special_period": lambda: self.env._(
                "§ 7g (5) EStG: special depreciation only in the year of delivery "
                "and the four following years."
            ),
            "special_limit": lambda: self.env._(
                "The special depreciation of %(total)s exceeds %(percent)s %% of "
                "the cost (§ 7g (5) EStG).",
                **params,
            ),
            "sec7b_group": lambda: self.env._(
                "§ 7b EStG applies to buildings (new rented flats) only."
            ),
            "sec7b_method": lambda: self.env._(
                "§ 7b EStG: special depreciation only next to linear or manual "
                "depreciation (§ 7 (4) or (5a) EStG)."
            ),
            "sec7b_period": lambda: self.env._(
                "§ 7b EStG: special depreciation only in the year of delivery and "
                "the three following years."
            ),
            "sec7b_limit": lambda: self.env._(
                "The special depreciation of %(total)s in %(year)s exceeds "
                "%(percent)s %% of the cost (§ 7b (1) EStG).",
                **params,
            ),
            "exceeds_book_value": lambda: self.env._(
                "The depreciation of %(year)s exceeds the book value.", **params
            ),
            "subsidy_exceeds_book_value": lambda: self.env._(
                "The subsidy of %(amount)s in %(year)s exceeds the book value of "
                "%(book_value)s. A subsidy reduces the cost at most to zero: enter "
                "at most the book value as subsidy and book the rest as business "
                "income (R 6.5 (2), (3) EStR).",
                **params,
            ),
            "group_without_depreciation": lambda: self.env._(
                "The AVEÜR group %(group)s has no depreciation.", **params
            ),
        }
        text = texts.get(error.code, lambda: str(error))()
        if not self:
            return text
        return self.env._("%(asset)s: %(error)s", asset=self.display_name, error=text)

    # ------------------------------------------------------------ constraints
    @api.constrains(
        "asset_kind",
        "aveuer_group",
        "method",
        "origin",
        "memo_value",
        "independent_use",
        "is_electric_or_plugin_hybrid",
        "is_battery_electric",
        "is_trivial_program",
        "cost_net",
        "input_tax",
        "vat_treatment",
        "delivery_date",
        "contribution_fair_value",
        "private_acquisition_date",
        "private_acquisition_cost",
    )
    def _check_kind(self):
        for asset in self:
            asset._check_kind_group()
            asset._check_limits()
            if asset.memo_value and asset.origin != "legacy_import":
                raise ValidationError(
                    self.env._(
                        "%(asset)s: only an asset taken over from another system "
                        "may keep a memo value; new assets are depreciated in "
                        "full (R 7.4 (3) EStR).",
                        asset=asset.display_name,
                    )
                )

    def _check_kind_group(self):
        kind, group, method = self.asset_kind, self.aveuer_group, self.method
        errors = []
        if kind == "current_4_3_4" and group != "current":
            errors.append(self.env._("Current assets belong to their own group."))
        if group == "current" and kind != "current_4_3_4":
            errors.append(self.env._("The group of current assets needs that kind."))
        if group in dep.NON_DEPRECIABLE_GROUPS and kind != "non_depreciable":
            errors.append(self.env._("Land and financial assets are not depreciable."))
        if kind in dep.IMMEDIATE_KINDS | {"pool"} and (
            group not in dep.MOVABLE_GROUPS | {"intangible"}
        ):
            errors.append(
                self.env._(
                    "Low-value assets and asset pools are movable assets "
                    "(§ 6 (2) EStG)."
                )
            )
        if (
            kind in dep.IMMEDIATE_KINDS | {"pool"}
            and group == "intangible"
            and not self.is_trivial_program
        ):
            errors.append(
                self.env._(
                    "An intangible asset is a low-value asset or a pool asset only "
                    "as a trivial program (R 5.5 (1) EStR)."
                )
            )
        if kind == "depreciable" and method == "none":
            errors.append(self.env._("Choose a depreciation method."))
        if method == "degressive" and group not in dep.MOVABLE_GROUPS:
            errors.append(
                self.env._("Degressive depreciation is for movable assets only.")
            )
        if method == "ev_7_2a" and (group != "vehicle" or not self.is_battery_electric):
            errors.append(
                self.env._(
                    "§ 7 (2a) EStG is for purely electric vehicles only (§ 9 (2) "
                    "KraftStG), not for plug-in hybrids."
                )
            )
        if method == "ev_7_2a" and self.origin not in ACQUIRED_ORIGINS:
            errors.append(
                self.env._("§ 7 (2a) EStG applies to acquired vehicles only.")
            )
        if method == "digital_1y" and group not in DIGITAL_GROUPS:
            errors.append(
                self.env._(
                    "The useful life of one year is for computer hardware and "
                    "software only (BMF 22.02.2022)."
                )
            )
        if errors:
            raise ValidationError(
                self.env._(
                    "%(asset)s: %(error)s",
                    asset=self.display_name,
                    error=" ".join(errors),
                )
            )

    def _check_limits(self):
        """Limits of § 6 (2) and (2a) EStG on the net value (R-30, R-31)."""
        kind = self.asset_kind
        if kind not in dep.IMMEDIATE_KINDS | {"pool"}:
            return
        if kind == "pool" and self.origin == "legacy_import":
            # a takeover may bring a whole pool of a year as one line
            return
        if not self.independent_use:
            raise ValidationError(
                self.env._(
                    "%(asset)s is not independently usable: no low-value asset "
                    "and no asset pool (§ 6 (2) sentence 2 EStG).",
                    asset=self.display_name,
                )
            )
        value = self.limit_value
        currency = self.currency_id
        if self.aveuer_group == "intangible":
            limit = self._get_parameter("trivial_program_limit")
            if currency.compare_amounts(value, limit) > 0:
                raise ValidationError(
                    self.env._(
                        "%(asset)s: a computer program above %(limit).2f is no "
                        "trivial program (R 5.5 (1) sentence 3 EStR).",
                        asset=self.display_name,
                        limit=limit,
                    )
                )
        limits = {
            "gwg": (None, self._get_parameter("gwg_limit")),
            "instant_250": (None, self._get_parameter("gwg_register_limit")),
            "pool": (
                self._get_parameter("pool_min"),
                self._get_parameter("pool_max"),
            ),
        }
        lower, upper = limits[kind]
        too_high = currency.compare_amounts(value, upper) > 0
        too_low = lower is not None and currency.compare_amounts(value, lower) <= 0
        if too_high or too_low:
            raise ValidationError(
                self.env._(
                    "%(asset)s: the net value %(value).2f is outside the limits "
                    "of this kind (above %(lower).2f up to %(upper).2f).",
                    asset=self.display_name,
                    value=value,
                    lower=lower or 0.0,
                    upper=upper,
                )
            )

    @api.constrains(
        "degressive_rate",
        "degressive_rate_max",
        "method",
        "useful_life_months",
        "delivery_date",
        "switch_to_linear_year",
        "opening_date",
    )
    def _check_degressive_rate(self):
        for asset in self.filtered(lambda a: a.method == "degressive"):
            if (
                asset.switch_to_linear_year
                and asset.delivery_date
                and asset.switch_to_linear_year <= asset.delivery_date.year
            ):
                raise ValidationError(
                    asset._engine_message(
                        dep.DepreciationError(
                            "switch_year", year=asset.delivery_date.year + 1
                        )
                    )
                )
            if (
                asset.opening_date
                and asset.switch_to_linear_year
                and asset.switch_to_linear_year <= asset.opening_date.year
            ):
                # the previous system already depreciates linearly: the rate
                # no longer matters (§ 7 (3) EStG)
                continue
            if asset.origin == "legacy_import" and not asset._has_degressive_rules():
                raise ValidationError(
                    self.env._(
                        "%(asset)s: there are no rule parameters of degressive "
                        "depreciation for a delivery on %(date)s, so the rate "
                        "cannot be checked. If the previous system already "
                        "depreciates linearly, enter the year of the switch; "
                        "otherwise add the rule parameters of that time.",
                        asset=asset.display_name,
                        date=asset.delivery_date,
                    )
                )
            maximum = asset._get_max_degressive_rate()
            if asset.degressive_rate_max:
                rate = maximum
            else:
                rate = Decimal(str(asset.degressive_rate))
            if not maximum or rate > maximum or rate <= 0:
                raise ValidationError(
                    self.env._(
                        "%(asset)s: degressive depreciation with %(rate)s %% is not "
                        "allowed for a delivery on %(date)s (maximum %(max)s %%).",
                        asset=asset.display_name,
                        rate=f"{rate * 100:.2f}",
                        date=asset.delivery_date,
                        max=f"{maximum * 100:.2f}",
                    )
                )

    @api.constrains(
        "origin",
        "asset_kind",
        "method",
        "delivery_date",
        "private_acquisition_date",
        "private_useful_life_months",
        "useful_life_months",
    )
    def _check_contribution(self):
        """§ 6 (1) no. 5 sentence 2 EStG and R 7.4 (10) EStR: within three
        years the private cost is reduced by the depreciation over the total
        useful life; the depreciation after the contribution follows the
        remaining useful life."""
        for asset in self.filtered(lambda a: a.origin == "contribution"):
            depreciable = asset.asset_kind not in dep.CASH_DISPOSAL_KINDS
            if (
                depreciable
                and not asset.private_useful_life_months
                and dep.contribution_reduced(
                    asset.private_acquisition_date, asset.delivery_date
                )
            ):
                raise ValidationError(
                    self.env._(
                        "%(asset)s was acquired privately within the last three "
                        "years before the contribution: enter its total useful "
                        "life, the private cost is reduced by the depreciation up "
                        "to the contribution (§ 6 (1) no. 5 sentence 2 EStG).",
                        asset=asset.display_name,
                    )
                )
            if (
                asset.method in ("linear", "degressive")
                and asset.private_useful_life_months
                and asset.useful_life_months >= asset.private_useful_life_months
            ):
                raise ValidationError(
                    self.env._(
                        "%(asset)s: after a contribution the asset is depreciated "
                        "over its remaining useful life (R 7.4 (10) EStR), which is "
                        "shorter than the total useful life of %(total)s months "
                        "(proposal: %(remaining)s months).",
                        asset=asset.display_name,
                        total=asset.private_useful_life_months,
                        remaining=asset._get_contribution_remaining_life(),
                    )
                )

    def _get_contribution_remaining_life(self):
        """Proposed remaining useful life of a contribution in months, or 0."""
        self.ensure_one()
        if self.origin != "contribution":
            return 0
        return dep.contribution_remaining_life(
            self.private_acquisition_date,
            self.delivery_date,
            self.private_useful_life_months,
        )

    @api.onchange(
        "origin",
        "delivery_date",
        "private_acquisition_date",
        "private_useful_life_months",
    )
    def _onchange_contribution_life(self):
        """Propose the remaining useful life of a contribution (R 7.4 (10))."""
        remaining = self._get_contribution_remaining_life()
        if remaining:
            self.useful_life_months = remaining

    @api.onchange("opening_book_value", "acquisition_cost")
    def _onchange_opening_book_value(self):
        """Propose the depreciation up to the takeover: cost less book value."""
        if self.opening_date and self.opening_book_value <= self.acquisition_cost:
            self.opening_accumulated = self.acquisition_cost - self.opening_book_value

    def _has_degressive_rules(self):
        """Whether rule parameters of degressive depreciation cover the delivery."""
        self.ensure_one()
        Parameter = self.env["l10n_de_euer.parameter"].sudo()
        try:
            Parameter._get_value("degressive_factor", self.delivery_date)
        except UserError:
            return False
        return True

    @api.constrains("vat_treatment", "input_tax", "delivery_date", "company_id")
    def _check_vat_treatment(self):
        """A small business cannot deduct input tax (§ 19 (1) sentence 4 UStG)."""
        for asset in self:
            if not asset.delivery_date or not asset.input_tax:
                continue
            regime = asset.company_id._l10n_de_euer_get_vat_regime(asset.delivery_date)
            if regime == "ku" and asset.vat_treatment != "not_deductible":
                raise ValidationError(
                    self.env._(
                        "%(asset)s: on %(date)s the business is a small business; "
                        "the input tax is not deductible and part of the cost "
                        "(R 9b (1) EStR).",
                        asset=asset.display_name,
                        date=asset.delivery_date,
                    )
                )

    @api.constrains(
        "disposal_kind",
        "disposal_vat_amount",
        "disposal_date",
        "input_tax",
        "vat_treatment",
    )
    def _check_disposal_vat(self):
        for asset in self:
            if asset.disposal_vat_amount and not (
                asset.disposal_kind == "withdrawal" and asset.disposal_vat_allowed
            ):
                raise ValidationError(
                    self.env._(
                        "%(asset)s: VAT on a withdrawal only if input tax was "
                        "deducted on the purchase and the business is not a small "
                        "business (§ 3 (1b), § 19 UStG).",
                        asset=asset.display_name,
                    )
                )

    @api.constrains(
        "move_line_ids", "cost_line_ids", "disposal_invoice_id", "company_id"
    )
    def _check_line_company(self):
        for asset in self:
            lines = asset.move_line_ids | asset.cost_line_ids
            companies = lines.company_id | asset.disposal_invoice_id.company_id
            if companies - asset.company_id._l10n_de_euer_business_companies():
                raise ValidationError(
                    self.env._(
                        "%(asset)s: journal items of another business.",
                        asset=asset.display_name,
                    )
                )

    def _check_engine(self):
        """Validate with the depreciation rules (raises ValidationError)."""
        for asset in self.filtered(lambda a: a.state != "draft"):
            try:
                engine_asset = asset._get_engine_asset()
                dep.validate(engine_asset)
                types = {event.type for event in engine_asset.events}
                if "sec7g_special" in types:
                    dep.check_special_limit(
                        engine_asset, asset._get_parameter("sec7g_special_rate")
                    )
                if "sec7b_special" in types:
                    dep.check_sec7b_limit(
                        engine_asset, asset._get_parameter("sec7b_special_rate")
                    )
                dep.schedule(engine_asset)
            except dep.DepreciationError as error:
                raise ValidationError(asset._engine_message(error)) from error
            asset._check_disposal_fields()
            asset._check_opening_values(engine_asset)

    def _check_opening_values(self, engine_asset):
        """Book value at the takeover = cost less the depreciation up to the
        takeover (spec 6.6), as the import checks it."""
        self.ensure_one()
        if not self.opening_date:
            return
        cost = dep.basis(engine_asset, self.opening_date.year)
        book_value = to_decimal(self.opening_book_value)
        accumulated = to_decimal(self.opening_accumulated)
        if book_value < 0 or book_value > cost:
            message = self.env._(
                "%(asset)s: the book value at the takeover %(book).2f must be "
                "between 0 and the cost %(cost).2f.",
                asset=self.display_name,
                book=book_value,
                cost=cost,
            )
        elif cost - accumulated != book_value:
            message = self.env._(
                "%(asset)s: book value %(book).2f is not cost %(cost).2f less the "
                "depreciation up to the takeover %(acc).2f.",
                asset=self.display_name,
                book=book_value,
                cost=cost,
                acc=accumulated,
            )
        elif to_decimal(self.memo_value) > book_value:
            message = self.env._(
                "%(asset)s: the memo value exceeds the book value at the takeover.",
                asset=self.display_name,
            )
        else:
            return
        raise ValidationError(message)

    def _check_disposal_fields(self):
        self.ensure_one()
        if self.state == "disposed" and not (self.disposal_date and self.disposal_kind):
            raise ValidationError(
                self.env._(
                    "%(asset)s: enter the date and kind of the disposal.",
                    asset=self.display_name,
                )
            )
        if (
            self.disposal_kind == "sale"
            and self.asset_kind in dep.CASH_DISPOSAL_KINDS
            and self.state == "disposed"
            and not self.disposal_receipt_date
        ):
            raise ValidationError(
                self.env._(
                    "%(asset)s: enter the date the proceeds were received; until "
                    "then the asset stays in the register (§ 4 (3) sentence 4 "
                    "EStG).",
                    asset=self.display_name,
                )
            )

    def _get_sec7g_issues(self):
        """Conditions of § 7g (6) no. 2 EStG the asset does not meet.

        Special depreciation under § 7g (5) needs an (almost) exclusive
        business use of at least 90 % in the year of delivery and the
        following year and the asset must stay in the business until the end
        of that year. Returns a list of translated texts.
        """
        self.ensure_one()
        if not self.event_ids.filtered(lambda e: e.type == "sec7g_special"):
            return []
        issues = []
        end = self.delivery_date.year + 1
        shares = [self.business_share] + self.event_ids.filtered(
            lambda e: e.type == "business_use_change" and e.date.year <= end
        ).mapped("business_share")
        if min(shares) < SEC7G_MIN_BUSINESS_SHARE:
            issues.append(
                self.env._(
                    "the business use in %(first)s and %(end)s is below 90 %% "
                    "(§ 7g (6) no. 2 EStG)",
                    first=self.delivery_date.year,
                    end=end,
                )
            )
        if (
            self.state == "disposed"
            and self.disposal_date
            and self.disposal_date < fields.Date.to_date(f"{end}-12-31")
        ):
            issues.append(
                self.env._(
                    "the asset left the business before the end of %(end)s "
                    "(§ 7g (6) no. 2 EStG)",
                    end=end,
                )
            )
        return issues

    # ------------------------------------------------------------ years
    def _compute_years(self, until=None):
        """Store the yearly values of the assets; locked years never change.

        The values are stored up to the end of the schedule, at least up to
        the locked year of the business (or ``until``) as long as the asset
        is present: a locked year keeps a line also for an asset that only
        fills the cost column there (fully depreciated, not depreciable,
        current assets), so a later disposal or event in an open year does
        not have to create lines in locked years.
        """
        locked = self._get_locked_year() if self else {}
        for asset in self:
            if asset.state == "draft":
                asset.year_ids.filtered(lambda year: not year.locked)._unlink_internal()
                continue
            locked_year = locked.get(asset.company_id.id, 0)
            horizon = max(until or 0, locked_year)
            computed = {
                row.year: asset.year_ids._values_from_engine(row, asset.aveuer_group)
                for row in asset._get_schedule(horizon)
            }
            if asset.opening_date:
                computed[asset.opening_date.year] = asset._get_opening_year_values()
            asset._store_years(computed, locked_year)
            asset.year_ids._compute_private_use()

    def _get_opening_year_values(self):
        """Line of the takeover year: only the book value at its end."""
        self.ensure_one()
        return {
            "year": self.opening_date.year,
            "aveuer_group": self.aveuer_group,
            "is_opening": True,
            "cost_start": 0.0,
            "book_value_start": 0.0,
            "additions": 0.0,
            "special_depreciation": 0.0,
            "depreciation": 0.0,
            "disposal_book_value": 0.0,
            "book_value_end": self.opening_book_value,
            "months_in_year": 0,
        }

    def _store_years(self, computed, locked_year):
        self.ensure_one()
        Year = self.env["l10n_de_euer.asset.year"]
        existing = {year.year: year for year in self.year_ids}
        for number, record in existing.items():
            if record.locked:
                record._check_unchanged(computed.get(number))
            elif number not in computed:
                record._unlink_internal()
        for number, values in computed.items():
            record = existing.get(number)
            if record and record.locked:
                continue
            values = {"is_opening": False, **values}
            if number <= locked_year and not values["is_opening"]:
                if record or not self._is_carried_forward(values):
                    raise UserError(
                        self.env._(
                            "%(asset)s changes the locked year %(year)s. Enter it "
                            "as a correction of that year.",
                            asset=self.display_name,
                            year=number,
                        )
                    )
                # a year locked before its line existed (the asset only
                # carried its values forward): the line keeps them, locked
                values["locked"] = True
            if record:
                record._write_internal(values)
            else:
                Year._create_internal({"asset_id": self.id, **values})

    def _is_carried_forward(self, values):
        """Whether yearly ``values`` only carry the book value forward."""
        currency = self.currency_id
        moved = any(
            currency.compare_amounts(values[name], 0.0)
            for name in (
                "additions",
                "special_depreciation",
                "depreciation",
                "disposal_book_value",
            )
        )
        return not moved and not currency.compare_amounts(
            values["book_value_start"], values["book_value_end"]
        )

    def _get_schedule(self, horizon=0):
        """Schedule up to its end, at least up to ``horizon`` while present."""
        self.ensure_one()
        try:
            engine_asset = self._get_engine_asset()
            rows = dep.schedule(engine_asset)
            if horizon and (not rows or rows[-1].year < horizon):
                rows = dep.schedule(engine_asset, until=horizon)
            return rows
        except dep.DepreciationError as error:
            raise ValidationError(self._engine_message(error)) from error

    def _get_locked_year(self):
        """Last locked year of the business of each asset (0 if none).

        Asset years and pools are locked when a statement is finalised (E4,
        :meth:`_lock_years`); an extension may add further sources.
        """
        companies = self.company_id
        result = {
            company.id: company.sudo().l10n_de_euer_asset_lock_year or 0
            for company in companies
        }
        groups = (
            self.env["l10n_de_euer.asset.year"]
            .sudo()
            ._read_group(
                Domain("company_id", "in", companies.ids) & Domain("locked", "=", True),
                ["company_id"],
                ["year:max"],
            )
        )
        for company, year in groups:
            result[company.id] = max(result[company.id], year or 0)
        pools = (
            self.env["l10n_de_euer.asset.pool"]
            .sudo()
            ._read_group(
                Domain("company_id", "in", companies.ids) & Domain("locked", "=", True),
                ["company_id"],
                ["year:max"],
            )
        )
        for company, year in pools:
            result[company.id] = max(result[company.id], year or 0)
        return result

    def _check_not_in_locked_year(self):
        """New assets and pool members must not change a locked year."""
        locked = self._get_locked_year()
        for asset in self:
            first = asset.opening_date.year + 1 if asset.opening_date else 0
            first = first or asset.delivery_date.year
            if first <= locked.get(asset.company_id.id, 0):
                raise UserError(
                    self.env._(
                        "%(asset)s changes the locked year %(year)s. Enter it as a "
                        "correction of that year.",
                        asset=asset.display_name,
                        year=first,
                    )
                )

    # ------------------------------------------------------------ pools
    def _assign_pool(self):
        """Put pool assets into the pool of their year of delivery."""
        Pool = self.env["l10n_de_euer.asset.pool"].sudo()
        touched = Pool.browse()
        for asset in self:
            old = asset.pool_id
            new = Pool.browse()
            if asset.asset_kind == "pool" and asset.state != "draft":
                year = asset.delivery_date.year
                if old.year == year and old.company_id == asset.company_id:
                    new = old
                else:
                    new = Pool.search(
                        Domain("company_id", "=", asset.company_id.id)
                        & Domain("year", "=", year),
                        limit=1,
                    ) or Pool._create_internal(
                        {"company_id": asset.company_id.id, "year": year}
                    )
            if new != old:
                asset._write_internal({"pool_id": new.id})
            touched |= old | new
        touched._check_amount_unchanged()

    def _write_internal(self, values):
        """Write fields the register calculates itself (:data:`COMPUTED_FIELDS`)."""
        return self.sudo().with_context(**{INTERNAL: True}).write(values)

    @api.model
    def _is_internal(self):
        return self.env.su and bool(self.env.context.get(INTERNAL))

    # ------------------------------------------------------------ actions
    def action_activate(self):
        """Enter the asset in the register and calculate its years."""
        for asset in self:
            if asset.state != "draft":
                raise UserError(
                    self.env._(
                        "%(asset)s is already in the register.",
                        asset=asset.display_name,
                    )
                )
        self._check_not_in_locked_year()
        self.write({"state": "active"})
        return True

    def action_dispose(self):
        """Mark the asset as disposed with the disposal data entered."""
        for asset in self:
            if asset.state != "active":
                raise UserError(
                    self.env._(
                        "Only assets in the register can be disposed: %(asset)s.",
                        asset=asset.display_name,
                    )
                )
        self.write({"state": "disposed"})
        return True

    def action_set_draft(self):
        self.write({"state": "draft"})
        return True

    def action_compute_years(self):
        # the yearly values are written as superuser: only users who may
        # change the assets may calculate them (readonly auditors may not)
        self.check_access("write")
        self._compute_years()
        return True

    def action_open_years(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_asset_year"
        )
        action["domain"] = [("asset_id", "=", self.id)]
        action["context"] = {"default_asset_id": self.id}
        return action

    def _get_journal_items(self):
        """Journal items behind the asset: purchase, running costs and the
        sales invoice of a disposal (spec 7.6 no. 3)."""
        lines = self.move_line_ids | self.cost_line_ids
        invoices = self.disposal_invoice_id
        lines |= invoices.line_ids.filtered(
            lambda line: line.display_type in ("product", "tax")
        )
        return lines

    def action_open_move_lines(self):
        """Journal items of the asset (spec 7.6 no. 3)."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Journal Items of %(asset)s", asset=self.display_name),
            "res_model": "account.move.line",
            "view_mode": "list,form",
            "domain": [("id", "in", self._get_journal_items().ids)],
            "context": {"create": False},
        }

    @api.onchange("disposal_invoice_id")
    def _onchange_disposal_invoice_id(self):
        """Propose the receipt of the proceeds from the paid sales invoice."""
        payments = self._get_disposal_payments()
        if payments and self.disposal_invoice_id.payment_state in (
            "paid",
            "in_payment",
        ):
            self.disposal_receipt_date = max(day for day, _amount in payments)

    def _get_disposal_payments(self):
        """``[(date, amount)]`` of the payments of the sales invoice.

        The date is the later date of the reconciled items (payment or bank
        statement line), the amount in company currency.
        """
        self.ensure_one()
        invoice = self.disposal_invoice_id
        if not invoice:
            return []
        terms = invoice.line_ids.filtered(
            lambda line: line.display_type == "payment_term"
        )
        partials = terms.matched_credit_ids | terms.matched_debit_ids
        return [(partial.max_date, partial.amount) for partial in partials]

    # ------------------------------------------------------------ ORM
    @api.model
    def _business_company_vals(self, vals):
        """The register belongs to the business: the main company with its
        branches or a branch that is a business of its own (spec 7.9)."""
        if vals.get("company_id"):
            company = self.env["res.company"].browse(vals["company_id"])
            business = company._l10n_de_euer_business_root()
            if business != company:
                vals = {**vals, "company_id": business.id}
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [self._business_company_vals(vals) for vals in vals_list]
        for vals in vals_list:
            self._check_computed_fields(vals)
        assets = super().create(vals_list)
        active = assets.filtered(lambda asset: asset.state != "draft")
        active._check_takeover_rights()
        active._check_not_in_locked_year()
        assets._after_change()
        return assets

    def write(self, vals):
        vals = self._business_company_vals(vals)
        self._check_computed_fields(vals)
        self._check_takeover_write(vals)
        self._check_locked_write(vals)
        locked_values = self._get_locked_form_values(vals)
        if "state" in vals and vals["state"] != "draft":
            to_check = self.filtered(lambda asset: asset.state == "draft")
        else:
            to_check = self.browse()
        result = super().write(vals)
        if to_check:
            to_check._check_takeover_rights()
            to_check._check_not_in_locked_year()
        if SCHEDULE_FIELDS.intersection(vals) or "state" in vals:
            self._after_change()
        self._check_locked_form_values(locked_values)
        return result

    def _check_computed_fields(self, vals):
        """What the register calculates is never written from outside."""
        protected = COMPUTED_FIELDS.intersection(vals)
        if protected and not self._is_internal():
            raise UserError(
                self.env._(
                    "%(fields)s are calculated by the asset register.",
                    fields=", ".join(
                        self._fields[name].get_description(self.env)["string"]
                        for name in sorted(protected)
                    ),
                )
            )

    def _check_takeover_write(self, vals):
        """Changing a takeover in the register needs a manager as well (7.10)."""
        if not TAKEOVER_FIELDS.intersection(vals) or self._is_takeover_manager():
            return
        for asset in self.filtered(lambda asset: asset.state != "draft"):
            before = asset.opening_date or asset.origin in TAKEOVER_ORIGINS
            after = (
                vals.get("opening_date", asset.opening_date)
                or vals.get("origin", asset.origin) in TAKEOVER_ORIGINS
            )
            if before or after:
                raise UserError(
                    self.env._(
                        "Only an accounting manager can change an asset taken "
                        "over from another system in the register: %(asset)s.",
                        asset=asset.display_name,
                    )
                )

    def _is_takeover_manager(self):
        return self.env.su or self.env.user.has_group("account.group_account_manager")

    def _get_locked_form_values(self, vals):
        """Form values of the locked years of the assets before a change.

        Returns ``{asset id: {year: values}}`` for the changes that are not
        :data:`NEUTRAL_FIELDS`, see :meth:`_get_form_values_of_year`.
        """
        changed = {
            name
            for name in vals
            if name not in NEUTRAL_FIELDS
            and not name.startswith(("message_", "activity_"))
        }
        if not changed:
            return {}
        locked = self._get_locked_year()
        result = {}
        for asset in self.filtered(lambda asset: asset.state != "draft"):
            lock = locked.get(asset.company_id.id, 0)
            first = asset.opening_date.year + 1 if asset.opening_date else 0
            first = first or asset.delivery_date.year
            result[asset.id] = {
                year: asset._get_form_values_of_year(year)
                for year in range(first, lock + 1)
            }
        return result

    def _check_locked_form_values(self, before):
        """Raise if a change alters the form values of a locked year (7.7)."""
        for asset in self.filtered(lambda asset: asset.id in before):
            for year, values in before[asset.id].items():
                if asset._get_form_values_of_year(year) != values:
                    raise UserError(
                        self.env._(
                            "%(asset)s: the change alters the values of the locked "
                            "year %(year)s. Record it as a correction of that year.",
                            asset=asset.display_name,
                            year=year,
                        )
                    )

    def _get_form_values_of_year(self, year):
        """Field contributions and Kz 407 of the asset in ``year``, or ``None``
        if they cannot be calculated (the change is then refused anyway)."""
        self.ensure_one()
        if self.state == "draft":
            return {}
        entry = self._get_register_entry(year)
        try:
            values = dep.field_contributions(year, [entry])
            electric = dep.electric_vehicle_present(year, [entry])
        except dep.DepreciationError:
            return None
        return {**values, dep.FIELD_ELECTRIC: electric}

    def _check_locked_write(self, vals):
        """Protect what locked years depend on (spec 7.7), for every way of
        writing: back to draft, another business, values outside the yearly
        values (withdrawal, Kz 407, home office)."""
        if not ({"state", "company_id"} | LOCKED_INPUT_FIELDS).intersection(vals):
            return
        locked_year = self._get_locked_year()
        for asset in self:
            locked_lines = asset.year_ids.filtered("locked")
            if (
                vals.get("state") == "draft"
                and asset.state != "draft"
                and (locked_lines)
            ):
                raise UserError(
                    self.env._(
                        "%(asset)s has locked years and stays in the register.",
                        asset=asset.display_name,
                    )
                )
            if (
                "company_id" in vals
                and vals["company_id"] != asset.company_id.id
                and (asset.state != "draft" or locked_lines)
            ):
                raise UserError(
                    self.env._(
                        "%(asset)s is in the register of its business and cannot "
                        "move to another one. Record a disposal and a new asset.",
                        asset=asset.display_name,
                    )
                )
            changed = [
                name
                for name in LOCKED_INPUT_FIELDS.intersection(vals)
                if asset._input_changes(name, vals[name])
            ]
            for name in changed:
                year = asset._get_locked_input_year(name, locked_year)
                if year:
                    raise UserError(
                        self.env._(
                            "%(asset)s: the year %(year)s is locked; %(field)s "
                            "cannot change. Record it as a correction.",
                            asset=asset.display_name,
                            year=year,
                            field=asset._fields[name].get_description(self.env)[
                                "string"
                            ],
                        )
                    )

    def _input_changes(self, name, value):
        self.ensure_one()
        field = self._fields[name]
        if field.type == "monetary":
            return bool(self.currency_id.compare_amounts(value or 0.0, self[name]))
        return bool(value) != bool(self[name])

    def _get_locked_input_year(self, name, locked_year):
        """Locked year whose form values depend on field ``name``, or 0."""
        self.ensure_one()
        lock = locked_year.get(self.company_id.id, 0)
        if name in ("disposal_value", "disposal_vat_amount"):
            disposal = self.disposal_date if self.state == "disposed" else None
            if self.disposal_kind == "withdrawal" and disposal:
                return disposal.year if disposal.year <= lock else 0
            return 0
        lines = self.year_ids.filtered(lambda line: line.locked and not line.is_opening)
        if name == "home_office_deductible":
            lines = lines.filtered("depreciation")
            relevant = self.aveuer_group == "office_building"
        else:
            relevant = self.aveuer_group == "vehicle"
        return max(lines.mapped("year"), default=0) if relevant else 0

    def _check_takeover_rights(self):
        """Taking over the old register is released by a manager (spec 7.10)."""
        if self._is_takeover_manager():
            return
        takeover = self.filtered("opening_date")
        if takeover:
            raise UserError(
                self.env._(
                    "Only an accounting manager can enter assets taken over from "
                    "another system in the register: %(assets)s.",
                    assets=", ".join(takeover.mapped("display_name")),
                )
            )

    @api.constrains("company_id")
    def _check_root_company(self):
        for asset in self:
            if asset.company_id != asset.company_id._l10n_de_euer_business_root():
                raise ValidationError(
                    self.env._(
                        "%(asset)s: the asset register belongs to the business "
                        "(its main company or a branch that is a business of its "
                        "own).",
                        asset=asset.display_name,
                    )
                )

    def _after_change(self):
        self._check_engine()
        self._assign_pool()
        self._compute_years()

    @api.ondelete(at_uninstall=False)
    def _unlink_only_draft(self):
        for asset in self:
            if asset.state != "draft" or asset.year_ids.filtered("locked"):
                raise UserError(
                    self.env._(
                        "%(asset)s is in the register and cannot be deleted "
                        "(§ 4 (3) sentence 5 EStG). Record a disposal instead.",
                        asset=asset.display_name,
                    )
                )
