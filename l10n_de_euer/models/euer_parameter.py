# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import datetime
import json

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Domain
from odoo.tools import float_repr


class L10nDeEuerParameter(models.Model):
    """Rule parameter with a period of validity (spec 3.2, rule set)."""

    _name = "l10n_de_euer.parameter"
    _description = "EÜR Rule Parameter"
    _order = "code, date_from"

    code = fields.Char(required=True, index=True, help="Technical key, e.g. gwg_limit.")
    name = fields.Char(required=True, translate=True)
    date_basis = fields.Selection(
        [
            ("fiscal_year", "Fiscal Year"),
            ("acquisition_date", "Acquisition Date"),
        ],
        required=True,
        default="fiscal_year",
        help="Date the validity refers to: the fiscal year, or for depreciation "
        "rules the acquisition or production date of the asset.",
    )
    date_from = fields.Date(string="Valid From", help="Empty: no lower limit.")
    date_to = fields.Date(string="Valid To", help="Empty: valid until changed.")
    value = fields.Float(digits=(16, 4))
    value_json = fields.Json(
        string="Structured Value",
        help="Structured value (list or text), used instead of the number.",
    )
    value_display = fields.Char(
        string="Displayed Value", compute="_compute_value_display"
    )
    source = fields.Char(required=True, help="Legal source (Fundstelle) of the value.")

    _date_range = models.Constraint(
        "CHECK(date_from IS NULL OR date_to IS NULL OR date_from <= date_to)",
        "The start of validity must not be after its end.",
    )

    @api.depends("value", "value_json")
    def _compute_value_display(self):
        for parameter in self:
            if parameter._has_json_value():
                parameter.value_display = json.dumps(
                    parameter.value_json, ensure_ascii=False
                )
            else:
                text = float_repr(parameter.value, 4)
                parameter.value_display = text.rstrip("0").rstrip(".") or "0"

    def _has_json_value(self):
        self.ensure_one()
        return self.value_json is not None and self.value_json is not False

    @api.constrains("code", "date_from", "date_to", "date_basis")
    def _check_periods(self):
        for code in set(self.mapped("code")):
            parameters = self.search(Domain("code", "=", code)).sorted(
                lambda parameter: parameter.date_from or datetime.date.min
            )
            if len(set(parameters.mapped("date_basis"))) > 1:
                raise ValidationError(
                    self.env._(
                        "All periods of parameter %(code)s need the same date basis.",
                        code=code,
                    )
                )
            for first, second in zip(parameters, parameters[1:], strict=False):
                if (
                    not first.date_to
                    or not second.date_from
                    or first.date_to >= second.date_from
                ):
                    raise ValidationError(
                        self.env._(
                            "The periods of parameter %(code)s overlap.", code=code
                        )
                    )

    @api.model
    def _get_value(self, code, on_date):
        """Return the value of parameter ``code`` valid on ``on_date``.

        ``on_date`` is a date or, for parameters per fiscal year, the year as
        integer. Returns the structured value if set, otherwise the number.
        """
        if isinstance(on_date, int):
            on_date = datetime.date(on_date, 1, 1)
        domain = (
            Domain("code", "=", code)
            & (Domain("date_from", "=", False) | Domain("date_from", "<=", on_date))
            & (Domain("date_to", "=", False) | Domain("date_to", ">=", on_date))
        )
        parameter = self.search(domain, limit=1)
        if not parameter:
            raise UserError(
                self.env._(
                    "There is no value of parameter %(code)s for %(date)s.",
                    code=code,
                    date=on_date,
                )
            )
        if parameter._has_json_value():
            return parameter.value_json
        return parameter.value
