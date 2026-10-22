# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Unit tests of the formula engine (pure Python, no database)."""

import random
from decimal import Decimal

from odoo.tests import BaseCase

from ..models.engine import form_calc as fc

D = Decimal


class TestFormulaParser(BaseCase):
    def test_parse_sum(self):
        self.assertEqual(
            fc.parse("EUER.20.111 + EUER.20.112 - EUER.G.UEB"),
            (
                "sub",
                ("add", ("ref", "EUER.20.111"), ("ref", "EUER.20.112")),
                ("ref", "EUER.G.UEB"),
            ),
        )

    def test_whitespace_is_irrelevant(self):
        self.assertEqual(
            fc.parse("  EUER.20.111+EUER.20.112 "),
            fc.parse("EUER.20.111 + EUER.20.112"),
        )

    def test_references(self):
        self.assertEqual(
            fc.references("max(0, SZ.43.A - SZ.43.B) + EUER.25.UEB2 - 2050.00"),
            {"SZ.43.A", "SZ.43.B", "EUER.25.UEB2"},
        )

    def test_signed_references(self):
        self.assertEqual(
            fc.signed_references("EUER.1.A - (EUER.1.B - EUER.1.C) + -EUER.1.D"),
            {"EUER.1.A": 1, "EUER.1.B": -1, "EUER.1.C": 1, "EUER.1.D": -1},
        )
        for formula in ("max(EUER.1.A, 0)", "EUER.1.A + 1", "EUER.1.A - EUER.1.A"):
            with self.assertRaises(fc.FormulaError):
                fc.signed_references(formula)

    def test_functions_and_literals(self):
        self.assertEqual(
            fc.parse("min(EUER.1.A, max(0, 12.50))"),
            (
                "min",
                (("ref", "EUER.1.A"), ("max", (("num", D("0")), ("num", D("12.50"))))),
            ),
        )

    def test_multiplication_by_constant(self):
        """A product needs a constant factor, so every formula stays linear."""
        self.assertEqual(
            fc.parse("0.06 * min(SZ.43.A, SZ.43.B)"),
            (
                "mul",
                ("num", D("0.06")),
                ("min", (("ref", "SZ.43.A"), ("ref", "SZ.43.B"))),
            ),
        )
        self.assertEqual(
            fc.parse("EUER.1.A * 2 + EUER.1.B"),
            ("add", ("mul", ("ref", "EUER.1.A"), ("num", D("2"))), ("ref", "EUER.1.B")),
        )
        self.assertEqual(fc.references("-0.5 * (EUER.1.A - 3)"), {"EUER.1.A"})
        for formula in ("EUER.1.A * EUER.1.B", "EUER.1.A * (EUER.1.B + 1)"):
            with self.subTest(formula=formula), self.assertRaises(fc.FormulaError):
                fc.parse(formula)
        with self.assertRaises(fc.FormulaError):
            fc.signed_references("2 * EUER.1.A")

    def test_rejects_everything_else(self):
        """Only + - * min max parentheses, codes and numbers; nothing is eval'ed."""
        invalid = [
            "",
            "   ",
            "EUER.20.111 * EUER.20.112",
            "EUER.20.111 / 2",
            "__import__('os').system('true')",
            "eval('1')",
            "euer.20.111",
            "EUER.20.111 EUER.20.112",
            "EUER.20.111 +",
            "(EUER.20.111",
            "EUER.20.111)",
            "min EUER.20.111",
            "min()",
            "min(EUER.20.111,)",
            "abs(EUER.20.111)",
            "minimum(EUER.20.111)",
            "EUER.20.111, EUER.20.112",
            "1e5",
            "EUER.20.111 ** 2",
            "EUER.20.111 if True else 0",
            "lambda: 0",
            "EUER.20.111.real",
            "[EUER.20.111]",
            "EUER.20.111; 1",
            # only ASCII digits (Arabic-Indic three, fullwidth twelve)
            "EUER.20.111 + \u0663",
            "\uff11\uff12",
            "EUER.20.111\u00a0+ 1",
            "9" * 16,
        ]
        for formula in invalid:
            with self.subTest(formula=formula), self.assertRaises(fc.FormulaError):
                fc.parse(formula)
        with self.assertRaises(fc.FormulaError):
            fc.parse(None)

    def test_limits(self):
        with self.assertRaises(fc.FormulaError):
            fc.parse("EUER.1.A + " * 300 + "EUER.1.B")
        with self.assertRaises(fc.FormulaError):
            fc.parse("(" * 60 + "EUER.1.A" + ")" * 60)
        with self.assertRaises(fc.FormulaError):
            fc.parse("-" * 60 + "EUER.1.A")
        self.assertEqual(fc.parse("((EUER.1.A))"), ("ref", "EUER.1.A"))

    def test_is_code(self):
        for code in ("EUER.20.111", "EUER.G.UEB", "AVEUER.40.490", "SZ.43.Z30_X"):
            self.assertTrue(fc.is_code(code), code)
        for code in (
            "EUER.20",
            "euer.20.111",
            "EUER.20.111 ",
            "EUER.20.111\n",
            "EUER.20.\u0663",
            "1.2.3",
            None,
        ):
            self.assertFalse(fc.is_code(code), code)


