# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Paths of the models without a test before the release (stage E5, O-06).

Messages of every finding of the calculation, the lock date exceptions of
C-39, the rules of the classification of unallocated rests, the wizards of
the account review and of the computation, VAT status periods, check
results and slices, events of assets and asset pools. Synthetic data (D6).
"""

from types import SimpleNamespace

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from ..models.engine import model as m
from ..models.euer_cash_slice import INTERNAL as SLICE_INTERNAL
from ..models.euer_statement import INTERNAL as STATEMENT_INTERNAL
from ..models.euer_ten_day_item import INTERNAL as TEN_DAY_INTERNAL
from .common import AssetCase
from .scenario_common import SKR03_ACCOUNTS, SKR03_TAXES, ScenarioCommon
from .statement_common import StatementCommon

KU = "EUER.20.111"

#: every key of a finding of the engine with its own message
FINDING_KEYS = (
    "direct_on_payment",
    "direct_on_payment_fields",
    "reclassification_other_year",
    "direct_without_rule",
    "unclear",
    "money_reconciliation",
    "document_completeness",
    "transit_over_realised",
    "bank_date_differs",
    "draft_entry",
    "partial_with_draft",
    "neutral_account_with_expense",
    "exchange_difference_missing",
    "unallocated_open_item",
    "unallocated_refund",
    "partial_other_business",
    "lock_date_shift",
    "reversal_pair_years",
    "writeoff_netted",
    "unallocated_outstanding",
    "neutralized_private",
    "late_reconciliation",
    "payment_on_bank_account_rest",
    "writeoff_unplaced",
    "unallocated_pair",
    "ten_day_input_missing",
    "unallocated_other",
    "cycle",
    "transit_both_sides",
    "transfer_order_date",
    "payment_on_bank_account",
    "neutralized",
    "ten_day_undecided",
    "direct_debit_undecided",
    "payment_not_matched",
    "revaluation",
    "booking_category_in_document",
    "input_vat_without_base",
    "ku_limit_near",
    "ku_limit_previous_year",
    "ku_previous_year_unknown",
    "ku_free_supply_not_counted",
    "ku_limit_forecast_exceeded",
    "ku_limit_exceeded_period_set",
    "ku_limit_exceeded",
    "tax_rate_missing_limit",
    "tax_rate_missing",
    "tax_line_without_repartition",
    "opening_item_missing",
    "money_reaches_writeoff",
    "money_to_money_years",
    "opening_after_money",
    "several_openings",
    "gift_without_recipient",
    "bulk_gift_unit_price",
    "gift_input_vat",
    "entertainment_ratio",
    "private_car_twice",
    "no_category",
)


@tagged("post_install_l10n", "post_install", "-at_install")
class TestReleaseCoverage(ScenarioCommon, AccountTestInvoicingCommon):
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_euer()
        cls.Checker = cls.env["l10n_de_euer.calculation.checker"]

    # ------------------------------------------------------------ messages
    def test_every_finding_key_has_its_message(self):
        """Every key of a finding of the engine has a message of its own in
        the language of the user, never the generic ``code: records``; an
        unknown key falls back to it."""
        invoice = self.sale("2026-03-15", 100)
        line = self.open_line(invoice)
        currency = self.company.currency_id
        messages = {}
        for key in FINDING_KEYS:
            finding = m.Finding(code="C-99", severity="info", key=key, amount=1234)
            message = self.Checker._message(
                finding, [finding], 2026, [invoice.id], [line.id], currency
            )
            self.assertTrue(message, key)
            self.assertFalse(message.startswith("C-99:"), key)
            messages[key] = message
        # the messages differ from each other
        self.assertEqual(len(set(messages.values())), len(FINDING_KEYS))
        unknown = m.Finding(code="C-99", severity="info", key="unknown")
        self.assertEqual(
            self.Checker._message(unknown, [unknown], 2026, [invoice.id], [], None),
            f"C-99: {invoice.display_name}",
        )

    def test_amount_text_and_one_result_per_record(self):
        """Amounts without currency keep two places; the same single finding
        twice on the same record gives one check result."""
        self.assertEqual(self.Checker._amount_text(1234), "12.34")
        self.assertEqual(self.Checker._amount_text(None), "0.00")
        invoice = self.sale("2026-03-15", 100)
        finding = m.Finding(
            code="C-03",
            severity="info",
            key="direct_on_payment",
            move_ids=(invoice.id,),
        )
        twin = m.Finding(
            code="C-03",
            severity="info",
            key="direct_on_payment",
            move_ids=(invoice.id,),
        )
        results = self.Checker._engine_results(
            self.company, 2026, SimpleNamespace(findings=[finding, twin])
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["res_ids"], [invoice.id])
        # without a run the checks of the run itself are left out
        stored = self.Checker._store(
            self.company, 2026, SimpleNamespace(findings=[finding])
        )
        self.assertEqual([result["code"] for result in stored], ["C-03"])

    # ------------------------------------------------------------ C-39
    def test_c39_tax_lock_date_and_earlier_exceptions(self):
        """GT-15: an exception that opened an earlier year does not open the
        year; one of the tax lock date opens it when no fiscal year lock date
        covers it, unless the hard lock date does."""
        Exception_ = self.env["account.lock_exception"]
        self.company.fiscalyear_lock_date = "2024-12-31"
        Exception_.create(
            {
                "company_id": self.company.id,
                "fiscalyear_lock_date": "2023-12-31",
                "reason": "late bank statement 2024",
            }
        )
        self.assertFalse(self.Checker._check_c39(self.company, 2026))
        self.company.tax_lock_date = "2026-12-31"
        exception = Exception_.create(
            {
                "company_id": self.company.id,
                "tax_lock_date": "2025-12-31",
                "reason": "VAT correction",
            }
        )
        results = self.Checker._check_c39(self.company, 2026)
        self.assertEqual([result["code"] for result in results], ["C-39"])
        self.assertEqual(results[0]["res_ids"], exception.ids)
        self.company.hard_lock_date = "2026-12-31"
        self.assertFalse(self.Checker._check_c39(self.company, 2026))

    # ------------------------------------------------------------ C-20 ...
    def test_residual_classification_rules(self):
        """Spec 5.7 no. 2: a realised kind needs a category of its role, a
        neutral kind a reason, the rate is a percentage; who decided and when
        is set by the record, also when a decision changes."""
        Classification = self.env["l10n_de_euer.residual.classification"]
        Category = self.env["l10n_de_euer.category"]
        income = Category.search([("role", "=", "be")], limit=1)
        expense = Category.search([("role", "=", "ba")], limit=1)
        invoice = self.sale("2026-03-15", 100)
        line = self.open_line(invoice)
        other = (invoice.line_ids - line)[:1]
        with self.assertRaises(UserError):
            Classification.create({"kind": "loan", "reason": "Loan of a friend"})
        values = {"source_line_id": line.id, "company_id": self.company.id}
        with self.assertRaises(ValidationError):
            Classification.create(dict(values, kind="revenue"))
        with self.assertRaises(ValidationError):
            Classification.create(dict(values, kind="expense", category_id=income.id))
        with self.assertRaises(ValidationError):
            Classification.create(dict(values, kind="loan"))
        with self.assertRaises(ValidationError):
            Classification.create(
                dict(values, kind="revenue", category_id=income.id, tax_rate=119)
            )
        item = Classification.create(
            dict(values, kind="revenue", category_id=income.id, tax_rate=19)
        )
        self.assertEqual(item.decided_by_id, self.env.user)
        self.assertEqual(item.source_line_ref, line.id)
        self.assertEqual(item.source_line_name, line.display_name)
        with self.assertRaises(UserError):
            item.write({"source_line_id": False})
        other_user = self.env["res.users"].search([("id", "!=", self.env.uid)], limit=1)
        item.write(
            {
                "kind": "expense_refund",
                "category_id": expense.id,
                "decided_by_id": other_user.id,
                "source_line_name": "changed by hand",
            }
        )
        self.assertEqual(item.decided_by_id, self.env.user)
        self.assertEqual(item.source_line_name, line.display_name)
        item.write({"source_line_id": other.id})
        self.assertEqual(
            (item.source_line_ref, item.source_line_name),
            (other.id, other.display_name),
        )
        self.assertEqual(
            Classification.search([("obsolete", "=", False), ("id", "=", item.id)]),
            item,
        )
        self.assertFalse(
            Classification.search([("obsolete", "!=", False), ("id", "=", item.id)])
        )
        self.assertIs(Classification._search_obsolete("in", [True]), NotImplemented)

    # ------------------------------------------------------------ wizards
    def test_review_and_compute_wizards(self):
        """The wizards check the year; the computation can leave out the
        checks of the account assignment and of the asset register; the
        account review confirms and lists the journal items of an account."""
        Review = self.env["l10n_de_euer.account.review"]
        Compute = self.env["l10n_de_euer.compute.year"]
        with self.assertRaises(UserError):
            Review.create({"company_id": self.company.id, "year": 1800})
        with self.assertRaises(UserError):
            Compute.create({"company_id": self.company.id, "year": 1800})
        invoice = self.sale("2026-03-15", 100)
        self.bank("2026-03-20", 100, matches=[(invoice, 100)])
        today = fields.Date.context_today(Review)
        review = Review.create({"company_id": self.company.id})
        self.assertEqual(review.year, today.year)
        review.year = 2026
        review.action_review()
        line = review.line_ids.filtered(
            lambda item: item.account_id == self.acc("ku_revenue")
        )
        self.assertEqual(line.action_confirm()["tag"], "soft_reload")
        action = review.line_ids.filtered(
            lambda item: item.account_id == self.acc("ku_revenue")
        ).action_open_move_lines()
        self.assertEqual(action["res_model"], "account.move.line")
        lines = self.env["account.move.line"].search(action["domain"])
        self.assertEqual(lines.account_id, self.acc("ku_revenue"))
        wizard = Compute.create(
            {"run_assignment_checks": False, "run_asset_checks": False}
        )
        self.assertEqual(wizard.company_id, self.env.company)
        # by default the year before the current one
        self.assertEqual(wizard.year, today.year - 1)
        wizard.year = 2026
        run = self.env["l10n_de_euer.run"].browse(wizard.action_compute()["res_id"])
        self.assertEqual(run.year_to, 2026)
        origins = set(
            self.env["l10n_de_euer.check"]
            .search([("company_id", "=", self.company.id), ("year", "=", 2026)])
            .mapped("origin")
        )
        self.assertNotIn("assignment", origins)
        self.assertNotIn("asset", origins)

    # ------------------------------------------------------------ C-23
    def test_ten_day_item_proposed_date(self):
        """C-23: the handover date of a payment order becomes the cash date
        of the payment; only a transfer item with a proposed date has one; a
        decision needs a reason."""
        Item = self.env["l10n_de_euer.ten_day.item"]
        bill = self.purchase("2025-12-20", 300)
        payment = self.bank("2026-01-04", -300, matches=[(bill, 300)])
        internal = Item.sudo().with_context(**{TEN_DAY_INTERNAL: True})
        transfer = internal.create(
            {
                "company_id": self.company.id,
                "year": 2025,
                "kind": "transfer_order",
                "source_move_id": payment.move_id.id,
                "origin_move_id": bill.id,
                "cash_date": "2026-01-04",
                "amount": 300.0,
                "proposed_cash_date": "2025-12-30",
            }
        )
        recurring = internal.create(
            {
                "company_id": self.company.id,
                "year": 2025,
                "kind": "recurring",
                "source_move_id": payment.move_id.id,
                "origin_move_id": bill.id,
                "cash_date": "2026-01-04",
                "amount": 300.0,
            }
        )
        transfer = Item.browse(transfer.id)
        recurring = Item.browse(recurring.id)
        self.assertEqual(transfer.partner_id, self.supplier)
        self.assertEqual(recurring.partner_id, self.supplier)
        with self.assertRaises(ValidationError):
            recurring._write_internal({"decision": "apply"})
        with self.assertRaises(UserError):
            recurring.action_use_proposed_date()
        transfer.action_use_proposed_date()
        self.assertEqual(transfer.decision, "apply")
        self.assertTrue(transfer.reason)
        self.assertEqual(str(payment.move_id.l10n_de_euer_cash_date), "2025-12-30")

    # ------------------------------------------------------------ categories
    def test_category_assignments_and_mapping_names(self):
        """A category counts and opens its account assignments; a mapping is
        named by category and field."""
        mapping = self.env["l10n_de_euer.category.mapping"].search(
            [("category_id.role", "=", "ba")], limit=1
        )
        category = mapping.category_id
        count = self.env["l10n_de_euer.account.assignment"].search_count(
            [("category_id", "=", category.id)]
        )
        self.assertEqual(category.assignment_count, count)
        action = category.action_open_assignments()
        self.assertEqual(action["domain"], [("category_id", "=", category.id)])
        self.assertEqual(action["context"], {"default_category_id": category.id})
        self.assertEqual(
            mapping.display_name,
            f"{category.display_name} → {mapping.field_id.code}",
        )

    # ------------------------------------------------------------ K-01
    def test_assignment_periods_and_names(self):
        """A new period of an account ends the open one the day before; the
        name shows the period; withdrawing the confirmation clears who
        confirmed; assignments belong to the main company, not a branch."""
        Assignment = self.env["l10n_de_euer.account.assignment"]
        account = self.acc("office")
        current = Assignment.search(
            [("company_id", "=", self.company.id), ("account_id", "=", account.id)]
        )
        self.assertEqual(len(current), 1)
        self.assertNotIn(" – ", current.display_name)
        work = self.category("EXP_WORK_EQUIPMENT")
        new = Assignment.create(
            {
                "company_id": self.company.id,
                "account_id": account.id,
                "category_id": work.id,
                "date_from": "2027-01-01",
            }
        )
        self.assertEqual(str(current.date_to), "2026-12-31")
        self.assertTrue(current.display_name.endswith("2026-12-31)"))
        self.assertTrue(new.display_name.endswith("(2027-01-01 – …)"))
        self.assertTrue(new.confirmed_by_id)
        new.write({"confirmed": False})
        self.assertFalse(new.confirmed_by_id or new.confirmed_on)
        action = new.action_new_period()
        self.assertEqual(action["context"]["default_account_id"], account.id)
        branch = self.env["res.company"].create(
            {"name": "Branch with assignment", "parent_id": self.company.id}
        )
        self.env.user.company_ids |= branch
        with self.assertRaises(ValidationError):
            Assignment.create(
                {
                    "company_id": branch.id,
                    "account_id": account.id,
                    "category_id": work.id,
                    "date_from": "2028-01-01",
                }
            )

    # ------------------------------------------------------------ periods
    def test_vat_period_name_and_overlap(self):
        Period = self.env["l10n_de_euer.vat.period"]
        period = Period.search([("company_id", "=", self.company.id)])
        self.assertIn("2020-01-01 – …", period.display_name)
        with self.assertRaises(ValidationError):
            Period.create(
                {
                    "company_id": self.company.id,
                    "date_from": "2025-01-01",
                    "regime": "regular",
                    "vat_return_period": "monthly",
                }
            )
        # nothing to recompute without a business
        self.assertIsNone(
            Period.browse()._recompute_move_regimes(self.env["res.company"])
        )

    # ------------------------------------------------------------ checks
    def test_check_actions_and_protection(self):
        """A result without records opens nothing; C-35 opens the takeover
        of the open item, C-32/C-33 the EÜR information of the document;
        users cannot delete results or change slices."""
        Check = self.env["l10n_de_euer.check"]
        self.assertFalse(Check.new({"code": "C-99"}).action_open_records())
        invoice = self.sale("2026-03-15", 100)
        line = self.open_line(invoice)
        action = Check.new(
            {
                "code": "C-35",
                "company_id": self.company.id,
                "res_model": "account.move.line",
                "res_ids": [line.id],
            }
        ).action_resolve()
        self.assertEqual(action["res_model"], "l10n_de_euer.opening.item")
        self.assertEqual(action["context"]["default_move_line_id"], line.id)
        action = Check.new(
            {
                "code": "C-33",
                "company_id": self.company.id,
                "res_model": "account.move",
                "res_ids": [invoice.id],
            }
        ).action_resolve()
        self.assertEqual(action, invoice.action_l10n_de_euer_info())
        self.bank("2026-03-20", 100, matches=[(invoice, 100)])
        self.compute(2026)
        check = Check.search(
            [("company_id", "=", self.company.id), ("year", "=", 2026)], limit=1
        )
        self.assertTrue(check)
        with self.assertRaises(UserError):
            check.unlink()
        slices = self.env["l10n_de_euer.cash.slice"].search(
            [("company_id", "=", self.company.id), ("attribution_year", "=", 2026)]
        )
        self.assertTrue(slices)
        one = slices[:1]
        self.assertEqual(
            one.path_text, " → ".join(str(item) for item in one.path or ())
        )
        with self.assertRaises(UserError):
            one.unlink()
        # the calculation itself may write its slices
        internal = one.sudo().with_context(**{SLICE_INTERNAL: True})
        self.assertTrue(internal.write({"partner_id": one.partner_id.id}))


@tagged("post_install_l10n", "post_install", "-at_install")
class TestReleaseAssetCoverage(AssetCase):
    """Events and asset pools (spec 6.2, 6.3)."""

    def additions(self, asset, year):
        return asset.year_ids.filtered(lambda item: item.year == year).additions

    def test_event_change_and_removal(self):
        """Changing or removing an event computes the asset again and logs
        it on the asset (G-06)."""
        asset = self.asset(delivery_date="2026-01-10")
        Event = self.env["l10n_de_euer.asset.event"]
        event = Event.create(
            {
                "asset_id": asset.id,
                "type": "additional_cost",
                "date": "2026-06-01",
                "amount": 120.0,
            }
        )
        first = self.additions(asset, 2026)
        event.write({"amount": 240.0})
        self.assertEqual(self.additions(asset, 2026) - first, 120.0)
        self.assertIn("Event changed", str(asset.message_ids[:1].body))
        event.unlink()
        self.assertEqual(first - self.additions(asset, 2026), 120.0)
        self.assertIn("Event removed", str(asset.message_ids[:1].body))

    def test_event_reason_and_business_share(self):
        asset = self.asset(delivery_date="2026-01-10")
        Event = self.env["l10n_de_euer.asset.event"]
        with self.assertRaises(ValidationError):
            Event.create(
                {
                    "asset_id": asset.id,
                    "type": "extraordinary",
                    "date": "2026-06-01",
                    "amount": 100.0,
                }
            )
        with self.assertRaises(ValidationError):
            Event.create(
                {
                    "asset_id": asset.id,
                    "type": "business_use_change",
                    "date": "2026-06-01",
                    "business_share": 1.5,
                    "reason": "More private use",
                }
            )

    def test_pool_releases_and_protection(self):
        """AT-POOL: 500 + 700 in 2026 are released by 240 a year until 2030;
        a pool with assets cannot be deleted, a locked pool keeps its amount
        and does not change."""
        first = self.asset(
            asset_kind="pool", method="none", cost_net=500.0, delivery_date="2026-02-01"
        )
        self.asset(
            asset_kind="pool", method="none", cost_net=700.0, delivery_date="2026-11-30"
        )
        pool = first.pool_id
        self.assertEqual(
            (pool.release_amount, pool.last_release_amount, pool.end_year),
            (240.0, 240.0, 2030),
        )
        html = str(pool.schedule_html)
        self.assertIn("<caption>", html)
        for year in range(2026, 2031):
            self.assertIn(f"<th scope='row'>{year}</th>", html)
        self.assertIn("<td class='text-end'>960.00</td>", html)
        with self.assertRaises(UserError):
            pool.sudo().unlink()
        Pool = self.env["l10n_de_euer.asset.pool"]
        empty = Pool._create_internal({"year": 2031})
        self.assertEqual(empty.company_id, self.company._l10n_de_euer_business_root())
        empty.sudo().unlink()
        self.assertFalse(empty.exists())
        pool._write_internal({"locked": True, "locked_amount": pool.amount})
        pool._check_amount_unchanged()
        with self.assertRaises(UserError):
            pool._write_internal({"year": 2027})
        self.assertEqual(pool.year, 2026)


@tagged("post_install_l10n", "post_install", "-at_install")
class TestReleaseStatementCoverage(StatementCommon, AccountTestInvoicingCommon):
    """Rules of the statement of the year (spec 7.3, 7.7, R-02)."""

    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_statement()

    def test_one_statement_per_business_and_year(self):
        """R-02: a valid year and one statement per business and year (also
        when created for a branch, which belongs to its main company); a
        correction needs a reason."""
        with self.assertRaises(ValidationError):
            self.statement(year=1800)
        statement = self.statement()
        with self.assertRaises(UserError):
            self.statement()
        branch = self.env["res.company"].create(
            {"name": "Branch of the business", "parent_id": self.company.id}
        )
        self.env.user.company_ids |= branch
        with self.assertRaises(UserError):
            self.statement(company=branch)
        internal = self.Statement.sudo().with_context(**{STATEMENT_INTERNAL: True})
        with self.assertRaises(ValidationError):
            internal.create(
                {
                    "company_id": self.company.id,
                    "year": 2026,
                    "correction_of_id": statement.id,
                    "version": 2,
                    "correction_reason": " ",
                }
            )

    def test_statement_and_values_are_set_by_actions(self):
        """Business and year never change; state and values are set by the
        actions; a value is only overridden, never a header value; values
        are neither created nor deleted by a user."""
        sale = self.sale("2026-03-10", 1000)
        self.bank("2026-03-20", 1000, matches=[(sale, 1000)])
        statement = self.statement()
        self.answer(statement)
        statement.action_compute()
        with self.assertRaises(UserError):
            statement.write({"year": 2025})
        with self.assertRaises(UserError):
            statement.write({"state": "draft"})
        statement.write({"submission_reference": "ticket-0001"})
        self.assertEqual(statement.submission_reference, "ticket-0001")
        line = statement.euer_line_ids.filtered(lambda item: item.code == KU)
        header = statement.euer_line_ids.filtered(
            lambda item: item.field_type in ("text", "key")
        )[:1]
        self.assertTrue(line and header)
        with self.assertRaises(UserError):
            line.write({"source": "manual"})
        with self.assertRaises(UserError):
            header.write({"source": "override", "override_amount": 1.0})
        with self.assertRaises(UserError):
            self.env["l10n_de_euer.statement.line"].create(
                {"statement_id": statement.id, "code": KU}
            )
        with self.assertRaises(UserError):
            line.unlink()
