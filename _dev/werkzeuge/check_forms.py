#!/usr/bin/env python3
# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Check the form data of l10n_de_euer against the text of the official forms.

Proof for the release of a form year (spec F-11, 3.4 step 3): every pair
(line, Kennzahl) of ``l10n_de_euer/data/forms/euer_<year>.xml`` must occur in
the text of the official form of that year, and every Kennzahl printed in the
form must have a field in the data. The check reads the delivered XML (not the
input of the generator) and compares it with two text extractions of the BMF
PDF, plus the blank forms if present. Both extractions read the same PDF: they
are two readings (with and without layout), not two independent sources.

* ``bmf<year>.txt``: ``pdftotext -layout`` of the BMF letter with forms and
  instructions (line number on the left, Kennzahl in the right column)
* ``c<year>.txt``: ``pdftotext`` (raw) of the same PDF
* ``e<yy>.txt``, ``av<yy>.txt``, ``sz<yy>.txt`` (optional): raw text of the
  blank forms (Anlage EÜR, AVEÜR, SZ); only the presence of each Kennzahl is
  checked, the blank Anlage EÜR prints no line numbers
* ``bmf<year>.pdf`` (optional): SHA-256 against ``source_sha256``

Results per field:

* ``line``: the Kennzahl is on the printed line with the line number
* ``block``: the Kennzahl is between this line and its neighbour (labels or
  boxes printed over several lines, e.g. a Kennzahl before the line number)
* ``missing``: not found at this line; the lines where the Kennzahl occurs
  instead are listed

A pair is confirmed if both extractions find it; found by one extraction
only it is a deviation. Further checks per field, each a deviation:

* the code ends with the Kennzahl (``EUER.25.174`` ↔ Kz 174);
* the official label (F-09): fields without Kennzahl (transfers, subtotals,
  header fields) need it word for word (without whitespace and hyphens) on
  their own line, so that two such lines cannot be swapped unnoticed; fields
  with Kennzahl need it as one sequence of words in the text of their line
  (Kennzahlen, form ids, footnote marks, operators, cent commas and the
  printed column headings left out), and on a line with several Kennzahlen
  of different labels each label must stand between the Kennzahl before it
  and its own Kennzahl (labels of one line swapped). Not found: a first or
  last word missing from a label whose remaining words stand together in the
  text (the release role compares the labels by sight, F-11);
* lines with two Kennzahlen under the column headings "nicht abziehbar" /
  "abziehbar" (gifts, entertainment ...) or "Gesamtbetrag" /
  "Korrekturbetrag" (InvStG, partial income method): the left column must be
  the field of the type "of which" (not in the sum), the right one an amount
  of the sum; the column follows from the print position in the layout text;
* the arithmetic printed in the form (layout text): the operator next to a
  Kennzahl, or alone on the line of a transfer or total without Kennzahl
  (``+``, ``−``, ``+/−``, ``=``), must match the ``sign`` of the field; a
  field marked ``=`` must be a total, and every total has the sign ``=``;
* the next total marked ``=`` below a field: a field marked ``+``/``−`` (or
  ``+/−`` with Kennzahl), a field with the sign ``+``/``-`` and every amount
  of the Anlage EÜR must enter it (through subtotals), with the printed
  operator, with ``+`` if none is printed (``-`` if the label starts with
  "abzüglich") and as its ``sign`` says, unless a total with Kennzahl but
  without operator stands between them;
