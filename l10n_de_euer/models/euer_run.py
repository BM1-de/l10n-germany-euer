# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import json
import logging
import time
from collections import defaultdict
from decimal import Decimal

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.fields import Domain

from .engine import engine as engine_module
from .engine.assignment import form_amount
from .euer_cash_slice import INTERNAL as SLICE_INTERNAL
from .euer_form import NON_AMOUNT_TYPES
from .euer_ten_day_item import reason_text

_logger = logging.getLogger(__name__)

#: rule of a contribution → rule of the stored slice
_RULES = {
    "mapping": "mapping",
    "revenue": "revenue",
    "tax": "tax",
    "gift": "gift",
    "private": "private",
    "private_journal": "private_journal",
    "settlement": "settlement",
    "overflow": "overflow",
    "asset": "asset",
    "adjustment": "adjustment",
}


class L10nDeEuerRun(models.Model):
    """Calculation of the cash basis profit of a business and year (spec 7.3).

    A run reads the bookings (phase A, :mod:`.euer_adapter`), lets the pure
    Python engine compute the slices and field contributions and stores them
    as :class:`l10n_de_euer.cash.slice`, together with the check results of
    the calculation. A new run of the same year replaces the previous one.
    Journal entries are only read (B-01).
    """

    _name = "l10n_de_euer.run"
    _description = "EÜR Calculation Run"
    _order = "date desc, id desc"
    _check_company_auto = True

    company_id = fields.Many2one("res.company", required=True, index=True)
    date = fields.Datetime(required=True, default=fields.Datetime.now)
    user_id = fields.Many2one(
        "res.users", default=lambda self: self.env.user, ondelete="restrict"
    )
    year_from = fields.Integer(required=True)
    year_to = fields.Integer(required=True)
    config_snapshot = fields.Json(
        help="Settings and VAT status periods the run used (reproducibility)."
    )
    hash = fields.Char(help="SHA-256 over the sorted slices (I-4).")
    invariant_ok = fields.Boolean(
        string="Invariants Hold", help="Invariants I-1 to I-3 hold (no C-11)."
    )
    log = fields.Json(help="Cycles, neutralisations and money in transit.")
    totals = fields.Json(
        string="Field Totals", help="Field code → amount of the statement year."
    )
    form_values = fields.Json(
        help="All fields of the forms of the year after the formulas (phase G), "
        "in euro; empty fields are left out.",
    )
    duration = fields.Float(string="Duration (s)", digits=(16, 3))
    locked = fields.Boolean(
        string="Finalised",
        readonly=True,
        copy=False,
        help="The run belongs to a finalised statement: it and its slices never "
        "change; a new calculation of the year is a comparison run next to it.",
    )
    slice_ids = fields.One2many("l10n_de_euer.cash.slice", "run_id", string="Slices")
    slice_count = fields.Integer(compute="_compute_slice_count")
    totals_text = fields.Text(string="Totals", compute="_compute_texts")
    form_values_text = fields.Text(
        string="Form Values (Text)", compute="_compute_texts"
    )
    log_text = fields.Text(compute="_compute_texts")

    @api.depends("totals", "form_values", "log", "config_snapshot")
    def _compute_texts(self):
        for run in self:
            run.totals_text = "\n".join(
                f"{code}: {Decimal(value) / 100:.2f}"
                for code, value in sorted((run.totals or {}).items())
            )
            run.form_values_text = "\n".join(
                f"{code}: {value}"
                for code, value in sorted((run.form_values or {}).items())
            )
            run.log_text = json.dumps(
                {"log": run.log or {}, "configuration": run.config_snapshot or {}},
                indent=2,
                sort_keys=True,
                default=str,
            )

    def _compute_slice_count(self):
        groups = self.env["l10n_de_euer.cash.slice"]._read_group(
            Domain("run_id", "in", self.ids), ["run_id"], ["__count"]
        )
        counts = {run.id: count for run, count in groups}
        for run in self:
            run.slice_count = counts.get(run.id, 0)

    @api.depends("company_id", "year_from", "date")
    def _compute_display_name(self):
        for run in self:
            run.display_name = (
                f"{run.company_id.name or ''} {run.year_from} "
                f"({fields.Datetime.to_string(run.date) if run.date else ''})"
            )

    def action_open_slices(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_cash_slice"
        )
        action["domain"] = [("run_id", "=", self.id)]
        return action

    # ------------------------------------------------------------ protection
    @api.model
    def _is_internal(self):
        return self.env.su and bool(self.env.context.get(SLICE_INTERNAL))

    def write(self, vals):
        """A finalised run never changes (spec 7.7), also not for an
        administrator; it is never unlocked."""
        if self.filtered("locked"):
            raise UserError(
                self.env._(
                    "The run belongs to a finalised statement and cannot be changed."
                )
            )
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_only_by_calculation(self):
        """A run and its slices are replaced by the next calculation only; a
        finalised run stays."""
        if self.filtered("locked"):
            raise UserError(
                self.env._(
                    "The run belongs to a finalised statement and cannot be deleted."
                )
            )
        if not self._is_internal():
            raise UserError(
                self.env._("Calculation runs are replaced by the next calculation.")
            )

    def _lock(self):
        """Freeze the run of a finalised statement (spec 7.7 step 3)."""
        self.sudo().filtered(lambda run: not run.locked).write({"locked": True})

    # ------------------------------------------------------------ calculation

    @api.model
    def _check_business_access(self, business):
        """The extraction reads with SQL: the user must have selected all
        companies of the business (spec 7.9, 7.10). Archived branches cannot
        be selected; their entries still belong to the business."""
        self.check_access("create")
        if self.env.su:
            return
        companies = business._l10n_de_euer_business_companies().sudo()
        missing = companies.filtered("active").with_env(self.env) - self.env.companies
        if missing:
            raise AccessError(
                self.env._(
                    "Select all companies of the business to compute it: "
                    "%(companies)s.",
                    companies=", ".join(missing.sudo().mapped("name")),
                )
            )

    @api.model
    def _engine_result(self, company, year):
        """Engine result of a year without storing it (phases A to E).

        For comparisons and regression checks of years without form data;
        the year is not stored and no check results change.
        """
        business = company._l10n_de_euer_business_root()
        self._check_business_access(business)
        data = self.env["l10n_de_euer.adapter"]._engine_input(business, year)
        return engine_module.run(data, year)

    @api.model
    def _compute_year(self, company, year):
        """Compute the cash basis profit of ``year`` (method "Jahr berechnen").

        Reads the bookings of the business of ``company``, stores the run with
        its slices and check results and returns the run. The field totals of
        the year are in :meth:`_get_field_amounts`, all form values after the
        formulas (phase G) in ``form_values``.
        """
        business = company._l10n_de_euer_business_root()
        self._check_business_access(business)
        root = business.sudo().root_id
        if not root.l10n_de_euer_enabled:
            raise UserError(
                self.env._(
                    "Cash basis accounting (EÜR) is not enabled for %(company)s.",
                    company=root.name,
                )
            )
        start = time.perf_counter()
        adapter = self.env["l10n_de_euer.adapter"]
        data = adapter._engine_input(business, year)
        extracted = time.perf_counter()
        result = engine_module.run(data, year)
        computed = time.perf_counter()
        run = self._store_run(business, year, data, result)
        form_results = self._form_calculation(run, year, result)
        self._store_ten_day_items(business, year, result)
        self.env["l10n_de_euer.calculation.checker"]._store(
            business, year, result, run, form_results
        )
        stored = time.perf_counter()
        run.sudo().duration = stored - start
        _logger.info(
            "EÜR %s %s: %s slices, extraction %.2f s, engine %.2f s, storing %.2f s",
            business.name,
            year,
            len(result.slices),
            extracted - start,
            computed - extracted,
            stored - computed,
        )
        return run

    @api.model
    def _form_calculation(self, run, year, result):
        """Phase G (spec 5.9): formulas, sign check (C-71), scope (C-40) and
        Anlage SZ (C-41) of the forms of the year; the values are stored."""
        forms, _provisional = (
            self.env["l10n_de_euer.form"].sudo()._get_forms_for_year(year)
        )
        amount_codes = set(
            forms.field_ids.filtered(
                lambda field: field.field_type not in NON_AMOUNT_TYPES
            ).mapped("code")
        )
        unknown = sorted(set(result.totals) - amount_codes)
        if unknown:
            _logger.warning(
                "EÜR %s: totals without form field are left out: %s", year, unknown
            )
        amounts = {
            code: Decimal(value) / 100
            for code, value in result.totals.items()
            if code in amount_codes
        }
        values = forms._calculate(amounts)
        run.sudo().form_values = {
            code: str(value) for code, value in sorted(values.items()) if value
        }
        return forms._check_values(values, amounts)

    def _get_field_amounts(self):
        """Field contributions of the statement year: code → ``Decimal`` euro."""
        self.ensure_one()
        return {
            code: Decimal(value) / 100 for code, value in (self.totals or {}).items()
        }

    @api.model
    def _store_run(self, business, year, data, result):
        Run = self.sudo()
        # the run of a finalised statement stays: a new calculation of the
        # year is a comparison run next to it (C-60)
        previous = Run.search(
            Domain("company_id", "=", business.id)
            & Domain("year_from", "=", year)
            & Domain("year_to", "=", year)
            & Domain("locked", "=", False)
        )
        previous.with_context(**{SLICE_INTERNAL: True}).unlink()
        run = Run.create(
            {
                "company_id": business.id,
                "year_from": year,
                "year_to": year,
                "config_snapshot": self._config_snapshot(business, data),
                "hash": result.hash,
                "invariant_ok": not any(f.code == "C-11" for f in result.findings),
                "log": self._log(result, year),
                "totals": dict(sorted(result.totals.items())),
            }
        )
        self._store_slices(run, business, year, data, result)
        return run.with_env(self.env)

    @api.model
    def _store_ten_day_items(self, business, year, result):
        """Review list of the turn of the year (spec 5.6.1, C-23, C-26)."""
        values = []
        for item in result.ten_day_items:
            if item.kind == "transfer_order" and item.decision is None:
                proposal = "confirm"
            else:
                proposal = item.proposal
            values.append(
                {
                    "kind": item.kind,
                    "source_move_id": item.source_move_id,
                    "origin_move_id": item.origin_move_id,
                    "cash_date": item.cash_date,
                    "amount": sum(
                        result.slices[index].amount for index in item.slice_indexes
                    )
                    / 100,
                    "candidate_reason": reason_text(self.env, item.reasons),
                    "proposal": proposal,
                    "proposed_cash_date": item.proposed_cash_date,
                    "economic_year": item.economic_year or 0,
                    "due_date": item.due_date,
                    "decision": item.decision or False,
                    "applied": item.applied,
                }
            )
        self.env["l10n_de_euer.ten_day.item"]._store_items(
            business, {year - 1, year}, values
        )

    @api.model
    def _config_snapshot(self, business, data):
        settings = data.settings
        return {
            "companies": sorted(data.company_ids),
            "settings": {
                "offsetting_is_payment": settings.offsetting_is_payment,
                "fx_mode": settings.fx_mode,
                "entertainment_mode": settings.entertainment_mode,
                "default_regime": settings.default_regime,
            },
            "vat_periods": [
                {
                    "date_from": period.date_from.isoformat(),
                    "date_to": period.date_to.isoformat() if period.date_to else None,
                    "regime": period.regime,
                    "basis": period.basis,
                    "vat_return_period": period.vat_return_period,
                    "permanent_extension": period.permanent_extension,
                }
                for period in data.vat_periods
            ],
            "founding_year": data.founding_year,
            "module_version": self.env["ir.module.module"]
            .sudo()
            .search([("name", "=", "l10n_de_euer")], limit=1)
            .latest_version,
        }

    @api.model
    def _log(self, result, year):
        notes = defaultdict(int)
        for note in result.notes:
            if note.date.year == year:
                notes[note.kind] += note.amount
        return {
            "notes": dict(notes),
            "overflow": result.overflow,
            "invariants": {
                "money": len(result.invariants.get("money") or {}),
                "documents": len(result.invariants.get("documents") or {}),
                "transit": len(result.invariants.get("transit") or {}),
            },
        }

    @api.model
    def _store_slices(self, run, business, year, data, result):
        """One record per slice and form field of the statement year.

        A slice attributed to another year (ten-day rule) can still reach a
        field of the statement year: a withdrawal or contribution keeps the
        year of its cash date. Its records are stored with the year of the
        contribution.
        """
        fields_by_code = self._form_fields(year)
        per_slice = defaultdict(list)
        free = []
        for contribution in result.contributions:
            if contribution.year != year:
                continue
            if contribution.slice_index is None:
                free.append(contribution)
            else:
                per_slice[contribution.slice_index].append(contribution)
        currency = business.currency_id
        categories = self._category_ids()
        names = {move.id: move.name for move in data.moves}
        vals_list = []
        for index, one in enumerate(result.slices):
            contributions = per_slice.get(index)
            if one.attribution_year != year and not contributions:
                continue
            base = self._slice_values(run, business, one, currency, categories, names)
            if not contributions:
                vals_list.append({**base, "rule": "none"})
                continue
            for contribution in contributions:
                balance = form_amount(contribution.field, contribution.amount)
                vals_list.append(
                    {
                        **base,
                        "attribution_year": contribution.year,
                        **_balance_values(balance, currency),
                        "field_code": contribution.field,
                        "field_id": fields_by_code.get(contribution.field),
                        "amount": contribution.amount / 100,
                        "rule": _RULES.get(contribution.rule, "mapping"),
                        **_gift_values(contribution),
                    }
                )
        adjustments = self._adjustment_ids(free)
        for contribution in free:
            vals_list.append(
                {
                    "run_id": run.id,
                    "company_id": business.id,
                    "date": f"{year}-12-31",
                    "attribution_year": year,
                    "kind": "overflow"
                    if contribution.rule == "overflow"
                    else "non_cash",
                    "rule": _RULES.get(contribution.rule, "mapping"),
                    "field_code": contribution.field,
                    "field_id": fields_by_code.get(contribution.field),
                    "amount": contribution.amount / 100,
                    "adjustment_id": adjustments.get(contribution.reference, False),
                }
            )
        self.env["l10n_de_euer.cash.slice"].sudo().with_context(
            **{SLICE_INTERNAL: True}
        ).create(vals_list)

    @api.model
    def _slice_values(self, run, business, one, currency, categories, names):
        part = None
        if one.key and one.key[1]:
            part = "tax" if one.virtual_tax else "net"
        real_line = bool(one.key) and not one.key[1]
        origin_line = one.origin_line_id if real_line else None
        return {
            "run_id": run.id,
            "company_id": business.id,
            "date": one.date,
            "attribution_year": one.attribution_year,
            "kind": one.kind,
            "source_move_id": one.source_move_id,
            "source_line_id": one.source_line_id,
            "origin_move_id": one.origin_move_id,
            "origin_line_id": origin_line,
            "document_move_id": one.document_move_id,
            "source_move_ref": one.source_move_id or 0,
            "source_line_ref": one.source_line_id or 0,
            "origin_move_ref": one.origin_move_id or 0,
            "origin_line_ref": origin_line or 0,
            "document_move_ref": one.document_move_id or 0,
            "source_move_name": names.get(one.source_move_id) or False,
            "origin_move_name": names.get(one.origin_move_id) or False,
            "account_id": one.account_id,
            "partner_id": one.partner_id,
            "tax_repartition_line_id": one.repartition_id,
            "category_id": categories.get(one.category),
            "path": list(one.path) if one.path else None,
            "override": one.override,
            "ten_day_applied": one.ten_day_applied,
            "virtual_part": part,
            **_balance_values(one.amount, currency),
        }

    @api.model
    def _adjustment_ids(self, contributions):
        """Reference of a phase F value → id of its profit correction."""
        prefix = "l10n_de_euer.adjustment,"
        ids = {
            contribution.reference: int(contribution.reference[len(prefix) :])
            for contribution in contributions
            if (contribution.reference or "").startswith(prefix)
        }
        existing = set(
            self.env["l10n_de_euer.adjustment"].sudo().browse(ids.values()).exists().ids
        )
        return {
            reference: record_id
            for reference, record_id in ids.items()
            if record_id in existing
        }

    @api.model
    def _category_ids(self):
        categories = (
            self.env["l10n_de_euer.category"]
            .sudo()
            .with_context(active_test=False)
            .search_read(Domain("code", "!=", False), ["code"])
        )
        return {category["code"]: category["id"] for category in categories}

    @api.model
    def _form_fields(self, year):
        forms, _provisional = (
            self.env["l10n_de_euer.form"].sudo()._get_forms_for_year(year)
        )
        return {field.code: field.id for field in forms.field_ids}


def _gift_values(contribution):
    """Recipient of a gift (register of gifts, G-08)."""
    if not (contribution.recipient or contribution.gift_bulk or contribution.gift_date):
        return {}
    return {
        "gift_recipient_id": contribution.recipient or False,
        "gift_date": contribution.gift_date or False,
        "gift_bulk": contribution.gift_bulk,
    }


def _balance_values(cents, currency):
    balance = currency.round(cents / 100)
    return {
        "balance": balance,
        "debit": balance if balance > 0 else 0.0,
        "credit": -balance if balance < 0 else 0.0,
    }
