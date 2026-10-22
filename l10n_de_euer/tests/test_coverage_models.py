# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Protection of the results of a calculation and of a finalised statement
(spec 7.7, GoBD; review E5): slices, check results, the stored payload and
the form years used by it. Synthetic data (D6)."""

import base64

from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from ..models.euer_check import INTERNAL as CHECK_INTERNAL
from .scenario_common import SKR03_ACCOUNTS, SKR03_TAXES, ScenarioCommon
from .test_statement_review import ReviewCommon


@tagged("post_install_l10n", "post_install", "-at_install")
class TestSliceProtection(ScenarioCommon, AccountTestInvoicingCommon):
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_euer()

    def test_slices_belong_to_the_calculation(self):
        """Not even the superuser deletes or changes a slice outside the
        calculation; computing the year again replaces them."""
        invoice = self.sale("2026-03-15", 100)
        self.bank("2026-03-20", 100, matches=[(invoice, 100)])
        self.env["l10n_de_euer.run"]._compute_year(self.company, 2026)
        slices = self.env["l10n_de_euer.cash.slice"].search(
            [("company_id", "=", self.company.id)]
        )
        self.assertTrue(slices)
        with self.assertRaises(UserError):
            slices.sudo().unlink()
        with self.assertRaises(UserError):
            slices.sudo().write({"amount": 1.0})
        self.assertTrue(slices.exists())


@tagged("post_install_l10n", "post_install", "-at_install")
class TestFinalisedStatementProtection(ReviewCommon, AccountTestInvoicingCommon):
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_finalisation()

    def test_payload_keeps_its_content(self):
        """G-04, G-10: the payload of a finalisation may get a new access
        token and be written again with the same content (moving between
        database and file store), never with another content or name."""
        final = self.finalized()
        attachment = final.payload_attachment_id
        raw = attachment.raw
        attachment.write({"access_token": "e5-coverage"})
        attachment.write({"raw": raw})
        attachment.write({"datas": base64.b64encode(raw).decode()})
        self.assertEqual(attachment.raw, raw)
        with self.assertRaises(UserError):
            attachment.write({"raw": raw + b" "})
        with self.assertRaises(UserError):
            attachment.write({"name": "anders.json"})
        with self.assertRaises(UserError):
            attachment.unlink()

    def test_form_year_of_a_finalised_statement_is_locked(self):
        """A mapping line cannot be moved into a form year that a finalised
        statement uses (spec 3.4: corrections are new versions)."""
        final = self.finalized()
        locked = final.form_ids.filtered(lambda form: form.form_type == "euer")
        self.assertTrue(locked.locked)
        mapping = self.env["l10n_de_euer.category.mapping"].search(
            [("form_id.year", "=", 2025), ("form_id.form_type", "=", "euer")],
            limit=1,
        )
        with self.assertRaises(UserError):
            mapping.sudo().write({"form_id": locked.id})

    def test_check_results_belong_to_the_checks(self):
        """Check results are deleted by the checks only; a result on records
        without own action opens its records."""
        Check = self.env["l10n_de_euer.check"].sudo()
        check = Check.with_context(**{CHECK_INTERNAL: True}).create(
            {
                "company_id": self.company.id,
                "year": 2026,
                "code": "C-33",
                "severity": "blocker",
                "message": "VAT rate missing",
                "res_model": "res.partner",
                "res_ids": [self.partner_a.id],
            }
        )
        action = check.action_resolve()
        self.assertEqual(action["res_model"], "res.partner")
        with self.assertRaises(UserError):
            check.with_context(**{CHECK_INTERNAL: False}).unlink()

    def test_field_names(self):
        """A form field is shown with its code and name."""
        field = self.env["l10n_de_euer.field"].search(
            [("code", "=", "EUER.20.112"), ("year", "=", 2026)], limit=1
        )
        self.assertTrue(field.display_name.startswith("EUER.20.112 "))
