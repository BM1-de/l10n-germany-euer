# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Differential test: engine against the independent oracle (spec 9.1, 9.8).

Random synthetic books with invoices, partial payments, credit notes,
write-offs, collective payments over a transit entry and reversal pairs;
both implementations must realise the same amounts per year and account,
to the cent.
"""

import random
from collections import defaultdict

from odoo.tests import BaseCase

from ..models.engine import model as m
from .engine_common import Book, d, ln
from .oracle import Oracle

LINE_KINDS = (
    ("8400", "1776", "ust19", 19),
    ("8300", "1771", "ust7", 7),
)


def _engine_totals(result):
    totals = defaultdict(int)
    for one in result.slices:
        if one.kind in m.MONEY_KINDS:
            totals[(one.date.year, one.account_id)] += one.amount
    return {key: value for key, value in totals.items() if value}


class Scenario:
    """Random book of one case."""

    def __init__(self, generator):
        self.random = generator
        self.book = Book(regime="regular")
        self.day_counter = 0

    def day(self):
        self.day_counter += self.random.randint(1, 40)
        return d(2026, 10, 1).fromordinal(d(2026, 10, 1).toordinal() + self.day_counter)

    def invoice(self):
        lines, total = [], 0
        for _n in range(self.random.randint(1, 3)):
            account, tax_account, tax, rate = self.random.choice(LINE_KINDS)
            net = self.random.randint(1, 50000)
            vat = round(net * rate / 100)
            lines += [
                ln(account, -net / 100, tax=tax),
                ln(tax_account, -vat / 100, rep=tax),
            ]
            total += net + vat
        if self.random.random() < 0.2:
            discount = self.random.randint(1, max(1, total // 10))
            lines.append(ln("8736", discount / 100, tax="ust19"))
            total -= discount
        entry = self.book.move(
            "RE", self.day(), ln("1410", total / 100), *lines, move_type="out_invoice"
        )
        return entry, total

    def credit_note(self, invoice, open_amount):
        amount = self.random.randint(1, open_amount)
        net = round(amount * 100 / 107)
        entry = self.book.move(
            "GS",
            self.day(),
            ln("1410", -amount / 100),
            ln("8300", net / 100, tax="ust7"),
            ln("1771", (amount - net) / 100, rep="ust7"),
            move_type="out_refund",
        )
        self.book.rec(invoice("1410"), entry("1410"))
        return amount

    def payment(self, invoice, open_amount):
        amount = self.random.randint(1, open_amount)
        bank = self.book.move(
            "Bank", self.day(), ("1200", amount / 100), ("1410", -amount / 100)
        )
        self.book.rec(invoice("1410"), bank("1410"), amount / 100)
        return amount

    def writeoff(self, invoice, open_amount):
        entry = self.book.move(
            "Ausbuchung",
            self.day(),
            ("2400", open_amount / 100),
            ("1410", -open_amount / 100),
        )
        self.book.rec(invoice("1410"), entry("1410"))
        return open_amount

    def one_invoice(self):
        invoice, open_amount = self.invoice()
        for _step in range(self.random.randint(1, 4)):
            if open_amount <= 0:
                break
            action = self.random.random()
            if action < 0.55:
                open_amount -= self.payment(invoice, open_amount)
            elif action < 0.85:
                open_amount -= self.credit_note(invoice, open_amount)
            else:
                open_amount -= self.writeoff(invoice, open_amount)

    def collective(self):
        invoices = [self.invoice() for _n in range(self.random.randint(2, 3))]
        total = sum(amount for _entry, amount in invoices)
        payment = self.book.move(
            "Sammelzahlung",
            self.day(),
            ("OR", total / 100),
            *[("1410", -amount / 100) for _entry, amount in invoices],
        )
        for index, (entry, _amount) in enumerate(invoices):
            self.book.rec(entry("1410"), payment("1410", index))
        bank = self.book.move(
            "Bank", self.day(), ("1200", total / 100), ("OR", -total / 100)
        )
        self.book.rec(payment("OR"), bank("OR"))

    def reversal(self):
        invoice, total = self.invoice()
        reverse = self.book.move(
            "Storno",
            self.day(),
            *[
                ln(self._code(line), -line.balance / 100, **self._tax(line))
                for line in invoice.lines
            ],
            reversed_entry_id=invoice.move.id,
            move_type="out_refund",
        )
        del total
        self.book.rec(invoice("1410"), reverse("1410"))

    def _code(self, line):
        return next(
            code
            for code, account in self.book.accounts.items()
            if account.id == line.account_id
        )

    def _tax(self, line):
        names = {tax.id: name for name, tax in self.book.taxes.items()}
        values = {}
        if line.tax_ids:
            values["tax"] = names[line.tax_ids[0]]
        if line.repartition_id:
            repartition = self.book.repartitions[line.repartition_id - 1]
            values["rep"] = names[repartition.tax_id]
        return values


class TestOracle(BaseCase):
    def test_engine_equals_oracle(self):
        """500 random books: same amounts per year and account (seeded)."""
        generator = random.Random(181026)
        for case in range(500):
            scenario = Scenario(generator)
            for _n in range(generator.randint(1, 3)):
                choice = generator.random()
                if choice < 0.7:
                    scenario.one_invoice()
                elif choice < 0.9:
                    scenario.collective()
                else:
                    scenario.reversal()
            data = scenario.book.data()
            result = scenario.book.run(2026)
            with self.subTest(case=case):
                self.assertEqual(_engine_totals(result), Oracle(data).run())
                self.assertEqual(result.invariants["money"], {})
                self.assertEqual(result.invariants["documents"], {})

    def test_prototype_cases(self):
        """Cases computed by the prototype (P): T03 and T08 per account."""
        book = Book(regime="regular")
        invoice = book.move(
            "RE",
            d(2026, 12, 1),
            ln("1410", 226),
            ln("8400", -100, tax="ust19"),
            ln("1776", -19, rep="ust19"),
            ln("8300", -100, tax="ust7"),
            ln("1771", -7, rep="ust7"),
            move_type="out_invoice",
        )
        first = book.move("Bank", d(2026, 12, 20), ("1200", 50), ("1410", -50))
        credit = book.move(
            "GS",
            d(2027, 1, 10),
            ln("1410", -119),
            ln("8400", 100, tax="ust19"),
            ln("1776", 19, rep="ust19"),
        )
        second = book.move("Bank", d(2027, 1, 20), ("1200", 57), ("1410", -57))
        book.rec(invoice("1410"), first("1410"))
        book.rec(invoice("1410"), credit("1410"))
        book.rec(invoice("1410"), second("1410"))
        expected = Oracle(book.data()).run()
        self.assertEqual(_engine_totals(book.run(2026)), expected)
        names = {account.id: code for code, account in book.accounts.items()}
        self.assertEqual(
            {
                (year, names[account]): cents
                for (year, account), cents in expected.items()
            },
            {
                (2026, "8400"): -2213,
                (2026, "1776"): -420,
                (2026, "8300"): -2212,
                (2026, "1771"): -155,
                (2027, "8400"): 2213,
                (2027, "1776"): 420,
                (2027, "8300"): -7788,
                (2027, "1771"): -545,
            },
        )
