# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from collections import defaultdict

from odoo import api, fields, models
from odoo.exceptions import ValidationError
from odoo.fields import Domain

from .engine import model as m
from .engine import vat

VAT_REGIMES = [("ku", "Small Business (§ 19 UStG)"), ("regular", "Standard Taxation")]
CASH_DATE_REASONS = [
    ("transfer_order", "Transfer Order Given to the Bank"),
    ("cheque", "Cheque"),
    ("tax_direct_debit", "Direct Debit of the Tax Office on the Due Date"),
    ("other", "Other"),
]
TAX_RATES = [("19", "19 %"), ("7", "7 %"), ("0", "0 % (Tax-Exempt)")]
#: move types whose VAT treatment is stored (documents with a date of supply)
DOCUMENT_TYPES = ("out_invoice", "out_refund", "in_invoice", "in_refund")
#: fields of the VAT treatment that trigger its recomputation
REGIME_FIELDS = ("date_from", "date_to", "regime", "basis", "company_id")


class L10nDeEuerMoveInfo(models.Model):
    """Additional EÜR data of a journal entry (spec 7.3, B-01).

    The module never writes into ``account.move`` or ``account.move.line``;
    the VAT treatment of a document, an overridden cash date, the economic
    year, the legal due date, the VAT return period and the VAT rate entered
    after exceeding the small business threshold are stored here.
    """

    _name = "l10n_de_euer.move.info"
    _inherit = ["mail.thread"]
    _description = "EÜR Information of a Journal Entry"
    _rec_name = "move_id"
    _order = "id desc"
    _check_company_auto = True

    move_id = fields.Many2one(
        "account.move",
        string="Journal Entry",
        required=True,
        index=True,
        ondelete="cascade",
        check_company=True,
    )
    company_id = fields.Many2one(
        related="move_id.company_id", store=True, index=True, precompute=True
    )
    partner_id = fields.Many2one(related="move_id.partner_id")
    move_date = fields.Date(related="move_id.date", string="Accounting Date")
    supply_date = fields.Date(
        compute="_compute_supply_date",
        string="Date of Supply",
        help="Date of supply, otherwise delivery date, invoice date or accounting "
        "date of the entry. It decides the VAT treatment of the document (R-25).",
    )
    vat_regime = fields.Selection(
        VAT_REGIMES,
        string="VAT Treatment",
        tracking=True,
        help="VAT treatment of the document after the VAT status periods on its "
        "date of supply (R-25). Recomputed when the periods change, unless it "
        "was set manually.",
    )
    vat_regime_manual = fields.Boolean(
        string="Set Manually",
        tracking=True,
        help="The VAT treatment was set by hand and is not recomputed.",
    )
    vat_regime_reason = fields.Text(
        string="Reason for the VAT Treatment", tracking=True
    )
    cash_date = fields.Date(
        string="Cash Date (EÜR)",
        tracking=True,
        help="Overrides the date of the bank statement as date of receipt or "
        "payment, e.g. the date the transfer order was given (H 11).",
    )
    cash_date_reason = fields.Selection(
        CASH_DATE_REASONS, string="Reason for the Cash Date", tracking=True
    )
    cash_date_note = fields.Text(string="Note on the Cash Date", tracking=True)
    economic_year = fields.Integer(
        tracking=True,
        help="Year the payment belongs to economically (ten-day rule, § 11 EStG). "
        "A year other than the year of payment makes a payment between 22 "
        "December and 10 January a candidate of the ten-day rule; unless the "
        "payment recurs regularly (flag on partner or category, steady "
        "payments), it is applied only after confirmation in the review list.",
    )
    due_date = fields.Date(
        string="Legal Due Date",
        tracking=True,
        help="Legal due date for the ten-day rule, if it differs from the due "
        "date of the invoice. It is not moved by § 108 (3) AO.",
    )
    vat_period = fields.Char(
        string="VAT Return Period",
        tracking=True,
        help="VAT return period of a tax payment or refund: 2026-12 (month), "
        "2026-Q4 (quarter) or 2026 (year).",
    )
    regular_tax_rate = fields.Selection(
        TAX_RATES,
        string="VAT Rate of the Supply",
        tracking=True,
        help="Under standard taxation a receipt for an invoice without VAT line "
        "(small business invoice, threshold exceeded) is split with this rate "
        "into net revenue and VAT (spec 4.4 no. 5). Correct the invoice, too.",
    )
    gift_bulk = fields.Boolean(
        string="Give-Aways (Bulk Booking)",
        tracking=True,
        help="Low-value give-aways of the same kind booked together without "
        "recipient (R 4.11 (2) EStR); the 50 € limit counts as kept.",
    )
    gift_recipient_id = fields.Many2one(
        "res.partner",
        string="Recipient of the Gifts",
        tracking=True,
        help="Person or business that receives the gifts of this document. The "
        "50 € limit is checked per recipient and year (§ 4 (5) S. 1 no. 1 EStG). "
        "The partner of an entry is the supplier or the shop that was paid, never "
        "the recipient; without a recipient a gift is blocked (C-51) unless the "
        "document is marked as give-aways. Several recipients on one document: "
        "enter them per line.",
    )
    gift_line_ids = fields.One2many(
        "l10n_de_euer.gift.line",
        "info_id",
        string="Recipients per Line",
        help="Recipients of the gifts per document line, if one document holds "
        "gifts for several recipients.",
    )
    gift_date = fields.Date(
        string="Date of the Gift",
        tracking=True,
        help="Date the gifts are handed over, if it differs from the date of "
        "supply; it decides the year of the 50 € limit (R 4.10 (2) EStR).",
    )

    _move_unique = models.Constraint(
        "UNIQUE(move_id)",
        "There is only one EÜR information per journal entry.",
    )

    @api.depends(
        "move_id.taxable_supply_date",
        "move_id.delivery_date",
        "move_id.invoice_date",
        "move_id.date",
    )
    def _compute_supply_date(self):
        for info in self:
            info.supply_date = self._regime_date(info.move_id)

    @api.model
    def _regime_date(self, move):
        """Date that decides the VAT treatment of a document (R-25)."""
        return (
            move.taxable_supply_date
            or move.delivery_date
            or move.invoice_date
            or move.date
        )

    @api.constrains("cash_date", "cash_date_reason")
    def _check_cash_date(self):
        for info in self:
            if info.cash_date and not info.cash_date_reason:
                raise ValidationError(
                    self.env._(
                        "Give a reason for the cash date of %(move)s.",
                        move=info.move_id.display_name,
                    )
                )

    @api.constrains("vat_regime_manual", "vat_regime", "vat_regime_reason")
    def _check_manual_regime(self):
        for info in self:
            if info.vat_regime_manual and not (
                info.vat_regime and info.vat_regime_reason
            ):
                raise ValidationError(
                    self.env._(
                        "A VAT treatment set by hand needs the treatment and a "
                        "reason (%(move)s).",
                        move=info.move_id.display_name,
                    )
                )

    @api.constrains("vat_period")
    def _check_vat_period(self):
        for info in self:
            if info.vat_period and vat.parse_vat_period(info.vat_period) is None:
                raise ValidationError(
                    self.env._(
                        "%(period)s is not a VAT return period; use e.g. 2026-12, "
                        "2026-Q4 or 2026.",
                        period=info.vat_period,
                    )
                )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("vat_regime_manual") or "vat_regime" in vals:
                continue
            move = self.env["account.move"].browse(vals.get("move_id"))
            if move:
                vals["vat_regime"] = self._regime_of_move(move)
        return super().create(vals_list)

    def write(self, vals):
        result = super().write(vals)
        if vals.get("vat_regime_manual") is False:
            self._update_regime()
        return result

    # ------------------------------------------------------------ VAT regime
    @api.model
    def _regime_of_move(self, move):
        """VAT treatment of a document after the status periods (R-25)."""
        if move.move_type not in DOCUMENT_TYPES:
            return False
        root = move.company_id.root_id
        return (
            self.env["l10n_de_euer.vat.period"]
            .sudo()
            ._get_regime(root, self._regime_date(move))
        )

    def _update_regime(self, tracking=True):
        """Recompute the VAT treatment of records not set by hand (UT-KU-8).

        ``tracking=False`` writes without a message per record: a change of
        the VAT status periods can touch thousands of documents, so the
        status period logs it once (:meth:`_recompute_regimes`).
        """
        periods = {}
        updates = defaultdict(lambda: self.browse())
        for info in self.filtered(lambda i: not i.vat_regime_manual):
            move = info.move_id
            regime = False
            if move.move_type in DOCUMENT_TYPES:
                root = move.company_id.root_id
                if root not in periods:
                    periods[root] = self._engine_periods(root)
                period = vat.period_at(periods[root], self._regime_date(move))
                regime = period.regime if period else False
            if (info.vat_regime or False) != regime:
                updates[regime] |= info
        changed = self.browse()
        for regime, infos in updates.items():
            if not tracking:
                infos = infos.with_context(tracking_disable=True)
            infos.write({"vat_regime": regime})
            changed |= infos
        return changed.with_env(self.env)

    @api.model
    def _recompute_regimes(self, companies):
        """Recompute the VAT treatment after a change of the status periods.

        Returns the changed records; they are written without a message of
        their own, the status period logs the change once.
        """
        roots = companies.root_id
        infos = self.sudo().search(
            Domain("company_id", "child_of", roots.ids)
            & Domain("vat_regime_manual", "=", False)
        )
        return infos._update_regime(tracking=False)

    @api.model
    def _get_or_create(self, moves):
        """Return the information records of ``moves``, created if missing.

        The VAT treatment of new documents is computed with the status
        periods loaded once per main company (no search per record).
        """
        existing = self.sudo().search(Domain("move_id", "in", moves.ids))
        missing = moves - existing.move_id
        periods = {}
        vals_list = []
        for move in missing:
            vals = {"move_id": move.id}
            if move.move_type in DOCUMENT_TYPES:
                root = move.company_id.root_id
                if root not in periods:
                    periods[root] = self._engine_periods(root)
                period = vat.period_at(periods[root], self._regime_date(move))
                vals["vat_regime"] = period.regime if period else False
            vals_list.append(vals)
        created = (
            self.sudo()
            .with_context(mail_create_nolog=True, mail_create_nosubscribe=True)
            .create(vals_list)
        )
        return (existing | created).with_env(self.env)

    @api.model
    def _engine_periods(self, root):
        """VAT status periods of a main company as engine data."""
        return [
            m.VatPeriod(
                date_from=period.date_from,
                date_to=period.date_to or None,
                regime=period.regime,
            )
            for period in self.env["l10n_de_euer.vat.period"]
            .sudo()
            .search(Domain("company_id", "=", root.id))
        ]

    def action_open_move(self):
        self.ensure_one()
        return self.move_id._get_records_action()
