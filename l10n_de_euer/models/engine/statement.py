# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Phase G: form calculation of a statement year (spec 5.9, R-12, F-06).

The field totals of the cash basis calculation (cent) are turned into the
values of the form with the formula engine of :mod:`.form_calc`; this module
adds only what the formulas cannot express: the overflow of negative
revenue into Kz 183 (R-12), the sign check (C-71) and the scope check
(C-40).
"""

from dataclasses import dataclass, field
from decimal import Decimal

from . import form_calc
from . import model as m

FIELD_OTHER = "EUER.25.183"
FIELD_KU = "EUER.20.111"
FIELD_KU_DAVON = "EUER.20.119"
#: revenue fields of the sum Kz 159 whose negative total moves to Kz 183
REVENUE_FIELDS = (
    "EUER.20.111",
    "EUER.20.104",
    "EUER.20.112",
    "EUER.20.103",
    "EUER.20.140",
    "EUER.20.141",
    "EUER.20.102",
    "EUER.20.106",
    "EUER.20.108",
)


@dataclass(frozen=True, kw_only=True)
class FormSpec:
    """Formulas and field properties of the form year (from ``l10n_de_euer.field``).

    ``known``: all codes of the year. ``blocked``: codes with scope status
    "not supported" (C-40). ``mandatory``: codes kept even when zero.
    """

    formulas: dict = field(default_factory=dict)
    conditions: dict = field(default_factory=dict)
    allow_negative: frozenset = frozenset()
    known: frozenset = frozenset()
    blocked: frozenset = frozenset()
    mandatory: frozenset = frozenset()


def overflow(totals):
    """R-12: a negative revenue total moves to Kz 183 (other expenses).

    Returns the corrected totals and the moved amounts ``{code: cent}``.
    "Of which" Kz 119 is limited to the range from 0 to Kz 111.
    """
    result = dict(totals)
    moved = {}
    for code in REVENUE_FIELDS:
        value = result.get(code, 0)
        if value < 0:
            moved[code] = -value
            result[code] = 0
            result[FIELD_OTHER] = result.get(FIELD_OTHER, 0) - value
    if FIELD_KU_DAVON in result:
        result[FIELD_KU_DAVON] = max(
            0, min(result[FIELD_KU_DAVON], result.get(FIELD_KU, 0))
        )
    return result, moved


def to_euro(cents):
    return (Decimal(cents) / 100).quantize(form_calc.CENT)


def form_values(totals, spec, year=None, overrides=None):
    """Values of all fields of the form year and the findings C-71, C-40.

    :param totals: code → cent of the input fields (after :func:`overflow`)
    :return: ``(values, findings)``; ``values`` maps every code to a
        ``Decimal`` in euro or ``None`` (empty field, R-46)
    """
    amounts = {code: to_euro(cents) for code, cents in totals.items() if cents}
    values = form_calc.compute(
        spec.formulas,
        amounts,
        overrides=overrides,
        conditions=spec.conditions,
        known=spec.known or None,
    )
    findings = []
    years = (year,) if year else ()
    for code in form_calc.negative_codes(values, spec.allow_negative):
        findings.append(
            m.Finding(
                code="C-71",
                severity=m.BLOCKER,
                key="negative_value",
                years=years,
                amount=int(values[code] * 100),
                params={"field": code},
            )
        )
    for code in sorted(spec.blocked):
        if values.get(code):
            findings.append(
                m.Finding(
                    code="C-40",
                    severity=m.BLOCKER,
                    key="field_out_of_scope",
                    years=years,
                    amount=int(values[code] * 100),
                    params={"field": code},
                )
            )
    return values, findings


def display_values(values, spec):
    """Values for output: empty and zero fields left out (R-46, FT-07)."""
    return form_calc.drop_empty(values, keep=spec.mandatory)


def compare(previous, current):
    """Differences ``{code: (old, new)}`` between two value sets (C-60)."""
    result = {}
    for code in sorted(set(previous) | set(current)):
        old, new = previous.get(code) or 0, current.get(code) or 0
        if old != new:
            result[code] = (old, new)
    return result
