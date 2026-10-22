# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Regression tests of the second review of the asset register (spec 6.3,
6.6, 7.7, 7.10, GT-16).

All amounts and names are synthetic (decision D6); expected values are
calculated by hand in the docstrings.
"""

import base64
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, new_test_user, tagged

from .common import AssetCase

D = Decimal
IMPORT_HEADER = (
    "Bezeichnung;Gruppe;Art;Lieferdatum;AK;ND (Monate);Methode;"
    "kumulierte AfA bis Stichtag;Restbuchwert;Sammelposten-Jahrgang;"
    "Sammelposten-Restbetrag;Sonder-AfA § 7g;Jahr Sonder-AfA § 7g\n"
)


@tagged("post_install_l10n", "post_install", "-at_install")
class TestAssetReview(AssetCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(
            cls.env,
            login="e3_review_user",
            groups="account.group_account_user",
            company_id=cls.company.id,
            company_ids=[Command.set(cls.company.ids)],
        )
        cls.auditor = new_test_user(
            cls.env,
            login="e3_review_auditor",
            groups="account.group_account_readonly",
            company_id=cls.company.id,
            company_ids=[Command.set(cls.company.ids)],
        )

    def lock(self, year):
        self.Asset._lock_years(self.company, year)

    @contextmanager
    def raises(self, exception, text):
        """``assertRaises`` (rolled back to a savepoint) with a part of the
        message; ``assertRaisesRegex`` would keep what was written before."""
        with self.assertRaises(exception) as catcher:
            yield catcher
        self.assertIn(text, str(catcher.exception))

    def event(self, asset, **values):
        return self.env["l10n_de_euer.asset.event"].create(
            {"asset_id": asset.id, **values}
        )

    def depreciation(self, asset):
        return {
            line.year: line.depreciation
            for line in asset.year_ids
            if not line.is_opening
        }

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

    def import_wizard(self, content, opening_date="2024-12-31"):
        if isinstance(content, str):
            content = content.encode()
        return self.env["l10n_de_euer.asset.import"].create(
            {
                "company_id": self.company.id,
                "opening_date": opening_date,
                "file": base64.b64encode(content),
                "filename": "anlagen.csv",
            }
        )

    # ------------------------------------------------------------ rules
    def test_building_keeps_its_percentage(self):
        """§ 7 (4) sentence 1 EStG, H 7.4 EStH: 300,000 from 01/2015 at 3 %,
        additional production cost 30,000 in 2025: 330,000 × 3 % = 9,900
        (Kz 114), not 240,000 over the remaining 280 months (10,285.71)."""
        building = self.asset(
            aveuer_group="building",
            cost_net=300000.0,
            useful_life_months=400,
            delivery_date="2015-01-15",
        )
        self.event(building, type="additional_cost", date="2025-06-01", amount=30000.0)
        years = self.depreciation(building)
        self.assertEqual((years[2025], years[2026]), (9900.0, 9900.0))
        self.assertEqual(max(years), 2049)
        self.assertEqual(self.contributions(2025)["AVEUER.40.114"], D("9900.00"))

    def test_sec7g_profit_of_the_year_before_the_acquisition(self):
        """§ 7g (6) no. 1 EStG: the profit of the business year before the
        acquisition (2023), also for special depreciation taken in 2026."""
        asset = self.asset(
            cost_net=10000.0, useful_life_months=120, delivery_date="2024-01-05"
        )
        with self.raises(ValidationError, "profit of 2023"):
            self.event(
                asset,
                type="sec7g_special",
                date="2026-06-01",
                amount=1000.0,
                reason="Sonder-AfA",
            )

    def test_contribution_over_the_remaining_life(self):
        """R 7.4 (10) EStR: bought privately 03/2023 for 2,000 with 84 months,
        contributed 09/2025 at a fair value of 1,500: 2,000 − 2,000 × 30 / 84
        = 1,285.71; remaining 54 months: J = 285.71, 2025 4/12 = 95.24."""
        values = {
            "origin": "contribution",
            "delivery_date": "2025-09-01",
            "cost_net": 0.0,
            "contribution_fair_value": 1500.0,
            "private_acquisition_date": "2023-03-10",
            "private_acquisition_cost": 2000.0,
        }
        with self.raises(ValidationError, "total useful life"):
            self.asset(useful_life_months=54, **values)
        with self.raises(ValidationError, "54 months"):
            self.asset(useful_life_months=84, private_useful_life_months=84, **values)
        asset = self.asset(
            useful_life_months=54, private_useful_life_months=84, **values
        )
        self.assertEqual(asset.acquisition_cost, 1285.71)
        years = self.depreciation(asset)
        self.assertEqual((years[2025], years[2026]), (95.24, 285.71))
        with Form(self.Asset) as form:
            form.name = "Einlage"
            form.origin = "contribution"
            form.delivery_date = date(2025, 9, 1)
            form.contribution_fair_value = 1500.0
            form.private_acquisition_date = date(2023, 3, 10)
            form.private_acquisition_cost = 2000.0
            form.private_useful_life_months = 84
            self.assertEqual(form.useful_life_months, 54)

    def test_rule_messages(self):
        """A switch to linear in the year of delivery, a subsidy above the
        book value and negative events are refused with a clear message."""
        with self.raises(ValidationError, "method Linear"):
            self.asset(
                method="degressive",
                degressive_rate=0.3,
                delivery_date="2025-08-01",
                cost_net=10000.0,
                useful_life_months=96,
                switch_to_linear_year=2025,
            )
        digital = self.asset(
            method="digital_1y",
            useful_life_months=0,
            delivery_date="2025-03-01",
            cost_net=1500.0,
            aveuer_group="office_equipment",
        )
        with self.raises(ValidationError, "business income"):
            self.event(digital, type="subsidy", date="2026-02-01", amount=300.0)
        asset = self.asset()
        for values in (
            {"type": "extraordinary", "amount": -100.0, "reason": "Schaden"},
            {"type": "additional_cost", "amount": 100.0, "useful_life_months": -5},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.event(asset, date="2025-03-01", **values)

    def test_takeover_values(self):
        """Spec 6.6 also in the form: 0 ≤ book value ≤ cost, book value =
        cost − depreciation up to the takeover; the form proposes the
        depreciation."""
        for book, accumulated in ((1300.0, -100.0), (-10.0, 1210.0), (400.0, 700.0)):
            with self.subTest(book=book), self.assertRaises(ValidationError):
                self.asset(
                    origin="legacy_import",
                    opening_date="2024-12-31",
                    opening_book_value=book,
                    opening_accumulated=accumulated,
                )
        with Form(self.Asset) as form:
            form.name = "Altgerät"
            form.origin = "legacy_import"
            form.delivery_date = date(2023, 1, 10)
            form.cost_net = 1200.0
            form.useful_life_months = 36
            form.opening_date = date(2024, 12, 31)
            form.opening_book_value = 400.0
            self.assertEqual(form.opening_accumulated, 800.0)

    # ------------------------------------------------------------ locks
    def test_changes_after_a_locked_year(self):
        """Assets without lines in the locked year still change in open years:
        land with development costs and sold in 2026 (Kz 102, 105), a fully
        depreciated asset scrapped in 2025 (Kz 420), securities of the current
        assets sold in 2025 (Kz 605 → Kz 100). The locked year stays."""
        land = self.land(delivery_date="2020-03-01")
        done = self.asset(name="Altgerät", delivery_date="2020-01-10")
        paper = self.asset(
            name="Wertpapier",
            asset_kind="current_4_3_4",
            method="none",
            aveuer_group="current",
            cost_net=8000.0,
            useful_life_months=0,
            delivery_date="2023-03-01",
        )
        before = self.contributions(2024)
        self.assertEqual(before["AVEUER.40.420"], D("1200.00"))
        self.lock(2024)
        self.event(land, type="additional_cost", date="2026-02-01", amount=3000.0)
        land.write(
            {
                "disposal_date": "2026-05-15",
                "disposal_kind": "sale",
                "disposal_value": 60000.0,
                "disposal_receipt_date": "2026-05-20",
            }
        )
        land.action_dispose()
        done.write({"disposal_date": "2025-06-30", "disposal_kind": "scrap"})
        done.action_dispose()
        paper.write(
            {
                "disposal_date": "2025-09-01",
                "disposal_kind": "sale",
                "disposal_value": 9000.0,
                "disposal_receipt_date": "2025-09-01",
            }
        )
        paper.action_dispose()
        self.assertEqual(self.contributions(2024), before)
        values = self.contributions(2025)
        self.assertEqual(values["AVEUER.40.605"], D("8000.00"))
        self.assertEqual(values["EUER.25.100"], D("8000.00"))
        self.assertEqual(values["AVEUER.40.420"], D("1200.00"))
        values = self.contributions(2026)
        self.assertEqual(values["AVEUER.40.102"], D("3000.00"))
        self.assertEqual(values["AVEUER.40.105"], D("53000.00"))
        self.assertNotIn("AVEUER.40.420", values)

    def test_locked_year_without_line(self):
        """A year locked before its line existed gets the line, locked, as
        long as the asset only carries its book value forward."""
        land = self.land(delivery_date="2020-03-01")
        self.lock(2024)
        self.env.cr.execute(
            "DELETE FROM l10n_de_euer_asset_year WHERE asset_id = %s AND year > 2020",
            [land.id],
        )
        self.env.invalidate_all()
        land.write(
            {
                "disposal_date": "2026-05-15",
                "disposal_kind": "sale",
                "disposal_receipt_date": "2026-05-20",
            }
        )
        land.action_dispose()
        lines = land.year_ids.filtered(lambda line: 2021 <= line.year <= 2024)
        self.assertEqual(len(lines), 4)
        self.assertTrue(all(lines.mapped("locked")))
        self.assertEqual(self.contributions(2026)["AVEUER.40.105"], D("50000.00"))

    def test_locked_withdrawal_by_its_effect(self):
        """Spec 7.7: what changes the form values of a locked year is refused,
        whatever the field (kind of disposal, input tax treatment, origin);
        the VAT of a recorded withdrawal is no longer a proposal."""
        self.period("regular", "2024-01-01")
        scrapped = self.asset(cost_net=1000.0, input_tax=190.0)
        scrapped.write(
            {
                "disposal_date": "2025-06-30",
                "disposal_kind": "scrap",
                "disposal_value": 300.0,
            }
        )
        scrapped.action_dispose()
        withdrawn = self.asset(name="Entnahme", cost_net=1000.0, input_tax=190.0)
        withdrawn.write(
            {
                "disposal_date": "2025-06-30",
                "disposal_kind": "withdrawal",
                "disposal_value": 500.0,
            }
        )
        withdrawn.action_dispose()
        self.lock(2025)
        before = self.contributions(2025)
        self.assertEqual(before["EUER.20.140"], D("95.00"))
        for record, values in (
            (scrapped, {"disposal_kind": "withdrawal"}),
            (withdrawn, {"vat_treatment": "sec15_1a"}),
            (withdrawn, {"disposal_kind": "sale"}),
            (withdrawn, {"input_tax": 0.0}),
        ):
            with self.subTest(values=values), self.assertRaises(UserError):
                record.write(values)
        self.assertEqual(self.contributions(2025), before)
        later = self.asset(
            name="Später", cost_net=1000.0, input_tax=190.0, delivery_date="2026-01-10"
        )
        later.write(
            {
                "disposal_date": "2026-06-30",
                "disposal_kind": "withdrawal",
                "disposal_value": 500.0,
            }
        )
        later.action_dispose()
        later.disposal_vat_amount = 80.0
        later.input_tax = 189.0
        self.assertEqual(later.disposal_vat_amount, 80.0)
        self.assertEqual(self.contributions(2026)["EUER.20.140"], D("80.00"))

    def test_calculated_fields_and_locked_pools(self):
        """Calculated fields of an asset and a locked pool cannot be written,
        also not as superuser; a locked pool keeps its amount (Kz 432) and a
        locked low-value asset its Kz 132."""
        member = self.asset(
            asset_kind="pool",
            method="none",
            cost_net=600.0,
            useful_life_months=0,
            delivery_date="2024-02-01",
        )
        gwg = self.asset(
            asset_kind="gwg",
            method="none",
            cost_net=500.0,
            useful_life_months=0,
            delivery_date="2024-03-01",
        )
        self.lock(2024)
        later = self.asset(
            asset_kind="pool",
            method="none",
            cost_net=700.0,
            useful_life_months=0,
            delivery_date="2025-02-01",
        )
        pool = member.pool_id
        self.assertTrue(pool.locked)
        for record, values in (
            (later, {"pool_id": pool.id}),
            (gwg, {"acquisition_cost": 50.0}),
            (gwg.sudo(), {"acquisition_cost": 50.0}),
            (later.with_user(self.user), {"limit_value": 1.0}),
            (gwg, {"in_gwg_register": False}),
        ):
            with self.subTest(values=values), self.assertRaises(UserError):
                record.write(values)
        for values in ({"locked": False}, {"year": 2026}, {"locked_amount": 1.0}):
            with self.subTest(values=values), self.assertRaises(UserError):
                pool.sudo().write(values)
        with self.assertRaises(UserError):
            self.env["l10n_de_euer.asset.pool"].sudo().create(
                {"company_id": self.company.id, "year": 2030}
            )
        self.env.cr.execute(
            "UPDATE l10n_de_euer_asset_pool SET amount = 999 WHERE id = %s", [pool.id]
        )
        self.env.cr.execute(
            "UPDATE l10n_de_euer_asset SET acquisition_cost = 50 WHERE id = %s",
            [gwg.id],
        )
        self.env.invalidate_all()
        values = self.contributions(2024)
        self.assertEqual(values["AVEUER.40.432"], D("600.00"))
        self.assertEqual(values["EUER.25.132"], D("500.00"))

    # ------------------------------------------------------------ rights
    def test_takeover_fields_need_a_manager(self):
        """Spec 7.10: a bookkeeper can neither turn an asset into a takeover
        nor change a takeover released by a manager."""
        asset = self.asset()
        with self.assertRaises(UserError):
            asset.with_user(self.user).write(
                {
                    "origin": "legacy_import",
                    "opening_date": "2024-12-31",
                    "opening_book_value": 50.0,
                    "opening_accumulated": 1150.0,
                }
            )
        taken = self.asset(
            origin="legacy_import",
            opening_date="2024-12-31",
            opening_book_value=400.0,
            opening_accumulated=800.0,
        )
        for values in (
            {"opening_book_value": 50.0, "opening_accumulated": 1150.0},
            {"origin": "purchase", "opening_date": False},
            {"memo_value": 1.0},
        ):
            with self.subTest(values=values), self.assertRaises(UserError):
                taken.with_user(self.user).write(values)
        taken.write({"opening_book_value": 50.0, "opening_accumulated": 1150.0})
        self.assertEqual(self.depreciation(taken)[2025], 50.0)
        asset.with_user(self.user).name = "Umbenannt"

    def test_auditor_cannot_recalculate(self):
        """Readonly users (auditors) read the register but do not write it."""
        asset = self.asset()
        with self.assertRaises(AccessError):
            asset.with_user(self.auditor).action_compute_years()
        asset.with_user(self.user).action_compute_years()

    def test_disposal_month_setting_recalculates(self):
        """The stored years of disposals follow the month convention."""
        asset = self.asset()
        asset.write({"disposal_date": "2025-06-30", "disposal_kind": "scrap"})
        asset.action_dispose()
        self.company.l10n_de_euer_disposal_month = False
        line = asset.year_ids.filtered(lambda line: line.year == 2025)
        self.assertEqual(
            (line.depreciation, line.disposal_book_value), (166.67, 633.33)
        )

    # ------------------------------------------------------------ wizards
    def test_journal_items_of_one_asset(self):
        """A journal item is the purchase of one asset (the cost would count
        twice); only posted items; one asset per item takes the delivery date
        of its own document."""
        bill = self.bill([("Kamera", self.acc("0490"), 1500.0, self.tax())])
        first = self.from_lines(bill.invoice_line_ids, useful_life_months=84)
        with self.raises(UserError, "already the purchase"):
            self.from_lines(bill.invoice_line_ids, useful_life_months=84)
        first.action_activate()
        twin = self.asset(
            name="Zwilling",
            cost_net=1500.0,
            delivery_date="2026-03-15",
            useful_life_months=84,
            move_line_ids=[Command.set(bill.invoice_line_ids.ids)],
        )
        results = self.results("C-68", 2026)
        self.assertEqual(len(results), 1)
        self.assertIn("3000.00", results[0]["message"])
        (first | twin).write({"cost_net": 750.0})
        self.assertFalse(self.results("C-68", 2026))
        draft = self.bill(
            [("Monitor", self.acc("0490"), 900.0, self.tax())], post=False
        )
        with self.raises(UserError, "posted"):
            self.from_lines(draft.invoice_line_ids, useful_life_months=36)
        march = self.bill(
            [("Regal", self.acc("0490"), 900.0, self.tax())], on_date="2026-03-02"
        )
        may = self.bill(
            [("Tisch", self.acc("0490"), 1100.0, self.tax())], on_date="2026-05-04"
        )
        lines = march.invoice_line_ids | may.invoice_line_ids
        wizard = (
            self.env["l10n_de_euer.asset.from.move.line"]
            .with_context(active_model="account.move.line", active_ids=lines.ids)
            .create({"mode": "per_line", "useful_life_months": 36})
        )
        assets = self.Asset.search(wizard.action_create()["domain"])
        self.assertEqual(
            sorted(assets.mapped("delivery_date")), [date(2026, 3, 2), date(2026, 5, 4)]
        )

    def test_import_rows_and_files(self):
        """Pool year and delivery date must agree; numbers out of range and
        files that are no CSV are errors, not tracebacks."""
        wizard = self.import_wizard(
            IMPORT_HEADER
            + "Pool;Andere;Sammelposten;15.03.2023;1000;;;;;2022;400;;\n"
            + "Pool falsch;Andere;Sammelposten;;1000;;;;;20221;400;;\n"
            + "Lang;Andere;abnutzbar;01.01.2024;1000;3000000000;linear;250;750;;;;\n"
            + "Pool gut;Andere;Sammelposten;;1000;;;;;2022;400;;\n"
        )
        wizard.action_parse()
        lines = {line.sequence: line for line in wizard.line_ids}
        self.assertIn("year of the asset pool", lines[2].error)
        self.assertTrue(lines[3].error)
        self.assertTrue(lines[4].error)
        self.assertFalse(lines[5].error)
        for content in (
            b"\xff\xfeB\x00e\x00z\x00",  # UTF-16, as a spreadsheet may save
            IMPORT_HEADER.encode() + b"Kamera\x00;Andere;abnutzbar\n",
            b"Bezeichnung;AK\n\x81\x8d;100\n",
        ):
            with self.subTest(content=content[:12]):
                with self.raises(UserError, "no CSV"):
                    self.import_wizard(content).action_parse()

    def test_import_special_depreciation(self):
        """§ 7a (9) after a takeover: 10,000 from 01/2024 over 10 years, § 7g
        4,000 in 2024, book value 3,000 on 31.12.2026: 1,000 in 2027 and
        2028, then 1,000 over 60 months (200). A book value below the linear
        depreciation without the column gets a hint."""
        wizard = self.import_wizard(
            IMPORT_HEADER
            + "Maschine;Andere;abnutzbar;05.01.2024;10.000;120;linear;7.000;3.000;;;"
            + "4.000;2024\n"
            + "Ohne;Andere;abnutzbar;05.01.2024;10.000;120;linear;7.000;3.000;;;;\n"
            + "Zu spät;Andere;abnutzbar;05.01.2024;10.000;120;linear;7.000;3.000;;;"
            + "4.000;2029\n",
            opening_date="2026-12-31",
        )
        wizard.action_parse()
        lines = {line.sequence: line for line in wizard.line_ids}
        self.assertFalse(lines[2].error)
        self.assertFalse(lines[2].warning)
        self.assertIn("below the linear depreciation", lines[3].warning)
        self.assertIn("four following years", lines[4].error)
        wizard.line_ids.filtered("error").unlink()
        wizard.action_import()
        machine = wizard.line_ids.asset_id.filtered(lambda a: a.name == "Maschine")
        self.assertEqual(machine.event_ids.mapped("type"), ["sec7g_special"])
        years = self.depreciation(machine)
        self.assertEqual(
            (years[2027], years[2028], years[2029]), (1000.0, 1000.0, 200.0)
        )
        self.assertEqual(max(years), 2033)

    # ------------------------------------------------------------ GT-16
    def snapshot(self):
        """Count and content of every journal entry and item, compared as
        text of the whole row (all columns, write_date included)."""
        self.env.flush_all()
        cr = self.env.cr
        cr.execute(
            "SELECT count(*), md5(coalesce(string_agg(m::text, '|' ORDER BY m.id), "
            "'')) FROM account_move m"
        )
        moves = cr.fetchone()
        cr.execute(
            "SELECT count(*), md5(coalesce(string_agg(l::text, '|' ORDER BY l.id), "
            "'')) FROM account_move_line l"
        )
        return moves, cr.fetchone()

    def test_gt16_no_write_to_journal_entries(self):
        """GT-16, B-01: a whole run of the register (wizard, activation,
        events, private use, import, disposal with a sales invoice, checks,
        field contributions, locking) leaves account_move and
        account_move_line unchanged, checked by SQL."""
        self.period("regular", "2024-01-01")
        bill = self.bill(
            [("Kamera", self.acc("0490"), 1500.0, self.tax())], on_date="2025-03-15"
        )
        invoice = self.env["account.move"].create(
            {
                "move_type": "out_invoice",
                "partner_id": self.partner_a.id,
                "invoice_date": "2026-06-30",
                "date": "2026-06-30",
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
        invoice.action_post()
        snapshot = self.snapshot()
        asset = self.from_lines(bill.invoice_line_ids, useful_life_months=84)
        asset.business_share = 0.8
        asset.action_activate()
        self.event(
            asset,
            type="extraordinary",
            date="2025-10-01",
            amount=100.0,
            reason="Schaden",
        )
        asset.year_ids.filtered(lambda line: line.year == 2025)._compute_private_use()
        asset.year_ids.filtered(
            lambda line: line.year == 2025
        ).action_confirm_private_use()
        wizard = self.import_wizard(
            IMPORT_HEADER
            + "Altgerät;Andere;abnutzbar;10.01.2023;1200;36;linear;800;400;;;;\n"
        )
        wizard.action_parse()
        wizard.action_import()
        asset.write(
            {
                "disposal_date": "2026-06-30",
                "disposal_kind": "sale",
                "disposal_value": 500.0,
                "disposal_invoice_id": invoice.id,
            }
        )
        asset.action_dispose()
        for year in (2025, 2026):
            self.Checker._run(self.company, year)
            self.env["l10n_de_euer.check"]._run_asset_checks(self.company, year)
            self.contributions(year)
            self.Asset._get_aveuer_details(self.company, year)
            self.Asset._get_key_values(self.company, year)
        self.lock(2025)
        self.assertEqual(self.snapshot(), snapshot)
