# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models
from odoo.exceptions import UserError

from .engine import scope as scope_engine
from .euer_statement import INTERNAL, check_same_parent

#: fields a user may write; the confirmation is set by the statement only
USER_FIELDS = frozenset({"answer", "note"})


def question_labels(env):
    """Texts of the questions in the language of the user (spec 7.3)."""
    return {
        "Q01_LEGAL_FORM": env._("The business is a sole proprietorship."),
        "Q02_FISCAL_YEAR": env._(
            "The fiscal year of the business deviates from the calendar year."
        ),
        "Q03_CLOSURE": env._(
            "The business was sold, closed or transferred free of charge in the year."
        ),
        "Q04_LAND": env._(
            "Land or rights equivalent to land were withdrawn or sold in the year."
        ),
        "Q05_LUF": env._(
            "The business has income from agriculture and forestry or applies "
            "§ 24 UStG."
        ),
        "Q06_IAB": env._(
            "Investment deductions under § 7g (1) to (4) EStG were formed, added "
            "back or reversed in the year or one of the three previous years."
        ),
        "Q07_RESERVES": env._(
            "Reserves or hidden reserves (§ 6b, § 6c EStG, R 6.6 EStR) or a "
            "compensating item (§ 4g EStG) were formed, transferred or released, "
            "or a profit surcharge applies."
        ),
        "Q08_METHOD_CHANGE": env._("The method of profit determination changed."),
        "Q09_PARTNERSHIP": env._(
            "The business holds shares in partnerships or cost-sharing communities."
        ),
        "Q10_INVSTG_TEV": env._(
            "The business has income under the InvStG or the partial income "
            "method (§ 3 no. 40 EStG, § 8b KStG)."
        ),
        "Q11_CRYPTO": env._("The business holds crypto assets."),
        "Q12_TAX_FREE": env._(
            "The business has tax-free income under § 3 or § 3a EStG or expenses "
            "not deductible under § 3c EStG."
        ),
        "Q13_PREPAYMENT": env._(
            "Advance payments for a use of more than five years (e.g. rent, "
            "leasehold) were paid and are spread over the period (§ 11 (2) "
            "sentence 3 EStG), or were received and are spread by choice (§ 11 "
            "(1) sentence 3 EStG)."
        ),
    }


