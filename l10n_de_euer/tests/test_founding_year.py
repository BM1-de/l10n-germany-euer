# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Small business limit in the year of foundation and the year after (R-27).

§ 19 (1) UStG in the version from 2025: a small business stays exempt if the
total turnover of the previous year did not exceed 25,000 € and that of the
current year does not exceed 100,000 €. A business that starts its activity
in the year has no previous year; for the year of the start the limit of the
current year is 25,000 € (section 19.1 (3) UStAE, steuerrecht.md K-01). The
year after is checked like every year: the actual turnover of the year of
foundation against 25,000 € (without conversion into a turnover of a full
year, which the version until 2024 required) and the current year against
100,000 €. Already the receipt with which the limit is exceeded is taxable
(section 19.1 (2) sentence 4 UStAE): C-32 blocks with that receipt.

The start of the activity is entered at the main company (setup wizard).
Synthetic amounts (D6), SKR03.
"""

from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from .scenario_common import SKR03_ACCOUNTS, SKR03_TAXES, ScenarioCommon

LIMIT_KEYS = (
    "ku_limit_near",
    "ku_limit_previous_year",
    "ku_limit_exceeded",
    "ku_limit_exceeded_period_set",
    "ku_limit_forecast_exceeded",
)


@tagged("post_install_l10n", "post_install", "-at_install")
class TestFoundingYear(ScenarioCommon, AccountTestInvoicingCommon):
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_euer()

    def receipts(self, year, amounts, month=5):
        """Small business invoices paid on the 10th of consecutive months."""
        for index, amount in enumerate(amounts):
            day = f"{year}-{month + index:02d}"
            invoice = self.sale(f"{day}-01", amount)
            self.bank(f"{day}-10", amount, matches=[(invoice, amount)])

    def limit(self, year):
        """Limit of the current year in cent and the C-32 findings of the
        limit check (key, severity, amount in cent)."""
        result = self.Run._engine_result(self.company, year)
        findings = sorted(
            (finding.key, finding.severity, finding.amount)
            for finding in result.findings
            if finding.code == "C-32" and finding.key in LIMIT_KEYS
        )
        return result.turnover.limit, findings

    def found(self, day="2025-04-01"):
        """Business founded on ``day``: start of the activity and a small
        business period from that day."""
        self.company.l10n_de_euer_activity_start = day
        self.set_regime("ku", date_from=day)

    # ------------------------------------------------------------ foundation
    def test_founding_year_limit_25000(self):
        """Year of foundation 2025: limit 25,000 €. 21,000 € reach 80 % of it
        (20,000 €, warning); the receipt of 4,000.01 € that brings the total
        to 25,000.01 € is the first taxable one (blocker on its date). Without
        the start of the activity the limit of 100,000 € applies."""
        self.found()
        self.receipts(2025, [15000, 6000])
        self.assertEqual(
            self.limit(2025),
            (2500000, [("ku_limit_near", "warning", 2100000)]),
        )
        self.receipts(2025, [4000.01], month=7)
        self.assertEqual(
            self.limit(2025),
            (2500000, [("ku_limit_exceeded", "blocker", 2500001)]),
        )
        result = self.Run._engine_result(self.company, 2025)
        self.assertEqual(str(result.turnover.crossing_date), "2025-07-10")
        self.compute(2025)
        self.assertIn("C-32", self.codes(2025, "blocker"))
        self.company.l10n_de_euer_activity_start = False
        self.assertEqual(self.limit(2025), (10000000, []))
        self.compute(2025)
        self.assertNotIn("C-32", self.codes(2025))

    def test_start_on_first_of_january_is_year_of_foundation(self):
        """The year of the start is the year of foundation also if the
        activity starts on 1 January (section 19.1 (3) UStAE: 'im Jahr der
        Aufnahme der Tätigkeit')."""
        self.found("2026-01-01")
        self.receipts(2026, [20000])
        self.assertEqual(
            self.limit(2026), (2500000, [("ku_limit_near", "warning", 2000000)])
        )

    def test_no_previous_year_in_the_year_of_foundation(self):
        """The year of foundation has no previous year: a turnover entered on
        the period by mistake does not count as previous year (it would block
        the status, 30,000 € > 25,000 €)."""
        self.found()
        self.set_regime("ku", date_from="2025-01-01", prev_year_turnover=30000)
        self.receipts(2025, [1000])
        self.assertEqual(self.limit(2025), (2500000, []))
        self.company.l10n_de_euer_activity_start = False
        self.assertEqual(
            self.limit(2025),
            (10000000, [("ku_limit_previous_year", "warning", 3000000)]),
        )

    # ------------------------------------------------------------ year after
    def test_year_after_foundation(self):
        """Year after the foundation: the turnover of 2025 (24,000 €, not
        converted into a full year) is below 25,000 €, the limit of 2026 is
        100,000 €; 30,000 € in 2026 give no finding. With 25,500 € in 2025 the
        status of 2026 is not possible (warning of the previous year)."""
        self.found()
        self.receipts(2025, [12000, 12000])
        self.receipts(2026, [30000], month=3)
        self.assertEqual(
            self.limit(2025),
            (2500000, [("ku_limit_near", "warning", 2400000)]),
        )
        self.assertEqual(self.limit(2026), (10000000, []))
        self.receipts(2025, [1500], month=9)
        self.assertEqual(
            self.limit(2025),
            (2500000, [("ku_limit_exceeded", "blocker", 2550000)]),
        )
        self.assertEqual(
            self.limit(2026),
            (10000000, [("ku_limit_previous_year", "warning", 2550000)]),
        )
        self.compute(2026)
        self.assertEqual(self.checks(2026, "C-32").severity, "warning")

    def test_entered_previous_year_only_for_the_first_year(self):
        """Regression: the turnover of the previous year entered on a period
        of a takeover (12,000 € for 2024) holds for the first year of the
        period only. 2026 takes the turnover of 2025 from the data (26,000 €
        > 25,000 €), not the value entered for 2024."""
        self.set_regime("ku", date_from="2025-01-01", prev_year_turnover=12000)
        self.receipts(2025, [13000, 13000])
        self.receipts(2026, [500], month=3)
        self.assertEqual(self.limit(2025), (10000000, []))
        self.assertEqual(
            self.limit(2026),
            (10000000, [("ku_limit_previous_year", "warning", 2600000)]),
        )

    # ------------------------------------------------------------ setup
    def test_activity_start_in_the_setup_wizard(self):
        """The setup wizard shows and writes the start of the activity of the
        main company; the run keeps the year of foundation."""
        self.company.l10n_de_euer_activity_start = "2024-09-01"
        wizard = self.env["l10n_de_euer.setup.wizard"].create(
            {"company_id": self.company.id}
        )
        self.assertEqual(str(wizard.activity_start), "2024-09-01")
        wizard.write({"activity_start": "2025-04-01", "create_vat_period": False})
        wizard.action_apply()
        self.assertEqual(str(self.company.l10n_de_euer_activity_start), "2025-04-01")
        run = self.compute(2025)
        self.assertEqual(run.config_snapshot["founding_year"], 2025)
