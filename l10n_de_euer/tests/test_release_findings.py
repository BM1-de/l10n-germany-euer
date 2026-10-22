# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Regression tests of the findings of review E5 (synthetic amounts, D6).

* Small business purchases (§ 9b (1) EStG): C-68 compares a gross purchase
  with the acquisition cost, the asset wizard splits it by a VAT rate.
* C-71 names a solution by the section of the negative field.
* C-23 leaves out returned direct debits (no transfer order).
* Ten-day rule with a due date on Saturday or Sunday (RT-01): no shift of
  the due date under § 108 (3) AO, the payment must be made by the 10th.
* Small business limit (R-27, C-32): the turnover of the previous year
  entered after a takeover holds for the year entered with it, whatever the
  start of the status period; a small business year without bookings of the
  previous year and without that input is reported instead of comparing 0.
  Until 2024 the turnover of the year of foundation is converted into a
  turnover of the full year (§ 19 (3) sentences 3 and 4 UStG in the version
  until 2024).
"""

from decimal import Decimal as D
from fractions import Fraction

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import Form, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from ..models.engine import model as m
from ..models.engine import vat
from .common import AssetCase
from .engine_common import Book, EngineCase, codes, d, fields
from .scenario_common import SKR03_ACCOUNTS, SKR03_TAXES, ScenarioCommon


def limit_findings(result):
    """C-32 findings of the limit check: (key, severity, amount in cent)."""
    return sorted(
        (f.key, f.severity, f.amount)
        for f in result.findings
        if f.code == "C-32" and f.key != "ku_free_supply_not_counted"
    )


class TestPreviousYearTurnover(EngineCase):
    """R-27: the previous year of the small business check is never 0 by
    default."""

    def book(self, *receipts, period=None):
        """Small business since 2018 with cash receipts ``(date, euro)``."""
        book = Book()
        book.set_regimes(period or m.VatPeriod(date_from=d(2018, 1, 1), regime="ku"))
        for index, (day, amount) in enumerate(receipts):
            book.move(f"Kasse {index}", day, ("1000", amount), ("8195", -amount))
        return book

    def test_entered_turnover_of_an_older_period(self):
        """Regression: the takeover writes the turnover into the earliest
        period (here from 2018). With the year 2025 entered it holds for
        2025 (30,000 € > 25,000 €); before, the value was ignored and the
        check of the previous year silently compared 0."""
        period = m.VatPeriod(
            date_from=d(2018, 1, 1),
            regime="ku",
            prev_year_turnover=3000000,
            prev_year_turnover_year=2025,
        )
        book = self.book((d(2025, 3, 2), 1000), period=period)
        self.assertEqual(
            limit_findings(self.run_year(book, 2025)),
            [("ku_limit_previous_year", m.WARNING, 3000000)],
        )
        # 2026 takes the turnover of 2025 from the data
        book.move("Kasse", d(2026, 3, 2), ("1000", 100), ("8195", -100))
        self.assertEqual(limit_findings(self.run_year(book, 2026)), [])

    def test_unknown_previous_year_is_reported(self):
        """No bookings before 2025 and nothing entered for 2025: warning
        instead of a comparison with 0. A value without year holds for the
        first year of the period (2018) only, so 2025 stays unknown."""
        book = self.book((d(2025, 3, 2), 1000))
        self.assertEqual(
            limit_findings(self.run_year(book, 2025)),
            [("ku_previous_year_unknown", m.WARNING, 0)],
        )
        finding = next(
            f for f in self.run_year(book, 2025).findings if f.key.endswith("unknown")
        )
        self.assertEqual(finding.params, {"limit": 2500000})
        book.set_regimes(
            m.VatPeriod(
                date_from=d(2018, 1, 1), regime="ku", prev_year_turnover=3000000
            )
        )
        self.assertEqual(
            limit_findings(self.run_year(book, 2025)),
            [("ku_previous_year_unknown", m.WARNING, 0)],
        )
        self.assertEqual(limit_findings(self.run_year(book, 2018)), [])

    def test_zero_entered_for_the_year_is_known(self):
        """A turnover of 0 entered with its year is an input (nothing
        received in 2024), not a missing value."""
        period = m.VatPeriod(
            date_from=d(2018, 1, 1),
            regime="ku",
            prev_year_turnover=0,
            prev_year_turnover_year=2025,
        )
        book = self.book((d(2025, 3, 2), 1000), period=period)
        self.assertEqual(limit_findings(self.run_year(book, 2025)), [])

    def test_bookings_of_an_earlier_year_make_it_known(self):
        """Bookings of 2024 (here an expense only) make the data the source:
        a turnover of 0 in 2024 is no finding; 26,000 € received in 2024
        exceed 25,000 €. An opening entry alone carries balances of the
        previous system, not its turnover: still unknown."""
        book = self.book((d(2025, 3, 2), 1000))
        book.move(
            "Eröffnung",
            d(2024, 12, 31),
            ("1200", 500),
            ("9000", -500),
            opening=True,
        )
        self.assertEqual(
            limit_findings(self.run_year(book, 2025)),
            [("ku_previous_year_unknown", m.WARNING, 0)],
        )
        book.move("Papier", d(2024, 6, 3), ("4930", 10), ("1000", -10))
        self.assertEqual(limit_findings(self.run_year(book, 2025)), [])
        book.move("Kasse", d(2024, 6, 4), ("1000", 26000), ("8195", -26000))
        self.assertEqual(
            limit_findings(self.run_year(book, 2025)),
            [("ku_limit_previous_year", m.WARNING, 2600000)],
        )

    def test_documents_alone_are_no_previous_year(self):
        """Open items of a takeover imported as documents with their dates
        (an invoice of 2024 paid in 2025) show no money received in 2024:
        the turnover of 2024 stays unknown. A money entry of 2024 makes the
        books the source of the previous year."""
        book = self.book()
        invoice = self.sale(book, "RE 2024", d(2024, 12, 10), 500)
        self.receipt(book, "Zahlung", d(2025, 1, 15), 500, invoice)
        self.assertEqual(
            limit_findings(self.run_year(book, 2025)),
            [("ku_previous_year_unknown", m.WARNING, 0)],
        )
        book.move("Kasse", d(2024, 11, 4), ("1000", 30), ("8195", -30))
        self.assertEqual(limit_findings(self.run_year(book, 2025)), [])

    def test_takeover_during_the_previous_year(self):
        """Books opened on 30 June 2025 with money from July miss the first
        half of 2025: unknown for the check of 2026 until the turnover is
        entered. Books opened on 31 December 2024 hold all of 2025; so do
        books opened with an activity that started on 1 July 2025, and books
        with money before a later opening entry (e.g. of a new cash
        account)."""

        def book_from(opening_day, start=None):
            book = self.book()
            book.move(
                "Eröffnung",
                opening_day,
                ("1200", 500),
                ("9000", -500),
                opening=True,
            )
            book.move("Kasse", d(2025, 7, 3), ("1000", 800), ("8195", -800))
            book.move("Kasse", d(2026, 3, 2), ("1000", 100), ("8195", -100))
            book.activity_start = start
            book.founding_year = start.year if start else None
            return book

        self.assertEqual(
            limit_findings(self.run_year(book_from(d(2025, 6, 30)), 2026)),
            [("ku_previous_year_unknown", m.WARNING, 0)],
        )
        for book in (
            book_from(d(2024, 12, 31)),
            book_from(d(2025, 6, 30), start=d(2025, 7, 1)),
        ):
            self.assertEqual(limit_findings(self.run_year(book, 2026)), [])
        book = book_from(d(2025, 6, 30))
        book.move("Kasse", d(2024, 5, 6), ("1000", 50), ("8195", -50))
        self.assertEqual(limit_findings(self.run_year(book, 2026)), [])
        # entered for 2026, the turnover of 2025 counts (30,000 € > 25,000 €)
        book = book_from(d(2025, 6, 30))
        book.previous_turnover(2026, 3000000)
        self.assertEqual(
            limit_findings(self.run_year(book, 2026)),
            [("ku_limit_previous_year", m.WARNING, 3000000)],
        )

    def test_founding_year_and_standard_taxation_need_no_previous_year(self):
        """The year of foundation has no previous year; a business under
        standard taxation on 1 January has no small business check."""
        book = self.book((d(2025, 5, 2), 1000))
        book.founding_year = 2025
        book.activity_start = d(2025, 5, 1)
        self.assertEqual(limit_findings(self.run_year(book, 2025)), [])
        book = self.book((d(2025, 5, 2), 1000))
        book.set_regimes(
            m.VatPeriod(
                date_from=d(2018, 1, 1), date_to=d(2025, 3, 31), regime="regular"
            ),
            m.VatPeriod(date_from=d(2025, 4, 1), regime="ku"),
        )
        self.assertEqual(limit_findings(self.run_year(book, 2025)), [])


class TestReturnedDebitTransferOrder(EngineCase):
    """C-23 asks for the order date of a transfer from 1 to 5 January; a
    returned direct debit has no order of the business (RT-02 B-2)."""

    def test_returned_debit_has_no_transfer_order(self):
        """Direct debit collected on 20.12.2025, returned on 05.01.2026 with
        a fee, paid again on 02.02.2026: no C-23. A refund transferred to a
        customer on 03.01.2026 for an overpayment of December is a transfer:
        C-23."""
        book = Book()
        invoice = self.sale(book, "RE", d(2025, 12, 1), 119)
        self.receipt(book, "Lastschrift", d(2025, 12, 20), 119, invoice)
        back = book.move(
            "Rücklastschrift",
            d(2026, 1, 5),
            ("1410", 119),
            ("4970", 3),
            ("1200", -122),
        )
        again = book.move("Zahlung", d(2026, 2, 2), ("1200", 119), ("1410", -119))
        book.rec(back("1410"), again("1410"))
        result = self.run_year(book, 2026)
        self.assertNotIn("C-23", {f.code for f in result.findings})
        overpaid = book.move("Zahlung", d(2025, 12, 22), ("1200", 50), ("1410", -50))
        refund = book.move("Erstattung", d(2026, 1, 3), ("1410", 50), ("1200", -50))
        book.rec(overpaid("1410"), refund("1410"))
        result = self.run_year(book, 2026)
        self.assertEqual(
            [f.move_ids for f in result.findings if f.code == "C-23"],
            [(refund.move.id,)],
        )


class TestTenDayWeekendDueDate(EngineCase):
    """§ 11 (2) sentence 2 EStG with a due date on a weekend (dates of RT-01):
    the due date of a VAT return stays the 10th (§ 18 (1) sentence 4 UStG,
    § 46 UStDV); § 108 (3) AO only extends the period for the payment. A
    payment by the 10th belongs to the previous year, a payment on the next
    working day to the year of payment (BFH X R 44/16 and VIII R 34/12, H 11
    EStH 'Allgemeines')."""

    def book(self, day, period, extension):
        book = Book(regime="regular")
        book.set_regimes(
            m.VatPeriod(
                date_from=d(2020, 1, 1),
                regime="regular",
                vat_return_period="monthly",
                permanent_extension=extension,
            )
        )
        bank = book.move(
            f"Voranmeldung {period}",
            day,
            ("1780", 510),
            ("1200", -510),
            partner="Finanzamt",
            info={"vat_period": period},
        )
        return book, bank

    def test_due_on_saturday_or_sunday(self):
        """12/2025 without extension is due on Saturday 10.01.2026, 11/2026
        with extension on Sunday 10.01.2027: paid on Friday or on the 10th
        itself, Kz 186 of the previous year; paid on Monday, Kz 186 of the
        year of payment. The engine asks (C-26) whether the Monday payment
        was a direct debit (R-06); as a transfer it stays in its year."""
        vat_paid = "EUER.25.186"
        for day, period, extension in (
            (d(2026, 1, 9), "2025-12", False),
            (d(2026, 1, 10), "2025-12", False),
            (d(2027, 1, 8), "2026-11", True),
        ):
            with self.subTest(day=day):
                self.assertEqual(vat.vat_due_date(period, extension).day, 10)
                book, _bank = self.book(day, period, extension)
                self.assertEqual(
                    fields(self.run_year(book, day.year - 1)),
                    {vat_paid: D("510.00")},
                )
                self.assertEqual(fields(self.run_year(book, day.year)), {})
        for day, period, extension in (
            (d(2026, 1, 12), "2025-12", False),
            (d(2027, 1, 11), "2026-11", True),
        ):
            with self.subTest(day=day):
                book, bank = self.book(day, period, extension)
                result = self.run_year(book, day.year)
                self.assertIn("C-26", codes(result, m.BLOCKER))
                book.decide(bank, bank, "reject", kind="direct_debit_tax")
                result = self.run_year(book, day.year)
                self.assertNotIn("C-26", codes(result))
                self.assertEqual(fields(result), {vat_paid: D("510.00")})
                self.assertEqual(fields(self.run_year(book, day.year - 1)), {})


class TestAnnualTurnover(EngineCase):
    """§ 19 (3) sentences 3 and 4 UStG in the version until 2024: a business
    that started during the year converts the turnover of that year into a
    turnover of the full year; begun months count as full months unless the
    conversion by days gives less."""

    def test_conversion_rules(self):
        # 1 October 2023: 3 begun months (32,000 €) or 92 of 365 days
        # (31,739.13 €): the lower value counts
        self.assertEqual(
            vat.annual_turnover(800000, d(2023, 10, 1), 2023),
            Fraction(800000 * 365, 92),
        )
        # 15 October 2023: 3 begun months (32,000 €) are less than 78 of 365
        # days (37,435.90 €)
        self.assertEqual(vat.annual_turnover(800000, d(2023, 10, 15), 2023), 3200000)
        # a start on 1 January, in another year or a check under the law from
        # 2025 converts nothing
        self.assertEqual(vat.annual_turnover(800000, d(2023, 1, 1), 2023), 800000)
        self.assertEqual(vat.annual_turnover(800000, d(2022, 10, 1), 2023), 800000)
        self.assertEqual(
            vat.annual_turnover(800000, d(2024, 10, 1), 2024, 2025), 800000
        )
        self.assertEqual(vat.annual_turnover(800000, None, 2023), 800000)

    def test_previous_year_converted_until_2024(self):
        """Founded on 1 October 2023 with 8,000 € in 2023: converted
        31,739.13 € exceed 22,000 € (check of 2024). The same start in 2024
        and 8,000 € in 2024 give no finding for 2025: the law from 2025 has
        no conversion (8,000 € ≤ 25,000 €)."""
        book = Book()
        book.move("Kasse", d(2023, 11, 2), ("1000", 8000), ("8195", -8000))
        book.move("Kasse", d(2024, 3, 2), ("1000", 100), ("8195", -100))
        book.founding_year = 2023
        book.activity_start = d(2023, 10, 1)
        result = self.run_year(book, 2024)
        annual = round(Fraction(800000 * 365, 92))
        self.assertEqual(
            limit_findings(result), [("ku_limit_previous_year", m.WARNING, annual)]
        )
        finding = next(f for f in result.findings if f.key == "ku_limit_previous_year")
        self.assertEqual(
            finding.params,
            {"limit": 2200000, "annual": annual, "start": d(2023, 10, 1)},
        )
        book = Book()
        book.move("Kasse", d(2024, 11, 2), ("1000", 8000), ("8195", -8000))
        book.move("Kasse", d(2025, 3, 2), ("1000", 100), ("8195", -100))
        book.founding_year = 2024
        book.activity_start = d(2024, 10, 1)
        self.assertEqual(limit_findings(self.run_year(book, 2025)), [])

    def test_founding_year_2024_converts_the_forecast(self):
        """Founded on 1 July 2024: 12,000 € received in 2024 are 23,869.57 €
        in a full year (184 of 366 days, less than 6 months: 24,000 €), above
        the limit of 22,000 € for the year of foundation; the second receipt
        exceeds it. The same in 2025 stays below 25,000 € (no conversion)."""
        for year, expected in (
            (2024, [("ku_limit_forecast_exceeded", m.WARNING, 1200000)]),
            (2025, []),
        ):
            with self.subTest(year=year):
                book = Book()
                book.move("Kasse 1", d(year, 8, 1), ("1000", 6000), ("8195", -6000))
                book.move("Kasse 2", d(year, 9, 1), ("1000", 6000), ("8195", -6000))
                book.founding_year = year
                book.activity_start = d(year, 7, 1)
                result = self.run_year(book, year)
                self.assertEqual(limit_findings(result), expected)
                if expected:
                    self.assertEqual(result.turnover.crossing_date, d(year, 9, 1))
                    finding = next(
                        f for f in result.findings if f.key.endswith("exceeded")
                    )
                    self.assertEqual(
                        finding.params["annual"],
                        round(Fraction(1200000 * 366, 184)),
                    )

    def test_near_limit_with_conversion(self):
        """80 % of the limit of the year of foundation 2024 (17,600 €) are
        reached by 9,000 € received from 1 July (17,902.17 € in a full
        year)."""
        book = Book()
        book.move("Kasse", d(2024, 8, 1), ("1000", 9000), ("8195", -9000))
        book.founding_year = 2024
        book.activity_start = d(2024, 7, 1)
        result = self.run_year(book, 2024)
        self.assertEqual(limit_findings(result), [("ku_limit_near", m.WARNING, 900000)])
        finding = next(f for f in result.findings if f.key == "ku_limit_near")
        self.assertEqual(finding.params["annual"], round(Fraction(900000 * 366, 184)))


@tagged("post_install_l10n", "post_install", "-at_install")
class TestReleaseFindingsOdoo(ScenarioCommon, AccountTestInvoicingCommon):
    """Scenarios: the year of the turnover entered in the takeover and in the
    setup; the proposal of C-71."""

    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_euer()

    def limit(self, year):
        return limit_findings(self.Run._engine_result(self.company, year))

    def test_takeover_binds_the_turnover_to_the_first_year(self):
        """PT-01 pattern: small business since 2021, Odoo from 2026. Without
        input 2026 is reported as unknown; the takeover proposes the year of
        the opening date and writes value and year into the earliest period,
        so the check of 2026 takes 30,000 € (> 25,000 €)."""
        self.set_regime("ku", date_from="2021-03-01")
        self.company.account_opening_date = "2026-01-01"
        invoice = self.sale("2026-03-10", 1000)
        self.bank("2026-03-20", 1000, matches=[(invoice, 1000)])
        self.assertEqual(self.limit(2026), [("ku_previous_year_unknown", "warning", 0)])
        self.compute(2026)
        check = self.checks(2026, "C-32")
        self.assertEqual(check.severity, "warning")
        self.assertIn("2025", check.message)
        self.assertIn("For the Year", check.message)
        # a new period for the year needs the end of the former one first
        self.assertIn("first end that period on 31 December 2025", check.message)
        self.assertIn("from 1 January 2026", check.message)
        wizard = self.env["l10n_de_euer.opening.wizard"].create(
            {"company_id": self.company.id}
        )
        self.assertEqual(wizard.prev_year_turnover_year, 2026)
        wizard.prev_year_turnover = 30000.0
        wizard.action_apply()
        period = wizard.vat_period_id
        self.assertEqual(
            (
                str(period.date_from),
                period.prev_year_turnover,
                period.prev_year_turnover_year,
            ),
            ("2021-03-01", 30000.0, 2026),
        )
        self.assertEqual(
            self.limit(2026), [("ku_limit_previous_year", "warning", 3000000)]
        )

    def test_takeover_without_turnover_keeps_it_unknown(self):
        """Regression (E5 review, C-32): a takeover of open items without a
        turnover of the previous year stores no year, so the amount field
        that shows 0.00 confirms no turnover of 0 and 2026 stays reported
        as unknown. Only the explicit confirmation stores 0 € for 2026; a
        later amount takes its place, and removing the confirmation without
        an amount makes the turnover unknown again."""
        self.set_regime("ku", date_from="2021-03-01")
        self.company.account_opening_date = "2026-01-01"
        invoice = self.sale("2026-03-10", 1000)
        self.bank("2026-03-20", 1000, matches=[(invoice, 1000)])
        unknown = [("ku_previous_year_unknown", "warning", 0)]
        Wizard = self.env["l10n_de_euer.opening.wizard"]
        wizard = Wizard.create({"company_id": self.company.id})
        period = wizard.vat_period_id
        self.assertEqual(str(period.date_from), "2021-03-01")
        self.assertEqual(
            (
                wizard.prev_year_turnover,
                wizard.prev_year_turnover_year,
                wizard.prev_year_turnover_zero,
            ),
            (0.0, 2026, False),
        )
        wizard.action_apply()
        self.assertEqual(
            (period.prev_year_turnover, period.prev_year_turnover_year), (0.0, 0)
        )
        self.assertEqual(self.limit(2026), unknown)
        wizard = Wizard.create({"company_id": self.company.id})
        wizard.prev_year_turnover_zero = True
        wizard.action_apply()
        self.assertEqual(
            (period.prev_year_turnover, period.prev_year_turnover_year), (0.0, 2026)
        )
        self.assertEqual(self.limit(2026), [])
        wizard = Wizard.create({"company_id": self.company.id})
        self.assertTrue(wizard.prev_year_turnover_zero)
        wizard.prev_year_turnover = 30000.0
        wizard.action_apply()
        self.assertEqual(
            (period.prev_year_turnover, period.prev_year_turnover_year),
            (30000.0, 2026),
        )
        wizard = Wizard.create({"company_id": self.company.id})
        self.assertFalse(wizard.prev_year_turnover_zero)
        wizard.prev_year_turnover = 0.0
        wizard.action_apply()
        self.assertEqual(
            (period.prev_year_turnover, period.prev_year_turnover_year), (0.0, 0)
        )
        self.assertEqual(self.limit(2026), unknown)
        # without a status period the takeover stores no turnover at all
        period.unlink()
        wizard = Wizard.create(
            {"company_id": self.company.id, "prev_year_turnover_zero": True}
        )
        self.assertFalse(wizard.vat_period_id)
        self.assertEqual(wizard.action_apply()["params"]["type"], "success")
        self.assertFalse(
            self.env["l10n_de_euer.vat.period"].search(
                [("company_id", "=", self.company.id)]
            )
        )

    def test_takeover_shows_the_input_of_the_chosen_period(self):
        """Regression (E5 review, C-32): amount, year and the confirmation of
        a turnover of 0 follow the status period chosen in the takeover.
        Without that the values of the first period stayed in the dialog, and
        a takeover without input wrote them into the chosen period: its
        turnover of the previous year was lost. The first period keeps its
        own input."""
        first = self.set_regime(
            "ku",
            date_from="2021-03-01",
            date_to="2025-12-31",
            prev_year_turnover_year=2021,
        )
        second = self.env["l10n_de_euer.vat.period"].create(
            {
                "company_id": self.company.id,
                "date_from": "2026-01-01",
                "regime": "ku",
                "vat_return_period": "monthly",
                "prev_year_turnover": 18000.0,
                "prev_year_turnover_year": 2026,
            }
        )

        def shown(form):
            return (
                form.prev_year_turnover,
                form.prev_year_turnover_year,
                form.prev_year_turnover_zero,
            )

        with Form(self.env["l10n_de_euer.opening.wizard"]) as form:
            self.assertEqual(form.vat_period_id, first)
            self.assertEqual(shown(form), (0.0, 2021, True))
            form.vat_period_id = second
            self.assertEqual(shown(form), (18000.0, 2026, False))
        form.record.action_apply()
        self.assertEqual(
            (second.prev_year_turnover, second.prev_year_turnover_year),
            (18000.0, 2026),
        )
        self.assertEqual(
            (first.prev_year_turnover, first.prev_year_turnover_year), (0.0, 2021)
        )
        # back to the first period: its confirmed 0 is shown again
        with Form(self.env["l10n_de_euer.opening.wizard"]) as form:
            form.vat_period_id = second
            form.vat_period_id = first
            self.assertEqual(shown(form), (0.0, 2021, True))

    def test_zero_with_year_and_the_payload(self):
        """0 € entered for the year is known; value and year are part of the
        payload of the finalisation and of the printed VAT periods."""
        period = self.set_regime(
            "ku", date_from="2018-01-01", prev_year_turnover_year=2026
        )
        invoice = self.sale("2026-03-10", 1000)
        self.bank("2026-03-20", 1000, matches=[(invoice, 1000)])
        self.assertEqual(self.limit(2026), [])
        period.prev_year_turnover_year = 0
        self.assertEqual(self.limit(2026), [("ku_previous_year_unknown", "warning", 0)])
        period.write({"prev_year_turnover": 18000.0, "prev_year_turnover_year": 2026})
        statement = self.env["l10n_de_euer.statement"].create(
            {"company_id": self.company.id, "year": 2026}
        )
        rows = statement._get_vat_period_rows()
        self.assertEqual(
            [
                (row["prev_year_turnover"], row["prev_year_turnover_year"])
                for row in rows
            ],
            [(18000, 2026)],
        )

    def test_setup_wizard_proposes_the_year_of_the_period(self):
        """The setup proposes the first year of the new period for the
        turnover entered; without a turnover the year stays empty."""
        self.env["l10n_de_euer.vat.period"].search(
            [("company_id", "=", self.company.id)]
        ).unlink()
        wizard = self.env["l10n_de_euer.setup.wizard"].create(
            {"company_id": self.company.id}
        )
        wizard.write(
            {
                "regime": "ku",
                "date_from": "2026-01-01",
                "prev_year_turnover": 12000.0,
                "switch_accounts": False,
            }
        )
        self.assertEqual(wizard.prev_year_turnover_year, 2026)
        wizard.prev_year_turnover_year = 2027
        wizard.action_apply()
        period = self.env["l10n_de_euer.vat.period"].search(
            [("company_id", "=", self.company.id)]
        )
        self.assertEqual(
            (period.prev_year_turnover, period.prev_year_turnover_year),
            (12000.0, 2027),
        )

    def test_negative_field_has_a_proposal(self):
        """C-71 names a solution (F-06, spec 5.7 no. 10): a refund of an
        expense of an earlier year that makes the expense field negative is
        business income or belongs to another field; a counter booking
        without money is deleted."""
        self.bank("2026-05-02", 50, extra=[("office", -50)])
        self.compute(2026)
        checks = self.checks(2026, "C-71")
        self.assertEqual(set(checks.mapped("severity")), {"blocker"})
        office = self.field_of("EXP_WORK_EQUIPMENT")
        message = checks.filtered(lambda check: office in check.message).message
        self.assertIn("allows no minus", message)
        self.assertIn("refund of an expense", message)
        self.assertIn("Kz 103", message)
        self.assertIn("Kz 111 and of it Kz 119", message)
        self.assertIn("without money is deleted", message)
        Form = self.env["l10n_de_euer.form"]
        income = self.env["l10n_de_euer.field"].search(
            [("box", "=", "20"), ("year", "=", 2026)], limit=1
        )
        other = self.env["l10n_de_euer.field"].search(
            [("box", "not in", ("20", "25")), ("year", "=", 2026)], limit=1
        )
        self.assertIn("refund to a customer", Form._negative_proposal(income))
        self.assertIn("drilldown", Form._negative_proposal(other))

    def test_short_name_and_review_line_without_company(self):
        """Coverage of the code of E5 (O-06): the short name under an
        official label is left out when the label contains it or one of
        them is empty; a review line without company shows the code of the
        account in the current company."""
        Statement = self.env["l10n_de_euer.statement"]
        self.assertEqual(
            Statement._fill_aid_short_name("Geschenke", "Geschenke, abziehbar"),
            "Geschenke, abziehbar",
        )
        self.assertEqual(
            Statement._fill_aid_short_name("Summe Geschenke", "Geschenke"), ""
        )
        self.assertEqual(Statement._fill_aid_short_name("", "Geschenke"), "")
        self.assertEqual(Statement._fill_aid_short_name("Geschenke", ""), "")
        account = self.acc("office")
        line = self.env["l10n_de_euer.account.review.line"].new(
            {"account_id": account.id}
        )
        self.assertFalse(line.company_id)
        self.assertEqual(line.account_code, account.code)


@tagged("post_install_l10n", "post_install", "-at_install")
class TestSmallBusinessPurchase(AssetCase):
    """A small business books purchases gross (§ 9b (1) EStG): the input tax
    that is not deductible is part of the acquisition cost, the limits of
    low-value assets and pools apply to the net cost (§ 6 (2) EStG, R 9b (2)
    EStR, PT-01: no false C-68)."""

    def setUp(self):
        super().setUp()
        self.period("ku", "2024-01-01")
        self.no_tax = self.env["account.tax"]

    def purchase_checks(self, year=2026):
        """C-68 results that compare the purchase items with the assets."""
        return [
            result["message"]
            for result in self.results("C-68", year)
            if "purchase" in result["message"]
        ]

    def test_gross_purchase_matches_the_acquisition_cost(self):
        """Regression: 1,800.00 gross on 0490 without tax item, asset with net
        cost 1,512.61 and input tax 287.39: no C-68 (before: net cost
        1,512.61 against items 1,800.00). A wrong input tax is still
        reported, with the acquisition cost in the message."""
        bill = self.bill([("Studioleuchten", self.acc("0490"), 1800.0, self.no_tax)])
        asset = self.asset(
            delivery_date="2026-03-15",
            cost_net=1512.61,
            input_tax=287.39,
            useful_life_months=84,
            move_line_ids=[Command.set(bill.invoice_line_ids.ids)],
        )
        self.assertEqual(asset.vat_treatment, "not_deductible")
        self.assertEqual(asset.acquisition_cost, 1800.0)
        self.assertEqual(self.purchase_checks(), [])
        asset.action_set_draft()
        asset.input_tax = 187.39
        asset.action_activate()
        messages = self.purchase_checks()
        self.assertEqual(len(messages), 1)
        self.assertIn("acquisition cost 1700.00", messages[0])
        self.assertIn("1800.00", messages[0])

    def test_input_tax_on_a_tax_item_and_standard_taxation(self):
        """With input tax booked on a tax item of its own (small business
        with the default tax of the chart, C-30) the product item shows the
        net cost; standard taxation compares the net cost as before."""
        bill = self.bill([("Kamera", self.acc("0490"), 1000.0, self.tax())])
        self.asset(
            delivery_date="2026-03-15",
            cost_net=1000.0,
            input_tax=190.0,
            useful_life_months=84,
            move_line_ids=[Command.set(bill.invoice_line_ids.ids)],
        )
        self.assertEqual(self.purchase_checks(), [])

    def test_shared_gross_items(self):
        """Two assets of one gross item: together the acquisition costs equal
        the item (1,000.00 + 800.00); a difference is reported."""
        bill = self.bill([("Set", self.acc("0490"), 1800.0, self.no_tax)])
        line = bill.invoice_line_ids
        first = self.asset(
            name="Leuchte",
            delivery_date="2026-03-15",
            cost_net=840.34,
            input_tax=159.66,
            useful_life_months=84,
            move_line_ids=[Command.set(line.ids)],
        )
        self.asset(
            name="Stativ",
            delivery_date="2026-03-15",
            cost_net=672.27,
            input_tax=127.73,
            useful_life_months=84,
            move_line_ids=[Command.set(line.ids)],
        )
        self.assertEqual(self.purchase_checks(), [])
        first.action_set_draft()
        first.input_tax = 59.66
        first.action_activate()
        messages = self.purchase_checks()
        self.assertEqual(len(messages), 1)
        self.assertIn("cost together 1700.00", messages[0])

    def test_wizard_splits_a_gross_purchase(self):
        """950.00 gross of a small business: with 19 % VAT shown on the
        invoice the wizard splits 798.32 net and 151.68 input tax, so the
        purchase is a low-value asset (before: 'net value 950.00 is outside
        the limits'). With 7 % the net cost is 887.85 (above 800 €). The VAT
        has no default (E5 re-check): until it is chosen the gross amount is
        the net cost and the asset cannot be created. The asset costs 950.00
        in Kz 132 and its purchase gives no C-68."""
        bill = self.bill([("Objektiv", self.acc("0490"), 950.0, self.no_tax)])
        line = bill.invoice_line_ids
        wizard = (
            self.env["l10n_de_euer.asset.from.move.line"]
            .with_context(active_model="account.move.line", active_ids=line.ids)
            .create({})
        )
        self.assertTrue(wizard.gross_split)
        self.assertFalse(wizard.vat_shown)
        self.assertEqual((wizard.cost_net, wizard.input_tax), (950.0, 0.0))
        self.assertEqual(wizard.asset_kind, "depreciable")
        with self.assertRaisesRegex(UserError, "VAT shown on the purchase invoice"):
            wizard.action_create()
        wizard.vat_shown = "19"
        self.assertEqual((wizard.cost_net, wizard.input_tax), (798.32, 151.68))
        self.assertEqual(wizard.asset_kind, "gwg")
        wizard.vat_shown = "7"
        self.assertEqual((wizard.cost_net, wizard.input_tax), (887.85, 62.15))
        self.assertEqual(wizard.asset_kind, "depreciable")
        wizard.vat_shown = "19"
        action = wizard.action_create()
        asset = self.Asset.browse(action["res_id"])
        asset.action_activate()
        self.assertEqual(
            (asset.asset_kind, asset.acquisition_cost, asset.limit_value),
            ("gwg", 950.0, 798.32),
        )
        self.assertEqual(self.contributions(2026), {"EUER.25.132": D("950.00")})
        self.assertEqual(self.purchase_checks(), [])

    def test_wizard_purchase_without_vat_is_no_low_value_asset(self):
        """Regression (E5 re-check, § 6 (2) sentence 1 EStG, R 9b (2) EStR):
        950.00 € paid to a private seller (used lens, no VAT on the invoice)
        are the net cost. Only a VAT contained in the invoice is taken out,
        so the lens is above 800 € and depreciable, not a low-value asset as
        with the former proposal of 19 % (798.32 €)."""
        bill = self.bill([("Objektiv gebraucht", self.acc("0490"), 950.0, self.no_tax)])
        line = bill.invoice_line_ids
        wizard = (
            self.env["l10n_de_euer.asset.from.move.line"]
            .with_context(active_model="account.move.line", active_ids=line.ids)
            .create({"useful_life_months": 84})
        )
        wizard.vat_shown = "none"
        self.assertEqual((wizard.cost_net, wizard.input_tax), (950.0, 0.0))
        self.assertEqual(wizard.asset_kind, "depreciable")
        action = wizard.action_create()
        asset = self.Asset.browse(action["res_id"])
        self.assertEqual(
            (asset.asset_kind, asset.acquisition_cost, asset.limit_value),
            ("depreciable", 950.0, 950.0),
        )

    def test_wizard_keeps_tax_items_and_standard_taxation(self):
        """Tax items or standard taxation: no split, no rate."""
        bill = self.bill([("Kamera", self.acc("0490"), 1000.0, self.tax())])
        wizard = (
            self.env["l10n_de_euer.asset.from.move.line"]
            .with_context(
                active_model="account.move.line", active_ids=bill.line_ids.ids
            )
            .create({})
        )
        self.assertFalse(wizard.gross_split)
        self.assertEqual((wizard.cost_net, wizard.input_tax), (1000.0, 190.0))
        self.env["l10n_de_euer.vat.period"].search(
            [("company_id", "=", self.company.id)]
        ).write({"regime": "regular"})
        bill = self.bill([("Kamera", self.acc("0490"), 950.0, self.no_tax)])
        wizard = (
            self.env["l10n_de_euer.asset.from.move.line"]
            .with_context(
                active_model="account.move.line", active_ids=bill.invoice_line_ids.ids
            )
            .create({})
        )
        self.assertFalse(wizard.gross_split)
        self.assertEqual((wizard.cost_net, wizard.input_tax), (950.0, 0.0))
