# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Independent second implementation of the cash flow core (spec 9.1).

Derived from the research prototype of the specification with its
corrections: reversal pairs only when reconciled with each other (B-03),
exchange differences bound to their payment (B-07) and amounts that are
already whole cent when a payment passes a transit entry in full (B-06 is
avoided by the cases used). It knows only money, open item and other lines
and returns the realised amounts per year and account; the differential
test compares them with :mod:`..models.engine`.
"""

import math
from collections import defaultdict
from fractions import Fraction

CASH_TYPES = {"asset_cash", "liability_credit_card"}
OPEN_TYPES = {"asset_receivable", "liability_payable"}


def hamilton(raw, total):
    """Largest remainder method, normalised to the sign of ``total``."""
    direction = -1 if total < 0 else 1
    scaled = {key: value * direction for key, value in raw.items()}
    floors = {key: math.floor(value) for key, value in scaled.items()}
    rest = total * direction - sum(floors.values())
    order = sorted(scaled, key=lambda key: (-(scaled[key] - floors[key]), key))
    for key in order[:rest]:
        floors[key] += 1
    return {key: value * direction for key, value in floors.items()}


class Oracle:
    def __init__(self, data):
        accounts = {account.id: account for account in data.accounts}
        self.moves = {move.id: move for move in data.moves if move.state == "posted"}
        self.lines = {}
        self.by_move = defaultdict(list)
        for line in sorted(data.lines, key=lambda line: line.id):
            if line.move_id in self.moves and line.balance:
                self.lines[line.id] = line
                self.by_move[line.move_id].append(line)
        self.role = {}
        for line in self.lines.values():
            account = accounts[line.account_id]
            if account.account_type in CASH_TYPES or account.liquidity:
                self.role[line.id] = "L"
            elif account.internal_group in ("asset", "liability") and (
                account.reconcile or account.account_type in OPEN_TYPES
            ):
                self.role[line.id] = "R"
            else:
                self.role[line.id] = "N"
        self.partials = sorted(data.partials, key=lambda partial: partial.id)
        self._drop_reversals()
        self.by_line = defaultdict(list)
        for partial in self.partials:
            debit = self.lines.get(partial.debit_line_id)
            credit = self.lines.get(partial.credit_line_id)
            if debit and credit and debit.move_id != credit.move_id:
                self.by_line[debit.id].append((partial, credit))
                self.by_line[credit.id].append((partial, debit))
        self.cls = {move_id: self._class(move_id) for move_id in self.moves}
        self.realised = defaultdict(int)

    def _drop_reversals(self):
        for move in sorted(self.moves.values(), key=lambda move: move.id):
            original = move.reversed_entry_id
            if original not in self.moves or move.id not in self.moves:
                continue
            pair = {move.id, original}
            sums = defaultdict(int)
            for move_id in pair:
                for line in self.by_move[move_id]:
                    sums[(line.account_id, line.partner_id, line.repartition_id)] += (
                        line.balance
                    )
            inside = all(
                self.lines[other].move_id in pair
                for partial in self.partials
                for one, other in (
                    (partial.debit_line_id, partial.credit_line_id),
                    (partial.credit_line_id, partial.debit_line_id),
                )
                if one in self.lines
                and other in self.lines
                and self.lines[one].move_id in pair
            )
            if inside and not any(sums.values()):
                for move_id in pair:
                    for line in self.by_move.pop(move_id):
                        del self.lines[line.id]
                    del self.moves[move_id]

    def _class(self, move_id):
        roles = [self.role[line.id] for line in self.by_move[move_id]]
        if "L" in roles:
            return "SOURCE"
        open_items = [
            line for line in self.by_move[move_id] if self.role[line.id] == "R"
        ]
        if not open_items:
            return "DIRECT"
        sides = {line.balance > 0 for line in open_items}
        return "TRANSIT" if len(sides) == 2 else "DOC"

    def _add(self, day, line, cents):
        if cents:
            self.realised[(day.year, line.account_id)] += cents

    def run(self):
        events = []
        for move in sorted(self.moves.values(), key=lambda move: (move.date, move.id)):
            if self.cls[move.id] != "SOURCE":
                continue
            for line in self.by_move[move.id]:
                if self.role[line.id] == "N":
                    self._add(move.date, line, line.balance)
                elif self.role[line.id] == "R":
                    self._source_line(move, line, events)
        self._documents(events)
        return {key: value for key, value in self.realised.items() if value}

    def _source_line(self, move, line, events):
        matched = 0
        for partial, other in self.by_line[line.id]:
            matched += partial.amount
            self._forward(other, partial.amount, move.date, {move.id}, partial, events)
        rest = abs(line.balance) - matched
        if rest:
            self._add(move.date, line, (1 if line.balance > 0 else -1) * rest)

    def _forward(self, line, amount, day, visited, partial, events):
        move_id = line.move_id
        if move_id in visited or self.cls[move_id] in ("SOURCE", "DIRECT"):
            return
        if self.cls[move_id] == "DOC":
            events.append((day, 1, partial.id, len(events), "E1", (line, amount)))
            return
        side = line.balance > 0
        lines = self.by_move[move_id]
        total = sum(
            abs(x.balance)
            for x in lines
            if self.role[x.id] == "R" and (x.balance > 0) == side
        )
        factor = Fraction(amount, total)
        assert factor == 1, "the oracle handles transit entries paid in full only"
        for x in lines:
            if self.role[x.id] == "N":
                self._add(day, x, x.balance)
            elif self.role[x.id] == "R" and (x.balance > 0) != side:
                for next_partial, other in self.by_line[x.id]:
                    self._forward(
                        other,
                        next_partial.amount,
                        day,
                        visited | {move_id},
                        next_partial,
                        events,
                    )

    def _documents(self, events):
        ports, req = {}, {}
        for move_id, cls in self.cls.items():
            if cls != "DOC":
                continue
            lines = self.by_move[move_id]
            port_lines = [line for line in lines if self.role[line.id] == "R"]
            total = sum(abs(port.balance) for port in port_lines)
            for port in port_lines:
                ports[port.id] = {
                    line.id: Fraction(line.balance * abs(port.balance), total)
                    for line in lines
                    if self.role[line.id] == "N"
                }
                req[port.id] = abs(port.balance)
        paid = {item[2] for item in events}
        bound = defaultdict(list)
        free = []
        for partial in self.partials:
            if partial.debit_line_id in ports and partial.credit_line_id in ports:
                free.append(partial)
        for partial in self.partials:
            if partial.exchange_move_id and partial.id in paid:
                for candidate in list(free):
                    moves = {
                        self.lines[candidate.debit_line_id].move_id,
                        self.lines[candidate.credit_line_id].move_id,
                    }
                    shares = {candidate.debit_line_id, candidate.credit_line_id} & {
                        partial.debit_line_id,
                        partial.credit_line_id,
                    }
                    if partial.exchange_move_id in moves and shares:
                        bound[partial.id].append(candidate)
                        free.remove(candidate)
        for partial in free:
            events.append((partial.max_date, 0, partial.id, -1, "E2", partial))
        for day, _order, partial_id, _seq, kind, item in sorted(
            events, key=lambda entry: entry[:4]
        ):
            if kind == "E1":
                for exchange in bound.pop(partial_id, ()):
                    self._e2(exchange, ports, req)
                self._e1(day, item, ports, req)
            else:
                self._e2(item, ports, req)

    def _e1(self, day, item, ports, req):
        port, amount = item
        amount = min(amount, req[port.id])
        if not amount:
            return
        composition = ports[port.id]
        raw = {key: value * amount / req[port.id] for key, value in composition.items()}
        allocation = hamilton(raw, -(1 if port.balance > 0 else -1) * amount)
        for key, cents in allocation.items():
            composition[key] -= cents
            self._add(day, self.lines[key], cents)
        req[port.id] -= amount

    def _e2(self, partial, ports, req):
        bundle = defaultdict(Fraction)
        ports_of = (partial.debit_line_id, partial.credit_line_id)
        for port_id in ports_of:
            if req[port_id] <= 0:
                continue
            share = Fraction(partial.amount, req[port_id])
            for key, value in ports[port_id].items():
                bundle[key] += value * share
                ports[port_id][key] = value - value * share
            req[port_id] -= partial.amount
        targets = [port_id for port_id in ports_of if req[port_id] > 0]
        if not targets:
            moves = {self.lines[port_id].move_id for port_id in ports_of}
            targets = [
                port_id
                for port_id in sorted(ports)
                if self.lines[port_id].move_id in moves and req[port_id] > 0
            ]
        total = sum(req[port_id] for port_id in targets)
        for port_id in targets:
            for key, value in bundle.items():
                ports[port_id][key] = (
                    ports[port_id].get(key, 0) + value * req[port_id] / total
                )
