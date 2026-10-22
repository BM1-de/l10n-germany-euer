# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Bank reconciliation of 31 December and lock dates (G-17, G-05).

GT-15 without the part after the finalisation: a missing or different
balance per statement of account is C-38, an active exception of the lock
date C-39, only a hard lock date the warning C-70 (no lock date: blocker).
Synthetic cases (D6).
"""

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from .scenario_common import SKR03_ACCOUNTS, SKR03_TAXES
from .statement_common import StatementCommon


@tagged("post_install_l10n", "post_install", "-at_install")
class TestCashBalance(StatementCommon, AccountTestInvoicingCommon):
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_statement()

    def computed(self):
        sale = self.sale("2026-03-10", 1000)
        self.bank("2026-03-20", 1000, matches=[(sale, 1000)])
        self.bank("2027-01-05", 300, extra=[("ku_revenue", -300)])
        statement = self.statement()
        statement.action_compute()
        self.answer(statement)
        return statement

    def c38(self, statement):
        return self.results(statement, "C-38")

    # ------------------------------------------------------------ C-38
    def test_gt15_balance_missing_or_different(self):
        """GT-15: no balance per statement → C-38; a different balance →
        C-38; the same balance → none. Lines after 31 December do not
        count."""
        statement = self.computed()
        balance = statement.cash_balance_ids.filtered(
            lambda line: line.journal_id == self.bank_journal
        )
        self.assertEqual(balance.balance_odoo, 1000.0)
        self.assertFalse(balance.entered_on)
        statement.action_check()
        result = self.c38(statement)
        self.assertEqual(result.severity, "blocker")
        self.assertEqual(result.res_ids, balance.ids)
        balance.balance_statement = 990.0
        self.assertEqual(balance.difference, -10.0)
        self.assertEqual(balance.entered_by_id, self.env.user)
        statement.action_check()
        self.assertEqual(self.c38(statement).amount, -10.0)
        balance.balance_statement = 1000.0
        statement.action_check()
        self.assertFalse(self.c38(statement))

    def test_balance_zero_must_be_entered(self):
        """A balance of 0.00 counts only once it is entered."""
        statement = self.computed()
        self.bank("2026-05-02", -1000, extra=[("office", 1000)])
        statement.action_compute()
        balance = statement.cash_balance_ids.filtered(
            lambda line: line.journal_id == self.bank_journal
        )
        self.assertEqual(balance.balance_odoo, 0.0)
        statement.action_check()
        self.assertTrue(self.c38(statement))
        balance.balance_statement = 0.0
        statement.action_check()
        self.assertFalse(self.c38(statement))

    def test_journals_of_the_reconciliation(self):
        """Every money journal with lines up to 31 December, also an archived
        one; not the journal of a private account (R-18), not a journal
        without lines."""
        second = self.env["account.journal"].create(
            {"name": "Second Bank", "type": "bank", "code": "BNK9"}
        )
        self.bank("2026-02-01", 50, extra=[("ku_revenue", -50)], journal=second)
        second.active = False
        empty = self.env["account.journal"].create(
            {"name": "Empty Bank", "type": "bank", "code": "BNK8"}
        )
        statement = self.computed()
        journals = statement.cash_balance_ids.journal_id
        self.assertIn(second, journals)
        self.assertNotIn(empty, journals)
        self.reconcile_banks(statement)
        statement.action_check()
        self.assertFalse(self.c38(statement))
        self.bank_journal.l10n_de_euer_private_account = True
        self.assertNotIn(self.bank_journal, statement._get_money_journals())

    def test_reconciliation_rights_and_protection(self):
        statement = self.computed()
        balance = statement.cash_balance_ids[:1]
        with self.assertRaises(UserError):
            balance.journal_id = self.cash_journal
        other = self.setup_other_company(name="Other Business")
        with self.assertRaises(ValidationError):
            self.env["l10n_de_euer.cash.balance"].create(
                {
                    "statement_id": statement.id,
                    "journal_id": other["default_journal_bank"].id,
                }
            )

    # ------------------------------------------------------------ C-39
    def test_gt15_lock_date_exception(self):
        """GT-15: an active exception of the lock date is the warning C-39."""
        statement = self.computed()
        self.company.fiscalyear_lock_date = "2026-12-31"
        self.env["account.lock_exception"].create(
            {
                "company_id": self.company.id,
                "fiscalyear_lock_date": "2025-12-31",
                "reason": "late bank statement",
            }
        )
        statement.action_check()
        self.assertEqual(self.results(statement, "C-39").severity, "warning")

    # ------------------------------------------------------------ C-70
    def test_gt15_lock_dates(self):
        """No lock date at 31 December: blocker; only the hard lock date: a
        warning; the global lock date: nothing."""
        statement = self.computed()
        statement.action_check()
        self.assertEqual(self.results(statement, "C-70").severity, "blocker")
        self.company.hard_lock_date = "2026-12-31"
        statement.action_check()
        self.assertEqual(self.results(statement, "C-70").severity, "warning")
        self.company.fiscalyear_lock_date = "2026-12-31"
        statement.action_check()
        self.assertFalse(self.results(statement, "C-70"))

    def test_lock_date_of_the_main_company(self):
        """A branch that is a business of its own is locked by the lock date
        of its main company, too (Odoo applies the parents' lock dates)."""
        own = self.env["res.company"].create(
            {
                "name": "Own Business",
                "parent_id": self.company.id,
                "l10n_de_euer_separate_business": True,
            }
        )
        self.env.user.company_ids |= own
        statement = self.statement(company=own)
        statement.action_compute()
        statement.action_check()
        self.assertTrue(self.results(statement, "C-70"))
        self.company.fiscalyear_lock_date = "2026-12-31"
        statement.action_check()
        self.assertFalse(self.results(statement, "C-70"))
