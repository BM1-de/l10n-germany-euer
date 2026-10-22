# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Engine scenarios T01–T31 (odoo_technik 7, spec 9.5) with synthetic data.

Pure engine tests: the entries are built as plain data (no database). The
expected values are those of the specification; "P" scenarios were also
computed by the prototype (oracle).
"""

import dataclasses
from decimal import Decimal as D

from ..models.engine import model as m
from ..models.engine import statement
from .engine_common import Book, EngineCase, by_account, codes, d, fields, ln

KU = "EUER.20.111"
TAXABLE = "EUER.20.112"
OUTPUT_VAT = "EUER.20.140"
INPUT_VAT = "EUER.25.185"
VAT_PAID = "EUER.25.186"
OTHER = "EUER.25.183"
WORK = "EUER.25.229"
WITHDRAWAL = "EUER.29.122"
CONTRIBUTION = "EUER.29.123"


class TestScenarios(EngineCase):
    def test_t01_invoice_paid(self):
        book = Book()
        invoice = self.sale(book, "RE", d(2027, 3, 15), 500)
        bank = self.receipt(book, "Bank", d(2027, 4, 2), 500, invoice)
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {KU: D("500.00")})
        realised = [s for s in result.slices if s.origin_move_id == invoice.move.id]
        self.assertEqual([s.date for s in realised], [d(2027, 4, 2)])
        self.assertEqual(realised[0].source_move_id, bank.move.id)

    def test_t02_payment_next_year(self):
        book = Book()
        invoice = self.sale(book, "RE", d(2026, 12, 15), 500)
        self.receipt(book, "Bank", d(2027, 1, 5), 500, invoice)
        self.assertEqual(fields(self.run_year(book, 2026)), {})
        self.assertEqual(fields(self.run_year(book, 2027)), {KU: D("500.00")})

    def test_t03_partial_payments_rounding(self):
        """Hamilton per payment: 500 of 1,404 (spec 5.10)."""
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
        self.receipt(book, "Bank1", d(2026, 12, 1), 500, invoice)
        self.receipt(book, "Bank2", d(2027, 1, 15), 904, invoice)
        result = self.run_year(book, 2026)
        self.assertEqual(
            by_account(result, book, 2026),
            {
                "1771": D("-4.99"),
                "1776": D("-67.66"),
                "8300": D("-71.22"),
                "8400": D("-356.13"),
            },
        )
        self.assertEqual(
            by_account(result, book, 2027),
            {
                "1771": D("-9.01"),
                "1776": D("-122.34"),
                "8300": D("-128.78"),
                "8400": D("-643.87"),
            },
        )
        self.assertEqual(fields(result), {TAXABLE: D("427.35"), OUTPUT_VAT: D("72.65")})

    def test_t04_payment_registered_bank_next_year(self):
        book = Book()
        invoice = self.sale(book, "RE", d(2026, 12, 28), 300)
        payment = book.move("Zahlung", d(2026, 12, 30), ("OR", 300), ("1410", -300))
        bank = book.move("Bank", d(2027, 1, 2), ("1200", 300), ("OR", -300))
        book.rec(invoice("1410"), payment("1410"))
        book.rec(payment("OR"), bank("OR"))
        self.assertEqual(fields(self.run_year(book, 2026)), {})
        self.assertEqual(fields(self.run_year(book, 2027)), {KU: D("300.00")})

    def _t04b(self, cash_date=None):
        book = Book()
        bill = book.move(
            "ER",
            d(2026, 12, 15),
            ("4930", 100),
            ("1610", -100),
            move_type="in_invoice",
        )
        payment = book.move("Zahlung", d(2026, 12, 30), ("1610", 100), ("OP", -100))
        info = {"cash_date": cash_date, "cash_date_reason": "order"}
        bank = book.move(
            "Bank",
            d(2027, 1, 2),
            ("OP", 100),
            ("1200", -100),
            info=info if cash_date else None,
        )
        book.rec(bill("1610"), payment("1610"))
        book.rec(payment("OP"), bank("OP"))
        return book

    def test_t04b_transfer_order(self):
        """Default: bank debit date plus review C-23; override: order date."""
        result = self.run_year(self._t04b(), 2027)
        self.assertEqual(fields(result), {WORK: D("100.00")})
        self.assertIn("C-23", codes(result))
        self.assertIn("C-23", codes(self.run_year(self._t04b(), 2026)))
        result = self.run_year(self._t04b(d(2026, 12, 30)), 2026)
        self.assertEqual(fields(result), {WORK: D("100.00")})
        self.assertNotIn("C-23", codes(result))
        self.assertTrue(all(s.override for s in result.slices))

    def _collective(self, bank_amount):
        book = Book()
        invoices = [
            self.sale(book, f"RE-{n}", d(2027, 5, 1), amount)
            for n, amount in (("A", 100), ("B", 200), ("C", 300))
        ]
        payment = book.move(
            "Sammelzahlung",
            d(2027, 5, 30),
            ("OR", 600),
            ("1410", -100),
            ("1410", -200),
            ("1410", -300),
        )
        for index, invoice in enumerate(invoices):
            book.rec(invoice("1410"), payment("1410", index))
        bank = book.move(
            "Bank", d(2027, 6, 1), ("1200", bank_amount), ("OR", -bank_amount)
        )
        book.rec(payment("OR"), bank("OR"), bank_amount)
        return book, invoices

    def test_t05_collective_payment(self):
        book, _invoices = self._collective(600)
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {KU: D("600.00")})
        self.assertNotIn("C-27", codes(result))

    def test_t05b_collective_payment_partly_matched(self):
        book, invoices = self._collective(300)
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {KU: D("300.00")})
        per_invoice = {
            invoice.move.name: sum(
                s.amount for s in result.slices if s.origin_move_id == invoice.move.id
            )
            for invoice in invoices
        }
        self.assertEqual(per_invoice, {"RE-A": -5000, "RE-B": -10000, "RE-C": -15000})
        self.assertIn("C-27", codes(result))

    def test_t06_full_reversal(self):
        for reversal_day in (d(2026, 11, 20), d(2027, 2, 1)):
            book = Book(regime="regular")
            invoice = self.regular_sale(book, d(2026, 11, 5), 1000)
            credit = book.move(
                "GS",
                reversal_day,
                ln("1410", -1190),
                ln("8400", 1000, tax="ust19"),
                ln("1776", 190, rep="ust19"),
                move_type="out_refund",
                reversed_entry_id=invoice.move.id,
            )
            book.rec(invoice("1410"), credit("1410"))
            for year in (2026, 2027):
                result = self.run_year(book, year)
                self.assertEqual(fields(result), {})
                self.assertEqual(result.slices, [])

    def _t07_invoice(self, book, day):
        return book.move(
            "RE",
            day,
            ln("1410", 226),
            ln("8400", -100, tax="ust19"),
            ln("1776", -19, rep="ust19"),
            ln("8300", -100, tax="ust7"),
            ln("1771", -7, rep="ust7"),
            move_type="out_invoice",
        )

    def _t07_credit(self, book, day):
        return book.move(
            "GS",
            day,
            ln("1410", -119),
            ln("8400", 100, tax="ust19"),
            ln("1776", 19, rep="ust19"),
            move_type="out_refund",
        )

    def test_t07_partial_credit_note(self):
        """The credit note changes the open composition (E2)."""
        book = Book(regime="regular")
        invoice = self._t07_invoice(book, d(2026, 11, 10))
        credit = self._t07_credit(book, d(2026, 11, 20))
        book.rec(invoice("1410"), credit("1410"))
        self.receipt(book, "Bank", d(2027, 1, 15), 107, invoice)
        result = self.run_year(book, 2027)
        self.assertEqual(
            by_account(result, book, 2027), {"1771": D("-7.00"), "8300": D("-100.00")}
        )
        self.assertEqual(fields(result), {TAXABLE: D("100.00"), OUTPUT_VAT: D("7.00")})
        self.assertEqual(fields(result, 2026), {})

    def test_t08_payment_before_credit_note(self):
        """Payments before the credit note stay unchanged (2026 frozen)."""
        book = Book(regime="regular")
        invoice = self._t07_invoice(book, d(2026, 12, 1))
        self.receipt(book, "Bank1", d(2026, 12, 20), 50, invoice)
        credit = self._t07_credit(book, d(2027, 1, 10))
        book.rec(invoice("1410"), credit("1410"))
        self.receipt(book, "Bank2", d(2027, 1, 20), 57, invoice)
        result = self.run_year(book, 2026)
        self.assertEqual(
            by_account(result, book, 2026),
            {
                "1771": D("-1.55"),
                "1776": D("-4.20"),
                "8300": D("-22.12"),
                "8400": D("-22.13"),
            },
        )
        self.assertEqual(
            by_account(result, book, 2027),
            {
                "1771": D("-5.45"),
                "1776": D("4.20"),
                "8300": D("-77.88"),
                "8400": D("22.13"),
            },
        )

    def _skonto_invoice(self, book):
        return book.move(
            "RE",
            d(2027, 3, 1),
            ln("1410", 1190),
            ln("8400", -1000, tax="ust19"),
            ln("1776", -190, rep="ust19"),
            move_type="out_invoice",
        )

    def test_t09_cash_discount_in_bank_entry(self):
        book = Book(regime="regular")
        invoice = self._skonto_invoice(book)
        bank = book.move(
            "Bank",
            d(2027, 3, 8),
            ln("1200", 1166.20),
            ln("8736", 20, tax="ust19"),
            ln("1776", 3.80, rep="ust19"),
            ln("1410", -1190),
        )
        book.rec(invoice("1410"), bank("1410"))
        result = self.run_year(book, 2027)
        self.assertEqual(
            by_account(result, book, 2027),
            {"1776": D("-186.20"), "8400": D("-1000.00"), "8736": D("20.00")},
        )
        self.assertEqual(
            fields(result), {TAXABLE: D("980.00"), OUTPUT_VAT: D("186.20")}
        )

    def test_t09b_cash_discount_in_payment(self):
        book = Book(regime="regular")
        invoice = self._skonto_invoice(book)
        payment = book.move(
            "Zahlung",
            d(2027, 3, 8),
            ln("OR", 1166.20),
            ln("8736", 20, tax="ust19"),
            ln("1776", 3.80, rep="ust19"),
            ln("1410", -1190),
        )
        bank = book.move("Bank", d(2027, 3, 10), ("1200", 1166.20), ("OR", -1166.20))
        book.rec(invoice("1410"), payment("1410"))
        book.rec(payment("OR"), bank("OR"))
        result = self.run_year(book, 2027)
        self.assertEqual(
            fields(result), {TAXABLE: D("980.00"), OUTPUT_VAT: D("186.20")}
        )
        self.assertEqual({s.date for s in result.slices}, {d(2027, 3, 10)})

    def test_t10_bad_debt(self):
        book = Book()
        invoice = self.sale(book, "RE", d(2026, 10, 1), 400)
        writeoff = book.move(
            "Ausbuchung", d(2027, 3, 31), ("2400", 400), ("1410", -400)
        )
        book.rec(invoice("1410"), writeoff("1410"))
        for year in (2026, 2027):
            self.assertEqual(fields(self.run_year(book, year)), {})
        self.assertIn("C-25", codes(self.run_year(book, 2027)))

    def test_t10b_partial_payment_and_bad_debt(self):
        book = Book()
        invoice = self.sale(book, "RE", d(2026, 10, 1), 400)
        self.receipt(book, "Bank", d(2026, 11, 1), 150, invoice)
        writeoff = book.move(
            "Ausbuchung", d(2027, 3, 31), ("2400", 250), ("1410", -250)
        )
        book.rec(invoice("1410"), writeoff("1410"))
        self.assertEqual(fields(self.run_year(book, 2026)), {KU: D("150.00")})
        self.assertEqual(fields(self.run_year(book, 2027)), {})

    def _t11(self, exchange_day, **options):
        book = Book(**options)
        invoice = book.move(
            "RE USD",
            d(2027, 3, 1),
            ln("1410", 909.09, currency_id=2, residual_currency_zero=True),
            ln("8195", -909.09),
            move_type="out_invoice",
        )
        bank = book.move("Bank", d(2027, 4, 1), ("1200", 900), ("1410", -900))
        exchange = book.move(
            "Kursdifferenz", exchange_day, ("2150", 9.09), ("1410", -9.09)
        )
        book.rec(invoice("1410"), bank("1410"), 900, exchange=exchange)
        book.rec(invoice("1410"), exchange("1410"))
        return book

    def test_t11_exchange_difference(self):
        """Bound to the payment, also when dated later (B-07, ST-T11c)."""
        for exchange_day in (d(2027, 4, 1), d(2027, 4, 15)):
            book = self._t11(exchange_day)
            result = self.run_year(book, 2027)
            self.assertEqual(
                by_account(result, book, 2027),
                {"2150": D("9.09"), "8195": D("-909.09")},
            )
            self.assertEqual(fields(result), {KU: D("909.09"), OTHER: D("9.09")})
            self.assertNotIn("C-19", codes(result))
        result = self.run_year(self._t11(d(2027, 4, 1), fx_mode="allocate"), 2027)
        self.assertEqual(fields(result), {KU: D("900.00")})

    def test_t11b_exchange_difference_in_bank_entry(self):
        book = Book()
        invoice = book.move(
            "RE USD", d(2027, 3, 1), ("1410", 909.09), ("8195", -909.09)
        )
        bank = book.move(
            "Bank", d(2027, 4, 1), ("1200", 900), ("2150", 9.09), ("1410", -909.09)
        )
        book.rec(invoice("1410"), bank("1410"))
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {KU: D("909.09"), OTHER: D("9.09")})

    def test_t12_bank_fee_and_withdrawal(self):
        book = Book()
        book.move("Bank", d(2027, 6, 30), ("4970", 9.90), ("1200", -9.90))
        book.move("Bank", d(2027, 7, 1), ("1800", 500), ("1200", -500))
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {OTHER: D("9.90"), WITHDRAWAL: D("500.00")})

    def test_t13_fixed_asset_purchase(self):
        """Acquisition is neutral; depreciation only from the register."""
        book = Book()
        bill = book.move(
            "ER",
            d(2027, 7, 15),
            ("0490", 1640),
            ("1610", -1640),
            move_type="in_invoice",
        )
        bank = book.move("Bank", d(2027, 7, 20), ("1610", 1640), ("1200", -1640))
        book.rec(bill("1610"), bank("1610"))
        depreciation = book.move(
            "AfA", d(2027, 12, 31), ("4830", 117.14), ("0490", -117.14)
        )
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {})
        self.assertEqual(result.asset_entries, [depreciation.move.id])
        self.assertTrue(result.asset_slices)
        # the register delivers the AVEÜR values; Kz 130 is their transfer
        book.asset_amounts.append(
            m.FieldAmount(
                year=2027, field="AVEUER.40.424", amount=11714, source="asset"
            )
        )
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {"AVEUER.40.424": D("117.14")})
        values, _findings = statement.form_values(result.totals, book.specs[2026], 2027)
        self.assertEqual(values["EUER.25.130"], D("117.14"))

    def test_t14_low_value_asset(self):
        """GWG on 4855: not from the payment, value from the register."""
        book = Book()
        bill = book.move(
            "ER", d(2026, 12, 20), ("4855", 600), ("1610", -600), move_type="in_invoice"
        )
        bank = book.move("Bank", d(2027, 1, 5), ("1610", 600), ("1200", -600))
        book.rec(bill("1610"), bank("1610"))
        book.asset_amounts.append(
            m.FieldAmount(year=2026, field="EUER.25.132", amount=60000, source="asset")
        )
        self.assertEqual(
            fields(self.run_year(book, 2026)), {"EUER.25.132": D("600.00")}
        )
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {})
        self.assertTrue(result.asset_slices)

    def test_t15_expense_paid_privately(self):
        book = Book()
        bill = book.move(
            "ER", d(2027, 3, 1), ("4930", 50), ("1610", -50), move_type="in_invoice"
        )
        private = book.move("Privat", d(2027, 3, 1), ("1610", 50), ("1890", -50))
        book.rec(bill("1610"), private("1610"))
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {WORK: D("50.00"), CONTRIBUTION: D("50.00")})

    def test_t16_set_off(self):
        book = Book()
        partner = {"partner": "X"}
        invoice = self.sale(book, "RE an X", d(2027, 5, 1), 500, **partner)
        bill = book.move(
            "ER von X",
            d(2027, 5, 3),
            ("4930", 200),
            ("1610", -200),
            move_type="in_invoice",
            **partner,
        )
        offset = book.move(
            "Verrechnung", d(2027, 5, 10), ("1610", 200), ("1410", -200), **partner
        )
        book.rec(invoice("1410"), offset("1410"))
        book.rec(bill("1610"), offset("1610"))
        self.receipt(book, "Bank", d(2027, 5, 20), 300, invoice)
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {KU: D("500.00"), WORK: D("200.00")})
        on_offset = {
            s.amount for s in result.slices if s.source_move_id == offset.move.id
        }
        self.assertEqual(on_offset, {-20000, 20000})
        self.assertEqual(result.classes[offset.move.id], m.SOURCE)

    def test_t17_unclear_statement_line(self):
        book = Book()
        book.move("Bank", d(2027, 12, 31), ("1200", 250), ("SUSP", -250))
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {})
        self.assertIn("C-10", codes(result, m.BLOCKER))

    def test_t18_advance_payment(self):
        book = Book()
        bank = book.move(
            "Bank", d(2026, 12, 20), ("1200", 300), ("1410", -300), partner="K"
        )
        result = self.run_year(book, 2026)
        self.assertEqual(fields(result), {})
        self.assertIn("C-20", codes(result, m.BLOCKER))
        book.classify(bank("1410"), "revenue", "REV_KU", regime="ku")
        result = self.run_year(book, 2026)
        self.assertEqual(fields(result), {KU: D("300.00")})
        self.assertNotIn("C-20", codes(result))
        invoice = self.sale(book, "RE", d(2027, 1, 10), 300, partner="K")
        book.rec(invoice("1410"), bank("1410"))
        result = self.run_year(book, 2026)
        self.assertEqual(fields(result), {KU: D("300.00")})
        self.assertEqual({s.kind for s in result.slices}, {m.DOC_VIA_PAYMENT})
        self.assertEqual(fields(self.run_year(book, 2027)), {})

    def test_t19_reversal_of_bank_entry(self):
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
            self.assertEqual(result.slices, [])
            self.assertEqual(fields(result), {})

    def test_t20_drafts(self):
        book = Book()
        draft = self.sale(book, "RE Entwurf", d(2027, 4, 1), 500, state="draft")
        book.move(
            "Zahlung Entwurf",
            d(2027, 4, 2),
            ("OR", 500),
            ("1410", -500),
            state="draft",
        )
        bank = book.move("Bank", d(2027, 4, 3), ("1200", 500), ("1410", -500))
        book.rec(draft("1410"), bank("1410"))
        cancelled = book.move(
            "ER storniert",
            d(2027, 4, 4),
            ("4930", 80),
            ("1610", -80),
            state="cancel",
        )
        del cancelled
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {})
        self.assertIn("C-13", codes(result))
        self.assertIn("C-14", codes(result))
        self.assertIn("C-20", codes(result, m.BLOCKER))

    def test_t21_other_business(self):
        """Entries of other companies are not part of the run."""
        book = Book()
        invoice = self.sale(book, "RE A", d(2027, 2, 1), 100, company_id=1)
        self.receipt(book, "Bank A", d(2027, 2, 5), 100, invoice, company_id=1)
        other = self.sale(book, "RE B", d(2027, 2, 1), 700, company_id=2)
        self.receipt(book, "Bank B", d(2027, 2, 5), 700, other, company_id=2)
        data = dataclasses.replace(book.data(), company_ids=frozenset({1}))
        from ..models.engine import engine

        result = engine.run(data, 2027)
        self.assertEqual(fields(result), {KU: D("100.00")})

    def _opening(self, with_items=True):
        book = Book()
        opening = book.move(
            "Eröffnung",
            d(2027, 1, 1),
            ln("1410", 260, partner="Kunde A"),
            ln("1410", 45, partner="Kunde B"),
            ln("1200", 5000),
            ln("9008", -305),
            ln("9000", -5000),
            opening=True,
        )
        if with_items:
            for index in (0, 1):
                book.opening(opening("1410", index), "REV_KU")
        bank = book.move(
            "Bank", d(2027, 1, 15), ("1200", 260), ("1410", -260), partner="Kunde A"
        )
        book.rec(opening("1410", 0), bank("1410"))
        return book

    def test_t22_opening_items(self):
        result = self.run_year(self._opening(), 2027)
        self.assertEqual(fields(result), {KU: D("260.00")})
        self.assertNotIn("C-35", codes(result))
        result = self.run_year(self._opening(with_items=False), 2027)
        self.assertEqual(fields(result), {})
        self.assertIn("C-35", codes(result, m.BLOCKER))

    def _t23(self, extension):
        book = Book(regime="regular")
        book.set_regimes(
            m.VatPeriod(
                date_from=d(2020, 1, 1),
                regime="regular",
                vat_return_period="monthly",
                permanent_extension=extension,
            )
        )
        book.move(
            "Bank",
            d(2027, 1, 8),
            ("1780", 300),
            ("1200", -300),
            partner="Finanzamt",
            info={"vat_period": "2026-12"},
        )
        return book

    def test_t23_vat_payment_ten_day_rule(self):
        result = self.run_year(self._t23(False), 2026)
        self.assertEqual(fields(result), {VAT_PAID: D("300.00")})
        self.assertEqual([i.decision for i in result.ten_day_items], ["auto"])
        self.assertTrue(result.ten_day_items[0].applied)
        self.assertNotIn("C-26", codes(result))
        result = self.run_year(self._t23(True), 2027)
        self.assertEqual(fields(result), {VAT_PAID: D("300.00")})
        self.assertEqual(fields(result, 2026), {})

    def test_t24_transitory_item(self):
        book = Book().previous_turnover(2027)
        book.move("Bank", d(2027, 3, 1), ("1200", 100), ("1590", -100))
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {})
        self.assertEqual(codes(result), [])

    def test_t25_money_in_transit(self):
        book = Book()
        out = book.move("Bank", d(2026, 12, 31), ("1360", 200), ("1200", -200))
        cash = book.move("Kasse", d(2027, 1, 2), ("1000", 200), ("1360", -200))
        book.rec(out("1360"), cash("1360"))
        for year in (2026, 2027):
            result = self.run_year(book, year)
            self.assertEqual(fields(result), {})
            self.assertEqual(result.slices, [])
        notes = {(n.date.year, n.amount) for n in result.notes}
        self.assertEqual(notes, {(2026, 20000), (2027, -20000)})

    def test_t26_credit_note_refunded(self):
        book = Book()
        credit = book.move(
            "GS", d(2027, 8, 1), ("8195", 50), ("1410", -50), move_type="out_refund"
        )
        bank = book.move("Bank", d(2027, 8, 5), ("1410", 50), ("1200", -50))
        book.rec(credit("1410"), bank("1410"))
        result = self.run_year(book, 2027)
        # R-12: without other revenue the negative amount moves to Kz 183
        self.assertEqual(fields(result), {OTHER: D("50.00")})
        self.assertEqual(result.overflow, {KU: 5000})
        invoice = self.sale(book, "RE", d(2027, 2, 1), 500)
        self.receipt(book, "Bank RE", d(2027, 2, 10), 500, invoice)
        self.assertEqual(fields(self.run_year(book, 2027)), {KU: D("450.00")})

    def test_t27_invoice_and_credit_in_one_payment(self):
        book = Book(regime="regular")
        invoice = book.move(
            "RE",
            d(2027, 6, 1),
            ln("1410", 119),
            ln("8400", -100, tax="ust19"),
            ln("1776", -19, rep="ust19"),
            move_type="out_invoice",
        )
        credit = book.move(
            "GS",
            d(2027, 6, 5),
            ln("1410", -20),
            ln("8300", 18.69, tax="ust7"),
            ln("1771", 1.31, rep="ust7"),
            move_type="out_refund",
        )
        bank = book.move(
            "Bank", d(2027, 6, 10), ("1200", 99), ("1410", 20), ("1410", -119)
        )
        book.rec(invoice("1410"), bank("1410", 1))
        book.rec(credit("1410"), bank("1410", 0))
        result = self.run_year(book, 2027)
        self.assertEqual(
            by_account(result, book, 2027),
            {
                "1771": D("1.31"),
                "1776": D("-19.00"),
                "8300": D("18.69"),
                "8400": D("-100.00"),
            },
        )

    def test_t28_change_after_finalisation(self):
        """A later reconciliation changes a closed year: the diff shows it."""
        book = Book()
        book.set_regimes(
            m.VatPeriod(date_from=d(2020, 1, 1), date_to=d(2026, 12, 31), regime="ku"),
            m.VatPeriod(date_from=d(2027, 1, 1), regime="regular"),
        )
        bank = book.move(
            "Bank", d(2026, 12, 20), ("1200", 300), ("1410", -300), partner="K"
        )
        book.classify(bank("1410"), "revenue", "REV_KU", regime="ku")
        before = self.run_year(book, 2026).totals
        invoice = self.regular_sale(book, d(2027, 1, 10), 252.10, partner="K")
        book.rec(invoice("1410"), bank("1410"))
        after = self.run_year(book, 2026).totals
        self.assertEqual(
            statement.compare(before, after),
            {KU: (30000, 0), TAXABLE: (0, 25210), OUTPUT_VAT: (0, 4790)},
        )

    def test_t29_keep_mode(self):
        book = Book()
        invoice = self.sale(book, "RE", d(2027, 3, 1), 250)
        bank = book.move("Bank", d(2027, 3, 10), ("1200", 250), ("SUSPR", -250))
        keep = book.move(
            "Keep-Ausgleich", d(2027, 4, 2), ("SUSPR", 250), ("1410", -250)
        )
        book.rec(bank("SUSPR"), keep("SUSPR"))
        book.rec(invoice("1410"), keep("1410"))
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {KU: D("250.00")})
        self.assertEqual({s.date for s in result.slices}, {d(2027, 3, 10)})
        self.assertNotIn("C-10", codes(result))

    def test_t30_cash_date_override(self):
        book = Book()
        bill = book.move(
            "ER", d(2026, 12, 10), ("4930", 80), ("1610", -80), move_type="in_invoice"
        )
        bank = book.move(
            "Bank",
            d(2027, 1, 2),
            ("1610", 80),
            ("1200", -80),
            info={"cash_date": d(2026, 12, 30), "cash_date_reason": "order"},
        )
        book.rec(bill("1610"), bank("1610"))
        self.assertEqual(fields(self.run_year(book, 2026)), {WORK: D("80.00")})
        self.assertEqual(fields(self.run_year(book, 2027)), {})

    def test_t31_small_business_invoice_paid_after_change(self):
        """KU invoice 2026, standard taxation from 2027: Kz 111 (U-10)."""
        book = Book()
        book.set_regimes(
            m.VatPeriod(date_from=d(2020, 1, 1), date_to=d(2026, 12, 31), regime="ku"),
            m.VatPeriod(date_from=d(2027, 1, 1), regime="regular"),
        )
        invoice = self.sale(book, "RE", d(2026, 12, 15), 500, info={"vat_regime": "ku"})
        self.receipt(book, "Bank", d(2027, 2, 10), 500, invoice)
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {KU: D("500.00")})
        self.assertNotIn("C-33", codes(result))