class TestFormulaEvaluation(BaseCase):
    def test_empty_stays_empty(self):
        """Fields without value are None, never 0 (R-46)."""
        self.assertIsNone(fc.evaluate("EUER.1.A + EUER.1.B", {}))
        self.assertIsNone(fc.evaluate("-EUER.1.A", {"EUER.1.A": None}))
        self.assertEqual(
            fc.evaluate("EUER.1.A - EUER.1.B", {"EUER.1.B": D("5")}), D("-5.00")
        )
        self.assertEqual(
            fc.evaluate("EUER.1.A + EUER.1.B", {"EUER.1.A": D("0")}), D("0.00")
        )

    def test_literals_alone_do_not_fill_a_field(self):
        self.assertIsNone(fc.evaluate("max(0, EUER.1.A - 2050)", {}))
        self.assertEqual(
            fc.evaluate("max(0, EUER.1.A - 2050)", {"EUER.1.A": D("3000.10")}),
            D("950.10"),
        )
        self.assertEqual(
            fc.evaluate("max(0, EUER.1.A - 2050)", {"EUER.1.A": D("1000")}), D("0.00")
        )
        self.assertEqual(fc.evaluate("2050.00", {}), D("2050.00"))

    def test_multiplication(self):
        """Anlage SZ line 25: 6 % of the lower amount, negative ⇒ 0."""
        formula = "max(0, 0.06 * min(SZ.43.A, SZ.43.B))"
        values = {"SZ.43.A": D("15000.00"), "SZ.43.B": D("20000.00")}
        self.assertEqual(fc.evaluate(formula, values), D("900.00"))
        self.assertEqual(fc.evaluate(formula, {"SZ.43.A": D("-5")}), D("0.00"))
        self.assertIsNone(fc.evaluate(formula, {}))
        self.assertEqual(
            fc.evaluate("0.06 * SZ.43.A", {"SZ.43.A": D("0.25")}), D("0.02")
        )

    def test_out_of_range(self):
        """Huge results are a formula error, not a server error."""
        formula = " * ".join(["999999999999999"] * 3)
        with self.assertRaises(fc.FormulaError):
            fc.evaluate(formula, {})

    def test_constant_fields_are_weak(self):
        values = {"SZ.1.SOCKEL": D("2050.00")}
        formula = "max(0, SZ.1.ZINS - SZ.1.SOCKEL)"
        self.assertEqual(fc.evaluate(formula, values), D("0.00"))
        self.assertIsNone(fc.evaluate(formula, values, weak={"SZ.1.SOCKEL"}))

    def test_min_max(self):
        values = {"A.1.X": D("10.00"), "A.1.Y": D("-3.50")}
        self.assertEqual(fc.evaluate("min(A.1.X, A.1.Y)", values), D("-3.50"))
        self.assertEqual(fc.evaluate("max(A.1.X, A.1.Y, A.1.Z)", values), D("10.00"))
        self.assertEqual(fc.evaluate("min(A.1.X, A.1.Z)", values), D("0.00"))
        self.assertIsNone(fc.evaluate("min(A.1.Z, A.1.W)", values))

    def test_to_amount(self):
        self.assertEqual(fc.to_amount(0.1 + 0.2), D("0.30"))
        self.assertEqual(fc.to_amount("12.345"), D("12.35"))
        self.assertEqual(fc.to_amount(-12.345), D("-12.35"))
        self.assertEqual(fc.to_amount(7), D("7.00"))
        self.assertIsNone(fc.to_amount(None))
        self.assertIsNone(fc.to_amount(False))
        for invalid in (True, "abc", float("nan"), float("inf"), [1]):
            with self.subTest(value=invalid), self.assertRaises(fc.FormulaError):
                fc.to_amount(invalid)


