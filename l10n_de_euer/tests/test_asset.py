# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Asset register tests AT-* (spec 9.6) with Odoo records (SKR03).

All amounts and names are synthetic (decision D6). Expected values are
calculated by hand in the docstrings.
"""

import base64
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, new_test_user, tagged

from .common import AssetCase

D = Decimal


@tagged("post_install_l10n", "post_install", "-at_install")
class TestAssetRegister(AssetCase):
    # ------------------------------------------------------------ GWG
    def test_at_gwg_1_limit_800_net(self):
        """AT-GWG-1: 800.00 net is a low-value asset, 800.01 is not."""
        gwg = self.asset(asset_kind="gwg", method="none", cost_net=800.0)
        self.assertEqual(self.contributions(2024), {"EUER.25.132": D("800.00")})
        self.assertTrue(gwg.in_gwg_register)
        with self.assertRaises(ValidationError):
            self.asset(asset_kind="gwg", method="none", cost_net=800.01)

    def test_at_gwg_2_small_business_gross(self):
        """AT-GWG-2: small business, 940 € gross (789.92 net): low-value asset
        with the gross cost as expense (R 9b (1) EStR, limit net)."""
        self.period("ku", "2024-01-01")
        gwg = self.asset(
            asset_kind="gwg",
            method="none",
            delivery_date="2026-03-01",
            cost_net=789.92,
            input_tax=150.08,
        )
        self.assertEqual(gwg.vat_treatment, "not_deductible")
        self.assertEqual(gwg.acquisition_cost, 940.0)
        self.assertEqual(gwg.limit_value, 789.92)
        self.assertEqual(self.contributions(2026), {"EUER.25.132": D("940.00")})

    def test_at_gwg_3_independent_use(self):
        """AT-GWG-3: without independent use no low-value asset (§ 6 (2) S. 2)."""
        with self.assertRaises(ValidationError):
            self.asset(
                asset_kind="gwg", method="none", cost_net=300.0, independent_use=False
            )

    def test_at_gwg_4_5_year_of_delivery_and_c64(self):
        """AT-GWG-4/5: purchase on 4855 delivered 20.12.2026, paid later:
        without register entry C-64; from the journal item the expense is in
        the year of delivery 2026 (Kz 132), nothing in 2027."""
        bill = self.bill(
            [("Blitzgerät", self.acc("4855"), 300.0, self.tax())],
            on_date="2026-12-20",
        )
        line = bill.invoice_line_ids
        self.assertTrue(self.results("C-64", 2026))
        asset = self.from_lines(line)
        self.assertEqual(asset.asset_kind, "gwg")
        self.assertEqual(asset.delivery_date, date(2026, 12, 20))
        self.assertEqual(asset.cost_net, 300.0)
        self.assertEqual(asset.input_tax, 57.0)
        # draft entries do not count yet
        self.assertTrue(self.results("C-64", 2026))
        asset.action_activate()
        self.assertFalse(self.results("C-64", 2026))
        self.assertEqual(self.contributions(2026), {"EUER.25.132": D("300.00")})
        self.assertEqual(self.contributions(2027), {})

    def test_at_gwg_6_register_above_250(self):
        """AT-GWG-6: 250.00 net not in the register, 250.01 is (§ 6 (2) S. 4)."""
        low = self.asset(asset_kind="gwg", method="none", cost_net=250.0)
        high = self.asset(asset_kind="gwg", method="none", cost_net=250.01)
        self.assertFalse(low.in_gwg_register)
        self.assertTrue(high.in_gwg_register)
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_asset_gwg"
        )
        self.assertIn("in_gwg_register", action["domain"])
        register = self.Asset.search([("in_gwg_register", "=", True)])
        self.assertIn(high, register)
        self.assertNotIn(low, register)

    # ------------------------------------------------------------ pools
    def test_at_pool_1_4(self):
        """AT-POOL-1/2/4: pool of 2026 with 500 + 700: release 240 per year;
        a low-value asset above 250 € in the same year is C-66."""
        first = self.asset(
            asset_kind="pool", method="none", cost_net=500.0, delivery_date="2026-02-01"
        )
        self.asset(
            asset_kind="pool", method="none", cost_net=700.0, delivery_date="2026-11-30"
        )
        pool = first.pool_id
        self.assertEqual((pool.year, pool.amount), (2026, 1200.0))
        self.assertEqual(
            self.contributions(2026),
            {"AVEUER.40.432": D("1200.00"), "AVEUER.40.434": D("240.00")},
        )
        values = self.form_values(2026)
        self.assertEqual(values["EUER.25.137"], D("240.00"))
        self.assertFalse(self.results("C-66", 2026))
        self.asset(
            asset_kind="gwg", method="none", cost_net=200.0, delivery_date="2026-05-01"
        )
        self.assertFalse(self.results("C-66", 2026))
        self.asset(
            asset_kind="gwg", method="none", cost_net=300.0, delivery_date="2026-05-01"
        )
        self.assertEqual(len(self.results("C-66", 2026)), 1)
        with self.assertRaises(ValidationError):
            self.asset(asset_kind="pool", method="none", cost_net=1000.01)
        with self.assertRaises(ValidationError):
            self.asset(asset_kind="pool", method="none", cost_net=250.0)

    def test_at_pool_3_disposal_without_effect(self):
        first = self.asset(
            asset_kind="pool", method="none", cost_net=500.0, delivery_date="2026-02-01"
        )
        first.write(
            {
                "disposal_date": "2027-03-01",
                "disposal_kind": "sale",
                "disposal_value": 50,
            }
        )
        first.action_dispose()
        self.assertEqual(first.pool_id.amount, 500.0)
        self.assertEqual(self.contributions(2027)["AVEUER.40.444"], D("100.00"))

    # ------------------------------------------------------------ methods
    def test_at_deg_1_to_3(self):
        """AT-DEG-1..3: H 7.4 with stored years; 25 % outside the limit; no
        degressive depreciation in 01–06/2025."""
        asset = self.asset(
            method="degressive",
            delivery_date="2024-04-10",
            cost_net=60000.0,
            useful_life_months=72,
            degressive_rate=0.2,
        )
        self.assertEqual(
            {line.year: line.depreciation for line in asset.year_ids},
            {
                2024: 9000.0,
                2025: 10200.0,
                2026: 9600.0,
                2027: 9600.0,
                2028: 9600.0,
                2029: 9600.0,
                2030: 2400.0,
            },
        )
        with self.assertRaises(ValidationError):
            self.asset(
                method="degressive",
                delivery_date="2024-04-10",
                cost_net=60000.0,
                useful_life_months=72,
                degressive_rate=0.25,
            )
        with self.assertRaises(ValidationError):
            self.asset(
                method="degressive",
                delivery_date="2025-03-01",
                cost_net=60000.0,
                useful_life_months=72,
                degressive_rate=0.1,
            )
        self.asset(
            method="degressive",
            delivery_date="2025-07-01",
            cost_net=10000.0,
            useful_life_months=120,
            degressive_rate=0.3,
        )
        with self.assertRaises(ValidationError):
            self.asset(
                method="degressive",
                delivery_date="2025-07-01",
                aveuer_group="building",
                cost_net=10000.0,
                useful_life_months=120,
                degressive_rate=0.3,
            )

    def test_at_ekfz_1_and_kz_407(self):
        """AT-EKFZ-1 and AVEÜR Kz 407 (from 2025, independent of the method)."""
        car = self.asset(
            method="ev_7_2a",
            delivery_date="2025-08-20",
            cost_net=47999.99,
            useful_life_months=0,
            aveuer_group="vehicle",
            is_battery_electric=True,
        )
        self.assertTrue(car.is_electric_or_plugin_hybrid)
        self.assertEqual(
            [line.depreciation for line in car.year_ids.sorted("year")],
            [35999.99, 4800.0, 2400.0, 2400.0, 1440.0, 960.0],
        )
        self.assertEqual(self.form_values(2025)["EUER.25.130"], D("35999.99"))
        self.assertEqual(
            self.Asset._get_key_values(self.company, 2025), {"AVEUER.40.407": "1"}
        )
        with self.assertRaises(ValidationError):
            self.asset(
                method="ev_7_2a",
                delivery_date="2025-08-20",
                useful_life_months=0,
                aveuer_group="vehicle",
            )
        with self.assertRaises(ValidationError):
            self.asset(
                method="ev_7_2a",
                delivery_date="2025-03-20",
                useful_life_months=0,
                aveuer_group="vehicle",
                is_battery_electric=True,
            )

    def test_at_7g_limit_from_parameters(self):
        """AT-7G-1 with the rule parameter: 40 % from 2024."""
        asset = self.asset(cost_net=10000.0, useful_life_months=120)
        self.env["l10n_de_euer.asset.event"].create(
            {
                "asset_id": asset.id,
                "type": "sec7g_special",
                "date": "2024-06-01",
                "amount": 4000.0,
                "reason": "Voraussetzungen erfüllt",
                "sec7g_profit_confirmed": True,
            }
        )
        self.assertEqual(
            asset.year_ids.filtered(lambda y: y.year == 2024).special_depreciation,
            4000.0,
        )
        self.assertEqual(self.form_values(2024)["EUER.25.134"], D("4000.00"))
        with self.assertRaises(ValidationError):
            self.env["l10n_de_euer.asset.event"].create(
                {
                    "asset_id": asset.id,
                    "type": "sec7g_special",
                    "date": "2025-06-01",
                    "amount": 0.01,
                    "reason": "zu viel",
                    "sec7g_profit_confirmed": True,
                }
            )

    def test_at_einl_1_contribution(self):
        """AT-EINL-1: reduced private cost 840 < fair value 900 → Kz 123."""
        asset = self.asset(
            origin="contribution",
            delivery_date="2026-09-01",
            cost_net=0.0,
            contribution_fair_value=900.0,
            private_acquisition_date="2025-03-10",
            private_acquisition_cost=1200.0,
            private_useful_life_months=60,
            useful_life_months=42,
        )
        self.assertEqual(asset.acquisition_cost, 840.0)
        values = self.contributions(2026)
        self.assertEqual(values["EUER.29.123"], D("840.00"))
        self.assertEqual(values["AVEUER.40.424"], D("80.00"))

    # ------------------------------------------------------------ disposal
    def test_at_abg_2_withdrawal_standard_and_small_business(self):
        """AT-ABG-2: withdrawal with fair value 500: standard taxation with
        input tax deducted → 102 500, 140 95, 122 595, 135 = residual 500;
        the same asset of a small business → 140 empty, 122 500."""
        self.period("regular", "2024-01-01")
        asset = self.asset(cost_net=1000.0, input_tax=190.0)
        asset.write(
            {
                "disposal_date": "2025-06-30",
                "disposal_kind": "withdrawal",
                "disposal_value": 500.0,
            }
        )
        self.assertEqual(asset.disposal_vat_amount, 95.0)
        asset.action_dispose()
        values = self.form_values(2025)
        self.assertEqual(values["EUER.20.102"], D("500.00"))
        self.assertEqual(values["EUER.20.140"], D("95.00"))
        self.assertEqual(values["EUER.29.122"], D("595.00"))
        self.assertEqual(values["EUER.25.135"], D("500.00"))
        self.assertEqual(values["EUER.25.130"], D("166.67"))

    def test_at_abg_2_small_business(self):
        self.period("ku", "2024-01-01")
        asset = self.asset(cost_net=1000.0, input_tax=190.0)
        self.assertEqual(asset.acquisition_cost, 1190.0)
        asset.write(
            {
                "disposal_date": "2025-06-30",
                "disposal_kind": "withdrawal",
                "disposal_value": 500.0,
            }
        )
        self.assertEqual(asset.disposal_vat_amount, 0.0)
        with self.assertRaises(ValidationError):
            asset.disposal_vat_amount = 95.0
        asset.action_dispose()
        values = self.form_values(2025)
        self.assertIsNone(values["EUER.20.140"])
        self.assertEqual(values["EUER.29.122"], D("500.00"))

    def test_at_abg_3_scrap_and_5_month_convention(self):
        """AT-ABG-3/5: scrapping: residual book value Kz 135; without the
        month of disposal the split moves from 130 to 135, the sum stays."""
        asset = self.asset()
        asset.write({"disposal_date": "2025-06-30", "disposal_kind": "scrap"})
        asset.action_dispose()
        values = self.form_values(2025)
        self.assertEqual(
            (values["EUER.25.130"], values["EUER.25.135"]), (D("200.00"), D("600.00"))
        )
        self.company.l10n_de_euer_disposal_month = False
        asset.action_compute_years()
        values = self.form_values(2025)
        self.assertEqual(
            (values["EUER.25.130"], values["EUER.25.135"]), (D("166.67"), D("633.33"))
        )

    def test_at_abg_4_land_at_receipt(self):
        """AT-ABG-4: land sold 2026, proceeds received 2027 → Kz 135 in 2027."""
        land = self.asset(
            asset_kind="non_depreciable",
            method="none",
            aveuer_group="land",
            cost_net=50000.0,
            useful_life_months=0,
        )
        land.write(
            {
                "disposal_date": "2026-11-05",
                "disposal_kind": "sale",
                "disposal_value": 60000,
            }
        )
        with self.assertRaises(ValidationError):
            land.action_dispose()
        land.write({"disposal_receipt_date": "2027-01-15"})
        land.action_dispose()
        self.assertNotIn("AVEUER.40.105", self.contributions(2026))
        self.assertEqual(self.form_values(2027)["EUER.25.135"], D("50000.00"))

    # ------------------------------------------------------------ AVEÜR
    def reference_assets(self):
        first = self.asset(
            name="Testgerät A",
            delivery_date="2021-05-03",
            cost_net=1999.0,
            useful_life_months=84,
        )
        second = self.asset(
            name="Testgerät B",
            delivery_date="2023-10-03",
            cost_net=869.99,
            useful_life_months=60,
        )
        return first, second

    def test_at_ave_1_3_groups_carry_forward_transfers(self):
        """AT-AVE-1..3: group other movable assets 2024: cost 2,868.99, BW
        beginning 2,063.97, AfA 459.57 → Kz 130, BW end 1,604.40; 2025 starts
        with 1,604.40; no C-61, no C-62."""
        self.reference_assets()
        values = self.form_values(2024)
        self.assertEqual(values["AVEUER.40.420"], D("2868.99"))
        self.assertEqual(values["AVEUER.40.421"], D("2063.97"))
        self.assertEqual(values["AVEUER.40.424"], D("459.57"))
        self.assertEqual(values["AVEUER.40.426"], D("1604.40"))
        self.assertEqual(values["AVEUER.40.490"], D("459.57"))
        self.assertEqual(values["EUER.25.130"], D("459.57"))
        self.assertEqual(self.form_values(2025)["AVEUER.40.421"], D("1604.40"))
        for year in (2024, 2025, 2026):
            self.assertFalse(self.results("C-61", year))
            self.assertFalse(self.results("C-62", year))
        aveuer = self.Asset._get_aveuer_details(self.company, 2024)
        self.assertEqual(
            [row["depreciation"] for row in aveuer], [D("285.57"), D("174.00")]
        )

    def test_locked_years(self):
        """Locked years (finalised statement) never change; C-61 reports a
        book value changed outside the register."""
        first, _second = self.reference_assets()
        self.Asset._lock_years(self.company, 2024)
        locked = first.year_ids.filtered(lambda line: line.year == 2024)
        self.assertTrue(locked.locked)
        with self.assertRaises(UserError):
            first.useful_life_months = 72
        with self.assertRaises(UserError):
            first.aveuer_group = "office_equipment"
        with self.assertRaises(UserError):
            self.env["l10n_de_euer.asset.event"].create(
                {
                    "asset_id": first.id,
                    "type": "extraordinary",
                    "date": "2024-10-01",
                    "amount": 10,
                    "reason": "Schaden",
                }
            )
        with self.assertRaises(UserError):
            locked.write({"depreciation": 1.0})
        with self.assertRaises(UserError):
            self.asset(delivery_date="2024-12-01")
        later = self.asset(delivery_date="2025-02-01")
        with self.assertRaises(UserError):
            later.delivery_date = "2024-11-01"
        # later years stay open
        self.env["l10n_de_euer.asset.event"].create(
            {
                "asset_id": first.id,
                "type": "extraordinary",
                "date": "2025-10-01",
                "amount": 10,
                "reason": "Schaden",
            }
        )
        self.assertFalse(self.results("C-61", 2025))
        self.env.cr.execute(
            "UPDATE l10n_de_euer_asset_year SET book_value_end = book_value_end + 1 "
            "WHERE id = %s",
            [locked.id],
        )
        locked.invalidate_recordset()
        self.assertEqual(len(self.results("C-61", 2025)), 1)

    # ------------------------------------------------------------ checks
    def test_c63_memo_value_only_takeover(self):
        with self.assertRaises(ValidationError):
            self.asset(memo_value=1.0)
        self.asset(
            origin="legacy_import",
            delivery_date="2020-07-01",
            cost_net=799.0,
            useful_life_months=60,
            memo_value=1.0,
            opening_date="2023-12-31",
            opening_book_value=239.70,
            opening_accumulated=559.30,
        )
        self.assertEqual(len(self.results("C-63", 2024)), 1)
        self.assertFalse(self.results("C-67", 2025))

    def test_c65_business_share(self):
        self.asset(business_share=0.05)
        self.assertEqual(len(self.results("C-65", 2024)), 1)

    def test_c67_takeover_inconsistent(self):
        """C-67: depreciation taken over (300) + new (2 × 400) ≠ cost 1,200.

        The form refuses such a takeover (spec 6.6); C-67 still finds one
        changed outside the register."""
        with self.assertRaises(ValidationError):
            self.asset(
                origin="legacy_import",
                opening_date="2024-12-31",
                opening_book_value=800.0,
                opening_accumulated=300.0,
            )
        taken = self.asset(
            origin="legacy_import",
            opening_date="2024-12-31",
            opening_book_value=800.0,
            opening_accumulated=400.0,
        )
        self.assertFalse(self.results("C-67", 2026))
        self.env.cr.execute(
            "UPDATE l10n_de_euer_asset SET opening_accumulated = 300 WHERE id = %s",
            [taken.id],
        )
        taken.invalidate_recordset()
        self.assertFalse(self.results("C-67", 2025))
        self.assertEqual(len(self.results("C-67", 2026)), 1)

    def test_c68_ledger_against_register(self):
        """C-68: AfA booked in the ledger is compared with the register."""
        self.asset()
        self.assertFalse(self.results("C-68", 2024))
        self.entry(
            [(self.acc("4830"), 400.0), (self.acc("0490"), -400.0)], "2024-12-31"
        )
        self.assertFalse(self.results("C-68", 2024))
        self.assertFalse(self.results("C-64", 2024))
        self.entry([(self.acc("4830"), -50.0), (self.acc("0490"), 50.0)], "2024-12-31")
        self.assertEqual(len(self.results("C-68", 2024)), 1)

    def test_at_priv_1_use_withdrawal(self):
        """AT-PRIV-1 (synthetic): small business, AfA 285.57, repair 100,
        business share 80 % → (285.57 + 100) × 20 % = 77.11 → Kz 108 and 122;
        Kz 140 empty; C-69 until decided."""
        self.period("ku", "2021-01-01")
        asset = self.asset(
            delivery_date="2021-05-03",
            cost_net=1999.0,
            useful_life_months=84,
            business_share=0.8,
        )
        repair = self.entry(
            [
                (self.acc("4805"), 100.0),
                (self.company_data["default_journal_bank"].default_account_id, -100.0),
            ],
            "2024-05-10",
        )
        asset.cost_line_ids = repair.line_ids.filtered(lambda line: line.balance > 0)
        asset.action_compute_years()
        line = asset.year_ids.filtered(lambda line: line.year == 2024)
        self.assertEqual((line.private_use_amount, line.private_use_vat), (77.11, 0.0))
        self.assertEqual(len(self.results("C-69", 2024)), 1)
        self.assertNotIn("EUER.20.108", self.contributions(2024))
        line.action_confirm_private_use()
        values = self.contributions(2024)
        self.assertEqual(values["EUER.20.108"], D("77.11"))
        self.assertEqual(values["EUER.29.122"], D("77.11"))
        self.assertNotIn("EUER.20.140", values)
        self.assertNotIn(
            "EUER.20.108", self.contributions(2024, include_private_use=False)
        )
        self.assertFalse(self.results("C-69", 2024))
        other = asset.year_ids.filtered(lambda line: line.year == 2025)
        with self.assertRaises(UserError):
            other.action_reject_private_use()
        other.private_use_reason = "Privatnutzung durch Nutzungsentgelt ausgeglichen"
        other.action_reject_private_use()
        self.assertFalse(self.results("C-69", 2025))

    def test_at_15a_1_change_of_vat_treatment(self):
        """AT-15A-1: small business until 2026, standard from 2027; asset
        11,900 gross (1,900 input tax) → C-42 with 380 per year; cost
        unchanged. With 380 input tax the bagatelle limit applies."""
        self.period("ku", "2024-01-01", "2026-12-31")
        self.period("regular", "2027-01-01")
        asset = self.asset(
            delivery_date="2025-07-01",
            cost_net=10000.0,
            input_tax=1900.0,
            useful_life_months=120,
        )
        self.assertEqual(asset.acquisition_cost, 11900.0)
        results = self.results("C-42", 2027)
        self.assertEqual(len(results), 1)
        self.assertIn("2027: 380.00", results[0]["message"])
        self.assertIn("2030: 190.00", results[0]["message"])
        self.assertFalse(self.results("C-42", 2026))
        self.assertEqual(asset.acquisition_cost, 11900.0)
        asset.input_tax = 380.0
        self.assertIn("1,000", self.results("C-42", 2027)[0]["message"])

    def test_c40_reduction_outside_scope(self):
        asset = self.asset()
        self.env["l10n_de_euer.asset.event"].create(
            {
                "asset_id": asset.id,
                "type": "sec7g_reduction",
                "date": "2024-02-01",
                "amount": 100,
            }
        )
        self.assertEqual(self.results("C-40", 2024)[0]["severity"], "blocker")

    def test_check_results_stored(self):
        self.asset(business_share=0.05)
        checks = self.env["l10n_de_euer.check"]._run_asset_checks(self.company, 2024)
        # C-69: the private use of 95 % needs a decision as well
        self.assertEqual(set(checks.mapped("code")), {"C-65", "C-69"})
        self.assertEqual(set(checks.mapped("origin")), {"asset"})
        wizard = self.env["l10n_de_euer.asset.check.wizard"].create(
            {"company_id": self.company.id, "year": 2024}
        )
        action = wizard.action_run()
        rerun = self.env["l10n_de_euer.check"].search(action["domain"])
        self.assertEqual(set(rerun.mapped("code")), {"C-65", "C-69"})

    # ------------------------------------------------------------ wizards
    def test_asset_from_move_line_proposals(self):
        """Proposals: delivery date from the document, group from the account
        (0420 → office equipment), net cost and input tax. The register never
        writes journal entries (B-01, GT-16)."""
        self.period("regular", "2024-01-01")
        bill = self.bill(
            [("Schreibtisch", self.acc("0420"), 1000.0, self.tax())],
            on_date="2026-04-02",
        )
        line = bill.invoice_line_ids
        self.assertTrue(self.results("C-64", 2026))
        forbidden = AssertionError("the asset register writes a journal entry")
        with (
            patch.object(type(line), "write", autospec=True, side_effect=forbidden),
            patch.object(type(bill), "write", autospec=True, side_effect=forbidden),
        ):
            asset = self.from_lines(line, useful_life_months=156)
            asset.action_activate()
            self.Checker._run(self.company, 2026)
            self.Asset._get_field_contributions(self.company, 2026)
            self.env.flush_all()
        self.assertEqual(asset.aveuer_group, "office_equipment")
        self.assertEqual(asset.asset_kind, "depreciable")
        self.assertEqual((asset.cost_net, asset.input_tax), (1000.0, 190.0))
        self.assertEqual(asset.acquisition_cost, 1000.0)
        self.assertEqual(asset.delivery_date, date(2026, 4, 2))
        self.assertEqual(asset.move_line_ids, line)
        self.assertFalse(self.results("C-64", 2026))

    def test_at_auf_1_voucher_not_deletable(self):
        """AT-AUF-1: the voucher of an asset in the register cannot be deleted.
        The wizard takes posted items only; a draft voucher is linked in the
        form of the asset."""
        bill = self.bill([("Kamera", self.acc("0490"), 1500.0, self.tax())], post=False)
        asset = self.asset(
            cost_net=1500.0,
            delivery_date="2026-03-15",
            useful_life_months=84,
            move_line_ids=[Command.set(bill.invoice_line_ids.ids)],
        )
        with self.assertRaisesRegex(UserError, "asset register"):
            bill.unlink()
        with self.assertRaises(UserError):
            asset.unlink()
        asset.action_set_draft()
        bill.unlink()

    def test_at_imp_1_csv_import(self):
        """AT-IMP-1: takeover on 31.12.2026 with plausibility check; the
        register continues with the residual book values."""
        content = (
            "Bezeichnung;Gruppe;Art;Lieferdatum;AK;ND (Monate);Methode;"
            "kumulierte AfA bis Stichtag;Restbuchwert;Sammelposten-Jahrgang;"
            "Sammelposten-Restbetrag\n"
            "Testgerät A;Andere;abnutzbar;03.05.2021;1.999,00;84;linear;1.618,23;"
            "380,77;;\n"
            "Sammelposten;Andere;Sammelposten;;2.345,67;;;;;2024;938,28\n"
            "Fehlerhaft;Andere;abnutzbar;2025-01-01;1000;36;linear;300;800;;\n"
        )
        wizard = self.env["l10n_de_euer.asset.import"].create(
            {
                "company_id": self.company.id,
                "opening_date": "2026-12-31",
                "file": base64.b64encode(content.encode()),
                "filename": "anlagen.csv",
            }
        )
        wizard.action_parse()
        self.assertEqual(wizard.error_count, 1)
        with self.assertRaises(UserError):
            wizard.action_import()
        wizard.line_ids.filtered("error").unlink()
        wizard.action_import()
        assets = wizard.line_ids.asset_id
        self.assertEqual(set(assets.mapped("state")), {"active"})
        device = assets.filtered(lambda asset: asset.asset_kind == "depreciable")
        self.assertEqual(
            {
                line.year: line.depreciation
                for line in device.year_ids
                if not line.is_opening
            },
            {2027: 285.57, 2028: 95.20},
        )
        values = self.contributions(2027)
        self.assertEqual(values["AVEUER.40.421"], D("380.77"))
        self.assertEqual(values["AVEUER.40.424"], D("285.57"))
        self.assertEqual(values["AVEUER.40.461"], D("938.28"))
        self.assertEqual(values["AVEUER.40.464"], D("469.13"))
        self.assertFalse(self.results("C-61", 2027))
        # years up to the takeover date are not calculated again
        self.assertNotIn("AVEUER.40.421", self.contributions(2026))
        self.assertNotIn("AVEUER.40.424", self.contributions(2026))

    # ------------------------------------------------------------ companies
    def test_multi_company_isolation(self):
        """GT-02: assets of another business are neither counted nor visible."""
        self.asset()
        other = self.env["res.company"].create({"name": "Zweitbetrieb E3"})
        foreign = self.asset(company_id=other.id, cost_net=3000.0)
        self.assertEqual(self.contributions(2024)["AVEUER.40.422"], D("1200.00"))
        self.assertEqual(
            self.contributions(2024, company=other)["AVEUER.40.422"], D("3000.00")
        )
        user = new_test_user(
            self.env,
            login="e3_asset_user",
            groups="account.group_account_user",
            company_id=self.company.id,
            company_ids=[Command.set(self.company.ids)],
        )
        visible = self.Asset.with_user(user).search([])
        self.assertNotIn(foreign, visible)
        with self.assertRaises(AccessError):
            foreign.with_user(user).read(["name"])

    def test_draft_and_delete(self):
        draft = self.asset(activate=False)
        self.assertFalse(draft.year_ids)
        self.assertEqual(self.contributions(2024), {})
        draft.unlink()
        active = self.asset()
        self.assertEqual(len(active.year_ids), 3)
        active.action_set_draft()
        self.assertFalse(active.year_ids)

    def test_form_view(self):
        """The form view with its modifiers creates and enters an asset."""
        with Form(self.Asset) as form:
            form.name = "Formulartest"
            form.delivery_date = date(2026, 2, 1)
            form.cost_net = 3000.0
            form.useful_life_months = 36
            self.assertEqual(form.vat_treatment, "deductible")
            with form.event_ids.new() as event:
                event.type = "extraordinary"
                event.date = date(2027, 6, 1)
                event.amount = 100.0
                event.reason = "Wasserschaden"
        asset = form.record
        asset.action_activate()
        # 1,000 per year: 2026 11 months 916.67; 2027 1,000 + AfaA 100; then
        # 983.33 over the remaining 13 months (01/2028–01/2029): 907.69 in
        # 2028 and the rest 75.64 in 2029
        self.assertEqual(
            [line.depreciation for line in asset.year_ids.sorted("year")],
            [916.67, 1100.0, 907.69, 75.64],
        )
