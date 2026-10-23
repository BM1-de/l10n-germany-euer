# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Rights and multi-company of the statement (spec 7.9, 7.10).

GT-13: a bookkeeper (``account.group_account_user``) computes and checks,
enters profit corrections, bank balances, answers and overrides; only a
manager confirms the questionnaire; a manager without the bookkeeper group
has all rights of a bookkeeper; an auditor only reads. GT-02: statements,
values, answers, corrections and bank reconciliations of one business are
not visible from another business and do not mix in the calculation.
Synthetic cases (D6).
"""

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from ..models.engine import scope
from .scenario_common import SKR03_ACCOUNTS, SKR03_TAXES
from .statement_common import D, StatementCommon

KU = "EUER.20.111"


@tagged("post_install_l10n", "post_install", "-at_install")
class TestStatementRights(StatementCommon, AccountTestInvoicingCommon):
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_statement()
        users = {}
        for login, groups in (
            ("euer_bookkeeper", "account.group_account_user"),
            ("euer_manager_only", "account.group_account_manager"),
            ("euer_auditor", "account.group_account_readonly"),
        ):
            users[login] = new_test_user(
                cls.env,
                login=login,
                groups=groups,
                company_id=cls.company.id,
                company_ids=[Command.set(cls.company.ids)],
            )
        cls.bookkeeper = users["euer_bookkeeper"]
        cls.manager = users["euer_manager_only"]
        cls.auditor = users["euer_auditor"]

    def year_2026(self):
        sale = self.sale("2026-03-10", 1000)
        self.bank("2026-03-20", 1000, matches=[(sale, 1000)])

    def work_as(self, user):
        """The yearly work of a bookkeeper on a new statement."""
        statement = self.Statement.with_user(user).create(
            {"company_id": self.company.id, "year": 2026}
        )
        self.Adjustment.with_user(user).create(
            {
                "statement_id": statement.id,
                "kind": "meals",
                "days_8h": 2,
                "reason": "Trip to a customer",
            }
        )
        statement.action_compute()
        for answer in statement.scope_answer_ids:
            answer.answer = (
                scope.YES if answer.question_code == "Q01_LEGAL_FORM" else scope.NO
            )
        for balance in statement.cash_balance_ids:
            balance.balance_statement = balance.balance_odoo
        line = statement.line_ids.filtered(lambda line: line.code == KU)
        line.write(
            {
                "source": "override",
                "override_amount": 1001.0,
                "override_reason": "Rounding of the cash book",
            }
        )
        statement.action_check()
        return statement

    def test_questions_belong_to_the_statement(self):
        """The questionnaire comes with the statement (R-52): no question is
        added or deleted by hand."""
        statement = self.Statement.create({"company_id": self.company.id, "year": 2026})
        answer = statement.scope_answer_ids[:1]
        self.assertTrue(answer)
        with self.assertRaises(UserError):
            self.env["l10n_de_euer.scope.answer"].create(
                {"statement_id": statement.id, "question_code": answer.question_code}
            )
        with self.assertRaises(UserError):
            answer.unlink()

    # ------------------------------------------------------------ GT-13
    def test_gt13_bookkeeper(self):
        """GT-13: the bookkeeper does the yearly work, but cannot confirm the
        questionnaire."""
        self.year_2026()
        statement = self.work_as(self.bookkeeper)
        self.assertEqual(statement.state, "computed")
        self.assertEqual(self.value(statement, KU), D("1001.00"))
        self.assertTrue(statement.check_ids)
        with self.assertRaises(UserError):
            statement.action_confirm_scope()
        self.assertIn("C-45", self.result_codes(statement, "blocker"))

    def test_gt13_manager_has_bookkeeper_rights(self):
        """GT-13: a manager without the bookkeeper group computes, checks and
        confirms (in Odoo 19 the manager group does not imply it)."""
        self.assertFalse(self.manager.has_group("account.group_account_user"))
        self.year_2026()
        statement = self.work_as(self.manager)
        statement.action_confirm_scope()
        statement.action_check()
        self.assertNotIn("C-45", self.result_codes(statement))
        self.assertEqual(statement.scope_confirmed_by_id, self.manager)

    def test_gt13_auditor_reads_only(self):
        """GT-13: an auditor reads statements and values, nothing else."""
        self.year_2026()
        statement = self.statement()
        self.correction(statement, "meals", days_8h=1)
        statement.action_compute()
        statement.action_check()
        as_auditor = statement.with_user(self.auditor)
        self.assertTrue(as_auditor.line_ids.mapped("amount"))
        self.assertTrue(as_auditor.check_ids)
        self.assertTrue(as_auditor.adjustment_ids.result_amount)
        self.assertTrue(as_auditor.scope_answer_ids.mapped("question"))
        with self.assertRaises(AccessError):
            as_auditor.action_compute()
        with self.assertRaises(AccessError):
            as_auditor.scope_answer_ids[:1].answer = scope.YES
        with self.assertRaises(AccessError):
            as_auditor.adjustment_ids.days_8h = 3
        with self.assertRaises(AccessError):
            self.Statement.with_user(self.auditor).create(
                {"company_id": self.company.id, "year": 2025}
            )

    # ------------------------------------------------------------ GT-02
    def test_gt02_multi_company(self):
        """GT-02: a second business with EÜR and a third company without:
        no mixing in the calculation, no view across businesses."""
        self.year_2026()
        other = self.setup_other_company(name="Other Business")
        company_b = other["company"]
        company_b.l10n_de_euer_enabled = True
        company_b.l10n_de_euer_income_type = "3"
        self.env["l10n_de_euer.vat.period"].create(
            {"company_id": company_b.id, "date_from": "2020-01-01", "regime": "ku"}
        )
        self.bank(
            "2026-05-05",
            70,
            extra=[(self.env.ref(f"account.{company_b.id}_account_8195"), -70)],
            journal=other["default_journal_bank"],
        )
        company_c = self.setup_other_company(name="Without EÜR")["company"]
        statement_a = self.statement()
        self.correction(statement_a, "meals", days_8h=2)
        statement_a.action_compute()
        statement_b = self.statement(company=company_b)
        self.correction(statement_b, "meals", days_8h=1)
        statement_b.action_compute()
        self.assertEqual(self.value(statement_a, KU), D("1000.00"))
        self.assertEqual(statement_b.total_income, 70.0)
        self.assertEqual(statement_a.profit, 972.0)
        self.assertEqual(
            statement_b.cash_balance_ids.journal_id, other["default_journal_bank"]
        )
        self.assertNotIn(
            other["default_journal_bank"], statement_a.cash_balance_ids.journal_id
        )
        with self.assertRaises(ValidationError):
            self.statement(company=company_c)
        # a bookkeeper of the first business sees nothing of the second one
        for model in (
            "l10n_de_euer.statement",
            "l10n_de_euer.statement.line",
            "l10n_de_euer.scope.answer",
            "l10n_de_euer.adjustment",
            "l10n_de_euer.cash.balance",
        ):
            with self.subTest(model=model):
                records = self.env[model].with_user(self.bookkeeper).search([])
                self.assertTrue(records)
                self.assertEqual(records.company_id, self.company)
        with self.assertRaises(AccessError):
            statement_b.with_user(self.bookkeeper).read(["profit"])
        # the checks of the second business stay there
        statement_b.action_check()
        self.assertEqual(statement_b.check_ids.company_id, company_b)
        self.assertNotIn(statement_b.check_ids[:1], statement_a.check_ids)
