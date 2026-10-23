# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Unit tests of the engine rules (spec 9.2), no database.

UT-ROLE, UT-TAX, UT-RND (including 3,000 random cases), UT-10T, UT-KU and
UT-DET, plus the building blocks (rounding, VAT periods, direct rules).
"""

import dataclasses
import random
import time
from decimal import Decimal as D
from fractions import Fraction

from odoo.tests import BaseCase

from ..models.engine import direct, engine, rounding, ten_day, vat
from ..models.engine import model as m
from ..models.engine.parameters import Parameters
from .engine_common import Book, EngineCase, codes, d, fields, ln

KU = "EUER.20.111"
KU_DAVON = "EUER.20.119"
TAXABLE = "EUER.20.112"
OUTPUT_VAT = "EUER.20.140"
REFUND = "EUER.20.141"
INPUT_VAT = "EUER.25.185"
VAT_PAID = "EUER.25.186"
WORK = "EUER.25.229"
SERVICES = "EUER.25.110"
WITHDRAWAL = "EUER.29.122"


class TestRounding(BaseCase):
    def test_hamilton_t03(self):
        """UT-RND-1: 500 of 1,404 (spec 5.10, odoo_technik.pruefung #16)."""
        raw = {
            1: Fraction(-100000 * 500, 1404),
            2: Fraction(-19000 * 500, 1404),
            3: Fraction(-20000 * 500, 1404),
            4: Fraction(-1400 * 500, 1404),
        }
        self.assertEqual(
            rounding.apportion(raw, -50000),
            {1: -35613, 2: -6766, 3: -7122, 4: -499},
        )

    def test_tie_goes_to_smaller_key(self):
        raw = {2: Fraction(1, 2), 1: Fraction(1, 2)}
        self.assertEqual(rounding.apportion(raw, 1), {1: 1, 2: 0})
        self.assertEqual(
            rounding.apportion(raw | {3: Fraction(-1)}, 0), {1: 1, 2: 0, 3: -1}
        )

    def test_negative_total_symmetric(self):
        raw = {1: Fraction(-1, 3), 2: Fraction(-1, 3), 3: Fraction(-1, 3)}
        self.assertEqual(sum(rounding.apportion(raw, -1).values()), -1)
        self.assertEqual(rounding.apportion(raw, -1), {1: -1, 2: 0, 3: 0})

    def test_differing_total_and_caps(self):
        raw = {1: Fraction(5), 2: Fraction(5, 2), 3: Fraction(5, 2)}
        self.assertEqual(sum(rounding.apportion(raw, 12).values()), 12)
        result = rounding.apportion(raw, 10, caps={2: 2})
        self.assertEqual(result[2], 2)
        self.assertEqual(sum(result.values()), 10)
        with self.assertRaises(ValueError):
            rounding.apportion({1: Fraction(5)}, 5, caps={1: 4})
        with self.assertRaises(ValueError):
            rounding.apportion({}, 1)
        self.assertEqual(rounding.apportion({}, 0), {})

    def test_gross_split(self):
        self.assertEqual(rounding.gross_split(1190000, 19), (1000000, 190000))
        self.assertEqual(rounding.gross_split(11900, 7), (11121, 779))
        self.assertEqual(rounding.gross_split(-119, 19), (-100, -19))
        self.assertEqual(rounding.gross_split(500, 0), (500, 0))
        self.assertEqual(
            rounding.split_by_shares(10001, {1: "0.7", 2: "0.3"}), {1: 7001, 2: 3000}
        )


class TestRoles(EngineCase):
    """UT-ROLE-1…4: structure rule for private lines (5.2, B-09, K-11)."""

    def roles(self, book, entry):
        result = book.run(entry.move.date.year)
        names = {account.id: code for code, account in book.accounts.items()}
        roles = {
            (names[line.account_id], line.balance): result.roles[line.id]
            for line in entry.lines
        }
        return roles, result.classes[entry.move.id]

    def test_ut_role_1_withdrawal(self):
        book = Book()
        entry = book.move("Entnahme", d(2027, 2, 1), ("1800", 500), ("1200", -500))
        roles, cls = self.roles(book, entry)
        self.assertEqual(set(roles.values()), {m.L})
        self.assertEqual(cls, m.SOURCE)

    def test_ut_role_2_paid_privately(self):
        book = Book()
        entry = book.move("Privat", d(2027, 2, 1), ("1610", 50), ("1890", -50))
        roles, cls = self.roles(book, entry)
        self.assertEqual(roles[("1890", -5000)], m.L)
        self.assertEqual(roles[("1610", 5000)], m.R)
        self.assertEqual(cls, m.SOURCE)

    def test_ut_role_3_non_deductible_share(self):
        """Private share of a vendor bill: N line "withdrawal at payment"."""
        book = Book(regime="regular")
        entry = book.move(
            "ER 50 %",
            d(2027, 2, 1),
            ln("4930", 100, tax="vst19"),
            ln("1576", 19, rep="vst19"),
            ln("4930", -50, tax="vst19"),
            ln("1576", -9.50, rep="vst19"),
            ln("1800", 59.50),
            ln("1610", -119),
            move_type="in_invoice",
        )
        roles, cls = self.roles(book, entry)
        self.assertEqual(roles[("1800", 5950)], m.N)
        self.assertEqual(cls, m.DOC)

    def test_ut_role_4_invoice_line_on_private_account(self):
        book = Book()
        entry = book.move(
            "ER",
            d(2027, 2, 1),
            ("4930", 80),
            ("1800", 20),
            ("1610", -100),
            move_type="in_invoice",
        )
        roles, cls = self.roles(book, entry)
        self.assertEqual(roles[("1800", 2000)], m.N)
        self.assertEqual(cls, m.DOC)

    def test_fixed_asset_line_blocks_private_money(self):
        """AB-05: an asset line keeps the private line from being money."""
        book = Book()
        entry = book.move(
            "ER Anlage",
            d(2027, 2, 1),
            ("0490", 900),
            ("1800", 100),
            ("1610", -1000),
            move_type="in_invoice",
        )
        roles, _cls = self.roles(book, entry)
        self.assertEqual(roles[("1800", 10000)], m.N)

    def test_reconcilable_expense_account_stays_n(self):
        """B-04: an income or expense account with reconcile is no open item."""
        book = Book()
        book.account("4930", reconcile=True)
        entry = book.move("ER", d(2027, 2, 1), ("4930", 80), ("1610", -80))
        roles, cls = self.roles(book, entry)
        self.assertEqual(roles[("4930", 8000)], m.N)
        self.assertEqual(cls, m.DOC)


class TestTaxLines(EngineCase):
    """UT-TAX-1…6: tax lines by repartition line in every class (B-17)."""

    def test_ut_tax_1_output_tax_in_source(self):
        book = Book(regime="regular")
        book.move(
            "Kasse",
            d(2027, 3, 1),
            ln("1000", 119),
            ln("8400", -100, tax="ust19"),
            ln("1776", -19, rep="ust19"),
        )
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {TAXABLE: D("100.00"), OUTPUT_VAT: D("19.00")})

    def test_ut_tax_2_input_tax_in_source(self):
        book = Book(regime="regular")
        book.move(
            "Bank",
            d(2027, 3, 1),
            ln("4930", 100, tax="vst19"),
            ln("1576", 19, rep="vst19"),
            ln("1200", -119),
        )
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {WORK: D("100.00"), INPUT_VAT: D("19.00")})
        self.assertEqual(codes(result), [])

    def _reverse_charge(self, regime):
        book = Book(regime=regime)
        bill = book.move(
            "ER 13b",
            d(2027, 3, 1),
            ln("4925", 100, tax="rc19"),
            ln("1577", 19, rep="rc19"),
            ln("1787", -19, rep="rc19-"),
            ln("1610", -100),
            move_type="in_invoice",
        )
        bank = book.move("Bank", d(2027, 3, 10), ("1610", 100), ("1200", -100))
        book.rec(bill("1610"), bank("1610"))
        book.move(
            "USt 13b an FA",
            d(2027, 4, 10),
            ("1780", 19),
            ("1200", -19),
            partner="Finanzamt",
        )
        return self.run_year(book, 2027)

    def test_ut_tax_3_reverse_charge_neutral(self):
        """R-22, ST-U07, ST-K05: 13b lines neutral, payment to the office 186."""
        for regime in ("regular", "ku"):
            result = self._reverse_charge(regime)
            self.assertEqual(
                fields(result), {SERVICES: D("100.00"), VAT_PAID: D("19.00")}
            )

    def test_ut_tax_4_not_in_tax_closing(self):
        book = Book(regime="regular")
        rep_values = {"use_in_tax_closing": False}
        book.move(
            "Bank",
            d(2027, 3, 1),
            ln("4930", 100, tax="vst19"),
            ln("1576", 19, rep="vst19", rep_values=rep_values),
            ln("1200", -119),
        )
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {WORK: D("119.00")})

    def test_ut_tax_5_input_tax_small_business(self):
        """R-28, ST-K10: input VAT of a small business follows the base line."""
        book = Book()
        bill = book.move(
            "ER",
            d(2027, 3, 1),
            ln("3200", 100, tax="vst19"),
            ln("1576", 19, rep="vst19"),
            ln("1610", -119),
            move_type="in_invoice",
        )
        asset = book.move(
            "ER Anlage",
            d(2027, 3, 2),
            ln("0490", 1000, tax="vst19"),
            ln("1576", 190, rep="vst19"),
            ln("1610", -1190),
            move_type="in_invoice",
        )
        bank = book.move("Bank", d(2027, 3, 10), ("1610", 1309), ("1200", -1309))
        bank_line = bank("1610")
        book.rec(bill("1610"), bank_line, 119)
        book.rec(asset("1610"), bank_line, 1190)
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {"EUER.25.100": D("119.00")})
        self.assertNotIn(INPUT_VAT, result.totals)
        asset_amount = sum(result.slices[i].amount for i in result.asset_slices)
        self.assertEqual(asset_amount, 100000)

    def test_ut_tax_6_input_tax_credit_reduces(self):
        """Cash discount via reconciliation model with tax: 185 = 190 − 3.80."""
        book = Book(regime="regular")
        bill = book.move(
            "ER",
            d(2027, 3, 1),
            ln("4930", 1000, tax="vst19"),
            ln("1576", 190, rep="vst19"),
            ln("1610", -1190),
            move_type="in_invoice",
        )
        bank = book.move(
            "Bank",
            d(2027, 3, 8),
            ln("1610", 1190),
            ln("4930", -20, tax="vst19"),
            ln("1576", -3.80, rep="vst19"),
            ln("1200", -1166.20),
        )
        book.rec(bill("1610"), bank("1610"))
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {WORK: D("980.00"), INPUT_VAT: D("186.20")})


