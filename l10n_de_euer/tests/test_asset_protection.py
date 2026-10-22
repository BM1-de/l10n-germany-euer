# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Protection of locked years, rights, wizards and SKR04 (spec 7.7, 7.10, O-05).

All amounts and names are synthetic (decision D6).
"""

import base64
from datetime import date
from decimal import Decimal

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, new_test_user, tagged

from .common import AssetCase

D = Decimal
HEADER = (
    "Bezeichnung;Inventarnummer;Gruppe;Art;Lieferdatum;AK;ND (Monate);Methode;"
    "Satz;Wechsel zu linear (Jahr);kumulierte AfA bis Stichtag;Restbuchwert\n"
)


@tagged("post_install_l10n", "post_install", "-at_install")
class TestAssetProtection(AssetCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(
            cls.env,
            login="e3_bookkeeper",
            groups="account.group_account_user",
            company_id=cls.company.id,
            company_ids=[Command.set(cls.company.ids)],
        )

    def lock(self, year):
        self.Asset._lock_years(self.company, year)

    def event(self, asset, **values):
        return self.env["l10n_de_euer.asset.event"].create(
            {"asset_id": asset.id, **values}
        )

    # ------------------------------------------------------------ locks
    def test_draft_by_write(self):
        """Back to draft is refused for every way of writing."""
        asset = self.asset()
        self.lock(2024)
        with self.assertRaises(UserError):
            asset.write({"state": "draft"})
        with self.assertRaises(UserError):
            asset.with_user(self.user).write({"state": "draft"})
        with self.assertRaises(UserError):
            asset.action_set_draft()
        self.assertEqual(self.contributions(2024)["AVEUER.40.424"], D("400.00"))

    def test_internal_context_needs_superuser(self):
        """The context key of the register works only as superuser; a locked
        year is never unlocked."""
        asset = self.asset()
        self.lock(2024)
        internal = {"l10n_de_euer_asset_year_internal": True}
        lines = asset.year_ids.with_user(self.user).with_context(**internal)
        locked = lines.filtered(lambda line: line.year == 2024)
        open_line = lines.filtered(lambda line: line.year == 2025)
        with self.assertRaises(UserError):
            locked.write({"locked": False})
        with self.assertRaises(UserError):
            open_line.write(
                {
                    "private_use_decision": "confirmed",
                    "private_use_confirmed_amount": 999.0,
                }
            )
        with self.assertRaises(UserError):
            locked.sudo().write({"locked": False})
        self.assertEqual(self.contributions(2024)["AVEUER.40.424"], D("400.00"))
        # check results (C-65) follow the same rule
        self.asset(business_share=0.05, delivery_date="2026-01-10")
        check = self.env["l10n_de_euer.check"]._run_asset_checks(self.company, 2026)
        blocker = check.filtered(lambda result: result.code == "C-65")
        with self.assertRaises(UserError):
            blocker.with_user(self.user).with_context(
                l10n_de_euer_check_internal=True
            ).write({"severity": "info"})

    def test_inputs_of_locked_years(self):
        """Withdrawal value and VAT, Kz 407 and the home office stay."""
        self.period("regular", "2024-01-01")
        withdrawn = self.asset(cost_net=1000.0, input_tax=190.0)
        withdrawn.write(
            {
                "disposal_date": "2025-06-30",
                "disposal_kind": "withdrawal",
                "disposal_value": 500.0,
            }
        )
        withdrawn.action_dispose()
        car = self.asset(
            aveuer_group="vehicle", cost_net=30000.0, useful_life_months=72
        )
        office = self.asset(
            aveuer_group="office_building",
            method="manual",
            useful_life_months=0,
            cost_net=20000.0,
        )
        self.event(
            office,
            type="manual_depreciation",
            date="2025-12-31",
            amount=400.0,
            reason="2 % AfA",
        )
        self.lock(2025)
        for record, values in (
            (withdrawn, {"disposal_value": 900.0}),
            (withdrawn, {"disposal_vat_amount": 171.0}),
            (car, {"is_electric_or_plugin_hybrid": True}),
            (car, {"is_battery_electric": True}),
            (office, {"home_office_deductible": False}),
        ):
            with self.subTest(values=values), self.assertRaises(UserError):
                record.write(values)
        values = self.contributions(2025)
        self.assertEqual(values["EUER.20.102"], D("500.00"))
        self.assertEqual(values["EUER.25.172"], D("400.00"))
        self.assertEqual(
            self.Asset._get_key_values(self.company, 2025), {"AVEUER.40.407": "2"}
        )
        # an unchanged value is no change
        withdrawn.write({"disposal_value": 500.0})

    def test_company_of_the_register(self):
        """No move to another business; a branch is the main company."""
        asset = self.asset()
        other = self.env["res.company"].create({"name": "E3 Zweitbetrieb"})
        with self.assertRaises(UserError):
            asset.write({"company_id": other.id})
        branch = self.env["res.company"].create(
            {"name": "E3 Filiale", "parent_id": self.company.id}
        )
        on_branch = self.asset(company_id=branch.id)
        self.assertEqual(on_branch.company_id, self.company)
        self.assertEqual(self.contributions(2024)["AVEUER.40.422"], D("2400.00"))

    def test_disposal_month_after_lock(self):
        """The month convention cannot change once a locked year has a
        disposal; the locked year keeps its values in any case."""
        asset = self.asset()
        asset.write({"disposal_date": "2025-06-30", "disposal_kind": "scrap"})
        asset.action_dispose()
        self.lock(2025)
        with self.assertRaises(UserError):
            self.company.l10n_de_euer_disposal_month = False
        self.env.cr.execute(
            "UPDATE res_company SET l10n_de_euer_disposal_month = false WHERE id = %s",
            [self.company.id],
        )
        self.env.invalidate_all()
        values = self.contributions(2025)
        self.assertEqual(
            (values["AVEUER.40.424"], values["AVEUER.40.425"]),
            (D("200.00"), D("600.00")),
        )

    def test_lock_without_assets(self):
        """The lock of a year holds also without any asset of that year."""
        self.lock(2025)
        self.assertEqual(self.company.l10n_de_euer_asset_lock_year, 2025)
        with self.assertRaises(UserError):
            self.asset(
                asset_kind="gwg",
                method="none",
                cost_net=500.0,
                delivery_date="2025-05-01",
            )
        self.asset(
            asset_kind="gwg", method="none", cost_net=500.0, delivery_date="2026-05-01"
        )
        with self.assertRaises(UserError):
            self.company.l10n_de_euer_asset_lock_year = 2024

    def test_locked_pool(self):
        """A locked pool keeps its amount."""
        member = self.asset(
            asset_kind="pool", method="none", cost_net=500.0, delivery_date="2024-02-01"
        )
        self.lock(2024)
        with self.assertRaises(UserError):
            member.cost_net = 600.0
        with self.assertRaises(UserError):
            self.asset(
                asset_kind="pool",
                method="none",
                cost_net=400.0,
                delivery_date="2024-03-01",
            )

    # ------------------------------------------------------------ form, VAT
    def test_form_view_other_kinds(self):
        """Kinds without depreciation get the method None in the form."""
        for kind, group in (
            ("gwg", "other_movable"),
            ("pool", "office_equipment"),
            ("non_depreciable", "land"),
        ):
            with self.subTest(kind=kind):
                with Form(self.Asset) as form:
                    form.name = f"Formular {kind}"
                    form.asset_kind = kind
                    form.aveuer_group = group
                    form.delivery_date = date(2026, 2, 1)
                    form.cost_net = 500.0
                self.assertEqual(form.record.method, "none")
                form.record.action_activate()
                self.assertEqual(form.record.state, "active")

    def test_vat_treatment_against_status(self):
        """C-30: no status period, or small business with deductible input
        tax; a new asset of a small business cannot deduct input tax."""
        self.asset(cost_net=1000.0, input_tax=190.0, delivery_date="2026-02-01")
        self.assertEqual(len(self.results("C-30", 2026)), 1)
        self.period("ku", "2026-01-01")
        results = self.results("C-30", 2026)
        self.assertIn("small business", results[0]["message"])
        with self.assertRaises(ValidationError):
            self.asset(
                cost_net=1000.0,
                input_tax=190.0,
                delivery_date="2026-03-01",
                vat_treatment="deductible",
            )

    # ------------------------------------------------------------ vouchers
    def test_journal_items_against_asset(self):
        """C-68: the purchase reset to draft or changed after the asset."""
        bill = self.bill([("Kamera", self.acc("0490"), 1500.0, self.tax())])
        asset = self.from_lines(bill.invoice_line_ids, useful_life_months=84)
        asset.action_activate()
        self.assertFalse(self.results("C-68", 2026))
        bill.button_draft()
        self.assertEqual(len(self.results("C-68", 2026)), 1)
        bill.invoice_line_ids.price_unit = 3000.0
        bill.action_post()
        results = self.results("C-68", 2026)
        self.assertEqual(len(results), 1)
        self.assertIn("3000.00", results[0]["message"])

    def test_sales_invoice_not_deletable(self):
        """The sales invoice of a disposal is a voucher of the register."""
        asset = self.asset()
        invoice = self.env["account.move"].create(
            {
                "move_type": "out_invoice",
                "partner_id": self.partner_a.id,
                "invoice_date": "2025-06-30",
                "invoice_line_ids": [
                    Command.create(
                        {
                            "name": "Verkauf",
                            "account_id": self.acc("8829").id,
                            "price_unit": 500.0,
                            "tax_ids": [Command.clear()],
                        }
                    )
                ],
            }
        )
        asset.write(
            {
                "disposal_date": "2025-06-30",
                "disposal_kind": "sale",
                "disposal_invoice_id": invoice.id,
            }
        )
        asset.action_dispose()
        with self.assertRaisesRegex(UserError, "asset register"):
            invoice.unlink()

    def test_from_move_line_items(self):
        """Only debit items of fixed asset accounts; one asset per item gets
        the kind of the item; 0085 is land, not a building."""
        bill = self.bill(
            [
                ("Monitor", self.acc("0490"), 500.0, self.tax()),
                ("Drucker", self.acc("0490"), 900.0, self.tax()),
            ]
        )
        Wizard = self.env["l10n_de_euer.asset.from.move.line"]
        everything = Wizard.with_context(
            active_model="account.move.line", active_ids=bill.line_ids.ids
        ).create({})
        with self.assertRaises(UserError):
            everything.action_create()
        per_line = Wizard.with_context(
            active_model="account.move.line", active_ids=bill.invoice_line_ids.ids
        ).create({"mode": "per_line", "useful_life_months": 36})
        assets = self.Asset.search(per_line.action_create()["domain"])
        self.assertEqual(sorted(assets.mapped("asset_kind")), ["depreciable", "gwg"])
        land_bill = self.bill(
            [("Grund und Boden", self.acc("0085"), 30000.0, self.env["account.tax"])]
        )
        land = Wizard.with_context(
            active_model="account.move.line", active_ids=land_bill.invoice_line_ids.ids
        ).create({})
        self.assertEqual(
            (land.aveuer_group, land.asset_kind), ("land", "non_depreciable")
        )

    # ------------------------------------------------------------ import
    def wizard(self, content, encoding="utf-8", **values):
        return self.env["l10n_de_euer.asset.import"].create(
            {
                "company_id": self.company.id,
                "opening_date": "2026-12-31",
                "file": base64.b64encode(content.encode(encoding)),
                "filename": "anlagen.csv",
                **values,
            }
        )

    def test_import_preview_and_values(self):
        """German thousands separators, rates, the switch to linear, errors of
        the register and duplicate inventory numbers in the preview."""
        self.asset(code="ALT-1")
        content = HEADER + (
            "Laptop;INV-1;Büroausstattung;abnutzbar;01.07.2025;2.400;48;linear;;;"
            "900;1.500\n"
            "Doppelt;INV-1;Andere;abnutzbar;01.07.2025;1000;36;linear;;;;1000\n"
            "Ohne ND;INV-3;Andere;abnutzbar;01.07.2025;1000;;linear;;;0;1000\n"
            "Maschine;INV-4;Andere;abnutzbar;01.07.2025;50.000,00;120;degressiv;"
            "0,3;2026;11.973,68;38.026,32\n"
            "Komisch;INV-5;Andere;abnutzbar;01.07.2025;1234.567;36;linear;;;0;"
            "1234.567\n"
            "Vorhanden;ALT-1;Andere;abnutzbar;01.07.2025;1000;36;linear;;;;1000\n"
            "Degressiv;INV-8;Andere;abnutzbar;01.07.2025;10.000;120;degressiv;30;;"
            "4.050;5.950\n"
        )
        wizard = self.wizard(
            content,
            total_ids=[
                Command.create(
                    {"aveuer_group": "other_movable", "book_value_end": 43976.32}
                )
            ],
        )
        wizard.action_parse()
        lines = {line.sequence: line for line in wizard.line_ids}
        self.assertEqual(lines[2].cost, 2400.0)
        self.assertEqual(lines[5].degressive_rate, 0.3)
        self.assertEqual(lines[8].degressive_rate, 0.3)
        self.assertEqual(
            sorted(number for number, line in lines.items() if line.error),
            [2, 3, 4, 6, 7],
        )
        self.assertIn("useful life", lines[4].error)
        self.assertIn("ALT-1", lines[7].error)
        self.assertTrue(lines[8].warning)
        self.assertFalse(lines[5].warning)
        wizard.line_ids.filtered("error").unlink()
        wizard.action_import()
        machine = wizard.line_ids.asset_id.filtered(lambda a: a.name == "Maschine")
        self.assertEqual(machine.switch_to_linear_year, 2026)
        # 38,026.32 ÷ 102 × 12 = 4,473.68
        self.assertEqual(
            machine.year_ids.filtered(lambda line: line.year == 2027).depreciation,
            4473.68,
        )
        self.assertFalse(self.results("C-61", 2027))
        total = self.env["l10n_de_euer.asset.opening.total"].search(
            [("company_id", "=", self.company.id), ("year", "=", 2026)]
        )
        total.book_value_end = 40000.0
        self.assertEqual(len(self.results("C-61", 2027)), 1)

    def test_import_file_errors(self):
        """Missing columns, a takeover date that is no 31 December and a file
        in cp1252."""
        with self.assertRaisesRegex(UserError, "Missing columns"):
            self.wizard("Bezeichnung;AK\nTest;100\n").action_parse()
        with self.assertRaises(ValidationError):
            self.wizard(HEADER, opening_date="2026-06-30")
        wizard = self.wizard(
            HEADER
            + "Schrank;INV-9;Büroausstattung;abnutzbar;01.07.2025;1.200;36;linear;"
            ";;600;600\n",
            encoding="cp1252",
        )
        wizard.action_parse()
        self.assertEqual(wizard.line_ids.aveuer_group, "office_equipment")
        self.assertFalse(wizard.line_ids.error)

    def test_takeover_needs_manager(self):
        """Taking over the old register is released by a manager (7.10)."""
        with self.assertRaises(AccessError):
            self.env["l10n_de_euer.asset.import"].with_user(self.user).create(
                {
                    "company_id": self.company.id,
                    "file": base64.b64encode(HEADER.encode()),
                }
            )
        taken = self.Asset.with_user(self.user).create(
            {
                "name": "Altgerät",
                "company_id": self.company.id,
                "delivery_date": "2023-01-10",
                "origin": "legacy_import",
                "cost_net": 1200.0,
                "useful_life_months": 36,
                "opening_date": "2024-12-31",
                "opening_book_value": 400.0,
                "opening_accumulated": 800.0,
            }
        )
        with self.assertRaises(UserError):
            taken.action_activate()
        self.Asset.browse(taken.id).action_activate()  # as manager
        self.assertEqual(taken.state, "active")


@tagged("post_install_l10n", "post_install", "-at_install")
class TestAssetUserFlow(AssetCase):
    def test_bookkeeper_flow(self):
        """GT-13/O-09: the whole flow with account.group_account_user only."""
        user = new_test_user(
            self.env,
            login="e3_flow_user",
            groups="account.group_account_user",
            company_id=self.company.id,
            company_ids=[Command.set(self.company.ids)],
        )
        self.assertFalse(user.has_group("account.group_account_manager"))
        self.period("regular", "2024-01-01")
        bill = self.bill([("Kamera", self.acc("0490"), 1500.0, self.tax())])
        env = self.env(user=user)
        wizard = (
            env["l10n_de_euer.asset.from.move.line"]
            .with_context(
                active_model="account.move.line", active_ids=bill.invoice_line_ids.ids
            )
            .create({"useful_life_months": 84})
        )
        asset = env["l10n_de_euer.asset"].browse(wizard.action_create()["res_id"])
        asset.business_share = 0.8
        asset.action_activate()
        env["l10n_de_euer.asset.event"].create(
            {
                "asset_id": asset.id,
                "type": "extraordinary",
                "date": "2027-06-01",
                "amount": 100.0,
                "reason": "Schaden",
            }
        )
        line = asset.year_ids.filtered(lambda line: line.year == 2026)
        line.action_confirm_private_use()
        self.assertEqual(line.private_use_decision, "confirmed")
        checks = env["l10n_de_euer.check"]._run_asset_checks(self.company, 2026)
        self.assertFalse(checks.filtered(lambda check: check.code == "C-69"))
        env["l10n_de_euer.asset.check.wizard"].create(
            {"company_id": self.company.id, "year": 2026}
        ).action_run()
        asset.write({"disposal_date": "2028-02-01", "disposal_kind": "scrap"})
        asset.action_dispose()
        self.assertEqual(asset.state, "disposed")
        self.assertTrue(
            env["l10n_de_euer.asset"]._get_field_contributions(self.company, 2028)
        )


@tagged("post_install_l10n", "post_install", "-at_install")
class TestAssetSkr04(AssetCase):
    chart = "de_skr04"

    def test_group_proposals(self):
        """O-05: proposals of SKR04 accounts (0235 land, 0850 partial income
        method, 0520 vehicles, 0690 other movable assets)."""
        Wizard = self.env["l10n_de_euer.asset.from.move.line"]
        for code, group, kind in (
            ("0690", "other_movable", "depreciable"),
            ("0520", "vehicle", "depreciable"),
            ("0235", "land", "non_depreciable"),
            ("0850", "fin_tev", "non_depreciable"),
        ):
            with self.subTest(code=code):
                bill = self.bill(
                    [("Zugang", self.acc(code), 30000.0, self.env["account.tax"])]
                )
                wizard = Wizard.with_context(
                    active_model="account.move.line",
                    active_ids=bill.invoice_line_ids.ids,
                ).create({})
                self.assertEqual(
                    (wizard.aveuer_group, wizard.asset_kind), (group, kind)
                )

    def test_c64_and_register(self):
        """C-64 with SKR04 until the asset is in the register."""
        bill = self.bill([("Regal", self.acc("0690"), 2000.0, self.tax())])
        self.assertTrue(self.results("C-64", 2026))
        asset = self.from_lines(bill.invoice_line_ids, useful_life_months=120)
        asset.action_activate()
        self.assertFalse(self.results("C-64", 2026))
        self.assertEqual(asset.aveuer_group, "other_movable")
        # 2,000 ÷ 10 = 200, 10 months: 166.67
        self.assertEqual(self.contributions(2026)["AVEUER.40.424"], D("166.67"))
