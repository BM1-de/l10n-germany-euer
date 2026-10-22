# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import datetime
from decimal import Decimal

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.misc import formatLang

from .engine import adjustment as adj
from .engine.parameters import Parameters
from .euer_statement import check_same_parent

ADJUSTMENT_KINDS = [
    (adj.CAR_PRIVATE_USE, "Private Use of Vehicles (Kz 106)"),
    (adj.WITHDRAWAL_IN_KIND, "Withdrawal of Goods, Use or Services (Kz 108)"),
    (adj.WITHDRAWAL_VAT, "VAT on Free Supplies (Kz 140)"),
    (adj.USE_CONTRIBUTION, "Business Trips with a Private Vehicle (Kz 147)"),
    (adj.COMMUTING, "Commuting Allowance (Kz 176, 142)"),
    (adj.HOME_OFFICE_DAYS, "Daily Home Office Allowance (Kz 163)"),
    (adj.HOME_OFFICE_ROOM, "Home Office (Kz 172, 162)"),
    (adj.MEALS, "Additional Meal Expenses (Kz 171)"),
    (adj.FLAT_RATE, "Flat Rate of Business Expenses (Kz 195)"),
    (adj.TAX_FREE, "Tax-Free Income, § 3c Expenses (Kz 240 to 245)"),
    (adj.INCOME_IN_KIND, "Income in Kind or Barter (Kz 111, 112, 103)"),
]
FLAT_KINDS = [
    (adj.FLAT_WRITER, "Writer or Journalist, Main Occupation (30 %, max. 3,600 €)"),
    (
        adj.FLAT_SIDE_ACTIVITY,
        "Scientific, Artistic or Literary Side Activity, Part-Time Teaching and "
        "Examining (25 %, max. 900 €)",
    ),
    (adj.FLAT_CHILDCARE, "Child Day Care (per Child and Month)"),
]
TAX_FREE_FIELDS = [
    ("EUER.G.240", "Kz 240: Tax-Free Income (§ 3 No. 26, 26a, 26b EStG)"),
    ("EUER.G.241", "Kz 241: Other Tax-Free Income (§ 3 EStG)"),
    ("EUER.G.242", "Kz 242: Tax-Free Income (§ 3a EStG)"),
    ("EUER.G.243", "Kz 243: Expenses (§ 3 No. 26, 26a, 26b EStG)"),
    ("EUER.G.244", "Kz 244: Expenses Not Deductible (§ 3c (1) EStG)"),
    ("EUER.G.245", "Kz 245: Expenses Not Deductible (§ 3c (4) EStG)"),
]
REVENUE_FIELDS = [
    (adj.FIELD_TAXABLE, "Kz 112: Taxable Income"),
    (adj.FIELD_EXEMPT, "Kz 103: Tax-Free or Not Taxable Income"),
]
#: inputs of the engine and the fields that hold them
INPUT_FIELDS = (
    "kind",
    "amount",
    "vat_amount",
    "days",
    "days_8h",
    "days_24h",
    "distance_km",
    "km",
    "months",
    "costs",
    "revenue",
    "other_amount",
    "reduction",
    "child_months",
    "hours_per_week",
    "free_place_months",
    "method",
    "center_of_activity",
    "double_household",
    "own_car",
    "flat_kind",
    "tax_free_field",
    "vat_base",
    "revenue_field",
)
_INPUT_NAMES = {"vat_amount": "vat"}
DECIMAL_INPUTS = frozenset(
    {
        "amount",
        "vat_amount",
        "vat_base",
        "costs",
        "revenue",
        "other_amount",
        "reduction",
        "distance_km",
        "km",
        "child_months",
        "hours_per_week",
        "free_place_months",
    }
)
TEXT_INPUTS = frozenset({"method", "flat_kind", "tax_free_field", "revenue_field"})
RESULT_FIELDS = (
    "result_amount",
    "turnover_amount",
    "field_amounts",
    "withdrawal_effect",
    "contribution_effect",
    "field_codes",
    "parameters",
    "notes",
    "error",
)


