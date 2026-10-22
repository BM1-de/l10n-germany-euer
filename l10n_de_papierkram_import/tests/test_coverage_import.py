# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Import of a fictitious business with regular VAT, edge cases of the
reader and the checks before an import."""

import base64
import os
import tempfile
import unicodedata
import zipfile
from datetime import date
from decimal import Decimal

from odoo.exceptions import UserError
from odoo.tests import BaseCase, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from ..lib.reader import Contact, Invoice, PapierkramExport
from ..models.papierkram_import import _Plan
from .common import (
    H_BELEGE,
    H_KONTAKTE,
    H_RECHNUNGEN,
    H_ZAHLUNGEN,
    KATEGORIEN,
    make_export,
)

CATEGORIES = KATEGORIEN + (
    "GWG-Sammelposten;485;Sammelposten;NEUTRAL;GWG;ASSET\n"
    "Geringwertige Wirtschaftsgüter (GWG);480;GWG;NEUTRAL;INSTANT;ASSET\n"
    "EDV-Software;27;EDV-Software;NEUTRAL;AFA;ASSET\n"
)
ASSETS = (
    "\nBelegnummer;Beleg;Restwert Jahresende;Anschaffungsdatum;Anschaffungswert;"
    "monatl. Abschreibung\n"
    "B-00110;Regale;0,00;01.04.2025;500,00;0,00\n"
    "B-00111;Stuhl;0,00;01.04.2025;400,00;0,00\n"
    "B-00112;Lizenz;930,01;01.04.2025;1.200,00;33,33\n"
    "B-00113;Kleinteil;1,00;15.01.2025;1,00;0,10\n"
    "B-09998;Ohne Beleg;0,00;01.04.2025;100,00;0,00\n"
    "B-00100;Kein Anlagegut;0,00;01.04.2025;100,00;0,00\n"
)


def contact(name, kind, number, country="", vat=""):
    """Line of ``Kontakte.csv`` with postal country and VAT number."""
    cells = [name, kind, number, "", "", "", country] + [""] * 8 + [vat] + [""] * 18
    return ";".join(cells)


def build_export(directory, year, rechnungen=(), belege=(), zahlungen=(), pdfs=()):
    """Export with contacts of other countries and an invalid VAT number."""
    path = os.path.join(
        directory, f"export_firma_papierkram_2026-01-15_120000_csv_and_pdf_{year}.zip"
    )
    contacts = (
        contact("Muster GmbH", "Kunde", "K-00001", "Atlantis"),
        contact("Nordlicht SL", "Kunde", "K-00002", "Spain", "ES123"),
        contact("Porto AG", "Lieferant", "L-00001"),
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("CSV/Rechnungen.csv", "\n".join((H_RECHNUNGEN, *rechnungen)) + "\n")
        zf.writestr("CSV/Belege.csv", "\n".join((H_BELEGE, *belege)) + "\n")
        zf.writestr(
            "CSV/Zahlungseingänge.csv", "\n".join((H_ZAHLUNGEN, *zahlungen)) + "\n"
        )
        zf.writestr("CSV/Kontakte.csv", "\n".join((H_KONTAKTE, *contacts)) + "\n")
        for name, content in pdfs:
            zf.writestr(name, content)
    return path


def german(amount):
    """Amount of the export (``1.234,56``) as ``Decimal``."""
    return Decimal(amount.replace(".", "").replace(",", "."))


def invoice(number, customer, day, state, net, tax, paid="0,00", **more):
    """Line of ``Rechnungen.csv``; amounts as in the export (German)."""
    gross = (f"{german(net) + german(tax):.2f}").replace(".", ",")
    name, number_customer = customer
    return ";".join(
        (
            number,
            more.get("subject", "Workshop"),
            name,
            "Deutschland",
            "",
            number_customer,
            day,
            "",
            state,
            more.get("kind", "Rechnung"),
            net,
            tax,
            more.get("gross", gross),
            more.get("discount", "0,00"),
            paid,
            more.get("last_payment", ""),
            "",
            "",
            "",
        )
    )


def voucher(number, day, state, net, tax, category, paid_on="", private="0"):
    """Line of ``Belege.csv``; expenses negative, money coming in positive."""
    gross = (f"{german(net) + german(tax):.2f}").replace(".", ",")
    return (
        f"{number};Beleg {number};Porto AG;{day};;{paid_on};{state};{net};{tax};"
        f"{gross};{private};domestic;{category};;;;;"
    )


MUSTER = ("Muster GmbH", "K-00001")
NORDLICHT = ("Nordlicht SL", "K-00002")


def attach(env, path):
    with open(path, "rb") as fh:
        return env["ir.attachment"].create(
            {"name": os.path.basename(path), "datas": base64.b64encode(fh.read())}
        )


@tagged("post_install_l10n", "post_install", "-at_install")
class TestRegularVatImport(AccountTestInvoicingCommon):
    """A business with regular VAT: rates, cancellations, refunds, vouchers
    coming in, depreciation accounts and the notes of the import."""

    @classmethod
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        path = build_export(
            cls.tmp.name,
            2025,
            rechnungen=[
                invoice(
                    "R-00100",
                    MUSTER,
                    "01.02.2025",
                    "Bezahlt",
                    "100,00",
                    "19,00",
                    "119,00",
                    last_payment="2025-02-10",
                ),
                invoice(
                    "R-00101", NORDLICHT, "03.02.2025", "Unbezahlt", "100,00", "7,00"
                ),
                # 5 % is no rate of the chart: without tax, total differs
                invoice("R-00102", MUSTER, "05.02.2025", "Unbezahlt", "100,00", "5,00"),
                # cancelled without a cancellation document
                invoice("R-00103", MUSTER, "07.02.2025", "Storniert", "50,00", "9,50"),
                # cancelled by a cancellation that is still a draft (with PDF)
                invoice("R-00104", MUSTER, "10.02.2025", "Storniert", "50,00", "9,50"),
                invoice(
                    "R-00104-S",
                    MUSTER,
                    "12.02.2025",
                    "Entwurf",
                    "-50,00",
                    "-9,50",
                    kind="Stornorechnung",
                ),
                # customer without number: found by name
                invoice(
                    "R-00105",
                    ("Laufkundschaft", ""),
                    "14.02.2025",
                    "Unbezahlt",
                    "20,00",
                    "3,80",
                ),
                # cash discount with regular VAT
                invoice(
                    "R-00106",
                    MUSTER,
                    "01.03.2025",
                    "Bezahlt",
                    "100,00",
                    "19,00",
                    "107,10",
                    discount="11,90",
                    last_payment="2025-03-05",
                ),
                # paid twice, half of it paid back: the refund is not
                # reconciled with the invoice
                invoice(
                    "R-00107",
                    MUSTER,
                    "10.03.2025",
                    "Bezahlt",
                    "100,00",
                    "19,00",
                    "119,00",
                    last_payment="2025-03-15",
                ),
            ],
            belege=[
                voucher(
                    "B-00100",
                    "03.02.2025",
                    "paid",
                    "-100,00",
                    "-19,00",
                    "Sonstige betriebliche Aufwendungen",
                    paid_on="2025-02-03",
                    private="10,00",
                ),
                # two categories, no split, input VAT on both lines
                voucher(
                    "B-00101",
                    "04.02.2025",
                    "unpaid",
                    "-50,00",
                    "-9,50",
                    "Porto, Sonstige betriebliche Aufwendungen",
                ),
                # money coming in: an income voucher and a refund of costs
                voucher(
                    "B-00102",
                    "20.02.2025",
                    "paid",
                    "50,00",
                    "0,00",
                    "Sonstige Einnahme (Erlöse)",
                    paid_on="2025-02-20",
                ),
                voucher("B-00103", "21.02.2025", "unpaid", "10,00", "0,00", "Porto"),
                # two pages of PDF
                voucher("B-00104", "22.02.2025", "unpaid", "-5,00", "0,00", "Porto"),
                voucher(
                    "B-00110",
                    "01.04.2025",
                    "unpaid",
                    "-500,00",
                    "-95,00",
                    "GWG-Sammelposten",
                ),
                voucher(
                    "B-00111",
                    "01.04.2025",
                    "unpaid",
                    "-400,00",
                    "-76,00",
                    "Geringwertige Wirtschaftsgüter (GWG)",
                ),
                voucher(
                    "B-00112",
                    "01.04.2025",
                    "unpaid",
                    "-1.200,00",
                    "-228,00",
                    "EDV-Software",
                ),
                voucher(
                    "B-00113",
                    "15.01.2025",
                    "unpaid",
                    "-1,00",
                    "0,00",
                    "Betriebsausstattung (Anlage)",
                ),
            ],
            zahlungen=[
                "Zahlung zu R-00100;K-00001;R-00100;;2025-02-10;119,00",
                "Zahlung zu R-00106;K-00001;R-00106;;2025-03-05;107,10",
                "Zahlung zu R-00107;K-00001;R-00107;;2025-03-12;169,00",
                "Rückzahlung zu R-00107;K-00001;R-00107;;2025-03-15;-50,00",
                "Zahlung zu R-09999;K-00001;R-09999;;2025-03-20;10,00",
            ],
            pdfs=[
                (
                    "Rechnungen/Rechnung_R-00104-S_Muster_GmbH_2025-02-12.pdf",
                    b"%PDF-1.4 storno",
                ),
                ("Belege/B-00104_1.pdf", b"%PDF-1.4 page 1"),
                ("Belege/B-00104_2.pdf", b"%PDF-1.4 page 2"),
            ],
        )
        cls.attachment = attach(cls.env, path)
        cls.pk_run = cls.env["papierkram.import"].create(
            {
                "export_attachment_ids": [(6, 0, cls.attachment.ids)],
                "categories_file": base64.b64encode(CATEGORIES.encode()),
                "assets_file": base64.b64encode(ASSETS.encode()),
                "small_business": False,
                "depreciation_until": date(2025, 12, 31),
            }
        )
        cls.pk_run.action_check()
        cls.pk_run.action_import()
        cls.moves = cls.pk_run.move_ids
        cls.log = str(cls.pk_run.log)

    def _move(self, number, move_type=None):
        moves = self.moves.filtered(lambda m: m.papierkram_number == number)
        if move_type:
            moves = moves.filtered(lambda m: m.move_type == move_type)
        self.assertEqual(len(moves), 1, number)
        return moves

    def test_revenue_account_and_tax_by_rate(self):
        """19 % → 8400, 7 % → 8300 with their taxes; 5 % has no tax in the
        chart: 8200 without tax and a note on the differing total."""
        expected = {"R-00100": ("8400", 19.0), "R-00101": ("8300", 7.0)}
        for number, (code, rate) in expected.items():
            line = self._move(number, "out_invoice").invoice_line_ids
            self.assertEqual(line.account_id.code, code)
            self.assertEqual(line.tax_ids.amount, rate)
        odd = self._move("R-00102", "out_invoice")
        self.assertEqual(odd.invoice_line_ids.account_id.code, "8200")
        self.assertFalse(odd.invoice_line_ids.tax_ids)
        self.assertIn("No tax rate of 5 % found.", self.log)
        self.assertIn("R-00102: total in Odoo 100.0, in Papierkram 105.00", self.log)

    def test_cancellations(self):
        """Without cancellation document the invoice is reversed on its own
        date; a cancellation that is only a draft is posted with its PDF."""
        plain = self._move("R-00103-S", "out_refund")
        self.assertEqual(plain.date, date(2025, 2, 7))
        self.assertIn("R-00103 is cancelled but Papierkram contains no", self.log)
        draft = self._move("R-00104-S", "out_refund")
        self.assertEqual(draft.date, date(2025, 2, 12))
        self.assertTrue(draft.message_main_attachment_id)
        self.assertIn("Cancellation R-00104-S is only a draft", self.log)

    def test_partners(self):
        """Contacts: an unknown country and an invalid VAT number are only
        reported (the number is not stored); a customer without number is
        found or created by name."""
        muster = self._move("R-00100", "out_invoice").partner_id
        self.assertFalse(muster.country_id)
        self.assertIn("Country 'Atlantis' not found.", self.log)
        nordlicht = self._move("R-00101", "out_invoice").partner_id
        self.assertEqual(nordlicht.country_id.code, "ES")
        self.assertFalse(nordlicht.vat)
        self.assertIn("Contact K-00002: VAT number 'ES123' not imported.", self.log)
        walk_in = self._move("R-00105", "out_invoice").partner_id
        self.assertEqual(walk_in.name, "Laufkundschaft")
        self.assertFalse(walk_in.papierkram_number)

    def test_valid_vat_number_is_stored(self):
        """A valid VAT number of a new contact is stored on the partner; the
        savepoint around the check keeps it (coverage of the change of E5,
        O-06)."""
        run = self.env["papierkram.import"].create(
            {"export_attachment_ids": [(6, 0, self.attachment.ids)]}
        )
        export = run._read_export()
        self.addCleanup(export.close)
        export.contacts["K-00009"] = Contact(
            number="K-00009",
            name="Eiche Verlag GmbH",
            kind="customer",
            vat="DE123456788",
        )
        partner = _Plan(run, export)._partner_by_number("K-00009")
        self.assertEqual(
            (partner.name, partner.papierkram_number, partner.vat),
            ("Eiche Verlag GmbH", "K-00009", "DE123456788"),
        )

    def test_cash_discount_with_vat_is_reported(self):
        discount = self._move("R-00106-SKONTO", "out_refund")
        self.assertEqual(discount.amount_total, 11.9)
        self.assertEqual(self._move("R-00106", "out_invoice").payment_state, "paid")
        self.assertIn("R-00106: cash discount posted to 8730 without VAT", self.log)

    def test_payment_back_is_not_reconciled_with_the_invoice(self):
        inv = self._move("R-00107", "out_invoice")
        self.assertEqual(inv.payment_state, "paid")
        back = self.env["account.payment"].search(
            [("memo", "=", "R-00107"), ("payment_type", "=", "outbound")]
        )
        self.assertEqual(back.amount, 50.0)
        self.assertFalse(back.is_reconciled)
        self.assertIn(
            "Payment for R-09999 without imported document skipped.", self.log
        )

    def test_vouchers(self):
        """Private share and input VAT on several lines are reported; a
        voucher without split goes to its first category with the net share;
        money coming in is an invoice (income) or a refund (costs)."""
        self.assertIn("Voucher B-00100 has a private share", self.log)
        self.assertIn("Voucher B-00101: please check input VAT", self.log)
        self.assertIn("B-00101: split into Porto, Sonstige", self.log)
        unsplit = self._move("B-00101", "in_invoice").invoice_line_ids
        self.assertEqual(unsplit.account_id.code, "4910")
        self.assertEqual(unsplit.price_subtotal, 50.0)
        self.assertEqual(unsplit.tax_ids.amount, 19.0)
        income = self._move("B-00102", "out_invoice")
        self.assertEqual(income.invoice_line_ids.account_id.code, "8200")
        self.assertEqual(income.payment_state, "paid")
        self.assertEqual(self._move("B-00103", "in_refund").amount_total, 10.0)
        two_pages = self._move("B-00104", "in_invoice")
        pdfs = self.env["ir.attachment"].search(
            [("res_model", "=", "account.move"), ("res_id", "=", two_pages.id)]
        )
        self.assertEqual(len(pdfs), 2)
        self.assertEqual(two_pages.message_main_attachment_id.name, "B-00104_1.pdf")

    def test_depreciation_accounts(self):
        """Pool → 4862, low-value asset → 4855, software → 4822; nothing for
        an amount of zero (memo value only) and a note for assets without an
        asset category."""
        dep = self.moves.filtered(lambda m: (m.ref or "").startswith("AfA"))
        accounts = {
            move.ref.split()[2]: move.line_ids.filtered("debit").account_id.code
            for move in dep
        }
        self.assertEqual(
            accounts, {"B-00110": "4862", "B-00111": "4855", "B-00112": "4822"}
        )
        self.assertIn("Ohne Beleg (B-09998): voucher or asset category", self.log)
        self.assertIn("Kein Anlagegut (B-00100): voucher or asset category", self.log)
        self.assertEqual(self.pk_run.move_count, len(self.moves))

    def test_checks_before_the_import(self):
        """A missing account blocks the import; a lock date is reported."""
        self.env.company.fiscalyear_lock_date = date(2024, 12, 31)
        path = make_export(
            self.tmp.name,
            2026,
            belege=[voucher("B-00120", "02.01.2026", "unpaid", "-5,00", "0,00", "X")],
        )
        run = self.env["papierkram.import"].create(
            {
                "export_attachment_ids": [(6, 0, attach(self.env, path).ids)],
                "categories_file": base64.b64encode(
                    (KATEGORIEN + "X;1;Unbekannt;EXPENSE;;EXPENSE\n").encode()
                ),
            }
        )
        run.action_check()
        self.assertIn("Account 1 (SKR03) does not exist in this company.", run.log)
        self.assertIn("A lock date (2024-12-31) is set.", run.log)
        export = run._read_export()
        self.addCleanup(export.close)
        plan = _Plan(run, export)
        with self.assertRaisesRegex(UserError, "Account 0002 .SKR03. is missing"):
            plan._account("2")
        self.assertFalse(plan._account("2", raise_missing=False))

    def test_chart_of_accounts_is_required(self):
        company = self.env["res.company"].create({"name": "Ohne Kontenrahmen"})
        self.env.user.company_ids |= company
        run = (
            self.env["papierkram.import"]
            .with_company(company)
            .create(
                {
                    "company_id": company.id,
                    "export_attachment_ids": [(6, 0, self.attachment.ids)],
                }
            )
        )
        run.action_check()
        self.assertIn("needs the chart of accounts SKR03 or SKR04", run.log)


@tagged("post_install", "-at_install")
class TestReaderEdgeCases(BaseCase):
    """Edge cases of the reader without Odoo."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_base_number_of_an_invoice_is_its_number(self):
        fields = dict.fromkeys(
            (
                "subject",
                "customer_name",
                "customer_number",
                "country",
                "note",
                "project",
            ),
            "",
        )
        amounts = dict.fromkeys(
            ("net", "tax", "gross", "cash_discount", "paid"), Decimal(0)
        )
        common = {
            **fields,
            **amounts,
            "date": date(2025, 1, 1),
            "due_date": None,
            "last_payment_date": None,
            "export_year": 2025,
        }
        self.assertEqual(
            Invoice(
                number="R-00077-2", kind="invoice", state="open", **common
            ).base_number,
            "R-00077-2",
        )
        self.assertEqual(
            Invoice(
                number="R-00077-S", kind="cancellation", state="paid", **common
            ).base_number,
            "R-00077",
        )

    def test_decomposed_umlauts_and_foreign_pdfs(self):
        """macOS writes "ä" decomposed; PDFs without a document number are
        ignored; the same payment in two exports counts once."""
        payment = "Zahlung zu R-00010;K-00001;R-00010;;2024-12-28;100,00"
        path = make_export(
            self.tmp.name,
            2024,
            rechnungen=[
                "R-00010;Workshop;Muster GmbH;Deutschland;;K-00001;20.12.2024;;"
                "Bezahlt;Rechnung;100,00;0,00;100,00;0,00;100,00;2024-12-28;;;"
            ],
            zahlungen=[payment],
            pdfs=[
                ("Rechnungen/Uebersicht.pdf", b"%PDF"),
                ("Belege/Uebersicht.pdf", b"%PDF"),
            ],
        )
        nfd = os.path.join(self.tmp.name, "nfd_2025.zip")
        name = unicodedata.normalize("NFD", "CSV/Zahlungseingänge.csv")
        with zipfile.ZipFile(path) as source, zipfile.ZipFile(nfd, "w") as target:
            for info in source.infolist():
                content = source.read(info.filename)
                if info.filename == "CSV/Zahlungseingänge.csv":
                    target.writestr(name, content)
                else:
                    target.writestr(info.filename, content)
        export = PapierkramExport.load([path, nfd])
        self.addCleanup(export.close)
        self.assertEqual(export.years, [2024, 2025])
        self.assertEqual(len(export.payments), 1)
        self.assertEqual(len(export.payments_for("R-00010")), 1)
        self.assertFalse(export.payments_for("R-09999"))
        self.assertIsNone(export.invoice_pdf("R-00010"))
        self.assertFalse(export.voucher_pdfs("B-00001"))

    def test_no_export_at_all(self):
        export = PapierkramExport.load([])
        self.assertEqual(export.years, [])
        self.assertFalse(export.contacts or export.invoices or export.notes)
