# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Finalisation, integrity, comparison and correction (spec 7.7, 9.7).

GT-04 (payload complete, hash reproducible, chain), GT-05 (finalised data
cannot be changed or deleted, also as administrator and with ``sudo()``),
GT-06 (prerequisites of G-05, change log of the choices), GT-07 (correction
with differences, previous version unchanged), GT-14 (answers in the
payload), GT-15 (deviation after the finalisation: C-60) and GT-16 (nothing
is written into journal entries, also hashed ones). SKR03 and SKR04;
synthetic cases (D6).
"""

import hashlib
import json

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from ..models.engine import integrity, scope
from ..models.euer_statement_final import FROZEN_SECTIONS
from .scenario_common import (
    SKR03_ACCOUNTS,
    SKR03_TAXES,
    SKR04_ACCOUNTS,
    SKR04_TAXES,
)
from .statement_common import D, StatementCommon

KU = "EUER.20.111"
INCOME = "EUER.20.159"
PROFIT = "EUER.G.219"
MEALS = "EUER.25.171"
DEPRECIATION = "EUER.25.130"
#: the internal context keys of the module (they need ``sudo()`` as well)
INTERNAL = {
    "l10n_de_euer_statement_internal": True,
    "l10n_de_euer_slice_internal": True,
    "l10n_de_euer_asset_year_internal": True,
    "l10n_de_euer_check_internal": True,
}


def _numbers(value):
    """All numbers in a JSON structure."""
    if isinstance(value, dict):
        for item in value.values():
            yield from _numbers(item)
    elif isinstance(value, list):
        for item in value:
            yield from _numbers(item)
    elif isinstance(value, int | float) and not isinstance(value, bool):
        yield value


class FinalScenarios(StatementCommon):
    def year_2026(self):
        """KU business: 1,000.00 received, 200.00 paid in 2026."""
        sale = self.sale("2026-03-10", 1000)
        self.bank("2026-03-20", 1000, matches=[(sale, 1000)])
        bill = self.purchase("2026-04-10", 200)
        self.bank("2026-04-15", -200, matches=[(bill, 200)])
        return sale, bill

    def asset(self):
        """A camera of 2,150.00 bought in July 2026, 84 months (Kz 130)."""
        bill = self.purchase("2026-07-15", 2150, account="asset")
        self.bank("2026-07-20", -2150, matches=[(bill, 2150)])
        asset = self.env["l10n_de_euer.asset"].create(
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
        asset.action_activate()
        return asset

    def finalized(self, with_correction=True):
        self.year_2026()
        statement = self.statement()
        if with_correction:
            self.correction(statement, "meals", days_8h=1)
        return self.finalize(statement)

    def payload(self, statement):
        return json.loads(statement.payload_attachment_id.raw)

    def c60(self, statement):
        return self.results(statement, "C-60").filtered(
            lambda check: check.res_model
            in ("l10n_de_euer.field", "l10n_de_euer.cash.balance", "account.journal")
        )

    # ------------------------------------------------------------ GT-04
    def test_gt04_payload_hash_and_chain(self):
        """GT-04: the payload is canonical JSON with amounts in cent and all
        parts of the year; its hash and the chain hash can be computed
        again; the next finalisation of the business follows in the chain."""
        asset = self.asset()
        statement = self.finalized()
        self.assertEqual(statement.state, "final")
        self.assertEqual(statement.finalized_by_id, self.env.user)
        self.assertTrue(statement.finalized_on)
        raw = statement.payload_attachment_id.raw
        self.assertEqual(statement.payload_attachment_id.mimetype, "application/json")
        self.assertEqual(hashlib.sha256(raw).hexdigest(), statement.payload_sha256)
        self.assertEqual(statement.chain_index, 1)
        self.assertEqual(statement.prev_hash, "0" * 64)
        self.assertEqual(
            statement.chain_hash,
            hashlib.sha256(
                (statement.prev_hash + statement.payload_sha256).encode()
            ).hexdigest(),
        )
        # canonical: sorted keys, no blanks, amounts as cent integers
        payload = json.loads(raw)
        self.assertEqual(
            raw,
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode(),
        )
        self.assertFalse([n for n in _numbers(payload) if isinstance(n, float)])
        self.assertEqual(payload["schema"], integrity.SCHEMA)
        for section in (
            *FROZEN_SECTIONS,
            "slice_totals",
            "settings",
            "choices",
            "vat_periods",
            "assignment",
            "parameters",
            "asset_pools",
            "checks",
            "classifications",
            "opening_items",
            "ten_day_items",
            "references",
        ):
            self.assertIn(section, payload)
        # field values with line, Kz and official text
        values = {row["code"]: row for row in payload["fields"]}
        self.assertEqual(values[KU]["amount"], 100000)
        self.assertEqual((values[KU]["line"], values[KU]["kz"]), (12, "111"))
        self.assertTrue(values[KU]["official_label"])
        self.assertEqual(values[PROFIT]["amount"], round(statement.profit * 100))
        # slices, their sums and the run
        self.assertEqual(len(payload["slices"]), len(statement.run_id.slice_ids))
        self.assertEqual(payload["slice_totals"][KU], 100000)
        self.assertEqual(payload["run"]["totals"], statement.run_id.totals)
        # account → category → field, VAT status, settings, rule set
        accounts = {
            row["account_id"]: row["category_id"]
            for row in payload["assignment"]["accounts"]
        }
        revenue = self.acc("ku_revenue")
        self.assertIn(revenue.id, accounts)
        self.assertIn(
            KU,
            {row["field_code"] for row in payload["assignment"]["mappings"]},
        )
        self.assertEqual(
            payload["references"]["account.account"][str(revenue.id)]["code"],
            revenue.code,
        )
        self.assertEqual(payload["vat_periods"][0]["regime"], "ku")
        self.assertEqual(
            payload["settings"]["main_company"]["fiscalyear_lock_date"], "2026-12-31"
        )
        self.assertEqual(payload["choices"]["entertainment_mode"], "separate")
        self.assertTrue(payload["parameters"])
        # profit correction, asset year, checks and their acknowledgements
        self.assertEqual(payload["adjustments"][0]["kind"], "meals")
        self.assertEqual(payload["adjustments"][0]["field_amounts"][MEALS], 1400)
        years = {row["asset_id"]: row for row in payload["asset_years"]}
        self.assertTrue(years[asset.id]["locked"])
        self.assertEqual(years[asset.id]["depreciation"], 15357)
        warnings = [row for row in payload["checks"] if row["severity"] == "warning"]
        self.assertTrue(all(row["acknowledged_on"] for row in warnings))
        # versions of the forms, the rule set and the module
        self.assertEqual(statement.module_version, "19.0.1.0.0")
        self.assertEqual(statement.ruleset_version, statement._get_ruleset_version())
        self.assertIn("2026", statement.form_versions)
        self.assertEqual({row["year"] for row in payload["forms"]}, {2026})
        self.assertTrue(all(row["state"] == "verified" for row in payload["forms"]))
        # the frozen parts are reproducible from the records
        self.assertEqual(
            integrity.changed_sections(
                payload, statement._get_frozen_sections(), FROZEN_SECTIONS
            ),
            [],
        )
        self.assertEqual(statement._verify_integrity(), [])
        # locked: run, asset years, forms
        self.assertTrue(statement.run_id.locked)
        self.assertTrue(asset.year_ids.filtered(lambda line: line.year == 2026).locked)
        self.assertTrue(all(statement.form_ids.mapped("locked")))
        # the next finalisation of the business follows in the chain
        previous = self.statement(2025)
        self.finalize(previous)
        self.assertEqual(previous.chain_index, 2)
        self.assertEqual(previous.prev_hash, statement.chain_hash)
        self.assertEqual(previous._verify_integrity(), [])

    def test_gt04_integrity_check_finds_changes(self):
        """GT-04: the integrity check finds changed records, a changed
        payload and a broken chain (changes by SQL cannot be prevented, they
        are made visible); the result is logged in the chatter."""
        statement = self.finalized()
        statement.action_check_integrity()
        self.assertIn("unchanged", statement.message_ids[0].body)
        cr = self.env.cr
        line = statement.line_ids.filtered(lambda line: line.code == KU)
        cr.execute(
            "UPDATE l10n_de_euer_statement_line SET amount = 1100 WHERE id = %s",
            [line.id],
        )
        self.env.invalidate_all()
        problems = statement._verify_integrity()
        self.assertEqual(len(problems), 1)
        self.assertIn("field values", problems[0])
        statement.action_check_integrity()
        self.assertIn("failed", statement.message_ids[0].body)
        cr.execute(
            "UPDATE l10n_de_euer_statement_line SET amount = 1000 WHERE id = %s",
            [line.id],
        )
        # slices of the finalised run
        one = statement.run_id.slice_ids[:1]
        cr.execute(
            "UPDATE l10n_de_euer_cash_slice SET amount = amount + 1 WHERE id = %s",
            [one.id],
        )
        self.env.invalidate_all()
        self.assertIn("slices", statement._verify_integrity()[0])
        cr.execute(
            "UPDATE l10n_de_euer_cash_slice SET amount = amount - 1 WHERE id = %s",
            [one.id],
        )
        self.env.invalidate_all()
        self.assertEqual(statement._verify_integrity(), [])
        # another payload in the attachment
        other = self.env["ir.attachment"].create({"name": "other", "raw": b"{}"})
        cr.execute(
            "UPDATE ir_attachment SET (store_fname, db_datas) = "
            "(SELECT store_fname, db_datas FROM ir_attachment WHERE id = %s) "
            "WHERE id = %s",
            [other.id, statement.payload_attachment_id.id],
        )
        self.env.invalidate_all()
        problems = statement._verify_integrity()
        self.assertTrue(any("payload was changed" in text for text in problems))
        # a broken chain
        cr.execute(
            "UPDATE l10n_de_euer_statement SET prev_hash = %s WHERE id = %s",
            ["1" * 64, statement.id],
        )
        self.env.invalidate_all()
        problems = statement._verify_integrity()
        self.assertTrue(any("previous hash" in text for text in problems))
        self.assertTrue(any("chain hash does not follow" in text for text in problems))

    def test_gt04_chain_per_business(self):
        """GT-04: a branch that is a business of its own has its own chain."""
        self.finalized()
        own = self.env["res.company"].create(
            {
                "name": "Own Business",
                "parent_id": self.company.id,
                "l10n_de_euer_separate_business": True,
            }
        )
        self.env.user.company_ids |= own
        statement = self.finalize(self.statement(company=own))
        self.assertEqual(statement.chain_index, 1)
        self.assertEqual(statement.prev_hash, "0" * 64)

    # ------------------------------------------------------------ GT-05
    def test_gt05_finalised_data_cannot_change(self):
        """GT-05: statement, values, slices, run, payload, asset years,
        profit corrections, questionnaire and bank reconciliation of a
        finalised statement can be neither changed nor deleted, also not by
        an administrator, with ``sudo()`` or the internal context keys."""
        asset = self.asset()
        statement = self.finalized()
        line = statement.line_ids.filtered(lambda line: line.code == KU)
        one = statement.run_id.slice_ids[:1]
        answer = statement.scope_answer_ids[:1]
        balance = statement.cash_balance_ids[:1]
        receipt = self.env["ir.attachment"].create({"name": "receipt", "raw": b"x"})
        adjustment = statement.adjustment_ids[:1]
        self.assertTrue(adjustment)
        payload = statement.payload_attachment_id
        year_line = asset.year_ids.filtered(lambda line: line.year == 2026)
        before = statement.payload_sha256
        changes = [
            (statement, {"profit": 1.0}),
            (statement, {"state": "draft"}),
            (statement, {"correction_reason": "other"}),
            (statement, {"payload_sha256": "0" * 64}),
            (line, {"amount": 1.0}),
            (line, {"source": "override", "override_amount": 5.0}),
            (one, {"amount": 1.0}),
            (statement.run_id, {"hash": "other"}),
            (answer, {"answer": "yes"}),
            (balance, {"balance_statement": 1.0}),
            (balance, {"attachment_ids": [Command.link(receipt.id)]}),
            (adjustment, {"reason": "other"}),
            (adjustment, {"days_8h": 3}),
            (payload, {"raw": b"{}"}),
            (payload, {"name": "other.json"}),
            (year_line, {"depreciation": 1.0}),
        ]
        for record, vals in changes:
            for variant in (
                record,
                record.sudo(),
                record.sudo().with_context(**INTERNAL),
            ):
                with self.subTest(model=record._name, vals=vals, su=variant.env.su):
                    with self.assertRaises(UserError):
                        variant.write(vals)
        for record in (
            statement,
            line,
            one,
            statement.run_id,
            answer,
            balance,
            adjustment,
            payload,
            year_line,
        ):
            with self.subTest(model=record._name):
                with self.assertRaises(UserError):
                    record.sudo().with_context(**INTERNAL).unlink()
        with self.assertRaises(UserError):
            self.correction(statement, "meals", days_8h=1)
        with self.assertRaises(UserError):
            self.env["l10n_de_euer.cash.slice"].sudo().with_context(**INTERNAL).create(
                {
                    "run_id": statement.run_id.id,
                    "company_id": self.company.id,
                    "date": "2026-12-31",
                    "attribution_year": 2026,
                    "kind": "non_cash",
                }
            )
        with self.assertRaises(UserError):
            self.env["l10n_de_euer.statement.line"].sudo().with_context(
                **INTERNAL
            ).create({**line.copy_data()[0], "statement_id": statement.id})
        with self.assertRaises(UserError):
            self.env["l10n_de_euer.cash.balance"].sudo().create(
                {"statement_id": statement.id, "journal_id": self.cash_journal.id}
            )
        # moving the payload between database and file store does not change it
        payload._migrate()
        self.assertEqual(
            hashlib.sha256(payload.raw).hexdigest(), statement.payload_sha256
        )
        # a new calculation of the year leaves the finalised run alone
        self.Run._compute_year(self.company, 2026)
        self.assertTrue(statement.run_id.exists())
        self.assertEqual(statement.payload_sha256, before)
        self.assertEqual(statement._verify_integrity(), [])
        # the submission can be entered, with change log (G-06)
        # records created in the same transaction are not tracked: end it
        self.env.flush_all()
        self.env.cr.flush()
        tracked = statement.with_context(tracking_disable=False, mail_notrack=False)
        tracked.write(
            {"submitted_on": "2027-05-02", "submission_reference": "et-2026-0001"}
        )
        self.env.flush_all()
        self.env.cr.flush()
        values = statement.message_ids.tracking_value_ids.filtered(
            lambda value: value.field_id.name == "submission_reference"
        )
        self.assertTrue(values)
        self.assertEqual(statement._verify_integrity(), [])

    # ------------------------------------------------------------ GT-06
    def test_gt06_prerequisites(self):
        """GT-06 (G-05): blockers prevent the finalisation, a warning without
        acknowledgement too; the lock date has to cover 31 December; the
        form year must be released; the statement has to match the current
        bookings."""
        self.year_2026()
        statement = self.statement()
        statement.action_compute()
        with self.assertRaisesRegex(UserError, "C-45"):
            statement.action_finalize()
        self.assertEqual(statement.state, "computed")
        self.assertFalse(statement.payload_attachment_id)
        self.answer(statement)
        with self.assertRaisesRegex(UserError, "C-38"):
            statement.action_finalize()
        self.reconcile_banks(statement)
        # a booking after the computation
        sale = self.sale("2026-06-10", 50)
        self.bank("2026-06-20", 50, matches=[(sale, 50)])
        with self.assertRaisesRegex(UserError, "compute it again"):
            statement.action_finalize()
        statement.action_compute()
        self.reconcile_banks(statement)
        with self.assertRaisesRegex(UserError, "C-70"):
            statement.action_finalize()
        # only the hard lock date: a warning that needs an acknowledgement
        self.company.hard_lock_date = "2026-12-31"
        statement.action_check()
        self.assertEqual(self.results(statement, "C-70").severity, "warning")
        with self.assertRaisesRegex(UserError, "Warning C-70 not acknowledged"):
            statement.action_finalize()
        self.acknowledge(statement)
        with self.assertRaisesRegex(UserError, "C-72"):
            statement.action_finalize()
        self.verify_forms()
        statement.action_finalize()
        self.assertEqual(statement.state, "final")
        self.assertEqual(self.value(statement, KU), D("1050.00"))
        with self.assertRaises(UserError):
            statement.action_finalize()

    def test_gt06_provisional_form_year(self):
        """GT-06 (F-13): a provisional statement on the forms of an earlier
        year cannot be finalised (C-72)."""
        statement = self.statement(2027)
        statement.action_compute()
        self.answer(statement)
        self.lock_year(2027)
        self.verify_forms(2026)
        statement.action_check()
        self.acknowledge(statement)
        with self.assertRaisesRegex(UserError, "C-72"):
            statement.action_finalize()

    def test_gt06_change_log_of_choices(self):
        """GT-06 (G-06): a computation with other choices than before is
        logged in the chatter of the statement."""
        self.year_2026()
        statement = self.statement()
        statement.action_compute()
        self.company.l10n_de_euer_entertainment_mode = "split"
        statement.action_compute()
        self.assertEqual(statement.wahlrechte_json["entertainment_mode"], "split")
        bodies = statement.message_ids.mapped("body")
        # the change log shows the labels of the choices, not their keys
        self.assertTrue(
            any(
                "Business entertainment: Separate Accounts (70 % and 30 % Booked "
                "Separately) → Split 70/30 Automatically" in str(b)
                for b in bodies
            ),
            bodies,
        )

    # ------------------------------------------------------------ GT-07
    def test_gt07_correction(self):
        """GT-07: a correction is a new version with a reason, the
        differences to the previous version are reported; the previous
        version stays unchanged and readable and is superseded when the
        correction is finalised."""
        statement = self.finalized()
        amounts = {line.code: line.amount for line in statement.line_ids}
        payload_hash = statement.payload_sha256
        Wizard = self.env["l10n_de_euer.statement.correction"]
        with self.assertRaises(UserError):
            statement._create_correction(" ")
        with self.assertRaises(UserError):
            self.Statement.create(
                {
                    "company_id": self.company.id,
                    "year": 2026,
                    "version": 2,
                    "correction_of_id": statement.id,
                    "correction_reason": "by hand",
                }
            )
        action = Wizard.create(
            {"statement_id": statement.id, "reason": "Meals of a second trip"}
        ).action_create()
        correction = self.Statement.browse(action["res_id"])
        self.assertEqual(correction.version, 2)
        self.assertEqual(correction.correction_of_id, statement)
        self.assertEqual(correction.correction_reason, "Meals of a second trip")
        self.assertEqual(correction.state, "draft")
        self.assertEqual(statement.state, "final")
        self.assertEqual(statement.superseded_by_id, correction)
        # the correction starts with the previous version
        self.assertEqual(
            {line.code: line.amount for line in correction.line_ids}, amounts
        )
        self.assertEqual(correction.adjustment_ids.mapped("kind"), ["meals"])
        self.assertTrue(all(correction.cash_balance_ids.mapped("entered_on")))
        self.assertFalse(correction.scope_confirmed_on)
        self.assertEqual(
            set(correction.scope_answer_ids.mapped("proposal_source")),
            {"previous_version"},
        )
        self.assertEqual(correction._get_correction_diff(), [])
        with self.assertRaises(UserError):
            statement._create_correction("Another correction")
        # the change
        correction.adjustment_ids.days_8h = 3
        correction.action_compute()
        diff = {row["code"]: row for row in correction._get_correction_diff()}
        self.assertEqual((diff[MEALS]["old"], diff[MEALS]["new"]), (1400, 4200))
        self.assertEqual(diff[PROFIT]["difference"], -2800)
        html = str(correction.correction_diff_html)
        self.assertIn("<caption>", html)
        self.assertIn('<th scope="col"', html)
        self.assertEqual(correction.correction_diff_count, len(diff))
        # finalise the correction
        self.finalize(correction)
        self.assertEqual(correction.state, "final")
        self.assertEqual(statement.state, "superseded")
        self.assertEqual(correction.chain_index, 2)
        self.assertEqual(correction.prev_hash, statement.chain_hash)
        payload = self.payload(correction)
        self.assertEqual(payload["correction"]["of_id"], statement.id)
        self.assertEqual(payload["correction"]["reason"], "Meals of a second trip")
        self.assertIn(
            MEALS, [row["code"] for row in payload["correction"]["differences"]]
        )
        # the previous version is unchanged and readable
        self.assertEqual(
            {line.code: line.amount for line in statement.line_ids}, amounts
        )
        self.assertEqual(statement.payload_sha256, payload_hash)
        self.assertEqual(statement._verify_integrity(), [])
        self.assertEqual(correction._verify_integrity(), [])
        with self.assertRaises(UserError):
            statement.sudo().with_context(**INTERNAL).write({"state": "final"})
        with self.assertRaises(UserError):
            statement._create_correction("Correct the old version")
        self.assertEqual(
            self.Statement._get_for_calculation(self.company, 2026), correction
        )

    # ------------------------------------------------------------ GT-14
    def test_gt14_answers_in_the_payload(self):
        """GT-14: the confirmed answers of the questionnaire are part of the
        payload."""
        statement = self.statement()
        self.answer(statement, Q04_LAND="no")
        self.finalize(statement)
        questionnaire = self.payload(statement)["questionnaire"]
        self.assertEqual(questionnaire["confirmed_by_id"], self.env.uid)
        answers = {row["question_code"]: row for row in questionnaire["answers"]}
        self.assertEqual(len(answers), len(scope.QUESTIONS))
        self.assertEqual(answers["Q01_LEGAL_FORM"]["answer"], "yes")
        self.assertEqual(answers["Q04_LAND"]["answer"], "no")
        self.assertTrue(all(row["confirmed_on"] for row in answers.values()))
        values = {row["code"]: row for row in self.payload(statement)["fields"]}
        self.assertEqual(values["EUER.15.120"]["value_text"], "2")

    # ------------------------------------------------------------ GT-15
    def test_gt15_deviation_after_finalisation(self):
        """GT-15 (G-07, G-17): a new calculation of a finalised year is a
        comparison run; a booking posted later shows as C-60 per field, per
        sum of slices and for the bank balance; the finalised statement does
        not change."""
        statement = self.finalized()
        run = statement.run_id
        statement.action_compare()
        self.assertFalse(self.c60(statement))
        self.assertEqual(statement.run_id, run)
        # a late entry of the year through an exception of the lock date
        self.env["account.lock_exception"].create(
            {
                "company_id": self.company.id,
                "fiscalyear_lock_date": "2025-12-31",
                "reason": "credit note found late",
            }
        )
        sale = self.sale("2026-12-20", 300)
        self.bank("2026-12-28", 300, matches=[(sale, 300)])
        statement.action_compare()
        results = self.c60(statement)
        fields_ = {
            self.env["l10n_de_euer.field"].browse(check.res_ids[0]).code: check
            for check in results
            if check.res_model == "l10n_de_euer.field"
        }
        self.assertEqual(set(fields_) >= {KU, INCOME, PROFIT}, True)
        self.assertEqual(fields_[KU].severity, "warning")
        self.assertEqual(fields_[KU].amount, 300.0)
        self.assertIn("slices", fields_[KU].message)
        bank = results.filtered(
            lambda check: check.res_model == "l10n_de_euer.cash.balance"
        )
        self.assertEqual(bank.amount, 300.0)
        self.assertTrue(self.results(statement, "C-39"))
        # the finalised statement and its run are unchanged
        self.assertEqual(statement.state, "final")
        self.assertEqual(statement.run_id, run)
        self.assertTrue(run.locked)
        self.assertEqual(self.value(statement, KU), D("1000.00"))
        self.assertTrue(statement.comparison_run_id)
        self.assertNotEqual(statement.comparison_run_id, run)
        self.assertEqual(statement._verify_integrity(), [])
        # every new calculation shows the deviation, the comparison run is
        # replaced
        comparison = statement.comparison_run_id
        self.Run._compute_year(self.company, 2026)
        self.assertFalse(comparison.exists())
        self.assertTrue(self.c60(statement))

    def test_st_t28_rest_settled_after_finalisation(self):
        """ST-T28: 2026 is finalised with a classified rest (revenue of a
        small business); an invoice of 2027 under standard taxation settles
        the rest. The new calculation of 2026 shows the deviation C-60 (Kz
        111 −300.00, Kz 112 and 140 +300.00); the finalised statement stays."""
        bank = self.bank("2026-12-20", 300, rest="receivable", partner=self.customer)
        rest_line = bank.move_id.line_ids.filtered(
            lambda line: line.account_id == self.receivable
        )
        self.env["l10n_de_euer.residual.classification"].create(
            {
                "company_id": self.company.id,
                "source_line_id": rest_line.id,
                "kind": "revenue",
                "category_id": self.category("REV_KU").id,
                "vat_regime": "ku",
            }
        )
        statement = self.finalize(self.statement())
        self.assertEqual(self.value(statement, KU), D("300.00"))
        payload = self.payload(statement)
        self.assertEqual(payload["classifications"][0]["kind"], "revenue")
        # standard taxation from 2027, the invoice settles the rest
        Period = self.env["l10n_de_euer.vat.period"]
        Period.search([("company_id", "=", self.company.id)]).date_to = "2026-12-31"
        Period.create(
            {
                "company_id": self.company.id,
                "date_from": "2027-01-01",
                "regime": "regular",
                "vat_return_period": "monthly",
            }
        )
        invoice = self.sale("2027-01-10", 252.10, account="revenue_19", taxes=["ust19"])
        self.assertAlmostEqual(invoice.amount_total, 300.0)
        self.reconcile(invoice, rest_line)
        statement.action_compare()
        deviations = {
            self.env["l10n_de_euer.field"].browse(check.res_ids).code: check.amount
            for check in self.c60(statement)
            if check.res_model == "l10n_de_euer.field"
        }
        self.assertEqual(deviations[KU], -300.0)
        self.assertEqual(deviations["EUER.20.112"], 252.10)
        self.assertEqual(deviations["EUER.20.140"], 47.90)
        self.assertNotIn(PROFIT, deviations)
        self.assertEqual(self.value(statement, KU), D("300.00"))
        self.assertEqual(statement._verify_integrity(), [])

    # ------------------------------------------------------------ GT-16
    def snapshot(self):
        """Count and full rows of all journal entries and items."""
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
        """GT-16 (B-01): computation, override, checks, finalisation,
        comparison, integrity check and correction write nothing into
        account_move or account_move_line, also with hashed entries."""
        self.company_data["default_journal_sale"].restrict_mode_hash_table = True
        sale, _bill = self.year_2026()
        self.assertTrue(sale.inalterable_hash)
        self.lock_year()
        snapshot = self.snapshot()
        statement = self.statement()
        statement.action_compute()
        line = statement.line_ids.filtered(lambda line: line.code == KU)
        line.write(
            {
                "source": "override",
                "override_amount": 1000.0,
                "override_reason": "Same as computed, entered for the test",
            }
        )
        self.finalize(statement)
        statement.action_compare()
        statement.action_check_integrity()
        correction = statement._create_correction("Test of the correction")
        correction.action_compute()
        self.assertEqual(self.snapshot(), snapshot)


@tagged("post_install_l10n", "post_install", "-at_install")
class TestStatementFinalSkr03(FinalScenarios, AccountTestInvoicingCommon):
    allow_inherited_tests_method = True
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_finalisation()


@tagged("post_install_l10n", "post_install", "-at_install")
class TestStatementFinalSkr04(FinalScenarios, AccountTestInvoicingCommon):
    allow_inherited_tests_method = True
    CHART = "de_skr04"
    ACCOUNTS = SKR04_ACCOUNTS
    TAXES = SKR04_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr04")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_finalisation()


@tagged("post_install_l10n", "post_install", "-at_install")
class TestStatementFinalRights(StatementCommon, AccountTestInvoicingCommon):
    """GT-13: only a manager finalises and corrects; a bookkeeper compares
    and enters the submission; an auditor checks the integrity."""

    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_finalisation()
        users = {}
        for login, groups in (
            ("final_bookkeeper", "account.group_account_user"),
            ("final_manager_only", "account.group_account_manager"),
            ("final_auditor", "account.group_account_readonly"),
        ):
            users[login] = new_test_user(
                cls.env,
                login=login,
                groups=groups,
                company_id=cls.company.id,
                company_ids=[Command.set(cls.company.ids)],
            )
        cls.bookkeeper = users["final_bookkeeper"]
        cls.manager = users["final_manager_only"]
        cls.auditor = users["final_auditor"]

    def test_gt13_finalise_and_correct_only_as_manager(self):
        sale = self.sale("2026-03-10", 1000)
        self.bank("2026-03-20", 1000, matches=[(sale, 1000)])
        statement = self.Statement.with_user(self.bookkeeper).create(
            {"company_id": self.company.id, "year": 2026}
        )
        statement.action_compute()
        statement = statement.with_env(self.env)
        self.reconcile_banks(statement)
        self.answer(statement)
        self.lock_year()
        self.verify_forms()
        statement.action_check()
        self.acknowledge(statement)
        with self.assertRaises(UserError):
            statement.with_user(self.bookkeeper).action_finalize()
        with self.assertRaises(UserError):
            statement.with_user(self.auditor).action_finalize()
        self.assertEqual(statement.state, "computed")
        self.assertFalse(self.manager.has_group("account.group_account_user"))
        statement.with_user(self.manager).action_finalize()
        self.assertEqual(statement.state, "final")
        self.assertEqual(statement.finalized_by_id, self.manager)
        # a bookkeeper compares and enters the submission, but corrects not
        as_bookkeeper = statement.with_user(self.bookkeeper)
        as_bookkeeper.action_compare()
        as_bookkeeper.submission_reference = "et-2026-0001"
        with self.assertRaises(UserError):
            as_bookkeeper.action_open_correction_wizard()
        with self.assertRaises(UserError):
            as_bookkeeper._create_correction("Not allowed")
        # an auditor reads the payload and checks the integrity
        as_auditor = statement.with_user(self.auditor)
        self.assertTrue(as_auditor.payload_attachment_id.raw)
        as_auditor.action_check_integrity()
        self.assertIn("unchanged", statement.message_ids[0].body)
        with self.assertRaises(AccessError):
            as_auditor.action_compare()
        with self.assertRaises(AccessError):
            as_auditor.submission_reference = "other"
        # the manager corrects
        correction = statement.with_user(self.manager)._create_correction(
            "Late credit note"
        )
        self.assertEqual(correction.version, 2)
