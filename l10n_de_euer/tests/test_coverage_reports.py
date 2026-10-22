# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Layout of the printed reports (spec 7.5, 7.12, review E5).

The PDF keeps the caption and the header of a long table together with its
first row (wkhtmltopdf ignores ``page-break-after: avoid``); the fill-in aid
gives a text value the width of the value and the previous year columns, as
a text has no previous year. Synthetic data (D6).
"""

from lxml import html

from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from ..models.euer_check import INTERNAL as CHECK_INTERNAL
from .scenario_common import SKR03_ACCOUNTS, SKR03_TAXES
from .test_statement_review import ReviewCommon

REPORTS = (
    "l10n_de_euer.report_fill_aid",
    "l10n_de_euer.report_evidence",
    "l10n_de_euer.report_check_protocol",
)


@tagged("post_install_l10n", "post_install", "-at_install")
class TestReportLayout(ReviewCommon, AccountTestInvoicingCommon):
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_finalisation()

    def computed(self):
        self.year()
        self.asset()
        statement = self.statement()
        statement.action_compute()
        return statement

    def render(self, xml_id, statement, pdf=False):
        content, _kind = self.env["ir.actions.report"]._render_qweb_html(
            xml_id, statement.ids, data={"report_type": "pdf"} if pdf else None
        )
        return html.fromstring(content)

    def test_pdf_keeps_header_with_first_row(self):
        """In the PDF every long table follows an empty keep-with-next block
        hidden from assistive technology; the HTML view has none."""
        statement = self.computed()
        for xml_id in REPORTS:
            document = self.render(xml_id, statement, pdf=True)
            tables = document.xpath("//table[contains(@class, 'o_l10n_de_euer_fixed')]")
            self.assertTrue(tables, xml_id)
            for table in tables:
                block = table.getprevious()
                self.assertIsNotNone(block, xml_id)
                self.assertIn("o_l10n_de_euer_keep_next", block.get("class"), xml_id)
                self.assertEqual(block.get("aria-hidden"), "true")
                self.assertEqual(block.text_content().strip(), "")
            screen = self.render(xml_id, statement)
            self.assertTrue(
                screen.xpath("//table[contains(@class, 'o_l10n_de_euer_fixed')]")
            )
            self.assertFalse(
                screen.xpath("//*[contains(@class, 'o_l10n_de_euer_keep_next')]"),
                xml_id,
            )

    def test_fill_aid_columns_fit_the_german_headings(self):
        """Regression (E5 re-check): with 5 % the heading "Zeile" of the
        line column broke into "Zeil" and "e" in the portrait PDF. The line
        column has 7 %, the Kz column 6 %; the columns fill the width of the
        table (fixed layout)."""
        statement = self.computed()
        document = self.render("l10n_de_euer.report_fill_aid", statement, pdf=True)
        tables = document.xpath("//table[contains(@class, 'o_l10n_de_euer_fill_aid')]")
        self.assertTrue(tables)
        for table in tables:
            widths = [
                float(cell.get("style").split(":")[1].strip().rstrip("%"))
                for cell in table.xpath("./thead/tr/th")
            ]
            self.assertGreaterEqual(widths[0], 7.0)
            self.assertGreaterEqual(widths[1], 6.0)
            self.assertEqual(sum(widths), 100.0)

    def test_check_protocol_without_results_has_no_block(self):
        """Without check results there is no table and no block."""
        statement = self.computed()
        statement.check_ids.sudo().with_context(**{CHECK_INTERNAL: True}).unlink()
        statement.invalidate_recordset(["check_ids"])
        self.assertFalse(statement.check_ids)
        document = self.render(
            "l10n_de_euer.report_check_protocol", statement, pdf=True
        )
        self.assertFalse(document.xpath("//table[contains(@class, 'checks')]"))
        self.assertFalse(
            document.xpath("//*[contains(@class, 'o_l10n_de_euer_keep_next')]")
        )
        self.assertIn("The checks found nothing.", document.text_content())

    def test_fill_aid_text_values_span_two_columns(self):
        """Header fields (type of business, legal form) have no previous
        year: their text takes the value and the previous year column, every
        row still covers the six columns of the table."""
        statement = self.computed()
        document = self.render("l10n_de_euer.report_fill_aid", statement, pdf=True)
        texts, amounts = 0, 0
        for table in document.xpath(
            "//table[contains(@class, 'o_l10n_de_euer_fill_aid')]"
        ):
            self.assertEqual(len(table.xpath("./thead/tr/th")), 6)
            for row in table.xpath("./tbody/tr"):
                cells = row.xpath("./th|./td")
                width = sum(int(cell.get("colspan", 1)) for cell in cells)
                self.assertEqual(width, 6, html.tostring(row))
                if any(cell.get("colspan") == "2" for cell in cells):
                    texts += 1
                else:
                    amounts += 1
        self.assertTrue(texts, "no header field in the fill-in aid")
        self.assertTrue(amounts, "no amount in the fill-in aid")