class TestRandomRounding(BaseCase):
    """UT-RND-2/3: splits over partials keep the money reconciliation (B-06)."""

    def check(self, result):
        self.assertEqual(result.invariants["money"], {})
        self.assertEqual(result.invariants["documents"], {})
        self.assertEqual(result.invariants["transit"], {})

    def collective(self, amounts, bank_amounts):
        book = Book()
        invoices = []
        for index, amount in enumerate(amounts):
            lines = [ln("1410", amount)]
            first = round(amount * 0.6, 2)
            lines += [ln("8195", -first), ln("2650", -round(amount - first, 2))]
            invoices.append(
                book.move(f"RE{index}", d(2027, 5, 1), *lines, move_type="out_invoice")
            )
        total = round(sum(amounts), 2)
        payment = book.move(
            "Sammelzahlung",
            d(2027, 5, 30),
            ("OR", total),
            *[("1410", -amount) for amount in amounts],
        )
        for index, invoice in enumerate(invoices):
            book.rec(invoice("1410"), payment("1410", index))
        for number, bank_amount in enumerate(bank_amounts):
            bank = book.move(
                f"Bank{number}",
                d(2027, 6, 1 + number),
                ("1200", bank_amount),
                ("OR", -bank_amount),
            )
            book.rec(payment("OR"), bank("OR"), bank_amount)
        return book

    def test_ut_rnd_2_known_case(self):
        """0.70 / 145.95 / 87.28 €, bank 74.97 € (pruefung #17)."""
        result = self.collective([0.70, 145.95, 87.28], [74.97]).run(2027)
        self.check(result)
        realised = sum(s.amount for s in result.slices if s.kind == m.DOC_VIA_PAYMENT)
        self.assertEqual(realised, -7497)
        self.assertEqual(fields(result)[KU], D("74.97"))

    def test_ut_rnd_3_random_cases(self):
        """3,000 random collective payments: 0 cent deviation (seeded)."""
        generator = random.Random(20261018)
        for _case in range(3000):
            amounts = [
                generator.randint(1, 50000) / 100
                for _n in range(generator.randint(1, 4))
            ]
            total = int(round(sum(amounts) * 100))
            parts = sorted(
                generator.randint(1, total) for _n in range(generator.randint(1, 3))
            )
            banks, previous = [], 0
            for part in parts:
                if part > previous:
                    banks.append((part - previous) / 100)
                    previous = part
            result = self.collective(amounts, banks).run(2027)
            with self.subTest(amounts=amounts, banks=banks):
                self.check(result)
                realised = sum(
                    s.amount for s in result.slices if s.kind == m.DOC_VIA_PAYMENT
                )
                self.assertEqual(realised, -int(round(sum(banks) * 100)))


