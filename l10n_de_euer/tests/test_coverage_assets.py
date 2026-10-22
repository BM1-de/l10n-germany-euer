# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Rules of the asset register without a test before the release (E5, O-06).

Kind and AVEÜR group of an asset, the formula help of § 15a UStG, the
takeover values, the disposal, the actions and the rights of a takeover;
the parsers and the plausibility rules of the takeover of an asset register
by CSV (spec 6.6, R-48). Synthetic data (D6).
"""

import base64
import datetime
from contextlib import contextmanager
from decimal import Decimal

from odoo import Command
from odoo.exceptions import UserError, ValidationError
from odoo.tests import BaseCase, new_test_user, tagged

from ..models.engine import depreciation as dep
from ..models.euer_check import INTERNAL as CHECK_INTERNAL
from ..wizards import asset_import
from .common import AssetCase

HEADER = (
    "Bezeichnung;Gruppe;Art;Lieferdatum;AK;ND (Monate);Methode;"
    "kumulierte AfA bis Stichtag;Restbuchwert;Sammelposten-Jahrgang;"
    "Sammelposten-Restbetrag;Sonder-AfA § 7g;Jahr Sonder-AfA § 7g\n"
)


@tagged("post_install", "-at_install")
class TestAssetImportParsers(BaseCase):
    """Cells of the CSV file (spec 6.6): German and ISO notation, every other
    text is an error of the row, never a traceback."""

    def test_numbers(self):
        self.assertEqual(asset_import.parse_number("12,5 %"), Decimal("12.5"))
        self.assertEqual(asset_import.parse_number("2.400"), Decimal("2400"))
        self.assertEqual(asset_import.parse_number("1.234,56 €"), Decimal("1234.56"))
        self.assertIsNone(asset_import.parse_number("  "))
        with self.assertRaises(ValueError):
            asset_import.parse_number("zwölf")

    def test_amounts_have_cents_at_most(self):
        self.assertEqual(asset_import.parse_amount("1.000,10"), Decimal("1000.10"))
        self.assertIsNone(asset_import.parse_amount(""))
        with self.assertRaises(ValueError):
            asset_import.parse_amount("10,005")

    def test_rates_in_percent_or_as_fraction(self):
        self.assertEqual(asset_import.parse_rate("25"), Decimal("0.25"))
        self.assertEqual(asset_import.parse_rate("25 %"), Decimal("0.25"))
        self.assertEqual(asset_import.parse_rate("0,25"), Decimal("0.25"))
        self.assertIsNone(asset_import.parse_rate(""))

    def test_dates(self):
        day = datetime.date(2024, 1, 31)
        self.assertEqual(asset_import.parse_date("2024-01-31"), day)
        self.assertEqual(asset_import.parse_date("31.01.2024"), day)
        self.assertIsNone(asset_import.parse_date(" "))
        with self.assertRaises(ValueError):
            asset_import.parse_date("31/01/2024")

    def test_choices_by_key_label_or_german_alias(self):
        self.assertEqual(asset_import.parse_choice("asset_kind", "gwg"), "gwg")
        self.assertEqual(
            asset_import.parse_choice("asset_kind", "Abnutzbar"), "depreciable"
        )
        self.assertIsNone(asset_import.parse_choice("asset_kind", ""))
        with self.assertRaises(ValueError):
            asset_import.parse_choice("asset_kind", "Maschine")


class AssetCoverageCase(AssetCase):
    @contextmanager
    def raises(self, exception, text):
        """``assertRaises`` with a part of the message."""
        with self.assertRaises(exception) as catcher:
            yield catcher
        self.assertIn(text, str(catcher.exception))

    def import_wizard(self, content, opening_date="2024-12-31"):
        return self.env["l10n_de_euer.asset.import"].create(
            {
                "company_id": self.company.id,
                "opening_date": opening_date,
                "file": base64.b64encode(content.encode()),
                "filename": "anlagen.csv",
            }
        )

    def parsed(self, content, opening_date="2024-12-31"):
        wizard = self.import_wizard(content, opening_date)
        wizard.action_parse()
        return wizard, {line.sequence: line for line in wizard.line_ids}


@tagged("post_install_l10n", "post_install", "-at_install")
class TestAssetImportRows(AssetCoverageCase):
    """Plausibility of the rows of a takeover (spec 6.6)."""

    def test_row_errors(self):
        """Every error of a row is listed in the preview."""
        _wizard, lines = self.parsed(
            HEADER
            + ";Andere;abnutzbar;05.01.2024;1000;60;linear;200;800;;;;\n"
            + "Datum;Andere;abnutzbar;31/01/2024;1000;60;linear;200;800;;;;\n"
            + "Spät;Andere;abnutzbar;05.01.2025;1000;60;linear;0;1000;;;;\n"
            + "Jahr;Andere;abnutzbar;05.01.2024;1000;60;linear;200;800;;;;2024\n"
            + "Pool;Andere;Sammelposten;;1000;;;;;2022;500;;\n"
            + "Pool ohne Jahr;Andere;Sammelposten;;1000;;;;;;400;;\n"
            + "Gut;Andere;abnutzbar;05.01.2024;1000;60;linear;200;800;;;;\n"
        )
        self.assertIn("The name is missing.", lines[2].error)
        self.assertIn("Invalid delivery_date: 31/01/2024", lines[3].error)
        self.assertIn("The delivery date is missing.", lines[3].error)
        self.assertIn("Delivered after the takeover date.", lines[4].error)
        self.assertIn("amount of the special depreciation is missing", lines[5].error)
        self.assertIn("Remaining pool 500.00 differs", lines[6].error)
        self.assertIn("(400.00)", lines[6].error)
        self.assertIn("The year of the asset pool is missing.", lines[7].error)
        self.assertFalse(lines[8].error)
        self.assertFalse(lines[8].warning)

    def test_memo_value_up_to_the_book_value(self):
        _wizard, lines = self.parsed(
            "Bezeichnung;Gruppe;Art;Lieferdatum;AK;ND;Methode;kumulierte AfA;"
            "Restbuchwert;Erinnerungswert\n"
            "Zu hoch;Andere;abnutzbar;05.01.2024;1000;60;linear;200;800;900\n"
            "Passt;Andere;abnutzbar;05.01.2024;1000;60;linear;200;800;1\n"
        )
        self.assertIn("The memo value exceeds the book value.", lines[2].error)
        self.assertFalse(lines[3].error)

    def test_register_rules_in_the_preview(self):
        """A row the register refuses (a low-value asset is movable, § 6 (2)
        EStG) shows the error of the register in the preview."""
        _wizard, lines = self.parsed(
            HEADER + "Hütte;Gebäude;GWG;05.01.2024;700;;;700;0;;;;\n"
        )
        self.assertIn("movable assets", lines[2].error)

    def test_import_needs_a_clean_preview(self):
        wizard = self.import_wizard(HEADER + ";Andere;abnutzbar;;1000;;;;;;;;\n")
        with self.raises(UserError, "Read the file first."):
            wizard.action_import()
        wizard.action_parse()
        with self.raises(UserError, "Correct the 1 rows with errors."):
            wizard.action_import()

    def test_defaults_of_the_takeover(self):
        """The business of the user and 31 December of the last year."""
        wizard = self.env["l10n_de_euer.asset.import"].create(
            {"file": base64.b64encode(HEADER.encode())}
        )
        self.assertEqual(wizard.company_id, self.company)
        today = datetime.date.today()
        self.assertEqual(wizard.opening_date, datetime.date(today.year - 1, 12, 31))

    def test_empty_file(self):
        with self.raises(UserError, "The file is empty."):
            self.import_wizard(";;\n\n").action_parse()

    def test_filed_totals_are_updated_by_a_second_import(self):
        """The filed AVEÜR per group (C-61) is kept once per business, year
        and group; a later import of the same year updates it."""
        Total = self.env["l10n_de_euer.asset.opening.total"]
        for number, value in ((1, 800.0), (2, 750.0)):
            wizard = self.import_wizard(
                HEADER
                + f"Gerät {number};Andere;abnutzbar;05.01.2024;1000;60;linear;"
                + "200;800;;;;\n"
            )
            wizard.total_ids = [
                Command.create(
                    {"aveuer_group": "other_movable", "book_value_end": value}
                )
            ]
            wizard.action_parse()
            wizard.action_import()
        totals = Total.search(
            [("company_id", "=", self.company.id), ("year", "=", 2024)]
        )
        self.assertEqual(len(totals), 1)
        self.assertEqual(totals.book_value_end, 750.0)


@tagged("post_install_l10n", "post_install", "-at_install")
class TestAssetCoverageRules(AssetCoverageCase):
    """Kind, group, takeover and disposal of an asset (spec 6.2, 6.3)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(
            cls.env,
            login="e5_coverage_asset_user",
            groups="account.group_account_user",
            company_id=cls.company.id,
            company_ids=[Command.set(cls.company.ids)],
        )

    def test_kind_and_group_agree(self):
        cases = (
            (
                {"asset_kind": "current_4_3_4", "method": "none"},
                "Current assets belong to their own group.",
            ),
            (
                {"aveuer_group": "current"},
                "The group of current assets needs that kind.",
            ),
            (
                {"aveuer_group": "land"},
                "Land and financial assets are not depreciable.",
            ),
            (
                {"asset_kind": "gwg", "aveuer_group": "building", "cost_net": 700.0},
                "Low-value assets and asset pools are movable assets",
            ),
            ({"method": "none"}, "Choose a depreciation method."),
        )
        for values, text in cases:
            with self.subTest(values=values), self.raises(ValidationError, text):
                self.asset(activate=False, **values)

    def test_sec15a_help_needs_a_change_within_the_period(self):
        """§ 15a UStG: a change of the VAT treatment within five years from
        the delivery gives the corrections per year; a later change or none
        gives no help. A period with the same treatment is no change."""
        self.period("ku", "2020-01-01", "2024-12-31")
        self.period("ku", "2025-01-01", "2025-12-31")
        asset = self.asset(input_tax=228.0, delivery_date="2024-01-10")
        self.assertFalse(asset.sec15a_help)
        self.period("regular", "2026-01-01")
        asset.invalidate_recordset(["sec15a_help"])
        self.assertEqual(asset._get_regime_change_date(), datetime.date(2026, 1, 1))
        self.assertIn("2026", asset._get_sec15a_text())
        self.assertFalse(asset._get_sec15a_text(datetime.date(2029, 2, 1)))

    def test_values_without_a_date(self):
        draft = self.Asset.new({"name": "Neu", "company_id": self.company.id})
        self.assertEqual(draft._get_max_degressive_rate(), dep.ZERO)
        asset = self.asset()
        self.assertIsNone(asset._get_disposal_effective_date())
        self.assertEqual(asset._get_sec7g_issues(), [])
        self.assertEqual(self.asset(activate=False)._get_form_values_of_year(2024), {})

    def test_engine_message_without_an_asset(self):
        """A message of the depreciation rules names the asset if there is
        one."""
        error = dep.DepreciationError("no_end")
        self.assertEqual(self.Asset._engine_message(error), "The schedule does not end")
        asset = self.asset()
        self.assertIn(asset.display_name, asset._engine_message(error))

    def test_schedule_errors_become_validation_errors(self):
        asset = self.asset()
        self.env.cr.execute(
            "UPDATE l10n_de_euer_asset SET useful_life_months = 0 WHERE id = %s",
            [asset.id],
        )
        asset.invalidate_recordset()
        with self.assertRaises(ValidationError):
            asset._get_schedule()

    def test_takeover_memo_value_up_to_the_book_value(self):
        with self.raises(ValidationError, "memo value exceeds the book value"):
            self.asset(
                origin="legacy_import",
                delivery_date="2020-07-01",
                cost_net=799.0,
                useful_life_months=60,
                memo_value=300.0,
                opening_date="2023-12-31",
                opening_book_value=239.70,
                opening_accumulated=559.30,
            )

    def test_disposal_needs_date_and_kind(self):
        asset = self.asset()
        with self.raises(ValidationError, "enter the date and kind of the disposal"):
            asset.write({"state": "disposed"})

    def test_actions_by_state(self):
        asset = self.asset()
        with self.raises(UserError, "is already in the register"):
            asset.action_activate()
        draft = self.asset(activate=False)
        with self.raises(UserError, "Only assets in the register can be disposed"):
            draft.action_dispose()
        action = asset.action_open_years()
        self.assertEqual(action["domain"], [("asset_id", "=", asset.id)])
        self.assertEqual(action["context"], {"default_asset_id": asset.id})

    def test_journal_items_of_the_business_only(self):
        """An asset links journal items of its own business only (O-08)."""
        data = self.setup_other_company(name="Andere Firma (synthetisch)")
        move = (
            self.env["account.move"]
            .with_company(data["company"])
            .create(
                {
                    "journal_id": data["default_journal_misc"].id,
                    "date": "2024-01-10",
                    "line_ids": [
                        Command.create(
                            {
                                "account_id": data["default_account_expense"].id,
                                "debit": 100.0,
                            }
                        ),
                        Command.create(
                            {
                                "account_id": data["default_account_revenue"].id,
                                "credit": 100.0,
                            }
                        ),
                    ],
                }
            )
        )
        asset = self.asset()
        with self.raises(ValidationError, "journal items of another business"):
            asset.sudo().move_line_ids = move.line_ids[:1]

    def test_takeover_is_changed_by_a_manager(self):
        """Spec 7.10: an accounting user may change an asset of the register,
        but not make it a takeover nor change a takeover."""
        taken = self.asset(
            origin="legacy_import",
            delivery_date="2020-07-01",
            cost_net=799.0,
            useful_life_months=60,
            memo_value=1.0,
            opening_date="2023-12-31",
            opening_book_value=239.70,
            opening_accumulated=559.30,
        )
        own = self.asset(name="Eigenes Gerät")
        as_user = self.Asset.with_user(self.user)
        with self.raises(UserError, "Only an accounting manager"):
            as_user.browse(taken.id).write({"opening_accumulated": 559.31})
        with self.raises(UserError, "Only an accounting manager"):
            as_user.browse(own.id).write({"origin": "legacy_import"})
        as_user.browse(own.id).write({"name": "Umbenannt"})
        self.assertEqual(own.name, "Umbenannt")


