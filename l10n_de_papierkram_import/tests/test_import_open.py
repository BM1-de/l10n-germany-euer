# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Open items only, completed later by a complete import."""

import base64
import os
import tempfile
from datetime import date

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tools import format_date

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from ..models.papierkram_import import _Plan
from .common import KATEGORIEN, make_export


@tagged("post_install_l10n", "post_install", "-at_install")
class TestPapierkramImportOpenItems(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        d = cls.tmp.name
        paths = [
            make_export(
                d,
                2025,
                rechnungen=[
                    "R-00020;Workshop;Muster GmbH;Deutschland;;K-00001;01.03.2025;"
                    "15.03.2025;Bezahlt;Rechnung;100,00;0,00;100,00;0,00;100,00;"
                    "2025-03-05;;;"
                ],
                zahlungen=["Zahlung zu R-00020;K-00001;R-00020;;2025-03-05;100,00"],
            ),
            make_export(
                d,
                2026,
                rechnungen=[
                    # partly paid
                    "R-00021;Workshop;Muster GmbH;Deutschland;;K-00001;01.05.2026;"
                    "15.05.2026;Teilweise bezahlt;Rechnung;300,00;0,00;300,00;0,00;"
                    "100,00;2026-05-10;;;",
                    "R-00022;Lektorat;Muster GmbH;Deutschland;;K-00001;01.06.2026;"
                    "15.06.2026;Unbezahlt;Rechnung;200,00;0,00;200,00;0,00;0,00;;;;",
                ],
                belege=[
                    "B-00030;Toner;Technik AG;02.06.2026;;;unpaid;-50,00;0,00;-50,00;"
                    "0;domestic;Porto;;;;;",
                    "B-00031;Paketmarken;Technik AG;03.06.2026;;2026-06-03;paid;"
                    "-9,95;0,00;-9,95;0;domestic;Porto;;;;;",
                ],
                zahlungen=["Zahlung zu R-00021;K-00001;R-00021;;2026-05-10;100,00"],
            ),
        ]
        cls.attachments = cls.env["ir.attachment"]
        for path in paths:
            with open(path, "rb") as fh:
                cls.attachments |= cls.env["ir.attachment"].create(
                    {
                        "name": os.path.basename(path),
                        "datas": base64.b64encode(fh.read()),
                    }
                )

    def run_import(self, scope):
        run = self.env["papierkram.import"].create(
            {
                "export_attachment_ids": [(6, 0, self.attachments.ids)],
                "categories_file": base64.b64encode(KATEGORIEN.encode()),
                "scope": scope,
            }
        )
        run.action_check()
        run.action_import()
        return run

    def numbers(self, run):
        return sorted(filter(None, run.move_ids.mapped("papierkram_number")))

    def test_open_items_then_complete(self):
        """First only the open documents with their partial payment, then a
        complete import adds the rest without duplicates."""
        first = self.run_import("open")
        self.assertEqual(self.numbers(first), ["B-00030", "R-00021", "R-00022"])
        partly = first.move_ids.filtered(lambda m: m.papierkram_number == "R-00021")
        self.assertEqual(partly.amount_residual, 200.0)
        self.assertEqual(partly.payment_state, "partial")
        payment_dates = partly.line_ids.matched_credit_ids.credit_move_id.mapped("date")
        self.assertEqual(payment_dates, [date(2026, 5, 10)])
        self.assertEqual(
            first.move_ids.filtered(
                lambda m: m.papierkram_number == "B-00030"
            ).amount_residual,
            50.0,
        )
        self.assertFalse(
            first.move_ids.filtered(lambda m: (m.ref or "").startswith("AfA")),
            "no depreciation for open items",
        )
        second = self.run_import("complete")
        self.assertEqual(self.numbers(second), ["B-00031", "R-00020"])
        self.assertEqual(second.sale_journal_id, first.sale_journal_id)

    def test_existing_contact_used(self):
        """A contact of the same name (e.g. shared with another company)
        gets the Papierkram number instead of a duplicate; its own data stay."""
        existing = self.env["res.partner"].create(
            {"name": "MUSTER GmbH", "is_company": True, "ref": "B-7"}
        )
        run = self.run_import("open")
        self.assertEqual(
            run.move_ids.filtered("papierkram_number").partner_id
            - self.env["res.partner"].search([("name", "=", "Technik AG")]),
            existing,
        )
        self.assertEqual(existing.papierkram_number, "K-00001")
        self.assertEqual(existing.ref, "B-7")
        self.assertEqual(
            self.env["res.partner"].search_count([("name", "=ilike", "muster gmbh")]),
            1,
        )

    def test_small_business_with_vat_and_name(self):
        """Small business ticked (the default) but an invoice with VAT: the
        check warns; the default name carries the date in the format of the
        language, not ISO."""
        path = make_export(
            self.tmp.name,
            2024,
            rechnungen=[
                "R-00040;Workshop;Muster GmbH;Deutschland;;K-00001;01.03.2024;"
                "15.03.2024;Unbezahlt;Rechnung;100,00;19,00;119,00;0,00;0,00;;;;"
            ],
        )
        with open(path, "rb") as fh:
            attachment = self.env["ir.attachment"].create(
                {"name": os.path.basename(path), "datas": base64.b64encode(fh.read())}
            )
        run = self.env["papierkram.import"].create(
            {
                "export_attachment_ids": [(6, 0, attachment.ids)],
                "categories_file": base64.b64encode(KATEGORIEN.encode()),
            }
        )
        today = fields.Date.today()
        self.assertNotIn(today.isoformat(), run.name)
        self.assertIn(format_date(self.env, today), run.name)
        self.assertTrue(run.small_business)
        run.action_check()
        self.assertIn("invoices show VAT (R-00040)", str(run.log))
        run.small_business = False
        run.action_check()
        self.assertNotIn("invoices show VAT", str(run.log))

    def test_errors_and_actions(self):
        """Without export, with a file that is no export, a second run and a
        blocked import the user gets a message; the action lists the moves."""
        run = self.env["papierkram.import"].create({})
        with self.assertRaisesRegex(UserError, "upload at least one"):
            run.action_check()
        broken = self.env["ir.attachment"].create(
            {"name": "kaputt.zip", "datas": base64.b64encode(b"no zip")}
        )
        run.export_attachment_ids = broken
        with self.assertRaises(UserError):
            run.action_check()
        path = make_export(
            self.tmp.name,
            2023,
            belege=[
                "B-00070;X;Technik AG;02.06.2023;;;unpaid;-50,00;0,00;-50,00;0;"
                "domestic;Kategorie ohne Konto;;;;;",
            ],
        )
        with open(path, "rb") as fh:
            attachment = self.env["ir.attachment"].create(
                {"name": os.path.basename(path), "datas": base64.b64encode(fh.read())}
            )
        blocked = self.env["papierkram.import"].create(
            {
                "export_attachment_ids": [(6, 0, attachment.ids)],
                "categories_file": base64.b64encode(KATEGORIEN.encode()),
            }
        )
        with self.assertRaisesRegex(UserError, "blocked"):
            blocked.action_import()
        done = self.run_import("open")
        with self.assertRaisesRegex(UserError, "already been run"):
            done.action_import()
        action = done.action_view_moves()
        self.assertEqual(action["res_model"], "account.move")
        self.assertEqual(action["domain"], [("papierkram_import_id", "=", done.id)])


@tagged("post_install_l10n", "post_install", "-at_install")
class TestPapierkramImportSkr04(AccountTestInvoicingCommon):
    """SKR04: the SKR03 accounts of Papierkram are found by the table of
    differing accounts or by the German name of the template account."""

    @classmethod
    @AccountTestInvoicingCommon.setup_chart_template("de_skr04")
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        path = make_export(
            cls.tmp.name,
            2025,
            rechnungen=[
                "R-00080;Workshop;Muster GmbH;Österreich;;K-00001;01.03.2025;"
                "15.03.2025;Bezahlt;Rechnung;100,00;0,00;100,00;0,00;100,00;"
                "2025-03-05;;;"
            ],
            belege=[
                "B-00080;Paketmarken;Technik AG;03.03.2025;;2025-03-03;paid;-9,95;"
                "0,00;-9,95;0;domestic;Porto;;;;;",
                "B-00081;Bürobedarf;Technik AG;04.03.2025;;2025-03-04;paid;-20,00;"
                "0,00;-20,00;0;domestic;Betriebsbedarf;;;;;",
            ],
            zahlungen=["Zahlung zu R-00080;K-00001;R-00080;;2025-03-05;100,00"],
            # the partner takes its country from the contact (Kontakte.csv)
            customer_country="Österreich",
        )
        with open(path, "rb") as fh:
            cls.attachment = cls.env["ir.attachment"].create(
                {"name": os.path.basename(path), "datas": base64.b64encode(fh.read())}
            )

    def test_import_on_skr04(self):
        categories = (
            KATEGORIEN + "Betriebsbedarf;4980;Betriebsbedarf;EXPENSE;;EXPENSE\n"
        )
        run = self.env["papierkram.import"].create(
            {
                "export_attachment_ids": [(6, 0, self.attachment.ids)],
                "categories_file": base64.b64encode(categories.encode()),
            }
        )
        run.action_check()
        run.action_import()
        moves = run.move_ids
        invoice = moves.filtered(lambda m: m.papierkram_number == "R-00080")
        self.assertEqual(invoice.partner_id.country_id.code, "AT")
        self.assertEqual(invoice.invoice_line_ids.account_id.code, "4185")
        postage = moves.filtered(lambda m: m.papierkram_number == "B-00080")
        self.assertEqual(postage.invoice_line_ids.account_id.code, "6800")
        supplies = moves.filtered(lambda m: m.papierkram_number == "B-00081")
        # Papierkram's 4980 means leasing in today's chart: booked on 4985
        # (SKR04 6845, by the name of the template account)
        self.assertEqual(supplies.invoice_line_ids.account_id.code, "6845")
        self.assertIn("instead of", str(run.log))

    def test_accounts_without_a_single_skr04_counterpart(self):
        """An SKR03 account without template name, or whose name several SKR04
        accounts carry (0110 Garagen), is not guessed."""
        run = self.env["papierkram.import"].create(
            {"export_attachment_ids": [(6, 0, self.attachment.ids)]}
        )
        export = run._read_export()
        self.addCleanup(export.close)
        plan = _Plan(run, export)
        self.assertFalse(plan._account("0001", raise_missing=False))
        self.assertFalse(plan._account("0110", raise_missing=False))
        self.assertTrue(plan._account("4910", raise_missing=False))
