# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Takeover of fictitious Papierkram assets of every kind: several assets on
one voucher, pool, low-value asset, non-depreciable asset, assets without
voucher or category and purchases that cannot be linked or entered."""

import base64
import os
import tempfile
from datetime import date
from decimal import Decimal

from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.l10n_de_papierkram_import.tests.common import make_export

from .test_asset_takeover import CATEGORIES, voucher

D = Decimal
MORE_CATEGORIES = CATEGORIES + (
    "GWG-Sammelposten;485;Sammelposten;NEUTRAL;GWG;ASSET\n"
    "Geringwertige Wirtschaftsgüter (GWG);480;GWG;NEUTRAL;INSTANT;ASSET\n"
)
ASSETS = (
    "\nBelegnummer;Beleg;Restwert Jahresende;Anschaffungsdatum;Anschaffungswert;"
    "monatl. Abschreibung\n"
    # two assets on one voucher, 60 months each
    "B-00020;Tisch;480,00;10.01.2025;600,00;10,00\n"
    "B-00020;Lampe;240,00;10.01.2025;300,00;5,00\n"
    # asset pool, written off at once by Papierkram
    "B-00021;Regale;0,00;05.02.2025;900,00;0,00\n"
    # asset pool depreciated by Papierkram like the register: one fifth
    "B-00025;Kleinregal;400,00;10.01.2025;500,00;8,33\n"
    # voucher split into postage and the asset
    "B-00022;Monitor;715,00;01.03.2025;990,00;27,50\n"
    # depreciable category without a monthly rate: not depreciated
    "B-00023;Skulptur;0,00;01.03.2025;2.000,00;0,00\n"
    "B-09999;Ohne Beleg;0,00;01.03.2025;100,00;0,00\n"
    "B-00024;Porto statt Anlage;0,00;01.03.2025;10,00;0,00\n"
    # purchases after the takeover date
    "B-00030;Drucker;700,00;01.02.2026;700,00;19,44\n"
    "B-00031;Kamera;1.000,00;01.03.2026;1.000,00;27,78\n"
    "B-00032;Großer Stuhl;0,00;01.04.2026;1.500,00;0,00\n"
)
SPLITS = (
    "belegnummer;position;kategorie;betrag_brutto\n"
    "B-00022;1;Porto;10.00\n"
    "B-00022;2;Betriebsausstattung (Anlage);990.00\n"
)


@tagged("post_install_l10n", "post_install", "-at_install")
class TestAssetTakeoverCases(AccountTestInvoicingCommon):
    @classmethod
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.l10n_de_euer_enabled = True
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        asset = "Betriebsausstattung (Anlage)"
        paths = [
            make_export(
                cls.tmp.name,
                2025,
                belege=[
                    voucher("B-00020", "Möbel", "10.01.2025", "2025-01-12", 900, asset),
                    voucher(
                        "B-00021",
                        "Regale",
                        "05.02.2025",
                        "2025-02-06",
                        900,
                        "GWG-Sammelposten",
                    ),
                    voucher(
                        "B-00022",
                        "Monitor",
                        "01.03.2025",
                        "2025-03-02",
                        1000,
                        f"Porto, {asset}",
                    ),
                    voucher(
                        "B-00023", "Skulptur", "01.03.2025", "2025-03-02", 2000, asset
                    ),
                    voucher(
                        "B-00024", "Porto", "01.03.2025", "2025-03-02", 10, "Porto"
                    ),
                    voucher(
                        "B-00025",
                        "Kleinregal",
                        "10.01.2025",
                        "2025-01-12",
                        500,
                        "GWG-Sammelposten",
                    ),
                ],
            ),
            make_export(
                cls.tmp.name,
                2026,
                belege=[
                    # deleted without payment: not imported, nothing to link
                    voucher("B-00030", "Drucker", "01.02.2026", "", 700, asset).replace(
                        ";paid;", ";deleted;"
                    ),
                    # the voucher differs from the asset value
                    voucher(
                        "B-00031", "Kamera", "01.03.2026", "2026-03-02", 1100, asset
                    ),
                    voucher(
                        "B-00032",
                        "Großer Stuhl",
                        "01.04.2026",
                        "2026-04-02",
                        1500,
                        "Geringwertige Wirtschaftsgüter (GWG)",
                    ),
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
        cls.pk_run = cls.env["papierkram.import"].create(
            {
                "export_attachment_ids": [(6, 0, attachments.ids)],
                "categories_file": base64.b64encode(MORE_CATEGORIES.encode()),
                "assets_file": base64.b64encode(ASSETS.encode()),
                "splits_file": base64.b64encode(SPLITS.encode()),
                "depreciation_until": date(2026, 12, 31),
                "euer_takeover_date": date(2025, 12, 31),
            }
        )
        cls.pk_run.action_check()
        cls.pk_run.action_import()
        cls.log = str(cls.pk_run.log)

    def asset(self, code):
        asset = self.pk_run.euer_asset_ids.filtered(lambda a: a.code == code)
        self.assertEqual(len(asset), 1, code)
        return asset

    def test_several_assets_on_one_voucher(self):
        self.assertEqual(self.asset("B-00020/1").name, "Tisch")
        self.assertEqual(self.asset("B-00020/2").name, "Lampe")
        self.assertEqual(self.asset("B-00020/2").opening_book_value, 240.0)

    def test_kinds_of_the_register(self):
        """Pool with the releases of the register (and a note on the differing
        Papierkram value), a non-depreciable asset at its cost, the asset of a
        split voucher found behind its postage line."""
        pool = self.asset("B-00021")
        self.assertEqual(pool.asset_kind, "pool")
        self.assertIn("B-00021 Regale: asset pool taken over with 720.00", self.log)
        self.assertEqual(self.asset("B-00025").asset_kind, "pool")
        self.assertNotIn("B-00025 Kleinregal: asset pool", self.log)
        sculpture = self.asset("B-00023")
        self.assertEqual(sculpture.asset_kind, "non_depreciable")
        self.assertEqual(sculpture.opening_book_value, 2000.0)
        self.assertEqual(self.asset("B-00022").opening_book_value, 715.0)

    def test_assets_without_voucher_or_category_are_reported(self):
        self.assertIn("B-09999 Ohne Beleg: voucher or asset category", self.log)
        self.assertIn("B-00024 Porto statt Anlage: voucher or asset", self.log)
        codes = self.pk_run.euer_asset_ids.mapped("code")
        self.assertNotIn("B-09999", codes)
        self.assertNotIn("B-00024", codes)

    def test_purchases_that_cannot_be_linked_or_entered(self):
        """Without an imported voucher the purchase has no journal item; a
        single item of another amount is still the purchase; a low-value
        asset above the limit is not entered and reported."""
        self.assertIn("B-00030 Drucker: no journal item of the purchase", self.log)
        camera = self.asset("B-00031")
        self.assertEqual(camera.move_line_ids.move_id.papierkram_number, "B-00031")
        self.assertIn("B-00032 Großer Stuhl was not entered in the register", self.log)
        self.assertNotIn("B-00032", self.pk_run.euer_asset_ids.mapped("code"))

    def test_rows_with_errors_are_not_imported(self):
        """One invalid row stops the register import of all rows; the errors
        are listed for a correction by hand."""
        notes = []
        entry = {
            "name": "",
            "code": "X-1",
            "group": "other_movable",
            "kind": "depreciable",
            "method": "linear",
            "delivery_date": date(2025, 1, 10),
            "cost": D("600.00"),
            "useful_life_months": 60,
            "accumulated": D("120.00"),
            "book_value": D("480.00"),
            "memo_value": D("1.00"),
        }
        assets = self.pk_run._euer_import_register([entry], notes)
        self.assertFalse(assets)
        self.assertIn("The fixed assets were not taken over", notes[0])
        self.assertIn("The name is missing.", notes[1])

    def test_log_without_notes(self):
        self.pk_run.log = False
        self.pk_run.warning_count = 0
        self.pk_run._euer_write_log(self.pk_run.euer_asset_ids, 1, 0, [])
        self.assertIn("Assets in the register", str(self.pk_run.log))
        self.assertEqual(self.pk_run.warning_count, 0)
