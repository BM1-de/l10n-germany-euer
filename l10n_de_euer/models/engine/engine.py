# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Cash basis engine: one run over all phases (spec 5.1, 5.11).

``run(data, year)`` computes over the whole reconciliation graph and filters
at the end by the year of attribution:

* phase A post-processing (:mod:`.graph`), cash dates (:mod:`.ten_day`),
* phases B and C (:mod:`.money`), direct entries (:mod:`.direct`),
* phase D (:mod:`.ten_day`), phase E (:mod:`.assignment`),
* phase F: amounts that do not come from money (asset register, profit
  corrections) – :meth:`Engine.asset_amounts` is the interface for the
  fixed asset register of stage E3,
* invariants I-1 to I-4. The form calculation (phase G) is
  :func:`.statement.form_values` on :attr:`Result.totals`.
"""

import datetime
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field

from . import model as m
from . import statement, vat
from .assignment import Assigner, mapping_lines
from .direct import DirectEntries
from .graph import Graph
from .money import MoneyFlow
from .parameters import Parameters
from .rounding import sign
from .ten_day import CashDate, CashDates, TenDayRule

FIELD_ENTERTAINMENT = "EUER.25.175"
FIELD_ENTERTAINMENT_NOT_DEDUCTIBLE = "EUER.25.165"
FIELD_PRIVATE_CAR = "EUER.20.106"
#: sub key of the invariant per open item in ``Result.invariants["documents"]``
PORT_CHECK = 9


@dataclass(kw_only=True)
class Result:
    """Result of a run for one statement year."""

    year: int
    slices: list = field(default_factory=list)
    contributions: list = field(default_factory=list)
    findings: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    ten_day_items: list = field(default_factory=list)
    turnover: object = None
    totals: dict = field(default_factory=dict)
    overflow: dict = field(default_factory=dict)
    invariants: dict = field(default_factory=dict)
    hash: str = ""
    asset_slices: list = field(default_factory=list)
    asset_entries: list = field(default_factory=list)
    classes: dict = field(default_factory=dict)
    roles: dict = field(default_factory=dict)

    def findings_for(self, code=None):
        return [f for f in self.findings if code is None or f.code == code]

    def blockers(self):
        return [f for f in self.findings if f.severity == m.BLOCKER]

    def totals_for(self, year, overflow=True):
        """Field totals (cent) of any year.

        The overflow of R-12 is computed for the statement year only;
        ``overflow=False`` leaves it out.
        """
        totals = defaultdict(int)
        for contribution in self.contributions:
            if contribution.year == year and (
                overflow or contribution.rule != "overflow"
            ):
                totals[contribution.field] += contribution.amount
        return {code: value for code, value in totals.items() if value}


class Engine:
    """All phases of the cash basis calculation for one business."""

    def __init__(self, data):
        self.data = data
        self.params = Parameters(data.parameters)
        self.option = f"entertainment_{data.settings.entertainment_mode}"

    def fields_of(self, category, day):
        lines = mapping_lines(self.data.mappings, category.code, day.year, self.option)
        return {line.field for line in lines}

    # ------------------------------------------------------------ phase F
    def asset_amounts(self, year):
        """Field amounts of the fixed asset register (stage E3, spec 5.8).

        Interface for the asset register: returns the
        :class:`~.model.FieldAmount` records of ``year``: the input fields
        of the AVEÜR (``AVEUER.40.*``; Kz 25.130, 25.131, 25.134–25.137 are
        their transfers by formula), Kz 25.132 (low-value assets), 20.102
        and 20.140 of withdrawals, 29.122/29.123, read by the adapter from the
        register (``data.asset_amounts``).
        """
        return [amount for amount in self.data.asset_amounts if amount.year == year]

    def adjustment_amounts(self, year):
        """Profit corrections of the statement (stage E4, spec 5.8)."""
        return [
            amount for amount in self.data.adjustment_amounts if amount.year == year
        ]

    def phase_f(self, year):
        return self.asset_amounts(year) + self.adjustment_amounts(year)

    # ------------------------------------------------------------ run
    def run(self, year):
        graph = Graph(self.data)
        cash = CashDates(graph, self.params)
        flow = MoneyFlow(graph, cash)
        flow.run_sources()
        flow.run_documents(fx_allocate=self.data.settings.fx_mode == "allocate")
        direct = DirectEntries(graph, flow, self.fields_of)
        direct.run()
        rule = TenDayRule(graph, self.params, flow.slices)
        rule.run()
        assigner = Assigner(graph, flow.slices, self.params, cash, flow.notes)
        assigner.run()
        result = Result(
            year=year,
            slices=flow.slices,
            contributions=assigner.contributions,
            notes=flow.notes,
            ten_day_items=cash.items + rule.items,
            asset_slices=assigner.asset_slices,
            asset_entries=direct.asset_entries,
            classes=dict(graph.cls),
            roles=dict(graph.role),
        )
        for amount in self.phase_f(year):
            result.contributions.append(
                m.Contribution(
                    field=amount.field,
                    amount=amount.amount,
                    year=amount.year,
                    rule=amount.source,
                    reference=amount.reference,
                )
            )
        findings = (
            graph.findings
            + cash.findings
            + flow.findings
            + direct.findings
            + rule.findings
            + assigner.findings
        )
        complete, books_start = self._books_before(
            graph, year, self.data.activity_start
        )
        findings += self._turnover(
            result,
            assigner.turnover,
            year,
            assigner.free_supplies_left_out,
            previous_data=complete,
            books_start=books_start,
        )
        findings += self._opening_checks(graph, cash)
        findings += self._invariants(result, graph, flow, cash)
        self._totals(result, year)
        findings += self._year_checks(result, year)
        result.findings = [f for f in findings if f.applies_to(year)]
        result.hash = slice_hash(result.slices)
        return result

    # ------------------------------------------------------------ totals
    def _totals(self, result, year):
        totals = result.totals_for(year)
        corrected, moved = statement.overflow(totals)
        for code in sorted(corrected):
            difference = corrected[code] - totals.get(code, 0)
            if difference:
                result.contributions.append(
                    m.Contribution(
                        field=code, amount=difference, year=year, rule="overflow"
                    )
                )
        result.overflow = moved
        result.totals = {code: value for code, value in corrected.items() if value}

    @staticmethod
    def _books_before(graph, year, activity_start=None):
        """Whether the books hold the money of the year before ``year``:
        ``(complete, start)``; ``start`` is the date of the opening entry if
        the books start during that year (takeover during the year), else
        ``None``.

        The total turnover counts money received, so only money entries
        (entries with money lines) of a year before ``year`` make the books
        the source of the previous year; documents alone, e.g. open items of
        a takeover imported with their dates, show no money received, and
        opening entries carry the balances of the previous system, not its
        turnover. An opening entry before the first of these money entries
        marks the start of the books: if it lies after 1 January of the
        previous year (or after the start of the activity, if that is
        later), the books miss the first part of the previous year
        (takeover during the year).
        """
        money = [
            move.date
            for move in graph.moves.values()
            if move.date.year < year and graph.cls.get(move.id) == m.SOURCE
        ]
        if not money:
            return False, None
        needed = datetime.date(year - 1, 1, 1)
        if activity_start and activity_start > needed:
            needed = activity_start
        starts = [
            move.date
            for move in graph.moves.values()
            if graph.cls.get(move.id) == m.OPENING and move.date <= min(money)
        ]
        if not starts or min(starts) <= needed:
            return True, None
        # without an opening entry the books count as complete (ROADMAP)
        return False, min(starts)

    def _turnover(
        self, result, entries, year, left_out=(), previous_data=True, books_start=None
    ):
        """Total turnover and the small business limit (R-27, C-32).

        Phase F adds only what the adapter passes as
        :class:`~.model.TurnoverAmount`: taxable free supplies at their VAT
        base and income in kind, of the year and the previous year. The
        values of Kz 106/108 for income tax never count by themselves: a
        free supply of a good without input tax deduction is not taxable
        and no part of the total turnover (UStAE 19.2 (2) sentences 2, 4).
        Booked ones count only with output VAT; ``left_out``: those booked
        in small business periods, listed in a hint of the limit check.
        ``previous_data``: the books hold entries of an earlier year (the
        turnover of the previous year is known, see :func:`.vat.check_limit`);
        ``books_start``: the books start during the previous year.
        """
        entries = list(entries)
        for amount in self.data.turnover_amounts:
            if amount.amount:
                entries.append((amount.day(), None, amount.amount))
        # a missing parameter (years before the limits of the module) is no
        # limit of 0: the check is left out
        limits = {
            code: self.params.cents(code, year)
            for code in ("ku_limit_prev", "ku_limit_cur", "ku_limit_founding")
        }
        result.turnover, findings = vat.check_limit(
            entries,
            year,
            limits,
            self.data.vat_periods,
            self.data.founding_year,
            previous_data=previous_data,
            books_start=books_start,
            activity_start=self.data.activity_start,
        )
        if result.turnover.limit is not None:
            findings += vat.free_supplies_hint(left_out, year)
        return findings

    def _year_checks(self, result, year):
        findings = []
        totals = result.totals
        deductible = totals.get(FIELD_ENTERTAINMENT, 0)
        not_deductible = totals.get(FIELD_ENTERTAINMENT_NOT_DEDUCTIBLE, 0)
        both = deductible + not_deductible
        if both > 0 and abs(not_deductible * 100 - 30 * both) > 2 * both:
            findings.append(
                m.Finding(
                    code="C-53",
                    severity=m.WARNING,
                    key="entertainment_ratio",
                    years=(year,),
                    amount=not_deductible,
                    params={"deductible": deductible, "total": both},
                )
            )
        booked = any(
            c.field == FIELD_PRIVATE_CAR
            and c.year == year
            and c.slice_index is not None
            for c in result.contributions
        )
        corrected = any(
            a.field == FIELD_PRIVATE_CAR for a in self.adjustment_amounts(year)
        )
        if booked and corrected:
            findings.append(
                m.Finding(
                    code="C-54",
                    severity=m.BLOCKER,
                    key="private_car_twice",
                    years=(year,),
                )
            )
        return findings

    @staticmethod
    def _opening_checks(graph, cash):
        """C-46: opening entry after money moved on the same account."""
        findings = []
        openings = graph.moves_of_class(m.OPENING)
        per_year = defaultdict(list)
        for opening in openings:
            accounts = {line.account_id for line in graph.lines_of(opening.id, m.L)}
            if accounts:
                per_year[opening.date.year].append(opening.id)
            earlier = [
                source.id
                for source in graph.moves_of_class(m.SOURCE)
                if source.date < opening.date
                and any(
                    line.account_id in accounts
                    for line in graph.lines_of(source.id, m.L)
                )
            ]
            if earlier:
                findings.append(
                    m.Finding(
                        code="C-46",
                        severity=m.WARNING,
                        key="opening_after_money",
                        years=(opening.date.year,),
                        move_ids=(opening.id, *earlier[:5]),
                    )
                )
        for year, move_ids in sorted(per_year.items()):
            if len(move_ids) > 1:
                findings.append(
                    m.Finding(
                        code="C-46",
                        severity=m.WARNING,
                        key="several_openings",
                        years=(year,),
                        move_ids=tuple(move_ids),
                    )
                )
        return findings

    # ------------------------------------------------------------ invariants
    def _invariants(self, result, graph, flow, cash=None):
        """I-1 to I-3 of spec 5.11; a violation is blocker C-11."""
        money = check_money(graph, flow.slices, flow.notes, cash)
        documents = check_documents(flow)
        documents.update(check_ports(graph, flow))
        transit = check_transits(graph, flow)
        result.invariants = {
            "money": money,
            "documents": documents,
            "transit": transit,
        }
        findings = []
        for (year, source_id), diff in sorted(money.items()):
            findings.append(
                m.Finding(
                    code="C-11",
                    severity=m.BLOCKER,
                    key="money_reconciliation",
                    years=(year,),
                    move_ids=(source_id,),
                    amount=diff,
                )
            )
        for key, diff in sorted(documents.items()):
            findings.append(
                m.Finding(
                    code="C-11",
                    severity=m.BLOCKER,
                    key="document_completeness",
                    line_ids=(key[0],),
                    params={"difference": str(diff)},
                )
            )
        for move_id in sorted(transit):
            findings.append(
                m.Finding(
                    code="C-11",
                    severity=m.BLOCKER,
                    key="transit_over_realised",
                    move_ids=(move_id,),
                )
            )
        return findings


def check_money(graph, slices, notes, cash=None):
    """I-1 per source and year: money lines + realised + rests + money in
    transit = 0 (spec 5.11).

    The money of a source belongs to the year of its cash date; a payment
    booked on the bank account and split over statement lines of several
    years (C-24) to the years of its parts. Every slice from money must
    carry the cash date of its source, or the cash date entered on the last
    payment entry its money passed (5.6): a slice in the wrong year is a
    violation even if the sums agree. ``cash``: the
    :class:`~.ten_day.CashDates` of the run (``None``: the entry dates).
    Returns ``{(year, source id): difference}`` for violations (cent).
    """
    totals = defaultdict(int)
    wrong = defaultdict(int)
    tranche_dates = {}
    for source in graph.moves_of_class(m.SOURCE):
        money = sum(line.balance for line in graph.lines_of(source.id, m.L))
        if cash is None:
            tranches = [(CashDate(source.date), None)]
        else:
            tranches = cash.tranches_of(source.id)
        tranche_dates[source.id] = {one.date for one, _weight in tranches}
        if len(tranches) == 1:
            totals[(tranches[0][0].date.year, source.id)] += money
            continue
        for one, weight in tranches:
            totals[(one.date.year, source.id)] += sign(money) * weight
    for one in slices:
        if one.kind not in m.MONEY_KINDS or one.source_move_id not in tranche_dates:
            continue
        money_date = one.money_date or one.date
        totals[(money_date.year, one.source_move_id)] += one.amount
        if money_date not in tranche_dates[
            one.source_move_id
        ] or one.date != _expected_date(graph, one, money_date):
            wrong[(one.date.year, one.source_move_id)] += abs(one.amount)
    for note in notes:
        money_date = note.money_date or note.date
        totals[(money_date.year, note.source_move_id)] += note.amount
    result = {key: value for key, value in totals.items() if value}
    for key, value in wrong.items():
        result[key] = result.get(key, 0) + value
    return result


def _expected_date(graph, one, money_date):
    """Date of a slice: the cash date of its money, replaced by the cash
    date entered on a payment entry the money passed (the last one)."""
    day = money_date
    current = one.source_move_id
    for partial_id in one.path:
        partial = graph.partials.get(partial_id)
        if partial is None:
            continue
        moves = [
            graph.lines[line_id].move_id
            for line_id in (partial.debit_line_id, partial.credit_line_id)
            if line_id in graph.lines
        ]
        following = next((move_id for move_id in moves if move_id != current), None)
        if following is None:
            continue
        current = following
        move = graph.moves[following]
        if graph.cls.get(following) == m.TRANSIT and move.info.cash_date:
            day = move.info.cash_date
    return day


def check_documents(flow):
    """I-2: realised + open + neutralised = initial amount per document line."""
    open_amounts = defaultdict(int)
    for composition in flow.ports.values():
        for key, value in composition.items():
            open_amounts[key] += value
    result = {}
    for key, initial in flow.initial.items():
        diff = flow.realized[key] + open_amounts[key] + flow.neutralized[key] - initial
        if diff:
            result[key] = diff
    return result


def check_ports(graph, flow):
    """I-2 per open item: ``Σ K(q) = −sign(q) · req(q)``; an item whose
    partials have all been processed (money arrived, credit note applied)
    has nothing open. Returns ``{(port id, PORT_CHECK): difference}``."""
    result = {}
    for port_id, composition in flow.ports.items():
        line = graph.lines[port_id]
        open_amount = flow.req[port_id]
        diff = sum(composition.values()) + sign(line.balance) * open_amount
        partials = [partial for partial, _other in graph.edges.get(port_id, ())]
        settled = sum(partial.amount for partial in partials) == abs(line.balance)
        if (
            settled
            and open_amount
            and all(
                flow.processed[(port_id, partial.id)] >= partial.amount
                for partial in partials
            )
        ):
            diff = diff or open_amount
        if diff:
            result[(port_id, PORT_CHECK)] = diff
    return result


def check_transits(graph, flow):
    """I-3 for transit entries: φ ≤ 1, no money flowing back, and once the
    money of a side has fully arrived every output passed exactly its
    partial and (φ = 1) every line exactly its balance.

    Returns ``{move id: φ}`` for violations.
    """
    result = {}
    for move in graph.moves_of_class(m.TRANSIT):
        phi = flow.phi[move.id]
        wrong = phi > 1 or move.id in flow.transit_violations
        if phi == 1:
            wrong = wrong or any(
                flow.transit_done[line.id] != line.balance
                for line in graph.lines_of(move.id, m.N)
            )
        for side in (1, -1):
            if flow.reached[(move.id, side)] != 1:
                continue
            for line_q in graph.lines_of(move.id, m.R):
                if sign(line_q.balance) != -side:
                    continue
                for partial, _other in graph.edges.get(line_q.id, ()):
                    wrong = wrong or (
                        flow.forwarded[(partial.id, line_q.id)] != partial.amount
                    )
        if wrong:
            result[move.id] = phi
    return result


def slice_hash(slices):
    """I-4: SHA-256 over the sorted slices (determinism, UT-DET-1)."""
    rows = sorted(
        (
            one.date.isoformat(),
            one.kind,
            one.source_move_id or 0,
            one.origin_move_id or 0,
            list(one.key or (0, 0)),
            one.amount,
            one.attribution_year or 0,
            one.category or "",
            list(one.path),
            one.virtual_tax or "",
        )
        for one in slices
    )
    payload = json.dumps(rows, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def run(data, year):
    """Run the engine for the statement ``year``; returns :class:`Result`."""
    return Engine(data).run(year)
