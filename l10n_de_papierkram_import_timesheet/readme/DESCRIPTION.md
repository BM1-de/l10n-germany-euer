Adds products, projects, tasks and time entries to the Papierkram import, and
makes the work that is not billed yet in Papierkram billable in Odoo.
Installed automatically together with `sale_timesheet`.

- The goods and services of Papierkram become products; services that
  Papierkram uses in time entries are billed by the time entries.
- Time entries without project ("Kein Projekt") go into one project per
  customer, "Retainer (customer)"; time entries of a Papierkram project into a
  project of that name. Each Papierkram task becomes a task of the project.
- Papierkram's export does not tell which time entries are billed. The list of
  time entries does: exported with the filter "Noch nicht abgerechnet", it
  gives the unbilled work. For it the import creates a confirmed sales order
  per customer with a line per service and rate; "Create Invoice" bills the
  time entries, and later time entries on the same task are billed on the
  same order.
- Every time entry keeps its Papierkram identity: a second import skips the
  time entries already present.
