# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Matrix of bank reconciliation variants (B-13, spec 9.5 "Matrix").

The core scenarios T01, T03, T05, T05b, T09, T15, T16 and T29 give the same
EÜR values, however the money reaches the documents:

* ``standard``: the bank statement line is reconciled with the invoice (the
  structure of Odoo's bank reconciliation and of OCA ``edit`` mode);
* ``payment``: payment wizard with outstanding account, the statement line
  is reconciled with the payment (a transit entry);
* ``in_payment``: payments without journal entry (``account_in_payment``),
  the statement line is reconciled with the invoice;
* ``keep``: OCA ``keep`` mode: the statement line stays on the reconcilable
  suspense account, a separate entry with a later date reconciles it;
* ``oca_edit`` and ``oca_keep``: the OCA module ``account_reconcile_oca``
  itself (only if installed, in the CI job with the OCA modules);
* OCA ``account_payment_order`` (only if installed): transfer and direct
  debit with and without outstanding account (ST-B14).
"""

import datetime
from contextlib import contextmanager
from unittest.mock import patch

from odoo.tests import Form, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from .scenario_common import SKR03_ACCOUNTS, SKR03_TAXES, ScenarioCommon, euro

KU = "EUER.20.111"
TAXABLE = "EUER.20.112"
OUTPUT_VAT = "EUER.20.140"
CONTRIBUTION = "EUER.29.123"


class MatrixScenarios(ScenarioCommon):
    VARIANT = "standard"

    def setUp(self):
        super().setUp()
        if self.VARIANT.startswith("oca") and not self._installed(
            "account_reconcile_oca"
        ):
            self.skipTest("account_reconcile_oca is not installed")
        if self.VARIANT in ("keep", "oca_keep"):
            self.bank_journal.suspense_account_id.reconcile = True
        if self.VARIANT == "oca_keep":
            self.bank_journal.reconcile_mode = "keep"
        if self.VARIANT == "oca_edit":
            self.bank_journal.reconcile_mode = "edit"
        if self.VARIANT.startswith("oca"):
            # partial amounts are entered in fields the widget shows itself
            self.env["ir.ui.view"].create(
                {
                    "name": "EÜR test: reconciliation fields",
                    "model": "account.bank.statement.line",
                    "inherit_id": self.env.ref(
                        "account_reconcile_oca.bank_statement_line_form_reconcile_view"
                    ).id,
                    "arch": """
                    <data>
                        <field name="manual_reference" position="attributes">
                            <attribute name="invisible">0</attribute>
                        </field>
                        <field name="manual_amount" position="attributes">
                            <attribute name="invisible">0</attribute>
                            <attribute name="readonly">0</attribute>
                        </field>
                    </data>
                    """,
                }
            )

    def _installed(self, module):
        return bool(
            self.env["ir.module.module"].search(
                [("name", "=", module), ("state", "=", "installed")]
            )
        )

    @contextmanager
    def _in_payment(self):
        """Payments without journal entry, as with ``account_in_payment``."""
        methods = (
            self.bank_journal.inbound_payment_method_line_ids
            | self.bank_journal.outbound_payment_method_line_ids
        )
        methods.payment_account_id = False
        with patch.object(
            type(self.env["account.move"]),
            "_get_invoice_in_payment_state",
            lambda _self: "in_payment",
        ):
            yield

    # ------------------------------------------------------------ variants
    def receive(self, date, items, bank_amount=None):
        """Money of ``bank_amount`` for ``items`` = ``[(document, amount)]``."""
        total = sum(amount for _doc, amount in items)
        bank_amount = total if bank_amount is None else bank_amount
        sign = 1 if items[0][0].move_type in ("out_invoice", "in_refund") else -1
        variant = self.VARIANT
        if variant == "standard":
            return self.bank(date, sign * bank_amount, matches=items)
        if variant == "payment":
            documents = self.env["account.move"].concat(*(doc for doc, _a in items))
            values = {"group_payment": True} if len(items) > 1 else {}
            if len(items) == 1:
                values["amount"] = items[0][1]
            payments = self.pay(documents, date, **values)
            return self.bank_for_payment(date, payments, amount=bank_amount)
        if variant == "in_payment":
            documents = self.env["account.move"].concat(*(doc for doc, _a in items))
            with self._in_payment():
                payments = self.pay(
                    documents,
                    date,
                    **({"group_payment": True} if len(items) > 1 else {}),
                )
                self.assertFalse(payments.move_id)
                return self.bank(date, sign * bank_amount, matches=items)
        if variant == "keep":
            return self._receive_keep(date, items, sign, bank_amount)
        return self._receive_oca(date, items, sign, bank_amount)

    def _receive_keep(self, date, items, sign, bank_amount):
        suspense = self.bank_journal.suspense_account_id
        statement = self.bank(date, sign * bank_amount)
        later = datetime.date.fromisoformat(date) + datetime.timedelta(days=3)
        lines = [(suspense, sign * bank_amount)]
        for document, amount in items:
            line = self.open_line(document)
            lines.append(
                (
                    line.account_id,
                    -amount if line.balance > 0 else amount,
                    {"partner_id": line.partner_id.id},
                )
            )
        rest = round(-sum(line[1] for line in lines), 2)
        if rest:
            lines.append((suspense, rest))
        keep = self.entry(later.isoformat(), lines)
        (
            statement.move_id.line_ids.filtered(
                lambda line: line.account_id == suspense
            )
            | keep.line_ids.filtered(
                lambda line: line.account_id == suspense
                and line.balance == sign * bank_amount
            )
        ).reconcile()
        targets = keep.line_ids.filtered(
            lambda line: line.account_id != suspense
        ).sorted("id")
        for (document, _amount), line in zip(items, targets, strict=True):
            (line | self.open_line(document)).reconcile()
        return statement

    def _receive_oca(self, date, items, sign, bank_amount):
        statement = self.bank(date, sign * bank_amount)
        with Form(
            statement,
            view="account_reconcile_oca.bank_statement_line_form_reconcile_view",
        ) as form:
            for document, amount in items:
                line = self.open_line(document)
                form.add_account_move_line_id = line
                if amount != abs(line.amount_residual):
                    form.manual_reference = f"account.move.line;{line.id}"
                    form.manual_amount = -sign * amount
        statement.reconcile_bank_line()
        return statement

    # ------------------------------------------------------------ scenarios
    def test_matrix_t01(self):
        invoice = self.sale("2026-03-15", 500)
        self.receive("2026-04-02", [(invoice, 500)])
        self.assertFields(2026, {KU: 500})
        self.assertEqual(self.codes(2026, "blocker"), [])

    def test_matrix_t03_partial_payments(self):
        self.set_regime("regular")
        invoice = self.invoice(
            "out_invoice",
            "2025-11-10",
            [("revenue_19", 1000, ["ust19"]), ("revenue_7", 200, ["ust7"])],
        )
        self.receive("2025-12-01", [(invoice, 500)])
        self.receive("2026-01-15", [(invoice, 904)])
        self.assertFields(2025, {TAXABLE: "427.35", OUTPUT_VAT: "72.65"})
        self.assertFields(2026, {TAXABLE: "772.65", OUTPUT_VAT: "131.35"})

    def test_matrix_t05_collective(self):
        invoices = [self.sale("2026-05-01", amount) for amount in (100, 200, 300)]
        self.receive(
            "2026-06-01", [(invoice, invoice.amount_total) for invoice in invoices]
        )
        self.assertFields(2026, {KU: 600})

    def test_matrix_t05b_collective_partly(self):
        if self.VARIANT != "payment":
            self.skipTest("partial bank matching of a collective payment")
        invoices = [self.sale("2026-05-01", amount) for amount in (100, 200, 300)]
        self.receive(
            "2026-06-01",
            [(invoice, invoice.amount_total) for invoice in invoices],
            bank_amount=300,
        )
        self.assertFields(2026, {KU: 300})
        self.assertIn("C-27", self.codes(2026))

    def test_matrix_t09_discount(self):
        self.set_regime("regular")
        invoice = self.invoice(
            "out_invoice", "2026-03-01", [("revenue_19", 1000, ["ust19"])]
        )
        if self.VARIANT in ("standard", "keep", "oca_edit", "oca_keep", "in_payment"):
            self.bank(
                "2026-03-08",
                1166.20,
                matches=[(invoice, 1190)],
                extra=[
                    ("discount_19", 20, self.base_line("ust19")),
                    ("vat_out", 3.80, self.tax_line("ust19", "refund")),
                ],
            )
        else:
            payment = self.pay(
                invoice,
                "2026-03-08",
                amount=1166.20,
                payment_difference_handling="reconcile",
                writeoff_account_id=self.acc("discount_19").id,
                writeoff_label="Discount",
            )
            self.bank_for_payment("2026-03-10", payment)
        fields = self.fields(2026)
        self.assertEqual(sum(fields.values()), euro("1166.20"))

    def test_matrix_t15_paid_privately(self):
        bill = self.purchase("2026-03-01", 50)
        private = self.entry(
            "2026-03-01",
            [(self.open_line(bill).account_id, 50), ("private_c", -50)],
            partner=self.supplier,
        )
        self.reconcile(bill, private)
        self.assertFields(
            2026, {self.field_of("EXP_WORK_EQUIPMENT"): 50, CONTRIBUTION: 50}
        )

    def test_matrix_t16_offsetting(self):
        invoice = self.sale("2026-05-01", 500)
        bill = self.purchase("2026-05-03", 200, partner=self.customer)
        offset = self.entry(
            "2026-05-10",
            [
                (self.open_line(bill).account_id, 200),
                (self.open_line(invoice).account_id, -200),
            ],
            partner=self.customer,
        )
        self.reconcile(bill, offset.line_ids.filtered(lambda line: line.debit))
        self.reconcile(invoice, offset.line_ids.filtered(lambda line: line.credit))
        self.receive("2026-05-20", [(invoice, 300)])
        self.assertFields(2026, {KU: 500, self.field_of("EXP_WORK_EQUIPMENT"): 200})

    def test_matrix_t29_turn_of_year(self):
        """Money on 30 December, reconciled in January: receipt in December."""
        invoice = self.sale("2026-11-15", 500)
        self.receive("2026-12-30", [(invoice, 500)])
        self.assertFields(2026, {KU: 500})
        self.assertFields(2027, {})


def _matrix_class(name, variant):
    @tagged("post_install_l10n", "post_install", "-at_install")
    class Matrix(MatrixScenarios, AccountTestInvoicingCommon):
        allow_inherited_tests_method = True
        CHART = "de_skr03"
        ACCOUNTS = SKR03_ACCOUNTS
        TAXES = SKR03_TAXES
        VARIANT = variant

        @classmethod
        @AccountTestInvoicingCommon.setup_country("de")
        @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
        def setUpClass(cls):
            super().setUpClass()
            cls.setup_euer()

    Matrix.__name__ = Matrix.__qualname__ = name
    return Matrix


TestMatrixStandard = _matrix_class("TestMatrixStandard", "standard")
TestMatrixPayment = _matrix_class("TestMatrixPayment", "payment")
TestMatrixInPayment = _matrix_class("TestMatrixInPayment", "in_payment")
TestMatrixKeep = _matrix_class("TestMatrixKeep", "keep")
TestMatrixOcaEdit = _matrix_class("TestMatrixOcaEdit", "oca_edit")
TestMatrixOcaKeep = _matrix_class("TestMatrixOcaKeep", "oca_keep")


@tagged("post_install_l10n", "post_install", "-at_install")
class TestPaymentOrder(ScenarioCommon, AccountTestInvoicingCommon):
    """ST-B14: OCA payment orders, transfer and direct debit (U-23)."""

    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_euer()

    def setUp(self):
        super().setUp()
        if "account.payment.order" not in self.env:
            self.skipTest("account_payment_order is not installed")
        self.env.user.group_ids |= self.env.ref(
            "account_payment_order.group_account_payment"
        )

    def _mode(self, payment_type, outstanding):
        method = self.env.ref(
            "account.account_payment_method_manual_out"
            if payment_type == "outbound"
            else "account.account_payment_method_manual_in"
        )
        lines = (
            self.bank_journal.outbound_payment_method_line_ids
            if payment_type == "outbound"
            else self.bank_journal.inbound_payment_method_line_ids
        ).filtered(lambda line: line.payment_method_id == method)
        if not outstanding:
            lines.payment_account_id = False
        return self.env["account.payment.mode"].create(
            {
                "name": f"Mode {payment_type}",
                "company_id": self.company.id,
                "bank_account_link": "fixed",
                "fixed_journal_id": self.bank_journal.id,
                "payment_method_id": method.id,
                "default_date_prefered": "fixed",
            }
        )

    def _order(self, document, mode):
        from freezegun import freeze_time

        document.payment_mode_id = mode
        if not document.ref:
            document.ref = f"REF-{document.id}"
        with freeze_time("2026-12-30"):
            document.create_account_payment_line()
            order = self.env["account.payment.order"].search(
                [("payment_mode_id", "=", mode.id), ("state", "=", "draft")]
            )
            order.date_scheduled = "2026-12-30"
            order.draft2open()
            order.open2generated()
            order.generated2uploaded()
        self.assertEqual(order.date_uploaded, datetime.date(2026, 12, 30))
        return order

    def _check_order(self, document, amount, outstanding, field):
        payment_type = "outbound" if amount < 0 else "inbound"
        order = self._order(document, self._mode(payment_type, outstanding))
        payments = order.payment_ids
        if payments.move_id:
            statement = self.bank_for_payment("2027-01-02", payments)
        else:
            statement = self.bank(
                "2027-01-02", amount, matches=[(document, abs(amount))]
            )
        self.compute(2027)
        item = self.env["l10n_de_euer.ten_day.item"].search(
            [("source_move_id", "=", statement.move_id.id)]
        )
        if amount < 0:
            self.assertIn("C-23", self.codes(2027))
            self.assertEqual(item.proposed_cash_date, datetime.date(2026, 12, 30))
            self.assertFields(2027, {field: abs(amount)})
            item.action_use_proposed_date()
            self.assertFields(2026, {field: abs(amount)})
            self.assertFields(2027, {})
        else:
            # direct debit received: money in January, receipt in January
            self.assertFields(2027, {field: amount})

    def test_b14_transfer_with_outstanding_account(self):
        bill = self.purchase("2026-12-01", 100)
        self._check_order(bill, -100, True, self.field_of("EXP_WORK_EQUIPMENT"))

    def test_b14_transfer_without_outstanding_account(self):
        bill = self.purchase("2026-12-01", 100)
        self._check_order(bill, -100, False, self.field_of("EXP_WORK_EQUIPMENT"))

    def test_b14_direct_debit_with_outstanding_account(self):
        invoice = self.sale("2026-12-01", 100)
        self._check_order(invoice, 100, True, KU)

    def test_b14_direct_debit_without_outstanding_account(self):
        invoice = self.sale("2026-12-01", 100)
        self._check_order(invoice, 100, False, KU)

    def test_b14_cancelled_order(self):
        """s74: a cancelled order keeps its dates and lines, but it was not
        handed over: no proposal from it."""
        bill = self.purchase("2026-12-01", 100)
        order = self._order(bill, self._mode("outbound", False))
        order.write({"state": "cancel"})
        payments = order.payment_ids
        if payments.move_id:
            statement = self.bank_for_payment("2027-01-02", payments)
        else:
            statement = self.bank("2027-01-02", -100, matches=[(bill, 100)])
        self.compute(2027)
        item = self.env["l10n_de_euer.ten_day.item"].search(
            [("source_move_id", "=", statement.move_id.id)]
        )
        self.assertIn("C-23", self.codes(2027))
        self.assertFalse(item.proposed_cash_date)
