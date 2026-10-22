# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Mandatory questionnaire of a statement (R-52, spec 1.2, 7.3).

Many cases outside the program scope leave no trace in the bookings
(investment deductions, closure of the business, reserves, change of the
profit method, partnerships, crypto assets). The user answers yes/no
questions that follow the header and the supplementary questions of the
form. An answer counts only when a manager confirmed it; proposals of the
previous year are no confirmation (C-45).

The field codes of a question are stable; their line and Kz per form year
come from the form data.
"""

from dataclasses import dataclass

from . import adjustment
from .model import BLOCKER, WARNING

YES = "yes"
NO = "no"


@dataclass(frozen=True)
class Question:
    """A question of the questionnaire.

    ``fields``: codes of the form fields it stands for. ``blocking``: the
    answer that means a case outside the program scope (C-40), ``None`` if
    every answer is supported.
    """

    code: str
    fields: tuple
    blocking: str | None


QUESTIONS = (
    Question("Q01_LEGAL_FORM", ("EUER.15.RECHTSFORM",), NO),
    Question("Q02_FISCAL_YEAR", ("EUER.15.131", "EUER.15.132"), YES),
    Question("Q03_CLOSURE", ("EUER.15.111",), YES),
    Question("Q04_LAND", ("EUER.15.120",), None),
    Question("Q05_LUF", ("EUER.15.103",), YES),
    Question("Q06_IAB", ("EUER.G.180", "EUER.G.181", "EUER.G.182", "EUER.G.187"), YES),
    Question(
        "Q07_RESERVES",
        (
            "EUER.G.123",
            "EUER.27.187",
            "EUER.27.170",
            "EUER.27.191",
            "EUER.27.120",
            "EUER.27.125",
        ),
        YES,
    ),
    Question("Q08_METHOD_CHANGE", ("EUER.G.250",), YES),
    Question("Q09_PARTNERSHIP", ("EUER.G.255",), YES),
    Question(
        "Q10_INVSTG_TEV", ("EUER.G.261", "EUER.G.262", "EUER.G.263", "EUER.G.264"), YES
    ),
    Question("Q11_CRYPTO", (), YES),
    Question("Q12_TAX_FREE", adjustment.TAX_FREE_FIELDS, None),
    # § 11 (2) sentence 3 EStG: advance payments for a use of more than five
    # years are spread over the period (income by choice, § 11 (1) sentence
    # 3); not supported in version 1
    Question("Q13_PREPAYMENT", (), YES),
)
QUESTION_CODES = tuple(question.code for question in QUESTIONS)
QUESTIONS_BY_CODE = {question.code: question for question in QUESTIONS}
LEGAL_FORM = "Q01_LEGAL_FORM"
LAND = "Q04_LAND"
TAX_FREE = "Q12_TAX_FREE"
FIELD_LAND = "EUER.15.120"
#: value of Kz 15.120: 1 = yes, 2 = no
LAND_VALUES = {YES: "1", NO: "2"}


@dataclass(frozen=True, kw_only=True)
class Answer:
    """Answer of a question: ``answer`` is ``yes``, ``no`` or ``None``."""

    question: str
    answer: str | None = None
    confirmed: bool = False


@dataclass(frozen=True, kw_only=True)
class ScopeFinding:
    code: str
    severity: str
    key: str
    questions: tuple = ()


def evaluate(
    answers, tax_free_correction=False, land_disposal=False, joint_owners=False
):
    """Findings of the questionnaire (C-40, C-43, C-44, C-45) and its cross
    checks with the data.

    :param answers: :class:`Answer` records of the statement
    :param tax_free_correction: the statement has a profit correction of
        G.240–G.245
    :param land_disposal: the asset register has a sale or withdrawal of
        land or a building in the year
    :param joint_owners: the business belongs to both spouses (Kz 104 = 3)
    """
    by_code = {answer.question: answer for answer in answers}
    findings = []
    open_questions = tuple(
        question.code
        for question in QUESTIONS
        if not by_code.get(question.code)
        or not by_code[question.code].answer
        or not by_code[question.code].confirmed
    )
    if open_questions:
        findings.append(
            ScopeFinding(
                code="C-45",
                severity=BLOCKER,
                key="not_confirmed",
                questions=open_questions,
            )
        )
    for question in QUESTIONS:
        answer = by_code.get(question.code)
        if question.blocking and answer and answer.answer == question.blocking:
            findings.append(
                ScopeFinding(
                    code="C-40",
                    severity=BLOCKER,
                    key="out_of_scope",
                    questions=(question.code,),
                )
            )
    land = by_code.get(LAND)
    if land and land.answer == YES and not land_disposal:
        findings.append(
            ScopeFinding(
                code="C-43",
                severity=WARNING,
                key="land_without_disposal",
                questions=(LAND,),
            )
        )
    if land and land.answer == NO and land_disposal:
        # Kz 15.120 is mandatory (Anleitung 2026 line 11)
        findings.append(
            ScopeFinding(
                code="C-43",
                severity=WARNING,
                key="disposal_without_land",
                questions=(LAND,),
            )
        )
    tax_free = by_code.get(TAX_FREE)
    if tax_free and tax_free.answer == YES and not tax_free_correction:
        findings.append(
            ScopeFinding(
                code="C-44",
                severity=BLOCKER,
                key="tax_free_without_correction",
                questions=(TAX_FREE,),
            )
        )
    if tax_free and tax_free.answer == NO and tax_free_correction:
        findings.append(
            ScopeFinding(
                code="C-44",
                severity=WARNING,
                key="correction_without_tax_free",
                questions=(TAX_FREE,),
            )
        )
    legal_form = by_code.get(LEGAL_FORM)
    if legal_form and legal_form.answer == YES and joint_owners:
        # Anleitung 2026 line 9: a business of both spouses together is
        # usually a partnership, outside the program scope
        findings.append(
            ScopeFinding(
                code="C-40",
                severity=WARNING,
                key="joint_owners",
                questions=(LEGAL_FORM,),
            )
        )
    return findings


def land_value(answer):
    """Value of Kz 15.120 for the answer of question Q04 (F-08)."""
    return LAND_VALUES.get(answer) or ""
