# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Further engine scenarios of spec 9.5 (G, K, U, N, E, B, D, R, F, T22b/c).

Pure engine tests with synthetic data; the Odoo scenario tests with real
entries follow with the extraction (stage E2, part 2).
"""

import dataclasses
from decimal import Decimal as D
from fractions import Fraction

from ..models.engine import model as m
from ..models.engine import statement
from .engine_common import Book, EngineCase, codes, d, fields, ln

KU = "EUER.20.111"
KU_DAVON = "EUER.20.119"
TAXABLE = "EUER.20.112"
EXEMPT = "EUER.20.103"
ASSET_SALE = "EUER.20.102"
PRIVATE_CAR = "EUER.20.106"
OUTPUT_VAT = "EUER.20.140"
REFUND = "EUER.20.141"
INPUT_VAT = "EUER.25.185"
VAT_PAID = "EUER.25.186"
OTHER = "EUER.25.183"
WORK = "EUER.25.229"
PERSONNEL = "EUER.25.120"
GIFT = "EUER.25.174"
GIFT_NOT_DEDUCTIBLE = "EUER.25.164"
ENTERTAINMENT = "EUER.25.175"
ENTERTAINMENT_NOT_DEDUCTIBLE = "EUER.25.165"
WITHDRAWAL = "EUER.29.122"
CONTRIBUTION = "EUER.29.123"


def periods(*changes):
    """Status periods: ``(start, regime)`` pairs, each until the next one."""
    result = []
    for index, (start, regime) in enumerate(changes):
        end = None
        if index + 1 < len(changes):
            end = changes[index + 1][0].fromordinal(
                changes[index + 1][0].toordinal() - 1
            )
        result.append(m.VatPeriod(date_from=start, date_to=end, regime=regime))
    return result


class TestCreditNotes(EngineCase):
    def test_g1_full_credit_after_payment(self):
        """B-03: credit note after payment is no excluded pair (G1, R-12)."""
        book = Book(regime="regular")
        invoice = self.regular_sale(book, d(2026, 5, 1), 1000)
        self.receipt(book, "Bank", d(2026, 5, 10), 1190, invoice)
        credit = book.move(
            "GS",
            d(2027, 2, 1),
            ln("1410", -1190),
            ln("8400", 1000, tax="ust19"),
            ln("1776", 190, rep="ust19"),
            move_type="out_refund",
            reversed_entry_id=invoice.move.id,
        )
        refund = book.move(
            "Rückzahlung", d(2027, 2, 5), ("1410", 1190), ("1200", -1190)
        )
        book.rec(credit("1410"), refund("1410"))
        self.assertEqual(
            fields(self.run_year(book, 2026)),
            {TAXABLE: D("1000.00"), OUTPUT_VAT: D("190.00")},
        )
        result = self.run_year(book, 2027)
        self.assertEqual(
            result.totals_for(2027, overflow=False),
            {TAXABLE: -100000, OUTPUT_VAT: -19000},
        )
        self.assertEqual(fields(result), {OTHER: D("1190.00")})

    def test_g2_document_pair_without_money(self):
        book = Book(regime="regular")
        invoice = self.regular_sale(book, d(2027, 5, 1), 100)
        credit = book.move(
            "GS",
            d(2027, 5, 2),
            ln("1410", -119),
            ln("8400", 100, tax="ust19"),
            ln("1776", 19, rep="ust19"),
            move_type="out_refund",
        )
        book.rec(invoice("1410"), credit("1410"))
        result = self.run_year(book, 2027)
        self.assertEqual(result.slices, [])
        self.assertNotIn("C-25", codes(result))


class TestSmallBusiness(EngineCase):
    def test_k02_change_to_small_business(self):
        book = Book()
        book.set_regimes(*periods((d(2020, 1, 1), "regular"), (d(2027, 1, 1), "ku")))
        invoice = self.regular_sale(book, d(2026, 12, 15), 100)
        self.receipt(book, "Bank", d(2027, 1, 20), 119, invoice)
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {TAXABLE: D("100.00"), OUTPUT_VAT: D("19.00")})

    def test_k04_supply_date_decides(self):
        book = Book()
        book.set_regimes(*periods((d(2020, 1, 1), "ku"), (d(2027, 1, 1), "regular")))
        invoice = self.sale(
            book,
            "RE",
            d(2027, 1, 5),
            400,
            invoice_date=d(2027, 1, 5),
            supply_date=d(2026, 12, 20),
        )
        self.receipt(book, "Bank", d(2027, 1, 25), 400, invoice)
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {KU: D("400.00")})
        self.assertNotIn("C-33", codes(result))

    def test_k04b_standard_taxation_on_small_business_account(self):
        """C-33: standard taxation books on 8195; rate input splits it."""
        book = Book(regime="regular")
        invoice = self.sale(book, "RE", d(2027, 3, 1), 238)
        self.receipt(book, "Bank", d(2027, 3, 5), 238, invoice)
        result = self.run_year(book, 2027)
        self.assertIn("C-33", codes(result, m.BLOCKER))
        book.moves[0] = dataclasses.replace(
            book.moves[0], info=m.MoveInfo(regular_tax_rate=Fraction(19))
        )
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {TAXABLE: D("200.00"), OUTPUT_VAT: D("38.00")})
        self.assertNotIn("C-33", codes(result))

    def test_k06_to_k08_of_which_and_asset_sale(self):
        book = Book()
        book.move("Zinsen", d(2027, 3, 31), ("1200", 12.34), ("2650", -12.34))
        book.move("EU-Kunde", d(2027, 4, 2), ("1200", 800), ("8338", -800))
        book.move("Anlagenverkauf", d(2027, 5, 2), ("1200", 300), ("8800", -300))
        result = self.run_year(book, 2027)
        self.assertEqual(
            fields(result),
            {KU: D("812.34"), KU_DAVON: D("812.34"), ASSET_SALE: D("300.00")},
        )

    def test_k09_unjustified_vat(self):
        """§ 14c: VAT on a small business invoice stays gross in 111; 186."""
        book = Book()
        invoice = book.move(
            "RE",
            d(2027, 3, 1),
            ln("1410", 1190),
            ln("8195", -1000),
            ln("1776", -190, rep="ust19"),
            move_type="out_invoice",
        )
        self.receipt(book, "Bank", d(2027, 3, 10), 1190, invoice)
        book.move(
            "Abführung", d(2027, 4, 10), ("1780", 190), ("1200", -190), partner="FA"
        )
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {KU: D("1190.00"), VAT_PAID: D("190.00")})


class TestVat(EngineCase):
    def test_u01_to_u04_payments_and_refunds_not_netted(self):
        book = Book(regime="regular")
        bill = book.move(
            "ER",
            d(2027, 2, 1),
            ln("4930", 200, tax="vst19"),
            ln("1576", 38, rep="vst19"),
            ln("1610", -238),
            move_type="in_invoice",
        )
        bank = book.move("Bank", d(2027, 2, 10), ("1610", 238), ("1200", -238))
        book.rec(bill("1610"), bank("1610"))
        book.move("USt-VZ", d(2027, 3, 10), ("1780", 300), ("1200", -300), partner="FA")
        book.move(
            "Erstattung", d(2027, 4, 10), ("1200", 80), ("1780", -80), partner="FA"
        )
        result = self.run_year(book, 2027)
        self.assertEqual(
            fields(result),
            {
                WORK: D("200.00"),
                INPUT_VAT: D("38.00"),
                VAT_PAID: D("300.00"),
                REFUND: D("80.00"),
            },
        )

    def test_u06_private_car_use(self):
        book = Book(regime="regular")
        book.move(
            "Kfz privat",
            d(2027, 12, 31),
            ln("1800", 119),
            ln("8921", -100, tax="ust19"),
            ln("1776", -19, rep="ust19"),
        )
        result = self.run_year(book, 2027)
        self.assertEqual(
            fields(result),
            {PRIVATE_CAR: D("100.00"), OUTPUT_VAT: D("19.00"), WITHDRAWAL: D("119.00")},
        )
        book.adjustment_amounts.append(
            m.FieldAmount(year=2027, field=PRIVATE_CAR, amount=10000)
        )
        self.assertIn("C-54", codes(self.run_year(book, 2027), m.BLOCKER))

    def test_u08_non_deductible_input_vat_account(self):
        book = Book(regime="regular")
        book.move(
            "Bank",
            d(2027, 3, 1),
            ln("4930", 100, tax="vst19"),
            ln("1566", 19, rep="vst19"),
            ln("1200", -119),
        )
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {WORK: D("119.00")})

    def test_u09_interest_surcharges_refunds(self):
        book = Book(regime="regular")
        book.move("Zinsen", d(2027, 3, 1), ("2100", 12), ("1200", -12))
        book.move("Säumnis", d(2027, 3, 2), ("4396", 5), ("1200", -5))
        book.move("Erstattung", d(2027, 3, 3), ("1200", 7), ("2750", -7))
        result = self.run_year(book, 2027)
        self.assertEqual(
            fields(result),
            {"EUER.25.234": D("12.00"), OTHER: D("5.00"), EXEMPT: D("7.00")},
        )
        book = Book()
        book.move("Erstattung", d(2027, 3, 3), ("1200", 7), ("2750", -7))
        self.assertEqual(
            fields(self.run_year(book, 2027)), {KU: D("7.00"), KU_DAVON: D("7.00")}
        )

    def test_u11_manual_tax_line(self):
        """Same values without repartition line, plus C-34."""
        book = Book(regime="regular")
        book.move(
            "Bank",
            d(2027, 3, 1),
            ln("4930", 100),
            ln("1576", 19),
            ln("1200", -119),
        )
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {WORK: D("100.00"), INPUT_VAT: D("19.00")})
        self.assertIn("C-34", codes(result, m.WARNING))

    def test_u13_manual_vat_settlement(self):
        book = Book(regime="regular")
        settlement = book.move(
            "USt-Verrechnung",
            d(2027, 1, 31),
            ("1776", 500),
            ("1576", -200),
            ("1780", -300),
        )
        bank = book.move(
            "Zahlung", d(2027, 2, 10), ("1780", 300), ("1200", -300), partner="FA"
        )
        book.rec(settlement("1780"), bank("1780"))
        book.move(
            "Erstattung", d(2027, 3, 10), ("1200", 80), ("1545", -80), partner="FA"
        )
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {VAT_PAID: D("300.00"), REFUND: D("80.00")})
        self.assertNotIn("C-20", codes(result))
        self.assertNotIn("C-34", codes(result))


class TestImportVat(EngineCase):
    def _import(self, regime):
        book = Book(regime=regime)
        bill = book.move(
            "ER Einfuhr",
            d(2027, 3, 1),
            ln("3200", 500, tax="eust"),
            ln("1588", 95, rep="eust"),
            ln("1588", -95, rep="eust-"),
            ln("1610", -500),
            move_type="in_invoice",
        )
        bank = book.move("Bank", d(2027, 3, 10), ("1610", 500), ("1200", -500))
        book.rec(bill("1610"), bank("1610"))
        book.move("Zoll", d(2027, 3, 20), ("1588", 95), ("1200", -95), partner="Zoll")
        return self.run_year(book, 2027)

    def test_import_vat_paid_to_customs(self):
        """EUSt: neutral in the document; payment 185, small business 183."""
        self.assertEqual(
            fields(self._import("regular")),
            {"EUER.25.100": D("500.00"), INPUT_VAT: D("95.00")},
        )
        self.assertEqual(
            fields(self._import("ku")),
            {"EUER.25.100": D("500.00"), OTHER: D("95.00")},
        )


class TestGifts(EngineCase):
    def gift(self, book, day, net, partner="Empfänger", vat=None, **values):
        """Gift paid by bank; the recipient is entered on the entry (the
        partner of a bank entry is the shop, never the recipient)."""
        lines = [ln("4630", net, tax="vst19" if vat else ())]
        if vat:
            lines.append(ln("1576", vat, rep="vst19"))
        total = net + (vat or 0)
        lines.append(ln("1200", -total))
        if partner:
            values["info"] = {"gift_recipient_id": book.partner(partner)}
        return book.move("Geschenk", day, *lines, partner="Laden", **values)

    def test_n01_two_gifts_same_recipient(self):
        book = Book()
        self.gift(book, d(2027, 3, 1), 30)
        self.gift(book, d(2027, 9, 1), 30)
        self.assertEqual(
            fields(self.run_year(book, 2027)), {GIFT_NOT_DEDUCTIBLE: D("60.00")}
        )

    def test_n02_gift_below_limit(self):
        book = Book()
        self.gift(book, d(2027, 3, 1), 45)
        self.gift(book, d(2027, 3, 1), 30, partner="Andere")
        self.assertEqual(fields(self.run_year(book, 2027)), {GIFT: D("75.00")})

    def test_n03_standard_taxation_net_limit(self):
        book = Book(regime="regular")
        self.gift(book, d(2027, 3, 1), 50, vat=9.50)
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {GIFT: D("50.00"), INPUT_VAT: D("9.50")})
        book = Book(regime="regular")
        self.gift(book, d(2027, 3, 1), 55, vat=10.45)
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {GIFT_NOT_DEDUCTIBLE: D("65.45")})
        self.assertIn("C-52", codes(result, m.WARNING))

    def test_n03b_small_business_gross_limit(self):
        """KU: the limit is checked gross (R-41)."""
        book = Book()
        book.move(
            "Geschenk",
            d(2027, 3, 1),
            ln("4630", 45, tax="vst19"),
            ln("1576", 8.55, rep="vst19"),
            ln("1200", -53.55),
            partner="Laden",
            info={"gift_recipient_id": book.partner("Empfänger")},
        )
        self.assertEqual(
            fields(self.run_year(book, 2027)), {GIFT_NOT_DEDUCTIBLE: D("53.55")}
        )

    def test_n06_bulk_and_year_of_the_gift(self):
        book = Book()
        book.move(
            "Kugelschreiber",
            d(2027, 2, 1),
            ln("4630", 600, unit_price=2),
            ln("1200", -600),
            info={"gift_bulk": True},
        )
        bill = book.move(
            "Geschenk 2026",
            d(2026, 12, 20),
            ln("4630", 40, supply_date=d(2026, 12, 20)),
            ln("1610", -40),
            partner="Händler",
            move_type="in_invoice",
            info={"gift_recipient_id": book.partner("Empfänger")},
        )
        bank = book.move("Bank", d(2027, 1, 5), ("1610", 40), ("1200", -40))
        book.rec(bill("1610"), bank("1610"))
        self.gift(book, d(2027, 3, 15), 20)
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {GIFT: D("660.00")})
        self.assertNotIn("C-51", codes(result))

    def test_gift_without_recipient(self):
        book = Book()
        self.gift(book, d(2027, 3, 1), 30, partner=None)
        self.assertIn("C-51", codes(self.run_year(book, 2027), m.BLOCKER))
        book = Book()
        book.move(
            "Präsentkörbe",
            d(2027, 2, 1),
            ln("4630", 300, unit_price=60),
            ln("1200", -300),
            info={"gift_bulk": True},
        )
        self.assertIn("C-51", codes(self.run_year(book, 2027), m.WARNING))


class TestEntertainment(EngineCase):
    def test_n04_separate_accounts(self):
        book = Book()
        book.move(
            "Bewirtung", d(2027, 3, 1), ("4650", 70), ("4654", 30), ("1200", -100)
        )
        result = self.run_year(book, 2027)
        self.assertEqual(
            fields(result),
            {ENTERTAINMENT: D("70.00"), ENTERTAINMENT_NOT_DEDUCTIBLE: D("30.00")},
        )
        self.assertNotIn("C-53", codes(result))

    def test_n05_split_automatically(self):
        book = Book(entertainment_mode="split")
        book.move("Bewirtung", d(2027, 3, 1), ("4650", 100.01), ("1200", -100.01))
        result = self.run_year(book, 2027)
        self.assertEqual(
            fields(result),
            {ENTERTAINMENT: D("70.01"), ENTERTAINMENT_NOT_DEDUCTIBLE: D("30.00")},
        )
        book = Book()
        book.move("Bewirtung", d(2027, 3, 1), ("4650", 100), ("1200", -100))
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {ENTERTAINMENT: D("100.00")})
        self.assertIn("C-53", codes(result, m.WARNING))


class TestPrivate(EngineCase):
    def test_e01_e02_withdrawal_and_contribution(self):
        book = Book()
        book.move("Entnahme", d(2027, 2, 1), ("1800", 500), ("1200", -500))
        book.move("Einlage", d(2027, 3, 1), ("1200", 250), ("1890", -250))
        book.move("Umbuchung", d(2027, 12, 31), ("1890", 250), ("1800", -250))
        result = self.run_year(book, 2027)
        self.assertEqual(
            fields(result), {WITHDRAWAL: D("500.00"), CONTRIBUTION: D("250.00")}
        )

    def test_e03_withdrawal_in_kind_by_correction(self):
        book = Book()
        book.adjustment_amounts += [
            m.FieldAmount(year=2027, field="EUER.20.108", amount=4000),
            m.FieldAmount(year=2027, field=WITHDRAWAL, amount=4000),
        ]
        self.assertEqual(
            fields(self.run_year(book, 2027)),
            {"EUER.20.108": D("40.00"), WITHDRAWAL: D("40.00")},
        )

    def private_journal(self, private=True):
        book = Book()
        invoice = self.sale(book, "RE", d(2027, 3, 1), 500)
        bank = book.move(
            "Privatkonto",
            d(2027, 3, 5),
            ("1210", 500),
            ("1410", -500),
            private_journal=private,
        )
        book.rec(invoice("1410"), bank("1410"))
        book.move(
            "Privatkonto",
            d(2027, 3, 6),
            ("4930", 200),
            ("1210", -200),
            private_journal=private,
        )
        book.move(
            "Einkauf privat",
            d(2027, 3, 7),
            ("1800", 80),
            ("1210", -80),
            private_journal=private,
        )
        return book

    def test_e04_e06_private_account_journal(self):
        """R-18: income = withdrawal, expense = contribution; no double count."""
        result = self.run_year(self.private_journal(), 2027)
        self.assertEqual(
            fields(result),
            {
                KU: D("500.00"),
                WORK: D("200.00"),
                WITHDRAWAL: D("500.00"),
                CONTRIBUTION: D("200.00"),
            },
        )
        result = self.run_year(self.private_journal(private=False), 2027)
        self.assertEqual(
            fields(result), {KU: D("500.00"), WORK: D("200.00"), WITHDRAWAL: D("80.00")}
        )

    def test_use_contribution_not_counted_twice(self):
        """4590 → 25.147 and 29.123, booked against 1890 only once."""
        book = Book()
        book.move("Nutzungseinlage", d(2027, 12, 31), ("4590", 300), ("1890", -300))
        self.assertEqual(
            fields(self.run_year(book, 2027)),
            {"EUER.25.147": D("300.00"), CONTRIBUTION: D("300.00")},
        )


class TestPaymentWays(EngineCase):
    def test_b02_credit_card(self):
        book = Book()
        book.move("Karte", d(2026, 12, 28), ("4930", 50), ("1730", -50))
        book.move("Abrechnung", d(2027, 1, 15), ("1730", 50), ("1200", -50))
        self.assertEqual(fields(self.run_year(book, 2026)), {WORK: D("50.00")})
        self.assertEqual(fields(self.run_year(book, 2027)), {})

    def _paypal(self, account):
        book = Book()
        invoice = self.sale(book, "RE", d(2026, 12, 20), 100)
        paid = book.move("PayPal", d(2026, 12, 30), (account, 100), ("1410", -100))
        payout = book.move("Auszahlung", d(2027, 1, 3), ("1200", 100), (account, -100))
        book.rec(invoice("1410"), paid("1410"))
        if account == "PPC":
            book.rec(paid(account), payout(account))
        return book

    def test_b03_payment_provider(self):
        book = self._paypal("PP")
        self.assertEqual(fields(self.run_year(book, 2026)), {KU: D("100.00")})
        book = self._paypal("PPC")
        self.assertEqual(fields(self.run_year(book, 2026)), {})
        self.assertEqual(fields(self.run_year(book, 2027)), {KU: D("100.00")})

    def test_b04_down_payment(self):
        book = Book()
        first = self.sale(book, "Anzahlung", d(2026, 11, 1), 500)
        self.receipt(book, "Bank", d(2026, 11, 10), 500, first)
        final = book.move(
            "Schlussrechnung",
            d(2027, 2, 1),
            ("1410", 1000),
            ("8195", -1500),
            ("8195", 500),
            move_type="out_invoice",
        )
        self.receipt(book, "Bank", d(2027, 2, 10), 1000, final)
        self.assertEqual(fields(self.run_year(book, 2026)), {KU: D("500.00")})
        self.assertEqual(fields(self.run_year(book, 2027)), {KU: D("1000.00")})

    def test_b06_instalments(self):
        book = Book()
        invoice = book.move(
            "RE",
            d(2026, 10, 1),
            ("1410", 600),
            ("1410", 400),
            ("8195", -1000),
            move_type="out_invoice",
        )
        first = book.move("Rate 1", d(2026, 11, 1), ("1200", 600), ("1410", -600))
        second = book.move("Rate 2", d(2027, 2, 1), ("1200", 400), ("1410", -400))
        book.rec(invoice("1410", 0), first("1410"))
        book.rec(invoice("1410", 1), second("1410"))
        self.assertEqual(fields(self.run_year(book, 2026)), {KU: D("600.00")})
        self.assertEqual(fields(self.run_year(book, 2027)), {KU: D("400.00")})

    def _deductible(self, partial=None):
        book = Book(regime="regular")
        bill = book.move(
            "ER 50 %",
            d(2026, 12, 10),
            ln("4930", 100, tax="vst19"),
            ln("1576", 19, rep="vst19"),
            ln("4930", -50, tax="vst19"),
            ln("1576", -9.50, rep="vst19"),
            ln("1800", 59.50),
            ln("1610", -119),
            move_type="in_invoice",
        )
        rest = 119
        if partial:
            early = book.move(
                "Bank", d(2026, 12, 20), ("1610", partial), ("1200", -partial)
            )
            book.rec(bill("1610"), early("1610"))
            rest -= partial
        bank = book.move("Bank", d(2027, 1, 20), ("1610", rest), ("1200", -rest))
        book.rec(bill("1610"), bank("1610"))
        return book

    def test_b13_private_share_with_payment(self):
        book = self._deductible()
        self.assertEqual(fields(self.run_year(book, 2026)), {})
        self.assertEqual(
            fields(self.run_year(book, 2027)),
            {WORK: D("50.00"), INPUT_VAT: D("9.50"), WITHDRAWAL: D("59.50")},
        )
        book = self._deductible(partial=59.50)
        self.assertEqual(
            fields(self.run_year(book, 2026)),
            {WORK: D("25.00"), INPUT_VAT: D("4.75"), WITHDRAWAL: D("29.75")},
        )

    def test_b13b_invoice_line_on_private_account(self):
        book = Book()
        bill = book.move(
            "ER",
            d(2026, 12, 10),
            ("4930", 80),
            ("1800", 20),
            ("1610", -100),
            move_type="in_invoice",
        )
        bank = book.move("Bank", d(2027, 1, 20), ("1610", 100), ("1200", -100))
        book.rec(bill("1610"), bank("1610"))
        self.assertEqual(fields(self.run_year(book, 2026)), {})
        self.assertEqual(
            fields(self.run_year(book, 2027)),
            {WORK: D("80.00"), WITHDRAWAL: D("20.00")},
        )

    def test_b14_payment_order_proposal(self):
        book = Book()
        bill = book.move(
            "ER", d(2026, 12, 15), ("4930", 100), ("1610", -100), move_type="in_invoice"
        )
        payment = book.move("Auftrag", d(2026, 12, 30), ("1610", 100), ("OP", -100))
        bank = book.move(
            "Bank",
            d(2027, 1, 2),
            ("OP", 100),
            ("1200", -100),
            payment_order_date=d(2026, 12, 30),
        )
        book.rec(bill("1610"), payment("1610"))
        book.rec(payment("OP"), bank("OP"))
        result = self.run_year(book, 2026)
        item = [i for i in result.ten_day_items if i.kind == "transfer_order"][0]
        self.assertEqual(item.proposed_cash_date, d(2026, 12, 30))
        book.confirmations.add(bank.move.id)
        self.assertNotIn("C-23", codes(self.run_year(book, 2026)))

    def test_b12_bank_date(self):
        book = Book()
        book.move(
            "Auszug",
            d(2027, 1, 3),
            ("1200", 250),
            ("8195", -250),
            statement=True,
            bank_date=d(2026, 12, 30),
        )
        result = self.run_year(book, 2026)
        self.assertIn("C-12", codes(result, m.WARNING))

    def test_c24_payment_booked_on_bank_account(self):
        """Payment on the bank account matched with the statement line."""
        book = Book()
        invoice = self.sale(book, "RE", d(2026, 12, 1), 100)
        payment = book.move("Zahlung", d(2026, 12, 28), ("1200", 100), ("1410", -100))
        statement_entry = book.move(
            "Auszug",
            d(2027, 1, 4),
            ("1200", 100),
            ("1200", -100),
            statement=True,
        )
        book.rec(invoice("1410"), payment("1410"))
        book.rec(payment("1200"), statement_entry("1200", 1))
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {KU: D("100.00")})
        self.assertIn("C-24", codes(result, m.WARNING))


class TestDirectEntries(EngineCase):
    def test_d01_benefit_in_kind(self):
        book = Book(regime="regular")
        book.move(
            "Sachbezug",
            d(2027, 6, 30),
            ln("4120", 100),
            ln("8590", -100, tags=("81_BASE",), tax="ust19"),
        )
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {PERSONNEL: D("100.00"), TAXABLE: D("100.00")})
        self.assertEqual({s.kind for s in result.slices}, {m.BOOKING})

    def test_d02_payroll_liability(self):
        book = Book()
        book.move("Lohn", d(2026, 12, 31), ("4120", 2000), ("1740", -2000))
        book.move("Zahlung", d(2027, 1, 5), ("1740", 2000), ("1200", -2000))
        result = self.run_year(book, 2026)
        self.assertEqual(fields(result), {})
        self.assertIn("C-03", codes(result, m.INFO))
        self.assertEqual(fields(self.run_year(book, 2027)), {PERSONNEL: D("2000.00")})

    def test_d03_reclassification(self):
        book = Book()
        book.move("Bank", d(2027, 3, 1), ("4930", 100), ("1200", -100))
        book.move("Umgliederung", d(2027, 3, 31), ("4965", 100), ("4930", -100))
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {"EUER.25.222": D("100.00")})
        book = Book()
        book.move("Umgliederung", d(2027, 3, 31), ("4965", 100), ("4930", -100))
        result = self.run_year(book, 2027)
        self.assertEqual(
            fields(result), {"EUER.25.222": D("100.00"), WORK: D("-100.00")}
        )
        _values, findings = statement.form_values(result.totals, book.specs[2026], 2027)
        self.assertEqual([f.params["field"] for f in findings], [WORK])
        book = Book()
        book.move("Bank", d(2026, 3, 1), ("4930", 100), ("1200", -100))
        book.move("Umgliederung", d(2027, 1, 31), ("4965", 100), ("4930", -100))
        self.assertIn("C-03", codes(self.run_year(book, 2027), m.INFO))

    def test_d04_ledger_depreciation(self):
        book = Book().previous_turnover(2027)
        entry = book.move("AfA", d(2027, 12, 31), ("4830", 234.29), ("0490", -234.29))
        result = self.run_year(book, 2027)
        self.assertEqual(result.asset_entries, [entry.move.id])
        self.assertEqual(codes(result), [])

    def test_d05_expense_on_neutral_liability(self):
        book = Book(regime="regular")
        book.move(
            "Aufwand", d(2027, 3, 1), ln("4930", 100), ln("1576", 19), ln("1700", -119)
        )
        book.move("Zahlung", d(2027, 3, 20), ("1700", 119), ("1200", -119))
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {})
        self.assertIn("C-04", codes(result, m.BLOCKER))
        self.assertIn("C-18", codes(result, m.BLOCKER))

    def test_d06_provision(self):
        book = Book()
        book.move("Rückstellung", d(2027, 12, 31), ("4950", 800), ("0970", -800))
        self.assertIn("C-04", codes(self.run_year(book, 2027), m.BLOCKER))

    def test_d07_neutral_transfer(self):
        book = Book().previous_turnover(2027)
        book.move("Umbuchung", d(2027, 6, 1), ("1700", 1000), ("0630", -1000))
        result = self.run_year(book, 2027)
        self.assertEqual(result.slices, [])
        self.assertEqual(codes(result), [])


class TestReturnedDebits(EngineCase):
    def lastschrift(self, book, day, invoice):
        return self.receipt(book, "Lastschrift", day, 119, invoice)

    def returned(self, book, day, debit, fee=3):
        entry = book.move(
            "Rücklastschrift",
            day,
            ("1410", 119),
            ("4970", fee),
            ("1200", -(119 + fee)),
        )
        del debit
        return entry

    def test_r01_new_payment_same_year(self):
        book = Book()
        invoice = self.sale(book, "RE", d(2027, 3, 1), 119)
        debit = self.lastschrift(book, d(2027, 3, 10), invoice)
        back = self.returned(book, d(2027, 3, 20), debit)
        again = book.move("Zahlung", d(2027, 4, 5), ("1200", 119), ("1410", -119))
        book.rec(back("1410"), again("1410"))
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {KU: D("119.00"), OTHER: D("3.00")})
        self.assertNotIn("C-37", codes(result))

    def test_r02_written_off(self):
        book = Book()
        invoice = self.sale(book, "RE", d(2027, 3, 1), 119)
        debit = self.lastschrift(book, d(2027, 3, 10), invoice)
        back = self.returned(book, d(2027, 3, 20), debit)
        writeoff = book.move(
            "Ausbuchung", d(2027, 6, 30), ("2400", 119), ("1410", -119)
        )
        book.rec(back("1410"), writeoff("1410"))
        result = self.run_year(book, 2027)
        found = [f for f in result.findings if f.code == "C-36"]
        self.assertEqual(found[0].severity, m.BLOCKER)
        self.assertEqual(found[0].params["proposal_move_ids"], (invoice.move.id,))
        book.classify(writeoff("2400"), "negative_revenue", "REV_KU", regime="ku")
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {OTHER: D("3.00")})
        self.assertNotIn("C-36", codes(result))

    def test_r03_returned_next_year_paid_again(self):
        book = Book()
        invoice = self.sale(book, "RE", d(2026, 12, 1), 119)
        debit = self.lastschrift(book, d(2026, 12, 15), invoice)
        back = self.returned(book, d(2027, 1, 10), debit, fee=0)
        again = book.move("Zahlung", d(2027, 2, 10), ("1200", 119), ("1410", -119))
        book.rec(back("1410"), again("1410"))
        self.assertEqual(fields(self.run_year(book, 2026)), {KU: D("119.00")})
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {})
        self.assertNotIn("C-37", codes(result))

    def test_r04_money_to_money_between_years(self):
        book = Book()
        invoice = self.sale(book, "RE", d(2026, 12, 1), 119)
        debit = self.lastschrift(book, d(2026, 12, 15), invoice)
        back = self.returned(book, d(2026, 12, 28), debit, fee=0)
        again = book.move("Zahlung", d(2027, 1, 15), ("1200", 119), ("1410", -119))
        book.rec(back("1410"), again("1410"))
        self.assertIn("C-37", codes(self.run_year(book, 2026), m.BLOCKER))
        self.assertIn("C-37", codes(self.run_year(book, 2027), m.BLOCKER))
        book.classify(back("1410"), "negative_revenue", "REV_KU", regime="ku")
        book.classify(again("1410"), "revenue", "REV_KU", regime="ku")
        result = self.run_year(book, 2026)
        self.assertEqual(result.totals_for(2026), {})
        self.assertNotIn("C-37", codes(result))
        self.assertEqual(fields(self.run_year(book, 2027)), {KU: D("119.00")})


class TestWriteOffInPayment(EngineCase):
    def test_writeoff_line_in_bank_entry(self):
        """A write-off inside the payment reduces the invoice (5.5): no C-36,
        the money realises the rest of the composition."""
        book = Book()
        invoice = self.sale(book, "RE", d(2027, 3, 1), 100)
        bank = book.move(
            "Bank", d(2027, 3, 10), ("1200", 95), ("2400", 5), ("1410", -100)
        )
        book.rec(invoice("1410"), bank("1410"))
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {KU: D("95.00")})
        self.assertNotIn("C-36", codes(result))


class TestOpening(EngineCase):
    def test_t22b_balance_carryforward_entries(self):
        book = Book()
        receivable = book.move(
            "Saldenvortrag Debitoren",
            d(2027, 1, 1),
            ("1410", 260),
            ("9008", -260),
            partner="Kunde",
        )
        book.move("Saldenvortrag Bank", d(2027, 1, 1), ("1200", 5000), ("9000", -5000))
        bank = book.move("Bank", d(2027, 1, 15), ("1200", 260), ("1410", -260))
        book.rec(receivable("1410"), bank("1410"))
        result = self.run_year(book, 2027)
        self.assertEqual(result.classes[receivable.move.id], m.OPENING)
        self.assertIn("C-35", codes(result, m.BLOCKER))
        book.opening(receivable("1410"), "REV_KU")
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {KU: D("260.00")})
        self.assertNotIn("C-46", codes(result))

    def test_t22c_standard_taxation(self):
        book = Book(regime="regular")
        opening = book.move(
            "Eröffnung",
            d(2027, 1, 1),
            ("1410", 119),
            ("9008", -119),
            ("9009", 238),
            ("1610", -238),
        )
        book.opening(opening("1410"), "REV_TAXABLE", "regular", 19)
        book.opening(opening("1610"), "EXP_WORK_EQUIPMENT", "regular", 19)
        income = book.move("Bank", d(2027, 2, 1), ("1200", 119), ("1410", -119))
        expense = book.move("Bank", d(2027, 2, 2), ("1610", 238), ("1200", -238))
        book.rec(opening("1410"), income("1410"))
        book.rec(opening("1610"), expense("1610"))
        result = self.run_year(book, 2027)
        self.assertEqual(
            fields(result),
            {
                TAXABLE: D("100.00"),
                OUTPUT_VAT: D("19.00"),
                WORK: D("200.00"),
                INPUT_VAT: D("38.00"),
            },
        )

    def test_c46_opening_after_money(self):
        book = Book()
        book.move("Bank", d(2027, 1, 2), ("1200", 50), ("8195", -50))
        book.move(
            "Eröffnung", d(2027, 1, 3), ("1200", 1000), ("9000", -1000), opening=True
        )
        self.assertIn("C-46", codes(self.run_year(book, 2027), m.WARNING))


class TestCurrencyAndChecks(EngineCase):
    def test_f01_missing_exchange_difference(self):
        book = Book()
        invoice = book.move(
            "RE USD",
            d(2027, 3, 1),
            ln("1410", 909.09, currency_id=2, residual_currency_zero=True),
            ln("8195", -909.09),
            move_type="out_invoice",
        )
        self.receipt(book, "Bank", d(2027, 4, 1), 900, invoice)
        result = self.run_year(book, 2027)
        self.assertIn("C-19", codes(result, m.BLOCKER))
        self.assertEqual(fields(result), {KU: D("900.00")})

    def test_c28_revaluation_left_out(self):
        book = Book()
        book.move(
            "Neubewertung",
            d(2027, 12, 31),
            ("1200", 12),
            ("2660", -12),
            revaluation=True,
        )
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {})
        self.assertIn("C-28", codes(result, m.WARNING))

    def test_c29_booking_category_in_document(self):
        book = Book()
        book.move(
            "RE Sachbezug",
            d(2027, 3, 1),
            ("1410", 100),
            ("8590", -100),
            move_type="out_invoice",
        )
        self.assertIn("C-29", codes(self.run_year(book, 2027), m.BLOCKER))

    def test_c21_cycle(self):
        """Two transit entries reconciled with each other in a loop."""
        book = Book()
        first = book.move("T1", d(2027, 3, 1), ("OR", 100), ("1360", -100))
        second = book.move("T2", d(2027, 3, 2), ("1360", 100), ("OR", -100))
        bank = book.move("Bank", d(2027, 3, 3), ("1200", 30), ("OR", -30))
        book.rec(first("OR"), bank("OR"), 30)
        book.rec(first("OR"), second("OR"), 70)
        book.rec(second("1360"), first("1360"))
        result = self.run_year(book, 2027)
        self.assertIn("C-21", codes(result, m.BLOCKER))
        self.assertEqual([n.amount for n in result.notes if n.kind == "cycle"], [-2100])


class TestGraphCases(EngineCase):
    def test_c22_transit_with_money_from_both_sides(self):
        """φ caps the realisation of a transit entry at 100 % (I-3, C-22)."""
        book = Book()
        transit = book.move(
            "Zahlung", d(2027, 3, 1), ("OR", 100), ("4970", 10), ("1410", -110)
        )
        first = book.move("Bank1", d(2027, 3, 2), ("1200", 100), ("OR", -100))
        second = book.move("Bank2", d(2027, 3, 3), ("1410", 110), ("1200", -110))
        book.rec(transit("OR"), first("OR"))
        book.rec(second("1410"), transit("1410"))
        result = self.run_year(book, 2027)
        self.assertIn("C-22", codes(result, m.INFO))
        self.assertEqual(fields(result), {OTHER: D("10.00")})
        self.assertIn("transit_capped", {n.kind for n in result.notes})

    def test_credit_note_moves_to_other_port(self):
        """E2 without open port left: the bundle goes to the other ports."""
        book = Book()
        invoice = book.move(
            "RE",
            d(2027, 2, 1),
            ("1410", 600),
            ("1410", 400),
            ("8195", -1000),
            move_type="out_invoice",
        )
        credit = book.move(
            "GS", d(2027, 2, 10), ("8195", 600), ("1410", -600), move_type="out_refund"
        )
        book.rec(invoice("1410", 0), credit("1410"))
        bank = book.move("Bank", d(2027, 3, 1), ("1200", 400), ("1410", -400))
        book.rec(invoice("1410", 1), bank("1410"))
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {KU: D("400.00")})
        origins = {s.origin_move_id for s in result.slices}
        self.assertEqual(origins, {invoice.move.id, credit.move.id})
        self.assertEqual({s.document_move_id for s in result.slices}, {invoice.move.id})


class TestFormCalculation(EngineCase):
    def test_profit_from_totals(self):
        """Phase G: totals → form values with the formulas of the year."""
        book = Book()
        invoice = self.sale(book, "RE", d(2026, 3, 1), 1000)
        self.receipt(book, "Bank", d(2026, 3, 10), 1000, invoice)
        book.move("Bank", d(2026, 4, 1), ("4930", 200), ("1200", -200))
        result = self.run_year(book, 2026)
        values, findings = statement.form_values(result.totals, book.specs[2026], 2026)
        self.assertEqual(findings, [])
        self.assertEqual(values["EUER.20.159"], D("1000.00"))
        self.assertEqual(values["EUER.25.199"], D("200.00"))
        self.assertEqual(values["EUER.G.219"], D("800.00"))
        shown = statement.display_values(values, book.specs[2026])
        self.assertNotIn("EUER.20.112", shown)
        self.assertIsNone(values.get("SZ.43.HINZURECHNUNG"))

    def test_overflow_and_of_which(self):
        totals, moved = statement.overflow({KU: -500, KU_DAVON: 700, OTHER: 100})
        self.assertEqual(totals, {KU: 0, KU_DAVON: 0, OTHER: 600})
        self.assertEqual(moved, {KU: 500})
        totals, moved = statement.overflow({KU: 500, KU_DAVON: 700})
        self.assertEqual(totals[KU_DAVON], 500)
        self.assertEqual(moved, {})

    def test_blocked_field_and_compare(self):
        book = Book()
        spec = book.specs[2026]
        _values, findings = statement.form_values({"EUER.G.180": 10000}, spec, 2026)
        self.assertEqual([f.code for f in findings], ["C-40"])
        self.assertEqual(
            statement.compare({KU: 1, TAXABLE: 2}, {KU: 1, OTHER: 3}),
            {TAXABLE: (2, 0), OTHER: (0, 3)},
        )
