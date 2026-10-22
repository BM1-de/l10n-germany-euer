# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Takeover of fictitious Papierkram fixed assets into the asset register."""

import base64
import os
import tempfile
from datetime import date
from decimal import Decimal

from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.l10n_de_papierkram_import.tests.common import (
    KATEGORIEN,
    make_export,
)

from ..models.papierkram_import import _group

D = Decimal
CATEGORIES = KATEGORIEN + (
    "Sofortabschreibung ab 2021: PC, Laptop und andere Computer-Hardware;490;"
    "Sonstige BGA;NEUTRAL;INSTANT;ASSET\n"
)
ASSETS = (
    "\nBelegnummer;Beleg;Restwert Jahresende;Anschaffungsdatum;Anschaffungswert;"
    "monatl. Abschreibung\n"
    "B-00002;Beamer;240,00;12.07.2024;840,00;20,00\n"
    "B-00005;Laptop;0,00;03.02.2025;999,00;0,00\n"
    "B-00010;Plotter;866,67;10.03.2026;1.200,00;33,33\n"
)


def voucher(number, name, day, paid, amount, category):
    gross = f"-{amount:.2f}".replace(".", ",")
    return (
        f"{number};{name};Technik AG;{day};;{paid};paid;{gross};0,00;{gross};0;"
        f"domestic;{category};;;;;"
    )


def build_attachments(cls):
    """Exports 2024–2026 with a projector, a laptop and a plotter."""
    cls.tmp = tempfile.TemporaryDirectory()
    cls.addClassCleanup(cls.tmp.cleanup)
    d = cls.tmp.name
    paths = [
        make_export(
            d,
            2024,
            belege=[
                voucher(
                    "B-00002",
                    "Beamer",
                    "12.07.2024",
                    "2024-07-15",
                    840,
                    "Betriebsausstattung (Anlage)",
                )
            ],
        ),
        make_export(
            d,
            2025,
            belege=[
                voucher(
                    "B-00005",
                    "Laptop",
                    "03.02.2025",
                    "2025-02-05",
                    999,
                    "Sofortabschreibung ab 2021: PC, Laptop und andere "
                    "Computer-Hardware",
                )
            ],
        ),
        make_export(
            d,
            2026,
            belege=[
                voucher(
                    "B-00010",
                    "Plotter",
                    "10.03.2026",
                    "2026-03-12",
                    1200,
                    "Betriebsausstattung (Anlage)",
                )
            ],
        ),
    ]
    attachments = cls.env["ir.attachment"]
    for path in paths:
        with open(path, "rb") as fh:
            attachments |= cls.env["ir.attachment"].create(
                {
                    "name": os.path.basename(path),
                    "datas": base64.b64encode(fh.read()),
                }
            )
    return attachments


