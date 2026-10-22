# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from werkzeug.exceptions import NotFound

from odoo import http
from odoo.http import content_disposition, request

from ..models.euer_statement_output import EXPORTS


class EuerExportController(http.Controller):
    """Downloads of a statement: workbook, evidence, check protocol, list of
    the Kennzahlen, data set and data access package (spec 7.5).

    The statement is read with the rights of the user: record rules keep
    the businesses apart (spec 7.9).
    """

    @http.route(
        "/l10n_de_euer/statement/<int:statement_id>/<string:kind>",
        type="http",
        auth="user",
        methods=["GET"],
    )
    def download(self, statement_id, kind, **kwargs):
        if kind not in EXPORTS:
            raise NotFound()
        statement = request.env["l10n_de_euer.statement"].browse(statement_id).exists()
        if not statement:
            raise NotFound()
        file_name, mimetype, content = statement._get_export(kind)
        return request.make_response(
            content,
            headers=[
                ("Content-Type", mimetype),
                ("Content-Length", str(len(content))),
                ("Content-Disposition", content_disposition(file_name)),
                ("X-Content-Type-Options", "nosniff"),
            ],
        )
