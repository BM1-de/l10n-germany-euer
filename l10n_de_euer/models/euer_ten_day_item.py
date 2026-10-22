# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Domain

TEN_DAY_KINDS = [
    ("recurring", "Regularly Recurring Payment"),
    ("vat", "VAT Payment or Refund"),
    ("direct_debit_tax", "Direct Debit of the Tax Office"),
    ("transfer_order", "Transfer at the Turn of the Year"),
]
DECISIONS = [
    ("apply", "Apply"),
    ("reject", "Do Not Apply"),
    ("auto", "Decided Automatically"),
]
PROPOSALS = [
    ("apply", "Apply"),
    ("review", "Review"),
    ("reject", "Do Not Apply"),
    ("confirm", "Confirm"),
]
#: context key: only the calculation and the actions write the items
INTERNAL = "l10n_de_euer_ten_day_internal"
#: what a user enters before deciding (G-06: the decision itself is an action)
USER_FIELDS = frozenset({"reason", "economic_year", "due_date"})
#: values of the rule a user enters: kept by the next calculation
ENTERED_FIELDS = ("economic_year", "due_date")


def reason_labels(env):
    """Readable reasons of a candidate in the language of the user."""
    return {
        "other_year_due": env._("document of the other year, due in the window"),
        "repeated_partner": env._("repeated similar payments to the partner"),
        "tax_office": env._("tax office"),
        "flag": env._("flag on partner or category"),
        "economic_year_entered": env._("economic year entered"),
    }


def reason_text(env, reasons):
    labels = reason_labels(env)
    return ", ".join(labels.get(reason, reason) for reason in reasons or ())


