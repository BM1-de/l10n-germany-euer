# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Phase F: the asset register in the calculation of the year (spec 5.8).

The purchase of an asset is neutral in the payment calculation; its
depreciation and low-value assets come from the register only, also if
depreciation is booked in the ledger. The register belongs to the business:
a branch that is a business of its own has its own register (spec 7.9).
The cases run on SKR03 and SKR04 with a small business; all amounts are
synthetic (D6).
"""

from decimal import Decimal

from odoo import Command
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from .scenario_common import (
    SKR03_ACCOUNTS,
    SKR03_TAXES,
    SKR04_ACCOUNTS,
    SKR04_TAXES,
    ScenarioCommon,
)

D = Decimal
DEPRECIATION = "EUER.25.130"
LOW_VALUE = "EUER.25.132"


class PhaseFScenarios(ScenarioCommon):
    def register(self, lines=None, company=None, **values):
        """Active asset of the register, linked to the journal items ``lines``."""
        vals = {
            "name": "Testgerät",
            "company_id": (company or self.company).id,
            "delivery_date": "2026-07-15",
            "cost_net": 2150.0,
            "method": "linear",
            "useful_life_months": 84,
            "aveuer_group": "other_movable",
            **values,
        }
        if lines:
            vals["move_line_ids"] = [Command.set(lines.ids)]
        asset = self.env["l10n_de_euer.asset"].create(vals)
        asset.action_activate()
        return asset

    def register_values(self, year, company=None):
        return {
            code: value
            for code, value in self.env["l10n_de_euer.asset"]
            ._get_field_contributions(company or self.company, year)
            .items()
            if value
        }

    def test_purchase_and_depreciation_from_the_register(self):
        """T13 with the register: 2,150.00 paid in July, 84 months; 6 months
        in 2026 = 2,150.00 / 84 × 6 = 153.57 (Kz 130). The payment and the
        depreciation booked in the ledger add nothing."""
        bill = self.purchase("2026-07-15", 2150, account="asset")
        self.bank("2026-07-20", -2150, matches=[(bill, 2150)])
        self.entry("2026-12-31", [("depreciation", 153.57), ("asset", -153.57)])
        self.register(bill.invoice_line_ids)
        expected = self.register_values(2026)
        self.assertTrue(expected)
        self.assertEqual(self.fields(2026), expected)
        run = self.compute(2026)
        self.assertEqual(D(run.form_values[DEPRECIATION]), D("153.57"))
        self.assertEqual(self.codes(2026, "blocker"), [])
        # the following year: 12 months = 307.14
        self.assertEqual(D(self.compute(2027).form_values[DEPRECIATION]), D("307.14"))

    def test_low_value_asset_counts_once(self):
        """GWG of 600.00 (small business: gross = cost) booked on the GWG
        account and paid: Kz 132 = 600.00 from the register, not twice."""
        bill = self.purchase("2026-03-10", 600, account="gwg")
        self.bank("2026-03-12", -600, matches=[(bill, 600)])
        self.register(
            bill.invoice_line_ids,
            asset_kind="gwg",
            method="none",
            cost_net=600.0,
            delivery_date="2026-03-10",
        )
        self.assertEqual(self.fields(2026), {LOW_VALUE: D("600.00")})

    def test_compute_wizard_runs_the_asset_checks(self):
        """The method "compute year" also checks the register against the
        computed form values; an addition without register entry is C-64."""
        bill = self.purchase("2026-07-15", 2150, account="asset")
        self.bank("2026-07-20", -2150, matches=[(bill, 2150)])
        wizard = self.env["l10n_de_euer.compute.year"].create(
            {"company_id": self.company.id, "year": 2026}
        )
        wizard.action_compute()
        Check = self.env["l10n_de_euer.check"]
        domain = [
            ("company_id", "=", self.company.id),
            ("year", "=", 2026),
            ("origin", "=", "asset"),
        ]
        self.assertIn("C-64", Check.search(domain).mapped("code"))
        self.register(bill.invoice_line_ids)
        wizard.action_compute()
        self.assertNotIn("C-64", Check.search(domain).mapped("code"))
        self.assertNotIn("C-62", Check.search(domain).mapped("code"))

    def test_separate_business_has_its_own_register(self):
        """An asset of a branch that is a business of its own belongs to that
        branch and counts only there; an asset of an ordinary branch belongs
        to the main company."""
        own = self.env["res.company"].create(
            {
                "name": "Own Business",
                "parent_id": self.company.id,
                "l10n_de_euer_separate_business": True,
            }
        )
        branch = self.env["res.company"].create(
            {"name": "Branch", "parent_id": self.company.id}
        )
        self.env.user.company_ids |= own | branch
        main_asset = self.register(company=branch, cost_net=840.0)
        own_asset = self.register(company=own, cost_net=1680.0)
        self.assertEqual(main_asset.company_id, self.company)
        self.assertEqual(own_asset.company_id, own)
        main_run = self.Run._compute_year(self.company, 2026)
        own_run = self.Run._compute_year(own, 2026)
        # 6 of 84 months
        self.assertEqual(D(main_run.form_values[DEPRECIATION]), D("60.00"))
        self.assertEqual(D(own_run.form_values[DEPRECIATION]), D("120.00"))
        self.assertEqual(
            self.register_values(2026, own),
            {
                code: D(value) / 100
                for code, value in own_run.totals.items()
                if code in self.register_values(2026, own)
            },
        )


@tagged("post_install_l10n", "post_install", "-at_install")
class TestPhaseFSkr03(PhaseFScenarios, AccountTestInvoicingCommon):
    allow_inherited_tests_method = True
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_euer()


@tagged("post_install_l10n", "post_install", "-at_install")
class TestPhaseFSkr04(PhaseFScenarios, AccountTestInvoicingCommon):
    allow_inherited_tests_method = True
    CHART = "de_skr04"
    ACCOUNTS = SKR04_ACCOUNTS
    TAXES = SKR04_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr04")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_euer()
