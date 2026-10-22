Imports the accounting data of a [Papierkram](https://www.papierkram.de) account
into Odoo: contacts, customer invoices, cancellations and credit notes, expense
and income vouchers, payments, fixed assets with their depreciation, and the
original PDF documents.

Every payment is posted as a cash entry with its own payment date on a dedicated
money account. A cash-basis profit computation (German *Einnahmenüberschuss­rechnung*,
see `l10n_de_euer`) can therefore tell exactly when money came in or went out.
Imported documents keep their Papierkram number, and an import can be run again
without creating duplicates.