@tagged("post_install_l10n", "post_install", "-at_install")
class TestAssetYearProtection(AssetCoverageCase):
    """The yearly values belong to the register (spec 6.2, 7.7)."""

    def test_yearly_values_are_calculated_by_the_register(self):
        asset = self.asset()
        asset.action_compute_years()
        year = asset.year_ids.sorted("year")[:1]
        Year = self.env["l10n_de_euer.asset.year"]
        with self.raises(UserError, "Yearly values are calculated by the register."):
            Year.create({"asset_id": asset.id, "year": 2030})
        with self.raises(UserError, "Yearly values are calculated by the register."):
            year.sudo().unlink()
        year._write_internal(
            {"private_use_decision": "rejected", "private_use_reason": "Ausgleich"}
        )
        with self.raises(UserError, "The reason of a decided use withdrawal is fixed."):
            year.private_use_reason = "Anders"
        year._write_internal({"locked": True})
        with self.raises(UserError, "The year is locked."):
            year.action_confirm_private_use()


@tagged("post_install_l10n", "post_install", "-at_install")
class TestDocumentInformation(AssetCoverageCase):
    """EÜR information of a journal entry (spec 5.6, R-25)."""

    def test_entry_without_vat_treatment(self):
        """Only documents get a VAT treatment from the status periods; a
        journal entry has none. Its information opens the entry."""
        self.period("ku", "2020-01-01")
        bill = self.bill(
            [("Kamera", self.acc("0490"), 1190.0, self.env["account.tax"])], post=False
        )
        bank = self.company_data["default_journal_bank"].default_account_id
        entry = self.entry([(self.acc("4930"), 100.0), (bank, -100.0)], "2026-03-15")
        Info = self.env["l10n_de_euer.move.info"]
        info = Info._get_or_create(entry)
        self.assertFalse(info.vat_regime)
        self.assertEqual(info.action_open_move()["res_id"], entry.id)
        self.assertEqual(Info._get_or_create(bill).vat_regime, "ku")

    def test_vat_rate_finding_opens_the_document_information(self):
        """C-32/C-33 (VAT rate of a supply missing) open the EÜR information
        of the invoice of the result."""
        bill = self.bill(
            [("Kamera", self.acc("0490"), 1190.0, self.env["account.tax"])]
        )
        check = (
            self.env["l10n_de_euer.check"]
            .sudo()
            .with_context(**{CHECK_INTERNAL: True})
            .create(
                {
                    "company_id": self.company.id,
                    "year": 2026,
                    "code": "C-33",
                    "severity": "blocker",
                    "message": "VAT rate missing",
                    "res_model": "account.move",
                    "res_ids": [bill.id],
                }
            )
        )
        action = check.action_resolve()
        self.assertEqual(action["res_model"], "l10n_de_euer.move.info")
        info = self.env["l10n_de_euer.move.info"].browse(action["res_id"])
        self.assertEqual(info.move_id, bill)


