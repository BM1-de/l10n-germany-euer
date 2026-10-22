# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Import of fictitious Papierkram time entries and services."""

import base64
import os
import tempfile
import zipfile
from datetime import date
from decimal import Decimal

from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.l10n_de_papierkram_import.tests.common import (
    KATEGORIEN,
    make_export,
)

from ..lib.timesheets import parse_duration, parse_time_entry_file

H_TIME = (
    "Datum;Beginn;Ende;Dauer (hh:mm);Dauer (abrechenbar) (hh:mm);Nicht-Abrechenbar;"
    "Zeiterfasser;Kunde;Projekt;Aufgabe;Dienstleistung;Preis pro Stunde;Betrag ();"
    "Kommentar"
)
BILLED = (
    "10.01.2026;09:00;11:00;2:00;2:00;nein;Erika Test;Muster GmbH;Kein Projekt;"
    "Konzeption;Lektorat (70);70,0;140,0;Altes Thema"
)
OPEN_A = (
    "05.03.2026;10:00;10:45;0:45;0:45;nein;Erika Test;Muster GmbH;Kein Projekt;"
    "Konzeption;Lektorat (70);70,0;52,5;Offen A"
)
OPEN_B = (
    "06.03.2026;08:00;09:30;1:30;1:30;nein;Erika Test;Muster GmbH;Kein Projekt;"
    "Konzeption;Lektorat (85);85,0;127,5;Offen B"
)
H_PRODUCTS = (
    "Name;Artikelnummer;Beschreibung;Typ;Netto-Preis;Brutto-Preis;"
    "Einheitbezeichnung Einzel;Einheitbezeichnung Mehrfach;USt.-Satz"
)
PRODUCTS = [
    "Lektorat (70);;Konzeption;Dienstleistung;70,00;83,30;Stunde;Stunden;19",
    "Kilometergeld;;je km;Dienstleistung;0,35;0,42;Stunde;Stunden;19",
    "Tagessatz;T-1;Ganzer Tag;Dienstleistung;500,00;595,00;Tag;Tage;19",
]


