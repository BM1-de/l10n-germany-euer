# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Domain

from .engine import form_calc

FORM_TYPES = [
    ("euer", "Anlage EÜR"),
    ("aveuer", "Anlage AVEÜR"),
    ("sz", "Anlage SZ"),
]
# Field types that hold no amount and take no part in the calculation
NON_AMOUNT_TYPES = ("text", "key", "date")


class L10nDeEuerForm(models.Model):
    """Official form of one assessment year (spec 3.2, F-01, F-11, F-15)."""

    _name = "l10n_de_euer.form"
    _description = "EÜR Form Year"
    _order = "year desc, form_type, version desc"

    # Fields that may still be written when a finalised statement uses the form
    _LOCK_FREE_FIELDS = frozenset({"superseded_by_id"})

    name = fields.Char(compute="_compute_name", store=True)
    year = fields.Integer(
        required=True,
        index=True,
        help="Assessment year (Veranlagungsjahr) the form belongs to.",
    )
    form_type = fields.Selection(FORM_TYPES, string="Form", required=True)
    version = fields.Integer(
        required=True,
        default=1,
        help="Version of the form data. A correction of delivered data is a new "
        "version with its own records; statements keep the version they used.",
    )
    superseded_by_id = fields.Many2one(
        "l10n_de_euer.form",
        ondelete="restrict",
        help="Newer version of the same form year that corrects this one.",
    )
    vordruck_ids = fields.Char(
        string="Form IDs",
        help="Form numbers printed in the footer of the official form, "
        "e.g. 2025AnlEÜR801–804.",
    )
    bmf_date = fields.Date(string="BMF Letter Date")
    bmf_reference = fields.Char(
        string="BMF Reference",
        help="File reference (Geschäftszeichen) of the BMF letter.",
    )
    bmf_dok = fields.Char(string="BMF Document Number")
    instructions_version = fields.Char(
        help="Version of the official instructions (Anleitung) of this year."
    )
    source_url = fields.Char(
        string="Source URL", help="Address of the evaluated PDF of the BMF letter."
    )
    source_sha256 = fields.Char(
        string="Source SHA-256", help="Checksum of the evaluated PDF of the BMF letter."
    )
    state = fields.Selection(
        [("draft", "Draft"), ("verified", "Verified")],
        required=True,
        default="draft",
        help="A form year is verified only after the review by a person who did "
        "not create the data (tax advisor or second maintainer).",
    )
    verified_by = fields.Char(help="Name of the person who verified the form data.")
    verified_role = fields.Selection(
        [
            ("tax_advisor", "Tax Advisor"),
            ("second_maintainer", "Second Maintainer"),
        ],
        help="Role of the person who verified the form data.",
    )
    verified_on = fields.Date()
    verification_note = fields.Text()
    locked = fields.Boolean(
        compute="_compute_locked",
        help="Set as soon as a finalised statement uses this form year. The form, "
        "its fields and mappings can then no longer be changed.",
    )
    fill_condition = fields.Char(
        help="Expression over codes of other forms of the same year. The form is "
        "filled in only if its result is above zero, e.g. Anlage SZ only if the "
        "other interest on debt exceeds 2,050 € (EUER.25.234 - 2050.00). Empty: "
        "the form is always filled in.",
    )
    field_ids = fields.One2many("l10n_de_euer.field", "form_id", string="Fields")
    field_count = fields.Integer(compute="_compute_field_count")

    _year_form_version_uniq = models.Constraint(
        "UNIQUE(year, form_type, version)",
        "There can only be one form per year, form type and version.",
    )
    _version_positive = models.Constraint(
        "CHECK(version > 0)", "The version of a form year must be positive."
    )

    @api.depends("year", "form_type", "version")
    def _compute_name(self):
        labels = dict(FORM_TYPES)
        for form in self:
            form.name = (
                f"{labels.get(form.form_type, '')} {form.year} (v{form.version})"
            )

    @api.depends("field_ids")
    def _compute_field_count(self):
        for form in self:
            form.field_count = len(form.field_ids)

    def _compute_locked(self):
        used = self._get_finalized_forms()
        for form in self:
            form.locked = form in used

    def _get_finalized_forms(self):
        """Return the forms of ``self`` used by a finalised statement."""
        if not self.ids:
            return self.browse()
        statements = (
            self.env["l10n_de_euer.statement"]
            .sudo()
            .search(
                Domain("state", "in", ["final", "superseded"])
                & Domain("form_ids", "in", self.ids)
            )
        )
        return (statements.form_ids & self).with_env(self.env)

    def _check_not_locked(self):
        locked = self.filtered("locked")
        if locked:
            raise UserError(
                self.env._(
                    "The form year %(names)s is used by a finalised statement and "
                    "cannot be changed. Deliver a correction as a new version.",
                    names=", ".join(locked.mapped("name")),
                )
            )

    @api.constrains("state", "verified_by", "verified_role", "verified_on")
    def _check_verification(self):
        for form in self:
            if form.state == "verified" and not (
                form.verified_by and form.verified_role and form.verified_on
            ):
                raise ValidationError(
                    self.env._(
                        "%(name)s can only be set to verified with the name, role "
                        "and date of the person who verified it.",
                        name=form.name,
                    )
                )

    @api.constrains("superseded_by_id", "year", "form_type", "version")
    def _check_superseded_by(self):
        for form in self.filtered("superseded_by_id"):
            newer = form.superseded_by_id
            if (
                newer.year != form.year
                or newer.form_type != form.form_type
                or newer.version <= form.version
            ):
                raise ValidationError(
                    self.env._(
                        "%(name)s can only be superseded by a newer version of the "
                        "same form and year.",
                        name=form.name,
                    )
                )

    @api.constrains("fill_condition", "form_type", "year")
    def _check_fill_condition(self):
        prefixes = {"euer": "EUER", "aveuer": "AVEUER", "sz": "SZ"}
        for form in self.filtered("fill_condition"):
            year_fields = self.env["l10n_de_euer.field"].search(
                Domain("year", "=", form.year)
            )
            try:
                refs = form_calc.references(form.fill_condition)
            except form_calc.FormulaError as error:
                raise ValidationError(
                    self.env._(
                        "Invalid fill condition of %(name)s: %(error)s",
                        name=form.name,
                        error=error,
                    )
                ) from error
            own = sorted(
                ref for ref in refs if ref.split(".", 1)[0] == prefixes[form.form_type]
            )
            unknown = sorted(refs - set(year_fields.mapped("code")))
            if own or unknown:
                raise ValidationError(
                    self.env._(
                        "The fill condition of %(name)s may only use codes of other "
                        "forms of the same year: %(codes)s",
                        name=form.name,
                        codes=", ".join(own + unknown),
                    )
                )
            # no cycle through formulas of the other forms
            formulas = {}
            for field in year_fields.filtered("formula"):
                formulas.setdefault(field.code, set()).add(field.formula)
            graph = {
                code: " + ".join(f"({formula})" for formula in sorted(variants))
                for code, variants in formulas.items()
            }
            for field in year_fields.filtered(lambda f, fm=form: f.form_id == fm):
                parts = [graph.get(field.code), f"({form.fill_condition})"]
                graph[field.code] = " + ".join(part for part in parts if part)
            try:
                form_calc.calculation_order(graph)
            except form_calc.FormulaError as error:
                raise ValidationError(
                    self.env._(
                        "Invalid fill condition of %(name)s: %(error)s",
                        name=form.name,
                        error=error,
                    )
                ) from error

    def write(self, vals):
        if set(vals) - self._LOCK_FREE_FIELDS:
            self._check_not_locked()
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_locked(self):
        self._check_not_locked()

    def action_open_fields(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_field"
        )
        action["domain"] = [("form_id", "=", self.id)]
        action["context"] = {"default_form_id": self.id}
        return action

    @api.model
    def _get_forms_for_year(self, year):
        """Return ``(forms, provisional)`` for an assessment year (F-13).

        Per form type the newest verified version is used, or the newest
        version if none is verified yet. If the year has no form data (e.g.
        2027 before the BMF letter), the latest earlier year is used and
        ``provisional`` is true; such a statement cannot be finalised.
        """
        candidates = self.search(Domain("year", "<=", year), order="year desc")
        if not candidates:
            raise UserError(
                self.env._("There is no form data for %(year)s or earlier.", year=year)
            )
        form_year = candidates[0].year
        candidates = candidates.filtered(lambda form: form.year == form_year)
        result = self.browse()
        for form_type, _label in FORM_TYPES:
            versions = candidates.filtered(
                lambda form, t=form_type: form.form_type == t
            )
            pool = versions.filtered(lambda form: form.state == "verified") or versions
            if pool:
                result |= max(pool, key=lambda form: form.version)
        return result, form_year != year

    def _calculate(self, amounts, overrides=None):
        """Compute all fields of the forms in ``self`` (spec 5.9, phase G).

        ``self`` holds the forms of one assessment year, at most one version
        per form type (see :meth:`_get_forms_for_year`). Formulas may use the
        codes of all these forms, e.g. the transfers AVEÜR → EÜR.

        :param amounts: dict code → amount of the input fields
        :param overrides: dict code → amount replacing a formula result
        :return: dict code → ``Decimal`` or ``None`` (empty field)
        """
        if len(set(self.mapped("year"))) > 1:
            raise UserError(self.env._("Only forms of one year can be calculated."))
        if len(self) != len(set(self.mapped("form_type"))):
            raise UserError(self.env._("Use only one version per form type."))
        fields_ = self.field_ids
        amount_codes = set(
            fields_.filtered(
                lambda field: field.field_type not in NON_AMOUNT_TYPES
            ).mapped("code")
        )
        unknown = sorted((set(amounts) | set(overrides or {})) - amount_codes)
        if unknown:
            raise UserError(
                self.env._(
                    "No amount fields of %(forms)s: %(codes)s",
                    forms=", ".join(self.mapped("name")),
                    codes=", ".join(unknown),
                )
            )
        formulas = {field.code: field.formula for field in fields_ if field.formula}
        conditions = {
            field.code: field.form_id.fill_condition
            for field in fields_
            if field.form_id.fill_condition and field.code in amount_codes
        }
        values = dict.fromkeys(amount_codes)
        try:
            values.update(
                form_calc.compute(
                    formulas,
                    amounts,
                    overrides,
                    conditions=conditions,
                    known=amount_codes,
                )
            )
            return values
        except form_calc.FormulaError as error:
            raise UserError(
                self.env._("The form cannot be calculated: %(error)s", error=error)
            ) from error

    def _check_values(self, values, amounts=None):
        """Checks of the calculated values of the forms in ``self`` (spec 7.8).

        ``values`` is the result of :meth:`_calculate`, ``amounts`` its input.
        Returns check results like the account assignment checks:

        - C-40: a field outside the program scope has an input value
          (values derived by a formula are covered by their inputs);
        - C-41: Anlage SZ has to be filled in (other interest on debt above
          2,050 €), which is not supported yet (F-14);
        - C-71: negative value in a field whose official form allows no minus
          (F-06, by the flag ``allow_negative`` of the field).
        """
        results = []
        fields_ = {field.code: field for field in self.field_ids}
        for code in sorted(amounts or {}):
            field = fields_.get(code)
            value = values.get(code)
            if not field or field.scope_status != "blocked" or not value:
                continue
            results.append(
                self._check_result(
                    "C-40",
                    self.env._(
                        "%(code)s %(name)s is outside the program scope of the "
                        "module and has a value of %(amount)s.",
                        code=code,
                        name=field.name,
                        amount=f"{value:.2f}",
                    ),
                    field,
                    value,
                )
            )
        for form in self.filtered(lambda f: f.form_type == "sz" and f.fill_condition):
            if form_calc.condition_met(form.fill_condition, values):
                results.append(
                    self._check_result(
                        "C-41",
                        self.env._(
                            "Other interest on debt exceeds the deductible base "
                            "amount: %(form)s is required (§ 4 (4a) EStG). Its "
                            "calculation is not supported yet.",
                            form=form.name,
                        ),
                        form,
                        values.get("EUER.25.234") or 0.0,
                    )
                )
        allowed = [code for code, field in fields_.items() if field.allow_negative]
        for code in form_calc.negative_codes(values, allowed):
            if code not in fields_:
                continue
            results.append(
                self._check_result(
                    "C-71",
                    self.env._(
                        "%(code)s %(name)s is negative (%(amount)s), but the "
                        "official form allows no minus in this field.",
                        code=code,
                        name=fields_[code].name,
                        amount=f"{values[code]:.2f}",
                    )
                    + " "
                    + self._negative_proposal(fields_[code]),
                    fields_[code],
                    values[code],
                )
            )
        return results

    def _negative_proposal(self, field):
        """Proposal for a negative value in a field without minus (C-71,
        F-06, spec 5.7 no. 10), by the section of the field."""
        if field.box == "25":
            return self.env._(
                "Usually a refund of an expense paid in an earlier year exceeds "
                "the expenses of this field in the year. Check the refund in the "
                "drilldown: a real refund is business income (classify it or "
                "book it on an income account: Kz 103, as small business Kz "
                "111 and of it Kz 119) or belongs to another expense field with "
                "expenses in the year; a counter booking without money is "
                "deleted; a reclassification between expense fields is booked "
                "in the year of the payments."
            )
        if field.box == "20":
            return self.env._(
                "Usually a refund to a customer exceeds the income of this field "
                "in the year. Check it in the drilldown: book or classify it as "
                "reduction of the income of its year, or as other expense (Kz "
                "183) if the income of the year is too small."
            )
        return self.env._(
            "Check the values of the field in the drilldown and correct the "
            "booking or the entry that makes it negative."
        )

    @api.model
    def _check_result(self, code, message, records, amount):
        return {
            "code": code,
            "severity": "blocker",
            "message": message,
            "res_model": records._name,
            "res_ids": sorted(records.ids),
            "amount": float(amount),
        }

    def _finalization_issues(self, provisional=False):
        """Reasons why a statement on the forms of ``self`` cannot be finalised.

        C-72: the form year is missing (``provisional``, F-13) or not yet
        verified by the release role (F-11, D7).
        """
        results = []
        if provisional:
            results.append(
                self._check_result(
                    "C-72",
                    self.env._(
                        "There is no form data for the assessment year; the "
                        "statement is provisional on the forms of %(year)s.",
                        year=", ".join(str(year) for year in set(self.mapped("year"))),
                    ),
                    self,
                    0.0,
                )
            )
        drafts = self.filtered(lambda form: form.state != "verified")
        if drafts:
            results.append(
                self._check_result(
                    "C-72",
                    self.env._(
                        "Form years not verified yet by a tax advisor or second "
                        "maintainer (F-11): %(names)s",
                        names=", ".join(drafts.mapped("name")),
                    ),
                    drafts,
                    0.0,
                )
            )
        return results
