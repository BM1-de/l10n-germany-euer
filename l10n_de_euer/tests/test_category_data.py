# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Categories, mappings per form year and default files (spec 4.1-4.3)."""

from collections import defaultdict
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, new_test_user, tagged

from ..models.euer_account_assignment import default_rows, range_rules
from ..models.euer_category import ROLES_WITHOUT_VALUE

YEARS = (2024, 2025, 2026)
#: tax categories without a field of their own: they follow the base line or
#: are neutral in the document (spec 5.7 no. 4)
TAX_WITHOUT_FIELD = {"VAT_INPUT_NON_DEDUCTIBLE", "VAT_REVERSE_CHARGE"}


@tagged("post_install", "-at_install")
class TestCategoryData(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Category = cls.env["l10n_de_euer.category"]
        cls.categories = {c.code: c for c in cls.Category.search([])}
        cls.forms = {
            year: cls.env["l10n_de_euer.form"]
            ._get_forms_for_year(year)[0]
            .filtered(lambda form: form.form_type == "euer")
            for year in YEARS
        }

    def mappings(self, code, year):
        return self.categories[code]._get_mappings(self.forms[year])

    def summary(self, code, year):
        return sorted(
            (
                m.field_id.code,
                round(m.share, 4),
                m.condition,
                m.option or "",
                m.davon_field_id.code or "",
                m.additional_field_id.code or "",
            )
            for m in self.mappings(code, year)
        )

    def test_every_category_has_fields_per_year(self):
        """Roles with form values have a mapping in every form year (F-01)."""
        self.assertEqual(len(self.categories), 106)
        for code, category in self.categories.items():
            for year in YEARS:
                mappings = self.mappings(code, year)
                with self.subTest(code=code, year=year):
                    if (
                        category.role in ROLES_WITHOUT_VALUE
                        or code in TAX_WITHOUT_FIELD
                    ):
                        self.assertFalse(mappings)
                    else:
                        self.assertTrue(mappings)
                    # depreciation: transfer fields of the AVEÜR, for the
                    # comparison with the ledger only (spec 6.5)
                    types = ("amount", "amount_davon")
                    if category.role == "afa":
                        types += ("transfer",)
                    for mapping in mappings:
                        self.assertEqual(mapping.field_id.form_id, self.forms[year])
                        self.assertIn(mapping.field_id.field_type, types)

    def test_shares_sum_to_one(self):
        totals = defaultdict(float)
        for mapping in self.env["l10n_de_euer.category.mapping"].search([]):
            key = (
                mapping.category_id.code,
                mapping.year,
                mapping.condition,
                mapping.option,
            )
            totals[key] += mapping.share
        for key, total in totals.items():
            with self.subTest(key=key):
                self.assertAlmostEqual(total, 1.0)

    def test_mappings_equal_over_years(self):
        """Codes are stable; 2024-2026 only differ in the lines (F-02)."""
        for code in self.categories:
            with self.subTest(code=code):
                self.assertEqual(self.summary(code, 2024), self.summary(code, 2026))
                self.assertEqual(self.summary(code, 2025), self.summary(code, 2026))

    def test_corrections_of_spec_4_3(self):
        y = 2026
        # 1. entertainment 100 % to 175/165, option 70/30 (R-42)
        self.assertEqual(
            self.summary("EXP_ENTERTAINMENT", y),
            [
                ("EUER.25.165", 0.3, "always", "entertainment_split", "", ""),
                ("EUER.25.175", 0.7, "always", "entertainment_split", "", ""),
                ("EUER.25.175", 1.0, "always", "entertainment_separate", "", ""),
            ],
        )
        self.assertEqual(
            self.summary("EXP_ENTERTAINMENT_NON_DEDUCTIBLE", y),
            [("EUER.25.165", 1.0, "always", "", "", "")],
        )
        # 3. private accounts by side, independent of money (29.122/29.123)
        for code in ("PRIVATE_WITHDRAWAL", "PRIVATE_CONTRIBUTION"):
            self.assertEqual(
                self.summary(code, y),
                [
                    ("EUER.29.122", 1.0, "debit", "", "", ""),
                    ("EUER.29.123", 1.0, "credit", "", "", ""),
                ],
            )
        # 4. § 19a and refunded ancillary tax payments: 103, small business 111/119
        for code in ("REV_S19A", "REV_ANCILLARY_REFUND", "REV_INTEREST"):
            self.assertEqual(
                self.summary(code, y),
                [
                    ("EUER.20.103", 1.0, "regular", "", "", ""),
                    ("EUER.20.111", 1.0, "ku", "", "EUER.20.119", ""),
                ],
            )
        # 8. input VAT only under standard taxation (K-14, R-28)
        self.assertEqual(
            self.summary("VAT_INPUT", y), [("EUER.25.185", 1.0, "regular", "", "", "")]
        )
        # 9. write-off flag (K-15)
        self.assertEqual(
            [code for code, c in self.categories.items() if c.writeoff], ["WRITEOFF"]
        )
        # 10. balance sheet accounts with income or expense role
        self.assertEqual(
            sorted(code for code, c in self.categories.items() if c.on_payment),
            [
                "EXP_ADVANCE_PAID",
                "EXP_CONSTRUCTION_WITHHOLDING",
                "EXP_EXCISE_LIABILITY",
                "EXP_PAYROLL_LIABILITY",
                "REV_ADVANCE_RECEIVED",
            ],
        )

    def test_special_mappings(self):
        y = 2026
        self.assertEqual(
            self.summary("VAT_SETTLEMENT", y),
            [
                ("EUER.20.141", 1.0, "credit", "", "", ""),
                ("EUER.25.186", 1.0, "debit", "", "", ""),
            ],
        )
        self.assertEqual(
            self.summary("VAT_OUTPUT", y),
            [
                ("EUER.20.111", 1.0, "ku", "", "", ""),
                ("EUER.20.140", 1.0, "regular", "", "", ""),
            ],
        )
        self.assertEqual(
            self.summary("EXP_CAR_PRIVATE_USE", y),
            [("EUER.25.147", 1.0, "always", "", "", "EUER.29.123")],
        )
        self.assertEqual(
            self.summary("EXP_PROPERTY_MAINTENANCE", y),
            [("EUER.25.151", 1.0, "always", "", "EUER.25.153", "")],
        )
        # import VAT: small business without deduction (expense, C-30)
        self.assertEqual(
            self.summary("VAT_IMPORT", y),
            [
                ("EUER.25.183", 1.0, "ku", "", "", ""),
                ("EUER.25.185", 1.0, "regular", "", "", ""),
            ],
        )
        # excise duty liability: same field as the excise duty expense
        self.assertEqual(
            self.summary("EXP_EXCISE_LIABILITY", y),
            [("EUER.25.223", 1.0, "always", "", "", "")],
        )
        self.assertEqual(
            self.summary("EXP_GOODS_ON_SALE_PARTIAL_INCOME", y),
            [("EUER.25.100", 1.0, "always", "", "", "")],
        )
        self.assertEqual(
            self.categories["EXP_GOODS_ON_SALE_PARTIAL_INCOME"].scope_status, "blocked"
        )
        self.assertEqual(
            self.summary("REV_KU", y),
            [
                ("EUER.20.111", 1.0, "ku", "", "", ""),
                ("EUER.20.112", 1.0, "regular", "", "", ""),
            ],
        )

    def test_default_files(self):
        """Default categories exist; range rules cover all codes (K-03)."""
        for chart, expected in (("de_skr03", 1274), ("de_skr04", 1192)):
            rows = default_rows(chart)
            template_rows = [row for row in rows if row["code"] and row["xml_id"]]
            self.assertEqual(len(template_rows), expected)
            # DATEV small business revenue accounts by code (K-07)
            self.assertEqual(
                [(row["code"], row["category"]) for row in rows if not row["xml_id"]],
                [("8192" if chart == "de_skr03" else "4184", "REV_KU")],
            )
            for row in rows:
                with self.subTest(chart=chart, xml_id=row["xml_id"]):
                    self.assertIn(row["category"], self.categories)
                    self.assertIn(row["confidence"], ("h", "m", "n"))
            rules = range_rules(chart)
            self.assertEqual(rules[0][0], 0)
            self.assertEqual(rules[-1][1], 9999)
            for first, second in zip(rules, rules[1:], strict=False):
                self.assertEqual(first[1] + 1, second[0])
                # empty: no proposal for the codes of the range
                self.assertIn(second[2], {*self.categories, ""})

    def range_category(self, chart, code):
        return next(
            category
            for first, last, category in range_rules(chart)
            if first <= code <= last
        )

    def default_category(self, chart, code):
        return next(
            (
                row["category"]
                for row in default_rows(chart)
                if row["xml_id"] and row["code"] == f"{code:04d}"
            ),
            None,
        )

    def test_datev_auxiliary_accounts(self):
        """K-08: DATEV auxiliary accounts of the EÜR warn (C-05), whether they
        come with the chart template or are created by the user."""
        codes = {
            "de_skr03": (1370, 1371, 1391, 1449, 1580, 1583, 1609, 1692, 1693)
            + (8580, 8582, 8589, 9287, 9289, 9893, 9902),
            "de_skr04": (1219, 1480, 1483, 1485, 1486, 3309, 4580, 4582, 4589)
            + (9287, 9902),
        }
        for chart, numbers in codes.items():
            for number in numbers:
                with self.subTest(chart=chart, code=number):
                    category = self.default_category(chart, number)
                    if category is None:
                        category = self.range_category(chart, number)
                    self.assertEqual(category, "WARN_DATEV")

    def test_opening_codes(self):
        """K-13: opening only for 9000-9009, 9060-9089 and 9090; no proposal
        for the other codes of the 9xxx area (statistical accounts)."""
        for chart in ("de_skr03", "de_skr04"):
            for number in (9000, 9009, 9060, 9089, 9090):
                with self.subTest(chart=chart, code=number):
                    category = self.default_category(chart, number)
                    self.assertIn(
                        category or self.range_category(chart, number),
                        ("OPENING",),
                    )
            for number in (9010, 9059, 9091, 9500, 9950):
                with self.subTest(chart=chart, code=number):
                    self.assertEqual(self.range_category(chart, number), "")

    def test_review_corrections(self):
        """Corrections of the review: equal accounts in SKR03 and SKR04."""
        expected = {
            # DATEV 4980 is "Sonstiger Betriebsbedarf" like SKR04 6850
            ("de_skr03", 4980): ("EXP_WORK_EQUIPMENT", "n"),
            ("de_skr04", 6850): ("EXP_WORK_EQUIPMENT", "n"),
            ("de_skr03", 2007): ("EXP_OTHER", "n"),
            ("de_skr04", 7553): ("EXP_OTHER", "n"),
            ("de_skr03", 2507): ("WARN_DISPOSAL_BALANCE", "m"),
            ("de_skr04", 7453): ("WARN_DISPOSAL_BALANCE", "m"),
            ("de_skr03", 1725): ("VAT_REVERSE_CHARGE", "m"),
            ("de_skr04", 3865): ("VAT_REVERSE_CHARGE", "m"),
            ("de_skr03", 1783): ("VAT_OUTPUT", "n"),
            ("de_skr04", 3851): ("VAT_OUTPUT", "n"),
            ("de_skr03", 1300): ("WARN_OTHER", "m"),
            ("de_skr04", 1230): ("WARN_OTHER", "m"),
            ("de_skr03", 1722): ("REV_ADVANCE_RECEIVED", "m"),
            ("de_skr04", 1190): ("REV_ADVANCE_RECEIVED", "m"),
            ("de_skr03", 1350): ("CURRENT_ASSET_4_3_4", "h"),
            ("de_skr04", 1390): ("CURRENT_ASSET_4_3_4", "h"),
            ("de_skr03", 1355): ("NEUTRAL", "h"),
            ("de_skr04", 1378): ("NEUTRAL", "h"),
            ("de_skr03", 2328): ("EXP_GOODS_ON_SALE_PARTIAL_INCOME", "n"),
            ("de_skr03", 2742): ("REV_NOT_TAXABLE", "m"),
            ("de_skr04", 4970): ("REV_NOT_TAXABLE", "m"),
            ("de_skr03", 8100): ("REV_EXEMPT_19_2", "n"),
            ("de_skr04", 4100): ("REV_EXEMPT_19_2", "n"),
            ("de_skr03", 9089): ("OPENING", "h"),
            ("de_skr03", 39): ("NEUTRAL", "m"),
            ("de_skr04", 170): ("NEUTRAL", "m"),
            ("de_skr03", 2600): ("REV_NOT_TAXABLE", "n"),
            ("de_skr04", 7020): ("REV_NOT_TAXABLE", "n"),
            ("de_skr04", 9991): ("REV_TAX", "n"),
        }
        rows = {
            chart: {row["code"]: row for row in default_rows(chart) if row["xml_id"]}
            for chart in ("de_skr03", "de_skr04")
        }
        for (chart, number), wanted in expected.items():
            with self.subTest(chart=chart, code=number):
                row = rows[chart][f"{number:04d}"]
                self.assertEqual((row["category"], row["confidence"]), wanted)
        for chart in ("de_skr03", "de_skr04"):
            utility = next(
                row
                for row in default_rows(chart)
                if row["xml_id"] == "default_cash_difference_income_account_id"
            )
            self.assertEqual(
                (utility["category"], utility["confidence"]), ("REV_TAX", "n")
            )
            # open items only on receivable and payable accounts (B-04)
            self.assertEqual(
                rows[chart]["1460" if chart == "de_skr03" else "1241"]["category"],
                "NEUTRAL",
            )

    def test_revenue_fields_records(self):
        form = self.forms[2026]
        category = self.categories["REV_TAX"]
        field, davon = category._get_revenue_fields(form, "regular", ["76"])
        self.assertEqual(field.code, "EUER.20.104")
        self.assertFalse(davon)
        field, davon = self.categories["REV_NOT_TAXABLE"]._get_revenue_fields(
            form, "ku"
        )
        self.assertEqual((field.code, davon.code), ("EUER.20.111", "EUER.20.119"))
        with self.assertRaises(ValidationError):
            self.categories["EXP_TELECOM"]._get_revenue_fields(form, "regular")

    def test_category_constraints(self):
        with self.assertRaises(ValidationError):
            self.Category.create({"code": "X_BE", "name": "X", "role": "be"})
        with self.assertRaises(ValidationError):
            self.Category.create(
                {"code": "X_BA", "name": "X", "role": "ba", "be_art": "stpfl"}
            )
        with self.assertRaises(ValidationError):
            self.Category.create(
                {"code": "X_VAT", "name": "X", "role": "ba", "vat_kind": "input"}
            )

    def test_mapping_constraints_and_lock(self):
        category = self.Category.create({"code": "X_TEST", "name": "X", "role": "ba"})
        form = self.forms[2026]
        field = form.field_ids.filtered(lambda f: f.code == "EUER.25.183")
        mapping = self.env["l10n_de_euer.category.mapping"].create(
            {"category_id": category.id, "form_id": form.id, "field_id": field.id}
        )
        with self.assertRaises(ValidationError):
            self.env["l10n_de_euer.category.mapping"].create(
                {
                    "category_id": category.id,
                    "form_id": form.id,
                    "field_id": field.id,
                    "share": 0.5,
                }
            )
        other_year = self.forms[2025].field_ids.filtered(
            lambda f: f.code == "EUER.25.183"
        )
        with self.assertRaises(ValidationError):
            mapping.davon_field_id = other_year
        with self.assertRaises(ValidationError):
            mapping.field_id = other_year
        # FT-12: a form year used by a finalised statement locks its mappings
        with patch.object(
            type(self.env["l10n_de_euer.form"]),
            "_get_finalized_forms",
            lambda forms: forms.filtered(lambda f: f.year == 2026),
        ):
            with self.assertRaises(UserError):
                mapping.share = 0.5
            with self.assertRaises(UserError):
                mapping.unlink()

    def test_access_rights(self):
        manager = new_test_user(
            self.env, login="euer_cat_manager", groups="account.group_account_manager"
        )
        category = self.categories["EXP_TELECOM"].with_user(manager)
        self.assertEqual(category.name, "Telecommunication")
        with self.assertRaises(AccessError):
            category.name = "Phone"
        mapping = category.mapping_ids[:1]
        with self.assertRaises(AccessError):
            mapping.share = 0.5