class TestTenDayWindow(BaseCase):
    """UT-10T-1…7: window, due dates (R-07, R-08, Z-10 to Z-14)."""

    def setUp(self):
        super().setUp()
        self.window = ten_day.Window(Parameters(Book().parameters))

    def test_ut_10t_1_window_bounds(self):
        self.assertIsNone(self.window.base_year(d(2026, 12, 21)))
        self.assertEqual(self.window.base_year(d(2026, 12, 22)), 2026)
        self.assertEqual(self.window.base_year(d(2027, 1, 10)), 2026)
        self.assertIsNone(self.window.base_year(d(2027, 1, 11)))

    def test_ut_10t_2_due_outside_window(self):
        """BFH X R 2/21: payment in the window, due date outside."""
        self.assertFalse(self.window.applies(d(2027, 1, 5), 2026, d(2027, 1, 15)))
        self.assertTrue(self.window.applies(d(2027, 1, 5), 2026, d(2027, 1, 2)))
        self.assertFalse(self.window.applies(d(2027, 1, 5), 2027, d(2027, 1, 2)))
        self.assertFalse(self.window.applies(d(2027, 1, 5), None, d(2027, 1, 2)))

    def test_ut_10t_3_due_on_sunday_paid_monday(self):
        """BFH VIII R 34/12 (followed by X R 44/16, para. 15): due 10.01.
        (Sunday), paid 11.01. → year of payment."""
        self.assertFalse(self.window.applies(d(2027, 1, 11), 2026, d(2027, 1, 10)))
        self.assertTrue(self.window.applies(d(2027, 1, 10), 2026, d(2027, 1, 10)))

    def test_ut_10t_4_to_7_vat_due_dates(self):
        cases = {
            ("2026-12", False): d(2027, 1, 10),
            ("2026-12", True): d(2027, 2, 10),
            ("2026-11", True): d(2027, 1, 10),
            ("2026-11", False): d(2026, 12, 10),
            ("2026-Q4", False): d(2027, 1, 10),
            ("2026-Q4", True): d(2027, 2, 10),
            ("2026-Q1", False): d(2026, 4, 10),
            ("2026", False): None,
            ("2026-13", False): None,
            ("Dezember", False): None,
        }
        for (period, extension), due in cases.items():
            with self.subTest(period=period, extension=extension):
                self.assertEqual(vat.vat_due_date(period, extension), due)
        self.assertFalse(self.window.applies(d(2027, 1, 8), 2026, d(2027, 2, 10)))

    def test_regular_rhythm_needs_enough_payments(self):
        """A rhythm of 1, 3, 6 or 12 months needs two dates, or two earlier
        dates and the day of the payment; one earlier payment is none."""
        self.assertFalse(ten_day._regular_dates([d(2026, 12, 10)]))
        self.assertFalse(ten_day._regular_dates([d(2026, 12, 10)], d(2027, 1, 10)))
        self.assertTrue(
            ten_day._regular_dates([d(2026, 11, 10), d(2026, 12, 10)], d(2027, 1, 10))
        )
        self.assertFalse(
            ten_day._regular_dates([d(2026, 10, 10), d(2026, 12, 10)], d(2027, 1, 10))
        )


