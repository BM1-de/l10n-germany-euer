# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Form and data tests FT-01 to FT-11 (spec 9.3) on the delivered form years."""

import csv
import os
from decimal import Decimal

from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from ..models.engine import form_calc

D = Decimal
YEARS = (2024, 2025, 2026)
FORMS = {"Anlage EÜR": "euer", "Anlage AVEÜR": "aveuer", "Anlage SZ": "sz"}
TYPES = {
    "Text": "text",
    "Schlüssel": "key",
    "Datum": "date",
    "Betrag": "amount",
    "Betrag-davon": "amount_davon",
    "Übernahme": "transfer",
    "Zwischensumme": "subtotal",
    "Summe": "total",
    "Konstante": "constant",
}
SIGNS = {"+": "+", "−": "-", "±": "±", "=": "=", "0": "0", "−/+": "±", "": False}
# SHA-256 of the evaluated BMF letters (spec F-01)
SOURCE_SHA256 = {
    2024: "8d1384f1a43f646b79a8be53a254d940828e0431cc7042145b9274ea871d45a8",
    2025: "b69b5cf0a982d28cbce20644e67677a36be0bc494bed4fae2310dc08230a1599",
    2026: "03d3033acc5c5a126729e58743128596ae39193759c55b207872da9432b83a8f",
}
BMF_DATES = {2024: "2024-09-02", 2025: "2025-08-29", 2026: "2026-09-01"}


def snapshot_rows():
    path = os.path.join(os.path.dirname(__file__), "data", "form_tables.csv")
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter=";"))


