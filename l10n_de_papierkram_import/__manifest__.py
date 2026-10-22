# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
{
    "name": "Papierkram Import",
    "summary": "Import contacts, invoices, vouchers, payments and assets "
    "from Papierkram",
    "version": "19.0.1.0.0",
    "category": "Accounting/Localizations",
    "website": "https://github.com/BM1-de/l10n-germany-euer",
    "author": "Baumgärtner Marketing GmbH",
    "maintainers": ["bm1-phillip"],
    "license": "AGPL-3",
    "development_status": "Alpha",
    "depends": ["account", "l10n_de", "mail"],
    "data": [
        "security/ir.model.access.csv",
        "views/papierkram_import_views.xml",
    ],
    "installable": True,
}