class TestTenDayScenarios(EngineCase):
    """UT-10T-8…10: refund, prepayment, non-recurring, rent without flag."""

    def test_ut_10t_8_refund_in_window(self):
        """A refund is due with the consent of the tax office (§ 168 S. 2,
        § 220 (2) S. 2 AO), not on the 10th: decided automatically only with
        an entered date, otherwise C-26 with the date of receipt."""
        book = Book(regime="regular")
        refund = book.move(
            "Erstattung",
            d(2027, 1, 7),
            ("1200", 80),
            ("1780", -80),
            partner="Finanzamt",
            info={"vat_period": "2026-12"},
        )
        result = self.run_year(book, 2026)
        self.assertIn("C-26", codes(result, m.BLOCKER))
        self.assertEqual(result.ten_day_items[0].due_date, d(2027, 1, 7))
        self.assertEqual(fields(result), {})
        book.decide(refund, refund, "apply")
        self.assertEqual(fields(self.run_year(book, 2026)), {REFUND: D("80.00")})
        book = Book(regime="regular")
        book.move(
            "Erstattung",
            d(2027, 1, 7),
            ("1200", 80),
            ("1780", -80),
            partner="Finanzamt",
            info={"vat_period": "2026-12", "due_date": d(2027, 1, 5)},
        )
        result = self.run_year(book, 2026)
        self.assertNotIn("C-26", codes(result))
        self.assertEqual(result.ten_day_items[0].decision, "auto")
        self.assertEqual(fields(result), {REFUND: D("80.00")})

    def test_ut_10t_9_prepayment_for_next_year(self):
        book = Book()
        bank = book.move(
            "Miete Januar",
            d(2026, 12, 28),
            ("4210", 500),
            ("1200", -500),
            partner="Vermieter",
            info={"economic_year": 2027, "due_date": d(2027, 1, 1)},
        )
        result = self.run_year(book, 2026)
        # economic year and due date entered: decided automatically
        self.assertNotIn("C-26", codes(result))
        item = result.ten_day_items[0]
        self.assertEqual(item.reasons, ("flag", "economic_year_entered"))
        self.assertEqual((item.decision, item.applied), ("auto", True))
        self.assertEqual(fields(result), {})
        book.decide(bank, bank, "apply")
        self.assertEqual(fields(self.run_year(book, 2026)), {})
        self.assertEqual(
            fields(self.run_year(book, 2027)), {"EUER.25.150": D("500.00")}
        )

    def test_ut_10t_9b_not_recurring(self):
        book = Book()
        invoice = book.move(
            "RE",
            d(2026, 12, 15),
            ("1410", 500),
            ("8195", -500),
            move_type="out_invoice",
            partner="Kunde",
        )
        bank = book.move("Bank", d(2027, 1, 5), ("1200", 500), ("1410", -500))
        book.rec(invoice("1410"), bank("1410"))
        result = self.run_year(book, 2027)
        self.assertEqual(result.ten_day_items, [])
        self.assertEqual(fields(result), {KU: D("500.00")})

    def test_ut_10t_10_rent_without_flags(self):
        """Monthly rent, no flags: candidate from the data, C-26 until decided."""
        book = Book()
        book.account("4210", assignments=(m.Assignment(category="EXP_PROPERTY"),))
        for month in range(1, 13):
            bill = book.move(
                f"Miete {month}",
                d(2026, month, 1),
                ("4210", 600),
                ("1610", -600),
                move_type="in_invoice",
                partner="Vermieter",
                invoice_date=d(2026, month, 1),
                invoice_date_due=d(2027, 1, 1) if month == 12 else d(2026, month, 3),
            )
            day = d(2027, 1, 5) if month == 12 else d(2026, month, 3)
            bank = book.move(
                f"Bank {month}",
                day,
                ("1610", 600),
                ("1200", -600),
                partner="Vermieter",
            )
            book.rec(bill("1610"), bank("1610"))
        result = self.run_year(book, 2026)
        items = [i for i in result.ten_day_items if i.kind == "recurring"]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.reasons, ("other_year_due", "repeated_partner"))
        self.assertEqual((item.economic_year, item.due_date), (2026, d(2027, 1, 1)))
        # outgoing money on 3 or 5 January: transfer order date unknown (C-23)
        self.assertIn("C-23", codes(result))
        self.assertIn("C-26", codes(result, m.BLOCKER))
        self.assertEqual(fields(result), {"EUER.25.151": D("6600.00")})
        book.decide(bank, bill, "apply")
        result = self.run_year(book, 2026)
        self.assertNotIn("C-26", codes(result))
        self.assertEqual(fields(result), {"EUER.25.151": D("7200.00")})
        self.assertEqual(fields(self.run_year(book, 2027)), {})

    def test_candidate_per_document_after_credit_note(self):
        """Lines of a credit note realised through the bill: one candidate."""
        book = Book(regime="regular")
        bill = book.move(
            "Miete",
            d(2026, 12, 1),
            ln("4210", 1000, tax="vst19"),
            ln("1576", 190, rep="vst19"),
            ln("1610", -1190),
            move_type="in_invoice",
            partner="Vermieter",
            invoice_date_due=d(2027, 1, 1),
        )
        credit = book.move(
            "Gutschrift",
            d(2026, 12, 15),
            ln("4210", -100, tax="vst19"),
            ln("1576", -19, rep="vst19"),
            ln("1610", 119),
            move_type="in_refund",
            partner="Vermieter",
        )
        book.rec(bill("1610"), credit("1610"))
        bank = book.move(
            "Bank", d(2027, 1, 8), ("1610", 1071), ("1200", -1071), partner="Vermieter"
        )
        book.rec(bill("1610"), bank("1610"))
        result = self.run_year(book, 2027)
        items = [i for i in result.ten_day_items if i.kind == "recurring"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].origin_move_id, bill.move.id)
        book.decide(bank, bill, "apply")
        self.assertEqual(
            fields(self.run_year(book, 2026)),
            {"EUER.25.150": D("900.00"), INPUT_VAT: D("171.00")},
        )

    def test_reject_keeps_payment_year(self):
        book = Book(regime="regular")
        bank = book.move(
            "Bank",
            d(2027, 1, 8),
            ("1780", 300),
            ("1200", -300),
            partner="Finanzamt",
            info={"vat_period": "2026-12"},
        )
        book.decide(bank, bank, "reject")
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {VAT_PAID: D("300.00")})
        self.assertEqual(result.ten_day_items[0].decision, "reject")

    def test_direct_debit_tax_office(self):
        """R-06, ST-U05: direct debit 12.01. takes the due date 10.01."""
        book = Book(regime="regular")
        bank = book.move(
            "Lastschrift FA",
            d(2027, 1, 12),
            ("1780", 300),
            ("1200", -300),
            partner="Finanzamt",
            info={"vat_period": "2026-12"},
        )
        result = self.run_year(book, 2027)
        self.assertIn("C-26", codes(result, m.BLOCKER))
        self.assertEqual(result.ten_day_items[0].proposed_cash_date, d(2027, 1, 10))
        book.decisions.append(
            m.TenDayDecision(
                source_move_id=bank.move.id,
                origin_move_id=bank.move.id,
                decision="apply",
                kind="direct_debit_tax",
            )
        )
        self.assertEqual(fields(self.run_year(book, 2026)), {VAT_PAID: D("300.00")})
        self.assertEqual(fields(self.run_year(book, 2027)), {})

    def test_direct_debit_tax_office_on_the_bank_line(self):
        """R-06: the tax office as partner of the bank line (statement
        import) makes the debit a tax payment; the entry has no partner."""
        book = Book(regime="regular")
        book.partner("Finanzamt", tax_office=True)
        book.move(
            "Lastschrift",
            d(2027, 1, 12),
            ln("1200", -300, partner="Finanzamt"),
            ln("1780", 300),
            info={"vat_period": "2026-12"},
        )
        result = self.run_year(book, 2027)
        self.assertIn("C-26", codes(result, m.BLOCKER))
        item = result.ten_day_items[0]
        self.assertEqual(
            (item.kind, item.proposed_cash_date), ("direct_debit_tax", d(2027, 1, 10))
        )

    def test_direct_debit_on_the_settlement_account(self):
        """R-06: the VAT settlement account makes the debit a tax payment,
        whatever the partner of the bank line."""
        book = Book(regime="regular")
        book.move(
            "Lastschrift",
            d(2027, 1, 12),
            ln("1200", -300, partner="Hausbank"),
            ln("1780", 300),
            info={"vat_period": "2026-12"},
        )
        result = self.run_year(book, 2027)
        self.assertIn("C-26", codes(result, m.BLOCKER))
        self.assertEqual(result.ten_day_items[0].kind, "direct_debit_tax")

    def test_direct_debit_of_a_supplier_is_no_tax_payment(self):
        """R-06 concerns the tax office only: a debit in January with a VAT
        period, but neither the tax office nor the settlement account, gets
        no proposal."""
        book = Book(regime="regular")
        book.move(
            "Lastschrift",
            d(2027, 1, 12),
            ln("1200", -300, partner="Versorger"),
            ln("2150", 300, partner="Versorger"),
            info={"vat_period": "2026-12"},
        )
        result = self.run_year(book, 2027)
        self.assertEqual(result.ten_day_items, [])
        self.assertNotIn("C-26", codes(result))

    def test_direct_debit_without_due_date_in_january(self):
        """R-06 only for returns due on 10 January: the annual return has no
        fixed due date, October is due on 10 November; debited in January
        both are paid in the year of payment, without a proposal."""
        for period in ("2026", "2026-10"):
            with self.subTest(period=period):
                book = Book(regime="regular")
                book.move(
                    "Lastschrift FA",
                    d(2027, 1, 12),
                    ("1780", 300),
                    ("1200", -300),
                    partner="Finanzamt",
                    info={"vat_period": period},
                )
                result = self.run_year(book, 2027)
                self.assertEqual(result.ten_day_items, [])
                self.assertEqual(fields(result), {VAT_PAID: D("300.00")})