@tagged("post_install", "-at_install")
class TestFormData(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Form = cls.env["l10n_de_euer.form"]
        cls.forms = {}
        cls.fields = {}
        for year in YEARS:
            forms, provisional = cls.Form._get_forms_for_year(year)
            assert not provisional
            cls.forms[year] = forms
            cls.fields[year] = {field.code: field for field in forms.field_ids}
        cls.snapshot = snapshot_rows()

    def calculate(self, year, amounts):
        return self.forms[year]._calculate(amounts)

    # FT-01 -----------------------------------------------------------------

    def test_ft01_form_years(self):
        for year in YEARS:
            forms = self.forms[year]
            self.assertEqual(
                sorted(forms.mapped("form_type")), ["aveuer", "euer", "sz"]
            )
            for form in forms:
                with self.subTest(form=form.name):
                    self.assertEqual(form.year, year)
                    self.assertEqual(form.version, 1)
                    self.assertEqual(form.state, "draft")
                    self.assertFalse(form.verified_by)
                    self.assertEqual(form.source_sha256, SOURCE_SHA256[year])
                    self.assertTrue(form.source_url.startswith("https://"))
                    self.assertEqual(str(form.bmf_date), BMF_DATES[year])
                    self.assertTrue(form.bmf_reference.startswith("IV C 6 - S 2142/"))
                    self.assertTrue(form.bmf_dok)
                    self.assertTrue(form.instructions_version)
                    self.assertTrue(form.vordruck_ids.startswith(f"{year}Anl"))
                    self.assertFalse(form.locked)

    def test_ft01_fields_complete(self):
        """Every line of the official tables exists once with line, Kz and text."""
        seen = {year: set() for year in YEARS}
        counts = {}
        for row in self.snapshot:
            year, form_type = int(row["jahr"]), FORMS[row["formular"]]
            counts[(year, form_type)] = counts.get((year, form_type), 0) + 1
            matches = [
                field
                for field in self.forms[year].field_ids
                if field.form_type == form_type
                and field.line == int(row["zeile"])
                and (field.kz or "") == row["kennzahl"]
                and field.official_label == row["bezeichnung"].strip()
            ]
            with self.subTest(row=row):
                self.assertEqual(len(matches), 1)
                field = matches[0]
                self.assertEqual(field.field_type, TYPES[row["typ"]])
                self.assertEqual(field.sign, SIGNS[row["vorzeichen"]])
                self.assertEqual(
                    field.box == "G", "Ermittlung des Gewinns" in row["abschnitt"]
                )
                self.assertEqual(field.box == "00", row["abschnitt"].startswith("00"))
                seen[year].add(field.id)
        self.assertEqual(
            counts,
            {
                (2024, "euer"): 114,
                (2024, "aveuer"): 95,
                (2024, "sz"): 30,
                (2025, "euer"): 114,
                (2025, "aveuer"): 96,
                (2025, "sz"): 30,
                (2026, "euer"): 115,
                (2026, "aveuer"): 96,
                (2026, "sz"): 30,
            },
        )
        for year in YEARS:
            self.assertEqual(seen[year], set(self.forms[year].field_ids.ids))

    def test_ft01_verification_needs_release(self):
        """F-11: verified only with name, role and date of the reviewer."""
        form = self.forms[2026].filtered(lambda f: f.form_type == "euer")
        with self.assertRaises(ValidationError):
            form.write({"state": "verified"})
        with self.assertRaises(ValidationError):
            form.write({"state": "verified", "verified_by": "Tax Advisor Example"})
        form.write(
            {
                "state": "verified",
                "verified_by": "Tax Advisor Example",
                "verified_role": "tax_advisor",
                "verified_on": "2026-11-02",
            }
        )
        self.assertEqual(form.state, "verified")

    # FT-02 -----------------------------------------------------------------

    def test_ft02_codes(self):
        prefixes = {"euer": "EUER", "aveuer": "AVEUER", "sz": "SZ"}
        for year in YEARS:
            for form in self.forms[year]:
                codes = form.field_ids.mapped("code")
                self.assertEqual(len(codes), len(set(codes)), form.name)
                for field in form.field_ids:
                    prefix, box, key = field.code.split(".")
                    self.assertEqual(prefix, prefixes[form.form_type])
                    self.assertEqual(box, field.box)
                    if field.kz:
                        self.assertEqual(key, field.kz)

    def test_ft02_kz_collisions_in_different_boxes(self):
        for year in YEARS:
            euer = self.forms[year].field_ids.filtered(lambda f: f.form_type == "euer")
            for kz in ("100", "103", "104", "111", "120", "123", "131", "132", "187"):
                with self.subTest(year=year, kz=kz):
                    boxes = euer.filtered(lambda f, k=kz: f.kz == k).mapped("box")
                    self.assertGreaterEqual(len(boxes), 2)
                    self.assertEqual(len(boxes), len(set(boxes)))
            # Kz 123: profit surcharge (G) and contributions (29)
            self.assertIn("EUER.G.123", self.fields[year])
            self.assertIn("EUER.29.123", self.fields[year])

    def test_ft02_profit_determination_in_box_g(self):
        lines = {2024: (76, 98), 2025: (76, 98), 2026: (77, 99)}
        for row in self.snapshot:
            year = int(row["jahr"])
            if (
                row["formular"] != "Anlage EÜR"
                or "Ermittlung des Gewinns" not in (row["abschnitt"])
            ):
                continue
            first, last = lines[year]
            self.assertTrue(first <= int(row["zeile"]) <= last)
            if row["kennzahl"]:
                with self.subTest(year=year, kz=row["kennzahl"]):
                    self.assertIn(f"EUER.G.{row['kennzahl']}", self.fields[year])
                    self.assertNotIn(f"EUER.25.{row['kennzahl']}", self.fields[year])
        for year in YEARS:
            # regular expense codes of box 25 stay untouched
            self.assertIn("EUER.25.280", self.fields[year])
            self.assertIn("EUER.25.281", self.fields[year])
            g_lines = [
                f.line
                for f in self.fields[year].values()
                if f.code.startswith("EUER.G.")
            ]
            self.assertEqual(min(g_lines), lines[year][0])

    # FT-03 -----------------------------------------------------------------

    def test_ft03_davon_fields_not_in_sums(self):
        for year in YEARS:
            fields_ = self.forms[year].field_ids
            used = form_calc.codes_of(fields_.filtered("formula").mapped("formula"))
            davon = fields_.filtered(lambda f: f.field_type == "amount_davon")
            self.assertEqual(
                sorted(davon.filtered(lambda f: f.form_type == "euer").mapped("code")),
                sorted(
                    [
                        "EUER.20.119",
                        "EUER.25.153",
                        "EUER.25.164",
                        "EUER.25.165",
                        "EUER.25.162",
                        "EUER.25.168",
                        "EUER.G.263",
                        "EUER.G.261",
                    ]
                ),
            )
            self.assertFalse(set(davon.mapped("code")) & used)

    def test_ft03_formulas(self):
        income = {
            f"EUER.20.{key}": 1
            for key in ("111", "104", "112", "103", "140", "141", "102", "106", "108")
        }
        income["EUER.20.Z22_RUECKL"] = 1
        expenses = {
            f"EUER.25.{key}": 1
            for key in ("174", "175", "171", "172", "163", "177")
            + ("144", "145", "146", "147", "176")
        }
        expenses.update({"EUER.25.UEB2": 1, "EUER.25.142": -1, "EUER.25.139": -1})
        for year in YEARS:
            fields_ = self.fields[year]
            with self.subTest(year=year):
                signed = form_calc.signed_references
                self.assertEqual(signed(fields_["EUER.20.159"].formula), income)
                self.assertEqual(signed(fields_["EUER.25.199"].formula), expenses)
                ueb1 = form_calc.references(fields_["EUER.25.UEB1"].formula)
                ueb2 = form_calc.references(fields_["EUER.25.UEB2"].formula)
                self.assertIn("EUER.25.UEB1", ueb2)
                self.assertNotIn("EUER.25.153", ueb2)
                if year == 2026:
                    # F-03: goods moved below carry-forward 1 (line 29)
                    self.assertIn("EUER.25.Z27_TIERBEW", ueb1)
                    self.assertNotIn("EUER.25.100", ueb1)
                    self.assertIn("EUER.25.100", ueb2)
                else:
                    self.assertNotIn("EUER.25.Z27_TIERBEW", fields_)
                    self.assertIn("EUER.25.100", ueb1)
                    self.assertNotIn("EUER.25.100", ueb2)
                self.assertEqual(
                    signed(fields_["EUER.G.UEB"].formula),
                    {
                        "EUER.G.BE": 1,
                        "EUER.G.BA": -1,
                        "EUER.G.240": -1,
                        "EUER.G.241": -1,
                        "EUER.G.242": -1,
                        "EUER.G.243": 1,
                        "EUER.G.244": 1,
                        "EUER.G.245": 1,
                        "EUER.G.180": 1,
                        "EUER.G.181": 1,
                        "EUER.G.182": 1,
                        "EUER.G.123": 1,
                        "EUER.G.187": -1,
                        "EUER.G.250": 1,
                    },
                )
                self.assertEqual(fields_["EUER.G.BE"].formula, "EUER.20.159")
                self.assertEqual(fields_["EUER.G.BA"].formula, "EUER.25.199")
                self.assertEqual(
                    fields_["EUER.G.290"].formula, "EUER.G.UEB + EUER.G.255"
                )
                self.assertEqual(
                    fields_["EUER.G.293"].formula,
                    "EUER.G.290 + EUER.G.264 + EUER.G.262",
                )
                self.assertEqual(
                    fields_["EUER.G.219"].formula, "EUER.G.293 + EUER.G.271"
                )

    def test_ft03_profit_small_business(self):
        """Synthetic small business case: same profit in every form year."""
        amounts = {
            "EUER.20.111": "18500.00",
            "EUER.20.119": "1200.00",  # of which, not added
            "EUER.20.102": "150.00",
            "EUER.25.100": "1000.00",  # goods: carry-forward 1 or 2 by year
            "EUER.25.229": "312.40",
            "EUER.25.280": "480.00",
            "EUER.25.223": "211.50",
            "EUER.25.153": "99.00",  # of which, not added
            "AVEUER.40.414": "333.33",  # depreciation office equipment
            "EUER.25.132": "650.00",
            "EUER.25.174": "40.00",
            "EUER.25.164": "60.00",  # non-deductible, informational
            "EUER.25.175": "70.00",
            "EUER.25.165": "30.00",  # non-deductible, informational
            "EUER.25.142": "100.00",
            "EUER.25.176": "80.00",
            "EUER.G.240": "50.00",
            "EUER.29.122": "2000.00",  # no effect on profit
            "EUER.29.123": "100.00",
        }
        income = D("18500.00") + D("150.00")
        expenses = (
            D("1000.00")
            + D("312.40")
            + D("480.00")
            + D("211.50")
            + D("333.33")
            + D("650.00")
            + D("40.00")
            + D("70.00")
            - D("100.00")
            + D("80.00")
        )
        for year in YEARS:
            with self.subTest(year=year):
                values = self.calculate(year, amounts)
                self.assertEqual(values["EUER.20.159"], D("18650.00"))
                self.assertEqual(values["EUER.25.130"], D("333.33"))
                self.assertEqual(values["EUER.25.199"], expenses)
                self.assertEqual(values["EUER.25.199"], D("3077.23"))
                self.assertEqual(values["EUER.G.219"], income - expenses - D("50.00"))
                self.assertEqual(values["EUER.G.219"], D("15522.77"))
                self.assertIsNone(values["EUER.G.271"])

    def test_ft03_profit_regular_taxation(self):
        amounts = {
            "EUER.20.112": "10000.00",
            "EUER.20.140": "1900.00",
            "EUER.20.141": "120.50",
            "EUER.25.110": "2500.00",
            "EUER.25.185": "475.00",
            "EUER.25.186": "1300.00",
            "EUER.25.234": "2500.00",
        }
        for year in YEARS:
            with self.subTest(year=year):
                values = self.calculate(year, amounts)
                self.assertEqual(values["EUER.20.159"], D("12020.50"))
                self.assertEqual(values["EUER.25.199"], D("6775.00"))
                # no excess withdrawals: no addition (§ 4 (4a) EStG)
                self.assertEqual(values["SZ.43.HOECHSTBETRAG"], D("450.00"))
                self.assertEqual(values["SZ.43.ZINS_6PROZ"], D("0.00"))
                self.assertEqual(values["EUER.G.271"], D("0.00"))
                self.assertEqual(values["EUER.G.219"], D("5245.50"))

    def test_ft03_interest_addition(self):
        """Anlage SZ lines 25, 29, 30: addition of 6 % of excess withdrawals."""
        amounts = {
            "EUER.20.112": "10000.00",
            "EUER.20.140": "1900.00",
            "EUER.20.141": "120.50",
            # profit 5,000.00 before the addition
            "EUER.25.110": "2745.50",
            "EUER.25.185": "475.00",
            "EUER.25.186": "1300.00",
            "EUER.25.234": "2500.00",
            "EUER.29.122": "20000.00",
        }
        for year in YEARS:
            with self.subTest(year=year):
                forms = self.forms[year]
                values = self.calculate(year, amounts)
                self.assertEqual(values["SZ.43.UEBERENTN_KUM"], D("15000.00"))
                self.assertEqual(values["SZ.43.ENTN_UEBERSCHUSS"], D("20000.00"))
                # 6 % of the lower amount 15,000.00 = 900.00
                self.assertEqual(values["SZ.43.ZINS_6PROZ"], D("900.00"))
                self.assertEqual(values["SZ.43.HOECHSTBETRAG"], D("450.00"))
                self.assertEqual(values["SZ.43.HINZURECHNUNG"], D("450.00"))
                self.assertEqual(values["EUER.G.271"], D("450.00"))
                self.assertEqual(values["EUER.G.219"], D("5450.00"))
                # C-41: Anlage SZ is outside the program scope of version 1
                results = forms._check_values(values, amounts)
                self.assertEqual([r["code"] for r in results], ["C-41"])
                self.assertEqual(results[0]["severity"], "blocker")
                self.assertEqual(results[0]["amount"], 2500.0)

    def test_ft03_sz_only_above_base_amount(self):
        """Anlage SZ is filled in only with other interest above 2,050 € (F-14)."""
        sz = {
            year: self.forms[year].filtered(lambda f: f.form_type == "sz")
            for year in YEARS
        }
        for year in YEARS:
            with self.subTest(year=year):
                self.assertEqual(sz[year].fill_condition, "EUER.25.234 - 2050.00")
                for interest in (None, "2050.00"):
                    amounts = {"EUER.20.111": "5000.00", "EUER.29.122": "9000.00"}
                    if interest:
                        amounts["EUER.25.234"] = interest
                    values = self.calculate(year, amounts)
                    sz_values = {
                        code: value
                        for code, value in values.items()
                        if code.startswith("SZ.") and value is not None
                    }
                    self.assertEqual(sz_values, {})
                    self.assertIsNone(values["EUER.G.271"])
                    self.assertFalse(self.forms[year]._check_values(values, amounts))
                values = self.calculate(year, {"EUER.25.234": "2050.01"})
                self.assertEqual(values["SZ.43.HOECHSTBETRAG"], D("0.01"))

    def test_ft03_loss(self):
        for year in YEARS:
            values = self.calculate(
                year, {"EUER.20.111": "1000.00", "EUER.25.229": "2500.55"}
            )
            self.assertEqual(values["EUER.G.219"], D("-1500.55"))
            allowed = (
                self.forms[year].field_ids.filtered("allow_negative").mapped("code")
            )
            self.assertEqual(form_calc.negative_codes(values, allowed), [])

    # FT-04 -----------------------------------------------------------------

    def test_ft04_transfers_aveuer_to_euer(self):
        """F-04: transfers from Anlage AVEÜR into Anlage EÜR in every year."""
        disposals = ("105", "115", "125", "205", "215", "325", "405", "415")
        disposals += ("425", "505", "515")
        amounts = {
            "AVEUER.40.114": "1000.00",
            "AVEUER.40.124": "200.00",
            "AVEUER.40.324": "300.00",
            "AVEUER.40.404": "400.00",
            "AVEUER.40.414": "500.00",
            "AVEUER.40.424": "600.00",
            "AVEUER.40.113": "60.00",
            "AVEUER.40.403": "70.00",
            "AVEUER.40.413": "80.00",
            "AVEUER.40.423": "90.00",
            "AVEUER.40.434": "20.00",
            "AVEUER.40.444": "30.00",
            "AVEUER.40.454": "40.00",
            "AVEUER.40.464": "50.00",
            "AVEUER.40.474": "60.00",
        }
        for index, kz in enumerate(disposals, start=1):
            amounts[f"AVEUER.40.{kz}"] = str(index)
        for year in YEARS:
            with self.subTest(year=year):
                values = self.calculate(year, amounts)
                self.assertEqual(values["AVEUER.40.190"], D("1200.00"))
                self.assertEqual(values["EUER.25.136"], values["AVEUER.40.190"])
                self.assertEqual(values["EUER.25.131"], D("300.00"))
                self.assertEqual(values["AVEUER.40.490"], D("1500.00"))
                self.assertEqual(values["EUER.25.130"], values["AVEUER.40.490"])
                self.assertEqual(values["AVEUER.40.480"], D("240.00"))
                self.assertEqual(values["EUER.25.134"], D("300.00"))
                self.assertEqual(values["AVEUER.40.499"], D("200.00"))
                self.assertEqual(values["EUER.25.137"], values["AVEUER.40.499"])
                self.assertEqual(values["EUER.25.135"], D("66.00"))
                self.assertEqual(
                    values["EUER.25.199"],
                    D("1200.00")
                    + D("300.00")
                    + D("1500.00")
                    + D("300.00")
                    + D("266.00"),
                )

    def test_ft04_book_values(self):
        values = self.calculate(
            2026,
            {
                "AVEUER.40.411": "1000.00",
                "AVEUER.40.412": "-100.00",  # subsequent reduction of cost
                "AVEUER.40.413": "50.00",
                "AVEUER.40.414": "150.00",
                "AVEUER.40.415": "200.00",
                "AVEUER.40.600": "300.00",
                "AVEUER.40.602": "50.00",
                "AVEUER.40.605": "120.00",
            },
        )
        self.assertEqual(values["AVEUER.40.416"], D("500.00"))
        self.assertEqual(values["AVEUER.40.606"], D("230.00"))

    # FT-05 -----------------------------------------------------------------

    def test_ft05_relative_years(self):
        pools = {"43": 0, "44": -1, "45": -2, "46": -3, "47": -4}
        for year in YEARS:
            for field in self.fields[year].values():
                if field.form_type != "aveuer" or not field.kz:
                    continue
                if field.kz[:2] in pools and field.kz != "499":
                    with self.subTest(year=year, code=field.code):
                        relative = pools[field.kz[:2]]
                        self.assertEqual(field.relative_year, relative)
                        # the year is the heading of the block, not line text
                        self.assertNotIn("Sammelposten", field.official_label)
            for code, relative in (
                ("EUER.G.180", -3),
                ("EUER.G.181", -2),
                ("EUER.G.182", -1),
            ):
                field = self.fields[year][code]
                self.assertEqual(field.relative_year, relative)
                self.assertIn(f"aus {year + relative} (", field.official_label)
            self.assertEqual(self.fields[year]["AVEUER.40.499"].relative_year, 0)

    # FT-06 -----------------------------------------------------------------

    def test_ft06_negative_values(self):
        official = {
            "EUER.G.250",
            "EUER.G.255",
            "EUER.G.262",
            "EUER.G.264",
            "EUER.G.261",
            "EUER.G.263",
            "SZ.43.153",
            "SZ.43.154",
            "SZ.43.405",
        }
        results = {"EUER.G.UEB", "EUER.G.290", "EUER.G.293", "EUER.G.219"}
        never = {"EUER.20.111", "EUER.20.159", "EUER.25.130", "EUER.25.199"}
        never |= {"EUER.29.122", "EUER.29.123", "EUER.25.183", "AVEUER.40.490"}
        for year in YEARS:
            fields_ = self.forms[year].field_ids
            allowed = set(fields_.filtered("allow_negative").mapped("code"))
            with self.subTest(year=year):
                self.assertTrue(official | results <= allowed)
                self.assertFalse(never & allowed)
                # subsequent reductions of cost are negative additions
                self.assertIn("AVEUER.40.412", allowed)
                # results, AVEÜR additions (subsequent reductions) and the
                # carry-forward of Anlage SZ may be negative as well (F-06)
                for code in ("EUER.G.PERSGES", "AVEUER.40.102", "AVEUER.40.602"):
                    self.assertIn(code, allowed)
                self.assertIn("SZ.43.315", allowed)
                amounts = {"EUER.20.111": "-10.00", "EUER.G.250": "-20.00"}
                values = self.calculate(year, amounts)
                self.assertEqual(
                    form_calc.negative_codes(values, allowed),
                    ["EUER.20.111", "EUER.20.159", "EUER.G.BE"],
                )
                # C-71 on the flag of the field; C-40 for the input of Kz 250
                checks = self.forms[year]._check_values(values, amounts)
                self.assertEqual(
                    [(r["code"], r["res_model"]) for r in checks],
                    [("C-40", "l10n_de_euer.field")]
                    + [("C-71", "l10n_de_euer.field")] * 3,
                )

    # FT-07 -----------------------------------------------------------------

    def test_ft07_empty_fields(self):
        """R-46: fields without data stay empty, never 0.00."""
        for year in YEARS:
            values = self.calculate(year, {})
            filled = {
                code: value for code, value in values.items() if value is not None
            }
            # not even the pre-printed 2,050.00: Anlage SZ is not filled in
            self.assertEqual(filled, {})
            values = self.calculate(year, {"EUER.20.111": "0.00", "EUER.25.229": "100"})
            shown = form_calc.drop_empty(values, keep=["EUER.15.120"])
            self.assertNotIn("EUER.20.111", shown)
            self.assertNotIn("EUER.20.159", shown)
            self.assertNotIn("EUER.25.130", shown)
            self.assertEqual(shown["EUER.25.199"], D("100.00"))
            self.assertEqual(shown["EUER.G.219"], D("-100.00"))
            mandatory = self.forms[year].field_ids.filtered("mandatory").mapped("code")
            self.assertEqual(mandatory, ["EUER.15.120"])

    def test_ft07_small_business_outputs(self):
        """Small business without interest: only Anlage EÜR and AVEÜR values."""
        amounts = {
            "EUER.20.111": "18500.00",
            "EUER.20.119": "300.00",
            "EUER.25.229": "312.40",
            "AVEUER.40.411": "1000.00",
            "AVEUER.40.414": "333.33",
            "EUER.29.122": "12000.00",
            "EUER.29.123": "500.00",
        }
        for year in YEARS:
            with self.subTest(year=year):
                shown = form_calc.drop_empty(self.calculate(year, amounts))
                self.assertFalse([code for code in shown if code.startswith("SZ.")])
                self.assertNotIn("EUER.G.PERSGES", shown)
                self.assertNotIn("EUER.G.271", shown)
                self.assertEqual(shown["EUER.G.219"], D("17854.27"))
                self.assertFalse(
                    self.forms[year]._check_values(
                        self.calculate(year, amounts), amounts
                    )
                )

    def test_ft07_finalisation_needs_verified_forms(self):
        """F-11, D7: draft form years and provisional statements block (C-72)."""
        forms = self.forms[2026]
        issues = forms._finalization_issues()
        self.assertEqual([issue["code"] for issue in issues], ["C-72"])
        self.assertEqual(sorted(issues[0]["res_ids"]), sorted(forms.ids))
        issues = forms._finalization_issues(provisional=True)
        self.assertEqual([issue["code"] for issue in issues], ["C-72", "C-72"])
        forms.write(
            {
                "state": "verified",
                "verified_by": "Tax Advisor Example",
                "verified_role": "tax_advisor",
                "verified_on": "2026-11-02",
            }
        )
        self.assertEqual(forms._finalization_issues(), [])

    # FT-08 -----------------------------------------------------------------

    def test_ft08_official_label_not_translatable(self):
        model = self.env["l10n_de_euer.field"]
        self.assertFalse(model._fields["official_label"].translate)
        self.assertTrue(model._fields["name"].translate)
        self.env["res.lang"]._activate_lang("de_DE")
        self.env["ir.module.module"]._load_module_terms(["l10n_de_euer"], ["de_DE"])
        field = self.fields[2026]["EUER.20.111"]
        german = field.with_context(lang="de_DE")
        english = field.with_context(lang="en_US")
        self.assertEqual(english.name, "Revenue as small business")
        self.assertEqual(german.name, "BE Kleinunternehmer")
        self.assertEqual(german.official_label, english.official_label)
        self.assertTrue(german.official_label.startswith("Betriebseinnahmen als"))

    def test_ft08_official_wording(self):
        """F-09: the label is the wording of the form, not a paraphrase."""
        labels = {
            year: {code: field.official_label for code, field in fields_.items()}
            for year, fields_ in self.fields.items()
        }
        for year in YEARS:
            with self.subTest(year=year):
                self.assertEqual(
                    labels[year]["EUER.20.112"],
                    "Umsatzsteuerpflichtige Betriebseinnahmen (ohne solche zum "
                    "ermäßigten Steuersatz von 0 %)",
                )
                self.assertEqual(
                    labels[year]["EUER.25.172"],
                    "Aufwendungen für ein häusliches Arbeitszimmer (siehe Anleitung)",
                )
                self.assertEqual(labels[year]["EUER.25.164"], "Geschenke")
                self.assertEqual(labels[year]["EUER.25.174"], "Geschenke")
                self.assertTrue(
                    labels[year]["EUER.25.120"].endswith(
                        "(z. B. Gehälter, Löhne und Versicherungsbeiträge)"
                    )
                )
                self.assertIn("Wirtschaftsgüter", labels[year]["AVEUER.40.490"])
                self.assertNotIn("netto", labels[year]["EUER.20.112"])
        # line lists that move with the form
        self.assertIn("45, 52, 59, 85, 90", labels[2024]["EUER.25.135"])
        self.assertIn("45, 53, 60, 86, 91", labels[2025]["EUER.25.135"])
        self.assertIn("45, 53, 60, 86, 91", labels[2026]["EUER.25.135"])
        self.assertIn("Zeile 31 der Anlage EÜR", labels[2025]["AVEUER.40.190"])
        self.assertIn("Zeile 32 der Anlage EÜR", labels[2026]["AVEUER.40.190"])

    # FT-09 -----------------------------------------------------------------

    def test_ft09_elster_ids(self):
        """F-10: no ELSTER field IDs in the public data; verified needs an ID."""
        for year in YEARS:
            fields_ = self.forms[year].field_ids
            self.assertFalse(fields_.filtered("elster_field_id"))
            self.assertFalse(fields_.filtered("elster_verified"))
        field = self.fields[2026]["EUER.20.111"]
        with self.assertRaises(ValidationError):
            field.elster_verified = True
        field.write({"elster_field_id": "E0000000", "elster_verified": True})
        self.assertTrue(field.elster_verified)

    # FT-10 -----------------------------------------------------------------

    def test_ft10_missing_year_is_provisional(self):
        forms, provisional = self.Form._get_forms_for_year(2027)
        self.assertTrue(provisional)
        self.assertEqual(set(forms.mapped("year")), {2026})
        self.assertEqual(len(forms), 3)
        forms, provisional = self.Form._get_forms_for_year(2026)
        self.assertFalse(provisional)
        with self.assertRaises(UserError):
            self.Form._get_forms_for_year(2023)

    def test_ft10_version_selection(self):
        euer = self.forms[2026].filtered(lambda f: f.form_type == "euer")
        version2 = euer.copy({"version": 2, "field_ids": False})
        forms, _provisional = self.Form._get_forms_for_year(2026)
        self.assertIn(version2, forms)
        self.assertNotIn(euer, forms)
        euer.write(
            {
                "state": "verified",
                "verified_by": "Second Maintainer Example",
                "verified_role": "second_maintainer",
                "verified_on": "2026-11-02",
            }
        )
        forms, _provisional = self.Form._get_forms_for_year(2026)
        self.assertIn(euer, forms)
        self.assertNotIn(version2, forms)
        euer.superseded_by_id = version2
        with self.assertRaises(ValidationError):
            version2.superseded_by_id = euer

    # FT-11 -----------------------------------------------------------------

    def lines(self, year, form_type):
        return {
            code: field.line
            for code, field in self.fields[year].items()
            if field.form_type == form_type
        }

    def test_ft11_line_shifts_euer(self):
        lines_2024 = self.lines(2024, "euer")
        lines_2025 = self.lines(2025, "euer")
        lines_2026 = self.lines(2026, "euer")
        self.assertEqual(lines_2024, lines_2025)
        self.assertEqual(set(lines_2026) - set(lines_2025), {"EUER.25.Z27_TIERBEW"})
        self.assertEqual(lines_2026["EUER.25.Z27_TIERBEW"], 27)
        for code, line in lines_2025.items():
            with self.subTest(code=code):
                if code == "EUER.25.100":
                    self.assertEqual((line, lines_2026[code]), (27, 29))
                elif line < 27 or code == "EUER.25.UEB1":
                    self.assertEqual(lines_2026[code], line)
                else:
                    self.assertEqual(lines_2026[code], line + 1)
        # reference lines of spec F-03 and the instructions
        self.assertEqual(lines_2026["EUER.25.130"], 34)
        self.assertEqual(lines_2026["EUER.25.183"], 61)
        self.assertEqual(lines_2026["EUER.25.UEB1"], 28)
        self.assertEqual(lines_2026["EUER.25.UEB2"], 62)
        self.assertEqual(lines_2025["EUER.25.UEB2"], 61)

    def test_ft11_line_shifts_aveuer_and_sz(self):
        lines_2024 = self.lines(2024, "aveuer")
        lines_2025 = self.lines(2025, "aveuer")
        self.assertEqual(set(lines_2025) - set(lines_2024), {"AVEUER.40.407"})
        self.assertEqual(lines_2025["AVEUER.40.407"], 47)
        for code, line in lines_2024.items():
            with self.subTest(code=code):
                self.assertEqual(lines_2025[code], line if line < 47 else line + 1)
        self.assertEqual(lines_2024["AVEUER.40.490"], 62)
        self.assertEqual(lines_2025["AVEUER.40.490"], 63)
        self.assertEqual(lines_2025, self.lines(2026, "aveuer"))
        self.assertEqual(self.lines(2024, "sz"), self.lines(2025, "sz"))
        self.assertEqual(self.lines(2025, "sz"), self.lines(2026, "sz"))
