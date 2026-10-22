# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Helpers of the statement tests (stage E4, synthetic data, D6)."""

from decimal import Decimal

from ..models.engine import scope
from .scenario_common import ScenarioCommon

D = Decimal


class StatementCommon(ScenarioCommon):
    """A business with header values and helpers for its statements."""

    @classmethod
    def setup_statement(cls):
        cls.setup_euer()
        cls.Statement = cls.env["l10n_de_euer.statement"]
        cls.Adjustment = cls.env["l10n_de_euer.adjustment"]
        cls.company.write(
            {
                "l10n_de_euer_business_type": "Fotografie",
                "l10n_de_euer_legal_form": "Einzelunternehmen",
                "l10n_de_euer_income_type": "2",
                "l10n_de_euer_owner": "1",
            }
        )

    @classmethod
    def setup_finalisation(cls):
        """A business without blockers of the account assignment: the
        receivable and payable accounts the test framework copies for its
        partners get the category of open items (else C-02)."""
        cls.setup_statement()
        Assignment = cls.env["l10n_de_euer.account.assignment"]
        category = cls.env["l10n_de_euer.category"].search([("code", "=", "OPEN_ITEM")])
        for account in cls.receivable | cls.payable:
            if not Assignment.search(
                [("company_id", "=", cls.company.id), ("account_id", "=", account.id)]
            ):
                Assignment.create(
                    {
                        "company_id": cls.company.id,
                        "account_id": account.id,
                        "category_id": category.id,
                    }
                )

    def statement(self, year=2026, company=None):
        return self.Statement.create(
            {"company_id": (company or self.company).id, "year": year}
        )

    def answer(self, statement, confirm=True, **answers):
        """Answer all questions with supported answers, except ``answers``;
        ``confirm``: confirm as manager."""
        values = {question.code: scope.NO for question in scope.QUESTIONS}
        values["Q01_LEGAL_FORM"] = scope.YES
        values.update(answers)
        for answer in statement.scope_answer_ids:
            answer.answer = values[answer.question_code]
        if confirm:
            statement.action_confirm_scope()

    def correction(
        self, statement, kind, reason="Calculation in the own receipt", **values
    ):
        return self.Adjustment.create(
            {"statement_id": statement.id, "kind": kind, "reason": reason, **values}
        )

    def reconcile_banks(self, statement):
        """Enter the balance in Odoo as balance per statement of account."""
        statement._update_cash_balances()
        for balance in statement.cash_balance_ids:
            balance.balance_statement = balance.balance_odoo

    # ------------------------------------------------------------ finalise
    def verify_forms(self, year=2026):
        """Release of the form years by the release role (F-11, synthetic)."""
        forms, _provisional = self.env["l10n_de_euer.form"]._get_forms_for_year(year)
        forms.sudo().filtered(lambda form: form.state != "verified").write(
            {
                "state": "verified",
                "verified_by": "Synthetic Tax Advisor",
                "verified_role": "tax_advisor",
                "verified_on": "2026-10-01",
            }
        )
        return forms

    def acknowledge(self, statement):
        """Acknowledge all open warnings of the statement with a reason."""
        for check in statement.check_ids.filtered(
            lambda check: check.severity == "warning" and not check.acknowledged_on
        ):
            check.acknowledge_reason = "Checked in the test"
            check.action_acknowledge()

    def lock_year(self, year=2026, company=None):
        company = (company or self.company).root_id
        end = f"{year}-12-31"
        if not company.fiscalyear_lock_date or str(company.fiscalyear_lock_date) < end:
            company.fiscalyear_lock_date = end

    def finalize(self, statement):
        """Meet the prerequisites of the finalisation (G-05) and finalise."""
        if statement.state == "draft":
            statement.action_compute()
        if not statement.scope_confirmed_on:
            self.answer(statement)
        self.reconcile_banks(statement)
        self.lock_year(statement.year, statement.company_id)
        self.verify_forms(statement.year)
        statement.action_check()
        self.acknowledge(statement)
        statement.action_finalize()
        return statement

    def results(self, statement, code=None, origin=None):
        checks = statement.check_ids
        if code:
            checks = checks.filtered(lambda check: check.code == code)
        if origin:
            checks = checks.filtered(lambda check: check.origin == origin)
        return checks

    def result_codes(self, statement, severity=None):
        checks = statement.check_ids
        if severity:
            checks = checks.filtered(lambda check: check.severity == severity)
        return sorted(set(checks.mapped("code")))

    def value(self, statement, code):
        line = statement.line_ids.filtered(lambda line: line.code == code)
        if not line:
            return None
        if line.field_type in ("text", "key", "date"):
            return line.value_text
        return D(str(line.amount)).quantize(D("0.01"))