class TestSmallBusinessLimit(EngineCase):
    """UT-KU-1…7: limits, total turnover, exceeding payment (R-25, R-27)."""

    def book(self, year, amounts, regime="ku", **values):
        book = Book(regime=regime)
        # the turnover of the previous year is known (0 entered for the
        # year): without bookings of an earlier year it would be reported
        # as unknown (test_release_findings)
        book.set_regimes(
            m.VatPeriod(
                date_from=d(2000, 1, 1),
                regime=regime,
                prev_year_turnover=0,
                prev_year_turnover_year=year,
            )
        )
        for index, amount in enumerate(amounts):
            book.move(
                f"Kasse {index}",
                d(year, 3, 1 + index),
                ("1000", amount),
                (values.get("account", "8195"), -amount),
            )
        return book

    def limit_codes(self, result):
        return sorted(
            {(f.key, f.severity) for f in result.findings if f.code == "C-32"}
        )

    def test_ut_ku_1_limits_2024(self):
        result = self.run_year(self.book(2024, [30000, 20000.01]), 2024)
        self.assertEqual(result.turnover.limit, 5000000)
        self.assertEqual(
            self.limit_codes(result),
            [("ku_limit_forecast_exceeded", m.WARNING)],
        )

    def test_ut_ku_2_limits_2025_and_2026(self):
        for year in (2025, 2026):
            with self.subTest(year=year):
                result = self.run_year(self.book(year, [60000, 40000]), year)
                self.assertEqual(result.turnover.limit, 10000000)
                self.assertEqual(
                    self.limit_codes(result), [("ku_limit_near", m.WARNING)]
                )
                result = self.run_year(self.book(year, [60000, 40000.01]), year)
                self.assertEqual(
                    self.limit_codes(result), [("ku_limit_exceeded", m.BLOCKER)]
                )
                self.assertEqual(result.turnover.crossing_date, d(year, 3, 2))

    def test_ut_ku_3_founding_year(self):
        book = self.book(2026, [20000, 5000.01])
        book.founding_year = 2026
        result = self.run_year(book, 2026)
        self.assertEqual(result.turnover.limit, 2500000)
        self.assertEqual(self.limit_codes(result), [("ku_limit_exceeded", m.BLOCKER)])

    def test_ut_ku_3b_year_after_foundation(self):
        """The year after the foundation: previous year (founding year, not
        converted into a full year) against 25,000 €, current year against
        100,000 €; the year of foundation itself has no previous year."""
        book = self.book(2025, [25000.01])
        book.move("Kasse", d(2026, 2, 1), ("1000", 30000), ("8195", -30000))
        book.founding_year = 2025
        result = self.run_year(book, 2026)
        self.assertEqual(result.turnover.limit, 10000000)
        self.assertEqual(
            self.limit_codes(result), [("ku_limit_previous_year", m.WARNING)]
        )
        book = self.book(2026, [100])
        book.set_regimes(
            m.VatPeriod(
                date_from=d(2026, 1, 1), regime="ku", prev_year_turnover=2600000
            )
        )
        book.founding_year = 2026
        self.assertEqual(self.limit_codes(self.run_year(book, 2026)), [])

    def test_ut_ku_4b_entered_previous_year_only_first_year(self):
        """Regression: the turnover entered for the year before a takeover
        holds for the first year of the period only; later years take the
        data (26,000 € in 2025 > 25,000 €)."""
        book = self.book(2025, [13000, 13000])
        book.move("Kasse", d(2026, 2, 1), ("1000", 100), ("8195", -100))
        book.set_regimes(
            m.VatPeriod(
                date_from=d(2025, 1, 1), regime="ku", prev_year_turnover=1200000
            )
        )
        self.assertEqual(self.limit_codes(self.run_year(book, 2025)), [])
        self.assertEqual(
            self.limit_codes(self.run_year(book, 2026)),
            [("ku_limit_previous_year", m.WARNING)],
        )

    def test_ut_ku_4_previous_year(self):
        book = self.book(2025, [26000])
        book.move("Kasse", d(2026, 2, 1), ("1000", 100), ("8195", -100))
        result = self.run_year(book, 2026)
        self.assertEqual(
            self.limit_codes(result), [("ku_limit_previous_year", m.WARNING)]
        )
        book = self.book(2026, [100])
        book.set_regimes(
            m.VatPeriod(
                date_from=d(2026, 1, 1), regime="ku", prev_year_turnover=2600000
            )
        )
        result = self.run_year(book, 2026)
        self.assertEqual(
            self.limit_codes(result), [("ku_limit_previous_year", m.WARNING)]
        )

    def test_ut_ku_5_total_turnover(self):
        """Not taxable (119) and asset sales are left out; taxable free
        supplies count at their VAT base, never at the value of Kz 106/108
        for income tax (R-27, UStAE 19.2 (2) sentences 2 and 4). A free
        supply booked by a small business (8910 without VAT) does not count:
        whether it is taxable is not in the books (hint)."""
        book = self.book(2026, [1000])
        book.move("Kasse", d(2026, 4, 1), ("1000", 500), ("8338", -500))
        book.move("Kasse", d(2026, 4, 2), ("1000", 700), ("8800", -700))
        withdrawal = book.move("Entnahme", d(2026, 4, 3), ("1800", 50), ("8910", -50))
        book.adjustment_amounts.append(
            m.FieldAmount(year=2026, field="EUER.20.106", amount=12000)
        )
        result = self.run_year(book, 2026)
        # the private use of a car without input tax deduction is not taxable
        self.assertEqual(result.turnover.amount, 100000)
        hint = [f for f in result.findings if f.key == "ku_free_supply_not_counted"]
        self.assertEqual(
            [(f.code, f.severity, f.amount, f.move_ids) for f in hint],
            [("C-32", m.INFO, 5000, (withdrawal.move.id,))],
        )
        # with input tax deduction its VAT base counts (80 % of 120.00)
        book.turnover_amounts.append(m.TurnoverAmount(year=2026, amount=9600))
        result = self.run_year(book, 2026)
        self.assertEqual(result.turnover.amount, 100000 + 9600)
        self.assertEqual(
            fields(result),
            {
                KU: D("1500.00"),
                KU_DAVON: D("500.00"),
                "EUER.20.102": D("700.00"),
                "EUER.20.106": D("120.00"),
                "EUER.20.108": D("50.00"),
                WITHDRAWAL: D("50.00"),
            },
        )

    def test_turnover_amount_without_value_is_left_out(self):
        """R-27: a correction without a VAT base (a free supply that is not
        taxable) adds no entry to the total turnover."""
        book = self.book(2026, [1000])
        book.turnover_amounts.append(m.TurnoverAmount(year=2026, amount=0))
        result = self.run_year(book, 2026)
        self.assertEqual(result.turnover.amount, 100000)
        self.assertEqual(
            [day for (day, _source), _amount in result.turnover.entries],
            [d(2026, 3, 1)],
        )

    def test_ut_ku_use_withdrawal_without_input_tax(self):
        """Review E4: a small business with 99,500 € and the use withdrawal
        of a camera bought without input tax deduction (600 € in Kz 108)
        stays below the limit: only the warning at 80 %, no blocker."""
        book = self.book(2026, [60000, 39500])
        book.asset_amounts += [
            m.FieldAmount(year=2026, field="EUER.20.108", amount=60000, source="asset"),
            m.FieldAmount(year=2026, field=WITHDRAWAL, amount=60000, source="asset"),
        ]
        result = self.run_year(book, 2026)
        self.assertEqual(result.turnover.amount, 9950000)
        self.assertEqual(self.limit_codes(result), [("ku_limit_near", m.WARNING)])
        # year of foundation: 22,000 € and private use of a car (4,800 €)
        book = self.book(2026, [22000])
        book.founding_year = 2026
        book.adjustment_amounts.append(
            m.FieldAmount(year=2026, field="EUER.20.106", amount=480000)
        )
        result = self.run_year(book, 2026)
        self.assertEqual(self.limit_codes(result), [("ku_limit_near", m.WARNING)])

    def test_ut_ku_booked_free_supply_small_business(self):
        """Review E4 (P5): 99,500 € received and the private use of a car
        booked on 8921 without VAT (600 € in Kz 106) stay below the limit:
        the warning at 80 % and the hint, no blocker. Only the VAT base of
        a profit correction counts the supply as taxable."""
        book = self.book(2026, [60000, 39500])
        book.move("Kfz privat", d(2026, 12, 31), ("1800", 600), ("8921", -600))
        result = self.run_year(book, 2026)
        self.assertEqual(fields(result)["EUER.20.106"], D("600.00"))
        self.assertEqual(result.turnover.amount, 9950000)
        self.assertEqual(
            self.limit_codes(result),
            [("ku_free_supply_not_counted", m.INFO), ("ku_limit_near", m.WARNING)],
        )
        book.turnover_amounts.append(m.TurnoverAmount(year=2026, amount=60000))
        result = self.run_year(book, 2026)
        self.assertEqual(result.turnover.amount, 10010000)
        self.assertIn(("ku_limit_exceeded", m.BLOCKER), self.limit_codes(result))
        # no hint in a year without a limit parameter (no check, 2023)
        book = self.book(2023, [1000])
        book.move("Kfz privat", d(2023, 12, 31), ("1800", 600), ("8921", -600))
        self.assertEqual(self.limit_codes(self.run_year(book, 2023)), [])

    def test_ut_ku_booked_free_supply_regular(self):
        """R-27 under standard taxation: a free supply booked with output VAT
        is taxable, its net amount (the VAT base) counts; booked without VAT
        it does not. The turnover of 2025 decides the small business status
        of 2026: 24,950 € and a VAT base of 100 € exceed 25,000 €."""
        for with_vat in (True, False):
            with self.subTest(with_vat=with_vat):
                book = Book()
                book.set_regimes(
                    m.VatPeriod(
                        date_from=d(2020, 1, 1),
                        date_to=d(2025, 12, 31),
                        regime="regular",
                    ),
                    m.VatPeriod(date_from=d(2026, 1, 1), regime="ku"),
                )
                book.move(
                    "Kasse",
                    d(2025, 3, 1),
                    ln("1000", 29690.50),
                    ln("8400", -24950, tax="ust19"),
                    ln("1776", -4740.50, rep="ust19"),
                )
                if with_vat:
                    book.move(
                        "Kfz privat",
                        d(2025, 12, 31),
                        ln("1800", 119),
                        ln("8921", -100, tax="ust19"),
                        ln("1776", -19, rep="ust19"),
                    )
                else:
                    book.move(
                        "Kfz privat", d(2025, 12, 31), ("1800", 100), ("8921", -100)
                    )
                book.move("Kasse", d(2026, 2, 1), ("1000", 100), ("8195", -100))
                previous = self.run_year(book, 2025)
                self.assertEqual(fields(previous)["EUER.20.106"], D("100.00"))
                self.assertEqual(
                    previous.turnover.amount, 2505000 if with_vat else 2495000
                )
                self.assertEqual(
                    self.limit_codes(self.run_year(book, 2026)),
                    [("ku_limit_previous_year", m.WARNING)] if with_vat else [],
                )

    def test_ut_ku_previous_year_with_free_supplies(self):
        """Review E4: the turnover of the previous year holds its taxable free
        supplies like the check of that year: 24,900 € received and a VAT
        base of 200 € exceed 25,000 €."""
        book = self.book(2025, [24900])
        book.move("Kasse", d(2026, 2, 1), ("1000", 100), ("8195", -100))
        result = self.run_year(book, 2026)
        self.assertEqual(self.limit_codes(result), [])
        book.turnover_amounts.append(m.TurnoverAmount(year=2025, amount=20000))
        self.assertEqual(self.run_year(book, 2025).turnover.amount, 2510000)
        result = self.run_year(book, 2026)
        self.assertEqual(
            self.limit_codes(result), [("ku_limit_previous_year", m.WARNING)]
        )

    def test_ut_ku_6_exceeding_payment_and_later_ones(self):
        """ST-K03: from the exceeding payment on standard taxation, rate input."""
        book = Book().previous_turnover(2026)
        sales = []
        for index, amount in enumerate((90000, 11900, 5950)):
            invoice = book.move(
                f"RE{index}",
                d(2026, 5, 1),
                ("1410", amount),
                ("8195", -amount),
                move_type="out_invoice",
            )
            bank = book.move(
                f"Bank{index}",
                d(2026, 6, 1 + index),
                ("1200", amount),
                ("1410", -amount),
            )
            book.rec(invoice("1410"), bank("1410"))
            sales.append(invoice)
        result = self.run_year(book, 2026)
        self.assertEqual(result.turnover.crossing_date, d(2026, 6, 2))
        self.assertEqual(self.limit_codes(result), [("ku_limit_exceeded", m.BLOCKER)])
        book.set_regimes(
            m.VatPeriod(date_from=d(2020, 1, 1), date_to=d(2026, 6, 1), regime="ku"),
            m.VatPeriod(
                date_from=d(2026, 6, 2), regime="regular", basis="receipt_date"
            ),
        ).previous_turnover(2026)
        result = self.run_year(book, 2026)
        self.assertEqual(
            self.limit_codes(result),
            [
                ("ku_limit_exceeded_period_set", m.WARNING),
                ("tax_rate_missing_limit", m.BLOCKER),
            ],
        )
        book.moves = [
            dataclasses.replace(move, info=m.MoveInfo(regular_tax_rate=Fraction(19)))
            if move.id in (sales[1].move.id, sales[2].move.id)
            else move
            for move in book.moves
        ]
        result = self.run_year(book, 2026)
        self.assertEqual(
            fields(result),
            {KU: D("90000.00"), TAXABLE: D("15000.00"), OUTPUT_VAT: D("2850.00")},
        )
        self.assertEqual(
            self.limit_codes(result), [("ku_limit_exceeded_period_set", m.WARNING)]
        )

    def test_ut_ku_7_cash_date_and_unjustified_vat(self):
        """Turnover by the actual cash date; § 14c VAT is not turnover."""
        book = Book()
        book.partner("Kunde", recurring=True)
        rent = book.move(
            "Bank",
            d(2027, 1, 5),
            ("1200", 1190),
            ("8195", -1000),
            ln("1776", -190, rep="ust19"),
            partner="Kunde",
            info={"economic_year": 2026, "due_date": d(2027, 1, 1)},
        )
        book.decide(rent, rent, "apply")
        result = self.run_year(book, 2026)
        self.assertEqual(fields(result), {KU: D("1190.00")})
        self.assertEqual(result.turnover.amount, 0)
        result = self.run_year(book, 2027)
        self.assertEqual(result.turnover.amount, 100000)


