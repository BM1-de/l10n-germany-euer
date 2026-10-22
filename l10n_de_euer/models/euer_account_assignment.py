# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import csv
import datetime
from functools import lru_cache

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.fields import Domain
from odoo.tools.misc import file_open

from .euer_category import CONFIDENCE

#: Chart templates with default assignments (spec 4.3)
DEFAULT_CHARTS = ("de_skr03", "de_skr04")
MAPPING_PATH = "l10n_de_euer/data/mapping"
MONEY_ACCOUNT_TYPES = ("asset_cash", "liability_credit_card")
#: Outstanding receipts/payments accounts of the chart (account_payment.py)
OUTSTANDING_ACCOUNTS = (
    "account_journal_payment_debit_account_id",
    "account_journal_payment_credit_account_id",
)
#: Roles a proposal from a code range may have, per internal group (K-03)
RANGE_ROLES = {
    "income": {"be", "neutral", "warn", "privat"},
    "expense": {"ba", "afa", "neutral", "warn", "privat"},
}
BALANCE_SHEET_ROLES = {
    "geld",
    "op",
    "steuer",
    "av",
    "neutral",
    "warn",
    "privat",
    "eroeffnung",
    "ungeklaert",
}


@lru_cache
def _read_csv(name):
    with file_open(f"{MAPPING_PATH}/{name}") as handle:
        return tuple(csv.DictReader(handle))


def default_rows(chart):
    """Default category per template XML id without company prefix (spec 4.3)."""
    return _read_csv(f"category_{chart}.csv")


def range_rules(chart):
    """Code ranges ``(from, to, category code)`` of a chart template (K-03)."""
    return tuple(
        (int(row["code_from"]), int(row["code_to"]), row["category"])
        for row in _read_csv("range_rules.csv")
        if row["chart"] == chart
    )


