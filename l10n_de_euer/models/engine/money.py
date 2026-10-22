# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Phases B and C: money flow and realisation (spec 5.4, 5.5).

Phase B leads the money of every source through transit entries to the
documents; phase C realises the documents chronologically. Every split of
an amount is rounded to whole cent by the largest remainder method over all
its parts at once (B-06), so the money reconciliation (I-1) holds to the
cent.

Composition of an open item ("port") ``q`` of a document: ``K(q)`` holds
the not yet realised amounts of all non-reconcilable lines, ``req(q)`` the
open amount. A payment (E1) realises ``K(q) · a / req(q)``; a partial
between two documents without money (E2: credit note, write-off, exchange
difference) changes the composition of what is still open.
"""

import dataclasses
import itertools
import math
from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction

from . import model as m
from . import vat
from .rounding import apportion, apportion_bounded, gross_split, sign

#: sub keys of virtual lines (opening items, classifications)
NET = 1
TAX = 2
#: roles of accounts whose opening items need no takeover (B-08, C-35)
_SELF_ROLES = frozenset({"neutral", "steuer", "ungeklaert"})


@dataclass
class _Payment:
    """Event E1: money reaches a port.

    ``cash``: :class:`~.ten_day.CashDate` of the money at the port.
    ``writeoff``: part of ``amount`` that is no money but a write-off booked
    on the money or payment entry (payment difference, 5.5): it reduces the
    composition of the document before the money realises the rest.
    """

    cash: object
    port: object
    amount: int
    source_id: int
    path: tuple
    partial_id: int
    seq: int
    writeoff: int = 0

    @property
    def date(self):
        return self.cash.date

    @property
    def override(self):
        return self.cash.override


@dataclass
class _Virtual:
    """Line of an opening item that has no journal item of its own."""

    category: str | None
    virtual_tax: str | None
    regime: str | None
    tax_rate: Fraction | None
    move_id: int
    account_id: int
    partner_id: int | None


class MoneyFlow:
    """Phases B and C over a :class:`~.graph.Graph`."""

    def __init__(self, graph, cash_dates):
        self.graph = graph
        self.cash_dates = cash_dates
        self.data = graph.data
        self.slices = []
        self.notes = []
        self.findings = []
        self._events = itertools.count(1)
        self._seq = itertools.count(1)
        self.payments = []
        self.phi = defaultdict(Fraction)
        self.reached = defaultdict(Fraction)
        self.transit_done = defaultdict(int)
        self._transit_exact = defaultdict(Fraction)
        self._transit_leftover = defaultdict(int)
        self.transit_violations = set()
        self.forwarded = defaultdict(int)
        self._rest_used = defaultdict(int)
        self.classifications = {c.line_id: c for c in self.data.classifications}
        self.used_classifications = set()
        self.ports = {}
        self.port_initial = {}
        self._fx_documents = {}
        self.req = {}
        self.initial = defaultdict(Fraction)
        self.realized = defaultdict(int)
        self.neutralized = defaultdict(Fraction)
        #: amount of a partial processed at a port: (port id, partial id)
        self.processed = defaultdict(int)
        self.virtual = {}
        #: years in which money reached a transit entry (C-27)
        self._transit_years = defaultdict(set)

    def new_event(self):
        return next(self._events)

    def money_line(self, source_id):
        """First money line of a source (``None`` for a set-off)."""
        lines = self.graph.lines_of(source_id, m.L)
        return lines[0].id if lines else None

    # ================================================================ phase B
    def run_sources(self):
        sources = self.graph.moves_of_class(m.SOURCE)
        sources.sort(key=lambda move: (self.cash_dates.get(move.id).date, move.id))
        for source in sources:
            lines_n = self.graph.lines_of(source.id, m.N)
            lines_r = self.graph.lines_of(source.id, m.R)
            for cash, parts in self._source_tranches(source, lines_n, lines_r):
                self._source_money(source, cash, parts, lines_n, lines_r)
        self._check_transits()

    def _source_tranches(self, source, lines_n, lines_r):
        """Money of a source per cash date: ``[(cash, parts)]``.

        ``parts``: key → cent with the sign of the balance: ``("n", line)``
        per non-reconcilable line, ``("p", partial, line)`` per partial of an
        open item and ``("r", line)`` per unmatched rest of an open item. A
        payment booked on the bank account and matched with statement lines
        of several dates (C-24) is split by the matched amounts: each part is
        divided in whole cent (cumulative shares, largest remainders), so
        every cash date receives exactly its money and every part its total.
        """
        parts = {}
        for line in lines_n:
            parts[("n", line.id)] = line.balance
        for line in lines_r:
            matched = 0
            for partial, _other in self.graph.edges.get(line.id, ()):
                parts[("p", partial.id, line.id)] = sign(line.balance) * partial.amount
                matched += partial.amount
            rest = abs(line.balance) - matched
            if rest > 0:
                parts[("r", line.id)] = sign(line.balance) * rest
        tranches = self.cash_dates.tranches_of(source.id)
        if len(tranches) == 1:
            return [(tranches[0][0], parts)]
        total = sum(weight for _cash, weight in tranches)
        direction = sign(sum(parts.values()))
        result, before, cumulative_weight = [], dict.fromkeys(parts, 0), 0
        for cash, weight in tranches:
            cumulative_weight += weight
            exact = {
                key: Fraction(value * cumulative_weight, total)
                for key, value in parts.items()
            }
            bounds = {
                key: (min(before[key], value), max(before[key], value))
                for key, value in parts.items()
            }
            try:
                cumulative = apportion_bounded(
                    exact, direction * cumulative_weight, bounds
                )
            except ValueError:
                cumulative = apportion(exact, direction * cumulative_weight)
            result.append((cash, {key: cumulative[key] - before[key] for key in parts}))
            before = cumulative
        return result

    def _source_money(self, source, cash, parts, lines_n, lines_r):
        """Money of a source at one cash date (5.4 nos. 1 to 3)."""
        event = self.new_event()
        edges = [
            (line, partial, other)
            for line in lines_r
            for partial, other in self.graph.edges.get(line.id, ())
        ]
        netted, shares = self._net_writeoffs(
            lines_n,
            {line.id: parts[("n", line.id)] for line in lines_n},
            [
                (partial.id, line, partial, abs(parts[("p", partial.id, line.id)]))
                for line, partial, _other in edges
            ],
            cash.date,
            frozenset({source.id}),
        )
        for line in lines_n:
            if line.id in netted:
                continue
            self._line_slice(
                line,
                parts[("n", line.id)],
                cash,
                event,
                kind=m.CASH_DIRECT,
                source=source,
            )
        for line, partial, other in edges:
            cents = abs(parts[("p", partial.id, line.id)])
            self.forwarded[(partial.id, line.id)] += cents
            self._forward(
                other,
                cents,
                cash,
                frozenset({source.id}),
                (partial.id,),
                source,
                line,
                shares.get(partial.id, 0),
            )
        for line in lines_r:
            rest = parts.get(("r", line.id))
            if rest:
                self._rest(line, rest, cash, source, source, ())

    def _is_writeoff(self, line, day):
        category = self.graph.category(line, day)
        return category is not None and category.writeoff

    def _writeoff_capacity(self, other, cents, visited):
        """Cent of the money ``cents`` passed to ``other`` that surely reach
        documents (DOC, opening item), directly or through payment entries:
        a write-off booked with the money can reduce them (5.5). One cent
        per output of a payment entry is kept back for its rounding."""
        move = self.graph.move_of(other)
        if move.id in visited or cents <= 0:
            return 0
        cls = self.graph.cls[move.id]
        if cls in (m.DOC, m.OPENING):
            return cents
        if cls != m.TRANSIT:
            return 0
        side = sign(other.balance)
        open_items = self.graph.lines_of(move.id, m.R)
        total_side = sum(abs(x.balance) for x in open_items if sign(x.balance) == side)
        capacity = 0
        for line_q in open_items:
            if sign(line_q.balance) != -side:
                continue
            for partial, target in self.graph.edges.get(line_q.id, ()):
                share = math.floor(Fraction(cents * partial.amount, total_side)) - 1
                capacity += self._writeoff_capacity(target, share, visited | {move.id})
        return capacity

    def _net_writeoffs(self, lines_n, cents, outputs, day, visited):
        """Write-off lines on a money or payment entry (5.5, 5.7 no. 11).

        A payment difference written off in the bank reconciliation or the
        payment wizard ("2400 an 1410" next to the money) is no money. It
        reduces the documents the money reaches, like a neutralised write-off
        just before the payment: their composition shrinks in proportion and
        the money realises the rest. The money may reach the documents
        through payment entries (statement matched with a registered
        payment, payment order). ``cents``: amount of each N line in this
        money; ``outputs``: ``(key, line, partial, cents)`` of the money
        passed on. Returns the ids of the netted write-off lines and the
        write-off share per output key (largest remainders by the amount of
        the outputs); nothing is netted if the money does not reach
        documents or the write-off does not reduce them.
        """
        writeoff_lines = [
            line
            for line in lines_n
            if cents.get(line.id) and self._is_writeoff(line, day)
        ]
        total = sum(cents[line.id] for line in writeoff_lines)
        if not total:
            return set(), {}
        caps, weights = {}, {}
        for key, line, partial, value in outputs:
            other_id = (
                partial.credit_line_id
                if partial.debit_line_id == line.id
                else partial.debit_line_id
            )
            other = self.graph.lines.get(other_id)
            if not value or other is None or sign(line.balance) != -sign(total):
                continue
            cap = self._writeoff_capacity(other, abs(value), visited)
            if cap > 0:
                caps[key] = cap
                weights[key] = abs(value)
        if not caps or abs(total) > sum(caps.values()):
            return set(), {}
        weight = sum(weights.values())
        raw = {
            key: Fraction(abs(total) * value, weight) for key, value in weights.items()
        }
        shares = apportion(raw, abs(total), caps=caps)
        self.findings.append(
            m.Finding(
                code="C-25",
                severity=m.INFO,
                key="writeoff_netted",
                years=(day.year,),
                move_ids=tuple(sorted({line.move_id for line in writeoff_lines})),
                line_ids=tuple(line.id for line in writeoff_lines),
                amount=abs(total),
            )
        )
        return {line.id for line in writeoff_lines}, shares

    def _forward(
        self, line, amount, cash, visited, path, source, from_line, writeoff=0
    ):
        """WEITER: money ``amount`` reaches ``line`` (spec 5.4)."""
        if not amount:
            return
        move = self.graph.move_of(line)
        if move.id in visited:
            self._note("cycle", cash, -sign(line.balance) * amount, source, line)
            self.findings.append(
                m.Finding(
                    code="C-21",
                    severity=m.BLOCKER,
                    key="cycle",
                    years=(cash.date.year,),
                    move_ids=tuple(sorted(visited | {move.id})),
                    amount=amount,
                    params={"path": path},
                )
            )
            return
        cls = self.graph.cls[move.id]
        if cls == m.SOURCE:
            self._money_to_money(line, amount, cash, source, from_line, path)
        elif cls in (m.DOC, m.OPENING):
            self.payments.append(
                _Payment(
                    cash,
                    line,
                    amount,
                    source.id,
                    path,
                    path[-1],
                    next(self._seq),
                    writeoff,
                )
            )
        elif cls == m.TRANSIT:
            self._transit(line, amount, cash, visited, path, source, writeoff)

    def _money_to_money(self, line, amount, cash, source, from_line, path):
        """Money reaches money (transfer, refund): nothing to realise.

        Over a receivable or payable between two years this is typically a
        returned direct debit with a new payment: C-37 until classified.
        """
        value = -sign(line.balance) * amount
        other = self.graph.move_of(line)
        other_date = self.cash_dates.get(other.id).date
        category = self.graph.category(line, cash.date)
        if (
            category is not None
            and category.role == "op"
            and other_date.year != cash.date.year
        ):
            classification = self.classifications.get(from_line.id)
            if classification is not None:
                self._classified(from_line, value, cash, source, source, path)
                return
            self.findings.append(
                m.Finding(
                    code="C-37",
                    severity=m.BLOCKER,
                    key="money_to_money_years",
                    years=(cash.date.year,),
                    move_ids=(source.id, other.id),
                    line_ids=(from_line.id,),
                    amount=amount,
                )
            )
        self._note("money_to_money", cash, value, source, line)

    def _transit(self, line, amount, cash, visited, path, source, writeoff=0):
        """Money passes a transit entry (payment, keep reconciliation).

        The parts are rounded cumulatively: each key receives the difference
        between its exact cumulative share after this money and the cents it
        already received. So many partial amounts do not drift: when the
        money of a side has fully arrived, every output has passed exactly
        its partial and every line exactly its balance. ``writeoff``: part
        of ``amount`` written off with the money (5.5); it goes on to the
        outputs that reach documents.
        """
        move = self.graph.move_of(line)
        if move.info.cash_date:
            # cash date entered on the payment entry (cheque registered as
            # payment): the money passing it takes this date (5.6)
            cash = dataclasses.replace(
                cash,
                date=move.info.cash_date,
                override=True,
                origin=cash.money_date,
            )
        self._transit_years[move.id].add(cash.date.year)
        side = sign(line.balance)
        open_items = self.graph.lines_of(move.id, m.R)
        total_side = sum(abs(x.balance) for x in open_items if sign(x.balance) == side)
        factor = Fraction(amount, total_side)
        self.reached[(move.id, side)] += factor
        delta = min(factor, 1 - self.phi[move.id])
        raw, bounds, targets, received = self._transit_targets(
            move, side, factor, delta
        )
        if delta < factor:
            self.findings.append(
                m.Finding(
                    code="C-22",
                    severity=m.INFO,
                    key="transit_both_sides",
                    years=(cash.date.year,),
                    move_ids=(move.id,),
                )
            )
        deficits = {}
        for key, value in raw.items():
            exact = self._transit_exact[(move.id, key)] + value
            self._transit_exact[(move.id, key)] = exact
            deficits[key] = exact - received[key]
        try:
            allocation = apportion_bounded(deficits, -side * amount, bounds)
        except ValueError:
            allocation = apportion(deficits, -side * amount)
            self.transit_violations.add(move.id)
        self.phi[move.id] += delta
        event = self.new_event()
        lines_n = self.graph.lines_of(move.id, m.N)
        visited = visited | {move.id}
        netted, shares = self._net_writeoffs(
            lines_n,
            {line_n.id: allocation[(0, line_n.id)] for line_n in lines_n},
            [
                (key, targets[key][0], targets[key][1], allocation[key])
                for key in sorted(targets)
                if targets[key][1] is not None
            ],
            cash.date,
            visited,
        )
        for line_n in lines_n:
            cents = allocation[(0, line_n.id)]
            self.transit_done[line_n.id] += cents
            if line_n.id in netted:
                continue
            self._line_slice(
                line_n,
                cents,
                cash,
                event,
                kind=m.KIND_TRANSIT,
                source=source,
                path=path,
            )
        passed = self._pass_writeoff(
            writeoff, targets, allocation, side, shares, visited, cash, source, move
        )
        for key in sorted(targets):
            self._transit_output(
                targets[key],
                allocation[key],
                side,
                cash,
                visited,
                path,
                source,
                shares.get(key, 0) + passed.get(key, 0),
            )
        leftover = allocation.get((3, 0), 0)
        if leftover:
            self._transit_leftover[move.id] += leftover
            self._note("transit_capped", cash, leftover, source, None, move.id)

    def _pass_writeoff(
        self, writeoff, targets, allocation, side, shares, visited, cash, source, move
    ):
        """Share of a write-off that came with the money per output (5.5).

        The write-off goes on to the outputs that reach documents, by the
        amount of the outputs (largest remainders), at most what each of them
        can still take. A rest that no document can take (cannot happen with
        consistent reconciliations) stays as note and blocks the year.
        """
        if not writeoff:
            return {}
        caps, weights = {}, {}
        for key in sorted(targets):
            _line_q, partial, other = targets[key]
            cents = allocation[key]
            if partial is None or not cents or sign(cents) != -side:
                continue
            room = abs(cents) - shares.get(key, 0)
            cap = min(room, self._writeoff_capacity(other, abs(cents), visited))
            if cap > 0:
                caps[key] = cap
                weights[key] = abs(cents)
        placed = min(writeoff, sum(caps.values()))
        passed = {}
        if placed:
            weight = sum(weights.values())
            raw = {
                key: Fraction(placed * value, weight) for key, value in weights.items()
            }
            passed = apportion(raw, placed, caps=caps)
        if writeoff > placed:
            self._note(
                "writeoff_unplaced",
                cash,
                side * (writeoff - placed),
                source,
                None,
                move.id,
            )
            self.findings.append(
                m.Finding(
                    code="C-36",
                    severity=m.BLOCKER,
                    key="writeoff_unplaced",
                    years=(cash.date.year,),
                    move_ids=(source.id, move.id),
                    amount=writeoff - placed,
                )
            )
        return passed

    def _transit_targets(self, move, side, factor, delta):
        """Exact parts of this money, bounds and cents received so far."""
        raw, bounds, targets, received = {}, {}, {}, {}
        lines_n = self.graph.lines_of(move.id, m.N)
        for line_n in lines_n:
            key = (0, line_n.id)
            raw[key] = delta * line_n.balance
            done = self.transit_done[line_n.id]
            received[key] = done
            bounds[key] = (min(0, line_n.balance) - done, max(0, line_n.balance) - done)
        for line_q in self.graph.lines_of(move.id, m.R):
            if sign(line_q.balance) != -side:
                continue
            matched = 0
            for partial, other in self.graph.edges.get(line_q.id, ()):
                key = (1, partial.id, line_q.id)
                raw[key] = -side * factor * partial.amount
                done = self.forwarded[key[1:]]
                received[key] = -side * done
                bounds[key] = (0, -side * max(partial.amount - done, 0))
                targets[key] = (line_q, partial, other)
                matched += partial.amount
            rest = abs(line_q.balance) - matched
            if rest > 0:
                key = (2, line_q.id)
                raw[key] = -side * factor * rest
                done = self._rest_used[line_q.id]
                received[key] = -side * done
                bounds[key] = (0, -side * max(rest - done, 0))
                targets[key] = (line_q, None, None)
        if delta < factor:
            raw[(3, 0)] = (factor - delta) * sum(x.balance for x in lines_n)
        received[(3, 0)] = self._transit_leftover[move.id]
        return raw, bounds, targets, received

    def _transit_output(
        self, target, cents, side, cash, visited, path, source, writeoff=0
    ):
        line_q, partial, other = target
        if cents and sign(cents) != -side:
            # money would flow back through the transit entry (I-3)
            self.transit_violations.add(line_q.move_id)
            return
        if partial is not None:
            self.forwarded[(partial.id, line_q.id)] += abs(cents)
            self._forward(
                other,
                abs(cents),
                cash,
                visited,
                path + (partial.id,),
                source,
                line_q,
                writeoff,
            )
        elif cents:
            self._rest_used[line_q.id] += abs(cents)
            self._rest(line_q, cents, cash, source, self.graph.move_of(line_q), path)

    def _check_transits(self):
        """C-27: payment registered, but money has not (fully) arrived."""
        for move in self.graph.moves_of_class(m.TRANSIT):
            reached = max(self.reached[(move.id, 1)], self.reached[(move.id, -1)])
            if reached >= 1:
                continue
            side = (
                1 if self.reached[(move.id, 1)] >= self.reached[(move.id, -1)] else -1
            )
            total = sum(
                abs(line.balance)
                for line in self.graph.lines_of(move.id, m.R)
                if sign(line.balance) == side
            )
            self.findings.append(
                m.Finding(
                    code="C-27",
                    severity=m.WARNING,
                    key="payment_not_matched",
                    years=tuple(
                        sorted({move.date.year} | self._transit_years[move.id])
                    ),
                    move_ids=(move.id,),
                    amount=round(total * (1 - reached)),
                )
            )

    # ------------------------------------------------------- rests
    def _rest(self, line, value, cash, source, origin, path):
        """Unallocated rest of a reconcilable line (5.4 no. 3)."""
        account = self.graph.accounts[line.account_id]
        if account.suspense:
            kind = m.UNCLEAR
        elif line.id in self.classifications:
            self._classified(line, value, cash, source, origin, path)
            return
        else:
            kind = m.UNALLOCATED
        event = self.new_event()
        self._line_slice(
            line,
            value,
            cash,
            event,
            kind=kind,
            source=source,
            origin=origin,
            path=path,
        )

    def _classified(self, line, value, cash, source, origin, path):
        """Realise ``value`` as the user classified it (5.7 no. 2)."""
        classification = self.classifications[line.id]
        self.used_classifications.add(line.id)
        event = self.new_event()
        day = cash.date
        common = {
            "kind": m.CLASSIFIED,
            "date": day,
            "money_date": cash.money_date,
            "event": event,
            "source_move_id": source.id,
            "source_line_id": self.money_line(source.id),
            "origin_move_id": origin.id,
            "origin_line_id": line.id,
            "document_move_id": origin.id,
            "account_id": line.account_id,
            "partner_id": line.partner_id,
            "path": path,
            "override": cash.override,
        }
        if classification.kind in m.NEUTRAL_CLASSIFICATIONS:
            self.slices.append(m.Slice(amount=value, key=(line.id, NET), **common))
            return
        category = self.graph.categories.get(classification.category)
        regime = classification.vat_regime or vat.regime_at(
            self.data.vat_periods, day, self.data.settings.default_regime
        )
        rate = classification.tax_rate if regime == m.REGULAR else 0
        net, tax = gross_split(value, rate)
        self.slices.append(
            m.Slice(
                amount=net,
                key=(line.id, NET),
                category=classification.category,
                regime=regime,
                tax_rate=Fraction(rate),
                **common,
            )
        )
        if tax:
            tax_kind = "sale" if category and category.role == "be" else "purchase"
            self.slices.append(
                m.Slice(
                    amount=tax,
                    key=(line.id, TAX),
                    virtual_tax=tax_kind,
                    regime=regime,
                    **common,
                )
            )

    def _classified_writeoff(self, line, category):
        """Money reached a classified write-off line (C-36, R-53)."""
        return (
            category is not None
            and category.writeoff
            and line.id in self.classifications
        )

    def _note(self, kind, cash, value, source, line, move_id=None):
        self.notes.append(
            m.MoneyNote(
                kind=kind,
                date=cash.date,
                amount=value,
                source_move_id=source.id,
                move_id=move_id or (line.move_id if line else None),
                line_id=line.id if line else None,
                money_date=cash.money_date,
            )
        )

    # ------------------------------------------------------- slices
    def _line_slice(self, line, cents, cash, event, **values):
        """Slice of a real line; suspense accounts are unclear (B-05)."""
        if not cents:
            return
        source = values.pop("source")
        origin = values.pop("origin", None) or self.graph.move_of(line)
        kind = values.pop("kind")
        if self.graph.accounts[line.account_id].suspense:
            kind = m.UNCLEAR
        day = cash.date
        category = self.graph.category(line, day)
        if self._classified_writeoff(line, category):
            self._classified(line, cents, cash, source, origin, values.get("path", ()))
            return
        self.slices.append(
            m.Slice(
                kind=kind,
                date=day,
                money_date=cash.money_date,
                amount=cents,
                event=event,
                key=(line.id, 0),
                source_move_id=source.id,
                source_line_id=self.money_line(source.id),
                origin_move_id=origin.id,
                origin_line_id=line.id,
                document_move_id=origin.id,
                account_id=line.account_id,
                partner_id=line.partner_id or origin.partner_id,
                repartition_id=line.repartition_id,
                category=category.code if category else None,
                path=values.pop("path", ()),
                override=cash.override,
            )
        )

    # ================================================================ phase C
    def run_documents(self, fx_allocate=False):
        self._init_ports()
        exchange_moves = self.graph.exchange_move_ids()
        if fx_allocate:
            self._fx_documents = self._exchange_documents()
        bound, free = self._doc_partials()
        paid = {payment.partial_id for payment in self.payments}
        queue = []
        bound_by_partial = defaultdict(list)
        for partial_id, partials in bound.items():
            if partial_id in paid:
                bound_by_partial[partial_id] = partials
            else:
                free += partials
        for payment in self.payments:
            queue.append(
                (payment.date, 1, payment.partial_id, payment.seq, 0, "E1", payment)
            )
        for partial in free:
            queue.append((partial.max_date, 0, partial.id, 0, 0, "E2", partial))
        for _date, _order, partial_id, _seq, _after, kind, item in sorted(
            queue, key=lambda entry: entry[:5]
        ):
            if kind == "E1":
                for exchange in bound_by_partial.pop(partial_id, ()):
                    self._partial_between_documents(
                        exchange, exchange_moves, fx_allocate
                    )
                self._payment(item)
            else:
                self._partial_between_documents(item, exchange_moves, fx_allocate)
        self._check_currency()

    def _is_write_off_port(self, port_id):
        line = self.graph.lines[port_id]
        move = self.graph.move_of(line)
        for line_n in self.graph.lines_of(move.id, m.N):
            category = self.graph.category(line_n, move.date)
            if category is not None and category.writeoff:
                return True
        return False

    def _init_ports(self):
        """Composition of the open items of the documents (5.5).

        Instalments: the lines are divided over the ports in whole cent so
        that every line and every port adds up exactly (controlled rounding
        of the cumulative shares); paid in full, each line is realised with
        its exact balance.
        """
        for move in self.graph.moves_of_class(m.DOC):
            ports = self.graph.lines_of(move.id, m.R)
            total = sum(abs(port.balance) for port in ports)
            lines_n = self.graph.lines_of(move.id, m.N)
            keys = [(line.id, 0) for line in lines_n]
            before = dict.fromkeys(keys, 0)
            share = 0
            for port in ports:
                share += abs(port.balance)
                exact = {
                    (line.id, 0): Fraction(line.balance * share, total)
                    for line in lines_n
                }
                cumulative = apportion(exact, int(sum(exact.values()))) if exact else {}
                composition = {
                    key: Fraction(cumulative[key] - before[key]) for key in keys
                }
                before = cumulative
                self._add_port(port, composition)
        openings = {item.line_id: item for item in self.data.opening_items}
        for move in self.graph.moves_of_class(m.OPENING):
            for port in self.graph.lines_of(move.id, m.R):
                self._add_port(port, self._opening_composition(port, openings))

    def _add_port(self, port, composition):
        self.ports[port.id] = composition
        self.port_initial[port.id] = dict(composition)
        self.req[port.id] = abs(port.balance)
        for key, value in composition.items():
            self.initial[key] += value

    def _opening_composition(self, port, openings):
        """Opening item: net and tax from the takeover input (B-08, C-35)."""
        item = openings.get(port.id)
        gross = -port.balance
        move = self.graph.move_of(port)
        category = self.graph.category(port, move.date)
        if item is None and category is not None and category.role in _SELF_ROLES:
            # open item on a money transit, VAT or suspense account (1360,
            # 1780, 1545): no invoice behind it, the category of the account
            # itself is the composition (neutral; VAT payment or refund 5.7
            # no. 5; unclear C-10); receivables, payables and accounts of
            # income or expense need the takeover (C-35)
            self.virtual[(port.id, NET)] = _Virtual(
                category.code,
                None,
                None,
                None,
                move.id,
                port.account_id,
                port.partner_id,
            )
            return {(port.id, NET): Fraction(gross)}
        if item is None:
            self.virtual[(port.id, NET)] = _Virtual(
                None, None, None, None, move.id, port.account_id, port.partner_id
            )
            return {(port.id, NET): Fraction(gross)}
        rate = item.tax_rate if item.vat_regime == m.REGULAR else 0
        net, tax = gross_split(gross, rate)
        self.virtual[(port.id, NET)] = _Virtual(
            item.category,
            None,
            item.vat_regime,
            Fraction(rate),
            move.id,
            port.account_id,
            port.partner_id,
        )
        composition = {(port.id, NET): Fraction(net)}
        if tax:
            self.virtual[(port.id, TAX)] = _Virtual(
                None,
                "sale" if port.balance > 0 else "purchase",
                item.vat_regime,
                Fraction(rate),
                move.id,
                port.account_id,
                port.partner_id,
            )
            composition[(port.id, TAX)] = Fraction(tax)
        return composition

    def _doc_partials(self):
        """Partials between two ports, split into bound (exchange) and free."""
        partials = [
            partial
            for partial in sorted(self.graph.partials.values(), key=lambda p: p.id)
            if partial.debit_line_id in self.ports
            and partial.credit_line_id in self.ports
        ]
        by_line = defaultdict(list)
        for partial in partials:
            by_line[partial.debit_line_id].append(partial)
            by_line[partial.credit_line_id].append(partial)
        bound, used = defaultdict(list), set()
        for partial in sorted(self.graph.partials.values(), key=lambda p: p.id):
            if not partial.exchange_move_id:
                continue
            for line_id in (partial.debit_line_id, partial.credit_line_id):
                for candidate in by_line.get(line_id, ()):
                    lines = (candidate.debit_line_id, candidate.credit_line_id)
                    other_moves = {self.graph.lines[other].move_id for other in lines}
                    if partial.exchange_move_id in other_moves and (
                        candidate.id not in used
                    ):
                        bound[partial.id].append(candidate)
                        used.add(candidate.id)
        free = [partial for partial in partials if partial.id not in used]
        return bound, free

    def _payment(self, payment):
        """E1: realise ``K(q) · a / req(q)`` at the cash date (5.5).

        A write-off share of the payment is neutralised first, like an E2
        just before the money (5.5, payment difference).
        """
        port = payment.port
        document_port = self._fx_documents.get(port.move_id)
        if (
            document_port is not None
            and not payment.writeoff
            and sign(document_port.balance) == sign(port.balance)
            and sum(self.port_initial[document_port.id].values())
        ):
            self._allocate_exchange(payment, document_port)
            return
        if payment.writeoff:
            self._neutralize_share(port, payment.writeoff, payment.partial_id)
        open_amount = self.req[port.id]
        money = payment.amount - payment.writeoff
        amount = min(money, open_amount)
        excess = money - amount
        composition = self.ports[port.id]
        source = self.graph.moves[payment.source_id]
        event = self.new_event()
        if amount and composition:
            raw = {
                key: value * amount / open_amount for key, value in composition.items()
            }
            allocation = apportion(raw, -sign(port.balance) * amount)
            for key in sorted(allocation):
                cents = allocation[key]
                composition[key] -= cents
                self.realized[key] += cents
                self._doc_slice(key, cents, payment, event, source)
        self.req[port.id] -= amount
        self.processed[(port.id, payment.partial_id)] += amount
        if excess:
            # cannot happen with consistent partials; keeps I-1 balanced
            self._rest(
                port,
                -sign(port.balance) * excess,
                payment.cash,
                source,
                source,
                payment.path,
            )

    def _exchange_documents(self):
        """Exchange difference entry → open item of its document (B-07)."""
        result = {}
        for partial in sorted(self.graph.partials.values(), key=lambda p: p.id):
            if not partial.exchange_move_id:
                continue
            for line_id in (partial.debit_line_id, partial.credit_line_id):
                line = self.graph.lines.get(line_id)
                if (
                    line is not None
                    and line.id in self.ports
                    and line.move_id != partial.exchange_move_id
                    and self.graph.cls[line.move_id] == m.DOC
                ):
                    result.setdefault(partial.exchange_move_id, line)
        return result

    def _allocate_exchange(self, payment, document_port):
        """Setting "allocate" (5.12): money that reaches an exchange
        difference entry (gain on a receivable, loss on a payable) is
        realised with the composition of the document, like the rest of
        the payment; the exchange line itself is neutralised. A difference
        between two documents without money is neutralised in
        :meth:`_partial_between_documents`."""
        port = payment.port
        amount = min(payment.amount, self.req[port.id])
        excess = payment.amount - amount
        source = self.graph.moves[payment.source_id]
        self._neutralize_share(port, amount, payment.partial_id)
        initial = self.port_initial[document_port.id]
        total = abs(sum(initial.values()))
        raw = {key: value * amount / total for key, value in initial.items()}
        allocation = apportion(raw, -sign(port.balance) * amount) if amount else {}
        event = self.new_event()
        for key in sorted(allocation):
            cents = allocation[key]
            self.realized[key] += cents
            # realised beyond the document: no part of its open composition
            self.neutralized[key] -= cents
            self._doc_slice(key, cents, payment, event, source)
        if excess:
            self._rest(
                port,
                -sign(port.balance) * excess,
                payment.cash,
                source,
                source,
                payment.path,
            )

    def _neutralize_share(self, port, amount, partial_id):
        """Neutralise ``amount`` of the open composition of ``port``."""
        open_amount = self.req[port.id]
        amount = min(amount, open_amount)
        if amount <= 0:
            return
        composition = self.ports[port.id]
        for key, value in composition.items():
            part = value * amount / open_amount
            composition[key] = value - part
            self.neutralized[key] += part
        self.req[port.id] -= amount
        self.processed[(port.id, partial_id)] += amount

    def _doc_slice(self, key, cents, payment, event, source):
        if not cents:
            return
        if key[1]:
            self._virtual_slice(key, cents, payment, event, source)
            return
        line = self.graph.lines[key[0]]
        category = self.graph.category(line, payment.date)
        if self._classified_writeoff(line, category):
            self._classified(
                line,
                cents,
                payment.cash,
                source,
                self.graph.move_of(line),
                payment.path,
            )
            return
        origin = self.graph.move_of(line)
        document = self.graph.move_of(payment.port)
        opening = self.graph.cls[document.id] == m.OPENING
        kind = m.KIND_OPENING if opening else m.DOC_VIA_PAYMENT
        if self.graph.accounts[line.account_id].suspense:
            # a document line on the bank suspense account (B-05)
            kind = m.UNCLEAR
        self.slices.append(
            m.Slice(
                kind=kind,
                date=payment.date,
                money_date=payment.cash.money_date,
                amount=cents,
                event=event,
                key=key,
                source_move_id=payment.source_id,
                source_line_id=self.money_line(payment.source_id),
                origin_move_id=origin.id,
                origin_line_id=line.id,
                document_move_id=document.id,
                account_id=line.account_id,
                partner_id=line.partner_id or origin.partner_id,
                repartition_id=line.repartition_id,
                category=category.code if category else None,
                path=payment.path,
                override=payment.override,
            )
        )

    def _virtual_slice(self, key, cents, payment, event, source):
        virtual = self.virtual[key]
        self.slices.append(
            m.Slice(
                kind=m.KIND_OPENING,
                date=payment.date,
                money_date=payment.cash.money_date,
                amount=cents,
                event=event,
                key=key,
                source_move_id=source.id,
                source_line_id=self.money_line(source.id),
                origin_move_id=virtual.move_id,
                origin_line_id=key[0],
                document_move_id=virtual.move_id,
                account_id=virtual.account_id,
                partner_id=virtual.partner_id,
                category=virtual.category,
                path=payment.path,
                override=payment.override,
                virtual_tax=virtual.virtual_tax,
                regime=virtual.regime,
                tax_rate=virtual.tax_rate,
            )
        )

    def _partial_between_documents(self, partial, exchange_moves, fx_allocate):
        """E2: a partial between two ports without money (5.5)."""
        ports = (
            self.graph.lines[partial.debit_line_id],
            self.graph.lines[partial.credit_line_id],
        )
        amount = partial.amount
        bundle = defaultdict(Fraction)
        for port in ports:
            open_amount = self.req[port.id]
            if open_amount <= 0:
                continue
            taken = min(amount, open_amount)
            composition = self.ports[port.id]
            for key, value in composition.items():
                part = value * taken / open_amount
                bundle[key] += part
                composition[key] = value - part
            self.req[port.id] -= taken
            self.processed[(port.id, partial.id)] += taken
        moves = {port.move_id for port in ports}
        if any(self._is_write_off_port(port.id) for port in ports):
            # a write-off settles what is open and never changes the
            # composition of what money pays later (5.5, T10)
            self._neutralize(bundle, partial)
            return
        self._check_late_reconciliation(partial, moves)
        targets = [port for port in ports if self.req[port.id] > 0]
        if not targets:
            targets = [
                port
                for move_id in sorted(moves)
                for port in self.graph.lines_of(move_id, m.R)
                if port.id in self.req and self.req[port.id] > 0
            ]
        allocate = fx_allocate and bool(moves & exchange_moves)
        if allocate or not targets:
            self._neutralize(bundle, partial, notify=not allocate)
            return
        total = sum(self.req[port.id] for port in targets)
        for port in targets:
            composition = self.ports[port.id]
            for key, value in bundle.items():
                composition[key] = (
                    composition.get(key, 0) + value * self.req[port.id] / total
                )

    def _check_late_reconciliation(self, partial, moves):
        """C-60: a credit note reconciled after the end of its year.

        Partials between documents are processed on their date (5.5). One
        made after the end of that year changes what the money of that year
        realised after it (the composition of the open rest), although the
        year may already be filed. Until a filed run is compared (C-60 of
        stage E4) this is reported.
        """
        created = partial.created
        if created is None or created.year <= partial.max_date.year:
            return
        year = partial.max_date.year
        affected = [
            payment
            for payment in self.payments
            if payment.port.move_id in moves
            and payment.date >= partial.max_date
            and payment.date.year == year
        ]
        if affected:
            self.findings.append(
                m.Finding(
                    code="C-60",
                    severity=m.WARNING,
                    key="late_reconciliation",
                    years=(year, created.year),
                    move_ids=tuple(sorted(moves)),
                    line_ids=(partial.debit_line_id, partial.credit_line_id),
                    amount=partial.amount,
                    params={"created": created},
                )
            )

    def _neutralize(self, bundle, partial, notify=True):
        """No open port left: the bundle is dropped (no money flowed).

        A private line of a journal entry in the bundle means that the entry
        paid a document with private money (or the reverse) and was not
        recognised as payment (5.2 c): the withdrawal or contribution and
        the income or expense would be lost, so it is a blocker.
        """
        per_category = defaultdict(Fraction)
        private = Fraction(0)
        for key, value in bundle.items():
            self.neutralized[key] += value
            per_category[self._key_category(key, partial.max_date)] += value
            if not key[1] and key[0] in self.graph.private_lines:
                line = self.graph.lines[key[0]]
                if self.graph.move_of(line).move_type == "entry":
                    private += value
        if private:
            self.findings.append(
                m.Finding(
                    code="C-25",
                    severity=m.BLOCKER,
                    key="neutralized_private",
                    years=(partial.max_date.year,),
                    move_ids=tuple(
                        sorted(
                            {
                                self.graph.lines[line_id].move_id
                                for line_id in (
                                    partial.debit_line_id,
                                    partial.credit_line_id,
                                )
                            }
                        )
                    ),
                    line_ids=(partial.debit_line_id, partial.credit_line_id),
                    amount=partial.amount,
                )
            )
            return
        if notify and any(per_category.values()):
            self.findings.append(
                m.Finding(
                    code="C-25",
                    severity=m.INFO,
                    key="neutralized",
                    years=(partial.max_date.year,),
                    line_ids=(partial.debit_line_id, partial.credit_line_id),
                    amount=partial.amount,
                )
            )

    def _key_category(self, key, day):
        if key[1]:
            return self.virtual[key].category or self.virtual[key].virtual_tax
        category = self.graph.category(self.graph.lines[key[0]], day)
        return category.code if category else None

    def _check_currency(self):
        """C-19: foreign currency item settled, rest open in company currency.

        Reported in the year of the document and in the years in which money
        reached it: there the composition is realised too low.
        """
        money_years = defaultdict(set)
        for payment in self.payments:
            money_years[payment.port.id].add(payment.date.year)
        for port_id in sorted(self.ports):
            line = self.graph.lines[port_id]
            if not line.currency_id or not line.residual_currency_zero:
                continue
            partials = [partial for partial, _ in self.graph.edges[port_id]]
            matched = sum(partial.amount for partial in partials)
            missing = abs(line.balance) - matched
            if missing > 0:
                years = {self.graph.move_of(line).date.year} | money_years[port_id]
                years |= {partial.max_date.year for partial in partials}
                self.findings.append(
                    m.Finding(
                        code="C-19",
                        severity=m.BLOCKER,
                        key="exchange_difference_missing",
                        years=tuple(sorted(years)),
                        move_ids=(line.move_id,),
                        line_ids=(line.id,),
                        amount=missing,
                    )
                )
