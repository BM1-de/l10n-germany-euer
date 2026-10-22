# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Unit tests of the revenue field rules (spec 4.4, R-26, R-29), no database."""

from odoo.tests import BaseCase

from ..models.engine import revenue as rv

KU = rv.FIELD_KU
DAVON = rv.FIELD_KU_DAVON
TAXABLE = rv.FIELD_TAXABLE
EXEMPT = rv.FIELD_EXEMPT
LUF = rv.FIELD_LUF


class TestRevenueFields(BaseCase):
    def test_tag_numbers(self):
        self.assertEqual(
            rv.tag_numbers(["81_BASE", "-86_TAX", "+45_BASE", "60rc", "", None, "x"]),
            {"81", "86", "45", "60"},
        )

    def test_field_from_tags(self):
        """R-29 including the corrections 76 → 104 and 49 → 103 (KT-06)."""
        cases = {
            ("81_BASE",): TAXABLE,
            ("86_BASE",): TAXABLE,
            ("35",): TAXABLE,
            ("76",): LUF,
            ("77",): LUF,
            ("49",): EXEMPT,
            ("41",): EXEMPT,
            ("42",): EXEMPT,
            ("43",): EXEMPT,
            ("44",): EXEMPT,
            ("48",): EXEMPT,
            ("60",): EXEMPT,
            ("87",): EXEMPT,
            ("21",): EXEMPT,
            ("45_BASE",): EXEMPT,
            ("89_BASE",): None,
            (): None,
        }
        for tags, field in cases.items():
            with self.subTest(tags=tags):
                self.assertEqual(rv.field_from_tags(tags), field)

    def test_field_from_several_tags(self):
        self.assertEqual(rv.field_from_tags(["81_BASE", "41"]), TAXABLE)
        self.assertEqual(rv.field_from_tags(["76", "81_BASE"]), LUF)

    def test_regular(self):
        self.assertEqual(
            rv.revenue_fields("stpfl", "regular", (), TAXABLE), (TAXABLE, None)
        )
        self.assertEqual(
            rv.revenue_fields("nstb", "regular", ("21",), EXEMPT), (EXEMPT, None)
        )
        self.assertEqual(
            rv.revenue_fields("tax", "regular", ("76",), TAXABLE), (LUF, None)
        )
        self.assertEqual(
            rv.revenue_fields("tax", "regular", ("49",), TAXABLE), (EXEMPT, None)
        )
        # no tags: default of the category (4.4 no. 4)
        self.assertEqual(
            rv.revenue_fields("tax", "regular", (), TAXABLE), (TAXABLE, None)
        )
        # small business revenue account under standard taxation: net field,
        # the tax rate input of 4.4 no. 5 is applied by the calculation
        self.assertEqual(
            rv.revenue_fields("ku", "regular", ("21",), TAXABLE), (TAXABLE, None)
        )

    def test_small_business(self):
        """R-26: Kz 111, of which 119 for not taxable, § 19 (2), § 19a, interest."""
        for be_art in ("nstb", "frei192", "hilfs", "s19a"):
            with self.subTest(be_art=be_art):
                self.assertEqual(
                    rv.revenue_fields(be_art, "ku", (), EXEMPT), (KU, DAVON)
                )
        for be_art in ("stpfl", "frei", "tax"):
            with self.subTest(be_art=be_art):
                self.assertEqual(
                    rv.revenue_fields(be_art, "ku", ("81_BASE",)), (KU, None)
                )
        # tag "not taxable" of the base line
        self.assertEqual(rv.revenue_fields("tax", "ku", ("45_BASE",)), (KU, DAVON))

    def test_small_business_account_ignores_tax_free_eu(self):
        """K-06: tag 21 of the default tax of 8195/4185 does not mean 119."""
        self.assertEqual(rv.revenue_fields("ku", "ku", ("21",), TAXABLE), (KU, None))

    def test_fixed_fields(self):
        """Asset sales, private car use and withdrawals stay in their field."""
        for regime in ("ku", "regular"):
            with self.subTest(regime=regime):
                self.assertEqual(
                    rv.revenue_fields("fixed", regime, ("81_BASE",), "EUER.20.102"),
                    ("EUER.20.102", None),
                )
                self.assertEqual(
                    rv.revenue_fields("luf24", regime, (), LUF), (LUF, None)
                )

    def test_unknown_regime(self):
        with self.assertRaises(ValueError):
            rv.revenue_fields("stpfl", None, (), TAXABLE)

    def test_small_business_tax_under_standard_taxation(self):
        """Tag 87 of the Odoo small business tax is not tax-exempt (4.4 no. 5).

        After exceeding the limit, invoices with "0 % USt (Pflichtbefreit z.B.
        als Kleinunt. ...)" are net revenue after the VAT rate input, not
        Kz 103; tag 87 of other taxes stays tax-exempt (R-29).
        """
        self.assertEqual(
            rv.revenue_fields("tax", "regular", ("87",), TAXABLE, ku_tax=True),
            (TAXABLE, None),
        )
        self.assertEqual(
            rv.revenue_fields("tax", "regular", ("87",), TAXABLE), (EXEMPT, None)
        )
        self.assertEqual(
            rv.revenue_fields("tax", "ku", ("87",), TAXABLE, ku_tax=True), (KU, None)
        )
        self.assertTrue(
            rv.needs_tax_rate_input("tax", "regular", ("87",), True, ku_tax=True)
        )
        self.assertFalse(rv.needs_tax_rate_input("tax", "regular", ("87",), True))
        self.assertFalse(
            rv.needs_tax_rate_input("tax", "ku", ("87",), True, ku_tax=True)
        )

    def test_needs_tax_rate_input(self):
        cases = [
            # small business revenue account under standard taxation
            (("ku", "regular", (), False), True),
            # taxable category without tax and without exempt tag
            (("stpfl", "regular", (), False), True),
            (("tax", "regular", ("81_BASE",), False), True),
            (("tax", "regular", ("48",), False), False),
            (("tax", "regular", ("81_BASE",), True), False),
            (("nstb", "regular", (), False), False),
            (("fixed", "regular", (), False), False),
            (("stpfl", "ku", (), False), False),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(rv.needs_tax_rate_input(*args), expected)

    def test_is_ku_tax(self):
        for xml_id in (
            "account.1_tax_ust_no_ustpflicht_skr03",
            "account.12_tax_ust_no_ustpflicht_skr04",
            "3_tax_ust_no_ustpflicht_skr03",
        ):
            self.assertTrue(rv.is_ku_tax(xml_id), xml_id)
        for xml_id in ("account.1_tax_free_eu_skr03", "", None, "tax_ust_19_skr03"):
            self.assertFalse(rv.is_ku_tax(xml_id), xml_id)