class L10nDeEuerAccountAssignment(models.Model):
    """Category of an account per business and period (spec 4.1, K-01)."""

    _name = "l10n_de_euer.account.assignment"
    _inherit = ["mail.thread"]
    _description = "EÜR Account Assignment"
    _order = "account_id, date_from"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        default=lambda self: self.env.company.root_id,
        domain="[('parent_id', '=', False)]",
        tracking=True,
        help="Business: main company; the assignment also applies to its branches.",
    )
    account_id = fields.Many2one(
        "account.account",
        required=True,
        index=True,
        ondelete="cascade",
        # accounts of the main company and of its branches (an account created
        # in a branch belongs only to the branch); checked by _check_account
        domain="[('company_ids', 'child_of', company_id)]",
        tracking=True,
    )
    # the code of the account in the business, also for users who do not see
    # it in the name of the account (E5 review)
    account_code = fields.Char(string="Code", compute="_compute_account_code")
    category_id = fields.Many2one(
        "l10n_de_euer.category",
        required=True,
        index=True,
        ondelete="restrict",
        tracking=True,
    )
    role = fields.Selection(related="category_id.role", store=True)
    date_from = fields.Date(
        string="Valid From",
        tracking=True,
        help="Empty: valid from the beginning. A change of use gets a new period; "
        "corrections of errors change the existing record.",
    )
    date_to = fields.Date(string="Valid To", tracking=True, help="Empty: no end.")
    confidence = fields.Selection(
        CONFIDENCE,
        compute="_compute_confidence",
        store=True,
        readonly=False,
        precompute=True,
        tracking=True,
        help="Confidence of a default assignment. Assignments with low "
        "confidence must be confirmed if the account is used.",
    )
    confirmed = fields.Boolean(tracking=True, copy=False)
    confirmed_by_id = fields.Many2one("res.users", copy=False, readonly=True)
    confirmed_on = fields.Datetime(copy=False, readonly=True)
    needs_confirmation = fields.Boolean(
        compute="_compute_needs_confirmation",
        store=True,
        help="Low confidence and not yet confirmed.",
    )
    source = fields.Selection(
        [
            ("template", "Chart Template"),
            ("structure", "Accounting Setup"),
            ("proposal", "Proposal"),
            ("manual", "Manual"),
        ],
        required=True,
        default="manual",
        tracking=True,
    )
    ambiguity_case = fields.Char(
        help="Documented ambiguous case (A01 to A34) of the default assignment."
    )
    note = fields.Text(tracking=True)

    @api.depends("category_id")
    def _compute_confidence(self):
        for assignment in self:
            assignment.confidence = assignment.category_id.confidence or "h"

    @api.depends("confidence", "confirmed")
    def _compute_needs_confirmation(self):
        for assignment in self:
            assignment.needs_confirmation = (
                assignment.confidence == "n" and not assignment.confirmed
            )

    @api.depends("account_id", "company_id")
    def _compute_account_code(self):
        for assignment in self:
            account = assignment.account_id
            if assignment.company_id:
                account = account.with_company(assignment.company_id)
            assignment.account_code = account.code or ""

    @api.depends("account_id", "category_id", "date_from", "date_to")
    def _compute_display_name(self):
        for assignment in self:
            period = ""
            if assignment.date_from or assignment.date_to:
                period = (
                    f" ({assignment.date_from or '…'} – {assignment.date_to or '…'})"
                )
            assignment.display_name = (
                f"{assignment.account_id.display_name or ''} → "
                f"{assignment.category_id.display_name or ''}{period}"
            )

    @api.constrains("company_id", "account_id")
    def _check_account(self):
        """The account belongs to the business (main company or a branch)."""
        for assignment in self:
            if assignment.company_id not in (
                assignment.account_id.sudo().company_ids.root_id
            ):
                raise ValidationError(
                    self.env._(
                        "The account %(account)s does not belong to %(company)s or "
                        "one of its branches.",
                        account=assignment.account_id.sudo().display_name,
                        company=assignment.company_id.name,
                    )
                )

    @api.constrains("company_id", "account_id", "date_from", "date_to")
    def _check_period(self):
        for assignment in self:
            if (
                assignment.date_from
                and assignment.date_to
                and assignment.date_from > assignment.date_to
            ):
                raise ValidationError(
                    self.env._("The start of validity must not be after its end.")
                )
            if assignment.company_id.parent_id:
                raise ValidationError(
                    self.env._(
                        "Account assignments belong to the main company of a "
                        "business, not to the branch %(company)s.",
                        company=assignment.company_id.name,
                    )
                )
            domain = (
                Domain("company_id", "=", assignment.company_id.id)
                & Domain("account_id", "=", assignment.account_id.id)
                & Domain("id", "!=", assignment.id)
            )
            if assignment.date_to:
                domain &= Domain("date_from", "=", False) | Domain(
                    "date_from", "<=", assignment.date_to
                )
            if assignment.date_from:
                domain &= Domain("date_to", "=", False) | Domain(
                    "date_to", ">=", assignment.date_from
                )
            if self.search_count(domain, limit=1):
                raise ValidationError(
                    self.env._(
                        "The periods of the assignments of account %(account)s "
                        "overlap.",
                        account=assignment.account_id.display_name,
                    )
                )

    def _confirmation_values(self):
        return {
            "confirmed": True,
            "confirmed_by_id": self.env.uid,
            "confirmed_on": fields.Datetime.now(),
        }

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            self._close_previous_period(vals)
            if vals.get("source", "manual") in ("manual", "proposal") and (
                "confirmed" not in vals
            ):
                # a manual decision of the user is a confirmation
                vals.update(self._confirmation_values())
        return super().create(vals_list)

    def write(self, vals):
        if "category_id" in vals and "confirmed" not in vals:
            vals = dict(vals, source=vals.get("source", "manual"))
            vals.update(self._confirmation_values())
        if vals.get("confirmed") is False:
            vals = dict(vals, confirmed_by_id=False, confirmed_on=False)
        return super().write(vals)

    def _close_previous_period(self, vals):
        """A new period ends the open period of the same account the day before."""
        if not vals.get("date_from") or not vals.get("account_id"):
            return
        date_from = fields.Date.to_date(vals["date_from"])
        company_id = vals.get("company_id") or self.env.company.root_id.id
        previous = self.search(
            Domain("company_id", "=", company_id)
            & Domain("account_id", "=", vals["account_id"])
            & Domain("date_to", "=", False)
            & (Domain("date_from", "=", False) | Domain("date_from", "<", date_from)),
            limit=1,
        )
        if previous:
            previous.date_to = date_from - datetime.timedelta(days=1)

    def action_confirm(self):
        self.write(self._confirmation_values())

    def action_new_period(self):
        """Open a new assignment of the same account (change of use)."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "view_mode": "form",
            "target": "current",
            "context": {
                "default_company_id": self.company_id.id,
                "default_account_id": self.account_id.id,
                "default_category_id": self.category_id.id,
                "default_date_from": fields.Date.context_today(self),
            },
        }

    # ------------------------------------------------------------ lookups
    @api.model
    def _get_assignments(self, company, accounts, on_date):
        """Return ``{account: assignment}`` valid on a date for a business."""
        domain = (
            Domain("company_id", "=", company.root_id.id)
            & Domain("account_id", "in", accounts.ids)
            & (Domain("date_from", "=", False) | Domain("date_from", "<=", on_date))
            & (Domain("date_to", "=", False) | Domain("date_to", ">=", on_date))
        )
        return {assignment.account_id: assignment for assignment in self.search(domain)}

    @api.model
    def _get_categories_by_code(self):
        categories = (
            self.env["l10n_de_euer.category"]
            .sudo()
            .with_context(active_test=False)
            .search(Domain("code", "!=", False))
        )
        return {category.code: category for category in categories}

    # ------------------------------------------------------------ defaults
    @api.model
    def _prefill_defaults(self, companies):
        """Create missing default assignments for SKR03/SKR04 businesses (K-02).

        Existing assignments of an account (any period) are never changed. Used
        by the post-init hook and after loading a chart template.
        """
        created = self.browse()
        for root in companies.root_id:
            if root.chart_template in DEFAULT_CHARTS:
                created |= self._prefill_company(root)
        return created

    def _prefill_company(self, root):
        rows = default_rows(root.chart_template)
        by_xml_id = {f"{root.id}_{row['xml_id']}": row for row in rows if row["xml_id"]}
        data = (
            self.env["ir.model.data"]
            .sudo()
            .search_read(
                [
                    ("module", "=", "account"),
                    ("model", "=", "account.account"),
                    ("name", "in", list(by_xml_id)),
                ],
                ["name", "res_id"],
                order="id",
            )
        )
        defaults = [(item["res_id"], by_xml_id[item["name"]]) for item in data]
        codes = self._get_code_defaults(root)
        if codes:
            accounts = (
                self.env["account.account"]
                .sudo()
                .with_company(root)
                .search(
                    Domain("company_ids", "child_of", root.id)
                    & Domain("code", "in", list(codes))
                )
            )
            defaults += [(account.id, codes[account.code]) for account in accounts]
        structure = self._get_structural_defaults(root)
        return self._create_defaults(root, defaults, structure)

    @api.model
    def _prefill_new_accounts(self, accounts):
        """Default of DATEV accounts the chart templates lack (K-07, 8192/4184).

        Called when the user creates accounts; other new accounts only get a
        proposal (K-03).
        """
        created = self.browse()
        for root in accounts.sudo().company_ids.root_id:
            codes = self._get_code_defaults(root)
            if not codes:
                continue
            defaults = [
                (account.id, codes[account.with_company(root).code])
                for account in accounts.sudo()
                if root in account.company_ids.root_id
                and account.with_company(root).code in codes
            ]
            if defaults:
                created |= self._create_defaults(root, defaults, {})
        return created

    @api.model
    def _get_code_defaults(self, root):
        if root.chart_template not in DEFAULT_CHARTS:
            return {}
        return {
            row["code"]: row
            for row in default_rows(root.chart_template)
            if not row["xml_id"]
        }

    def _create_defaults(self, root, defaults, structure):
        """Create assignments for accounts of ``root`` that have none yet."""
        categories = self._get_categories_by_code()
        assigned = set(
            self.sudo().search(Domain("company_id", "=", root.id)).account_id.ids
        )
        vals_list = []
        for account_id, row in defaults:
            if account_id in assigned:
                continue
            assigned.add(account_id)
            vals_list.append(
                {
                    "company_id": root.id,
                    "account_id": account_id,
                    "category_id": categories[row["category"]].id,
                    "confidence": row["confidence"],
                    "ambiguity_case": row["case"] or False,
                    "source": "template",
                    "confirmed": False,
                }
            )
        for account, code in structure.items():
            if account.id in assigned:
                continue
            assigned.add(account.id)
            vals_list.append(
                {
                    "company_id": root.id,
                    "account_id": account.id,
                    "category_id": categories[code].id,
                    "confidence": categories[code].confidence,
                    "source": "structure",
                    "confirmed": False,
                }
            )
        return (
            self.sudo()
            .with_context(mail_create_nolog=True, mail_create_nosubscribe=True)
            .create(vals_list)
        )

    @api.model
    def _get_structural_defaults(self, root):
        """Accounts whose kind follows from the accounting setup (spec 5.2).

        Bank, cash and credit card journals: default account money, suspense
        account unclear, outstanding payment accounts in transit; purchase
        journals: private share account private (K-11).
        """
        Account = self.env["account.account"]
        companies = self.env["res.company"].sudo().search([("id", "child_of", root.id)])
        journals = (
            self.env["account.journal"]
            .sudo()
            .with_context(active_test=False)
            .search([("company_id", "in", companies.ids)], order="id")
        )
        result = {}
        for journal in journals:
            if journal.type in ("bank", "cash", "credit"):
                result.setdefault(journal.default_account_id, "CASH")
                result.setdefault(journal.suspense_account_id, "SUSPENSE")
                methods = (
                    journal.inbound_payment_method_line_ids
                    | journal.outbound_payment_method_line_ids
                )
                for account in methods.payment_account_id:
                    result.setdefault(account, "TRANSIT")
            elif journal.type == "purchase":
                result.setdefault(
                    journal.non_deductible_account_id, "PRIVATE_WITHDRAWAL"
                )
        result.setdefault(root.account_journal_suspense_account_id, "SUSPENSE")
        result.setdefault(root.transfer_account_id, "TRANSIT")
        for name in OUTSTANDING_ACCOUNTS:
            account = self.env.ref(
                f"account.{root.id}_{name}", raise_if_not_found=False
            )
            result.setdefault(account or Account, "TRANSIT")
        result.pop(Account, None)
        return result

    @api.model
    def _get_proposals(self, company, accounts):
        """Proposed category per account without assignment (K-03, no default).

        Order: accounting setup (journals), account type bank/cash or credit
        card, code range of the chart template.
        """
        root = company.root_id
        categories = self._get_categories_by_code()
        structural = self._get_structural_defaults(root)
        rules = (
            range_rules(root.chart_template)
            if root.chart_template in DEFAULT_CHARTS
            else ()
        )
        result = {}
        for account in accounts:
            code = structural.get(account)
            if not code and account.account_type in MONEY_ACCOUNT_TYPES:
                code = "CASH"
            category = categories.get(code) if code else None
            if not category and rules:
                # the code of the business; read as superuser because users of
                # a branch need not have access to the main company
                category = self._get_range_proposal(
                    account.sudo().with_company(root), rules, categories
                )
            if category:
                result[account] = category
        return result

    @api.model
    def _get_range_proposal(self, account, rules, categories):
        digits = (account.code or "")[:4]
        if len(digits) < 4 or not digits.isdigit():
            return None
        number = int(digits)
        for code_from, code_to, code in rules:
            if code_from <= number <= code_to:
                category = categories.get(code)
                break
        else:
            return None
        if not category:
            return None  # range without proposal (e.g. between opening codes)
        group = account.internal_group
        if group in RANGE_ROLES:
            allowed = category.role in RANGE_ROLES[group]
        else:
            allowed = category.role in BALANCE_SHEET_ROLES or category.on_payment
        return category if allowed else None
