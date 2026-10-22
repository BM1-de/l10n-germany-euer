# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import models

from .euer_account_assignment import DEFAULT_CHARTS


class AccountChartTemplate(models.AbstractModel):
    _inherit = "account.chart.template"

    def _post_load_data(self, template_code, company, template_data):
        """Add the default EÜR assignments of new SKR03/SKR04 companies (K-02).

        Only missing assignments are created. This is not template data of the
        chart (``@template``): reloading the chart would otherwise overwrite
        corrections of the user and the history of periods (spec 4.3).
        """
        result = super()._post_load_data(template_code, company, template_data)
        if template_code in DEFAULT_CHARTS:
            self.env["l10n_de_euer.account.assignment"]._prefill_defaults(company)
        return result
