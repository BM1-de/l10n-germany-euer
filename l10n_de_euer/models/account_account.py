# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models


class AccountAccount(models.Model):
    _inherit = "account.account"

    l10n_de_euer_category_id = fields.Many2one(
        "l10n_de_euer.category",
        string="EÜR Category",
        compute="_compute_l10n_de_euer_category_id",
        groups="account.group_account_readonly,account.group_account_manager",
        help="Category of the account for the EÜR of the current business today.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        accounts = super().create(vals_list)
        if not self.env.context.get("chart_template_load"):
            # accounts of a chart template get their defaults after loading it
            self.env["l10n_de_euer.account.assignment"]._prefill_new_accounts(accounts)
        return accounts

    @api.depends_context("company")
    def _compute_l10n_de_euer_category_id(self):
        assignments = (
            self.env["l10n_de_euer.account.assignment"]
            .sudo()
            ._get_assignments(self.env.company, self, fields.Date.context_today(self))
        )
        for account in self:
            account.l10n_de_euer_category_id = assignments.get(
                account, self.env["l10n_de_euer.account.assignment"]
            ).category_id

    def _l10n_de_euer_is_open_item(self):
        """Open item (role R) of the cash basis calculation (spec 5.2, B-04).

        Only balance sheet accounts that are reconcilable or receivable/payable.
        An income or expense account stays an ordinary line (role N) even if
        reconciliation is allowed on it (C-08).
        """
        self.ensure_one()
        return self.internal_group in ("asset", "liability") and (
            self.reconcile
            or self.account_type in ("asset_receivable", "liability_payable")
        )

    def action_l10n_de_euer_assignments(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_account_assignment"
        )
        action["domain"] = [("account_id", "=", self.id)]
        action["context"] = {
            "default_account_id": self.id,
            "default_company_id": self.env.company.root_id.id,
        }
        return action
