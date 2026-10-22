# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Take over the fixed assets of Papierkram into the EÜR asset register.

Assets acquired up to the takeover date (31 December of the last year
computed in Papierkram) are taken over with the register import of
``l10n_de_euer`` and their Papierkram book value at that date; their
depreciation is posted as journal entries only up to that date, as history.
Assets acquired later are entered in the register as purchases, linked to the
journal items of their voucher. From the takeover date on, the register
computes the depreciation, and the EÜR counts only the register (spec 5.8).
"""

import base64
import csv
import io
from datetime import date
from decimal import Decimal

from markupsafe import Markup, escape

from odoo import Command, api, fields, models
from odoo.exceptions import UserError, ValidationError

from odoo.addons.l10n_de_euer.models.engine import depreciation as dep
from odoo.addons.l10n_de_euer.wizards.asset_from_move_line import group_ranges
from odoo.addons.l10n_de_papierkram_import.lib.depreciation import (
    papierkram_schedule,
)

#: Papierkram keeps a memo value of 1 € at the end of the depreciation
MEMO_VALUE = Decimal("1.00")
#: AVEÜR groups that are not depreciated
NOT_DEPRECIABLE = frozenset({"land", "office_land", "fin_tev", "fin_other"})
#: columns of the register import (``l10n_de_euer.asset.import``)
COLUMNS = (
    "name",
    "code",
    "group",
    "kind",
    "delivery_date",
    "cost",
    "useful_life_months",
    "method",
    "accumulated",
    "book_value",
    "memo_value",
    "pool_year",
    "pool_remaining",
)


def _group(account_skr03):
    """AVEÜR group of a Papierkram category by its SKR03 account."""
    code = account_skr03.zfill(4)
    for chart, start, end, group in group_ranges():
        if chart == "de_skr03" and start <= code[: len(start)] <= end:
            return group
    return "other_movable"


class PapierkramImport(models.Model):
    _inherit = "papierkram.import"

    euer_asset_takeover = fields.Boolean(
        string="Take Over Fixed Assets (EÜR)",
        default=lambda self: self.env.company.root_id.l10n_de_euer_enabled,
        help="Enters the fixed assets of Papierkram in the EÜR asset register. "
        "Assets acquired up to the takeover date are taken over with their "
        "Papierkram book value; later acquisitions are entered as purchases. "
        "Depreciation is then posted as journal entries only up to the "
        "takeover date.",
    )
    euer_takeover_date = fields.Date(
        string="Takeover Date (Asset Register)",
        default=lambda self: date(fields.Date.today().year - 1, 12, 31),
        help="31 December of the last year computed in Papierkram. From the "
        "following year on, the asset register computes the depreciation.",
    )
    euer_asset_ids = fields.Many2many(
        "l10n_de_euer.asset",
        string="Assets in the Register",
        readonly=True,
        copy=False,
    )
    euer_asset_count = fields.Integer(
        string="Number of Assets", compute="_compute_euer_asset_count"
    )

    @api.depends("euer_asset_ids")
    def _compute_euer_asset_count(self):
        for rec in self:
            rec.euer_asset_count = len(rec.euer_asset_ids)

    @api.constrains("euer_asset_takeover", "euer_takeover_date")
    def _check_euer_takeover_date(self):
        for rec in self:
            if not rec.euer_asset_takeover:
                continue
            day = rec.euer_takeover_date
            if not day or (day.month, day.day) != (12, 31):
                raise ValidationError(
                    self.env._(
                        "The takeover date of the asset register must be a 31 December."
                    )
                )

    # ------------------------------------------------------------ actions
    def action_check(self):
        self._euer_prepare()
        return super().action_check()

    def action_import(self):
        self._euer_prepare()
        result = super().action_import()
        if self.euer_asset_takeover:
            self._euer_take_over_assets()
        return result

    def action_euer_take_over_assets(self):
        """Take over the fixed assets of an import that ran before the EÜR
        (or without the takeover) into the asset register."""
        self.ensure_one()
        if self.state != "done":
            raise UserError(self.env._("Run the import first."))
        # both fields in one write: the constraint on the takeover date must
        # see the default date together with the switched-on takeover
        self.write(
            {
                "euer_asset_takeover": True,
                "euer_takeover_date": self.euer_takeover_date
                or date(fields.Date.today().year - 1, 12, 31),
            }
        )
        if not self.company_id.sudo().root_id.l10n_de_euer_enabled:
            raise UserError(
                self.env._(
                    "Enable the EÜR for %(company)s (Settings → Accounting) first.",
                    company=self.company_id.root_id.name,
                )
            )
        self._euer_take_over_assets()

    def action_view_euer_assets(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "l10n_de_euer.action_l10n_de_euer_asset"
        )
        action["domain"] = [("id", "in", self.euer_asset_ids.ids)]
        return action

    # ------------------------------------------------------------ preparation
    def _euer_prepare(self):
        """Depreciation entries only up to the takeover date; the EÜR must be
        enabled for the asset register."""
        self.ensure_one()
        if not self.euer_asset_takeover or self.state == "done":
            return
        if not self.company_id.sudo().root_id.l10n_de_euer_enabled:
            raise UserError(
                self.env._(
                    "Enable the EÜR for %(company)s (Settings → Accounting) or "
                    "switch off the takeover of the fixed assets.",
                    company=self.company_id.root_id.name,
                )
            )
        takeover = self.euer_takeover_date
        if not self.depreciation_until or self.depreciation_until > takeover:
            self.depreciation_until = takeover

    # ------------------------------------------------------------ takeover
    def _euer_take_over_assets(self):
        self.ensure_one()
        notes = []
        export = self._read_export()
        try:
            self._euer_vat_period(export, notes)
            entries = self._euer_entries(export, notes)
        finally:
            export.close()
        business = self.company_id._l10n_de_euer_business_root()
        existing = set(
            self.env["l10n_de_euer.asset"]
            .sudo()
            .search(
                [
                    ("company_id", "=", business.id),
                    ("code", "in", [entry["code"] for entry in entries]),
                ]
            )
            .mapped("code")
        )
        for entry in entries:
            if entry["code"] in existing:
                notes.append(
                    self.env._(
                        "%(code)s %(name)s is already in the asset register.",
                        code=entry["code"],
                        name=entry["name"],
                    )
                )
        entries = [entry for entry in entries if entry["code"] not in existing]
        takeover = self.euer_takeover_date
        old = [entry for entry in entries if entry["delivery_date"] <= takeover]
        new = [entry for entry in entries if entry["delivery_date"] > takeover]
        assets = self._euer_import_register(old, notes)
        assets |= self._euer_create_purchases(new, notes)
        self.euer_asset_ids |= assets
        self._euer_write_log(assets, len(old), len(new), notes)

    def _euer_vat_period(self, export, notes):
        """VAT treatment of the business from the first export year on, unless
        the business already has one (spec 4.6)."""
        Period = self.env["l10n_de_euer.vat.period"]
        root = self.company_id.root_id
        if not export.years or Period.search_count([("company_id", "=", root.id)]):
            return
        start = date(export.years[0], 1, 1)
        regime = "ku" if self.small_business else "regular"
        Period.create({"company_id": root.id, "date_from": start, "regime": regime})
        labels = dict(Period._fields["regime"]._description_selection(self.env))
        notes.append(
            self.env._(
                "VAT treatment %(regime)s entered from %(date)s; check it under "
                "EÜR → VAT Treatment.",
                regime=labels[regime],
                date=start.strftime("%d.%m.%Y"),
            )
        )

    def _euer_entries(self, export, notes):
        """Values of the register per Papierkram asset (or ``None``)."""
        takeover = self.euer_takeover_date
        entries = []
        codes = {}
        for asset in export.assets:
            category = self._euer_category(export, asset)
            if not category:
                notes.append(
                    self.env._(
                        "%(nr)s %(name)s: voucher or asset category is missing; "
                        "enter the asset in the register by hand.",
                        nr=asset.voucher_number,
                        name=asset.name,
                    )
                )
                continue
            count = codes.get(asset.voucher_number, 0) + 1
            codes[asset.voucher_number] = count
            entry = self._euer_entry(asset, category, takeover)
            entry["code"] = asset.voucher_number
            entry["count"] = count
            entries.append(entry)
        # vouchers with several assets: inventory numbers B-01234/1, B-01234/2
        for entry in entries:
            if codes[entry["code"]] > 1:
                entry["code"] = f"{entry['code']}/{entry['count']}"
        return entries

    @staticmethod
    def _euer_category(export, asset):
        voucher = export.vouchers.get(asset.voucher_number)
        for line in voucher.lines if voucher else ():
            category = export.categories.get(line.category)
            if category and category.category_type == "ASSET":
                return category
        return None

    def _euer_entry(self, asset, category, takeover):
        group = _group(category.account_skr03)
        cost = Decimal(asset.acquisition_value)
        months = asset.useful_life_months
        kind, method = "depreciable", "linear"
        if category.kind == "GWG":
            kind, method = "pool", "none"
        elif category.kind == "INSTANT":
            if "GWG" in category.name:
                kind, method = "gwg", "none"
            else:
                # immediate write-off of computer hardware and software
                method = "digital_1y"
        elif group in NOT_DEPRECIABLE or not months:
            kind, method = "non_depreciable", "none"
        accumulated = sum(
            (
                amount
                for year, amount in papierkram_schedule(asset)
                if year <= takeover.year
            ),
            Decimal(0),
        )
        if kind == "non_depreciable":
            accumulated = Decimal(0)
        book_value = cost - accumulated
        memo = (
            MEMO_VALUE
            if method == "linear" and months and book_value >= MEMO_VALUE
            else Decimal(0)
        )
        return {
            "name": asset.name,
            "group": group,
            "kind": kind,
            "method": method,
            "delivery_date": asset.acquisition_date,
            "cost": cost,
            "useful_life_months": months if method == "linear" else 0,
            "accumulated": accumulated,
            "book_value": book_value,
            "memo_value": memo,
            "category": category,
        }

    def _euer_import_register(self, entries, notes):
        """Take over the assets acquired up to the takeover date with the
        register import (its checks apply, nothing is imported on errors)."""
        Asset = self.env["l10n_de_euer.asset"]
        if not entries:
            return Asset
        takeover = self.euer_takeover_date
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";", lineterminator="\n")
        writer.writerow(COLUMNS)
        for entry in entries:
            pool = entry["kind"] == "pool"
            remaining = ""
            if pool:
                year = entry["delivery_date"].year
                expected = dep.pool_year(
                    dep.Pool(year, entry["cost"]), takeover.year + 1
                )
                remaining = expected[0] if expected else Decimal(0)
                if remaining != entry["book_value"]:
                    notes.append(
                        self.env._(
                            "%(code)s %(name)s: asset pool taken over with "
                            "%(remaining).2f (one fifth per year, § 6 (2a) EStG) "
                            "instead of the Papierkram book value %(book).2f.",
                            code=entry["code"],
                            name=entry["name"],
                            remaining=remaining,
                            book=entry["book_value"],
                        )
                    )
            writer.writerow(
                [
                    entry["name"],
                    entry["code"],
                    entry["group"],
                    entry["kind"],
                    entry["delivery_date"].isoformat(),
                    f"{entry['cost']:.2f}",
                    entry["useful_life_months"] or "",
                    entry["method"],
                    "" if pool else f"{entry['accumulated']:.2f}",
                    "" if pool else f"{entry['book_value']:.2f}",
                    f"{entry['memo_value']:.2f}" if entry["memo_value"] else "",
                    entry["delivery_date"].year if pool else "",
                    f"{remaining:.2f}" if pool else "",
                ]
            )
        wizard = self.env["l10n_de_euer.asset.import"].create(
            {
                "company_id": self.company_id.id,
                "opening_date": takeover,
                "file": base64.b64encode(buffer.getvalue().encode()),
                "filename": "papierkram_assets.csv",
            }
        )
        wizard.action_parse()
        for line in wizard.line_ids.filtered("warning"):
            notes.append(f"{line.name}: {line.warning}")
        if wizard.error_count:
            notes.append(
                self.env._(
                    "The fixed assets were not taken over; correct the rows and "
                    "take them over under Asset Register → Take Over Asset "
                    "Register:"
                )
            )
            notes += [
                f"{line.name}: {line.error}"
                for line in wizard.line_ids.filtered("error")
            ]
            return Asset
        wizard.action_import()
        return wizard.line_ids.asset_id

    def _euer_create_purchases(self, entries, notes):
        """Enter the assets acquired after the takeover date as purchases,
        linked to the journal items of their voucher."""
        Asset = self.env["l10n_de_euer.asset"]
        assets = Asset
        business = self.company_id._l10n_de_euer_business_root()
        for entry in entries:
            lines = self._euer_purchase_lines(entry)
            if not lines:
                notes.append(
                    self.env._(
                        "%(code)s %(name)s: no journal item of the purchase found; "
                        "the asset is entered without it.",
                        code=entry["code"],
                        name=entry["name"],
                    )
                )
            values = {
                "name": entry["name"],
                "code": entry["code"],
                "company_id": business.id,
                "asset_kind": entry["kind"],
                "aveuer_group": entry["group"],
                "delivery_date": entry["delivery_date"],
                "origin": "purchase",
                "cost_net": float(entry["cost"]),
                "input_tax": 0.0,
                "method": entry["method"],
                "useful_life_months": entry["useful_life_months"],
                "move_line_ids": [Command.set(lines.ids)],
            }
            try:
                with self.env.cr.savepoint():
                    asset = Asset.create(values)
                    asset.action_activate()
            except (UserError, ValidationError) as error:
                notes.append(
                    self.env._(
                        "%(code)s %(name)s was not entered in the register: %(error)s",
                        code=entry["code"],
                        name=entry["name"],
                        error=error.args[0] if error.args else error,
                    )
                )
                continue
            assets |= asset
        return assets

    def _euer_purchase_lines(self, entry):
        """Journal items of the voucher on fixed asset or depreciation
        accounts; with several assets of one voucher the item of its cost."""
        number = entry["code"].split("/")[0]
        move = self.env["account.move"].search(
            [
                ("company_id", "=", self.company_id.id),
                ("papierkram_number", "=", number),
                ("move_type", "=", "in_invoice"),
            ],
            limit=1,
        )
        Assignment = self.env["l10n_de_euer.account.assignment"]
        root = self.company_id.root_id
        lines = move.invoice_line_ids.filtered(
            lambda line: (
                Assignment._get_assignments(root, line.account_id, line.date)
                .get(line.account_id, Assignment)
                .category_id.role
                in ("av", "afa")
            )
        )
        exact = lines.filtered(
            lambda line: (
                abs(Decimal(str(line.balance)) - entry["cost"]) < Decimal("0.005")
            )
        )
        if exact:
            return exact[:1]
        return lines if len(lines) == 1 else lines.browse()

    def _euer_write_log(self, assets, old, new, notes):
        parts = [
            Markup("<h3>%s</h3><ul>") % self.env._("EÜR asset register"),
            Markup("<li>%s: %s</li>")
            % (
                self.env._(
                    "Taken over with the book value at %s",
                    self.euer_takeover_date.strftime("%d.%m.%Y"),
                ),
                old,
            ),
            Markup("<li>%s: %s</li>") % (self.env._("Acquired later (purchases)"), new),
            Markup("<li>%s: %s</li>")
            % (self.env._("Assets in the register"), len(assets)),
            Markup("</ul>"),
        ]
        if notes:
            parts.append(Markup("<ul>"))
            parts += [Markup("<li>%s</li>") % escape(note) for note in notes]
            parts.append(Markup("</ul>"))
        self.log = (self.log or Markup("")) + Markup("").join(parts)
        self.warning_count += len(notes)
