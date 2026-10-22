# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Form field of a revenue line (spec 4.4, R-26, R-29).

Pure Python without any ORM access. The VAT treatment of the revenue
(small business ``ku`` or standard taxation ``regular``) is an input: it is
never derived from taxes or tax tags (K-06).
"""

import re

KU = "ku"
REGULAR = "regular"

FIELD_KU = "EUER.20.111"
FIELD_KU_DAVON = "EUER.20.119"
FIELD_TAXABLE = "EUER.20.112"
FIELD_EXEMPT = "EUER.20.103"
FIELD_LUF = "EUER.20.104"

#: UStVA tag number of the base line → form field (R-29). 76 and 77 are
#: supplies under § 24 UStG (Kz 104); 49 (new vehicles, tax-free) and the
#: 0 % rate 87 are tax-exempt (Kz 103). Tag 87 of the Odoo tax for small
#: businesses and medical services (``KU_TAXES``) does not count: see
#: :func:`revenue_fields`.
TAG_FIELDS = {
    "81": FIELD_TAXABLE,
    "86": FIELD_TAXABLE,
    "35": FIELD_TAXABLE,
    "76": FIELD_LUF,
    "77": FIELD_LUF,
    "41": FIELD_EXEMPT,
    "42": FIELD_EXEMPT,
    "43": FIELD_EXEMPT,
    "44": FIELD_EXEMPT,
    "48": FIELD_EXEMPT,
    "49": FIELD_EXEMPT,
    "60": FIELD_EXEMPT,
    "87": FIELD_EXEMPT,
    "21": FIELD_EXEMPT,
    "45": FIELD_EXEMPT,
}
#: Tags of non-taxable supplies (R-26: "of which" Kz 119 for small businesses)
NOT_TAXABLE_TAGS = frozenset({"21", "45"})
#: Revenue types that go into Kz 119 for small businesses (R-26)
KU_DAVON_ARTS = frozenset({"nstb", "frei192", "hilfs", "s19a"})
#: Revenue types whose field never depends on the VAT treatment (4.4 no. 1)
FIXED_ARTS = frozenset({"fixed", "luf24"})
#: Odoo taxes "0 % USt (Pflichtbefreit z.B. als Kleinunt. oder bei med.
#: Leistg.)" (template XML ids without company prefix). Under standard
#: taxation a line with such a tax is treated like a line on a small business
#: revenue account: the VAT rate is entered per invoice (spec 4.4 no. 5).
KU_TAXES = frozenset({"tax_ust_no_ustpflicht_skr03", "tax_ust_no_ustpflicht_skr04"})
_XMLID_PREFIX_RE = re.compile(r"^(?:[a-z0-9_]+\.)?\d+_")
#: Priority if a line carries tags of several fields
_PRIORITY = (FIELD_LUF, FIELD_TAXABLE, FIELD_EXEMPT)
_TAG_RE = re.compile(r"^[+-]?(\d+)")


def tag_numbers(tag_names):
    """Return the UStVA numbers of tag names such as ``81_BASE`` or ``-45``."""
    numbers = set()
    for name in tag_names or ():
        match = _TAG_RE.match((name or "").strip())
        if match:
            numbers.add(match.group(1))
    return numbers


def field_from_tags(tag_names):
    """Return the revenue field of the tags of a base line, or ``None``."""
    fields = {TAG_FIELDS[n] for n in tag_numbers(tag_names) if n in TAG_FIELDS}
    for field in _PRIORITY:
        if field in fields:
            return field
    return None


def is_ku_tax(xml_id):
    """Return whether ``xml_id`` (``account.<company>_<template id>``) is the
    Odoo tax for small businesses and medical services."""
    return _XMLID_PREFIX_RE.sub("", xml_id or "") in KU_TAXES


def revenue_fields(
    be_art, regime, tag_names=(), default=None, ku_tax=False, real_tax=False
):
    """Return ``(field, davon_field)`` of a realised revenue slice (spec 4.4).

    :param be_art: revenue type of the category (``stpfl``, ``tax`` ...)
    :param regime: ``ku`` or ``regular`` of the supply (R-25)
    :param tag_names: tax tag names of the base line
    :param default: field of the category for standard taxation
    :param ku_tax: the base line carries one of ``KU_TAXES``. Under standard
        taxation its tag 87 does not mean tax-exempt: the line is net revenue
        (Kz 112) after the VAT rate entered per invoice (4.4 no. 5, see
        :func:`needs_tax_rate_input`).
    :param real_tax: the base line carries an output VAT with a rate above 0
        (its tax line counts). A small business revenue account under
        standard taxation is then ordinary revenue after its tags.
    """
    if regime not in (KU, REGULAR):
        raise ValueError(f"Unknown VAT treatment {regime!r}")
    if be_art in FIXED_ARTS:
        return default, None
    if regime == REGULAR and ku_tax:
        return FIELD_TAXABLE, None
    if regime == REGULAR and be_art == "ku" and real_tax:
        return field_from_tags(tag_names) or default or FIELD_TAXABLE, None
    if regime == KU:
        davon = be_art in KU_DAVON_ARTS or (
            # the tag of the default tax of 8195/4185 (tax_free_eu) must not
            # count as "not taxable" (K-06)
            be_art != "ku" and bool(tag_numbers(tag_names) & NOT_TAXABLE_TAGS)
        )
        return FIELD_KU, FIELD_KU_DAVON if davon else None
    if be_art == "tax":
        return field_from_tags(tag_names) or default, None
    if be_art == "stpfl":
        # taxable revenue account, but the tax of the line is tax-exempt or
        # § 24 UStG: the tags decide (R-29)
        tagged = field_from_tags(tag_names)
        if tagged in (FIELD_EXEMPT, FIELD_LUF):
            return tagged, None
    return default, None


def needs_tax_rate_input(
    be_art, regime, tag_names=(), has_tax=False, ku_tax=False, real_tax=False
):
    """Return whether a revenue line needs the VAT rate entered per invoice.

    Spec 4.4 no. 5: under standard taxation a line on a small business revenue
    account, with the Odoo small business tax (``ku_tax``) or of a taxable
    category without tax and without a tag "tax-exempt/not taxable" (typically
    after exceeding the small business limit) contains the VAT; the rate input
    splits it into Kz 112 and 140 (0 %: Kz 103). Missing input: C-32/C-33.
    A line with a real output VAT (``real_tax``: rate above 0, its own tax
    line) never needs it, even on a small business revenue account: the VAT
    is already split off.
    """
    if regime != REGULAR or be_art in FIXED_ARTS or real_tax:
        return False
    if be_art == "ku" or ku_tax:
        return True
    if has_tax or be_art not in ("stpfl", "tax"):
        return False
    return field_from_tags(tag_names) in (None, FIELD_TAXABLE)