class L10nDeEuerScopeAnswer(models.Model):
    """Answer of the mandatory questionnaire of a statement (R-52).

    The answers are created with the statement; proposals from the previous
    year are no confirmation. A manager confirms the questionnaire with the
    action of the statement; changing an answer later removes its
    confirmation (C-45).
    """

    _name = "l10n_de_euer.scope.answer"
    _inherit = ["mail.thread"]
    _description = "EÜR Questionnaire Answer"
    _order = "statement_id, sequence, id"

    statement_id = fields.Many2one(
        "l10n_de_euer.statement", required=True, index=True, ondelete="cascade"
    )
    company_id = fields.Many2one(
        related="statement_id.company_id", store=True, index=True
    )
    year = fields.Integer(related="statement_id.year", store=True)
    sequence = fields.Integer()
    question_code = fields.Selection(
        [(code, code) for code in scope_engine.QUESTION_CODES],
        required=True,
        readonly=True,
    )
    # text fields: the list of the questionnaire wraps them instead of
    # cutting the question off
    question = fields.Text(compute="_compute_question")
    form_reference = fields.Text(
        string="Form",
        compute="_compute_question",
        help="Lines and Kz of the form of the year the question stands for.",
    )
    answer = fields.Selection(
        [(scope_engine.YES, "Yes"), (scope_engine.NO, "No")], tracking=True
    )
    value = fields.Char(
        compute="_compute_value",
        help="Value of the form field, e.g. Kz 15.120: 1 = yes, 2 = no.",
    )
    unsupported = fields.Boolean(
        compute="_compute_value",
        help="The answer is a case outside the program scope (C-40).",
    )
    proposal_source = fields.Selection(
        [("previous_year", "Previous Year"), ("previous_version", "Previous Version")],
        string="Proposed From",
        readonly=True,
        help="The answer was proposed; it counts only after the confirmation.",
    )
    confirmed_by_id = fields.Many2one(
        "res.users", readonly=True, copy=False, tracking=True, ondelete="restrict"
    )
    confirmed_on = fields.Datetime(readonly=True, copy=False, tracking=True)
    note = fields.Text()

    _statement_question_uniq = models.Constraint(
        "UNIQUE(statement_id, question_code)",
        "Each question is answered once per statement.",
    )

    @api.depends("question_code", "statement_id.form_ids", "year")
    @api.depends_context("lang")
    def _compute_question(self):
        labels = question_labels(self.env)
        forms_by_year = {}
        for answer in self:
            answer.question = labels.get(answer.question_code, answer.question_code)
            year = answer.year
            if year and year not in forms_by_year:
                forms = answer.statement_id.form_ids
                if not forms:
                    try:
                        forms, _provisional = self.env[
                            "l10n_de_euer.form"
                        ]._get_forms_for_year(year)
                    except UserError:
                        forms = self.env["l10n_de_euer.form"]
                forms_by_year[year] = {field.code: field for field in forms.field_ids}
            answer.form_reference = answer._format_reference(
                forms_by_year.get(year) or {}
            )

    def _format_reference(self, fields_by_code):
        """``Zeile 85, 86 (Kz 180, 181)`` of the fields of the question."""
        question = scope_engine.QUESTIONS_BY_CODE.get(self.question_code)
        if not question or not question.fields:
            return ""
        found = [
            fields_by_code[code] for code in question.fields if code in fields_by_code
        ]
        lines = sorted({field.line for field in found if field.line})
        kzs = [field.kz for field in found if field.kz]
        text = self.env._(
            "Line %(lines)s", lines=", ".join(str(line) for line in lines)
        )
        if kzs:
            text += f" (Kz {', '.join(dict.fromkeys(kzs))})"
        return text if lines else ""

    @api.depends("answer", "question_code")
    def _compute_value(self):
        for answer in self:
            question = scope_engine.QUESTIONS_BY_CODE.get(answer.question_code)
            answer.unsupported = bool(
                question and question.blocking and answer.answer == question.blocking
            )
            answer.value = (
                scope_engine.land_value(answer.answer)
                if answer.question_code == scope_engine.LAND
                else ""
            )

    def _get_engine_answer(self):
        self.ensure_one()
        return scope_engine.Answer(
            question=self.question_code,
            answer=self.answer or None,
            confirmed=bool(self.confirmed_on),
        )

    @api.model
    def _is_internal(self):
        return self.env.su and bool(self.env.context.get(INTERNAL))

    @api.model_create_multi
    def create(self, vals_list):
        if not self._is_internal():
            raise UserError(self.env._("The questions are created with the statement."))
        answers = super().create(vals_list)
        answers.statement_id._check_open()
        return answers

    def write(self, vals):
        # the answers of a finalised statement never change (spec 7.7)
        check_same_parent(self, "statement_id", vals)
        self.statement_id._check_open()
        if self._is_internal():
            return super().write(vals)
        if set(vals) - USER_FIELDS:
            raise UserError(
                self.env._(
                    "Only the answer and the note can be entered; a manager "
                    "confirms the questionnaire on the statement."
                )
            )
        self.statement_id._check_open()
        changed = self.filtered(
            lambda answer: "answer" in vals
            and answer.answer != (vals["answer"] or False)
        )
        result = super().write(vals)
        if changed:
            # a changed answer needs a new confirmation (R-52)
            changed._write_internal(
                {
                    "confirmed_by_id": False,
                    "confirmed_on": False,
                    "proposal_source": False,
                }
            )
            changed.statement_id._write_internal(
                {"scope_confirmed_by_id": False, "scope_confirmed_on": False}
            )
            changed.statement_id._refresh_header_lines()
        return result

    @api.ondelete(at_uninstall=False)
    def _unlink_only_internal(self):
        self.statement_id._check_open()
        if not self._is_internal():
            raise UserError(
                self.env._("The questions belong to the statement and stay.")
            )

    def _write_internal(self, values):
        return self.sudo().with_context(**{INTERNAL: True}).write(values)