class TestFormCompute(BaseCase):
    FORMULAS = {
        "F.1.SUM": "F.1.A + F.1.B",
        "F.1.TOTAL": "F.1.SUM - F.1.C",
        "F.1.RESULT": "max(0, F.1.TOTAL - F.1.BASE)",
        "F.1.BASE": "100.00",
    }

    def test_order(self):
        order = fc.calculation_order(self.FORMULAS)
        self.assertLess(order.index("F.1.SUM"), order.index("F.1.TOTAL"))
        self.assertLess(order.index("F.1.TOTAL"), order.index("F.1.RESULT"))
        self.assertLess(order.index("F.1.BASE"), order.index("F.1.RESULT"))

    def test_compute(self):
        values = fc.compute(
            self.FORMULAS, {"F.1.A": 200, "F.1.B": "50.5", "F.1.C": None}
        )
        self.assertEqual(values["F.1.SUM"], D("250.50"))
        self.assertEqual(values["F.1.TOTAL"], D("250.50"))
        self.assertEqual(values["F.1.RESULT"], D("150.50"))
        self.assertIsNone(values["F.1.C"])
        empty = fc.compute(self.FORMULAS, {})
        self.assertIsNone(empty["F.1.SUM"])
        self.assertIsNone(empty["F.1.RESULT"])
        self.assertEqual(empty["F.1.BASE"], D("100.00"))

    def test_fill_conditions(self):
        """A conditional form stays empty, inputs included (Anlage SZ, F-14)."""
        formulas = {
            "S.1.BASE": "2050.00",
            "S.1.MAX": "max(0, S.1.INTEREST - S.1.BASE)",
            "S.1.INTEREST": "E.1.INTEREST",
            "E.1.ADD": "S.1.MAX",
        }
        condition = "E.1.INTEREST - 2050.00"
        conditions = {code: condition for code in formulas if code.startswith("S.")}
        conditions["S.1.CARRY"] = condition
        values = fc.compute(
            formulas,
            {"E.1.INTEREST": 2000, "S.1.CARRY": 5},
            conditions=conditions,
        )
        self.assertEqual(values["E.1.INTEREST"], D("2000.00"))
        for code in ("S.1.BASE", "S.1.MAX", "S.1.INTEREST", "S.1.CARRY", "E.1.ADD"):
            self.assertIsNone(values[code], code)
        values = fc.compute(
            formulas, {"E.1.INTEREST": 2500, "S.1.CARRY": 5}, conditions=conditions
        )
        self.assertEqual(values["S.1.BASE"], D("2050.00"))
        self.assertEqual(values["S.1.CARRY"], D("5.00"))
        self.assertEqual(values["E.1.ADD"], D("450.00"))
        self.assertFalse(fc.condition_met(condition, {"E.1.INTEREST": D("2050")}))
        self.assertFalse(fc.condition_met(condition, {}))
        with self.assertRaises(fc.FormulaError):
            fc.compute({"S.1.A": "E.1.B"}, {}, conditions={"S.1.A": "S.1.A"})

    def test_unknown_references(self):
        """A formula on a renamed or deleted field fails instead of being 0."""
        formulas = {"K.1.SUM": "K.1.A + K.1.GONE"}
        self.assertIsNone(fc.compute(formulas, {})["K.1.SUM"])
        with self.assertRaisesRegex(fc.FormulaError, "K.1.GONE"):
            fc.compute(formulas, {"K.1.A": 1}, known={"K.1.A", "K.1.SUM"})
        values = fc.compute(formulas, {"K.1.A": 1}, known={"K.1.A", "K.1.GONE"})
        self.assertEqual(values["K.1.SUM"], D("1.00"))

    def test_override(self):
        values = fc.compute(self.FORMULAS, {"F.1.A": 10}, overrides={"F.1.SUM": 500})
        self.assertEqual(values["F.1.TOTAL"], D("500.00"))
        with self.assertRaises(fc.FormulaError):
            fc.compute(self.FORMULAS, {}, overrides={"F.1.A": 1})

    def test_computed_field_is_no_input(self):
        with self.assertRaises(fc.FormulaError):
            fc.compute(self.FORMULAS, {"F.1.SUM": 1})
        # an explicitly empty value is harmless
        self.assertIsNone(fc.compute(self.FORMULAS, {"F.1.SUM": None})["F.1.SUM"])

    def test_cycle(self):
        formulas = {"C.1.A": "C.1.B + C.1.X", "C.1.B": "C.1.C", "C.1.C": "C.1.A"}
        with self.assertRaisesRegex(fc.FormulaError, "Cyclic"):
            fc.calculation_order(formulas)
        with self.assertRaises(fc.FormulaError):
            fc.compute(formulas, {})
        with self.assertRaises(fc.FormulaError):
            fc.calculation_order({"C.1.A": "C.1.A"})

    def test_validate(self):
        errors = fc.validate(
            {"V.1.A": "V.1.B + V.1.UNKNOWN", "V.1.B": "V.1.X *", "V.1.C": "V.1.D"},
            {"V.1.A", "V.1.B", "V.1.C", "V.1.D", "V.1.X"},
        )
        self.assertEqual(len(errors), 2)
        self.assertIn("V.1.UNKNOWN", errors[0])
        self.assertTrue(errors[1].startswith("V.1.B"))
        self.assertEqual(fc.validate(self.FORMULAS, {"F.1.A", "F.1.B", "F.1.C"}), [])

    def test_negative_codes(self):
        values = {
            "N.1.A": D("-1.00"),
            "N.1.B": D("-2.00"),
            "N.1.C": D("3"),
            "N.1.D": None,
        }
        self.assertEqual(fc.negative_codes(values, {"N.1.B"}), ["N.1.A"])

    def test_drop_empty(self):
        values = {
            "E.1.A": D("0.00"),
            "E.1.B": None,
            "E.1.C": D("1.00"),
            "E.1.M": D("0"),
        }
        self.assertEqual(fc.drop_empty(values), {"E.1.C": D("1.00")})
        self.assertEqual(
            fc.drop_empty(values, keep={"E.1.M"}), {"E.1.C": D("1.00"), "E.1.M": D("0")}
        )

    def test_random_sums(self):
        """Random signed sums agree with direct decimal arithmetic."""
        rng = random.Random(4711)
        for _case in range(500):
            codes = [f"R.1.X{index}" for index in range(rng.randint(1, 12))]
            signs = [rng.choice((1, -1)) for _code in codes]
            amounts = {
                code: D(rng.randint(-(10**7), 10**7)) / 100
                if rng.random() > 0.2
                else None
                for code in codes
            }
            formula = " ".join(
                f"{'+' if sign > 0 else '-'} {code}"
                for sign, code in zip(signs, codes, strict=True)
            )
            result = fc.compute({"R.1.SUM": formula}, amounts)["R.1.SUM"]
            present = [
                sign * amounts[code]
                for sign, code in zip(signs, codes, strict=True)
                if amounts[code] is not None
            ]
            expected = sum(present, D("0.00")) if present else None
            self.assertEqual(result, expected, formula)
