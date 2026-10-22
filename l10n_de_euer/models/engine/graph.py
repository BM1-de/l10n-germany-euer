# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Reconciliation graph: exclusions, line roles, move classes (spec 5.2, 5.3).

Builds from the extracted data the graph the money flows through:

* only posted entries count; cash basis taxes entries, sections and notes and
  zero lines are left out; partials with an excluded side do not exist
  (B-02); a partial with a line of another business is a blocker (C-47);
* reversal pairs are left out only if all their reconcilable and money lines
  are reconciled exclusively with each other and, if they move money, both
  lie in the same year (B-03);
* line roles L (money), R (open item) and N (everything else), with the
  structure rule for private accounts (B-09, K-11);
* move classes opening, source, document, transit and direct (5.2).
"""

import datetime
from collections import defaultdict

from . import model as m

#: display types that are never amounts
_NO_AMOUNT_TYPES = frozenset({"line_section", "line_subsection", "line_note"})
#: category roles that keep a private line from being a money line (5.2 b)
_BLOCKING_ROLES = frozenset({"be", "ba", "steuer", "av", "afa"})


def category_at(account, categories, on_date):
    """Return the :class:`~.model.Category` of ``account`` on ``on_date``."""
    best = None
    for assignment in account.assignments:
        if assignment.date_from and assignment.date_from > on_date:
            continue
        if assignment.date_to and assignment.date_to < on_date:
            continue
        if best is None or (assignment.date_from or on_date.min) > (
            best.date_from or on_date.min
        ):
            best = assignment
    return categories.get(best.category) if best else None


class Graph:
    """Lines, roles, classes and edges of one business."""

    def __init__(self, data):
        self.data = data
        self.settings = data.settings
        self.accounts = {account.id: account for account in data.accounts}
        self.categories = {category.code: category for category in data.categories}
        self.taxes = {tax.id: tax for tax in data.taxes}
        self.repartitions = {rep.id: rep for rep in data.repartitions}
        self.partners = {partner.id: partner for partner in data.partners}
        self.findings = []
        self.moves = {}
        self.drafts = {}
        self.lines = {}
        self.move_lines = defaultdict(list)
        self.role = {}
        self.private_lines = set()
        self.cls = {}
        self.edges = defaultdict(list)
        self.partials = {}
        self.excluded_pairs = set()
        #: lines reconciled with a line of another business (C-47)
        self.foreign_lines = set()
        self._partials_of_line = defaultdict(list)
        self._draft_line_ids = set()
        self._data_line_ids = {line.id for line in data.lines}
        self._select()
        self._exclude_reversal_pairs()
        self._assign_roles()
        self._classify()
        self._build_edges()

    # ------------------------------------------------------------ helpers
    def move_of(self, line):
        return self.moves[line.move_id]

    def category(self, line, on_date):
        """Category of the account of ``line`` on ``on_date``."""
        return category_at(self.accounts[line.account_id], self.categories, on_date)

    def lines_of(self, move_id, role=None):
        lines = self.move_lines.get(move_id, ())
        if role is None:
            return list(lines)
        return [line for line in lines if self.role[line.id] == role]

    def moves_of_class(self, cls):
        return [move for move in self.sorted_moves() if self.cls[move.id] == cls]

    def sorted_moves(self):
        return [self.moves[move_id] for move_id in sorted(self.moves)]

    def exchange_move_ids(self):
        return {
            partial.exchange_move_id
            for partial in self.partials.values()
            if partial.exchange_move_id
        }

    # ------------------------------------------------------------ selection
    def _in_business(self, move):
        companies = self.data.company_ids
        return not companies or move.company_id in companies

    def _select(self):
        """Posted entries and their lines; drafts are kept for C-13/C-14."""
        lines_by_move = defaultdict(list)
        for line in self.data.lines:
            lines_by_move[line.move_id].append(line)
        for move in sorted(self.data.moves, key=lambda move: move.id):
            if not self._in_business(move):
                continue
            if move.state == "draft":
                self.drafts[move.id] = move
                self._draft_line_ids.update(line.id for line in lines_by_move[move.id])
                continue
            if move.state != "posted" or move.caba:
                continue
            if move.revaluation:
                self.findings.append(
                    m.Finding(
                        code="C-28",
                        severity=m.WARNING,
                        key="revaluation",
                        years=(move.date.year,),
                        move_ids=(move.id,),
                    )
                )
                continue
            self.moves[move.id] = move
            for line in sorted(lines_by_move[move.id], key=lambda line: line.id):
                if line.display_type in _NO_AMOUNT_TYPES or not line.balance:
                    continue
                self.lines[line.id] = line
                self.move_lines[move.id].append(line)
        for partial in sorted(self.data.partials, key=lambda partial: partial.id):
            for line_id in (partial.debit_line_id, partial.credit_line_id):
                self._partials_of_line[line_id].append(partial)
        for move in self.drafts.values():
            self.findings.append(
                m.Finding(
                    code="C-13",
                    severity=m.WARNING,
                    key="draft_entry",
                    years=(move.date.year,),
                    move_ids=(move.id,),
                )
            )
        self._check_bank_dates()

    def _check_bank_dates(self):
        """C-12: statement entry dated differently from its bank date, or
        without a bank date where Odoo puts entries of a locked period
        (B-12)."""
        for move in self.sorted_moves():
            if move.statement and move.bank_date and move.bank_date != move.date:
                self.findings.append(
                    m.Finding(
                        code="C-12",
                        severity=m.WARNING,
                        key="bank_date_differs",
                        years=tuple(sorted({move.date.year, move.bank_date.year})),
                        move_ids=(move.id,),
                        params={"bank_date": move.bank_date, "date": move.date},
                    )
                )
            elif move.statement and not move.bank_date and move.lock_date_shift:
                last_locked = move.lock_date_shift
                if not isinstance(last_locked, datetime.date):
                    last_locked = move.date - datetime.timedelta(days=1)
                self.findings.append(
                    m.Finding(
                        code="C-12",
                        severity=m.WARNING,
                        key="lock_date_shift",
                        years=tuple(sorted({last_locked.year, move.date.year})),
                        move_ids=(move.id,),
                        params={"date": move.date},
                    )
                )

    def _tax_key(self, line):
        """Tax repartition of a line, the same for invoice and refund lines."""
        repartition = self.repartitions.get(line.repartition_id)
        if repartition is None:
            return line.repartition_id
        return (repartition.tax_id, repartition.factor_percent)

    def _reversal_pair_cancels(self, move, original):
        sums = defaultdict(lambda: [0, 0])
        for line in self.move_lines[move.id] + self.move_lines[original.id]:
            key = (line.account_id, line.partner_id, self._tax_key(line))
            key += (line.currency_id,)
            sums[key][0] += line.balance
            sums[key][1] += line.amount_currency
        return all(not balance and not currency for balance, currency in sums.values())

    def _reconciled_inside(self, move_ids):
        """Whether no partial joins a line of ``move_ids`` with another entry.

        A line of another business (not extracted at all) is outside.
        """
        for move_id in move_ids:
            for line in self.move_lines[move_id]:
                for partial in self._partials_of_line[line.id]:
                    for other_id in (partial.debit_line_id, partial.credit_line_id):
                        if other_id not in self._data_line_ids:
                            return False
                        other = self.lines.get(other_id)
                        if other is not None and other.move_id not in move_ids:
                            return False
        return True

    def _is_money_line(self, line, move):
        """Line on a money account (role L before the structure rule)."""
        account = self.accounts[line.account_id]
        category = self.category(line, move.date)
        return (
            account.account_type in m.CASH_TYPES
            or account.liquidity
            or (category is not None and category.role == "geld")
        )

    def _money_lines(self, pair):
        """Money lines of the pair (B-03)."""
        result = []
        for move_id in pair:
            move = self.moves[move_id]
            result += [
                line
                for line in self.move_lines[move_id]
                if self._is_money_line(line, move)
            ]
        return result

    def _money_unreconciled(self, pair):
        """Whether a money line of the pair is not reconciled at all.

        Money lines are normally not reconcilable; then the condition "only
        reconciled with each other" says nothing about them.
        """
        return any(
            not self._partials_of_line.get(line.id) for line in self._money_lines(pair)
        )

    @staticmethod
    def _money_year(move):
        return (move.info.cash_date or move.date).year

    def _exclude_reversal_pairs(self):
        """B-03: drop reversal pairs that are reconciled only with each other.

        If original and reversal lie in different years (cash date), a pair
        with unreconciled money lines stays: the reversal of a closed year
        in the next one changes both years like any other money movement
        (year 1 plus, year 2 minus). A money pair over the turn of the year
        whose money lines Odoo reconciled with each other (reversal wizard,
        ``cancel=True``) is left out like any other pair: no money flowed.
        This changes the earlier year, so it is reported (C-48, T19).
        """
        for move in self.sorted_moves():
            original = self.moves.get(move.reversed_entry_id)
            if original is None or {move.id, original.id} & self.excluded_pairs:
                continue
            pair = {move.id, original.id}
            if (
                (
                    self._money_year(move) == self._money_year(original)
                    or not self._money_unreconciled(pair)
                )
                and self._reversal_pair_cancels(move, original)
                and self._reconciled_inside(pair)
            ):
                self.excluded_pairs |= pair
                self._check_pair_years(move, original, pair)
        for move_id in self.excluded_pairs:
            for line in self.move_lines.pop(move_id, ()):
                del self.lines[line.id]
            del self.moves[move_id]

    def _check_pair_years(self, move, original, pair):
        """C-48: money pair over the turn of the year left out (T19)."""
        years = {self._money_year(move), self._money_year(original)}
        lines = self._money_lines(pair)
        if len(years) < 2 or not lines:
            return
        self.findings.append(
            m.Finding(
                code="C-48",
                severity=m.WARNING,
                key="reversal_pair_years",
                years=tuple(sorted(years)),
                move_ids=tuple(sorted(pair)),
                amount=sum(abs(line.balance) for line in lines) // 2,
            )
        )

    # ------------------------------------------------------------ roles
    def _base_role(self, line, category):
        account = self.accounts[line.account_id]
        if (
            account.account_type in m.CASH_TYPES
            or account.liquidity
            or (category is not None and category.role == "geld")
        ):
            return m.L
        if category is not None and category.role == "privat":
            return None
        if line.repartition_id:
            return m.N
        if account.internal_group in m.BALANCE_GROUPS and (
            account.reconcile or account.account_type in m.OPEN_ITEM_TYPES
        ):
            return m.R
        return m.N

    def _assign_roles(self):
        """Two passes per entry: first all lines but private ones (5.2).

        A private line is money (a) in an entry without open items or (b) in
        a journal entry whose open items only settle other items (payment
        substitute "1610 an 1890", also a VAT payment "1780 an 1890" or a
        refund set off against private tax "1800 an 1545"): only the lines
        next to the open items decide, never the open items themselves.
        A journal entry with income, expense or tax lines next to its open
        items is a payment substitute, too, if it settles documents (c):
        the customer pays to the private account after a discount
        ("1800 980 / 8730 20 an 1410 1.000"). In an invoice or credit note
        it is always withdrawal or contribution on payment (N): the document
        is realised with its payment, not on its date.

        A money line on an account of the category private (the default
        account of a journal "paid privately" without the mark of R-18) is
        private money as well: it counts as withdrawal or contribution.
        """
        candidates = []
        for move in self.sorted_moves():
            lines = self.move_lines[move.id]
            pending = []
            blocking = move.move_type != "entry"
            for line in lines:
                category = self.category(line, move.date)
                role = self._base_role(line, category)
                if role is None:
                    pending.append(line)
                    continue
                self.role[line.id] = role
                if (
                    role == m.L
                    and category is not None
                    and category.role == "privat"
                    and not self.accounts[line.account_id].private_money
                ):
                    self.private_lines.add(line.id)
                if role != m.R and (
                    line.repartition_id
                    or (category is not None and category.role in _BLOCKING_ROLES)
                ):
                    blocking = True
            has_open_item = any(self.role.get(line.id) == m.R for line in lines)
            private_role = m.L if not has_open_item or not blocking else m.N
            for line in pending:
                self.role[line.id] = private_role
                self.private_lines.add(line.id)
            if pending and private_role == m.N and move.move_type == "entry":
                candidates.append((move, pending))
        for move, pending in candidates:
            if self._settles_documents(move, pending):
                for line in pending:
                    self.role[line.id] = m.L

    def _settles_documents(self, move, pending):
        """5.2 (c): a journal entry pays documents with private money.

        Its private lines lie on the other side than its open items, and
        every open item is fully reconciled with open items of documents
        (or opening items): the entry settles them and leaves nothing open.
        A journal entry booked as document ("4930 / 1800 an 1600", still
        open or later paid by bank) stays withdrawal on payment.
        """
        open_items = self.lines_of(move.id, m.R)
        sides = {line.balance > 0 for line in open_items}
        if len(sides) != 1 or any((line.balance > 0) in sides for line in pending):
            return False
        for line in open_items:
            matched = 0
            for partial in self._partials_of_line[line.id]:
                other_id = (
                    partial.credit_line_id
                    if partial.debit_line_id == line.id
                    else partial.debit_line_id
                )
                other = self.lines.get(other_id)
                if other is None or other.move_id == move.id:
                    return False
                if self.role.get(other.id) != m.R or self._move_class(
                    self.moves[other.move_id]
                ) not in (m.DOC, m.OPENING):
                    return False
                matched += partial.amount
            if matched != abs(line.balance):
                return False
        return True

    # ------------------------------------------------------------ classes
    def _is_opening(self, move):
        if move.opening:
            return True
        for line in self.move_lines[move.id]:
            category = self.category(line, move.date)
            if category is not None and category.role == "eroeffnung":
                return True
        return False

    def _move_class(self, move):
        lines = self.move_lines[move.id]
        if self._is_opening(move):
            return m.OPENING
        roles = [self.role[line.id] for line in lines]
        if m.L in roles:
            return m.SOURCE
        open_items = [line for line in lines if self.role[line.id] == m.R]
        types = {self.accounts[line.account_id].account_type for line in open_items}
        if self.settings.offsetting_is_payment and m.OPEN_ITEM_TYPES <= types:
            return m.SOURCE
        if not open_items:
            return m.DIRECT
        sides = {line.balance > 0 for line in open_items}
        return m.TRANSIT if len(sides) == 2 else m.DOC

    def _classify(self):
        for move in self.sorted_moves():
            self.cls[move.id] = self._move_class(move)

    # ------------------------------------------------------------ edges
    def _build_edges(self):
        """Partials between L/R lines of different entries (both directions)."""
        for partial in sorted(self.data.partials, key=lambda partial: partial.id):
            debit = self.lines.get(partial.debit_line_id)
            credit = self.lines.get(partial.credit_line_id)
            if debit is None or credit is None:
                self._check_draft_partial(partial, debit or credit)
                self._check_foreign_partial(partial, debit or credit)
                continue
            if debit.move_id == credit.move_id:
                continue
            if self.role[debit.id] == m.N or self.role[credit.id] == m.N:
                continue
            self.partials[partial.id] = partial
            self.edges[debit.id].append((partial, credit))
            self.edges[credit.id].append((partial, debit))

    def _check_foreign_partial(self, partial, kept):
        """C-47: partial with a line of another business (another company of
        the same main company). Odoo allows it, but the money and the
        document belong to two different EÜR."""
        if kept is None:
            return
        other_id = (
            partial.credit_line_id
            if kept.id == partial.debit_line_id
            else partial.debit_line_id
        )
        if other_id in self._data_line_ids:
            return
        self.foreign_lines.add(kept.id)
        self.findings.append(
            m.Finding(
                code="C-47",
                severity=m.BLOCKER,
                key="partial_other_business",
                years=(partial.max_date.year,),
                move_ids=(kept.move_id,),
                line_ids=(kept.id,),
                amount=partial.amount,
                params={"other_line_id": other_id},
            )
        )

    def _check_draft_partial(self, partial, kept):
        """C-14: partial whose other side is a draft entry."""
        if kept is None:
            return
        other_id = (
            partial.credit_line_id
            if kept.id == partial.debit_line_id
            else partial.debit_line_id
        )
        if other_id in self._draft_line_ids:
            self.findings.append(
                m.Finding(
                    code="C-14",
                    severity=m.WARNING,
                    key="partial_with_draft",
                    years=(partial.max_date.year,),
                    move_ids=(kept.move_id,),
                    line_ids=(kept.id, other_id),
                    amount=partial.amount,
                )
            )
