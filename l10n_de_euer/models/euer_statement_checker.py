# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import datetime
from collections import defaultdict
from decimal import Decimal

from odoo import api, fields, models
from odoo.fields import Domain
from odoo.tools import format_amount

from .engine import adjustment as adj
from .engine import scope as scope_engine
from .engine.parameters import Parameters
from .euer_scope_answer import question_labels
from .euer_statement import MANDATORY_HEADER

#: groups of Anlage AVEÜR that hold land and buildings (C-43)
LAND_GROUPS = ("land", "building", "land_rights", "office_land", "office_building")
#: disposals that Kz 15.120 asks for: sale and withdrawal, not scrapping or
#: loss (e.g. demolition)
LAND_DISPOSAL_KINDS = ("sale", "withdrawal")


class L10nDeEuerCheck(models.Model):
    _inherit = "l10n_de_euer.check"  # pylint: disable=consider-merging-classes-inherited

    origin = fields.Selection(
        selection_add=[("statement", "Statement")],
        ondelete={"statement": "cascade"},
    )


class L10nDeEuerStatementChecker(models.AbstractModel):
    """Checks of the statement itself (spec 7.8).

    F-08 (mandatory header values), C-38 (bank reconciliation of 31
    December), C-40 (questionnaire, and bookings or settings outside the
    program scope: fiscal year that deviates from the calendar year, income
    from agriculture and forestry, accounts of categories that are not
    supported), C-43, C-44, C-45 (questionnaire), C-55 and C-56 (flat rates),
    C-70 (lock dates) and C-72 (form years).
    """

    _name = "l10n_de_euer.statement.checker"
    _description = "EÜR Statement Checks"

    @api.model
    def _run(self, statement):
        values = statement._get_values() if statement.run_id else {}
        results = []
        results += self._check_header(statement)
        results += self._check_scope(statement)
        results += self._check_scope_bookings(statement)
        results += self._check_c38(statement)
        results += self._check_c70(statement)
        results += self._check_plausibility(statement, values)
        results += statement.form_ids._finalization_issues(statement.provisional_form)
        return results

    def _result(self, code, severity, message, records=None, amount=0.0):
        return {
            "code": code,
            "severity": severity,
            "message": message,
            "res_model": records._name if records else False,
            "res_ids": sorted(records.ids) if records else [],
            "amount": float(amount),
        }

    # ------------------------------------------------------------ header
    def _check_header(self, statement):
        """F-08: the mandatory header values of Anlage EÜR."""
        headers = statement._get_header_values()
        fields_ = {
            field.code: field
            for field in statement.form_ids.field_ids
            if field.code in MANDATORY_HEADER
        }
        missing = [
            f"{fields_[code].name} (Kz {fields_[code].kz})"
            if fields_[code].kz
            else fields_[code].name
            for code in MANDATORY_HEADER
            if code in fields_ and not headers.get(code)
        ]
        if not missing:
            return []
        return [
            self._result(
                "F-08",
                "blocker",
                self.env._(
                    "Mandatory header values of Anlage EÜR are missing in the "
                    "settings of %(company)s: %(fields)s.",
                    company=statement.company_id.sudo().root_id.display_name,
                    fields=", ".join(missing),
                ),
                statement.company_id.sudo().root_id,
            )
        ]

    # ------------------------------------------------------------ scope
    def _check_scope(self, statement):
        """C-40, C-43, C-44, C-45 from the answers of the questionnaire."""
        answers = statement.scope_answer_ids
        findings = scope_engine.evaluate(
            [answer._get_engine_answer() for answer in answers],
            tax_free_correction=bool(
                statement.adjustment_ids.filtered(
                    lambda adjustment: adjustment.kind == adj.TAX_FREE
                    and adjustment.result_amount
                )
            ),
            land_disposal=self._has_land_disposal(statement),
            joint_owners=statement.company_id.sudo().root_id.l10n_de_euer_owner == "3",
        )
        labels = question_labels(self.env)
        results = []
        for finding in findings:
            records = answers.filtered(
                lambda answer, f=finding: answer.question_code in f.questions
            )
            questions = "; ".join(labels[code] for code in finding.questions)
            if finding.key == "not_confirmed":
                message = self.env._(
                    "The questionnaire on the program scope is not answered and "
                    "confirmed by a manager: %(questions)s",
                    questions=questions,
                )
            elif finding.key == "out_of_scope":
                message = self.env._(
                    "Questionnaire: the case is outside the program scope of the "
                    "module: %(questions)s",
                    questions=questions,
                )
            elif finding.key == "land_without_disposal":
                message = self.env._(
                    "Questionnaire: land withdrawn or sold (Kz 120 = 1), but the "
                    "asset register has no sale or withdrawal of land or "
                    "buildings in %(year)s.",
                    year=statement.year,
                )
            elif finding.key == "disposal_without_land":
                message = self.env._(
                    "Questionnaire: no land withdrawn or sold (Kz 120 = 2), but the "
                    "asset register has a sale or withdrawal of land or buildings "
                    "in %(year)s. Kz 120 is a mandatory statement: check the "
                    "answer.",
                    year=statement.year,
                )
            elif finding.key == "correction_without_tax_free":
                message = self.env._(
                    "Questionnaire: no tax-free income or § 3c expenses, but the "
                    "statement has a profit correction of Kz 240 to 245: check the "
                    "answer."
                )
            elif finding.key == "joint_owners":
                message = self.env._(
                    "Questionnaire: sole proprietorship, but the business belongs "
                    "to both spouses (Kz 104 = 3). A business run by spouses "
                    "together is usually a partnership, which is outside the "
                    "program scope: check the owner and the answer."
                )
            else:
                message = self.env._(
                    "Questionnaire: tax-free income or § 3c expenses, but no "
                    "profit correction of Kz 240 to 245."
                )
            results.append(
                self._result(finding.code, finding.severity, message, records)
            )
        return results

    def _has_land_disposal(self, statement):
        return bool(
            self.env["l10n_de_euer.asset"]
            .sudo()
            .search_count(
                Domain("company_id", "=", statement.company_id.id)
                & Domain("aveuer_group", "in", LAND_GROUPS)
                & Domain("state", "=", "disposed")
                & Domain("disposal_kind", "in", LAND_DISPOSAL_KINDS)
                & Domain("disposal_date", ">=", datetime.date(statement.year, 1, 1))
                & Domain("disposal_date", "<=", datetime.date(statement.year, 12, 31)),
                limit=1,
            )
        )

    def _check_scope_bookings(self, statement):
        """C-40 from the books and settings (R-52 b), independent of the
        questionnaire: a fiscal year that deviates from the calendar year,
        income from agriculture and forestry (Kz 103 = 1), and bookings of
        the year on accounts of categories outside the program scope."""
        results = []
        business = statement.company_id.sudo()
        if business.fiscalyear_last_month != "12" or business.fiscalyear_last_day != 31:
            results.append(
                self._result(
                    "C-40",
                    "blocker",
                    self.env._(
                        "The fiscal year of %(company)s ends on %(day)s.%(month)s.: "
                        "a fiscal year that deviates from the calendar year is "
                        "outside the program scope (§ 4a EStG).",
                        company=business.display_name,
                        day=business.fiscalyear_last_day,
                        month=business.fiscalyear_last_month,
                    ),
                    statement.company_id,
                )
            )
        if business.root_id.l10n_de_euer_income_type == "1":
            results.append(
                self._result(
                    "C-40",
                    "blocker",
                    self.env._(
                        "%(company)s has income from agriculture and forestry "
                        "(Kz 103 = 1): Anlage LuF is outside the program scope.",
                        company=business.root_id.display_name,
                    ),
                    business.root_id,
                )
            )
        checker = self.env["l10n_de_euer.assignment.checker"]
        companies = business._l10n_de_euer_business_companies().sudo()
        context = {"root": business.root_id}
        domain = (
            Domain("company_id", "in", companies.ids)
            & Domain("parent_state", "=", "posted")
            & Domain("date", ">=", datetime.date(statement.year, 1, 1))
            & Domain("date", "<=", datetime.date(statement.year, 12, 31))
        )
        blocked = defaultdict(lambda: self.env["account.account"])
        for account, usages in checker._segments(context, domain).items():
            for assignment, usage in usages.items():
                category = assignment.category_id
                if usage["count"] and category.scope_status == "blocked":
                    blocked[category] |= account
        reasons = dict(
            self.env["l10n_de_euer.category"]
            ._fields["scope_reason"]
            ._description_selection(self.env)
        )
        for category, accounts in sorted(
            blocked.items(), key=lambda item: item[0].code
        ):
            results.append(
                self._result(
                    "C-40",
                    "blocker",
                    self.env._(
                        "%(accounts)s (%(category)s) has bookings in %(year)s: "
                        "%(reason)s is outside the program scope of the module.",
                        accounts=", ".join(accounts.mapped("display_name")),
                        category=category.display_name,
                        year=statement.year,
                        reason=reasons.get(category.scope_reason)
                        or category.display_name,
                    ),
                    accounts,
                )
            )
        return results

    # ------------------------------------------------------------ C-38
    def _check_c38(self, statement):
        """Bank reconciliation of 31 December missing or different (G-17)."""
        results = []
        # the balance in Odoo is not stored: read the bookings again
        statement.cash_balance_ids.invalidate_recordset(["balance_odoo", "difference"])
        balances = {
            balance.journal_id: balance for balance in statement.cash_balance_ids
        }
        for journal in statement._get_money_journals():
            balance = balances.get(journal)
            if balance and balance._is_reconciled():
                continue
            odoo = balance.balance_odoo if balance else 0.0
            currency = balance.currency_id if balance else journal.currency_id
            currency = currency or statement.company_id.currency_id
            if not balance or not balance.entered_on:
                if not balance:
                    odoo = (
                        self.env["l10n_de_euer.cash.balance"]
                        .new({"statement_id": statement.id, "journal_id": journal.id})
                        ._get_odoo_balance()
                    )
                message = self.env._(
                    "Bank reconciliation of 31 December %(year)s: no balance per "
                    "statement of account entered for %(journal)s (balance in Odoo "
                    "%(odoo)s).",
                    year=statement.year,
                    journal=journal.display_name,
                    odoo=format_amount(self.env, odoo, currency),
                )
                amount = odoo
            else:
                message = self.env._(
                    "Bank reconciliation of 31 December %(year)s: the balance per "
                    "statement of account of %(journal)s (%(statement)s) differs "
                    "from the balance in Odoo (%(odoo)s) by %(difference)s.",
                    year=statement.year,
                    journal=journal.display_name,
                    statement=format_amount(
                        self.env, balance.balance_statement, currency
                    ),
                    odoo=format_amount(self.env, odoo, currency),
                    difference=format_amount(self.env, balance.difference, currency),
                )
                amount = balance.difference
            results.append(
                self._result(
                    "C-38",
                    "blocker",
                    message,
                    balance or journal,
                    amount,
                )
            )
        return results

    # ------------------------------------------------------------ C-70
    def _check_c70(self, statement):
        """Lock dates of the business at the end of the year (G-05).

        Neither the global nor the hard lock date covers 31 December:
        blocker for the finalisation. Only the hard lock date covers it: a
        warning, because no exception can open the year for December lines
        posted late.
        """
        end = datetime.date(statement.year, 12, 31)
        companies = statement.company_id._l10n_de_euer_business_companies().sudo()
        open_companies, hard_only = [], []
        for company in companies.filtered("active"):
            chain = company.parent_ids
            fiscal = max(
                filter(None, chain.mapped("fiscalyear_lock_date")), default=None
            )
            hard = max(filter(None, chain.mapped("hard_lock_date")), default=None)
            fiscal_ok = bool(fiscal and fiscal >= end)
            hard_ok = bool(hard and hard >= end)
            if not fiscal_ok and not hard_ok:
                open_companies.append(company)
            elif hard_ok and not fiscal_ok:
                hard_only.append(company)
        results = []
        if open_companies:
            records = self.env["res.company"].concat(*open_companies)
            results.append(
                self._result(
                    "C-70",
                    "blocker",
                    self.env._(
                        "%(companies)s: neither the global lock date nor the hard "
                        "lock date covers 31 December %(year)s. Set the global lock "
                        "date before finalising the statement.",
                        companies=", ".join(records.mapped("display_name")),
                        year=statement.year,
                    ),
                    records,
                )
            )
        if hard_only:
            records = self.env["res.company"].concat(*hard_only)
            results.append(
                self._result(
                    "C-70",
                    "warning",
                    self.env._(
                        "%(companies)s: only the hard lock date covers 31 December "
                        "%(year)s. December lines posted late are moved to the next "
                        "year without exception; the global lock date is "
                        "recommended.",
                        companies=", ".join(records.mapped("display_name")),
                        year=statement.year,
                    ),
                    records,
                )
            )
        return results

    # ------------------------------------------------------------ C-55, C-56
    def _get_room_months(self, statement, values):
        """Months in which a home office is deducted (Kz 172): from the
        profit corrections of the home office as centre of all activities; a
        value of Kz 172 from elsewhere (asset register) counts for the year."""
        rooms = statement.adjustment_ids.filtered(
            lambda adjustment: adjustment.kind == adj.HOME_OFFICE_ROOM
            and adjustment.center_of_activity
            and adjustment.result_amount
        )
        from_rooms = sum(
            Decimal(str(value or 0))
            for value in rooms.mapped(
                lambda adjustment: (adjustment.field_amounts or {}).get(
                    adj.FIELD_HOME_OFFICE
                )
            )
        )
        if not rooms or (values.get(adj.FIELD_HOME_OFFICE) or 0) > from_rooms:
            return 12
        return min(sum(rooms.mapped("months")), 12)

    def _get_tax_free_limit(self, statement):
        """Maximum of Kz 240 in the year: § 3 no. 26 and no. 26a EStG (no.
        26b shares the limit of no. 26); ``None`` without parameters."""
        params = Parameters(self.env["l10n_de_euer.adapter"]._parameters())
        trainer = params.get("tax_free_trainer", statement.year)
        volunteer = params.get("tax_free_volunteer", statement.year)
        if trainer is None or volunteer is None:
            return None
        return Decimal(str(trainer)) + Decimal(str(volunteer))

    def _check_plausibility(self, statement, values):
        """C-55: daily home office allowance and home office in the same year;
        C-56: flat rate of business expenses next to actual expenses; C-44:
        Kz 240 above the tax-free maximums of the year."""
        expense_codes = {
            field.code
            for field in statement.form_ids.field_ids
            if field.box == "25"
            and field.form_id.form_type == "euer"
            and field.field_type in ("amount", "transfer")
        }
        results = []
        findings = adj.plausibility(
            values,
            expense_codes,
            room_months=self._get_room_months(statement, values),
            tax_free_limit=self._get_tax_free_limit(statement),
        )
        for code, key, params in findings:
            severity = "blocker"
            kinds = (adj.HOME_OFFICE_DAYS, adj.HOME_OFFICE_ROOM)
            if key == "home_office_twice":
                message = self.env._(
                    "The daily home office allowance (Kz 163) and costs of a home "
                    "office (Kz 172) are deducted in the same year, the home office "
                    "for all 12 months (§ 4 (5) sentence 1 no. 6c sentence 3 EStG)."
                )
                amount = params["days"]
            elif key == "home_office_part_year":
                severity = "warning"
                message = self.env._(
                    "The daily home office allowance (Kz 163) is deducted next to a "
                    "home office for %(months)s months (Kz 172). The allowance is "
                    "excluded so far as the home office is deducted (§ 4 (5) "
                    "sentence 1 no. 6c sentence 3 EStG): the days must lie outside "
                    "these months.",
                    months=params["months"],
                )
                amount = params["days"]
            elif key == "tax_free_above_limit":
                severity = "warning"
                kinds = (adj.TAX_FREE,)
                message = self.env._(
                    "Tax-free income of § 3 no. 26, 26a, 26b EStG (Kz 240) of "
                    "%(amount)s is above the maximums of the year (%(limit)s; no. "
                    "26 and 26b together at most the limit of no. 26): check the "
                    "amount.",
                    amount=format_amount(
                        self.env, float(params["amount"]), statement.currency_id
                    ),
                    limit=format_amount(
                        self.env, float(params["limit"]), statement.currency_id
                    ),
                )
                amount = params["amount"]
            else:
                kinds = (adj.FLAT_RATE,)
                message = self.env._(
                    "The flat rate of business expenses (Kz 195) replaces all "
                    "actual expenses, but the year has actual expenses: %(fields)s.",
                    fields=", ".join(params["fields"]),
                )
                amount = params["flat"]
            results.append(
                self._result(
                    code,
                    severity,
                    message,
                    statement.adjustment_ids.filtered(
                        lambda adjustment, k=kinds: adjustment.kind in k
                    )
                    or statement,
                    amount,
                )
            )
        return results
