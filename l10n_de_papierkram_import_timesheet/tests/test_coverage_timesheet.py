# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Further cases of the time entry import: regular VAT, entries that are
only in the file, no time entries, missing rights and the parsers."""

import base64
import os
import tempfile
import zipfile
from decimal import Decimal
from types import SimpleNamespace

from odoo.tests import BaseCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.l10n_de_papierkram_import.tests.common import (
    KATEGORIEN,
    make_export,
)

from ..lib.timesheets import parse_duration, parse_products, parse_time_entries
from .test_timesheet_import import BILLED, H_PRODUCTS, H_TIME, OPEN_A, PRODUCTS


@tagged("post_install", "-at_install")
class TestTimesheetParsers(BaseCase):
    def test_durations_in_hours(self):
        self.assertEqual(parse_duration("2h"), Decimal(2))
        self.assertEqual(parse_duration("1,5h"), Decimal("1.5"))
        with self.assertRaises(ValueError):
            parse_duration("anderthalb")

    def test_rows_without_date_or_name_are_skipped(self):
        rows = [{"Datum": "", "Dauer (hh:mm)": "1:00"}, {"Datum": " "}]
        self.assertEqual(parse_time_entries(rows), [])
        self.assertEqual(parse_products([{"Name": ""}, {"Name": "  "}]), [])


@tagged("post_install_l10n", "post_install", "-at_install")
class TestTimesheetImportCases(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        path = make_export(cls.tmp.name, 2026)
        with zipfile.ZipFile(path, "a") as zf:
            zf.writestr("CSV/Zeiteinträge.csv", "\n".join((H_TIME, BILLED, OPEN_A)))
            zf.writestr(
                "CSV/Waren_oder_Dienstleistungen.csv",
                "\n".join((H_PRODUCTS, *PRODUCTS)),
            )
        with open(path, "rb") as fh:
            cls.export = cls.env["ir.attachment"].create(
                {"name": os.path.basename(path), "datas": base64.b64encode(fh.read())}
            )
        # recorded by the user of Odoo for a customer that is no contact of
        # the export; only in the file of unbilled time entries
        cls.only_in_file = (
            f"07.03.2026;14:00;15:00;1:00;1:00;nein;{cls.env.user.name};"
            "Fremdkunde AG;Kein Projekt;Konzeption;Lektorat (70);70,0;70,0;Nur Datei"
        )
        cls.unbilled = base64.b64encode(
            "\n".join((H_TIME, OPEN_A, cls.only_in_file)).encode()
        )
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

    def test_all_entries_with_regular_vat(self):
        """An unbilled entry missing in the exports comes from the file; its
        customer is created by name, its employee belongs to the user; the
        products of a business with regular VAT carry the sales tax."""
        run = self.run_import(time_entries="all", small_business=False)
        self.assertIn("1 unbilled time entries are not in the exports", run.log)
        lines = self.timesheets(run)
        self.assertEqual(len(lines), 3)
        only = lines.filtered(lambda line: line.name == "Nur Datei")
        self.assertTrue(only.so_line)
        self.assertEqual(only.project_id.partner_id.name, "Fremdkunde AG")
        self.assertFalse(only.project_id.partner_id.papierkram_number)
        self.assertEqual(only.employee_id.user_id, self.env.user)
        self.assertEqual(only.employee_id.company_id, self.env.company)
        product = self.env["product.product"].search([("name", "=", "Lektorat (70)")])
        self.assertEqual(product.taxes_id.amount, 19.0)

    def test_no_time_entries_and_no_products(self):
        run = self.run_import(time_entries="none", import_products=False)
        self.assertNotIn("No file with unbilled time entries", run.log)
        self.assertFalse(self.timesheets(run))
        self.assertFalse(run.sale_order_ids)
        self.assertFalse(
            self.env["product.product"].search([("default_code", "=", "T-1")])
        )

    def test_rights_to_create_are_checked(self):
        """Without the rights of sales, projects and employees the import of
        time entries is blocked before anything is created."""
        user = new_test_user(
            self.env,
            login="pk_accountant",
            groups="account.group_account_manager",
            company_id=self.env.company.id,
            company_ids=[(6, 0, self.env.company.ids)],
        )
        run = self.env["papierkram.import"].create(
            {"unbilled_file": self.unbilled, "time_entries": "unbilled"}
        )
        plan = SimpleNamespace(blockers=[], warnings=[])
        run.with_user(user)._check_extra(plan)
        self.assertEqual(len(plan.blockers), 1)
        self.assertIn("needs the right to create", plan.blockers[0])
        self.assertIn("Sales Order", plan.blockers[0])
