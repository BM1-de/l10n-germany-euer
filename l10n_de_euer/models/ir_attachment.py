# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import base64

from odoo import api, models
from odoo.exceptions import UserError
from odoo.fields import Domain

from .euer_statement import CLOSED_STATES

#: fields an attachment of a finalised statement may get written without
#: a change: a new access token; the content only unchanged (moving between
#: database and file store)
_FREE_FIELDS = frozenset({"access_token"})
_CONTENT_FIELDS = frozenset({"raw", "datas"})


class IrAttachment(models.Model):
    _inherit = "ir.attachment"

    def _l10n_de_euer_protected(self):
        """Attachments of finalised statements (spec 7.7, G-04, G-10): the
        payload of the finalisation and the receipts of its profit
        corrections and bank reconciliation."""
        if not self.ids:
            return self.browse()
        ids = set(
            self.env["l10n_de_euer.statement"]
            .sudo()
            .search(Domain("payload_attachment_id", "in", self.ids))
            .payload_attachment_id.ids
        )
        for model in ("l10n_de_euer.adjustment", "l10n_de_euer.cash.balance"):
            records = (
                self.env[model]
                .sudo()
                .search(
                    Domain("attachment_ids", "in", self.ids)
                    & Domain("statement_id.state", "in", list(CLOSED_STATES))
                )
            )
            ids |= set(records.attachment_ids.ids)
        return self.browse(sorted(ids & set(self.ids)))

    def _l10n_de_euer_raise(self):
        raise UserError(
            self.env._(
                "%(names)s belongs to a finalised EÜR statement and cannot be "
                "changed or deleted.",
                names=", ".join(self.mapped("name")),
            )
        )

    def write(self, vals):
        protected = self._l10n_de_euer_protected()
        for attachment in protected.sudo():
            for name, value in vals.items():
                if name in _FREE_FIELDS:
                    continue
                if name in _CONTENT_FIELDS:
                    content = value or b""
                    if name == "datas":
                        content = base64.b64decode(content)
                    elif isinstance(content, str):
                        content = content.encode()
                    if attachment._compute_checksum(content) == attachment.checksum:
                        continue
                    protected._l10n_de_euer_raise()
                field = attachment._fields.get(name)
                current = (
                    field.convert_to_write(attachment[name], attachment)
                    if field
                    else None
                )
                if not field or (current or False) != (value or False):
                    protected._l10n_de_euer_raise()
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_except_l10n_de_euer_final(self):
        protected = self._l10n_de_euer_protected()
        if protected:
            protected._l10n_de_euer_raise()
