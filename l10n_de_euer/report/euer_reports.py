# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Values of the QWeb reports of a statement (spec 7.5, 7.12).

The reports only present what the statement prepares
(:mod:`..models.euer_statement_output`); the HTML view is the accessible
main output, the PDF is printed from the same template.
"""

from odoo import api, models


class _StatementReport(models.AbstractModel):
    _name = "report.l10n_de_euer.statement_report_base"
    _description = "EÜR Statement Report Base"

    #: method of the statement that prepares the values of one statement
    _data_method = None

    @api.model
    def _get_report_values(self, docids, data=None):
        statements = self.env["l10n_de_euer.statement"].browse(docids)
        outputs = {
            statement.id: getattr(statement, self._data_method)()
            for statement in statements
        }
        return {
            "doc_ids": docids,
            "doc_model": "l10n_de_euer.statement",
            "docs": statements,
            "outputs": outputs,
        }


class ReportFillAid(models.AbstractModel):
    _name = "report.l10n_de_euer.report_fill_aid"
    _inherit = "report.l10n_de_euer.statement_report_base"
    _description = "EÜR Fill-In Aid for Mein ELSTER"
    _data_method = "_get_fill_aid_data"


class ReportEvidence(models.AbstractModel):
    _name = "report.l10n_de_euer.report_evidence"
    _inherit = "report.l10n_de_euer.statement_report_base"
    _description = "EÜR Evidence per Kennzahl"
    _data_method = "_get_evidence_data"


class ReportCheckProtocol(models.AbstractModel):
    _name = "report.l10n_de_euer.report_check_protocol"
    _inherit = "report.l10n_de_euer.statement_report_base"
    _description = "EÜR Check Protocol"
    _data_method = "_get_check_data"


class ReportProcedure(models.AbstractModel):
    _name = "report.l10n_de_euer.report_procedure"
    _inherit = "report.l10n_de_euer.statement_report_base"
    _description = "EÜR Procedure Documentation"
    _data_method = "_get_procedure_data"