class TestDeterminism(BaseCase):
    def test_ut_det_1_same_hash(self):
        """I-4: two runs and shuffled input give the same hash."""
        book = Book(regime="regular")
        invoice = book.move(
            "RE",
            d(2026, 11, 10),
            ln("1410", 1404),
            ln("8400", -1000, tax="ust19"),
            ln("1776", -190, rep="ust19"),
            ln("8300", -200, tax="ust7"),
            ln("1771", -14, rep="ust7"),
            move_type="out_invoice",
        )
        for day, amount in ((d(2026, 12, 1), 500), (d(2027, 1, 15), 904)):
            bank = book.move("Bank", day, ("1200", amount), ("1410", -amount))
            book.rec(invoice("1410"), bank("1410"))
        data = book.data()
        first = engine.run(data, 2026)
        second = engine.run(data, 2026)
        self.assertEqual(first.hash, second.hash)
        generator = random.Random(4)
        shuffled = dataclasses.replace(
            data,
            moves=tuple(generator.sample(data.moves, len(data.moves))),
            lines=tuple(generator.sample(data.lines, len(data.lines))),
            partials=tuple(generator.sample(data.partials, len(data.partials))),
        )
        self.assertEqual(engine.run(shuffled, 2026).hash, first.hash)
        self.assertEqual(len(first.hash), 64)


