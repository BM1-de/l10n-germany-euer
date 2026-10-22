Papierkram exports one ZIP file per year, cut by the *creation date* of each
document. A payment therefore appears in the export of the year its document was
created, which is often not the year it was paid. The module merges all yearly
exports before importing.

Some payments are not listed in Papierkram's payment table because they were
matched with a bank transaction. The module derives them from the invoice's last
payment date or the voucher's payment date and lists them separately in the log.

This module helps with migrating bookkeeping data. It does not replace the review
by the taxpayer or a tax advisor; check the log and the imported figures before
relying on them.
