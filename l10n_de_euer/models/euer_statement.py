# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import datetime
from decimal import Decimal

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Domain
from odoo.tools.misc import formatLang

from .engine import form_calc
from .engine import scope as scope_engine
from .euer_form import FORM_TYPES, NON_AMOUNT_TYPES

STATES = [
    ("draft", "Draft"),
    ("computed", "Computed"),
    ("final", "Finalised"),
    ("superseded", "Superseded"),
]
#: states in which a statement and its inputs can still change
OPEN_STATES = ("draft", "computed")
#: states of a finalised statement: it and its inputs never change (7.7)
CLOSED_STATES = ("final", "superseded")
#: fields entered by hand also after the finalisation (spec 7.3)
SUBMISSION_FIELDS = frozenset({"submitted_on", "submission_reference"})
#: fields a user writes on an open statement; all others are set by the
#: statement itself (state, values, finalisation)
USER_FIELDS = SUBMISSION_FIELDS | {
    "correction_reason",
    "scope_answer_ids",
    "cash_balance_ids",
    "adjustment_ids",
}
LINE_SOURCES = [
    ("computed", "Computed"),
    ("manual", "Manual"),
    ("override", "Override"),
]
#: context key: lines, answers and statement values are written by the
#: statement only, always as superuser (a client cannot set ``env.su``
#: through RPC)
INTERNAL = "l10n_de_euer_statement_internal"
#: header fields of Anlage EÜR and the company field they come from (F-08)
HEADER_FIELDS = {
    "EUER.00.STNR": "l10n_de_stnr",
    "EUER.15.101": "l10n_de_widnr",
    "EUER.15.100": "l10n_de_euer_business_type",
    "EUER.15.RECHTSFORM": "l10n_de_euer_legal_form",
    "EUER.15.103": "l10n_de_euer_income_type",
    "EUER.15.104": "l10n_de_euer_owner",
}
#: header fields that must be filled in (F-08); Kz 15.120 comes from the
#: questionnaire (Q04)
MANDATORY_HEADER = (
    "EUER.15.100",
    "EUER.15.RECHTSFORM",
    "EUER.15.103",
    "EUER.15.104",
)
FIELD_PROFIT = "EUER.G.219"
FIELD_INCOME = "EUER.20.159"
FIELD_EXPENSE = "EUER.25.199"
_FORM_ORDER = {form_type: index for index, (form_type, _label) in enumerate(FORM_TYPES)}


def _decimal(value):
    return Decimal(str(value or 0))


def check_same_parent(records, name, vals):
    """A record of a statement or run never moves to another one (spec 7.7,
    G-04): checked on every write, also as superuser and with the internal
    context keys, because the protection of a finalised statement only
    looks at the records it already has."""
    if name not in vals:
        return
    new = vals[name]
    if isinstance(new, models.BaseModel):
        new = new.id
    if any(record[name].id != (new or False) for record in records):
        raise UserError(
            records.env._(
                "The record belongs to its statement and cannot be moved to "
                "another statement."
            )
        )