@tagged("post_install_l10n", "post_install", "-at_install")
class TestAssetRegisterOutputs(AssetCoverageCase):
    """Outputs of the register per year (spec 6.4)."""

    def test_details_only_for_assets_present_in_the_year(self):
        """The voluntary details of Anlage AVEÜR list an asset in the years
        it is in the register, not after its disposal."""
        asset = self.asset()
        asset.write(
            {
                "disposal_date": "2024-06-30",
                "disposal_kind": "scrap",
                "state": "disposed",
            }
        )
        details = self.Asset._get_aveuer_details(self.company, 2024)
        self.assertEqual([row["asset"] for row in details], [asset])
        self.assertEqual(self.Asset._get_aveuer_details(self.company, 2025), [])

    def test_electric_vehicle_key_from_2025(self):
        """Kz 407 (electric vehicle in the business) exists from the form
        2025 on; for 2024 there is no key field."""
        self.asset(aveuer_group="vehicle", delivery_date="2024-01-10")
        self.assertEqual(self.Asset._get_key_values(self.company, 2024), {})

    def test_contribution_error_names_the_asset(self):
        """Data changed outside the register (land with depreciation, which
        Anlage AVEÜR has no column for) stops the contributions with the
        name of the asset."""
        land = self.asset(
            name="Grundstück",
            asset_kind="non_depreciable",
            method="none",
            aveuer_group="land",
            cost_net=50000.0,
            useful_life_months=0,
        )
        self.env.cr.execute(
            "UPDATE l10n_de_euer_asset SET asset_kind = 'depreciable', "
            "method = 'linear', useful_life_months = 120 WHERE id = %s",
            [land.id],
        )
        land.invalidate_recordset()
        with self.raises(UserError, "Grundstück"):
            self.contributions(2024)


