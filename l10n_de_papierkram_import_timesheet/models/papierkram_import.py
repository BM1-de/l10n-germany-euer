# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Products, projects, tasks and time entries of a Papierkram import.

Time entries without project ("Kein Projekt") go into one project per
customer. Time entries that are not billed yet in Papierkram (the CSV of
the list of time entries with the filter "Noch nicht abgerechnet") get a
sales order per customer with a line per service and rate: "Create Invoice"
on the order bills them in Odoo, like every later time entry on the same
task.
"""

import base64
from collections import defaultdict

from odoo import Command, api, fields, models

from ..lib.timesheets import (
    parse_products,
    parse_time_entries,
    parse_time_entry_file,
)

TIMESHEET_CSV = "Zeiteinträge.csv"
PRODUCT_CSV = "Waren_oder_Dienstleistungen.csv"
UOM_REFS = {
    "hour": "uom.product_uom_hour",
    "day": "uom.product_uom_day",
    "unit": "uom.product_uom_unit",
}


class PapierkramImport(models.Model):
    _inherit = "papierkram.import"

    time_entries = fields.Selection(
        [
            ("none", "None"),
            ("unbilled", "Unbilled Time Entries"),
            ("all", "All Time Entries"),
        ],
        default="unbilled",
        required=True,
        help="Unbilled: only the time entries of the file below, billable in Odoo "
        "through a sales order per customer. All: every time entry of the "
        "exports as history; those of the file below are billable.",
    )
    unbilled_file = fields.Binary(
        string="Unbilled Time Entries (CSV)",
        attachment=True,
        help='In Papierkram: Zeiterfassung → Einzelbuchungen, filter "Noch nicht '
        'abgerechnet" and "Gesamter Zeitraum", button CSV.',
    )
    unbilled_filename = fields.Char()
    import_products = fields.Boolean(
        string="Import Products and Services",
        default=True,
        help="Creates the goods and services of Papierkram as products.",
    )
    sale_order_ids = fields.Many2many(
        "sale.order", string="Sales Orders", readonly=True, copy=False
    )
    sale_order_count = fields.Integer(
        string="Number of Sales Orders", compute="_compute_timesheet_counts"
    )
    timesheet_count = fields.Integer(
        string="Number of Time Entries", compute="_compute_timesheet_counts"
    )

    @api.depends("sale_order_ids")
    def _compute_timesheet_counts(self):
        Line = self.env["account.analytic.line"]
        for rec in self:
            rec.sale_order_count = len(rec.sale_order_ids)
            rec.timesheet_count = (
                Line.search_count([("papierkram_import_id", "=", rec.id)])
                if rec.id
                else 0
            )

    def action_view_sale_orders(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("sale.action_orders")
        action["domain"] = [("id", "in", self.sale_order_ids.ids)]
        action["context"] = {}
        return action

    def action_view_timesheets(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "hr_timesheet.act_hr_timesheet_line"
        )
        action["domain"] = [("papierkram_import_id", "=", self.id)]
        action["context"] = {}
        return action

    # ------------------------------------------------------------ hooks
    def _check_extra(self, plan):
        super()._check_extra(plan)
        if self.time_entries == "none":
            return
        if self.time_entries == "unbilled" and not self.unbilled_file:
            plan.warnings.append(
                self.env._(
                    "No file with unbilled time entries: no time entries are imported."
                )
            )
            return
        missing = [
            self.env[model]._description
            for model in (
                "sale.order",
                "project.project",
                "account.analytic.line",
                "hr.employee",
            )
            if not self.env[model].has_access("create")
        ]
        if missing:
            plan.blockers.append(
                self.env._(
                    "Importing time entries needs the right to create: %s.",
                    ", ".join(missing),
                )
            )

    def _import_extra(self, plan):
        result = super()._import_extra(plan)
        _TimesheetImport(self, plan).run()
        return result


class _TimesheetImport:
    """Products, projects, tasks, time entries and sales orders of one import.
    Not an Odoo model."""

    def __init__(self, run, plan):
        self.run_rec = run
        self.plan = plan
        self.env = run.env
        self._ = run.env._
        self.company = run.company_id
        self.counts = defaultdict(int)
        self._products = {}
        self._projects = {}
        self._tasks = {}
        self._employees = {}
        self._services = None
        self._contacts_by_name = {
            contact.name: number for number, contact in plan.export.contacts.items()
        }

    def run(self):
        if self.run_rec.import_products:
            self._import_products()
        entries, open_keys = self._entries()
        Line = self.env["account.analytic.line"]
        existing = set(
            Line.search(
                [
                    ("company_id", "=", self.company.id),
                    ("papierkram_key", "in", [entry.key for entry in entries]),
                ]
            ).mapped("papierkram_key")
        )
        new = [entry for entry in entries if entry.key not in existing]
        self.counts["skipped"] = len(entries) - len(new)
        billable = [
            entry for entry in new if entry.key in open_keys and not entry.not_billable
        ]
        sale_lines = self._sale_orders(billable)
        for entry in new:
            self._create_timesheet(entry, sale_lines.get(entry.key))
        labels = {
            "products": self._("Products created"),
            "projects": self._("Projects created"),
            "tasks": self._("Tasks created"),
            "timesheets": self._("Time entries imported"),
            "billable": self._("of which billable through a sales order"),
            "orders": self._("Sales orders for unbilled time entries"),
            "skipped": self._("Time entries already present"),
        }
        self.plan.extra += [
            (labels[key], self.counts[key]) for key in labels if self.counts[key]
        ]

    # ------------------------------------------------------------ input
    def _entries(self):
        """Time entries to import and the keys of the unbilled ones."""
        mode = self.run_rec.time_entries
        if mode == "unbilled" and not self.run_rec.unbilled_file:
            return [], set()
        unbilled = self._unbilled()
        open_keys = {entry.key for entry in unbilled}
        if mode == "none":
            return [], open_keys
        if mode == "unbilled":
            return unbilled, open_keys
        rows = [row for _year, row in self.plan.export.table(TIMESHEET_CSV)]
        entries = parse_time_entries(rows)
        known = {entry.key for entry in entries}
        missing = [entry for entry in unbilled if entry.key not in known]
        if missing:
            self.plan.warnings.append(
                self._(
                    "%s unbilled time entries are not in the exports; they are "
                    "imported from the file.",
                    len(missing),
                )
            )
        return entries + missing, open_keys

    # ------------------------------------------------------------ products
    def _import_products(self):
        rows = [row for _year, row in self.plan.export.table(PRODUCT_CSV, newest=True)]
        for product in parse_products(rows):
            self._product(product.name, product)

    def _time_services(self):
        """Services that Papierkram uses in time entries: only they are
        billed by the time entries in Odoo."""
        if self._services is None:
            rows = [row for _year, row in self.plan.export.table(TIMESHEET_CSV)]
            self._services = {entry.service for entry in parse_time_entries(rows)}
            self._services |= {entry.service for entry in self._unbilled()}
        return self._services

    def _unbilled(self):
        if not self.run_rec.unbilled_file:
            return []
        text = base64.b64decode(self.run_rec.unbilled_file).decode("utf-8-sig")
        return parse_time_entry_file(text)

    def _product(self, name, product=None, rate=None):
        """Product of a Papierkram service (created if missing)."""
        if name in self._products:
            return self._products[name]
        Product = self.env["product.product"].with_company(self.company)
        domain = [("company_id", "in", (False, self.company.id))]
        found = Product.browse()
        if product and product.code:
            found = Product.search(domain + [("default_code", "=", product.code)])[:1]
        if not found:
            found = Product.search(domain + [("name", "=", name)])[:1]
        if not found:
            unit = product.unit if product else "hour"
            values = {
                "name": name,
                "default_code": (product and product.code) or False,
                "description_sale": (product and product.description) or False,
                "type": "service",
                "list_price": float(product.price if product else rate or 0),
                "uom_id": self.env.ref(UOM_REFS[unit]).id,
                "company_id": self.company.id,
                "taxes_id": [Command.set(self._sale_taxes(product).ids)],
            }
            by_time = unit == "hour" and (not product or name in self._time_services())
            if by_time:
                values.update(service_policy="delivered_timesheet")
            found = Product.create(values)
            if by_time:
                found.service_tracking = "no"
            self.counts["products"] += 1
        self._products[name] = found
        return found

    def _sale_taxes(self, product):
        Tax = self.env["account.tax"]
        if self.run_rec.small_business or not product or not product.vat_rate:
            return Tax
        return Tax.search(
            [
                ("company_id", "=", self.company.id),
                ("type_tax_use", "=", "sale"),
                ("amount_type", "=", "percent"),
                ("amount", "=", float(product.vat_rate)),
                ("price_include", "=", False),
            ],
            limit=1,
        )

    # ------------------------------------------------------------ partners
    def _partner(self, name):
        number = self._contacts_by_name.get(name)
        if number:
            return self.plan._partner_by_number(number)
        return self.plan._partner_by_name(name, supplier=False)

    def _employee(self, name):
        if name in self._employees:
            return self._employees[name]
        Employee = self.env["hr.employee"].with_company(self.company)
        user = self.env["res.users"].search([("name", "=", name)], limit=1)
        domain = [("company_id", "=", self.company.id)]
        employee = Employee.search(
            domain + ([("user_id", "=", user.id)] if user else [("name", "=", name)]),
            limit=1,
        )
        if not employee:
            values = {
                "name": name or self._("Papierkram"),
                "company_id": self.company.id,
            }
            if user and self.company in user.company_ids:
                values["user_id"] = user.id
            employee = Employee.create(values)
        self._employees[name] = employee
        return employee

    # ------------------------------------------------------------ projects
    def _project(self, entry):
        key = f"{entry.customer}\x1f{entry.project}"
        if key in self._projects:
            return self._projects[key]
        Project = self.env["project.project"].with_company(self.company)
        project = Project.search(
            [("company_id", "=", self.company.id), ("papierkram_key", "=", key)],
            limit=1,
        )
        if not project:
            name = (
                self._("Retainer (%s)", entry.customer)
                if entry.without_project
                else entry.project
            )
            project = Project.create(
                {
                    "name": name,
                    "partner_id": self._partner(entry.customer).id,
                    "company_id": self.company.id,
                    "allow_timesheets": True,
                    "allow_billable": True,
                    "papierkram_key": key,
                }
            )
            self.counts["projects"] += 1
        self._projects[key] = project
        return project

    def _task(self, project, name):
        name = name or self._("General")
        key = (project.id, name)
        if key in self._tasks:
            return self._tasks[key]
        Task = self.env["project.task"].with_company(self.company)
        task = Task.search(
            [("project_id", "=", project.id), ("name", "=", name)], limit=1
        )
        if not task:
            task = Task.create({"name": name, "project_id": project.id})
            self.counts["tasks"] += 1
        self._tasks[key] = task
        return task

    # ------------------------------------------------------------ billing
    def _sale_orders(self, entries):
        """One confirmed sales order per customer with a line per service and
        rate; returns ``{entry key: sales order line}``."""
        by_customer = defaultdict(list)
        for entry in entries:
            by_customer[entry.customer].append(entry)
        result = {}
        Order = self.env["sale.order"].with_company(self.company)
        for customer, items in sorted(by_customer.items()):
            groups = defaultdict(list)
            for entry in items:
                groups[(entry.service, entry.rate)].append(entry)
            lines = []
            for (service, rate), group in sorted(groups.items()):
                product = self._product(service or self._("Services"), rate=rate)
                hours = sum(entry.billable_duration for entry in group)
                lines.append(
                    Command.create(
                        {
                            "product_id": product.id,
                            "product_uom_qty": float(round(hours, 2)),
                            "price_unit": float(rate),
                        }
                    )
                )
            order = Order.create(
                {
                    "partner_id": self._partner(customer).id,
                    "company_id": self.company.id,
                    "client_order_ref": self._("Unbilled time entries (Papierkram)"),
                    "order_line": lines,
                }
            )
            order.action_confirm()
            self.run_rec.sale_order_ids |= order
            self.counts["orders"] += 1
            for (_service, rate), group in sorted(groups.items()):
                line = order.order_line.filtered(
                    lambda sol, r=rate: abs(sol.price_unit - float(r)) < 0.005
                )[:1]
                for entry in group:
                    result[entry.key] = line
            # later time entries on the task are billed on its main line
            main = max(
                groups.items(),
                key=lambda item: sum(e.billable_duration for e in item[1]),
            )
            for entry in items:
                task = self._task(self._project(entry), entry.task)
                if not task.sale_line_id:
                    task.sale_line_id = result[main[1][0].key]
        return result

    # ------------------------------------------------------------ time entries
    def _create_timesheet(self, entry, sale_line=None):
        project = self._project(entry)
        task = self._task(project, entry.task)
        billable = bool(sale_line)
        hours = entry.billable_duration if billable else entry.duration
        name = entry.comment or entry.task or "/"
        values = {
            "date": entry.day,
            "name": name,
            "project_id": project.id,
            "task_id": task.id,
            "employee_id": self._employee(entry.user).id,
            "unit_amount": float(hours),
            "company_id": self.company.id,
            "papierkram_key": entry.key,
            "papierkram_import_id": self.run_rec.id,
        }
        if billable:
            values.update(so_line=sale_line.id, is_so_line_edited=True)
            self.counts["billable"] += 1
        else:
            values.update(so_line=False, is_so_line_edited=True)
        self.env["account.analytic.line"].with_company(self.company).create(values)
        self.counts["timesheets"] += 1