class L10nDeEuerStatement(models.Model):
    """Statement of a business and year: Anlage EÜR and AVEÜR (spec 7.3).

    A statement collects everything the year needs: the questionnaire on the
    program scope (R-52), the bank reconciliation of 31 December (G-17), the
    profit corrections (spec 5.8), the field values of the forms with their
    sources and overrides, and the check results. "Compute" runs the cash
    basis calculation, "Check" runs all checks. There is one valid statement
    per business and year (R-02); a business is the main company or a branch
    that is a business of its own (spec 7.9).
    """

    _name = "l10n_de_euer.statement"
    _inherit = ["mail.thread"]
    _description = "EÜR Statement"
    _order = "year desc, company_id, version desc"
    _check_company_auto = True

    name = fields.Char(compute="_compute_name", store=True)
    company_id = fields.Many2one(
        "res.company",
        string="Business",
        required=True,
        index=True,
        default=lambda self: self.env.company._l10n_de_euer_business_root(),
        help="Main company or branch that is a business of its own.",
    )
    currency_id = fields.Many2one(related="company_id.currency_id")
    year = fields.Integer(
        required=True,
        index=True,
        default=lambda self: fields.Date.context_today(self).year - 1,
    )
    state = fields.Selection(
        STATES, required=True, default="draft", readonly=True, tracking=True
    )
    version = fields.Integer(required=True, default=1, readonly=True)
    correction_of_id = fields.Many2one(
        "l10n_de_euer.statement",
        readonly=True,
        copy=False,
        index=True,
    )
    correction_reason = fields.Text(tracking=True, copy=False)
    run_id = fields.Many2one(
        "l10n_de_euer.run",
        string="Calculation Run",
        readonly=True,
        copy=False,
        ondelete="set null",
    )
    form_ids = fields.Many2many(
        "l10n_de_euer.form", string="Form Years", readonly=True, copy=False
    )
    provisional_form = fields.Boolean(
        readonly=True,
        copy=False,
        help="There is no form data of the year yet: the statement uses the "
        "forms of an earlier year and cannot be finalised (F-13).",
    )
    computed_on = fields.Datetime(readonly=True, copy=False)
    checked_on = fields.Datetime(readonly=True, copy=False)
    line_ids = fields.One2many(
        "l10n_de_euer.statement.line", "statement_id", string="Field Values"
    )
    euer_line_ids = fields.One2many(
        "l10n_de_euer.statement.line",
        "statement_id",
        string="Anlage EÜR",
        domain=[("form_type", "!=", "aveuer")],
    )
    aveuer_line_ids = fields.One2many(
        "l10n_de_euer.statement.line",
        "statement_id",
        string="Anlage AVEÜR",
        domain=[("form_type", "=", "aveuer")],
    )
    check_ids = fields.Many2many(
        "l10n_de_euer.check", string="Check Results", compute="_compute_check_ids"
    )
    blocker_count = fields.Integer(compute="_compute_check_ids")
    open_warning_count = fields.Integer(
        string="Warnings Not Acknowledged", compute="_compute_check_ids"
    )
    adjustment_ids = fields.One2many(
        "l10n_de_euer.adjustment", "statement_id", string="Profit Corrections"
    )
    private_use_ids = fields.Many2many(
        "l10n_de_euer.asset.year",
        string="Proposed Use Withdrawals",
        compute="_compute_review_ids",
        help="Proposals of the asset register for the private use of assets (R-54).",
    )
    ten_day_ids = fields.Many2many(
        "l10n_de_euer.ten_day.item",
        string="Turn of the Year",
        compute="_compute_review_ids",
    )
    classification_ids = fields.Many2many(
        "l10n_de_euer.residual.classification",
        string="Classifications of Rests",
        compute="_compute_review_ids",
    )
    scope_answer_ids = fields.One2many(
        "l10n_de_euer.scope.answer", "statement_id", string="Questionnaire"
    )
    # users referred to by the payload of a finalisation cannot be deleted
    # (G-04): archive them instead
    scope_confirmed_by_id = fields.Many2one(
        "res.users",
        string="Questionnaire Confirmed By",
        readonly=True,
        copy=False,
        ondelete="restrict",
    )
    scope_confirmed_on = fields.Datetime(
        string="Questionnaire Confirmed On", readonly=True, copy=False
    )
    cash_balance_ids = fields.One2many(
        "l10n_de_euer.cash.balance", "statement_id", string="Bank Reconciliation"
    )
    profit = fields.Monetary(
        string="Profit or Loss",
        readonly=True,
        copy=False,
        help="Taxable profit or loss (Kz 219).",
    )
    total_income = fields.Monetary(
        string="Total Business Income", readonly=True, copy=False, help="Kz 159."
    )
    total_expense = fields.Monetary(
        string="Total Business Expenses", readonly=True, copy=False, help="Kz 199."
    )
    use_pool = fields.Boolean(
        string="Asset Pool Chosen",
        readonly=True,
        copy=False,
        tracking=True,
        help="Assets between 250 € and 1,000 € go to the asset pool of the year "
        "(§ 6 (2a) EStG), from the asset register.",
    )
    wahlrechte_json = fields.Json(
        string="Choices", readonly=True, copy=False, help="Choices of the year."
    )
    choices_text = fields.Text(string="Choices (Text)", compute="_compute_texts")
    log_text = fields.Text(related="run_id.log_text", string="Calculation Log")
    slice_count = fields.Integer(compute="_compute_slice_count")
    # finalisation (stage E4, part 2)
    payload_attachment_id = fields.Many2one(
        "ir.attachment", readonly=True, copy=False, ondelete="restrict"
    )
    payload_sha256 = fields.Char(readonly=True, copy=False)
    prev_hash = fields.Char(string="Previous Hash", readonly=True, copy=False)
    chain_hash = fields.Char(readonly=True, copy=False)
    module_version = fields.Char(readonly=True, copy=False)
    ruleset_version = fields.Char(readonly=True, copy=False)
    form_versions = fields.Char(readonly=True, copy=False)
    finalized_by_id = fields.Many2one(
        "res.users",
        string="Finalised By",
        readonly=True,
        copy=False,
        ondelete="restrict",
    )
    finalized_on = fields.Datetime(string="Finalised On", readonly=True, copy=False)
    submitted_on = fields.Date(
        tracking=True, copy=False, help="Date the values were sent to ELSTER."
    )
    submission_reference = fields.Char(
        tracking=True, copy=False, help="Transfer ticket of ELSTER."
    )

    _company_year_version_uniq = models.UniqueIndex(
        "(company_id, year, version)",
        "There is only one statement per business, year and version.",
    )

    # ------------------------------------------------------------ computes
    @api.depends("company_id", "year", "version")
    def _compute_name(self):
        for statement in self:
            name = self.env._(
                "EÜR %(year)s %(company)s",
                year=statement.year or "",
                company=statement.company_id.name or "",
            )
            if statement.version > 1:
                name += f" (v{statement.version})"
            statement.name = name

    def _check_domain(self):
        """Check results of the statement: those of the business and, for the
        account assignment, of its main company (spec 7.9)."""
        self.ensure_one()
        business = self.company_id
        return Domain("year", "=", self.year) & (
            (
                Domain("company_id", "=", business.id)
                & Domain("origin", "!=", "assignment")
            )
            | (
                Domain("company_id", "=", business.sudo().root_id.id)
                & Domain("origin", "=", "assignment")
            )
        )

    @api.depends("company_id", "year", "checked_on", "computed_on")
    def _compute_check_ids(self):
        Check = self.env["l10n_de_euer.check"]
        for statement in self:
            if not statement.id or not statement.company_id:
                statement.check_ids = Check
                statement.blocker_count = statement.open_warning_count = 0
                continue
            checks = Check.search(statement._check_domain())
            statement.check_ids = checks
            statement.blocker_count = len(
                checks.filtered(lambda check: check.severity == "blocker")
            )
            statement.open_warning_count = len(
                checks.filtered(
                    lambda check: check.severity == "warning"
                    and not check.acknowledged_on
                )
            )

    @api.depends("company_id", "year", "computed_on", "adjustment_ids")
    def _compute_review_ids(self):
        Year = self.env["l10n_de_euer.asset.year"]
        TenDay = self.env["l10n_de_euer.ten_day.item"]
        Classification = self.env["l10n_de_euer.residual.classification"]
        for statement in self:
            if not statement.company_id or not statement.year:
                statement.private_use_ids = Year
                statement.ten_day_ids = TenDay
                statement.classification_ids = Classification
                continue
            business = statement.company_id
            statement.private_use_ids = Year.search(
                Domain("company_id", "=", business.id)
                & Domain("year", "=", statement.year)
                & (
                    Domain("private_use_amount", "!=", 0)
                    | Domain("private_use_vat", "!=", 0)
                    | Domain("private_use_decision", "!=", False)
                )
            )
            statement.ten_day_ids = TenDay.search(
                Domain("company_id", "=", business.id)
                & Domain("year", "in", [statement.year - 1, statement.year])
            )
            date_from = datetime.date(statement.year, 1, 1)
            date_to = datetime.date(statement.year, 12, 31)
            statement.classification_ids = Classification.search(
                Domain("company_id", "=", business.id)
                & (
                    Domain("date", "=", False)
                    | (Domain("date", ">=", date_from) & Domain("date", "<=", date_to))
                )
            )

    def _choice_labels(self):
        return {
            "entertainment_mode": self.env._("Business entertainment"),
            "fx_mode": self.env._("Realised exchange differences"),
            "offsetting_is_payment": self.env._("Offsetting is payment"),
            "disposal_month": self.env._("Depreciation in the month of disposal"),
            "use_pool": self.env._("Asset pool chosen"),
        }

    def _choice_value_text(self, key, value):
        """Readable value of a choice: the label of a selection, yes or no."""
        if isinstance(value, bool):
            return self.env._("Yes") if value else self.env._("No")
        Company = self.env["res.company"]
        name = f"l10n_de_euer_{key}"
        if name in Company._fields and Company._fields[name].type == "selection":
            labels = dict(Company._fields[name]._description_selection(self.env))
            return labels.get(value, value)
        return "" if value is None else str(value)

    @api.depends("wahlrechte_json", "use_pool")
    @api.depends_context("lang")
    def _compute_texts(self):
        labels = self._choice_labels()
        for statement in self:
            choices = statement.wahlrechte_json or {}
            statement.choices_text = "\n".join(
                f"{labels.get(key, key)}: {statement._choice_value_text(key, value)}"
                for key, value in sorted(choices.items())
            )

    def _compute_slice_count(self):
        Slice = self.env["l10n_de_euer.cash.slice"]
        for statement in self:
            statement.slice_count = (
                Slice.search_count(
                    Domain("run_id", "=", statement.run_id.id)
                    & Domain("attribution_year", "=", statement.year)
                )
                if statement.run_id
                else 0
            )

    # ------------------------------------------------------------ constraints
    @api.constrains("company_id")
    def _check_business(self):
        for statement in self:
            company = statement.company_id
            if company._l10n_de_euer_business_root() != company:
                raise ValidationError(
                    self.env._(
                        "%(company)s is a branch of a business: create the "
                        "statement for the main company or a branch that is a "
                        "business of its own.",
                        company=company.display_name,
                    )
                )
            if not company.sudo().root_id.l10n_de_euer_enabled:
                raise ValidationError(
                    self.env._(
                        "Cash basis accounting (EÜR) is not enabled for %(company)s.",
                        company=company.sudo().root_id.display_name,
                    )
                )

    @api.constrains("company_id", "year", "state", "correction_of_id")
    def _check_one_per_year(self):
        """R-02: one valid statement per business and year; a correction
        exists next to the version it corrects until it is finalised."""
        for statement in self.filtered(lambda s: s.state != "superseded"):
            if not 1900 < statement.year < 3000:
                raise ValidationError(self.env._("Enter a valid year."))
            others = self.sudo().search(
                Domain("company_id", "=", statement.company_id.id)
                & Domain("year", "=", statement.year)
                & Domain("state", "!=", "superseded")
                & Domain("id", "!=", statement.id)
            )
            others = others.filtered(
                lambda other, s=statement: other.correction_of_id != s
                and s.correction_of_id != other
            )
            if others:
                raise ValidationError(
                    self.env._(
                        "There is already a statement for %(company)s and "
                        "%(year)s: %(names)s.",
                        company=statement.company_id.display_name,
                        year=statement.year,
                        names=", ".join(others.mapped("name")),
                    )
                )

    @api.constrains("correction_of_id", "correction_reason")
    def _check_correction_reason(self):
        for statement in self.filtered("correction_of_id"):
            if not (statement.correction_reason or "").strip():
                raise ValidationError(
                    self.env._(
                        "Enter the reason of the correction %(name)s.",
                        name=statement.name,
                    )
                )

    # ------------------------------------------------------------ CRUD
    @api.model
    def _is_internal(self):
        return self.env.su and bool(self.env.context.get(INTERNAL))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("company_id"):
                company = self.env["res.company"].browse(vals["company_id"])
                vals["company_id"] = company._l10n_de_euer_business_root().id
            if not self._is_internal() and (
                vals.get("correction_of_id")
                or vals.get("version", 1) != 1
                or vals.get("state", "draft") != "draft"
            ):
                raise UserError(
                    self.env._(
                        "A correction is created from the finalised statement with "
                        "the action Create Correction."
                    )
                )
            if not vals.get("correction_of_id"):
                self._check_no_valid_statement(
                    vals.get("company_id")
                    or self.env.company._l10n_de_euer_business_root().id,
                    vals.get("year"),
                )
        statements = super().create(vals_list)
        for statement in statements:
            statement._create_answers()
            statement._update_cash_balances()
        return statements

    @api.model
    def _check_no_valid_statement(self, company_id, year):
        """R-02 before the record exists (clearer than the unique index)."""
        if not year:
            return
        existing = self.sudo().search(
            Domain("company_id", "=", company_id)
            & Domain("year", "=", year)
            & Domain("state", "!=", "superseded"),
            limit=1,
        )
        if existing:
            raise ValidationError(
                self.env._(
                    "There is already a statement for %(company)s and %(year)s: "
                    "%(names)s.",
                    company=existing.company_id.display_name,
                    year=year,
                    names=existing.name,
                )
            )

    def write(self, vals):
        """A finalised statement never changes (spec 7.7, G-04), also not
        for an administrator or with ``sudo()``: only the date and the
        reference of the submission can be entered. The statement itself
        only marks a finalised version as superseded by its correction."""
        internal = self._is_internal()
        closed = self.filtered(lambda statement: statement.state in CLOSED_STATES)
        if closed:
            allowed = set(SUBMISSION_FIELDS)
            if (
                internal
                and vals.get("state") == "superseded"
                and all(statement.state == "final" for statement in closed)
            ):
                allowed.add("state")
            if set(vals) - allowed:
                closed._raise_closed()
        if not internal:
            if {"company_id", "year"} & set(vals):
                raise UserError(
                    self.env._(
                        "The business and the year of a statement cannot change; "
                        "create a new statement."
                    )
                )
            if set(vals) - USER_FIELDS:
                raise UserError(
                    self.env._(
                        "The state, the values and the finalisation of a statement "
                        "are set by its actions."
                    )
                )
            if set(vals) - SUBMISSION_FIELDS:
                self._check_open()
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_only_open(self):
        """Finalised statements are kept, there is no way to delete them
        (archiving, § 147 AO)."""
        closed = self.filtered(lambda statement: statement.state in CLOSED_STATES)
        if closed:
            raise UserError(
                self.env._(
                    "%(names)s is finalised and cannot be deleted; keep it for at "
                    "least 10 years (§ 147 (3) AO).",
                    names=", ".join(closed.mapped("name")),
                )
            )

    def _check_open(self):
        closed = self.filtered(lambda statement: statement.state not in OPEN_STATES)
        if closed:
            closed._raise_closed()

    def _raise_closed(self):
        raise UserError(
            self.env._(
                "%(names)s is finalised and cannot be changed; create a correction.",
                names=", ".join(self.mapped("name")),
            )
        )

    def _write_internal(self, values):
        return self.sudo().with_context(**{INTERNAL: True}).write(values)

    def _set_draft(self):
        """Inputs of the calculation changed: compute again."""
        computed = self.filtered(lambda statement: statement.state == "computed")
        if computed:
            computed._write_internal({"state": "draft"})

    # ------------------------------------------------------------ previous
    def _get_previous(self):
        """Valid statement of the previous year (latest version)."""
        self.ensure_one()
        return self.search(
            Domain("company_id", "=", self.company_id.id)
            & Domain("year", "=", self.year - 1)
            & Domain("state", "!=", "superseded"),
            order="version desc",
            limit=1,
        )

    @api.model
    def _get_for_calculation(self, business, year):
        """Statement whose profit corrections the calculation of ``business``
        and ``year`` uses: the one in the context, else the valid one."""
        statement_id = self.env.context.get("l10n_de_euer_statement_id")
        Statement = self.sudo()
        if statement_id:
            statement = Statement.browse(statement_id).exists()
            if statement.company_id == business and statement.year == year:
                return statement
        return Statement.search(
            Domain("company_id", "=", business.id)
            & Domain("year", "=", year)
            & Domain("state", "!=", "superseded"),
            order="version desc",
            limit=1,
        )

    # ------------------------------------------------------------ questions
    def _create_answers(self):
        """One answer per question; the answers of the previous year (of the
        corrected version for a correction) are proposals, never a
        confirmation (R-52, GT-14)."""
        self.ensure_one()
        source = self.correction_of_id or self._get_previous()
        origin = "previous_version" if self.correction_of_id else "previous_year"
        previous = {answer.question_code: answer for answer in source.scope_answer_ids}
        existing = set(self.scope_answer_ids.mapped("question_code"))
        values = []
        for sequence, question in enumerate(scope_engine.QUESTIONS, start=1):
            if question.code in existing:
                continue
            proposal = previous.get(question.code)
            values.append(
                {
                    "statement_id": self.id,
                    "question_code": question.code,
                    "sequence": sequence,
                    "answer": proposal.answer if proposal else False,
                    "note": proposal.note
                    if proposal and self.correction_of_id
                    else False,
                    "proposal_source": origin
                    if proposal and proposal.answer
                    else False,
                }
            )
        self.env["l10n_de_euer.scope.answer"].sudo().with_context(
            **{INTERNAL: True}
        ).create(values)

    def action_confirm_scope(self):
        """Confirm all answers of the questionnaire (managers, R-52)."""
        self._check_manager()
        for statement in self:
            statement._check_open()
            missing = statement.scope_answer_ids.filtered(lambda a: not a.answer)
            if missing or not statement.scope_answer_ids:
                raise UserError(
                    self.env._(
                        "Answer all questions before confirming the "
                        "questionnaire: %(questions)s",
                        questions="; ".join(missing.mapped("question")),
                    )
                )
            now = fields.Datetime.now()
            statement.scope_answer_ids.filtered(
                lambda answer: not answer.confirmed_on
            )._write_internal({"confirmed_by_id": self.env.uid, "confirmed_on": now})
            statement._write_internal(
                {"scope_confirmed_by_id": self.env.uid, "scope_confirmed_on": now}
            )
            statement.message_post(
                body=self.env._(
                    "Questionnaire confirmed by %(user)s.", user=self.env.user.name
                )
            )
            statement._refresh_header_lines()
        return True

    def _check_manager(self):
        if not self.env.su and not self.env.user.has_group(
            "account.group_account_manager"
        ):
            raise UserError(
                self.env._("Only an accounting manager can confirm the questionnaire.")
            )

    # ------------------------------------------------------------ cash
    def _get_money_journals(self):
        """Money journals of the business that need a balance of 31 December:
        bank, cash and credit card journals (also archived) with posted
        lines up to the end of the year. Journals of a private account
        (R-18) are left out: they hold the business transactions only."""
        self.ensure_one()
        companies = self.company_id._l10n_de_euer_business_companies().sudo()
        journals = (
            self.env["account.journal"]
            .sudo()
            .with_context(active_test=False)
            .search(
                Domain("company_id", "in", companies.ids)
                & Domain("type", "in", ["bank", "cash", "credit"])
                & Domain("l10n_de_euer_private_account", "=", False)
                & Domain("default_account_id", "!=", False)
            )
        )
        if not journals:
            return journals
        groups = (
            self.env["account.move.line"]
            .sudo()
            ._read_group(
                Domain("company_id", "in", companies.ids)
                & Domain("account_id", "in", journals.default_account_id.ids)
                & Domain("parent_state", "=", "posted")
                & Domain("date", "<=", datetime.date(self.year, 12, 31)),
                ["account_id"],
                ["__count"],
            )
        )
        used = {account.id for account, count in groups if count}
        return journals.filtered(lambda journal: journal.default_account_id.id in used)

    def _update_cash_balances(self):
        """Add a bank reconciliation line for each money journal (G-17)."""
        for statement in self.filtered(lambda s: s.state in OPEN_STATES):
            existing = statement.cash_balance_ids.journal_id
            new = statement._get_money_journals() - existing
            if new:
                self.env["l10n_de_euer.cash.balance"].sudo().create(
                    [
                        {"statement_id": statement.id, "journal_id": journal.id}
                        for journal in new
                    ]
                )

    def action_update_cash_balances(self):
        self.check_access("write")
        self._check_open()
        self._update_cash_balances()
        return True

    # ------------------------------------------------------------ compute
    def action_compute(self):
        """Compute the year (spec 7.7 step 1): a new run of the cash basis
        calculation with the profit corrections of the statement, then the
        field values of the forms."""
        self.check_access("write")
        for statement in self:
            statement._check_open()
            # a question added to the questionnaire later (e.g. Q13) reaches
            # the statements created before
            statement._create_answers()
            statement.adjustment_ids._recompute_results()
            errors = statement.adjustment_ids.filtered("error")
            if errors:
                raise UserError(
                    "\n".join(
                        f"{adjustment.display_name}: {adjustment.error}"
                        for adjustment in errors
                    )
                )
            run = (
                self.env["l10n_de_euer.run"]
                .with_context(l10n_de_euer_statement_id=statement.id)
                ._compute_year(statement.company_id, statement.year)
            )
            statement._store_lines(run)
            statement._update_cash_balances()
            statement.message_post(
                body=self.env._(
                    "Computed: profit or loss %(profit)s.",
                    profit=formatLang(self.env, statement.profit, digits=2),
                )
            )
        return True

    def _store_lines(self, run, computed_now=True):
        """Field values of the forms of the year from the run (phase G),
        with the header values and the overrides of the lines.

        ``computed_now``: the run was just computed (state computed);
        otherwise only the values change (overrides).
        """
        self.ensure_one()
        forms, provisional = (
            self.env["l10n_de_euer.form"].sudo()._get_forms_for_year(self.year)
        )
        values, computed = self._form_values(forms, run)
        overrides = {
            line.code: line for line in self.line_ids if line.source == "override"
        }
        previous = self._get_previous_values()
        headers = self._get_header_values()
        adjustment_amounts = self.adjustment_ids._get_total_amounts()
        totals = run._get_field_amounts()
        lines = {}
        for field in self._sorted_fields(forms):
            code = field.code
            if field.field_type in NON_AMOUNT_TYPES:
                text = headers.get(code) or ""
                if not text and not field.mandatory and code not in HEADER_FIELDS:
                    continue
                lines[code] = {
                    **self._line_base(field),
                    "value_text": text,
                    "source": "manual",
                }
                continue
            value = values.get(code)
            prev = previous.get(code)
            override = overrides.get(code)
            if not value and not prev and not override and not field.mandatory:
                continue
            total = totals.get(code)
            manual = bool(total) and adjustment_amounts.get(code) == total
            line = {
                **self._line_base(field),
                "amount": float(value or 0),
                "amount_computed": float(computed.get(code) or 0),
                "amount_prev_year": float(prev or 0),
                "value_text": False,
                "source": "computed",
            }
            if override:
                line.update(
                    source="override",
                    override_amount=override.override_amount,
                    override_reason=override.override_reason,
                )
            elif manual:
                line["source"] = "manual"
            lines[code] = line
        # update the lines in place: an open line keeps its record
        Line = self.env["l10n_de_euer.statement.line"]
        for line in self.line_ids:
            vals = lines.pop(line.code, None)
            if vals is None:
                line.sudo().with_context(**{INTERNAL: True}).unlink()
            else:
                line._write_internal(vals)
        Line.sudo().with_context(**{INTERNAL: True}).create(
            [{**vals, "statement_id": self.id} for vals in lines.values()]
        )
        choices = self._get_choices()
        self._log_choices(choices)
        vals = {
            "run_id": run.id,
            "form_ids": [fields.Command.set(forms.ids)],
            "provisional_form": provisional,
            "profit": float(values.get(FIELD_PROFIT) or 0),
            "total_income": float(values.get(FIELD_INCOME) or 0),
            "total_expense": float(values.get(FIELD_EXPENSE) or 0),
            "use_pool": choices["use_pool"],
            "wahlrechte_json": choices,
        }
        if computed_now:
            vals.update(state="computed", computed_on=fields.Datetime.now())
        self._write_internal(vals)

    def _line_base(self, field):
        return {
            "field_id": field.id,
            "code": field.code,
            "form_type": field.form_id.form_type,
            "box": field.box,
            "line": field.line,
            "kz": field.kz,
            "sequence": _FORM_ORDER.get(field.form_id.form_type, 9) * 100000
            + field.sequence,
            "field_type": field.field_type,
        }

    @api.model
    def _sorted_fields(self, forms):
        return forms.field_ids.sorted(
            lambda field: (
                _FORM_ORDER.get(field.form_id.form_type, 9),
                field.sequence,
                field.line,
                field.id,
            )
        )

    def _form_values(self, forms, run, overrides=None):
        """``(values, computed)``: the values of the forms with the overrides
        of the lines and without them (phase G, spec 5.9)."""
        self.ensure_one()
        if overrides is None:
            overrides = {
                line.code: _decimal(line.override_amount)
                for line in self.line_ids
                if line.source == "override"
            }
        amount_fields = forms.field_ids.filtered(
            lambda field: field.field_type not in NON_AMOUNT_TYPES
        )
        formulas = {field.code for field in amount_fields if field.formula}
        known = set(amount_fields.mapped("code"))
        amounts = {
            code: value
            for code, value in run._get_field_amounts().items()
            if code in known
        }
        computed = forms._calculate(amounts)
        inputs = dict(amounts)
        inputs.update(
            {
                code: value
                for code, value in overrides.items()
                if code in known and code not in formulas
            }
        )
        values = forms._calculate(
            inputs,
            {code: value for code, value in overrides.items() if code in formulas},
        )
        return values, computed

    def _get_previous_values(self):
        """Values of the previous year: its statement, else its last run."""
        previous = self._get_previous()
        if previous.line_ids:
            return {line.code: _decimal(line.amount) for line in previous.line_ids}
        run = (
            self.env["l10n_de_euer.run"]
            .sudo()
            .search(
                Domain("company_id", "=", self.company_id.id)
                & Domain("year_from", "=", self.year - 1)
                & Domain("year_to", "=", self.year - 1),
                limit=1,
            )
        )
        return {
            code: _decimal(value) for code, value in (run.form_values or {}).items()
        }

    def _get_header_values(self):
        """Header values of Anlage EÜR (F-08): from the company and, for
        Kz 15.120, from question Q04 of the questionnaire."""
        self.ensure_one()
        company = self.company_id.sudo()
        root = company.root_id
        values = {}
        for code, name in HEADER_FIELDS.items():
            source = company if name in ("l10n_de_stnr", "l10n_de_widnr") else root
            value = source[name] if name in source._fields else False
            if not value and source != root and name in root._fields:
                value = root[name]
            values[code] = value or ""
        land = self.scope_answer_ids.filtered(
            lambda answer: answer.question_code == scope_engine.LAND
        )
        # a proposal of the previous year is no answer (R-52, C-45): Kz
        # 15.120 is filled in only after the confirmation
        values[scope_engine.FIELD_LAND] = (
            scope_engine.land_value(land.answer) if land.confirmed_on else ""
        )
        return values

    def _refresh_header_lines(self):
        """Header values changed (questionnaire): update the lines."""
        for statement in self.filtered("line_ids"):
            headers = statement._get_header_values()
            for line in statement.line_ids.filtered(
                lambda line: line.field_type in NON_AMOUNT_TYPES
            ):
                text = headers.get(line.code) or ""
                if line.value_text != text:
                    line._write_internal({"value_text": text})

    def _get_use_pool(self):
        pools = (
            self.env["l10n_de_euer.asset.pool"]
            .sudo()
            .search(
                Domain("company_id", "=", self.company_id.id)
                & Domain("year", "=", self.year)
            )
        )
        return bool(pools.filtered(lambda pool: pool.amount))

    def _get_choices(self):
        """Choices of the year kept with the statement (R-45)."""
        root = self.company_id.sudo().root_id
        return {
            "entertainment_mode": root.l10n_de_euer_entertainment_mode or "separate",
            "fx_mode": root.l10n_de_euer_fx_mode or "separate",
            "offsetting_is_payment": bool(root.l10n_de_euer_offsetting_is_payment),
            "disposal_month": bool(root.l10n_de_euer_disposal_month),
            "use_pool": self._get_use_pool(),
        }

    def _log_choices(self, choices):
        """Change log of the choices of the year (G-06): a computation with
        other choices than the previous one is logged in the chatter."""
        self.ensure_one()
        old = self.wahlrechte_json or {}
        if not old or old == choices:
            return
        labels = self._choice_labels()
        changes = [
            f"{labels.get(key, key)}: {self._choice_value_text(key, old.get(key))}"
            f" → {self._choice_value_text(key, choices.get(key))}"
            for key in sorted(set(old) | set(choices))
            if old.get(key) != choices.get(key)
        ]
        self.message_post(
            body=self.env._(
                "Choices of the year changed: %(changes)s", changes="; ".join(changes)
            )
        )

    def _recalculate(self):
        """Overrides changed: compute the form values again from the stored
        run, without a new calculation."""
        for statement in self.filtered("run_id"):
            statement._store_lines(statement.run_id, computed_now=False)

    # ------------------------------------------------------------ check
    def action_check(self):
        """Run all checks (spec 7.7 step 2): account assignment, calculation,
        asset register with the form values of the statement, and the checks
        of the statement itself."""
        self.check_access("write")
        Check = self.env["l10n_de_euer.check"]
        for statement in self:
            statement._check_open()
            if not statement.run_id:
                raise UserError(self.env._("Compute the statement first."))
            business, year = statement.company_id, statement.year
            Run = self.env["l10n_de_euer.run"].with_context(
                l10n_de_euer_statement_id=statement.id
            )
            result = Run._engine_result(business, year)
            stored = {
                code: int(value)
                for code, value in (statement.run_id.totals or {}).items()
            }
            if {code: v for code, v in result.totals.items() if v} != stored:
                raise UserError(
                    self.env._(
                        "Bookings, profit corrections or the asset register "
                        "changed since the statement was computed: compute it "
                        "again."
                    )
                )
            values = statement._get_values()
            known = set(statement.form_ids.field_ids.mapped("code"))
            amounts = {
                code: Decimal(value) / 100
                for code, value in stored.items()
                if code in known
            }
            Check._run_assignment_checks(business.sudo().root_id, year)
            form_results = statement.form_ids._check_values(values, amounts)
            self.env["l10n_de_euer.calculation.checker"]._store(
                business, year, result, statement.run_id, form_results
            )
            Check._run_asset_checks(business, year, form_values=values)
            statement._update_cash_balances()
            results = self.env["l10n_de_euer.statement.checker"]._run(statement)
            Check._store_results(business, year, "statement", results)
            statement._write_internal({"checked_on": fields.Datetime.now()})
        return True

    def _get_formulas(self):
        """Formula per field code of the forms of the statement."""
        self.ensure_one()
        return {
            field.code: field.formula
            for field in self.form_ids.field_ids
            if field.formula
        }

    def _get_values(self):
        """Final values of the forms: code → ``Decimal`` (with overrides)."""
        self.ensure_one()
        values, _computed = self._form_values(self.form_ids, self.run_id)
        return values

    # ------------------------------------------------------------ actions
    def action_take_over_private_use(self):
        """Take over the open proposals of use withdrawals of the asset
        register as profit corrections (R-54). The register then leaves out
        the private use of these assets, so nothing counts twice."""
        self.check_access("write")
        Adjustment = self.env["l10n_de_euer.adjustment"]
        for statement in self:
            statement._check_open()
            taken = statement.adjustment_ids.asset_id
            for line in statement.private_use_ids:
                if line.asset_id in taken or line.private_use_decision:
                    continue
                if not (
                    line.private_use_amount
                    or line.private_use_vat
                    or line.private_use_vat_base
                ):
                    continue
                Adjustment.create(
                    {
                        "statement_id": statement.id,
                        "kind": "withdrawal_in_kind",
                        "asset_id": line.asset_id.id,
                        "amount": line.private_use_amount,
                        "vat_amount": line.private_use_vat,
                        "vat_base": line.private_use_vat_base,
                        "date": datetime.date(statement.year, 12, 31),
                        "reason": self.env._(
                            "Use withdrawal proposed by the asset register for "
                            "the private use of %(asset)s (R-54).",
                            asset=line.asset_id.display_name,
                        ),
                    }
                )
        return True

    def action_open_slices(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_cash_slice"
        )
        action["domain"] = [
            ("run_id", "=", self.run_id.id),
            ("attribution_year", "=", self.year),
        ]
        action["context"] = {}
        return action

    def action_open_assets(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_asset"
        )
        action["domain"] = [("company_id", "=", self.company_id.id)]
        return action

    def action_open_assignments(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_account_assignment"
        )
        action["domain"] = [("company_id", "=", self.company_id.sudo().root_id.id)]
        return action

    def action_open_checks(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_check"
        )
        action["domain"] = list(self._check_domain())
        action["context"] = {}
        return action


