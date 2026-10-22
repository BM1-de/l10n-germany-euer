# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import datetime
from collections import defaultdict

from odoo import api, fields, models
from odoo.fields import Domain

from .euer_account_assignment import MONEY_ACCOUNT_TYPES

INCOME_EXPENSE = ("income", "expense")
EXPENSE_TYPES = (
    "expense",
    "expense_other",
    "expense_depreciation",
    "expense_direct_cost",
)
NON_DEDUCTIBLE_TYPES = ("non_deductible_product_total", "non_deductible_tax")
#: Standard accounts of the chart templates with 19 % tax (spec 4.3 no. 5)
STANDARD_ACCOUNTS = {
    "de_skr03": ("account_3400", "account_8400"),
    "de_skr04": ("chart_skr04_5400", "chart_skr04_4400"),
}
#: Tax accounts a small business should not move (C-30); import VAT paid to
#: customs is not deductible either (part of the expense or acquisition cost)
KU_VAT_KINDS = ("input", "output", "input_non_deductible", "import")


def _empty_usage():
    return {"balance": 0.0, "debit": 0.0, "credit": 0.0, "count": 0, "moves": set()}


class L10nDeEuerAssignmentChecker(models.AbstractModel):
    """Checks of the account assignment that need no cash basis calculation.

    C-01 to C-09, C-30, C-31 and C-50 of spec 7.8 (4.6). Each check returns a
    list of results ``{code, severity, message, res_model, res_ids, amount}``.
    Lines are read for the whole business (main company and branches).
    """

    _name = "l10n_de_euer.assignment.checker"
    _description = "EÜR Account Assignment Checks"

    @api.model
    def _run(self, company, year):
        context = self._prepare(company, year)
        results = []
        for check in (
            self._check_c01,
            self._check_c02,
            self._check_c05,
            self._check_c06,
            self._check_c07,
            self._check_c08,
            self._check_c09,
            self._check_c30,
            self._check_c31,
            self._check_c50,
        ):
            results += check(context)
        return results

    # ------------------------------------------------------------ data
    @api.model
    def _prepare(self, company, year):
        """Posted lines of the year per account and assignment period."""
        root = company.root_id
        companies = self.env["res.company"].sudo().search([("id", "child_of", root.id)])
        date_from = datetime.date(year, 1, 1)
        date_to = datetime.date(year, 12, 31)
        lines = Domain("company_id", "in", companies.ids) & Domain(
            "parent_state", "=", "posted"
        )
        context = {
            "root": root,
            "companies": companies,
            "year": year,
            "date_from": date_from,
            "date_to": date_to,
            "lines": lines,
            "year_lines": lines
            & Domain("date", ">=", date_from)
            & Domain("date", "<=", date_to),
        }
        context["segments"] = self._segments(context, context["year_lines"])
        return context

    def _aml(self, context):
        return self.env["account.move.line"].sudo().with_company(context["root"])

    def _segments(self, context, domain, with_moves=False):
        """Return ``{account: {assignment: usage}}`` for the lines of ``domain``.

        The assignment is the one valid on the date of each line; an empty
        recordset stands for lines without assignment.
        """
        groupby = ["account_id", "date:day"] + (["move_id"] if with_moves else [])
        groups = self._aml(context)._read_group(
            domain, groupby, ["debit:sum", "credit:sum", "balance:sum", "__count"]
        )
        accounts = self.env["account.account"].browse({group[0].id for group in groups})
        periods = self._assignments(context, accounts)
        empty = self.env["l10n_de_euer.account.assignment"]
        result = defaultdict(lambda: defaultdict(_empty_usage))
        for group in groups:
            account, day = group[0], fields.Date.to_date(group[1])
            debit, credit, balance, count = group[-4:]
            assignment = next(
                (
                    item
                    for item in periods.get(account.id, ())
                    if (not item.date_from or item.date_from <= day)
                    and (not item.date_to or item.date_to >= day)
                ),
                empty,
            )
            usage = result[account.with_company(context["root"])][assignment]
            usage["debit"] += debit
            usage["credit"] += credit
            usage["balance"] += balance
            usage["count"] += count
            if with_moves:
                usage["moves"].add(group[2].id)
        return result

    def _assignments(self, context, accounts):
        assignments = (
            self.env["l10n_de_euer.account.assignment"]
            .sudo()
            .search(
                Domain("company_id", "=", context["root"].id)
                & Domain("account_id", "in", accounts.ids)
            )
        )
        result = defaultdict(list)
        for assignment in assignments:
            result[assignment.account_id.id].append(assignment)
        return result

    def _result(self, code, severity, message, records, amount=0.0):
        return {
            "code": code,
            "severity": severity,
            "message": message,
            "res_model": records._name,
            "res_ids": sorted(records.ids),
            "amount": amount,
        }

    def _unassigned(self, segments):
        empty = self.env["l10n_de_euer.account.assignment"]
        for account, usages in segments.items():
            usage = usages.get(empty)
            if usage and usage["count"]:
                yield account, usage

    def _proposals(self, context):
        """Proposals for all accounts without assignment, once per run (K-03)."""
        if "proposals" not in context:
            accounts = self.env["account.account"].concat(
                *(account for account, _usage in self._unassigned(context["segments"]))
            )
            context["proposals"] = self.env[
                "l10n_de_euer.account.assignment"
            ]._get_proposals(context["root"], accounts)
        return context["proposals"]

    def _proposal_text(self, context, account):
        proposals = self._proposals(context)
        if account not in proposals:
            return ""
        return " " + self.env._(
            "Proposal: %(category)s.", category=proposals[account].display_name
        )

    # ------------------------------------------------------------ C-01, C-02
    def _check_c01(self, context):
        """Income or expense account with bookings and without category."""
        results = []
        for account, usage in self._unassigned(context["segments"]):
            if account.internal_group not in INCOME_EXPENSE:
                continue
            message = self.env._(
                "Income or expense account %(account)s has %(count)s posted lines "
                "in %(year)s without EÜR category.",
                account=account.display_name,
                count=usage["count"],
                year=context["year"],
            ) + self._proposal_text(context, account)
            results.append(
                self._result("C-01", "blocker", message, account, usage["balance"])
            )
        return results

    def _money_accounts(self, context):
        Account = self.env["account.account"].sudo()
        journals = (
            self.env["account.journal"]
            .sudo()
            .search(
                Domain("company_id", "in", context["companies"].ids)
                & Domain("type", "in", ("bank", "cash", "credit"))
            )
        )
        assigned = (
            self.env["l10n_de_euer.account.assignment"]
            .sudo()
            .search(
                Domain("company_id", "=", context["root"].id)
                & Domain("role", "=", "geld")
            )
        )
        typed = Account.search(
            Domain("company_ids", "in", context["companies"].ids)
            & Domain("account_type", "in", MONEY_ACCOUNT_TYPES)
        )
        return typed | journals.default_account_id | assigned.account_id

    def _check_c02(self, context):
        """Counter account of a money movement without category."""
        money = self._money_accounts(context)
        domain = (
            context["year_lines"]
            & Domain("move_id.line_ids.account_id", "in", money.ids)
            & Domain("account_id", "not in", money.ids)
        )
        results = []
        for account, usage in self._unassigned(self._segments(context, domain)):
            if account.internal_group in INCOME_EXPENSE:
                continue  # reported by C-01
            message = self.env._(
                "Account %(account)s is the counter account of %(count)s lines of "
                "money movements in %(year)s and has no EÜR category.",
                account=account.display_name,
                count=usage["count"],
                year=context["year"],
            ) + self._proposal_text(context, account)
            results.append(
                self._result("C-02", "blocker", message, account, usage["balance"])
            )
        return results

    # ------------------------------------------------------------ C-05 to C-09
    def _check_c05(self, context):
        """Account of a category the EÜR does not provide for, or manual only."""
        results = []
        for account, usages in context["segments"].items():
            for assignment, usage in usages.items():
                category = assignment.category_id
                if not category or not usage["count"]:
                    continue
                if category.role == "warn" or category.warning:
                    reason = dict(
                        category._fields["warning"]._description_selection(self.env)
                    ).get(category.warning or "other")
                    message = self.env._(
                        "%(account)s (%(category)s) has %(count)s posted lines in "
                        "%(year)s: %(reason)s.",
                        account=account.display_name,
                        category=category.display_name,
                        count=usage["count"],
                        year=context["year"],
                        reason=reason,
                    )
                elif category.erfassung == "M":
                    message = self.env._(
                        "%(account)s (%(category)s) has %(count)s posted lines in "
                        "%(year)s. This value is entered as profit correction, not "
                        "taken from bookings.",
                        account=account.display_name,
                        category=category.display_name,
                        count=usage["count"],
                        year=context["year"],
                    )
                else:
                    continue
                results.append(
                    self._result("C-05", "warning", message, account, usage["balance"])
                )
        return results

    def _interim_accounts(self, context):
        Assignment = self.env["l10n_de_euer.account.assignment"].sudo()
        date_to = context["date_to"]
        assignments = Assignment.search(
            Domain("company_id", "=", context["root"].id)
            & (Domain("date_from", "=", False) | Domain("date_from", "<=", date_to))
            & (Domain("date_to", "=", False) | Domain("date_to", ">=", date_to))
            & (
                Domain("category_id.interim", "=", True)
                | Domain("role", "=", "ungeklaert")
            )
        )
        structural = Assignment._get_structural_defaults(context["root"])
        accounts = assignments.account_id
        for account, code in structural.items():
            if code in ("SUSPENSE", "TRANSIT"):
                accounts |= account
        return accounts

    def _check_c06(self, context):
        """Interim, transit or suspense account not zero at the end of the year."""
        accounts = self._interim_accounts(context)
        groups = self._aml(context)._read_group(
            context["lines"]
            & Domain("date", "<=", context["date_to"])
            & Domain("account_id", "in", accounts.ids),
            ["account_id"],
            ["balance:sum"],
        )
        currency = context["root"].currency_id
        results = []
        for account, balance in groups:
            if currency.is_zero(balance):
                continue
            account = account.with_company(context["root"])
            message = self.env._(
                "Interim account %(account)s has a balance of %(amount)s on "
                "31.12.%(year)s; money movements may be missing.",
                account=account.display_name,
                amount=f"{balance:.2f}",
                year=context["year"],
            )
            results.append(self._result("C-06", "warning", message, account, balance))
        return results

    def _check_c07(self, context):
        """Assignment with low confidence used and not confirmed."""
        results = []
        for account, usages in context["segments"].items():
            for assignment, usage in usages.items():
                if not assignment.needs_confirmation or not usage["count"]:
                    continue
                message = self.env._(
                    "The EÜR category %(category)s of %(account)s has low confidence "
                    "and is not confirmed; confirm or change it.",
                    category=assignment.category_id.display_name,
                    account=account.display_name,
                )
                if assignment.ambiguity_case:
                    message += " " + self.env._(
                        "Ambiguous case %(case)s.", case=assignment.ambiguity_case
                    )
                results.append(
                    self._result(
                        "C-07", "blocker", message, assignment, usage["balance"]
                    )
                )
        return results

    def _check_c08(self, context):
        """Income or expense account that allows reconciliation."""
        results = []
        for account, usages in context["segments"].items():
            if account.internal_group not in INCOME_EXPENSE or not account.reconcile:
                continue
            balance = sum(usage["balance"] for usage in usages.values())
            message = self.env._(
                "Reconciliation is allowed on income or expense account %(account)s. "
                "Its lines are treated as income or expense, not as open items.",
                account=account.display_name,
            )
            results.append(self._result("C-08", "warning", message, account, balance))
        return results

    def _check_c09(self, context):
        """Private share of mixed expenses on a fallback account."""
        domain = context["year_lines"] & Domain(
            "display_type", "in", NON_DEDUCTIBLE_TYPES
        )
        results = []
        for account, usages in self._segments(context, domain, with_moves=True).items():
            moves = set()
            balance = 0.0
            for assignment, usage in usages.items():
                if assignment.role != "privat":
                    moves |= usage["moves"]
                    balance += usage["balance"]
            if not moves:
                continue
            message = self.env._(
                "The private share of mixed expenses is booked on %(account)s, which "
                "is not a private account. Set the private share account of the "
                "purchase journal.",
                account=account.display_name,
            )
            records = self.env["account.move"].browse(moves)
            results.append(self._result("C-09", "warning", message, records, balance))
        return results

    # ------------------------------------------------------------ C-30, C-31
    def _ku_periods(self, context):
        return (
            self.env["l10n_de_euer.vat.period"]
            .sudo()
            .search(
                Domain("company_id", "=", context["root"].id)
                & Domain("regime", "=", "ku")
                & Domain("date_from", "<=", context["date_to"])
                & (
                    Domain("date_to", "=", False)
                    | Domain("date_to", ">=", context["date_from"])
                )
            )
        )

    @api.model
    def _regime_date(self, move):
        """Date that decides the VAT treatment of a document (R-25)."""
        return (
            move.taxable_supply_date
            or move.delivery_date
            or move.invoice_date
            or move.date
        )

    def _in_periods(self, periods, move):
        day = self._regime_date(move)
        return any(
            period.date_from <= day and (not period.date_to or day <= period.date_to)
            for period in periods
        )

    @api.model
    def _is_reverse_charge(self, repartition_line):
        """Tax with tax and counter tax for the same document type (R-22)."""
        lines = repartition_line.tax_id.repartition_line_ids.filtered(
            lambda line: line.document_type == repartition_line.document_type
            and line.repartition_type == "tax"
        )
        factors = lines.mapped("factor_percent")
        return any(f < 0 for f in factors) and any(f > 0 for f in factors)

    def _standard_accounts(self, context):
        root = context["root"]
        accounts = root.expense_account_id | root.income_account_id
        for name in STANDARD_ACCOUNTS.get(root.chart_template, ()):
            accounts |= (
                self.env.ref(f"account.{root.id}_{name}", raise_if_not_found=False)
                or self.env["account.account"]
            )
        return accounts

    def _ku_vat_lines(self, context, periods):
        """Lines a small business should not book (C-30), per account."""
        AML = self._aml(context)
        tax_accounts = (
            self.env["l10n_de_euer.account.assignment"]
            .sudo()
            .search(
                Domain("company_id", "=", context["root"].id)
                & Domain("category_id.vat_kind", "in", KU_VAT_KINDS)
            )
            .account_id
        )
        input_vat = AML.search(
            context["year_lines"]
            & Domain("tax_repartition_line_id", "!=", False)
            & Domain("tax_line_id.type_tax_use", "=", "purchase")
        ).filtered(
            lambda line: not self._is_reverse_charge(line.tax_repartition_line_id)
        )
        manual = AML.search(
            context["year_lines"]
            & Domain("tax_repartition_line_id", "=", False)
            & Domain("account_id", "in", tax_accounts.ids)
        )
        standard = AML.search(
            context["year_lines"]
            & Domain("account_id", "in", self._standard_accounts(context).ids)
        )
        found = {"vat": defaultdict(AML.browse), "standard": defaultdict(AML.browse)}
        for kind, lines in (("vat", input_vat | manual), ("standard", standard)):
            for line in lines:
                if self._in_periods(periods, line.move_id):
                    found[kind][line.account_id] |= line
        return found

    def _check_c30(self, context):
        """Small business books VAT or on the standard 19 % accounts."""
        periods = self._ku_periods(context)
        if not periods:
            return []
        found = self._ku_vat_lines(context, periods)
        results = []
        for kind, per_account in found.items():
            for account, lines in per_account.items():
                account = account.with_company(context["root"])
                if kind == "vat":
                    message = self.env._(
                        "Small business: %(count)s lines in %(year)s on tax account "
                        "%(account)s. Input VAT is not deductible; it is treated as "
                        "part of the expense or of the acquisition cost.",
                        count=len(lines),
                        year=context["year"],
                        account=account.display_name,
                    )
                else:
                    message = self.env._(
                        "Small business: %(count)s lines in %(year)s on the standard "
                        "account %(account)s of the chart template, which carries "
                        "19 %% VAT. Use accounts without VAT.",
                        count=len(lines),
                        year=context["year"],
                        account=account.display_name,
                    )
                results.append(
                    self._result(
                        "C-30",
                        "warning",
                        message,
                        lines.move_id,
                        sum(lines.mapped("balance")),
                    )
                )
        return results

    def _check_c31(self, context):
        """Small business invoice with VAT shown (§ 14c UStG)."""
        periods = self._ku_periods(context)
        if not periods:
            return []
        lines = self._aml(context).search(
            context["year_lines"]
            & Domain("tax_repartition_line_id", "!=", False)
            & Domain("tax_line_id.type_tax_use", "=", "sale")
        )
        per_move = defaultdict(float)
        for line in lines:
            if self._is_reverse_charge(line.tax_repartition_line_id):
                continue
            if self._in_periods(periods, line.move_id):
                per_move[line.move_id] += -line.balance
        results = []
        for move, amount in per_move.items():
            if context["root"].currency_id.is_zero(amount):
                continue
            message = self.env._(
                "Small business document %(move)s shows %(amount)s VAT (§ 14c UStG). "
                "The VAT stays in Kz 111; its payment to the tax office is Kz 186.",
                move=move.display_name,
                amount=f"{amount:.2f}",
            )
            results.append(self._result("C-31", "warning", message, move, amount))
        return results

    # ------------------------------------------------------------ C-50
    def _separate_kinds(self, context):
        """Account → kind (gift, entertainment) of assignments in the year."""
        assignments = (
            self.env["l10n_de_euer.account.assignment"]
            .sudo()
            .search(
                Domain("company_id", "=", context["root"].id)
                & Domain("category_id.separate_record", "!=", False)
                & (
                    Domain("date_from", "=", False)
                    | Domain("date_from", "<=", context["date_to"])
                )
                & (
                    Domain("date_to", "=", False)
                    | Domain("date_to", ">=", context["date_from"])
                )
            )
        )
        return {
            assignment.account_id: assignment.category_id.separate_record
            for assignment in assignments
        }

    def _general_accounts(self, context):
        """Default expense accounts of the business: they collect other costs."""
        root = context["root"]
        journals = (
            self.env["account.journal"]
            .sudo()
            .search(
                Domain("company_id", "in", context["companies"].ids)
                & Domain("type", "=", "purchase")
            )
        )
        accounts = root.expense_account_id | journals.default_account_id
        for company in context["companies"]:
            default = (
                self.env["ir.default"]
                .sudo()
                ._get(
                    "product.category",
                    "property_account_expense_categ_id",
                    company_id=company.id,
                )
            )
            if default:
                accounts |= self.env["account.account"].browse(default)
        return accounts

    def _check_c50(self, context):
        """§ 4 (7) EStG: gifts and entertainment on separate accounts only."""
        kinds = self._separate_kinds(context)
        if not kinds:
            return []
        results = []
        for account in self._general_accounts(context) & self.env[
            "account.account"
        ].concat(*kinds):
            account = account.with_company(context["root"])
            message = self.env._(
                "%(account)s is an account for gifts or entertainment and at the same "
                "time a default expense account, so other expenses are booked on it "
                "(§ 4 (7) EStG).",
                account=account.display_name,
            )
            results.append(self._result("C-50", "warning", message, account))
        return results + self._check_c50_products(context, kinds)

    def _check_c50_products(self, context, kinds):
        """Products of one kind booked on an account of another kind."""
        groups = self._aml(context)._read_group(
            context["year_lines"]
            & Domain("display_type", "=", "product")
            & Domain("product_id", "!=", False)
            & Domain("account_id.account_type", "in", EXPENSE_TYPES),
            ["account_id", "product_id", "move_id"],
            ["balance:sum"],
        )
        product_kinds = {}
        mixed = defaultdict(lambda: [set(), 0.0])
        for account, product, move, balance in groups:
            if product not in product_kinds:
                expense = product.product_tmpl_id.with_company(
                    context["root"]
                )._get_product_accounts()["expense"]
                product_kinds[product] = kinds.get(expense)
            if product_kinds[product] == kinds.get(account):
                continue
            mixed[account][0].add(move.id)
            mixed[account][1] += balance
        results = []
        for account, (moves, balance) in mixed.items():
            account = account.with_company(context["root"])
            if kinds.get(account):
                message = self.env._(
                    "Other expenses are booked on the account for gifts or "
                    "entertainment %(account)s (§ 4 (7) EStG).",
                    account=account.display_name,
                )
            else:
                message = self.env._(
                    "Gifts or entertainment are booked on the general account "
                    "%(account)s instead of a separate account (§ 4 (7) EStG).",
                    account=account.display_name,
                )
            records = self.env["account.move"].browse(moves)
            results.append(self._result("C-50", "warning", message, records, balance))
        return results
