# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Direct entries: neither money nor open items (spec 5.5.1, B-16).

Money never reaches such an entry, so each one gets a fixed rule (first
match): ``d`` depreciation and fixed assets only (compare with the asset
register), ``b`` income or expense at payment on a balance sheet account
(ignore: the payment realises it), ``a`` category realised at booking,
``c`` reclassification between income and expense accounts (realise at the
booking date), ``e`` neutral or tax accounts only (nothing), ``f``
everything else (blocker C-04). C-18 reports neutral balance sheet
accounts that carry money and income or expense of direct entries.
"""

from collections import defaultdict

from . import model as m

RULE_ASSETS = "d"
RULE_ON_PAYMENT = "b"
RULE_BOOKING = "a"
RULE_RECLASS = "c"
RULE_NEUTRAL = "e"
RULE_BLOCKED = "f"

_PROFIT_ROLES = frozenset({"be", "ba"})


def _is_tax(line, category):
    return bool(line.repartition_id) or (
        category is not None and category.role == "steuer"
    )


def _is_profit(category):
    return category is not None and category.role in _PROFIT_ROLES


def direct_rule(lines, categories, accounts):
    """Return the rule letter of a direct entry.

    ``categories``: category per line (``None`` if not assigned),
    ``accounts``: account per line.
    """
    pairs = list(zip(lines, categories, accounts, strict=True))
    if all(
        cat is not None and cat.role in ("afa", "av") and not line.repartition_id
        for line, cat, _acc in pairs
    ):
        return RULE_ASSETS
    on_payment = [
        line
        for line, cat, acc in pairs
        if _is_profit(cat) and cat.on_payment and acc.internal_group in m.BALANCE_GROUPS
    ]
    if on_payment and all(
        line in on_payment or _is_profit(cat) or _is_tax(line, cat)
        for line, cat, _acc in pairs
    ):
        return RULE_ON_PAYMENT
    booked = [
        line
        for line, cat, _acc in pairs
        if cat is not None and cat.erfassung == "B" and cat.role != "afa"
    ]
    if booked and all(
        line in booked or _is_profit(cat) or _is_tax(line, cat)
        for line, cat, _acc in pairs
    ):
        return RULE_BOOKING
    if any(_is_profit(cat) for _line, cat, _acc in pairs) and all(
        _is_profit(cat) or _is_tax(line, cat) for line, cat, _acc in pairs
    ):
        return RULE_RECLASS
    if all(
        cat is not None and cat.role == "neutral" and not line.repartition_id
        for line, cat, _acc in pairs
    ) or all(
        cat is not None and cat.role == "steuer" and not line.repartition_id
        for line, cat, _acc in pairs
    ):
        return RULE_NEUTRAL
    return RULE_BLOCKED


class DirectEntries:
    """Apply the rules of 5.5.1 to all direct entries of the graph."""

    def __init__(self, graph, flow, fields_of):
        """``fields_of(category, day)``: set of form fields of a category."""
        self.graph = graph
        self.flow = flow
        self.fields_of = fields_of
        self.findings = []
        self.asset_entries = []
        self.rules = {}
        self._years = None

    def run(self):
        for move in self.graph.moves_of_class(m.DIRECT):
            lines = self.graph.lines_of(move.id)
            categories = [self.graph.category(line, move.date) for line in lines]
            accounts = [self.graph.accounts[line.account_id] for line in lines]
            rule = direct_rule(lines, categories, accounts)
            self.rules[move.id] = rule
            if rule == RULE_ASSETS:
                self.asset_entries.append(move.id)
            elif rule == RULE_ON_PAYMENT:
                self._on_payment(move, lines, categories)
            elif rule in (RULE_BOOKING, RULE_RECLASS):
                self._book(move, lines)
                if rule == RULE_RECLASS:
                    self._check_reclass_year(move, lines)
            elif rule == RULE_BLOCKED:
                self.findings.append(
                    m.Finding(
                        code="C-04",
                        severity=m.BLOCKER,
                        key="direct_without_rule",
                        years=(move.date.year,),
                        move_ids=(move.id,),
                        amount=sum(max(line.balance, 0) for line in lines),
                    )
                )
        self._check_neutral_carriers()
        return self.findings

    def _book(self, move, lines):
        event = self.flow.new_event()
        for line in lines:
            category = self.graph.category(line, move.date)
            self.flow.slices.append(
                m.Slice(
                    kind=m.BOOKING,
                    date=move.date,
                    amount=line.balance,
                    event=event,
                    key=(line.id, 0),
                    origin_move_id=move.id,
                    origin_line_id=line.id,
                    document_move_id=move.id,
                    account_id=line.account_id,
                    partner_id=line.partner_id or move.partner_id,
                    repartition_id=line.repartition_id,
                    category=category.code if category else None,
                )
            )

    def _money_years(self):
        """Years of money realised per account (for the hint of rule c)."""
        if self._years is None:
            self._years = defaultdict(set)
            for one in self.flow.slices:
                if one.kind in m.MONEY_KINDS and one.account_id:
                    self._years[one.account_id].add(one.date.year)
        return self._years

    def _check_reclass_year(self, move, lines):
        """C-03: reclassification dated in another year than the payments."""
        years = set()
        for line in lines:
            years |= self._money_years().get(line.account_id, set())
        if years and move.date.year not in years:
            self.findings.append(
                m.Finding(
                    code="C-03",
                    severity=m.INFO,
                    key="reclassification_other_year",
                    years=(move.date.year,),
                    move_ids=(move.id,),
                    params={"payment_years": sorted(years)},
                )
            )

    def _on_payment(self, move, lines, categories):
        """Rule b: the later payment realises the expense; compare fields."""
        balance_fields, profit_fields = set(), set()
        for line, category in zip(lines, categories, strict=True):
            if not _is_profit(category):
                continue
            account = self.graph.accounts[line.account_id]
            target = (
                balance_fields
                if category.on_payment and account.internal_group in m.BALANCE_GROUPS
                else profit_fields
            )
            target |= self.fields_of(category, move.date)
        differs = bool(profit_fields) and profit_fields != balance_fields
        self.findings.append(
            m.Finding(
                code="C-03",
                severity=m.WARNING if differs else m.INFO,
                key="direct_on_payment_fields" if differs else "direct_on_payment",
                years=(move.date.year,),
                move_ids=(move.id,),
                params={
                    "fields": sorted(profit_fields),
                    "payment_fields": sorted(balance_fields),
                },
            )
        )

    def _check_neutral_carriers(self):
        """C-18: neutral balance account with money and direct P&L entries."""
        money_accounts = defaultdict(set)
        for move in self.graph.moves_of_class(m.SOURCE):
            for line in self.graph.lines_of(move.id, m.N):
                if self._neutral_balance(line, move):
                    money_accounts[line.account_id].add(move.date.year)
        for move_id, rule in sorted(self.rules.items()):
            if rule not in (RULE_BLOCKED, RULE_RECLASS, RULE_BOOKING):
                continue
            move = self.graph.moves[move_id]
            lines = self.graph.lines_of(move_id)
            has_profit = any(
                _is_profit(self.graph.category(line, move.date)) for line in lines
            )
            for line in lines:
                years = money_accounts.get(line.account_id)
                if has_profit and years and self._neutral_balance(line, move):
                    self.findings.append(
                        m.Finding(
                            code="C-18",
                            severity=m.BLOCKER,
                            key="neutral_account_with_expense",
                            years=tuple(sorted(years | {move.date.year})),
                            move_ids=(move_id,),
                            line_ids=(line.id,),
                            params={"account_id": line.account_id},
                        )
                    )

    def _neutral_balance(self, line, move):
        account = self.graph.accounts[line.account_id]
        category = self.graph.category(line, move.date)
        return (
            account.internal_group in m.BALANCE_GROUPS
            and category is not None
            and category.role == "neutral"
            and self.graph.role[line.id] == m.N
        )
