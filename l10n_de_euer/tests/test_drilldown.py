# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Drilldown to the receipt, forward and backward (spec 7.6, G-03, GT-08).

Forward: value of the statement → slices of its field and year (grouped by
account, partner, month) → money entry, document, attachments and the
partial reconciliations of the path; values of the asset register → asset →
yearly values → journal items; profit correction → own receipt. Backward:
a journal entry shows its slices with year and Kz. The slices keep the
recipient of a gift (register of gifts, G-08) and the profit correction
they come from. SKR03 and SKR04; synthetic cases (D6).
"""

from odoo import Command
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from ..models.euer_statement_final import SLICE_FIELDS
from .scenario_common import (
    SKR03_ACCOUNTS,
    SKR03_TAXES,
    SKR04_ACCOUNTS,
    SKR04_TAXES,
)
from .statement_common import StatementCommon

KU = "EUER.20.111"
GIFT = "EUER.25.174"
MEALS = "EUER.25.171"
DEPRECIATION = "EUER.25.130"
DEPRECIATION_AVEUER = "AVEUER.40.490"


class DrilldownScenarios(StatementCommon):
    def scenario(self):
        """A paid invoice with a receipt, a gift to a recipient, a camera of
        the asset register and a profit correction with its own receipt."""
        sale = self.sale("2026-03-10", 1000)
        self.env["ir.attachment"].create(
            {
                "name": "Rechnung.pdf",
                "raw": b"%PDF synthetic",
                "res_model": "account.move",
                "res_id": sale.id,
            }
        )
        self.bank("2026-03-20", 1000, matches=[(sale, 1000)])
        recipient = self.env["res.partner"].create({"name": "Recipient"})
        gift = self.invoice("in_invoice", "2026-05-02", [("gifts", 30)])
        gift.l10n_de_euer_gift_recipient_id = recipient
        self.bank("2026-05-05", -30, matches=[(gift, 30)])
        bill = self.purchase("2026-07-15", 2150, account="asset")
        self.bank("2026-07-20", -2150, matches=[(bill, 2150)])
        camera = self.env["l10n_de_euer.asset"].create(
            {
                "name": "Kamera",
                "company_id": self.company.id,
                "delivery_date": "2026-07-15",
                "cost_net": 2150.0,
                "method": "linear",
                "useful_life_months": 84,
                "aveuer_group": "other_movable",
                "move_line_ids": [Command.set(bill.invoice_line_ids.ids)],
            }
        )
        camera.action_activate()
        statement = self.statement()
        receipt = self.env["ir.attachment"].create(
            {"name": "Eigenbeleg.txt", "raw": b"Reise"}
        )
        correction = self.correction(
            statement, "meals", days_8h=1, attachment_ids=[Command.set(receipt.ids)]
        )
        statement.action_compute()
        return statement, sale, gift, recipient, camera, bill, correction, receipt

    def line(self, statement, code):
        return statement.line_ids.filtered(lambda line: line.code == code)

    def slices(self, action):
        return self.env[action["res_model"]].search(action["domain"])

    # ------------------------------------------------------------ forward
    def test_gt08_forward_value_to_receipt(self):
        """GT-08 forward: value → slices (grouped by account, partner, month)
        → money entry, document, attachments and partials."""
        statement, sale, *_rest = self.scenario()
        action = self.line(statement, KU).action_open_slices()
        self.assertEqual(action["res_model"], "l10n_de_euer.cash.slice")
        slices = self.slices(action)
        self.assertEqual(slices.mapped("field_code"), [KU])
        self.assertEqual(set(slices.mapped("attribution_year")), {2026})
        self.assertEqual(sum(slices.mapped("amount")), 1000.0)
        # grouping by account, partner and month
        search = self.env.ref("l10n_de_euer.l10n_de_euer_cash_slice_view_search")
        for name in ("group_account", "group_partner", "group_month"):
            self.assertIn(f'name="{name}"', search.arch)
        groups = self.env["l10n_de_euer.cash.slice"]._read_group(
            action["domain"], ["date:month"], ["amount:sum"]
        )
        self.assertEqual(len(groups), 1)
        one = slices
        self.assertEqual(one.action_open_origin()["res_id"], sale.id)
        money = one.action_open_source()
        self.assertEqual(money["res_model"], "account.move")
        self.assertEqual(money["res_id"], one.source_move_id.id)
        # attachments of the document and the money entry
        self.assertEqual(one.attachment_count, 1)
        attachments = self.slices(one.action_open_attachments())
        self.assertEqual(attachments.mapped("name"), ["Rechnung.pdf"])
        # the path of the money as partial reconciliations
        self.assertTrue(one.path)
        self.assertEqual(one.partial_ids.ids, one.path)
        partials = self.slices(one.action_open_path())
        self.assertEqual(partials, one.partial_ids)
        self.assertIn(
            sale.line_ids.filtered(lambda line: line.account_id == self.receivable),
            partials.debit_move_id | partials.credit_move_id,
        )

    def test_gt08_forward_asset_register(self):
        """GT-08 forward: value of the asset register → asset → yearly
        values → journal items of the purchase (spec 7.6 no. 3)."""
        statement, _sale, _gift, _recipient, camera, bill, *_rest = self.scenario()
        line = self.line(statement, DEPRECIATION)
        self.assertTrue(line.has_register_source)
        self.assertFalse(self.line(statement, KU).has_register_source)
        years = self.slices(line.action_open_assets())
        self.assertEqual(years.asset_id, camera)
        self.assertEqual(years.year, 2026)
        self.assertEqual(years.depreciation, 153.57)
        items = self.slices(years.action_open_move_lines())
        self.assertEqual(items, bill.invoice_line_ids)
        self.assertEqual(
            self.slices(camera.action_open_move_lines()), bill.invoice_line_ids
        )
        # from the slice of the register as well
        aveuer = self.line(statement, DEPRECIATION_AVEUER)
        one = self.slices(aveuer.action_open_slices())
        self.assertTrue(one)
        self.assertEqual(set(one.mapped("rule")), {"asset"})
        self.assertEqual(self.slices(one[:1].action_open_assets()).asset_id, camera)

    def test_gt08_forward_asset_pool(self):
        """GT-08 forward: the release of an asset pool leads to the pool."""
        pooled = self.env["l10n_de_euer.asset"].create(
            {
                "name": "Stativ",
                "company_id": self.company.id,
                "asset_kind": "pool",
                "method": "none",
                "delivery_date": "2025-06-01",
                "cost_net": 500.0,
                "aveuer_group": "other_movable",
            }
        )
        pooled.action_activate()
        statement = self.statement()
        statement.action_compute()
        actions = [
            line.action_open_assets()
            for line in statement.line_ids
            if line.has_register_source
        ]
        pools = [
            action
            for action in actions
            if action["res_model"] == "l10n_de_euer.asset.pool"
        ]
        self.assertTrue(pools)
        self.assertEqual(self.slices(pools[0]), pooled.pool_id)

    def test_gt08_forward_profit_correction(self):
        """GT-08 forward: value of a profit correction → its slice → the
        correction → its own receipt (spec 7.6 no. 4)."""
        statement, *_rest, correction, receipt = self.scenario()
        one = self.slices(self.line(statement, MEALS).action_open_slices())
        self.assertEqual(one.adjustment_id, correction)
        self.assertEqual(one.rule, "adjustment")
        opened = one.action_open_adjustment()
        self.assertEqual(
            (opened["res_model"], opened["res_id"]),
            ("l10n_de_euer.adjustment", correction.id),
        )
        self.assertEqual(self.slices(correction.action_open_receipts()), receipt)
        # the meal allowance and its contribution (Kz 123)
        slices = self.slices(correction.action_open_slices())
        self.assertIn(one, slices)
        self.assertEqual(set(slices.mapped("field_code")), {MEALS, "EUER.29.123"})

    # ------------------------------------------------------------ backward
    def test_gt08_backward_entry_to_slices(self):
        """GT-08 backward: the smart button EÜR of a journal entry shows its
        slices with year and Kz."""
        statement, sale, *_rest = self.scenario()
        self.assertEqual(sale.l10n_de_euer_slice_count, 1)
        action = sale.action_l10n_de_euer_slices()
        slices = self.slices(action)
        self.assertEqual(
            [(one.attribution_year, one.kz) for one in slices], [(2026, "111")]
        )
        self.assertEqual(slices.run_id, statement.run_id)
        money = slices.source_move_id
        self.assertEqual(self.slices(money.action_l10n_de_euer_slices()), slices)

    # ------------------------------------------------------------ G-08
    def test_gift_recipient_on_slices(self):
        """The slices of a gift keep its recipient and the day it is handed
        over; give-aways are marked (register of gifts per recipient, G-08).
        The fields belong to the payload of the finalisation."""
        statement, _sale, gift, recipient, *_rest = self.scenario()
        bulk = self.invoice("in_invoice", "2026-06-02", [("gifts", 3, (), 2)])
        bulk.l10n_de_euer_gift_bulk = True
        self.bank("2026-06-05", -6, matches=[(bulk, 6)])
        statement.action_compute()
        slices = self.slices(self.line(statement, GIFT).action_open_slices())
        named = slices.filtered(lambda one: one.origin_move_id == gift)
        self.assertEqual(named.gift_recipient_id, recipient)
        self.assertEqual(str(named.gift_date), "2026-05-02")
        self.assertFalse(named.gift_bulk)
        give_aways = slices.filtered(lambda one: one.origin_move_id == bulk)
        self.assertTrue(give_aways.gift_bulk)
        self.assertFalse(give_aways.gift_recipient_id)
        for name in ("gift_recipient_id", "gift_date", "gift_bulk", "adjustment_id"):
            self.assertIn(name, SLICE_FIELDS)


@tagged("post_install_l10n", "post_install", "-at_install")
class TestDrilldownSkr03(DrilldownScenarios, AccountTestInvoicingCommon):
    allow_inherited_tests_method = True
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_statement()


@tagged("post_install_l10n", "post_install", "-at_install")
class TestDrilldownSkr04(DrilldownScenarios, AccountTestInvoicingCommon):
    allow_inherited_tests_method = True
    CHART = "de_skr04"
    ACCOUNTS = SKR04_ACCOUNTS
    TAXES = SKR04_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr04")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_statement()
