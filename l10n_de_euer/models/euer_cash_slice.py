# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain

SLICE_KINDS = [
    ("doc_via_payment", "Document Paid"),
    ("cash_direct", "Booked on the Money Entry"),
    ("transit", "Payment Entry"),
    ("unallocated", "Unallocated Rest"),
    ("classified", "Classified Rest"),
    ("unclear", "Unclear (Bank Suspense)"),
    ("opening", "Open Item Taken Over"),
    ("neutralized", "Neutralised"),
    ("booking", "Realised on the Booking Date"),
    ("overflow", "Negative Income Moved (R-12)"),
    ("non_cash", "Not From Money"),
    ("private", "Private Money Line"),
]
RULES = [
    ("mapping", "Category Mapping"),
    ("revenue", "Revenue"),
    ("tax", "Tax"),
    ("gift", "Gift"),
    ("private", "Private"),
    ("private_journal", "Private Account"),
    ("settlement", "VAT Payment or Refund"),
    ("overflow", "Overflow"),
    ("asset", "Asset Register"),
    ("adjustment", "Profit Correction"),
    ("none", "No Form Field"),
]
#: context key: only the calculation creates and deletes slices and runs,
#: always as superuser (a client cannot set ``env.su`` through RPC)
INTERNAL = "l10n_de_euer_slice_internal"


