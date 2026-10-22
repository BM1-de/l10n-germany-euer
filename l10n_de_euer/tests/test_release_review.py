# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Review of the outputs before the release (stage E5, spec 7.5, 7.12).

The fill-in aid names the column or group of a field whose official label
alone is ambiguous, shows a key value once with its meaning and keeps the
notes short (the text above the tables explains them); the PDF in the
DIN 5008 layout fills the information block; numbers of the rule
parameters are grouped in the language of the user. Synthetic data (D6).
"""

from lxml import etree, html

from odoo import Command
from odoo.tests import TransactionCase, tagged
from odoo.tools.misc import formatLang

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from .scenario_common import SKR03_ACCOUNTS, SKR03_TAXES
from .test_statement_output import DEPRECIATION, GIFT, INCOME, KU, OutputScenarios

GIFT_NON_DEDUCTIBLE = "EUER.25.164"
#: integer fields with "year" in their name that hold no year
NOT_YEARS = {"months_in_year", "relative_year"}
INCOME_TYPE = "EUER.15.103"


@tagged("post_install_l10n", "post_install", "-at_install")
class TestReleaseOutputs(OutputScenarios, AccountTestInvoicingCommon):
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_statement()

    def rows(self, statement):
        return {
            row["code"]: row
            for section in statement._get_fill_aid_sections()
            for row in section["rows"]
        }

    def test_fill_aid_short_names(self):
        """The short name follows the official label only where the label is
        shared by another field of the form (Geschenke: Kz 164 and 174) and
        on Anlage AVEÜR, whose labels repeat under each group."""
        statement = self.scenario()["statement"]
        rows = self.rows(statement)
        field = self.env["l10n_de_euer.field"].search(
            [("code", "=", GIFT), ("year", "=", 2026)]
        )
        other = self.env["l10n_de_euer.field"].search(
            [("code", "=", GIFT_NON_DEDUCTIBLE), ("year", "=", 2026)]
        )
        self.assertEqual(field.official_label, other.official_label)
        self.assertEqual(rows[GIFT]["short_name"], field.name)
        # an unambiguous label stands alone
        self.assertEqual(rows[KU]["short_name"], "")
        aveuer = [row for row in rows.values() if row["code"].startswith("AVEUER.40.")]
        self.assertTrue(aveuer)
        self.assertTrue(all(row["short_name"] for row in aveuer))
        # the short name is never a part of the label
        for row in rows.values():
            if row["short_name"]:
                self.assertNotIn(row["short_name"].casefold(), row["label"].casefold())
        document = html.fromstring(
            self.env["ir.actions.report"]._render_qweb_html(
                "l10n_de_euer.report_fill_aid", statement.ids
            )[0]
        )
        names = document.xpath(
            "//th[@lang='de']/span[@class='o_l10n_de_euer_short_name']"
        )
        self.assertIn(field.name, [name.text_content().strip() for name in names])
        self.assertTrue(all(name.get("lang") == "en" for name in names))

    def test_fill_aid_key_meaning_and_notes(self):
        """A meaning that starts with its key is shown once
        (``2 Trade or Business``, not ``2 (2 Trade or Business)``); sums
        and transfers carry a short note, explained above the tables."""
        self.company.l10n_de_euer_income_type = "2"
        statement = self.scenario()["statement"]
        rows = self.rows(statement)
        self.assertTrue(rows[INCOME_TYPE]["meaning_only"])
        self.assertEqual(rows[INCOME]["note"], "Control value")
        self.assertEqual(rows[DEPRECIATION]["note"], "Transferred value")
        document = html.fromstring(
            self.env["ir.actions.report"]._render_qweb_html(
                "l10n_de_euer.report_fill_aid", statement.ids
            )[0]
        )
        text = " ".join(document.text_content().split())
        self.assertIn(rows[INCOME_TYPE]["meaning"], text)
        self.assertNotIn(f"2 ({rows[INCOME_TYPE]['meaning']})", text)
        self.assertIn("Transfers from another line or form are marked", text)
        # the tables keep their columns over all sections (fixed layout)
        tables = document.xpath("//table[contains(@class, 'o_l10n_de_euer_fill_aid')]")
        self.assertTrue(tables)
        for table in tables:
            self.assertIn("o_l10n_de_euer_fixed", table.get("class"))

    def test_pdf_din5008_information_block(self):
        """The PDF in the DIN 5008 layout shows year, version, state and the
        date of creation in the information block next to the address."""
        layout = self.env.ref("l10n_din5008.external_layout_din5008")
        self.company.external_report_layout_id = layout
        statement = self.scenario()["statement"]
        for report in ("action_report_fill_aid", "action_report_procedure"):
            content, _kind = self.env["ir.actions.report"]._render_qweb_pdf(
                f"l10n_de_euer.{report}", statement.ids
            )
            document = html.fromstring(content)
            blocks = document.xpath("//div[@class='information_block']")
            self.assertEqual(len(blocks), 1, report)
            values = {
                row.xpath("./td")[0].text_content().strip(): row.xpath("./td")[1]
                .text_content()
                .strip()
                for row in blocks[0].xpath(".//tr")
            }
            self.assertEqual(values["Year"], "2026")
            self.assertEqual(values["Version"], str(statement.version))
            self.assertTrue(values["State"])
            self.assertTrue(values["Created on"])
            self.assertEqual(blocks[0].xpath(".//table")[0].get("role"), "presentation")

    def test_correction_fields_in_user_language(self):
        """The form fields of a profit correction show their amounts in the
        language of the user (14,00 in German), not with a decimal point."""
        correction = self.scenario()["correction"]
        self.env["res.lang"]._activate_lang("de_DE")
        amount = correction.field_amounts and next(
            iter(correction._get_amounts().values())
        )
        self.assertTrue(amount)
        english = correction.with_context(lang="en_US").field_text
        german = correction.with_context(lang="de_DE").field_text
        self.assertIn(f"{amount:.2f}", english)
        self.assertIn(f"{amount:.2f}".replace(".", ","), german)

    def test_chatter_amounts_in_user_language(self):
        """The chatter of a statement shows the profit in the language of the
        user who computed it (German: decimal comma)."""
        statement = self.scenario()["statement"]
        self.env["res.lang"]._activate_lang("de_DE")
        german = statement.with_context(lang="de_DE")
        german.action_compute()
        expected = formatLang(german.env, statement.profit, digits=2)
        self.assertIn(",", expected)
        bodies = statement.message_ids.mapped("body")
        self.assertTrue(any(expected in str(body) for body in bodies))

    def test_account_code_column(self):
        """Odoo shows the code in the name of an account only to users with
        read-only accounting (an administrator of Odoo 19 has only the
        manager group); the account review and the account assignment show
        the code of the business in a column of its own (E5 review)."""
        self.scenario()
        manager = self.env["res.users"].create(
            {
                "name": "EÜR Manager",
                "login": "euer_release_manager",
                "company_id": self.company.id,
                "company_ids": [Command.set(self.company.ids)],
                "group_ids": [
                    Command.set(
                        [
                            self.env.ref("base.group_user").id,
                            self.env.ref("account.group_account_manager").id,
                        ]
                    )
                ],
            }
        )
        self.assertFalse(manager.has_group("account.group_account_readonly"))
        env = self.env(user=manager, context={"lang": "en_US"})
        self.assertEqual(env.company, self.company)
        review = env["l10n_de_euer.account.review"].create(
            {"company_id": self.company.id, "year": 2026}
        )
        review.action_review()
        self.assertTrue(review.line_ids)
        for line in review.line_ids:
            self.assertTrue(line.account_code)
            self.assertEqual(
                line.account_code, line.account_id.with_company(self.company).code
            )
            self.assertNotIn(line.account_code, line.account_id.display_name)
        assignments = env["l10n_de_euer.account.assignment"].search(
            [("company_id", "=", self.company.root_id.id)], limit=5
        )
        self.assertTrue(assignments)
        for assignment in assignments:
            self.assertEqual(
                assignment.account_code,
                assignment.account_id.with_company(self.company).code,
            )
        # without a business the code of the current company
        line = review.line_ids[:1]
        self.assertEqual(
            env["l10n_de_euer.account.assignment"]
            .new({"account_id": line.account_id.id, "company_id": False})
            .account_code,
            line.account_code,
        )

    def test_parameter_numbers_grouped(self):
        """Rule parameters show thousands separators in the language of the
        user (100.000 in German, 100,000 in English), decimals without
        trailing zeros."""
        statement = self.scenario()["statement"]
        self.env["res.lang"]._activate_lang("de_DE")
        german = statement.with_context(lang="de_DE")
        english = statement.with_context(lang="en_US")
        self.assertEqual(german._number_text(100000), "100.000")
        self.assertEqual(german._number_text(4500), "4.500")
        self.assertEqual(german._number_text(0.38), "0,38")
        self.assertEqual(german._number_text(1234567.5), "1.234.567,5")
        self.assertEqual(german._number_text(-2050), "-2.050")
        self.assertEqual(german._number_text(250), "250")
        self.assertEqual(english._number_text(100000), "100,000")
        self.assertEqual(english._number_text(0.7), "0.7")
        values = {
            row["code"]: row["value"]
            for row in german._get_parameter_rows()
            if row["code"] == "ku_limit_cur"
        }
        self.assertEqual(values["ku_limit_cur"], "100.000")


@tagged("post_install", "-at_install")
class TestReleaseViews(TransactionCase):
    """Views of the module (stage E5, review of the user interface)."""

    def test_years_without_thousands_separator(self):
        """Odoo 19 reads ``enable_formatting`` (no longer ``format``): a year
        such as 2026 must not be shown as 2.026. Every visible integer field
        with a year in a list or form view of the module has the option."""
        views = self.env["ir.ui.view"].search(
            [("model", "like", "l10n_de_euer.%"), ("type", "in", ("list", "form"))]
        ) | self.env.ref("l10n_de_euer.view_move_form_l10n_de_euer")
        checked = []

        def walk(node, model, view):
            for child in node:
                if not isinstance(child.tag, str):
                    continue
                if child.tag != "field":
                    walk(child, model, view)
                    continue
                name = child.get("name")
                options = child.get("options") or ""
                self.assertNotIn("'format'", options, f"{view.xml_id}: {name}")
                field = model._fields.get(name)
                if (
                    field
                    and field.type == "integer"
                    and "year" in name
                    and name not in NOT_YEARS
                    and child.get("invisible") != "1"
                ):
                    checked.append(name)
                    self.assertIn(
                        "'enable_formatting': false", options, f"{view.xml_id}: {name}"
                    )
                if field is not None and field.comodel_name:
                    walk(child, self.env[field.comodel_name], view)

        for view in views:
            walk(etree.fromstring(view.arch_db), self.env[view.model], view)
        self.assertGreater(len(checked), 10)
