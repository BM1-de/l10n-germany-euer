# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""UT-PERF-1: run time of a year with 20,000 partial reconciliations (B-15).

Not part of the standard tests (tag ``l10n_de_euer_perf``): the limit of
120 s applies to the reference runner of the CI. The bookings are cloned
with SQL from one paid invoice with VAT (lines with their taxes and tags),
so creating them does not dominate the run; an older history of the same
size must not slow the year down (spec 5.1). Run it with ``--test-tags
l10n_de_euer_perf``; the times of extraction, engine and storing are logged.
With the environment variable ``L10N_DE_EUER_PERF_FILE`` the run times are
written to that file (JSON), so the CI can compare them with the median of
the last five runs (B-15, 150 %).
"""

import json
import logging
import os
import time

from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

from .scenario_common import SKR03_ACCOUNTS, SKR03_TAXES, ScenarioCommon, euro

_logger = logging.getLogger(__name__)

PAIRS = 20000
HISTORY = 20000
LIMIT = 120


@tagged(
    "post_install_l10n", "post_install", "-at_install", "-standard", "l10n_de_euer_perf"
)
class TestPerformance(ScenarioCommon, AccountTestInvoicingCommon):
    CHART = "de_skr03"
    ACCOUNTS = SKR03_ACCOUNTS
    TAXES = SKR03_TAXES

    @classmethod
    @AccountTestInvoicingCommon.setup_country("de")
    @AccountTestInvoicingCommon.setup_chart_template("de_skr03")
    def setUpClass(cls):
        super().setUpClass()
        cls.setup_euer()

    def _columns(self, table, exclude):
        self.env.cr.execute(
            """
            SELECT column_name FROM information_schema.columns
             WHERE table_name = %s AND column_name <> ALL(%s)
             ORDER BY ordinal_position
            """,
            [table, list(exclude)],
        )
        return [row[0] for row in self.env.cr.fetchall()]

    def _clone(self, moves, partials, count):
        """Clone ``moves`` with their lines, the taxes and tags of the lines
        and ``partials`` ``count`` times."""
        cr = self.env.cr
        self.env.flush_all()
        cr.execute("DROP TABLE IF EXISTS euer_move_map, euer_line_map")
        cr.execute(
            """
            CREATE TEMP TABLE euer_move_map AS
            SELECT n, m.id AS old_id, nextval('account_move_id_seq') AS new_id
              FROM generate_series(1, %s) n, unnest(%s::int[]) m(id)
            """,
            [count, moves.ids],
        )
        move_cols = self._columns("account_move", ("id", "name", "statement_line_id"))
        names = ", ".join(move_cols)
        values = ", ".join("m." + column for column in move_cols)
        cr.execute(
            f"""
            INSERT INTO account_move (id, name, {names})
            SELECT c.new_id, m.name || '/' || c.n, {values}
              FROM euer_move_map c JOIN account_move m ON m.id = c.old_id
            """
        )
        cr.execute(
            """
            CREATE TEMP TABLE euer_line_map AS
            SELECT c.n, l.id AS old_id, nextval('account_move_line_id_seq') AS new_id,
                   c.new_id AS move_id
              FROM euer_move_map c JOIN account_move_line l ON l.move_id = c.old_id
            """
        )
        line_cols = self._columns(
            "account_move_line",
            (
                "id",
                "move_id",
                "full_reconcile_id",
                "matching_number",
                "statement_line_id",
            ),
        )
        cr.execute(
            f"""
            INSERT INTO account_move_line (id, move_id, {", ".join(line_cols)})
            SELECT lm.new_id, lm.move_id, {", ".join("l." + c for c in line_cols)}
              FROM euer_line_map lm JOIN account_move_line l ON l.id = lm.old_id
            """
        )
        AML = self.env["account.move.line"]
        for name in ("tax_ids", "tax_tag_ids"):
            field = AML._fields[name]
            cr.execute(
                f"""
                INSERT INTO {field.relation} ({field.column1}, {field.column2})
                SELECT lm.new_id, rel.{field.column2}
                  FROM euer_line_map lm
                  JOIN {field.relation} rel ON rel.{field.column1} = lm.old_id
                """
            )
        partial_cols = self._columns(
            "account_partial_reconcile",
            ("id", "debit_move_id", "credit_move_id", "full_reconcile_id"),
        )
        cr.execute(
            f"""
            INSERT INTO account_partial_reconcile
                   (debit_move_id, credit_move_id, {", ".join(partial_cols)})
            SELECT d.new_id, c.new_id, {", ".join("p." + c for c in partial_cols)}
              FROM account_partial_reconcile p
              JOIN euer_line_map d ON d.old_id = p.debit_move_id
              JOIN euer_line_map c ON c.old_id = p.credit_move_id AND c.n = d.n
             WHERE p.id = ANY(%s)
            """,
            [partials.ids],
        )
        # statistics of the cloned rows (visible to the own transaction): a
        # test database emptied by earlier tests may tell the planner that
        # the tables are empty, and every query would scan them in a loop
        tables = ["account_move", "account_move_line", "account_partial_reconcile"]
        tables += [AML._fields[name].relation for name in ("tax_ids", "tax_tag_ids")]
        cr.execute(f"ANALYZE {', '.join(tables)}")
        self.env.invalidate_all()

    def _pair(self, year):
        """Invoice with VAT of ``year`` paid by bank, and its partials."""
        invoice = self.sale(f"{year}-03-01", 10, account="revenue_19", taxes=["ust19"])
        bank = self.entry(
            f"{year}-04-01",
            [("bank", 11.90), ("receivable", -11.90)],
            journal=self.bank_journal,
            partner=self.customer,
        )
        self.reconcile(invoice, bank.line_ids.filtered(lambda line: line.credit))
        partials = self.env["account.partial.reconcile"].search(
            [("debit_move_id", "in", invoice.line_ids.ids)]
        )
        return invoice | bank, partials

    def test_ut_perf_1(self):
        self.set_regime("regular", date_from="2010-01-01")
        moves, partials = self._pair(2020)
        start = time.perf_counter()
        self._clone(moves, partials, HISTORY - 1)
        moves, partials = self._pair(2026)
        self._clone(moves, partials, PAIRS - 1)
        _logger.info(
            "UT-PERF-1: %s + %s pairs cloned in %.1f s",
            PAIRS,
            HISTORY,
            time.perf_counter() - start,
        )
        data = self.env["l10n_de_euer.adapter"]._engine_input(self.company, 2026)
        # the history of 2020 is not read for 2026 (spec 5.1)
        self.assertLess(len(data.moves), 2 * PAIRS + 100)
        start = time.perf_counter()
        run = self.Run._compute_year(self.company, 2026)
        duration = time.perf_counter() - start
        _logger.info(
            "UT-PERF-1: %s partials, first run %.1f s (move information created)",
            PAIRS,
            duration,
        )
        self.assertEqual(
            run._get_field_amounts(),
            {"EUER.20.112": euro(10 * PAIRS), "EUER.20.140": euro(1.9 * PAIRS)},
        )
        start = time.perf_counter()
        run = self.Run._compute_year(self.company, 2026)
        second = time.perf_counter() - start
        _logger.info("UT-PERF-1: %s partials, second run %.1f s", PAIRS, second)
        self._write_times(duration, second)
        self.assertLess(second, LIMIT)

    @staticmethod
    def _write_times(first, second):
        path = os.environ.get("L10N_DE_EUER_PERF_FILE")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as file:
            json.dump(
                {
                    "pairs": PAIRS,
                    "first": round(first, 3),
                    "second": round(second, 3),
                    "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                },
                file,
            )
