# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Import of a Papierkram full export into Odoo accounting.

An import run (``papierkram.import``) keeps the source files, checks them and
then creates, in one transaction:

* contacts (with their Papierkram number),
* customer invoices, cancellations and credit notes in a dedicated journal,
* expense and income vouchers with one line per category,
* every payment as a cash entry *with its payment date* on a dedicated money
  account, so that a cash-basis profit computation (German EÜR) knows when
  money actually came in or went out,
* the depreciation of fixed assets following Papierkram's schedule,
* the original PDFs as attachments.

Whatever cannot be imported unambiguously is reported in the log instead of
being guessed.
"""

import base64
import csv
import os
import tempfile
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from markupsafe import Markup, escape

from odoo import Command, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import file_open, format_date

from ..lib.depreciation import papierkram_schedule
from ..lib.reader import PapierkramExport, PapierkramFormatError

CENT = Decimal("0.01")

# Revenue accounts (SKR03); SKR04 is resolved by account name.
REVENUE_SKR03 = {"small_business": "8195", 19: "8400", 7: "8300", 0: "8200"}
# Cash discounts granted (SKR03)
DISCOUNT_SKR03 = "8730"
# Depreciation accounts (SKR03)
DEPRECIATION_SKR03 = {
    "afa": "4830",  # depreciation of tangible fixed assets
    "afa_intangible": "4822",  # amortisation of intangible assets
    "gwg": "4855",  # immediate write-off of low-value assets
    "pool": "4862",  # write-down of the collective item (Sammelposten)
}

# Papierkram categories whose SKR03 account means something else in today's
# DATEV/Odoo chart: Papierkram books "Betriebsbedarf" on 4980, which DATEV now
# uses for leasing of machines (EÜR line 222). 4985 "Werkzeuge und Kleingeräte"
# lands in the same EÜR line as SKR04 6850 "Sonstiger Betriebsbedarf" (229).
CATEGORY_ACCOUNT_OVERRIDES = {"Betriebsbedarf": "4985"}

# SKR03 → SKR04 where the account names in Odoo's templates differ
SKR04_BY_SKR03 = {
    "0027": "0135",  # EDV-Software
    "2280": "7640",  # Gewerbesteuernachzahlungen Vorjahre
    "4175": "6090",  # Fahrtkostenerstattung Wohnung/Arbeitsstätte
    "4805": "6470",  # Reparaturen und Instandhaltung BGA
    "4810": "6498",  # Mietleasing technische Anlagen und Maschinen
    "4855": "6260",  # Sofortabschreibung GWG
}

# Country names as exported by Papierkram
COUNTRY_CODES = {
    "Deutschland": "DE",
    "Österreich": "AT",
    "Schweiz": "CH",
    "Liechtenstein": "LI",
    "Luxemburg": "LU",
    "Belgien": "BE",
    "Niederlande": "NL",
    "Frankreich": "FR",
    "Italien": "IT",
    "Spanien": "ES",
    "Portugal": "PT",
    "Dänemark": "DK",
    "Schweden": "SE",
    "Norwegen": "NO",
    "Finnland": "FI",
    "Polen": "PL",
    "Tschechien": "CZ",
    "Tschechische Republik": "CZ",
    "Irland": "IE",
    "Vereinigtes Königreich": "GB",
    "Großbritannien": "GB",
    "Vereinigte Staaten von Amerika": "US",
    "Vereinigte Staaten": "US",
    "Kanada": "CA",
}


def _money(value):
    return float(Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP))


class PapierkramImport(models.Model):
    _name = "papierkram.import"
    _description = "Papierkram Import"
    _inherit = ["mail.thread"]
    _order = "id desc"

    name = fields.Char(
        required=True,
        # the date in the format of the language of the user, not ISO
        default=lambda self: self.env._(
            "Papierkram import %s", format_date(self.env, fields.Date.today())
        ),
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        readonly=True,
    )
    state = fields.Selection(
        [("draft", "Draft"), ("checked", "Checked"), ("done", "Imported")],
        default="draft",
        required=True,
        readonly=True,
        tracking=True,
    )
    export_attachment_ids = fields.Many2many(
        "ir.attachment",
        string="Papierkram Exports",
        help="The ZIP files exported from Papierkram (Settings → Export and Import, "
        "export type 'Complete (CSV, PDF)'), one per year. Upload all years "
        "together: a payment is contained in the export of the year in which its "
        "document was created.",
    )
    categories_file = fields.Binary(string="Category List (CSV)", attachment=True)
    categories_filename = fields.Char()
    assets_file = fields.Binary(string="Fixed Assets (CSV)", attachment=True)
    assets_filename = fields.Char()
    splits_file = fields.Binary(string="Split Vouchers (CSV)", attachment=True)
    splits_filename = fields.Char()

    small_business = fields.Boolean(
        string="Small Business (§ 19 UStG)",
        default=True,
        help="Import invoices and vouchers without VAT. Disable only if the "
        "business was subject to regular VAT in Papierkram.",
    )
    scope = fields.Selection(
        [("complete", "Complete History"), ("open", "Open Items Only")],
        default="complete",
        required=True,
        help="Complete history: every document of the exports with its payments "
        "and the depreciation. Open items only: unpaid invoices and vouchers with "
        "their partial payments, nothing else. A later complete import adds the "
        "rest; documents already imported are skipped.",
    )
    create_depreciation = fields.Boolean(
        string="Post Depreciation",
        default=True,
        help="Posts the yearly depreciation of fixed assets following "
        "Papierkram's schedule (including the memo value of 1 €).",
    )
    depreciation_until = fields.Date(
        string="Post Depreciation Until",
        default=lambda self: date(fields.Date.today().year - 1, 12, 31),
        help="Depreciation is posted up to and including this date. The current "
        "year is posted at year end.",
    )
    sale_journal_id = fields.Many2one(
        "account.journal",
        string="Invoice Journal",
        domain="[('type', '=', 'sale'), ('company_id', '=', company_id)]",
    )
    purchase_journal_id = fields.Many2one(
        "account.journal",
        string="Voucher Journal",
        domain="[('type', '=', 'purchase'), ('company_id', '=', company_id)]",
    )
    money_journal_id = fields.Many2one(
        "account.journal",
        string="Payment Journal",
        domain="[('type', 'in', ('bank', 'cash')), ('company_id', '=', company_id)]",
        help="Money account that receives every payment from Papierkram with its "
        "payment date.",
    )
    misc_journal_id = fields.Many2one(
        "account.journal",
        string="Depreciation Journal",
        domain="[('type', '=', 'general'), ('company_id', '=', company_id)]",
    )

    log = fields.Html(readonly=True, sanitize=True)
    warning_count = fields.Integer(string="Notes", readonly=True)
    move_ids = fields.One2many("account.move", "papierkram_import_id", readonly=True)
    move_count = fields.Integer(compute="_compute_move_count")

    @api.depends("move_ids")
    def _compute_move_count(self):
        for rec in self:
            rec.move_count = len(rec.move_ids)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_check(self):
        self.ensure_one()
        export = self._read_export()
        try:
            plan = _Plan(self, export)
            plan.check()
            self._check_extra(plan)
            self._write_log(plan)
        finally:
            export.close()
        self.state = "checked"

    def action_import(self):
        self.ensure_one()
        if self.state == "done":
            raise UserError(self.env._("This import has already been run."))
        export = self._read_export()
        try:
            plan = _Plan(self, export)
            plan.check()
            self._check_extra(plan)
            if plan.blockers:
                self._write_log(plan)
                raise UserError(
                    self.env._("The import is blocked:\n%s", "\n".join(plan.blockers))
                )
            plan.run()
            self._import_extra(plan)
            self._write_log(plan)
        finally:
            export.close()
        self.state = "done"

    def _check_extra(self, plan):
        """Hook for extension modules: further checks of an import.

        :param plan: the running import with ``export`` (the read exports),
            ``blockers``, ``warnings`` and ``extra`` (summary lines)
        """

    def _import_extra(self, plan):
        """Hook for extension modules: import further data after the
        documents and payments (same ``plan`` as :meth:`_check_extra`)."""

    def action_view_moves(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Imported Entries"),
            "res_model": "account.move",
            "view_mode": "list,form",
            "domain": [("papierkram_import_id", "=", self.id)],
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _decode(self, field_name):
        value = self[field_name]
        if not value:
            return None
        return base64.b64decode(value).decode("utf-8-sig")

    def _read_export(self):
        if not self.export_attachment_ids:
            raise UserError(self.env._("Please upload at least one Papierkram export."))
        tmpdir = tempfile.mkdtemp(prefix="papierkram_")
        paths = []
        for att in self.export_attachment_ids:
            safe = os.path.basename(att.name or f"export_{att.id}.zip")
            path = os.path.join(tmpdir, safe)
            with open(path, "wb") as fh:
                fh.write(base64.b64decode(att.datas))
            paths.append(path)
        try:
            return PapierkramExport.load(
                paths,
                categories_csv=self._decode("categories_file"),
                assets_csv=self._decode("assets_file"),
                splits_csv=self._decode("splits_file"),
            )
        except PapierkramFormatError as exc:
            raise UserError(str(exc)) from exc

    def _write_log(self, plan):
        parts = [Markup("<h3>%s</h3><ul>") % self.env._("Result")]
        for label, value in plan.summary():
            parts.append(Markup("<li>%s: %s</li>") % (label, value))
        parts.append(Markup("</ul>"))
        for title, items in (
            (self.env._("Blocked"), plan.blockers),
            (self.env._("Notes"), plan.warnings),
            (
                self.env._(
                    "Payments with derived date (Papierkram only knows them from a "
                    "bank transaction or from the payment date of the voucher)"
                ),
                plan.derived,
            ),
        ):
            if items:
                parts.append(Markup("<h3>%s (%s)</h3><ul>") % (title, len(items)))
                for item in items:
                    parts.append(Markup("<li>%s</li>") % escape(item))
                parts.append(Markup("</ul>"))
        self.log = Markup("").join(parts)
        self.warning_count = len(plan.warnings) + len(plan.blockers)


class _Plan:
    """Checks and runs one import. Not an Odoo model."""

    def __init__(self, run, export):
        self.run_rec = run
        self.env = run.env
        self._ = run.env._
        self.company = run.company_id
        self.export = export
        self.events = export.cash_events()  # may add to export.notes
        self.warnings = [self._note_text(note) for note in export.notes]
        self.blockers = []
        self.counts = defaultdict(int)
        self.extra = []  # summary lines of extension modules
        self.open_only = run.scope == "open"
        if self.open_only:
            self._restrict_to_open_items()
        self.derived = []
        self._accounts = {}
        self._partners = {}
        self._moves = {}
        self._tmpl = None
        self._overridden = set()

    def _restrict_to_open_items(self):
        """Keep only unpaid invoices and vouchers with their partial payments
        (scope "open items only")."""
        exp = self.export
        invoices = {
            number: inv
            for number, inv in exp.invoices.items()
            if inv.state == "open" and inv.kind in ("invoice", "credit_note")
        }
        vouchers = {
            number: voucher
            for number, voucher in exp.vouchers.items()
            if voucher.state == "unpaid"
        }
        self.counts["skipped"] += (
            len(exp.invoices) + len(exp.vouchers) - len(invoices) - len(vouchers)
        )
        exp.invoices, exp.vouchers = invoices, vouchers
        keep = set(invoices) | set(vouchers)
        self.events = [event for event in self.events if event.document in keep]

    def _note_text(self, note):
        """Translatable text for a note of the reader."""
        p = note.params
        messages = {
            "split_unknown_voucher": lambda: self._(
                "Split given for unknown voucher %s.", p["voucher"]
            ),
            "split_sum_mismatch": lambda: self._(
                "Split of voucher %(voucher)s: total %(total)s differs from the "
                "voucher amount %(gross)s; split ignored.",
                **p,
            ),
            "split_missing": lambda: self._(
                "Voucher %(voucher)s has several categories (%(categories)s) but no "
                "amount per category.",
                **p,
            ),
            "unknown_category": lambda: self._(
                "Voucher %(voucher)s: unknown category %(categories)s.", **p
            ),
            "payment_unknown_invoice": lambda: self._(
                "Payment for an unknown invoice: %s.", p["reference"]
            ),
            "payment_unknown_voucher": lambda: self._(
                "Payment for an unknown voucher: %s.", p["reference"]
            ),
            "invoice_paid_without_date": lambda: self._(
                "Invoice %(invoice)s: %(amount)s paid without payment date.", **p
            ),
            "voucher_paid_without_date": lambda: self._(
                "Voucher %s is paid but has no payment date.", p["voucher"]
            ),
        }
        message = messages.get(note.code)
        return message() if message else str(note)

    # -- Check ------------------------------------------------------------

    def check(self):
        chart = self.company.chart_template
        if chart not in ("de_skr03", "de_skr04"):
            self.blockers.append(
                self._(
                    "The company needs the chart of accounts SKR03 or SKR04 (is: %s).",
                    chart,
                )
            )
            return
        needed = set()
        for voucher in self.export.vouchers.values():
            for line in voucher.lines:
                code = self._category_code(line.category)
                if not code:
                    self.blockers.append(
                        self._(
                            "Voucher %(nr)s: category '%(cat)s' has no account.",
                            nr=voucher.number,
                            cat=line.category,
                        )
                    )
                else:
                    needed.add(code)
        needed |= set(DEPRECIATION_SKR03.values()) | {DISCOUNT_SKR03}
        needed |= {
            REVENUE_SKR03["small_business"]
            if self.run_rec.small_business
            else REVENUE_SKR03[19]
        }
        for code in sorted(needed):
            if not self._account(code, raise_missing=False):
                self.blockers.append(
                    self._("Account %s (SKR03) does not exist in this company.", code)
                )
        if self.run_rec.small_business:
            with_vat = sorted(
                number for number, inv in self.export.invoices.items() if inv.tax
            )
            if with_vat:
                self.warnings.append(
                    self._(
                        "Small business is ticked, but invoices show VAT (%s): "
                        "untick it if the business was subject to regular VAT in "
                        "Papierkram. Ticked, these invoices are imported without "
                        "their VAT.",
                        ", ".join(with_vat[:5]) + (" …" if len(with_vat) > 5 else ""),
                    )
                )
        for voucher in self.export.vouchers.values():
            if voucher.private_share:
                self.warnings.append(
                    self._(
                        "Voucher %s has a private share; it is not split.",
                        voucher.number,
                    )
                )
            if (
                not self.run_rec.small_business
                and voucher.tax
                and len(voucher.lines) > 1
            ):
                self.warnings.append(
                    self._(
                        "Voucher %s: please check input VAT on several lines.",
                        voucher.number,
                    )
                )
        lock = self.company._get_user_fiscal_lock_date(self.run_rec.money_journal_id)
        if lock and lock > date.min:
            self.warnings.append(
                self._(
                    "A lock date (%s) is set. Odoo moves entries into a locked period "
                    "behind the lock date, which would change payment dates.",
                    lock,
                )
            )
        existing = self.env["account.move"].search_count(
            [("company_id", "=", self.company.id), ("papierkram_number", "!=", False)]
        )
        if existing:
            self.warnings.append(
                self._(
                    "%s entries from Papierkram already exist; they are skipped.",
                    existing,
                )
            )

    def summary(self):
        exp = self.export
        items = [
            (self._("Contacts in export"), len(exp.contacts)),
            (self._("Invoices in export"), len(exp.invoices)),
            (self._("Vouchers in export"), len(exp.vouchers)),
            (self._("Payments in export"), len(exp.payments)),
            (self._("Fixed assets"), len(exp.assets)),
        ]
        labels = {
            "partners": self._("Contacts created"),
            "partners_matched": self._("Existing contacts used"),
            "invoices": self._("Invoices imported"),
            "refunds": self._("Cancellations and credit notes imported"),
            "vouchers": self._("Vouchers imported"),
            "payments": self._("Payments posted"),
            "derived_payments": self._("of which payments with derived date"),
            "depreciations": self._("Depreciation entries"),
            "attachments": self._("PDF attachments"),
            "skipped": self._(
                "Skipped (draft, cancelled or deleted without payment, already present)"
            ),
        }
        items += [(labels[k], v) for k, v in self.counts.items() if k in labels]
        return items + self.extra

    # -- Accounts and partners -------------------------------------------

    def _template_names(self):
        if self._tmpl is None:
            self._tmpl = {}
            for chart in ("skr03", "skr04"):
                path = f"l10n_de/data/template/account.account-de_{chart}.csv"
                with file_open(path) as fh:
                    for row in csv.DictReader(fh):
                        self._tmpl[(chart, row["code"])] = (
                            row.get("name@de") or row["name"]
                        )
        return self._tmpl

    def _account(self, code_skr03, raise_missing=True):
        """Account of the company for an SKR03 code (SKR04 resolved by name)."""
        code_skr03 = str(code_skr03).zfill(4)
        if code_skr03 in self._accounts:
            return self._accounts[code_skr03]
        Account = self.env["account.account"].with_company(self.company)
        account = Account.browse()
        if self.company.chart_template == "de_skr03":
            account = Account.search(
                [("code", "=", code_skr03), ("company_ids", "in", self.company.id)],
                limit=1,
            )
        elif code_skr03 in SKR04_BY_SKR03:
            account = Account.search(
                [
                    ("code", "=", SKR04_BY_SKR03[code_skr03]),
                    ("company_ids", "in", self.company.id),
                ],
                limit=1,
            )
        else:
            name = self._template_names().get(("skr03", code_skr03))
            if name:
                codes = [
                    code
                    for (chart, code), n in self._template_names().items()
                    if chart == "skr04" and n == name
                ]
                if len(codes) == 1:
                    account = Account.search(
                        [
                            ("code", "=", codes[0]),
                            ("company_ids", "in", self.company.id),
                        ],
                        limit=1,
                    )
        if not account and raise_missing:
            raise UserError(
                self._("Account %s (SKR03) is missing in the company.", code_skr03)
            )
        self._accounts[code_skr03] = account
        return account

    def _category_code(self, category):
        """SKR03 account of a Papierkram category, with known corrections."""
        if category in CATEGORY_ACCOUNT_OVERRIDES:
            if category not in self._overridden:
                self._overridden.add(category)
                self.warnings.append(
                    self._(
                        "Category '%(cat)s' is booked on account %(new)s instead of "
                        "Papierkram's %(old)s, which means leasing in today's chart.",
                        cat=category,
                        new=CATEGORY_ACCOUNT_OVERRIDES[category],
                        old=self.export.account_for_category(category),
                    )
                )
            return CATEGORY_ACCOUNT_OVERRIDES[category]
        return self.export.account_for_category(category)

    def _country(self, name):
        Country = self.env["res.country"]
        if not name:
            return Country
        code = COUNTRY_CODES.get(name.strip())
        if code:
            return Country.search([("code", "=", code)], limit=1)
        lang = "de_DE" if self.env["res.lang"]._lang_get("de_DE") else self.env.lang
        country = Country.with_context(lang=lang).search(
            [("name", "=ilike", name)], limit=1
        )
        if not country:
            self.warnings.append(self._("Country '%s' not found.", name))
        return country

    def _partner_by_number(self, number):
        if number in self._partners:
            return self._partners[number]
        Partner = self.env["res.partner"].with_company(self.company)
        partner = Partner.search(
            [
                ("papierkram_number", "=", number),
                ("company_id", "in", (False, self.company.id)),
            ],
            limit=1,
        )
        contact = self.export.contacts.get(number)
        if not partner and contact:
            # contacts are often shared between the companies of a database:
            # use one of the same name instead of creating a duplicate
            partner = self._existing_partner(contact.name)
            if partner:
                partner.papierkram_number = number
                self.counts["partners_matched"] += 1
        if not partner and contact:
            person = f"{contact.first_name} {contact.last_name}".strip()
            partner = Partner.create(
                {
                    "name": contact.name,
                    "is_company": person != contact.name,
                    "papierkram_number": number,
                    "ref": number,
                    "street": contact.street or False,
                    "zip": contact.zip or False,
                    "city": contact.city or False,
                    "country_id": self._country(contact.country).id or False,
                    "email": contact.email or False,
                    "phone": contact.phone or False,
                    "website": contact.website or False,
                    "comment": contact.note or False,
                    "customer_rank": 1 if contact.kind == "customer" else 0,
                    "supplier_rank": 1 if contact.kind == "supplier" else 0,
                }
            )
            if contact.vat:
                try:
                    # the savepoint also discards the invalid number from the
                    # cache, so that it is not written with the next flush
                    with self.env.cr.savepoint():
                        partner.vat = contact.vat
                except Exception:  # noqa: BLE001 - an invalid VAT number is only reported
                    self.warnings.append(
                        self._(
                            "Contact %(nr)s: VAT number '%(vat)s' not imported.",
                            nr=number,
                            vat=contact.vat,
                        )
                    )
            self.counts["partners"] += 1
        self._partners[number] = partner
        return partner

    def _existing_partner(self, name):
        """Contact of the same name (ignoring case) without a Papierkram
        number, visible in the company."""
        return (
            self.env["res.partner"]
            .with_company(self.company)
            .search(
                [
                    ("name", "=ilike", name),
                    ("parent_id", "=", False),
                    ("papierkram_number", "=", False),
                    ("company_id", "in", (False, self.company.id)),
                ],
                limit=1,
            )
        )

    def _partner_by_name(self, name, supplier=True):
        name = (name or "").strip() or self._("Unknown (Papierkram)")
        key = ("name", name, supplier)
        if key in self._partners:
            return self._partners[key]
        Partner = self.env["res.partner"].with_company(self.company)
        partner = Partner.search(
            [
                ("name", "=ilike", name),
                ("parent_id", "=", False),
                ("company_id", "in", (False, self.company.id)),
            ],
            limit=1,
        )
        if not partner:
            partner = Partner.create(
                {
                    "name": name,
                    "is_company": True,
                    "supplier_rank": 1 if supplier else 0,
                    "customer_rank": 0 if supplier else 1,
                }
            )
            self.counts["partners"] += 1
        self._partners[key] = partner
        return partner

    # -- Journals ----------------------------------------------------------

    def _journals(self):
        run = self.run_rec
        Journal = self.env["account.journal"].with_company(self.company)
        defaults = (
            ("sale_journal_id", self._("Papierkram Invoices"), "PKR", "sale"),
            ("purchase_journal_id", self._("Papierkram Vouchers"), "PKB", "purchase"),
            ("misc_journal_id", self._("Papierkram Depreciation"), "PKA", "general"),
            ("money_journal_id", self._("Papierkram Payments"), "PKZ", "bank"),
        )
        for field_name, name, code, journal_type in defaults:
            if run[field_name]:
                continue
            # a later import of the same company uses the journals of the first
            journal = Journal.search(
                [
                    ("company_id", "=", self.company.id),
                    ("code", "=", code),
                    ("type", "=", journal_type),
                ],
                limit=1,
            )
            run[field_name] = journal or Journal.create(
                {
                    "name": name,
                    "code": code,
                    "type": journal_type,
                    "company_id": self.company.id,
                }
            )
        money = run.money_journal_id
        # Payments are posted directly on the money account: every payment is
        # then a cash entry with its own date. Odoo explicitly allows this (see
        # the domain of account.payment.method.line.payment_account_id).
        method_lines = (
            money.inbound_payment_method_line_ids
            | money.outbound_payment_method_line_ids
        )
        for line in method_lines:
            if line.payment_account_id != money.default_account_id:
                line.payment_account_id = money.default_account_id

    # -- Run ---------------------------------------------------------------

    def run(self):
        """Create all documents, then replay payments and cancellations in
        chronological order, as they happened in Papierkram."""
        self._journals()
        events = defaultdict(list)
        for event in self.events:
            events[event.document].append(event)
        timeline = []  # (date, rank, action, arguments)
        timeline += self._create_invoices(events)
        timeline += self._create_vouchers(events)
        for number in events:
            known = (
                number in self._moves
                or number in self.export.vouchers
                or number in self.export.invoices
            )
            if not known:
                self.warnings.append(
                    self._("Payment for %s without imported document skipped.", number)
                )
        for when, _rank, action, args in sorted(timeline, key=lambda t: (t[0], t[1])):
            self._apply(when, action, args)
        if self.run_rec.create_depreciation and not self.open_only:
            self._import_depreciation()

    def _create_invoices(self, events):
        """Create invoices and credit notes; return their timeline entries."""
        invoices = sorted(
            self.export.invoices.values(), key=lambda i: (i.date, i.number)
        )
        cancellations = {i.base_number: i for i in invoices if i.kind == "cancellation"}
        timeline = []
        for inv in invoices:
            if inv.kind == "cancellation" or not self._import_invoice(inv):
                continue
            for ev in events.get(inv.number, []):
                timeline.append((ev.date, 1, "pay", (inv.number, ev)))
            if inv.cash_discount > 0 and inv.kind == "invoice":
                when = inv.last_payment_date or inv.date
                timeline.append((when, 2, "discount", (inv.number, inv.cash_discount)))
            if inv.state == "cancelled":
                storno = cancellations.get(inv.number)
                when = storno.date if storno else inv.date
                timeline.append((when, 2, "reverse", (inv.number, storno)))
        for storno in cancellations.values():
            if storno.base_number not in self._moves:
                self.warnings.append(
                    self._(
                        "Cancellation %(nr)s: original invoice %(orig)s is missing, "
                        "skipped.",
                        nr=storno.number,
                        orig=storno.base_number,
                    )
                )
                self.counts["skipped"] += 1
        return timeline

    def _create_vouchers(self, events):
        """Create vouchers; return their timeline entries."""
        timeline = []
        vouchers = sorted(
            self.export.vouchers.values(), key=lambda v: (v.date, v.number)
        )
        for voucher in vouchers:
            evs = events.get(voucher.number, [])
            if not self._import_voucher(voucher, evs):
                continue
            move = self._moves[voucher.number]
            refund_sign = 1 if move.move_type in ("in_invoice", "out_refund") else -1
            # Payments in the opposite direction (refund of a cancelled voucher)
            back = [ev for ev in evs if (ev.amount > 0) == (refund_sign > 0)]
            cancelled = voucher.state in ("canceled", "deleted")
            if cancelled and back:
                first = min(ev.date for ev in back)
                timeline.append((first, 0, "reverse_voucher", (voucher.number,)))
            for ev in evs:
                if cancelled and ev in back:
                    target = f"{voucher.number}-S"
                else:
                    target = voucher.number
                timeline.append((ev.date, 1, "pay", (target, ev)))
        return timeline

    def _apply(self, when, action, args):
        """Carry out one timeline entry."""
        if action == "pay":
            number, ev = args
            self._register_payment(self._moves[number], ev)
        elif action == "reverse":
            number, storno = args
            self._reverse_invoice(number, storno)
        elif action == "discount":
            number, amount = args
            self._discount(self._moves[number], amount, when)
        elif action == "reverse_voucher":
            (number,) = args
            self._reverse(self._moves[number], when, f"Storno {number}", f"{number}-S")
            self.warnings.append(
                self._(
                    "Voucher %(nr)s is cancelled in Papierkram; the refund on %(d)s "
                    "was posted as a credit note.",
                    nr=number,
                    d=when,
                )
            )

    def _reverse_invoice(self, number, storno):
        move = self._moves[number]
        if storno:
            if storno.is_draft:
                self.warnings.append(
                    self._(
                        "Cancellation %s is only a draft in Papierkram; posted because "
                        "the invoice is cancelled.",
                        storno.number,
                    )
                )
            self._reverse(move, storno.date, storno.number, storno.number)
        else:
            self._reverse(move, move.invoice_date, f"{number}-S", f"{number}-S")
            self.warnings.append(
                self._(
                    "Invoice %s is cancelled but Papierkram contains no cancellation "
                    "document; cancellation posted on the invoice date.",
                    number,
                )
            )

    def _existing(self, number, move_type):
        return self.env["account.move"].search(
            [
                ("company_id", "=", self.company.id),
                ("papierkram_number", "=", number),
                ("move_type", "=", move_type),
            ],
            limit=1,
        )

    def _revenue_account(self, inv):
        if self.run_rec.small_business:
            return self._account(REVENUE_SKR03["small_business"])
        rate = int((inv.tax / inv.net * 100).quantize(Decimal(1))) if inv.net else 0
        return self._account(REVENUE_SKR03.get(rate, REVENUE_SKR03[0]))

    def _taxes(self, net, tax, use):
        if self.run_rec.small_business or not tax or not net:
            return self.env["account.tax"]
        rate = (tax / net * 100).quantize(Decimal(1))
        found = self.env["account.tax"].search(
            [
                ("company_id", "=", self.company.id),
                ("type_tax_use", "=", use),
                ("amount_type", "=", "percent"),
                ("amount", "=", float(rate)),
                ("price_include", "=", False),
            ],
            limit=1,
        )
        if not found:
            self.warnings.append(self._("No tax rate of %s %% found.", rate))
        return found

    def _attach(self, move, files):
        for filename, content in files:
            att = self.env["ir.attachment"].create(
                {
                    "name": filename,
                    "datas": base64.b64encode(content),
                    "res_model": "account.move",
                    "res_id": move.id,
                    "mimetype": "application/pdf",
                }
            )
            if not move.message_main_attachment_id:
                move.message_main_attachment_id = att
            self.counts["attachments"] += 1

    def _common_vals(self, number):
        return {
            "papierkram_number": number,
            "papierkram_import_id": self.run_rec.id,
            "company_id": self.company.id,
        }

    def _import_invoice(self, inv):
        """Create an invoice or credit note. Return ``True`` if it is new."""
        if inv.is_draft:
            self.counts["skipped"] += 1
            return False
        move_type = "out_refund" if inv.kind == "credit_note" else "out_invoice"
        if self._existing(inv.number, move_type):
            self.counts["skipped"] += 1
            return False
        if inv.customer_number:
            partner = self._partner_by_number(inv.customer_number)
        else:
            partner = self._partner_by_name(inv.customer_name, supplier=False)
        taxes = self._taxes(abs(inv.net), abs(inv.tax), "sale")
        vals = {
            **self._common_vals(inv.number),
            "move_type": move_type,
            "journal_id": self.run_rec.sale_journal_id.id,
            "partner_id": partner.id,
            "invoice_date": inv.date,
            "date": inv.date,
            "invoice_date_due": max(inv.due_date or inv.date, inv.date),
            "ref": inv.number,
            "payment_reference": inv.number,
            "narration": inv.note or False,
            "invoice_line_ids": [
                Command.create(
                    {
                        "name": inv.subject or inv.number,
                        "quantity": 1,
                        "price_unit": _money(abs(inv.net)),
                        "account_id": self._revenue_account(inv).id,
                        # SKR accounts carry default taxes: always set explicitly
                        "tax_ids": [Command.set(taxes.ids)],
                    }
                )
            ],
        }
        move = self.env["account.move"].with_company(self.company).create(vals)
        move.action_post()
        self._check_total(move, abs(inv.gross), inv.number)
        pdf = self.export.invoice_pdf(inv.number)
        if pdf:
            self._attach(move, [(f"{inv.number}.pdf", pdf)])
        self._moves[inv.number] = move
        self.counts["refunds" if move_type == "out_refund" else "invoices"] += 1
        return True

    def _reverse(self, move, when, ref, number):
        reversal = move._reverse_moves(
            [
                {
                    "date": when,
                    "invoice_date": when,
                    "ref": ref,
                    **self._common_vals(number),
                }
            ],
            cancel=True,
        )
        pdf = self.export.invoice_pdf(number)
        if pdf:
            self._attach(reversal, [(f"{number}.pdf", pdf)])
        self._moves[number] = reversal
        self.counts["refunds"] += 1
        return reversal

    def _voucher_move_type(self, voucher):
        accounts = [self._category_code(line.category) or "" for line in voucher.lines]
        income = bool(accounts) and all(
            code.zfill(4).startswith("8") for code in accounts if code
        )
        if voucher.incoming:
            return "out_invoice" if income else "in_refund"
        return "out_refund" if income else "in_invoice"

    def _import_voucher(self, voucher, events):
        """Create a voucher. Return ``True`` if it is new."""
        if voucher.state in ("canceled", "deleted") and not events:
            self.counts["skipped"] += 1
            return False
        move_type = self._voucher_move_type(voucher)
        if self._existing(voucher.number, move_type):
            self.counts["skipped"] += 1
            return False
        supplier = move_type.startswith("in_")
        partner = self._partner_by_name(voucher.supplier, supplier=supplier)
        taxes = self._taxes(
            voucher.net, voucher.tax, "purchase" if supplier else "sale"
        )
        lines = []
        for line in voucher.lines:
            gross = line.gross if line.gross is not None else voucher.gross
            if line.gross is None and len(voucher.lines) > 1:
                # Split unknown: the whole amount goes to the first category
                if line is not voucher.lines[0]:
                    continue
                self.warnings.append(
                    self._(
                        "Voucher %(nr)s: split into %(cats)s is missing, the whole "
                        "amount was posted to '%(first)s'.",
                        nr=voucher.number,
                        cats=", ".join(voucher.categories),
                        first=line.category,
                    )
                )
            if self.run_rec.small_business or not voucher.tax:
                net = gross
            else:
                net = gross * voucher.net / voucher.gross
            account = self._account(self._category_code(line.category))
            lines.append(
                Command.create(
                    {
                        "name": line.title
                        or voucher.subject
                        or line.category
                        or voucher.number,
                        "quantity": 1,
                        "price_unit": _money(net),
                        "account_id": account.id,
                        "tax_ids": [Command.set(taxes.ids)],
                    }
                )
            )
        journal = (
            self.run_rec.purchase_journal_id
            if supplier
            else self.run_rec.sale_journal_id
        )
        vals = {
            **self._common_vals(voucher.number),
            "move_type": move_type,
            "journal_id": journal.id,
            "partner_id": partner.id,
            "invoice_date": voucher.date,
            "date": voucher.date,
            "invoice_date_due": max(voucher.due_date or voucher.date, voucher.date),
            "ref": voucher.subject or voucher.number,
            "payment_reference": voucher.number,
            "narration": voucher.note or False,
            "invoice_line_ids": lines,
        }
        move = self.env["account.move"].with_company(self.company).create(vals)
        move.action_post()
        self._check_total(move, voucher.gross, voucher.number)
        files = self.export.voucher_pdfs(voucher.number)
        if files:
            self._attach(move, files)
        self._moves[voucher.number] = move
        self.counts["vouchers"] += 1
        return True

    def _discount(self, move, amount, when):
        """Cash discount granted, as a credit note without money flow."""
        if not self.run_rec.small_business:
            self.warnings.append(
                self._(
                    "%s: cash discount posted to 8730 without VAT split; please check.",
                    move.papierkram_number,
                )
            )
        number = f"{move.papierkram_number}-SKONTO"
        refund = (
            self.env["account.move"]
            .with_company(self.company)
            .create(
                {
                    **self._common_vals(number),
                    "move_type": "out_refund",
                    "journal_id": move.journal_id.id,
                    "partner_id": move.partner_id.id,
                    "invoice_date": when,
                    "date": when,
                    "ref": self._("Cash discount for %s", move.papierkram_number),
                    "reversed_entry_id": move.id,
                    "invoice_line_ids": [
                        Command.create(
                            {
                                "name": self._("Cash discount granted"),
                                "quantity": 1,
                                "price_unit": _money(amount),
                                "account_id": self._account(DISCOUNT_SKR03).id,
                                "tax_ids": [Command.clear()],
                            }
                        )
                    ],
                }
            )
        )
        refund.action_post()
        lines = (move.line_ids | refund.line_ids).filtered(
            lambda line: line.account_id.account_type == "asset_receivable"
            and not line.reconciled
        )
        if len(lines) > 1:
            lines.reconcile()
        self._moves[number] = refund
        self.counts["refunds"] += 1

    def _check_total(self, move, expected, number):
        if abs(Decimal(str(move.amount_total)) - Decimal(expected)) > CENT / 2:
            self.warnings.append(
                self._(
                    "%(nr)s: total in Odoo %(odoo)s, in Papierkram %(pk)s.",
                    nr=number,
                    odoo=move.amount_total,
                    pk=expected,
                )
            )

    def _register_payment(self, move, event):
        """Post a money flow as a payment directly on the money account and
        reconcile it with the open item of the document if the direction fits."""
        amount = Decimal(event.amount)
        payment_type = "inbound" if amount > 0 else "outbound"
        partner_type = "customer" if move.move_type.startswith("out_") else "supplier"
        journal = self.run_rec.money_journal_id
        if payment_type == "inbound":
            method_lines = journal.inbound_payment_method_line_ids
        else:
            method_lines = journal.outbound_payment_method_line_ids
        payment = (
            self.env["account.payment"]
            .with_company(self.company)
            .create(
                {
                    "payment_type": payment_type,
                    "partner_type": partner_type,
                    "partner_id": move.commercial_partner_id.id,
                    "amount": _money(abs(amount)),
                    "date": event.date,
                    "journal_id": journal.id,
                    "payment_method_line_id": method_lines[:1].id,
                    "memo": move.papierkram_number,
                }
            )
        )
        payment.action_post()
        payment.move_id.papierkram_import_id = self.run_rec
        inbound_doc = move.move_type in ("out_invoice", "in_refund")
        if (payment_type == "inbound") == inbound_doc:
            open_lines = (payment.move_id.line_ids | move.line_ids).filtered(
                lambda line: line.account_id.account_type
                in ("asset_receivable", "liability_payable")
                and not line.reconciled
            )
            for account in open_lines.account_id:
                group = open_lines.filtered(
                    lambda line, a=account: line.account_id == a
                )
                if len(group) > 1:
                    group.reconcile()
        self.counts["payments"] += 1
        if event.source != "payment":
            self.counts["derived_payments"] += 1
            if event.source == "last_payment_date":
                source = self._("last payment date")
            else:
                source = self._("paid on")
            self.derived.append(
                self._(
                    "%(nr)s: payment of %(amount)s on %(date)s derived from '%(src)s'.",
                    nr=move.papierkram_number,
                    amount=amount,
                    date=event.date,
                    src=source,
                )
            )

    # -- Depreciation --------------------------------------------------------

    def _depreciation_account(self, voucher_number):
        voucher = self.export.vouchers.get(voucher_number)
        category = None
        if voucher:
            for line in voucher.lines:
                cat = self.export.categories.get(line.category)
                if cat and cat.category_type == "ASSET":
                    category = cat
                    break
        if category and category.kind == "GWG":
            return self._account(DEPRECIATION_SKR03["pool"]), category
        if category and category.kind == "INSTANT" and "GWG" in category.name:
            return self._account(DEPRECIATION_SKR03["gwg"]), category
        if category and category.account_skr03.zfill(4).startswith("00"):
            return self._account(DEPRECIATION_SKR03["afa_intangible"]), category
        return self._account(DEPRECIATION_SKR03["afa"]), category

    def _import_depreciation(self):
        until = self.run_rec.depreciation_until
        for asset in self.export.assets:
            expense, category = self._depreciation_account(asset.voucher_number)
            if not category:
                self.warnings.append(
                    self._(
                        "Fixed asset %(name)s (%(nr)s): voucher or asset category is "
                        "missing, no depreciation posted.",
                        name=asset.name,
                        nr=asset.voucher_number,
                    )
                )
                continue
            asset_account = self._account(category.account_skr03)
            for year, amount in papierkram_schedule(asset):
                booking = date(year, 12, 31)
                if until and booking > until:
                    break
                if not amount:
                    continue
                ref = f"AfA {year} {asset.voucher_number} {asset.name}"
                number = f"AFA-{year}-{asset.voucher_number}-{asset.name[:20]}"
                if self._existing(number, "entry"):
                    continue
                move = (
                    self.env["account.move"]
                    .with_company(self.company)
                    .create(
                        {
                            **self._common_vals(number),
                            "move_type": "entry",
                            "journal_id": self.run_rec.misc_journal_id.id,
                            "date": booking,
                            "ref": ref,
                            "line_ids": [
                                Command.create(
                                    {
                                        "name": ref,
                                        "account_id": expense.id,
                                        "debit": _money(amount),
                                        "credit": 0.0,
                                    }
                                ),
                                Command.create(
                                    {
                                        "name": ref,
                                        "account_id": asset_account.id,
                                        "debit": 0.0,
                                        "credit": _money(amount),
                                    }
                                ),
                            ],
                        }
                    )
                )
                move.action_post()
                self.counts["depreciations"] += 1
