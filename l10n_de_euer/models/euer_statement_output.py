# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Outputs of a statement (spec 7.5, 7.12, G-08, G-09, G-11).

The statement collects the values of its outputs here; the reports
(:mod:`..report`) and the downloads (:mod:`..controllers`) only present
them. All outputs read the same records: the values of the statement
lines, the slices of its run, the asset register of the business and the
check results, so every output shows the same amounts as the statement.
They read only the data of the business of the statement (spec 7.9).
"""

import datetime
import io
from collections import Counter, defaultdict
from decimal import Decimal

import xlsxwriter

from odoo import api, fields, models, tools
from odoo.exceptions import UserError
from odoo.fields import Domain
from odoo.tools import float_repr

from .engine import depreciation as dep
from .engine import export
from .engine import scope as scope_engine
from .euer_form import FORM_TYPES, NON_AMOUNT_TYPES
from .euer_statement import CLOSED_STATES, MANDATORY_HEADER
from .euer_statement_final import PAYLOAD_LANG

#: states in which a statement has values to print and export
OUTPUT_STATES = ("computed", "final", "superseded")
#: sums of the form: the outputs show them as control values, because it is
#: open whether Mein ELSTER computes them itself (spec 8.1, U-12)
CONTROL_TYPES = ("subtotal", "total")
FIELD_WITHDRAWALS = "EUER.29.122"
FIELD_CONTRIBUTIONS = "EUER.29.123"
XLSX_MIMETYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
#: downloads of a statement: kind → method, mimetype, file extension
EXPORTS = {
    "workbook": ("_export_workbook", XLSX_MIMETYPE, "xlsx"),
    "evidence": ("_export_evidence_xlsx", XLSX_MIMETYPE, "xlsx"),
    "checks": ("_export_checks_xlsx", XLSX_MIMETYPE, "xlsx"),
    "elster": ("_export_elster_csv", "text/csv; charset=utf-8", "csv"),
    "json": ("_export_json", "application/json", "json"),
    "z3": ("_export_z3", "application/zip", "zip"),
}
#: characters Excel does not allow in sheet names
_SHEET_FORBIDDEN = str.maketrans({char: " " for char in "[]:*?/\\"})


def _decimal(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


class L10nDeEuerStatement(models.Model):
    _inherit = "l10n_de_euer.statement"  # pylint: disable=consider-merging-classes-inherited

    # ------------------------------------------------------------ helpers
    def _check_output(self):
        """Outputs need computed values (spec 7.5)."""
        self.ensure_one()
        if self.state not in OUTPUT_STATES or not self.run_id:
            raise UserError(
                self.env._(
                    "Compute %(name)s before printing or exporting it.",
                    name=self.name,
                )
            )

    def _output_record(self):
        """The statement as superuser for reading its data: the user may
        read the statement (checked), the data belongs to its business."""
        self.ensure_one()
        self.check_access("read")
        self._check_output()
        return self.sudo()

    def _selection_labels(self, model, name):
        Model = self.env[model]
        return dict(Model._fields[name]._description_selection(self.env))

    def _local_datetime(self, value):
        if not value:
            return ""
        local = fields.Datetime.context_timestamp(self, value)
        return local.strftime("%d.%m.%Y %H:%M")

    def _account_text(self, account):
        if not account:
            return ""
        account = account.with_company(self.company_id)
        return f"{account.code or ''} {account.name or ''}".strip()

    def _fields_by_code(self):
        return {field.code: field for field in self.form_ids.field_ids}

    # ------------------------------------------------------------ payload
    @tools.ormcache("statement_id", "payload_sha256")
    def _get_payload_parts(self, statement_id, payload_sha256):
        """Account assignment and names of the payload of a finalisation
        (cached per payload, which never changes)."""
        statement = self.sudo().browse(statement_id)
        _raw, payload = statement._read_payload()
        payload = payload or {}
        return {
            "assignment": payload.get("assignment"),
            "references": payload.get("references"),
        }

    def _get_frozen_parts(self):
        """``{"assignment", "references"}`` of the payload of a finalised
        statement: its outputs show the account assignment and the codes and
        names of accounts and partners as finalised, not as they are today;
        ``None`` for a statement that is not finalised."""
        self.ensure_one()
        statement = self.sudo()
        if statement.state not in CLOSED_STATES or not statement.payload_sha256:
            return None
        parts = self._get_payload_parts(statement.id, statement.payload_sha256)
        if not parts["assignment"] or parts["references"] is None:
            return None
        return parts

    def _reference(self, references, model, record_id):
        """Entry of the names of the payload for a record id, or ``None``.

        Translated names are stored per language (``{lang: name}``): the
        entry holds the name in the language of the output, else in the
        language of the payload."""
        if references is None or not record_id:
            return None
        entry = (references.get(model) or {}).get(str(record_id))
        if isinstance(entry, dict) and isinstance(entry.get("name"), dict):
            names = entry["name"]
            name = names.get(self.env.lang or PAYLOAD_LANG)
            if name is None:
                name = names.get(PAYLOAD_LANG) or ""
            entry = {**entry, "name": name}
        return entry

    # ------------------------------------------------------------ status
    def _get_output_status(self):
        """State, version and hashes shown on every output (spec 7.5)."""
        statement = self.sudo()
        states = self._selection_labels(self._name, "state")
        form_names = ", ".join(
            statement.form_ids.sorted(lambda form: (form.form_type, form.id)).mapped(
                "name"
            )
        )
        return {
            "state": statement.state,
            "state_label": states.get(statement.state, statement.state),
            "final": statement.state in CLOSED_STATES,
            "version": statement.version,
            "provisional": statement.provisional_form,
            "computed_on": self._local_datetime(statement.computed_on),
            "finalized_on": self._local_datetime(statement.finalized_on),
            "finalized_by": statement.finalized_by_id.name or "",
            "payload_sha256": statement.payload_sha256 or "",
            "chain_hash": statement.chain_hash or "",
            "chain_index": statement.chain_index or 0,
            "module_version": statement.module_version
            or statement._get_module_version(),
            "ruleset_version": statement.ruleset_version
            or statement._get_ruleset_version(),
            "form_versions": statement.form_versions or form_names,
            "generated_on": self._local_datetime(fields.Datetime.now()),
        }

    def _get_status_rows(self):
        """Status as label/value pairs (spreadsheets, data sets)."""
        status = self._get_output_status()
        rows = [
            (self.env._("Statement"), self.name),
            (self.env._("Business"), self.company_id.name),
            (self.env._("Year"), str(self.year)),
            (self.env._("State"), status["state_label"]),
            (self.env._("Version"), str(status["version"])),
            (self.env._("Computed On"), status["computed_on"]),
            (self.env._("Finalised On"), status["finalized_on"]),
            (self.env._("Finalised By"), status["finalized_by"]),
            (self.env._("Payload SHA-256"), status["payload_sha256"]),
            (self.env._("Chain Hash"), status["chain_hash"]),
            (self.env._("Module Version"), status["module_version"]),
            (self.env._("Rule Set Version"), status["ruleset_version"]),
            (self.env._("Form Versions"), status["form_versions"]),
            (self.env._("Created On"), status["generated_on"]),
        ]
        if status["provisional"]:
            rows.append(
                (
                    self.env._("Provisional"),
                    self.env._("Provisional on the forms of an earlier year."),
                )
            )
        return rows

    # ------------------------------------------------------------ fill aid
    def _key_meaning(self, code, value):
        """Meaning of a key value of the header (F-08)."""
        if not value:
            return ""
        if code == "EUER.15.103":
            labels = self._selection_labels("res.company", "l10n_de_euer_income_type")
            return labels.get(value, "")
        if code == "EUER.15.104":
            labels = self._selection_labels("res.company", "l10n_de_euer_owner")
            return labels.get(value, "")
        if code in (scope_engine.FIELD_LAND, dep.FIELD_ELECTRIC):
            return {"1": self.env._("Yes"), "2": self.env._("No")}.get(value, "")
        return ""

    def _fill_aid_row(self, line, ambiguous=frozenset()):
        """Row of the fill-in aid; ``ambiguous``: official labels of more
        than one field of the form."""
        header = line.field_type in NON_AMOUNT_TYPES
        mandatory = set(MANDATORY_HEADER) | {scope_engine.FIELD_LAND}
        notes = []
        if header:
            value = line.value_text or ""
            meaning = self._key_meaning(line.code, value)
            missing = not value and line.code in mandatory
            if missing:
                notes.append(
                    self.env._(
                        "Mandatory, but missing: enter it in the settings of the "
                        "business or in the questionnaire."
                    )
                )
        else:
            value = export.amount_text(line.amount)
            meaning = ""
            missing = False
        control = line.field_type in CONTROL_TYPES
        if control:
            # explained once in the text above the tables
            notes.append(self.env._("Control value"))
        if line.code == scope_engine.FIELD_LAND and not self._land_confirmed():
            notes.append(
                self.env._(
                    "Not confirmed yet: confirm the questionnaire (C-45); an "
                    "answer proposed from the previous year is no answer."
                )
            )
        elif line.field_type == "transfer":
            notes.append(self.env._("Transferred value"))
        if line.source == "override":
            notes.append(
                self.env._(
                    "Overridden instead of %(computed)s: %(reason)s",
                    computed=export.amount_text(line.amount_computed),
                    reason=line.override_reason or "",
                )
            )
        prev = (
            export.amount_text(line.amount_prev_year)
            if not header and line.amount_prev_year
            else ""
        )
        label = line.official_label or line.name or line.code
        return {
            "code": line.code,
            "line": line.line or "",
            "kz": line.kz or "",
            "label": label,
            "name": line.name or "",
            "short_name": self._fill_aid_short_name(label, line.name)
            if label in ambiguous or line.form_type == "aveuer"
            else "",
            "value": value,
            "meaning": meaning,
            # a meaning that starts with the key ("2 Trade or Business")
            # is shown alone instead of "2 (2 Trade or Business)"
            "meaning_only": bool(meaning and meaning.startswith(f"{value} ")),
            "amount": None if header else _decimal(line.amount),
            "prev": prev,
            "numeric": not header,
            "negative": not header and line.amount < 0,
            "control": control,
            "missing": missing,
            "note": " ".join(notes),
        }

    @staticmethod
    def _fill_aid_short_name(label, name):
        """Short name of a field under its official label where the label
        alone is ambiguous: the column of a line (``Geschenke`` →
        ``Geschenke, abziehbar``) or the group of Anlage AVEÜR, whose
        labels repeat under each group heading (``Summe AfA`` →
        ``Büroausstattung: AfA``)."""
        if not name or not label or name.casefold() in label.casefold():
            return ""
        return name

    def _land_confirmed(self):
        """Question Q04 (Kz 15.120) is answered and confirmed."""
        land = self.sudo().scope_answer_ids.filtered(
            lambda answer: answer.question_code == scope_engine.LAND
        )
        return bool(land.answer and land.confirmed_on)

    def _get_filled_lines(self):
        """Lines of the fill-in aid: fields with a value and the mandatory
        header fields, in the order of the official forms (spec 7.5)."""
        statement = self.sudo()
        mandatory = set(MANDATORY_HEADER) | {scope_engine.FIELD_LAND}
        lines = statement.line_ids.sorted(lambda line: (line.sequence, line.id))
        return lines.filtered(
            lambda line: (
                line.value_text or line.code in mandatory
                if line.field_type in NON_AMOUNT_TYPES
                else line.has_value
            )
        )

    def _get_fill_aid_sections(self):
        """Fill-in aid per form: Anlage EÜR, AVEÜR, SZ (only filled forms)."""
        statement = self.sudo()
        filled = statement._get_filled_lines()
        labels = dict(FORM_TYPES)
        sections = []
        for form_type, _label in FORM_TYPES:
            lines = filled.filtered(lambda line, t=form_type: line.form_type == t)
            if not lines:
                continue
            form = statement.form_ids.filtered(
                lambda form, t=form_type: form.form_type == t
            )[:1]
            counts = Counter(form.field_ids.mapped("official_label"))
            ambiguous = frozenset(label for label, count in counts.items() if count > 1)
            sections.append(
                {
                    "form_type": form_type,
                    "title": labels[form_type],
                    "form": form.name or "",
                    "vordruck": form.vordruck_ids or "",
                    "rows": [
                        statement._fill_aid_row(line, ambiguous) for line in lines
                    ],
                }
            )
        return sections

    def _get_aveuer_detail_rows(self):
        """Voluntary details per asset of Anlage AVEÜR (spec 6.4)."""
        statement = self.sudo()
        groups = self._selection_labels("l10n_de_euer.asset", "aveuer_group")
        rows = []
        for detail in self.env["l10n_de_euer.asset"]._get_aveuer_details(
            statement.company_id, statement.year
        ):
            asset = detail["asset"]
            rows.append(
                {
                    "asset_id": asset.id,
                    "code": asset.code or "",
                    "name": asset.name,
                    "group": groups.get(detail["group"], detail["group"] or ""),
                    "delivery_date": asset.delivery_date,
                    "cost_start": _decimal(detail["cost_start"]),
                    "book_value_start": _decimal(detail["book_value_start"]),
                    "additions": _decimal(detail["additions"]),
                    "special_depreciation": _decimal(detail["special_depreciation"]),
                    "depreciation": _decimal(detail["depreciation"]),
                    "disposal_book_value": _decimal(detail["disposal_book_value"]),
                    "book_value_end": _decimal(detail["book_value_end"]),
                }
            )
        return rows

    def _get_fill_aid_data(self):
        """Everything the fill-in aid for Mein ELSTER shows (spec 7.5)."""
        statement = self._output_record()
        return {
            "status": statement._get_output_status(),
            "sections": statement._get_fill_aid_sections(),
            "assets": statement._get_aveuer_detail_rows(),
            # language of the short names (the official labels are German)
            "lang": (self.env.lang or "en_US").split("_")[0],
            "money": export.amount_text,
            "date": export.date_text,
        }

    # ------------------------------------------------------------ evidence
    def _get_year_slices(self):
        """Slices of the year with a form field, as in the statement."""
        return self.sudo().run_id.slice_ids.filtered(
            lambda one: one.field_code and one.attribution_year == self.year
        )

    def _slice_source(self, one, kinds, rules):
        if one.kind in ("non_cash", "overflow") and one.rule in rules:
            return rules[one.rule]
        return kinds.get(one.kind, one.kind or "")

    def _slice_share(self, one):
        """Share of the slice in its document line, or its part."""
        parts = {"net": self.env._("Net"), "tax": self.env._("VAT")}
        if one.virtual_part:
            return None, parts[one.virtual_part]
        line = one.origin_line_id.sudo()
        if not line or not line.balance:
            return None, ""
        share = abs(Decimal(str(one.balance)) / Decimal(str(line.balance)))
        return share, export.percent_text(share)

    def _get_slice_rows(self, slices):
        """Rows of the evidence (spec 7.5): cash date, year, document,
        partner, account, tax, share, amount and source of each slice."""
        kinds = self._selection_labels("l10n_de_euer.cash.slice", "kind")
        rules = self._selection_labels("l10n_de_euer.cash.slice", "rule")
        fields_by_code = self._fields_by_code()
        frozen = self._get_frozen_parts()
        references = frozen["references"] if frozen else None
        rows = []
        for one in slices.sudo().sorted(lambda one: (one.date, one.id)):
            field = fields_by_code.get(one.field_code)
            share, share_text = self._slice_share(one)
            account = self._reference(
                references, "account.account", one.account_id.id
            ) or {
                "code": one.account_id.with_company(self.company_id).code or "",
                "name": one.account_id.name or "",
            }
            partner = self._reference(references, "res.partner", one.partner_id.id)
            recipient = self._reference(
                references, "res.partner", one.gift_recipient_id.id
            )
            category = self._reference(
                references, "l10n_de_euer.category", one.category_id.id
            )
            document = (
                one.origin_move_name
                or one.origin_move_id.name
                or one.source_move_name
                or ""
            )
            rows.append(
                {
                    "id": one.id,
                    "field_code": one.field_code or "",
                    "line": field.line if field else "",
                    "kz": field.kz or "" if field else "",
                    "date": one.date,
                    "year": one.attribution_year,
                    "document": document,
                    # header of the row in the reports: the document, else
                    # the profit correction or the source (asset register)
                    "row_header": document
                    or one.adjustment_id.display_name
                    or self._slice_source(one, kinds, rules),
                    "document_id": one.origin_move_ref or 0,
                    "money": one.source_move_name or one.source_move_id.name or "",
                    "money_id": one.source_move_ref or 0,
                    "money_line_id": one.source_line_ref or 0,
                    "document_line_id": one.origin_line_ref or 0,
                    "partner": partner
                    if partner is not None
                    else one.partner_id.display_name or "",
                    "account": " ".join(
                        part for part in (account["code"], account["name"]) if part
                    ),
                    "account_code": account["code"] or "",
                    "account_name": account["name"] or "",
                    "tax": one.tax_repartition_line_id.tax_id.name or "",
                    "category": (category or {}).get("code")
                    or one.category_id.code
                    or "",
                    "share": share,
                    "share_text": share_text,
                    "amount": _decimal(one.amount),
                    "source": self._slice_source(one, kinds, rules),
                    "kind": one.kind,
                    "rule": one.rule or "",
                    "partials": " ".join(str(item) for item in one.path or ()),
                    "ten_day_applied": one.ten_day_applied,
                    "override": one.override,
                    "gift_recipient": recipient
                    if recipient is not None
                    else one.gift_recipient_id.display_name or "",
                    "gift_date": one.gift_date,
                    "gift_bulk": one.gift_bulk,
                    "adjustment": one.adjustment_id.display_name or "",
                    "adjustment_id": one.adjustment_id.id or 0,
                    "run_id": one.run_id.id,
                }
            )
        return rows

    def _get_evidence_sections(self):
        """Evidence per Kennzahl (spec 7.5): the slices of each field of the
        year with their sum and the value of the statement."""
        statement = self.sudo()
        slices = statement._get_year_slices()
        per_code = defaultdict(lambda: slices.browse())
        for one in slices:
            per_code[one.field_code] |= one
        fields_by_code = statement._fields_by_code()
        lines = {line.code: line for line in statement.line_ids}

        def order(code):
            field = fields_by_code.get(code)
            line = lines.get(code)
            return (line.sequence if line else 10**9, field.id if field else 0, code)

        sections = []
        for code in sorted(per_code, key=order):
            field = fields_by_code.get(code)
            line = lines.get(code)
            rows = statement._get_slice_rows(per_code[code])
            total = sum((row["amount"] for row in rows), Decimal("0.00"))
            value = _decimal(line.amount) if line else Decimal("0.00")
            computed = _decimal(line.amount_computed) if line else Decimal("0.00")
            sections.append(
                {
                    "code": code,
                    "line": field.line if field else "",
                    "kz": (field.kz or "") if field else "",
                    "label": (field.official_label or field.name) if field else code,
                    "rows": rows,
                    "total": total,
                    "value": value,
                    "computed": computed,
                    "override": bool(line and line.source == "override"),
                }
            )
        return sections

    def _get_evidence_data(self):
        statement = self._output_record()
        return {
            "status": statement._get_output_status(),
            "sections": statement._get_evidence_sections(),
            "money": export.amount_text,
            "date": export.date_text,
        }

    # ------------------------------------------------------------ registers
    def _get_asset_register_rows(self):
        """Register of assets of the year (G-08): every asset in the register
        with its values of the year."""
        statement = self.sudo()
        Asset = self.env["l10n_de_euer.asset"]
        kinds = self._selection_labels("l10n_de_euer.asset", "asset_kind")
        groups = self._selection_labels("l10n_de_euer.asset", "aveuer_group")
        methods = self._selection_labels("l10n_de_euer.asset", "method")
        states = self._selection_labels("l10n_de_euer.asset", "state")
        assets, entries, _pools = Asset._get_register_entries(
            statement.company_id, statement.year
        )
        rows = []
        for asset, entry in zip(assets, entries, strict=True):
            if asset.delivery_date.year > statement.year:
                continue
            try:
                values = dep.entry_values(entry, statement.year)
            except dep.DepreciationError:
                values = None
            if values is None and not (
                asset.delivery_date.year == statement.year
                or (asset.disposal_date and asset.disposal_date.year == statement.year)
            ):
                continue

            def value(name, values=values):
                return _decimal(getattr(values, name)) if values else None

            rows.append(
                {
                    "asset_id": asset.id,
                    "code": asset.code or "",
                    "name": asset.name,
                    "kind": kinds.get(asset.asset_kind, ""),
                    "group": groups.get(asset.aveuer_group, ""),
                    "delivery_date": asset.delivery_date,
                    "cost_net": _decimal(asset.cost_net),
                    "cost": _decimal(asset.acquisition_cost),
                    "method": methods.get(asset.method, ""),
                    "useful_life": asset.useful_life_months or 0,
                    "business_share": Decimal(str(asset.business_share or 0)),
                    "book_value_start": value("book_value_start"),
                    "additions": value("additions"),
                    "special_depreciation": value("special_depreciation"),
                    "depreciation": value("depreciation"),
                    "disposal_book_value": value("disposal_book_value"),
                    "book_value_end": value("book_value_end"),
                    "state": states.get(asset.state, ""),
                    "disposal_date": asset.disposal_date,
                }
            )
        return rows

    def _get_low_value_rows(self):
        """Register of low-value assets (G-08, § 6 (2) S. 4 EStG): net cost
        above 250 €, delivered in the year."""
        statement = self.sudo()
        assets = (
            self.env["l10n_de_euer.asset"]
            .sudo()
            .search(
                Domain("company_id", "=", statement.company_id.id)
                & Domain("in_gwg_register", "=", True)
                & Domain("state", "!=", "draft")
                & Domain("delivery_date", ">=", datetime.date(statement.year, 1, 1))
                & Domain("delivery_date", "<=", datetime.date(statement.year, 12, 31)),
                order="delivery_date, id",
            )
        )
        return [
            {
                "asset_id": asset.id,
                "code": asset.code or "",
                "name": asset.name,
                "delivery_date": asset.delivery_date,
                "cost_net": _decimal(asset.cost_net),
                "input_tax": _decimal(asset.input_tax),
                "cost": _decimal(asset.acquisition_cost),
                "documents": ", ".join(
                    sorted(set(asset.move_line_ids.move_id.mapped("name")))
                ),
            }
            for asset in assets
        ]

    def _get_pool_rows(self):
        """Asset pools with a value in the year (G-08, § 6 (2a) EStG)."""
        statement = self.sudo()
        pools = (
            self.env["l10n_de_euer.asset.pool"]
            .sudo()
            .search(
                Domain("company_id", "=", statement.company_id.id)
                & Domain("year", "<=", statement.year)
                & Domain("year", ">", statement.year - dep.POOL_YEARS),
                order="year, id",
            )
        )
        rows = []
        for pool in pools:
            values = dep.pool_year(pool._get_engine_pool(), statement.year)
            if values is None:
                continue
            start, release = values
            rows.append(
                {
                    "pool_id": pool.id,
                    "year": pool.year,
                    "amount": _decimal(pool._get_engine_pool().amount),
                    "start": _decimal(start),
                    "release": _decimal(release),
                    "end": _decimal(start - release),
                    "assets": ", ".join(pool.asset_ids.mapped("name")),
                }
            )
        return rows

    def _get_withdrawal_rows(self):
        """Withdrawals and contributions of the year (G-08, Kz 122/123)."""
        slices = self._get_year_slices().filtered(
            lambda one: one.field_code in (FIELD_WITHDRAWALS, FIELD_CONTRIBUTIONS)
        )
        rows = self._get_slice_rows(slices)
        for row in rows:
            row["direction"] = (
                self.env._("Withdrawal")
                if row["field_code"] == FIELD_WITHDRAWALS
                else self.env._("Contribution")
            )
        return rows

    def _get_separate_slices(self, record):
        return self._get_year_slices().filtered(
            lambda one: one.category_id.separate_record == record
            or (record == "gift" and one.rule == "gift")
        )

    def _get_gift_rows(self):
        """Gifts per recipient (G-08, § 4 (5) no. 1, (7) EStG); give-aways
        without recipient separately."""
        rows = self._get_slice_rows(self._get_separate_slices("gift"))
        totals = defaultdict(Decimal)
        for row in rows:
            if row["gift_recipient"]:
                key = (row["gift_recipient"], (row["gift_date"] or row["date"]).year)
                totals[key] += row["amount"]
        for row in rows:
            if row["gift_recipient"]:
                key = (row["gift_recipient"], (row["gift_date"] or row["date"]).year)
                row["recipient_total"] = totals[key]
            else:
                row["recipient_total"] = None
        rows.sort(
            key=lambda row: (
                row["gift_bulk"] or not row["gift_recipient"],
                row["gift_recipient"],
                row["gift_date"] or row["date"],
                row["id"],
            )
        )
        return rows

    def _get_entertainment_rows(self):
        """Business entertainment (G-08, § 4 (5) no. 2, (7) EStG)."""
        return self._get_slice_rows(self._get_separate_slices("entertainment"))

    def _get_check_rows(self):
        """All check results of the statement with their acknowledgement."""
        statement = self.sudo()
        severities = self._selection_labels("l10n_de_euer.check", "severity")
        origins = self._selection_labels("l10n_de_euer.check", "origin")
        order = {"blocker": 0, "warning": 1, "info": 2}
        checks = statement.check_ids.sorted(
            lambda check: (order.get(check.severity, 3), check.code, check.id)
        )
        rows = []
        for check in checks:
            if check.severity == "warning":
                status = (
                    self.env._("Acknowledged")
                    if check.acknowledged_on
                    else self.env._("Not acknowledged")
                )
            elif check.severity == "blocker":
                status = self.env._("Blocks the finalisation")
            else:
                status = self.env._("Note")
            rows.append(
                {
                    "id": check.id,
                    "code": check.code,
                    "severity": severities.get(check.severity, check.severity),
                    "severity_key": check.severity,
                    "origin": origins.get(check.origin, check.origin),
                    "message": check.message,
                    "amount": _decimal(check.amount) if check.amount else None,
                    "records": check.record_count,
                    "status": status,
                    "acknowledged_by": check.acknowledged_by_id.name or "",
                    "acknowledged_on": self._local_datetime(check.acknowledged_on),
                    "reason": check.acknowledge_reason or "",
                }
            )
        return rows

    def _get_check_data(self):
        statement = self._output_record()
        rows = statement._get_check_rows()
        counts = defaultdict(int)
        for row in rows:
            counts[row["severity_key"]] += 1
        return {
            "status": statement._get_output_status(),
            "rows": rows,
            "counts": dict(counts),
            "open_warnings": len(
                [
                    row
                    for row in rows
                    if row["severity_key"] == "warning" and not row["acknowledged_by"]
                ]
            ),
            "money": export.amount_text,
        }

    def _get_adjustment_rows(self):
        """Profit corrections with their own receipts (spec 5.8, G-15)."""
        statement = self.sudo()
        kinds = self._selection_labels("l10n_de_euer.adjustment", "kind")
        return [
            {
                "id": adjustment.id,
                "kind": kinds.get(adjustment.kind, adjustment.kind),
                "date": adjustment.date,
                "partner": adjustment.partner_id.display_name or "",
                "asset": adjustment.asset_id.display_name or "",
                "amount": _decimal(adjustment.result_amount),
                "fields": adjustment.field_text or "",
                "withdrawal": _decimal(adjustment.withdrawal_effect),
                "contribution": _decimal(adjustment.contribution_effect),
                "reason": adjustment.reason or "",
                "receipts": ", ".join(adjustment.attachment_ids.mapped("name")),
            }
            for adjustment in statement.adjustment_ids.sorted("id")
        ]

    def _get_assignment_rows(self, used_only=False):
        """Account → category → fields of the year (K-12, G-11).

        A finalised statement shows the assignment of its payload with the
        codes and names of the finalisation, so its outputs do not change
        with the master data. ``used_only``: only accounts with posted
        journal items in the year and assignments that deviate from the
        default (chart template, accounting setup); the full list is in the
        data access package.
        """
        statement = self.sudo()
        frozen = statement._get_frozen_parts()
        snapshot = frozen["assignment"] if frozen else None
        references = frozen["references"] if frozen else None
        if snapshot is None:
            snapshot = statement._get_assignment_snapshot()
        fields_by_code = statement._fields_by_code()
        fields_by_category = defaultdict(set)
        for row in snapshot["mappings"]:
            field = fields_by_code.get(row["field_code"])
            fields_by_category[row["category_id"]].add(
                f"Kz {field.kz}" if field and field.kz else row["field_code"] or ""
            )
        roles = self._selection_labels("l10n_de_euer.category", "role")
        Account = self.env["account.account"].sudo().with_company(statement.company_id)
        Category = self.env["l10n_de_euer.category"].sudo()
        used = statement._get_used_account_ids() if used_only else None
        rows = []
        for row in snapshot["accounts"]:
            if (
                used is not None
                and row["account_id"] not in used
                and row["source"] in ("template", "structure")
            ):
                continue
            account = self._reference(references, "account.account", row["account_id"])
            if account is None:
                record = Account.browse(row["account_id"]).exists()
                account = {"code": record.code or "", "name": record.name or ""}
            category = Category.browse(row["category_id"]).exists()
            names = self._reference(
                references, "l10n_de_euer.category", row["category_id"]
            ) or {"code": category.code or "", "name": category.name or ""}
            rows.append(
                {
                    "id": row["id"],
                    "account_code": account["code"] or "",
                    "account_name": account["name"] or "",
                    "category_code": names["code"] or "",
                    "category_name": names["name"] or "",
                    "role": roles.get(category.role, category.role or ""),
                    "fields": ", ".join(sorted(fields_by_category[row["category_id"]])),
                    "date_from": fields.Date.to_date(row["date_from"]),
                    "date_to": fields.Date.to_date(row["date_to"]),
                    "confirmed": bool(row["confirmed"]),
                }
            )
        rows.sort(
            key=lambda row: (
                row["account_code"],
                row["date_from"] or datetime.date.min,
                row["id"],
            )
        )
        return rows

    def _get_used_account_ids(self):
        """Accounts with posted journal items of the business in the year."""
        statement = self.sudo()
        companies = statement.company_id._l10n_de_euer_business_companies().sudo()
        groups = (
            self.env["account.move.line"]
            .sudo()
            ._read_group(
                Domain("company_id", "in", companies.ids)
                & Domain("parent_state", "=", "posted")
                & Domain("date", ">=", datetime.date(statement.year, 1, 1))
                & Domain("date", "<=", datetime.date(statement.year, 12, 31)),
                ["account_id"],
                ["__count"],
            )
        )
        return {account.id for account, _count in groups}

    # ------------------------------------------------------------ procedure
    def _get_settings_rows(self):
        """Settings and choices of the business (G-11)."""
        statement = self.sudo()
        business = statement.company_id
        root = business.root_id
        Company = self.env["res.company"]
        rows = []
        for name in (
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
            "fiscalyear_lock_date",
            "hard_lock_date",
            "tax_lock_date",
        ):
            if name not in Company._fields:
                continue
            field = Company._fields[name]
            source = business if name == "l10n_de_euer_separate_business" else root
            value = source[name]
            if field.type == "selection":
                value = dict(field._description_selection(self.env)).get(value, "")
            elif field.type == "boolean":
                value = self.env._("Yes") if value else self.env._("No")
            elif field.type == "date":
                value = export.date_text(value)
            rows.append((field._description_string(self.env), str(value or "")))
        return rows

    def _get_vat_period_rows(self):
        statement = self.sudo()
        regimes = self._selection_labels("l10n_de_euer.vat.period", "regime")
        periods = (
            self.env["l10n_de_euer.vat.period"]
            .sudo()
            .search(
                Domain("company_id", "=", statement.company_id.root_id.id),
                order="date_from",
            )
        )
        return [
            {
                "date_from": period.date_from,
                "date_to": period.date_to,
                "regime": regimes.get(period.regime, ""),
                "prev_year_turnover": _decimal(period.prev_year_turnover)
                if period.prev_year_turnover or period.prev_year_turnover_year
                else None,
                "prev_year_turnover_year": period.prev_year_turnover_year or None,
                "reason": period.reason or "",
            }
            for period in periods
        ]

    def _get_journal_rows(self):
        """Money journals of the business and whether they are private."""
        statement = self.sudo()
        companies = statement.company_id._l10n_de_euer_business_companies().sudo()
        journals = (
            self.env["account.journal"]
            .sudo()
            .search(
                Domain("company_id", "in", companies.ids)
                & Domain("type", "in", ["bank", "cash", "credit", "purchase"]),
                order="type, code",
            )
        )
        types = self._selection_labels("account.journal", "type")
        return [
            {
                "code": journal.code,
                "name": journal.name,
                "type": types.get(journal.type, journal.type),
                "account": self._account_text(journal.default_account_id),
                "private": journal.l10n_de_euer_private_account,
                "private_share": self._account_text(journal.non_deductible_account_id)
                if "non_deductible_account_id" in journal._fields
                else "",
            }
            for journal in journals
        ]

    def _get_role_rows(self):
        """Rights per accounting group (spec 7.10) and their users with
        access to the business."""
        business = self.company_id.sudo()
        rights = [
            [
                "account.group_account_readonly",
                self.env._("Read all EÜR data, reports and exports (also auditors)."),
            ],
            [
                "account.group_account_user",
                self.env._(
                    "Compute, profit corrections, assets, review lists, "
                    "acknowledgements, overrides, classification of rests, bank "
                    "reconciliation."
                ),
            ],
            [
                "account.group_account_manager",
                self.env._(
                    "Finalise, correct, confirm the questionnaire, release open "
                    "items taken over, account assignment, VAT status periods, "
                    "settings of the business."
                ),
            ],
            [
                "base.group_system",
                self.env._(
                    "Change form years, categories and rule parameters; release a "
                    "form year with the review of the release role."
                ),
            ],
        ]
        rows = []
        for xml_id, text in rights:
            group = self.env.ref(xml_id, raise_if_not_found=False)
            if not group:
                continue
            users = (
                self.env["res.users"]
                .sudo()
                .search(
                    Domain("all_group_ids", "in", group.ids)
                    & Domain("company_ids", "in", business.ids)
                    & Domain("share", "=", False),
                    order="name",
                )
            )
            rows.append(
                {
                    "group": group.sudo().full_name or group.sudo().name,
                    "rights": text,
                    "users": ", ".join(users.mapped("name")),
                }
            )
        return rows

    def _get_parameter_rows(self):
        """Rule parameters valid in the year (the rule set, G-11)."""
        statement = self.sudo()
        start = datetime.date(statement.year, 1, 1)
        end = datetime.date(statement.year, 12, 31)
        parameters = (
            self.env["l10n_de_euer.parameter"]
            .sudo()
            .search(
                (Domain("date_from", "=", False) | Domain("date_from", "<=", end))
                & (Domain("date_to", "=", False) | Domain("date_to", ">=", start)),
                order="code, date_from",
            )
        )
        return [
            {
                "code": parameter.code,
                "name": parameter.name,
                "value": self._parameter_text(parameter),
                "date_from": parameter.date_from,
                "date_to": parameter.date_to,
                "source": parameter.source or "",
            }
            for parameter in parameters
        ]

    def _number_text(self, value):
        """Number in the language of the user: up to four places, no
        trailing zeros, thousands separated (e.g. 0,38 and 100.000 in
        German, 0.38 and 100,000 in English)."""
        lang = self.env["res.lang"]._get_data(code=self.env.lang or "en_US")
        text = float_repr(float(value), 4)
        sign = "-" if text.startswith("-") else ""
        whole, _dot, fraction = text.lstrip("-").partition(".")
        fraction = fraction.rstrip("0")
        groups = []
        while len(whole) > 3:
            groups.insert(0, whole[-3:])
            whole = whole[:-3]
        groups.insert(0, whole)
        text = sign + (lang.thousands_sep or "").join(groups)
        if fraction:
            text += (lang.decimal_point or ".") + fraction
        return text

    def _parameter_text(self, parameter):
        """Value of a rule parameter for reading (G-11)."""

        def text(value):
            if isinstance(value, bool) or value is None:
                return str(value or "")
            if isinstance(value, int | float):
                return self._number_text(value)
            if isinstance(value, list):
                return "; ".join(text(item) for item in value)
            if isinstance(value, dict):
                return "; ".join(f"{key}: {text(item)}" for key, item in value.items())
            return str(value)

        if parameter._has_json_value():
            return text(parameter.value_json)
        return self._number_text(parameter.value or 0)

    def _get_form_rows(self):
        statement = self.sudo()
        states = self._selection_labels("l10n_de_euer.form", "state")
        roles = self._selection_labels("l10n_de_euer.form", "verified_role")
        return [
            {
                "name": form.name,
                "state": states.get(form.state, form.state),
                "verified": ", ".join(
                    part
                    for part in (
                        form.verified_by or "",
                        roles.get(form.verified_role, "") if form.verified_role else "",
                        export.date_text(form.verified_on),
                    )
                    if part
                ),
                "bmf": " ".join(
                    part
                    for part in (
                        export.date_text(form.bmf_date),
                        form.bmf_reference or "",
                    )
                    if part
                ),
                "vordruck": form.vordruck_ids or "",
                "sha256": form.source_sha256 or "",
            }
            for form in statement.form_ids.sorted(
                lambda form: (form.form_type, form.id)
            )
        ]

    def _get_procedure_data(self):
        """Template of the procedure documentation (G-11): data flow, rules,
        assignment, settings, roles, lock concept and versions."""
        statement = self._output_record()
        business = statement.company_id
        return {
            "status": statement._get_output_status(),
            "business": business.name,
            "companies": ", ".join(
                business._l10n_de_euer_business_companies().mapped("name")
            ),
            "settings": statement._get_settings_rows(),
            "vat_periods": statement._get_vat_period_rows(),
            "journals": statement._get_journal_rows(),
            "assignment": statement._get_assignment_rows(used_only=True),
            "parameters": statement._get_parameter_rows(),
            "roles": statement._get_role_rows(),
            "forms": statement._get_form_rows(),
            "money": export.amount_text,
            "date": export.date_text,
        }

    # ------------------------------------------------------------ actions
    def _report_action(self, xml_id, config=True):
        """Action of a report; the HTML views need no document layout of the
        company (``config``)."""
        self.ensure_one()
        self._output_record()
        return self.env.ref(xml_id).report_action(self, config=config)

    def action_view_fill_aid(self):
        """Fill-in aid for Mein ELSTER as accessible HTML (spec 7.5)."""
        return self._report_action(
            "l10n_de_euer.action_report_fill_aid_html", config=False
        )

    def action_print_fill_aid(self):
        return self._report_action("l10n_de_euer.action_report_fill_aid")

    def action_print_evidence(self):
        return self._report_action("l10n_de_euer.action_report_evidence")

    def action_print_checks(self):
        return self._report_action("l10n_de_euer.action_report_check_protocol")

    def action_view_procedure(self):
        return self._report_action(
            "l10n_de_euer.action_report_procedure_html", config=False
        )

    def action_print_procedure(self):
        return self._report_action("l10n_de_euer.action_report_procedure")

    def _export_action(self, kind):
        self.ensure_one()
        self._output_record()
        return {
            "type": "ir.actions.act_url",
            "url": f"/l10n_de_euer/statement/{self.id}/{kind}",
            "target": "download",
        }

    def action_export_workbook(self):
        return self._export_action("workbook")

    def action_export_evidence(self):
        return self._export_action("evidence")

    def action_export_checks(self):
        return self._export_action("checks")

    def action_export_elster(self):
        return self._export_action("elster")

    def action_export_json(self):
        return self._export_action("json")

    def action_export_z3(self):
        return self._export_action("z3")

    # ------------------------------------------------------------ downloads
    def _export_file_name(self, kind, extension):
        names = {
            "workbook": "euer",
            "evidence": "euer_evidence",
            "checks": "euer_checks",
            "elster": "euer_elster",
            "json": "euer",
            "z3": "euer_z3",
        }
        company = "".join(
            char if char.isalnum() else "_" for char in self.company_id.name or ""
        ).strip("_")
        return f"{names[kind]}_{self.year}_{company}_v{self.version}.{extension}"

    def _get_export(self, kind):
        """``(file name, mimetype, content)`` of a download (spec 7.5)."""
        self.ensure_one()
        if kind not in EXPORTS:
            raise UserError(self.env._("Unknown export %(kind)s.", kind=kind))
        method, mimetype, extension = EXPORTS[kind]
        statement = self._output_record()
        content = getattr(statement, method)()
        return self._export_file_name(kind, extension), mimetype, content

    # ------------------------------------------------------------ XLSX
    @api.model
    def _sheet_name(self, name, used):
        name = (name or "").translate(_SHEET_FORBIDDEN).strip()[:31] or "Sheet"
        base, index = name, 2
        while name.lower() in used:
            suffix = f" {index}"
            name = base[: 31 - len(suffix)] + suffix
            index += 1
        used.add(name.lower())
        return name

    def _xlsx_formats(self, workbook):
        return {
            "header": workbook.add_format({"bold": True, "text_wrap": True}),
            "amount": workbook.add_format({"num_format": "#,##0.00;-#,##0.00"}),
            "percent": workbook.add_format({"num_format": "0.00%"}),
            "date": workbook.add_format({"num_format": "dd.mm.yyyy"}),
            "text": workbook.add_format({"text_wrap": False}),
        }

    def _xlsx_sheet(self, workbook, formats, used, title, columns, rows):
        """One sheet with a header line; ``columns``: ``(label, kind, key)``
        with kind ``text``, ``amount``, ``int``, ``percent``, ``date``."""
        sheet = workbook.add_worksheet(self._sheet_name(title, used))
        for index, (label, _kind, _key) in enumerate(columns):
            sheet.write_string(0, index, label, formats["header"])
            sheet.set_column(index, index, max(10, min(60, len(label) + 4)))
        for row_index, row in enumerate(rows, start=1):
            for index, (_label, kind, key) in enumerate(columns):
                value = row[key] if isinstance(row, dict) else row[index]
                if value is None or value is False or value == "":
                    continue
                if kind == "amount":
                    sheet.write_number(
                        row_index, index, float(value), formats["amount"]
                    )
                elif kind == "percent":
                    sheet.write_number(
                        row_index, index, float(value), formats["percent"]
                    )
                elif kind == "int":
                    sheet.write_number(row_index, index, int(value))
                elif kind == "date":
                    sheet.write_datetime(
                        row_index,
                        index,
                        datetime.datetime.combine(value, datetime.time()),
                        formats["date"],
                    )
                else:
                    sheet.write_string(row_index, index, str(value))
        sheet.freeze_panes(1, 0)
        return sheet

    def _xlsx(self, sheets):
        buffer = io.BytesIO()
        workbook = xlsxwriter.Workbook(buffer, {"in_memory": True})
        formats = self._xlsx_formats(workbook)
        used = set()
        for title, columns, rows in sheets:
            self._xlsx_sheet(workbook, formats, used, title, columns, rows)
        workbook.close()
        return buffer.getvalue()

    def _form_line_rows(self, form_types):
        sources = self._selection_labels("l10n_de_euer.statement.line", "source")
        rows = []
        for line in (
            self.sudo()
            ._get_filled_lines()
            .filtered(lambda line: line.form_type in form_types)
        ):
            row = self._fill_aid_row(line)
            row["source"] = sources.get(line.source, line.source)
            row["prev_amount"] = (
                _decimal(line.amount_prev_year)
                if row["numeric"] and line.amount_prev_year
                else None
            )
            row["text"] = "" if row["numeric"] else row["value"]
            rows.append(row)
        return rows

    def _form_columns(self):
        return [
            (self.env._("Line"), "text", "line"),
            (self.env._("Kz"), "text", "kz"),
            (self.env._("Code"), "text", "code"),
            (self.env._("Official Label"), "text", "label"),
            (self.env._("Amount"), "amount", "amount"),
            (self.env._("Value"), "text", "text"),
            (self.env._("Previous Year"), "amount", "prev_amount"),
            (self.env._("Source"), "text", "source"),
            (self.env._("Note"), "text", "note"),
        ]

    def _evidence_columns(self):
        return [
            (self.env._("Field"), "text", "field_code"),
            (self.env._("Line"), "text", "line"),
            (self.env._("Kz"), "text", "kz"),
            (self.env._("Cash Date"), "date", "date"),
            (self.env._("Year"), "int", "year"),
            (self.env._("Document"), "text", "document"),
            (self.env._("Money Entry"), "text", "money"),
            (self.env._("Partner"), "text", "partner"),
            (self.env._("Account"), "text", "account"),
            (self.env._("Tax"), "text", "tax"),
            (self.env._("Share"), "percent", "share"),
            (self.env._("Amount"), "amount", "amount"),
            (self.env._("Source"), "text", "source"),
        ]

    def _evidence_rows(self):
        return [
            row for section in self._get_evidence_sections() for row in section["rows"]
        ]

    def _check_columns(self):
        return [
            (self.env._("Code"), "text", "code"),
            (self.env._("Severity"), "text", "severity"),
            (self.env._("Status"), "text", "status"),
            (self.env._("Origin"), "text", "origin"),
            (self.env._("Message"), "text", "message"),
            (self.env._("Amount"), "amount", "amount"),
            (self.env._("Records"), "int", "records"),
            (self.env._("Acknowledged By"), "text", "acknowledged_by"),
            (self.env._("Acknowledged On"), "text", "acknowledged_on"),
            (self.env._("Reason"), "text", "reason"),
        ]

    def _register_sheets(self):
        """Sheets of the registers of the year (G-08)."""
        asset_columns = [
            (self.env._("Inventory Number"), "text", "code"),
            (self.env._("Asset"), "text", "name"),
            (self.env._("Kind"), "text", "kind"),
            (self.env._("AVEÜR Group"), "text", "group"),
            (self.env._("Delivery Date"), "date", "delivery_date"),
            (self.env._("Net Cost"), "amount", "cost_net"),
            (self.env._("Cost (Basis)"), "amount", "cost"),
            (self.env._("Method"), "text", "method"),
            (self.env._("Useful Life (Months)"), "int", "useful_life"),
            (self.env._("Business Share"), "percent", "business_share"),
            (self.env._("Book Value at Start"), "amount", "book_value_start"),
            (self.env._("Additions"), "amount", "additions"),
            (self.env._("Special Depreciation"), "amount", "special_depreciation"),
            (self.env._("Depreciation"), "amount", "depreciation"),
            (self.env._("Disposals"), "amount", "disposal_book_value"),
            (self.env._("Book Value at End"), "amount", "book_value_end"),
            (self.env._("State"), "text", "state"),
            (self.env._("Disposal Date"), "date", "disposal_date"),
        ]
        low_value_columns = [
            (self.env._("Inventory Number"), "text", "code"),
            (self.env._("Asset"), "text", "name"),
            (self.env._("Delivery Date"), "date", "delivery_date"),
            (self.env._("Net Cost"), "amount", "cost_net"),
            (self.env._("Input Tax"), "amount", "input_tax"),
            (self.env._("Cost (Basis)"), "amount", "cost"),
            (self.env._("Documents"), "text", "documents"),
        ]
        pool_columns = [
            (self.env._("Year of Formation"), "int", "year"),
            (self.env._("Amount"), "amount", "amount"),
            (self.env._("Book Value at Start"), "amount", "start"),
            (self.env._("Release of the Year"), "amount", "release"),
            (self.env._("Book Value at End"), "amount", "end"),
            (self.env._("Assets"), "text", "assets"),
        ]
        withdrawal_columns = [
            (self.env._("Kind"), "text", "direction"),
            (self.env._("Date"), "date", "date"),
            (self.env._("Kz"), "text", "kz"),
            (self.env._("Document"), "text", "document"),
            (self.env._("Partner"), "text", "partner"),
            (self.env._("Account"), "text", "account"),
            (self.env._("Amount"), "amount", "amount"),
            (self.env._("Source"), "text", "source"),
        ]
        gift_columns = [
            (self.env._("Recipient"), "text", "gift_recipient"),
            (self.env._("Date of the Gift"), "date", "gift_date"),
            (self.env._("Give-Away"), "text", "bulk_text"),
            (self.env._("Kz"), "text", "kz"),
            (self.env._("Document"), "text", "document"),
            (self.env._("Supplier"), "text", "partner"),
            (self.env._("Account"), "text", "account"),
            (self.env._("Amount"), "amount", "amount"),
            (self.env._("Total of the Recipient"), "amount", "recipient_total"),
        ]
        entertainment_columns = [
            (self.env._("Date"), "date", "date"),
            (self.env._("Kz"), "text", "kz"),
            (self.env._("Document"), "text", "document"),
            (self.env._("Partner"), "text", "partner"),
            (self.env._("Account"), "text", "account"),
            (self.env._("Amount"), "amount", "amount"),
        ]
        gifts = self._get_gift_rows()
        for row in gifts:
            row["bulk_text"] = self.env._("Yes") if row["gift_bulk"] else ""
        return [
            (
                self.env._("Asset Register"),
                asset_columns,
                self._get_asset_register_rows(),
            ),
            (
                self.env._("Register of Low-Value Assets"),
                low_value_columns,
                self._get_low_value_rows(),
            ),
            (self.env._("Asset Pools"), pool_columns, self._get_pool_rows()),
            (
                self.env._("Withdrawals and Contributions"),
                withdrawal_columns,
                self._get_withdrawal_rows(),
            ),
            (self.env._("Gifts per Recipient"), gift_columns, gifts),
            (
                self.env._("Business Entertainment"),
                entertainment_columns,
                self._get_entertainment_rows(),
            ),
        ]

    def _adjustment_columns(self):
        return [
            (self.env._("Type"), "text", "kind"),
            (self.env._("Date"), "date", "date"),
            (self.env._("Partner"), "text", "partner"),
            (self.env._("Asset"), "text", "asset"),
            (self.env._("Amount"), "amount", "amount"),
            (self.env._("Form Fields"), "text", "fields"),
            (self.env._("Withdrawal"), "amount", "withdrawal"),
            (self.env._("Contribution"), "amount", "contribution"),
            (self.env._("Reason"), "text", "reason"),
            (self.env._("Own Receipt"), "text", "receipts"),
        ]

    def _export_workbook(self):
        """Workbook of the statement (spec 7.5)."""
        sheets = [
            (
                self.env._("Statement"),
                [
                    (self.env._("Description"), "text", 0),
                    (self.env._("Value"), "text", 1),
                ],
                self._get_status_rows(),
            ),
            (
                self.env._("EÜR"),
                self._form_columns(),
                self._form_line_rows(("euer", "sz")),
            ),
            (
                self.env._("AVEÜR"),
                self._form_columns(),
                self._form_line_rows(("aveuer",)),
            ),
            (self.env._("Evidence"), self._evidence_columns(), self._evidence_rows()),
            *self._register_sheets(),
            (
                self.env._("Check Protocol"),
                self._check_columns(),
                self._get_check_rows(),
            ),
            (
                self.env._("Profit Corrections"),
                self._adjustment_columns(),
                self._get_adjustment_rows(),
            ),
        ]
        return self._xlsx(sheets)

    def _export_evidence_xlsx(self):
        return self._xlsx(
            [
                (
                    self.env._("Evidence"),
                    self._evidence_columns(),
                    self._evidence_rows(),
                )
            ]
        )

    def _export_checks_xlsx(self):
        return self._xlsx(
            [
                (
                    self.env._("Check Protocol"),
                    self._check_columns(),
                    self._get_check_rows(),
                )
            ]
        )

    # ------------------------------------------------------------ CSV
    def _get_elster_rows(self):
        """Kennzahlen of the filled fields (spec 7.5, F-10)."""
        statement = self.sudo()
        forms = dict(FORM_TYPES)
        sources = self._selection_labels("l10n_de_euer.statement.line", "source")
        rows = []
        for line in statement._get_filled_lines():
            field = line.field_id
            header = line.field_type in NON_AMOUNT_TYPES
            if header and not line.value_text:
                continue
            rows.append(
                {
                    "year": statement.year,
                    "form": forms.get(line.form_type, ""),
                    "form_version": field.form_id.version,
                    "box": line.box or "",
                    "line": line.line or "",
                    "kz": line.kz or "",
                    "code": line.code,
                    "label": line.official_label or "",
                    "value": line.value_text
                    if header
                    else export.number_text(line.amount),
                    "amount": None if header else _decimal(line.amount),
                    "elster_id": field.elster_field_id or "",
                    "xpath": field.elster_xpath or "",
                    "verified": field.elster_verified,
                    "source": sources.get(line.source, line.source),
                    "control": line.field_type in CONTROL_TYPES,
                }
            )
        return rows

    def _export_elster_csv(self):
        """List of the Kennzahlen for Mein ELSTER (UTF-8, semicolon)."""
        header = [
            self.env._("Year"),
            self.env._("Form"),
            self.env._("Form Version"),
            self.env._("Box"),
            self.env._("Line"),
            self.env._("Kz"),
            self.env._("Code"),
            self.env._("Official Label"),
            self.env._("Amount"),
            self.env._("ELSTER Field ID"),
            self.env._("XPath"),
            self.env._("Verified"),
            self.env._("Source"),
        ]
        rows = [
            [
                row["year"],
                row["form"],
                row["form_version"],
                row["box"],
                row["line"],
                row["kz"],
                row["code"],
                row["label"],
                row["value"],
                row["elster_id"],
                row["xpath"],
                self.env._("Yes") if row["verified"] else self.env._("No"),
                row["source"],
            ]
            for row in self._get_elster_rows()
        ]
        return export.csv_bytes(header, rows, bom=True)

    # ------------------------------------------------------------ JSON
    def _dataset_field(self, line):
        field = line.field_id
        header = line.field_type in NON_AMOUNT_TYPES
        sources = {
            "computed": "berechnet",
            "manual": "manuell",
            "override": "uebersteuert",
        }
        return {
            "code": line.code,
            "formular": line.form_type,
            "abschnitt": line.box or "",
            "zeile": line.line or None,
            "kz": line.kz or None,
            "bezeichnung": line.official_label or "",
            "betrag": None if header else export.decimal_string(line.amount),
            "wert": line.value_text if header else None,
            "vorjahr": None
            if header or not line.amount_prev_year
            else export.decimal_string(line.amount_prev_year),
            "quelle": sources.get(line.source, line.source),
            "kontrollwert": line.field_type in CONTROL_TYPES,
            "elster_kennung": field.elster_field_id or None,
            "xpath": field.elster_xpath or None,
            "verifiziert": bool(field.elster_verified),
        }

    def _export_json(self):
        """Machine readable data set ``l10n_de_euer/1`` (spec 7.5)."""
        statement = self.sudo()
        filled = statement._get_filled_lines()
        business = statement.company_id
        root = business.root_id
        status = statement._get_output_status()
        details = [
            {
                "inventarnummer": row["code"] or None,
                "bezeichnung": row["name"],
                "gruppe": row["group"],
                "lieferdatum": row["delivery_date"].isoformat()
                if row["delivery_date"]
                else None,
                "ak_beginn": export.decimal_string(row["cost_start"]),
                "buchwert_beginn": export.decimal_string(row["book_value_start"]),
                "zugaenge": export.decimal_string(row["additions"]),
                "sonder_afa": export.decimal_string(row["special_depreciation"]),
                "afa": export.decimal_string(row["depreciation"]),
                "abgaenge": export.decimal_string(row["disposal_book_value"]),
                "buchwert_ende": export.decimal_string(row["book_value_end"]),
            }
            for row in statement._get_aveuer_detail_rows()
        ]
        return export.dataset_json(
            year=statement.year,
            form_versions=[
                {
                    "formular": form.form_type,
                    "jahr": form.year,
                    "version": form.version,
                    "freigegeben": form.state == "verified",
                }
                for form in statement.form_ids.sorted(
                    lambda form: (form.form_type, form.id)
                )
            ],
            business={
                "name": business.name,
                "steuernummer": business.l10n_de_stnr or root.l10n_de_stnr or None,
                "w_idnr": business.l10n_de_widnr or root.l10n_de_widnr or None,
                "art": root.l10n_de_euer_business_type or None,
                "rechtsform": root.l10n_de_euer_legal_form or None,
                "einkunftsart": root.l10n_de_euer_income_type or None,
                "inhaber": root.l10n_de_euer_owner or None,
            },
            fields=[
                statement._dataset_field(line)
                for line in filled.filtered(lambda line: line.form_type != "aveuer")
            ],
            aveuer={
                "gruppen": [
                    statement._dataset_field(line)
                    for line in filled.filtered(lambda line: line.form_type == "aveuer")
                ],
                "einzelangaben": details,
            },
            status={
                "zustand": statement.state,
                "version": statement.version,
                "vorlaeufig": statement.provisional_form,
                "festgeschrieben_am": fields.Datetime.to_string(statement.finalized_on)
                if statement.finalized_on
                else None,
                "kettenhash": statement.chain_hash or None,
                "modulversion": status["module_version"],
                "regelwerksversion": status["ruleset_version"],
            },
            hash_value=statement.payload_sha256 or None,
            extra={
                "kontrollwerte": {
                    "gewinn": export.decimal_string(statement.profit),
                    "betriebseinnahmen": export.decimal_string(statement.total_income),
                    "betriebsausgaben": export.decimal_string(statement.total_expense),
                }
            },
        )

    # ------------------------------------------------------------ Z3
    def _z3_table(self, file, name, description, columns, rows):
        """Table of the data access package from rows as dicts; every column
        names the key of its value (``Column.key``)."""
        return export.Table(
            file,
            name,
            description,
            tuple(columns),
            [[row[column.key] for column in columns] for row in rows],
        )

    def _z3_field_rows(self):
        rows = []
        for line in self.sudo().line_ids.sorted(lambda line: (line.sequence, line.id)):
            header = line.field_type in NON_AMOUNT_TYPES
            rows.append(
                {
                    "code": line.code,
                    "form": line.form_type,
                    "box": line.box or "",
                    "line": line.line or None,
                    "kz": line.kz or "",
                    "label": line.official_label or "",
                    "amount": None if header else _decimal(line.amount),
                    "value": line.value_text or "",
                    "prev": None if header else _decimal(line.amount_prev_year),
                    "source": line.source,
                    "reason": line.override_reason or "",
                }
            )
        return rows

    def _z3_tables(self):
        """Tables of the data access package (G-09): slices, field values,
        account assignment and the registers of the year."""
        Column = export.Column

        def text(name, key, description):
            return Column(name, "text", description, key=key)

        def number(name, key, description, places=2):
            return Column(name, "numeric", description, places, key=key)

        def day(name, key, description):
            return Column(name, "date", description, key=key)

        slices = self._get_slice_rows(self.sudo().run_id.slice_ids)
        slice_columns = [
            number("slice_id", "id", self.env._("Id of the slice"), 0),
            number("run_id", "run_id", self.env._("Id of the calculation run"), 0),
            day("cash_date", "date", self.env._("Date of receipt or payment")),
            number("attribution_year", "year", self.env._("Year of attribution"), 0),
            text("kind", "kind", self.env._("Kind of slice")),
            text("rule", "rule", self.env._("Rule of the form field")),
            text("field_code", "field_code", self.env._("Form field (FORM.BOX.KZ)")),
            text("kz", "kz", self.env._("Kennzahl")),
            number("amount", "amount", self.env._("Amount in the form field")),
            number(
                "money_entry_id", "money_id", self.env._("Id of the money entry"), 0
            ),
            text("money_entry", "money", self.env._("Money entry")),
            number(
                "money_line_id", "money_line_id", self.env._("Id of the money line"), 0
            ),
            number("document_id", "document_id", self.env._("Id of the document"), 0),
            text("document", "document", self.env._("Document")),
            number(
                "document_line_id",
                "document_line_id",
                self.env._("Id of the document line"),
                0,
            ),
            text("account_code", "account_code", self.env._("Account code")),
            text("account_name", "account_name", self.env._("Account name")),
            text("partner", "partner", self.env._("Partner")),
            text("tax", "tax", self.env._("Tax")),
            text("category", "category", self.env._("EÜR category")),
            text("partials", "partials", self.env._("Partial reconciliations passed")),
            text(
                "ten_day_rule",
                "ten_day_applied",
                self.env._("Ten-day rule applied (1/0)"),
            ),
            text(
                "gift_recipient", "gift_recipient", self.env._("Recipient of the gift")
            ),
            number(
                "adjustment_id",
                "adjustment_id",
                self.env._("Id of the profit correction"),
                0,
            ),
        ]
        field_columns = [
            text("code", "code", self.env._("Form field (FORM.BOX.KZ)")),
            text("form", "form", self.env._("Form")),
            text("box", "box", self.env._("Box")),
            number("line", "line", self.env._("Line of the form of the year"), 0),
            text("kz", "kz", self.env._("Kennzahl")),
            text("official_label", "label", self.env._("Official label")),
            number("amount", "amount", self.env._("Amount")),
            text("value", "value", self.env._("Value of a header field")),
            number("previous_year", "prev", self.env._("Amount of the previous year")),
            text("source", "source", self.env._("Source of the value")),
            text("override_reason", "reason", self.env._("Reason of an override")),
        ]
        assignment_columns = [
            number("assignment_id", "id", self.env._("Id of the assignment"), 0),
            text("account_code", "account_code", self.env._("Account code")),
            text("account_name", "account_name", self.env._("Account name")),
            text("category_code", "category_code", self.env._("EÜR category")),
            text("category_name", "category_name", self.env._("Name of the category")),
            text("role", "role", self.env._("Role of the category")),
            text("fields", "fields", self.env._("Form fields of the year")),
            day("valid_from", "date_from", self.env._("Valid from")),
            day("valid_to", "date_to", self.env._("Valid to")),
            text("confirmed", "confirmed", self.env._("Confirmed (1/0)")),
        ]
        asset_columns = [
            number("asset_id", "asset_id", self.env._("Id of the asset"), 0),
            text("inventory_number", "code", self.env._("Inventory number")),
            text("name", "name", self.env._("Asset")),
            text("kind", "kind", self.env._("Kind")),
            text("group", "group", self.env._("AVEÜR group")),
            day("delivery_date", "delivery_date", self.env._("Delivery date")),
            number("cost", "cost", self.env._("Cost (basis)")),
            text("method", "method", self.env._("Method")),
            number(
                "useful_life", "useful_life", self.env._("Useful life in months"), 0
            ),
            number(
                "book_value_start",
                "book_value_start",
                self.env._("Book value at start"),
            ),
            number("additions", "additions", self.env._("Additions")),
            number(
                "special_depreciation",
                "special_depreciation",
                self.env._("Special depreciation"),
            ),
            number("depreciation", "depreciation", self.env._("Depreciation")),
            number("disposals", "disposal_book_value", self.env._("Disposals")),
            number("book_value_end", "book_value_end", self.env._("Book value at end")),
        ]
        low_value_columns = [
            number("asset_id", "asset_id", self.env._("Id of the asset"), 0),
            text("inventory_number", "code", self.env._("Inventory number")),
            text("name", "name", self.env._("Asset")),
            day("delivery_date", "delivery_date", self.env._("Delivery date")),
            number("net_cost", "cost_net", self.env._("Net cost")),
            number("input_tax", "input_tax", self.env._("Input tax")),
            text("documents", "documents", self.env._("Documents")),
        ]
        pool_columns = [
            number("pool_id", "pool_id", self.env._("Id of the asset pool"), 0),
            number("year_of_formation", "year", self.env._("Year of formation"), 0),
            number("amount", "amount", self.env._("Amount")),
            number("book_value_start", "start", self.env._("Book value at start")),
            number("release", "release", self.env._("Release of the year")),
            number("book_value_end", "end", self.env._("Book value at end")),
        ]
        correction_columns = [
            number("correction_id", "id", self.env._("Id of the profit correction"), 0),
            text("type", "kind", self.env._("Type")),
            day("date", "date", self.env._("Date")),
            number("amount", "amount", self.env._("Amount")),
            text("fields", "fields", self.env._("Form fields")),
            number("withdrawal", "withdrawal", self.env._("Withdrawal")),
            number("contribution", "contribution", self.env._("Contribution")),
            text("reason", "reason", self.env._("Reason")),
            text("own_receipt", "receipts", self.env._("Own receipt")),
        ]
        check_columns = [
            number("check_id", "id", self.env._("Id of the check result"), 0),
            text("code", "code", self.env._("Check code")),
            text("severity", "severity", self.env._("Severity")),
            text("message", "message", self.env._("Message")),
            number("amount", "amount", self.env._("Amount")),
            text("acknowledged_by", "acknowledged_by", self.env._("Acknowledged by")),
            text("acknowledged_on", "acknowledged_on", self.env._("Acknowledged on")),
            text("reason", "reason", self.env._("Reason")),
        ]
        register_columns = [
            number("slice_id", "id", self.env._("Id of the slice"), 0),
            day("date", "date", self.env._("Date of receipt or payment")),
            text("field_code", "field_code", self.env._("Form field (FORM.BOX.KZ)")),
            text("kz", "kz", self.env._("Kennzahl")),
            text("document", "document", self.env._("Document")),
            text("partner", "partner", self.env._("Partner")),
            text("account_code", "account_code", self.env._("Account code")),
            number("amount", "amount", self.env._("Amount")),
            text(
                "gift_recipient", "gift_recipient", self.env._("Recipient of the gift")
            ),
            day("gift_date", "gift_date", self.env._("Date of the gift")),
        ]
        table = self._z3_table
        return [
            table(
                "slices.csv",
                self.env._("Slices"),
                self.env._(
                    "Realised amounts of the journal items at their cash date "
                    "(§ 11 EStG) with the form field they reach."
                ),
                slice_columns,
                slices,
            ),
            table(
                "statement_fields.csv",
                self.env._("Field Values"),
                self.env._("Values of the form fields of the statement."),
                field_columns,
                self._z3_field_rows(),
            ),
            table(
                "account_assignment.csv",
                self.env._("Account Assignment"),
                self.env._("Account, EÜR category and form fields of the year."),
                assignment_columns,
                self._get_assignment_rows(),
            ),
            table(
                "asset_register.csv",
                self.env._("Asset Register"),
                self.env._("Assets of the register with their values of the year."),
                asset_columns,
                self._get_asset_register_rows(),
            ),
            table(
                "low_value_assets.csv",
                self.env._("Register of Low-Value Assets"),
                self.env._("Low-value assets of the year with a net cost above 250 €."),
                low_value_columns,
                self._get_low_value_rows(),
            ),
            table(
                "asset_pools.csv",
                self.env._("Asset Pools"),
                self.env._("Asset pools with their release of the year."),
                pool_columns,
                self._get_pool_rows(),
            ),
            table(
                "withdrawals_contributions.csv",
                self.env._("Withdrawals and Contributions"),
                self.env._(
                    "Withdrawals (Kz 122) and contributions (Kz 123) of the year."
                ),
                register_columns,
                self._get_withdrawal_rows(),
            ),
            table(
                "gifts.csv",
                self.env._("Gifts per Recipient"),
                self.env._(
                    "Gifts with recipient and date; give-aways without recipient."
                ),
                register_columns,
                self._get_gift_rows(),
            ),
            table(
                "entertainment.csv",
                self.env._("Business Entertainment"),
                self.env._("Expenses of business entertainment of the year."),
                register_columns,
                self._get_entertainment_rows(),
            ),
            table(
                "profit_corrections.csv",
                self.env._("Profit Corrections"),
                self.env._("Profit corrections of the statement with their reason."),
                correction_columns,
                self._get_adjustment_rows(),
            ),
            table(
                "checks.csv",
                self.env._("Check Protocol"),
                self.env._(
                    "Check results of the statement with their acknowledgement."
                ),
                check_columns,
                self._get_check_rows(),
            ),
        ]

    def _export_z3(self):
        """Data access package Z3 (§ 147 (6) AO, G-09) as ZIP."""
        statement = self.sudo()
        business = statement.company_id
        location = ", ".join(
            part for part in (business.street, business.zip, business.city) if part
        )
        return export.z3_archive(
            statement._z3_tables(),
            supplier=business.name,
            location=location or business.name,
            comment=self.env._(
                "EÜR %(year)s of %(business)s, version %(version)s, state "
                "%(state)s; created by the module l10n_de_euer %(module)s.",
                year=statement.year,
                business=business.name,
                version=statement.version,
                state=statement._get_output_status()["state_label"],
                module=statement._get_output_status()["module_version"],
            ),
            media=self.env._("EÜR %(year)s", year=statement.year),
            date_from=datetime.date(statement.year, 1, 1),
            date_to=datetime.date(statement.year, 12, 31),
        )
