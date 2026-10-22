# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Mandatory questionnaire and program scope (R-52, spec 1.2, 7.3).

UT-SCOPE-1 to UT-SCOPE-12: one test per question Q01 to Q12; the booking
heuristics independent of the questionnaire (fiscal year, accounts of
categories outside the program scope, other interest on debt above
2,050 € → C-41); GT-14 as far as it can be checked without finalisation
(confirmation by a manager only, proposals of the previous year are no
confirmation). Synthetic cases (D6).
"""

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from ..models.engine import scope
from .scenario_common import (
    SKR03_ACCOUNTS,
    SKR03_TAXES,
    SKR04_ACCOUNTS,
    SKR04_TAXES,
)
from .statement_common import StatementCommon


class ScopeCommon(StatementCommon):
    def checked(self, statement=None, **answers):
        """Statement of 2026 computed, answered, confirmed and checked."""
        statement = statement or self.statement()
        statement.action_compute()
        self.answer(statement, **answers)
        statement.action_check()
        return statement

    def scope_results(self, statement, code):
        return self.results(statement, code, origin="statement")

    def assertOutOfScope(self, question, answer):
        statement = self.checked(**{question: answer})
        results = self.scope_results(statement, "C-40")
        self.assertEqual(len(results), 1, results.mapped("message"))
        self.assertEqual(results.severity, "blocker")
        self.assertEqual(results.res_model, "l10n_de_euer.scope.answer")
        record = self.env["l10n_de_euer.scope.answer"].browse(results.res_ids)
        self.assertEqual(record.question_code, question)
        self.assertTrue(record.unsupported)
        self.assertNotIn("C-45", self.result_codes(statement))
        # the other answer is supported
        other = scope.NO if answer == scope.YES else scope.YES
        record.answer = other
        statement.action_confirm_scope()
        results = self.env["l10n_de_euer.statement.checker"]._run(statement)
        self.assertNotIn("C-40", [result["code"] for result in results])


@tagged("post_install_l10n", "post_install", "-at_install")
class TestScopeQuestions(ScopeCommon, AccountTestInvoicingCommon):
    CHART = "de_skr03"
    ACCOUNTS = {**SKR03_ACCOUNTS, "interest": "account_2110"}
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_statement()

    # ------------------------------------------------------------ UT-SCOPE
    def test_ut_scope_01_legal_form(self):
        """Q01: a business that is no sole proprietorship (ER/SE/AVSE)."""
        self.assertOutOfScope("Q01_LEGAL_FORM", scope.NO)

    def test_ut_scope_02_fiscal_year(self):
        """Q02: fiscal year deviating from the calendar year (Kz 131/132)."""
        self.assertOutOfScope("Q02_FISCAL_YEAR", scope.YES)

    def test_ut_scope_03_closure(self):
        """Q03: sale, closure or free transfer of the business (Kz 15.111)."""
        self.assertOutOfScope("Q03_CLOSURE", scope.YES)

    def test_ut_scope_04_land(self):
        """Q04: Kz 15.120 = 1; without a disposal of land in the register
        warning C-43, with a withdrawal of land none."""
        statement = self.checked(Q04_LAND=scope.YES)
        self.assertEqual(self.value(statement, "EUER.15.120"), "1")
        result = self.scope_results(statement, "C-43")
        self.assertEqual(result.severity, "warning")
        self.assertFalse(self.scope_results(statement, "C-40"))
        land = self.env["l10n_de_euer.asset"].create(
            {
                "name": "Grundstück",
                "company_id": self.company.id,
                "delivery_date": "2020-01-10",
                "cost_net": 50000.0,
                "asset_kind": "non_depreciable",
                "method": "none",
                "useful_life_months": 0,
                "aveuer_group": "land",
            }
        )
        land.action_activate()
        land.write(
            {
                "disposal_date": "2026-11-05",
                "disposal_kind": "withdrawal",
                "disposal_value": 60000,
            }
        )
        land.action_dispose()
        statement.action_compute()
        statement.action_check()
        self.assertFalse(self.scope_results(statement, "C-43"))
        statement.scope_answer_ids.filtered(
            lambda answer: answer.question_code == "Q04_LAND"
        ).answer = scope.NO
        # a changed answer counts again after the confirmation (R-52)
        self.assertFalse(self.value(statement, "EUER.15.120"))
        statement.action_confirm_scope()
        self.assertEqual(self.value(statement, "EUER.15.120"), "2")

    def test_ut_scope_05_luf(self):
        """Q05: agriculture and forestry, § 24 UStG."""
        self.assertOutOfScope("Q05_LUF", scope.YES)

    def test_ut_scope_06_iab(self):
        """Q06: investment deductions § 7g (1) to (4) EStG."""
        self.assertOutOfScope("Q06_IAB", scope.YES)

    def test_ut_scope_07_reserves(self):
        """Q07: reserves, compensating item, profit surcharge."""
        self.assertOutOfScope("Q07_RESERVES", scope.YES)

    def test_ut_scope_08_method_change(self):
        """Q08: change of the profit method (Kz G.250)."""
        self.assertOutOfScope("Q08_METHOD_CHANGE", scope.YES)

    def test_ut_scope_09_partnership(self):
        """Q09: shares in partnerships (Kz G.255)."""
        self.assertOutOfScope("Q09_PARTNERSHIP", scope.YES)

    def test_ut_scope_10_invstg_tev(self):
        """Q10: InvStG and partial income method (Kz 261 to 264)."""
        self.assertOutOfScope("Q10_INVSTG_TEV", scope.YES)

    def test_ut_scope_11_crypto(self):
        """Q11: crypto assets."""
        self.assertOutOfScope("Q11_CRYPTO", scope.YES)

    def test_ut_scope_12_tax_free(self):
        """Q12: tax-free income needs a profit correction of Kz 240 to 245,
        otherwise blocker C-44."""
        statement = self.checked(Q12_TAX_FREE=scope.YES)
        self.assertEqual(self.scope_results(statement, "C-44").severity, "blocker")
        self.correction(
            statement,
            "tax_free",
            amount=840.0,
            tax_free_field="EUER.G.240",
            reason="Volunteer allowance in the income",
        )
        statement.action_compute()
        statement.action_check()
        self.assertFalse(self.scope_results(statement, "C-44"))
        self.assertEqual(str(self.value(statement, "EUER.G.240")), "840.00")

    # ------------------------------------------------------------ GT-14
    def test_gt14_confirmation(self):
        """GT-14: unanswered or unconfirmed answers block (C-45); only a
        manager confirms; a changed answer needs a new confirmation."""
        statement = self.statement()
        statement.action_compute()
        statement.action_check()
        result = self.scope_results(statement, "C-45")
        self.assertEqual(result.severity, "blocker")
        self.assertEqual(len(result.res_ids), len(scope.QUESTIONS))
        self.answer(statement, confirm=False)
        bookkeeper = new_test_user(
            self.env,
            login="euer_scope_user",
            groups="account.group_account_user",
            company_id=self.company.id,
            company_ids=[Command.set(self.company.ids)],
        )
        with self.assertRaises(UserError):
            statement.with_user(bookkeeper).action_confirm_scope()
        statement.action_check()
        self.assertTrue(self.scope_results(statement, "C-45"))
        statement.action_confirm_scope()
        self.assertEqual(statement.scope_confirmed_by_id, self.env.user)
        self.assertTrue(all(statement.scope_answer_ids.mapped("confirmed_on")))
        statement.action_check()
        self.assertFalse(self.scope_results(statement, "C-45"))
        # the bookkeeper changes an answer: the confirmation is gone
        answer = statement.scope_answer_ids.filtered(
            lambda answer: answer.question_code == "Q11_CRYPTO"
        )
        answer.with_user(bookkeeper).answer = scope.YES
        self.assertFalse(answer.confirmed_on)
        self.assertFalse(statement.scope_confirmed_on)
        statement.action_check()
        self.assertTrue(self.scope_results(statement, "C-45"))
        with self.assertRaises(UserError):
            answer.with_user(bookkeeper).confirmed_on = "2026-01-01 00:00:00"
        with self.assertRaises(UserError):
            answer.unlink()

    def test_gt14_previous_year_is_only_a_proposal(self):
        """GT-14: the answers of the previous year are proposed, but they are
        no confirmation."""
        previous = self.statement(2025)
        self.answer(previous, Q04_LAND=scope.YES)
        statement = self.statement()
        answers = {
            answer.question_code: answer for answer in statement.scope_answer_ids
        }
        self.assertEqual(len(answers), len(scope.QUESTIONS))
        self.assertEqual(answers["Q04_LAND"].answer, scope.YES)
        self.assertEqual(answers["Q04_LAND"].proposal_source, "previous_year")
        self.assertFalse(any(statement.scope_answer_ids.mapped("confirmed_on")))
        statement.action_compute()
        statement.action_check()
        self.assertTrue(self.scope_results(statement, "C-45"))

    def test_questions_per_form_year(self):
        """The fields of each question exist in the forms 2024 to 2026; line
        and Kz come from the form year (Q12: lines 78–83, 2026 79–84)."""
        expected = {
            2024: "Line 78, 79, 80, 81, 82, 83",
            2026: "Line 79, 80, 81, 82, 83, 84",
        }
        for year in (2024, 2025, 2026):
            forms, _provisional = self.env["l10n_de_euer.form"]._get_forms_for_year(
                year
            )
            codes = set(forms.field_ids.mapped("code"))
            for question in scope.QUESTIONS:
                with self.subTest(year=year, question=question.code):
                    self.assertTrue(set(question.fields) <= codes)
            statement = self.statement(year)
            answers = {
                answer.question_code: answer for answer in statement.scope_answer_ids
            }
            self.assertEqual(answers["Q04_LAND"].form_reference, "Line 11 (Kz 120)")
            self.assertFalse(answers["Q11_CRYPTO"].form_reference)
            if year in expected:
                self.assertTrue(
                    answers["Q12_TAX_FREE"].form_reference.startswith(expected[year])
                )

    # ------------------------------------------------------------ heuristics
    def test_heuristic_fiscal_year(self):
        """R-52 b: a deviating fiscal year blocks, whatever the answers."""
        self.company.write({"fiscalyear_last_month": "6", "fiscalyear_last_day": 30})
        statement = self.checked()
        results = self.scope_results(statement, "C-40")
        self.assertEqual(len(results), 1)
        self.assertEqual(results.res_model, "res.company")

    def test_heuristic_income_type_luf(self):
        """R-52 b: income from agriculture and forestry in the settings
        (Kz 103 = 1) blocks, whatever the answers."""
        self.company.l10n_de_euer_income_type = "1"
        statement = self.checked()
        results = self.scope_results(statement, "C-40")
        self.assertEqual(len(results), 1)
        self.assertEqual(results.res_ids, self.company.ids)

    def test_heuristic_interest_on_debt(self):
        """Other interest on debt above 2,050 €: Anlage SZ (C-41), whatever
        the answers."""
        self.bank("2026-05-02", -2100, extra=[("interest", 2100)])
        statement = self.checked()
        self.assertIn("C-41", self.result_codes(statement, "blocker"))


class ScopeBookings(ScopeCommon):
    def test_heuristic_accounts_outside_scope(self):
        """R-52 b: bookings on an account of a category outside the program
        scope (reserves) block, whatever the answers."""
        self.entry("2026-06-30", [("reserve", 500), ("provision", -500)])
        statement = self.checked()
        results = self.scope_results(statement, "C-40")
        self.assertEqual(len(results), 1, results.mapped("message"))
        self.assertEqual(results.res_model, "account.account")
        self.assertEqual(results.res_ids, self.acc("reserve").ids)


@tagged("post_install_l10n", "post_install", "-at_install")
class TestScopeBookingsSkr03(ScopeBookings, AccountTestInvoicingCommon):
    allow_inherited_tests_method = True
    CHART = "de_skr03"
    ACCOUNTS = {**SKR03_ACCOUNTS, "reserve": "account_2342"}
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_statement()


@tagged("post_install_l10n", "post_install", "-at_install")
class TestScopeBookingsSkr04(ScopeBookings, AccountTestInvoicingCommon):
    allow_inherited_tests_method = True
    CHART = "de_skr04"
    ACCOUNTS = {**SKR04_ACCOUNTS, "reserve": "chart_skr04_6922"}
    TAXES = SKR04_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr04")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_statement()
