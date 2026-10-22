# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Takeover from a previous system (spec 7.4, B-08, C-35).

Opening entries are never money: their open receivables and payables are
invoices of the previous system. The wizard reads the opening entries of a
business (the opening move of the company and every entry with an account
of the role opening balance), lists each open item and asks for the
category, the VAT treatment and the VAT rate of the original invoice; it
stores them as ``l10n_de_euer.opening.item``. It also takes the turnover
of the previous year into the VAT status period and leads to the takeover
of the asset register (spec 6.6).
"""

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain

from ..models.euer_move_info import VAT_REGIMES

#: roles of accounts whose open items need no takeover (B-08)
SELF_ROLES = ("neutral", "steuer", "ungeklaert")
STANDARD_RATE = 19.0


class L10nDeEuerOpeningWizard(models.TransientModel):
    _name = "l10n_de_euer.opening.wizard"
    _description = "EÜR Takeover of Open Items"

    company_id = fields.Many2one(
        "res.company",
        string="Business",
        required=True,
        default=lambda self: self.env.company._l10n_de_euer_business_root(),
        domain="['|', ('parent_id', '=', False), "
        "('l10n_de_euer_separate_business', '=', True)]",
    )
    currency_id = fields.Many2one(related="company_id.currency_id")
    line_ids = fields.One2many(
        "l10n_de_euer.opening.wizard.line", "wizard_id", string="Open Items"
    )
    move_count = fields.Integer(string="Opening Entries")
    missing_count = fields.Integer(
        string="Open Items without Category", compute="_compute_missing_count"
    )
    vat_period_id = fields.Many2one(
        "l10n_de_euer.vat.period",
        string="VAT Status Period",
        compute="_compute_vat_period_id",
        store=True,
        readonly=False,
        domain="[('company_id', '=', root_company_id)]",
        help="Period that receives the turnover of the previous year.",
    )
    root_company_id = fields.Many2one(
        "res.company", compute="_compute_vat_period_id", store=True
    )
    prev_year_turnover = fields.Monetary(
        string="Turnover of the Previous Year",
        compute="_compute_prev_year_turnover",
        store=True,
        readonly=False,
        help="Total turnover of the year before the first year in Odoo for the "
        "small business thresholds (§ 19 UStG). It counts for the year entered "
        "next to it only; later years take the turnover from the data.",
    )
    prev_year_turnover_year = fields.Integer(
        string="For the Year",
        compute="_compute_prev_year_turnover",
        store=True,
        readonly=False,
        help="First year in Odoo: its small business check takes the turnover "
        "entered as that of the previous year. Proposed from the opening date "
        "of the company; stored only with a turnover or with the confirmation "
        "of a turnover of 0.",
    )
    prev_year_turnover_zero = fields.Boolean(
        string="No Turnover in the Previous Year",
        compute="_compute_prev_year_turnover",
        store=True,
        readonly=False,
        help="Confirms a total turnover of 0 in the previous year. An empty "
        "amount alone is no input: without a turnover or this confirmation the "
        "small business check of the year reports the turnover of the previous "
        "year as unknown (C-32).",
    )

    @api.depends("line_ids.category_id")
    def _compute_missing_count(self):
        for wizard in self:
            wizard.missing_count = len(
                wizard.line_ids.filtered(lambda line: not line.category_id)
            )

    @api.depends("company_id")
    def _compute_vat_period_id(self):
        Period = self.env["l10n_de_euer.vat.period"]
        for wizard in self:
            root = wizard.company_id.root_id
            wizard.root_company_id = root
            wizard.vat_period_id = Period.search(
                Domain("company_id", "=", root.id), order="date_from", limit=1
            )

    @api.depends("vat_period_id")
    def _compute_prev_year_turnover(self):
        """Show the input of the chosen status period: after a change of
        the period the values of the former one would otherwise be written
        into it, and an empty amount would delete its turnover (E5 review)."""
        for wizard in self:
            period = wizard.vat_period_id
            wizard.prev_year_turnover = period.prev_year_turnover
            opening = wizard.company_id.root_id.account_opening_date
            wizard.prev_year_turnover_year = (
                period.prev_year_turnover_year
                or (opening.year if opening else 0)
                or (period.date_from.year if period else 0)
            )
            # 0 with its year is an input of the period (nothing received)
            wizard.prev_year_turnover_zero = bool(
                period.prev_year_turnover_year and not period.prev_year_turnover
            )

    # ------------------------------------------------------------ reading
    def _get_opening_moves(self):
        """Posted opening entries of the business (B-08): the opening move of
        its companies and every entry with an account of the role opening
        balance."""
        self.ensure_one()
        companies = self.company_id._l10n_de_euer_business_companies().sudo()
        root = self.company_id.sudo().root_id
        accounts = self.env["l10n_de_euer.asset"]._get_opening_accounts(root)
        Move = self.env["account.move"].sudo()
        moves = companies.account_opening_move_id.filtered(
            lambda move: move.state == "posted"
        )
        if accounts:
            lines = (
                self.env["account.move.line"]
                .sudo()
                .search(
                    Domain("company_id", "in", companies.ids)
                    & Domain("parent_state", "=", "posted")
                    & Domain("account_id", "in", accounts.ids)
                )
            )
            moves |= lines.move_id
        return Move.browse(sorted(moves.ids))

    def _get_open_items(self):
        """Open items of the opening entries that need a takeover: lines on
        receivable, payable or other reconcilable balance sheet accounts,
        except accounts without invoice behind them (money in transit, VAT,
        bank suspense)."""
        self.ensure_one()
        root = self.company_id.sudo().root_id
        Assignment = self.env["l10n_de_euer.account.assignment"].sudo()
        items = self.env["account.move.line"]
        for move in self._get_opening_moves():
            lines = move.line_ids.filtered(
                lambda line: line.account_id._l10n_de_euer_is_open_item()
                and not line.company_currency_id.is_zero(line.balance)
            )
            assignments = Assignment._get_assignments(root, lines.account_id, move.date)
            for line in lines:
                assignment = assignments.get(line.account_id)
                if assignment and assignment.category_id.role in SELF_ROLES:
                    continue
                items |= line
        return items

    def action_load(self):
        """List the open items of the opening entries (B-08)."""
        self.ensure_one()
        self._check_manager()
        Item = self.env["l10n_de_euer.opening.item"]
        lines = self._get_open_items()
        existing = {
            item.move_line_id: item
            for item in Item.search(Domain("move_line_id", "in", lines.ids))
        }
        Period = self.env["l10n_de_euer.vat.period"]
        values = []
        for line in lines.sorted(lambda line: (line.date, line.move_id.id, line.id)):
            item = existing.get(line)
            regime = (
                item.vat_regime
                if item
                else Period._get_regime(self.company_id, line.date) or "regular"
            )
            values.append(
                fields.Command.create(
                    {
                        "move_line_id": line.id,
                        "item_id": item.id if item else False,
                        "category_id": item.category_id.id if item else False,
                        "vat_regime": regime,
                        "tax_rate": item.tax_rate
                        if item
                        else (STANDARD_RATE if regime == "regular" else 0.0),
                        "origin_reference": item.origin_reference
                        if item
                        else line.ref or line.name or False,
                        "reason": item.reason if item else False,
                    }
                )
            )
        self.write(
            {
                "line_ids": [fields.Command.clear(), *values],
                "move_count": len(self._get_opening_moves()),
            }
        )
        return self._reopen()

    def _reopen(self):
        # the title of the menu and of the first dialog (E5 review)
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Takeover from a Previous System"),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }

    # ------------------------------------------------------------ apply
    def _check_manager(self):
        if not self.env.user.has_group("account.group_account_manager"):
            raise UserError(
                self.env._("Only an accounting manager can release a takeover.")
            )

    def action_apply(self):
        """Store the takeover of the open items with a category (C-35) and
        the turnover of the previous year."""
        self.ensure_one()
        self._check_manager()
        Item = self.env["l10n_de_euer.opening.item"]
        created = updated = 0
        for line in self.line_ids.filtered("category_id"):
            values = {
                "category_id": line.category_id.id,
                "vat_regime": line.vat_regime,
                "tax_rate": line.tax_rate if line.vat_regime == "regular" else 0.0,
                "origin_reference": line.origin_reference,
                "reason": line.reason,
            }
            item = line.item_id.exists()
            if item:
                item.write(values)
                updated += 1
            else:
                line.item_id = Item.create(
                    {
                        "company_id": self.company_id.id,
                        "move_line_id": line.move_line_id.id,
                        **values,
                    }
                )
                created += 1
        self._apply_turnover()
        message = self.env._(
            "%(created)s open items taken over, %(updated)s updated.",
            created=created,
            updated=updated,
        )
        if self.missing_count:
            message += " " + self.env._(
                "%(count)s open items have no category yet: their payments are "
                "blocked (C-35).",
                count=self.missing_count,
            )
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": self.env._("Takeover of Open Items"),
                "message": message,
                "type": "warning" if self.missing_count else "success",
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    def _apply_turnover(self):
        """Write the turnover of the previous year into the status period.

        The year is stored only with a turnover or with the confirmation of
        a turnover of 0: the amount field shows 0.00 when nothing is
        entered, and 0 with its year is a known turnover. A takeover of open
        items alone must not confirm a turnover of 0 (C-32, E5 review).
        """
        period = self.vat_period_id
        if not period:
            return
        known = bool(self.prev_year_turnover) or self.prev_year_turnover_zero
        wanted = {
            "prev_year_turnover": self.prev_year_turnover if known else 0.0,
            "prev_year_turnover_year": self.prev_year_turnover_year if known else 0,
        }
        values = {
            name: value for name, value in wanted.items() if period[name] != value
        }
        if values:
            period.write(values)

    def action_open_asset_import(self):
        """Takeover of the asset register by CSV (spec 6.6)."""
        return self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_asset_import"
        )


class L10nDeEuerOpeningWizardLine(models.TransientModel):
    _name = "l10n_de_euer.opening.wizard.line"
    _description = "EÜR Takeover of an Open Item"
    _order = "date, id"

    wizard_id = fields.Many2one(
        "l10n_de_euer.opening.wizard", required=True, ondelete="cascade"
    )
    move_line_id = fields.Many2one(
        "account.move.line", string="Open Item", required=True, readonly=True
    )
    item_id = fields.Many2one("l10n_de_euer.opening.item", readonly=True)
    move_id = fields.Many2one(related="move_line_id.move_id", string="Opening Entry")
    date = fields.Date(related="move_line_id.date", store=True)
    partner_id = fields.Many2one(related="move_line_id.partner_id")
    account_id = fields.Many2one(related="move_line_id.account_id")
    currency_id = fields.Many2one(related="move_line_id.company_currency_id")
    balance = fields.Monetary(related="move_line_id.balance")
    residual = fields.Monetary(
        related="move_line_id.amount_residual", string="Open Amount"
    )
    category_id = fields.Many2one(
        "l10n_de_euer.category",
        domain="[('role', 'in', ('be', 'ba'))]",
        help="Kind of income or expense of the original invoice.",
    )
    vat_regime = fields.Selection(
        VAT_REGIMES, string="VAT Treatment", required=True, default="regular"
    )
    tax_rate = fields.Float(
        string="VAT Rate (%)",
        digits=(5, 2),
        help="VAT rate of the original invoice under standard taxation.",
    )
    origin_reference = fields.Char(string="Reference in the Previous System")
    reason = fields.Text()
