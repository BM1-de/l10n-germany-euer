# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Tests of the Papierkram reader with fictitious exports."""

import io
import os
import tempfile
import zipfile
from datetime import date
from decimal import Decimal

from odoo.tests import BaseCase, tagged

from ..lib import reader
from ..lib.reader import (
    PapierkramExport,
    PapierkramFormatError,
    parse_amount,
    parse_date,
    split_categories,
)
from .common import KATEGORIEN, make_export


@tagged("post_install", "-at_install")
class TestParsing(BaseCase):
    def test_amount(self):
        self.assertEqual(parse_amount("1.234,56"), Decimal("1234.56"))
        self.assertEqual(parse_amount("-40.0"), Decimal("-40.0"))
        self.assertEqual(parse_amount("-0,01"), Decimal("-0.01"))
        self.assertIsNone(parse_amount(""))
        with self.assertRaises(PapierkramFormatError):
            parse_amount("abc")

    def test_date(self):
        self.assertEqual(parse_date("05.09.2022"), date(2022, 9, 5))
        self.assertEqual(parse_date("2026-04-30"), date(2026, 4, 30))
        self.assertIsNone(parse_date(""))
        with self.assertRaises(PapierkramFormatError):
            parse_date("31.02.2025")

    def test_split_categories_with_comma_in_name(self):
        known = {
            "Zeitschriften, Bücher",
            "Porto",
            "Werbekosten",
            "Roh-, Hilfs- und Betriebsstoffe",
        }
        self.assertEqual(
            split_categories("Werbekosten, Zeitschriften, Bücher", known),
            ("Werbekosten", "Zeitschriften, Bücher"),
        )
        self.assertEqual(
            split_categories("Roh-, Hilfs- und Betriebsstoffe", known),
            ("Roh-, Hilfs- und Betriebsstoffe",),
        )
        self.assertEqual(
            split_categories("Unbekannt, Porto", known), ("Unbekannt", "Porto")
        )
        self.assertEqual(split_categories("", known), ())


