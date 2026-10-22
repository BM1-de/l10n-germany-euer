# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Tax rules of the asset register with Odoo records (spec 6.3, 6.4, R-30 ff.).

All amounts and names are synthetic (decision D6); expected values are
calculated by hand in the docstrings.
"""

from datetime import date
from decimal import Decimal

from odoo import Command
from odoo.exceptions import UserError, ValidationError
from odoo.tests import Form, tagged

from .common import AssetCase

D = Decimal
CONFIRMED = {"sec7g_profit_confirmed": True, "reason": "Voraussetzungen erfüllt"}


@tagged("post_install_l10n", "post_install", "-at_install")
class TestAssetRules(AssetCase):
    def event(self, asset, **values):
        return self.env["l10n_de_euer.asset.event"].create(
            {"asset_id": asset.id, **values}
        )

    def land(self, **values):
        return self.asset(
            name=values.pop("name", "Grundstück"),
            asset_kind="non_depreciable",
            method="none",
            aveuer_group="land",
            cost_net=50000.0,
            useful_life_months=0,
            **values,
        )

    def sales_invoice(self, amount, on_date):
        invoice = self.env["account.move"].create(
            {
                "move_type": "out_invoice",
                "partner_id": self.partner_a.id,
                "invoice_date": on_date,
                "date": on_date,
                "invoice_line_ids": [
                    Command.create(
                        {
                            "name": "Verkauf",
                            "account_id": self.acc("8829").id,
                            "price_unit": amount,
                            "quantity": 1,
                            "tax_ids": [Command.clear()],
                        }
                    )
                ],
            }
        )
        invoice.action_post()
        return invoice

    def settle(self, invoice, on_date, amount):
        """Payment of ``amount`` on ``on_date`` reconciled with the invoice."""
        receivable = invoice.line_ids.filtered(
            lambda line: line.display_type == "payment_term"
        )
        bank = self.company_data["default_journal_bank"].default_account_id
        move = self.env["account.move"].create(
            {
                "move_type": "entry",
                "date": on_date,
                "journal_id": self.company_data["default_journal_misc"].id,
                "line_ids": [
                    Command.create(
                        {"account_id": bank.id, "balance": amount, "name": "Zahlung"}
                    ),
                    Command.create(
                        {
                            "account_id": receivable.account_id.id,
                            "partner_id": invoice.partner_id.id,
                            "balance": -amount,
                            "name": "Zahlung",
                        }
                    ),
                ],
            }
        )
        move.action_post()
        (
            receivable
            + move.line_ids.filtered(
                lambda line: line.account_id == receivable.account_id
            )
        ).reconcile()

    # ------------------------------------------------------------ methods
    def test_ekfz_only_purely_electric(self):
        """§ 7 (2a) EStG only for vehicles with electric motors only (§ 9 (2)
        KraftStG): a plug-in hybrid counts for Kz 407 but is depreciated
        linearly: 40,000 ÷ 6 = 6,666.67 × 4 / 12 = 2,222.22 in 2025."""
        values = {
            "delivery_date": "2025-09-01",
            "cost_net": 40000.0,
            "aveuer_group": "vehicle",
        }
        with self.assertRaises(ValidationError):
            self.asset(
                method="ev_7_2a",
                useful_life_months=0,
                is_electric_or_plugin_hybrid=True,
                **values,
            )
        hybrid = self.asset(
            useful_life_months=72, is_electric_or_plugin_hybrid=True, **values
        )
        self.assertEqual(
            hybrid.year_ids.filtered(lambda line: line.year == 2025).depreciation,
            2222.22,
        )
        self.assertEqual(
            self.Asset._get_key_values(self.company, 2025), {"AVEUER.40.407": "1"}
        )
        # § 7 (2a): "angeschafft", not produced
        with self.assertRaises(ValidationError):
            self.asset(
                method="ev_7_2a",
                useful_life_months=0,
                origin="production",
                is_battery_electric=True,
                **values,
            )

    def test_digital_asset_only_hardware_and_software(self):
        """BMF 22.02.2022: one year only for computers and software."""
        with self.assertRaises(ValidationError):
            self.asset(
                method="digital_1y",
                useful_life_months=0,
                aveuer_group="vehicle",
                cost_net=30000.0,
            )
        laptop = self.asset(
            method="digital_1y",
            useful_life_months=0,
            aveuer_group="office_equipment",
            cost_net=1500.0,
        )
        self.assertEqual(laptop.year_ids.depreciation, 1500.0)

    def test_short_useful_life(self):
        """H 7.4 EStH: up to 12 months the cost is no depreciation."""
        with self.assertRaises(ValidationError):
            self.asset(useful_life_months=12)

    def test_intangible_low_value_only_trivial_program(self):
        """R 5.5 (1) EStR: a program is a low-value or pool asset only as a
        trivial program (800 € from 2018, 410 € before)."""
        values = {
            "asset_kind": "gwg",
            "method": "none",
            "aveuer_group": "intangible",
            "cost_net": 500.0,
            "delivery_date": "2026-02-01",
        }
        with self.assertRaises(ValidationError):
            self.asset(**values)
        self.asset(is_trivial_program=True, **values)
        self.assertEqual(self.contributions(2026), {"EUER.25.132": D("500.00")})
        pool = {
            "asset_kind": "pool",
            "method": "none",
            "aveuer_group": "intangible",
            "is_trivial_program": True,
        }
        with self.assertRaises(ValidationError):
            self.asset(cost_net=900.0, delivery_date="2026-02-01", **pool)
        with self.assertRaises(ValidationError):
            self.asset(cost_net=420.0, delivery_date="2017-06-01", **pool)

    def test_degressive_highest_rate(self):
        """04/2024, 12 years: the highest rate is 2 × 1/12 = 1/6; 16.67 % is
        above it. With the highest rate: 120,000 × 1/6 × 9/12 = 15,000.00."""
        values = {
            "method": "degressive",
            "delivery_date": "2024-04-10",
            "cost_net": 120000.0,
            "useful_life_months": 144,
        }
        with self.assertRaises(ValidationError):
            self.asset(degressive_rate=0.1667, **values)
        exact = self.asset(degressive_rate_max=True, **values)
        self.assertAlmostEqual(exact.degressive_rate, 0.1667, places=4)
        self.assertEqual(
            exact.year_ids.filtered(lambda line: line.year == 2024).depreciation,
            15000.0,
        )

    def test_degressive_takeover_before_the_rules(self):
        """No rule parameters of degressive depreciation before 2020: the
        takeover needs the year of the switch; then 6,000 over the remaining
        38 months (01/2027–02/2030): 1,894.74 per year."""
        values = {
            "origin": "legacy_import",
            "method": "degressive",
            "degressive_rate": 0.25,
            "delivery_date": "2010-03-01",
            "cost_net": 20000.0,
            "useful_life_months": 240,
            "opening_date": "2026-12-31",
            "opening_book_value": 6000.0,
            "opening_accumulated": 14000.0,
        }
        with self.assertRaisesRegex(ValidationError, "switch"):
            self.asset(**values)
        taken = self.asset(switch_to_linear_year=2016, **values)
        self.assertEqual(
            taken.year_ids.filtered(lambda line: line.year == 2027).depreciation,
            1894.74,
        )

    # ------------------------------------------------------------ § 7g, § 7b
    def test_sec7g_conditions(self):
        """§ 7g (6): profit limit confirmed, at least 90 % business use in the
        year of delivery and the following year, the asset stays until the
        end of the following year; a later breach is C-40."""
        special = {"type": "sec7g_special", "date": "2024-06-01", "amount": 2000.0}
        asset = self.asset(cost_net=10000.0, useful_life_months=120)
        with self.assertRaises(ValidationError):
            self.event(asset, reason="ohne Bestätigung", **special)
        half = self.asset(cost_net=10000.0, useful_life_months=120, business_share=0.5)
        with self.assertRaises(ValidationError):
            self.event(half, **CONFIRMED, **special)
        self.event(asset, **CONFIRMED, **special)
        self.assertFalse(self.results("C-40", 2025))
        self.event(
            asset,
            type="business_use_change",
            date="2025-03-01",
            business_share=0.5,
            reason="Privatnutzung",
        )
        self.assertEqual(len(self.results("C-40", 2024)), 1)
        self.assertEqual(len(self.results("C-40", 2025)), 1)
        self.assertFalse(self.results("C-40", 2026))
        sold = self.asset(cost_net=10000.0, useful_life_months=120)
        self.event(sold, **CONFIRMED, **special)
        sold.write(
            {
                "disposal_date": "2025-03-15",
                "disposal_kind": "sale",
                "disposal_value": 8000.0,
            }
        )
        sold.action_dispose()
        blocked = [
            result
            for result in self.results("C-40", 2024)
            if sold.id in result["res_ids"]
        ]
        self.assertEqual(len(blocked), 1)
        self.assertIn("left the business", blocked[0]["message"])

    def test_sec7g_limit_follows_the_basis(self):
        """H 7a EStH: 2,000 + 360 at a cost of 10,000 + 1,800 (20 % of 11,800)
        are allowed, AfA 04 = 1,180; a subsidy lowers the limit: 40 % of
        8,000 = 3,200."""
        old = self.asset(
            delivery_date="2016-01-05", cost_net=10000.0, useful_life_months=120
        )
        self.event(
            old, type="sec7g_special", date="2016-06-01", amount=2000.0, **CONFIRMED
        )
        self.event(old, type="additional_cost", date="2019-12-01", amount=1800.0)
        self.event(
            old, type="sec7g_special", date="2019-12-31", amount=360.0, **CONFIRMED
        )
        self.assertEqual(
            old.year_ids.filtered(lambda line: line.year == 2019).depreciation, 1180.0
        )
        new = self.asset(
            delivery_date="2024-01-05", cost_net=10000.0, useful_life_months=120
        )
        self.event(new, type="subsidy", date="2024-03-01", amount=2000.0)
        with self.assertRaises(ValidationError):
            self.event(
                new, type="sec7g_special", date="2024-06-01", amount=4000.0, **CONFIRMED
            )
        self.event(
            new, type="sec7g_special", date="2024-06-01", amount=3200.0, **CONFIRMED
        )

    def test_sec7b_buildings(self):
        """§ 7b: 5 % of 300,000 = 15,000 → Kz 113 → Kz 134; the building
        depreciation 9,000 → Kz 114 → Kz 136."""
        building = self.asset(
            aveuer_group="building",
            cost_net=300000.0,
            useful_life_months=400,
            delivery_date="2024-01-15",
        )
        self.event(
            building,
            type="sec7b_special",
            date="2024-12-31",
            amount=15000.0,
            reason="Mietwohnungsneubau, Voraussetzungen § 7b Abs. 2, 3 erfüllt",
        )
        values = self.form_values(2024)
        self.assertEqual(values["AVEUER.40.113"], D("15000.00"))
        self.assertEqual(values["EUER.25.134"], D("15000.00"))
        self.assertEqual(values["EUER.25.136"], D("9000.00"))
        with self.assertRaises(ValidationError):
            self.event(
                building,
                type="sec7b_special",
                date="2025-12-31",
                amount=15000.01,
                reason="zu viel",
            )
        with self.assertRaises(ValidationError):
            self.event(
                self.asset(cost_net=3000.0),
                type="sec7b_special",
                date="2024-12-31",
                amount=10.0,
                reason="kein Gebäude",
            )

    # ------------------------------------------------------------ events
    def test_no_extraordinary_depreciation_of_land(self):
        """§ 7 (1) sentence 7 EStG: AfaA only for depreciable assets."""
        land = self.land()
        with self.assertRaises(ValidationError):
            self.event(
                land, type="extraordinary", date="2025-03-01", amount=4000.0, reason="x"
            )

    def test_subsidy_before_delivery(self):
        """Approved before the delivery: less addition in the year of delivery;
        approved in an earlier year: C-40 (R 6.5 (4) EStR)."""
        asset = self.asset(
            cost_net=10000.0, useful_life_months=60, delivery_date="2025-06-01"
        )
        self.event(asset, type="subsidy", date="2025-03-01", amount=1000.0)
        self.assertEqual(
            asset.year_ids.filtered(lambda line: line.year == 2025).additions, 9000.0
        )
        self.assertFalse(self.results("C-40", 2025))
        early = self.asset(
            cost_net=10000.0, useful_life_months=60, delivery_date="2025-06-01"
        )
        self.event(early, type="subsidy", date="2024-11-01", amount=1000.0)
        self.assertEqual(len(self.results("C-40", 2025)), 1)

    def test_contribution_after_other_income(self):
        """§ 7 (1) sentence 5 EStG is outside the program scope (C-40)."""
        self.asset(
            origin="contribution",
            delivery_date="2026-09-01",
            cost_net=0.0,
            contribution_fair_value=900.0,
            useful_life_months=36,
            contribution_prior_income_use=True,
        )
        self.assertEqual(len(self.results("C-40", 2026)), 1)

    # ------------------------------------------------------------ private use
    def test_vehicle_private_use(self):
        """Private use of a vehicle is Kz 106 (profit corrections), not a use
        withdrawal of the register: no proposal, C-69 with a note."""
        car = self.asset(
            aveuer_group="vehicle",
            cost_net=30000.0,
            useful_life_months=72,
            business_share=0.7,
        )
        line = car.year_ids.filtered(lambda line: line.year == 2024)
        self.assertEqual(line.private_use_amount, 0.0)
        self.assertNotIn("EUER.20.108", self.contributions(2024))
        results = self.results("C-69", 2024)
        self.assertEqual(len(results), 1)
        self.assertIn("Kz 106", results[0]["message"])

    def test_at_priv_2_vat_basis(self):
        """AT-PRIV-2: machine 10,000 net with input tax, 10 years, 20 %
        private: use withdrawal 1,000 × 20 % = 200; VAT basis 2,000 per year
        for five years → 2,000 × 20 % × 19 % = 76.00, then no VAT."""
        self.period("regular", "2024-01-01")
        machine = self.asset(
            cost_net=10000.0,
            input_tax=1900.0,
            useful_life_months=120,
            business_share=0.8,
        )
        lines = {line.year: line for line in machine.year_ids}
        self.assertEqual(
            (lines[2024].private_use_amount, lines[2024].private_use_vat),
            (200.0, 76.0),
        )
        self.assertEqual(lines[2028].private_use_vat, 76.0)
        self.assertEqual(lines[2029].private_use_vat, 0.0)

    # ------------------------------------------------------------ pools
    def test_c66_depreciable_asset_in_pool_year(self):
        """§ 6 (2a) sentence 5 EStG: in a pool year a movable asset of 600 €
        net cannot be depreciated on its own (C-66); 1,200 € can."""
        self.asset(
            asset_kind="pool", method="none", cost_net=500.0, delivery_date="2026-02-01"
        )
        single = self.asset(cost_net=600.0, delivery_date="2026-03-01")
        self.asset(cost_net=1200.0, delivery_date="2026-03-01")
        results = self.results("C-66", 2026)
        self.assertEqual(len(results), 1)
        self.assertIn(single.id, results[0]["res_ids"])

    # ------------------------------------------------------------ disposal
    def test_withdrawal_of_current_assets(self):
        """Withdrawal of current assets: fair value in Kz 108, not Kz 102."""
        shares = self.asset(
            asset_kind="current_4_3_4",
            method="none",
            aveuer_group="current",
            cost_net=5000.0,
            useful_life_months=0,
        )
        shares.write(
            {
                "disposal_date": "2025-05-01",
                "disposal_kind": "withdrawal",
                "disposal_value": 6000.0,
            }
        )
        shares.action_dispose()
        values = self.contributions(2025)
        self.assertEqual(values["EUER.20.108"], D("6000.00"))
        self.assertEqual(values["EUER.25.100"], D("5000.00"))
        self.assertNotIn("EUER.20.102", values)

    def test_goods_on_sale_compared_with_register(self):
        """2327 (current assets on sale) is not realised but compared with the
        register (Kz 605 → 100): no C-68 if equal, no C-64; Kz 100 once."""
        category = self.env["l10n_de_euer.category"].search(
            [("code", "=", "EXP_GOODS_ON_SALE")]
        )
        self.assertEqual(category.role, "afa")
        shares = self.asset(
            asset_kind="current_4_3_4",
            method="none",
            aveuer_group="current",
            cost_net=5000.0,
            useful_life_months=0,
        )
        shares.write(
            {
                "disposal_date": "2025-05-01",
                "disposal_kind": "sale",
                "disposal_value": 6000.0,
                "disposal_receipt_date": "2025-05-03",
            }
        )
        shares.action_dispose()
        self.entry(
            [(self.acc("2327"), 5000.0), (self.acc("1372"), -5000.0)], "2025-05-03"
        )
        self.assertFalse(self.results("C-68", 2025))
        self.assertFalse(self.results("C-64", 2025))
        self.assertEqual(self.contributions(2025)["EUER.25.100"], D("5000.00"))
        self.entry(
            [(self.acc("2327"), 100.0), (self.acc("1372"), -100.0)], "2025-06-03"
        )
        self.assertEqual(len(self.results("C-68", 2025)), 1)

    def test_receipt_from_sales_invoice(self):
        """The receipt of the proceeds is proposed from the payment of the
        sales invoice; another year is C-68, instalments are C-40."""
        land = self.land()
        invoice = self.sales_invoice(60000.0, "2026-11-05")
        self.settle(invoice, "2027-01-15", 60000.0)
        with Form(land) as form:
            form.disposal_date = date(2026, 11, 5)
            form.disposal_kind = "sale"
            form.disposal_value = 60000.0
            form.disposal_invoice_id = invoice
            self.assertEqual(form.disposal_receipt_date, date(2027, 1, 15))
        land.action_dispose()
        self.assertFalse(self.results("C-68", 2027))
        land.disposal_receipt_date = "2026-12-20"
        self.assertEqual(len(self.results("C-68", 2026)), 1)
        self.assertEqual(len(self.results("C-68", 2027)), 1)
        # instalments: 30,000 in 2026 and 30,000 in 2027
        other = self.land(name="Grundstück 2")
        instalments = self.sales_invoice(60000.0, "2026-11-05")
        self.settle(instalments, "2026-12-01", 30000.0)
        self.settle(instalments, "2027-01-15", 30000.0)
        other.write(
            {
                "disposal_date": "2026-11-05",
                "disposal_kind": "sale",
                "disposal_invoice_id": instalments.id,
                "disposal_receipt_date": "2027-01-15",
            }
        )
        other.action_dispose()
        for year in (2026, 2027):
            self.assertTrue(
                [r for r in self.results("C-40", year) if other.id in r["res_ids"]]
            )

    # ------------------------------------------------------------ C-62
    def test_c62_with_final_form_values(self):
        """C-62 checks the final form values of the statement: an override of
        Kz 130 or a Kz 100 below Kz 605 is a blocker."""
        self.asset()
        shares = self.asset(
            asset_kind="current_4_3_4",
            method="none",
            aveuer_group="current",
            cost_net=5000.0,
            useful_life_months=0,
        )
        shares.write(
            {
                "disposal_date": "2024-05-01",
                "disposal_kind": "sale",
                "disposal_receipt_date": "2024-05-03",
            }
        )
        shares.action_dispose()
        forms, _provisional = self.env["l10n_de_euer.form"]._get_forms_for_year(2024)
        contributions = self.contributions(2024)
        final = forms._calculate(contributions)

        def c62(values):
            return [
                result
                for result in self.Checker._run(self.company, 2024, values)
                if result["code"] == "C-62"
            ]

        self.assertFalse(c62(final))
        overridden = forms._calculate(contributions, {"EUER.25.130": D("100.00")})
        self.assertEqual(len(c62(overridden)), 1)
        self.assertIn("EUER.25.130", c62(overridden)[0]["message"])
        self.assertFalse(c62({**final, "EUER.25.100": D("7000.00")}))
        self.assertEqual(len(c62({**final, "EUER.25.100": D("4000.00")})), 1)
        checks = self.env["l10n_de_euer.check"]._run_asset_checks(
            self.company, 2024, overridden
        )
        self.assertIn("C-62", checks.mapped("code"))

    def test_c62_asset_that_cannot_be_calculated(self):
        """An asset whose calculation fails is a C-62 blocker naming it; the
        other checks still run, the form calculation names the asset."""
        land = self.land()
        event = self.event(land, type="additional_cost", date="2025-03-01", amount=10.0)
        self.env.cr.execute(
            "UPDATE l10n_de_euer_asset_event SET type = 'extraordinary' WHERE id = %s",
            [event.id],
        )
        self.env.invalidate_all()
        self.asset(business_share=0.05)
        results = self.Checker._run(self.company, 2025)
        c62 = [result for result in results if result["code"] == "C-62"]
        self.assertEqual([result["res_ids"] for result in c62], [land.ids])
        self.assertIn("C-65", [result["code"] for result in results])
        with self.assertRaisesRegex(UserError, "Grundstück"):
            self.contributions(2025)
