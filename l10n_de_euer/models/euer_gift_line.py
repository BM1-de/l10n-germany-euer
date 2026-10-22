# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models
from odoo.exceptions import ValidationError


class L10nDeEuerGiftLine(models.Model):
    """Recipient of the gifts of one document line (spec 5.7 no. 7).

    One invoice can hold gifts for several recipients (e.g. gift baskets
    for different customers). The 50 € limit is checked per recipient and
    year, so the recipients are entered per line; a line given to several
    recipients is split by ``quantity`` (number or share of the gifts).
    Without lines of its own a document line goes to the recipient of the
    document. The journal item itself is never written (B-01).
    """

    _name = "l10n_de_euer.gift.line"
    _description = "EÜR Recipient of Gifts per Line"
    _order = "move_line_id, id"
    _check_company_auto = True

    info_id = fields.Many2one(
        "l10n_de_euer.move.info",
        string="EÜR Information",
        required=True,
        index=True,
        ondelete="cascade",
    )
    move_id = fields.Many2one(
        related="info_id.move_id", store=True, index=True, string="Journal Entry"
    )
    company_id = fields.Many2one(
        related="info_id.company_id", store=True, index=True, precompute=True
    )
    move_line_id = fields.Many2one(
        "account.move.line",
        string="Gift Line",
        required=True,
        index=True,
        ondelete="cascade",
        domain="[('move_id', '=', move_id), ('display_type', '=', 'product')]",
        help="Line of the document with the gifts.",
    )
    recipient_id = fields.Many2one(
        "res.partner",
        required=True,
        help="Person or business that receives the gifts of the line.",
    )
    quantity = fields.Float(
        string="Number or Share",
        default=1.0,
        digits=(16, 2),
        help="Number or share of the gifts of the line this recipient receives. "
        "A line with several recipients is split in proportion.",
    )
    gift_date = fields.Date(
        string="Date of the Gift",
        help="Date the gift is handed over, if it differs from the date of the "
        "document; it decides the year of the 50 € limit.",
    )

    @api.constrains("move_line_id", "info_id", "quantity")
    def _check_line(self):
        for line in self:
            if line.move_line_id.move_id != line.info_id.move_id:
                raise ValidationError(
                    self.env._(
                        "%(line)s is not a line of %(move)s.",
                        line=line.move_line_id.display_name,
                        move=line.info_id.move_id.display_name,
                    )
                )
            if line.quantity <= 0:
                raise ValidationError(
                    self.env._("The number or share of the gifts must be positive.")
                )
