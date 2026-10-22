#!/usr/bin/env python3
# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Write a German ``de.po`` from a ``.pot`` file and a translation table.

The table is a JSON object ``{msgid: msgstr}`` (``de_<addon>.json`` next to
this script). Every entry of the template keeps its comments and references;
its ``msgstr`` is taken from the table. The header gets ``Language: de``.
Entries without a translation get an empty ``msgstr`` and are listed at the
end, so the table can be completed.

Usage::

    python3 -I _dev/werkzeuge/po_fill.py ADDON/i18n/ADDON.pot \
        _dev/werkzeuge/de_ADDON.json ADDON/i18n/de.po
"""

# pylint: disable=print-used

import json
import sys

LANGUAGE_EMPTY = '"Language: \\n"'
LANGUAGE_DE = '"Language: de\\n"'
LANGUAGE_PREFIX = '"Language: de'
ENCODING = '"Content-Transfer-Encoding: \\n"'


def parse(path):
    """Entries of a PO file as lists of lines (separated by empty lines)."""
    blocks, current = [], []
    with open(path, encoding="utf-8") as file:
        for line in file:
            if line.strip():
                current.append(line.rstrip("\n"))
            elif current:
                blocks.append(current)
                current = []
    if current:
        blocks.append(current)
    return blocks


def msgid_of(block):
    """The ``msgid`` of an entry, continuation lines joined."""
    parts, inside = [], False
    for line in block:
        if line.startswith("msgid "):
            inside = True
            parts.append(json.loads(line[6:]))
        elif line.startswith("msgstr"):
            break
        elif inside and line.startswith('"'):
            parts.append(json.loads(line))
    return "".join(parts)


def quote(text):
    """A PO string literal (the escapes of PO and JSON agree here)."""
    return json.dumps(text, ensure_ascii=False)


def header(block):
    """The header entry with ``Language: de``."""
    text = "\n".join(block).replace(LANGUAGE_EMPTY, LANGUAGE_DE)
    if LANGUAGE_PREFIX not in text:
        text = text.replace(ENCODING, f"{ENCODING}\n{LANGUAGE_DE}")
    return text


def fill(pot_path, table, out_path):
    """Write the PO file; return the msgids without translation."""
    entries, missing = [], []
    for block in parse(pot_path):
        msgid = msgid_of(block)
        if msgid == "":
            entries.append(header(block))
            continue
        index = next(i for i, line in enumerate(block) if line.startswith("msgstr"))
        msgstr = table.get(msgid, "")
        if not msgstr:
            missing.append(msgid)
        entries.append("\n".join(block[:index]) + "\nmsgstr " + quote(msgstr))
    with open(out_path, "w", encoding="utf-8") as file:
        file.write("\n\n".join(entries) + "\n")
    return missing


def main(argv):
    if len(argv) != 3:
        sys.exit(__doc__)
    pot_path, table_path, out_path = argv
    with open(table_path, encoding="utf-8") as file:
        table = json.load(file)
    missing = fill(pot_path, table, out_path)
    print("missing:", len(missing))
    for msgid in missing:
        print("  ", msgid)


if __name__ == "__main__":
    main(sys.argv[1:])