class L10nDeEuerStatementLine(models.Model):
    """Value of a form field in a statement (spec 7.3).

    ``source``: computed from the bookings, the asset register and the
    profit corrections; manual (header values, values of profit corrections
    only); override (entered with a reason instead of the computed value,
    G-15). Empty fields have no value (R-46).
    """

    _name = "l10n_de_euer.statement.line"
    _description = "EÜR Statement Field Value"
    _order = "statement_id, sequence, id"

    statement_id = fields.Many2one(
        "l10n_de_euer.statement", required=True, index=True, ondelete="cascade"
    )
    company_id = fields.Many2one(
        related="statement_id.company_id", store=True, index=True
    )
    year = fields.Integer(related="statement_id.year", store=True)
    currency_id = fields.Many2one(related="statement_id.currency_id")
    field_id = fields.Many2one("l10n_de_euer.field", string="Form Field", required=True)
    code = fields.Char(required=True, index=True)
    form_type = fields.Selection(FORM_TYPES, string="Form")
    box = fields.Char()
    line = fields.Integer(help="Line of the form of the year.")
    kz = fields.Char()
    sequence = fields.Integer()
    field_type = fields.Char()
    name = fields.Char(related="field_id.name", string="Description")
    official_label = fields.Text(related="field_id.official_label")
    amount = fields.Monetary(readonly=True)
    amount_computed = fields.Monetary(
        string="Computed Amount", readonly=True, help="Value without override."
    )
    amount_prev_year = fields.Monetary(string="Previous Year", readonly=True)
    value_text = fields.Char(string="Value", readonly=True)
    has_value = fields.Boolean(compute="_compute_has_value")
    source = fields.Selection(LINE_SOURCES, required=True, default="computed")
    override_amount = fields.Monetary(
        help="Amount entered instead of the computed value."
    )
    override_reason = fields.Text(help="Reason of the override (own receipt, G-15).")
    slice_count = fields.Integer(string="Slices", compute="_compute_slice_count")
    has_register_source = fields.Boolean(
        string="From the Asset Register",
        compute="_compute_has_register_source",
        help="The value comes (also) from the asset register.",
    )

    @api.depends("amount", "value_text", "field_type")
    def _compute_has_value(self):
        for line in self:
            if line.field_type in NON_AMOUNT_TYPES:
                line.has_value = bool(line.value_text)
            else:
                line.has_value = not line.currency_id.is_zero(line.amount)

    def _get_slice_codes(self, formulas=None):
        """Codes whose slices make up the value: the field and the inputs of
        its formula, recursively (drilldown, G-03). ``formulas``: code →
        formula of the forms of the statement (computed once by the
        caller)."""
        self.ensure_one()
        if formulas is None:
            formulas = self.statement_id._get_formulas()
        codes, todo = set(), [self.code]
        while todo:
            code = todo.pop()
            if code in codes:
                continue
            codes.add(code)
            if code in formulas:
                todo.extend(form_calc.references(formulas[code]))
        return codes

    def _slice_domain(self):
        self.ensure_one()
        return (
            Domain("run_id", "=", self.statement_id.run_id.id)
            & Domain("attribution_year", "=", self.statement_id.year)
            & Domain("field_code", "in", sorted(self._get_slice_codes()))
        )

    def _compute_slice_count(self):
        """Slices per line with one grouped read per statement (no query per
        line)."""
        Slice = self.env["l10n_de_euer.cash.slice"]
        counts = {}
        readable = Slice.has_access("read")
        for statement in self.statement_id:
            if not statement.run_id or not readable:
                continue
            groups = Slice._read_group(
                Domain("run_id", "=", statement.run_id.id)
                & Domain("attribution_year", "=", statement.year)
                & Domain("field_code", "!=", False),
                ["field_code"],
                ["__count"],
            )
            counts[statement.id] = dict(groups)
        formulas = {}
        for line in self:
            statement = line.statement_id
            if (
                statement.id not in counts
                or line.field_type in NON_AMOUNT_TYPES
                or not line.code
            ):
                line.slice_count = 0
                continue
            if statement.id not in formulas:
                formulas[statement.id] = statement._get_formulas()
            codes = line._get_slice_codes(formulas[statement.id])
            line.slice_count = sum(counts[statement.id].get(code, 0) for code in codes)

    def _compute_has_register_source(self):
        Slice = self.env["l10n_de_euer.cash.slice"]
        codes = {}
        for statement in self.statement_id:
            groups = (
                Slice._read_group(
                    Domain("run_id", "=", statement.run_id.id)
                    & Domain("attribution_year", "=", statement.year)
                    & Domain("rule", "=", "asset"),
                    ["field_code"],
                    ["__count"],
                )
                if statement.run_id and Slice.has_access("read")
                else []
            )
            codes[statement.id] = {code for code, _count in groups if code}
        formulas = {}
        for line in self:
            statement = line.statement_id
            register = codes.get(statement.id) or set()
            if not register or line.field_type in NON_AMOUNT_TYPES:
                line.has_register_source = False
                continue
            if statement.id not in formulas:
                formulas[statement.id] = statement._get_formulas()
            line.has_register_source = bool(
                line._get_slice_codes(formulas[statement.id]) & register
            )

    def action_open_assets(self):
        """Assets behind the value: yearly values → asset → journal items
        (spec 7.6 no. 3)."""
        self.ensure_one()
        statement = self.statement_id
        return self.env["l10n_de_euer.asset"]._action_open_field_sources(
            statement.company_id, statement.year, self._get_slice_codes()
        )

    def action_open_slices(self):
        """Show the slices behind the value (spec 7.6)."""
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_cash_slice"
        )
        action["domain"] = list(self._slice_domain())
        action["context"] = {"search_default_group_field": 1}
        action["name"] = self.env._(
            "Slices of %(code)s", code=f"{self.kz or self.code} ({self.year})"
        )
        return action

    @api.constrains("source", "override_reason")
    def _check_override_reason(self):
        for line in self:
            if line.source == "override" and not (line.override_reason or "").strip():
                raise ValidationError(
                    self.env._(
                        "Enter a reason for the override of %(code)s (G-15).",
                        code=line.code,
                    )
                )

    @api.model
    def _is_internal(self):
        return self.env.su and bool(self.env.context.get(INTERNAL))

    def write(self, vals):
        # the values of a finalised statement never change (spec 7.7), and a
        # value never moves into another statement, also not internally
        check_same_parent(self, "statement_id", vals)
        self.statement_id._check_open()
        if self._is_internal():
            return super().write(vals)
        allowed = {"source", "override_amount", "override_reason"}
        if set(vals) - allowed:
            raise UserError(
                self.env._(
                    "The values of a statement are computed; override a value "
                    "with a reason instead."
                )
            )
        if vals.get("source") not in (None, "override", "computed"):
            raise UserError(self.env._("Only an override can be entered."))
        self.statement_id._check_open()
        if self.filtered(lambda line: line.field_type in NON_AMOUNT_TYPES):
            raise UserError(
                self.env._("Header values come from the company and the questionnaire.")
            )
        vals = dict(vals)
        if vals.get("source") == "computed":
            vals.update(override_amount=0.0, override_reason=False)
        result = super().write(vals)
        changed = self.filtered(
            lambda line: "source" in vals
            or (line.source == "override" and "override_amount" in vals)
        )
        for line in changed:
            # change log of overrides (G-06)
            if line.source == "override":
                body = self.env._(
                    "Override of %(code)s: %(amount)s instead of %(computed)s. "
                    "Reason: %(reason)s",
                    code=line.kz or line.code,
                    amount=formatLang(self.env, line.override_amount, digits=2),
                    computed=formatLang(self.env, line.amount_computed, digits=2),
                    reason=line.override_reason or "",
                )
            else:
                body = self.env._(
                    "Override of %(code)s removed.", code=line.kz or line.code
                )
            line.statement_id.message_post(body=body)
        if "source" in vals or self.filtered(lambda line: line.source == "override"):
            self.statement_id._recalculate()
        return result

    def action_apply_override(self):
        """Use the entered amount instead of the computed value (G-15)."""
        self.write({"source": "override"})
        return {"type": "ir.actions.act_window_close"}

    def action_override(self):
        """Override the computed value (opens the line for input)."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "views": [
                (
                    self.env.ref(
                        "l10n_de_euer.l10n_de_euer_statement_line_view_form"
                    ).id,
                    "form",
                )
            ],
            "target": "new",
        }

    def action_reset(self):
        """Back to the computed value."""
        self.write({"source": "computed"})
        return {"type": "ir.actions.act_window_close"}

    @api.model_create_multi
    def create(self, vals_list):
        if not self._is_internal():
            raise UserError(self.env._("The values of a statement are computed."))
        lines = super().create(vals_list)
        lines.statement_id._check_open()
        return lines

    @api.ondelete(at_uninstall=False)
    def _unlink_only_internal(self):
        self.statement_id._check_open()
        if not self._is_internal():
            raise UserError(self.env._("The values of a statement are computed."))

    def _write_internal(self, values):
        return self.sudo().with_context(**{INTERNAL: True}).write(values)
