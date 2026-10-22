# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Rules of the pure calculation engine without a test before the release
(E5, O-06): depreciation after a takeover and in its last year, the limits
of the book value, the VAT basis of a use withdrawal, the category of an
account per period, the order of the form formulas, bounded rounding, the
hash chain and the columns of the data access package. Synthetic values
(D6); runs without Odoo as well (``pytest``)."""

import dataclasses
from datetime import date
from decimal import Decimal
from fractions import Fraction

from odoo.tests import BaseCase, tagged

from ..models.engine import assignment, export, form_calc, graph, integrity, rounding
from ..models.engine import depreciation as dep
from ..models.engine import model as m
from .engine_common import Book, EngineCase, codes, d, fields, ln

D = Decimal


def asset(**values):
    defaults = {
        "kind": "depreciable",
        "method": "linear",
        "delivery_date": date(2024, 1, 10),
        "cost": D("1200.00"),
        "useful_life_months": 36,
    }
    defaults.update(values)
    return dep.Asset(**defaults)


def depreciation(rows):
    return {row.year: row.depreciation for row in rows}


@tagged("post_install", "-at_install")
class TestDepreciationTakeover(BaseCase):
    """A takeover continues from the book value at the takeover date; the
    events before it decide how (spec 6.6, R 7.4 (9) EStR, § 7a EStG)."""

    def test_new_useful_life_before_the_takeover(self):
        """2022: 1,200 over 60 months; from 2023 a remaining life of 24
        months; taken over on 31.12.2023 at 480: the rest in 2024."""
        rows = dep.schedule(
            asset(
                delivery_date=date(2022, 1, 10),
                useful_life_months=60,
                events=(
                    dep.Event(
                        "useful_life_change", date(2023, 1, 1), useful_life_months=24
                    ),
                ),
                opening_date=date(2023, 12, 31),
                opening_book_value=D("480.00"),
            )
        )
        self.assertEqual(depreciation(rows), {2024: D("480.00")})

    def test_event_after_the_takeover_counts_in_its_year(self):
        """Events after the takeover date are no history: the remaining life
        of 12 months from 2025 spreads the book value of 480 over 2025."""
        rows = dep.schedule(
            asset(
                delivery_date=date(2022, 1, 10),
                useful_life_months=60,
                events=(
                    dep.Event(
                        "useful_life_change", date(2025, 1, 1), useful_life_months=12
                    ),
                ),
                opening_date=date(2023, 12, 31),
                opening_book_value=D("720.00"),
            )
        )
        self.assertEqual(depreciation(rows), {2024: D("240.00"), 2025: D("480.00")})

    def test_building_keeps_its_percentage_after_a_takeover(self):
        """H 7.4 EStH: 300,000 from 01/2015 at 3 %, additional cost 30,000 in
        2020, taken over on 31.12.2023: 3 % of 330,000 = 9,900 per year."""
        rows = dep.schedule(
            asset(
                group="building",
                delivery_date=date(2015, 1, 15),
                cost=D("300000.00"),
                useful_life_months=400,
                events=(dep.Event("additional_cost", date(2020, 6, 1), D("30000.00")),),
                opening_date=date(2023, 12, 31),
                opening_book_value=D("245400.00"),
            ),
            until=2025,
        )
        values = depreciation(rows)
        self.assertEqual((values[2024], values[2025]), (D("9900.00"), D("9900.00")))

    def test_cost_with_changes_within_the_special_period(self):
        """§ 7a (1) EStG: within the period of a special depreciation (§ 7g,
        5 years from 2021) the linear amount follows the cost with its
        changes: (10,000 + 1,000) × 12 / 120 = 1,100."""
        rows = dep.schedule(
            asset(
                delivery_date=date(2021, 3, 1),
                cost=D("10000.00"),
                useful_life_months=120,
                events=(
                    dep.Event("sec7g_special", date(2021, 6, 1), D("2000.00")),
                    dep.Event("additional_cost", date(2022, 5, 1), D("1000.00")),
                ),
                opening_date=date(2023, 12, 31),
                opening_book_value=D("6000.00"),
            ),
            until=2024,
        )
        self.assertEqual(depreciation(rows)[2024], D("1100.00"))

    def test_low_value_asset_of_a_takeover_has_no_year(self):
        """A low-value asset is an expense in its year of delivery, so after
        a takeover nothing is left to depreciate."""
        self.assertEqual(
            dep.schedule(
                asset(
                    kind="gwg",
                    method="none",
                    cost=D("700.00"),
                    useful_life_months=0,
                    opening_date=date(2024, 12, 31),
                    opening_book_value=D("0.00"),
                )
            ),
            [],
        )


@tagged("post_install", "-at_install")
class TestDepreciationLimits(BaseCase):
    def test_depreciation_never_exceeds_the_book_value(self):
        """Special depreciation above the cost of 1,000, or an extraordinary
        depreciation above the book value, are refused."""
        cases = (
            dep.Event("sec7g_special", date(2024, 6, 1), D("1100.00")),
            dep.Event("extraordinary", date(2025, 6, 1), D("2000.00")),
        )
        for event in cases:
            with self.subTest(event=event.type):
                with self.assertRaises(dep.DepreciationError) as catcher:
                    dep.schedule(
                        asset(
                            cost=D("1000.00"),
                            useful_life_months=120,
                            events=(event,),
                        )
                    )
                self.assertEqual(catcher.exception.code, "exceeds_book_value")

    def test_group_without_depreciation_column(self):
        """Land has no depreciation column in Anlage AVEÜR (Kz 100–105)."""
        values = dep.YearValues(
            year=2026,
            cost_start=D("1000.00"),
            book_value_start=D("1000.00"),
            additions=D("0.00"),
            special_depreciation=D("0.00"),
            depreciation=D("100.00"),
            disposal_book_value=D("0.00"),
            book_value_end=D("900.00"),
        )
        entry = dep.RegisterAsset(
            asset=asset(group="land"),
            locked_values=values,
        )
        with self.assertRaises(dep.DepreciationError) as catcher:
            dep.field_contributions(2026, [entry])
        self.assertEqual(catcher.exception.code, "group_without_depreciation")


@tagged("post_install", "-at_install")
class TestPrivateValues(BaseCase):
    def test_vat_basis_of_a_use_withdrawal(self):
        """§ 10 (4) no. 2 UStG: below 500 € the cost counts in the year of
        delivery only; no cost, no basis."""
        delivery = date(2024, 5, 2)
        self.assertEqual(dep.private_use_vat_base(0, delivery, 2024), dep.ZERO)
        self.assertEqual(
            dep.private_use_vat_base(D("400.00"), delivery, 2024), D("400.00")
        )
        self.assertEqual(dep.private_use_vat_base(D("400.00"), delivery, 2025), 0)

    def test_three_years_back_from_29_february(self):
        """§ 6 (1) no. 5 (a) EStG: contributed on 29.02.2028, three years
        back is 28.02.2025; bought after that day the value is limited."""
        contribution = date(2028, 2, 29)
        self.assertTrue(dep.contribution_reduced(date(2025, 3, 1), contribution))
        self.assertFalse(dep.contribution_reduced(date(2025, 2, 28), contribution))

    def test_sec15a_without_input_tax(self):
        self.assertEqual(
            dep.sec15a_correction(D("0"), date(2024, 1, 10), date(2026, 1, 1)),
            ({}, True),
        )


@tagged("post_install", "-at_install")
class TestEngineHelpers(BaseCase):
    def test_category_of_an_account_per_period(self):
        """The valid assignment with the latest start wins; future and ended
        assignments do not count."""
        account = m.Account(
            id=1,
            code="4930",
            account_type="expense",
            internal_group="expense",
            reconcile=False,
            assignments=(
                m.Assignment(category="OLD", date_to=date(2023, 12, 31)),
                m.Assignment(category="BASE", date_from=date(2024, 1, 1)),
                m.Assignment(category="FUTURE", date_from=date(2027, 1, 1)),
                m.Assignment(category="EARLY", date_from=date(2020, 1, 1)),
            ),
        )
        categories = {
            code: m.Category(code=code, role="ba")
            for code in ("OLD", "BASE", "FUTURE", "EARLY")
        }
        self.assertEqual(
            graph.category_at(account, categories, date(2026, 6, 1)).code, "BASE"
        )
        self.assertEqual(
            graph.category_at(account, categories, date(2019, 6, 1)).code, "OLD"
        )

    def test_formulas_without_cycle(self):
        """The order of the formulas ignores references outside them."""
        self.assertFalse(
            form_calc._find_cycle(
                {"A": {"B", "X"}, "B": {"C"}, "C": set(), "D": {"C"}},
                {"A", "B", "C", "D"},
            )
        )
        self.assertEqual(
            form_calc._find_cycle({"A": {"B"}, "B": {"A"}}, {"A", "B"}),
            ["A", "B", "A"],
        )

    def test_bounds_without_limits(self):
        """A key without lower and upper bound takes the normal share."""
        raw = {"a": D("1.5"), "b": D("1.5")}
        self.assertEqual(
            rounding.apportion_bounded(raw, 3, {"a": (None, None)}),
            rounding.apportion(raw, 3),
        )

    def test_integrity_of_values(self):
        """Amounts and values of a payload must be canonical; a link with
        a hash that is no hash is reported, not raised."""
        with self.assertRaises(integrity.IntegrityError):
            integrity.cents("zwölf")
        with self.assertRaises(integrity.IntegrityError):
            integrity.normalize({1, 2})
        good = "0" * 64
        link = integrity.Link(
            index=1,
            prev_hash=integrity.GENESIS,
            payload_sha256="kein Hash",
            chain_hash=good,
            content_sha256="kein Hash",
        )
        keys = [problem.key for problem in integrity.verify_chain([link])]
        self.assertIn("chain_hash", keys)

    def test_column_without_description(self):
        """``index.xml`` names a column; the description is optional."""
        lines = []
        export._column_xml(lines, 1, "VariableColumn", export.Column("Kz"))
        text = "\n".join(lines)
        self.assertIn("<Name>Kz</Name>", text)
        self.assertNotIn("Description", text)


EXEMPT = "EUER.20.103"
INPUT_VAT = "EUER.25.185"
OUTPUT_VAT = "EUER.20.140"
TAXABLE = "EUER.20.112"
WORK = "EUER.25.229"


@tagged("post_install", "-at_install")
class TestManualTaxLines(EngineCase):
    """Tax account lines without a tax (entered by hand, 5.7 no. 4 and 5):
    the account decides, C-34 asks for the tax."""

    def test_small_business_input_vat_belongs_to_the_cost(self):
        """§ 19 UStG: no input tax deduction, the VAT is part of the cost
        (R 9b (1) EStR)."""
        book = Book()
        book.move(
            "Bank", d(2026, 3, 1), ln("4930", 100), ln("1576", 19), ln("1200", -119)
        )
        self.assertEqual(fields(self.run_year(book, 2026)), {WORK: D("119.00")})

    def test_input_vat_not_deductible_belongs_to_the_cost(self):
        book = Book(regime="regular")
        book.move(
            "Bank", d(2026, 3, 1), ln("4930", 100), ln("1566", 19), ln("1200", -119)
        )
        self.assertEqual(fields(self.run_year(book, 2026)), {WORK: D("119.00")})

    def test_output_vat_entered_by_hand(self):
        """Cash sale with the VAT on 1776 typed in: Kz 112 and 140."""
        book = Book(regime="regular")
        book.move(
            "Kasse", d(2026, 3, 1), ln("1200", 119), ln("8400", -100), ln("1776", -19)
        )
        result = self.run_year(book, 2026)
        self.assertEqual(fields(result), {TAXABLE: D("100.00"), OUTPUT_VAT: D("19.00")})
        self.assertIn("C-34", codes(result, m.WARNING))

    def test_reverse_charge_entered_by_hand_is_neutral(self):
        """§ 13b UStG: the tax owed and the input tax of the recipient cancel
        out; only the service is an expense."""
        book = Book(regime="regular")
        book.move(
            "Bank",
            d(2026, 3, 1),
            ln("4930", 100),
            ln("1577", 19),
            ln("1787", -19),
            ln("1200", -100),
        )
        result = fields(self.run_year(book, 2026))
        self.assertEqual(result, {WORK: D("100.00")})


@tagged("post_install", "-at_install")
class TestRevenueRules(EngineCase):
    def test_entered_rate_zero_is_an_exempt_supply(self):
        """Standard taxation booked on the small business account 8195: the
        rate entered for the supply (C-33) is 0 %, so the gross receipt is a
        tax-free supply (Kz 103), not split into net and VAT."""
        book = Book(regime="regular")
        invoice = self.sale(book, "RE", d(2027, 3, 1), 238)
        self.receipt(book, "Bank", d(2027, 3, 5), 238, invoice)
        book.moves[0] = dataclasses.replace(
            book.moves[0], info=m.MoveInfo(regular_tax_rate=Fraction(0))
        )
        result = self.run_year(book, 2027)
        self.assertEqual(fields(result), {EXEMPT: D("238.00")})
        self.assertNotIn("C-33", codes(result))

    def test_category_without_form_data(self):
        """Without any form year there is no mapping line (F-13)."""
        self.assertEqual(assignment.mapping_lines({}, "EXP_OTHER", 2026), [])
