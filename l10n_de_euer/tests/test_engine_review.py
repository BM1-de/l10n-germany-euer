# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Regression tests of the review of the calculation (stage E2, round 2).

Pure engine tests with synthetic data (decision D6): private lines next to
VAT items, due dates of journal entries, entered economic years, proposals
of the review list, slices moved by the ten-day rule, gift recipients of
money entries and per line, revenue accounts of small businesses with real
VAT, the private account journal, write-offs in payments and before them,
cash dates on payment entries, opening items without invoice, suspense lines
of documents, transfer orders of payments on the bank account, categories
realised on booking, reversal pairs over the turn of the year, gifts on
non-deductible accounts and rests that cancel each other.
"""

from decimal import Decimal as D

from ..models.engine import model as m
from .engine_common import Book, EngineCase, codes, d, fields, ln

KU = "EUER.20.111"
TAXABLE = "EUER.20.112"
OUTPUT_VAT = "EUER.20.140"
REFUND = "EUER.20.141"
SERVICES = "EUER.25.110"
CAR_USE = "EUER.25.147"
RENT = "EUER.25.150"
GIFT_NOT_DEDUCTIBLE = "EUER.25.164"
GIFT = "EUER.25.174"
OTHER = "EUER.25.183"
INPUT_VAT = "EUER.25.185"
VAT_PAID = "EUER.25.186"
WORK = "EUER.25.229"
WITHDRAWAL = "EUER.29.122"
CONTRIBUTION = "EUER.29.123"


class TestPrivateLineNextToVatItem(EngineCase):
    """5.2 (b): open items never keep a private line from being money."""

    def test_vat_paid_privately(self):
        """B13: "1780 an 1890": VAT payment and contribution."""
        book = Book(regime="regular")
        entry = book.move(
            "USt privat bezahlt",
            d(2026, 5, 10),
            ("1780", 300),
            ("1890", -300),
            info={"vat_period": "2026-04"},
        )
        result = self.run_year(book, 2026)
        self.assertEqual(result.classes[entry.move.id], m.SOURCE)
        self.assertEqual(result.roles[entry("1890").id], m.L)
        self.assertEqual(
            fields(result), {VAT_PAID: D("300.00"), CONTRIBUTION: D("300.00")}
        )

    def test_refund_set_off_against_private_tax(self):
        """B12: the tax office sets a VAT refund off against income tax."""
        book = Book(regime="regular")
        book.move("Verrechnung", d(2026, 6, 1), ("1800", 500), ("1545", -500))
        self.assertEqual(
            fields(self.run_year(book, 2026)),
            {REFUND: D("500.00"), WITHDRAWAL: D("500.00")},
        )

    def test_settlement_account_without_reconciliation(self):
        """ "1789 an 1890": no open item at all (5.2 a)."""
        book = Book(regime="regular")
        book.move("USt privat", d(2026, 6, 1), ("1789", 100), ("1890", -100))
        self.assertEqual(
            fields(self.run_year(book, 2026)),
            {VAT_PAID: D("100.00"), CONTRIBUTION: D("100.00")},
        )

    def test_private_share_of_an_invoice_stays_on_payment(self):
        """In an invoice the private line remains "withdrawal on payment"."""
        book = Book()
        bill = book.move(
            "ER",
            d(2026, 3, 1),
            ("4930", 60),
            ("1800", 40),
            ("1610", -100),
            move_type="in_invoice",
        )
        self.assertEqual(Book.run(book, 2026).roles[bill("1800").id], m.N)


class TestDueDateOfEntries(EngineCase):
    """5.6.2: only an invoice has a due date; Odoo fills ``invoice_date_due``
    of journal entries with the day they were created."""

    def test_rent_paid_in_december_by_bank(self):
        """B05: January rent paid by bank on 28.12.: C-26, not "reject"."""
        book = Book()
        bank = book.move(
            "Miete Januar",
            d(2026, 12, 28),
            ("4210", 800),
            ("1200", -800),
            partner="Vermieter",
            invoice_date_due=d(2026, 10, 18),
        )
        result = self.run_year(book, 2026)
        item = result.ten_day_items[0]
        self.assertIsNone(item.due_date)
        self.assertIsNone(item.decision)
        self.assertIn("C-26", codes(result, m.BLOCKER))
        book.decide(bank, bank, "apply", economic_year=2027)
        result = self.run_year(book, 2026)
        # applied without a due date: still blocked, nothing moved
        self.assertIn("C-26", codes(result, m.BLOCKER))
        self.assertEqual(fields(result), {RENT: D("800.00")})
        book.decisions.clear()
        book.decide(bank, bank, "apply", economic_year=2027, due_date=d(2027, 1, 1))
        self.assertEqual(fields(self.run_year(book, 2026)), {})
        self.assertEqual(fields(self.run_year(book, 2027)), {RENT: D("800.00")})

    def test_vat_payment_without_period(self):
        """B09: VAT paid on 08.01. without return period: C-26."""
        book = Book(regime="regular")
        book.partner("Finanzamt", tax_office=True)
        book.move(
            "USt",
            d(2027, 1, 8),
            ("1780", 186),
            ("1200", -186),
            partner="Finanzamt",
            invoice_date_due=d(2026, 10, 18),
        )
        result = self.run_year(book, 2027)
        item = result.ten_day_items[0]
        self.assertEqual((item.kind, item.decision), ("vat", None))
        self.assertIn("C-26", codes(result, m.BLOCKER))

    def test_invoice_due_date_still_decides(self):
        """The due date of an invoice outside the window: nothing to decide."""
        book = Book()
        bill = book.move(
            "Miete",
            d(2026, 12, 1),
            ("4210", 6),
            ("1610", -6),
            move_type="in_invoice",
            partner="Vermieter",
            invoice_date_due=d(2026, 12, 20),
        )
        bank = book.move("Bank", d(2027, 1, 6), ("1610", 6), ("1200", -6))
        book.rec(bill("1610"), bank("1610"))
        result = self.run_year(book, 2027)
        self.assertEqual(result.ten_day_items[-1].decision, "auto")
        self.assertNotIn("C-26", codes(result))


class TestEnteredEconomicYear(EngineCase):
    """5.6.1: an entered economic year other than the year of payment."""

    def contract(self, book, due, paid=None):
        paid = paid or d(2026, 12, 30)
        bill = book.move(
            "Wartungsvertrag",
            d(2026, 12, 15),
            ln("4925", 1000, tax="vst19"),
            ln("1576", 190, rep="vst19"),
            ln("1610", -1190),
            move_type="in_invoice",
            partner="Wartung",
            invoice_date=d(2026, 12, 15),
            invoice_date_due=due,
            info={"economic_year": 2027},
        )
        bank = book.move("Bank", paid, ("1610", 1190), ("1200", -1190))
        book.rec(bill("1610"), bank("1610"))
        return bill, bank

    def test_b06_maintenance_contract(self):
        """Due date certain and in the window, the supplier recurs (flag):
        decided automatically."""
        book = Book(regime="regular")
        book.partner("Wartung", recurring=True)
        self.contract(book, d(2027, 1, 2))
        result = self.run_year(book, 2026)
        item = next(i for i in result.ten_day_items if i.kind == "recurring")
        self.assertIn("economic_year_entered", item.reasons)
        self.assertEqual((item.proposal, item.decision), ("apply", "auto"))
        self.assertNotIn("C-26", codes(result))
        self.assertEqual(fields(result), {})
        self.assertEqual(
            fields(self.run_year(book, 2027)),
            {SERVICES: D("1000.00"), INPUT_VAT: D("190.00")},
        )

    def test_due_date_outside_the_window(self):
        book = Book(regime="regular")
        self.contract(book, d(2026, 12, 20))
        result = self.run_year(book, 2026)
        item = next(i for i in result.ten_day_items if i.kind == "recurring")
        self.assertEqual((item.decision, item.proposal), ("auto", "reject"))
        self.assertEqual(
            fields(result), {SERVICES: D("1000.00"), INPUT_VAT: D("190.00")}
        )

    def test_entered_year_on_a_bank_entry(self):
        """Without a due date the user decides (C-26), proposal "apply"."""
        book = Book()
        book.move(
            "Wartung",
            d(2026, 12, 30),
            ("4925", 500),
            ("1200", -500),
            info={"economic_year": 2027},
        )
        result = self.run_year(book, 2026)
        item = result.ten_day_items[0]
        self.assertEqual(
            (item.reasons, item.proposal), (("economic_year_entered",), "apply")
        )
        self.assertIn("C-26", codes(result, m.BLOCKER))


class TestRepeatedPartnerProposal(EngineCase):
    """5.6.1 proposal: similar payments alone only propose "review"."""

    def project(self, book, day, amount, due, paid):
        invoice = self.sale(
            book,
            "RE",
            day,
            amount,
            partner="Stammkunde",
            invoice_date=day,
            invoice_date_due=due,
        )
        self.receipt(book, "Bank", paid, amount, invoice, partner="Stammkunde")
        return invoice

    def test_project_invoices_of_a_regular_customer(self):
        book = Book()
        self.project(book, d(2026, 3, 1), 410, d(2026, 3, 15), d(2026, 3, 20))
        self.project(book, d(2026, 7, 1), 365, d(2026, 7, 15), d(2026, 7, 20))
        self.project(book, d(2026, 12, 11), 438, d(2026, 12, 25), d(2027, 1, 5))
        result = self.run_year(book, 2027)
        item = result.ten_day_items[-1]
        self.assertEqual(item.reasons, ("other_year_due", "repeated_partner"))
        self.assertEqual(item.proposal, "review")
        self.assertIn("C-26", codes(result, m.BLOCKER))

    def test_steady_monthly_payments_propose_apply(self):
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
            bank = book.move("Bank", day, ("1610", 600), ("1200", -600))
            book.rec(bill("1610"), bank("1610"))
        item = next(
            i for i in self.run_year(book, 2026).ten_day_items if i.kind == "recurring"
        )
        self.assertNotIn("flag", item.reasons)
        self.assertEqual(item.proposal, "apply")


class TestTenDayMovesTheRecurringPart(EngineCase):
    """5.6: lines of the money entry that are not recurring stay."""

    def test_t1_late_payment_surcharge_with_vat(self):
        book = Book(regime="regular")
        book.partner("Finanzamt", tax_office=True)
        book.move(
            "USt Dezember",
            d(2027, 1, 5),
            ("1780", 1000),
            ("2150", 25),
            ("1200", -1025),
            partner="Finanzamt",
            info={"vat_period": "2026-12"},
        )
        self.assertEqual(fields(self.run_year(book, 2026)), {VAT_PAID: D("1000.00")})
        self.assertEqual(fields(self.run_year(book, 2027)), {OTHER: D("25.00")})

    def test_t2_bank_fee_with_the_rent(self):
        book = Book()
        bank = book.move(
            "Miete Dezember",
            d(2027, 1, 3),
            ("4210", 800),
            ("4970", 7.50),
            ("1200", -807.50),
            partner="Vermieter",
            info={"economic_year": 2026},
        )
        book.decide(bank, bank, "apply", due_date=d(2026, 12, 31))
        self.assertEqual(fields(self.run_year(book, 2026)), {RENT: D("800.00")})
        self.assertEqual(fields(self.run_year(book, 2027)), {OTHER: D("7.50")})


class TestGiftRecipientOfMoneyEntries(EngineCase):
    """5.7 no. 7: the partner of a bank entry is the shop, no recipient."""

    def test_i03_two_payments_at_one_shop(self):
        """Two gifts of 40 € paid at one shop for two customers."""
        for recipients in ((None, None), ("Kunde A", "Kunde B")):
            book = Book()
            for day, recipient in zip(
                (d(2026, 5, 2), d(2026, 6, 3)), recipients, strict=True
            ):
                info = {"gift_recipient_id": book.partner(recipient)}
                book.move(
                    "Geschenk",
                    day,
                    ("4630", 40),
                    ("1200", -40),
                    partner="Shop",
                    info=info if recipient else None,
                )
            result = self.run_year(book, 2026)
            self.assertEqual(fields(result), {GIFT: D("80.00")})
            if recipients[0]:
                self.assertNotIn("C-51", codes(result))
            else:
                self.assertIn("C-51", codes(result, m.BLOCKER))

    def test_single_payment_without_recipient(self):
        book = Book()
        book.move(
            "Geschenk", d(2026, 5, 2), ("4630", 30), ("1200", -30), partner="Shop"
        )
        self.assertIn("C-51", codes(self.run_year(book, 2026), m.BLOCKER))


class TestGiftLines(EngineCase):
    """5.7 no. 7: several recipients on one invoice (per line)."""

    def bill(self, book, *lines, **info):
        return book.move(
            "Präsentkörbe",
            d(2026, 11, 20),
            *lines,
            partner="Shop I04",
            move_type="in_invoice",
            info=info or None,
        )

    def pay(self, book, bill, amount):
        bank = book.move("Bank", d(2026, 11, 25), ("1610", amount), ("1200", -amount))
        book.rec(bill("1610"), bank("1610"))

    def test_i04_two_lines_for_two_recipients(self):
        book = Book()
        bill = self.bill(
            book,
            ln("4630", 40),
            ln("4630", 40),
            ln("1610", -80),
            gift_recipient_id=book.partner("Kunde A"),
        )
        self.pay(book, bill, 80)
        self.assertEqual(
            fields(self.run_year(book, 2026)), {GIFT_NOT_DEDUCTIBLE: D("80.00")}
        )
        book.gift_line(bill("4630", 0), "Kunde A")
        book.gift_line(bill("4630", 1), "Kunde B")
        self.assertEqual(fields(self.run_year(book, 2026)), {GIFT: D("80.00")})

    def test_one_line_shared_by_recipients(self):
        book = Book()
        bill = self.bill(book, ln("4630", 80), ln("1610", -80))
        self.pay(book, bill, 80)
        book.gift_line(bill("4630"), "Kunde A")
        book.gift_line(bill("4630"), "Kunde B")
        result = self.run_year(book, 2026)
        self.assertNotIn("C-51", codes(result))
        self.assertEqual(fields(result), {GIFT: D("80.00")})
        book.gift_lines.clear()
        book.gift_line(bill("4630"), "Kunde A", share=3)
        book.gift_line(bill("4630"), "Kunde B", share=1)
        self.assertEqual(
            fields(self.run_year(book, 2026)),
            {GIFT_NOT_DEDUCTIBLE: D("60.00"), GIFT: D("20.00")},
        )


class TestNonDeductibleGiftAccount(EngineCase):
    """Gifts on the account for non-deductible gifts stay non-deductible."""

    def gift(self, **info):
        book = Book()
        bill = book.move(
            "Präsent",
            d(2026, 11, 20),
            ("4635", 64.20),
            ("1610", -64.20),
            partner="Shop",
            move_type="in_invoice",
            info=info or None,
        )
        bank = book.move("Bank", d(2026, 11, 30), ("1610", 64.20), ("1200", -64.20))
        book.rec(bill("1610"), bank("1610"))
        return book

    def test_without_recipient(self):
        result = self.run_year(self.gift(), 2026)
        self.assertEqual(fields(result), {GIFT_NOT_DEDUCTIBLE: D("64.20")})
        self.assertNotIn("C-51", codes(result))

    def test_bulk_not_allowed(self):
        result = self.run_year(self.gift(gift_bulk=True), 2026)
        self.assertEqual(fields(result), {GIFT_NOT_DEDUCTIBLE: D("64.20")})

    def test_bulk_needs_a_category_that_allows_it(self):
        """Give-aways only where the category allows them (R-41)."""
        book = Book()
        book.categories += (
            m.Category(
                code="TEST_GIFT",
                role="ba",
                separate_record="gift",
                restricted_column="deductible",
            ),
        )
        book.mappings = dict(book.mappings)
        book.mappings[2026] += (m.MappingLine(category="TEST_GIFT", field=GIFT),)
        book.account("4630", assignments=(m.Assignment(category="TEST_GIFT"),))
        book.move(
            "Präsente",
            d(2026, 3, 1),
            ("4630", 30),
            ("1200", -30),
            info={"gift_bulk": True},
        )
        self.assertIn("C-51", codes(self.run_year(book, 2026), m.BLOCKER))


class TestSmallBusinessAccountWithVat(EngineCase):
    """4.4 no. 5: a real output VAT on 8195 needs no rate input (F04)."""

    def test_f04(self):
        book = Book(regime="regular")
        invoice = book.move(
            "RE",
            d(2026, 5, 1),
            ln("1410", 119),
            ln("8195", -100, tax="ust19", tags=("81_BASE",)),
            ln("1776", -19, rep="ust19"),
            move_type="out_invoice",
        )
        self.receipt(book, "Bank", d(2026, 5, 10), 119, invoice)
        result = self.run_year(book, 2026)
        self.assertNotIn("C-33", codes(result))
        self.assertEqual(fields(result), {TAXABLE: D("100.00"), OUTPUT_VAT: D("19.00")})


class TestPrivateAccountJournal(EngineCase):
    """R-18, 5.7 no. 9: the private account journal."""

    def book(self, regime=m.KU):
        book = Book(regime=regime)
        book.account("1210", private_money=True)
        return book

    def test_h06_private_share_paid_privately(self):
        """The private share of a bill paid from the private account is no
        withdrawal; the business part is contributed."""
        book = self.book(regime="regular")
        bill = book.move(
            "ER",
            d(2026, 4, 1),
            ln("4930", 100, tax="vst19"),
            ln("1576", 19, rep="vst19"),
            ln("1800", 100),
            ln("1610", -219),
            move_type="in_invoice",
        )
        bank = book.move(
            "Privatkonto",
            d(2026, 4, 5),
            ("1610", 219),
            ("1210", -219),
            private_journal=True,
        )
        book.rec(bill("1610"), bank("1610"))
        self.assertEqual(
            fields(self.run_year(book, 2026)),
            {WORK: D("100.00"), INPUT_VAT: D("19.00"), CONTRIBUTION: D("119.00")},
        )

    def test_p1_cash_to_the_private_account(self):
        book = self.book()
        book.move("Barverkauf", d(2026, 5, 2), ("1000", 500), ("8195", -500))
        out = book.move("Kasse", d(2026, 5, 3), ("1360", 500), ("1000", -500))
        private = book.move(
            "Privatkonto",
            d(2026, 5, 4),
            ("1210", 500),
            ("1360", -500),
            private_journal=True,
        )
        book.rec(out("1360"), private("1360"))
        self.assertEqual(
            fields(self.run_year(book, 2026)),
            {KU: D("500.00"), WITHDRAWAL: D("500.00")},
        )

    def test_p2_private_money_into_the_cash(self):
        book = self.book()
        private = book.move(
            "Privatkonto",
            d(2026, 5, 2),
            ("1360", 200),
            ("1210", -200),
            private_journal=True,
        )
        cash = book.move("Kasse", d(2026, 5, 3), ("1000", 200), ("1360", -200))
        book.rec(private("1360"), cash("1360"))
        book.move("Einkauf", d(2026, 5, 4), ("4930", 200), ("1000", -200))
        self.assertEqual(
            fields(self.run_year(book, 2026)),
            {WORK: D("200.00"), CONTRIBUTION: D("200.00")},
        )

    def test_m3_credit_card_paid_from_the_private_account(self):
        book = self.book()
        book.move("Karte", d(2026, 6, 1), ("4930", 100), ("1730", -100))
        book.move(
            "Abrechnung",
            d(2026, 6, 30),
            ("1730", 100),
            ("1210", -100),
            private_journal=True,
        )
        self.assertEqual(
            fields(self.run_year(book, 2026)),
            {WORK: D("100.00"), CONTRIBUTION: D("100.00")},
        )

    def test_h07_transfer_to_the_business_account(self):
        book = self.book()
        book.move(
            "Umbuchung",
            d(2026, 7, 1),
            ("1200", 1000),
            ("1210", -1000),
            private_journal=True,
        )
        self.assertEqual(
            fields(self.run_year(book, 2026)), {CONTRIBUTION: D("1000.00")}
        )

    def test_h08_contribution_on_the_day_of_payment(self):
        """The ten-day rule moves the expense, not the contribution."""
        book = self.book()
        bank = book.move(
            "Miete Januar",
            d(2026, 12, 28),
            ("4210", 800),
            ("1210", -800),
            partner="Vermieter",
            private_journal=True,
        )
        book.decide(bank, bank, "apply", economic_year=2027, due_date=d(2027, 1, 1))
        self.assertEqual(fields(self.run_year(book, 2026)), {CONTRIBUTION: D("800.00")})
        self.assertEqual(fields(self.run_year(book, 2027)), {RENT: D("800.00")})


class TestWriteOffs(EngineCase):
    """5.5: a write-off reduces the document; money never realises it."""

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

    def check(self, book, year=2026):
        result = self.run_year(book, year)
        self.assertNotIn("C-36", codes(result))
        self.assertEqual(
            fields(result), {TAXABLE: D("1769.91"), OUTPUT_VAT: D("230.09")}
        )

    def test_w1_payment_wizard_with_write_off(self):
        book = Book(regime="regular")
        invoice = self.invoice(book)
        payment = book.move(
            "Zahlung",
            d(2026, 3, 10),
            ("OR", 2000),
            ("2400", 260),
            ("1410", -2260),
        )
        book.rec(invoice("1410"), payment("1410"))
        bank = book.move("Bank", d(2026, 3, 12), ("1200", 2000), ("OR", -2000))
        book.rec(payment("OR"), bank("OR"))
        self.check(book)

    def test_w2_write_off_in_the_bank_reconciliation(self):
        book = Book(regime="regular")
        invoice = self.invoice(book)
        bank = book.move(
            "Bank",
            d(2026, 3, 12),
            ("1200", 2000),
            ("2400", 260),
            ("1410", -2260),
        )
        book.rec(invoice("1410"), bank("1410"))
        self.check(book)

    def test_w3_partial_write_off_before_the_payment(self):
        book = Book(regime="regular")
        invoice = self.invoice(book)
        writeoff = book.move("Ausbuchung", d(2026, 3, 5), ("2400", 260), ("1410", -260))
        book.rec(invoice("1410"), writeoff("1410"))
        bank = book.move("Bank", d(2026, 3, 12), ("1200", 2000), ("1410", -2000))
        book.rec(invoice("1410"), bank("1410"))
        self.check(book)

    def test_w4_write_off_after_the_payment(self):
        book = Book(regime="regular")
        invoice = self.invoice(book)
        bank = book.move("Bank", d(2026, 3, 12), ("1200", 2000), ("1410", -2000))
        book.rec(invoice("1410"), bank("1410"))
        writeoff = book.move(
            "Ausbuchung", d(2026, 3, 20), ("2400", 260), ("1410", -260)
        )
        book.rec(invoice("1410"), writeoff("1410"))
        self.check(book)

    def test_previous_year_does_not_wait_for_the_next(self):
        """t_wo_year: 2024 is the same with and without the statement of 2025."""
        book = Book(regime="regular")
        invoice = self.regular_sale(book, d(2024, 12, 1), 1000)
        payment = book.move("Zahlung", d(2024, 12, 20), ("OR", 500), ("1410", -500))
        book.rec(invoice("1410"), payment("1410"))
        writeoff = book.move("Ausbuchung", d(2024, 12, 21), ("2400", 90), ("1410", -90))
        book.rec(invoice("1410"), writeoff("1410"))
        bank = book.move("Bank", d(2024, 12, 28), ("1200", 600), ("1410", -600))
        book.rec(invoice("1410"), bank("1410"))
        expected = {TAXABLE: D("504.20"), OUTPUT_VAT: D("95.80")}
        self.assertEqual(fields(self.run_year(book, 2024)), expected)
        statement = book.move("Bank", d(2025, 1, 15), ("1200", 500), ("OR", -500))
        book.rec(payment("OR"), statement("OR"))
        self.assertEqual(fields(self.run_year(book, 2024)), expected)
        self.assertEqual(
            fields(self.run_year(book, 2025)),
            {TAXABLE: D("420.17"), OUTPUT_VAT: D("79.83")},
        )

    def test_returned_direct_debit_still_blocks(self):
        """R-53: money reaching a write-off over E1 stays C-36."""
        book = Book()
        invoice = self.sale(book, "RE", d(2026, 3, 1), 119)
        self.receipt(book, "Lastschrift", d(2026, 3, 10), 119, invoice)
        back = book.move(
            "Rücklastschrift", d(2026, 3, 20), ("1410", 119), ("1200", -119)
        )
        writeoff = book.move(
            "Ausbuchung", d(2026, 6, 30), ("2400", 119), ("1410", -119)
        )
        book.rec(back("1410"), writeoff("1410"))
        self.assertIn("C-36", codes(self.run_year(book, 2026), m.BLOCKER))


class TestCashDateOnPaymentEntry(EngineCase):
    """5.6: a cash date entered on a payment entry (cheque, O1)."""

    def test_o1_cheque_registered_as_payment(self):
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
        result = self.run_year(book, 2026)
        self.assertEqual(fields(result), {KU: D("200.00")})
        self.assertTrue(all(one.override for one in result.slices))
        self.assertEqual(fields(self.run_year(book, 2027)), {})


class TestEconomicYearOnPaymentEntry(EngineCase):
    """5.6.1: the economic year entered on a payment entry in the path."""

    def test_payment_entry_carries_the_economic_year(self):
        book = Book()
        book.partner("Wartung", recurring=True)
        bill = book.move(
            "Wartung",
            d(2026, 12, 15),
            ("4925", 500),
            ("1610", -500),
            move_type="in_invoice",
            invoice_date_due=d(2027, 1, 2),
            partner="Wartung",
        )
        payment = book.move(
            "Zahlung",
            d(2026, 12, 29),
            ("1610", 500),
            ("OR", -500),
            info={"economic_year": 2027},
        )
        book.rec(bill("1610"), payment("1610"))
        bank = book.move("Bank", d(2026, 12, 30), ("OR", 500), ("1200", -500))
        book.rec(payment("OR"), bank("OR"))
        result = self.run_year(book, 2026)
        item = next(i for i in result.ten_day_items if i.kind == "recurring")
        self.assertIn("economic_year_entered", item.reasons)
        self.assertEqual(item.decision, "auto")
        self.assertEqual(fields(result), {})
        self.assertEqual(fields(self.run_year(book, 2027)), {SERVICES: D("500.00")})


class TestOpeningWithoutInvoice(EngineCase):
    """B-08: open items on money transit and VAT accounts need no takeover."""

    def test_transit_and_vat_payment(self):
        book = Book(regime="regular")
        book.partner("Finanzamt", tax_office=True)
        opening = book.move(
            "EB",
            d(2026, 1, 1),
            ("1200", 5000),
            ("1360", 500),
            ("1780", -1000),
            ("9000", -4500),
        )
        bank = book.move("Bank", d(2026, 1, 2), ("1200", 500), ("1360", -500))
        book.rec(opening("1360"), bank("1360"))
        vat = book.move(
            "USt 12/2025",
            d(2026, 1, 8),
            ("1780", 1000),
            ("1200", -1000),
            partner="Finanzamt",
            info={"vat_period": "2025-12"},
        )
        book.rec(opening("1780"), vat("1780"))
        result = self.run_year(book, 2026)
        self.assertNotIn("C-35", codes(result))
        self.assertEqual(fields(result), {})
        self.assertEqual(fields(self.run_year(book, 2025)), {VAT_PAID: D("1000.00")})

    def test_receivable_still_needs_the_takeover(self):
        book = Book()
        opening = book.move("EB", d(2026, 1, 1), ("1410", 260), ("9008", -260))
        bank = book.move("Bank", d(2026, 1, 15), ("1200", 260), ("1410", -260))
        book.rec(opening("1410"), bank("1410"))
        self.assertIn("C-35", codes(self.run_year(book, 2026), m.BLOCKER))


class TestSmallFindings(EngineCase):
    def test_s1_document_line_on_the_suspense_account(self):
        """A document line on the suspense account is unclear (C-10)."""
        book = Book()
        bill = book.move(
            "ER",
            d(2026, 3, 1),
            ("4930", 100),
            ("SUSP", 50),
            ("1610", -150),
            move_type="in_invoice",
        )
        bank = book.move("Bank", d(2026, 3, 5), ("1610", 150), ("1200", -150))
        book.rec(bill("1610"), bank("1610"))
        result = self.run_year(book, 2026)
        self.assertIn("C-10", codes(result, m.BLOCKER))
        self.assertEqual(fields(result), {WORK: D("100.00")})

    def test_c1_payment_on_the_bank_account_in_january(self):
        """C-23 also for the date taken from the statement (C-24)."""
        book = Book()
        bill = book.move(
            "ER", d(2026, 12, 1), ("4930", 300), ("1610", -300), move_type="in_invoice"
        )
        payment = book.move("Zahlung", d(2026, 12, 30), ("1610", 300), ("1200", -300))
        book.rec(bill("1610"), payment("1610"))
        statement = book.move(
            "Auszug",
            d(2027, 1, 2),
            ("1200", 300),
            ("1200", -300),
            statement=True,
        )
        book.rec(statement("1200", 0), payment("1200"))
        result = self.run_year(book, 2027)
        self.assertIn("C-24", codes(result))
        self.assertIn("C-23", codes(result))
        item = next(i for i in result.ten_day_items if i.kind == "transfer_order")
        self.assertEqual(
            (item.source_move_id, item.cash_date), (payment.move.id, d(2027, 1, 2))
        )

    def test_m1_use_of_a_private_car_paid_by_bank(self):
        """4590 realised from business money: expense, no contribution."""
        book = Book()
        book.move("Kfz", d(2026, 3, 1), ("4590", 60), ("1200", -60))
        self.assertEqual(fields(self.run_year(book, 2026)), {CAR_USE: D("60.00")})

    def test_t19_reversal_pair_over_the_turn_of_the_year(self):
        """Left out, but reported in both years (C-48)."""
        book = Book()
        wrong = book.move("Fehlbuchung", d(2026, 12, 28), ("4930", 100), ("1200", -100))
        reversal = book.move(
            "Storno",
            d(2027, 1, 3),
            ("4930", -100),
            ("1200", 100),
            reversed_entry_id=wrong.move.id,
        )
        book.rec(wrong("1200"), reversal("1200"))
        for year in (2026, 2027):
            result = self.run_year(book, year)
            self.assertEqual(fields(result), {})
            self.assertIn("C-48", codes(result, m.WARNING))

    def test_rests_that_cancel_each_other(self):
        """+75 and −75 on the payable of one partner in one year."""
        book = Book()
        book.move("Zahlung", d(2026, 3, 1), ("1610", 75), ("1200", -75), partner="Shop")
        book.move(
            "Rückzahlung", d(2026, 3, 9), ("1200", 75), ("1610", -75), partner="Shop"
        )
        book.move(
            "Vorauszahlung", d(2026, 4, 1), ("1610", 50), ("1200", -50), partner="Shop"
        )
        result = self.run_year(book, 2026)
        keys = sorted((f.key, f.severity) for f in result.findings if f.code == "C-20")
        self.assertEqual(
            keys,
            [
                ("unallocated_open_item", m.BLOCKER),
                ("unallocated_pair", m.WARNING),
                ("unallocated_pair", m.WARNING),
            ],
        )


class TestExchangeGainAllocated(EngineCase):
    """Setting "allocate": a gain reached by money follows the document."""

    def book(self, mode):
        book = Book(regime="regular", fx_mode=mode)
        invoice = self.regular_sale(book, d(2026, 8, 1), 1000)
        bank = book.move("Bank", d(2026, 8, 20), ("1200", 1309), ("1410", -1309))
        gain = book.move("Kursdifferenz", d(2026, 8, 20), ("1410", 119), ("2660", -119))
        book.rec(invoice("1410"), bank("1410"), amount=1190, exchange=gain)
        book.rec(gain("1410"), bank("1410"), amount=119)
        return book

    def test_separate_position(self):
        self.assertEqual(
            fields(self.run_year(self.book("separate"), 2026)),
            {
                "EUER.20.103": D("119.00"),
                TAXABLE: D("1000.00"),
                OUTPUT_VAT: D("190.00"),
            },
        )

    def test_allocated_to_the_document(self):
        self.assertEqual(
            fields(self.run_year(self.book("allocate"), 2026)),
            {TAXABLE: D("1100.00"), OUTPUT_VAT: D("209.00")},
        )