@tagged("post_install_l10n", "post_install", "-at_install")
class TestAssetTakeover(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.l10n_de_euer_enabled = True
        attachments = build_attachments(cls)
        cls.pk_run = cls.env["papierkram.import"].create(
            {
                "export_attachment_ids": [(6, 0, attachments.ids)],
                "categories_file": base64.b64encode(CATEGORIES.encode()),
                "assets_file": base64.b64encode(ASSETS.encode()),
                "depreciation_until": date(2026, 12, 31),
                "euer_takeover_date": date(2025, 12, 31),
            }
        )
        cls.pk_run.action_check()
        cls.pk_run.action_import()
        cls.Asset = cls.env["l10n_de_euer.asset"]

    def asset(self, code):
        asset = self.pk_run.euer_asset_ids.filtered(lambda a: a.code == code)
        self.assertEqual(len(asset), 1, code)
        return asset

    def test_takeover_enabled_by_the_euer(self):
        self.assertTrue(self.pk_run.euer_asset_takeover)
        self.assertEqual(self.pk_run.state, "done")
        self.assertEqual(self.pk_run.euer_asset_count, 3)

    def test_depreciation_entries_only_up_to_the_takeover(self):
        """Journal entries keep the history up to 2025; from 2026 on the
        register computes the depreciation."""
        self.assertEqual(self.pk_run.depreciation_until, date(2025, 12, 31))
        entries = self.pk_run.move_ids.filtered(
            lambda m: m.move_type == "entry" and (m.ref or "").startswith("AfA")
        )
        self.assertEqual(
            sorted((m.date.year, m.amount_total) for m in entries),
            [(2024, 120.0), (2025, 240.0), (2025, 999.0)],
        )

    def test_asset_taken_over_with_the_papierkram_book_value(self):
        """840.00 from July 2024, 42 months: 120.00 (2024) + 240.00 (2025) =
        360.00, book value 480.00 at 31.12.2025, memo value 1 € like
        Papierkram."""
        projector = self.asset("B-00002")
        self.assertEqual(projector.origin, "legacy_import")
        self.assertEqual(projector.asset_kind, "depreciable")
        self.assertEqual(projector.method, "linear")
        self.assertEqual(projector.useful_life_months, 42)
        self.assertEqual(projector.aveuer_group, "other_movable")
        self.assertEqual(projector.opening_date, date(2025, 12, 31))
        self.assertEqual(projector.opening_book_value, 480.0)
        self.assertEqual(projector.opening_accumulated, 360.0)
        self.assertEqual(projector.memo_value, 1.0)

    def test_computer_hardware_written_off(self):
        laptop = self.asset("B-00005")
        self.assertEqual(laptop.method, "digital_1y")
        self.assertEqual(laptop.opening_book_value, 0.0)

    def test_later_purchase_linked_to_its_voucher(self):
        plotter = self.asset("B-00010")
        self.assertEqual(plotter.origin, "purchase")
        self.assertEqual(plotter.delivery_date, date(2026, 3, 10))
        self.assertEqual(plotter.cost_net, 1200.0)
        self.assertEqual(plotter.move_line_ids.move_id.papierkram_number, "B-00010")
        self.assertEqual(plotter.state, "active")

    def test_depreciation_2026_from_the_register(self):
        """2026: projector 12 × 20.00 = 240.00, plotter 1,200.00 / 36 × 10 months =
        333.33; together Kz 130 = 573.33."""
        values = self.Asset._get_form_values(self.env.company, 2026)
        self.assertEqual(values["EUER.25.130"], D("573.33"))

    def test_vat_treatment_entered(self):
        periods = self.env["l10n_de_euer.vat.period"].search(
            [("company_id", "=", self.env.company.id)]
        )
        self.assertEqual(
            periods.mapped(lambda p: (p.date_from, p.regime)),
            [(date(2024, 1, 1), "ku")],
        )

    def test_rerun_does_not_duplicate_assets(self):
        again = self.pk_run.copy({"state": "draft"})
        again.action_check()
        again.action_import()
        self.assertFalse(again.euer_asset_ids)
        self.assertIn("already in the asset register", again.log)
        self.assertEqual(
            self.Asset.search_count([("company_id", "=", self.env.company.id)]), 3
        )

    def test_without_euer_nothing_changes(self):
        """Without the EÜR the takeover is off by default; switched on, the
        import asks to enable the EÜR first."""
        company = self.env["res.company"].create({"name": "Ohne EÜR"})
        self.env.user.company_ids |= company
        Import = self.env["papierkram.import"].with_company(company)
        self.assertFalse(
            Import.default_get(["euer_asset_takeover"]).get("euer_asset_takeover")
        )
        run = Import.create({"euer_asset_takeover": True})
        run.export_attachment_ids = self.pk_run.export_attachment_ids
        with self.assertRaises(UserError):
            run.action_check()


@tagged("post_install_l10n", "post_install", "-at_install")
class TestAssetTakeoverLater(AccountTestInvoicingCommon):
    """An import made before the EÜR gets its assets later with the button."""

    @classmethod
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        attachments = build_attachments(cls)
        cls.pk_run = cls.env["papierkram.import"].create(
            {
                "export_attachment_ids": [(6, 0, attachments.ids)],
                "categories_file": base64.b64encode(CATEGORIES.encode()),
                "assets_file": base64.b64encode(ASSETS.encode()),
                "depreciation_until": date(2025, 12, 31),
            }
        )
        cls.pk_run.action_check()
        cls.pk_run.action_import()

    def test_take_over_later(self):
        run = self.pk_run
        self.assertFalse(run.euer_asset_takeover)
        self.assertFalse(run.euer_asset_ids)
        with self.assertRaises(UserError):
            run.action_euer_take_over_assets()
        self.env.company.l10n_de_euer_enabled = True
        run.euer_takeover_date = date(2025, 12, 31)
        run.action_euer_take_over_assets()
        self.assertEqual(run.euer_asset_count, 3)
        values = self.env["l10n_de_euer.asset"]._get_form_values(self.env.company, 2026)
        self.assertEqual(values["EUER.25.130"], D("573.33"))

    def test_takeover_without_a_date_takes_the_last_year(self):
        """Without a date the button takes 31 December of the last year: the
        year before is computed in Papierkram, the register starts after."""
        self.env.company.l10n_de_euer_enabled = True
        self.pk_run.euer_takeover_date = False
        self.pk_run.action_euer_take_over_assets()
        self.assertEqual(
            self.pk_run.euer_takeover_date, date(date.today().year - 1, 12, 31)
        )


@tagged("post_install_l10n", "post_install", "-at_install")
class TestAssetTakeoverGuards(AccountTestInvoicingCommon):
    """Rules and buttons of the takeover around the import itself."""

    @classmethod
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.l10n_de_euer_enabled = True

    def test_takeover_date_is_a_31_december(self):
        """The date separates the years computed in Papierkram from those of
        the register, so a part of a year is no takeover date."""
        Import = self.env["papierkram.import"]
        for day in (date(2025, 6, 30), False):
            with self.assertRaises(ValidationError):
                Import.create(
                    {
                        "name": "Übernahme",
                        "euer_asset_takeover": True,
                        "euer_takeover_date": day,
                    }
                )

    def test_assets_are_taken_over_after_the_import(self):
        """The button works on the result of an import; a draft has none."""
        run = self.env["papierkram.import"].create({"name": "Entwurf"})
        self.assertEqual(run.state, "draft")
        with self.assertRaises(UserError):
            run.action_euer_take_over_assets()

    def test_action_lists_the_assets_of_the_run(self):
        run = self.env["papierkram.import"].create({"name": "Lauf ohne Anlagen"})
        action = run.action_view_euer_assets()
        self.assertEqual(action["domain"], [("id", "in", [])])

    def test_group_of_an_account_outside_the_ranges(self):
        """A category on an account that is no fixed asset account of the
        SKR03 lands under the other movable goods of the AVEÜR."""
        self.assertEqual(_group("9999"), "other_movable")
        self.assertEqual(_group("0420"), "office_equipment")
        self.assertEqual(_group("320"), "vehicle")
