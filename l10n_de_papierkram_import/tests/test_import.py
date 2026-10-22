# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Import of fictitious Papierkram exports into an SKR03 company."""

import base64
import os
import tempfile
from datetime import date

from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from .common import KATEGORIEN, make_export

ASSETS = (
    "\nBelegnummer;Beleg;Restwert Jahresende;Anschaffungsdatum;Anschaffungswert;"
    "monatl. Abschreibung\n"
    "B-00002;Beamer;600,00;12.07.2024;840,00;20,00\n"
)


@tagged("post_install_l10n", "post_install", "-at_install")
class TestPapierkramImport(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        d = cls.tmp.name
        p2024 = make_export(
            d,
            2024,
            rechnungen=[
                # 300 €: 100 € paid in December, the rest only known from the
                # last payment date in January
                "R-00010;Workshop;Muster GmbH;Deutschland;;K-00001;20.12.2024;"
                "30.12.2024;Bezahlt;Rechnung;300,00;0,00;300,00;0,00;300,00;"
                "2025-01-03;;;",
                # cancelled, cancellation in the following year
                "R-00011;Workshop (storniert);Muster GmbH;Deutschland;;K-00001;"
                "21.12.2024;;"
                "Storniert;Rechnung;50,00;0,00;50,00;0,00;0,00;;;;",
                # cash discount: invoice of 100 €, 90 € paid
                "R-00012;Lektorat;Muster GmbH;Deutschland;;K-00001;01.10.2024;;"
                "Bezahlt;Rechnung;100,00;0,00;100,00;10,00;90,00;2024-10-05;;;",
                # drafts are not imported
                ";Entwurf;Muster GmbH;Deutschland;;K-00001;22.12.2024;;"
                "Entwurf;Rechnung;10,00;0,00;10,00;0,00;0,00;;;;",
            ],
            belege=[
                # split voucher with a fixed asset, paid without payment
                # assignment; the second category has a comma in its name
                "B-00002;Beamer;Technik AG;12.07.2024;;2024-07-15;paid;-849,95;0,00;"
                "-849,95;0;domestic;"
                "Betriebsausstattung (Anlage), Zeitschriften, Bücher;;;;;",
                # cancelled voucher with payment and refund
                "B-00003;Seminar;Akademie AG;15.08.2024;;2024-08-15;canceled;-12,34;"
                "0,00;-12,34;0;domestic;Sonstige betriebliche Aufwendungen;;;;;",
                # deleted without payment
                "B-00004;Irrtum;Akademie AG;02.07.2024;;;deleted;-5,00;0,00;-5,00;0;"
                "domestic;Porto;;;;;",
            ],
            zahlungen=[
                "Zahlung zu R-00010;K-00001;R-00010;;2024-12-28;100,00",
                "Zahlung zu R-00012;K-00001;R-00012;;2024-10-05;90,00",
                "Zahlung zu B-00003;;;B-00003;2024-08-15;-12,34",
            ],
            pdfs=[
                (
                    "Rechnungen/Rechnung_R-00010_Muster_GmbH_2024-12-20.pdf",
                    b"%PDF-1.4 R10",
                )
            ],
        )
        p2025 = make_export(
            d,
            2025,
            rechnungen=[
                "R-00011-S;Workshop;Muster GmbH;Deutschland;;K-00001;02.01.2025;;"
                "Bezahlt;Stornorechnung;-50,00;0,00;-50,00;-50,00;0,00;;;;",
            ],
            zahlungen=["Zahlung zu B-00003 (storniert);;;B-00003;2025-04-10;12,34"],
        )
        attachments = cls.env["ir.attachment"]
        for path in (p2024, p2025):
            with open(path, "rb") as fh:
                attachments |= cls.env["ir.attachment"].create(
                    {
                        "name": os.path.basename(path),
                        "datas": base64.b64encode(fh.read()),
                    }
                )
        splits = (
            "belegnummer;position;kategorie;betrag_brutto\n"
            "B-00002;1;Betriebsausstattung (Anlage);840.00\n"
            "B-00002;2;Zeitschriften, Bücher;9.95\n"
        )
        cls.pk_run = cls.env["papierkram.import"].create(
            {
                "export_attachment_ids": [(6, 0, attachments.ids)],
                "categories_file": base64.b64encode(KATEGORIEN.encode()),
                "assets_file": base64.b64encode(ASSETS.encode()),
                "splits_file": base64.b64encode(splits.encode()),
                "depreciation_until": date(2025, 12, 31),
            }
        )
        cls.pk_run.action_check()
        cls.pk_run.action_import()
        cls.moves = cls.pk_run.move_ids

    def _move(self, number, move_type=None):
        moves = self.moves.filtered(lambda m: m.papierkram_number == number)
        if move_type:
            moves = moves.filtered(lambda m: m.move_type == move_type)
        self.assertEqual(len(moves), 1, number)
        return moves

    def _money_lines(self):
        account = self.pk_run.money_journal_id.default_account_id
        return self.env["account.move.line"].search(
            [("account_id", "=", account.id), ("parent_state", "=", "posted")]
        )

    def test_state_and_journals(self):
        self.assertEqual(self.pk_run.state, "done")
        self.assertEqual(self.pk_run.money_journal_id.type, "bank")
        for line in (
            self.pk_run.money_journal_id.inbound_payment_method_line_ids
            | self.pk_run.money_journal_id.outbound_payment_method_line_ids
        ):
            self.assertEqual(
                line.payment_account_id, self.pk_run.money_journal_id.default_account_id
            )

    def test_invoice_payments_are_money_lines_with_their_date(self):
        inv = self._move("R-00010", "out_invoice")
        self.assertEqual(inv.payment_state, "paid")
        self.assertEqual(inv.amount_total, 300.0)
        self.assertEqual(inv.invoice_line_ids.account_id.code, "8195")
        self.assertFalse(inv.invoice_line_ids.tax_ids)
        money = {
            (line.date, line.balance)
            for line in self._money_lines()
            if line.move_id.payment_ids.memo == "R-00010"
            or "R-00010" in (line.move_id.ref or "")
        }
        self.assertIn((date(2024, 12, 28), 100.0), money)
        self.assertIn((date(2025, 1, 3), 200.0), money)
        self.assertTrue(inv.message_main_attachment_id)

    def test_cancellation_reverses_on_storno_date(self):
        inv = self._move("R-00011", "out_invoice")
        storno = self._move("R-00011-S", "out_refund")
        self.assertEqual(storno.date, date(2025, 1, 2))
        self.assertEqual(storno.reversed_entry_id, inv)
        self.assertEqual(inv.payment_state, "reversed")

    def test_cash_discount(self):
        inv = self._move("R-00012", "out_invoice")
        discount = self._move("R-00012-SKONTO", "out_refund")
        self.assertEqual(discount.amount_total, 10.0)
        self.assertEqual(discount.invoice_line_ids.account_id.code, "8730")
        self.assertEqual(inv.payment_state, "paid")

    def test_split_voucher_and_asset(self):
        bill = self._move("B-00002", "in_invoice")
        self.assertEqual(
            sorted(
                bill.invoice_line_ids.mapped(
                    lambda line: (line.account_id.code, line.price_total)
                )
            ),
            [("0400", 840.0), ("4940", 9.95)],
        )
        self.assertEqual(bill.payment_state, "paid")
        paid = bill.line_ids.filtered(
            lambda line: line.account_id.account_type == "liability_payable"
        )
        self.assertTrue(paid.full_reconcile_id)

    def test_canceled_voucher_with_refund(self):
        bill = self._move("B-00003", "in_invoice")
        refund = self._move("B-00003-S", "in_refund")
        self.assertEqual(refund.date, date(2025, 4, 10))
        self.assertEqual(refund.reversed_entry_id, bill)
        self.assertFalse(bill.amount_residual)
        self.assertFalse(refund.amount_residual)
        money = sorted(
            (line.date, line.balance)
            for line in self._money_lines()
            if line.move_id.payment_ids.memo in ("B-00003", "B-00003-S")
        )
        self.assertEqual(
            money, [(date(2024, 8, 15), -12.34), (date(2025, 4, 10), 12.34)]
        )

    def test_skipped(self):
        self.assertFalse(
            self.moves.filtered(lambda m: m.papierkram_number == "B-00004")
        )
        self.assertFalse(self.moves.filtered(lambda m: m.ref == "Entwurf"))

    def test_depreciation_schedule(self):
        dep = self.moves.filtered(
            lambda m: m.move_type == "entry" and m.ref.startswith("AfA")
        )
        by_year = {m.date.year: m.amount_total for m in dep}
        # 840 € / 42 months = 20 € per month; 2024: July to December = 6 months
        self.assertEqual(by_year, {2024: 120.0, 2025: 240.0})
        lines = dep.line_ids.filtered(lambda line: line.debit)
        self.assertEqual(set(lines.account_id.mapped("code")), {"4830"})

    def test_rerun_does_not_duplicate(self):
        again = self.pk_run.copy({"state": "draft"})
        again.action_check()
        again.action_import()
        self.assertFalse(
            again.move_ids.filtered(
                lambda m: m.move_type != "entry" or m.ref.startswith("AfA")
            )
        )
