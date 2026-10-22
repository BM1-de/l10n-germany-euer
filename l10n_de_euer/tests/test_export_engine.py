# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Unit tests of the output formats, no database (spec 7.5, 7.12, G-09).

German numbers with the minus as text, CSV with header line, semicolon,
decimal comma and CRLF, the ``index.xml`` of the description standard for
the data transfer (well-formed, mandatory elements; the DTD is not
available, U-25), the field description as JSON, the ZIP of the data
access package and the data set ``l10n_de_euer/1``. Synthetic values (D6).
"""

import datetime
import io
import json
import zipfile
from decimal import Decimal as D
from xml.etree import ElementTree

from odoo.tests import BaseCase

from ..models.engine import export


def _tables():
    return [
        export.Table(
            "slices.csv",
            "Slices",
            "Realised amounts",
            (
                export.Column("slice_id", "numeric", "Id", 0),
                export.Column("cash_date", "date", "Cash date"),
                export.Column("amount", "numeric", "Amount"),
                export.Column("partner", "text", "Partner"),
            ),
            [
                (1, datetime.date(2026, 3, 20), D("1234.5"), "Kunde; A"),
                (2, datetime.date(2026, 4, 15), D("-200"), 'Lieferant "B"'),
                (3, None, None, None),
            ],
        ),
        export.Table(
            "checks.csv",
            "Checks",
            "Check results",
            (
                export.Column("code", "text", "Code"),
                export.Column("ok", "text", "Acknowledged"),
            ),
            [("C-16", True), ("C-17", False)],
        ),
    ]


class TestExportEngine(BaseCase):
    # ------------------------------------------------------------ numbers
    def test_numbers_and_dates(self):
        """Decimal comma, thousands dot only for reading, the minus as text
        in front of the number (O-11), half up rounding, no minus zero."""
        self.assertEqual(export.number_text(D("1234.5")), "1234,50")
        self.assertEqual(export.amount_text(D("1234567.891")), "1.234.567,89")
        self.assertEqual(export.amount_text(-1234.5), "-1.234,50")
        self.assertEqual(export.amount_text("-0.004"), "0,00")
        self.assertEqual(export.amount_text(0.005), "0,01")
        self.assertEqual(export.amount_text(999), "999,00")
        self.assertEqual(export.amount_text(None), "")
        self.assertEqual(export.number_text(7, places=0), "7")
        self.assertEqual(export.number_text(D("-7.5"), places=0), "-8")
        self.assertEqual(export.percent_text(D("0.5")), "50,00 %")
        self.assertEqual(export.percent_text(None), "")
        self.assertEqual(export.date_text(datetime.date(2026, 12, 31)), "31.12.2026")
        self.assertEqual(
            export.date_text(datetime.datetime(2026, 1, 2, 23, 59)), "02.01.2026"
        )
        self.assertEqual(export.date_text("2026-05-04"), "04.05.2026")
        self.assertEqual(export.date_text(False), "")
        with self.assertRaises(export.ExportError):
            export.number_text("abc")

    def test_decimal_string(self):
        """Amounts of the data set as text with a decimal point."""
        self.assertEqual(export.decimal_string(1234.5), "1234.50")
        self.assertEqual(export.decimal_string(D("-0.004")), "0.00")
        self.assertEqual(export.decimal_string(D("-12.345")), "-12.35")
        self.assertIsNone(export.decimal_string(None))

    # ------------------------------------------------------------ CSV
    def test_csv(self):
        """Header line, semicolon, CRLF, UTF-8; cells with a semicolon or a
        quote are enclosed in quotes."""
        content = export.csv_bytes(["a", "Kz"], [["x;y", 'say "hi"'], ["ä", None]])
        self.assertEqual(
            content,
            'a;Kz\r\n"x;y";"say ""hi"""\r\nä;\r\n'.encode(),
        )
        with_bom = export.csv_bytes(["a"], [["b"]], bom=True)
        self.assertTrue(with_bom.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(with_bom.decode("utf-8-sig"), "a\r\nb\r\n")

    def test_table_csv(self):
        """Columns format their values: decimal comma without thousands
        dot, dates DD.MM.YYYY, booleans as 1/0, empty cells empty."""
        slices, checks = _tables()
        text = slices.csv().decode()
        self.assertEqual(
            text.split("\r\n"),
            [
                "slice_id;cash_date;amount;partner",
                '1;20.03.2026;1234,50;"Kunde; A"',
                '2;15.04.2026;-200,00;"Lieferant ""B"""',
                "3;;;",
                "",
            ],
        )
        self.assertEqual(checks.csv().decode(), "code;ok\r\nC-16;1\r\nC-17;0\r\n")
        with self.assertRaises(export.ExportError):
            export.Column("x", "money")
        with self.assertRaises(export.ExportError):
            export.Table("x.csv", "X", "", (export.Column("a"),), [(1, 2)])
        with self.assertRaises(export.ExportError):
            export.Table("x.csv", "X", "", ())

    # ------------------------------------------------------------ index.xml
    def test_index_xml(self):
        """``index.xml``: well-formed, DOCTYPE of the description standard,
        mandatory elements in the order of the standard, one table per CSV
        file with UTF-8, decimal comma, header line skipped, semicolon, CRLF,
        primary key and the type of every column (GT-10)."""
        content = export.index_xml(
            _tables(),
            "Muster GmbH",
            "Musterstraße 1, 12345 Musterstadt",
            "EÜR 2026",
            "EÜR 2026",
            datetime.date(2026, 1, 1),
            datetime.date(2026, 12, 31),
        )
        text = content.decode()
        self.assertTrue(text.startswith('<?xml version="1.0" encoding="UTF-8"?>'))
        self.assertIn('<!DOCTYPE DataSet SYSTEM "gdpdu-01-09-2004.dtd">', text)
        root = ElementTree.fromstring(content)
        self.assertEqual(root.tag, "DataSet")
        self.assertEqual(
            [child.tag for child in root], ["Version", "DataSupplier", "Media"]
        )
        supplier = root.find("DataSupplier")
        self.assertEqual(
            [child.tag for child in supplier], ["Name", "Location", "Comment"]
        )
        self.assertEqual(
            supplier.findtext("Location"), "Musterstraße 1, 12345 Musterstadt"
        )
        media = root.find("Media")
        self.assertEqual(media.findtext("Name"), "EÜR 2026")
        tables = media.findall("Table")
        self.assertEqual(
            [table.findtext("URL") for table in tables], ["slices.csv", "checks.csv"]
        )
        table = tables[0]
        self.assertEqual(
            [child.tag for child in table],
            [
                "URL",
                "Name",
                "Description",
                "Validity",
                "UTF8",
                "DecimalSymbol",
                "DigitGroupingSymbol",
                "Range",
                "VariableLength",
            ],
        )
        self.assertEqual(table.findtext("Validity/Range/From"), "01.01.2026")
        self.assertEqual(table.findtext("Validity/Range/To"), "31.12.2026")
        self.assertEqual(table.findtext("Validity/Format"), "DD.MM.YYYY")
        self.assertEqual(table.findtext("DecimalSymbol"), ",")
        self.assertEqual(table.findtext("Range/From"), "2")
        variable = table.find("VariableLength")
        self.assertEqual(variable.findtext("ColumnDelimiter"), ";")
        self.assertEqual(variable.findtext("RecordDelimiter"), "\r\n")
        self.assertEqual(variable.findtext("TextEncapsulator"), '"')
        key = variable.find("VariablePrimaryKey")
        self.assertEqual(key.findtext("Name"), "slice_id")
        self.assertIsNotNone(key.find("Numeric"))
        columns = variable.findall("VariableColumn")
        self.assertEqual(
            [column.findtext("Name") for column in columns],
            ["cash_date", "amount", "partner"],
        )
        self.assertEqual(columns[0].findtext("Date/Format"), "DD.MM.YYYY")
        self.assertEqual(columns[1].findtext("Numeric/Accuracy"), "2")
        self.assertIsNotNone(columns[2].find("AlphaNumeric"))
        # special characters are escaped
        escaped = export.index_xml(
            _tables()[:1], "A & B <GmbH>", "x", "y", "z", "2026-01-01", "2026-12-31"
        )
        self.assertEqual(
            ElementTree.fromstring(escaped).findtext("DataSupplier/Name"),
            "A & B <GmbH>",
        )

    def test_field_description(self):
        description = export.field_description(_tables())
        self.assertEqual(description["schema"], export.FIELD_SCHEMA)
        self.assertEqual(description["format"]["column_delimiter"], ";")
        self.assertEqual(description["format"]["decimal_symbol"], ",")
        slices = description["tables"][0]
        self.assertEqual(slices["records"], 3)
        self.assertEqual(slices["primary_key"], "slice_id")
        self.assertEqual(
            slices["columns"][2],
            {"name": "amount", "type": "numeric", "description": "Amount", "places": 2},
        )
        self.assertNotIn("places", slices["columns"][3])

    def test_z3_archive(self):
        """The ZIP holds the CSV files, ``index.xml`` and the field
        description; the same data gives the same bytes."""
        args = (
            _tables(),
            "Muster GmbH",
            "Musterstadt",
            "EÜR 2026",
            "EÜR 2026",
            datetime.date(2026, 1, 1),
            datetime.date(2026, 12, 31),
        )
        content = export.z3_archive(*args)
        self.assertEqual(content, export.z3_archive(*args))
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            self.assertEqual(
                archive.namelist(),
                ["slices.csv", "checks.csv", "index.xml", "field_description.json"],
            )
            self.assertEqual(archive.read("checks.csv"), _tables()[1].csv())
            description = json.loads(archive.read("field_description.json"))
            self.assertEqual(len(description["tables"]), 2)
            ElementTree.fromstring(archive.read("index.xml"))
        duplicate = _tables()
        duplicate[1].file = "slices.csv"
        with self.assertRaises(export.ExportError):
            export.z3_archive(duplicate, *args[1:])
        reserved = _tables()
        reserved[1].file = "index.xml"
        with self.assertRaises(export.ExportError):
            export.z3_archive(reserved, *args[1:])

    def test_dataset_json(self):
        """Data set ``l10n_de_euer/1`` with the keys of spec 7.5."""
        content = export.dataset_json(
            2026,
            [{"formular": "euer", "jahr": 2026, "version": 1}],
            {"name": "Muster"},
            [{"code": "EUER.20.111", "betrag": "1000.00"}],
            {"gruppen": [], "einzelangaben": []},
            {"zustand": "final"},
            "ab" * 32,
            extra={"kontrollwerte": {"gewinn": "800.00"}},
        )
        data = json.loads(content)
        self.assertEqual(data["schema"], "l10n_de_euer/1")
        self.assertEqual(data["jahr"], 2026)
        self.assertEqual(data["hash"], "ab" * 32)
        self.assertEqual(data["felder"][0]["betrag"], "1000.00")
        for key in ("formularversion", "betrieb", "aveuer", "kontrollwerte"):
            self.assertIn(key, data)
        self.assertTrue(content.endswith(b"\n"))
