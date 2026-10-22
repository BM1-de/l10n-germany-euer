- Papierkram's export does not tell which time entries are billed: only the
  list of time entries exported with the filter *Noch nicht abgerechnet* gives
  the unbilled work. With *All Time Entries* the rest is imported as history.
- Time entries of a person without a user in Odoo get an employee of that name
  in the company of the import.
- Odoo rounds the hours of a sales order line to two decimals, so an invoice
  can differ from Papierkram's amount by a few cents.
