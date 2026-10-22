# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Regression tests of the re-check of review E5 (synthetic amounts, D6).

* Ten-day rule: "apply" that moves nothing (the economic year is the year of
  payment, or the due date lies outside the window) is reported (C-26)
  instead of showing "apply" with the figures of "do not apply".
* Direct debit of the tax office (R-06) with a due date on a weekend: the
  period for the payment ends on the next working day (§ 108 (3) AO) and a
  direct debit counts as paid on that day (§ 224 (2) no. 3 AO), so the
  proposal is the year of payment; "apply" stays possible with a warning.
* Small business limit (C-32): books that start during the previous year
  are named as such in the message.
* Messages of the small business limit without a conversion into a full
  year (coverage of the code of E5, O-06).
"""

from decimal import Decimal as D

from odoo.tests import TransactionCase, tagged

from ..models.engine import model as m
from .engine_common import Book, EngineCase, codes, d, fields

RENT = "EUER.25.150"
VAT_PAID = "EUER.25.186"


def findings(result, key):
    return [f for f in result.findings if f.key == key]


class TestTenDayApplyWithoutEffect(EngineCase):
    """A decision "apply" must move the payment (R-07, E5 re-check)."""

    def rent(self, book, invoice_day, due, paid, amount=800):
        bill = book.move(
            "Miete",
            invoice_day,
            ("4210", amount),
            ("1610", -amount),
            move_type="in_invoice",
            partner="Vermieter",
            invoice_date=invoice_day,
            invoice_date_due=due,
        )
        bank = book.move(
            "Bank", paid, ("1610", amount), ("1200", -amount), partner="Vermieter"
        )
        book.rec(bill("1610"), bank("1610"))
        return bill, bank

    def book(self):
        """Rent of 800 € paid monthly in 2026; the rent of January 2027 is
        invoiced on 15.12.2026, due on 01.01.2027 and paid on 28.12.2026.
        The economic year derived from the invoice is 2026, the year of
        payment (proposal "review")."""
        book = Book()
        for month in range(1, 12):
            day = d(2026, month, 15)
            self.rent(book, d(2026, month, 1), day, day)
        bill, bank = self.rent(book, d(2026, 12, 15), d(2027, 1, 1), d(2026, 12, 28))
        return book, bill, bank

    def test_apply_with_the_year_of_payment_is_reported(self):
        """Regression (pattern of the private regression case): "apply"
        without changing the derived economic year 2026 moved nothing and
        no check remained. Now C-26 blocks both years until the economic
        year 2027 is entered; then the January rent belongs to 2027."""
        book, bill, bank = self.book()
        book.decide(bank, bill, "apply")
        for year in (2026, 2027):
            result = self.run_year(book, year)
            self.assertIn("C-26", codes(result, m.BLOCKER))
            (finding,) = findings(result, "ten_day_apply_without_effect")
            self.assertEqual(
                (finding.params["economic_year"], finding.params["payment_year"]),
                (2026, 2026),
            )
        result = self.run_year(book, 2026)
        self.assertEqual(fields(result), {RENT: D("9600.00")})
        (item,) = (i for i in result.ten_day_items if i.origin_move_id == bill.move.id)
        self.assertEqual((item.decision, item.applied), ("apply", False))
        book.decisions.clear()
        book.decide(bank, bill, "apply", economic_year=2027)
        result = self.run_year(book, 2026)
        self.assertNotIn("C-26", codes(result))
        self.assertEqual(fields(result), {RENT: D("8800.00")})
        self.assertEqual(fields(self.run_year(book, 2027)), {RENT: D("800.00")})

    def test_apply_with_a_due_date_outside_the_window(self):
        """A due date entered outside the window (15.01.2027): the rule
        cannot apply (BFH X R 2/21), "apply" is reported, the payment stays
        in 2026."""
        book, bill, bank = self.book()
        book.decide(bank, bill, "apply", economic_year=2027, due_date=d(2027, 1, 15))
        result = self.run_year(book, 2026)
        (finding,) = findings(result, "ten_day_apply_without_effect")
        self.assertEqual(finding.severity, m.BLOCKER)
        self.assertEqual(finding.params["due_date"], d(2027, 1, 15))
        self.assertEqual(finding.params["window"], (d(2026, 12, 22), d(2027, 1, 10)))
        self.assertEqual(fields(result), {RENT: D("9600.00")})

    def test_reject_needs_no_effect(self):
        book, bill, bank = self.book()
        book.decide(bank, bill, "reject")
        result = self.run_year(book, 2026)
        self.assertNotIn("C-26", codes(result))
        self.assertEqual(fields(result), {RENT: D("9600.00")})


class TestDirectDebitWeekend(EngineCase):
    """R-06 with the due dates of the supported years (10.01.2026 Saturday,
    10.01.2027 Sunday) and a working day (10.01.2025 Friday)."""

    def book(self, day, period, extension=False):
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
            f"Lastschrift {period}",
            day,
            ("1780", 510),
            ("1200", -510),
            partner="Finanzamt",
            info={"vat_period": period},
        )
        return book, bank

    def item(self, result):
        return next(i for i in result.ten_day_items if i.kind == "direct_debit_tax")

    def test_weekend_due_date_proposes_the_year_of_payment(self):
        """Regression: the direct debit of Monday 12.01.2026 for 12/2025 (due
        Saturday 10.01.2026) and of Monday 11.01.2027 for 11/2026 with
        extension (due Sunday 10.01.2027) were proposed for the previous
        year. Now the proposal is "do not apply" with the shifted due date in
        the check; "apply" moves the payment, but a warning asks for the
        confirmation of the tax advisor."""
        for day, period, extension, shifted in (
            (d(2026, 1, 12), "2025-12", False, d(2026, 1, 12)),
            (d(2027, 1, 11), "2026-11", True, d(2027, 1, 11)),
        ):
            with self.subTest(day=day):
                book, bank = self.book(day, period, extension)
                result = self.run_year(book, day.year)
                item = self.item(result)
                self.assertEqual(item.proposal, "reject")
                (finding,) = findings(result, "direct_debit_undecided")
                self.assertEqual(finding.params["shifted_due_date"], shifted)
                book.decide(bank, bank, "apply", kind="direct_debit_tax")
                result = self.run_year(book, day.year - 1)
                self.assertEqual(fields(result), {VAT_PAID: D("510.00")})
                (warning,) = findings(result, "direct_debit_weekend_applied")
                self.assertEqual(warning.severity, m.WARNING)
                self.assertEqual(
                    (warning.params["due_date"], warning.params["shifted_due_date"]),
                    (d(day.year, 1, 10), shifted),
                )
                self.assertNotIn("C-26", codes(result, m.BLOCKER))

    def test_working_day_due_date_proposes_apply(self):
        """12/2024 is due on Friday 10.01.2025; the direct debit of Monday
        13.01.2025 counts as paid on the due date (§ 224 (2) no. 3 AO):
        proposal "apply", no warning after the decision."""
        book, bank = self.book(d(2025, 1, 13), "2024-12")
        result = self.run_year(book, 2025)
        self.assertEqual(self.item(result).proposal, "apply")
        (finding,) = findings(result, "direct_debit_undecided")
        self.assertIsNone(finding.params["shifted_due_date"])
        book.decide(bank, bank, "apply", kind="direct_debit_tax")
        result = self.run_year(book, 2024)
        self.assertEqual(fields(result), {VAT_PAID: D("510.00")})
        self.assertFalse(findings(result, "direct_debit_weekend_applied"))
        # a due date entered on a working day replaces the weekend
        book, bank = self.book(d(2026, 1, 12), "2025-12")
        book.decide(
            bank, bank, "apply", kind="direct_debit_tax", due_date=d(2026, 1, 9)
        )
        result = self.run_year(book, 2025)
        self.assertEqual(fields(result), {VAT_PAID: D("510.00")})
        self.assertFalse(findings(result, "direct_debit_weekend_applied"))


class TestPreviousYearPartial(EngineCase):
    """C-32 names the start of books that begin during the previous year."""

    def test_books_start_during_the_previous_year(self):
        """Opening entry of 01.07.2025, money from July: the check of 2026
        names the start of the books; without opening entry the turnover of
        2025 is known (the books count as complete, ROADMAP)."""
        book = Book()
        book.set_regimes(m.VatPeriod(date_from=d(2018, 1, 1), regime="ku"))
        book.move(
            "Eröffnung", d(2025, 7, 1), ("1200", 500), ("9000", -500), opening=True
        )
        book.move("Kasse", d(2025, 8, 2), ("1000", 800), ("8195", -800))
        book.move("Kasse", d(2026, 3, 2), ("1000", 100), ("8195", -100))
        (finding,) = findings(self.run_year(book, 2026), "ku_previous_year_unknown")
        self.assertEqual(
            finding.params, {"limit": 2500000, "books_start": d(2025, 7, 1)}
        )
        (finding,) = findings(self.run_year(book, 2025), "ku_previous_year_unknown")
        self.assertEqual(finding.params, {"limit": 2500000})


@tagged("post_install", "-at_install")
class TestRecheckMessages(TransactionCase):
    """Messages of the new findings and of the limit warnings without a
    conversion into a full year."""

    def message(self, key, year=2026, **params):
        finding = m.Finding(
            code="C-26", severity=m.BLOCKER, key=key, years=(year,), params=params
        )
        currency = self.env.ref("base.EUR")
        return self.env["l10n_de_euer.calculation.checker"]._message(
            finding, [finding], year, [], [], currency
        )

    def test_ten_day_apply_without_effect(self):
        same = self.message(
            "ten_day_apply_without_effect",
            economic_year=2026,
            payment_year=2026,
            due_date=d(2027, 1, 1),
        )
        self.assertIn("economic year 2026 is the year of payment", same)
        self.assertIn("the year after for a payment in December", same)
        outside = self.message(
            "ten_day_apply_without_effect",
            economic_year=2027,
            payment_year=2026,
            due_date=d(2027, 1, 15),
            window=(d(2026, 12, 22), d(2027, 1, 10)),
        )
        self.assertIn("2027-01-15 lies outside the window", outside)
        self.assertIn("2026-12-22 to 2027-01-10", outside)

    def test_direct_debit_weekend(self):
        undecided = self.message(
            "direct_debit_undecided",
            due_date=d(2026, 1, 10),
            shifted_due_date=d(2026, 1, 12),
        )
        self.assertIn("ends on 2026-01-12 (§ 108 (3) AO)", undecided)
        self.assertIn("Proposal: Do Not Apply", undecided)
        working_day = self.message(
            "direct_debit_undecided", due_date=d(2025, 1, 10), shifted_due_date=None
        )
        self.assertNotIn("weekend", working_day)
        applied = self.message(
            "direct_debit_weekend_applied",
            due_date=d(2026, 1, 10),
            shifted_due_date=d(2026, 1, 12),
        )
        self.assertIn("confirmation of your tax advisor", applied)

    def test_previous_year_unknown_and_partial(self):
        unknown = self.message("ku_previous_year_unknown", limit=2500000)
        self.assertIn("Odoo holds no bookings of that year", unknown)
        self.assertIn("from 1 January 2026", unknown)
        partial = self.message(
            "ku_previous_year_unknown", limit=2500000, books_start=d(2025, 7, 1)
        )
        self.assertIn("incomplete in Odoo", partial)
        self.assertIn("opening entry of 2025-07-01", partial)
        self.assertNotIn("Odoo holds no bookings", partial)

    def test_limit_messages_without_conversion(self):
        """From 2025 (or without a start during the year) no turnover is
        converted: the messages carry no sentence on a full year."""
        for key in (
            "ku_limit_near",
            "ku_limit_previous_year",
            "ku_limit_forecast_exceeded",
        ):
            with self.subTest(key=key):
                text = self.message(key, limit=2500000)
                self.assertIn("25,000.00", text.replace("\xa0", " "))
                self.assertNotIn("converted", text)
        converted = self.message(
            "ku_limit_near", limit=1760000, annual=1790217, start=d(2024, 7, 1)
        )
        self.assertIn("converted into a turnover of the full year", converted)