* the line references of the official label: a sum or transfer that names
  its source lines ("Summe Zeilen 24 bis 27", "Übertrag aus Zeile 23 der
  Anlage EÜR", "Betrag aus Zeile 17 oder 24") must use exactly the fields of
  these lines in its formula and add them, unless it is a "Summe/Differenz"
  or a "Betrag aus …" whose label starts with "abzüglich"; a field that names
  its target ("Übertrag in Zeile 77", "in Zeile 13 eintragen") must be used
  by a field of that line, an "Übertrag" with ``+``;
* fields of the type "of which" are used in no formula, informational fields
  (sign ``0``) in no formula of their own form, and a field of the Anlage
  EÜR in at most one formula of the Anlage EÜR;
* every total marked ``=`` of the Anlage EÜR and SZ enters a further formula,
  except the last total of its box (Kz 219 in box G); the totals of Anlage
  AVEÜR (book values at the end) are final values.

Hints without deviation: a target "zu erfassen in Zeile …" that no formula of
the target line uses (the amount is part of that line, e.g. checked by the
module, not transferred by a formula).

The reverse check lists every Kennzahl of the form text that has no field at
its line ("missing in the data") and every line of the form without any field.

Not checked: what the form does not describe by a line reference or a
printed operator, e.g. the operator with which a total enters the next total
(Kz 290 → 293 → 219, the totals of Anlage SZ built from other totals), the
6 % and the cap of Anlage SZ;
furthermore ``allow_negative``, the program scope, the relative years of
collective items, categories and the ELSTER field ids. The checklist lists
formula, sign and sums of each field for the release role; the reference
cases RT-01 and PT-01 compute them.

The check confirms data, it does not release them: the release (``state =
verified`` with ``verified_by``, ``verified_role``, ``verified_on``) is given
by the release role of F-11, never by this tool.

Usage::

    python3 -I _dev/werkzeuge/check_forms.py --text-dir <folder with the texts>
    python3 -I _dev/werkzeuge/check_forms.py --text-dir <folder> \\
        --years 2024 2025 2026 --markdown report.md --json report.json \\
        --csv checklist.csv

Exit code: 0 without deviation, 1 with deviations, 2 on missing input.
"""

import argparse
import csv
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FORMS_DIR = REPO / "l10n_de_euer" / "data" / "forms"
YEARS = (2024, 2025, 2026)
#: form type of the data → name of the form in the form ids (2025AnlEÜR801)
FORM_NAMES = {"euer": "EÜR", "aveuer": "AVEÜR", "sz": "SZ"}
FORM_LABELS = {"euer": "Anlage EÜR", "aveuer": "Anlage AVEÜR", "sz": "Anlage SZ"}
#: file name patterns in the text folder ({year}: 2025, {yy}: 25)
DEFAULT_FILES = {
    "layout": "bmf{year}.txt",
    "raw": "c{year}.txt",
    "pdf": "bmf{year}.pdf",
    "blank_euer": "e{yy}.txt",
    "blank_aveuer": "av{yy}.txt",
    "blank_sz": "sz{yy}.txt",
}
FOOTER = re.compile(r"(\d{4})Anl(EÜR|AVEÜR|SZ|LuF)(\d{3})")
FORM_START = "Name des Steuerpflichtigen"
#: line number at the start of a line; layout: indented by up to 20 spaces
LINE_LAYOUT = re.compile(
    r"^(?:\s*\d{12}\s+)?\s{0,20}(\d{1,3})(?:\s+(?=[^\s=%),.])|\s*$)"
)
LINE_RAW = re.compile(r"^(?:\d{12}\s+)?(\d{1,3})(?: (?=[^\s=%),.])|\s*$)")
#: headings (left, right) of two-column lines (Anlage EÜR): limited expenses,
#: amounts already included in the profit (InvStG, partial income method)
COLUMN_PAIRS = (("nicht abziehbar", "abziehbar"), ("Gesamtbetrag", "Korrekturbetrag"))
#: field types of the left and right column
COLUMN_TYPES = {"left": "amount_davon", "right": "amount"}
#: operator printed next to a Kennzahl → ``sign`` of the field
MARKERS = {
    "+": "+",
    "−": "-",
    "-": "-",
    "+/−": "±",
    "+/-": "±",
    "−/+": "±",
    "-/+": "±",
    "=": "=",
}
CODE_RE = re.compile(r"[A-Z]+\.[0-9A-Z]+\.[0-9A-Z_]+")
#: field types that enter a sum
SUMMABLE = ("amount", "transfer", "subtotal", "total", "constant")
FORM_TYPES_BY_NAME = {name: form_type for form_type, name in FORM_NAMES.items()}
_LINE_LIST = r"\d{1,3}(?:\s*(?:,|und|bis|oder)\s*(?:Zeilen?\s+)?\d{1,3})*"
#: source lines named in a label: "Summe Zeilen 24 bis 27", "Summe der Beträge
#: aus Zeilen 7, 14 ... der Anlage AVEÜR", "Übertrag aus Zeile 23"
SOURCE_REF = re.compile(
    r"(?:Summe(?:/Differenz)?(?:\s+der)?(?:\s+Beträge)?(?:\s+aus)?|\baus)"
    rf"\s+Zeilen?\s+(?P<lines>{_LINE_LIST})"
    r"(?:\s+der\s+Anlage\s+(?P<form>[A-Za-zÄÖÜ]+))?(?P<previous>\s+des\s+Vorjahres)?"
)
#: target line named in a label: "Übertrag in Zeile 77", "zu erfassen in
#: Zeile 39 der Anlage EÜR", "in Zeile 13 eintragen"
TARGET_REF = re.compile(
    r"(?:(?P<verb>Übertrag|erfassen)\s+in\s+Zeile\s+(?P<line>\d{1,3})"
    r"|in\s+Zeile\s+(?P<line2>\d{1,3})(?=(?:\s+der\s+Anlage\s+\S+)?\s+eintragen))"
    r"(?:\s+der\s+Anlage\s+(?P<form>[A-Za-zÄÖÜ]+))?"
)
#: references and numbers that are not Kennzahlen
NOT_KZ = (
    re.compile(
        r"Zeilen?\s+\d{1,3}(?:\s*(?:,|und|bis|oder|sowie|–|-)\s*\d{1,3})*",
        re.IGNORECASE,
    ),
    re.compile(r"§\s*\d+[a-z]?"),
    re.compile(r"(?:Abs\.|Satz|Nr\.|Art\.|Buchst\.|Tz\.|Rz\.)\s*\d+[a-z]?"),
    re.compile(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?"),
    re.compile(r"\d+(?:,\d+)?\s*(?:%|€|Euro|EUR)"),
    re.compile(r"\d{4}Anl\S*"),
    re.compile(r"\d{5,}"),
    re.compile(r"\d\)"),
)
KZ_TOKEN = re.compile(r"(?<![\d.,/])\d{3}(?![\d.,/])")
LINE_WINDOW = 12


# ---------------------------------------------------------------------------
# Form data (XML)
# ---------------------------------------------------------------------------


@dataclass
class FormYear:
    xmlid: str
    year: int
    form_type: str
    version: int
    state: str = ""
    vordruck_ids: str = ""
    bmf_date: str = ""
    bmf_reference: str = ""
    bmf_dok: str = ""
    instructions_version: str = ""
    source_sha256: str = ""
    verified_by: str = ""
    verified_role: str = ""
    verified_on: str = ""


@dataclass
class FormField:
    xmlid: str
    year: int
    form_type: str
    code: str
    box: str
    kz: str
    line: int
    label: str
    field_type: str
    sign: str = ""
    formula: str = ""


def _values(record):
    values = {}
    for item in record.findall("field"):
        name = item.get("name")
        if name and "@" not in name:
            values[name] = item.get("ref") or item.get("eval") or (item.text or "")
            values[name] = " ".join(values[name].split())
    return values


def read_forms(forms_dir, years):
    """Form years and fields of the XML data files: ``(forms, fields)``."""
    forms, fields = {}, []
    for path in sorted(Path(forms_dir).glob("*.xml")):
        root = ET.parse(path).getroot()
        records = list(root.iter("record"))
        for record in records:
            if record.get("model") != "l10n_de_euer.form":
                continue
            v = _values(record)
            form = FormYear(
                xmlid=record.get("id"),
                year=int(v.get("year", 0)),
                form_type=v.get("form_type", ""),
                version=int(v.get("version") or 1),
                **{
                    name: v.get(name, "")
                    for name in (
                        "state",
                        "vordruck_ids",
                        "bmf_date",
                        "bmf_reference",
                        "bmf_dok",
                        "instructions_version",
                        "source_sha256",
                        "verified_by",
                        "verified_role",
                        "verified_on",
                    )
                },
            )
            forms[form.xmlid] = form
        for record in records:
            if record.get("model") != "l10n_de_euer.field":
                continue
            v = _values(record)
            form = forms.get(v.get("form_id"))
            if not form or form.year not in years:
                continue
            fields.append(
                FormField(
                    xmlid=record.get("id"),
                    year=form.year,
                    form_type=form.form_type,
                    code=v.get("code", ""),
                    box=v.get("box", ""),
                    kz=v.get("kz", ""),
                    line=int(v.get("line") or 0),
                    label=v.get("official_label", ""),
                    field_type=v.get("field_type", ""),
                    sign=v.get("sign", ""),
                    formula=v.get("formula", ""),
                )
            )
    forms = {k: f for k, f in forms.items() if f.year in years}
    return forms, fields


# ---------------------------------------------------------------------------
# Form text
# ---------------------------------------------------------------------------


def form_pages(text, year, layout):
    """Lines of each form (``euer``/``aveuer``/``sz``) in the text of a year.

    Pages are identified by their form id in the footer (``2025AnlEÜR802``);
    the layout text separates pages by form feeds, the raw text is cut after
    each footer line.
    """
    names = {name: form_type for form_type, name in FORM_NAMES.items()}
    chunks = []
    if layout:
        chunks = [page.splitlines() for page in text.split("\f")]
    else:
        current = []
        for line in text.splitlines():
            current.append(line)
            if FOOTER.search(line):
                chunks.append(current)
                current = []
        chunks.append(current)
    pages = defaultdict(list)
    for chunk in chunks:
        ids = [
            m
            for line in chunk
            for m in FOOTER.finditer(line)
            if int(m.group(1)) == year and m.group(2) in names
        ]
        if not ids:
            continue
        form_type = names[ids[0].group(2)]
        page = int(ids[0].group(3))
        starts = [
            i for i, line in enumerate(chunk) if line.strip().startswith(FORM_START)
        ]
        if page % 10 == 1 and starts:
            chunk = chunk[starts[-1] :]
        pages[form_type].append((page, chunk))
    return {
        form_type: [line for _page, chunk in sorted(items) for line in chunk]
        for form_type, items in pages.items()
    }


@dataclass
class Block:
    """Text of a printed line. ``own_columns`` keeps the column (position
    in the layout line) where each text of ``own`` starts; the lines of
    ``before`` and ``after`` start in column 0."""

    line: int
    own: list = field(default_factory=list)
    before: list = field(default_factory=list)
    after: list = field(default_factory=list)
    own_columns: list = field(default_factory=list)


def line_blocks(lines, layout):
    """Blocks per printed line number: the text of the numbered line, the
    lines between it and the previous numbered line (``before``) and up to the
    next one (``after``). Line numbers must ascend (window of 12 lines); a
    repeated number (carry-over at the top of a page) extends its block."""
    pattern = LINE_LAYOUT if layout else LINE_RAW
    anchors = []
    prev = None
    for index, text in enumerate(lines):
        match = pattern.match(text)
        if not match:
            continue
        number = int(match.group(1))
        if prev is None:
            if number > 5:
                continue
        elif not prev <= number <= prev + LINE_WINDOW:
            continue
        anchors.append((index, number, text[match.end() :]))
        prev = number
    blocks = {}
    for pos, (index, number, rest) in enumerate(anchors):
        block = blocks.setdefault(number, Block(number))
        block.own.append(rest)
        block.own_columns.append(len(lines[index]) - len(rest))
        start = anchors[pos - 1][0] + 1 if pos else 0
        end = anchors[pos + 1][0] if pos + 1 < len(anchors) else len(lines)
        block.before.extend(lines[start:index])
        block.after.extend(lines[index + 1 : end])
    return blocks


def _masked(text):
    """Text with references and numbers that are no Kennzahl blanked out
    (same length, positions stay)."""
    for pattern in NOT_KZ:
        text = pattern.sub(lambda match: " " * len(match.group()), text)
    return text


def kz_tokens(text):
    """Three-digit numbers of a text that can be a Kennzahl."""
    return set(KZ_TOKEN.findall(_masked(text)))


def kz_columns(block):
    """Column (position in the layout line) of each Kennzahl of a block: in
    the numbered line, or in the lines after it (right column printed one
    line lower)."""
    columns = {}
    texts = list(zip(block.own, block.own_columns, strict=False))
    texts += [(text, 0) for text in block.after]
    for text, start in texts:
        for match in KZ_TOKEN.finditer(_masked(text)):
            columns.setdefault(match.group(), start + match.start())
    return columns


def normalized(text):
    """Text without whitespace, hyphens and soft hyphens (joins hyphenation)."""
    text = text.replace("­", "").replace("–", "-").replace("−", "-")
    return re.sub(r"[\s\-]+", "", text).lower()


#: column headings printed into the labels of some lines
COLUMN_HEADINGS = re.compile(r"EUR\s+Ct|Gesamtbetrag\s+Korrekturbetrag")


def label_exact(label, block):
    """The label stands word for word in the block (its own line and the
    lines of the label printed before it), without whitespace and hyphens."""
    if not label or block is None:
        return False
    text = COLUMN_HEADINGS.sub(" ", " ".join(block.before + block.own))
    return normalized(label) in normalized(text)


#: printed parts of a line that are no part of a label: barcodes of the form,
#: footnote marks after a word ("Vorjahres3)"), operators and cent commas
#: standing alone, the column headings "EUR" and "Ct"
LABEL_NOISE = (
    re.compile(r"\d{5,}"),
    re.compile(r"(?<=[A-Za-zÄÖÜäöüß])\d\)"),
    re.compile(r"(?<!\S)(?:\+/[−-]|[−-]/\+|[+−=,])(?!\S)"),
    re.compile(r"\b(?:EUR|Ct)\b"),
)


def _without_kz(text):
    """Text without its Kennzahlen; numbers of references ("Zeile 106",
    "§ 4") stay, see :func:`_masked`."""
    spans = [match.span() for match in KZ_TOKEN.finditer(_masked(text))]
    for start, end in reversed(spans):
        text = text[:start] + " " + text[end:]
    return text


def label_text(texts):
    """Text of lines for the comparison with a label: column headings, form
    ids, Kennzahlen and :data:`LABEL_NOISE` left out."""
    text = COLUMN_HEADINGS.sub(" ", " ".join(texts))
    text = FOOTER.sub(" ", text)
    for pattern in LABEL_NOISE:
        text = pattern.sub(" ", text)
    return _without_kz(text)


def _label_key(label):
    """Label for the comparison, without whitespace and hyphens."""
    return normalized(_without_kz(label))


def label_sequence(label, block):
    """Whether the label stands in the block as one sequence of words (its
    lines before, the numbered line and the lines after it), apart from
    Kennzahlen and the printed noise of :func:`label_text`."""
    if not label or block is None:
        return None
    text = label_text(block.before + block.own + block.after)
    return _label_key(label) in normalized(text)


def label_before_kz(label, kz, others, block):
    """Whether the label stands on the numbered line between the Kennzahl of
    another field before it (``others``) and its own Kennzahl ``kz``;
    ``None`` if the Kennzahl is not on that line. Finds two labels of one
    line swapped (EUER.15.131/132: "davon abweichender Beginn 131 ... davon
    abweichendes Ende 132")."""
    if not label or block is None:
        return None
    text = " ".join(block.own)
    positions = {}
    for match in KZ_TOKEN.finditer(_masked(text)):
        positions.setdefault(match.group(), match.start())
    if kz not in positions:
        return None
    start = max(
        (positions[o] for o in others if positions.get(o, len(text)) < positions[kz]),
        default=0,
    )
    return _label_key(label) in normalized(label_text([text[start : positions[kz]]]))


def label_found(label, block):
    """Share of the label found in the block (1.0: word for word)."""
    if not label or block is None:
        return None
    if label_exact(label, block):
        return 1.0
    text = COLUMN_HEADINGS.sub(" ", " ".join(block.before + block.own + block.after))
    text = normalized(text)
    target = normalized(label)
    if target in text:
        return 1.0
    words = [w for w in re.findall(r"\w{3,}", label.lower()) if not w.isdigit()]
    if not words:
        return 1.0
    return round(sum(1 for w in words if normalized(w) in text) / len(words), 2)


# ---------------------------------------------------------------------------
# Check
# ---------------------------------------------------------------------------


@dataclass
class FieldResult:
    year: int
    form: str
    code: str
    kz: str
    line: int
    layout: str
    raw: str
    blank: str
    status: str
    elsewhere: str = ""
    label_ratio: float | None = None
    label: str = ""
    field_type: str = ""
    sign: str = ""
    column: str = ""
    problems: list = field(default_factory=list)
    sums: str = ""
    column_heading: str = ""
    printed: str = ""
    total: str = ""
    formula: str = ""
    hints: list = field(default_factory=list)


#: a line with text (a label), not only Kennzahlen, commas and headings
_TEXT_LINE = re.compile(r"[A-Za-zÄÖÜäöüß]{3,}")


def _is_text_line(text):
    text = COLUMN_HEADINGS.sub(" ", text)
    text = re.sub(r"\b(?:EUR|Ct)\b", " ", text)
    text = FOOTER.sub(" ", text)
    return bool(_TEXT_LINE.search(text))


def own_region(block):
    """Lines next to the numbered line that belong to it: the lines after
    it up to the label of the next line (a Kennzahl of the right column is
    printed one line lower) and the lines of its own label before it (a
    Kennzahl printed above the line number, after the start of its label).
    Lines between two numbered lines without any label belong to the line
    above, like the right column printed one line lower."""
    after = []
    for text in block.after:
        if _is_text_line(text):
            break
        after.append(text)
    texts = [_is_text_line(text) for text in block.before]
    before = block.before[texts.index(True) :] if any(texts) else []
    return after, before


def locate(blocks, kz, line):
    """Where a Kennzahl is relative to the printed line ``line``: on it
    (``line``), next to it in its own region (``block``, see
    :func:`own_region`) or ``missing``."""
    block = blocks.get(line)
    if block is not None:
        if kz in kz_tokens(" ".join(block.own)):
            return "line"
        after, before = own_region(block)
        if kz in kz_tokens(" ".join(after)) or kz in kz_tokens(" ".join(before)):
            return "block"
    return "missing"


def where(blocks, kz):
    return sorted(n for n, b in blocks.items() if kz in kz_tokens(" ".join(b.own)))


SOURCES = ("layout", "raw")
MONTHS = (
    "Januar Februar März April Mai Juni Juli August September Oktober November Dezember"
).split()


def _read(path):
    return path.read_text(encoding="utf-8") if path.exists() else None


def load_texts(year, files):
    """Inputs of one year: ``(inputs, texts, blocks, blanks)``."""
    inputs = {
        kind: files[kind].name if files[kind].exists() else None for kind in files
    }
    texts = {kind: _read(files[kind]) for kind in SOURCES}
    if not any(texts.values()):
        raise FileNotFoundError(
            f"no text for {year}: {files['layout']}, {files['raw']}"
        )
    blocks = {}
    for kind, text in texts.items():
        pages = form_pages(text, year, kind == "layout") if text else {}
        blocks[kind] = {
            ft: line_blocks(lines, kind == "layout") for ft, lines in pages.items()
        }
    blanks = {}
    for form_type in FORM_NAMES:
        text = _read(files[f"blank_{form_type}"])
        blanks[form_type] = kz_tokens(text) if text else None
    return inputs, texts, blocks, blanks


def check_meta(form, letter, digest):
    """Form ids, BMF letter, version of the instructions and checksum."""
    issues = []
    flat = re.sub(r"\s+", "", letter)
    m = re.fullmatch(r"(\d{4}Anl\S+?)(\d{3})(?:[–-](\d{3}))?", form.vordruck_ids)
    if not m:
        issues.append(f"form ids {form.vordruck_ids!r} unreadable")
    else:
        for page in range(int(m.group(2)), int(m.group(3) or m.group(2)) + 1):
            if f"{m.group(1)}{page}" not in letter:
                issues.append(f"form id {m.group(1)}{page} not in the text")
    for name in ("bmf_reference", "bmf_dok"):
        value = getattr(form, name)
        if not value or re.sub(r"\s+", "", value) not in flat:
            issues.append(f"{name} {value!r} not in the text")
    if form.bmf_date:
        y, month, day = form.bmf_date.split("-")
        if f"{int(day)}.{MONTHS[int(month) - 1]}{y}" not in flat:
            issues.append(f"date {form.bmf_date} not in the text")
    version = form.instructions_version
    if version and version not in letter:
        issues.append(f"instructions version {version} not in the text")
    if not digest:
        sha = "no PDF"
    else:
        sha = "ok" if digest == form.source_sha256 else "differs"
        if sha == "differs":
            issues.append("SHA-256 of the PDF differs from source_sha256")
    return {
        "form": form.form_type,
        "xmlid": form.xmlid,
        "version": form.version,
        "state": form.state,
        "verified_by": form.verified_by,
        "verified_role": form.verified_role,
        "verified_on": form.verified_on,
        "sha256": sha,
        "issues": issues,
    }


def check_duplicates(year_fields):
    codes, pairs = defaultdict(list), defaultdict(list)
    for f in year_fields:
        codes[(f.form_type, f.code)].append(f.xmlid)
        if f.kz:
            pairs[(f.form_type, f.line, f.kz)].append(f.code)
    return [
        {"key": list(key), "items": items}
        for key, items in list(codes.items()) + list(pairs.items())
        if len(items) > 1
    ]


def code_problem(f):
    """The code must end with the Kennzahl of the field (spec 3.1)."""
    suffix = f.code.rsplit(".", 1)[-1]
    if suffix.isdigit() and suffix != f.kz:
        return f"code {f.code} does not end with the Kz {f.kz or '-'}"
    if f.kz and not suffix.isdigit():
        return f"code {f.code} has no Kz although the field has Kz {f.kz}"
    return ""


def column_of(f, line_fields, form_blocks):
    """Column of a field in a line with two Kennzahlen (layout text):
    ``left``, ``right`` or ``""`` (one Kennzahl, or not found)."""
    pair = [x for x in line_fields if x.kz]
    if (
        len(pair) != 2
        or not any(x is f for x in pair)
        or pair[0].kz == pair[1].kz
        or not form_blocks
        or f.line not in form_blocks
    ):
        return ""
    columns = kz_columns(form_blocks[f.line])
    if not all(x.kz in columns for x in pair):
        return ""
    other = next(x for x in pair if x is not f)
    if columns[f.kz] == columns[other.kz]:
        return ""
    return "left" if columns[f.kz] < columns[other.kz] else "right"


def column_headings(form_blocks, line):
    """Headings ``(left, right)`` of the two columns above the line (in its
    block or the blocks of the lines before it in the section), or ``None``."""
    numbers = sorted(n for n in form_blocks if n <= line)
    for number in reversed(numbers[-8:]):
        block = form_blocks[number]
        text = " ".join(block.before + block.own)
        for left, right in COLUMN_PAIRS:
            if left in text and right in text.replace(left, ""):
                return left, right
    return None


def printed_marker(block, kz, summable=False):
    """Operator printed next to the Kennzahl in its block (layout text):
    ``+``, ``-``, ``±``, ``=`` or ``""``. A summable field without Kennzahl
    (transfer, subtotal) takes the first operator standing alone on its own
    line, e.g. "abzüglich Betriebsausgaben (…)   −" or "Kumulierte
    Einlagen   =" (only the typographic minus, the hyphen separates the
    digits of identification numbers)."""
    if block is None:
        return ""
    if not kz:
        if not summable:
            return ""
        match = re.search(r"(?<=\s)(\+/−|−/\+|−|\+|=)(?=\s|$)", " ".join(block.own))
        return MARKERS[match.group(1)] if match else ""
    after, before = own_region(block)
    text = " ".join(block.own + after + before)
    match = re.search(rf"(?<![\d.,/]){kz}\s*(\+/[−-]|[−-]/\+|[−-]|\+|=)(?=\s|$)", text)
    return MARKERS[match.group(1)] if match else ""


def terms(formula):
    """``(sign, code)`` of every code of a formula: -1 if a minus stands
    directly before it, else +1."""
    result = []
    for match in CODE_RE.finditer(formula or ""):
        before = formula[: match.start()].rstrip()
        result.append((-1 if before.endswith("-") else 1, match.group()))
    return result


def effective_signs(total, code, by_code, seen=()):
    """Signs with which ``code`` enters the formula of ``total``, through
    subtotals of the same form."""
    signs = set()
    for sign, member in terms(total.formula):
        if member == code:
            signs.add(sign)
            continue
        sub = by_code.get(member)
        if (
            sub is not None
            and sub.form_type == total.form_type
            and sub.formula
            and member not in seen
        ):
            signs |= {
                sign * s for s in effective_signs(sub, code, by_code, (*seen, member))
            }
    return signs


def line_numbers(text):
    """Line numbers of a reference: "24 bis 27 und Zeilen 29 bis 61"."""
    numbers = set()
    span = re.compile(r"(\d{1,3})\s*bis\s*(?:Zeilen?\s+)?(\d{1,3})")
    for start, end in span.findall(text):
        numbers |= set(range(int(start), int(end) + 1))
    numbers |= {int(n) for n in re.findall(r"\d{1,3}", span.sub(" ", text))}
    return numbers


def _covered(code, form_type, lines, by_code, seen=()):
    """Lines of ``form_type`` within ``lines`` a formula member stands for
    (a subtotal outside the lines stands for its own members), and the
    members outside the lines."""
    member = by_code.get(code)
    if member is None:
        return set(), {code}
    if member.form_type == form_type and member.line in lines:
        return {member.line}, set()
    if member.formula and code not in seen:
        covered, outside = set(), set()
        for _sign, sub in terms(member.formula):
            lines_in, lines_out = _covered(
                sub, form_type, lines, by_code, (*seen, code)
            )
            covered |= lines_in
            outside |= lines_out
        return covered, outside
    return set(), {code}


def check_formulas(year_fields, layout_blocks):
    """Arithmetic of the form data against the form text: printed operators,
    line references of the labels and the sums of the Anlage EÜR.

    Returns ``{code: {"problems": [...], "hints": [...], "printed": marker,
    "total": code of the next total marked "=" that uses the field}}``.
    """
    by_code = {f.code: f for f in year_fields}
    at_line = defaultdict(list)
    for f in year_fields:
        at_line[(f.form_type, f.line)].append(f)
    used = defaultdict(list)
    for f in year_fields:
        for _sign, code in terms(f.formula):
            used[code].append(f.code)
    result = {
        f.code: {"problems": [], "hints": [], "printed": "", "total": ""}
        for f in year_fields
    }
    printed = {}
    for f in year_fields:
        block = (layout_blocks.get(f.form_type) or {}).get(f.line)
        printed[f.code] = printed_marker(block, f.kz, f.field_type in SUMMABLE)
        result[f.code]["printed"] = printed[f.code]
    equals = sorted(
        (f for f in year_fields if printed[f.code] == "="),
        key=lambda f: f.line,
    )
    for f in year_fields:
        res = result[f.code]
        mark = printed[f.code]
        if mark and mark != f.sign:
            res["problems"].append(
                f"printed '{mark}' next to the Kz, but sign {f.sign!r}"
            )
        if mark == "=" and f.field_type not in ("total", "subtotal"):
            res["problems"].append(f"printed '=', but field type {f.field_type}")
        if (f.field_type in ("total", "subtotal")) != (f.sign == "="):
            res["problems"].append(f"field type {f.field_type}, but sign {f.sign!r}")
        # informational values (sign 0) may be transferred to another form
        # (Kz 122/123 to Anlage SZ), but enter no sum of their own form
        users = [
            code
            for code in used.get(f.code, ())
            if f.field_type == "amount_davon" or by_code[code].form_type == f.form_type
        ]
        if (f.field_type == "amount_davon" or f.sign == "0") and users:
            res["problems"].append(
                f"'of which' or informational field used in the formula of "
                f"{', '.join(users)}"
            )
        # the Anlage EÜR adds every amount once: a field enters at most one
        # formula of the Anlage EÜR (transfers to Anlage SZ come on top)
        euer_users = [
            code for code in used.get(f.code, ()) if by_code[code].form_type == "euer"
        ]
        if f.form_type == "euer" and len(euer_users) > 1:
            res["problems"].append(
                f"used in several formulas of the Anlage EÜR: {', '.join(euer_users)}"
            )
        below = [c for c in equals if c.form_type == f.form_type and c.line > f.line]
        signs = effective_signs(below[0], f.code, by_code) if below else set()
        if below and signs:
            res["total"] = below[0].code
            res["problems"] += _sign_problems(f, mark, below[0].code, signs)
        elif below and _must_enter(f, mark, below[0], at_line, printed):
            res["problems"].append(
                f"does not enter {below[0].code}, the next total marked '=' below it"
            )
        if not res["total"]:
            res["total"] = next(
                (c.code for c in below if effective_signs(c, f.code, by_code)), ""
            )
        res["problems"] += _reference_problems(f, by_code, at_line)
        res["hints"] += _reference_hints(f, at_line)
        if mark == "=" and _total_must_be_used(f, equals) and not used.get(f.code):
            res["problems"].append("total marked '=' enters no formula")
    return result


def _total_must_be_used(f, equals):
    """Totals marked "=" of the Anlage EÜR and SZ go on into the next
    calculation, except the last total of their box (code prefix, e.g. Kz 219
    in ``EUER.G``). The totals of Anlage AVEÜR (book values at the end) are
    final values."""
    if f.form_type not in ("euer", "sz"):
        return False
    box = f.code.rsplit(".", 1)[0]
    last = max(c.line for c in equals if c.code.rsplit(".", 1)[0] == box)
    return f.line < last


def _signs_text(signs):
    return "/".join("+" if s > 0 else "-" for s in sorted(signs))


def _sign_problems(f, mark, total, signs):
    """Operator with which the field enters the next total marked "=" below
    it: as printed next to it; without a printed operator, an amount,
    transfer or subtotal is added unless its label starts with "abzüglich";
    and as its ``sign`` says."""
    problems = []
    if mark in ("+", "-"):
        expected, why = (1 if mark == "+" else -1), f"printed '{mark}'"
    elif mark == "±" and f.kz:
        expected, why = 1, "printed '+/−' (signed value, added)"
    elif not mark and f.field_type in ("amount", "transfer", "subtotal"):
        minus = f.label.lower().startswith("abzüglich")
        expected = -1 if minus else 1
        why = "label 'abzüglich'" if minus else "no operator printed (adds)"
    else:
        expected = None
    if expected is not None and signs != {expected}:
        problems.append(f"{why}, but {total} uses it with {_signs_text(signs)}")
    wanted = {"+": {1}, "-": {-1}, "±": {1}}.get(f.sign)
    if wanted and f.field_type not in ("total", "subtotal") and signs != wanted:
        problems.append(
            f"sign {f.sign!r}, but {total} uses it with {_signs_text(signs)}"
        )
    return problems


def _must_enter(f, mark, total, at_line, printed):
    """A field marked ``+``/``−`` (or ``+/−`` with a Kennzahl), a field whose
    ``sign`` is ``+``/``-`` and every amount of the Anlage EÜR must enter the
    next total marked "=" below it, unless a total with a Kennzahl but
    without printed operator stands between them (e.g. the sum of all
    reversals of collective items in Anlage AVEÜR)."""
    if (
        mark not in ("+", "-")
        and not (mark == "±" and f.kz)
        and f.sign not in ("+", "-")
        and not (
            f.form_type == "euer" and f.field_type in ("amount", "transfer", "subtotal")
        )
    ):
        return False
    for line in range(f.line + 1, total.line):
        for other in at_line.get((f.form_type, line), ()):
            if (
                other.kz
                and other.field_type in ("total", "subtotal")
                and not printed.get(other.code)
            ):
                return False
    return True


def _reference_problems(f, by_code, at_line):
    """The formula uses the lines the label names as its source, and the
    target line the label names uses the field."""
    problems = []
    members = [code for _sign, code in terms(f.formula)]
    for match in SOURCE_REF.finditer(f.label):
        if match.group("previous"):
            continue
        form_type = (
            FORM_TYPES_BY_NAME.get(match.group("form"))
            if match.group("form")
            else f.form_type
        )
        if form_type is None:
            continue
        lines = line_numbers(match.group("lines"))
        if not members:
            problems.append(f"label names lines {sorted(lines)}, but no formula")
            continue
        covered, outside = set(), set()
        for code in members:
            lines_in, lines_out = _covered(code, form_type, lines, by_code)
            covered |= lines_in
            outside |= lines_out
        needed = {
            n
            for n in lines
            if any(x.field_type in SUMMABLE for x in at_line[(form_type, n)])
        }
        if needed - covered:
            problems.append(
                f"lines {sorted(needed - covered)} named in the label "
                "are not in the formula"
            )
        if outside:
            problems.append(
                f"formula uses {', '.join(sorted(outside))} outside the lines "
                f"{sorted(lines)} named in the label"
            )
        phrase = match.group()
        transfer = f.label[: match.start()].rstrip().endswith("Übertrag")
        adds = (
            (phrase.startswith("Summe") and not phrase.startswith("Summe/Differenz"))
            or (phrase.startswith("aus") and transfer)
            or (
                phrase.startswith("aus") and not f.label.lower().startswith("abzüglich")
            )
        )
        minus = [code for sign, code in terms(f.formula) if sign < 0]
        if adds and minus:
            problems.append(
                f"label '{phrase.split()[0]} …' adds, but the formula subtracts "
                f"{', '.join(minus)}"
            )
    for match in TARGET_REF.finditer(f.label):
        if match.group("verb") == "erfassen":
            continue
        form_type = (
            FORM_TYPES_BY_NAME.get(match.group("form"))
            if match.group("form")
            else f.form_type
        )
        if form_type is None:
            continue
        line = int(match.group("line") or match.group("line2"))
        targets = at_line.get((form_type, line), [])
        signs = {
            sign for t in targets for sign, code in terms(t.formula) if code == f.code
        }
        if not signs:
            problems.append(
                f"label transfers it to line {line}, no formula there uses it"
            )
        elif match.group("verb") == "Übertrag" and signs != {1}:
            problems.append(
                f"label transfers it to line {line}, but the formula there "
                f"uses it with {_signs_text(signs)}"
            )
    return problems


def _reference_hints(f, at_line):
    """ "zu erfassen in Zeile …" without a formula of the target line."""
    hints = []
    for match in TARGET_REF.finditer(f.label):
        if match.group("verb") != "erfassen":
            continue
        form_type = (
            FORM_TYPES_BY_NAME.get(match.group("form"))
            if match.group("form")
            else f.form_type
        )
        if form_type is None:
            continue
        line = int(match.group("line"))
        targets = at_line.get((form_type, line), [])
        if not any(f.code in CODE_RE.findall(t.formula or "") for t in targets):
            codes = ", ".join(t.code for t in targets) or "-"
            hints.append(
                f"to be included in line {line} ({FORM_LABELS[form_type]}: {codes}), "
                "no formula there uses it: check by eye"
            )
    return hints


def check_field(f, blocks, blanks, line_fields=(), sums="", formula_result=None):
    """Result of one field against both extractions and the blank form."""
    res = {}
    for kind in SOURCES:
        form_blocks = blocks[kind].get(f.form_type)
        if not form_blocks:
            res[kind] = "no text"
        elif f.kz:
            res[kind] = locate(form_blocks, f.kz, f.line)
        else:
            res[kind] = "line" if f.line in form_blocks else "missing"
    blank = blanks.get(f.form_type)
    if not f.kz or blank is None:
        blank_res = "-"
    else:
        blank_res = "yes" if f.kz in blank else "no"
    found = [res[k] for k in SOURCES if res[k] in ("line", "block")]
    available = [k for k in SOURCES if res[k] != "no text"]
    if found and len(found) == len(available):
        status = "confirmed"
    else:
        status = "one source" if found else "deviation"
    problems = []
    problem = code_problem(f)
    if problem:
        problems.append(problem)
    layout_blocks = blocks["layout"].get(f.form_type) or {}
    column = column_of(f, line_fields, layout_blocks)
    headings = column_headings(layout_blocks, f.line) if column else None
    heading = ""
    if column:
        heading = headings[0 if column == "left" else 1] if headings else column
    if headings:
        expected = COLUMN_TYPES[column]
        if f.field_type != expected:
            problems.append(
                f"in the column '{heading}', but field type {f.field_type} "
                f"(expected {expected})"
            )
    ratios = [
        label_found(f.label, blocks[k].get(f.form_type, {}).get(f.line))
        for k in SOURCES
    ]
    ratios = [r for r in ratios if r is not None]
    if f.label and not f.kz:
        exact = [
            label_exact(f.label, blocks[k].get(f.form_type, {}).get(f.line))
            for k in available
        ]
        if not all(exact):
            problems.append("official label not word for word on its line")
    elif f.label and ratios and max(ratios) < 1:
        problems.append(
            f"official label differs from the form text (share of its words "
            f"found {max(ratios)})"
        )
    elif f.label:
        problems += _kz_label_problems(f, blocks, available, line_fields)
    formula_result = formula_result or {}
    problems += formula_result.get("problems", [])
    if problems and status == "confirmed":
        status = "deviation"
    elsewhere = ""
    if f.kz and status != "confirmed":
        spots = {k: where(blocks[k].get(f.form_type, {}), f.kz) for k in SOURCES}
        elsewhere = "; ".join(f"{k}: {v}" for k, v in spots.items() if v)
    return asdict(
        FieldResult(
            year=f.year,
            form=f.form_type,
            code=f.code,
            kz=f.kz,
            line=f.line,
            layout=res["layout"],
            raw=res["raw"],
            blank=blank_res,
            status=status,
            elsewhere=elsewhere,
            label_ratio=max(ratios) if ratios else None,
            label=f.label,
            field_type=f.field_type,
            sign=f.sign,
            column=column,
            problems=problems,
            sums=sums,
            column_heading=heading,
            printed=formula_result.get("printed", ""),
            total=formula_result.get("total", ""),
            formula=f.formula,
            hints=formula_result.get("hints", []),
        )
    )


def _kz_label_problems(f, blocks, available, line_fields):
    """Label of a field with Kennzahl: one sequence of words in the text of
    its line, before its own Kennzahl if other labels share the line."""
    found = [
        label_sequence(f.label, blocks[k].get(f.form_type, {}).get(f.line))
        for k in available
    ]
    if not any(found):
        return ["official label not found as one sequence of words on its line"]
    others = [
        g.kz
        for g in line_fields
        if g.kz and g.kz != f.kz and g.label and g.label != f.label
    ]
    if not others:
        return []
    placed = [
        label_before_kz(
            f.label, f.kz, others, blocks[k].get(f.form_type, {}).get(f.line)
        )
        for k in available
    ]
    placed = [value for value in placed if value is not None]
    if placed and not any(placed):
        return [
            "official label not before its Kennzahl on its line (labels of the "
            "line swapped?)"
        ]
    return []


def sums_of(year_fields):
    """Codes of the fields whose formula uses a field: ``{code: "A, B"}``."""
    used = defaultdict(list)
    for f in year_fields:
        for code in re.findall(r"[A-Z]+\.[0-9A-Z]+\.[0-9A-Z_]+", f.formula or ""):
            used[(f.form_type, code)].append(f.code)
            if code.split(".", 1)[0] != f.code.split(".", 1)[0]:
                used[("*", code)].append(f.code)
    result = {}
    for f in year_fields:
        codes = used.get((f.form_type, f.code), []) + used.get(("*", f.code), [])
        result[f.xmlid] = ", ".join(sorted(set(codes)))
    return result


def _candidates(form_blocks):
    """(number, Kennzahl, allowed lines) of every Kennzahl-like number: on
    the numbered line or in its own region (:func:`own_region`)."""
    for number, block in sorted(form_blocks.items()):
        after, before = own_region(block)
        for kz in kz_tokens(" ".join(block.own + after + before)):
            yield number, kz, {number}


def reverse_check(year_fields, blocks):
    """Kennzahlen of the text without a field at their line, and printed
    lines without any field: ``(reverse, lines_without_field)``."""
    kz_lines = defaultdict(lambda: defaultdict(set))
    lines = defaultdict(set)
    for f in year_fields:
        lines[f.form_type].add(f.line)
        if f.kz:
            kz_lines[f.form_type][f.kz].add(f.line)
    found = defaultdict(set)
    for kind in SOURCES:
        for form_type, form_blocks in blocks[kind].items():
            for _number, kz, allowed in _candidates(form_blocks):
                if not kz_lines[form_type].get(kz, set()) & allowed:
                    found[(form_type, kz, min(allowed))].add(kind)
            for number in form_blocks:
                if number not in lines[form_type]:
                    found[(form_type, "-", number)].add(kind)
    reverse, without = [], []
    for (form_type, kz, number), kinds in sorted(found.items()):
        entry = {
            "form": form_type,
            "kz": kz,
            "line": number,
            "sources": sorted(kinds),
            "data_lines": sorted(kz_lines[form_type].get(kz, set())),
        }
        (without if kz == "-" else reverse).append(entry)
    return reverse, without


def check_year(year, forms, fields, files):
    """All results of one year."""
    inputs, texts, blocks, blanks = load_texts(year, files)
    letter = texts["layout"] or texts["raw"]
    pdf = files["pdf"]
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest() if pdf.exists() else None
    year_forms = sorted(
        (f for f in forms.values() if f.year == year),
        key=lambda f: (f.form_type, f.version),
    )
    year_fields = [f for f in fields if f.year == year]
    by_line = defaultdict(list)
    for f in year_fields:
        by_line[(f.form_type, f.line)].append(f)
    sums = sums_of(year_fields)
    formulas = check_formulas(year_fields, blocks["layout"])
    reverse, without = reverse_check(year_fields, blocks)
    return {
        "year": year,
        "inputs": inputs,
        "meta": [check_meta(form, letter, digest) for form in year_forms],
        "fields": [
            check_field(
                f,
                blocks,
                blanks,
                by_line[(f.form_type, f.line)],
                sums.get(f.xmlid, ""),
                formulas.get(f.code),
            )
            for f in year_fields
        ],
        "reverse": reverse,
        "lines_without_field": without,
        "duplicates": check_duplicates(year_fields),
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _pair(f):
    return f"{f['code']} (line {f['line']}, Kz {f['kz'] or '-'})"


def deviations(result):
    """Deviations of one year (those that block a release)."""
    items = []
    for meta in result["meta"]:
        items += [f"{meta['form']}: {issue}" for issue in meta["issues"]]
    for f in result["fields"]:
        if f["status"] == "confirmed":
            continue
        spot = f", Kz printed in lines {f['elsewhere']}" if f["elsewhere"] else ""
        if f["layout"] not in ("line", "block") and f["raw"] not in ("line", "block"):
            items.append(f"{_pair(f)}: not found{spot}")
        elif f["status"] == "one source" or not f.get("problems"):
            items.append(
                f"{_pair(f)}: found by one extraction only (layout "
                f"{f['layout']}, raw {f['raw']}){spot}"
            )
        items += [f"{_pair(f)}: {problem}" for problem in f.get("problems", ())]
    for r in result["reverse"]:
        if len(r["sources"]) == len(available_sources(result)):
            spot = f", data has it in line {r['data_lines']}" if r["data_lines"] else ""
            items.append(
                f"{r['form']}: Kz {r['kz']} printed at line {r['line']} "
                f"without field{spot}"
            )
    for r in result["lines_without_field"]:
        if len(r["sources"]) == len(available_sources(result)):
            items.append(f"{r['form']}: printed line {r['line']} without any field")
    items += [f"duplicate {d['key']}: {d['items']}" for d in result["duplicates"]]
    return items


def available_sources(result):
    return [kind for kind in SOURCES if result["inputs"].get(kind)]


def year_diff(fields, years):
    """Changes of the codes from one year to the next (spec 3.4 step 3)."""
    diffs = []
    by_year = defaultdict(dict)
    for f in fields:
        by_year[(f.year, f.form_type)][f.code] = f
    ordered = sorted(years)
    for prev, cur in zip(ordered, ordered[1:], strict=False):
        for form_type in FORM_NAMES:
            old, new = by_year[(prev, form_type)], by_year[(cur, form_type)]
            shifts = defaultdict(list)
            for code in sorted(set(old) & set(new), key=lambda c: old[c].line):
                a, b = old[code], new[code]
                if b.line != a.line:
                    shifts[str(b.line - a.line)].append(f"{code} {a.line}→{b.line}")
                elif a.kz != b.kz:
                    shifts["kz"].append(f"{code} Kz {a.kz}→{b.kz}")
            diffs.append(
                {
                    "years": [prev, cur],
                    "form": form_type,
                    "added": sorted(set(new) - set(old)),
                    "removed": sorted(set(old) - set(new)),
                    "shifts": dict(shifts),
                }
            )
    return diffs


def _summary_rows(results):
    rows = [
        "| Year | Form | Fields | with Kz | confirmed | one source | deviation "
        "| Kz in blank form | label < 1.0 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        for form_type in FORM_NAMES:
            items = [f for f in result["fields"] if f["form"] == form_type]
            if not items:
                continue
            kz = [f for f in items if f["kz"]]
            blank = sum(f["blank"] == "yes" for f in kz)
            count = {s: sum(f["status"] == s for f in items) for s in STATUSES}
            weak = sum(
                f["label_ratio"] is not None and f["label_ratio"] < 1 for f in items
            )
            rows.append(
                f"| {result['year']} | {FORM_LABELS[form_type]} | {len(items)} "
                f"| {len(kz)} | {count['confirmed']} | {count['one source']} "
                f"| {count['deviation']} | {blank}/{len(kz)} | {weak} |"
            )
    return rows


STATUSES = ("confirmed", "one source", "deviation")


def _section(title, lines):
    return ["", title, *lines] if lines else []


def _year_lines(result):
    out = ["", f"## {result['year']}", ""]
    out.append(", ".join(f"{k} = {v}" for k, v in result["inputs"].items()))
    out.append("")
    for meta in result["meta"]:
        issues = f"; issues: {'; '.join(meta['issues'])}" if meta["issues"] else ""
        out.append(
            f"- {meta['xmlid']}: state {meta['state'] or '-'}, "
            f"version {meta['version']}, SHA-256 {meta['sha256']}, "
            f"verified_by {meta['verified_by'] or '-'}, "
            f"verified_role {meta['verified_role'] or '-'}, "
            f"verified_on {meta['verified_on'] or '-'}{issues}"
        )
    devs = deviations(result)
    out += ["", f"Deviations: {len(devs)}", *[f"- {d}" for d in devs]]
    fields = result["fields"]
    out += _section(
        "Found by one extraction only (deviation):",
        [
            f"- {_pair(f)}: layout {f['layout']}, raw {f['raw']}"
            + (f"; Kz printed in lines {f['elsewhere']}" if f["elsewhere"] else "")
            for f in fields
            if f["status"] == "one source"
        ],
    )
    out += _section(
        "Confirmed next to the numbered line (label or box over several lines):",
        [
            f"- {_pair(f)}: layout {f['layout']}, raw {f['raw']}"
            for f in fields
            if f["status"] == "confirmed" and "block" in (f["layout"], f["raw"])
        ],
    )
    out += _section(
        "Kennzahl-like numbers without field in one extraction only (check by eye):",
        [
            f"- {r['form']} line {r['line']}: {r['kz']} ({r['sources'][0]})"
            for r in result["reverse"]
            if len(r["sources"]) < len(available_sources(result))
        ],
    )
    out += _section(
        "Printed lines without any field in one extraction only (check by eye):",
        [
            f"- {r['form']} line {r['line']} ({', '.join(r['sources'])})"
            for r in result["lines_without_field"]
            if len(r["sources"]) < len(available_sources(result))
        ],
    )
    out += _section(
        "Official label not found word for word (share of words found):",
        [
            f"- {_pair(f)}: {f['label_ratio']}"
            for f in fields
            if f["label_ratio"] is not None and f["label_ratio"] < 1
        ],
    )
    out += _section(
        "Kennzahl not in the blank form:",
        [f"- {_pair(f)}" for f in fields if f["blank"] == "no"],
    )
    out += _section(
        "Hints (no deviation, check by eye):",
        [f"- {_pair(f)}: {hint}" for f in fields for hint in f.get("hints", ())],
    )
    return out


def _diff_lines(diffs):
    out = ["", "## Changes of the codes from year to year", ""] if diffs else []
    for d in diffs:
        out.append(
            f"- {FORM_LABELS[d['form']]} {d['years'][0]} → {d['years'][1]}: "
            f"added {', '.join(d['added']) or '-'}; "
            f"removed {', '.join(d['removed']) or '-'}"
        )
        for delta, items in d["shifts"].items():
            label = "Kz changed" if delta == "kz" else f"line {int(delta):+d}"
            text = (
                f"{len(items)} fields ({items[0]} … {items[-1]})"
                if len(items) > 3
                else ", ".join(items)
            )
            out.append(f"  - {label}: {text}")
    return out


CHECKLIST = (
    "year",
    "form",
    "line",
    "kz",
    "code",
    "official_label",
    "column",
    "printed_operator",
    "field_type",
    "sign",
    "goes_into",
    "total_below",
    "formula",
    "layout",
    "raw",
    "blank_form",
    "result",
    "notes",
    "reviewed_by_release_role",
)


def _total_name(code, kz_by_code):
    if not code:
        return ""
    kz = kz_by_code.get(code)
    return f"Kz {kz}" if kz else code


def write_checklist(path, results):
    """One row per field, sorted like the form, with an empty column for the
    person who verifies the form year (F-11). ``column`` is the heading of
    the column in a two-column line (``left``/``right`` without heading),
    ``total_below`` the next total marked "=" the field enters."""
    order = {form_type: i for i, form_type in enumerate(FORM_NAMES)}
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(CHECKLIST)
        for result in results:
            kz_by_code = {f["code"]: f["kz"] for f in result["fields"]}
            rows = sorted(
                result["fields"],
                key=lambda f: (order[f["form"]], f["line"], f["kz"], f["code"]),
            )
            for f in rows:
                notes = list(f.get("problems", ())) + list(f.get("hints", ()))
                writer.writerow(
                    [
                        f["year"],
                        FORM_LABELS[f["form"]],
                        f["line"],
                        f["kz"],
                        f["code"],
                        f["label"],
                        f.get("column_heading", ""),
                        f.get("printed", ""),
                        f.get("field_type", ""),
                        f.get("sign", ""),
                        f.get("sums", ""),
                        _total_name(f.get("total", ""), kz_by_code),
                        f.get("formula", ""),
                        f["layout"],
                        f["raw"],
                        f["blank"],
                        f["status"],
                        "; ".join(notes),
                        "",
                    ]
                )


def markdown(results, diffs=()):
    out = ["# Form data check (F-11)", "", *_summary_rows(results)]
    for result in results:
        out += _year_lines(result)
    out += _diff_lines(diffs)
    return "\n".join(out) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--text-dir", required=True, type=Path, help="folder with the texts"
    )
    parser.add_argument("--forms-dir", type=Path, default=FORMS_DIR)
    parser.add_argument("--years", type=int, nargs="+", default=list(YEARS))
    for kind, pattern in DEFAULT_FILES.items():
        parser.add_argument(
            f"--{kind.replace('_', '-')}", default=pattern, help=f"default {pattern}"
        )
    parser.add_argument("--markdown", type=Path, help="write the report as Markdown")
    parser.add_argument("--json", type=Path, help="write all results as JSON")
    parser.add_argument(
        "--csv",
        type=Path,
        help="write a checklist for the release role (one row per field)",
    )
    args = parser.parse_args(argv)
    if not args.text_dir.is_dir():
        sys.stderr.write(f"text folder {args.text_dir} not found\n")
        return 2
    forms, fields = read_forms(args.forms_dir, set(args.years))
    results = []
    for year in args.years:
        files = {
            kind: args.text_dir
            / getattr(args, kind).format(year=year, yy=f"{year % 100:02d}")
            for kind in DEFAULT_FILES
        }
        try:
            results.append(check_year(year, forms, fields, files))
        except FileNotFoundError as exc:
            sys.stderr.write(f"{exc}\n")
            return 2
    diffs = year_diff(fields, args.years)
    report = markdown(results, diffs)
    if args.markdown:
        args.markdown.write_text(report, encoding="utf-8")
    if args.csv:
        write_checklist(args.csv, results)
    if args.json:
        args.json.write_text(
            json.dumps(
                {"years": results, "diffs": diffs}, indent=1, ensure_ascii=False
            ),
            encoding="utf-8",
        )
    total = 0
    for result in results:
        devs = deviations(result)
        total += len(devs)
        counts = defaultdict(int)
        for f in result["fields"]:
            counts[f["status"]] += 1
        sys.stdout.write(
            f"{result['year']}: {len(result['fields'])} fields, "
            + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
            + f"; deviations {len(devs)}\n"
        )
        for d in devs:
            sys.stdout.write(f"  - {d}\n")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
