# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Output formats of a statement (spec 7.5, G-09, O-11).

Pure Python without ORM access. The ORM collects the values of a statement;
this module writes them

* as German numbers and dates for the fill-in aid and the CSV files: decimal
  comma, the minus as text in front of the number, never as colour or
  bracket (:func:`number_text`, :func:`date_text`);
* as CSV with a header line, semicolon, decimal comma and CRLF
  (:func:`csv_bytes`, :class:`Table`);
* as data access package Z3 (§ 147 (6) AO): one CSV file per table, an
  ``index.xml`` after the description standard for the data transfer
  (*Beschreibungsstandard für die Datenüberlassung*) and the description of
  the fields as JSON (:func:`index_xml`, :func:`field_description`,
  :func:`z3_archive`);
* as machine readable data set ``l10n_de_euer/1`` (:func:`dataset_json`).

The document type definition of the description standard is not shipped
(spec U-25): ``index.xml`` refers to it by its usual file name.
"""

import csv
import datetime
import io
import json
import zipfile
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from xml.sax.saxutils import escape, quoteattr

#: schema of the machine readable data set (spec 7.5)
DATASET_SCHEMA = "l10n_de_euer/1"
#: schema of the description of the fields of the data access package
FIELD_SCHEMA = "l10n_de_euer.z3/1"
DELIMITER = ";"
RECORD_DELIMITER = "\r\n"
TEXT_ENCAPSULATOR = '"'
DECIMAL_SYMBOL = ","
GROUPING_SYMBOL = "."
#: date format of the CSV files and of ``index.xml``
DATE_FORMAT = "DD.MM.YYYY"
#: document type definition of the description standard (not shipped, U-25)
DTD = "gdpdu-01-09-2004.dtd"
INDEX_VERSION = "1.0"
INDEX_FILE = "index.xml"
FIELD_FILE = "field_description.json"
#: fixed time of the files in the archive: the same data gives the same file
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
COLUMN_KINDS = ("text", "numeric", "date")


class ExportError(ValueError):
    """A value that does not fit its column."""

    def __init__(self, key, **params):
        super().__init__(key)
        self.key = key
        self.params = params


# ------------------------------------------------------------ values
def to_decimal(value):
    """``Decimal`` of an amount (float, int, ``Decimal`` or text); ``None``
    for an empty value."""
    if value is None or value is False or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except InvalidOperation as error:
        raise ExportError("not_a_number", value=value) from error


def number_text(value, places=2, grouping=False):
    """German number: decimal comma, optional thousands dot, minus in front.

    ``number_text(-1234.5, grouping=True)`` is ``-1.234,50``. The minus is a
    character of the text (O-11): a screen reader reads it and a copy into
    Mein ELSTER keeps it. An empty value is an empty text.
    """
    amount = to_decimal(value)
    if amount is None:
        return ""
    quantum = Decimal(1).scaleb(-places)
    amount = amount.quantize(quantum, rounding=ROUND_HALF_UP)
    if not amount:
        amount = abs(amount)
    sign = "-" if amount < 0 else ""
    digits = f"{abs(amount):.{places}f}"
    whole, _dot, fraction = digits.partition(".")
    if grouping:
        groups = []
        while len(whole) > 3:
            groups.insert(0, whole[-3:])
            whole = whole[:-3]
        groups.insert(0, whole)
        whole = GROUPING_SYMBOL.join(groups)
    return sign + whole + (DECIMAL_SYMBOL + fraction if places else "")


def amount_text(value):
    """Amount for reading: two places and thousands dots (``1.234,56``)."""
    return number_text(value, 2, grouping=True)


def percent_text(value, places=2):
    """Share as percentage: ``0.5`` → ``50,00 %``."""
    share = to_decimal(value)
    if share is None:
        return ""
    return f"{number_text(share * 100, places)} %"


def date_text(value):
    """German date ``31.12.2026``; empty for no date."""
    if not value:
        return ""
    if isinstance(value, datetime.datetime):
        value = value.date()
    if isinstance(value, str):
        value = datetime.date.fromisoformat(value[:10])
    return value.strftime("%d.%m.%Y")


def single_line(text):
    """Text without line breaks and tabs (each becomes one space)."""
    return " ".join(text.replace("\r\n", "\n").splitlines()).replace("\t", " ")


# ------------------------------------------------------------ CSV
def csv_bytes(header, rows, bom=False):
    """CSV with header line, semicolon and CRLF in UTF-8.

    Cells are written as given (texts); a cell with a semicolon, a quote or a
    line break is enclosed in quotes, quotes inside are doubled. ``bom``
    writes the byte order mark that spreadsheet programs need to recognise
    UTF-8.
    """
    buffer = io.StringIO()
    writer = csv.writer(
        buffer,
        delimiter=DELIMITER,
        quotechar=TEXT_ENCAPSULATOR,
        lineterminator=RECORD_DELIMITER,
        quoting=csv.QUOTE_MINIMAL,
    )
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if cell is None else str(cell) for cell in row])
    return buffer.getvalue().encode("utf-8-sig" if bom else "utf-8")


@dataclass(frozen=True)
class Column:
    """Column of a table of the data access package.

    ``kind``: ``text``, ``numeric`` or ``date``; ``places``: decimal places
    of a numeric column (0 for counts and ids); ``key``: key of the value in
    the rows the caller prepares (not part of the output).
    """

    name: str
    kind: str = "text"
    description: str = ""
    places: int = 2
    key: str = ""

    def __post_init__(self):
        if self.kind not in COLUMN_KINDS:
            raise ExportError("unknown_kind", column=self.name, kind=self.kind)

    def cell(self, value):
        """Value of the column as text of the CSV file; a flag of a text
        column is ``1`` or ``0``. Line breaks and tabs in a text become
        spaces: CRLF separates the records (``index.xml``), and the test
        software of the tax authorities need not read it inside a field."""
        if isinstance(value, bool) and self.kind == "text":
            return "1" if value else "0"
        if value is None or value is False:
            return ""
        if self.kind == "numeric":
            return number_text(value, self.places)
        if self.kind == "date":
            return date_text(value)
        return single_line(str(value))


@dataclass
class Table:
    """Table of the data access package: one CSV file with header line.

    The first column is the primary key (``VariablePrimaryKey``).
    """

    file: str
    name: str
    description: str
    columns: tuple
    rows: list = field(default_factory=list)

    def __post_init__(self):
        if not self.columns:
            raise ExportError("no_columns", table=self.name)
        width = len(self.columns)
        for row in self.rows:
            if len(row) != width:
                raise ExportError("row_width", table=self.name, row=row)

    def csv(self):
        return csv_bytes(
            [column.name for column in self.columns],
            (
                [
                    column.cell(value)
                    for column, value in zip(self.columns, row, strict=True)
                ]
                for row in self.rows
            ),
        )


# ------------------------------------------------------------ index.xml
def _element(lines, depth, name, text=None):
    indent = "  " * depth
    if text is None:
        lines.append(f"{indent}<{name}/>")
    else:
        lines.append(f"{indent}<{name}>{escape(str(text))}</{name}>")


def _column_xml(lines, depth, tag, column):
    indent = "  " * depth
    lines.append(f"{indent}<{tag}>")
    _element(lines, depth + 1, "Name", column.name)
    if column.description:
        _element(lines, depth + 1, "Description", column.description)
    if column.kind == "numeric":
        if column.places:
            lines.append(f"{indent}  <Numeric>")
            _element(lines, depth + 2, "Accuracy", column.places)
            lines.append(f"{indent}  </Numeric>")
        else:
            _element(lines, depth + 1, "Numeric")
    elif column.kind == "date":
        lines.append(f"{indent}  <Date>")
        _element(lines, depth + 2, "Format", DATE_FORMAT)
        lines.append(f"{indent}  </Date>")
    else:
        _element(lines, depth + 1, "AlphaNumeric")
    lines.append(f"{indent}</{tag}>")


def index_xml(tables, supplier, location, comment, media, date_from, date_to):
    """``index.xml`` of the description standard for the data transfer.

    Every table is a CSV file in UTF-8 with a header line (data from record
    2), semicolon, CRLF, decimal comma and dates as ``DD.MM.YYYY``; its first
    column is the primary key. ``date_from`` and ``date_to`` give the period
    of validity (the year of the statement).
    """
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f"<!DOCTYPE DataSet SYSTEM {quoteattr(DTD)}>",
        "<DataSet>",
    ]
    _element(lines, 1, "Version", INDEX_VERSION)
    lines.append("  <DataSupplier>")
    _element(lines, 2, "Name", supplier)
    _element(lines, 2, "Location", location)
    _element(lines, 2, "Comment", comment)
    lines.append("  </DataSupplier>")
    lines.append("  <Media>")
    _element(lines, 2, "Name", media)
    for table in tables:
        lines.append("    <Table>")
        _element(lines, 3, "URL", table.file)
        _element(lines, 3, "Name", table.name)
        _element(lines, 3, "Description", table.description)
        lines.append("      <Validity>")
        lines.append("        <Range>")
        _element(lines, 5, "From", date_text(date_from))
        _element(lines, 5, "To", date_text(date_to))
        lines.append("        </Range>")
        _element(lines, 4, "Format", DATE_FORMAT)
        lines.append("      </Validity>")
        _element(lines, 3, "UTF8")
        _element(lines, 3, "DecimalSymbol", DECIMAL_SYMBOL)
        _element(lines, 3, "DigitGroupingSymbol", GROUPING_SYMBOL)
        # the first record is the header line
        lines.append("      <Range>")
        _element(lines, 4, "From", 2)
        lines.append("      </Range>")
        lines.append("      <VariableLength>")
        _element(lines, 4, "ColumnDelimiter", DELIMITER)
        # character references: a parser keeps CR LF as they are
        lines.append("        <RecordDelimiter>&#13;&#10;</RecordDelimiter>")
        _element(lines, 4, "TextEncapsulator", TEXT_ENCAPSULATOR)
        _column_xml(lines, 4, "VariablePrimaryKey", table.columns[0])
        for column in table.columns[1:]:
            _column_xml(lines, 4, "VariableColumn", column)
        lines.append("      </VariableLength>")
        lines.append("    </Table>")
    lines.append("  </Media>")
    lines.append("</DataSet>")
    return ("\n".join(lines) + "\n").encode("utf-8")


def field_description(tables):
    """Description of the tables and fields of the data access package as
    data (the content of ``index.xml`` without its XML syntax)."""
    return {
        "schema": FIELD_SCHEMA,
        "format": {
            "encoding": "UTF-8",
            "header_line": True,
            "column_delimiter": DELIMITER,
            "record_delimiter": "CRLF",
            "text_encapsulator": TEXT_ENCAPSULATOR,
            "decimal_symbol": DECIMAL_SYMBOL,
            "date_format": DATE_FORMAT,
            "line_breaks_in_texts": "replaced by spaces",
        },
        "tables": [
            {
                "file": table.file,
                "name": table.name,
                "description": table.description,
                "records": len(table.rows),
                "primary_key": table.columns[0].name,
                "columns": [
                    {
                        "name": column.name,
                        "type": column.kind,
                        "description": column.description,
                        **(
                            {"places": column.places}
                            if column.kind == "numeric"
                            else {}
                        ),
                    }
                    for column in table.columns
                ],
            }
            for table in tables
        ],
    }


def json_bytes(data):
    """JSON in UTF-8 with sorted keys and indentation (readable, stable)."""
    return (
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def z3_archive(tables, supplier, location, comment, media, date_from, date_to):
    """Data access package Z3 as ZIP: the CSV files, ``index.xml`` and the
    description of the fields as JSON. Same data, same bytes."""
    names = [table.file for table in tables]
    if len(set(names)) != len(names) or {INDEX_FILE, FIELD_FILE} & set(names):
        raise ExportError("duplicate_file", files=names)
    files = [(table.file, table.csv()) for table in tables]
    files.append(
        (
            INDEX_FILE,
            index_xml(tables, supplier, location, comment, media, date_from, date_to),
        )
    )
    files.append((FIELD_FILE, json_bytes(field_description(tables))))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files:
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    return buffer.getvalue()


# ------------------------------------------------------------ data set
def decimal_string(value):
    """Amount of the data set: text with a decimal point and two places, so
    no reader turns it into a binary float (``"-1234.50"``)."""
    amount = to_decimal(value)
    if amount is None:
        return None
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{amount:.2f}" if amount else "0.00"


def dataset_json(
    year, form_versions, business, fields, aveuer, status, hash_value, extra=None
):
    """Machine readable data set of a statement (spec 7.5)::

        {"schema": "l10n_de_euer/1", "jahr", "formularversion", "betrieb",
         "felder": [...], "aveuer": {...}, "hash", ...}

    ``fields`` and ``aveuer`` are prepared by the caller (amounts as text
    with :func:`decimal_string`); ``hash`` is the SHA-256 of the
    finalisation or ``None`` for a statement that is not finalised.
    """
    data = {
        "schema": DATASET_SCHEMA,
        "jahr": year,
        "formularversion": form_versions,
        "betrieb": business,
        "felder": fields,
        "aveuer": aveuer,
        "status": status,
        "hash": hash_value,
    }
    data.update(extra or {})
    return json_bytes(data)