@tagged("post_install_l10n", "post_install", "-at_install")
class TestTimesheetImport(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        path = make_export(cls.tmp.name, 2026)
        with zipfile.ZipFile(path, "a") as zf:
            zf.writestr(
                "CSV/Zeiteinträge.csv", "\n".join((H_TIME, BILLED, OPEN_A, OPEN_B))
            )
            zf.writestr(
                "CSV/Waren_oder_Dienstleistungen.csv",
                "\n".join((H_PRODUCTS, *PRODUCTS)),
            )
        with open(path, "rb") as fh:
            cls.export = cls.env["ir.attachment"].create(
                {"name": os.path.basename(path), "datas": base64.b64encode(fh.read())}
            )
        cls.unbilled = base64.b64encode("\n".join((H_TIME, OPEN_A, OPEN_B)).encode())
        cls.env.user.group_ids |= (
            cls.env.ref("sales_team.group_sale_salesman")
            | cls.env.ref("project.group_project_manager")
            | cls.env.ref("hr_timesheet.group_hr_timesheet_user")
            | cls.env.ref("hr.group_hr_user")
        )

    def run_import(self, **values):
        run = self.env["papierkram.import"].create(
            {
                "export_attachment_ids": [(6, 0, self.export.ids)],
                "categories_file": base64.b64encode(KATEGORIEN.encode()),
                "unbilled_file": self.unbilled,
                **values,
            }
        )
        run.action_check()
        run.action_import()
        return run

    def timesheets(self, run):
        return self.env["account.analytic.line"].search(
            [("papierkram_import_id", "=", run.id)], order="date"
        )

    def test_parse_duration(self):
        self.assertEqual(parse_duration("1:07"), Decimal(1) + Decimal(7) / 60)
        self.assertEqual(parse_duration("45m"), Decimal("0.75"))
        self.assertEqual(parse_duration(""), Decimal(0))
        entries = parse_time_entry_file("\n".join((H_TIME, OPEN_A, OPEN_A)))
        self.assertEqual(len({entry.key for entry in entries}), 2)

    def test_unbilled_entries_billable_through_order(self):
        """Two unbilled entries at 70 € and 85 €: one order with two lines;
        the invoice bills 0.75 h × 70 + 1.5 h × 85 = 180.00 €."""
        run = self.run_import()
        lines = self.timesheets(run)
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines.mapped("unit_amount"), [0.75, 1.5])
        project = lines.project_id
        self.assertEqual(project.name, "Retainer (Muster GmbH)")
        self.assertEqual(project.partner_id.papierkram_number, "K-00001")
        self.assertEqual(lines.task_id.name, "Konzeption")
        order = run.sale_order_ids
        self.assertEqual(len(order), 1)
        self.assertEqual(order.state, "sale")
        self.assertEqual(
            sorted(
                order.order_line.mapped(lambda sol: (sol.price_unit, sol.qty_delivered))
            ),
            [(70.0, 0.75), (85.0, 1.5)],
        )
        invoice = order._create_invoices()
        self.assertAlmostEqual(invoice.amount_untaxed, 180.0)
        self.assertFalse(invoice.invoice_line_ids.tax_ids)
        # a later time entry on the task is billed on the main line
        later = self.env["account.analytic.line"].create(
            {
                "date": date(2026, 4, 1),
                "name": "Neu",
                "project_id": project.id,
                "task_id": lines.task_id.id,
                "employee_id": lines.employee_id.id,
                "unit_amount": 1.0,
            }
        )
        self.assertEqual(later.so_line, lines.task_id.sale_line_id)
        self.assertTrue(later.so_line)

    def test_products(self):
        self.run_import()
        Product = self.env["product.product"]
        hourly = Product.search([("name", "=", "Lektorat (70)")])
        self.assertEqual(hourly.service_policy, "delivered_timesheet")
        self.assertEqual(hourly.uom_id, self.env.ref("uom.product_uom_hour"))
        self.assertEqual(hourly.list_price, 70.0)
        travel = Product.search([("name", "=", "Kilometergeld")])
        self.assertNotEqual(travel.service_policy, "delivered_timesheet")
        day = Product.search([("default_code", "=", "T-1")])
        self.assertEqual(day.uom_id, self.env.ref("uom.product_uom_day"))
        self.assertNotEqual(day.service_policy, "delivered_timesheet")

    def test_all_entries_only_unbilled_billable(self):
        run = self.run_import(time_entries="all")
        lines = self.timesheets(run)
        self.assertEqual(len(lines), 3)
        self.assertFalse(
            lines.filtered(lambda line: line.name == "Altes Thema").so_line
        )
        self.assertEqual(len(lines.filtered("so_line")), 2)

    def test_rerun_skips_present_entries(self):
        first = self.run_import()
        second = self.run_import(time_entries="all")
        self.assertEqual(len(self.timesheets(first)), 2)
        self.assertEqual(self.timesheets(second).mapped("name"), ["Altes Thema"])
        self.assertFalse(second.sale_order_ids)
        self.assertEqual(
            self.env["project.project"].search_count([("papierkram_key", "!=", False)]),
            1,
        )

    def test_without_file_no_time_entries(self):
        run = self.env["papierkram.import"].create(
            {
                "export_attachment_ids": [(6, 0, self.export.ids)],
                "categories_file": base64.b64encode(KATEGORIEN.encode()),
            }
        )
        run.action_check()
        self.assertIn("No file with unbilled time entries", run.log)
        run.action_import()
        self.assertFalse(self.timesheets(run))
        self.assertFalse(run.sale_order_ids)

    def test_buttons_count_and_list_the_result(self):
        """The buttons of the run lead to its sales orders and its time
        entries; a run that is not saved yet counts none."""
        run = self.run_import()
        entries = self.timesheets(run)
        self.assertTrue(run.sale_order_ids and entries)
        self.assertEqual(run.sale_order_count, len(run.sale_order_ids))
        self.assertEqual(run.timesheet_count, len(entries))
        orders = run.action_view_sale_orders()
        self.assertEqual(orders["domain"], [("id", "in", run.sale_order_ids.ids)])
        self.assertEqual(orders["context"], {})
        shown = run.action_view_timesheets()
        self.assertEqual(shown["domain"], [("papierkram_import_id", "=", run.id)])
        self.assertEqual(shown["context"], {})
        draft = self.env["papierkram.import"].new({"name": "Entwurf"})
        self.assertEqual(draft.timesheet_count, 0)
        self.assertEqual(draft.sale_order_count, 0)
