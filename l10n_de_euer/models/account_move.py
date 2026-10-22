# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models
from odoo.fields import Domain

from .euer_move_info import CASH_DATE_REASONS, TAX_RATES, VAT_REGIMES

#: helper field on account.move → field of l10n_de_euer.move.info
INFO_FIELDS = {
    "l10n_de_euer_vat_regime": "vat_regime",
    "l10n_de_euer_cash_date": "cash_date",
    "l10n_de_euer_cash_date_reason": "cash_date_reason",
    "l10n_de_euer_economic_year": "economic_year",
    "l10n_de_euer_due_date": "due_date",
    "l10n_de_euer_vat_period": "vat_period",
    "l10n_de_euer_regular_tax_rate": "regular_tax_rate",
    "l10n_de_euer_gift_bulk": "gift_bulk",
    "l10n_de_euer_gift_recipient_id": "gift_recipient_id",
    "l10n_de_euer_gift_date": "gift_date",
}
#: helper fields that are only displayed (the treatment is set on the record)
READONLY_INFO_FIELDS = frozenset({"l10n_de_euer_vat_regime"})


class AccountMove(models.Model):
    """Helper fields for the EÜR data of an entry (spec 7.3, B-01).

    The fields are not stored on ``account.move``: they read and write
    ``l10n_de_euer.move.info``. Writing them never writes the journal entry
    itself (no ``write_date``, no tracking, also on hashed entries; GT-16).
    """

    _inherit = "account.move"

    l10n_de_euer_enabled = fields.Boolean(
        string="EÜR Enabled", compute="_compute_l10n_de_euer_enabled"
    )
    l10n_de_euer_info_id = fields.Many2one(
        "l10n_de_euer.move.info",
        string="EÜR Information",
        compute="_compute_l10n_de_euer_info",
    )
    l10n_de_euer_vat_regime = fields.Selection(
        VAT_REGIMES,
        string="VAT Treatment (EÜR)",
        compute="_compute_l10n_de_euer_info",
    )
    l10n_de_euer_cash_date = fields.Date(
        string="Cash Date (EÜR)",
        compute="_compute_l10n_de_euer_info",
        inverse="_inverse_l10n_de_euer_info",
        help="Overrides the date of the bank statement as date of receipt or "
        "payment, e.g. the date the transfer order was given (H 11).",
    )
    l10n_de_euer_cash_date_reason = fields.Selection(
        CASH_DATE_REASONS,
        string="Reason for the Cash Date",
        compute="_compute_l10n_de_euer_info",
        inverse="_inverse_l10n_de_euer_info",
    )
    l10n_de_euer_economic_year = fields.Integer(
        string="Economic Year (EÜR)",
        compute="_compute_l10n_de_euer_info",
        inverse="_inverse_l10n_de_euer_info",
        help="Year the payment belongs to economically (ten-day rule, § 11 EStG). "
        "A year other than the year of payment makes a payment between 22 "
        "December and 10 January a candidate of the ten-day rule; unless the "
        "payment recurs regularly (flag on partner or category, steady "
        "payments), it is applied only after confirmation in the review list.",
    )
    l10n_de_euer_due_date = fields.Date(
        string="Legal Due Date (EÜR)",
        compute="_compute_l10n_de_euer_info",
        inverse="_inverse_l10n_de_euer_info",
    )
    l10n_de_euer_vat_period = fields.Char(
        string="VAT Return Period (EÜR)",
        compute="_compute_l10n_de_euer_info",
        inverse="_inverse_l10n_de_euer_info",
        help="2026-12 (month), 2026-Q4 (quarter) or 2026 (year).",
    )
    l10n_de_euer_regular_tax_rate = fields.Selection(
        TAX_RATES,
        string="VAT Rate of the Supply (EÜR)",
        compute="_compute_l10n_de_euer_info",
        inverse="_inverse_l10n_de_euer_info",
    )
    l10n_de_euer_gift_bulk = fields.Boolean(
        string="Give-Aways (EÜR)",
        compute="_compute_l10n_de_euer_info",
        inverse="_inverse_l10n_de_euer_info",
    )
    l10n_de_euer_gift_recipient_id = fields.Many2one(
        "res.partner",
        string="Recipient of the Gifts (EÜR)",
        compute="_compute_l10n_de_euer_info",
        inverse="_inverse_l10n_de_euer_info",
        help="Person or business that receives the gifts of this document; the "
        "supplier of the invoice is never the recipient (50 € limit).",
    )
    l10n_de_euer_gift_date = fields.Date(
        string="Date of the Gift (EÜR)",
        compute="_compute_l10n_de_euer_info",
        inverse="_inverse_l10n_de_euer_info",
        help="Date the gifts are handed over, if it differs from the date of supply.",
    )
    l10n_de_euer_slice_count = fields.Integer(
        string="EÜR Slices", compute="_compute_l10n_de_euer_slice_count"
    )

    @api.depends("company_id")
    def _compute_l10n_de_euer_enabled(self):
        # the main company may not be among the active companies of the user
        # (only the branch selected): read its setting as superuser
        for move in self:
            move.l10n_de_euer_enabled = (
                move.company_id.sudo().root_id.l10n_de_euer_enabled
            )

    def _compute_l10n_de_euer_info(self):
        infos = {
            info.move_id: info
            for info in self.env["l10n_de_euer.move.info"]
            .sudo()
            .search(Domain("move_id", "in", self.ids))
        }
        empty = self.env["l10n_de_euer.move.info"]
        for move in self:
            info = infos.get(move, empty)
            move.l10n_de_euer_info_id = info.with_env(self.env)
            for name, info_name in INFO_FIELDS.items():
                move[name] = info[info_name]

    def _inverse_l10n_de_euer_info(self):
        # only reached on create; write() stores the values itself. A form
        # sends the empty helper fields, too: no record without a value.
        for move in self:
            values = {
                name: move[name]
                for name in INFO_FIELDS
                if name not in READONLY_INFO_FIELDS
            }
            if any(values.values()):
                move._l10n_de_euer_write_info(values)

    def _compute_l10n_de_euer_slice_count(self):
        Slice = self.env["l10n_de_euer.cash.slice"]
        for move in self:
            move_id = move._origin.id
            move.l10n_de_euer_slice_count = (
                Slice.search_count(
                    Domain("source_move_id", "=", move_id)
                    | Domain("origin_move_id", "=", move_id)
                )
                if move_id and Slice.has_access("read")
                else 0
            )

    def write(self, vals):
        """Store the EÜR helper fields in ``l10n_de_euer.move.info``.

        They are removed from ``vals`` before the entry is written; if nothing
        else is written, the journal entry is not touched at all (B-01).
        """
        info_vals = {name: vals[name] for name in INFO_FIELDS if name in vals}
        if info_vals:
            vals = {
                name: value for name, value in vals.items() if name not in info_vals
            }
            self._l10n_de_euer_write_info(info_vals)
            if not vals:
                return True
        return super().write(vals)

    def _l10n_de_euer_write_info(self, values):
        values = {
            INFO_FIELDS[name]: value
            for name, value in values.items()
            if name not in READONLY_INFO_FIELDS
        }
        if not values or not self:
            return
        self.check_access("read")
        Info = self.env["l10n_de_euer.move.info"]
        Info.check_access("create")
        infos = Info._get_or_create(self)
        infos.check_access("write")

        def plain(value):
            return value.id if isinstance(value, models.BaseModel) else value

        changed = infos.filtered(
            lambda info: any(
                (plain(info[name]) or False) != (plain(value) or False)
                for name, value in values.items()
            )
        )
        changed.write({name: plain(value) for name, value in values.items()})
        self.invalidate_recordset(list(INFO_FIELDS) + ["l10n_de_euer_info_id"])

    def action_l10n_de_euer_slices(self):
        """Slices of this entry: as money entry or as document (spec 7.6 no. 5)."""
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_cash_slice"
        )
        action["domain"] = [
            "|",
            ("source_move_id", "in", self.ids),
            ("origin_move_id", "in", self.ids),
        ]
        action["context"] = {"search_default_group_year": 1}
        return action

    def action_l10n_de_euer_info(self):
        """Open the EÜR information of the entry (created if missing)."""
        self.ensure_one()
        info = self.env["l10n_de_euer.move.info"]._get_or_create(self)
        return {
            "type": "ir.actions.act_window",
            "res_model": "l10n_de_euer.move.info",
            "res_id": info.id,
            "view_mode": "form",
            "target": "current",
        }

    @api.model
    def _l10n_de_euer_info_field_names(self):
        return list(INFO_FIELDS)
