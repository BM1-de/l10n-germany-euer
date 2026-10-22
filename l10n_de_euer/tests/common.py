# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Common helpers of the asset register tests (synthetic data, D6)."""

from odoo import Command

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


class AssetCase(AccountTestInvoicingCommon):
    """Business with SKR03 (or ``chart``) and helpers of the asset tests."""

    chart = "de_skr03"

    @classmethod
    def setUpClass(cls):
        # what the decorators setup_country and setup_chart_template do
        cls.country_code = "DE"
        cls.chart_template = cls.chart
        super().setUpClass()
        cls.company = cls.env.company
        cls.company.l10n_de_euer_enabled = True
        cls.Asset = cls.env["l10n_de_euer.asset"]
        cls.Checker = cls.env["l10n_de_euer.asset.checker"]

    # ------------------------------------------------------------ helpers
    def acc(self, code):
        if self.chart == "de_skr04":
            return self.env.ref(f"account.{self.company.id}_chart_skr04_{int(code)}")
        return self.env.ref(f"account.{self.company.id}_account_{code}")

    def tax(self, xml_id=None):
        xml_id = xml_id or f"tax_vst_19_{self.chart[3:]}"
        tax = self.env.ref(f"account.{self.company.id}_{xml_id}")
        tax.active = True
        return tax

    def period(self, regime, date_from, date_to=False, company=None):
        return self.env["l10n_de_euer.vat.period"].create(
            {
                "company_id": (company or self.company).id,
                "date_from": date_from,
                "date_to": date_to,
                "regime": regime,
            }
        )

    def asset(self, activate=True, **values):
        vals = {
            "name": "Testgerät",
            "company_id": self.company.id,
            "delivery_date": "2024-01-10",
            "cost_net": 1200.0,
            "method": "linear",
            "useful_life_months": 36,
            "aveuer_group": "other_movable",
        }
        vals.update(values)
        record = self.Asset.create(vals)
        if activate:
            record.action_activate()
        return record

    def contributions(self, year, company=None, **kwargs):
        return self.Asset._get_field_contributions(
            company or self.company, year, **kwargs
        )

    def form_values(self, year, contributions=None):
        return self.Asset._get_form_values(self.company, year, contributions)

    def results(self, code, year):
        return [
            result
            for result in self.Checker._run(self.company, year)
            if result["code"] == code
        ]

    def bill(self, lines, on_date="2026-03-15", post=True):
        move = self.env["account.move"].create(
            {
                "move_type": "in_invoice",
                "partner_id": self.partner_a.id,
                "invoice_date": on_date,
                "date": on_date,
                "invoice_line_ids": [
                    Command.create(
                        {
                            "name": name,
                            "account_id": account.id,
                            "price_unit": amount,
                            "quantity": 1,
                            "tax_ids": [Command.set(taxes.ids)],
                        }
                    )
                    for name, account, amount, taxes in lines
                ],
            }
        )
        if post:
            move.action_post()
        return move

    def entry(self, lines, on_date):
        move = self.env["account.move"].create(
            {
                "move_type": "entry",
                "date": on_date,
                "journal_id": self.company_data["default_journal_misc"].id,
                "line_ids": [
                    Command.create(
                        {"account_id": account.id, "balance": amount, "name": "Test"}
                    )
                    for account, amount in lines
                ],
            }
        )
        move.action_post()
        return move

    def from_lines(self, lines, **values):
        wizard = (
            self.env["l10n_de_euer.asset.from.move.line"]
            .with_context(active_model="account.move.line", active_ids=lines.ids)
            .create(values)
        )
        action = wizard.action_create()
        return self.Asset.browse(action.get("res_id") or [])