def _decimal(value):
    return Decimal(str(value or 0))


class L10nDeEuerAdjustment(models.Model):
    """Profit correction of a statement (phase F, spec 5.8, R-44).

    Values without bookings: private use, withdrawals in kind, flat rates
    (home office, meals, commuting), the flat rate of business expenses and
    tax-free income. The amounts are computed from the inputs and the rule
    parameters of the year (:mod:`.engine.adjustment`); the effect on
    withdrawals and contributions (Kz 122/123) is added automatically. Every
    correction needs a reason and can hold an own receipt (G-15). The
    calculation of the year takes them over like the amounts of the asset
    register (``source = "adjustment"``).
    """

    _name = "l10n_de_euer.adjustment"
    _inherit = ["mail.thread"]
    _description = "EÜR Profit Correction"
    _order = "statement_id, kind, id"

    statement_id = fields.Many2one(
        "l10n_de_euer.statement", required=True, index=True, ondelete="cascade"
    )
    company_id = fields.Many2one(
        related="statement_id.company_id", store=True, index=True
    )
    year = fields.Integer(related="statement_id.year", store=True)
    currency_id = fields.Many2one(related="statement_id.currency_id")
    kind = fields.Selection(
        ADJUSTMENT_KINDS, string="Type", required=True, tracking=True
    )
    date = fields.Date(tracking=True)
    # referenced in the payload of a finalisation: deleting the partner
    # would change the finalised record (G-04)
    partner_id = fields.Many2one("res.partner", tracking=True, ondelete="restrict")
    asset_id = fields.Many2one(
        "l10n_de_euer.asset",
        tracking=True,
        help="Use withdrawal of an asset of the register: the register then "
        "leaves out its private use, so it does not count twice (R-54).",
    )
    # ------------------------------------------------------------ inputs
    amount = fields.Monetary(
        string="Value",
        tracking=True,
        help="Value of the withdrawal (fair value or cost), of the VAT or of "
        "the tax-free income; actual costs for business trips.",
    )
    vat_amount = fields.Monetary(
        string="VAT",
        tracking=True,
        help="VAT on the withdrawal or the income in kind (Kz 140); none for a "
        "small business (§ 19 (1) UStG).",
    )
    vat_base = fields.Monetary(
        string="VAT Base",
        tracking=True,
        help="Assessment basis of the free supply for VAT (§ 10 (4) UStG), e.g. "
        "the expenses with input tax deduction. Only for a taxable supply: the "
        "good or its costs gave input tax deduction (§ 3 (1b) sentence 2, (9a) "
        "no. 1 UStG). It counts in the total turnover of the small business "
        "limit (R-27); leave it empty for a supply without input tax deduction. "
        "For a free supply booked in the accounts (Kz 106, 108), enter only the "
        "VAT base and leave the value empty.",
    )
    vat_allowed = fields.Boolean(
        string="VAT Allowed",
        compute="_compute_vat_allowed",
        help="Standard taxation on the date: VAT can be entered.",
    )
    revenue_field = fields.Selection(
        REVENUE_FIELDS,
        string="Income Field",
        tracking=True,
        help="Standard taxation: field of the income in kind (default Kz 112).",
    )
    days = fields.Integer(tracking=True, help="Working days or home office days.")
    days_8h = fields.Integer(
        string="Days over 8 Hours",
        tracking=True,
        help="Days with more than 8 hours of absence, and days of arrival or "
        "departure.",
    )
    days_24h = fields.Integer(string="Days of 24 Hours", tracking=True)
    distance_km = fields.Float(
        string="Distance (km)",
        tracking=True,
        help="Distance between home and the first place of business; full km count.",
    )
    km = fields.Float(
        string="Kilometres", tracking=True, help="Business km with a private vehicle."
    )
    months = fields.Integer(
        default=12,
        tracking=True,
        help="Months in which the home office is the centre of all activities.",
    )
    costs = fields.Monetary(
        tracking=True,
        help="Home office: actual costs not booked. Commuting: actual costs of "
        "the trips booked as expenses (Kz 142).",
    )
    revenue = fields.Monetary(
        string="Income for the Flat Rate",
        tracking=True,
        help="Income the flat rate of business expenses is based on.",
    )
    other_amount = fields.Monetary(
        tracking=True,
        help="Home office days: allowance already claimed for other income. "
        "Meals: flat rates abroad.",
    )
    reduction = fields.Monetary(
        tracking=True, help="Reduction of the meal allowances for meals provided."
    )
    child_months = fields.Float(
        tracking=True, help="Child day care: children × months of care."
    )
    hours_per_week = fields.Float(
        default=40.0, tracking=True, help="Child day care: hours of care per week."
    )
    free_place_months = fields.Float(
        string="Reserved Place Months", tracking=True, help="Child day care."
    )
    method = fields.Selection(
        [("flat", "Flat Rate"), ("actual", "Actual Costs")],
        default="flat",
        tracking=True,
    )
    center_of_activity = fields.Boolean(
        string="Centre of All Activities",
        tracking=True,
        help="The home office is the centre of the entire business and "
        "professional activity (§ 4 (5) sentence 1 no. 6b EStG).",
    )
    no_other_workplace = fields.Boolean(
        tracking=True,
        help="No other workplace is permanently available: days with work "
        "outside count too (§ 4 (5) sentence 1 no. 6c sentence 2 EStG).",
    )
    double_household = fields.Boolean(
        tracking=True,
        help="Accommodation costs of a double household are deducted for the "
        "home: no daily allowance (§ 4 (5) sentence 1 no. 6c sentence 3 EStG).",
    )
    own_car = fields.Boolean(
        string="Own or Provided Vehicle",
        tracking=True,
        help="Commuting with an own or provided vehicle: no limit of 4,500 €.",
    )
    flat_kind = fields.Selection(FLAT_KINDS, string="Professional Group", tracking=True)
    tax_free_field = fields.Selection(TAX_FREE_FIELDS, string="Field", tracking=True)
    # ------------------------------------------------------------ results
    result_amount = fields.Monetary(
        string="Amount",
        compute="_compute_result",
        store=True,
        help="Main amount of the correction.",
    )
    turnover_amount = fields.Monetary(
        string="Total Turnover (§ 19 UStG)",
        compute="_compute_result",
        store=True,
        help="Part of the total turnover of the small business limit: the VAT "
        "base of a taxable free supply or the value of income in kind, except "
        "tax-free or not taxable income in kind (Kz 103) (R-27).",
    )
    field_amounts = fields.Json(
        compute="_compute_result",
        store=True,
        help="Amounts per field code, without withdrawals and contributions.",
    )
    withdrawal_effect = fields.Monetary(
        string="Withdrawal (Kz 122)", compute="_compute_result", store=True
    )
    contribution_effect = fields.Monetary(
        string="Contribution (Kz 123)", compute="_compute_result", store=True
    )
    field_codes = fields.Char(string="Fields", compute="_compute_result", store=True)
    parameters = fields.Json(
        compute="_compute_result",
        store=True,
        help="Inputs the amounts were computed with.",
    )
    notes = fields.Char(compute="_compute_result", store=True)
    error = fields.Char(compute="_compute_result", store=True)
    field_text = fields.Char(string="Form Fields", compute="_compute_field_text")
    reason = fields.Text(
        tracking=True, help="Reason and calculation of the correction (G-15)."
    )
    attachment_ids = fields.Many2many(
        "ir.attachment",
        string="Own Receipt",
        help="Own receipt (Eigenbeleg) of the correction.",
    )

    # ------------------------------------------------------------ computes
    @api.depends(
        *INPUT_FIELDS,
        "date",
        "statement_id.year",
        "statement_id.company_id",
        # the yearly maximums are shared with the other corrections
        *(f"statement_id.adjustment_ids.{name}" for name in INPUT_FIELDS),
    )
    def _compute_result(self):
        params = None
        for record in self:
            if params is None:
                params = Parameters(self.env["l10n_de_euer.adapter"]._parameters())
            values = dict.fromkeys(
                ("field_amounts", "parameters", "notes", "error"), False
            )
            values.update(
                result_amount=0.0,
                turnover_amount=0.0,
                withdrawal_effect=0.0,
                contribution_effect=0.0,
                field_codes=False,
            )
            if record.kind and record.year:
                try:
                    data = record._get_engine_input()
                    result = adj.compute(
                        data, params, record._get_limit_used(data, params)
                    )
                except adj.AdjustmentError as error:
                    values["error"] = record._error_message(error)
                else:
                    values.update(
                        result_amount=float(result.amount),
                        turnover_amount=float(result.turnover),
                        field_amounts={
                            code: str(value) for code, value in result.fields.items()
                        },
                        withdrawal_effect=float(result.withdrawal),
                        contribution_effect=float(result.contribution),
                        field_codes=", ".join(sorted(result.amounts())) or False,
                        notes=record._note_text(result.notes),
                        parameters=record._get_parameter_snapshot(),
                    )
            record.update(values)

    def _recompute_results(self):
        """Compute the amounts again, e.g. with changed rule parameters."""
        for name in RESULT_FIELDS:
            self.env.add_to_compute(self._fields[name], self)
        self._recompute_recordset(RESULT_FIELDS)

    def _get_engine_input(self):
        self.ensure_one()
        values = {"year": self.year, "small_business": self._is_small_business()}
        for name in INPUT_FIELDS:
            value = self[name]
            if name in DECIMAL_INPUTS:
                value = _decimal(value)
            elif name in TEXT_INPUTS:
                value = value or ""
            values[_INPUT_NAMES.get(name, name)] = value
        return adj.AdjustmentInput(**values)

    def _get_vat_date(self):
        self.ensure_one()
        if self.date:
            return self.date
        return datetime.date(self.year, 12, 31) if self.year else False

    def _is_small_business(self):
        """Small business (§ 19 UStG) on the date of the correction: a status
        period of a small business, not "standard taxation from receipt"
        after the limit was exceeded (R-25)."""
        self.ensure_one()
        day = self._get_vat_date()
        company = self.statement_id.company_id
        if not day or not company:
            return False
        return company.sudo()._l10n_de_euer_get_vat_regime(day) == "ku"

    @api.depends("date", "statement_id.company_id", "statement_id.year")
    def _compute_vat_allowed(self):
        for record in self:
            record.vat_allowed = not record._is_small_business()

    def _sort_key(self):
        """Order of the corrections of a statement for the yearly maximums:
        saved ones by id, new ones last."""
        return self._origin.id or float("inf")

    def _get_limit_used(self, data, params):
        """Part of the yearly maximum of ``data`` that the earlier corrections
        of the statement use (adjustment.limits_used)."""
        self.ensure_one()
        key = adj.limit_key(data)
        if key is None:
            return adj.ZERO
        own = self._sort_key()
        earlier = self.statement_id.adjustment_ids.filtered(
            lambda other: other._sort_key() < own and other.kind == self.kind
        ).sorted(lambda other: other._sort_key())
        inputs = [other._get_engine_input() for other in earlier if other.year]
        return adj.limits_used(inputs, params).get(key, adj.ZERO)

    def _note_text(self, notes):
        texts = {
            "capped": self.env._(
                "The amount is limited to the maximum of the year (together with "
                "the earlier corrections of the year)."
            ),
            "double_household": self.env._(
                "No daily allowance next to a double household."
            ),
        }
        return " ".join(texts.get(note, note) for note in notes) or False

    def _get_parameter_snapshot(self):
        """Inputs the amounts were computed with (reproducibility)."""
        self.ensure_one()
        inputs = {}
        for name in INPUT_FIELDS:
            value = self[name]
            if isinstance(value, float):
                value = str(_decimal(value))
            inputs[name] = value
        return {"inputs": inputs, "year": self.year}

    def _error_message(self, error):
        params = error.params
        if error.key == "missing_parameter":
            return self.env._(
                "There is no value of parameter %(parameter)s for %(year)s.",
                parameter=params.get("parameter"),
                year=params.get("year"),
            )
        if error.key == "negative_input":
            name = params.get("input")
            field = self._fields.get(name) or self._fields.get(
                {v: k for k, v in _INPUT_NAMES.items()}.get(name, "")
            )
            return self.env._(
                "%(input)s must not be negative.",
                input=field._description_string(self.env) if field else name,
            )
        if error.key == "unknown_flat_kind":
            return self.env._("Choose the group of the flat rate.")
        if error.key == "unknown_field":
            return self.env._("Choose the field of the tax-free income or expense.")
        if error.key == "too_many_days":
            return self.env._("A year has at most 366 days.")
        if error.key == "invalid_months":
            return self.env._("Enter between 0 and 12 months.")
        if error.key == "reduction_too_high":
            return self.env._("The reduction is higher than the meal allowances.")
        if error.key == "small_business_vat":
            return self.env._(
                "A small business has no VAT on free supplies or income in kind "
                "(§ 19 (1) UStG): leave the VAT empty. Kz 140 stays empty for a "
                "small business (R-28)."
            )
        if error.key == "vat_base_missing":
            return self.env._(
                "Enter the VAT base of the free supply (§ 10 (4) UStG); it counts "
                "in the total turnover of the small business limit."
            )
        if error.key == "vat_on_exempt":
            return self.env._("Tax-free income in kind (Kz 103) has no VAT.")
        return self.env._(
            "The profit correction cannot be computed: %(key)s", key=error.key
        )

    @api.depends("field_amounts", "withdrawal_effect", "contribution_effect", "year")
    @api.depends_context("lang")
    def _compute_field_text(self):
        fields_by_year = {}
        for record in self:
            year = record.year
            if year and year not in fields_by_year:
                fields_by_year[year] = record._get_year_fields()
            known = fields_by_year.get(year) or {}
            parts = []
            for code, value in sorted(
                record._get_amounts().items(),
                key=lambda item: (known[item[0]].line if item[0] in known else 0),
            ):
                field = known.get(code)
                label = (
                    self.env._(
                        "Line %(line)s (Kz %(kz)s)", line=field.line, kz=field.kz
                    )
                    if field and field.kz
                    else code
                )
                # amount in the language of the user (112,00 in German)
                parts.append(f"{label}: {formatLang(self.env, float(value), digits=2)}")
            record.field_text = "; ".join(parts)

    def _get_year_fields(self):
        """Fields of the forms of the year by code (the codes are resolved per
        form year: line and Kz can move, spec 3.1)."""
        try:
            forms, _provisional = self.env["l10n_de_euer.form"]._get_forms_for_year(
                self.year
            )
        except UserError:
            return {}
        return {field.code: field for field in forms.field_ids}

    @api.depends("kind", "asset_id", "partner_id", "result_amount")
    def _compute_display_name(self):
        labels = dict(self._fields["kind"]._description_selection(self.env))
        for record in self:
            name = labels.get(record.kind, "")
            other = record.asset_id.display_name or record.partner_id.display_name
            record.display_name = f"{name}: {other}" if other else name

    # ------------------------------------------------------------ amounts
    def _get_amounts(self):
        """Field amounts of one correction including Kz 122/123: code →
        ``Decimal``."""
        self.ensure_one()
        result = {
            code: Decimal(value) for code, value in (self.field_amounts or {}).items()
        }
        if self.withdrawal_effect:
            result[adj.FIELD_WITHDRAWALS] = result.get(
                adj.FIELD_WITHDRAWALS, Decimal(0)
            ) + _decimal(self.withdrawal_effect)
        if self.contribution_effect:
            result[adj.FIELD_CONTRIBUTIONS] = result.get(
                adj.FIELD_CONTRIBUTIONS, Decimal(0)
            ) + _decimal(self.contribution_effect)
        return {code: value for code, value in result.items() if value}

    def _get_total_amounts(self):
        """Sum of the field amounts of the corrections in ``self``."""
        totals = {}
        for record in self:
            for code, value in record._get_amounts().items():
                totals[code] = totals.get(code, Decimal(0)) + value
        return {code: value for code, value in totals.items() if value}

    # ------------------------------------------------------------ checks
    @api.constrains("reason")
    def _check_reason(self):
        """G-12, G-15: every profit correction needs a reason."""
        for record in self:
            if not (record.reason or "").strip():
                raise ValidationError(
                    self.env._(
                        "Enter a reason for the profit correction %(name)s.",
                        name=record.display_name,
                    )
                )

    @api.constrains(*INPUT_FIELDS, "date")
    def _check_inputs(self):
        for record in self:
            if record.error:
                raise ValidationError(record.error)
            known = record._get_year_fields()
            missing = [code for code in record._get_amounts() if code not in known]
            if missing:
                raise ValidationError(
                    self.env._(
                        "The forms of %(year)s have no fields %(codes)s.",
                        year=record.year,
                        codes=", ".join(missing),
                    )
                )

    @api.constrains("asset_id", "kind", "statement_id")
    def _check_asset(self):
        for record in self.filtered("asset_id"):
            if record.kind != adj.WITHDRAWAL_IN_KIND:
                raise ValidationError(
                    self.env._(
                        "Only a withdrawal of use refers to an asset of the register."
                    )
                )
            if record.asset_id.sudo().company_id != record.statement_id.company_id:
                raise ValidationError(
                    self.env._(
                        "%(asset)s does not belong to the business of the statement.",
                        asset=record.asset_id.display_name,
                    )
                )
            others = record.statement_id.adjustment_ids.filtered(
                lambda other, r=record: other != r and other.asset_id == r.asset_id
            )
            if others:
                raise ValidationError(
                    self.env._(
                        "The private use of %(asset)s is already corrected.",
                        asset=record.asset_id.display_name,
                    )
                )

    def action_open_receipts(self):
        """Own receipt of the correction (spec 7.6 no. 4, G-15)."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Own Receipt of %(name)s", name=self.display_name),
            "res_model": "ir.attachment",
            "view_mode": "list,form",
            "domain": [("id", "in", self.attachment_ids.ids)],
            "context": {"create": False},
        }

    def action_open_slices(self):
        """Slices of the correction in the computation of the statement."""
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_cash_slice"
        )
        action["domain"] = [("adjustment_id", "=", self.id)]
        action["context"] = {}
        return action

    # ------------------------------------------------------------ CRUD
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("date") and vals.get("statement_id"):
                statement = self.env["l10n_de_euer.statement"].browse(
                    vals["statement_id"]
                )
                vals["date"] = datetime.date(statement.year, 12, 31)
        records = super().create(vals_list)
        records.statement_id._check_open()
        records.statement_id._set_draft()
        return records

    def write(self, vals):
        # also as superuser: a correction never moves into another (possibly
        # finalised) statement (spec 7.7, G-04)
        check_same_parent(self, "statement_id", vals)
        self.statement_id._check_open()
        result = super().write(vals)
        if set(vals) - {"reason", "attachment_ids", "partner_id", "date"}:
            self.statement_id._set_draft()
        return result

    @api.ondelete(at_uninstall=False)
    def _unlink_only_open(self):
        self.statement_id._check_open()

    def unlink(self):
        statements = self.statement_id
        result = super().unlink()
        statements.exists()._set_draft()
        return result
