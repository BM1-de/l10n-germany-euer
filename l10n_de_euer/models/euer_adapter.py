# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Phase A: extraction of the bookings for the engine (spec 5.3, B-14).

The adapter is the only place that knows Odoo's accounting tables. It reads
moves, lines, partial reconciliations, taxes and the EÜR data of the module
with SQL and plain ORM reads and turns them into the data classes of
:mod:`.engine.model`. Access to unstable fields (statement lines, payments,
payment orders of OCA ``account_payment_order``, bank data of statement
imports) is kept here. It never writes into the accounting tables (B-01).

Differences to the extraction of ``odoo_technik.md`` 5.2 (spec 5.3): roles,
reversal pairs and move classes are computed by the engine
(:mod:`.engine.graph`), which binds role R to balance sheet accounts and
excludes reversal pairs only if they are reconciled with each other only;
account codes are read per business (``code_store`` of the main company).
"""

import datetime
import json
from collections import defaultdict
from decimal import Decimal
from fractions import Fraction

from odoo import api, fields, models
from odoo.fields import Domain
from odoo.tools import date_utils

from .engine import model as m
from .engine import revenue
from .euer_move_info import DOCUMENT_TYPES

MONEY_JOURNAL_TYPES = ("bank", "cash", "credit")
#: keys of ``transaction_details`` of statement imports holding the bank date
BANK_DATE_KEYS = (
    "booking_date",
    "bookingDate",
    "BookgDt",
    "value_date",
    "valueDate",
    "ValDt",
)


def _fraction(value):
    """Exact fraction of a float field value (rates, factors)."""
    return Fraction(str(value or 0))


def _date(value):
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    return fields.Date.to_date(str(value)[:10])


class L10nDeEuerAdapter(models.AbstractModel):
    _name = "l10n_de_euer.adapter"
    _description = "EÜR Extraction of the Bookings"

    # ================================================================ input
    @api.model
    def _engine_input(self, business, year):
        """Everything a run of ``business`` needs (phases 0 and A).

        The settings and status periods belong to the main company, which a
        branch that is a business of its own need not have selected: they
        are read as superuser.
        """
        root = business.sudo().root_id
        companies = business._l10n_de_euer_business_companies()
        self.env.flush_all()
        moves = self._read_moves(companies, year)
        move_ids = list(moves)
        lines = self._read_lines(move_ids)
        line_ids = [line["id"] for line in lines]
        partials = self._read_partials(companies, line_ids)
        tax_ids, repartition_ids = self._tax_references(lines)
        taxes, repartitions, children = self._read_taxes(tax_ids, repartition_ids)
        journals = self._money_journals(companies)
        account_ids = {line["account_id"] for line in lines}
        accounts = self._read_accounts(
            root, account_ids, journals, self._outstanding_accounts(companies, journals)
        )
        partner_ids = {line["partner_id"] for line in lines if line["partner_id"]}
        partner_ids |= {
            move["partner_id"] for move in moves.values() if move["partner_id"]
        }
        infos = self._read_infos(moves)
        bank_dates = self._bank_dates(moves)
        order_dates = self._payment_order_dates(moves, lines)
        revaluations = self._revaluations(companies, moves)
        shifted = self._lock_date_shifts(companies, moves)
        openings = set(companies.account_opening_move_id.ids)
        decisions, confirmations = self._ten_day_decisions(business)
        return m.EngineInput(
            accounts=tuple(accounts),
            categories=self._categories(),
            mappings=self._mappings(),
            moves=tuple(
                self._move(
                    move,
                    infos.get(move["id"], m.NO_INFO),
                    move["id"] in openings,
                    move["id"] in revaluations,
                    bank_dates.get(move["id"]),
                    order_dates.get(move["id"]),
                    shifted.get(move["id"]),
                )
                for move in moves.values()
            ),
            lines=tuple(self._line(line, children) for line in lines),
            partials=tuple(partials),
            taxes=tuple(taxes),
            repartitions=tuple(repartitions),
            partners=tuple(self._partners(partner_ids)),
            vat_periods=tuple(self._vat_periods(root)),
            parameters=self._parameters(),
            opening_items=tuple(self._opening_items(business)),
            classifications=tuple(self._classifications(business)),
            ten_day_decisions=tuple(decisions),
            gift_lines=tuple(self._gift_lines(moves)),
            transfer_confirmations=frozenset(confirmations),
            asset_amounts=tuple(self._asset_amounts(business, year)),
            adjustment_amounts=tuple(self._adjustment_amounts(business, year)),
            turnover_amounts=tuple(self._turnover_amounts(business, year)),
            founding_year=self._founding_year(root),
            activity_start=root.l10n_de_euer_activity_start or None,
            settings=m.Settings(
                offsetting_is_payment=root.l10n_de_euer_offsetting_is_payment,
                fx_mode=root.l10n_de_euer_fx_mode or "separate",
                entertainment_mode=root.l10n_de_euer_entertainment_mode or "separate",
                default_regime=m.REGULAR,
            ),
            company_ids=frozenset(companies.ids),
        )

    # ------------------------------------------------------------ phase F
    @api.model
    def _asset_amounts(self, business, year):
        """Field amounts of the fixed asset register (phase F, spec 5.8).

        :class:`~.engine.model.FieldAmount` records of ``year`` with
        ``source="asset"``: the input fields of Anlage AVEÜR (their transfers
        into Anlage EÜR are formulas), low-value assets, withdrawals and
        contributions of assets and confirmed use withdrawals. Purchases and
        depreciation bookings on accounts of role fixed asset or depreciation
        are never realised from money, so nothing counts twice.
        """
        statement = self.env["l10n_de_euer.statement"]._get_for_calculation(
            business, year
        )
        contributions = self.env["l10n_de_euer.asset"]._get_field_contributions(
            business,
            year,
            exclude_private_use=statement.adjustment_ids.asset_id,
        )
        return [
            m.FieldAmount(
                year=year,
                field=code,
                amount=int(value * 100),
                source="asset",
                reference="asset_register",
            )
            for code, value in sorted(contributions.items())
            if value
        ]

    @api.model
    def _adjustment_amounts(self, business, year):
        """Profit corrections of the statement (phase F, spec 5.8).

        :class:`~.engine.model.FieldAmount` records of ``year`` with
        ``source="adjustment"``, one per correction and field, including the
        effect on withdrawals and contributions (Kz 122/123). The statement
        is the one of the context (its "compute"), else the valid statement
        of the business and year. A use withdrawal of an asset replaces the
        private use of the register (:meth:`_asset_amounts`).
        """
        statement = self.env["l10n_de_euer.statement"]._get_for_calculation(
            business, year
        )
        result = []
        for adjustment in statement.adjustment_ids.sorted("id"):
            for code, value in sorted(adjustment._get_amounts().items()):
                result.append(
                    m.FieldAmount(
                        year=year,
                        field=code,
                        amount=int((value * 100).to_integral_value()),
                        source="adjustment",
                        reference=f"{adjustment._name},{adjustment.id}",
                    )
                )
        return result

    @api.model
    def _turnover_amounts(self, business, year):
        """Parts of the total turnover without money (R-27) of ``year`` and
        of the previous year (its limit check needs the same total).

        :class:`~.engine.model.TurnoverAmount` records: the VAT base of
        taxable free supplies and the value of income in kind from the
        profit corrections, and the VAT base of confirmed use withdrawals of
        the asset register that no correction takes over. A free supply
        without input tax deduction is not taxable and has no base, so it
        never counts (UStAE 19.2 (2) sentences 2 and 4).
        """
        Statement = self.env["l10n_de_euer.statement"]
        Year = self.env["l10n_de_euer.asset.year"].sudo()
        result = []
        for one_year in (year - 1, year):
            statement = Statement._get_for_calculation(business, one_year)
            for adjustment in statement.adjustment_ids.sorted("id"):
                amount = Decimal(str(adjustment.turnover_amount or 0))
                if not amount:
                    continue
                day = adjustment.date
                result.append(
                    m.TurnoverAmount(
                        year=one_year,
                        amount=int((amount * 100).to_integral_value()),
                        date=day if day and day.year == one_year else None,
                        source="adjustment",
                        reference=f"{adjustment._name},{adjustment.id}",
                    )
                )
            taken = statement.adjustment_ids.asset_id
            lines = Year.search(
                Domain("company_id", "=", business.id)
                & Domain("year", "=", one_year)
                & Domain("private_use_decision", "=", "confirmed")
                & Domain("private_use_confirmed_base", "!=", 0)
                & Domain("asset_id", "not in", taken.ids),
                order="id",
            )
            for line in lines:
                amount = Decimal(str(line.private_use_confirmed_base))
                result.append(
                    m.TurnoverAmount(
                        year=one_year,
                        amount=int((amount * 100).to_integral_value()),
                        source="asset",
                        reference=f"{line._name},{line.id}",
                    )
                )
        return result

    # ------------------------------------------------------------ moves
    @api.model
    def _read_moves(self, companies, year):
        """Entries of the business the run of ``year`` needs (spec 5.1).

        Posted entries of the years ``year - 2`` to ``year + 1`` (the review
        of the turn of the previous year looks one year further back) and
        everything reachable from them over partial reconciliations,
        exchange difference entries and reversals: an invoice of 2020 paid
        in the year is read, an unrelated entry of 2020 is not. The run does
        not grow with the history of the business. Drafts of the year or
        reconciled drafts are read for C-13/C-14.

        The partials are joined by debit and by credit line separately (no
        ``OR`` join) and the reached entries are joined, not looked up with
        ``IN`` next to an ``OR``: the indexes are used whatever the
        statistics say, and the run grows linearly with the entries.
        PostgreSQL overestimates the recursive query by far; its JIT
        compilation would cost more than the query itself, so it is switched
        off for this query only.
        """
        columns = """
            m.id, m.name, m.date, m.state, m.company_id, m.move_type,
            m.partner_id, m.reversed_entry_id,
            (m.tax_cash_basis_rec_id IS NOT NULL
             OR m.tax_cash_basis_origin_move_id IS NOT NULL) AS caba,
            m.statement_line_id, m.invoice_date, m.invoice_date_due,
            COALESCE(m.taxable_supply_date, m.delivery_date) AS supply_date,
            COALESCE(j.l10n_de_euer_private_account, FALSE) AS private_journal
        """
        self.env.cr.execute("SET LOCAL jit = off")
        self.env.cr.execute(
            f"""
            WITH RECURSIVE reached(id) AS (
                SELECT m.id
                  FROM account_move m
                 WHERE m.company_id = ANY(%(companies)s)
                   AND m.state = 'posted'
                   AND m.date BETWEEN %(seed_start)s AND %(seed_end)s
                UNION
                SELECT linked.id
                  FROM reached
                 CROSS JOIN LATERAL (
                       SELECT other.move_id AS id
                         FROM account_move_line l
                         JOIN account_partial_reconcile p
                           ON p.debit_move_id = l.id
                         JOIN account_move_line other
                           ON other.id = p.credit_move_id
                        WHERE l.move_id = reached.id
                       UNION ALL
                       SELECT other.move_id
                         FROM account_move_line l
                         JOIN account_partial_reconcile p
                           ON p.credit_move_id = l.id
                         JOIN account_move_line other
                           ON other.id = p.debit_move_id
                        WHERE l.move_id = reached.id
                       UNION ALL
                       SELECT p.exchange_move_id
                         FROM account_move_line l
                         JOIN account_partial_reconcile p
                           ON p.debit_move_id = l.id
                        WHERE l.move_id = reached.id
                          AND p.exchange_move_id IS NOT NULL
                       UNION ALL
                       SELECT p.exchange_move_id
                         FROM account_move_line l
                         JOIN account_partial_reconcile p
                           ON p.credit_move_id = l.id
                        WHERE l.move_id = reached.id
                          AND p.exchange_move_id IS NOT NULL
                       UNION ALL
                       SELECT r.reversed_entry_id
                         FROM account_move r
                        WHERE r.id = reached.id
                       UNION ALL
                       SELECT r.id
                         FROM account_move r
                        WHERE r.reversed_entry_id = reached.id
                 ) linked
                 WHERE linked.id IS NOT NULL
            )
            SELECT {columns}
              FROM reached
              JOIN account_move m ON m.id = reached.id
              JOIN account_journal j ON j.id = m.journal_id
             WHERE m.company_id = ANY(%(companies)s)
               AND m.state = 'posted'
            UNION ALL
            SELECT {columns}
              FROM account_move m
              JOIN account_journal j ON j.id = m.journal_id
             WHERE m.company_id = ANY(%(companies)s)
               AND m.state = 'draft'
               AND (m.date BETWEEN %(start)s AND %(end)s
                    OR EXISTS (
                       SELECT 1
                         FROM account_move_line l
                         JOIN account_partial_reconcile p
                           ON p.debit_move_id = l.id
                        WHERE l.move_id = m.id)
                    OR EXISTS (
                       SELECT 1
                         FROM account_move_line l
                         JOIN account_partial_reconcile p
                           ON p.credit_move_id = l.id
                        WHERE l.move_id = m.id))
             ORDER BY 1
            """,
            {
                "companies": companies.ids,
                "start": datetime.date(year, 1, 1),
                "end": datetime.date(year, 12, 31),
                "seed_start": datetime.date(year - 2, 1, 1),
                "seed_end": datetime.date(year + 1, 12, 31),
            },
        )
        result = {row["id"]: row for row in self.env.cr.dictfetchall()}
        self.env.cr.execute("SET LOCAL jit TO DEFAULT")
        return result

    @api.model
    def _move(
        self, row, info, opening, revaluation, bank_date, order_date, shifted=None
    ):
        return m.Move(
            id=row["id"],
            name=row["name"] or "",
            date=row["date"],
            state=row["state"],
            company_id=row["company_id"],
            move_type=row["move_type"],
            partner_id=row["partner_id"],
            reversed_entry_id=row["reversed_entry_id"],
            caba=row["caba"],
            opening=opening,
            statement=bool(row["statement_line_id"]),
            invoice_date=row["invoice_date"],
            invoice_date_due=row["invoice_date_due"],
            supply_date=row["supply_date"],
            private_journal=row["private_journal"],
            revaluation=revaluation,
            bank_date=bank_date,
            payment_order_date=order_date,
            lock_date_shift=shifted or None,
            info=info,
        )

    @api.model
    def _read_infos(self, moves):
        """EÜR data of the entries; documents get their VAT treatment stored."""
        Info = self.env["l10n_de_euer.move.info"].sudo()
        documents = self.env["account.move"].browse(
            [
                move_id
                for move_id, move in moves.items()
                if move["state"] == "posted" and move["move_type"] in DOCUMENT_TYPES
            ]
        )
        infos = Info._get_or_create(documents).sudo()
        infos |= Info.search(Domain("move_id", "in", list(moves)))
        infos._update_regime(tracking=False)
        result = {}
        for info in infos:
            result[info.move_id.id] = m.MoveInfo(
                vat_regime=info.vat_regime or None,
                cash_date=info.cash_date or None,
                cash_date_reason=info.cash_date_reason or None,
                economic_year=info.economic_year or None,
                due_date=info.due_date or None,
                vat_period=info.vat_period or None,
                regular_tax_rate=(
                    Fraction(info.regular_tax_rate) if info.regular_tax_rate else None
                ),
                gift_bulk=info.gift_bulk,
                gift_recipient_id=info.gift_recipient_id.id or None,
                gift_date=info.gift_date or None,
            )
        return result

    @api.model
    def _bank_dates(self, moves):
        """Bank date of statement entries from the data of the import (C-12)."""
        statement_moves = [
            move_id for move_id, move in moves.items() if move["statement_line_id"]
        ]
        if not statement_moves:
            return {}
        self.env.cr.execute(
            """
            SELECT move_id, transaction_details
              FROM account_bank_statement_line
             WHERE move_id = ANY(%s) AND transaction_details IS NOT NULL
            """,
            [statement_moves],
        )
        result = {}
        for move_id, details in self.env.cr.fetchall():
            day = self._bank_date_from_details(details)
            if day:
                result[move_id] = day
        return result

    @api.model
    def _bank_date_from_details(self, details):
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except ValueError:
                return None
        if not isinstance(details, dict):
            return None
        for key in BANK_DATE_KEYS:
            value = details.get(key)
            if isinstance(value, dict):
                value = value.get("Dt") or value.get("date")
            if value:
                try:
                    return _date(value)
                except ValueError:
                    continue
        return None

    @api.model
    def _lock_dates(self, companies):
        """Lock dates per company: ``{company id: (lock, tax lock)}``.

        Odoo applies the lock dates of the company and of all its parents
        (``parent_ids``): a lock date set on the main company locks its
        branches, too. ``lock`` is the latest fiscal year or hard lock date,
        ``tax lock`` the latest tax lock date (exceptions are not counted:
        they are temporary, C-39).
        """
        result = {}
        for company in companies.sudo():
            chain = company.with_context(active_test=False).parent_ids
            dates = chain.mapped("fiscalyear_lock_date") + chain.mapped(
                "hard_lock_date"
            )
            result[company.id] = (
                max(filter(None, dates), default=None),
                max(filter(None, chain.mapped("tax_lock_date")), default=None),
            )
        return result

    @api.model
    def _lock_date_shifts(self, companies, moves):
        """Statement entries Odoo may have moved out of a locked period (B-12).

        Posting a statement line of a locked period, ``_get_accounting_date``
        moves it without a trace: to the day after the lock date, the end of
        that month, the 31 December of that year or the day of posting,
        depending on the journal sequence and today. The lock date is the
        fiscal year or hard lock date, for entries with taxes also the tax
        lock date, of the company or one of its parents. Without a bank date
        in the import data (C-12) these dates are the only hint until the
        bank reconciliation on 31 December (C-38). An entry dated on the day
        of its posting is reported only if a later line of the same import
        is dated earlier (a daily bank feed is no shift). Returns ``{move id:
        lock date}``.
        """
        locks = self._lock_dates(companies)
        earliest = {}
        for company_id, (lock, tax_lock) in locks.items():
            dates = [day for day in (lock, tax_lock) if day]
            if dates:
                earliest[company_id] = min(dates)
        statement_moves = [
            move_id
            for move_id, move in moves.items()
            if move["statement_line_id"]
            and move["state"] == "posted"
            and move["company_id"] in earliest
            and move["date"] > earliest[move["company_id"]]
        ]
        if not statement_moves:
            return {}
        taxes = self.env["account.move.line"]._fields["tax_ids"]
        self.env.cr.execute(
            f"""
            SELECT m.id, m.create_date::date AS created,
                   EXISTS (
                       SELECT 1 FROM account_move_line l
                        WHERE l.move_id = m.id
                          AND (l.tax_repartition_line_id IS NOT NULL
                               OR EXISTS (SELECT 1 FROM {taxes.relation} rel
                                           WHERE rel.{taxes.column1} = l.id))
                   ) AS has_tax,
                   EXISTS (
                       SELECT 1 FROM account_move n
                        WHERE n.journal_id = m.journal_id
                          AND n.statement_line_id IS NOT NULL
                          AND n.id > m.id
                          AND n.date < m.date
                          AND n.create_date BETWEEN m.create_date
                              AND m.create_date + INTERVAL '10 minutes'
                   ) AS out_of_order
              FROM account_move m
             WHERE m.id = ANY(%s)
            """,
            [statement_moves],
        )
        result = {}
        for move_id, created, has_tax, out_of_order in self.env.cr.fetchall():
            move = moves[move_id]
            lock, tax_lock = locks.get(move["company_id"], (None, None))
            if has_tax and tax_lock and (not lock or tax_lock > lock):
                lock = tax_lock
            if not lock or move["date"] <= lock:
                continue
            first = lock + datetime.timedelta(days=1)
            candidates = {
                first,
                date_utils.end_of(first, "month"),
                datetime.date(first.year, 12, 31),
            }
            if move["date"] in candidates or (move["date"] == created and out_of_order):
                result[move_id] = lock
        return result

    @api.model
    def _unreconciled_statement_lines(self, companies, date_from, date_to):
        """Posted bank statement lines of a period that are not reconciled
        (C-15); unstable field ``is_reconciled`` (B-14)."""
        return (
            self.env["account.bank.statement.line"]
            .sudo()
            .search(
                Domain("company_id", "in", companies.ids)
                & Domain("date", ">=", date_from)
                & Domain("date", "<=", date_to)
                & Domain("is_reconciled", "=", False)
                & Domain("state", "=", "posted")
            )
        )

    @api.model
    def _payment_order_dates(self, moves, lines):
        """Date an OCA payment order was handed to the bank (C-23 proposal).

        Only for money leaving the business from 1 to 5 January. Found over
        the partial reconciliations of the entry: the payment entry of the
        order or the invoice line of a payment line of the order. Cancelled
        orders keep their dates and lines and are left out; of several
        valid orders the latest one counts (an order handed over again).
        """
        if "account.payment.order" not in self.env:
            return {}
        candidates = [
            move_id
            for move_id, move in moves.items()
            if move["state"] == "posted"
            and move["date"].month == 1
            and move["date"].day <= 5
        ]
        if not candidates:
            return {}
        self.env.cr.execute(
            """
            SELECT src.move_id,
                   MAX(COALESCE(o.date_uploaded, o.date_generated)) AS order_date
              FROM account_move_line src
              JOIN account_partial_reconcile p
                ON src.id IN (p.debit_move_id, p.credit_move_id)
              JOIN account_move_line other
                ON other.id = CASE WHEN p.debit_move_id = src.id
                                   THEN p.credit_move_id ELSE p.debit_move_id END
              JOIN account_move om ON om.id = other.move_id
              LEFT JOIN account_payment_line pl ON pl.move_line_id = other.id
              JOIN account_payment_order o
                ON o.id = COALESCE(om.payment_order_id, pl.order_id)
             WHERE src.move_id = ANY(%s)
               AND o.state <> 'cancel'
               AND COALESCE(o.date_uploaded, o.date_generated) IS NOT NULL
             GROUP BY src.move_id
            """,
            [candidates],
        )
        return dict(self.env.cr.fetchall())

    @api.model
    def _revaluations(self, companies, moves):
        """Valuations of foreign currency holdings (C-28).

        Entries of the exchange difference journal that do not belong to a
        reconciliation and change the euro amount of a foreign currency line
        without a currency amount.
        """
        journals = companies.currency_exchange_journal_id
        if not journals:
            return set()
        self.env.cr.execute(
            """
            SELECT m.id
              FROM account_move m
              JOIN res_company c ON c.id = m.company_id
             WHERE m.id = ANY(%(moves)s)
               AND m.journal_id = ANY(%(journals)s)
               AND NOT EXISTS (SELECT 1 FROM account_partial_reconcile p
                                WHERE p.exchange_move_id = m.id)
               AND EXISTS (SELECT 1 FROM account_move_line l
                            WHERE l.move_id = m.id
                              AND l.currency_id <> c.currency_id
                              AND l.amount_currency = 0
                              AND l.balance <> 0)
            """,
            {"moves": list(moves), "journals": journals.ids},
        )
        return {row[0] for row in self.env.cr.fetchall()}

    # ------------------------------------------------------------ lines
    @api.model
    def _read_lines(self, move_ids):
        if not move_ids:
            return []
        AML = self.env["account.move.line"]
        taxes = AML._fields["tax_ids"]
        tags = AML._fields["tax_tag_ids"]
        self.env.cr.execute(
            f"""
            SELECT l.id, l.move_id, l.account_id, l.company_id,
                   ROUND(l.balance * 100)::bigint AS balance,
                   l.partner_id, l.tax_repartition_line_id, l.display_type,
                   l.currency_id, ROUND(l.amount_currency * 100)::bigint
                       AS amount_currency,
                   l.amount_residual_currency, l.price_unit, l.name,
                   c.currency_id AS company_currency_id,
                   (acc.reconcile OR acc.account_type IN
                        ('asset_receivable', 'liability_payable')) AS reconcilable,
                   ARRAY(SELECT rel.{taxes.column2} FROM {taxes.relation} rel
                          WHERE rel.{taxes.column1} = l.id) AS tax_ids,
                   ARRAY(SELECT t.name->>'en_US'
                           FROM {tags.relation} rel
                           JOIN account_account_tag t
                             ON t.id = rel.{tags.column2}
                          WHERE rel.{tags.column1} = l.id) AS tag_names
              FROM account_move_line l
              JOIN account_account acc ON acc.id = l.account_id
              JOIN res_company c ON c.id = l.company_id
             WHERE l.move_id = ANY(%s)
             ORDER BY l.id
            """,
            [move_ids],
        )
        return self.env.cr.dictfetchall()

    @api.model
    def _line(self, row, children):
        foreign = (
            row["currency_id"] and row["currency_id"] != row["company_currency_id"]
        )
        tax_ids = []
        for tax_id in row["tax_ids"] or ():
            tax_ids.append(tax_id)
            tax_ids += children.get(tax_id, [])
        unit_price = None
        if row["display_type"] == "product" and row["price_unit"]:
            unit_price = round(Decimal(str(row["price_unit"])) * 100)
        return m.Line(
            id=row["id"],
            move_id=row["move_id"],
            account_id=row["account_id"],
            balance=int(row["balance"]),
            partner_id=row["partner_id"],
            repartition_id=row["tax_repartition_line_id"],
            tax_ids=tuple(dict.fromkeys(tax_ids)),
            tag_names=tuple(name for name in row["tag_names"] or () if name),
            display_type=row["display_type"] or "product",
            currency_id=row["currency_id"],
            amount_currency=int(row["amount_currency"] or 0),
            residual_currency_zero=bool(
                foreign and row["reconcilable"] and not row["amount_residual_currency"]
            ),
            unit_price=unit_price,
        )

    @api.model
    def _read_partials(self, companies, line_ids):
        if not line_ids:
            return []
        self.env.cr.execute(
            """
            SELECT p.id, p.debit_move_id, p.credit_move_id,
                   ROUND(p.amount * 100)::bigint AS amount, p.max_date,
                   p.exchange_move_id, p.create_date::date AS created
              FROM account_partial_reconcile p
             WHERE p.debit_move_id = ANY(%(lines)s)
                OR p.credit_move_id = ANY(%(lines)s)
             ORDER BY p.id
            """,
            {"lines": line_ids},
        )
        return [
            m.Partial(
                id=row["id"],
                debit_line_id=row["debit_move_id"],
                credit_line_id=row["credit_move_id"],
                amount=int(row["amount"]),
                max_date=row["max_date"],
                exchange_move_id=row["exchange_move_id"],
                created=row["created"],
            )
            for row in self.env.cr.dictfetchall()
        ]

    # ------------------------------------------------------------ taxes
    @api.model
    def _tax_references(self, lines):
        tax_ids, repartition_ids = set(), set()
        for line in lines:
            tax_ids.update(line["tax_ids"] or ())
            if line["tax_repartition_line_id"]:
                repartition_ids.add(line["tax_repartition_line_id"])
        return tax_ids, repartition_ids

    @api.model
    def _read_taxes(self, tax_ids, repartition_ids):
        """Taxes with type and factors; children of groups inherit the type."""
        Tax = self.env["account.tax"].sudo().with_context(active_test=False)
        Repartition = self.env["account.tax.repartition.line"].sudo()
        taxes = Tax.browse(tax_ids)
        taxes |= Repartition.browse(repartition_ids).tax_id
        groups = taxes.filtered(lambda tax: tax.amount_type == "group")
        children = {group.id: group.children_tax_ids.ids for group in groups}
        taxes |= groups.children_tax_ids
        parents = defaultdict(lambda: Tax.browse())
        for parent in Tax.search(Domain("children_tax_ids", "in", taxes.ids)):
            for child in parent.children_tax_ids:
                parents[child.id] |= parent
        xml_ids = taxes.get_external_id()
        result_taxes, result_repartitions = [], []
        for tax in taxes:
            type_tax_use = tax.type_tax_use
            if type_tax_use == "none" and parents[tax.id]:
                type_tax_use = parents[tax.id][0].type_tax_use
            lines = tax.invoice_repartition_line_ids | tax.refund_repartition_line_ids
            result_taxes.append(
                m.Tax(
                    id=tax.id,
                    type_tax_use=type_tax_use,
                    amount=_fraction(tax.amount),
                    factors=tuple(
                        (line.document_type, _fraction(line.factor_percent))
                        for line in lines
                        if line.repartition_type == "tax"
                    ),
                    ku_tax=revenue.is_ku_tax(xml_ids.get(tax.id)),
                )
            )
            for line in lines.filtered(lambda line: line.repartition_type == "tax"):
                result_repartitions.append(
                    m.Repartition(
                        id=line.id,
                        tax_id=tax.id,
                        document_type=line.document_type,
                        factor_percent=_fraction(line.factor_percent),
                        use_in_tax_closing=line.use_in_tax_closing,
                        has_account=bool(line.account_id),
                    )
                )
        return result_taxes, result_repartitions, children

    # ------------------------------------------------------------ accounts
    @api.model
    def _money_journals(self, companies):
        return (
            self.env["account.journal"]
            .sudo()
            .with_context(active_test=False)
            .search(
                Domain("company_id", "in", companies.ids)
                & Domain("type", "in", MONEY_JOURNAL_TYPES)
            )
        )

    @api.model
    def _outstanding_accounts(self, companies, journals):
        """Outstanding receipts and payments accounts of the payment methods
        (5.4 no. 3): the payment wizard books on them. Money transit
        accounts between own money accounts (transfer account) are left
        out."""
        methods = (
            self.env["account.payment.method.line"]
            .sudo()
            .search(Domain("journal_id", "in", journals.ids))
        )
        accounts = methods.payment_account_id
        Template = self.env["account.chart.template"]
        for root in companies.sudo().root_id:
            for xml_id in (
                "account_journal_payment_debit_account_id",
                "account_journal_payment_credit_account_id",
            ):
                accounts |= (
                    Template.with_company(root)
                    .sudo()
                    .ref(xml_id, raise_if_not_found=False)
                    or accounts.browse()
                )
        accounts -= journals.default_account_id
        accounts -= companies.sudo().transfer_account_id
        return set(accounts.ids)

    @api.model
    def _read_accounts(self, root, account_ids, journals, outstanding=()):
        liquidity = set(journals.default_account_id.ids)
        private = set(
            journals.filtered("l10n_de_euer_private_account").default_account_id.ids
        )
        suspense = set(journals.suspense_account_id.ids)
        suspense |= set(root.account_journal_suspense_account_id.ids)
        account_ids = set(account_ids) | liquidity | suspense
        accounts = (
            self.env["account.account"]
            .sudo()
            .with_company(root)
            .with_context(active_test=False)
            .browse(sorted(account_ids))
        )
        assignments = defaultdict(list)
        for assignment in (
            self.env["l10n_de_euer.account.assignment"]
            .sudo()
            .search(
                Domain("company_id", "=", root.id)
                & Domain("account_id", "in", accounts.ids)
            )
        ):
            assignments[assignment.account_id.id].append(
                m.Assignment(
                    category=assignment.category_id.code,
                    date_from=assignment.date_from or None,
                    date_to=assignment.date_to or None,
                )
            )
        return [
            m.Account(
                id=account.id,
                code=account.code or "",
                account_type=account.account_type,
                internal_group=account.internal_group or "",
                reconcile=account.reconcile,
                liquidity=account.id in liquidity,
                suspense=account.id in suspense,
                private_money=account.id in private,
                outstanding=account.id in outstanding,
                assignments=tuple(assignments.get(account.id, ())),
            )
            for account in accounts
        ]

    # ------------------------------------------------------------ master data
    @api.model
    def _categories(self):
        categories = (
            self.env["l10n_de_euer.category"]
            .sudo()
            .with_context(active_test=False)
            .search(Domain("code", "!=", False))
        )
        return tuple(
            m.Category(
                code=category.code,
                role=category.role,
                be_art=category.be_art or None,
                erfassung=category.erfassung or "Z",
                writeoff=category.writeoff,
                on_payment=category.on_payment,
                vat_kind=category.vat_kind or None,
                separate_record=category.separate_record or None,
                restricted_column=category.restricted_column or None,
                recurring_candidate=category.recurring_candidate,
                gift_bulk_allowed=category.gift_bulk_allowed,
                interim=category.interim,
                scope_status=category.scope_status or "supported",
            )
            for category in categories
        )

    @api.model
    def _mappings(self):
        """Mapping lines per form year (the version used for the year, F-13)."""
        Form = self.env["l10n_de_euer.form"].sudo()
        years = sorted(year for (year,) in Form._read_group([], ["year"]))
        result = {}
        for year in years:
            forms, _provisional = Form._get_forms_for_year(year)
            mappings = (
                self.env["l10n_de_euer.category.mapping"]
                .sudo()
                .search(Domain("form_id", "in", forms.ids))
            )
            result[year] = tuple(
                m.MappingLine(
                    category=mapping.category_id.code,
                    field=mapping.field_id.code,
                    share=Fraction(str(mapping.share or 1)),
                    condition=mapping.condition or "always",
                    option=mapping.option or None,
                    davon_field=mapping.davon_field_id.code or None,
                    additional_field=mapping.additional_field_id.code or None,
                    sequence=mapping.sequence,
                )
                for mapping in mappings
            )
        return result

    @api.model
    def _parameters(self):
        result = []
        parameters = self.env["l10n_de_euer.parameter"].sudo()
        for parameter in parameters.search(Domain("code", "!=", False)):
            value = (
                parameter.value_json
                if parameter._has_json_value()
                else Decimal(str(parameter.value))
            )
            result.append(
                m.ParameterValue(
                    code=parameter.code,
                    value=value,
                    date_from=parameter.date_from or None,
                    date_to=parameter.date_to or None,
                )
            )
        return tuple(result)

    @api.model
    def _partners(self, partner_ids):
        partners = (
            self.env["res.partner"]
            .sudo()
            .with_context(active_test=False)
            .browse(sorted(partner_ids))
        )
        return [
            m.Partner(
                id=partner.id,
                recurring=partner.l10n_de_euer_recurring
                or partner.commercial_partner_id.l10n_de_euer_recurring,
                tax_office=partner.l10n_de_euer_tax_office
                or partner.commercial_partner_id.l10n_de_euer_tax_office,
            )
            for partner in partners
        ]

    @api.model
    def _vat_periods(self, root):
        periods = (
            self.env["l10n_de_euer.vat.period"]
            .sudo()
            .search(Domain("company_id", "=", root.id), order="date_from")
        )
        return [
            m.VatPeriod(
                date_from=period.date_from,
                date_to=period.date_to or None,
                regime=period.regime,
                basis=period.basis or "supply_date",
                vat_return_period=period.vat_return_period or None,
                permanent_extension=period.permanent_extension,
                # with the year entered, 0 is a turnover (nothing received)
                prev_year_turnover=(
                    round(Decimal(str(period.prev_year_turnover)) * 100)
                    if period.prev_year_turnover or period.prev_year_turnover_year
                    else None
                ),
                prev_year_turnover_year=period.prev_year_turnover_year or None,
            )
            for period in periods
        ]

    @api.model
    def _founding_year(self, root):
        """Year the activity started (small business limit, R-27).

        Read from the main company like the VAT status periods: the limits
        of § 19 UStG apply to the whole enterprise (§ 2 (1) sentence 2 UStG),
        a branch that is a business of its own starts no new activity. The
        turnover of other parts of the enterprise outside Odoo is not known
        to the module (readme ROADMAP).
        """
        start = root.l10n_de_euer_activity_start
        return start.year if start else None

    # ------------------------------------------------------------ decisions
    @api.model
    def _opening_items(self, business):
        items = (
            self.env["l10n_de_euer.opening.item"]
            .sudo()
            .search(Domain("company_id", "=", business.id))
        )
        return [
            m.OpeningItem(
                line_id=item.move_line_id.id,
                category=item.category_id.code,
                vat_regime=item.vat_regime,
                tax_rate=_fraction(item.tax_rate),
            )
            for item in items
        ]

    @api.model
    def _gift_lines(self, moves):
        """Recipients of the gifts per document line (5.7 no. 7)."""
        lines = (
            self.env["l10n_de_euer.gift.line"]
            .sudo()
            .search(Domain("move_id", "in", list(moves)))
        )
        return [
            m.GiftLine(
                line_id=line.move_line_id.id,
                recipient_id=line.recipient_id.id,
                gift_date=line.gift_date or None,
                share=_fraction(line.quantity) or Fraction(1),
            )
            for line in lines
        ]

    @api.model
    def _classifications(self, business):
        items = (
            self.env["l10n_de_euer.residual.classification"]
            .sudo()
            .search(
                Domain("company_id", "=", business.id)
                & Domain("source_line_id", "!=", False)
            )
        )
        return [
            m.Classification(
                line_id=item.source_line_id.id,
                kind="expense" if item.kind == "expense_refund" else item.kind,
                category=item.category_id.code or None,
                tax_rate=_fraction(item.tax_rate),
                vat_regime=item.vat_regime or None,
            )
            for item in items
        ]

    @api.model
    def _ten_day_decisions(self, business):
        decisions, confirmations = [], set()
        for item in self.env["l10n_de_euer.ten_day.item"]._engine_decisions(business):
            if item.kind == "transfer_order":
                if item.decision == "apply":
                    confirmations.add(item.source_move_id.id)
                continue
            decisions.append(
                m.TenDayDecision(
                    source_move_id=item.source_move_id.id,
                    origin_move_id=item.origin_move_id.id,
                    decision=item.decision,
                    kind="direct_debit_tax"
                    if item.kind == "direct_debit_tax"
                    else "recurring",
                    economic_year=item.economic_year or None,
                    due_date=item.due_date or None,
                )
            )
        return decisions, confirmations
