# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import datetime

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Domain

from .euer_statement import check_same_parent

#: fields a user may write
USER_FIELDS = frozenset({"balance_statement", "attachment_ids", "note"})


class L10nDeEuerCashBalance(models.Model):
    """Bank reconciliation of 31 December (G-17).

    Per money journal of the business (bank, cash, credit card, payment
    service provider) the user enters the balance of the statement of
    account at 31 December with the statement as attachment. It has to
    match the balance in Odoo (posted lines up to 31 December on the account
    of the journal), otherwise check C-38 blocks the finalisation.
    """

    _name = "l10n_de_euer.cash.balance"
    _inherit = ["mail.thread"]
    _description = "EÜR Bank Reconciliation 31 December"
    _order = "statement_id, journal_id"

    statement_id = fields.Many2one(
        "l10n_de_euer.statement", required=True, index=True, ondelete="cascade"
    )
    company_id = fields.Many2one(
        related="statement_id.company_id", store=True, index=True
    )
    year = fields.Integer(related="statement_id.year", store=True)
    journal_id = fields.Many2one(
        "account.journal",
        required=True,
        domain="[('type', 'in', ('bank', 'cash', 'credit'))]",
    )
    account_id = fields.Many2one(
        related="journal_id.default_account_id", string="Account"
    )
    currency_id = fields.Many2one("res.currency", compute="_compute_currency_id")
    balance_statement = fields.Monetary(
        string="Balance per Statement",
        tracking=True,
        help="Balance of the statement of account at 31 December.",
    )
    balance_odoo = fields.Monetary(
        string="Balance in Odoo",
        compute="_compute_balance_odoo",
        help="Posted journal items up to 31 December on the account of the journal.",
    )
    difference = fields.Monetary(compute="_compute_balance_odoo")
    entered_by_id = fields.Many2one(
        "res.users", readonly=True, copy=False, ondelete="restrict"
    )
    entered_on = fields.Datetime(readonly=True, copy=False)
    attachment_ids = fields.Many2many(
        "ir.attachment",
        string="Statement of Account",
        help="Statement of account at 31 December as receipt.",
    )
    note = fields.Text()

    _statement_journal_uniq = models.Constraint(
        "UNIQUE(statement_id, journal_id)",
        "Each journal is reconciled once per statement.",
    )

    @api.depends("journal_id")
    def _compute_currency_id(self):
        for balance in self:
            balance.currency_id = (
                balance.journal_id.currency_id
                or balance.statement_id.company_id.currency_id
            )

    @api.depends("journal_id", "balance_statement", "year")
    def _compute_balance_odoo(self):
        for balance in self:
            balance.balance_odoo = balance._get_odoo_balance()
            balance.difference = balance.balance_statement - balance.balance_odoo

    def _get_odoo_balance(self):
        """Posted lines up to 31 December of the year on the account of the
        journal, in the currency of the journal."""
        self.ensure_one()
        account = self.journal_id.sudo().default_account_id
        if not account or not self.year:
            return 0.0
        companies = self.statement_id.company_id._l10n_de_euer_business_companies()
        foreign = self.journal_id.currency_id and (
            self.journal_id.currency_id != self.statement_id.company_id.currency_id
        )
        field = "amount_currency" if foreign else "balance"
        groups = (
            self.env["account.move.line"]
            .sudo()
            ._read_group(
                Domain("company_id", "in", companies.sudo().ids)
                & Domain("account_id", "=", account.id)
                & Domain("parent_state", "=", "posted")
                & Domain("date", "<=", datetime.date(self.year, 12, 31)),
                [],
                [f"{field}:sum"],
            )
        )
        return self.currency_id.round(groups[0][0] or 0.0) if groups else 0.0

    def _is_reconciled(self):
        """The balance is entered and matches the balance in Odoo."""
        self.ensure_one()
        return bool(self.entered_on) and self.currency_id.is_zero(self.difference)

    @api.constrains("journal_id", "statement_id")
    def _check_journal(self):
        for balance in self:
            companies = (
                balance.statement_id.company_id._l10n_de_euer_business_companies()
            )
            if balance.journal_id.sudo().company_id not in companies.sudo():
                raise ValidationError(
                    self.env._(
                        "%(journal)s does not belong to the business of the statement.",
                        journal=balance.journal_id.display_name,
                    )
                )

    @api.model_create_multi
    def create(self, vals_list):
        # who entered the balance and when is always the current user and
        # time (G-06, G-17), never a value of the caller
        vals_list = [
            {
                name: value
                for name, value in vals.items()
                if name not in ("entered_by_id", "entered_on")
            }
            for vals in vals_list
        ]
        records = super().create(vals_list)
        records.statement_id._check_open()
        entered = self.browse(
            [
                record.id
                for record, vals in zip(records, vals_list, strict=True)
                if "balance_statement" in vals
            ]
        )
        entered._set_entered()
        return records

    def write(self, vals):
        check_same_parent(self, "statement_id", vals)
        if set(vals) - USER_FIELDS and not self.env.su:
            raise UserError(
                self.env._(
                    "Only the balance per statement, its attachment and a note can "
                    "be entered."
                )
            )
        self.statement_id._check_open()
        result = super().write(vals)
        if "balance_statement" in vals:
            self._set_entered()
        return result

    def _set_entered(self):
        if not self:
            return True
        return super(L10nDeEuerCashBalance, self.sudo()).write(
            {"entered_by_id": self.env.uid, "entered_on": fields.Datetime.now()}
        )

    @api.ondelete(at_uninstall=False)
    def _unlink_only_open(self):
        self.statement_id._check_open()
