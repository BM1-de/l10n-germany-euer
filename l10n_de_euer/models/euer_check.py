# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain

SEVERITIES = [("blocker", "Blocker"), ("warning", "Warning"), ("info", "Info")]
#: context key of the check runs: only they create, change and delete results,
#: always as superuser (a client cannot set ``env.su`` through RPC)
INTERNAL = "l10n_de_euer_check_internal"
#: fields a user may write: the reason, as long as the result is not acknowledged
USER_FIELDS = frozenset({"acknowledge_reason"})
#: checks resolved by a classification of the rest (spec 5.7 no. 2)
RESOLVABLE_CLASSIFICATION = ("C-20", "C-36", "C-37")
#: checks with an action that resolves them
RESOLVABLE = RESOLVABLE_CLASSIFICATION + ("C-23", "C-26", "C-32", "C-33", "C-35")


class L10nDeEuerCheck(models.Model):
    """Result of a check of the EÜR (spec 7.8).

    Results of a check run are stored per business, year and origin. Warnings
    are acknowledged with a reason; blockers prevent the finalisation. Results
    are created, changed and deleted only by the check runs; users enter the
    reason and acknowledge with :meth:`action_acknowledge` (G-05, G-06).
    """

    _name = "l10n_de_euer.check"
    _inherit = ["mail.thread"]
    _description = "EÜR Check Result"
    _order = "year desc, severity, code, id"
    _check_company_auto = True

    company_id = fields.Many2one(
        "res.company",
        required=True,
        index=True,
        default=lambda self: self.env.company.root_id,
    )
    year = fields.Integer(required=True, index=True)
    origin = fields.Selection(
        [("assignment", "Account Assignment"), ("calculation", "Calculation")],
        required=True,
        default="assignment",
        help="Check run that produced the result.",
    )
    code = fields.Char(required=True, index=True, help="Check code, e.g. C-01.")
    severity = fields.Selection(SEVERITIES, required=True)
    message = fields.Text(required=True)
    res_model = fields.Char(string="Related Model")
    res_ids = fields.Json(string="Related Records")
    record_count = fields.Integer(compute="_compute_record_count")
    resolvable = fields.Boolean(compute="_compute_record_count")
    amount = fields.Monetary(currency_field="currency_id")
    currency_id = fields.Many2one(related="company_id.currency_id")
    acknowledged_by_id = fields.Many2one(
        "res.users", readonly=True, copy=False, tracking=True
    )
    acknowledged_on = fields.Datetime(readonly=True, copy=False, tracking=True)
    acknowledge_reason = fields.Text(copy=False, tracking=True)

    @api.depends("res_ids", "code")
    def _compute_record_count(self):
        for check in self:
            check.record_count = len(check.res_ids or [])
            check.resolvable = check.code in RESOLVABLE and bool(check.res_ids)

    def action_open_records(self):
        self.ensure_one()
        if not self.res_model or not self.res_ids:
            return False
        return {
            "type": "ir.actions.act_window",
            "name": self.code,
            "res_model": self.res_model,
            "view_mode": "list,form",
            "domain": [("id", "in", self.res_ids)],
        }

    def action_resolve(self):
        """Open what resolves the result (classification, takeover, review).

        C-20, C-36, C-37: classification of the rest; C-35: takeover of the
        open item; C-23, C-26: review list of the turn of the year; C-32,
        C-33: EÜR information of the document (VAT rate of the supply).
        """
        self.ensure_one()
        ids = self.res_ids or []
        if self.code in RESOLVABLE_CLASSIFICATION and self.res_model == (
            "account.move.line"
        ):
            line = self.env["account.move.line"].browse(ids[:1])
            existing = self.env["l10n_de_euer.residual.classification"].search(
                [("source_line_id", "=", line.id)], limit=1
            )
            return self._form_action(
                "l10n_de_euer.residual.classification",
                existing.id,
                {
                    "default_source_line_id": line.id,
                    "default_company_id": self.company_id.id,
                    "default_check_code": self.code,
                    "default_amount_classified": abs(self.amount),
                    "default_partial_path": False,
                },
            )
        if self.code == "C-35" and self.res_model == "account.move.line":
            line = self.env["account.move.line"].browse(ids[:1])
            existing = self.env["l10n_de_euer.opening.item"].search(
                [("move_line_id", "=", line.id)], limit=1
            )
            return self._form_action(
                "l10n_de_euer.opening.item",
                existing.id,
                {
                    "default_move_line_id": line.id,
                    "default_company_id": self.company_id.id,
                },
            )
        if self.code in ("C-23", "C-26") and self.res_model == "account.move":
            action = self.env["ir.actions.act_window"]._for_xml_id(
                "l10n_de_euer.action_l10n_de_euer_ten_day_item"
            )
            action["domain"] = [
                "|",
                ("source_move_id", "in", ids),
                ("origin_move_id", "in", ids),
            ]
            return action
        if self.code in ("C-32", "C-33") and self.res_model == "account.move":
            moves = self.env["account.move"].browse(ids)
            document = moves.filtered(lambda move: move.is_invoice()) or moves
            return document[:1].action_l10n_de_euer_info()
        return self.action_open_records()

    def _form_action(self, model, res_id, context):
        return {
            "type": "ir.actions.act_window",
            "res_model": model,
            "res_id": res_id or False,
            "view_mode": "form",
            "target": "current",
            "context": context if not res_id else {},
        }

    @api.model
    def _is_internal(self):
        return self.env.su and bool(self.env.context.get(INTERNAL))

    @api.model_create_multi
    def create(self, vals_list):
        if not self._is_internal():
            raise UserError(self.env._("Check results are created by the checks."))
        return super().create(vals_list)

    def write(self, vals):
        if not self._is_internal():
            if set(vals) - USER_FIELDS:
                raise UserError(
                    self.env._(
                        "Check results are changed by the checks only; acknowledge "
                        "a warning with the action Acknowledge."
                    )
                )
            if self.filtered("acknowledged_on"):
                raise UserError(
                    self.env._(
                        "The reason of an acknowledged result cannot be changed."
                    )
                )
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_only_by_checks(self):
        if not self._is_internal():
            raise UserError(self.env._("Check results are deleted by the checks."))

    def action_acknowledge(self):
        """Acknowledge warnings with the reason entered on the result."""
        self.check_access("write")
        for check in self:
            if check.severity == "blocker":
                raise UserError(
                    self.env._("A blocker cannot be acknowledged; resolve it.")
                )
            if check.acknowledged_on:
                raise UserError(
                    self.env._("%(code)s is already acknowledged.", code=check.code)
                )
            if not check.acknowledge_reason:
                raise UserError(
                    self.env._(
                        "Enter a reason to acknowledge %(code)s.", code=check.code
                    )
                )
        self.sudo().with_context(**{INTERNAL: True}).write(
            {
                "acknowledged_by_id": self.env.uid,
                "acknowledged_on": fields.Datetime.now(),
            }
        )

    @api.model
    def _key(self, values):
        return (
            values["code"],
            values.get("res_model") or False,
            tuple(values.get("res_ids") or ()),
        )

    @api.model
    def _store_results(self, company, year, origin, results):
        """Replace the open results of a check run.

        Acknowledged warnings stay as long as the same result comes again with
        the same message and amount. If the content changed (e.g. more lines
        booked on the account), the acknowledgement is reset with the new
        values. Acknowledged blockers (not possible through the user
        interface) are never kept.
        """
        Check = self.sudo().with_context(
            mail_create_nolog=True, mail_create_nosubscribe=True, **{INTERNAL: True}
        )
        # results of the calculation and the asset register belong to the
        # business (a branch can be a business of its own); the account
        # assignment to the main company
        root = (
            company.root_id
            if origin == "assignment"
            else company._l10n_de_euer_business_root()
        )
        previous = Check.search(
            Domain("company_id", "=", root.id)
            & Domain("year", "=", year)
            & Domain("origin", "=", origin)
        )
        acknowledged = {
            self._key(check._key_values()): check
            for check in previous
            if check.acknowledged_on and check.severity != "blocker"
        }
        new_results = []
        kept = Check.browse()
        for result in results:
            check = acknowledged.get(self._key(result))
            if not check or check in kept:
                new_results.append(result)
                continue
            kept |= check
            if check.message != result["message"] or not root.currency_id.is_zero(
                check.amount - (result.get("amount") or 0.0)
            ):
                check.write(
                    {
                        "severity": result["severity"],
                        "message": result["message"],
                        "amount": result.get("amount") or 0.0,
                        "acknowledged_by_id": False,
                        "acknowledged_on": False,
                    }
                )
                check.message_post(
                    body=self.env._(
                        "The result changed since its acknowledgement; acknowledge "
                        "it again."
                    )
                )
        # results that no longer occur go, acknowledged or not
        (previous - kept).unlink()
        return Check.create(
            [
                {"company_id": root.id, "year": year, "origin": origin, **result}
                for result in new_results
            ]
        ).with_env(self.env)

    def _key_values(self):
        self.ensure_one()
        return {
            "code": self.code,
            "res_model": self.res_model,
            "res_ids": self.res_ids,
        }

    @api.model
    def _run_assignment_checks(self, company, year):
        """Run checks C-01 to C-09, C-30, C-31 and C-50 and store the results."""
        self.check_access("write")
        results = self.env["l10n_de_euer.assignment.checker"]._run(company, year)
        self._store_results(company, year, "assignment", results)
        return self.search(
            Domain("company_id", "=", company.root_id.id)
            & Domain("year", "=", year)
            & Domain("origin", "=", "assignment")
        )
