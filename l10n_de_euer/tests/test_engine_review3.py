# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Regression tests of the review of the calculation (stage E2, round 3).

Pure engine tests with synthetic data (decision D6): private payments with
a discount next to the open items (5.2 c), private accounts as default
account of a money journal, the private account journal per money
movement, manual input VAT lines of gifts, write-offs in a statement
matched with a payment entry, payments on the bank account matched with
statement lines of two years, the years of the blockers C-19, C-27 and
C-29, the money reconciliation per year and the dates of the slices (I-1),
credit notes reconciled after the end of their year, rests on the
outstanding account and an entered economic year of a single invoice.
"""

import dataclasses
from decimal import Decimal as D
from unittest.mock import patch

from ..models.engine import model as m
from ..models.engine import money
from ..models.engine.ten_day import CashDate
from .engine_common import Book, EngineCase, codes, d, fields, ln

KU = "EUER.20.111"
TAXABLE = "EUER.20.112"
OUTPUT_VAT = "EUER.20.140"
GOODS = "EUER.25.100"
SERVICES = "EUER.25.110"
GIFT_NOT_DEDUCTIBLE = "EUER.25.164"
INPUT_VAT = "EUER.25.185"
WORK = "EUER.25.229"
WITHDRAWAL = "EUER.29.122"
CONTRIBUTION = "EUER.29.123"


def keys(result, code=None):
    return sorted(
        (f.code, f.severity, f.key)
        for f in result.findings
        if code is None or f.code == code
    )


class TestPrivatePaymentWithDiscount(EngineCase):
    """5.2 (c): a journal entry that pays documents with private money and
    books a discount or its tax correction next to the open items."""

    def test_n01_customer_pays_to_the_private_account(self):
        book = Book()
        invoice = self.sale(book, "RE", d(2026, 3, 1), 1000)
        entry = book.move(
            "Privat erhalten",
            d(2026, 3, 10),
            ("1800", 980),
            ("8730", 20),
            ("1410", -1000),
        )
        book.rec(invoice("1410"), entry("1410"))
        result = self.run_year(book, 2026)
        self.assertEqual(result.classes[entry.move.id], m.SOURCE)
        self.assertEqual(result.roles[entry("1800").id], m.L)
        self.assertEqual(fields(result), {KU: D("980.00"), WITHDRAWAL: D("980.00")})
        self.assertNotIn("C-25", codes(result))

    def test_n02_owner_pays_a_bill_privately(self):
        book = Book()
        bill = book.move(
            "ER",
            d(2026, 3, 1),
            ("4930", 1000),
            ("1610", -1000),
            move_type="in_invoice",
        )
        entry = book.move(
            "Privat bezahlt",
            d(2026, 3, 10),
            ("1610", 1000),
            ("1890", -980),
            ("3730", -20),
        )
        book.rec(bill("1610"), entry("1610"))
        self.assertEqual(
            fields(self.run_year(book, 2026)),
            {WORK: D("1000.00"), GOODS: D("-20.00"), CONTRIBUTION: D("980.00")},
        )

    def test_n03_with_the_correction_of_the_input_vat(self):
        book = Book(regime="regular")
        bill = book.move(
            "ER",
            d(2026, 3, 1),
            ln("4930", 1000, tax="vst19"),
            ln("1576", 190, rep="vst19"),
            ln("1610", -1190),
            move_type="in_invoice",
        )
        entry = book.move(
            "Privat bezahlt",
            d(2026, 3, 10),
            ln("1610", 1190),
            ln("1890", -1166.2),
            ln("3736", -20, tax="vst19"),
            ln("1576", -3.8, rep="vst19"),
        )
        book.rec(bill("1610"), entry("1610"))
        self.assertEqual(
            fields(self.run_year(book, 2026)),
            {
                WORK: D("1000.00"),
                GOODS: D("-20.00"),
                INPUT_VAT: D("186.20"),
                CONTRIBUTION: D("1166.20"),
            },
        )

    def test_entry_booked_as_document_stays_on_payment(self):
        """ "4930 / 1800 an 1610" still open, later paid by bank: expense and
        withdrawal on payment."""
        book = Book()
        document = book.move(
            "Beleg",
            d(2026, 3, 1),
            ("4930", 100),
            ("1800", 50),
            ("1610", -150),
        )
        self.assertEqual(self.run_year(book, 2026).roles[document("1800").id], m.N)
        bank = book.move("Bank", d(2027, 1, 5), ("1610", 150), ("1200", -150))
        book.rec(document("1610"), bank("1610"))
        result = self.run_year(book, 2027)
        self.assertEqual(result.roles[document("1800").id], m.N)
        self.assertEqual(fields(result), {WORK: D("100.00"), WITHDRAWAL: D("50.00")})

    def test_private_line_neutralised_is_a_blocker(self):
        """An entry that only partly settles the invoice stays a document;
        written off, its private line would be lost: C-25 blocker instead
        of info."""
        book = Book()
        invoice = self.sale(book, "RE", d(2026, 3, 1), 1000)
        entry = book.move(
            "Privat erhalten",
            d(2026, 3, 10),
            ("1800", 980),
            ("8730", 20),
            ("1410", -1000),
        )
        book.rec(invoice("1410"), entry("1410"), amount=600)
        writeoff = book.move(
            "Ausbuchung", d(2026, 3, 20), ("2400", 400), ("1410", -400)
        )
        book.rec(invoice("1410"), writeoff("1410"))
        result = self.run_year(book, 2026)
        self.assertEqual(result.roles[entry("1800").id], m.N)
        self.assertIn(("C-25", m.BLOCKER, "neutralized_private"), keys(result))


class TestPrivateAccountOfAMoneyJournal(EngineCase):
    """5.2: a private account as default account of a money journal."""

    def test_n04_journal_paid_privately(self):
        book = Book(regime="regular")
        book.account("1890", liquidity=True)
        bill = book.move(
            "ER",
            d(2026, 3, 1),
            ln("4930", 100, tax="vst19"),
            ln("1576", 19, rep="vst19"),
            ln("1610", -119),
            move_type="in_invoice",
        )
        paid = book.move(
            "Privat bezahlt",
            d(2026, 3, 5),
            ("1610", 119),
            ("1890", -119),
            statement=True,
        )
        book.rec(bill("1610"), paid("1610"))
        result = self.run_year(book, 2026)
        self.assertEqual(
            fields(result),
            {WORK: D("100.00"), INPUT_VAT: D("19.00"), CONTRIBUTION: D("119.00")},
        )

    def test_p02_transfer_to_the_private_journal(self):
        book = Book()
        book.account("1800", liquidity=True)
        book.move("Übertrag", d(2026, 5, 10), ("1800", 1000), ("1200", -1000))
        self.assertEqual(fields(self.run_year(book, 2026)), {WITHDRAWAL: D("1000.00")})

    def test_private_account_journal_is_not_counted_twice(self):
        """R-18 journal with a private account: its own rule counts."""
        book = Book()
        book.account("1800", liquidity=True, private_money=True)
        book.move(
            "Übertrag",
            d(2026, 5, 10),
            ("1800", 1000),
            ("1200", -1000),
        )
        self.assertEqual(fields(self.run_year(book, 2026)), {WITHDRAWAL: D("1000.00")})


class TestPrivateAccountJournalPerMovement(EngineCase):
    """R-18, 5.7 no. 9: withdrawal and contribution per money movement."""

    def test_p01_receipt_with_discount(self):
        book = Book(regime="regular")
        book.account("1210", private_money=True)
        invoice = self.regular_sale(book, d(2026, 5, 1), 1000)
        receipt = book.move(
            "Privatkonto",
            d(2026, 5, 10),
            ln("1210", 1166.2),
            ln("8736", 20, tax="ust19"),
            ln("1776", 3.8, rep="ust19"),
            ln("1410", -1190),
            private_journal=True,
        )
        book.rec(invoice("1410"), receipt("1410"))
        self.assertEqual(
            fields(self.run_year(book, 2026)),
            {
                TAXABLE: D("980.00"),
                OUTPUT_VAT: D("186.20"),
                WITHDRAWAL: D("1166.20"),
            },
        )

    def test_payment_and_fee_in_one_movement(self):
        book = Book()
        book.account("1210", private_money=True)
        bill = book.move(
            "ER", d(2026, 6, 1), ("4930", 300), ("1610", -300), move_type="in_invoice"
        )
        paid = book.move(
            "Privatkonto",
            d(2026, 6, 5),
            ("1610", 300),
            ("4970", 2),
            ("1210", -302),
            private_journal=True,
        )
        book.rec(bill("1610"), paid("1610"))
        result = self.run_year(book, 2026)
        self.assertEqual(fields(result)[CONTRIBUTION], D("302.00"))
        self.assertNotIn(WITHDRAWAL, fields(result))


class TestManualInputVatOfAGift(EngineCase):
    """5.7 nos. 4 and 7: a manual input VAT line follows the gift."""

    def gift(self, book, tax_line):
        bank = book.move(
            "Geschenk",
            d(2026, 3, 1),
            ln("4630", 60, tax="vst19" if tax_line else ()),
            ln("1576", 11.4, rep="vst19") if tax_line else ln("1576", 11.4),
            ln("1200", -71.4),
            partner="Shop",
            info={"gift_recipient_id": book.partner("Kunde A")},
        )
        return bank

    def test_i07_with_tax_line(self):
        book = Book(regime="regular")
        self.gift(book, True)
        result = self.run_year(book, 2026)
        self.assertEqual(fields(result), {GIFT_NOT_DEDUCTIBLE: D("71.40")})
        self.assertIn("C-52", codes(result))

    def test_i08_manual_line_without_repartition(self):
        book = Book(regime="regular")
        self.gift(book, False)
        result = self.run_year(book, 2026)
        self.assertEqual(fields(result), {GIFT_NOT_DEDUCTIBLE: D("71.40")})
        self.assertIn("C-52", codes(result))


class TestWriteOffThroughPaymentEntries(EngineCase):
    """5.5: a write-off in the statement matched with a registered payment
    or a payment order reduces the documents behind them."""

    def invoice(self, book):
        return book.move(
            "RE",
            d(2026, 3, 1),
            ln("1410", 2260),
            ln("8400", -1000, tax="ust19"),
            ln("1776", -190, rep="ust19"),
            ln("8300", -1000, tax="ust7"),
            ln("1771", -70, rep="ust7"),
            move_type="out_invoice",
        )

    def check(self, book):
        result = self.run_year(book, 2026)
        self.assertNotIn("C-36", codes(result))
        self.assertIn(("C-25", m.INFO, "writeoff_netted"), keys(result, "C-25"))
        self.assertEqual(
            fields(result), {TAXABLE: D("1769.91"), OUTPUT_VAT: D("230.09")}
        )

    def test_f1_statement_matched_with_the_payment(self):
        book = Book(regime="regular")
        invoice = self.invoice(book)
        payment = book.move("Zahlung", d(2026, 3, 10), ("OR", 2260), ("1410", -2260))
        book.rec(invoice("1410"), payment("1410"))
        bank = book.move(
            "Bank",
            d(2026, 3, 12),
            ("1200", 2000),
            ("2400", 260),
            ("OR", -2260),
            statement=True,
        )
        book.rec(payment("OR"), bank("OR"))
        self.check(book)

    def test_w5_payment_order(self):
        book = Book(regime="regular")
        invoice = self.invoice(book)
        payment = book.move("Zahlung", d(2026, 3, 10), ("OR", 2260), ("1410", -2260))
        book.rec(invoice("1410"), payment("1410"))
        batch = book.move("Sammel", d(2026, 3, 10), ("1360", 2260), ("OR", -2260))
        book.rec(payment("OR"), batch("OR"))
        bank = book.move(
            "Bank",
            d(2026, 3, 12),
            ("1200", 2000),
            ("2400", 260),
            ("1360", -2260),
            statement=True,
        )
        book.rec(batch("1360"), bank("1360"))
        self.check(book)

    def test_bank_fee_is_still_realised(self):
        """A fee instead of a write-off is an expense."""
        book = Book(regime="regular")
        invoice = self.invoice(book)
        payment = book.move("Zahlung", d(2026, 3, 10), ("OR", 2260), ("1410", -2260))
        book.rec(invoice("1410"), payment("1410"))
        bank = book.move(
            "Bank",
            d(2026, 3, 12),
            ("1200", 2000),
            ("4970", 260),
            ("OR", -2260),
            statement=True,
        )
        book.rec(payment("OR"), bank("OR"))
        result = self.run_year(book, 2026)
        self.assertEqual(
            fields(result),
            {
                TAXABLE: D("2000.00"),
                OUTPUT_VAT: D("260.00"),
                "EUER.25.183": D("260.00"),
            },
        )


class TestPaymentOnBankAccountInTwoYears(EngineCase):
    """5.4, C-24: a payment on the bank account matched with statement lines
    of two years counts in both years by the matched amounts."""

    def book(self, reverse=False, second=595):
        book = Book(regime="regular")
        invoice = self.regular_sale(book, d(2034, 11, 1), 1000)
        payment = book.move("Zahlung", d(2034, 12, 20), ("1200", 1190), ("1410", -1190))
        book.rec(invoice("1410"), payment("1410"))
        first = book.move(
            "Auszug A",
            d(2034, 12, 30),
            ("1200", 595),
            ("1200", -595),
            statement=True,
        )
        statements = [(first, 595)]
        if second:
            statements.append(
                (
                    book.move(
                        "Auszug B",
                        d(2035, 1, 20),
                        ("1200", second),
                        ("1200", -second),
                        statement=True,
                    ),
                    second,
                )
            )
        if reverse:
            statements.reverse()
        for statement, amount in statements:
            book.rec(payment("1200"), statement("1200", 1), amount=amount)
        return book

    def check(self, book):
        for year in (2034, 2035):
            result = self.run_year(book, year)
            self.assertEqual(
                fields(result), {TAXABLE: D("500.00"), OUTPUT_VAT: D("95.00")}
            )
            self.assertIn("C-24", codes(result, m.WARNING))

    def test_split_over_the_turn_of_the_year(self):
        self.check(self.book())

    def test_order_of_the_reconciliation_does_not_matter(self):
        self.check(self.book(reverse=True))

    def test_unmatched_rest_keeps_the_date_of_the_payment(self):
        book = self.book(second=0)
        result = self.run_year(book, 2034)
        self.assertEqual(
            fields(result), {TAXABLE: D("1000.00"), OUTPUT_VAT: D("190.00")}
        )
        self.assertIn(
            ("C-24", m.WARNING, "payment_on_bank_account_rest"), keys(result, "C-24")
        )
        self.assertEqual(fields(self.run_year(book, 2035)), {})

    def test_ten_day_rule_moves_only_the_part_in_the_window(self):
        """December rent paid on the bank account, on the statements on 5
        and 20 January: only the part of 5 January belongs to December."""
        book = Book()
        bill = book.move(
            "Miete Dezember",
            d(2026, 12, 1),
            ("4210", 800),
            ("1610", -800),
            move_type="in_invoice",
            partner="Vermieter",
            invoice_date_due=d(2026, 12, 31),
            info={"economic_year": 2026},
        )
        payment = book.move("Zahlung", d(2026, 12, 30), ("1610", 800), ("1200", -800))
        book.rec(bill("1610"), payment("1610"))
        for day in (d(2027, 1, 5), d(2027, 1, 20)):
            statement = book.move(
                "Auszug", day, ("1200", 400), ("1200", -400), statement=True
            )
            book.rec(statement("1200", 0), payment("1200"), amount=400)
        rent = "EUER.25.150"
        self.assertEqual(fields(self.run_year(book, 2026)), {rent: D("400.00")})
        self.assertEqual(fields(self.run_year(book, 2027)), {rent: D("400.00")})


class TestYearsOfBlockers(EngineCase):
    """C-19, C-27 and C-29 also in the year in which the money arrives."""

    def test_c19_in_the_year_of_payment(self):
        book = Book(regime="regular")
        invoice = book.move(
            "RE USD",
            d(2025, 11, 1),
            ln("1410", 1190, currency_id=2, residual_currency_zero=True),
            ln("8400", -1000, tax="ust19"),
            ln("1776", -190, rep="ust19"),
            move_type="out_invoice",
        )
        bank = book.move("Bank", d(2026, 1, 20), ("1200", 1100), ("1410", -1100))
        book.rec(invoice("1410"), bank("1410"), amount=1100)
        for year in (2025, 2026):
            self.assertIn("C-19", codes(self.run_year(book, year), m.BLOCKER))

    def test_c29_in_the_year_of_payment(self):
        book = Book(regime="regular")
        invoice = book.move(
            "RE",
            d(2025, 12, 10),
            ln("1410", 119),
            ln("8590", -100, tax="ust19"),
            ln("1776", -19, rep="ust19"),
            move_type="out_invoice",
        )
        self.receipt(book, "Bank", d(2026, 1, 20), 119, invoice)
        for year in (2025, 2026):
            self.assertIn("C-29", codes(self.run_year(book, year), m.BLOCKER))

    def test_c27_in_the_year_the_money_arrives(self):
        book = Book()
        invoice = self.sale(book, "RE", d(2025, 12, 1), 300)
        payment = book.move("Zahlung", d(2025, 12, 20), ("OR", 300), ("1410", -300))
        book.rec(invoice("1410"), payment("1410"))
        bank = book.move("Bank", d(2026, 1, 8), ("1200", 100), ("OR", -100))
        book.rec(payment("OR"), bank("OR"))
        for year in (2025, 2026):
            self.assertIn("C-27", codes(self.run_year(book, year), m.WARNING))


class TestMoneyReconciliationPerYear(EngineCase):
    """I-1 (5.11): every slice carries the cash date of its money."""

    def book(self):
        book = Book(regime="regular")
        invoice = self.regular_sale(book, d(2025, 12, 1), 1000)
        self.receipt(book, "Bank", d(2026, 1, 15), 1190, invoice)
        return book

    def test_correct_dates(self):
        book = self.book()
        self.assertEqual(fields(self.run_year(book, 2025)), {})
        self.assertEqual(
            fields(self.run_year(book, 2026)),
            {TAXABLE: D("1000.00"), OUTPUT_VAT: D("190.00")},
        )

    def mutated(self, keep_money_date):
        original = money.MoneyFlow._doc_slice

        def doc_slice(flow, key, cents, payment, event, source):
            # injected defect: realise on the date of the document
            document = flow.graph.move_of(payment.port)
            origin = payment.cash.money_date if keep_money_date else None
            cash = CashDate(document.date, payment.cash.override, origin)
            return original(
                flow, key, cents, dataclasses.replace(payment, cash=cash), event, source
            )

        return patch.object(money.MoneyFlow, "_doc_slice", doc_slice)

    def test_slice_in_the_wrong_year_is_found(self):
        for keep in (True, False):
            with self.mutated(keep):
                result = self.book().run(2025)
            self.assertTrue(result.invariants["money"])
            self.assertIn("C-11", codes(result, m.BLOCKER))

    def test_cash_date_of_a_payment_entry(self):
        """The cash date entered on a payment entry is the expected date."""
        book = Book()
        invoice = self.sale(book, "RE", d(2026, 11, 1), 200)
        payment = book.move(
            "Scheck",
            d(2026, 12, 29),
            ("1360", 200),
            ("1410", -200),
            info={"cash_date": d(2026, 12, 29), "cash_date_reason": "cheque"},
        )
        book.rec(invoice("1410"), payment("1410"))
        bank = book.move("Bank", d(2027, 1, 5), ("1200", 200), ("1360", -200))
        book.rec(payment("1360"), bank("1360"))
        for year in (2026, 2027):
            self.run_year(book, year)


class TestLateReconciliation(EngineCase):
    """C-60: a credit note of the old year reconciled after its end."""

    def book(self, created):
        book = Book(regime="regular")
        invoice = book.move(
            "RE",
            d(2025, 11, 3),
            ln("1410", 2190),
            ln("8400", -1000, tax="ust19"),
            ln("1776", -190, rep="ust19"),
            ln("8100", -1000),
            move_type="out_invoice",
        )
        credit = book.move(
            "GS",
            d(2025, 12, 15),
            ln("1410", -1190),
            ln("8400", 1000, tax="ust19"),
            ln("1776", 190, rep="ust19"),
            move_type="out_refund",
        )
        self.receipt(book, "Bank", d(2025, 12, 20), 1000, invoice)
        book.rec(invoice("1410"), credit("1410"), created=created)
        return book

    def test_reconciled_in_the_next_year(self):
        result = self.run_year(self.book(d(2026, 2, 1)), 2025)
        self.assertIn(("C-60", m.WARNING, "late_reconciliation"), keys(result, "C-60"))

    def test_reconciled_in_the_same_year(self):
        result = self.run_year(self.book(d(2025, 12, 16)), 2025)
        self.assertNotIn("C-60", codes(result))


class TestRestOnTheOutstandingAccount(EngineCase):
    """5.4 no. 3: a rest on the outstanding account blocks (C-20)."""

    def test_payment_entry_reversed_after_the_bank_reconciliation(self):
        book = Book(regime="regular")
        book.account("OR", outstanding=True)
        invoice = self.regular_sale(book, d(2026, 3, 1), 1000)
        payment = book.move("Zahlung", d(2026, 3, 10), ("OR", 1190), ("1410", -1190))
        book.move("Bank", d(2026, 3, 12), ("1200", 1190), ("OR", -1190), statement=True)
        reversal = book.move(
            "Storno",
            d(2026, 3, 20),
            ("1410", 1190),
            ("OR", -1190),
            reversed_entry_id=payment.move.id,
        )
        book.rec(payment("1410"), reversal("1410"))
        book.rec(payment("OR"), reversal("OR"))
        self.assertTrue(invoice)
        result = self.run_year(book, 2026)
        self.assertIn(
            ("C-20", m.BLOCKER, "unallocated_outstanding"), keys(result, "C-20")
        )

    def test_money_transit_stays_a_warning(self):
        book = Book()
        book.move("Bank", d(2026, 3, 12), ("1200", 500), ("1360", -500))
        result = self.run_year(book, 2026)
        self.assertEqual(
            keys(result, "C-20"), [("C-20", m.WARNING, "unallocated_other")]
        )


class TestEnteredEconomicYearOfASingleInvoice(EngineCase):
    """R-07, 5.6.1: an entered economic year alone does not show that the
    payment recurs; the user decides (C-26)."""

    def book(self, recurring=False):
        book = Book()
        book.partner("Agentur", recurring=recurring)
        invoice = self.sale(
            book,
            "RE",
            d(2026, 12, 13),
            80,
            partner="Agentur",
            invoice_date=d(2026, 12, 13),
            invoice_date_due=d(2026, 12, 28),
            info={"economic_year": 2026},
        )
        self.receipt(book, "Bank", d(2027, 1, 10), 80, invoice, partner="Agentur")
        return book

    def test_single_invoice(self):
        result = self.run_year(self.book(), 2027)
        item = next(i for i in result.ten_day_items if i.kind == "recurring")
        self.assertIn("economic_year_entered", item.reasons)
        self.assertEqual((item.proposal, item.decision), ("apply", None))
        self.assertFalse(item.applied)
        self.assertIn("C-26", codes(result, m.BLOCKER))
        self.assertEqual(fields(result), {KU: D("80.00")})

    def test_recurring_partner(self):
        result = self.run_year(self.book(recurring=True), 2027)
        item = next(i for i in result.ten_day_items if i.kind == "recurring")
        self.assertEqual(item.decision, "auto")
        self.assertNotIn("C-26", codes(result))
        self.assertEqual(fields(result), {})
