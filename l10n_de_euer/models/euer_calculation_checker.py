# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import datetime
from collections import defaultdict

from odoo import api, fields, models
from odoo.fields import Domain
from odoo.tools import format_amount

from .euer_ten_day_item import reason_text

#: findings listed together in one check result per run
GROUPED_KEYS = frozenset(
    {
        "draft_entry",
        "partial_with_draft",
        "revaluation",
        "neutralized",
        "transit_both_sides",
        "bank_date_differs",
        "payment_on_bank_account",
        "tax_line_without_repartition",
        "payment_not_matched",
        "transfer_order_date",
        "several_openings",
        "lock_date_shift",
        "writeoff_netted",
        "reversal_pair_years",
        "payment_on_bank_account_rest",
    }
)
#: findings whose record is the journal item to classify or take over
LINE_KEYS = frozenset(
    {
        "unallocated_open_item",
        "unallocated_refund",
        "unallocated_other",
        "unallocated_pair",
        "unallocated_outstanding",
        "partial_other_business",
        "money_reaches_writeoff",
        "money_to_money_years",
        "opening_item_missing",
        "exchange_difference_missing",
        "booking_category_in_document",
        "neutral_account_with_expense",
    }
)
_MAX_NAMES = 5


class L10nDeEuerCalculationChecker(models.AbstractModel):
    """Check results of the cash basis calculation (spec 7.8).

    Turns the findings of the engine (C-03, C-04, C-10 to C-37, C-46 to
    C-48, C-51 to C-54) into stored check results with a message, and adds
    the checks that read Odoo data after the run: C-15 (bank statement lines
    not reconciled), C-16 (document without file), C-17 (line without
    partner or text), C-27 (payment registered without journal entry), C-39
    (active lock date exception) and C-60 (deviation of a run of a finalised
    year from its finalised run, :meth:`_get_c60_results` of the statement).
    ``form_results`` are the checks of the form calculation (C-40, C-41,
    C-71, phase G).
    """

    _name = "l10n_de_euer.calculation.checker"
    _description = "EÜR Calculation Checks"

    @api.model
    def _store(self, business, year, result, run=None, form_results=()):
        results = self._engine_results(business, year, result)
        results += list(form_results)
        results += self._check_c15(business, year)
        results += self._check_c27(business, year)
        if run is not None:
            results += self._check_c16_c17(run)
            results += self.env["l10n_de_euer.statement"]._get_c60_results(
                business, year, run
            )
        results += self._check_c39(business, year)
        self.env["l10n_de_euer.check"]._store_results(
            business, year, "calculation", results
        )
        return results

    # ------------------------------------------------------------ engine
    @api.model
    def _engine_results(self, business, year, result):
        groups = defaultdict(list)
        single = []
        for finding in result.findings:
            if finding.key in GROUPED_KEYS:
                groups[(finding.code, finding.severity, finding.key)].append(finding)
            else:
                single.append(finding)
        results, seen = [], set()
        currency = business.currency_id
        for finding in single:
            values = self._result(
                finding.code, finding.severity, finding, year, [finding], currency
            )
            key = (values["code"], values["res_model"], tuple(values["res_ids"]))
            if key in seen:
                continue
            seen.add(key)
            results.append(values)
        for (code, severity, _key), findings in sorted(groups.items()):
            results.append(
                self._result(code, severity, findings[0], year, findings, currency)
            )
        return results

    @api.model
    def _amount_text(self, cents, currency=None):
        """Amount in the format of the language and the currency."""
        value = (cents or 0) / 100
        if currency:
            return format_amount(self.env, value, currency)
        return f"{value:.2f}"

    def _annual_text(self, params, currency=None):
        """Sentence on a turnover converted into a full year (until 2024)."""
        if not params.get("annual"):
            return ""
        return " " + self.env._(
            "The activity started on %(start)s: the turnover is converted into a "
            "turnover of the full year of %(annual)s (§ 19 (3) sentences 3 and 4 "
            "UStG in the version until 2024).",
            start=params.get("start") or "",
            annual=self._amount_text(params.get("annual"), currency),
        )

    @api.model
    def _result(self, code, severity, finding, year, findings, currency=None):
        move_ids = sorted({x for f in findings for x in f.move_ids})
        line_ids = sorted({x for f in findings for x in f.line_ids})
        if line_ids and (finding.key in LINE_KEYS or not move_ids):
            res_model, res_ids = "account.move.line", line_ids
        elif move_ids:
            res_model, res_ids = "account.move", move_ids
        else:
            res_model, res_ids = False, []
        amount = sum(f.amount for f in findings) / 100
        return {
            "code": code,
            "severity": severity,
            "message": self._message(
                finding, findings, year, move_ids, line_ids, currency
            ),
            "res_model": res_model,
            "res_ids": res_ids,
            "amount": amount,
        }

    @api.model
    def _names(self, model, ids):
        records = self.env[model].sudo().browse(ids[:_MAX_NAMES])
        names = ", ".join(record.display_name for record in records)
        if len(ids) > _MAX_NAMES:
            names += ", …"
        return names

    @api.model
    def _account_name(self, account_id):
        return self.env["account.account"].sudo().browse(account_id).display_name

    @api.model
    def _message(  # noqa: C901
        self, finding, findings, year, move_ids, line_ids, currency=None
    ):
        """Message of a finding in the language of the user."""
        key = finding.key
        params = finding.params
        moves = self._names("account.move", move_ids)
        lines = self._names("account.move.line", line_ids)
        move = self._names("account.move", move_ids[:1])
        line = self._names("account.move.line", line_ids[:1])
        line_record = self.env["account.move.line"].sudo().browse(line_ids[:1])
        account = line_record.account_id.display_name or ""
        amount = self._amount_text(abs(finding.amount), currency)
        count = len(findings)
        if key == "direct_on_payment":
            return self.env._(
                "%(move)s is not realised on its booking date: the payment of the "
                "balance sheet account realises the income or expense (5.5.1 b).",
                move=move,
            )
        if key == "direct_on_payment_fields":
            return self.env._(
                "%(move)s is realised by its payment, but the payment reaches other "
                "form fields (%(payment)s) than its income or expense lines "
                "(%(fields)s).",
                move=move,
                payment=", ".join(params.get("payment_fields") or ()),
                fields=", ".join(params.get("fields") or ()),
            )
        if key == "reclassification_other_year":
            return self.env._(
                "Reclassification %(move)s is booked in another year than the "
                "reclassified payments (%(years)s); book it in the year of the "
                "payments.",
                move=move,
                years=", ".join(str(y) for y in params.get("payment_years") or ()),
            )
        if key == "direct_without_rule":
            return self.env._(
                "%(move)s has neither money nor open items, and no realisation rule "
                "applies (e.g. expense against a non-reconcilable liability, "
                "provision, accrual). Make the liability reconcilable and book the "
                "invoice as document, or use a category for income or expense on "
                "payment.",
                move=move,
            )
        if key == "unclear":
            return self.env._(
                "%(amount)s of %(move)s are booked on the bank suspense account "
                "(statement line not reconciled). Reconcile the statement line.",
                amount=amount,
                move=move,
            )
        if key == "money_reconciliation":
            return self.env._(
                "The money of %(move)s does not add up (difference %(amount)s). This "
                "is an error of the calculation; please report it.",
                move=move,
                amount=amount,
            )
        if key == "document_completeness":
            return self.env._(
                "Realised, open and neutralised amounts of %(line)s do not add up "
                "(difference %(difference)s). This is an error of the calculation; "
                "please report it.",
                line=line,
                difference=params.get("difference", ""),
            )
        if key == "transit_over_realised":
            return self.env._(
                "Payment entry %(move)s is realised by more than 100 %%. This is an "
                "error of the calculation; please report it.",
                move=move,
            )
        if key == "bank_date_differs":
            return self.env._(
                "Bank statement entries dated differently from their bank date "
                "(lock date?): %(moves)s. Check the bank reconciliation on 31 "
                "December.",
                moves=moves,
            )
        if key == "draft_entry":
            return self.env._(
                "%(count)s draft entries dated in %(year)s are ignored: %(moves)s. "
                "Post or delete them.",
                count=count,
                year=year,
                moves=moves,
            )
        if key == "partial_with_draft":
            return self.env._(
                "%(count)s reconciliations with draft entries are ignored: %(moves)s.",
                count=count,
                moves=moves,
            )
        if key == "neutral_account_with_expense":
            return self.env._(
                "%(account)s is a neutral balance sheet account without "
                "reconciliation. It is the counter account of money movements and of "
                "income or expense lines in %(move)s; the expense would be lost. "
                "Make the account reconcilable and book the invoice as document.",
                account=self._account_name(params.get("account_id")) or account,
                move=move,
            )
        if key == "exchange_difference_missing":
            return self.env._(
                "Foreign currency item %(line)s is settled in its currency, but "
                "%(amount)s remain open in euro: the exchange difference entry is "
                "missing or not reconciled.",
                line=line,
                amount=amount,
            )
        if key == "unallocated_open_item":
            return self.env._(
                "%(move)s leaves an unallocated rest of %(amount)s on %(account)s "
                "(advance payment or payment before the invoice). Classify the rest "
                "or reconcile it with the invoice.",
                move=move,
                amount=amount,
                account=account,
            )
        if key == "unallocated_refund":
            return self.env._(
                "%(move)s leaves an unallocated rest of %(amount)s on %(account)s "
                "(money returned: refund or repayment without document). Classify "
                "the rest or reconcile it with the credit note.",
                move=move,
                amount=amount,
                account=account,
            )
        if key == "partial_other_business":
            return self.env._(
                "%(line)s is reconciled with a journal item of another business "
                "(a company of the same main company with its own EÜR). Money and "
                "document belong to different statements; correct the "
                "reconciliation.",
                line=line,
            )
        if key == "lock_date_shift":
            return self.env._(
                "Bank statement entries dated where Odoo puts entries of a locked "
                "period (day after the lock date, end of that month, end of that "
                "year or day of posting): %(moves)s. Odoo may have moved them from "
                "the locked period; check the bank reconciliation on 31 December.",
                moves=moves,
            )
        if key == "reversal_pair_years":
            return self.env._(
                "Reversal pairs over the turn of the year are left out in both "
                "years, as no money flowed: %(moves)s. If the earlier year was "
                "already filed, correct it.",
                moves=moves,
            )
        if key == "writeoff_netted":
            return self.env._(
                "Write-offs booked with a payment reduce the documents the money "
                "reached; they are no expense: %(moves)s.",
                moves=moves,
            )
        if key == "unallocated_outstanding":
            return self.env._(
                "%(move)s leaves an unallocated rest of %(amount)s on the "
                "outstanding account %(account)s: the payment it was reconciled "
                "with is missing (e.g. reversed after the bank reconciliation). "
                "Reconcile the statement line again or classify the rest.",
                move=move,
                amount=amount,
                account=account,
            )
        if key == "neutralized_private":
            return self.env._(
                "%(moves)s: a journal entry with a private line settles a "
                "document without money; the withdrawal or contribution and the "
                "income or expense would be lost. Book the private payment as "
                "an entry that settles the document completely (private account "
                "against the open item, discount next to it).",
                moves=moves,
            )
        if key == "late_reconciliation":
            return self.env._(
                "%(moves)s were reconciled on %(date)s, after the end of %(year)s; "
                "the values of %(year)s changed. If the year was already filed, "
                "correct it.",
                moves=moves,
                date=params.get("created") or "",
                year=finding.years[0] if finding.years else year,
            )
        if key == "payment_on_bank_account_rest":
            return self.env._(
                "Payments booked directly on the bank account are matched with "
                "bank statement lines only in part; the rest counts on the date "
                "of the payment: %(moves)s.",
                moves=moves,
            )
        if key == "writeoff_unplaced":
            return self.env._(
                "A write-off booked with the money of %(moves)s is larger than "
                "what reaches documents (%(amount)s). Check the reconciliation.",
                moves=moves,
                amount=amount,
            )
        if key == "unallocated_pair":
            return self.env._(
                "%(move)s leaves an unallocated rest of %(amount)s on %(account)s "
                "that another rest of the same partner cancels in the same year "
                "(payment and its return). Reconcile the two payments in Odoo.",
                move=move,
                amount=amount,
                account=account,
            )
        if key == "ten_day_input_missing":
            return self.env._(
                "The ten-day rule for %(moves)s is applied, but the economic year "
                "or the legal due date is missing. Enter them in the review list "
                "of the turn of the year.",
                moves=moves,
            )
        if key == "ten_day_apply_without_effect":
            if params.get("economic_year") == params.get("payment_year"):
                return self.env._(
                    "The ten-day rule for %(moves)s is decided as 'apply', but the "
                    "economic year %(year)s is the year of payment: the decision "
                    "moves nothing. Enter the economic year the payment belongs to "
                    "in the review list of the turn of the year (the year before "
                    "for a payment in January, the year after for a payment in "
                    "December), or decide 'Do Not Apply'.",
                    moves=moves,
                    year=params.get("economic_year") or "",
                )
            window = params.get("window") or ("", "")
            return self.env._(
                "The ten-day rule for %(moves)s is decided as 'apply', but the "
                "legal due date %(date)s lies outside the window from %(start)s "
                "to %(end)s: the rule cannot apply (BFH X R 2/21) and the decision "
                "moves nothing. Correct the due date in the review list of the turn "
                "of the year, or decide 'Do Not Apply'.",
                moves=moves,
                date=params.get("due_date") or "",
                start=window[0],
                end=window[1],
            )
        if key == "unallocated_other":
            return self.env._(
                "%(move)s leaves an unallocated rest of %(amount)s on %(account)s.",
                move=move,
                amount=amount,
                account=account,
            )
        if key == "cycle":
            return self.env._(
                "The reconciliations of %(moves)s form a cycle; the money is not "
                "realised.",
                moves=moves,
            )
        if key == "transit_both_sides":
            return self.env._(
                "Payment entries reached by money from both sides are realised up "
                "to 100 %%: %(moves)s.",
                moves=moves,
            )
        if key == "transfer_order_date":
            return self.env._(
                "Outgoing money from 1 to 5 January: %(moves)s. Confirm in the "
                "review list of the turn of the year that the transfer order was "
                "given in the new year, or set the order date.",
                moves=moves,
            )
        if key == "payment_on_bank_account":
            return self.env._(
                "Payments booked directly on the bank account; the date of the "
                "statement line is used: %(moves)s.",
                moves=moves,
            )
        if key == "neutralized":
            return self.env._(
                "Reconciliations between documents without money were neutralised: "
                "%(lines)s.",
                lines=lines,
            )
        if key == "ten_day_undecided":
            return self.env._(
                "%(moves)s is a candidate for the ten-day rule (%(reasons)s). Decide "
                "it in the review list of the turn of the year.",
                moves=moves,
                reasons=reason_text(self.env, params.get("reasons")),
            )
        if key == "direct_debit_undecided":
            message = self.env._(
                "Direct debit %(move)s of the tax office after 10 January: decide "
                "in the review list whether the due date %(date)s is the date of "
                "payment (VAT return submitted by 10 January).",
                move=move,
                date=params.get("due_date") or "",
            )
            if params.get("shifted_due_date"):
                message += " " + self.env._(
                    "The due date falls on a weekend: the period for the payment "
                    "ends on %(shifted)s (§ 108 (3) AO), and a direct debit counts "
                    "as paid on that day (§ 224 (2) no. 3 AO), after the ten days. "
                    "Proposal: Do Not Apply (year of payment); apply only with the "
                    "confirmation of your tax advisor.",
                    shifted=params["shifted_due_date"],
                )
            return message
        if key == "direct_debit_weekend_applied":
            return self.env._(
                "Direct debit %(move)s is decided as paid on the due date %(date)s "
                "(previous year), although that day falls on a weekend: the period "
                "for the payment ended on %(shifted)s (§ 108 (3) AO), and a direct "
                "debit counts as paid on that day (§ 224 (2) no. 3 AO; H 11 EStH "
                "'Allgemeines'). Keep the decision only with the confirmation of "
                "your tax advisor.",
                move=move,
                date=params.get("due_date") or "",
                shifted=params.get("shifted_due_date") or "",
            )
        if key == "payment_not_matched":
            return self.env._(
                "Payments registered, but the money is not (fully) on the bank "
                "account: %(moves)s.",
                moves=moves,
            )
        if key == "revaluation":
            return self.env._(
                "Valuations of foreign currency holdings are not realised: %(moves)s.",
                moves=moves,
            )
        if key == "booking_category_in_document":
            return self.env._(
                "%(move)s contains a line realised on booking (%(line)s) together "
                "with open items. Book it as an entry without open item.",
                move=move,
                line=line,
            )
        if key == "input_vat_without_base":
            return self.env._(
                "Input VAT of %(move)s that is not deductible has no base line; it "
                "is shown as other expense (Kz 183).",
                move=move,
            )
        if key == "ku_limit_near":
            return self.env._(
                "The total turnover reaches 80 %% of the small business limit of "
                "%(limit)s.",
                limit=self._amount_text(params.get("limit"), currency),
            ) + self._annual_text(params, currency)
        if key == "ku_limit_previous_year":
            return self.env._(
                "The turnover of the previous year exceeds %(limit)s: the small "
                "business status is not possible in %(year)s.",
                limit=self._amount_text(params.get("limit"), currency),
                year=year,
            ) + self._annual_text(params, currency)
        if key == "ku_previous_year_unknown":
            one_year = " " + self.env._(
                "A VAT status period holds this input for one year only: if its "
                "turnover is already entered for another year, first end that "
                "period on 31 December %(previous)s (status periods must not "
                "overlap), then add a status period with the same treatment from "
                "1 January %(year)s and enter it there.",
                previous=year - 1,
                year=year,
            )
            if params.get("books_start"):
                return (
                    self.env._(
                        "The total turnover of %(previous)s is incomplete in Odoo: "
                        "the books start with the opening entry of %(start)s, during "
                        "that year, and no turnover is entered for %(year)s. The "
                        "small business status of %(year)s requires a turnover of "
                        "the previous year of at most %(limit)s (§ 19 (1) UStG). "
                        "Enter the total turnover of the whole year %(previous)s, "
                        "including the months in the previous system and other parts "
                        "of the enterprise outside Odoo, at the VAT status period "
                        "(Turnover of the Previous Year, For the Year %(year)s).",
                        previous=year - 1,
                        start=params["books_start"],
                        year=year,
                        limit=self._amount_text(params.get("limit"), currency),
                    )
                    + one_year
                )
            return (
                self.env._(
                    "The total turnover of %(previous)s is unknown: Odoo holds no "
                    "bookings of that year and no turnover is entered for %(year)s. "
                    "The small business status of %(year)s requires a turnover of "
                    "the previous year of at most %(limit)s (§ 19 (1) UStG). Enter it "
                    "at the VAT status period (Turnover of the Previous Year, For the "
                    "Year %(year)s), including other parts of the enterprise outside "
                    "Odoo; if the activity started in %(year)s, enter the start of "
                    "the activity at the company instead.",
                    previous=year - 1,
                    year=year,
                    limit=self._amount_text(params.get("limit"), currency),
                )
                + one_year
            )
        if key == "ku_free_supply_not_counted":
            return self.env._(
                "Free supplies booked in the small business period (Kz 106, 108; "
                "%(amount)s: %(moves)s) do not count in the total turnover: "
                "without input tax deduction they are not taxable. If one is "
                "taxable (the good or its costs gave input tax deduction), enter "
                "its VAT base in a profit correction and leave its value empty.",
                amount=amount,
                moves=moves,
            )
        if key == "ku_limit_forecast_exceeded":
            return self.env._(
                "The total turnover of %(year)s exceeds the small business limit of "
                "%(limit)s for the turnover expected in the year (§ 19 (1) UStG in "
                "the version until 2024): check whether the forecast at the start "
                "of the year was justified.",
                year=year,
                limit=self._amount_text(params.get("limit"), currency),
            ) + self._annual_text(params, currency)
        if key == "ku_limit_exceeded_period_set":
            return self.env._(
                "The small business limit is exceeded; a VAT status period "
                "'standard taxation from receipt' exists."
            )
        if key == "ku_limit_exceeded":
            return self.env._(
                "The small business limit is exceeded with the receipt of %(move)s. "
                "Create a VAT status period 'standard taxation from receipt' from "
                "that date.",
                move=move,
            )
        if key == "tax_rate_missing_limit":
            return self.env._(
                "Receipt for %(move)s after the small business limit was exceeded: "
                "enter the VAT rate of the supply on the document and correct the "
                "invoice.",
                move=move,
            )
        if key == "tax_rate_missing":
            return self.env._(
                "Standard taxation, but %(move)s is booked on a small business "
                "revenue account or without VAT: enter the VAT rate of the supply "
                "on the document and correct the invoice.",
                move=move,
            )
        if key == "tax_line_without_repartition":
            return self.env._(
                "Lines on tax accounts without tax repartition are booked together "
                "with income or expense: %(moves)s. The category of the tax account "
                "decides.",
                moves=moves,
            )
        if key == "opening_item_missing":
            return self.env._(
                "Open item %(line)s of the opening entry has no category and VAT "
                "rate; enter them in the takeover of open items.",
                line=line,
            )
        if key == "money_reaches_writeoff":
            return self.env._(
                "Money of %(move)s reaches the write-off %(line)s (returned direct "
                "debit). Classify it, e.g. as reduction of the original income.",
                move=move,
                line=line,
            )
        if key == "money_to_money_years":
            return self.env._(
                "%(moves)s: money is returned over a receivable or payable between "
                "two years (returned direct debit or refund). Classify the money "
                "entry.",
                moves=moves,
            )
        if key == "opening_after_money":
            return self.env._(
                "Opening entry %(move)s is dated after money movements on its money "
                "accounts; its money lines count as opening balance.",
                move=move,
            )
        if key == "several_openings":
            return self.env._(
                "Several opening entries in one year: %(moves)s.",
                moves=moves,
            )
        if key == "gift_without_recipient":
            return self.env._(
                "Gift %(move)s has no recipient. Enter the partner or mark the "
                "document as give-aways.",
                move=move,
            )
        if key == "bulk_gift_unit_price":
            return self.env._(
                "Give-aways %(move)s have a unit price above 50 €.",
                move=move,
            )
        if key == "gift_input_vat":
            return self.env._(
                "Input VAT of gifts above 50 € per recipient is not deductible "
                "(%(move)s); correct the VAT return.",
                move=move,
            )
        if key == "entertainment_ratio":
            return self.env._(
                "Business entertainment: the non-deductible share differs from 30 "
                "% by more than 2 points."
            )
        if key == "private_car_twice":
            return self.env._(
                "Private use of the car is booked and entered as profit correction "
                "in the same year."
            )
        if key == "no_category":
            return self.env._(
                "%(amount)s realised in %(year)s on %(account)s without EÜR "
                "category (%(move)s).",
                amount=amount,
                year=year,
                account=account,
                move=move,
            )
        return self.env._(
            "%(code)s: %(moves)s",
            code=finding.code,
            moves=moves or lines,
        )

    # ------------------------------------------------------------ Odoo data
    @api.model
    def _check_c15(self, business, year):
        """Bank statement lines of the year older than 10 days, not reconciled."""
        companies = business._l10n_de_euer_business_companies()
        limit = min(
            fields.Date.context_today(self) - datetime.timedelta(days=10),
            datetime.date(year, 12, 31),
        )
        lines = self.env["l10n_de_euer.adapter"]._unreconciled_statement_lines(
            companies, datetime.date(year, 1, 1), limit
        )
        if not lines:
            return []
        return [
            {
                "code": "C-15",
                "severity": "info",
                "message": self.env._(
                    "%(count)s bank statement lines of %(year)s older than 10 days "
                    "are not reconciled.",
                    count=len(lines),
                    year=year,
                ),
                "res_model": "account.move",
                "res_ids": sorted(lines.move_id.ids),
                "amount": sum(lines.mapped("amount")),
            }
        ]

    @api.model
    def _check_c16_c17(self, run):
        """Documents of income and expense without file (C-16), lines without
        partner and text (C-17)."""
        slices = (
            self.env["l10n_de_euer.cash.slice"]
            .sudo()
            .search(Domain("run_id", "=", run.id) & Domain("field_code", "!=", False))
        )
        documents = slices.origin_move_id.filtered(
            lambda move: move.is_invoice(include_receipts=True)
        )
        results = []
        if documents:
            attached = set(
                self.env["ir.attachment"]
                .sudo()
                .search(
                    Domain("res_model", "=", "account.move")
                    & Domain("res_id", "in", documents.ids)
                )
                .mapped("res_id")
            )
            missing = documents.filtered(lambda move: move.id not in attached)
            if missing:
                results.append(
                    {
                        "code": "C-16",
                        "severity": "warning",
                        "message": self.env._(
                            "%(count)s documents with income or expense in %(year)s "
                            "have no file attached: %(moves)s.",
                            count=len(missing),
                            year=run.year_from,
                            moves=self._names("account.move", missing.ids),
                        ),
                        "res_model": "account.move",
                        "res_ids": sorted(missing.ids),
                        "amount": 0.0,
                    }
                )
        anonymous = slices.origin_line_id.filtered(
            lambda line: not line.partner_id
            and not line.move_id.partner_id
            and not (line.name or "").strip()
            and not (line.move_id.ref or "").strip()
        )
        if anonymous:
            results.append(
                {
                    "code": "C-17",
                    "severity": "warning",
                    "message": self.env._(
                        "%(count)s lines with income or expense in %(year)s have "
                        "neither partner nor text.",
                        count=len(anonymous),
                        year=run.year_from,
                    ),
                    "res_model": "account.move",
                    "res_ids": sorted(anonymous.move_id.ids),
                    "amount": 0.0,
                }
            )
        return results

    @api.model
    def _check_c27(self, business, year):
        """Payments of the year registered without journal entry (no
        outstanding account) whose money is not on the bank account yet: the
        invoice is "in payment" or already shown as paid, but its open item
        is not reconciled with the bank (B-13, C-27 like a payment entry)."""
        companies = business._l10n_de_euer_business_companies()
        payments = (
            self.env["account.payment"]
            .sudo()
            .search(
                Domain("company_id", "in", companies.ids)
                & Domain("move_id", "=", False)
                & Domain("state", "in", ("in_process", "paid"))
                & Domain("date", ">=", datetime.date(year, 1, 1))
                & Domain("date", "<=", datetime.date(year, 12, 31))
            )
            .filtered(
                lambda payment: payment.state == "in_process"
                or any(
                    not invoice.company_currency_id.is_zero(invoice.amount_residual)
                    for invoice in payment.invoice_ids
                )
            )
        )
        if not payments:
            return []
        return [
            {
                "code": "C-27",
                "severity": "warning",
                "message": self.env._(
                    "Payments registered without journal entry, the money is not "
                    "(fully) on the bank account: %(payments)s.",
                    payments=self._names("account.payment", payments.ids),
                ),
                "res_model": "account.payment",
                "res_ids": sorted(payments.ids),
                "amount": sum(payments.mapped("amount_company_currency_signed")),
            }
        ]

    @api.model
    def _check_c39(self, business, year):
        """Active exception of the lock date that opens the year.

        Odoo applies the exceptions of the company and of its parents: an
        exception of the main company opens a branch that is a business of
        its own, too. An exception of the fiscal year lock date opens the
        year; one of another lock date (tax, sales, purchases) only where
        the fiscal year and hard lock dates leave the year open.
        """
        if "account.lock_exception" not in self.env:
            return []
        companies = business._l10n_de_euer_business_companies().sudo()
        chains = {
            company: company.with_context(active_test=False).parent_ids
            for company in companies
        }
        start, end = datetime.date(year, 1, 1), datetime.date(year, 12, 31)
        candidates = (
            self.env["account.lock_exception"]
            .sudo()
            .search(
                Domain(
                    "company_id",
                    "in",
                    list({c.id for chain in chains.values() for c in chain}),
                )
                & Domain("state", "=", "active")
            )
        )

        def opens(exception):
            # the exception opens (lock_date, company_lock_date]
            low = max(
                exception.lock_date or datetime.date.min,
                start - datetime.timedelta(days=1),
            )
            high = min(exception.company_lock_date or end, end)
            if high < start or high <= low:
                return False
            for chain in chains.values():
                if exception.company_id not in chain:
                    continue
                hard = max(filter(None, chain.mapped("hard_lock_date")), default=None)
                if hard and hard >= high:
                    continue
                if exception.lock_date_field == "fiscalyear_lock_date":
                    return True
                fiscal = max(
                    filter(None, chain.mapped("fiscalyear_lock_date")), default=None
                )
                if not fiscal or fiscal < high:
                    return True
            return False

        exceptions = candidates.filtered(opens)
        if not exceptions:
            return []
        return [
            {
                "code": "C-39",
                "severity": "warning",
                "message": self.env._(
                    "An exception of the lock date is active for %(year)s; entries "
                    "can still be posted into the year.",
                    year=year,
                ),
                "res_model": "account.lock_exception",
                "res_ids": sorted(exceptions.ids),
                "amount": 0.0,
            }
        ]
