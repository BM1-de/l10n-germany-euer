# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import fields, models


class AccountJournal(models.Model):
    _inherit = "account.journal"

    l10n_de_euer_private_account = fields.Boolean(
        string="Private Account (EÜR)",
        tracking=True,
        help="The money journal is a private account; there is no separate "
        "business account. Every business income received on it is also a "
        "withdrawal, every business expense paid from it also a contribution "
        "(R-18). Bookings of this journal on private accounts are not counted "
        "a second time.",
    )
