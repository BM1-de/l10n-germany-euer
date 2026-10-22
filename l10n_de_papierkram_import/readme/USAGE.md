1. In Papierkram, open *Einstellungen → Export und Import* and create one export
   *Vollständig (CSV, PDF)* for every year, including the first one.
2. Optional but recommended:
   - *Ausgaben → Anlagegüter*, filter *Alle*, button *CSV*: the fixed assets.
   - The category list with SKR03 accounts (CSV with the columns `name`, `skr03`,
     `abschreibungsart`, `typ`). Without it, vouchers can only be imported if all
     their categories are known.
   - For vouchers with several categories, a split file with the columns
     `belegnummer;position;kategorie;betrag_brutto`; Papierkram's export does not
     contain the amount per category.
3. In Odoo, go to *Invoicing → Configuration → Papierkram Import*, create an
   import and upload the files.
4. Choose the *Scope*: the complete history, or only the open items (unpaid
   invoices and vouchers with their partial payments). A later import with the
   complete history adds the rest; documents already imported are skipped,
   also by a new import of the same company.
5. Click *Check* and read the log. Blocking issues must be solved first.
6. Click *Import*.

A document imported once is not updated by a later import: a payment that
Papierkram records afterwards for an imported invoice has to be entered in
Odoo.

With the cash-basis profit computation `l10n_de_euer`, the module
`l10n_de_papierkram_import_euer` (installed automatically) also takes the fixed
assets over into its asset register; for an import made before, the button
*Take Over Fixed Assets (EÜR)* does it later. With `sale_timesheet`, the module
`l10n_de_papierkram_import_timesheet` (installed automatically) imports
products, time entries and the unbilled work.
