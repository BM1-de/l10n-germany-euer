# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Constraints, write protection and access rights of the form models."""

from decimal import Decimal
from unittest.mock import patch

from psycopg2 import IntegrityError

from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger


@tagged("post_install", "-at_install")
class TestFormModel(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Form = cls.env["l10n_de_euer.form"]
        cls.Field = cls.env["l10n_de_euer.field"]
        # a fictitious form year far in the future, independent of the data
        cls.form = cls.Form.create({"year": 2099, "form_type": "euer"})
        cls.aveuer = cls.Form.create({"year": 2099, "form_type": "aveuer"})
        cls.income = cls.make_field("EUER.20.111", kz="111", line=12)
        cls.other = cls.make_field("EUER.20.112", kz="112", line=15)
        cls.depreciation = cls.make_field(
            "AVEUER.40.404", form=cls.aveuer, kz="404", line=44
        )

    @classmethod
    def make_field(cls, code, form=None, formula=False, **vals):
        box = code.split(".")[1]
        return cls.Field.create(
            {
                "form_id": (form or cls.form).id,
                "code": code,
                "box": box,
                "name": code,
                "official_label": f"Line {code}",
                "formula": formula,
                **vals,
            }
        )

    def test_form_name(self):
        self.assertEqual(self.form.name, "Anlage EÜR 2099 (v1)")
        self.assertEqual(self.form.field_count, 2)
        action = self.form.action_open_fields()
        self.assertEqual(action["domain"], [("form_id", "=", self.form.id)])

    def test_formula_constraint(self):
        total = self.make_field(
            "EUER.20.159", formula="EUER.20.111 + EUER.20.112", field_type="total"
        )
        transfer = self.make_field("EUER.25.130", formula="AVEUER.40.404")
        self.assertEqual(transfer.formula, "AVEUER.40.404")
        for formula in ("EUER.20.111 *", "EUER.20.999", "1 + eval(1)"):
            with self.subTest(formula=formula), self.assertRaises(ValidationError):
                total.formula = formula
        with self.assertRaises(ValidationError):
            self.make_field("EUER.G.UEB", formula="EUER.G.UEB + EUER.20.111")
        cycle = self.make_field("EUER.G.BE", formula="EUER.20.159")
        with self.assertRaises(ValidationError):
            total.formula = "EUER.G.BE + EUER.20.111"
        self.assertTrue(cycle.exists())

    def test_formula_integrity(self):
        """Renaming or deleting a field used by a formula fails (no silent 0)."""
        total = self.make_field("EUER.20.159", formula="EUER.20.111 + EUER.20.112")
        with self.assertRaises(ValidationError):
            self.income.write({"code": "EUER.20.999", "kz": "999"})
        with self.assertRaises(UserError):
            self.income.unlink()
        total.formula = "EUER.20.112"
        self.income.unlink()
        self.assertEqual(
            self.form._calculate({"EUER.20.112": 5})["EUER.20.159"], Decimal("5.00")
        )
        # a formula with a dangling code (written past the constraints) is a
        # calculation error, not an empty value
        self.env.cr.execute(
            "UPDATE l10n_de_euer_field SET formula = %s WHERE id = %s",
            ("EUER.20.112 + EUER.20.777", total.id),
        )
        total.invalidate_recordset(["formula"])
        with self.assertRaises(UserError):
            self.form._calculate({"EUER.20.112": 5})

    def test_fill_condition(self):
        """A conditional form uses codes of other forms of the year only."""
        self.make_field("EUER.25.234", kz="234", line=57)
        sz = self.Form.create({"year": 2099, "form_type": "sz"})
        base = self.make_field(
            "SZ.43.SOCKEL", form=sz, formula="2050.00", field_type="constant"
        )
        interest = self.make_field("SZ.43.ZINS", form=sz, formula="EUER.25.234")
        for condition in ("SZ.43.SOCKEL - 1", "EUER.25.999 - 1", "EUER.25.234 *"):
            with self.subTest(condition=condition), self.assertRaises(ValidationError):
                sz.fill_condition = condition
        sz.fill_condition = "EUER.25.234 - 2050.00"
        forms = self.form | sz
        values = forms._calculate({"EUER.25.234": 2000})
        self.assertIsNone(values[base.code])
        self.assertIsNone(values[interest.code])
        values = forms._calculate({"EUER.25.234": 2100})
        self.assertEqual(values[interest.code], Decimal("2100.00"))
        self.assertEqual(values[base.code], Decimal("2050.00"))
        # the condition must not depend on the form through formulas
        self.make_field("SZ.43.CARRY", form=sz)
        condition_field = self.Field.search(
            [("code", "=", "EUER.25.234"), ("year", "=", 2099)]
        )
        with self.assertRaises(ValidationError):
            condition_field.write({"formula": "SZ.43.CARRY", "field_type": "transfer"})
        with self.assertRaises(UserError):
            condition_field.unlink()

    def test_formula_other_year_unknown(self):
        """Formulas only use codes of the same assessment year."""
        form_2098 = self.Form.create({"year": 2098, "form_type": "euer"})
        with self.assertRaises(ValidationError):
            self.make_field("EUER.20.159", form=form_2098, formula="EUER.20.111")

    def test_code_constraint(self):
        for code, box, kz in (
            ("AVEUER.40.490", "40", "490"),
            ("EUER.20.113", "25", "113"),
            ("EUER.20.114", "20", "999"),
            ("EUER.20", "20", False),
            ("euer.20.115", "20", "115"),
        ):
            with self.subTest(code=code), self.assertRaises(ValidationError):
                self.Field.create(
                    {
                        "form_id": self.form.id,
                        "code": code,
                        "box": box,
                        "kz": kz,
                        "name": "x",
                        "official_label": "x",
                    }
                )

    @mute_logger("odoo.sql_db")
    def test_unique_code_per_form(self):
        with self.assertRaises(IntegrityError):
            self.make_field("EUER.20.111", kz="111", line=12)

    @mute_logger("odoo.sql_db")
    def test_unique_form_version(self):
        with self.assertRaises(IntegrityError):
            self.Form.create({"year": 2099, "form_type": "euer"})

    def test_calculate(self):
        self.make_field("EUER.20.159", formula="EUER.20.111 + EUER.20.112")
        self.make_field("EUER.25.130", formula="AVEUER.40.404")
        forms = self.form | self.aveuer
        values = forms._calculate({"EUER.20.111": 100, "AVEUER.40.404": "33.33"})
        self.assertEqual(values["EUER.20.159"], Decimal("100.00"))
        self.assertEqual(values["EUER.25.130"], Decimal("33.33"))
        with self.assertRaises(UserError):
            forms._calculate({"EUER.99.999": 1})
        self.make_field("EUER.15.120", kz="120", field_type="key")
        with self.assertRaises(UserError):
            forms._calculate({"EUER.15.120": 1})
        self.assertIsNone(forms._calculate({})["EUER.20.112"])
        self.assertNotIn("EUER.15.120", forms._calculate({}))
        with self.assertRaises(UserError):
            forms._calculate({"EUER.20.159": 1})
        values = forms._calculate({"EUER.20.111": 1}, overrides={"EUER.20.159": 5})
        self.assertEqual(values["EUER.20.159"], Decimal("5.00"))
        other_year = self.Form.create({"year": 2098, "form_type": "sz"})
        with self.assertRaises(UserError):
            (forms | other_year)._calculate({})
        version2 = self.Form.create({"year": 2099, "form_type": "euer", "version": 2})
        with self.assertRaises(UserError):
            (forms | version2)._calculate({})

    def test_lock(self):
        """F-15: a form used by a finalised statement can no longer change."""
        model = type(self.Form)
        with patch.object(
            model, "_get_finalized_forms", lambda forms: forms & self.form
        ):
            self.form.invalidate_recordset(["locked"])
            self.assertTrue(self.form.locked)
            self.assertFalse(self.aveuer.locked)
            with self.assertRaises(UserError):
                self.form.verification_note = "changed"
            with self.assertRaises(UserError):
                self.income.name = "changed"
            with self.assertRaises(UserError):
                self.make_field("EUER.20.103", kz="103", line=16)
            with self.assertRaises(UserError):
                self.income.unlink()
            with self.assertRaises(UserError):
                self.form.unlink()
            with self.assertRaises(UserError):
                self.depreciation.form_id = self.form
            # a new version may still supersede the locked one
            version2 = self.Form.create(
                {"year": 2099, "form_type": "euer", "version": 2}
            )
            self.form.superseded_by_id = version2
            self.assertEqual(self.form.superseded_by_id, version2)
            self.aveuer.verification_note = "allowed"
        self.form.invalidate_recordset(["locked"])
        self.assertFalse(self.form.locked)

    def test_access_rights(self):
        """Accountants read the form data; only system administrators change it."""
        reader = new_test_user(
            self.env, login="euer_reader", groups="account.group_account_readonly"
        )
        manager = new_test_user(
            self.env, login="euer_manager", groups="account.group_account_manager"
        )
        for user in (reader, manager):
            form = self.form.with_user(user)
            self.assertEqual(form.name, "Anlage EÜR 2099 (v1)")
            self.assertTrue(form.field_ids)
            parameter = self.env["l10n_de_euer.parameter"].with_user(user)
            self.assertEqual(parameter._get_value("gwg_limit", 2026), 800)
            with self.assertRaises(AccessError):
                form.verification_note = "changed"
            with self.assertRaises(AccessError):
                self.income.with_user(user).formula = "EUER.20.112"
            with self.assertRaises(AccessError):
                parameter.search([("code", "=", "gwg_limit")], limit=1).value = 1000
        nobody = new_test_user(self.env, login="euer_nobody", groups="base.group_user")
        with self.assertRaises(AccessError):
            self.form.with_user(nobody).read(["name"])
