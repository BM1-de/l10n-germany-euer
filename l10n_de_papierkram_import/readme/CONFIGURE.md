The company must use the German chart of accounts SKR03 or SKR04 (`l10n_de`).

The import creates four journals unless you select existing ones: *Papierkram
Invoices*, *Papierkram Vouchers*, *Papierkram Depreciation* and *Papierkram
Payments*. Payments of the payment journal are posted directly on its money
account so that each payment is a cash entry with its date.

Do not set a lock date before importing: Odoo moves entries of a locked period
behind the lock date, which would change payment dates.