class TestPerformance(BaseCase):
    def test_ut_perf_engine_20000_partials(self):
        """Engine part of UT-PERF-1: 20,000 partials (B-15).

        The limit of B-15 (120 s on the reference runner) holds for the
        whole run including the extraction; the pure engine must stay well
        below it.
        """
        generator = random.Random(15)
        book = Book(regime="regular")
        for index in range(5000):
            day = d(2027, 1 + index % 12, 1 + index % 28)
            net = generator.randint(100, 100000) / 100
            vat = round(net * 19) / 100
            total = round(net + vat, 2)
            invoice = book.move(
                "RE",
                day,
                ln("1410", total),
                ln("8400", -net, tax="ust19"),
                ln("1776", -vat, rep="ust19"),
                move_type="out_invoice",
            )
            payment = book.move("Zahlung", day, ("OR", total), ("1410", -total))
            book.rec(invoice("1410"), payment("1410"))
            parts = [round(total / 3, 2), round(total / 3, 2)]
            parts.append(round(total - sum(parts), 2))
            for part in parts:
                bank = book.move("Bank", day, ("1200", part), ("OR", -part))
                book.rec(payment("OR"), bank("OR"), part)
        data = book.data()
        self.assertEqual(len(data.partials), 20000)
        start = time.perf_counter()
        result = engine.run(data, 2027)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 60)
        self.assertEqual(result.invariants["money"], {})
        self.assertEqual(result.invariants["documents"], {})


