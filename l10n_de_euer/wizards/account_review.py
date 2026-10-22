# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain


class L10nDeEuerAccountReview(models.TransientModel):
    """List "check account assignment" of a business and year (spec 4.6)."""

    _name = "l10n_de_euer.account.review"
    _description = "EÜR Account Assignment Review"

    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company.root_id,
        domain="[('parent_id', '=', False)]",
    )
    year = fields.Integer(
        required=True, default=lambda self: fields.Date.context_today(self).year
    )
    line_ids = fields.One2many("l10n_de_euer.account.review.line", "review_id")

    @api.constrains("year")
    def _check_year(self):
        for review in self:
            if not 1900 < review.year < 3000:
                raise UserError(self.env._("Enter a valid year."))

    def _compute_lines(self):
        self.ensure_one()
        checker = self.env["l10n_de_euer.assignment.checker"]
        context = checker._prepare(self.company_id, self.year)
        accounts = self.env["account.account"].concat(*context["segments"])
        proposals = self.env["l10n_de_euer.account.assignment"]._get_proposals(
            self.company_id, accounts
        )
        empty = self.env["l10n_de_euer.account.assignment"]
        vals_list = []
        for account, usages in context["segments"].items():
            assignments = empty.concat(*(item for item in usages if item))
            current = assignments.sorted(
                lambda item: item.date_from or fields.Date.to_date("1900-01-01")
            )[-1:]
            category = current.category_id
            vals_list.append(
                {
                    "review_id": self.id,
                    "account_id": account.id,
                    "assignment_id": current.id,
                    "category_id": category.id,
                    "proposal_id": proposals.get(account, empty.category_id).id,
                    "debit": sum(usage["debit"] for usage in usages.values()),
                    "credit": sum(usage["credit"] for usage in usages.values()),
                    "balance": sum(usage["balance"] for usage in usages.values()),
                    "line_count": sum(usage["count"] for usage in usages.values()),
                    "unassigned": bool(usages.get(empty, {}).get("count")),
                    "needs_confirmation": any(
                        item.needs_confirmation for item in assignments
                    ),
                    "warn_used": any(
                        item.category_id.role == "warn" or item.category_id.warning
                        for item in assignments
                    ),
                }
            )
        self.line_ids.unlink()
        self.env["l10n_de_euer.account.review.line"].create(vals_list)

    def action_review(self):
        self.ensure_one()
        self._compute_lines()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_account_review_line"
        )
        action["domain"] = [("review_id", "=", self.id)]
        action["name"] = self.env._(
            "Account Assignment %(company)s %(year)s",
            company=self.company_id.name,
            year=self.year,
        )
        return action

    def action_run_checks(self):
        self.ensure_one()
        self.env["l10n_de_euer.check"]._run_assignment_checks(
            self.company_id, self.year
        )
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_check"
        )
        action["domain"] = [
            ("company_id", "=", self.company_id.id),
            ("year", "=", self.year),
            ("origin", "=", "assignment"),
        ]
        return action


class L10nDeEuerAccountReviewLine(models.TransientModel):
    """Account used in the year with its category, confidence and proposal."""

    _name = "l10n_de_euer.account.review.line"
    _description = "EÜR Account Assignment Review Line"
    _order = "unassigned desc, needs_confirmation desc, account_id"

    review_id = fields.Many2one(
        "l10n_de_euer.account.review", required=True, ondelete="cascade"
    )
    company_id = fields.Many2one(related="review_id.company_id")
    year = fields.Integer(related="review_id.year")
    account_id = fields.Many2one("account.account", required=True, readonly=True)
    # Odoo shows the code in the name of an account only to users with
    # read-only accounting; the review always needs it (E5 review)
    account_code = fields.Char(string="Code", compute="_compute_account_code")
    account_type = fields.Selection(related="account_id.account_type")
    assignment_id = fields.Many2one("l10n_de_euer.account.assignment", readonly=True)
    category_id = fields.Many2one("l10n_de_euer.category", readonly=True)
    role = fields.Selection(related="category_id.role")
    confidence = fields.Selection(related="assignment_id.confidence")
    confirmed = fields.Boolean(related="assignment_id.confirmed")
    proposal_id = fields.Many2one("l10n_de_euer.category", readonly=True)
    currency_id = fields.Many2one(related="review_id.company_id.currency_id")
    debit = fields.Monetary(readonly=True)
    credit = fields.Monetary(readonly=True)
    balance = fields.Monetary(readonly=True)
    line_count = fields.Integer(string="Lines", readonly=True)
    unassigned = fields.Boolean(
        string="Not Assigned",
        readonly=True,
        help="Lines of the year are not covered by an assignment.",
    )
    needs_confirmation = fields.Boolean(
        string="Low Confidence, Unconfirmed", readonly=True
    )
    warn_used = fields.Boolean(string="Not Provided For", readonly=True)

    @api.depends("account_id", "review_id.company_id")
    def _compute_account_code(self):
        for line in self:
            account = line.account_id
            if line.company_id:
                account = account.with_company(line.company_id)
            line.account_code = account.code or ""

    def action_apply_proposal(self):
        """Assign the proposed category to unassigned accounts (from now on open)."""
        Assignment = self.env["l10n_de_euer.account.assignment"]
        vals_list = []
        for line in self:
            if not line.proposal_id or line.assignment_id:
                continue
            if Assignment.search_count(
                Domain("company_id", "=", line.company_id.id)
                & Domain("account_id", "=", line.account_id.id),
                limit=1,
            ):
                continue  # partly assigned: the user decides the periods
            vals_list.append(
                {
                    "company_id": line.company_id.id,
                    "account_id": line.account_id.id,
                    "category_id": line.proposal_id.id,
                    "source": "proposal",
                }
            )
        if not vals_list:
            raise UserError(
                self.env._("There is no proposal to apply to unassigned accounts.")
            )
        Assignment.create(vals_list)
        for review in self.review_id:
            review._compute_lines()
        return {"type": "ir.actions.client", "tag": "soft_reload"}

    def action_confirm(self):
        self.assignment_id.filtered("needs_confirmation").action_confirm()
        for review in self.review_id:
            review._compute_lines()
        return {"type": "ir.actions.client", "tag": "soft_reload"}

    def action_open_assignment(self):
        self.ensure_one()
        # no with_company(main company): users of a branch may lack access to it
        action = self.account_id.action_l10n_de_euer_assignments()
        action["context"] = dict(
            action.get("context") or {}, default_company_id=self.company_id.id
        )
        return action

    def action_open_move_lines(self):
        self.ensure_one()
        companies = self.env["res.company"].search(
            [("id", "child_of", self.company_id.id)]
        )
        return {
            "type": "ir.actions.act_window",
            "name": self.account_id.display_name,
            "res_model": "account.move.line",
            "view_mode": "list,form",
            "domain": [
                ("account_id", "=", self.account_id.id),
                ("company_id", "in", companies.ids),
                ("parent_state", "=", "posted"),
                ("date", ">=", f"{self.year}-01-01"),
                ("date", "<=", f"{self.year}-12-31"),
            ],
        }
