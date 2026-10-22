# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import csv
from decimal import Decimal
from functools import lru_cache

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain
from odoo.tools.misc import file_path

from ..models.engine import depreciation as dep
from ..models.euer_asset import ASSET_KINDS, AVEUER_GROUPS, METHODS, to_decimal

#: Roles of the accounts an asset can be created from (spec 6.5)
ASSET_ROLES = ("av", "afa")
#: Groups that are not depreciable (§ 4 (3) sentence 4 EStG)
NOT_DEPRECIABLE_GROUPS = dep.NON_DEPRECIABLE_GROUPS


@lru_cache(maxsize=1)
def group_ranges():
    """Rules ``(chart, from, to, group)`` proposing the AVEÜR group (A31)."""
    path = file_path("l10n_de_euer/data/mapping/asset_group_ranges.csv")
    with open(path, encoding="utf-8") as handle:
        return tuple(
            (row["chart"], row["code_from"], row["code_to"], row["group"])
            for row in csv.DictReader(handle)
        )


class L10nDeEuerAssetFromMoveLine(models.TransientModel):
    """Create an asset from journal items of a purchase (spec 6.5).

    Proposals: delivery date from the supply date of the document (B-11),
    net cost and input tax from the lines, AVEÜR group from the account
    (only a proposal, A31), kind from the limits of § 6 (2) and (2a) EStG.
    A small business that booked the purchase gross without tax item gets
    net cost and input tax from the VAT shown on the purchase invoice, which
    the user chooses (19 %, 7 % or none, no default): the input tax that is
    not deductible belongs to the acquisition cost (§ 9b (1) EStG), the
    limits of low-value assets and pools apply to the net cost (R 9b (2)
    EStR). Only VAT contained in the invoice is taken out (§ 6 (2) sentence
    1, (2a) sentence 1 EStG): a purchase from a private person, from a small
    business or under the margin scheme (§ 25a UStG) shows none, its gross
    amount is the net cost (E5 review).
    """

    _name = "l10n_de_euer.asset.from.move.line"
    _description = "EÜR Asset from Journal Items"

    move_line_ids = fields.Many2many(
        "account.move.line", string="Journal Items", required=True
    )
    company_id = fields.Many2one(
        "res.company", compute="_compute_proposal", store=True, readonly=False
    )
    currency_id = fields.Many2one(related="company_id.currency_id")
    mode = fields.Selection(
        [("single", "One Asset"), ("per_line", "One Asset per Item")],
        required=True,
        default="single",
    )
    name = fields.Char(compute="_compute_proposal", store=True, readonly=False)
    delivery_date = fields.Date(compute="_compute_proposal", store=True, readonly=False)
    aveuer_group = fields.Selection(
        AVEUER_GROUPS,
        string="AVEÜR Group",
        compute="_compute_proposal",
        store=True,
        readonly=False,
    )
    asset_kind = fields.Selection(
        ASSET_KINDS,
        string="Kind",
        compute="_compute_proposal",
        store=True,
        readonly=False,
    )
    method = fields.Selection(METHODS, default="linear", required=True)
    useful_life_months = fields.Integer(string="Useful Life (Months)")
    cost_net = fields.Monetary(compute="_compute_proposal", store=True, readonly=False)
    input_tax = fields.Monetary(compute="_compute_proposal", store=True, readonly=False)
    gross_split = fields.Boolean(
        string="Gross Purchase of a Small Business",
        compute="_compute_proposal",
        store=True,
        help="The items carry no input tax: net cost and input tax are split "
        "from the gross amount by the VAT rate.",
    )
    vat_shown = fields.Selection(
        [("19", "19 %"), ("7", "7 %"), ("none", "No VAT on the Invoice")],
        string="VAT on the Purchase Invoice",
        help="Small business with a gross purchase: the VAT shown on the purchase "
        "invoice. It splits the gross amount into net cost and input tax (§ 9b "
        "(1) EStG); the limits of low-value assets and pools apply to the net "
        "cost (§ 6 (2) EStG, R 9b (2) EStR). Choose 'No VAT on the Invoice' for a "
        "purchase from a private person, from a small business or under the "
        "margin scheme (§ 25a UStG): the gross amount is then the net cost.",
    )
    vat_rate = fields.Float(
        string="VAT Rate of the Purchase (%)",
        compute="_compute_vat_rate",
        store=True,
        readonly=False,
        help="VAT rate that splits a gross purchase of a small business, from "
        "the VAT shown on the purchase invoice; 0 until it is chosen.",
    )

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        if (
            self.env.context.get("active_model") == "account.move.line"
            and "move_line_ids" in fields_list
        ):
            values["move_line_ids"] = [
                fields.Command.set(self.env.context.get("active_ids", []))
            ]
        return values

    def _get_lines(self):
        """Selected items that can make up an asset: posted debit items of
        products and the tax items of the purchase (spec 6.5)."""
        self.ensure_one()
        return self.move_line_ids.filtered(
            lambda line: line.parent_state == "posted"
            and (
                (line.display_type == "product" and line.balance > 0)
                or (line.display_type == "tax" and line.tax_line_id)
            )
        )

    @api.model
    def _get_delivery_date(self, move):
        """Delivery date proposed from the document (B-11)."""
        return (
            move.taxable_supply_date
            or move.delivery_date
            or move.invoice_date
            or move.date
        )

    def _check_lines(self):
        """Raise for items that cannot be part of an asset, or that already are
        the purchase of one (the cost would count twice)."""
        self.ensure_one()
        lines = self._get_lines()
        base = lines.filtered(lambda line: not line.tax_line_id)
        # a tax item may belong to the purchase of several assets
        used = (
            self.env["l10n_de_euer.asset"]
            .sudo()
            .search(Domain("move_line_ids", "in", base.ids))
        )
        if used:
            raise UserError(
                self.env._(
                    "The journal items are already the purchase of %(assets)s. "
                    "Change that asset or delete its draft; an item that covers "
                    "several assets is linked to the other assets in their form.",
                    assets=", ".join(used.mapped("display_name")),
                )
            )
        invalid = self.move_line_ids - lines
        if base:
            company = base.company_id.root_id[:1]
            Assignment = self.env["l10n_de_euer.account.assignment"]
            for line in base:
                assignment = Assignment._get_assignments(
                    company, line.account_id, line.date
                ).get(line.account_id)
                if not assignment or assignment.category_id.role not in ASSET_ROLES:
                    invalid |= line
        if invalid or not base:
            raise UserError(
                self.env._(
                    "Select the posted debit items of the purchase on fixed asset "
                    "or depreciation accounts (and their tax items). Not usable: "
                    "%(lines)s",
                    lines=", ".join(
                        f"{line.move_id.name} {line.account_id.code}"
                        for line in invalid
                    )
                    or self.env._("no item of a fixed asset account"),
                )
            )

    @api.depends("vat_shown")
    def _compute_vat_rate(self):
        rates = {"19": 19.0, "7": 7.0, "none": 0.0}
        for wizard in self:
            wizard.vat_rate = rates.get(wizard.vat_shown, 0.0)

    @api.depends("move_line_ids", "vat_rate")
    def _compute_proposal(self):
        for wizard in self:
            lines = wizard._get_lines()
            wizard.gross_split = False
            if not lines.filtered(lambda line: not line.tax_line_id):
                continue
            first = lines.filtered(lambda line: not line.tax_line_id)[0]
            move = first.move_id
            wizard.company_id = first.company_id._l10n_de_euer_business_root()
            wizard.name = first.name or first.product_id.display_name or move.name
            wizard.delivery_date = wizard._get_delivery_date(move)
            wizard.aveuer_group = wizard._propose_group(
                first.account_id, first.company_id.root_id
            )
            net, tax = wizard._amounts(lines)
            wizard.gross_split = wizard._is_gross_small_business(lines)
            wizard.cost_net = float(net)
            wizard.input_tax = float(tax)
            wizard.asset_kind = wizard._propose_kind(first, net, wizard.aveuer_group)

    def _is_gross_small_business(self, lines):
        """Purchase of a small business booked gross: no tax item and no
        tax in the items, small business on the date of supply."""
        tax_lines = lines.filtered("tax_line_id")
        base = lines - tax_lines
        if (
            not base
            or tax_lines
            or any(line.price_total != line.price_subtotal for line in base)
        ):
            return False
        company = base.company_id[:1]
        day = self._get_delivery_date(base.move_id[:1])
        return company._l10n_de_euer_get_vat_regime(day) == "ku"

    def _amounts(self, lines):
        """Net cost and input tax of ``lines`` in company currency; a gross
        purchase of a small business is split by :attr:`vat_rate`."""
        tax_lines = lines.filtered("tax_line_id")
        base = lines - tax_lines
        net = sum((to_decimal(line.balance) for line in base), dep.ZERO)
        if tax_lines:
            return net, sum((to_decimal(line.balance) for line in tax_lines), dep.ZERO)
        if self._is_gross_small_business(lines):
            gross = net
            net = dep.cents(gross / (1 + Decimal(str(self.vat_rate or 0)) / 100))
            return net, gross - net
        tax = dep.ZERO
        for line in base:
            if not line.price_total or not line.price_subtotal:
                continue
            ratio = Decimal(str(line.price_total)) / Decimal(str(line.price_subtotal))
            tax += dep.cents(to_decimal(line.balance) * (ratio - 1))
        return net, tax

    def _propose_group(self, account, root):
        chart = root.chart_template or ""
        code = account.with_company(root).code or ""
        for rule_chart, start, end, group in group_ranges():
            if rule_chart == chart and start <= code[: len(start)] <= end:
                return group
        return "other_movable"

    def _propose_kind(self, line, net, group):
        """Kind by the AVEÜR group, the account category and the net value."""
        if group == "current":
            return "current_4_3_4"
        if group in NOT_DEPRECIABLE_GROUPS:
            return "non_depreciable"
        if group not in dep.MOVABLE_GROUPS:
            # buildings, land rights, intangible assets: low-value assets and
            # pools only for movable assets (trivial programs: chosen by hand)
            return "depreciable"
        company = line.company_id.root_id
        assignment = (
            self.env["l10n_de_euer.account.assignment"]
            ._get_assignments(company, line.account_id, line.date)
            .get(line.account_id)
        )
        code = assignment.category_id.code if assignment else ""
        if code == "DEPR_POOL":
            return "pool"
        date = self.delivery_date or line.date
        Parameter = self.env["l10n_de_euer.parameter"].sudo()
        if code == "DEPR_LOW_VALUE" or net <= Decimal(
            str(Parameter._get_value("gwg_limit", date))
        ):
            if net <= Decimal(str(Parameter._get_value("gwg_register_limit", date))):
                return "instant_250"
            return "gwg"
        return "depreciable"

    def action_create(self):
        self.ensure_one()
        if not self.move_line_ids:
            raise UserError(self.env._("Select the journal items of the purchase."))
        self._check_lines()
        lines = self._get_lines()
        if self.gross_split and not self.vat_shown:
            raise UserError(
                self.env._(
                    "Choose the VAT shown on the purchase invoice (19 %, 7 % or "
                    "none): it decides the net cost and the kind of the asset."
                )
            )
        if self.mode == "single":
            assets = self._create_asset(lines, self.name)
        else:
            assets = self.env["l10n_de_euer.asset"]
            for line in lines.filtered(lambda line: not line.tax_line_id):
                assets |= self._create_asset(line, line.name or self.name)
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_asset"
        )
        if len(assets) == 1:
            action.update({"res_id": assets.id, "view_mode": "form", "views": []})
        else:
            action["domain"] = [("id", "in", assets.ids)]
        return action

    def _create_asset(self, lines, name):
        businesses = lines.company_id.mapped(
            lambda company: company._l10n_de_euer_business_root()
        )
        if len(businesses) > 1:
            raise UserError(self.env._("The items belong to different businesses."))
        if self.mode == "single":
            net, tax = to_decimal(self.cost_net), to_decimal(self.input_tax)
            kind = self.asset_kind or "depreciable"
            group = self.aveuer_group or "other_movable"
            delivery_date = self.delivery_date
        else:
            # one asset per item: kind, group and delivery date of each item
            net, tax = self._amounts(lines)
            group = self._propose_group(lines.account_id, lines.company_id.root_id)
            kind = self._propose_kind(lines, net, group)
            delivery_date = (
                self.delivery_date
                if lines.move_id == self.move_line_ids.move_id
                else self._get_delivery_date(lines.move_id)
            )
        method = self.method if kind == "depreciable" else "none"
        return self.env["l10n_de_euer.asset"].create(
            {
                "name": name or self.env._("Asset"),
                "company_id": businesses.id,
                "asset_kind": kind,
                "aveuer_group": group,
                "delivery_date": delivery_date,
                "origin": "purchase",
                "cost_net": float(net),
                "input_tax": float(tax),
                "method": method,
                "useful_life_months": self.useful_life_months,
                "move_line_ids": [fields.Command.set(lines.ids)],
            }
        )
