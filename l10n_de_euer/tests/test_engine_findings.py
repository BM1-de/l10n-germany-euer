# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Regression tests of the review of the calculation (stage E2, round 1).

Pure engine tests with synthetic data (decision D6): gifts per recipient,
ten-day rule with derived economic years and VAT refunds, revenue tags,
private account journal, VAT payments and refunds in one movement, order
of write-offs, reversal pairs, rounding of transit entries and instalments,
private lines in documents, cash date of statements, invariants, small
business limits without parameters, reconciliations with another business.
"""

import dataclasses
import random
from decimal import Decimal as D
from fractions import Fraction

from ..models.engine import engine
from ..models.engine import model as m
from ..models.engine.graph import Graph
from ..models.engine.money import MoneyFlow
from ..models.engine.parameters import Parameters
from ..models.engine.ten_day import CashDates
from .engine_common import Book, EngineCase, codes, d, fields, ln

KU = "EUER.20.111"
TAXABLE = "EUER.20.112"
EXEMPT = "EUER.20.103"
OUTPUT_VAT = "EUER.20.140"
REFUND = "EUER.20.141"
INPUT_VAT = "EUER.25.185"
VAT_PAID = "EUER.25.186"
RENT = "EUER.25.150"
GIFT = "EUER.25.174"
GIFT_NOT_DEDUCTIBLE = "EUER.25.164"
WITHDRAWAL = "EUER.29.122"
CONTRIBUTION = "EUER.29.123"


class TestGiftRecipients(EngineCase):
    """5.7 no. 7: recipient entered on the document, never the supplier."""

    def bill(self, book, day, net, supplier, recipient=None, vat=None, **info):
        lines = [ln("4630", net, tax="vst19" if vat else ())]
        if vat:
            account, amount = vat
            lines.append(ln(account, amount, rep="vst19"))
        total = net + (vat[1] if vat else 0)
        lines.append(ln("1610", -total))
        if recipient:
            info["gift_recipient_id"] = book.partner(recipient)
        return book.move(
            "Geschenk",
            day,
            *lines,
            partner=supplier,
            move_type="in_invoice",
            info=info or None,
        )

    def pay(self, book, bill, day):
        amount = -bill("1610").balance / 100
        bank = book.move("Bank", day, ("1610", amount), ("1200", -amount))
        book.rec(bill("1610"), bank("1610"))
        return bank

    def test_two_suppliers_one_recipient(self):
        """I2: 30 € at two shops for the same recipient → 164."""
        book = Book()
        for supplier in ("Shop A", "Shop B"):
            self.pay(
                book,
                self.bill(book, d(2026, 3, 1), 30, supplier, "Kunde R"),
                d(2026, 3, 2),
            )
        self.assertEqual(
            fields(self.run_year(book, 2026)), {GIFT_NOT_DEDUCTIBLE: D("60.00")}
        )

    def test_one_supplier_two_recipients(self):
        """I1a: two gifts of 40 € at one shop for two customers → 174."""
        book = Book()
        for recipient in ("Kunde X", "Kunde Y"):
            self.pay(
                book,
                self.bill(book, d(2026, 3, 1), 40, "Shop", recipient),
                d(2026, 3, 2),
            )
        self.assertEqual(fields(self.run_year(book, 2026)), {GIFT: D("80.00")})

    def test_invoice_without_recipient(self):
        """The supplier is no recipient: C-51 until the recipient is entered."""
        book = Book()
        self.pay(book, self.bill(book, d(2026, 3, 1), 30, "Shop"), d(2026, 3, 2))
        result = self.run_year(book, 2026)
        self.assertIn("C-51", codes(result, m.BLOCKER))

    def test_unpaid_gift_of_the_year_counts(self):
        """The limit counts every gift of the year, paid or not."""
        book = Book()
        first = self.bill(book, d(2025, 3, 1), 30, "Shop", "Kunde")
        self.pay(book, first, d(2025, 3, 2))
        second = self.bill(book, d(2025, 12, 15), 30, "Shop", "Kunde")
        self.assertEqual(
            fields(self.run_year(book, 2025)), {GIFT_NOT_DEDUCTIBLE: D("30.00")}
        )
        self.pay(book, second, d(2026, 5, 10))
        self.assertEqual(
            fields(self.run_year(book, 2025)), {GIFT_NOT_DEDUCTIBLE: D("30.00")}
        )
        self.assertEqual(
            fields(self.run_year(book, 2026)), {GIFT_NOT_DEDUCTIBLE: D("30.00")}
        )

    def test_date_of_the_gift(self):
        """I6: bought in November, handed over in January → year of handover."""
        book = Book()
        first = self.bill(
            book, d(2026, 11, 10), 30, "Shop", "Kunde", gift_date=d(2027, 1, 5)
        )
        self.pay(book, first, d(2026, 11, 12))
        second = self.bill(book, d(2027, 3, 1), 30, "Shop", "Kunde")
        self.pay(book, second, d(2027, 3, 2))
        self.assertEqual(
            fields(self.run_year(book, 2026)), {GIFT_NOT_DEDUCTIBLE: D("30.00")}
        )
        self.assertEqual(
            fields(self.run_year(book, 2027)), {GIFT_NOT_DEDUCTIBLE: D("30.00")}
        )

    def test_input_vat_not_deductible_counts_gross(self):
        """R 4.10 (3) EStR: VAT that is not deductible belongs to the value."""
        book = Book(regime="regular")
        bill = self.bill(book, d(2026, 3, 1), 45, "Shop", "Kunde", vat=("1566", 8.55))
        self.pay(book, bill, d(2026, 3, 2))
        self.assertEqual(
            fields(self.run_year(book, 2026)), {GIFT_NOT_DEDUCTIBLE: D("53.55")}
        )
        book = Book(regime="regular")
        bill = self.bill(book, d(2026, 3, 1), 45, "Shop", "Kunde", vat=("1576", 8.55))
        self.pay(book, bill, d(2026, 3, 2))
        self.assertEqual(
            fields(self.run_year(book, 2026)),
            {GIFT: D("45.00"), INPUT_VAT: D("8.55")},
        )

    def test_journal_entry_with_entered_recipient(self):
        """I5: bank entry on the gift account: the recipient is entered (the
        partner of a bank entry is the shop that was paid)."""
        book = Book()
        for day in (d(2026, 3, 1), d(2026, 4, 1)):
            book.move(
                "Geschenk",
                day,
                ("4630", 30),
                ("1200", -30),
                partner="Laden",
                info={"gift_recipient_id": book.partner("Kunde")},
            )
        self.assertEqual(
            fields(self.run_year(book, 2026)), {GIFT_NOT_DEDUCTIBLE: D("60.00")}
        )


class TestTenDayCandidates(EngineCase):
    """5.6.1/5.6.2: derived economic years, refunds, proposals."""

    def rent(self, book, invoice_day, due, paid, amount=800, **values):
        bill = book.move(
            "Miete",
            invoice_day,
            ("4210", amount),
            ("1610", -amount),
            move_type="in_invoice",
            partner="Vermieter",
            invoice_date=invoice_day,
            invoice_date_due=due,
            **values,
        )
        bank = book.move(
            "Bank", paid, ("1610", amount), ("1200", -amount), partner="Vermieter"
        )
        book.rec(bill("1610"), bank("1610"))
        return bill, bank

    def monthly(self, book):
        for month in range(1, 12):
            day = d(2026, month, 15)
            self.rent(book, d(2026, month, 1), day, day)

    def test_b13_invoice_of_december_for_january(self):
        """Derived economic year = year of payment: still a candidate."""
        book = Book()
        self.monthly(book)
        bill, bank = self.rent(book, d(2026, 12, 15), d(2027, 1, 1), d(2026, 12, 28))
        result = self.run_year(book, 2026)
        self.assertIn("C-26", codes(result, m.BLOCKER))
        item = next(i for i in result.ten_day_items if i.origin_move_id == bill.move.id)
        self.assertEqual((item.economic_year, item.proposal), (2026, "review"))
        self.assertIn("flag", item.reasons)
        self.assertEqual(fields(result), {RENT: D("9600.00")})
        book.decide(bank, bill, "apply", economic_year=2027)
        self.assertEqual(fields(self.run_year(book, 2026)), {RENT: D("8800.00")})
        self.assertEqual(fields(self.run_year(book, 2027)), {RENT: D("800.00")})

    def test_b14_invoice_of_january_for_december(self):
        book = Book()
        self.monthly(book)
        bill, bank = self.rent(book, d(2027, 1, 2), d(2027, 1, 5), d(2027, 1, 5))
        result = self.run_year(book, 2027)
        self.assertIn("C-26", codes(result, m.BLOCKER))
        self.assertEqual(fields(result), {RENT: D("800.00")})
        book.decide(bank, bill, "apply", economic_year=2026)
        self.assertEqual(fields(self.run_year(book, 2026)), {RENT: D("9600.00")})
        self.assertEqual(fields(self.run_year(book, 2027)), {})

    def test_entered_economic_year_of_the_payment_year(self):
        """Only an entered economic year equal to the payment year ends it."""
        book = Book()
        self.monthly(book)
        self.rent(
            book,
            d(2027, 1, 2),
            d(2027, 1, 5),
            d(2027, 1, 5),
            info={"economic_year": 2027},
        )
        result = self.run_year(book, 2027)
        self.assertNotIn("C-26", codes(result))

    def refund(self, period, day, extension=False, **info):
        book = Book(regime="regular")
        book.set_regimes(
            m.VatPeriod(
                date_from=d(2020, 1, 1),
                regime="regular",
                permanent_extension=extension,
            )
        )
        move = book.move(
            "Erstattung",
            day,
            ("1200", 80),
            ("1545", -80),
            partner="Finanzamt",
            info={"vat_period": period, **info},
        )
        return book, move

    def test_b6_refund_with_permanent_extension(self):
        """B6: refund for December with extension, received 07.01."""
        book, move = self.refund("2026-12", d(2027, 1, 7), extension=True)
        result = self.run_year(book, 2026)
        self.assertIn("C-26", codes(result, m.BLOCKER))
        book.decide(move, move, "apply")
        self.assertEqual(fields(self.run_year(book, 2026)), {REFUND: D("80.00")})
        self.assertEqual(fields(self.run_year(book, 2027)), {})

    def test_b7_refund_of_november(self):
        """B7: refund for November without extension, received 05.01."""
        book, _move = self.refund("2026-11", d(2027, 1, 5), due_date=d(2027, 1, 4))
        result = self.run_year(book, 2026)
        self.assertNotIn("C-26", codes(result))
        self.assertEqual(fields(result), {REFUND: D("80.00")})

    def test_document_of_the_other_year_only_proposes_review(self):
        """A single invoice of the other year is no recurring payment."""
        book = Book()
        invoice = self.sale(
            book,
            "RE",
            d(2026, 12, 15),
            500,
            partner="Kunde",
            invoice_date=d(2026, 12, 15),
            invoice_date_due=d(2027, 1, 5),
        )
        self.receipt(book, "Bank", d(2027, 1, 7), 500, invoice)
        result = self.run_year(book, 2027)
        item = result.ten_day_items[0]
        self.assertEqual((item.reasons, item.proposal), (("other_year_due",), "review"))
        self.assertIn("C-26", codes(result, m.BLOCKER))

    def test_due_date_outside_the_window(self):
        """Due date known and outside the window: nothing to decide."""
        book = Book()
        self.rent(book, d(2026, 12, 1), d(2026, 12, 20), d(2027, 1, 6), amount=6)
        result = self.run_year(book, 2027)
        item = result.ten_day_items[-1]
        self.assertEqual((item.decision, item.proposal), ("auto", "reject"))
        self.assertNotIn("C-26", codes(result))
        self.assertEqual(fields(result), {RENT: D("6.00")})


class TestRevenueTags(EngineCase):
    def test_f3_taxable_account_with_exempt_tax(self):
        """R-29: tag 41 on a taxable revenue account → Kz 103."""
        book = Book(regime="regular")
        book.account("8400", assignments=(m.Assignment(category="REV_TAXABLE"),))
        book.taxes["eu0"] = m.Tax(
            id=99, type_tax_use="sale", factors=(("invoice", Fraction(100)),)
        )
        invoice = book.move(
            "RE EU",
            d(2026, 5, 1),
            ln("1410", 300),
            ln("8400", -300, tax="eu0", tags=("41",)),
            move_type="out_invoice",
        )
        self.receipt(book, "Bank", d(2026, 5, 10), 300, invoice)
        self.assertEqual(fields(self.run_year(book, 2026)), {EXEMPT: D("300.00")})


class TestPrivateAccountJournal(EngineCase):
    def test_h6_fixed_asset_paid_from_private_account(self):
        """R-18: everything paid from the private account is contributed."""
        book = Book(regime="regular")
        bill = book.move(
            "ER Anlage",
            d(2026, 4, 1),
            ln("0490", 1000, tax="vst19"),
            ln("1576", 190, rep="vst19"),
            ln("1610", -1190),
            move_type="in_invoice",
        )
        bank = book.move(
            "Privatkonto",
            d(2026, 4, 5),
            ("1610", 1190),
            ("1210", -1190),
            private_journal=True,
        )
        book.rec(bill("1610"), bank("1610"))
        self.assertEqual(
            fields(self.run_year(book, 2026)),
            {INPUT_VAT: D("190.00"), CONTRIBUTION: D("1190.00")},
        )


class TestVatSetOff(EngineCase):
    def test_s1_payment_and_refund_in_one_bank_entry(self):
        """R-20: the tax office sets a refund off against a payment."""
        book = Book(regime="regular")
        book.move(
            "Finanzamt",
            d(2026, 3, 10),
            ("1780", 300),
            ("1545", -80),
            ("1200", -220),
            partner="Finanzamt",
        )
        self.assertEqual(
            fields(self.run_year(book, 2026)),
            {VAT_PAID: D("300.00"), REFUND: D("80.00")},
        )

    def test_settlement_document_stays_netted(self):
        """ "1776/1576 an 1780", paid by bank: the balance is the payment."""
        book = Book(regime="regular")
        settlement = book.move(
            "USt-Verrechnung",
            d(2026, 2, 10),
            ("1776", 500),
            ("1576", -200),
            ("1780", -300),
        )
        bank = book.move("Bank", d(2026, 2, 12), ("1780", 300), ("1200", -300))
        book.rec(settlement("1780"), bank("1780"))
        self.assertEqual(fields(self.run_year(book, 2026)), {VAT_PAID: D("300.00")})


class TestWriteOffOrder(EngineCase):
    """5.5: a write-off settles the rest after the earlier partials."""

    def invoice(self, book):
        return self.regular_sale(book, d(2025, 3, 1), 1000)

    def test_write_off_on_the_day_of_the_payment(self):
        book = Book(regime="regular")
        invoice = self.invoice(book)
        self.receipt(book, "Bank", d(2025, 4, 1), 950, invoice)
        writeoff = book.move("Ausbuchung", d(2025, 4, 1), ("2400", 240), ("1410", -240))
        book.rec(invoice("1410"), writeoff("1410"))
        result = self.run_year(book, 2025)
        self.assertEqual(
            fields(result), {TAXABLE: D("798.32"), OUTPUT_VAT: D("151.68")}
        )
        self.assertNotIn("C-36", codes(result))

    def test_write_off_between_registration_and_bank(self):
        book = Book(regime="regular")
        invoice = self.invoice(book)
        payment = book.move("Zahlung", d(2025, 4, 1), ("OR", 1100), ("1410", -1100))
        book.rec(invoice("1410"), payment("1410"))
        writeoff = book.move("Ausbuchung", d(2025, 4, 2), ("2400", 90), ("1410", -90))
        book.rec(invoice("1410"), writeoff("1410"))
        bank = book.move("Bank", d(2025, 4, 5), ("1200", 1100), ("OR", -1100))
        book.rec(payment("OR"), bank("OR"))
        result = self.run_year(book, 2025)
        self.assertEqual(
            fields(result), {TAXABLE: D("924.37"), OUTPUT_VAT: D("175.63")}
        )
        self.assertNotIn("C-36", codes(result))


class TestReversalPairs(EngineCase):
    def test_d1_cash_sale_reversed_next_year(self):
        """B-03: a money pair over the turn of the year is not left out."""
        book = Book(regime="regular")
        sale = book.move(
            "Kassenverkauf",
            d(2025, 12, 20),
            ln("1000", 119),
            ln("8400", -100, tax="ust19"),
            ln("1776", -19, rep="ust19"),
        )
        book.move(
            "Storno",
            d(2026, 1, 15),
            ln("1000", -119),
            ln("8400", 100, tax="ust19"),
            ln("1776", 19, rep="ust19"),
            reversed_entry_id=sale.move.id,
        )
        self.assertEqual(
            fields(self.run_year(book, 2025)),
            {TAXABLE: D("100.00"), OUTPUT_VAT: D("19.00")},
        )
        result = self.run_year(book, 2026)
        self.assertEqual(
            result.totals_for(2026, overflow=False),
            {TAXABLE: -10000, OUTPUT_VAT: -1900},
        )
        book.moves[-1] = dataclasses.replace(book.moves[-1], date=d(2025, 12, 30))
        self.assertEqual(fields(self.run_year(book, 2025)), {})

    def test_credit_note_with_refund_repartition(self):
        """Invoice and credit note of the reversal wizard: refund lines."""
        book = Book(regime="regular")
        invoice = self.regular_sale(book, d(2026, 5, 1), 100)
        credit = book.move(
            "Gutschrift",
            d(2026, 5, 2),
            ln("1410", -119),
            ln("8400", 100, tax="ust19"),
            ln("1776", 19, rep="ust19", rep_values={"document_type": "refund"}),
            move_type="out_refund",
            reversed_entry_id=invoice.move.id,
        )
        book.rec(invoice("1410"), credit("1410"))
        graph = Graph(book.data())
        self.assertEqual(graph.excluded_pairs, {invoice.move.id, credit.move.id})


class TestRounding(EngineCase):
    def transit(self, invoices, line_n, chunks, seed):
        """Payment over a transit entry with an N line, money in chunks."""
        book = Book()
        documents = [
            self.sale(book, f"RE {index}", d(2025, 1, 2), amount)
            for index, amount in enumerate(invoices)
        ]
        total = sum(D(str(x)) for x in invoices) + D(str(line_n))
        payment = book.move(
            "Zahlung",
            d(2025, 1, 3),
            ("OR", float(total)),
            *[("1410", -amount) for amount in invoices],
            ("2650", -line_n),
        )
        for index, document in enumerate(documents):
            book.rec(document("1410"), payment("1410", index))
        rng = random.Random(seed)
        cents = int(total * 100)
        cuts = sorted(rng.sample(range(1, cents), chunks - 1))
        parts = [b - a for a, b in zip([0, *cuts], [*cuts, cents], strict=True)]
        for index, part in enumerate(parts):
            day = d(2025 + index * 3 // chunks, 1 + index % 12, 1 + index % 28)
            bank = book.move("Bank", day, ("1200", part / 100), ("OR", -part / 100))
            book.rec(payment("OR"), bank("OR"))
        return book, documents, payment

    def realised(self, results, line_id):
        return sum(
            one.amount
            for result in results
            for one in result.slices
            if one.origin_line_id == line_id and one.attribution_year == result.year
        )

    def check_transit(self, invoices, line_n, chunks, seed):
        book, documents, payment = self.transit(invoices, line_n, chunks, seed)
        results = [self.run_year(book, year) for year in (2025, 2026, 2027)]
        self.assertEqual(
            self.realised(results, payment("2650").id), payment("2650").balance
        )
        for document in documents:
            line = document("8195")
            self.assertEqual(self.realised(results, line.id), line.balance)

    def test_transit_in_many_parts_without_drift(self):
        """fz_transit: the N line and every invoice exactly, no open cent."""
        self.check_transit((2.61, 2.50, 2.53, 2.55), 1.51, 6, 11)
        for seed in range(20):
            self.check_transit((0.02, 0.02, 0.04, 37.08), 0.14, 40, seed)

    def test_instalments_add_up_per_line(self):
        """t_ports: 1.01 + 0.19 in three instalments of 0.40."""
        book = Book(regime="regular")
        invoice = book.move(
            "RE",
            d(2026, 3, 1),
            ln("1410", 0.40),
            ln("1410", 0.40),
            ln("1410", 0.40),
            ln("8400", -1.01, tax="ust19"),
            ln("1776", -0.19, rep="ust19"),
            move_type="out_invoice",
        )
        for index in range(3):
            bank = book.move(
                "Rate", d(2026, 4 + index, 1), ("1200", 0.40), ("1410", -0.40)
            )
            book.rec(invoice("1410", index), bank("1410"))
        self.assertEqual(
            fields(self.run_year(book, 2026)),
            {TAXABLE: D("1.01"), OUTPUT_VAT: D("0.19")},
        )


class TestStructureRule(EngineCase):
    def test_invoice_only_on_private_account(self):
        """t_priv: "1800 an 1610" as invoice: withdrawal with the payment."""
        book = Book()
        bill = book.move(
            "ER privat",
            d(2024, 12, 10),
            ("1800", 119),
            ("1610", -119),
            move_type="in_invoice",
        )
        bank = book.move("Bank", d(2025, 1, 20), ("1610", 119), ("1200", -119))
        book.rec(bill("1610"), bank("1610"))
        result = self.run_year(book, 2024)
        self.assertEqual(fields(result), {})
        self.assertNotIn("C-37", codes(result))
        self.assertEqual(fields(self.run_year(book, 2025)), {WITHDRAWAL: D("119.00")})


class TestCashDateOfStatement(EngineCase):
    def test_c24_takes_the_override_of_the_statement(self):
        """t_c24: the cash date of the statement entry, not its date."""
        book = Book()
        invoice = self.sale(book, "RE", d(2026, 12, 1), 100)
        payment = book.move("Zahlung", d(2026, 12, 28), ("1200", 100), ("1410", -100))
        statement_entry = book.move(
            "Auszug",
            d(2027, 1, 4),
            ("1200", 100),
            ("1200", -100),
            statement=True,
            bank_date=d(2026, 12, 30),
            info={"cash_date": d(2026, 12, 30), "cash_date_reason": "other"},
        )
        book.rec(invoice("1410"), payment("1410"))
        book.rec(payment("1200"), statement_entry("1200", 1))
        self.assertEqual(fields(self.run_year(book, 2026)), {KU: D("100.00")})
        self.assertEqual(fields(self.run_year(book, 2027)), {})


class TestInvariants(EngineCase):
    def flow(self, book):
        graph = Graph(book.data())
        cash = CashDates(graph, Parameters(book.parameters))
        flow = MoneyFlow(graph, cash)
        flow.run_sources()
        flow.run_documents()
        return graph, flow

    def test_open_item_checks(self):
        """Σ K(q) = −sign · req and a settled item has nothing open."""
        book = Book()
        invoice = self.sale(book, "RE", d(2026, 3, 1), 100)
        self.receipt(book, "Bank", d(2026, 3, 5), 100, invoice)
        graph, flow = self.flow(book)
        self.assertEqual(engine.check_ports(graph, flow), {})
        port = invoice("1410").id
        flow.req[port] += 1
        self.assertIn((port, engine.PORT_CHECK), engine.check_ports(graph, flow))

    def test_transit_checks(self):
        book = Book()
        invoice = self.sale(book, "RE", d(2026, 3, 1), 100)
        payment = book.move("Zahlung", d(2026, 3, 2), ("OR", 100), ("1410", -100))
        bank = book.move("Bank", d(2026, 3, 5), ("1200", 100), ("OR", -100))
        book.rec(invoice("1410"), payment("1410"))
        book.rec(payment("OR"), bank("OR"))
        graph, flow = self.flow(book)
        self.assertEqual(engine.check_transits(graph, flow), {})
        partial = graph.edges[payment("1410").id][0][0]
        flow.forwarded[(partial.id, payment("1410").id)] -= 1
        self.assertIn(payment.move.id, engine.check_transits(graph, flow))


class TestChecks(EngineCase):
    def test_small_business_limit_without_parameters(self):
        """Years before the limit parameters: no limit of 0 (C-32)."""
        book = Book()
        self.receipt(
            book, "Bank", d(2018, 5, 2), 120, self.sale(book, "RE", d(2018, 5, 1), 120)
        )
        result = self.run_year(book, 2018)
        self.assertNotIn("C-32", codes(result))
        self.assertEqual(fields(result), {KU: D("120.00")})

    def test_partial_with_another_business(self):
        """C-47 instead of a misleading C-20; the pair is not left out."""
        book = Book()
        invoice = self.sale(book, "RE", d(2026, 3, 1), 595)
        book.partials.append(
            m.Partial(
                id=90,
                debit_line_id=invoice("1410").id,
                credit_line_id=99999,
                amount=59500,
                max_date=d(2026, 3, 5),
            )
        )
        bank = book.move("Bank", d(2026, 3, 5), ("1200", 100), ("1410", -100))
        book.partials.append(
            m.Partial(
                id=91,
                debit_line_id=99998,
                credit_line_id=bank("1410").id,
                amount=10000,
                max_date=d(2026, 3, 5),
            )
        )
        result = self.run_year(book, 2026)
        self.assertIn("C-47", codes(result, m.BLOCKER))
        self.assertNotIn("C-20", codes(result))
        self.assertEqual(fields(result), {})

    def test_lock_date_shift(self):
        """B-12: statement entry on the first day after the lock date."""
        book = Book()
        book.move(
            "Auszug",
            d(2039, 1, 1),
            ("1200", 620),
            ("8195", -620),
            statement=True,
            lock_date_shift=d(2038, 12, 31),
        )
        result = self.run_year(book, 2039)
        finding = next(f for f in result.findings if f.code == "C-12")
        self.assertEqual(
            (finding.key, finding.years), ("lock_date_shift", (2038, 2039))
        )

    def test_rest_of_a_refund(self):
        """Money from a supplier without document: a refund, not an advance."""
        book = Book()
        book.move("Bank", d(2026, 3, 5), ("1200", 50), ("1610", -50), partner="Shop")
        result = self.run_year(book, 2026)
        keys = {f.key for f in result.findings if f.code == "C-20"}
        self.assertEqual(keys, {"unallocated_refund"})