class L10nDeEuerTenDayItem(models.Model):
    """Review list of the turn of the year (spec 5.6, 7.3).

    * Ten-day rule (§ 11 (1) 2, (2) 2 EStG): every candidate found in the
      data must be decided (C-26); VAT returns with complete input are
      decided automatically.
    * Direct debits of the tax office after 10 January (R-06): the legal due
      date can become the cash date.
    * Outgoing transfers from 1 to 5 January (C-23): confirm that the order
      was given in the new year, or set the order date on the entry.

    The items are created by the calculation; the decisions are kept.
    """

    _name = "l10n_de_euer.ten_day.item"
    _inherit = ["mail.thread"]
    _description = "EÜR Review of the Turn of the Year"
    _order = "cash_date desc, id desc"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        default=lambda self: self.env.company._l10n_de_euer_business_root(),
        help="Business (main company with its branches, or a branch that is a "
        "business of its own).",
    )
    year = fields.Integer(
        string="Turn of the Year",
        required=True,
        index=True,
        help="Year whose end the item belongs to (window 22.12. to 10.01. of the "
        "next year, transfers up to 5 January, direct debits up to 31 January).",
    )
    kind = fields.Selection(TEN_DAY_KINDS, required=True, readonly=True)
    source_move_id = fields.Many2one(
        "account.move", string="Money Entry", required=True, readonly=True
    )
    origin_move_id = fields.Many2one(
        "account.move", string="Document", required=True, readonly=True
    )
    partner_id = fields.Many2one("res.partner", compute="_compute_partner_id")
    cash_date = fields.Date(readonly=True)
    amount = fields.Monetary(currency_field="currency_id", readonly=True)
    currency_id = fields.Many2one(related="company_id.currency_id")
    candidate_reason = fields.Char(
        string="Reasons",
        readonly=True,
        help="Why the payment is a candidate: document of the other year due in "
        "the window, repeated payments to the partner, tax office, flag.",
    )
    proposal = fields.Selection(
        PROPOSALS,
        readonly=True,
        help="Apply: flag, tax office or repeated payments and an economic year "
        "other than the year of payment. Review: enter the economic year or check "
        "the document. Do not apply: the due date lies outside the window, or a "
        "direct debit of the tax office is due on a weekend (§ 108 (3) AO).",
    )
    proposed_cash_date = fields.Date(
        readonly=True,
        help="Due date (direct debit of the tax office) or date the payment order "
        "was handed to the bank.",
    )
    economic_year = fields.Integer(
        tracking=True,
        help="Year the payment belongs to economically. 'Apply' moves the payment "
        "into this year, so it must differ from the year of payment: the year "
        "before for a payment in January, the year after for a payment in "
        "December. A year proposed from the date of the document is only a "
        "proposal; 'Apply' with the year of payment is reported (C-26).",
    )
    due_date = fields.Date(
        string="Legal Due Date",
        tracking=True,
        help="Due date by law or contract, without the shift of § 108 (3) AO. "
        "Payment and due date must both lie in the window from 22 December to "
        "10 January.",
    )
    values_entered = fields.Boolean(
        readonly=True,
        help="The economic year or the due date was entered by hand; the "
        "calculation keeps it.",
    )
    decision = fields.Selection(DECISIONS, tracking=True, readonly=True)
    applied = fields.Boolean(
        readonly=True, help="The last calculation applied the decision."
    )
    reason = fields.Text(tracking=True)
    decided_by_id = fields.Many2one("res.users", readonly=True)
    decided_on = fields.Datetime(readonly=True)

    _item_unique = models.Constraint(
        "UNIQUE(company_id, kind, source_move_id, origin_move_id)",
        "There is only one review item per payment and document.",
    )

    @api.depends("origin_move_id", "source_move_id")
    def _compute_partner_id(self):
        for item in self:
            item.partner_id = (
                item.origin_move_id.partner_id or item.source_move_id.partner_id
            )

    @api.constrains("decision", "reason")
    def _check_reason(self):
        for item in self:
            if item.decision in ("apply", "reject") and not item.reason:
                raise ValidationError(
                    self.env._(
                        "Give a reason for the decision on %(entry)s.",
                        entry=item.source_move_id.display_name,
                    )
                )

    @api.model
    def _is_internal(self):
        """Written by the calculation or an action: always as superuser with
        the context key (a client can send the key through RPC, but it cannot
        set ``env.su``)."""
        return self.env.su and bool(self.env.context.get(INTERNAL))

    def _write_internal(self, values):
        return self.sudo().with_context(**{INTERNAL: True}).write(values)

    @api.model_create_multi
    def create(self, vals_list):
        if not self._is_internal():
            raise UserError(
                self.env._("The review list is created by the calculation.")
            )
        return super().create(vals_list)

    def write(self, vals):
        if not self._is_internal():
            if set(vals) - USER_FIELDS:
                raise UserError(
                    self.env._(
                        "Decide with the actions Apply and Do Not Apply; only the "
                        "reason, the economic year and the due date can be entered."
                    )
                )
            if set(vals) & set(ENTERED_FIELDS):
                vals = dict(vals, values_entered=True)
        return super().write(vals)

    def _decide(self, decision):
        self.check_access("write")
        for item in self:
            if not item.reason:
                raise UserError(
                    self.env._(
                        "Give a reason for the decision on %(entry)s.",
                        entry=item.source_move_id.display_name,
                    )
                )
        self._write_internal(
            {
                "decision": decision,
                "decided_by_id": self.env.uid,
                "decided_on": fields.Datetime.now(),
            }
        )

    def action_apply(self):
        """Apply the rule; for transfers: the order was given in the new year."""
        self._decide("apply")

    def action_reject(self):
        self._decide("reject")

    def action_use_proposed_date(self):
        """Transfer by payment order: the handover date becomes the cash date."""
        self.check_access("write")
        for item in self:
            if item.kind != "transfer_order" or not item.proposed_cash_date:
                raise UserError(self.env._("There is no proposed date to use."))
            item.source_move_id._l10n_de_euer_write_info(
                {
                    "l10n_de_euer_cash_date": item.proposed_cash_date,
                    "l10n_de_euer_cash_date_reason": "transfer_order",
                }
            )
            item._write_internal(
                {
                    "decision": "apply",
                    "decided_by_id": self.env.uid,
                    "decided_on": fields.Datetime.now(),
                    "reason": item.reason
                    or self.env._("Date of the handover of the payment order."),
                }
            )

    @api.model
    def _engine_decisions(self, company):
        """Decisions of a business as input of the engine."""
        items = self.sudo().search(
            Domain("company_id", "=", company.id)
            & Domain("decision", "in", ("apply", "reject"))
        )
        return items

    @api.model
    def _turn_of_year(self, day):
        """Year whose end a date at the turn of the year belongs to."""
        return day.year if day.month >= 7 else day.year - 1

    @api.model
    def _store_items(self, company, years, values):
        """Create or update the items of a calculation; keep the decisions.

        ``years``: turns of the year the calculation covers (end of the
        previous and of the statement year). ``values``: dicts with the keys
        of the model. Undecided items of these years that the calculation no
        longer finds are removed.
        """
        Item = self.sudo().with_context(
            mail_create_nolog=True,
            mail_create_nosubscribe=True,
            tracking_disable=True,
            **{INTERNAL: True},
        )
        root = company
        existing = {
            (item.kind, item.source_move_id.id, item.origin_move_id.id): item
            for item in Item.search(Domain("company_id", "=", root.id))
        }
        seen = Item.browse()
        to_create = []
        for vals in values:
            year = self._turn_of_year(vals["cash_date"])
            if year not in years:
                continue
            vals = dict(vals, year=year)
            key = (vals["kind"], vals["source_move_id"], vals["origin_move_id"])
            item = existing.get(key)
            if item is None:
                to_create.append({"company_id": root.id, **vals})
                continue
            seen |= item
            update = {
                name: value
                for name, value in vals.items()
                if not (name in ENTERED_FIELDS and item.values_entered)
                and (
                    name not in ("economic_year", "due_date", "decision")
                    or not item.decision
                    or item.decision == "auto"
                )
            }
            if item.decision in ("apply", "reject"):
                update.pop("decision", None)
            item.write(update)
        stale = Item.search(
            Domain("company_id", "=", root.id)
            & Domain("year", "in", list(years))
            & (Domain("decision", "=", False) | Domain("decision", "=", "auto"))
        )
        (stale - seen).unlink()
        return seen | Item.create(to_create)
