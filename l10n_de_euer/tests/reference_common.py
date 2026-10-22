# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Reference cases PT-01 and RT-01 from JSON files (spec 9.8, decision D6).

A case file in ``tests/data`` holds the business, its business events and
the expected values per year and field code. The expected values are
calculated by hand from the law, the spec and the research notes (never
from the output of the module); every value lists its terms, so a second
calculation can follow it from the file alone.

Format (all amounts in euro as strings, dates ISO):

``company``
    header values, settings and the start of the activity.
``vat_periods``
    VAT status periods (regime, return period, permanent extension, the
    total turnover of the previous year entered after a takeover with the
    year it is entered for).
``partners``, ``journals``
    synthetic partners (key → name, tax office) and extra money journals.
``accounts``, ``tax_keys``
    names of the accounts of the chart and of the taxes used (for the reader
    and a second calculation; the test does not need them).
``events``
    business events in the order of booking:

    * ``invoice``: ``move_type``, ``date``, ``partner``, ``lines`` with
      ``account`` (code of the chart), ``category`` (EÜR category of the
      account, for the reader), ``amount`` (net price), ``taxes`` (keys of
      :data:`.scenario_common.SKR03_TAXES`), optional ``deductible`` (%),
      ``quantity``; optional ``due``, ``delivery``, ``gift_recipient``.
    * ``bank``: statement line of ``journal`` (default the bank account) on
      ``date`` with ``amount`` (positive = money in), ``matches`` (open
      items of events with the amount reconciled), ``lines`` (counterpart
      lines with ``account`` and ``amount`` as balance, optional
      ``tax_base``/``tax_line`` with ``document_type``), ``vat_period`` of a
      VAT payment.
    * ``entry``: journal entry with ``lines`` (``account``, ``amount`` as
      balance, ``partner``, ``reconcile`` with the open item of an event).
    * ``reconcile``: reconcile the open items of two events (credit note
      against invoice) without money.
    * ``asset``: asset of the register with its ``values`` (field names of
      ``l10n_de_euer.asset``), ``lines`` (events whose lines on ``account``
      are its purchase) and an optional ``disposal``.
    * ``asset_disposal``: disposal of an asset booked before (``asset``),
      e.g. a sale with its ``invoice``.
    * ``opening_item``: category, VAT treatment and rate of an open item of
      an opening entry (``line``: event, account, partner).
    * ``classification``: classification of an unallocated rest (``line``).
    * ``adjustment``: profit correction of the statement of ``year``.
    * ``ten_day``: decision of the review list of the turn of the year for
      ``payment`` (and ``document``), with ``reason``.

``expected``
    year → field code → ``value`` with ``terms`` ``[amount, explanation]``
    whose sum is the value. Field codes are stable over the form years
    (spec 3.1); every field with a value in the statement is listed.
``checks``
    year → blocking checks of the calculation expected (normally none) and
    the warnings of the checks of the statement: exactly these codes, apart
    from the checks of the environment of a test (:data:`ENVIRONMENT_CHECKS`).
