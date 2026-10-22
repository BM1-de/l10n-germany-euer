1. In Papierkram open *Zeiterfassung → Einzelbuchungen*, choose the filters
   *Noch nicht abgerechnet* and *Gesamter Zeitraum* and click *CSV*.
2. Upload the file as *Unbilled Time Entries (CSV)* in the Papierkram import and
   choose the *Time Entries*: only the unbilled ones, all (history plus the
   unbilled ones as billable), or none.
3. After the import, *Sales Orders* lists the orders with the unbilled work;
   *Create Invoice* on an order bills it.

Odoo rounds the hours of a sales order line to two decimals; Papierkram
computes each time entry to the minute. An invoice can therefore differ from
Papierkram's amount by a few cents.
