# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from .models.euer_account_assignment import DEFAULT_CHARTS


def post_init_hook(env):
    """Add the default account assignments of existing SKR03/SKR04 businesses.

    Form years, categories, mappings and rule parameters are data files.
    Companies that already use the chart template SKR03 or SKR04 get the
    default assignments of their accounts (spec 4.3, K-02): found by the XML id
    of the template account (``account.<company id>_<template id>``) and the
    accounting setup (journals). Only missing assignments are created; existing
    ones are never changed. Companies created later get them when their chart
    template is loaded.
    """
    # archived companies too: they may be reactivated for a last statement
    companies = (
        env["res.company"]
        .with_context(active_test=False)
        .search([("parent_id", "=", False), ("chart_template", "in", DEFAULT_CHARTS)])
    )
    env["l10n_de_euer.account.assignment"]._prefill_defaults(companies)


def uninstall_hook(env):
    """Remove the account assignments before the categories.

    The assignments refer to the categories with ``ondelete="restrict"``;
    without this the removal of the category data logs an error.
    """
    env["l10n_de_euer.account.assignment"].sudo().with_context(
        active_test=False
    ).search([]).unlink()
