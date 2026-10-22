# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Finalisation, integrity check, comparison and correction (spec 7.7, G-04,
G-05, G-07).

*Finalise* checks the prerequisites of G-05 on the current data, locks the
years of the asset register and the run, and stores the payload of the
statement as canonical JSON in an attachment, with its SHA-256 chained to
the previous finalisation of the business. *Check Integrity* computes the
hashes again, follows the chain and compares the stored records with the
payload. A new calculation of a finalised year is a comparison run next to
the finalised one (C-60). A correction is a new version with a reason; the
finalised version stays readable and becomes superseded when the correction
is finalised.
"""

from markupsafe import Markup, escape

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain
from odoo.tools import format_amount

from .engine import integrity
from .euer_adjustment import INPUT_FIELDS as ADJUSTMENT_INPUTS
from .euer_form import NON_AMOUNT_TYPES
from .euer_statement import CLOSED_STATES, INTERNAL, OPEN_STATES

#: language of all texts in the payload: the source terms, so the payload
#: does not depend on the language of the user who finalises or checks
PAYLOAD_LANG = "en_US"
#: models of the references whose names are stored per language
TRANSLATED_REFERENCES = frozenset(
    {"account.account", "account.journal", "l10n_de_euer.category"}
)
PAYLOAD_MIMETYPE = "application/json"
#: sections of the payload that hold records which never change after the
#: finalisation; the integrity check compares them with the records
FROZEN_SECTIONS = (
    "statement",
    "forms",
    "fields",
    "run",
    "slices",
    "adjustments",
    "questionnaire",
    "cash_balances",
    "asset_years",
)
STATEMENT_FIELDS = (
    "name",
    "company_id",
    "year",
    "version",
    "correction_of_id",
    "correction_reason",
    "provisional_form",
    "run_id",
    "form_ids",
    "computed_on",
    "checked_on",
    "profit",
    "total_income",
    "total_expense",
    "use_pool",
    "wahlrechte_json",
    "scope_confirmed_by_id",
    "scope_confirmed_on",
    "module_version",
    "ruleset_version",
    "form_versions",
    "chain_index",
    "finalized_by_id",
    "finalized_on",
)
FORM_FIELDS = (
    "year",
    "form_type",
    "version",
    "state",
    "verified_by",
    "verified_role",
    "verified_on",
    "verification_note",
    "vordruck_ids",
    "bmf_date",
    "bmf_reference",
    "bmf_dok",
    "instructions_version",
    "source_url",
    "source_sha256",
    "fill_condition",
)
#: line and Kz of the year and the official text of each field (F-15)
LINE_FIELDS = (
    "field_id",
    "code",
    "form_type",
    "box",
    "line",
    "kz",
    "sequence",
    "field_type",
    "name",
    "official_label",
    "amount",
    "amount_computed",
    "amount_prev_year",
    "value_text",
    "source",
    "override_amount",
    "override_reason",
)
RUN_FIELDS = (
    "company_id",
    "date",
    "user_id",
    "year_from",
    "year_to",
    "config_snapshot",
    "hash",
    "invariant_ok",
    "log",
    "totals",
    "locked",
)
#: stored values of a slice; the links to entries and items are kept as
#: snapshot (``*_ref``, names): Odoo deletes the items of a statement entry
#: when it is reconciled anew, which empties the links but not the snapshot
SLICE_FIELDS = (
    "date",
    "attribution_year",
    "kind",
    "rule",
    "source_move_ref",
    "source_line_ref",
    "origin_move_ref",
    "origin_line_ref",
    "document_move_ref",
    "source_move_name",
    "origin_move_name",
    "account_id",
    "partner_id",
    "tax_repartition_line_id",
    "category_id",
    "field_code",
    "amount",
    "balance",
    "debit",
    "credit",
    "path",
    "override",
    "ten_day_applied",
    "virtual_part",
    "adjustment_id",
    "gift_recipient_id",
    "gift_date",
    "gift_bulk",
)
ADJUSTMENT_FIELDS = (
    *ADJUSTMENT_INPUTS,
    "date",
    "partner_id",
    "asset_id",
    "no_other_workplace",
    "result_amount",
    "turnover_amount",
    "withdrawal_effect",
    "contribution_effect",
    "field_codes",
    "parameters",
    "notes",
    "error",
    "reason",
)
ANSWER_FIELDS = (
    "question_code",
    "sequence",
    "answer",
    "proposal_source",
    "confirmed_by_id",
    "confirmed_on",
    "note",
)
CASH_FIELDS = ("journal_id", "balance_statement", "entered_by_id", "entered_on", "note")
ASSET_YEAR_FIELDS = (
    "asset_id",
    "year",
    "aveuer_group",
    "is_opening",
    "cost_start",
    "book_value_start",
    "additions",
    "special_depreciation",
    "depreciation",
    "disposal_book_value",
    "book_value_end",
    "months_in_year",
    "locked",
    "business_share",
    "private_cost",
    "private_use_amount",
    "private_use_vat",
    "private_use_decision",
    "private_use_confirmed_amount",
    "private_use_confirmed_vat",
    "private_use_vat_base",
    "private_use_confirmed_base",
    "private_use_reason",
    "private_use_decided_by_id",
    "private_use_decided_on",
)
CHECK_FIELDS = (
    "company_id",
    "year",
    "origin",
    "code",
    "severity",
    "message",
    "res_model",
    "res_ids",
    "amount",
    "acknowledged_by_id",
    "acknowledged_on",
    "acknowledge_reason",
)
ASSIGNMENT_FIELDS = (
    "account_id",
    "category_id",
    "date_from",
    "date_to",
    "confirmed",
    "confirmed_by_id",
    "source",
    "note",
)
MAPPING_FIELDS = (
    "category_id",
    "form_id",
    "field_code",
    "sequence",
    "share",
    "condition",
    "option",
    "davon_field_id",
    "additional_field_id",
)
VAT_PERIOD_FIELDS = (
    "date_from",
    "date_to",
    "regime",
    "basis",
    "vat_return_period",
    "permanent_extension",
    "prev_year_turnover",
    "prev_year_turnover_year",
    "reason",
)
PARAMETER_FIELDS = (
    "code",
    "date_basis",
    "date_from",
    "date_to",
    "value",
    "value_json",
    "source",
)
POOL_FIELDS = ("year", "amount", "locked", "locked_amount", "asset_ids")
CLASSIFICATION_FIELDS = (
    "source_line_id",
    "source_line_ref",
    "source_line_name",
    "obsolete",
    "check_code",
    "partial_path",
    "kind",
    "category_id",
    "tax_rate",
    "vat_regime",
    "amount_classified",
    "reason",
    "decided_by_id",
    "decided_on",
)
OPENING_FIELDS = (
    "move_line_id",
    "account_id",
    "partner_id",
    "balance",
    "category_id",
    "vat_regime",
    "tax_rate",
    "origin_reference",
    "reason",
)
TEN_DAY_FIELDS = (
    "year",
    "kind",
    "source_move_id",
    "origin_move_id",
    "cash_date",
    "amount",
    "candidate_reason",
    "proposal",
    "proposed_cash_date",
    "economic_year",
    "due_date",
    "values_entered",
    "decision",
    "applied",
    "reason",
    "decided_by_id",
    "decided_on",
)
#: settings of the business in the payload (choices and header values)
COMPANY_FIELDS = (
    "name",
    "vat",
    "l10n_de_stnr",
    "l10n_de_widnr",
    "currency_id",
    "fiscalyear_last_day",
    "fiscalyear_last_month",
    "fiscalyear_lock_date",
    "hard_lock_date",
    "tax_lock_date",
    "l10n_de_euer_enabled",
    "l10n_de_euer_separate_business",
    "l10n_de_euer_entertainment_mode",
    "l10n_de_euer_offsetting_is_payment",
    "l10n_de_euer_fx_mode",
    "l10n_de_euer_disposal_month",
    "l10n_de_euer_business_type",
    "l10n_de_euer_legal_form",
    "l10n_de_euer_income_type",
    "l10n_de_euer_owner",
    "l10n_de_euer_activity_start",
)


def _value(record, name):
    """Value of a stored field in the canonical form of the payload."""
    field = record._fields[name]
    value = record[name]
    kind = field.type
    if kind == "monetary":
        return integrity.cents(value)
    if kind == "float":
        return integrity.decimal_text(value or 0.0)
    if kind == "integer":
        return int(value or 0)
    if kind == "boolean":
        return bool(value)
    if kind == "date":
        return fields.Date.to_string(value) if value else None
    if kind == "datetime":
        return fields.Datetime.to_string(value) if value else None
    if kind == "many2one":
        return value.id or None
    if kind in ("many2many", "one2many"):
        return sorted(value.ids)
    if kind == "json":
        return None if value is None or value is False else integrity.normalize(value)
    return value or None


def _snapshot(records, names):
    """Values of ``records`` in the order of their ids."""
    return [
        {"id": record.id, **{name: _value(record, name) for name in names}}
        for record in records.sorted("id")
    ]


def _attachments(attachments):
    """Name and SHA-1 checksum of attached receipts."""
    return [
        {"id": attachment.id, "name": attachment.name, "checksum": attachment.checksum}
        for attachment in attachments.sorted("id")
    ]


class L10nDeEuerStatement(models.Model):
    _inherit = "l10n_de_euer.statement"

    chain_index = fields.Integer(
        string="Position in the Chain",
        readonly=True,
        copy=False,
        help="Position of the finalisation in the hash chain of the business.",
    )
    superseded_by_id = fields.Many2one(
        "l10n_de_euer.statement",
        compute="_compute_superseded_by_id",
        string="Corrected By",
        help="Correction of this version.",
    )
    comparison_run_id = fields.Many2one(
        "l10n_de_euer.run",
        compute="_compute_comparison_run_id",
        help="Latest calculation of the finalised year next to its finalised run "
        "(C-60).",
    )
    correction_diff_html = fields.Html(
        string="Differences to the Previous Version",
        compute="_compute_correction_diff_html",
        sanitize=False,
    )
    correction_diff_count = fields.Integer(compute="_compute_correction_diff_html")

    _company_chain_uniq = models.UniqueIndex(
        "(company_id, chain_index) WHERE chain_index IS NOT NULL",
        "A position of the hash chain of a business is used once.",
    )

    # ------------------------------------------------------------ computes
    def _compute_superseded_by_id(self):
        Statement = self.sudo()
        for statement in self:
            newer = (
                Statement.search(
                    Domain("correction_of_id", "=", statement.id),
                    order="id desc",
                    limit=1,
                )
                if statement.id
                else Statement
            )
            statement.superseded_by_id = newer.with_env(self.env)

    def _compute_comparison_run_id(self):
        Run = self.env["l10n_de_euer.run"].sudo()
        for statement in self:
            run = Run
            if statement.state in CLOSED_STATES and statement.company_id:
                run = Run.search(
                    Domain("company_id", "=", statement.company_id.id)
                    & Domain("year_from", "=", statement.year)
                    & Domain("year_to", "=", statement.year)
                    & Domain("locked", "=", False),
                    limit=1,
                )
            statement.comparison_run_id = run.with_env(self.env)

    @api.depends("line_ids.amount", "line_ids.value_text", "correction_of_id")
    def _compute_correction_diff_html(self):
        for statement in self:
            rows = (
                statement._get_correction_diff() if statement.correction_of_id else []
            )
            statement.correction_diff_count = len(rows)
            statement.correction_diff_html = (
                statement._correction_diff_table(rows)
                if statement.correction_of_id
                else False
            )

    # ------------------------------------------------------------ finalise
    def _check_manager_for(self, message):
        if not self.env.su and not self.env.user.has_group(
            "account.group_account_manager"
        ):
            raise UserError(message)

    def action_finalize(self):
        """Finalise the statement (spec 7.7 step 3, G-04, G-05)."""
        self._check_manager_for(
            self.env._("Only an accounting manager can finalise a statement.")
        )
        for statement in self:
            statement._finalize()
        return True

    def _finalize(self):
        self.ensure_one()
        if self.state != "computed":
            raise UserError(
                self.env._(
                    "%(name)s cannot be finalised: compute and check it first.",
                    name=self.name,
                )
            )
        # all checks again on the current data; this also refuses a
        # statement whose run no longer matches the bookings (G-05)
        self.action_check()
        issues = self._get_finalization_issues()
        if issues:
            raise UserError(
                self.env._(
                    "%(name)s cannot be finalised:\n%(issues)s",
                    name=self.name,
                    issues="\n".join(f"- {issue}" for issue in issues),
                )
            )
        business, year = self.company_id, self.year
        self.env["l10n_de_euer.asset"]._lock_years(business, year)
        self.run_id._lock()
        previous = self._get_chain_previous()
        self._write_internal(
            {
                "chain_index": (previous.chain_index or 0) + 1,
                "module_version": self._get_module_version(),
                "ruleset_version": self._get_ruleset_version(),
                "form_versions": ", ".join(
                    self.form_ids.sorted(lambda form: (form.form_type, form.id)).mapped(
                        "name"
                    )
                ),
                "finalized_by_id": self.env.uid,
                "finalized_on": fields.Datetime.now(),
            }
        )
        raw = integrity.canonical_json(self._build_payload())
        payload_sha256 = integrity.sha256_hex(raw)
        prev_hash = previous.chain_hash or integrity.GENESIS
        attachment = (
            self.env["ir.attachment"]
            .sudo()
            .create(
                {
                    "name": self._payload_file_name(),
                    "raw": raw,
                    "mimetype": PAYLOAD_MIMETYPE,
                    "res_model": self._name,
                    "res_id": self.id,
                    "company_id": business.id,
                }
            )
        )
        self._write_internal(
            {
                "state": "final",
                "payload_attachment_id": attachment.id,
                "payload_sha256": payload_sha256,
                "prev_hash": prev_hash,
                "chain_hash": integrity.chain_hash(prev_hash, payload_sha256),
            }
        )
        corrected = self.correction_of_id
        if corrected:
            corrected._write_internal({"state": "superseded"})
            corrected.sudo().message_post(
                body=self.env._(
                    "Superseded by the correction %(name)s; this version stays "
                    "unchanged and readable.",
                    name=self.name,
                )
            )
        self.message_post(
            body=self.env._(
                "Finalised by %(user)s: payload SHA-256 %(payload)s, chain hash "
                "%(chain)s (position %(index)s). Keep both hashes also outside the "
                "database, e.g. with the PDF of the fill-in aid: only then a "
                "consistent change of the database becomes visible. Keep the "
                "statement for at least 10 years (§ 147 (3) AO).",
                user=self.env.user.name,
                payload=payload_sha256,
                chain=self.chain_hash,
                index=self.chain_index,
            )
        )

    def _get_finalization_issues(self):
        """What prevents the finalisation after the checks (G-05): blockers
        and warnings that are not acknowledged."""
        self.ensure_one()
        self.invalidate_recordset(["check_ids", "blocker_count", "open_warning_count"])
        issues = []
        checks = self.check_ids.sorted(lambda check: (check.code, check.id))
        for check in checks.filtered(lambda check: check.severity == "blocker"):
            issues.append(
                self.env._(
                    "Blocker %(code)s: %(message)s",
                    code=check.code,
                    message=check.message,
                )
            )
        for check in checks.filtered(
            lambda check: check.severity == "warning" and not check.acknowledged_on
        ):
            issues.append(
                self.env._(
                    "Warning %(code)s not acknowledged: %(message)s",
                    code=check.code,
                    message=check.message,
                )
            )
        return issues

    def _get_chain_previous(self):
        """Last finalisation of the business (the previous link)."""
        self.ensure_one()
        return self.sudo().search(
            Domain("company_id", "=", self.company_id.id)
            & Domain("chain_index", "!=", False)
            & Domain("id", "!=", self.id),
            order="chain_index desc",
            limit=1,
        )

    def _get_module_version(self):
        module = (
            self.env["ir.module.module"]
            .sudo()
            .search(Domain("name", "=", "l10n_de_euer"), limit=1)
        )
        return module.latest_version or ""

    def _get_parameters_snapshot(self):
        parameters = (
            self.env["l10n_de_euer.parameter"]
            .sudo()
            .with_context(active_test=False)
            .search(Domain("code", "!=", False))
        )
        rows = _snapshot(parameters, PARAMETER_FIELDS)
        return sorted(
            rows, key=lambda row: (row["code"], row["date_from"] or "", row["id"])
        )

    def _get_ruleset_version(self):
        """SHA-256 over the rule parameters (the rule set of the year)."""
        return integrity.sha256_hex(
            integrity.canonical_json(self._get_parameters_snapshot())
        )

    def _payload_file_name(self):
        return f"euer_{self.year}_{self.company_id.id}_v{self.version}_payload.json"

    # ------------------------------------------------------------ payload
    def _payload_env(self):
        return self.with_context(lang=PAYLOAD_LANG, active_test=False).sudo()

    def _get_asset_years(self):
        return (
            self.env["l10n_de_euer.asset.year"]
            .sudo()
            .search(
                Domain("company_id", "=", self.company_id.id)
                & Domain("year", "=", self.year)
            )
        )

    def _get_frozen_sections(self, asset_years=None):
        """Sections of the payload that never change after the finalisation.

        ``asset_years``: the asset years of the payload (the integrity check
        compares the stored ones); default: those of the year now.
        """
        self.ensure_one()
        statement = self._payload_env()
        run = statement.run_id
        if asset_years is None:
            asset_years = statement._get_asset_years()
        asset_years = asset_years.with_env(statement.env)
        statement_values = {name: _value(statement, name) for name in STATEMENT_FIELDS}
        run_values = _snapshot(run, RUN_FIELDS)[0] if run else None
        if run_values is not None:
            run_values["form_values"] = {
                code: integrity.cents(value)
                for code, value in sorted((run.form_values or {}).items())
            }
        adjustments = []
        for adjustment in statement.adjustment_ids.sorted("id"):
            row = _snapshot(adjustment, ADJUSTMENT_FIELDS)[0]
            row["field_amounts"] = {
                code: integrity.cents(value)
                for code, value in sorted((adjustment.field_amounts or {}).items())
            }
            row["attachments"] = _attachments(adjustment.attachment_ids)
            adjustments.append(row)
        balances = []
        for balance in statement.cash_balance_ids.sorted("id"):
            row = _snapshot(balance, CASH_FIELDS)[0]
            row["attachments"] = _attachments(balance.attachment_ids)
            balances.append(row)
        return {
            "statement": statement_values,
            "forms": _snapshot(statement.form_ids, FORM_FIELDS),
            "fields": sorted(
                _snapshot(statement.line_ids, LINE_FIELDS),
                key=lambda row: (row["sequence"], row["code"], row["id"]),
            ),
            "run": run_values,
            "slices": _snapshot(
                run.slice_ids if run else self.env["l10n_de_euer.cash.slice"],
                SLICE_FIELDS,
            ),
            "adjustments": adjustments,
            "questionnaire": {
                "confirmed_by_id": statement_values["scope_confirmed_by_id"],
                "confirmed_on": statement_values["scope_confirmed_on"],
                "answers": sorted(
                    _snapshot(statement.scope_answer_ids, ANSWER_FIELDS),
                    key=lambda row: (row["sequence"], row["question_code"]),
                ),
            },
            "cash_balances": balances,
            "asset_years": _snapshot(asset_years, ASSET_YEAR_FIELDS),
        }

    def _build_payload(self):
        """Payload of the finalisation (spec 7.7): the frozen sections and
        the context of the year at the time of the finalisation."""
        self.ensure_one()
        statement = self._payload_env()
        business = statement.company_id
        root = business.root_id
        year = statement.year
        company_fields = [name for name in COMPANY_FIELDS if name in business._fields]
        payload = {
            "schema": integrity.SCHEMA,
            "lang": PAYLOAD_LANG,
            **statement._get_frozen_sections(),
            "slice_totals": statement._get_slice_totals(),
            "settings": {
                "business": _snapshot(business, company_fields)[0],
                "main_company": _snapshot(root, company_fields)[0],
                "companies": sorted(business._l10n_de_euer_business_companies().ids),
            },
            "choices": integrity.normalize(statement.wahlrechte_json or {}),
            "vat_periods": _snapshot(
                self.env["l10n_de_euer.vat.period"]
                .sudo()
                .search(Domain("company_id", "=", root.id)),
                VAT_PERIOD_FIELDS,
            ),
            "assignment": statement._get_assignment_snapshot(),
            "parameters": statement._get_parameters_snapshot(),
            "asset_pools": _snapshot(
                self.env["l10n_de_euer.asset.pool"]
                .sudo()
                .search(
                    Domain("company_id", "=", business.id) & Domain("year", "<=", year)
                ),
                POOL_FIELDS,
            ),
            "checks": _snapshot(statement.check_ids, CHECK_FIELDS),
            "classifications": _snapshot(
                statement.classification_ids, CLASSIFICATION_FIELDS
            ),
            "opening_items": _snapshot(
                self.env["l10n_de_euer.opening.item"]
                .sudo()
                .search(Domain("company_id", "=", business.id)),
                OPENING_FIELDS,
            ),
            "ten_day_items": _snapshot(statement.ten_day_ids, TEN_DAY_FIELDS),
            "cash_balance_odoo": {
                str(balance.journal_id.id): integrity.cents(balance.balance_odoo)
                for balance in statement.cash_balance_ids
            },
            "correction": statement._get_correction_payload(),
        }
        payload["references"] = statement._get_references(payload)
        return payload

    def _get_slice_totals(self):
        """Sum of the slices per field code of the year (cent)."""
        totals = {}
        for one in self.run_id.slice_ids:
            if one.field_code and one.attribution_year == self.year:
                totals[one.field_code] = totals.get(
                    one.field_code, 0
                ) + integrity.cents(one.amount)
        return dict(sorted(totals.items()))

    def _get_assignment_snapshot(self):
        """Account → category → field of the year (spec 4.1)."""
        root = self.company_id.root_id
        start, end = f"{self.year}-01-01", f"{self.year}-12-31"
        assignments = (
            self.env["l10n_de_euer.account.assignment"]
            .sudo()
            .search(
                Domain("company_id", "=", root.id)
                & (Domain("date_from", "=", False) | Domain("date_from", "<=", end))
                & (Domain("date_to", "=", False) | Domain("date_to", ">=", start))
            )
        )
        euer = self.form_ids.filtered(lambda form: form.form_type == "euer")
        mappings = (
            self.env["l10n_de_euer.category.mapping"]
            .sudo()
            .search(
                Domain("category_id", "in", assignments.category_id.ids)
                & Domain("form_id", "in", euer.ids)
            )
        )
        return {
            "accounts": _snapshot(assignments, ASSIGNMENT_FIELDS),
            "mappings": _snapshot(mappings, MAPPING_FIELDS),
        }

    def _get_references(self, payload):
        """Names of the records the payload refers to by id, as they were at
        the finalisation (codes and names can change later).

        Accounts, journals and categories have translated names: they are
        stored per installed language, ``{"code", "name": {lang: name}}``,
        so an output shows them in its language as before the finalisation
        (:meth:`_reference` of the outputs)."""
        business = self.company_id
        langs = sorted(
            {PAYLOAD_LANG}
            | {code for code, _name in self.env["res.lang"].get_installed()}
        )
        ids = {
            "account.account": set(),
            "res.partner": set(),
            "l10n_de_euer.category": set(),
            "account.journal": set(),
            "res.users": set(),
            "l10n_de_euer.asset": set(),
        }
        for row in payload["slices"]:
            ids["account.account"].add(row["account_id"])
            ids["res.partner"].add(row["partner_id"])
            ids["res.partner"].add(row["gift_recipient_id"])
            ids["l10n_de_euer.category"].add(row["category_id"])
        for row in payload["assignment"]["accounts"]:
            ids["account.account"].add(row["account_id"])
            ids["l10n_de_euer.category"].add(row["category_id"])
        for row in payload["assignment"]["mappings"]:
            ids["l10n_de_euer.category"].add(row["category_id"])
        for row in payload["cash_balances"]:
            ids["account.journal"].add(row["journal_id"])
            ids["res.users"].add(row["entered_by_id"])
        for row in payload["adjustments"]:
            ids["res.partner"].add(row["partner_id"])
            ids["l10n_de_euer.asset"].add(row["asset_id"])
        for row in payload["asset_years"]:
            ids["l10n_de_euer.asset"].add(row["asset_id"])
        for row in payload["checks"]:
            ids["res.users"].add(row["acknowledged_by_id"])
        for row in payload["questionnaire"]["answers"]:
            ids["res.users"].add(row["confirmed_by_id"])
        statement = payload["statement"]
        ids["res.users"].update(
            {statement["finalized_by_id"], statement["scope_confirmed_by_id"]}
        )
        result = {}
        for model, record_ids in ids.items():
            records = (
                self.env[model]
                .sudo()
                .with_context(lang=PAYLOAD_LANG, active_test=False)
                .browse(sorted(record_ids - {None}))
                .exists()
            )
            if model in TRANSLATED_REFERENCES:
                if model == "account.account":
                    records = records.with_company(business)
                names = {
                    lang: dict(
                        zip(
                            records.ids,
                            records.with_context(lang=lang).mapped("name"),
                            strict=True,
                        )
                    )
                    for lang in langs
                }
                result[model] = {
                    str(record.id): {
                        "code": record.code,
                        "name": {lang: names[lang][record.id] for lang in langs},
                    }
                    for record in records
                }
            else:
                result[model] = {
                    str(record.id): record.display_name for record in records
                }
        return result

    def _read_payload(self):
        """``(raw, payload)`` of the stored payload; ``None`` parts if it is
        missing or no valid JSON."""
        self.ensure_one()
        attachment = self.sudo().payload_attachment_id.exists()
        if not attachment:
            return None, None
        raw = attachment.raw or b""
        try:
            return raw, integrity.load(raw)
        except ValueError:
            return raw, None

    # ------------------------------------------------------------ integrity
    def action_check_integrity(self):
        """Compute the hashes again, follow the chain and compare the stored
        records with the payload (spec 7.7 step 4); result in the chatter."""
        self.ensure_one()
        self.check_access("read")
        problems = self._verify_integrity()
        if problems:
            body = Markup("<p>%s</p><ul>%s</ul>") % (
                self.env._(
                    "Integrity check of %(name)s by %(user)s failed:",
                    name=self.name,
                    user=self.env.user.name,
                ),
                Markup().join(Markup("<li>%s</li>") % problem for problem in problems),
            )
        else:
            body = self.env._(
                "Integrity check of %(name)s by %(user)s: the payload, its hash "
                "%(payload)s, the chain of %(count)s finalisations of %(business)s "
                "and the finalised records are unchanged.",
                name=self.name,
                user=self.env.user.name,
                payload=self.payload_sha256,
                count=self.chain_index,
                business=self.company_id.name,
            )
        self.sudo().message_post(body=body, author_id=self.env.user.partner_id.id)
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": self.env._("Integrity Check"),
                "message": self.env._("The integrity check failed; see the log.")
                if problems
                else self.env._("The statement is unchanged."),
                "type": "danger" if problems else "success",
                "sticky": bool(problems),
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def _verify_integrity(self):
        """Problems of the finalised statement as texts (empty: unchanged)."""
        self.ensure_one()
        if self.state not in CLOSED_STATES or not self.chain_index:
            raise UserError(
                self.env._("Only a finalised statement has an integrity check.")
            )
        chain = self.sudo().search(
            Domain("company_id", "=", self.company_id.id)
            & Domain("chain_index", "!=", False)
            & Domain("chain_index", "<=", self.chain_index),
            order="chain_index",
        )
        links = []
        for statement in chain:
            raw, _payload = statement._read_payload()
            links.append(
                integrity.Link(
                    index=statement.chain_index,
                    prev_hash=statement.prev_hash or "",
                    payload_sha256=statement.payload_sha256 or "",
                    chain_hash=statement.chain_hash or "",
                    content_sha256=integrity.sha256_hex(raw)
                    if raw is not None
                    else None,
                    reference=statement,
                )
            )
        texts = [
            self._problem_text(problem) for problem in integrity.verify_chain(links)
        ]
        raw, payload = self._read_payload()
        if payload is None:
            if raw is not None:
                texts.append(self.env._("The stored payload is no valid JSON."))
            return texts
        asset_years = self.env["l10n_de_euer.asset.year"].browse(
            [row["id"] for row in payload.get("asset_years") or []]
        )
        current = self._get_frozen_sections(asset_years=asset_years.exists())
        if len(asset_years.exists()) != len(asset_years):
            current["asset_years"] = None
        changed = integrity.changed_sections(payload, current, FROZEN_SECTIONS)
        if changed:
            labels = self._section_labels()
            texts.append(
                self.env._(
                    "The finalised records differ from the payload: %(sections)s.",
                    sections=", ".join(labels[name] for name in changed),
                )
            )
        texts += self._verify_receipts(payload)
        return texts

    def _verify_receipts(self, payload):
        """Receipts of the finalisation (statements of account, own receipts):
        their content must still have the SHA-1 checksum of the payload.

        The checksum stored on an attachment is not computed again when its
        content changes in the database or the file store; the content is
        hashed here.
        """
        Attachment = self.env["ir.attachment"].sudo()
        labels = self._section_labels()
        texts = []
        for section in ("cash_balances", "adjustments"):
            for row in payload.get(section) or []:
                for item in row.get("attachments") or []:
                    attachment = Attachment.browse(item.get("id")).exists()
                    raw = attachment.raw if attachment else None
                    if raw is None or attachment._compute_checksum(raw) != item.get(
                        "checksum"
                    ):
                        texts.append(
                            self.env._(
                                "The receipt %(name)s of the %(section)s is missing "
                                "or its content was changed after the finalisation.",
                                name=item.get("name") or item.get("id"),
                                section=labels[section],
                            )
                        )
        return texts

    def _section_labels(self):
        return {
            "statement": self.env._("statement"),
            "forms": self.env._("form years"),
            "fields": self.env._("field values"),
            "run": self.env._("calculation run"),
            "slices": self.env._("slices"),
            "adjustments": self.env._("profit corrections"),
            "questionnaire": self.env._("questionnaire"),
            "cash_balances": self.env._("bank reconciliation"),
            "asset_years": self.env._("asset years"),
        }

    def _problem_text(self, problem):
        name = problem.reference.name if problem.reference else ""
        texts = {
            "payload_missing": self.env._(
                "%(name)s (position %(index)s): the stored payload is missing.",
                name=name,
                index=problem.index,
            ),
            "payload_changed": self.env._(
                "%(name)s (position %(index)s): the stored payload was changed, its "
                "hash differs from the hash of the finalisation.",
                name=name,
                index=problem.index,
            ),
            "chain_hash": self.env._(
                "%(name)s (position %(index)s): the chain hash does not follow from "
                "the previous hash and the payload hash.",
                name=name,
                index=problem.index,
            ),
            "broken_link": self.env._(
                "%(name)s (position %(index)s): the previous hash is not the chain "
                "hash of the finalisation before.",
                name=name,
                index=problem.index,
            ),
            "gap": self.env._(
                "The chain has a gap: position %(expected)s is missing before "
                "%(name)s (position %(index)s).",
                expected=problem.params.get("expected"),
                name=name,
                index=problem.index,
            ),
        }
        return texts.get(problem.key, problem.key)

    # ------------------------------------------------------------ C-60
    def action_compare(self):
        """Compute the finalised year again into a comparison run, without
        changing the finalised data (spec 7.7 step 5, G-07); deviations are
        the warnings C-60."""
        self.ensure_one()
        if self.state != "final":
            raise UserError(
                self.env._("Only the valid finalised version can be compared.")
            )
        correction = self.sudo().search(
            Domain("correction_of_id", "=", self.id)
            & Domain("state", "in", list(OPEN_STATES)),
            limit=1,
        )
        if correction:
            # a new calculation of the year replaces the run of the
            # correction in progress
            raise UserError(
                self.env._(
                    "The correction %(name)s is in progress: compute it instead; "
                    "its calculation shows the differences to this version.",
                    name=correction.name,
                )
            )
        run = (
            self.env["l10n_de_euer.run"]
            .with_context(l10n_de_euer_statement_id=self.id)
            ._compute_year(self.company_id, self.year)
        )
        self.invalidate_recordset(
            ["check_ids", "blocker_count", "open_warning_count", "comparison_run_id"]
        )
        deviations = self.env["l10n_de_euer.check"].search_count(
            Domain("company_id", "=", self.company_id.id)
            & Domain("year", "=", self.year)
            & Domain("origin", "=", "calculation")
            & Domain("code", "=", "C-60")
            & Domain(
                "res_model",
                "in",
                ["l10n_de_euer.field", "l10n_de_euer.cash.balance", "account.journal"],
            )
        )
        self.message_post(
            body=self.env._(
                "Compared with a new calculation of the year (%(run)s): %(count)s "
                "deviations (C-60). The finalised values stay unchanged.",
                run=run.display_name,
                count=deviations,
            )
        )
        return True

    def action_open_comparison_run(self):
        self.ensure_one()
        return self.comparison_run_id.action_open_slices()

    @api.model
    def _get_c60_results(self, business, year, run):
        """C-60: deviations of ``run`` from the finalised run of the year.

        Compares the form values and the sums of the slices per field with
        the finalised run, and the balances in Odoo at 31 December with the
        finalised bank reconciliation (G-17). Not for the run of a correction
        in progress: its differences are in the report of the correction.
        """
        Statement = self.sudo()
        final = Statement.search(
            Domain("company_id", "=", business.id)
            & Domain("year", "=", year)
            & Domain("state", "=", "final"),
            limit=1,
        )
        if not final or not final.run_id or final.run_id == run:
            return []
        if self._get_for_calculation(business, year) != final:
            return []
        frozen = final.run_id
        currency = business.currency_id
        fields_by_code = {field.code: field for field in final.form_ids.field_ids}
        values = {
            code: (old, new)
            for code, old, new in integrity.diff(
                {
                    code: integrity.cents(v)
                    for code, v in (frozen.form_values or {}).items()
                },
                {
                    code: integrity.cents(v)
                    for code, v in (run.form_values or {}).items()
                },
            )
        }
        totals = {
            code: (old, new)
            for code, old, new in integrity.diff(
                {code: int(v) for code, v in (frozen.totals or {}).items()},
                {code: int(v) for code, v in (run.totals or {}).items()},
            )
        }

        def money(cents):
            return format_amount(self.env, cents / 100, currency)

        results = []
        for code in sorted(set(values) | set(totals)):
            field = fields_by_code.get(code)
            label = (
                self.env._("Line %(line)s (Kz %(kz)s)", line=field.line, kz=field.kz)
                if field and field.kz
                else code
            )
            changes = []
            if code in values:
                old, new = values[code]
                changes.append(
                    self.env._(
                        "value %(old)s, now %(new)s", old=money(old), new=money(new)
                    )
                )
            if code in totals:
                old, new = totals[code]
                changes.append(
                    self.env._(
                        "slices %(old)s, now %(new)s", old=money(old), new=money(new)
                    )
                )
            old, new = values.get(code) or totals[code]
            results.append(
                {
                    "code": "C-60",
                    "severity": "warning",
                    "message": self.env._(
                        "Deviation from the finalised statement %(name)s in "
                        "%(field)s: finalised %(changes)s. Create a correction if "
                        "the statement is affected.",
                        name=final.name,
                        field=label,
                        changes="; ".join(changes),
                    ),
                    "res_model": "l10n_de_euer.field" if field else False,
                    "res_ids": [field.id] if field else [],
                    "amount": (new - old) / 100,
                }
            )
        for balance in final.cash_balance_ids.sorted("id"):
            odoo = balance._get_odoo_balance()
            balance_currency = balance.currency_id or currency
            if not balance_currency.compare_amounts(odoo, balance.balance_statement):
                continue
            results.append(
                {
                    "code": "C-60",
                    "severity": "warning",
                    "message": self.env._(
                        "Bank reconciliation of 31 December %(year)s: the balance in "
                        "Odoo of %(journal)s is now %(odoo)s; the finalised statement "
                        "%(name)s has the balance per statement of account "
                        "%(statement)s.",
                        year=year,
                        journal=balance.journal_id.display_name,
                        odoo=format_amount(self.env, odoo, balance_currency),
                        name=final.name,
                        statement=format_amount(
                            self.env, balance.balance_statement, balance_currency
                        ),
                    ),
                    "res_model": "l10n_de_euer.cash.balance",
                    "res_ids": [balance.id],
                    "amount": odoo - balance.balance_statement,
                }
            )
        for journal in final._get_money_journals() - final.cash_balance_ids.journal_id:
            results.append(
                {
                    "code": "C-60",
                    "severity": "warning",
                    "message": self.env._(
                        "Bank reconciliation of 31 December %(year)s: %(journal)s has "
                        "entries of the year now, but no balance in the finalised "
                        "statement %(name)s.",
                        year=year,
                        journal=journal.display_name,
                        name=final.name,
                    ),
                    "res_model": "account.journal",
                    "res_ids": [journal.id],
                    "amount": 0.0,
                }
            )
        return results

    # ------------------------------------------------------------ correction
    def action_open_correction_wizard(self):
        self.ensure_one()
        self._check_manager_for(
            self.env._("Only an accounting manager can correct a statement.")
        )
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Create Correction"),
            "res_model": "l10n_de_euer.statement.correction",
            "view_mode": "form",
            "target": "new",
            "context": {"default_statement_id": self.id},
        }

    def _create_correction(self, reason):
        """New version of a finalised statement (spec 7.7 step 6, G-07).

        The correction starts from the values, overrides, profit corrections
        and the bank reconciliation of the version it corrects; the answers
        of the questionnaire are proposals that a manager confirms again.
        The finalised version stays unchanged; it is superseded when the
        correction is finalised.
        """
        self.ensure_one()
        self._check_manager_for(
            self.env._("Only an accounting manager can correct a statement.")
        )
        if self.state != "final":
            raise UserError(
                self.env._("Only the valid finalised version can be corrected.")
            )
        if not (reason or "").strip():
            raise UserError(self.env._("Enter the reason of the correction."))
        Statement = self.sudo().with_context(**{INTERNAL: True})
        existing = Statement.search(
            Domain("company_id", "=", self.company_id.id)
            & Domain("year", "=", self.year)
            & Domain("state", "in", list(OPEN_STATES))
        )
        if existing:
            raise UserError(
                self.env._(
                    "There is already a correction of %(year)s in progress: %(names)s.",
                    year=self.year,
                    names=", ".join(existing.mapped("name")),
                )
            )
        version = (
            max(
                Statement.search(
                    Domain("company_id", "=", self.company_id.id)
                    & Domain("year", "=", self.year)
                ).mapped("version")
            )
            + 1
        )
        correction = Statement.create(
            {
                "company_id": self.company_id.id,
                "year": self.year,
                "version": version,
                "correction_of_id": self.id,
                "correction_reason": reason.strip(),
            }
        )
        line_values = []
        for line in self.line_ids:
            vals = line.copy_data()[0]
            vals["statement_id"] = correction.id
            line_values.append(vals)
        self.env["l10n_de_euer.statement.line"].sudo().with_context(
            **{INTERNAL: True}
        ).create(line_values)
        correction._write_internal(
            {
                "form_ids": [fields.Command.set(self.form_ids.ids)],
                "provisional_form": self.provisional_form,
            }
        )
        correction = correction.with_env(self.env)
        for adjustment in self.adjustment_ids.sorted("id"):
            adjustment.copy({"statement_id": correction.id})
        entered = {
            balance.journal_id: balance
            for balance in self.cash_balance_ids
            if balance.entered_on
        }
        for balance in correction.cash_balance_ids:
            old = entered.get(balance.journal_id)
            if old:
                balance.write(
                    {
                        "balance_statement": old.balance_statement,
                        "attachment_ids": [fields.Command.set(old.attachment_ids.ids)],
                        "note": old.note,
                    }
                )
        correction.message_post(
            body=self.env._(
                "Correction of %(name)s created by %(user)s. Reason: %(reason)s",
                name=self.name,
                user=self.env.user.name,
                reason=correction.correction_reason,
            )
        )
        self.message_post(
            body=self.env._(
                "Correction %(name)s created. Reason: %(reason)s",
                name=correction.name,
                reason=correction.correction_reason,
            )
        )
        return correction

    @api.model
    def _line_value(self, line):
        if line.field_type in NON_AMOUNT_TYPES:
            return line.value_text or ""
        return integrity.cents(line.amount)

    def _get_correction_diff(self):
        """Differences of the values to the version this one corrects:
        ``[{code, line, kz, name, old, new, difference}]`` in the order of
        the form; amounts in cent, texts as text (``difference`` ``None``)."""
        self.ensure_one()
        previous = self.correction_of_id
        if not previous:
            return []
        old_lines = {line.code: line for line in previous.line_ids}
        new_lines = {line.code: line for line in self.line_ids}
        differences = integrity.diff(
            {code: self._line_value(line) for code, line in old_lines.items()},
            {code: self._line_value(line) for code, line in new_lines.items()},
        )
        rows = []
        for code, old, new in differences:
            line = new_lines.get(code) or old_lines[code]
            rows.append(
                {
                    "code": code,
                    "line": line.line,
                    "kz": line.kz or "",
                    "name": line.name or code,
                    "sequence": line.sequence,
                    "old": old,
                    "new": new,
                    "difference": None if isinstance(old, str) else new - old,
                }
            )
        rows.sort(key=lambda row: (row["sequence"], row["code"]))
        return rows

    def _correction_diff_table(self, rows):
        """Report of the differences as an accessible table (O-11)."""
        self.ensure_one()
        currency = self.company_id.currency_id

        def text(value):
            if isinstance(value, str):
                return value
            return format_amount(self.env, value / 100, currency)

        caption = self.env._(
            "Differences of %(name)s to %(previous)s",
            name=self.name,
            previous=self.correction_of_id.name,
        )
        if not rows:
            return Markup("<p>%s</p>") % self.env._(
                "No differences to %(previous)s.", previous=self.correction_of_id.name
            )
        head = Markup("").join(
            Markup('<th scope="col"%s>%s</th>')
            % (Markup(' class="text-end"') if numeric else Markup(""), label)
            for label, numeric in (
                (self.env._("Line"), False),
                (self.env._("Kz"), False),
                (self.env._("Field"), False),
                (self.correction_of_id.name, True),
                (self.name, True),
                (self.env._("Difference"), True),
            )
        )
        body = Markup("").join(
            Markup(
                '<tr><td>%s</td><td>%s</td><th scope="row">%s</th>'
                '<td class="text-end">%s</td><td class="text-end">%s</td>'
                '<td class="text-end">%s</td></tr>'
            )
            % (
                row["line"] or "",
                row["kz"],
                row["name"],
                text(row["old"]),
                text(row["new"]),
                text(row["difference"]) if row["difference"] is not None else "",
            )
            for row in rows
        )
        return Markup(
            '<table class="table table-sm"><caption>%s</caption>'
            "<thead><tr>%s</tr></thead><tbody>%s</tbody></table>"
        ) % (escape(caption), head, body)

    def _get_correction_payload(self):
        """The corrected version and the differences to it (G-07)."""
        previous = self.correction_of_id
        if not previous:
            return None
        return {
            "of_id": previous.id,
            "of_version": previous.version,
            "of_payload_sha256": previous.payload_sha256,
            "of_chain_hash": previous.chain_hash,
            "reason": self.correction_reason,
            "differences": [
                {
                    "code": row["code"],
                    "line": row["line"],
                    "kz": row["kz"],
                    "old": row["old"],
                    "new": row["new"],
                }
                for row in self._get_correction_diff()
            ],
        }
