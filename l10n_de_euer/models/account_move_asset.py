# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Vouchers of assets in the register cannot be deleted (G-10, AT-AUF-1).

The retention period of the vouchers does not end as long as they matter for
taxes with an open assessment period (§ 147 (3) sentence 5 AO); an asset in
the register is depreciated on their basis. These extensions only read: the
module never writes journal entries (B-01).
"""

from odoo import api, models
from odoo.exceptions import UserError
from odoo.fields import Domain


def _check_vouchers(env, lines, moves=None):
    """Raise if ``lines`` or ``moves`` are vouchers of an asset in the register."""
    if not lines and not moves:
        return
    domain = Domain.FALSE
    if lines:
        domain |= Domain("move_line_ids", "in", lines.ids)
    if moves:
        domain |= Domain("disposal_invoice_id", "in", moves.ids)
    assets = (
        env["l10n_de_euer.asset"].sudo().search(Domain("state", "!=", "draft") & domain)
    )
    if assets:
        raise UserError(
            env._(
                "The entry is a voucher of the asset %(asset)s in the EÜR asset "
                "register and cannot be deleted.",
                asset=", ".join(assets.mapped("display_name")),
            )
        )


class AccountMove(models.Model):
    # kept apart from account_move.py like the other extensions of the asset
    # register (*_asset.py): protection of the vouchers, see the docstring
    _inherit = "account.move"  # pylint: disable=consider-merging-classes-inherited

    @api.ondelete(at_uninstall=False)
    def _unlink_except_l10n_de_euer_asset(self):
        # the journal items of the purchase are protected on their own (the
        # entry deletes them first); here: the sales invoice of a disposal
        _check_vouchers(self.env, self.env["account.move.line"], self)


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    @api.ondelete(at_uninstall=False)
    def _unlink_except_l10n_de_euer_asset(self):
        # tax lines are recreated while a draft is edited; base lines are not
        _check_vouchers(self.env, self.filtered(lambda line: not line.tax_line_id))
