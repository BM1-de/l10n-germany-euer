# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Domain

from .engine import form_calc

CODE_PREFIXES = {"euer": "EUER", "aveuer": "AVEUER", "sz": "SZ"}


class L10nDeEuerField(models.Model):
    """Field (line and Kennzahl) of a form year (spec 3.1, 3.2, F-02)."""

    _name = "l10n_de_euer.field"
    _description = "EÜR Form Field"
    _order = "form_id, sequence, id"
    _rec_names_search = ["code", "name", "kz", "official_label"]

    form_id = fields.Many2one(
        "l10n_de_euer.form",
        string="Form Year",
        required=True,
        index=True,
        ondelete="cascade",
    )
    year = fields.Integer(related="form_id.year", store=True, index=True)
    form_type = fields.Selection(related="form_id.form_type", store=True)
    code = fields.Char(
        required=True,
        index=True,
        help="Stable key FORM.BOX.KZ, e.g. EUER.20.111. Box G is the profit "
        "determination without printed box; lines without Kennzahl have a "
        "speaking key such as EUER.25.UEB1. The line is only an attribute per year.",
    )
    box = fields.Char(
        required=True,
        help="Box (Kasten) of the form section: 15, 20, 25, 27, 29, 40, 43, "
        "G for the profit determination and 00 for the header without box.",
    )
    kz = fields.Char(help="Printed Kennzahl; empty if the line has none.")
    line = fields.Integer(help="Line of the form in this year.")
    sequence = fields.Integer(default=10, help="Order of the official form.")
    official_label = fields.Text(
        required=True,
        translate=False,
        help="Official wording of the form line. German and never translated.",
    )
    name = fields.Char(string="Short Name", required=True, translate=True)
    field_type = fields.Selection(
        [
            ("text", "Text"),
            ("key", "Key"),
            ("date", "Date"),
            ("amount", "Amount"),
            ("amount_davon", "Amount (of Which)"),
            ("transfer", "Transfer"),
            ("subtotal", "Subtotal"),
            ("total", "Total"),
            ("constant", "Constant"),
        ],
        required=True,
        default="amount",
        help="Amounts of type 'of which' are informational and never part of a sum.",
    )
    sign = fields.Selection(
        [
            ("+", "+ (adds)"),
            ("-", "- (subtracts)"),
            ("±", "± (signed value)"),
            ("=", "= (result)"),
            ("0", "0 (informational)"),
        ],
        help="Effect of the value on the sum it goes into.",
    )
    formula = fields.Char(
        help="Expression over codes of the same assessment year with +, -, min(), "
        "max(), parentheses and numbers, e.g. EUER.25.UEB2 + EUER.25.174 - "
        "EUER.25.142. Fields with a formula are computed.",
    )
    allow_negative = fields.Boolean(
        help="The official form accepts a negative value in this field."
    )
    relative_year = fields.Integer(
        help="Year of an asset pool or investment deduction relative to the "
        "assessment year (0, -1 … -4)."
    )
    mandatory = fields.Boolean(help="The field must always be filled in.")
    elster_field_id = fields.Char(string="ELSTER Field ID")
    elster_xpath = fields.Char(string="ELSTER XPath")
    elster_verified = fields.Boolean(
        string="ELSTER ID Verified",
        help="The ELSTER field ID was checked against the official documentation. "
        "Unverified IDs are marked in exports.",
    )
    scope_status = fields.Selection(
        [
            ("supported", "Supported"),
            ("manual", "Manual Input"),
            ("blocked", "Not Supported"),
        ],
        string="Program Scope",
        required=True,
        default="supported",
        help="Supported: computed from the bookings. Manual input: entered with "
        "formula help and a voucher of your own. Not supported: a value in this "
        "field is outside the program scope and blocks finalisation; values "
        "derived by a formula are covered by the checks of their inputs.",
    )

    _form_code_uniq = models.Constraint(
        "UNIQUE(form_id, code)", "The code of a field must be unique per form year."
    )

    @api.depends("code", "name")
    def _compute_display_name(self):
        for field in self:
            field.display_name = f"{field.code} {field.name or ''}".strip()

    @api.constrains("code", "box", "kz", "form_id")
    def _check_code(self):
        for field in self:
            parts = (field.code or "").split(".")
            prefix = CODE_PREFIXES.get(field.form_id.form_type)
            if (
                not form_calc.is_code(field.code)
                or parts[0] != prefix
                or parts[1] != field.box
                or (field.kz and parts[2] != field.kz)
            ):
                raise ValidationError(
                    self.env._(
                        "The code %(code)s does not match form %(prefix)s, box "
                        "%(box)s and Kz %(kz)s.",
                        code=field.code,
                        prefix=prefix,
                        box=field.box,
                        kz=field.kz or "-",
                    )
                )

    @api.constrains("elster_verified", "elster_field_id")
    def _check_elster(self):
        for field in self:
            if field.elster_verified and not field.elster_field_id:
                raise ValidationError(
                    self.env._(
                        "%(code)s: only an existing ELSTER field ID can be verified.",
                        code=field.code,
                    )
                )

    @api.constrains("formula", "code", "form_id")
    def _check_formula(self):
        """Formulas use only known codes of the same year and contain no cycle.

        All formulas of the affected years are checked, so renaming a field
        that another formula refers to fails as well.
        """
        for year in set(self.mapped("year")):
            year_fields = self.search(Domain("year", "=", year))
            self._check_year_formulas(year, year_fields)

    @api.model
    def _check_year_formulas(self, year, year_fields):
        known = set(year_fields.mapped("code"))
        # several versions of a form share their codes: check the union
        formulas = {}
        for field in year_fields.filtered("formula"):
            formulas.setdefault(field.code, set()).add(field.formula)
        merged = {
            code: " + ".join(f"({formula})" for formula in sorted(variants))
            for code, variants in formulas.items()
        }
        for field in year_fields.filtered("formula"):
            errors = form_calc.validate({field.code: field.formula}, known)
            if errors:
                raise ValidationError(
                    self.env._(
                        "Invalid formula of %(code)s: %(errors)s",
                        code=field.code,
                        errors="; ".join(errors),
                    )
                )
        try:
            form_calc.calculation_order(merged)
        except form_calc.FormulaError as error:
            raise ValidationError(
                self.env._(
                    "Invalid formulas in %(year)s: %(error)s",
                    year=year,
                    error=error,
                )
            ) from error
        for form in year_fields.form_id.filtered("fill_condition"):
            unknown = form_calc.references(form.fill_condition) - known
            if unknown:
                raise ValidationError(
                    self.env._(
                        "The fill condition of %(name)s uses unknown codes: %(codes)s",
                        name=form.name,
                        codes=", ".join(sorted(unknown)),
                    )
                )
            # the fields of the form come after the inputs of the condition
            for field in year_fields.filtered(lambda f, fm=form: f.form_id == fm):
                parts = [merged.get(field.code), f"({form.fill_condition})"]
                merged[field.code] = " + ".join(part for part in parts if part)
        try:
            form_calc.calculation_order(merged)
        except form_calc.FormulaError as error:
            raise ValidationError(
                self.env._(
                    "Invalid formulas or fill conditions in %(year)s: %(error)s",
                    year=year,
                    error=error,
                )
            ) from error

    @api.ondelete(at_uninstall=False)
    def _unlink_except_referenced(self):
        """A field must not be deleted while a formula or condition uses its code."""
        for year in set(self.mapped("year")):
            remaining = self.search(Domain("year", "=", year)) - self
            codes = set(self.filtered(lambda f, y=year: f.year == y).mapped("code"))
            codes -= set(remaining.mapped("code"))
            if not codes:
                continue
            users = remaining.filtered(
                lambda f, c=codes: f.formula and form_calc.references(f.formula) & c
            )
            names = users.mapped("code")
            forms = self.env["l10n_de_euer.form"].search(
                Domain("year", "=", year) & Domain("fill_condition", "!=", False)
            )
            names += forms.filtered(
                lambda f, c=codes: form_calc.references(f.fill_condition) & c
            ).mapped("name")
            if names:
                raise UserError(
                    self.env._(
                        "%(codes)s cannot be deleted; it is used by %(users)s.",
                        codes=", ".join(sorted(codes)),
                        users=", ".join(sorted(names)),
                    )
                )

    @api.model_create_multi
    def create(self, vals_list):
        fields_ = super().create(vals_list)
        fields_.form_id._check_not_locked()
        return fields_

    def write(self, vals):
        self.form_id._check_not_locked()
        if vals.get("form_id"):
            self.env["l10n_de_euer.form"].browse(vals["form_id"])._check_not_locked()
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_locked_form(self):
        self.form_id._check_not_locked()
