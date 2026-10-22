# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Phase D: cash date and year of attribution (spec 5.6, R-04 to R-08).

* Cash date of a source: manual override with reason, else the date of the
  entry; a payment booked on the bank account takes the date of the
  statement line it is reconciled with (C-24). Outgoing money from 1 to 5
  January is listed for review (C-23); direct debits of the tax office
  after 10 January can take the legal due date (R-06).
* Ten-day rule (§ 11 (1) 2, (2) 2 EStG): a regularly recurring payment
  belongs to its economic year if payment **and** legal due date lie in
  the window 22.12.–10.01. Candidates are derived from the data; each one
  must be decided (C-26), VAT returns with complete input are decided
  automatically.
"""

import datetime
from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction

from . import model as m
from . import vat
from .rounding import apportion, sign

#: months between regular payments (5.6.1 no. 2)
_REGULAR_GAPS = frozenset({1, 3, 6, 12})
#: documents whose ``invoice_date_due`` is a due date of the claim (5.6.2);
#: Odoo fills the field of journal entries with the day they were created
INVOICE_TYPES = frozenset(
    {
        "out_invoice",
        "in_invoice",
        "out_refund",
        "in_refund",
        "out_receipt",
        "in_receipt",
    }
)
#: reasons that propose "apply" if the economic year differs (5.6.1)
_APPLY_REASONS = frozenset({"flag", "tax_office", "economic_year_entered"})
#: "practically equal" amounts of steady payments (5.6.1 proposal)
_STEADY_TOLERANCE = 100


@dataclass(frozen=True)
class CashDate:
    """Cash date of money (5.6).

    ``override``: entered by the user (on the source or a payment entry in
    the path). ``origin``: cash date at the source, if a cash date entered
    on a payment entry replaced it (:attr:`money_date`).
    """

    date: datetime.date
    override: bool = False
    origin: datetime.date | None = None

    @property
    def money_date(self):
        """Cash date of the money at its source (I-1)."""
        return self.origin or self.date


class Window:
    """Window of the ten-day rule around the turn of a year (Z-10)."""

    def __init__(self, params):
        self.params = params

    def bounds(self, base_year):
        """First and last day of the window at the end of ``base_year``."""
        start = self.params.month_day("ten_day_from", base_year, (12, 22))
        end = self.params.month_day("ten_day_to", base_year, (1, 10))
        return (
            datetime.date(base_year, *start),
            datetime.date(base_year + 1, *end),
        )

    def contains(self, day, base_year):
        if day is None:
            return False
        start, end = self.bounds(base_year)
        return start <= day <= end

    def base_year(self, day):
        """Year whose year end window contains ``day``, else ``None``."""
        for base in (day.year, day.year - 1):
            if self.contains(day, base):
                return base
        return None

    def applies(self, payment, economic_year, due):
        """Whether the rule moves a payment into ``economic_year`` (R-07)."""
        if economic_year is None or due is None or economic_year == payment.year:
            return False
        base = min(economic_year, payment.year)
        return self.contains(payment, base) and self.contains(due, base)


# ------------------------------------------------------------ cash dates
class CashDates:
    """Cash date of every source (5.6) with the reviews C-23, C-24, R-06."""

    def __init__(self, graph, params):
        self.graph = graph
        self.params = params
        self.data = graph.data
        self.dates = {}
        #: sources with money on several cash dates: [(CashDate, cent)]
        self.tranches = {}
        self.items = []
        self.findings = []
        self._transfer_checked = set()
        self._decisions = {
            (decision.source_move_id, decision.origin_move_id, decision.kind): decision
            for decision in self.data.ten_day_decisions
        }
        for move in graph.moves_of_class(m.SOURCE):
            self.dates[move.id] = self._cash_date(move)

    def get(self, move_id):
        """Cash date of a source (the first one of a split payment)."""
        return self.dates[move_id]

    def tranches_of(self, move_id):
        """``[(CashDate, cent)]`` of a source: several for a payment booked
        on the bank account and matched with statement lines of different
        dates (C-24), else one (the weight is then ``None``)."""
        return self.tranches.get(move_id) or [(self.dates[move_id], None)]

    def _money_out(self, move):
        return sum(line.balance for line in self.graph.lines_of(move.id, m.L)) < 0

    def _cash_date(self, move):
        if move.info.cash_date:
            return CashDate(move.info.cash_date, True)
        debit = self._direct_debit(move)
        if debit is not None:
            return CashDate(debit, True)
        tranches = self._statement_tranches(move)
        if tranches:
            for cash, _weight in tranches:
                if not cash.override:
                    # payment on the bank account: the date of the statement
                    # counts, the order date is just as unknown (C-23, C-24)
                    self._check_transfer_order(move, cash.date)
            if len(tranches) > 1:
                self.tranches[move.id] = tranches
            return tranches[0][0]
        self._check_transfer_order(move, move.date)
        return CashDate(move.date)

    def _statement_tranches(self, move):
        """Cash dates of a payment booked on the bank account (C-24).

        The money of the payment counts when it is on the statement: each
        statement line it is matched with gives its cash date (its override,
        otherwise its date) to the matched amount. Matched with statement
        lines of different dates (two years), the money is split; an
        unmatched rest keeps the date of the payment (warning). Returns
        ``[(CashDate, cent)]`` sorted by date, or ``None``.
        """
        if move.statement:
            return None
        lines = self.graph.lines_of(move.id, m.L)
        total = abs(sum(line.balance for line in lines))
        weights = defaultdict(int)
        statements = set()
        for line in lines:
            for partial, other in self.graph.edges.get(line.id, ()):
                other_move = self.graph.move_of(other)
                if self.graph.role[other.id] == m.L and other_move.statement:
                    day = other_move.info.cash_date or other_move.date
                    cash = CashDate(day, bool(other_move.info.cash_date))
                    weights[cash] += partial.amount
                    statements.add(other_move.id)
        if not weights or not total:
            return None
        rest = total - sum(weights.values())
        if rest > 0:
            weights[CashDate(move.date)] += rest
            self.findings.append(
                m.Finding(
                    code="C-24",
                    severity=m.WARNING,
                    key="payment_on_bank_account_rest",
                    years=(move.date.year,),
                    move_ids=(move.id,),
                    amount=rest,
                )
            )
        elif rest < 0:
            # matched beyond the money of the payment: split by the matches
            scale = Fraction(total, total - rest)
            weights = _scaled(weights, scale, total)
        tranches = sorted(
            weights.items(), key=lambda item: (item[0].date, item[0].override)
        )
        self.findings.append(
            m.Finding(
                code="C-24",
                severity=m.WARNING,
                key="payment_on_bank_account",
                years=tuple(sorted({cash.date.year for cash, _weight in tranches})),
                move_ids=(move.id, *sorted(statements)),
            )
        )
        return tranches

    def _is_returned_debit(self, move, day):
        """Money out over a receivable that the business did not order: a
        returned direct debit (the bank of the customer takes collected
        money back; USAGE: booked against the receivable of the customer as
        a new open item). Its receivable items are open or reconciled only
        with money received later (the new payment). A refund transferred to
        a customer offsets an earlier credit and stays a transfer (C-23)."""
        receivables = [
            line
            for line in self.graph.lines_of(move.id)
            if self.graph.role[line.id] != m.L
            and line.balance > 0
            and self.graph.accounts[line.account_id].account_type == "asset_receivable"
        ]
        if not receivables:
            return False
        return all(
            self.graph.move_of(other).date > day
            for line in receivables
            for _partial, other in self.graph.edges.get(line.id, ())
        )

    def _check_transfer_order(self, move, day):
        """C-23: outgoing money from 1 to 5 January (order date unknown);
        not for a returned direct debit, which the bank debits without an
        order of the business (§ 11 (2) EStG: the debit counts)."""
        if not (day.month == 1 and day.day <= 5) or not self._money_out(move):
            return
        if move.id in self.data.transfer_confirmations:
            return
        if self._is_returned_debit(move, day):
            return
        if move.id in self._transfer_checked:
            return
        self._transfer_checked.add(move.id)
        self.items.append(
            m.TenDayItem(
                kind="transfer_order",
                source_move_id=move.id,
                origin_move_id=move.id,
                cash_date=day,
                proposal="confirm",
                proposed_cash_date=move.payment_order_date,
            )
        )
        self.findings.append(
            m.Finding(
                code="C-23",
                severity=m.WARNING,
                key="transfer_order_date",
                years=(day.year - 1, day.year),
                move_ids=(move.id,),
                params={"proposed_date": move.payment_order_date},
            )
        )

    def _is_tax_payment(self, move):
        partner = self.graph.partners.get(move.partner_id)
        if partner is not None and partner.tax_office:
            return True
        for line in self.graph.lines_of(move.id):
            category = self.graph.category(line, move.date)
            if category is not None and category.vat_kind == "settlement":
                return True
            partner = self.graph.partners.get(line.partner_id)
            if partner is not None and partner.tax_office:
                return True
        return False

    def _direct_debit(self, move):
        """R-06: direct debit of the tax office between 11 and 31 January.

        Candidates for the VAT return of December or Q4 (without permanent
        extension) or November (with it). ``apply`` sets the cash date to
        the legal due date.

        Due date on a Saturday or Sunday (10.01.2026, 10.01.2027): the
        period for the payment ends on the next working day (§ 108 (3) AO),
        and a direct debit counts as paid on the due day of that period
        (§ 224 (2) no. 3 AO), which then lies after 10 January (H 11 EStH
        "Allgemeines": with a due date postponed under § 108 (3) AO the
        payment belongs to the previous year only if made within the ten
        days). The proposal is the year of payment; "apply" stays the
        decision of the user and is reported for the confirmation of the tax
        advisor (E5 review).
        """
        day = move.date
        parsed = vat.parse_vat_period(move.info.vat_period)
        if not (day.month == 1 and day.day > 10) or parsed is None:
            return None
        if not self._money_out(move) or not self._is_tax_payment(move):
            return None
        extension = vat.permanent_extension_for(
            move.info.vat_period, self.data.vat_periods
        )
        due = vat.vat_due_date(move.info.vat_period, extension)
        if due is None or due.year != day.year or due.month != 1:
            return None
        decision = self._decisions.get((move.id, move.id, "direct_debit_tax"))
        shifted = _next_working_day(due)
        item = m.TenDayItem(
            kind="direct_debit_tax",
            source_move_id=move.id,
            origin_move_id=move.id,
            cash_date=day,
            reasons=("tax_office",),
            economic_year=parsed[0],
            due_date=due,
            proposal="apply" if shifted == due else "reject",
            proposed_cash_date=due,
            decision=decision.decision if decision else None,
        )
        self.items.append(item)
        if decision is None:
            self.findings.append(
                m.Finding(
                    code="C-26",
                    severity=m.BLOCKER,
                    key="direct_debit_undecided",
                    years=(day.year - 1, day.year),
                    move_ids=(move.id,),
                    params={
                        "due_date": due,
                        "shifted_due_date": shifted if shifted != due else None,
                    },
                )
            )
            return None
        if decision.decision == "apply":
            item.applied = True
            cash = decision.due_date or due
            if _next_working_day(cash) != cash:
                self.findings.append(
                    m.Finding(
                        code="C-26",
                        severity=m.WARNING,
                        key="direct_debit_weekend_applied",
                        years=(day.year - 1, day.year),
                        move_ids=(move.id,),
                        params={
                            "due_date": cash,
                            "shifted_due_date": _next_working_day(cash),
                        },
                    )
                )
            return cash
        return None


# ------------------------------------------------------------ attribution
class _Event:
    """Slices of one money event at one origin entry."""

    def __init__(self, key, date):
        self.key = key
        self.date = date
        self.indexes = []
        self.amount = 0
        self.categories = defaultdict(int)


class TenDayRule:
    """Year of attribution of all slices (5.6)."""

    def __init__(self, graph, params, slices):
        self.graph = graph
        self.data = graph.data
        self.params = params
        self.slices = slices
        self.window = Window(params)
        self.items = []
        self.findings = []
        self._decisions = {
            (decision.source_move_id, decision.origin_move_id): decision
            for decision in self.data.ten_day_decisions
            if decision.kind == "recurring"
        }

    def run(self):
        for one in self.slices:
            one.attribution_year = one.date.year
        events = self._events()
        history = self._history(events)
        for event in events.values():
            base = self.window.base_year(event.date)
            if base is not None:
                self._review(event, base, history)
        return self.items, self.findings

    def _events(self):
        events = {}
        for index, one in enumerate(self.slices):
            if one.kind not in m.MONEY_KINDS or one.source_move_id is None:
                continue
            document = one.document_move_id or one.origin_move_id
            key = (one.source_move_id, document or one.source_move_id)
            event = events.get(key)
            if event is None:
                event = events[key] = _Event(key, one.date)
            event.indexes.append(index)
            event.amount += one.amount
            if one.category and not one.repartition_id and not one.virtual_tax:
                event.categories[one.category] += abs(one.amount)
        return events

    # ------------------------------------------------------- facts
    def _partner(self, event):
        source = self.graph.moves.get(event.key[0])
        origin = self.graph.moves.get(event.key[1])
        for move in (origin, source):
            if move is not None and move.partner_id:
                return move.partner_id
        for index in event.indexes:
            if self.slices[index].partner_id:
                return self.slices[index].partner_id
        return None

    @staticmethod
    def _main_category(event):
        if not event.categories:
            return None
        return max(event.categories.items(), key=lambda item: (item[1], item[0]))[0]

    def _history(self, events):
        """Money events per partner: (date, direction, category, amount, key)."""
        history = defaultdict(list)
        for event in events.values():
            partner = self._partner(event)
            if partner:
                history[partner].append(
                    (
                        event.date,
                        sign(event.amount),
                        self._main_category(event),
                        abs(event.amount),
                        event.key,
                    )
                )
        return history

    def _repeated(self, event, base, history):
        """5.6.1 no. 2: at least two similar payments to the same partner.

        Returns ``(repeated, steady)``. Steady payments have practically the
        same amount (± 1 %) and a regular rhythm; only they propose "apply"
        without a flag: changing amounts of project invoices of a regular
        customer are no regularly recurring payments.
        """
        partner = self._partner(event)
        direction = sign(event.amount)
        category = self._main_category(event)
        others = [
            entry
            for entry in history.get(partner, ())
            if entry[4] != event.key
            and entry[1] == direction
            and entry[2] == category
            and entry[0].year in (base - 1, base, base + 1)
            and entry[0] < self.window.bounds(base)[1]
        ]
        if len(others) < 2:
            return False, False
        amount = abs(event.amount)
        similar = [entry for entry in others if abs(entry[3] - amount) * 5 <= amount]
        repeated = len(similar) >= 2 or _regular_dates(
            [e[0] for e in others], event.date
        )
        equal = [
            entry
            for entry in others
            if abs(entry[3] - amount) * _STEADY_TOLERANCE <= amount
        ]
        # the rhythm of the earlier payments: the date of this payment is the
        # very question (December rent paid on 5 January)
        steady = len(equal) >= 2 and _regular_dates([e[0] for e in equal])
        return repeated, steady

    def _info(self, event):
        source = self.graph.moves.get(event.key[0])
        origin = self.graph.moves.get(event.key[1]) or source
        return source, origin

    def _related(self, event, source, origin):
        """Entries whose EÜR information counts for an event (5.6.1).

        The document first, then the payment entries the money passed (a
        cheque registered as payment), the money entry last.
        """
        moves = {origin.id: origin}
        path = self.slices[event.indexes[0]].path if event.indexes else ()
        for partial_id in reversed(path):
            partial = self.graph.partials.get(partial_id)
            if partial is None:
                continue
            for line_id in (partial.debit_line_id, partial.credit_line_id):
                line = self.graph.lines.get(line_id)
                if line is not None and self.graph.cls.get(line.move_id) == m.TRANSIT:
                    moves.setdefault(line.move_id, self.graph.move_of(line))
        moves.setdefault(source.id, source)
        return list(moves.values())

    def _economic_year(self, related, source, origin):
        """Economic year and how it is known (``(year, how)``).

        ``entered``: the field on the document, a payment entry or the money
        entry; ``vat_period``: the VAT return period; ``derived``: from the
        date of supply of the document, only a proposal (5.6.1).
        """
        for move in related:
            if move.info.economic_year:
                return move.info.economic_year, "entered"
        for move in related:
            parsed = vat.parse_vat_period(move.info.vat_period)
            if parsed is not None:
                return parsed[0], "vat_period"
        if origin.id != source.id and origin.move_type != "entry":
            return vat.supply_date(origin).year, "derived"
        return None, None

    def _due_date(self, event, related, origin, is_tax):
        """Legal due date and whether it is certain (``(date, certain)``).

        A VAT refund is due only when the tax office agrees (§ 168 S. 2,
        § 220 (2) S. 2 AO): without an entered date the date of receipt is
        only a proposal and the user decides (5.6.2). Only the due date of
        an invoice is a due date; Odoo fills it with the day of creation on
        journal entries (bank statement entries, direct debits), so their
        due date is unknown until it is entered.
        """
        for move in related:
            if move.info.due_date:
                return move.info.due_date, True
        if is_tax and event.amount < 0:
            return event.date, False
        for move in related:
            if move.info.vat_period:
                extension = vat.permanent_extension_for(
                    move.info.vat_period, self.data.vat_periods
                )
                due = vat.vat_due_date(move.info.vat_period, extension)
                return due, due is not None
        if origin.move_type in INVOICE_TYPES and origin.invoice_date_due:
            return origin.invoice_date_due, True
        return None, False

    def _is_tax(self, event, related):
        for move in related:
            partner = self.graph.partners.get(move.partner_id)
            if partner is not None and partner.tax_office:
                return True
        for code in event.categories:
            category = self.graph.categories.get(code)
            if category is not None and category.vat_kind == "settlement":
                return True
        return False

    def _reasons(self, event, base, history, facts):
        """Reasons of a candidate (5.6.1 no. 1 to 5) and whether the
        payments of the partner are steady."""
        source, origin, due, is_tax, economic_year, how = facts
        reasons = []
        if (
            origin.id != source.id
            and origin.move_type != "entry"
            and vat.supply_date(origin).year != event.date.year
            and self.window.contains(due, base)
        ):
            reasons.append("other_year_due")
        repeated, steady = self._repeated(event, base, history)
        if repeated:
            reasons.append("repeated_partner")
        if is_tax:
            reasons.append("tax_office")
        partner = self.graph.partners.get(self._partner(event))
        flagged = partner is not None and partner.recurring
        for code in event.categories:
            category = self.graph.categories.get(code)
            flagged = flagged or bool(category and category.recurring_candidate)
        if flagged:
            reasons.append("flag")
        if how == "entered" and economic_year != event.date.year:
            reasons.append("economic_year_entered")
        return tuple(reasons), steady

    # ------------------------------------------------------- review
    def _proposal(self, reasons, steady, economic_year, payment_year):
        """Pre-decision of a candidate (5.6.1): flags, the tax office, an
        entered economic year and steady payments propose "apply" if the
        economic year differs from the year of payment; similar payments of
        changing amounts or a document of the other year alone do not."""
        if economic_year is None or economic_year == payment_year:
            return "review"
        if set(reasons) & _APPLY_REASONS or steady:
            return "apply"
        return "review"

    def _shifted(self, event):
        """Slices the rule moves (5.6, R-07).

        All slices of a document. Of the lines booked on the money entry
        itself only those of the main category, of recurring or VAT
        settlement categories and tax lines: a bank fee or a late payment
        surcharge on the same statement line stays in the year of payment.
        """
        if event.key[0] != event.key[1]:
            return list(event.indexes)
        main = self._main_category(event)
        result = []
        for index in event.indexes:
            one = self.slices[index]
            category = self.graph.categories.get(one.category) if one.category else None
            if (
                one.repartition_id
                or one.virtual_tax
                or (main is not None and one.category == main)
                or (
                    category is not None
                    and (
                        category.recurring_candidate
                        or category.vat_kind
                        in ("settlement", "input", "output", "input_non_deductible")
                    )
                )
            ):
                result.append(index)
        return result

    def _review(self, event, base, history):
        source, origin = self._info(event)
        related = self._related(event, source, origin)
        economic_year, how = self._economic_year(related, source, origin)
        explicit = how in ("entered", "vat_period")
        if explicit and economic_year == event.date.year:
            return
        is_tax = self._is_tax(event, related)
        due, certain = self._due_date(event, related, origin, is_tax)
        reasons, steady = self._reasons(
            event, base, history, (source, origin, due, is_tax, economic_year, how)
        )
        if not reasons:
            return
        decision = self._decisions.get(event.key)
        item = m.TenDayItem(
            kind="vat" if "tax_office" in reasons else "recurring",
            source_move_id=event.key[0],
            origin_move_id=event.key[1],
            cash_date=event.date,
            reasons=reasons,
            economic_year=economic_year,
            due_date=due,
            proposal=self._proposal(reasons, steady, economic_year, event.date.year),
            slice_indexes=tuple(event.indexes),
        )
        self.items.append(item)
        if decision is not None:
            item.decision = decision.decision
            economic_year = decision.economic_year or economic_year
            due = decision.due_date or due
            if decision.decision == "apply" and (due is None or not economic_year):
                # the rule needs the economic year and the legal due date
                # (5.6.2); a bank entry has no due date of its own
                item.economic_year, item.due_date = economic_year, due
                self.findings.append(
                    m.Finding(
                        code="C-26",
                        severity=m.BLOCKER,
                        key="ten_day_input_missing",
                        years=(base, base + 1),
                        move_ids=tuple(sorted(set(event.key))),
                        amount=event.amount,
                    )
                )
                return
            if decision.decision == "apply" and not any(
                self.window.applies(self.slices[index].date, economic_year, due)
                for index in self._shifted(event)
            ):
                # "apply" that moves nothing (E5 review): the economic year is
                # the year of payment (a derived proposal kept unchanged) or
                # the due date lies outside the window. The list would show
                # "apply" with the figures of "do not apply".
                item.economic_year, item.due_date = economic_year, due
                self.findings.append(
                    m.Finding(
                        code="C-26",
                        severity=m.BLOCKER,
                        key="ten_day_apply_without_effect",
                        years=(base, base + 1),
                        move_ids=tuple(sorted(set(event.key))),
                        amount=event.amount,
                        params={
                            "economic_year": economic_year,
                            "payment_year": event.date.year,
                            "due_date": due,
                            "window": self.window.bounds(base),
                        },
                    )
                )
                return
        elif certain and not self.window.contains(due, base):
            # the known due date lies outside the window: the rule cannot
            # apply, nothing to decide (BFH X R 2/21)
            item.decision = "auto"
            item.proposal = "reject"
            return
        elif (
            economic_year
            and certain
            and (
                (item.kind == "vat" and explicit)
                or (
                    "economic_year_entered" in reasons and (steady or "flag" in reasons)
                )
            )
        ):
            # complete input: economic year (VAT return period or entered)
            # and a certain due date in the window. An entered economic year
            # alone does not show that the payment recurs regularly (R-07):
            # a single invoice of December stays for the user to decide.
            item.decision = "auto"
        else:
            self.findings.append(
                m.Finding(
                    code="C-26",
                    severity=m.BLOCKER,
                    key="ten_day_undecided",
                    years=(base, base + 1),
                    move_ids=tuple(sorted(set(event.key))),
                    amount=event.amount,
                    params={"reasons": reasons},
                )
            )
            return
        if item.decision in ("apply", "auto") and self.window.applies(
            event.date, economic_year, due
        ):
            item.applied = True
            item.economic_year = economic_year
            item.due_date = due
            for index in self._shifted(event):
                # a payment split over statement lines of several dates
                # (C-24): only the part paid in the window moves
                if not self.window.applies(self.slices[index].date, economic_year, due):
                    continue
                self.slices[index].attribution_year = economic_year
                self.slices[index].ten_day_applied = True


def _scaled(weights, scale, total):
    """Weights scaled to ``total`` cent (largest remainders)."""
    raw = {cash: value * scale for cash, value in weights.items()}
    order = sorted(raw, key=lambda cash: (cash.date, cash.override))
    rounded = apportion({index: raw[cash] for index, cash in enumerate(order)}, total)
    return {cash: rounded[index] for index, cash in enumerate(order) if rounded[index]}


def _next_working_day(day):
    """``day``, or the Monday after it if it is a Saturday or Sunday
    (§ 108 (3) AO). The due dates of the direct debits (R-06) are always the
    10th of January, which is no public holiday in any German state."""
    if day.weekday() < 5:
        return day
    return day + datetime.timedelta(days=7 - day.weekday())


def _regular_dates(dates, day=None):
    """Whether ``dates`` (and ``day``) follow a regular rhythm of 1, 3, 6 or
    12 months."""
    ordered = sorted({*dates, day} - {None})
    if len(ordered) < (3 if day is not None else 2):
        return False
    months = [date.year * 12 + date.month for date in ordered]
    gaps = {after - before for before, after in zip(months, months[1:], strict=False)}
    return len(gaps) == 1 and gaps <= _REGULAR_GAPS