class L10nDeEuerCashSlice(models.Model):
    """Realised amount of a journal item at its cash date (spec 7.3).

    One record per form field a slice reaches (``field_code``); slices that
    reach no field (neutral, fixed asset, unallocated ...) have one record
    without field. ``amount`` is the value in the field (income and expense
    positive), ``balance`` the realised amount with the sign of the journal
    item (debit positive). Created by the calculation only.
    """

    _name = "l10n_de_euer.cash.slice"
    _description = "EÜR Slice"
    _order = "date, id"
    _check_company_auto = True

    run_id = fields.Many2one(
        "l10n_de_euer.run", required=True, index=True, ondelete="cascade"
    )
    company_id = fields.Many2one("res.company", required=True, index=True)
    date = fields.Date(
        required=True, help="Cash date, or the booking date of a booked entry."
    )
    attribution_year = fields.Integer(
        string="Year", required=True, help="Year of attribution (ten-day rule)."
    )
    kind = fields.Selection(SLICE_KINDS, required=True)
    rule = fields.Selection(RULES, default="none")
    # Journal items of statement entries are deleted and created again when a
    # statement line is reconciled anew; the slices of a stored run keep their
    # reference as snapshot (ids and names) and are never deleted with them.
    source_move_id = fields.Many2one(
        "account.move", string="Money Entry", index=True, ondelete="set null"
    )
    source_line_id = fields.Many2one(
        "account.move.line", string="Money Line", ondelete="set null"
    )
    origin_move_id = fields.Many2one(
        "account.move", string="Document", index=True, ondelete="set null"
    )
    origin_line_id = fields.Many2one(
        "account.move.line", string="Document Line", ondelete="set null"
    )
    document_move_id = fields.Many2one(
        "account.move",
        string="Entry Reached",
        ondelete="set null",
        help="Entry the money reached: the document of the open item, the money "
        "entry itself or the payment entry.",
    )
    source_move_ref = fields.Integer(
        string="Money Entry ID", help="Snapshot of the money entry at the run."
    )
    source_line_ref = fields.Integer(string="Money Line ID")
    origin_move_ref = fields.Integer(
        string="Document ID", help="Snapshot of the document at the run."
    )
    origin_line_ref = fields.Integer(string="Document Line ID")
    document_move_ref = fields.Integer(string="Entry Reached ID")
    source_move_name = fields.Char(string="Money Entry Name")
    origin_move_name = fields.Char(string="Document Name")
    account_id = fields.Many2one("account.account")
    # the payload of a finalisation refers to partners and categories: they
    # cannot be deleted while a slice refers to them (G-04)
    partner_id = fields.Many2one("res.partner", index=True, ondelete="restrict")
    tax_repartition_line_id = fields.Many2one("account.tax.repartition.line")
    category_id = fields.Many2one("l10n_de_euer.category")
    field_code = fields.Char(string="Field", index=True)
    field_id = fields.Many2one("l10n_de_euer.field", string="Form Field")
    kz = fields.Char(related="field_id.kz", string="Kz")
    currency_id = fields.Many2one(related="company_id.currency_id")
    amount = fields.Monetary(
        currency_field="currency_id",
        help="Amount in the form field: income and expense positive.",
    )
    balance = fields.Monetary(
        currency_field="currency_id",
        help="Realised amount with the sign of the journal item (debit positive).",
    )
    debit = fields.Monetary(currency_field="currency_id")
    credit = fields.Monetary(currency_field="currency_id")
    path = fields.Json(
        string="Partials", help="Partial reconciliations the money passed."
    )
    path_text = fields.Char(string="Path", compute="_compute_path_text")
    override = fields.Boolean(help="The cash date was set by hand.")
    ten_day_applied = fields.Boolean(string="Ten-Day Rule Applied")
    virtual_part = fields.Selection(
        [("net", "Net"), ("tax", "VAT")],
        help="Part of an open item taken over or of a classified rest.",
    )
    adjustment_id = fields.Many2one(
        "l10n_de_euer.adjustment",
        string="Profit Correction",
        index="btree_not_null",
        ondelete="set null",
        help="Profit correction the value comes from (own receipt).",
    )
    gift_recipient_id = fields.Many2one(
        "res.partner",
        string="Recipient of the Gift",
        ondelete="restrict",
        help="Recipient of a gift; the 50 € limit counts per recipient and year.",
    )
    gift_date = fields.Date(
        string="Date of the Gift", help="Date the gift is handed over."
    )
    gift_bulk = fields.Boolean(
        string="Give-Away", help="Give-away without recipient (R 4.11 (2) EStR)."
    )
    partial_ids = fields.Many2many(
        "account.partial.reconcile",
        string="Partial Reconciliations",
        compute="_compute_partial_ids",
        help="Partial reconciliations the money passed, in their order.",
    )
    attachment_count = fields.Integer(
        string="Attachments", compute="_compute_attachment_count"
    )

    _company_year_field = models.Index("(company_id, attribution_year, field_code)")

    @api.depends("path")
    def _compute_path_text(self):
        for one in self:
            one.path_text = " → ".join(str(item) for item in one.path or ())

    @api.depends("path")
    def _compute_partial_ids(self):
        # a partial deleted by a new reconciliation stays in the path (text)
        Partial = self.env["account.partial.reconcile"]
        for one in self:
            ids = [item for item in one.path or () if isinstance(item, int)]
            one.partial_ids = Partial.browse(ids).exists()

    def _get_moves(self):
        """Entries of the slices: money entry, document and entry reached."""
        return (
            self.source_move_id | self.origin_move_id | self.document_move_id
        ).exists()

    def _attachment_domain(self):
        return Domain("res_model", "=", "account.move") & Domain(
            "res_id", "in", self._get_moves().ids
        )

    def _compute_attachment_count(self):
        Attachment = self.env["ir.attachment"]
        readable = Attachment.has_access("read")
        for one in self:
            one.attachment_count = (
                Attachment.search_count(one._attachment_domain())
                if readable and one._get_moves()
                else 0
            )

    @api.model
    def _is_internal(self):
        return self.env.su and bool(self.env.context.get(INTERNAL))

    def _check_not_locked(self):
        """Slices of a finalised run never change (spec 7.7, G-04), also not
        for an administrator or the calculation itself."""
        if self.run_id.filtered("locked"):
            raise UserError(
                self.env._(
                    "The slices belong to a finalised statement and cannot be "
                    "changed or deleted."
                )
            )

    @api.model_create_multi
    def create(self, vals_list):
        if not self._is_internal():
            raise UserError(self.env._("Slices are created by the calculation."))
        runs = self.env["l10n_de_euer.run"].browse(
            {vals["run_id"] for vals in vals_list if vals.get("run_id")}
        )
        if runs.filtered("locked"):
            raise UserError(
                self.env._(
                    "The run belongs to a finalised statement; it gets no new slices."
                )
            )
        return super().create(vals_list)

    def write(self, vals):
        self._check_not_locked()
        if "run_id" in vals and any(one.run_id.id != vals["run_id"] for one in self):
            # a slice never moves to another (possibly finalised) run
            raise UserError(
                self.env._("A slice belongs to its run and cannot be moved.")
            )
        if not self._is_internal():
            raise UserError(
                self.env._("Slices cannot be changed; compute the year again.")
            )
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_only_by_calculation(self):
        self._check_not_locked()
        if not self._is_internal():
            raise UserError(self.env._("Slices are deleted by the calculation."))

    def action_open_source(self):
        """Money entry of the slice (spec 7.6 no. 2)."""
        self.ensure_one()
        return self.source_move_id._get_records_action()

    def action_open_origin(self):
        """Original document of the slice (spec 7.6 no. 2)."""
        self.ensure_one()
        return self.origin_move_id._get_records_action()

    def action_open_attachments(self):
        """Files attached to the money entry and the document (receipts)."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Receipts"),
            "res_model": "ir.attachment",
            "view_mode": "list,form",
            "domain": list(self._attachment_domain()),
            "context": {"create": False},
        }

    def action_open_path(self):
        """Partial reconciliations the money passed (spec 7.6 no. 2)."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": self.env._("Path of the Money"),
            "res_model": "account.partial.reconcile",
            "view_mode": "list",
            "views": [
                (
                    self.env.ref(
                        "l10n_de_euer.l10n_de_euer_partial_reconcile_view_list"
                    ).id,
                    "list",
                )
            ],
            "domain": [("id", "in", self.partial_ids.ids)],
            "context": {"create": False},
        }

    def action_open_adjustment(self):
        """Profit correction of the value and its own receipt (7.6 no. 4)."""
        self.ensure_one()
        return self.adjustment_id._get_records_action()

    def action_open_assets(self):
        """Yearly values of the assets behind a value of the asset register
        (spec 7.6 no. 3)."""
        self.ensure_one()
        return self.env["l10n_de_euer.asset"]._action_open_field_sources(
            self.company_id, self.attribution_year, {self.field_code}
        )
