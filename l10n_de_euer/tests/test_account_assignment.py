# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Account assignment tests KT-01 to KT-13 (spec 9.4) in SKR03 and SKR04.

Every test runs on a company created from the chart template; the default
assignments come from the extension of ``_post_load_data``.
"""

from datetime import date

from odoo import Command
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from ..hooks import post_init_hook
from ..models.euer_account_assignment import default_rows

YEAR = 2026
DAY = "2026-03-15"


class AssignmentCases:
    """Test cases shared by SKR03 and SKR04; ``ACCOUNTS`` names template ids."""

    CHART = None
    ACCOUNTS = {}
    TEMPLATE_COUNT = 0
    DATEV_KU_CODE = None
    UTILITY_COUNT = 0
    WRITEOFF_RANGE = ()
    OWN_EXPENSE_START = None
    OWN_ASSET_START = None

    @classmethod
    def setup_euer(cls):
        cls.company = cls.env.company
        cls.Assignment = cls.env["l10n_de_euer.account.assignment"]
        cls.Checker = cls.env["l10n_de_euer.assignment.checker"]
        cls.categories = {
            category.code: category
            for category in cls.env["l10n_de_euer.category"].search([])
        }
        cls.bank = cls.company_data["default_journal_bank"].default_account_id
        cls.form = (
            cls.env["l10n_de_euer.form"]
            ._get_forms_for_year(YEAR)[0]
            .filtered(lambda form: form.form_type == "euer")
        )

    # ------------------------------------------------------------ helpers
    def acc(self, key):
        return self.ref_xml(self.ACCOUNTS[key])

    def ref_xml(self, xml_id):
        return self.env.ref(f"account.{self.company.id}_{xml_id}")

    def tax(self, key):
        tax = self.ref_xml(self.ACCOUNTS[key])
        tax.active = True
        return tax

    def assignment(self, account, on_date=DAY):
        return self.Assignment._get_assignments(
            self.company, account, date.fromisoformat(on_date)
        ).get(account, self.Assignment)

    def category_code(self, account):
        return self.assignment(account).category_id.code

    def entry(self, lines, on_date=DAY):
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

    def invoice(self, move_type, lines, on_date=DAY):
        move = self.env["account.move"].create(
            {
                "move_type": move_type,
                "partner_id": self.partner_a.id,
                "invoice_date": on_date,
                "date": on_date,
                "invoice_line_ids": [Command.create(line) for line in lines],
            }
        )
        move.action_post()
        return move

    def own_account(self, start, account_type, name):
        code = self.env["account.account"]._search_new_account_code(start)
        return self.env["account.account"].create(
            {"code": code, "name": name, "account_type": account_type}
        )

    def results(self, code, year=YEAR):
        return [
            result
            for result in self.Checker._run(self.company, year)
            if result["code"] == code
        ]

    def result_ids(self, code, year=YEAR):
        ids = set()
        for result in self.results(code, year):
            ids.update(result["res_ids"])
        return ids

    def ku_period(self, date_from="2026-01-01"):
        return self.env["l10n_de_euer.vat.period"].create(
            {"company_id": self.company.id, "date_from": date_from, "regime": "ku"}
        )

    def flush_tracking(self):
        self.env.flush_all()
        self.env.cr.flush()

    # ------------------------------------------------------------ KT-01
    def test_kt01_period_and_tracking(self):
        """Assignment with period of validity and change log (K-01, G-06)."""
        account = self.acc("telecom")
        # the test environment disables tracking; the module does not
        assignment = self.assignment(account).with_context(
            tracking_disable=False, mail_notrack=False
        )
        self.assertEqual(assignment.category_id.code, "EXP_TELECOM")
        self.assertEqual(account.l10n_de_euer_category_id, assignment.category_id)
        self.assertEqual(assignment.source, "template")
        self.assertFalse(assignment.confirmed)
        # records created in the same transaction are not tracked: end it
        self.flush_tracking()
        # correction of an error: changes the record, logged and confirmed
        assignment.category_id = self.categories["EXP_IT"]
        self.flush_tracking()
        self.assertEqual(assignment.source, "manual")
        self.assertTrue(assignment.confirmed)
        self.assertEqual(assignment.confirmed_by_id, self.env.user)
        messages = self.env["mail.message"].search(
            [("model", "=", assignment._name), ("res_id", "=", assignment.id)]
        )
        tracked = messages.tracking_value_ids.filtered(
            lambda value: value.field_id.name == "category_id"
        )
        self.assertTrue(tracked)
        # change of use: new period, the old one ends the day before
        new = self.Assignment.create(
            {
                "company_id": self.company.id,
                "account_id": account.id,
                "category_id": self.categories["EXP_TELECOM"].id,
                "date_from": "2026-07-01",
            }
        )
        self.assertEqual(assignment.date_to, date(2026, 6, 30))
        self.assertEqual(self.assignment(account, "2026-06-30"), assignment)
        self.assertEqual(self.assignment(account, "2026-07-01"), new)
        with self.assertRaises(ValidationError):
            self.Assignment.create(
                {
                    "company_id": self.company.id,
                    "account_id": account.id,
                    "category_id": self.categories["EXP_IT"].id,
                    "date_from": "2026-06-01",
                    "date_to": "2026-08-31",
                }
            )
        with self.assertRaises(ValidationError):
            new.date_to = "2026-01-01"

    # ------------------------------------------------------------ KT-02
    def test_kt02_defaults_complete(self):
        """Every template account and every account of the setup is assigned."""
        template = self.env["account.chart.template"]._get_chart_template_data(
            self.CHART
        )["account.account"]
        self.assertEqual(len(template), self.TEMPLATE_COUNT)
        accounts = self.env["account.account"]
        for xml_id in template:
            accounts |= self.ref_xml(xml_id)
        assignments = self.Assignment.search(
            [("company_id", "=", self.company.id), ("account_id", "in", accounts.ids)]
        )
        self.assertEqual(len(assignments), self.TEMPLATE_COUNT)
        self.assertEqual(set(assignments.mapped("source")), {"template"})
        # all accounts the chart created, including the utility accounts;
        # journals created later (here by the test setup) only get proposals
        names = [
            f"{self.company.id}_{row['xml_id']}"
            for row in default_rows(self.CHART)
            if row["xml_id"]
        ]
        created = self.env["ir.model.data"].search(
            [
                ("module", "=", "account"),
                ("model", "=", "account.account"),
                ("name", "in", names),
            ]
        )
        self.assertEqual(len(created), self.TEMPLATE_COUNT + self.UTILITY_COUNT)
        missing = (
            self.env["account.account"].browse(created.mapped("res_id"))
            - self.Assignment.search([("company_id", "=", self.company.id)]).account_id
        )
        self.assertFalse(missing, "accounts of the chart without assignment")
        self.assertEqual(self.category_code(self.bank), "CASH")
        suspense = self.company_data["default_journal_bank"].suspense_account_id
        self.assertEqual(self.category_code(suspense), "SUSPENSE")
        self.assertEqual(
            self.category_code(self.company.transfer_account_id), "TRANSIT"
        )
        self.assertEqual(self.category_code(self.acc("ku_revenue")), "REV_KU")  # K-07

    def test_kt02_datev_small_business_account(self):
        """DATEV 8192/4184 get the small business default when created (K-07)."""
        account = self.env["account.account"].create(
            {"code": self.DATEV_KU_CODE, "name": "Erlöse KU", "account_type": "income"}
        )
        assignment = self.assignment(account)
        self.assertEqual(assignment.category_id.code, "REV_KU")
        self.assertEqual(assignment.source, "template")
        self.assertEqual(
            self.Assignment._get_proposals(self.company, account)[account].code,
            "REV_KU",
        )

    # ------------------------------------------------------------ KT-03
    def test_kt03_hook_existing_company(self):
        """post_init_hook creates the assignments of an existing company."""
        assignments = self.Assignment.search([("company_id", "=", self.company.id)])
        before = {a.account_id.id: a.category_id.code for a in assignments}
        assignments.unlink()
        changed = self.acc("office")
        own = self.Assignment.create(
            {
                "company_id": self.company.id,
                "account_id": changed.id,
                "category_id": self.categories["EXP_OTHER"].id,
            }
        )
        post_init_hook(self.env)
        after = self.Assignment.search([("company_id", "=", self.company.id)])
        recreated = {a.account_id.id: a.category_id.code for a in after - own}
        self.assertEqual(
            {k: v for k, v in recreated.items() if k in before},
            {k: v for k, v in before.items() if k != changed.id},
        )
        # accounts of journals created after loading the chart (test setup)
        extra = (after - own).filtered(lambda a: a.account_id.id not in before)
        self.assertEqual(set(extra.mapped("source")) - {"structure"}, set())
        # an existing assignment is never changed
        self.assertEqual(own.category_id.code, "EXP_OTHER")
        self.assertEqual(after.filtered(lambda a: a.account_id == changed), own)

    def test_kt03_reload_template(self):
        """Reloading the chart keeps corrections and history, adds missing ones."""
        telecom = self.assignment(self.acc("telecom"))
        telecom.category_id = self.categories["EXP_IT"]
        office = self.acc("office")
        old = self.assignment(office)
        new = self.Assignment.create(
            {
                "company_id": self.company.id,
                "account_id": office.id,
                "category_id": self.categories["EXP_ADVERTISING"].id,
                "date_from": "2026-01-01",
            }
        )
        missing = self.assignment(self.acc("gift"))
        missing.unlink()
        before = self.Assignment.search([("company_id", "=", self.company.id)])
        values = {a: (a.category_id, a.date_from, a.date_to) for a in before}
        self.env["account.chart.template"].try_loading(
            self.CHART, self.company, install_demo=False, force_create=False
        )
        self.assertEqual(
            {a: (a.category_id, a.date_from, a.date_to) for a in before}, values
        )
        self.assertEqual(telecom.category_id.code, "EXP_IT")
        self.assertEqual(old.date_to, date(2025, 12, 31))
        self.assertEqual(old.category_id.code, "EXP_WORK_EQUIPMENT")
        self.assertEqual(new.category_id.code, "EXP_ADVERTISING")
        gift = self.assignment(self.acc("gift"))
        self.assertEqual(gift.category_id.code, "EXP_GIFTS")
        self.assertEqual(gift.source, "template")
        added = self.Assignment.search([("company_id", "=", self.company.id)]) - before
        self.assertIn(gift, added)
        self.assertEqual(set((added - gift).mapped("source")) - {"structure"}, set())

    # ------------------------------------------------------------ KT-04
    def test_kt04_own_account_proposal(self):
        """Own accounts get a proposal, never a silent assignment (K-03)."""
        own = self.own_account(self.OWN_EXPENSE_START, "expense", "Own supplies")
        self.assertFalse(self.assignment(own))
        proposals = self.Assignment._get_proposals(self.company, own)
        self.assertEqual(proposals[own].code, "EXP_WORK_EQUIPMENT")
        journal = self.env["account.journal"].create(
            {"name": "Second Bank", "code": "BNK2", "type": "bank"}
        )
        bank = journal.default_account_id
        self.assertFalse(self.assignment(bank))
        self.assertEqual(
            self.Assignment._get_proposals(self.company, bank)[bank].code, "CASH"
        )
        # review list: proposal shown, applied only on request
        self.entry([(own, 40.0), (self.bank, -40.0)])
        review = self.env["l10n_de_euer.account.review"].create(
            {"company_id": self.company.id, "year": YEAR}
        )
        action = review.action_review()
        self.assertEqual(action["domain"], [("review_id", "=", review.id)])
        line = review.line_ids.filtered(lambda item: item.account_id == own)
        self.assertTrue(line.unassigned)
        self.assertEqual(line.proposal_id.code, "EXP_WORK_EQUIPMENT")
        line.action_apply_proposal()
        assignment = self.assignment(own)
        self.assertEqual(assignment.category_id.code, "EXP_WORK_EQUIPMENT")
        self.assertEqual(assignment.source, "proposal")
        self.assertTrue(assignment.confirmed)
        line = review.line_ids.filtered(lambda item: item.account_id == own)
        self.assertFalse(line.unassigned)
        with self.assertRaises(UserError):
            line.action_apply_proposal()

    # ------------------------------------------------------------ KT-05
    def test_kt05_unassigned_accounts(self):
        """Unassigned income/expense account C-01, counter account C-02 (K-04)."""
        expense = self.own_account(self.OWN_EXPENSE_START, "expense", "Own costs")
        asset = self.own_account(self.OWN_ASSET_START, "asset_current", "Own claim")
        self.entry([(expense, 100.0), (self.bank, -100.0)])
        self.entry([(asset, 200.0), (self.bank, -200.0)])
        self.entry([(asset, 10.0), (self.acc("telecom"), -10.0)])
        c01 = self.results("C-01")
        self.assertEqual([r["res_ids"] for r in c01], [expense.ids])
        self.assertEqual(c01[0]["severity"], "blocker")
        self.assertEqual(c01[0]["amount"], 100.0)
        self.assertIn("Proposal", c01[0]["message"])
        c02 = self.results("C-02")
        self.assertEqual([r["res_ids"] for r in c02], [asset.ids])
        self.assertEqual(c02[0]["amount"], 200.0)
        # assigned from 2026-07-01 only: lines before are still unassigned
        self.Assignment.create(
            {
                "company_id": self.company.id,
                "account_id": expense.id,
                "category_id": self.categories["EXP_OTHER"].id,
                "date_from": "2026-07-01",
            }
        )
        self.assertEqual(self.result_ids("C-01"), set(expense.ids))
        self.assertFalse(self.results("C-01", 2025))

    # ------------------------------------------------------------ KT-06
    def test_kt06_tags_and_small_business(self):
        """UStVA tags → Kz (76 → 104, 49 → 103); KU never from taxes (K-06)."""
        revenue_tax = self.acc("revenue_tax")
        category = self.assignment(revenue_tax).category_id
        self.assertEqual(category.code, "REV_TAX")
        cases = (("tax_ust_19", "EUER.20.112"), ("tax_farmer", "EUER.20.104"))
        for key, code in cases:
            invoice = self.invoice(
                "out_invoice",
                [
                    {
                        "name": key,
                        "account_id": revenue_tax.id,
                        "price_unit": 100.0,
                        "tax_ids": [Command.set(self.tax(key).ids)],
                    }
                ],
            )
            tags = invoice.invoice_line_ids.tax_tag_ids.mapped("name")
            field, davon = category._get_revenue_fields(self.form, "regular", tags)
            with self.subTest(tax=key):
                self.assertEqual(field.code, code)
                self.assertFalse(davon)
        field, _davon = category._get_revenue_fields(self.form, "regular", ["49"])
        self.assertEqual(field.code, "EUER.20.103")
        # the Odoo tax for small businesses (tag 87 in SKR03) is no tax-exempt
        # revenue under standard taxation: VAT rate input (4.4 no. 5)
        ku_tax = self.tax("tax_ku")
        field, _davon = category._get_revenue_fields(
            self.form, "regular", ["87"], ku_tax
        )
        self.assertEqual(field.code, "EUER.20.112")
        field, _davon = category._get_revenue_fields(self.form, "regular", ["87"])
        self.assertEqual(field.code, "EUER.20.103")
        # small business revenue account with the inactive default tax tax_free_eu
        ku_account = self.acc("ku_revenue")
        invoice = self.invoice(
            "out_invoice",
            [
                {
                    "name": "small business",
                    "account_id": ku_account.id,
                    "price_unit": 100.0,
                    "tax_ids": [Command.set(self.tax("tax_free_eu").ids)],
                }
            ],
        )
        tags = invoice.invoice_line_ids.tax_tag_ids.mapped("name")
        self.assertIn("21", tags)
        regime = self.company._l10n_de_euer_get_vat_regime(date(YEAR, 3, 15))
        self.assertFalse(regime, "the VAT treatment is not derived from taxes")
        self.ku_period()
        regime = self.company._l10n_de_euer_get_vat_regime(date(YEAR, 3, 15))
        self.assertEqual(regime, "ku")
        field, davon = self.assignment(ku_account).category_id._get_revenue_fields(
            self.form, regime, tags
        )
        self.assertEqual((field.code, davon), ("EUER.20.111", davon.browse()))
        self.assertFalse(davon, "tag 21 of tax_free_eu is not Kz 119")
        self.assertFalse(self.company._l10n_de_euer_get_vat_regime(date(2025, 12, 31)))

    # ------------------------------------------------------------ KT-07
    def test_kt07_warning_accounts(self):
        """WARN accounts, DATEV collective accounts and manual-only accounts."""
        provision = self.acc("provision")
        collective = self.acc("collective")
        commute = self.acc("commute")
        for account in (provision, collective, commute):
            self.entry([(account, 30.0), (self.bank, -30.0)])
        self.assertEqual(
            self.result_ids("C-05"), set((provision | collective | commute).ids)
        )
        self.assertFalse(self.results("C-30"))

    def test_kt07_small_business_standard_accounts(self):
        """KU books on 3400/8400 (5400/4400) and moves VAT: C-30, C-31."""
        bill = self.invoice(
            "in_invoice",
            [
                {
                    "name": "goods",
                    "account_id": self.acc("expense_default").id,
                    "price_unit": 100.0,
                    "tax_ids": [Command.set(self.tax("tax_vst_19").ids)],
                }
            ],
        )
        invoice = self.invoice(
            "out_invoice",
            [
                {
                    "name": "service",
                    "account_id": self.acc("revenue").id,
                    "price_unit": 200.0,
                    "tax_ids": [Command.set(self.tax("tax_ust_19").ids)],
                }
            ],
        )
        self.assertFalse(self.results("C-30"), "no KU period: no warning")
        self.ku_period()
        c30 = self.results("C-30")
        self.assertEqual(len(c30), 3)  # input VAT, 3400/5400, 8400/4400
        self.assertEqual({r["severity"] for r in c30}, {"warning"})
        self.assertEqual(self.result_ids("C-30"), set((bill | invoice).ids))
        c31 = self.results("C-31")
        self.assertEqual([r["res_ids"] for r in c31], [invoice.ids])
        self.assertAlmostEqual(c31[0]["amount"], 38.0)
        # import VAT paid to customs is not deductible for a small business
        customs = self.entry([(self.acc("vat_import"), 19.0), (self.bank, -19.0)])
        self.assertIn(customs.id, self.result_ids("C-30"))
        self.assertEqual(
            self.assignment(self.acc("vat_import")).category_id.code, "VAT_IMPORT"
        )

    # ------------------------------------------------------------ KT-08
    def test_kt08_low_confidence(self):
        """Low confidence used → C-07 until confirmed (K-05)."""
        account = self.acc("low")
        assignment = self.assignment(account)
        self.assertEqual(assignment.confidence, "n")
        self.assertTrue(assignment.needs_confirmation)
        self.assertFalse(self.results("C-07"), "not used: no blocker")
        self.entry([(account, 50.0), (self.bank, -50.0)])
        c07 = self.results("C-07")
        self.assertEqual([r["res_ids"] for r in c07], [assignment.ids])
        self.assertEqual(c07[0]["severity"], "blocker")
        assignment.action_confirm()
        self.assertFalse(assignment.needs_confirmation)
        self.assertFalse(self.results("C-07"))

    # ------------------------------------------------------------ KT-09
    def test_kt09_separate_records(self):
        """§ 4 (7) EStG: gifts on a general account and mixed accounts (C-50)."""
        gift = self.acc("gift")
        office = self.acc("office")
        gift_product = self._create_product(
            name="Gift basket", property_account_expense_id=gift.id
        )
        paper = self._create_product(
            name="Paper", property_account_expense_id=office.id
        )
        general = self.invoice(
            "in_invoice",
            [
                {
                    "product_id": gift_product.id,
                    "account_id": office.id,
                    "price_unit": 30.0,
                    "tax_ids": [Command.clear()],
                }
            ],
        )
        mixed = self.invoice(
            "in_invoice",
            [
                {
                    "product_id": paper.id,
                    "account_id": gift.id,
                    "price_unit": 20.0,
                    "tax_ids": [Command.clear()],
                }
            ],
        )
        proper = self.invoice(
            "in_invoice",
            [
                {
                    "product_id": gift_product.id,
                    "account_id": gift.id,
                    "price_unit": 25.0,
                    "tax_ids": [Command.clear()],
                }
            ],
        )
        c50 = self.results("C-50")
        self.assertEqual(
            sorted(r["res_ids"] for r in c50), sorted([general.ids, mixed.ids])
        )
        self.assertNotIn(proper.id, self.result_ids("C-50"))
        # an account for gifts that is the default expense account collects
        # other costs as well
        default = self.company.expense_account_id
        self.Assignment.create(
            {
                "company_id": self.company.id,
                "account_id": default.id,
                "category_id": self.categories["EXP_GIFTS"].id,
                "date_from": "2026-01-01",
            }
        )
        general_accounts = [
            r for r in self.results("C-50") if r["res_model"] == "account.account"
        ]
        self.assertEqual([r["res_ids"] for r in general_accounts], [default.ids])

    # ------------------------------------------------------------ KT-10
    def test_kt10_interim_accounts(self):
        """Transit and interim accounts not zero on 31.12. → C-06 (K-09)."""
        transit = self.company.transfer_account_id
        self.entry([(transit, 50.0), (self.bank, -50.0)])
        c06 = self.results("C-06")
        self.assertEqual([r["res_ids"] for r in c06], [transit.ids])
        self.assertEqual(c06[0]["amount"], 50.0)
        self.entry([(self.bank, 50.0), (transit, -50.0)], on_date="2026-12-31")
        self.assertFalse(self.results("C-06"))
        self.entry([(transit, 5.0), (self.bank, -5.0)], on_date="2027-01-02")
        self.assertFalse(self.results("C-06"), "balance on 31.12. counts")

    # ------------------------------------------------------------ KT-11
    def test_kt11_reconcilable_income(self):
        """Income account with reconciliation → C-08, still role N (B-04)."""
        revenue = self.acc("revenue")
        revenue.reconcile = True
        self.entry([(self.bank, 100.0), (revenue, -100.0)])
        self.assertEqual(self.result_ids("C-08"), set(revenue.ids))
        self.assertFalse(revenue._l10n_de_euer_is_open_item())
        receivable = self.company_data["default_account_receivable"]
        self.assertTrue(receivable._l10n_de_euer_is_open_item())
        self.assertTrue(self.company.transfer_account_id._l10n_de_euer_is_open_item())
        self.assertFalse(self.bank._l10n_de_euer_is_open_item())

    # ------------------------------------------------------------ KT-12, KT-13
    def test_kt12_kt13_opening_and_writeoff(self):
        """9000-9009, 9060-9089, 9090 opening (K-13), write-off flag (K-15)."""
        for xml_id in self.ACCOUNTS["opening"]:
            with self.subTest(xml_id=xml_id):
                self.assertEqual(
                    self.assignment(self.ref_xml(xml_id)).role, "eroeffnung"
                )
        start, end = self.WRITEOFF_RANGE
        writeoff = self.Assignment.search(
            [("company_id", "=", self.company.id), ("category_id.writeoff", "=", True)]
        )
        codes = {int(a.account_id.with_company(self.company).code) for a in writeoff}
        self.assertTrue(codes)
        self.assertTrue(all(start <= code <= end for code in codes), codes)
        expected = {
            "entertainment": "EXP_ENTERTAINMENT",
            "entertainment_nd": "EXP_ENTERTAINMENT_NON_DEDUCTIBLE",
            "private_w": "PRIVATE_WITHDRAWAL",
            "private_c": "PRIVATE_CONTRIBUTION",
            "vat_in": "VAT_INPUT",
            "vat_out": "VAT_OUTPUT",
            "payroll": "EXP_PAYROLL_LIABILITY",
            "advance": "REV_ADVANCE_RECEIVED",
        }
        for key, code in expected.items():
            with self.subTest(account=key):
                self.assertEqual(self.category_code(self.acc(key)), code)
        self.assertTrue(self.assignment(self.acc("payroll")).category_id.on_payment)

    # ------------------------------------------------------------ C-09
    def test_c09_private_share(self):
        """Private share of mixed expenses on the fallback account (K-11)."""
        journal = self.company_data["default_journal_purchase"]
        self.assertFalse(journal.non_deductible_account_id)
        line = {
            "name": "phone",
            "account_id": self.acc("telecom").id,
            "price_unit": 100.0,
            "deductible_amount": 60.0,
            "tax_ids": [Command.set(self.tax("tax_vst_19").ids)],
        }
        fallback = self.invoice("in_invoice", [line])
        private = fallback.line_ids.filtered(
            lambda item: item.display_type == "non_deductible_product_total"
        )
        self.assertTrue(private)
        self.assertEqual(self.result_ids("C-09"), set(fallback.ids))
        journal.non_deductible_account_id = self.acc("private_w")
        proper = self.invoice("in_invoice", [line])
        self.assertNotIn(proper.id, self.result_ids("C-09"))

    # ------------------------------------------------------------ storage
    def test_check_storage_and_acknowledgement(self):
        expense = self.own_account(self.OWN_EXPENSE_START, "expense", "Own costs")
        self.entry([(expense, 100.0), (self.bank, -100.0)])
        self.entry([(self.acc("provision"), 10.0), (self.bank, -10.0)])
        Check = self.env["l10n_de_euer.check"]
        checks = Check._run_assignment_checks(self.company, YEAR)
        blocker = checks.filtered(lambda c: c.code == "C-01")
        warning = checks.filtered(lambda c: c.code == "C-05")
        self.assertEqual(blocker.res_ids, expense.ids)
        self.assertEqual(blocker.record_count, 1)
        self.assertEqual(blocker.action_open_records()["res_model"], "account.account")
        with self.assertRaises(UserError):
            blocker.action_acknowledge()
        with self.assertRaises(UserError):
            warning.action_acknowledge()
        warning.acknowledge_reason = "Booked by the tax advisor, reversed in 2027."
        warning.action_acknowledge()
        self.assertTrue(warning.acknowledged_on)
        with self.assertRaises(UserError):
            warning.acknowledge_reason = "changed afterwards"
        with self.assertRaises(UserError):
            warning.action_acknowledge()
        again = Check._run_assignment_checks(self.company, YEAR)
        self.assertIn(warning, again)
        self.assertNotIn(blocker, again)
        self.assertEqual(len(again.filtered(lambda c: c.code == "C-05")), 1)
        self.assertEqual(len(again.filtered(lambda c: c.code == "C-01")), 1)
        # more bookings on the account: the acknowledgement is reset
        self.entry([(self.acc("provision"), 10000.0), (self.bank, -10000.0)])
        again = Check._run_assignment_checks(self.company, YEAR)
        self.assertIn(warning, again)
        self.assertFalse(warning.acknowledged_on)
        self.assertAlmostEqual(warning.amount, 10010.0)
        self.assertEqual(len(again.filtered(lambda c: c.code == "C-05")), 1)
        review = self.env["l10n_de_euer.account.review"].create(
            {"company_id": self.company.id, "year": YEAR}
        )
        self.assertEqual(review.action_run_checks()["res_model"], "l10n_de_euer.check")

    def test_access_rights(self):
        """Assignment: managers write, auditors and bookkeepers read (7.10)."""
        manager = new_test_user(
            self.env,
            login="euer_assign_manager",
            groups="account.group_account_manager",
            company_id=self.company.id,
        )
        reader = new_test_user(
            self.env,
            login="euer_assign_reader",
            groups="account.group_account_readonly",
            company_id=self.company.id,
        )
        bookkeeper = new_test_user(
            self.env,
            login="euer_assign_user",
            groups="account.group_account_user",
            company_id=self.company.id,
        )
        assignment = self.assignment(self.acc("telecom"))
        assignment.with_user(manager).action_confirm()
        self.assertTrue(assignment.confirmed)
        for user in (reader, bookkeeper):
            with self.subTest(user=user.login):
                self.assertEqual(
                    assignment.with_user(user).category_id.code, "EXP_TELECOM"
                )
                with self.assertRaises(AccessError):
                    assignment.with_user(user).note = "x"
        checks = self.env["l10n_de_euer.check"].with_user(bookkeeper)
        checks._run_assignment_checks(self.company, YEAR)
        # G-05: results are changed by the checks only (no silent
        # acknowledgement of a blocker by RPC or import)
        expense = self.own_account(self.OWN_EXPENSE_START, "expense", "Own costs")
        self.entry([(expense, 100.0), (self.bank, -100.0)])
        blocker = checks._run_assignment_checks(self.company, YEAR).filtered(
            lambda check: check.code == "C-01"
        )
        self.assertEqual(blocker.severity, "blocker")
        with self.assertRaises(UserError):
            blocker.write({"acknowledged_on": "2026-12-31 12:00:00"})
        with self.assertRaises(UserError):
            blocker.write({"severity": "warning"})
        with self.assertRaises(AccessError):
            blocker.unlink()
        with self.assertRaises(UserError):
            checks.create(
                {"year": YEAR, "code": "C-01", "severity": "info", "message": "x"}
            )
        # even an acknowledged blocker (written as superuser) is not kept
        blocker.sudo().with_context(l10n_de_euer_check_internal=True).write(
            {"acknowledged_on": "2026-12-31 12:00:00"}
        )
        again = checks._run_assignment_checks(self.company, YEAR)
        self.assertNotIn(blocker.id, again.ids)
        self.assertFalse(again.filtered(lambda c: c.code == "C-01").acknowledged_on)
        reader = new_test_user(
            self.env,
            login="euer_assign_reader2",
            groups="account.group_account_readonly",
            company_id=self.company.id,
        )
        with self.assertRaises(AccessError):
            checks.with_user(reader)._run_assignment_checks(self.company, YEAR)

    # ------------------------------------------------------------ 7.9
    def test_branch_and_other_company(self):
        """Spec 7.9, GT-02: accounts and users of a branch, other businesses."""
        branch = self._create_company(name="EÜR Branch", parent_id=self.company.id)
        Account = self.env["account.account"].with_company(branch)
        # DATEV small business account created in the branch (K-07)
        datev = Account.create(
            {
                "code": self.DATEV_KU_CODE,
                "name": "Branch small business revenue",
                "account_type": "income",
            }
        )
        self.assertEqual(datev.company_ids, branch)
        self.assertEqual(self.category_code(datev), "REV_KU")
        # own account of the branch: C-01 until assigned, then assignable
        own = Account.create(
            {
                "code": Account._search_new_account_code(self.OWN_EXPENSE_START),
                "name": "Branch costs",
                "account_type": "expense",
            }
        )
        journal = (
            self.env["account.journal"].search(
                [("company_id", "=", branch.id), ("type", "=", "general")], limit=1
            )
            or self.company_data["default_journal_misc"]
        )
        move = (
            self.env["account.move"]
            .with_company(branch)
            .create(
                {
                    "move_type": "entry",
                    "date": DAY,
                    "journal_id": journal.id,
                    "line_ids": [
                        Command.create(
                            {"account_id": own.id, "balance": 50.0, "name": "Branch"}
                        ),
                        Command.create(
                            {
                                "account_id": self.bank.id,
                                "balance": -50.0,
                                "name": "Bank",
                            }
                        ),
                    ],
                }
            )
        )
        move.action_post()
        self.assertIn(own.id, self.result_ids("C-01"))
        branch_user = new_test_user(
            self.env,
            login="euer_branch_user",
            groups="account.group_account_manager,account.group_account_user",
            company_id=branch.id,
            company_ids=[Command.set(branch.ids)],
        )
        review = (
            self.env["l10n_de_euer.account.review"]
            .with_user(branch_user)
            .with_company(branch)
            .create({"year": YEAR})
        )
        self.assertEqual(review.company_id, self.company)
        action = review.action_review()
        lines = (
            self.env["l10n_de_euer.account.review.line"]
            .with_user(branch_user)
            .search(action["domain"])
        )
        line = lines.filtered(lambda item: item.account_id == own)
        self.assertTrue(line.unassigned)
        self.assertEqual(line.proposal_id.code, "EXP_WORK_EQUIPMENT")
        action = line.action_open_assignment()
        self.assertEqual(action["context"]["default_company_id"], self.company.id)
        line.action_apply_proposal()
        self.assertEqual(self.category_code(own), "EXP_WORK_EQUIPMENT")
        self.assertNotIn(own.id, self.result_ids("C-01"))
        # another business: its accounts belong neither to the checks nor to
        # the assignments of this business
        other = self.setup_other_company(name="EÜR Other")
        other_account = other["default_account_expense"]
        with self.assertRaises(ValidationError):
            self.Assignment.create(
                {
                    "company_id": self.company.id,
                    "account_id": other_account.id,
                    "category_id": self.categories["EXP_OTHER"].id,
                }
            )
        other_move = (
            self.env["account.move"]
            .with_company(other["company"])
            .create(
                {
                    "move_type": "entry",
                    "date": DAY,
                    "journal_id": other["default_journal_misc"].id,
                    "line_ids": [
                        Command.create(
                            {
                                "account_id": other_account.id,
                                "balance": 70.0,
                                "name": "x",
                            }
                        ),
                        Command.create(
                            {
                                "account_id": other[
                                    "default_journal_bank"
                                ].default_account_id.id,
                                "balance": -70.0,
                                "name": "y",
                            }
                        ),
                    ],
                }
            )
        )
        other_move.action_post()
        accounts = set()
        for result in self.Checker._run(self.company, YEAR):
            if result["res_model"] == "account.account":
                accounts.update(result["res_ids"])
        self.assertNotIn(other_account.id, accounts)


@tagged("post_install_l10n", "post_install", "-at_install")
class TestAssignmentSkr03(AssignmentCases, AccountTestInvoicingCommon):
    allow_inherited_tests_method = True
    CHART = "de_skr03"
    TEMPLATE_COUNT = 1274
    DATEV_KU_CODE = "8192"
    # suspense, outstanding receipts/payments, transfer, cash differences,
    # undistributed profits
    UTILITY_COUNT = 7
    WRITEOFF_RANGE = (2400, 2451)
    OWN_EXPENSE_START = "4930"
    OWN_ASSET_START = "1500"
    ACCOUNTS = {
        "telecom": "account_4920",
        "office": "account_4930",
        "gift": "account_4630",
        "entertainment": "account_4650",
        "entertainment_nd": "account_4654",
        "revenue": "account_8400",
        "revenue_tax": "account_8200",
        "ku_revenue": "account_8195",
        "expense_default": "account_3400",
        "private_w": "account_1800",
        "private_c": "account_1890",
        "vat_in": "account_1576",
        "vat_out": "account_1776",
        "provision": "account_0950",
        "collective": "account_4500",
        "commute": "account_4680",
        "low": "account_4964",
        "payroll": "account_1740",
        "advance": "account_1710",
        "opening": (
            "account_9000",
            "account_9008",
            "account_9009",
            "account_9089",
            "account_9090",
        ),
        "vat_import": "account_1788",
        "tax_ku": "tax_ust_no_ustpflicht_skr03",
        "tax_ust_19": "tax_ust_19_skr03",
        "tax_vst_19": "tax_vst_19_skr03",
        "tax_farmer": "tax_ust_19_farmer_skr03",
        "tax_free_eu": "tax_free_eu_skr03",
    }

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_euer()

    def test_skr03_names(self):
        """1585 is a tax account (A11); 4810 is leasing, 4980 operating supplies."""
        self.assertEqual(
            self.category_code(self.ref_xml("account_1585")), "VAT_REVERSE_CHARGE"
        )
        self.assertEqual(
            self.category_code(self.ref_xml("account_1755")), "EXP_PAYROLL_LIABILITY"
        )
        self.assertEqual(
            self.category_code(self.ref_xml("account_4810")), "EXP_LEASING"
        )
        # DATEV 4980 is "Sonstiger Betriebsbedarf" (Odoo: wrong German name)
        assignment = self.assignment(self.ref_xml("account_4980"))
        self.assertEqual(assignment.category_id.code, "EXP_WORK_EQUIPMENT")
        self.assertTrue(assignment.needs_confirmation)


@tagged("post_install_l10n", "post_install", "-at_install")
class TestAssignmentSkr04(AssignmentCases, AccountTestInvoicingCommon):
    allow_inherited_tests_method = True
    CHART = "de_skr04"
    TEMPLATE_COUNT = 1192
    DATEV_KU_CODE = "4184"
    # cash differences are template accounts 9991/9994 in SKR04
    UTILITY_COUNT = 5
    WRITEOFF_RANGE = (6280, 6938)
    OWN_EXPENSE_START = "6815"
    OWN_ASSET_START = "1300"
    ACCOUNTS = {
        "telecom": "chart_skr04_6805",
        "office": "chart_skr04_6815",
        "gift": "chart_skr04_6610",
        "entertainment": "chart_skr04_6640",
        "entertainment_nd": "chart_skr04_6644",
        "revenue": "chart_skr04_4400",
        "revenue_tax": "chart_skr04_4200",
        "ku_revenue": "chart_skr04_4185",
        "expense_default": "chart_skr04_5400",
        "private_w": "chart_skr04_2100",
        "private_c": "chart_skr04_2180",
        "vat_in": "chart_skr04_1406",
        "vat_out": "chart_skr04_3806",
        "provision": "chart_skr04_3000",
        "collective": "chart_skr04_6500",
        "commute": "chart_skr04_6690",
        "low": "chart_skr04_6837",
        "payroll": "chart_skr04_3720",
        "advance": "chart_skr04_3250",
        "opening": (
            "chart_skr04_9000",
            "chart_skr04_9008",
            "chart_skr04_9009",
            "chart_skr04_9090",
        ),
        "vat_import": "chart_skr04_3850",
        "tax_ku": "tax_ust_no_ustpflicht_skr04",
        "tax_ust_19": "tax_ust_19_skr04",
        "tax_vst_19": "tax_vst_19_skr04",
        "tax_farmer": "tax_ust_19_farmer_skr04",
        "tax_free_eu": "tax_free_eu_skr04",
    }

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr04")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_euer()

    def test_skr04_writeoff_from_6930(self):
        """SKR04 6930 ff. carry the write-off flag (K-15)."""
        for code in ("6930", "6931", "6936", "6938"):
            with self.subTest(code=code):
                assignment = self.assignment(self.ref_xml(f"chart_skr04_{code}"))
                self.assertTrue(assignment.category_id.writeoff)