"""

import json
from decimal import Decimal

from odoo import Command
from odoo.tools import file_open

from .scenario_common import SKR03_TAXES
from .statement_common import StatementCommon

D = Decimal
CENT = D("0.01")
NON_AMOUNT_TYPES = ("text", "key", "date")
OPEN_ITEM_TYPES = {"receivable": "asset_receivable", "payable": "liability_payable"}
#: checks of the environment of a test, not of the case: documents without
#: file (C-16), bank reconciliation (C-38), questionnaire (C-45), lock date
#: (C-70) and release of the form years (C-72)
ENVIRONMENT_CHECKS = frozenset({"C-16", "C-38", "C-45", "C-70", "C-72"})


def amount(value):
    return D(str(value)).quantize(CENT)


class ReferenceCase(StatementCommon):
    """Books a reference case from its file and compares the statements."""

    DATA = None
    TAXES = SKR03_TAXES

    @classmethod
    def load_case(cls):
        with file_open(f"l10n_de_euer/tests/data/{cls.DATA}") as file:
            return json.load(file)

    # ------------------------------------------------------------ master data
    def setup_case(self, case):
        self.case = case
        self.refs = {}
        self.case_partners = {}
        self.case_journals = {"bank": self.bank_journal, "cash": self.cash_journal}
        company = case["company"]
        self.company.write(
            {
                "name": company["name"],
                "l10n_de_euer_business_type": company["business_type"],
                "l10n_de_euer_legal_form": company["legal_form"],
                "l10n_de_euer_income_type": company["income_type"],
                "l10n_de_euer_owner": company.get("owner", "1"),
                "l10n_de_euer_entertainment_mode": company.get(
                    "entertainment_mode", "separate"
                ),
                "l10n_de_euer_disposal_month": company.get("disposal_month", True),
                "l10n_de_euer_activity_start": company.get("activity_start") or False,
            }
        )
        if company.get("private_share_account"):
            self.company_data[
                "default_journal_purchase"
            ].non_deductible_account_id = self.account(company["private_share_account"])
        Period = self.env["l10n_de_euer.vat.period"]
        Period.search([("company_id", "=", self.company.id)]).unlink()
        for period in case["vat_periods"]:
            Period.create(
                {
                    "company_id": self.company.id,
                    "date_from": period["date_from"],
                    "date_to": period.get("date_to") or False,
                    "regime": period["regime"],
                    "vat_return_period": period.get("vat_return_period", "monthly"),
                    "permanent_extension": period.get("permanent_extension", False),
                    "prev_year_turnover": float(period.get("prev_year_turnover", 0)),
                    "prev_year_turnover_year": period.get("prev_year_turnover_year", 0),
                }
            )
        for key, partner in case.get("partners", {}).items():
            self.case_partners[key] = self.env["res.partner"].create(
                {
                    "name": partner["name"],
                    "l10n_de_euer_tax_office": partner.get("tax_office", False),
                }
            )
        immediate = self.env.ref("account.account_payment_term_immediate")
        for partner in self.case_partners.values():
            partner.with_company(self.company).write(
                {
                    "property_payment_term_id": immediate.id,
                    "property_supplier_payment_term_id": immediate.id,
                }
            )
        for key, journal in case.get("journals", {}).items():
            self.case_journals[key] = self.env["account.journal"].create(
                {
                    "name": journal["name"],
                    "code": journal["code"],
                    "type": journal["type"],
                    "l10n_de_euer_private_account": journal.get(
                        "private_account", False
                    ),
                }
            )

    def account(self, code, partner=None):
        """Account by its code in the chart; ``receivable`` and ``payable``
        are the accounts of the partner."""
        if code in ("receivable", "payable") and partner:
            partner = partner.with_company(self.company)
            return (
                partner.property_account_receivable_id
                if code == "receivable"
                else partner.property_account_payable_id
            )
        if code in ("receivable", "payable", "bank", "cash", "transfer", "suspense"):
            return self.acc(code)
        return self.template_account(f"account_{code}")

    def partner_of(self, key):
        return self.case_partners[key] if key else self.env["res.partner"]

    def line_of(self, spec):
        """Journal item of an event: ``{"event", "account", "partner"}``."""
        record = self.refs[spec["event"]]
        move = record.move_id if record._name != "account.move" else record
        lines = move.line_ids
        if spec.get("account") in OPEN_ITEM_TYPES:
            account_type = OPEN_ITEM_TYPES[spec["account"]]
            lines = lines.filtered(
                lambda line: line.account_id.account_type == account_type
            )
        elif spec.get("account"):
            account = self.account(spec["account"])
            lines = lines.filtered(lambda line: line.account_id == account)
        if spec.get("partner"):
            partner = self.partner_of(spec["partner"])
            lines = lines.filtered(lambda line: line.partner_id == partner)
        self.assertEqual(len(lines), 1, f"one journal item of {spec}")
        return lines

    def open_item(self, ref):
        """Open item of an event (document or a line given as dict)."""
        if isinstance(ref, dict):
            return self.line_of(ref)
        record = self.refs[ref]
        move = record.move_id if record._name != "account.move" else record
        return self.open_line(move)

    # ------------------------------------------------------------ events
    def book_case(self):
        for event in self.case["events"]:
            handler = getattr(self, f"_book_{event['type']}")
            record = handler(event)
            if event.get("id"):
                self.refs[event["id"]] = record

    def _book_invoice(self, event):
        lines = []
        for index, spec in enumerate(event["lines"]):
            values = {
                "name": spec.get("text") or f"Position {index + 1}",
                "account_id": self.account(spec["account"]).id,
                "price_unit": float(spec["amount"]),
                "quantity": spec.get("quantity", 1),
                "tax_ids": [Command.set([self.tax(key).id for key in spec["taxes"]])],
            }
            if spec.get("deductible") is not None:
                values["deductible_amount"] = spec["deductible"]
            lines.append(Command.create(values))
        values = {
            "move_type": event["move_type"],
            "partner_id": self.partner_of(event["partner"]).id,
            "invoice_date": event["date"],
            "date": event["date"],
            "ref": event.get("id"),
            "invoice_line_ids": lines,
        }
        if event.get("delivery"):
            values["delivery_date"] = event["delivery"]
        move = self.env["account.move"].create(values)
        move.action_post()
        if event.get("due"):
            move.invoice_date_due = event["due"]
        if event.get("gift_recipient"):
            move.l10n_de_euer_gift_recipient_id = self.partner_of(
                event["gift_recipient"]
            )
        self.assertEqual(
            amount(move.amount_total),
            amount(event["total"]),
            f"total of {event['id']}",
        )
        return move

    def _counterpart(self, spec, partner=None):
        values = {}
        if spec.get("tax_base"):
            values.update(self.base_line(spec["tax_base"]))
        if spec.get("tax_line"):
            values.update(
                self.tax_line(spec["tax_line"], spec.get("document_type", "invoice"))
            )
        if spec.get("partner"):
            partner = self.partner_of(spec["partner"])
        if partner:
            values["partner_id"] = partner.id
        account = self.account(spec["account"], partner)
        return (account, float(spec["amount"]), values)

    def _book_bank(self, event):
        statement_line = self.bank(
            event["date"],
            float(event["amount"]),
            matches=[
                (self.open_item(match["event"]), float(match["amount"]))
                for match in event.get("matches", ())
            ],
            extra=[
                self._counterpart(spec, self.partner_of(event.get("partner")))
                for spec in event.get("lines", ())
            ],
            partner=self.partner_of(event.get("partner")) or None,
            journal=self.case_journals[event.get("journal", "bank")],
            ref=event.get("id") or "Bank",
        )
        if event.get("vat_period"):
            statement_line.move_id.l10n_de_euer_vat_period = event["vat_period"]
        return statement_line

    def _book_entry(self, event):
        partner = self.partner_of(event.get("partner"))
        lines = []
        reconcile = []
        for spec in event["lines"]:
            lines.append(self._counterpart(spec, partner))
            reconcile.append(spec.get("reconcile"))
        move = self.entry(
            event["date"], lines, journal=self.case_journals.get(event.get("journal"))
        )
        for line, ref in zip(move.line_ids.sorted("id"), reconcile, strict=True):
            if ref:
                (line | self.open_item(ref)).reconcile()
        return move

    def _book_reconcile(self, event):
        lines = self.env["account.move.line"]
        for ref in event["events"]:
            lines |= self.open_item(ref)
        lines.reconcile()
        return lines.move_id[:1]

    def _book_asset(self, event):
        values = dict(event["values"], company_id=self.company.id)
        purchase = self.env["account.move.line"]
        for ref in event.get("lines", ()):
            purchase |= self.line_of(ref)
        if purchase:
            values["move_line_ids"] = [Command.set(purchase.ids)]
        asset = self.env["l10n_de_euer.asset"].create(values)
        asset.action_activate()
        disposal = event.get("disposal")
        if disposal:
            values = {
                "disposal_date": disposal["date"],
                "disposal_kind": disposal["kind"],
                "disposal_value": float(disposal["value"]),
            }
            if disposal.get("invoice"):
                values["disposal_invoice_id"] = self.refs[disposal["invoice"]].id
            asset.write(values)
            if disposal.get("vat") is not None:
                asset.disposal_vat_amount = float(disposal["vat"])
            asset.action_dispose()
        return asset

    def _book_asset_disposal(self, event):
        """Disposal of an asset booked before (sale with its invoice)."""
        asset = self.refs[event["asset"]]
        values = {
            "disposal_date": event["date"],
            "disposal_kind": event["kind"],
            "disposal_value": float(event["value"]),
        }
        if event.get("invoice"):
            values["disposal_invoice_id"] = self.refs[event["invoice"]].id
        asset.write(values)
        asset.action_dispose()
        return asset

    def _category(self, code):
        category = self.category(code)
        self.assertTrue(category, f"category {code}")
        return category

    def _book_opening_item(self, event):
        return self.env["l10n_de_euer.opening.item"].create(
            {
                "company_id": self.company.id,
                "move_line_id": self.line_of(event["line"]).id,
                "category_id": self._category(event["category"]).id,
                "vat_regime": event["vat_regime"],
                "tax_rate": event.get("tax_rate", 0.0),
                "origin_reference": event.get("origin_reference"),
            }
        )

    def _book_classification(self, event):
        return self.env["l10n_de_euer.residual.classification"].create(
            {
                "company_id": self.company.id,
                "source_line_id": self.line_of(event["line"]).id,
                "kind": event["kind"],
                "category_id": self._category(event["category"]).id
                if event.get("category")
                else False,
                "vat_regime": event.get("vat_regime") or False,
                "tax_rate": event.get("tax_rate", 0.0),
                "reason": event["reason"],
            }
        )

    def _book_adjustment(self, event):
        # created with the statements of the case
        self.adjustment_events.append(event)

    def _book_ten_day(self, event):
        self.ten_day_events.append(event)

    # ------------------------------------------------------------ statements
    def statements_of_case(self):
        """Statements of the years of the case with their corrections and the
        decisions of the review list; computed."""
        years = sorted(int(year) for year in self.case["expected"])
        statements = {year: self.statement(year) for year in years}
        for event in self.adjustment_events:
            self.correction(
                statements[event["year"]],
                event["kind"],
                reason=event["reason"],
                **event["values"],
            )
        for year in years:
            self.Run._compute_year(self.company, year)
        Item = self.env["l10n_de_euer.ten_day.item"]
        for event in self.ten_day_events:
            domain = [
                ("company_id", "=", self.company.id),
                ("source_move_id", "=", self.refs[event["payment"]].move_id.id),
            ]
            if event.get("document"):
                domain.append(("origin_move_id", "=", self.refs[event["document"]].id))
            item = Item.search(domain)
            self.assertEqual(len(item), 1, f"review item of {event}")
            item.reason = event["reason"]
            if event["decision"] == "apply":
                item.action_apply()
            else:
                item.action_reject()
        for statement in statements.values():
            statement.action_compute()
        return statements

    def statement_values(self, statement):
        """Every field with a value in the statement (amounts)."""
        return {
            line.code: amount(line.amount)
            for line in statement.line_ids
            if line.has_value and line.field_type not in NON_AMOUNT_TYPES
        }

    def expected_values(self, year):
        return {
            code: amount(spec["value"])
            for code, spec in self.case["expected"][str(year)].items()
        }

    # ------------------------------------------------------------ test bodies
    def check_derivations(self):
        """Each expected value is the sum of its terms (hand calculation)."""
        for year, values in self.case["expected"].items():
            for code, spec in values.items():
                with self.subTest(year=year, code=code):
                    self.assertTrue(spec["terms"], "derivation missing")
                    self.assertEqual(
                        sum((amount(term[0]) for term in spec["terms"]), D("0.00")),
                        amount(spec["value"]),
                    )
                    self.assertTrue(spec.get("basis"), "legal basis missing")

    def check_categories(self):
        """The categories named in the file are the ones the module assigns
        to the accounts (the reader of the file and the module agree)."""
        Assignment = self.env["l10n_de_euer.account.assignment"]
        for event in self.case["events"]:
            for spec in event.get("lines", ()):
                if not isinstance(spec, dict) or not spec.get("category"):
                    continue
                account = self.account(spec["account"])
                assignment = Assignment.search(
                    [
                        ("company_id", "=", self.company.id),
                        ("account_id", "=", account.id),
                    ],
                    limit=1,
                )
                with self.subTest(event=event.get("id"), account=spec["account"]):
                    self.assertEqual(assignment.category_id.code, spec["category"])

    def check_statements(self, statements):
        for year, statement in statements.items():
            with self.subTest(year=year):
                self.assertEqual(
                    self.statement_values(statement), self.expected_values(year)
                )
                # the lines named in the file are the lines of the form year
                expected = self.case["expected"][str(year)]
                lines = {
                    line.code: line.line
                    for line in statement.line_ids
                    if line.code in expected
                }
                self.assertEqual(
                    lines, {code: spec["line"] for code, spec in expected.items()}
                )
                expected = self.case.get("checks", {}).get(str(year), {})
                # blockers of the calculation itself (the prerequisites of a
                # finalisation such as the questionnaire are no part of it)
                self.assertEqual(
                    self.codes(year, "blocker"), sorted(expected.get("blockers", ()))
                )
                statement.action_check()
                # exactly the warnings of the case: an unexpected warning is a
                # false alarm or an expectation missing in the file
                warnings = set(self.result_codes(statement, "warning"))
                self.assertEqual(
                    sorted(warnings - ENVIRONMENT_CHECKS),
                    sorted(expected.get("warnings", ())),
                )

    def run_case(self):
        self.adjustment_events = []
        self.ten_day_events = []
        self.setup_case(self.load_case())
        self.check_derivations()
        self.book_case()
        self.check_categories()
        statements = self.statements_of_case()
        self.check_statements(statements)
        return statements
