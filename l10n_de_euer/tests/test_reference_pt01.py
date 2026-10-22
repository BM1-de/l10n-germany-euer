# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""PT-01: synthetic small business case (spec 9.8, decision D6).

A designer as small business (§ 19 UStG) in SKR03 over the turn of the year
2025/2026: invoices and payments, costs on the single lines of the form, two
linear assets and an asset taken over from another system with a memo value
(R-49, C-63), withdrawals and contributions (Kz 122/123). The business
events and the expected value of every field with a value, each with its
hand calculation, are in ``data/pt01.json``; the statements of both years
are computed with ``l10n_de_euer.statement`` and compared field by field.
"""

from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from .reference_common import ReferenceCase
from .scenario_common import SKR03_ACCOUNTS, SKR03_TAXES


@tagged("post_install_l10n", "post_install", "-at_install")
class TestReferencePt01(ReferenceCase, AccountTestInvoicingCommon):
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES
    DATA = "pt01.json"

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_finalisation()

    def test_pt01_statements(self):
        """Every field of the statements 2025 and 2026 against the values
        calculated by hand; no blocker of the calculation, the memo value of
        the asset taken over is reported (C-63)."""
        self.run_case()
