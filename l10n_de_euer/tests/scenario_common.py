# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Helpers of the scenario tests with real Odoo bookings (spec 9.5).

Accounts and taxes are named by a key; ``ACCOUNTS`` and ``TAXES`` of the
test class give the XML ids of the chart template (SKR03 or SKR04), so the
same scenario runs on both charts. Amounts are written in euro. All cases
are synthetic (decision D6).
"""

from decimal import Decimal

from odoo import Command

SKR03_ACCOUNTS = {
    "ku_revenue": "account_8195",
    "revenue_19": "account_8400",
    "revenue_7": "account_8300",
    "discount_19": "account_8736",
    "discount_received_19": "account_3736",
    "discount_given": "account_8730",
    "discount_received": "account_3730",
    "exempt_revenue": "account_8338",
    "interest_income": "account_2650",
    "asset_sale": "account_8800",
    "office": "account_4930",
    "bank_fees": "account_4970",
    "rent": "account_4210",
    "leasing": "account_4965",
    "wages": "account_4120",
    "non_cash_benefit": "account_8590",
    "private_car": "account_8921",
    "gifts": "account_4630",
    "entertainment": "account_4650",
    "entertainment_nd": "account_4654",
    "advice": "account_4950",
    "private_w": "account_1800",
    "private_c": "account_1890",
    "vat_payment": "account_1780",
    "vat_refund": "account_1545",
    "vat_in": "account_1576",
    "vat_out": "account_1776",
    "writeoff": "account_2400",
    "fx_loss": "account_2150",
    "fx_gain": "account_2660",
    "asset": "account_0490",
    "depreciation": "account_4830",
    "gwg": "account_4855",
    "transitory": "account_1590",
    "loan": "account_0630",
    "liability": "account_1700",
    "payroll": "account_1740",
    "provision": "account_0970",
    "opening": "account_9000",
    "opening_debtors": "account_9008",
    "opening_creditors": "account_9009",
}
SKR04_ACCOUNTS = {
    "ku_revenue": "chart_skr04_4185",
    "revenue_19": "chart_skr04_4400",
    "revenue_7": "chart_skr04_4300",
    "discount_19": "chart_skr04_4736",
    "discount_received_19": "chart_skr04_5736",
    "discount_given": "chart_skr04_4730",
    "discount_received": "chart_skr04_5730",
    "exempt_revenue": "chart_skr04_4338",
    "interest_income": "chart_skr04_7100",
    "asset_sale": "chart_skr04_4844",
    "office": "chart_skr04_6815",
    "bank_fees": "chart_skr04_6855",
    "rent": "chart_skr04_6310",
    "leasing": "chart_skr04_6840",
    "wages": "chart_skr04_6020",
    "non_cash_benefit": "chart_skr04_4947",
    "private_car": "chart_skr04_4645",
    "gifts": "chart_skr04_6610",
    "entertainment": "chart_skr04_6640",
    "entertainment_nd": "chart_skr04_6644",
    "advice": "chart_skr04_6825",
    "private_w": "chart_skr04_2100",
    "private_c": "chart_skr04_2180",
    "vat_payment": "chart_skr04_3820",
    "vat_refund": "chart_skr04_1421",
    "vat_in": "chart_skr04_1406",
    "vat_out": "chart_skr04_3806",
    "writeoff": "chart_skr04_6930",
    "fx_loss": "chart_skr04_6880",
    "fx_gain": "chart_skr04_4840",
    "asset": "chart_skr04_690",
    "depreciation": "chart_skr04_6220",
    "gwg": "chart_skr04_6260",
    "transitory": "chart_skr04_1370",
    "loan": "chart_skr04_3150",
    "liability": "chart_skr04_3500",
    "payroll": "chart_skr04_3720",
    "provision": "chart_skr04_3070",
    "opening": "chart_skr04_9000",
    "opening_debtors": "chart_skr04_9008",
    "opening_creditors": "chart_skr04_9009",
}
SKR03_TAXES = {
    "ust19": "tax_ust_19_skr03",
    "ust7": "tax_ust_7_skr03",
    "vst19": "tax_vst_19_skr03",
    "vst7": "tax_vst_7_skr03",
    "igE19": "tax_eu_19_purchase_skr03",
    "ku": "tax_ust_no_ustpflicht_skr03",
}
SKR04_TAXES = {
    key: value.replace("skr03", "skr04") for key, value in SKR03_TAXES.items()
}


def euro(value):
    return Decimal(str(value)).quantize(Decimal("0.01"))


class ScenarioCommon:
    """Bookings of one business and the EÜR calculation of a year."""

    CHART = None
    ACCOUNTS = {}
    TAXES = {}
    REGIME = "ku"

    @classmethod
    def setup_euer(cls):
        cls.company = cls.env.company
        cls.company.l10n_de_euer_enabled = True
        cls.env["l10n_de_euer.vat.period"].create(
            {
                "company_id": cls.company.id,
                "date_from": "2020-01-01",
                "regime": cls.REGIME,
                "vat_return_period": "monthly",
            }
        )
        cls.bank_journal = cls.company_data["default_journal_bank"]
        cls.cash_journal = cls.company_data["default_journal_cash"]
        cls.misc_journal = cls.company_data["default_journal_misc"]
        cls.customer = cls.partner_a
        cls.supplier = cls.partner_b
        # one open item per document; instalments are tested on their own
        immediate = cls.env.ref("account.account_payment_term_immediate")
        (cls.customer | cls.supplier).with_company(cls.company).write(
            {
                "property_payment_term_id": immediate.id,
                "property_supplier_payment_term_id": immediate.id,
            }
        )
        cls.receivable = cls.customer.with_company(
            cls.company
        ).property_account_receivable_id
        cls.payable = cls.supplier.with_company(cls.company).property_account_payable_id
        cls.Run = cls.env["l10n_de_euer.run"]

    # ------------------------------------------------------------ chart
    def acc(self, key):
        if key == "bank":
            return self.bank_journal.default_account_id
        if key == "cash":
            return self.cash_journal.default_account_id
        if key == "receivable":
            return self.receivable
        if key == "payable":
            return self.payable
        if key == "transfer":
            return self.company.transfer_account_id
        if key == "suspense":
            return self.bank_journal.suspense_account_id
        return self.env.ref(f"account.{self.company.id}_{self.ACCOUNTS[key]}")

    def tax(self, key):
        tax = self.env.ref(f"account.{self.company.id}_{self.TAXES[key]}")
        tax.active = True
        return tax

    def rep(self, key, document_type="invoice"):
        """Tax repartition line (of tax) of a tax for manual tax lines."""
        lines = (
            self.tax(key).invoice_repartition_line_ids
            if document_type == "invoice"
            else self.tax(key).refund_repartition_line_ids
        )
        return lines.filtered(lambda line: line.repartition_type == "tax")[:1]

    def tax_line(self, key, document_type="invoice"):
        """Values of a manual tax line (repartition line, its account)."""
        repartition = self.rep(key, document_type)
        return {"tax_repartition_line_id": repartition.id}

    def base_line(self, *keys):
        """Values of a manual base line carrying taxes."""
        return {"tax_ids": [Command.set([self.tax(key).id for key in keys])]}

    def tax_office(self):
        partner = self.env["res.partner"].create(
            {"name": "Finanzamt", "l10n_de_euer_tax_office": True}
        )
        return partner

    def set_regime(self, regime, date_from="2020-01-01", **values):
        """Replace the VAT status periods by one period from ``date_from``."""
        self.env["l10n_de_euer.vat.period"].search(
            [("company_id", "=", self.company.id)]
        ).unlink()
        return self.env["l10n_de_euer.vat.period"].create(
            {
                "company_id": self.company.id,
                "date_from": date_from,
                "regime": regime,
                "vat_return_period": "monthly",
                **values,
            }
        )

    def previous_turnover(self, year, amount=0.0):
        """Enter the turnover of the year before ``year`` on the VAT status
        periods: a small business year without bookings of earlier years and
        without this input gets the warning C-32 'turnover of the previous
        year unknown'."""
        self.env["l10n_de_euer.vat.period"].search(
            [("company_id", "=", self.company.id)], order="date_from", limit=1
        ).write({"prev_year_turnover": amount, "prev_year_turnover_year": year})

    def _account(self, account):
        return self.acc(account) if isinstance(account, str) else account

    def template_account(self, xml_id):
        """Account of the chart template by its XML id (``account_2107``)."""
        return self.env.ref(f"account.{self.company.id}_{xml_id}")

    def category(self, code):
        return self.env["l10n_de_euer.category"].search([("code", "=", code)])

    def field_of(self, category_code, year=2026, condition=None):
        """Form field a category is mapped to in the form year."""
        mappings = self.env["l10n_de_euer.category.mapping"].search(
            [("category_id.code", "=", category_code), ("year", "=", min(year, 2026))]
        )
        if condition:
            mappings = mappings.filtered(lambda line: line.condition == condition)
        return mappings[:1].field_id.code

    # ------------------------------------------------------------ documents
    def invoice(self, move_type, date, lines, partner=None, post=True, **values):
        """Invoice with lines ``(account key, price, tax keys)``."""
        partner = partner or (
            self.customer if move_type.startswith("out") else self.supplier
        )
        move = self.env["account.move"].create(
            {
                "move_type": move_type,
                "partner_id": partner.id,
                "invoice_date": date,
                "date": date,
                **values,
                "invoice_line_ids": [
                    Command.create(
                        {
                            "name": f"Line {index}",
                            "account_id": self._account(spec[0]).id,
                            "price_unit": spec[1],
                            "quantity": spec[3] if len(spec) > 3 else 1,
                            "tax_ids": [
                                Command.set([self.tax(key).id for key in spec[2]])
                            ]
                            if len(spec) > 2
                            else [Command.clear()],
                        }
                    )
                    for index, spec in enumerate(lines)
                ],
            }
        )
        if post:
            move.action_post()
        return move

    def sale(self, date, amount, account="ku_revenue", taxes=(), **values):
        return self.invoice("out_invoice", date, [(account, amount, taxes)], **values)

    def purchase(self, date, amount, account="office", taxes=(), **values):
        return self.invoice("in_invoice", date, [(account, amount, taxes)], **values)

    def entry(self, date, lines, journal=None, partner=None, post=True, **values):
        """Journal entry with lines ``(account key, balance[, values])``."""
        move = self.env["account.move"].create(
            {
                "move_type": "entry",
                "date": date,
                "journal_id": (journal or self.misc_journal).id,
                "partner_id": partner.id if partner else False,
                **values,
                "line_ids": [
                    Command.create(
                        {
                            "name": "Test",
                            "account_id": self._account(spec[0]).id,
                            "balance": spec[1],
                            "partner_id": partner.id if partner else False,
                            **(spec[2] if len(spec) > 2 else {}),
                        }
                    )
                    for spec in lines
                ],
            }
        )
        if post:
            move.action_post()
        return move

    @staticmethod
    def open_lines(move):
        """All receivable or payable lines of a document."""
        return move.line_ids.filtered(
            lambda line: line.account_id.account_type
            in ("asset_receivable", "liability_payable")
        ).sorted("id")

    @classmethod
    def open_line(cls, move):
        """Receivable or payable line of a document (the first one), else the
        first reconcilable balance sheet line."""
        return (
            cls.open_lines(move)
            or move.line_ids.filtered(
                lambda line: line.account_id.reconcile
                and line.account_id.internal_group in ("asset", "liability")
            ).sorted("id")
        )[:1]

    # ------------------------------------------------------------ money
    def bank(
        self,
        date,
        amount,
        matches=(),
        extra=(),
        rest=None,
        partner=None,
        journal=None,
        ref="Bank",
    ):
        """Bank statement line, reconciled like the bank reconciliation does.

        ``matches``: ``(journal item or document, amount)``: a counterpart
        line on the account of the item, reconciled with it. ``extra``:
        ``(account key, balance[, values])`` lines in the bank entry (fees,
        discounts, taxes). ``rest``: account key of the rest (open item of
        the partner); without it the rest stays on the suspense account.
        """
        journal = journal or self.bank_journal
        st_line = self.env["account.bank.statement.line"].create(
            {
                "journal_id": journal.id,
                "date": date,
                "amount": amount,
                "payment_ref": ref,
                "partner_id": partner.id if partner else False,
            }
        )
        if not matches and not extra and not rest:
            return st_line
        move = st_line.move_id
        _liquidity, suspense, _other = st_line._seek_for_lines()
        commands = [Command.unlink(line.id) for line in suspense]
        total = 0.0
        targets = []
        for spec in matches:
            target, value = spec[0], spec[1]
            if target._name == "account.move":
                target = self.open_line(target)
            balance = -value if target.balance > 0 else value
            total += balance
            targets.append(target)
            values = {
                "name": ref,
                "account_id": target.account_id.id,
                "partner_id": target.partner_id.id,
                "balance": balance,
            }
            if len(spec) > 2:
                # foreign currency: amount in the currency of the item
                foreign = spec[2] if target.balance < 0 else -spec[2]
                values.update(
                    currency_id=target.currency_id.id, amount_currency=foreign
                )
            commands.append(Command.create(values))
        for spec in extra:
            total += spec[1]
            commands.append(
                Command.create(
                    {
                        "name": ref,
                        "account_id": self._account(spec[0]).id,
                        "balance": spec[1],
                        "partner_id": partner.id if partner else False,
                        **(spec[2] if len(spec) > 2 else {}),
                    }
                )
            )
        remaining = round(-amount - total, 2)
        if remaining:
            commands.append(
                Command.create(
                    {
                        "name": ref,
                        "account_id": (
                            self._account(rest) if rest else suspense.account_id
                        ).id,
                        "balance": remaining,
                        "partner_id": partner.id if partner else False,
                    }
                )
            )
        before = move.line_ids
        move.button_draft()
        move.with_context(skip_account_move_synchronization=True).write(
            {"line_ids": commands}
        )
        move.with_context(skip_account_move_synchronization=True).action_post()
        new_lines = (move.line_ids - before).sorted("id")
        for target, line in zip(targets, new_lines, strict=False):
            (line | target).reconcile()
        return st_line

    def pay(self, documents, date, amount=None, journal=None, **values):
        """Payment through the payment wizard (payment entry with outstanding
        account, a transit entry)."""
        wizard = (
            self.env["account.payment.register"]
            .with_context(active_model="account.move", active_ids=documents.ids)
            .create(
                {
                    "payment_date": date,
                    "journal_id": (journal or self.bank_journal).id,
                    **({"amount": amount} if amount is not None else {}),
                    **values,
                }
            )
        )
        return wizard._create_payments()

    def bank_for_payment(self, date, payment, amount=None, **values):
        """Statement line reconciled with the outstanding line of a payment."""
        line = payment.move_id.line_ids.filtered(
            lambda line: line.account_id == payment.outstanding_account_id
        )
        value = amount if amount is not None else abs(line.balance)
        sign = 1 if line.balance > 0 else -1
        return self.bank(date, sign * value, matches=[(line, value)], **values)

    def reconcile(self, *records):
        lines = self.env["account.move.line"]
        for record in records:
            lines |= (
                (self.open_lines(record) or self.open_line(record))
                if record._name == "account.move"
                else record
            )
        lines.reconcile()

    # ------------------------------------------------------------ calculation
    def compute(self, year):
        run = self.Run._compute_year(self.company, year)
        self.assertTrue(
            run.invariant_ok,
            f"invariants violated: {self.checks(year, 'C-11').mapped('message')}",
        )
        return run

    def fields(self, year):
        """Field totals of the year in euro (``Decimal``), zero fields left out."""
        run = self.compute(year)
        return {
            code: euro(value)
            for code, value in sorted(run._get_field_amounts().items())
            if value
        }

    def checks(self, year, code=None):
        domain = [
            ("company_id", "=", self.company.root_id.id),
            ("year", "=", year),
            ("origin", "=", "calculation"),
        ]
        if code:
            domain.append(("code", "=", code))
        return self.env["l10n_de_euer.check"].search(domain)

    def codes(self, year, severity=None):
        checks = self.checks(year)
        if severity:
            checks = checks.filtered(lambda check: check.severity == severity)
        return sorted(set(checks.mapped("code")))

    def assertFields(self, year, expected):
        self.assertEqual(
            self.fields(year), {code: euro(value) for code, value in expected.items()}
        )