class TestDirectRule(BaseCase):
    def test_rule_order(self):
        book = Book()
        categories = {c.code: c for c in book.categories}

        def rule(*pairs):
            lines = [
                m.Line(id=i, move_id=1, account_id=1, balance=1, repartition_id=rep)
                for i, (_code, rep) in enumerate(pairs, start=1)
            ]
            cats = [categories.get(code) for code, _rep in pairs]
            accounts = [
                book.accounts["1740" if code == "EXP_PAYROLL_LIABILITY" else "4930"]
                for code, _rep in pairs
            ]
            return direct.direct_rule(lines, cats, accounts)

        self.assertEqual(rule(("DEPR_MOVABLE", None), ("FIXED_ASSET", None)), "d")
        self.assertEqual(
            rule(("EXP_PERSONNEL", None), ("EXP_PAYROLL_LIABILITY", None)), "b"
        )
        self.assertEqual(rule(("EXP_PERSONNEL", None), ("REV_TAX_NONCASH", None)), "a")
        self.assertEqual(rule(("EXP_LEASING", None), ("EXP_WORK_EQUIPMENT", None)), "c")
        self.assertEqual(rule(("NEUTRAL", None), ("NEUTRAL", None)), "e")
        self.assertEqual(rule(("VAT_OUTPUT", None), ("VAT_SETTLEMENT", None)), "e")
        self.assertEqual(rule(("EXP_WORK_EQUIPMENT", None), ("NEUTRAL", None)), "f")
        self.assertEqual(rule(("EXP_ADVICE", None), ("WARN_PROVISION", None)), "f")
        self.assertEqual(rule(("EXP_ADVICE", None), (None, None)), "f")


class TestVatHelpers(BaseCase):
    def test_parse_vat_period(self):
        self.assertEqual(
            vat.parse_vat_period("2026-02")[1:],
            (d(2026, 2, 1), d(2026, 2, 28), "month"),
        )
        self.assertEqual(vat.parse_vat_period("2026-Q2")[2], d(2026, 6, 30))
        self.assertIsNone(vat.parse_vat_period(None))

    def test_overlapping_periods_latest_start_wins(self):
        """Overlapping status periods: on a day the one that starts last
        applies, whatever the order of the list."""
        later = m.VatPeriod(date_from=d(2026, 7, 1), regime="regular")
        earlier = m.VatPeriod(date_from=d(2024, 1, 1), regime="ku")
        self.assertIs(vat.period_at([later, earlier], d(2026, 8, 1)), later)
        self.assertIs(vat.period_at([later, earlier], d(2026, 3, 1)), earlier)

    def test_permanent_extension_of_a_return_period(self):
        """DFV: the period of the return decides; a text that is no period
        has no extension."""
        periods = [
            m.VatPeriod(
                date_from=d(2024, 1, 1), regime="regular", permanent_extension=True
            )
        ]
        self.assertTrue(vat.permanent_extension_for("2026-12", periods))
        self.assertFalse(vat.permanent_extension_for("2026-13", periods))
        self.assertFalse(vat.permanent_extension_for("2026-12", []))

    def test_regime_from_periods(self):
        periods = (
            m.VatPeriod(date_from=d(2020, 1, 1), date_to=d(2026, 6, 1), regime="ku"),
            m.VatPeriod(
                date_from=d(2026, 6, 2), regime="regular", basis="receipt_date"
            ),
        )
        self.assertEqual(vat.regime_at(periods, d(2026, 6, 1)), "ku")
        self.assertEqual(vat.regime_at(periods, d(2026, 6, 2)), "regular")
        self.assertEqual(vat.regime_at((), d(2026, 6, 2), "ku"), "ku")
        self.assertIsNone(vat.receipt_regime_at(periods, d(2026, 6, 1)))
        self.assertEqual(vat.receipt_regime_at(periods, d(2026, 7, 1)), "regular")
        invoice = m.Move(id=1, date=d(2026, 7, 1), supply_date=d(2026, 5, 30))
        self.assertEqual(vat.document_regime(invoice, periods), "ku")
        self.assertEqual(vat.revenue_regime(invoice, d(2026, 7, 5), periods), "regular")
        stored = dataclasses.replace(invoice, info=m.MoveInfo(vat_regime="regular"))
        self.assertEqual(vat.document_regime(stored, periods), "regular")

    def test_reverse_charge_detection(self):
        rc = m.Tax(
            id=1, type_tax_use="purchase", factors=(("invoice", 100), ("invoice", -100))
        )
        normal = m.Tax(
            id=2, type_tax_use="purchase", factors=(("invoice", 100), ("refund", -100))
        )
        self.assertTrue(vat.is_reverse_charge(rc, "invoice"))
        self.assertFalse(vat.is_reverse_charge(rc, "refund"))
        self.assertFalse(vat.is_reverse_charge(normal, "invoice"))


class TestParameters(BaseCase):
    def test_values(self):
        params = Parameters(Book().parameters)
        self.assertEqual(params.cents("ku_limit_cur", 2024), 5000000)
        self.assertEqual(params.cents("ku_limit_cur", d(2026, 5, 1)), 10000000)
        self.assertEqual(params.cents("gift_limit", 2026), 5000)
        self.assertEqual(params.month_day("ten_day_from", 2026, None), (12, 22))
        self.assertIsNone(params.get("unknown", 2026))
        self.assertEqual(params.cents("unknown", 2026, 7), 7)