@tagged("post_install_l10n", "post_install", "-at_install")
class TestAssetCheckerRules(AssetCoverageCase):
    """Checks of the register (spec 7.8)."""

    def test_c42_only_assets_of_the_correction(self):
        """C-42 (§ 15a UStG) on the change to standard taxation in 2026: only
        an asset with input tax, delivered before the change and still in
        the register."""
        self.period("ku", "2020-01-01", "2025-12-31")
        self.period("regular", "2026-01-01")
        camera = self.asset(name="Kamera", input_tax=228.0)
        self.asset(name="Ohne Vorsteuer")
        self.asset(name="Später", input_tax=228.0, delivery_date="2026-03-01")
        sold = self.asset(name="Verkauft", input_tax=228.0)
        sold.write(
            {
                "disposal_date": "2025-06-30",
                "disposal_kind": "scrap",
                "state": "disposed",
            }
        )
        results = self.results("C-42", 2026)
        self.assertEqual([result["res_ids"] for result in results], [camera.ids])

    def test_c62_names_the_asset_of_a_contribution_error(self):
        """Data changed outside the register (land with depreciation) is
        reported by C-62 with the asset instead of stopping the checks."""
        land = self.asset(
            name="Grundstück",
            asset_kind="non_depreciable",
            method="none",
            aveuer_group="land",
            cost_net=50000.0,
            useful_life_months=0,
        )
        self.env.cr.execute(
            "UPDATE l10n_de_euer_asset SET asset_kind = 'depreciable', "
            "method = 'linear', useful_life_months = 120 WHERE id = %s",
            [land.id],
        )
        land.invalidate_recordset()
        results = self.results("C-62", 2024)
        self.assertTrue(results)
        self.assertIn(land.id, results[-1]["res_ids"])

    def test_years_without_form_data(self):
        """Before the first form year (2024) the register checks that need a
        form (C-62, C-68) have nothing to compare."""
        self.asset(delivery_date="2020-01-10")
        self.assertFalse(self.results("C-62", 2020))
        self.assertFalse(self.results("C-68", 2020))