@tagged("post_install", "-at_install")
class TestExport(BaseCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        # Invoice created in 2024, paid partly in December 2024 and in January 2025
        # via a bank transaction (without payment assignment).
        self.p2024 = make_export(
            d,
            2024,
            rechnungen=[
                "R-00010;Workshop;Muster GmbH;Deutschland;;K-00001;20.12.2024;"
                "30.12.2024;Bezahlt;Rechnung;300,00;0,00;300,00;0,00;300,00;"
                "2025-01-03;;;",
                "R-00011;Storniert;Muster GmbH;Deutschland;;K-00001;21.12.2024;;"
                "Storniert;Rechnung;50,00;0,00;50,00;0,00;0,00;;;;",
                ";Entwurf;Muster GmbH;Deutschland;;K-00001;22.12.2024;;"
                "Entwurf;Rechnung;10,00;0,00;10,00;0,00;0,00;;;;",
            ],
            belege=[
                "B-00001;Paketmarken;Porto AG;30.12.2024;;;unpaid;-5,00;0,00;-5,00;0;"
                "domestic;Porto;;;;;",
            ],
            zahlungen=["Zahlung zu R-00010;K-00001;R-00010;;2024-12-28;100,00"],
            pdfs=[
                ("Rechnungen/Rechnung_R-00010_Muster_GmbH_2024-12-20.pdf", b"%PDF-R10"),
                ("Belege/B-00001_1.pdf", b"%PDF-B1"),
            ],
        )
        # 2025: cancellation of R-00011, payment of the 2024 voucher and a
        # split voucher with a fixed asset.
        self.p2025 = make_export(
            d,
            2025,
            rechnungen=[
                "R-00011-S;Storniert;Muster GmbH;Deutschland;;K-00001;02.01.2025;;"
                "Bezahlt;Stornorechnung;-50,00;0,00;-50,00;0,00;0,00;;;;",
            ],
            belege=[
                "B-00002;Beamer;Technik AG;10.01.2025;;2025-01-12;paid;-1.009,95;"
                "0,00;-1.009,95;0;domestic;"
                "Betriebsausstattung (Anlage), Zeitschriften, Bücher;;;;;",
            ],
            zahlungen=["Zahlung zu B-00001;;;B-00001;2025-02-03;-5,00"],
        )

    def _load(self, **kw):
        export = PapierkramExport.load(
            [self.p2025, self.p2024], categories_csv=KATEGORIEN, **kw
        )
        self.addCleanup(export.close)
        return export

    def test_merge_across_years(self):
        export = self._load()
        self.assertEqual(set(export.contacts), {"K-00001", "L-00001"})
        self.assertIn("R-00010", export.invoices)
        self.assertIn("R-00011-S", export.invoices)
        self.assertEqual(export.invoices["R-00011-S"].kind, "cancellation")
        self.assertEqual(export.invoices["R-00011-S"].base_number, "R-00011")
        self.assertEqual(len([i for i in export.invoices.values() if i.is_draft]), 1)
        self.assertEqual(len(export.payments), 2)
        self.assertEqual(export.bank_transactions[0].purpose, "Porto zweite Zeile")

    def test_pdfs(self):
        export = self._load()
        self.assertEqual(export.invoice_pdf("R-00010"), b"%PDF-R10")
        self.assertEqual(
            export.voucher_pdfs("B-00001"), [("B-00001_1.pdf", b"%PDF-B1")]
        )
        self.assertIsNone(export.invoice_pdf("R-00011"))

    def test_cash_events(self):
        export = self._load()
        events = {
            (e.document, e.date, e.amount, e.source) for e in export.cash_events()
        }
        self.assertEqual(
            events,
            {
                ("R-00010", date(2024, 12, 28), Decimal("100.00"), "payment"),
                # remainder of the invoice, only known from the last payment date
                ("R-00010", date(2025, 1, 3), Decimal("200.00"), "last_payment_date"),
                ("B-00001", date(2025, 2, 3), Decimal("-5.00"), "payment"),
                # paid voucher without payment assignment
                ("B-00002", date(2025, 1, 12), Decimal("-1009.95"), "voucher_paid_on"),
            },
        )

    def test_split_needed_and_applied(self):
        export = self._load()
        self.assertTrue(export.vouchers["B-00002"].needs_split)
        # the comma inside "Zeitschriften, Bücher" does not split the name
        self.assertEqual(
            export.vouchers["B-00002"].categories,
            ("Betriebsausstattung (Anlage)", "Zeitschriften, Bücher"),
        )
        self.assertIn(
            ("split_missing", "B-00002"),
            [(n.code, n.params.get("voucher")) for n in export.notes],
        )
        splits = (
            "belegnummer;position;kategorie;betrag_brutto\n"
            "B-00002;1;Betriebsausstattung (Anlage);1000.00\n"
            "B-00002;2;Zeitschriften, Bücher;9.95\n"
        )
        export = self._load(splits_csv=splits)
        voucher = export.vouchers["B-00002"]
        self.assertFalse(voucher.needs_split)
        self.assertEqual(
            [(line.category, line.gross) for line in voucher.lines],
            [
                ("Betriebsausstattung (Anlage)", Decimal("1000.00")),
                ("Zeitschriften, Bücher", Decimal("9.95")),
            ],
        )
        self.assertEqual(export.account_for_category("Porto"), "4910")
        self.assertEqual(export.account_for_category("Zeitschriften, Bücher"), "4940")

    def test_split_with_wrong_total_is_rejected(self):
        splits = (
            "belegnummer;position;kategorie;betrag_brutto\n"
            "B-00002;1;Betriebsausstattung (Anlage);1000.00\n"
        )
        export = self._load(splits_csv=splits)
        self.assertTrue(export.vouchers["B-00002"].needs_split)
        self.assertIn("split_sum_mismatch", [n.code for n in export.notes])

    def test_assets_csv_with_leading_blank_line(self):
        assets = (
            "\nBelegnummer;Beleg;Restwert Jahresende;Anschaffungsdatum;"
            "Anschaffungswert;monatl. Abschreibung\n"
            "B-00002;Beamer;857,14;12.01.2025;1.000,00;11,90\n"
            "B-00003;Aktenschrank;0,00;14.03.2023;399,00;0,00\n"
        )
        export = self._load(assets_csv=assets)
        self.assertEqual(export.assets[0].useful_life_months, 84)
        self.assertEqual(export.assets[1].useful_life_months, 0)

    def test_not_an_export(self):
        path = os.path.join(self.tmp.name, "leer.zip")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("readme.txt", "x")
        with self.assertRaises(PapierkramFormatError):
            PapierkramExport.load([path])

    def test_unknown_invoice_status(self):
        path = make_export(
            self.tmp.name,
            2023,
            rechnungen=[
                "R-1;X;Muster GmbH;Deutschland;;K-00001;01.01.2023;;Erfunden;Rechnung;"
                "1,00;0,00;1,00;0,00;0,00;;;;"
            ],
        )
        with self.assertRaises(PapierkramFormatError):
            PapierkramExport.load([path])

    def test_bytes_io_is_not_needed(self):
        # directory instead of ZIP
        d = os.path.join(self.tmp.name, "entpackt_2024")
        with zipfile.ZipFile(self.p2024) as zf:
            zf.extractall(d)
        export = PapierkramExport.load([d])
        self.addCleanup(export.close)
        self.assertIn("R-00010", export.invoices)
        self.assertIsInstance(io.BytesIO(export.invoice_pdf("R-00010")), io.BytesIO)

    # -- coverage of the remaining paths (spec O-06) ----------------------
    def test_notes_of_unknown_references(self):
        """Splits, categories and payments referring to unknown documents are
        reported, never dropped silently; a note reads as code and values."""
        path = make_export(
            self.tmp.name,
            2023,
            rechnungen=[
                # paid, but without payment date
                "R-00090;X;Muster GmbH;Deutschland;;K-00001;01.03.2023;;Bezahlt;"
                "Rechnung;20,00;0,00;20,00;0,00;20,00;;;;",
            ],
            belege=[
                # paid without date and with an unknown category
                "B-00090;Y;Porto AG;02.03.2023;;;paid;-12,00;0,00;-12,00;0;domestic;"
                "Erfundene Kategorie;;;;;",
            ],
            zahlungen=[
                "Zahlung zu R-09999;K-00001;R-09999;;2023-03-05;10,00",
                "Zahlung zu B-09999;;;B-09999;2023-03-06;-10,00",
            ],
        )
        splits = "belegnummer;position;kategorie;betrag_brutto\nB-08888;1;Porto;1.00\n"
        export = PapierkramExport.load(
            [path], categories_csv=KATEGORIEN, splits_csv=splits
        )
        self.addCleanup(export.close)
        export.cash_events()
        codes = {note.code: note for note in export.notes}
        self.assertEqual(
            set(codes),
            {
                "split_unknown_voucher",
                "unknown_category",
                "payment_unknown_invoice",
                "payment_unknown_voucher",
                "invoice_paid_without_date",
                "voucher_paid_without_date",
            },
        )
        self.assertEqual(
            str(codes["split_unknown_voucher"]),
            "split_unknown_voucher: voucher=B-08888",
        )
        self.assertEqual(export.account_for_category("Geschenke bis 35 Euro"), "4630")
        self.assertIsNone(export.account_for_category("Erfundene Kategorie"))

    def test_sources_that_are_no_export(self):
        """A path that is neither a ZIP file nor a folder, an unknown member and
        a CSV above the size limit are refused."""
        with self.assertRaises(PapierkramFormatError):
            PapierkramExport.load([os.path.join(self.tmp.name, "fehlt.zip")])
        export = self._load()
        with self.assertRaises(KeyError):
            export._sources[0].read_bytes("CSV/Gibt_es_nicht.csv")
        limit = reader.MAX_CSV_BYTES
        reader.MAX_CSV_BYTES = 10
        self.addCleanup(setattr, reader, "MAX_CSV_BYTES", limit)
        with self.assertRaises(PapierkramFormatError):
            PapierkramExport.load([self.p2024])
