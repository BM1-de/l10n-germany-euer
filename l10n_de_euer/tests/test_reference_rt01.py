# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""RT-01: synthetic case of standard taxation (spec 9.8, decisions D6, D7).

A photo and design studio under standard taxation in SKR03, 2025 and 2026,
taken over from another system on 1 January 2025 with an open receivable,
an open payable and an asset. The case covers the paths of RT-01: Kz 112,
103, 140, 141, 185, 186, the sale of an asset (Kz 102), Kz 106, 108, 122,
123, the ten-day rule (VAT returns with and without permanent extension,
rent without flags), credit notes, a partial payment over the turn of the
year, discounts, a returned direct debit, an advance payment, private
matters (private share, paid privately, withdrawal of an asset with VAT),
business entertainment 70/30, gifts below and above 50 €, assets (linear,
low-value asset, asset pool, disposal) and the opening with old items.

The business events and the expected values with their hand calculation
are in ``data/rt01.json``. The tax advisor confirms them (D7); until then
``confirmation`` in the file is empty. Kz 104 (agriculture and forestry,
§ 24 UStG) cannot occur in this business: its own test shows that such
income is outside the program scope.
"""

from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from .reference_common import ReferenceCase, amount
from .scenario_common import SKR03_ACCOUNTS, SKR03_TAXES


@tagged("post_install_l10n", "post_install", "-at_install")
class TestReferenceRt01(ReferenceCase, AccountTestInvoicingCommon):
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = dict(SKR03_TAXES, farmer19="tax_ust_19_farmer_skr03")
    DATA = "rt01.json"

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_finalisation()

    def test_rt01_statements(self):
        """Every field of the statements 2025 and 2026 against the values
        calculated by hand; no blocker of the calculation."""
        statements = self.run_case()
        self.assertEqual(self.load_case()["confirmation"], {"by": None, "on": None})
        self.assertTrue(statements)

    def test_rt01_kz104_luf_is_blocked(self):
        """Kz 104: income under the average rates of § 24 UStG belongs to
        agriculture and forestry (Anleitung EÜR line 14), outside the program
        scope (spec 1.2): the field is computed and the statement blocked
        (C-40), on the revenue account of § 24 (8190) as well as through the
        tax of farmers on a standard revenue account (tag 76)."""
        self.set_regime("regular")
        luf = self.invoice(
            "out_invoice",
            "2026-03-02",
            [(self.template_account("account_8190"), 1000, ())],
        )
        self.bank("2026-03-10", 1000, matches=[(luf, 1000)])
        statement = self.statement()
        statement.action_compute()
        self.assertEqual(self.value(statement, "EUER.20.104"), amount(1000))
        statement.action_check()
        self.assertIn("C-40", self.result_codes(statement, "blocker"))
        self.assertTrue(
            any(
                "EUER.20.104" in message
                for message in self.results(statement, "C-40").mapped("message")
            )
        )

    def test_rt01_kz104_farmer_tax_is_blocked(self):
        """The tax of farmers (UStVA tag 76) on a standard revenue account
        gives Kz 104 as well and blocks like the account of § 24."""
        self.set_regime("regular")
        sale = self.invoice(
            "out_invoice", "2026-03-02", [("revenue_19", 1000, ["farmer19"])]
        )
        self.bank("2026-03-10", 1190, matches=[(sale, 1190)])
        statement = self.statement()
        statement.action_compute()
        self.assertEqual(self.value(statement, "EUER.20.104"), amount(1000))
        statement.action_check()
        self.assertIn("C-40", self.result_codes(statement, "blocker"))
        self.assertTrue(
            any(
                "EUER.20.104" in message
                for message in self.results(statement, "C-40").mapped("message")
            )
        )
