# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Findings of the review of stage E4 (spec 5.8, 7.3 to 7.12, 9.7).

Profit corrections: no VAT for small businesses (R-28), the total turnover
of the small business limit with taxable free supplies only (R-27), yearly
maximums over all corrections of a year (R-44), income in kind, C-55 only
so far as the home office is deducted, the maximum of Kz 240 and the cross
checks of the questionnaire (R-52). Finalisation: no record moves into a
finalised statement (G-04, GT-05), internal context keys need the
superuser, the asset register of another business stays invisible (7.9,
GT-02), changed receipts are found (GT-04), referenced partners and users
stay. Outputs: titles and sections in the PDF of the DIN 5008 layout, the
finalised account assignment, accessible HTML (O-11). Synthetic cases (D6).
"""

import io
import zipfile

from lxml import html
from psycopg2 import IntegrityError

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.service.model import call_kw
from odoo.tests import new_test_user, tagged
from odoo.tools import mute_logger

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from ..models.engine import model as m
from ..models.engine import scope
from ..models.euer_form import NON_AMOUNT_TYPES
from .scenario_common import (
    SKR03_ACCOUNTS,
    SKR03_TAXES,
    SKR04_ACCOUNTS,
    SKR04_TAXES,
)
from .statement_common import D, StatementCommon

KU = "EUER.20.111"
PRIVATE_CAR = "EUER.20.106"
IN_KIND = "EUER.20.108"
OUTPUT_VAT = "EUER.20.140"
DAYS = "EUER.25.163"
COMMUTING = "EUER.25.176"
LAND = "EUER.15.120"
#: the internal context keys of the module (they need ``sudo()`` as well)
INTERNAL = {
    "l10n_de_euer_statement_internal": True,
    "l10n_de_euer_slice_internal": True,
    "l10n_de_euer_asset_year_internal": True,
    "l10n_de_euer_check_internal": True,
    "l10n_de_euer_ten_day_internal": True,
}


class ReviewCommon(StatementCommon):
    def year(self, amount=1000, year=2026):
        sale = self.sale(f"{year}-03-10", amount)
        self.bank(f"{year}-03-20", amount, matches=[(sale, amount)])
        return sale

    def finalized(self):
        self.year()
        statement = self.statement()
        self.correction(statement, "meals", days_8h=1)
        return self.finalize(statement)

    def engine_result(self, statement):
        return (
            self.env["l10n_de_euer.run"]
            .with_context(l10n_de_euer_statement_id=statement.id)
            ._engine_result(statement.company_id, statement.year)
        )

    def limit_keys(self, result):
        return sorted((f.key, f.severity) for f in result.findings if f.code == "C-32")

    def asset(self, **values):
        asset = self.env["l10n_de_euer.asset"].create(
            {
                "name": "Kamera",
                "company_id": self.company.id,
                "delivery_date": "2026-01-10",
                "cost_net": 1200.0,
                "method": "linear",
                "useful_life_months": 36,
                "aveuer_group": "other_movable",
                **values,
            }
        )
        asset.action_activate()
        asset.action_compute_years()
        return asset

    def make_user(self, login, groups, companies=None):
        companies = companies or self.company
        return new_test_user(
            self.env,
            login=login,
            groups=groups,
            company_id=companies[:1].id,
            company_ids=[Command.set(companies.ids)],
        )


class CorrectionReviewScenarios(ReviewCommon):
    # ------------------------------------------------------------ R-28
    def test_small_business_has_no_vat_on_corrections(self):
        """R-28, § 19 (1) UStG: a small business enters no VAT on a
        withdrawal and no VAT on free supplies; Kz 140 stays empty. Under
        standard taxation the VAT needs its base; a later small business
        period stops the computation."""
        statement = self.statement()
        with self.assertRaises(ValidationError):
            self.correction(
                statement, "withdrawal_in_kind", amount=500.0, vat_amount=95.0
            )
        with self.assertRaises(ValidationError):
            self.correction(statement, "withdrawal_vat", amount=19.0)
        correction = self.correction(statement, "withdrawal_in_kind", amount=500.0)
        self.assertFalse(correction.vat_allowed)
        statement.action_compute()
        self.assertEqual(self.value(statement, IN_KIND), D("500.00"))
        self.assertIsNone(self.value(statement, OUTPUT_VAT))
        self.set_regime("regular")
        correction.invalidate_recordset(["vat_allowed"])
        self.assertTrue(correction.vat_allowed)
        with self.assertRaises(ValidationError):
            correction.vat_amount = 95.0
        correction.write({"vat_amount": 95.0, "vat_base": 500.0})
        statement.action_compute()
        self.assertEqual(self.value(statement, OUTPUT_VAT), D("95.00"))
        self.set_regime("ku")
        with self.assertRaises(UserError):
            statement.action_compute()

    # ------------------------------------------------------------ R-27
    def test_turnover_counts_taxable_free_supplies_only(self):
        """R-27, UStAE 19.2 (2): 99,500 € received and the use withdrawal of
        a camera bought without input tax deduction stay below the limit
        (warning at 80 %); with input tax deduction (VAT base 600 €) the
        limit is exceeded (C-32 blocker). The turnover of 2025 is entered
        (0 €, no bookings of 2025): only the current year is checked."""
        self.previous_turnover(2026)
        self.year(99500)
        statement = self.statement()
        correction = self.correction(
            statement, "withdrawal_in_kind", amount=600.0, reason="Camera, private"
        )
        result = self.engine_result(statement)
        self.assertEqual(result.turnover.amount, 9950000)
        self.assertEqual(self.limit_keys(result), [("ku_limit_near", m.WARNING)])
        correction.vat_base = 600.0
        self.assertEqual(correction.turnover_amount, 600.0)
        result = self.engine_result(statement)
        self.assertEqual(result.turnover.amount, 10010000)
        self.assertEqual(self.limit_keys(result), [("ku_limit_exceeded", m.BLOCKER)])

    def test_turnover_of_the_previous_year(self):
        """The check of 2026 counts the taxable free supplies of 2025 in the
        turnover of the previous year, like the check of 2025 did."""
        self.year(24900, year=2025)
        statement = self.statement()
        self.assertEqual(self.limit_keys(self.engine_result(statement)), [])
        previous = self.statement(2025)
        self.correction(previous, "withdrawal_in_kind", amount=300.0, vat_base=200.0)
        amounts = self.env["l10n_de_euer.adapter"]._turnover_amounts(self.company, 2026)
        self.assertEqual(
            [(one.year, one.amount, one.source) for one in amounts],
            [(2025, 20000, "adjustment")],
        )
        self.assertEqual(self.engine_result(previous).turnover.amount, 2510000)
        self.assertEqual(
            self.limit_keys(self.engine_result(statement)),
            [("ku_limit_previous_year", m.WARNING)],
        )

    def test_register_use_withdrawal_base(self):
        """R-54 with R-27: an asset bought under standard taxation with input
        tax deduction and used privately in a small business year: no VAT,
        but the VAT base (cost spread over the § 15a period, 400 × 20 % = 80)
        counts in the total turnover; an asset bought as small business has
        no base."""
        Period = self.env["l10n_de_euer.vat.period"]
        Period.search([("company_id", "=", self.company.id)]).unlink()
        Period.create(
            {
                "company_id": self.company.id,
                "date_from": "2020-01-01",
                "date_to": "2025-12-31",
                "regime": "regular",
                "vat_return_period": "monthly",
            }
        )
        Period.create(
            {
                "company_id": self.company.id,
                "date_from": "2026-01-01",
                "regime": "ku",
                "vat_return_period": "monthly",
                "reason": "Small business from 2026",
            }
        )
        camera = self.asset(
            delivery_date="2025-01-10", input_tax=228.0, business_share=0.8
        )
        self.assertEqual(camera.vat_treatment, "deductible")
        years = {line.year: line for line in camera.year_ids}
        self.assertEqual(years[2025].private_use_vat, 15.2)
        self.assertEqual(years[2025].private_use_vat_base, 80.0)
        self.assertEqual(years[2026].private_use_amount, 80.0)
        self.assertEqual(years[2026].private_use_vat, 0.0)
        self.assertEqual(years[2026].private_use_vat_base, 80.0)
        lens = self.asset(
            name="Objektiv",
            delivery_date="2026-02-01",
            cost_net=600.0,
            business_share=0.5,
        )
        self.assertEqual(lens.vat_treatment, "not_deductible")
        lens_2026 = lens.year_ids.filtered(lambda line: line.year == 2026)
        self.assertTrue(lens_2026.private_use_amount)
        self.assertFalse(lens_2026.private_use_vat_base)
        lens_2026.action_confirm_private_use()
        Adapter = self.env["l10n_de_euer.adapter"]
        self.assertEqual(Adapter._turnover_amounts(self.company, 2026), [])
        years[2026].action_confirm_private_use()
        self.assertEqual(
            [
                (one.amount, one.source)
                for one in Adapter._turnover_amounts(self.company, 2026)
            ],
            [(8000, "asset")],
        )

    def test_register_proposal_taken_over_with_base(self):
        """The take-over of a proposal of the register copies its VAT base;
        the correction then counts instead of the register."""
        self.set_regime("regular")
        camera = self.asset(input_tax=228.0, business_share=0.8)
        line = camera.year_ids.filtered(lambda line: line.year == 2026)
        statement = self.statement()
        statement.action_take_over_private_use()
        correction = statement.adjustment_ids
        self.assertEqual(
            (correction.vat_amount, correction.vat_base, correction.turnover_amount),
            (line.private_use_vat, 80.0, 80.0),
        )
        line.action_confirm_private_use()
        amounts = self.env["l10n_de_euer.adapter"]._turnover_amounts(self.company, 2026)
        self.assertEqual(
            [(one.amount, one.source) for one in amounts], [(8000, "adjustment")]
        )

    # ------------------------------------------------------------ R-44
    def test_yearly_maximums_over_all_corrections(self):
        """R-44: two daily allowances of 115 days give at most 1,260 € (Kz
        163); two commuting allowances of 115 days × 60 km without own
        vehicle at most 4,500 € (Kz 176); deleting the first one frees the
        maximum for the second."""
        statement = self.statement()
        first = self.correction(statement, "home_office_days", days=115)
        second = self.correction(statement, "home_office_days", days=115)
        self.assertEqual((first.result_amount, second.result_amount), (690.0, 570.0))
        self.assertTrue(second.notes)
        trips = [
            self.correction(statement, "commuting", days=115, distance_km=60.0)
            for _index in range(2)
        ]
        self.assertEqual([trip.result_amount for trip in trips], [2622.0, 1878.0])
        statement.action_compute()
        self.assertEqual(self.value(statement, DAYS), D("1260.00"))
        self.assertEqual(self.value(statement, COMMUTING), D("4500.00"))
        first.unlink()
        statement.action_compute()
        self.assertEqual(second.result_amount, 690.0)
        self.assertEqual(self.value(statement, DAYS), D("690.00"))

    def test_booked_free_supply_not_in_turnover(self):
        """R-27, review E4 (P5): 99,500 € received and the private use of a
        car booked by a small business without VAT (600 € in Kz 106) stay
        below the limit: the warning at 80 % and a hint, no blocker. A
        profit correction with only the VAT base counts it as taxable,
        without a second Kz 106 (no C-54). The turnover of 2025 is entered
        (0 €, no bookings of 2025)."""
        self.previous_turnover(2026)
        self.year(99500)
        self.entry("2026-12-31", [("private_w", 600), ("private_car", -600)])
        statement = self.statement()
        result = self.engine_result(statement)
        self.assertEqual(result.turnover.amount, 9950000)
        self.assertEqual(
            self.limit_keys(result),
            [("ku_free_supply_not_counted", m.INFO), ("ku_limit_near", m.WARNING)],
        )
        statement.action_compute()
        self.assertEqual(self.value(statement, PRIVATE_CAR), D("600.00"))
        hint = self.results(statement, "C-32").filtered(
            lambda check: check.severity == "info"
        )
        self.assertEqual(len(hint), 1)
        self.assertEqual(hint.res_model, "account.move")
        self.assertIn("Kz 106, 108", hint.message)
        self.assertNotIn("blocker", self.results(statement, "C-32").mapped("severity"))
        correction = self.correction(
            statement,
            "car_private_use",
            vat_base=600.0,
            reason="Car bought with input tax deduction",
        )
        self.assertEqual(correction.turnover_amount, 600.0)
        self.assertFalse(correction.field_codes)
        result = self.engine_result(statement)
        self.assertEqual(result.turnover.amount, 10010000)
        self.assertIn(("ku_limit_exceeded", m.BLOCKER), self.limit_keys(result))
        statement.action_compute()
        self.assertEqual(self.value(statement, PRIVATE_CAR), D("600.00"))
        self.assertFalse(self.results(statement, "C-54"))

    def test_booked_free_supply_with_vat_in_turnover(self):
        """R-27 under standard taxation: the private use of a car booked with
        output VAT is taxable, its net amount (the VAT base) counts in the
        total turnover; booked without VAT it does not."""
        self.set_regime("regular")
        self.entry(
            "2026-12-31",
            [
                ("private_w", 119),
                ("private_car", -100, self.base_line("ust19")),
                ("vat_out", -19, self.tax_line("ust19")),
            ],
        )
        self.entry("2026-12-31", [("private_w", 50), ("private_car", -50)])
        statement = self.statement()
        result = self.engine_result(statement)
        self.assertEqual(result.turnover.amount, 10000)
        self.assertEqual(self.limit_keys(result), [])
        statement.action_compute()
        self.assertEqual(self.value(statement, PRIVATE_CAR), D("150.00"))

    def test_income_in_kind_exempt_not_in_turnover(self):
        """R-27, § 19 (2) UStG: tax-free or not taxable income in kind (Kz
        103) of a year under standard taxation is no part of the total
        turnover, so it does not bar the small business status of the next
        year; taxable income in kind (Kz 112) does. The books of 2025 are in
        Odoo (an expense paid in 2025), so the turnover of 2025 comes from
        the data, not from an input (C-32 'previous year unknown')."""
        self.bank("2025-06-03", -10, extra=[("office", 10)])
        Period = self.env["l10n_de_euer.vat.period"]
        Period.search([("company_id", "=", self.company.id)]).unlink()
        Period.create(
            {
                "company_id": self.company.id,
                "date_from": "2020-01-01",
                "date_to": "2025-12-31",
                "regime": "regular",
                "vat_return_period": "monthly",
            }
        )
        Period.create(
            {
                "company_id": self.company.id,
                "date_from": "2026-01-01",
                "regime": "ku",
                "vat_return_period": "monthly",
                "reason": "Small business from 2026",
            }
        )
        previous = self.statement(2025)
        correction = self.correction(
            previous,
            "income_in_kind",
            amount=26000.0,
            revenue_field="EUER.20.103",
            reason="Barter, tax-free",
        )
        self.assertEqual(correction.turnover_amount, 0.0)
        previous.action_compute()
        self.assertEqual(self.value(previous, "EUER.20.103"), D("26000.00"))
        statement = self.statement()
        self.assertEqual(self.limit_keys(self.engine_result(statement)), [])
        correction.write({"revenue_field": "EUER.20.112", "vat_amount": 4940.0})
        self.assertEqual(correction.turnover_amount, 26000.0)
        self.assertEqual(
            self.limit_keys(self.engine_result(statement)),
            [("ku_limit_previous_year", m.WARNING)],
        )

    def test_income_in_kind(self):
        """Spec 5.8: income in kind of a small business is Kz 111 and part of
        the total turnover; no withdrawal."""
        statement = self.statement()
        correction = self.correction(
            statement, "income_in_kind", amount=250.0, reason="Barter: photos"
        )
        self.assertEqual(correction.turnover_amount, 250.0)
        self.assertFalse(correction.withdrawal_effect)
        statement.action_compute()
        self.assertEqual(self.value(statement, KU), D("250.00"))
        self.assertEqual(self.engine_result(statement).turnover.amount, 25000)

    # ------------------------------------------------------------ C-55
    def test_c55_daily_allowance_outside_the_home_office_months(self):
        """§ 4 (5) sentence 1 no. 6c sentence 3 EStG: a home office as
        centre for 6 months and the daily allowance is a warning that needs
        an acknowledgement, not a blocker."""
        statement = self.statement()
        self.correction(
            statement, "home_office_room", center_of_activity=True, months=6
        )
        self.correction(statement, "home_office_days", days=60)
        statement.action_compute()
        statement.action_check()
        result = self.results(statement, "C-55")
        self.assertEqual(result.severity, "warning")
        self.assertIn("6", result.message)

    # ------------------------------------------------------------ Kz 240
    def test_tax_free_income_above_the_maximums(self):
        """§ 3 no. 26, 26a EStG 2026: at most 3,300 + 960 € in Kz 240;
        4,300 € is a warning C-44."""
        statement = self.statement()
        self.correction(
            statement, "tax_free", amount=4300.0, tax_free_field="EUER.G.240"
        )
        statement.action_compute()
        self.answer(statement, Q12_TAX_FREE=scope.YES)
        statement.action_check()
        result = self.results(statement, "C-44")
        self.assertEqual(result.severity, "warning")
        self.assertEqual(result.amount, 4300.0)

    # ------------------------------------------------------------ R-52
    def land(self, name, kind):
        land = self.env["l10n_de_euer.asset"].create(
            {
                "name": name,
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
                "disposal_kind": kind,
                "disposal_value": 60000 if kind == "withdrawal" else 0,
            }
        )
        land.action_dispose()
        return land

    def test_questionnaire_cross_checks(self):
        """R-52, Anleitung 2026 lines 9 and 11: a loss of land is no sale or
        withdrawal (C-43 for "yes"); a withdrawal of land with "no" is a
        warning C-43; a correction of Kz 240 with "no" for tax-free income a
        warning C-44; a sole proprietorship of both spouses (Kz 104 = 3) a
        warning C-40."""
        self.land("Grundstück (Abriss)", "loss")
        statement = self.statement()
        statement.action_compute()
        self.answer(statement, Q04_LAND=scope.YES)
        statement.action_check()
        self.assertEqual(
            self.results(statement, "C-43", origin="statement").severity, "warning"
        )
        self.land("Grundstück", "withdrawal")
        self.correction(
            statement, "tax_free", amount=100.0, tax_free_field="EUER.G.241"
        )
        self.company.l10n_de_euer_owner = "3"
        statement.action_compute()
        self.answer(statement)
        statement.action_check()
        results = self.results(statement, origin="statement")
        found = {
            (check.code, check.severity)
            for check in results
            if check.code in ("C-40", "C-43", "C-44")
        }
        self.assertEqual(
            found, {("C-40", "warning"), ("C-43", "warning"), ("C-44", "warning")}
        )
        self.assertIn(
            "mandatory", self.results(statement, "C-43", origin="statement").message
        )

    def test_new_question_reaches_existing_statements(self):
        """R-52: a question added to the questionnaire later (Q13) is added
        to an open statement with its next computation (C-45 until it is
        answered)."""
        statement = self.statement()
        self.answer(statement)
        question = statement.scope_answer_ids.filtered(
            lambda answer: answer.question_code == "Q13_PREPAYMENT"
        )
        self.env.cr.execute(
            "DELETE FROM l10n_de_euer_scope_answer WHERE id = %s", question.ids
        )
        self.env.invalidate_all()
        statement.action_compute()
        question = statement.scope_answer_ids.filtered(
            lambda answer: answer.question_code == "Q13_PREPAYMENT"
        )
        self.assertTrue(question)
        self.assertFalse(question.confirmed_on)
        self.assertEqual(len(statement.scope_answer_ids), len(scope.QUESTIONS))

    def test_land_value_only_after_confirmation(self):
        """GT-14: Kz 15.120 from a proposal of the previous year is no value
        until the questionnaire is confirmed; the fill-in aid says so."""
        previous = self.statement(2025)
        self.answer(previous, Q04_LAND=scope.YES)
        statement = self.statement()
        statement.action_compute()
        self.assertFalse(self.value(statement, LAND))
        rows = [
            row
            for section in statement._get_fill_aid_sections()
            for row in section["rows"]
            if row["code"] == LAND
        ]
        self.assertTrue(rows[0]["missing"])
        self.assertIn("confirm the questionnaire", rows[0]["note"])
        self.answer(statement, Q04_LAND=scope.YES)
        self.assertEqual(self.value(statement, LAND), "1")


@tagged("post_install_l10n", "post_install", "-at_install")
class TestCorrectionReviewSkr03(CorrectionReviewScenarios, AccountTestInvoicingCommon):
    allow_inherited_tests_method = True
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_finalisation()


@tagged("post_install_l10n", "post_install", "-at_install")
class TestCorrectionReviewSkr04(CorrectionReviewScenarios, AccountTestInvoicingCommon):
    allow_inherited_tests_method = True
    CHART = "de_skr04"
    ACCOUNTS = SKR04_ACCOUNTS
    TAXES = SKR04_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr04")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_finalisation()


@tagged("post_install_l10n", "post_install", "-at_install")
class TestFinalisationReview(ReviewCommon, AccountTestInvoicingCommon):
    """GT-02, GT-04, GT-05 with the attacks of the review (SKR03)."""

    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_finalisation()

    def open_year(self):
        """An open statement of 2025 with values, slices and answers."""
        self.year(500, year=2025)
        statement = self.statement(2025)
        self.correction(statement, "meals", days_8h=1)
        statement.action_compute()
        return statement

    def test_gt05_no_correction_moves_into_a_finalised_statement(self):
        """GT-05: a profit correction of an open statement cannot be moved
        into a finalised one: as bookkeeper, through RPC and with sudo()."""
        statement = self.open_year()
        correction = statement.adjustment_ids
        final = self.finalized()
        corrections = final.adjustment_ids
        bookkeeper = self.make_user("review_bookkeeper", "account.group_account_user")
        Adjustment = self.env["l10n_de_euer.adjustment"]
        for variant in (correction.with_user(bookkeeper), correction.sudo()):
            with self.assertRaises(UserError):
                variant.write({"statement_id": final.id})
        with self.assertRaises(UserError):
            call_kw(
                Adjustment.with_user(bookkeeper),
                "write",
                [correction.ids, {"statement_id": final.id}],
                {},
            )
        self.assertEqual(final.adjustment_ids, corrections)
        self.assertEqual(correction.statement_id, statement)
        self.assertEqual(final._verify_integrity(), [])

    def test_gt05_no_record_moves_into_a_finalised_statement(self):
        """GT-05: bank reconciliation, values, answers and slices of an open
        statement cannot be moved into a finalised statement or run, also
        not with sudo() and the internal context keys."""
        statement = self.open_year()
        final = self.finalized()
        self.assertTrue(statement.run_id.slice_ids)
        moves = [
            (statement.cash_balance_ids[:1], {"statement_id": final.id}),
            (statement.line_ids[:1], {"statement_id": final.id}),
            (statement.scope_answer_ids[:1], {"statement_id": final.id}),
            (statement.run_id.slice_ids[:1], {"run_id": final.run_id.id}),
        ]
        for record, vals in moves:
            self.assertTrue(record, vals)
            for variant in (record.sudo(), record.sudo().with_context(**INTERNAL)):
                with self.subTest(model=record._name, su=variant.env.su):
                    with self.assertRaises(UserError):
                        variant.write(vals)
        self.assertEqual(final._verify_integrity(), [])

    def test_ten_day_items_need_the_superuser(self):
        """G-06, spec 7.3: the context key of the review list sent through
        RPC does not open the decision fields; the actions still work."""
        sale = self.year()
        bank = self.env["account.move"].search(
            [("statement_line_id", "!=", False), ("company_id", "=", self.company.id)],
            limit=1,
        )
        Item = self.env["l10n_de_euer.ten_day.item"]
        item = (
            Item.sudo()
            .with_context(**INTERNAL)
            .create(
                {
                    "company_id": self.company.id,
                    "year": 2026,
                    "kind": "recurring",
                    "source_move_id": bank.id,
                    "origin_move_id": sale.id,
                    "cash_date": "2026-12-30",
                }
            )
        )
        bookkeeper = self.make_user("review_ten_day", "account.group_account_user")
        manager = self.make_user(
            "review_ten_day_manager", "account.group_account_manager"
        )
        context = {"l10n_de_euer_ten_day_internal": True}
        with self.assertRaises(UserError):
            call_kw(
                Item.with_user(bookkeeper),
                "write",
                [
                    item.ids,
                    {
                        "decision": "apply",
                        "reason": "forged",
                        "decided_by_id": manager.id,
                        "decided_on": "2020-01-01 00:00:00",
                        "applied": True,
                    },
                ],
                {"context": context},
            )
        with self.assertRaises(UserError):
            call_kw(
                Item.with_user(bookkeeper),
                "create",
                [
                    {
                        "company_id": self.company.id,
                        "year": 2026,
                        "kind": "vat",
                        "source_move_id": bank.id,
                        "origin_move_id": bank.id,
                    }
                ],
                {"context": context},
            )
        self.assertFalse(item.decision)
        item.with_user(bookkeeper).reason = "Rent of December"
        item.with_user(bookkeeper).action_reject()
        self.assertEqual((item.decision, item.decided_by_id), ("reject", bookkeeper))

    def test_gt02_branch_of_its_own_does_not_see_the_register(self):
        """GT-02, spec 7.9: a user of a branch that is a business of its own
        does not see the asset register of the main company; a user of an
        ordinary branch sees the register of its business."""
        camera = self.asset()
        own = self.env["res.company"].create(
            {
                "name": "Own Business",
                "parent_id": self.company.id,
                "l10n_de_euer_separate_business": True,
            }
        )
        branch = self.env["res.company"].create(
            {"name": "Branch", "parent_id": self.company.id}
        )
        self.assertEqual(own.l10n_de_euer_business_id, own)
        self.assertEqual(branch.l10n_de_euer_business_id, self.company)
        self.env.user.company_ids |= own
        own_asset = self.asset(name="Drucker", company_id=own.id)
        own_user = self.make_user("review_own", "account.group_account_user", own)
        branch_user = self.make_user(
            "review_branch", "account.group_account_user", branch
        )
        for model in (
            "l10n_de_euer.asset",
            "l10n_de_euer.asset.year",
            "l10n_de_euer.asset.event",
        ):
            Model = self.env[model]
            with self.subTest(model=model):
                self.assertFalse(
                    Model.with_user(own_user)
                    .search([])
                    .filtered(lambda record: record.company_id == self.company)
                )
        self.assertEqual(
            self.env["l10n_de_euer.asset"].with_user(own_user).search([]), own_asset
        )
        with self.assertRaises(AccessError):
            camera.with_user(own_user).read(["name", "acquisition_cost"])
        self.assertEqual(
            self.env["l10n_de_euer.asset"].with_user(branch_user).search([]), camera
        )

    def test_gt04_changed_receipt_is_found(self):
        """GT-04: a statement of account replaced in the database after the
        finalisation (same stored checksum) is found by Check Integrity."""
        self.env["ir.config_parameter"].sudo().set_param("ir_attachment.location", "db")
        self.year()
        statement = self.statement()
        statement.action_compute()
        receipt = self.env["ir.attachment"].create(
            {"name": "Kontoauszug_12.pdf", "raw": b"Statement of account 31.12."}
        )
        self.reconcile_banks(statement)
        statement.cash_balance_ids[:1].attachment_ids = [Command.link(receipt.id)]
        final = self.finalize(statement)
        self.assertEqual(final._verify_integrity(), [])
        self.env.cr.execute(
            "UPDATE ir_attachment SET db_datas = %s WHERE id = %s",
            [b"FORGED STATEMENT 31.12.", receipt.id],
        )
        self.env.invalidate_all()
        problems = final._verify_integrity()
        self.assertEqual(len(problems), 1)
        self.assertIn("Kontoauszug_12.pdf", problems[0])

    def test_gt04_referenced_partners_users_and_categories_stay(self):
        """G-04: partners, users and categories the finalised records refer
        to cannot be deleted (the records would change without trace)."""
        partner = self.env["res.partner"].create({"name": "Hotel"})
        self.year()
        statement = self.statement()
        self.correction(statement, "meals", days_8h=1, partner_id=partner.id)
        final = self.finalize(statement)
        with mute_logger("odoo.sql_db"), self.assertRaises(IntegrityError) as error:
            with self.env.cr.savepoint():
                partner.unlink()
        self.assertIn("l10n_de_euer_adjustment", str(error.exception))
        bookkeeper = self.make_user("review_balance", "account.group_account_user")
        open_statement = self.statement(2025)
        balance = open_statement.cash_balance_ids[:1] or self.env[
            "l10n_de_euer.cash.balance"
        ].create(
            {"statement_id": open_statement.id, "journal_id": self.bank_journal.id}
        )
        balance.with_user(bookkeeper).balance_statement = 0.0
        self.assertEqual(balance.entered_by_id, bookkeeper)
        with mute_logger("odoo.sql_db"), self.assertRaises(IntegrityError) as error:
            with self.env.cr.savepoint():
                bookkeeper.unlink()
        self.assertIn("l10n_de_euer_cash_balance", str(error.exception))
        category = final.run_id.slice_ids.filtered("category_id")[:1].category_id
        self.assertTrue(category)
        with self.assertRaises(UserError):
            category.unlink()
        self.assertEqual(final._verify_integrity(), [])

    def test_g17_balance_entered_by_the_current_user(self):
        """G-17: who entered a balance and when are never taken from the
        caller, so a balance of 0.00 is never entered in another name."""
        statement = self.statement()
        manager = self.make_user(
            "review_balance_manager", "account.group_account_manager"
        )
        journal = self.env["account.journal"].create(
            {"name": "Bank 2", "type": "bank", "code": "BNK9"}
        )
        balance = self.env["l10n_de_euer.cash.balance"].create(
            {
                "statement_id": statement.id,
                "journal_id": journal.id,
                "entered_by_id": manager.id,
                "entered_on": "2020-01-01 00:00:00",
            }
        )
        self.assertFalse(balance.entered_by_id)
        self.assertFalse(balance.entered_on)
        self.assertFalse(balance._is_reconciled())

    def test_c60_comparison_waits_for_the_correction(self):
        """A comparison run of the finalised version would replace the run of
        a correction in progress: refused while the correction is open."""
        final = self.finalized()
        correction = final._create_correction("Receipt found later")
        correction.action_compute()
        with self.assertRaises(UserError):
            final.action_compare()
        self.assertTrue(correction.run_id.exists())
        self.assertEqual(correction.state, "computed")


@tagged("post_install_l10n", "post_install", "-at_install")
class TestOutputReview(ReviewCommon, AccountTestInvoicingCommon):
    """Outputs of the review (spec 7.5, 7.12, 8.1, G-09, G-11), SKR03."""

    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_finalisation()

    def computed(self):
        self.year()
        self.asset()
        statement = self.statement()
        self.correction(statement, "meals", days_8h=1)
        statement.action_compute()
        return statement

    def render(self, xml_id, statement, pdf=False):
        content, _kind = self.env["ir.actions.report"]._render_qweb_html(
            xml_id, statement.ids, data={"report_type": "pdf"} if pdf else None
        )
        return html.fromstring(content)

    def test_pdf_layout_din5008(self):
        """The PDF in the DIN 5008 layout of l10n_din5008 shows the title of
        each report and the sections of the procedure documentation (the
        layout hides h1 and the h2 directly in .page); the captions stand
        above the tables; evidence and check protocol use the internal
        layout in landscape."""
        statement = self.computed()
        self.company.external_report_layout_id = self.env.ref(
            "l10n_din5008.external_layout_din5008"
        )
        document = self.render("l10n_de_euer.report_fill_aid", statement, pdf=True)
        titles = [
            title.text_content()
            for title in document.xpath("//div[contains(@class, 'din_page')]//h2")
        ]
        self.assertTrue(
            any("Fill-in aid for Mein ELSTER" in title for title in titles), titles
        )
        procedure = self.render("l10n_de_euer.report_procedure", statement, pdf=True)
        page = "contains(concat(' ', normalize-space(@class), ' '), ' page ')"
        self.assertFalse(procedure.xpath(f"//div[{page}]/h2"))
        sections = [
            section.text_content().strip()
            for section in procedure.xpath(
                "//div[contains(@class, 'o_l10n_de_euer_content')]/h2"
            )
        ]
        self.assertEqual(sections[0], "1. Data flow")
        self.assertEqual(sections[-1], "7. Versions")
        titles = [
            title.text_content()
            for title in procedure.xpath("//div[contains(@class, 'din_page')]//h2")
        ]
        self.assertTrue(any("Procedure documentation" in t for t in titles), titles)
        for rendered in (document, procedure):
            for table in rendered.xpath("//table[contains(@class, 'o_l10n_de_euer')]"):
                self.assertIn("caption-top", table.get("class"))
        for xml_id in (
            "l10n_de_euer.report_evidence",
            "l10n_de_euer.report_check_protocol",
        ):
            document = self.render(xml_id, statement, pdf=True)
            self.assertFalse(document.xpath("//*[contains(@class, 'din_page')]"))
            self.assertTrue(document.xpath(f"//div[{page}]//h1"))
        evidence = self.render("l10n_de_euer.report_evidence", statement, pdf=True)
        for foot in evidence.xpath("//tfoot"):
            self.assertIn("table-row-group", foot.get("style"))

    def test_html_title_row_headers_and_control_values(self):
        """O-11, WCAG 2.4.2: the HTML views have a title; every row of the
        evidence has its header (also values of the register and the
        corrections); a control value is described once and without the
        claim that Mein ELSTER computes it (U-12)."""
        statement = self.computed()
        for xml_id, title in (
            ("l10n_de_euer.report_fill_aid", "Fill-in aid for Mein ELSTER"),
            ("l10n_de_euer.report_evidence", "Evidence per Kennzahl"),
            ("l10n_de_euer.report_check_protocol", "Check protocol"),
            ("l10n_de_euer.report_procedure", "Procedure documentation"),
        ):
            document = self.render(xml_id, statement)
            self.assertIn(title, document.findtext(".//title"))
            self.assertIn(statement.name, document.findtext(".//title"))
        evidence = self.render("l10n_de_euer.report_evidence", statement)
        headers = evidence.xpath("//tbody/tr/th[@scope='row']")
        self.assertTrue(headers)
        self.assertTrue(all(header.text_content().strip() for header in headers))
        rows = [
            row
            for section in statement._get_evidence_sections()
            for row in section["rows"]
        ]
        self.assertTrue(any(row["rule"] == "asset" for row in rows))
        fill_aid = self.render("l10n_de_euer.report_fill_aid", statement)
        text = fill_aid.text_content()
        self.assertNotIn("computes this sum itself", text)
        self.assertNotIn("Control value.", text)
        control = [
            row
            for section in statement._get_fill_aid_sections()
            for row in section["rows"]
            if row["control"]
        ]
        self.assertTrue(control)
        self.assertEqual(control[0]["note"].count("Control value"), 1)

    def test_finalised_outputs_keep_assignment_and_names(self):
        """Spec 7.7: the data access package of a finalised year is the same
        after the account assignment, an account or a partner changed: it
        reads them from the payload."""
        final = self.finalized()
        before = final._get_export("z3")[2]
        procedure = final._get_procedure_data()["assignment"]
        revenue = self.acc("ku_revenue")
        assignment = self.env["l10n_de_euer.account.assignment"].search(
            [("company_id", "=", self.company.id), ("account_id", "=", revenue.id)]
        )
        assignment.category_id = self.category("REV_TAXABLE")
        revenue.name = "Renamed revenue"
        self.customer.name = "Renamed customer"
        after = final._get_export("z3")[2]
        self.assertEqual(before, after)
        self.assertEqual(final._get_procedure_data()["assignment"], procedure)
        archive = zipfile.ZipFile(io.BytesIO(after))
        self.assertIn(";REV_KU;", archive.read("account_assignment.csv").decode())
        self.assertNotIn(b"Renamed", archive.read("slices.csv"))

    def test_finalised_names_in_the_language_of_the_output(self):
        """Review E4 (P1): the payload stores the names of accounts and
        categories per language. A German output shows the same German names
        before and after the finalisation (account assignment, evidence),
        an English one the English names; a later renaming changes neither."""
        self.env["res.lang"]._activate_lang("de_DE")
        revenue = self.acc("ku_revenue")
        category = self.category("REV_KU")
        revenue.with_context(lang="de_DE").name = "Erlöse Kleinunternehmer"
        category.with_context(lang="de_DE").name = "Kategorie Kleinunternehmer"
        english = revenue.with_context(lang="en_US").name
        statement = self.computed()

        def names(lang):
            record = statement.with_context(lang=lang)
            assignment = [
                (row["account_name"], row["category_name"])
                for row in record._get_assignment_rows()
                if row["account_code"] == revenue.code
            ]
            evidence = sorted(
                {
                    row["account_name"]
                    for row in record._get_slice_rows(record.run_id.slice_ids)
                    if row["account_code"] == revenue.code
                }
            )
            return assignment, evidence

        def evidence_text(lang):
            Report = self.env["ir.actions.report"].with_context(lang=lang)
            content, _kind = Report._render_qweb_html(
                "l10n_de_euer.report_evidence", statement.ids
            )
            return html.fromstring(content).text_content()

        before = {lang: names(lang) for lang in ("de_DE", "en_US")}
        self.assertEqual(
            before["de_DE"],
            (
                [("Erlöse Kleinunternehmer", "Kategorie Kleinunternehmer")],
                ["Erlöse Kleinunternehmer"],
            ),
        )
        self.assertEqual(before["en_US"][1], [english])
        self.finalize(statement)
        payload = statement._read_payload()[1]
        self.assertEqual(
            payload["references"]["account.account"][str(revenue.id)]["name"],
            {"de_DE": "Erlöse Kleinunternehmer", "en_US": english},
        )
        self.assertEqual({lang: names(lang) for lang in before}, before)
        text = evidence_text("de_DE")
        self.assertIn("Erlöse Kleinunternehmer", text)
        self.assertNotIn(english, text)
        revenue.with_context(lang="de_DE").name = "Umbenannt"
        category.with_context(lang="de_DE").name = "Umbenannt"
        self.assertEqual(names("de_DE"), before["de_DE"])
        # a language installed after the finalisation shows the payload names
        # of the language of the payload
        self.env["res.lang"]._activate_lang("fr_FR")
        self.assertEqual(names("fr_FR"), before["en_US"])

    def test_procedure_documentation_used_assignments_and_numbers(self):
        """G-11: the procedure documentation lists the accounts with bookings
        and the assignments that deviate from the default (the Z3 package
        all of them) and shows rule parameters in the language of the
        user."""
        statement = self.computed()
        full = statement._get_assignment_rows()
        used = statement._get_procedure_data()["assignment"]
        self.assertLess(len(used), len(full))
        codes = {row["account_code"] for row in used}
        self.assertIn(self.acc("ku_revenue").code, codes)
        self.assertNotIn(self.acc("rent").code, codes)
        self.env["res.lang"]._activate_lang("de_DE")
        values = {
            row["code"]: row["value"]
            for row in statement.with_context(lang="de_DE")._get_parameter_rows()
        }
        self.assertEqual(values["commute_rate_21"], "0,38")
        values = {
            row["code"]: row["value"]
            for row in statement.with_context(lang="en_US")._get_parameter_rows()
        }
        self.assertEqual(values["commute_rate_21"], "0.38")

    def test_z3_texts_on_one_line(self):
        """G-09: a reason with line breaks is one record of the CSV file
        (CRLF separates the records)."""
        self.year()
        statement = self.statement()
        self.correction(
            statement, "meals", days_8h=1, reason='Reise; Kunde "A"\r\nZweite Zeile'
        )
        statement.action_compute()
        archive = zipfile.ZipFile(io.BytesIO(statement._get_export("z3")[2]))
        raw = archive.read("profit_corrections.csv").decode()
        self.assertEqual(raw.count("\r\n"), 2)
        self.assertIn('"Reise; Kunde ""A"" Zweite Zeile"', raw)

    def test_choices_counts_and_actions(self):
        """The choices are shown with their labels; the number of slices per
        value comes from one grouped read; smoke test of the actions of the
        statement form."""
        self.year()
        statement = self.statement()
        self.correction(statement, "meals", days_8h=1)
        statement.action_compute()
        self.assertIn("Separate Accounts", statement.choices_text)
        self.assertNotIn("True", statement.choices_text)
        Slice = self.env["l10n_de_euer.cash.slice"]
        self.assertTrue(statement.line_ids.filtered("slice_count"))
        for line in statement.line_ids:
            self.assertEqual(
                line.slice_count,
                Slice.search_count(line._slice_domain())
                if line.field_type not in NON_AMOUNT_TYPES
                else 0,
                line.code,
            )
        self.assertTrue(statement.action_update_cash_balances())
        self.assertEqual(
            statement.action_open_assets()["res_model"], "l10n_de_euer.asset"
        )
        self.assertEqual(
            statement.action_open_assignments()["res_model"],
            "l10n_de_euer.account.assignment",
        )
        self.assertEqual(
            statement.action_open_checks()["res_model"], "l10n_de_euer.check"
        )
        line = statement.line_ids.filtered(lambda line: line.code == KU)
        self.assertEqual(line.action_override()["res_id"], line.id)
        final = self.finalize(statement)
        final.action_compare()
        self.assertTrue(final.comparison_run_id)
        self.assertEqual(
            final.action_open_comparison_run()["res_model"],
            "l10n_de_euer.cash.slice",
        )


@tagged("post_install_l10n", "post_install", "-at_install")
class TestSetupWizardReview(ReviewCommon, AccountTestInvoicingCommon):
    """Setup wizard created through the API with some values only."""

    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_statement()

    def test_wizard_with_some_values(self):
        Wizard = self.env["l10n_de_euer.setup.wizard"]
        self.env["l10n_de_euer.vat.period"].search(
            [("company_id", "=", self.company.id)]
        ).unlink()
        self.company.l10n_de_euer_owner = "1"
        wizard = Wizard.create(
            {"company_id": self.company.id, "business_type": "Foto", "regime": "ku"}
        )
        self.assertEqual(
            (wizard.owner, wizard.entertainment_mode, wizard.create_vat_period),
            ("1", "separate", True),
        )
        self.assertEqual(wizard.legal_form, self.company.l10n_de_euer_legal_form)
        wizard.create_vat_period = False
        with self.assertRaises(UserError):
            wizard.action_apply()
        wizard.create_vat_period = True
        wizard.action_apply()
        self.assertEqual(self.company.l10n_de_euer_owner, "1")
        self.assertTrue(
            self.env["l10n_de_euer.vat.period"].search(
                [("company_id", "=", self.company.id)]
            )
        )
        self.assertIn("standard taxation", Wizard._fields["create_vat_period"].help)
