**Setup of a business** (*Accounting*, Community: *Invoicing*, *→
Configuration → EÜR → EÜR Setup*, accounting managers). The wizard collects
the settings in one place; it books nothing and never sets a lock date.

1. Enable the cash-basis profit statement for the company and enter the
   header values of the form: type of business, legal form, type of income and
   owner.
2. *VAT Status Periods*: small business (§ 19 UStG) or standard taxation, with
   the return period and the permanent extension (Dauerfristverlängerung).
   The treatment is never derived from the taxes of the bookings.
3. Small businesses switch the default accounts and taxes to the accounts
   without VAT (SKR03 8195/3200, SKR04 4185/5200); otherwise every booking
   with VAT is reported (C-30).
4. Private matters: the private share account of the purchase journals takes
   the private share of a bill (e.g. of a phone bill); the private accounts
   (SKR03 1800/1890, SKR04 2100/2180) give the withdrawals and contributions
   (Kz 122/123). Mark the money journal of a private account without a
   separate business account as *private account* (R-18): business income
   received there is a withdrawal, business expenses paid from it are a
   contribution.
5. Payment providers and credit cards (PayPal, Stripe, card accounts): the
   booking decides when the money counts. With a bank journal of its own the
   money counts when it is credited to the provider or card account; booked
   on a reconcilable clearing account it counts with the payout to the bank
   account.
6. Business entertainment: by default 70 % are booked on 4650 (SKR04 6640)
   and 30 % on 4654 (6644). If you book the full amount on 4650, choose *Split
   70/30 automatically* (also in *Accounting → Configuration → Settings →
   EÜR*).
7. Lock date: set the global lock date (fiscal year lock date) to 31 December
   of a year before its statement is finalised. Prefer it to the hard lock
   date, which allows no exception for December lines imported late (C-70).
8. *Takeover from a Previous System*: the open receivables and payables of the
   opening entries get their category, VAT treatment and rate (C-35); the
   turnover of the previous year goes into the chosen VAT status period, whose
   current input the dialog shows (an empty amount stores nothing, tick
   *No Turnover in the Previous Year* to confirm 0); the asset register of the
   previous system is imported by CSV (*Take Over Asset Register*).

Then check the account assignment of the business (*Check Account
Assignment*, see *Usage*).

**Form data.** No configuration is needed for the form data.

- Form years, form fields and rule parameters are shared by all companies. They
  are listed under *Accounting* (Community: *Invoicing*) *→ Configuration →
  EÜR* and can only be changed by system administrators.
- A form year is delivered in state *Draft*. It is set to *Verified* only after
  the review by a person who did not create the data (tax advisor or second
  maintainer); name, role and date of the review are mandatory.
- Corrections of a delivered form year are added as a new version with its own
  records. The old version is marked as superseded and stays unchanged as soon as
  a finalised statement uses it.
- The form years are delivered with the module; you need no generator. The
  maintainers generate a new form year from the BMF letter with the official
  forms and instructions (PDF published by the Federal Ministry of Finance) and
  a table of its lines, Kennzahlen, labels and formulas prepared from it; that
  table and the notes are not part of the repository. The generator
  `_dev/werkzeuge/gen_forms.py` (option `--spec`, folder of the table) checks
  every pair of line and Kennzahl, all sums and transfers and writes
  `data/forms/euer_<year>.xml`; `_dev/werkzeuge/check_forms.py` then compares
  the delivered data with the text of the official PDF (`pdftotext`) for the
  release of the form year.

**Account assignment.** Every account gets an EÜR category per business (main
company with its branches) and period. The category says how the account is
treated (money, open item, tax, income, expense, private, fixed asset,
depreciation, opening balance, neutral) and, per form year, into which fields
its amounts go.

- Companies with the chart template SKR03 or SKR04 get the default assignment of
  all template accounts when the module is installed, and new companies when
  their chart template is loaded. Bank, cash and credit card accounts, suspense
  and transfer accounts are assigned from the journals. Only missing assignments
  are created; corrections are never overwritten, also not by reloading the
  chart template.
- Accounts you create yourself get a proposal (from the code range of the chart
  template), never a silent assignment. Other charts of accounts have no
  defaults; the checks list every account that is missing.
- Assignments with low confidence (ambiguous cases A01–A34) must be confirmed
  once the account is used. A change of use gets a new period (*New Period*);
  a correction of an error changes the record. All changes are logged.
- *Start of the Activity* (tab *EÜR* of the main company, *EÜR Setup*): in the
  year the activity starts the small business limit of the current year is
  25,000 € instead of 100,000 € and there is no previous year (§ 19 (1) UStG
  from 2025, section 19.1 (3) UStAE). A turnover of the previous year entered on
  a VAT status period (takeover from another system) counts for the year entered
  next to it (*For the Year*, empty: the first year of the period) only; later
  years take the turnover from the data. A turnover of 0 counts only with its
  year (in the takeover: *No Turnover in the Previous Year*).
- Gifts: the category *Gifts (deductible)* allows collective entries without
  recipient for low-value promotional gifts (R 4.11 (2) EStR), booked on the
  gift accounts 4630 (SKR04 6610). The DATEV account *Streuartikel* 4605 (6605)
  stays an advertising account (Kz 224) and is not part of the gift check.
- Gifts per recipient: enter the recipient on the bill or the bank entry (tab
  *EÜR*, *Recipient of the Gifts*). The partner of a bill or a bank entry is the
  shop, never the recipient; without a recipient a gift is blocked (C-51). A bill
  with gifts for several recipients gets the recipients per line (*All EÜR
  Information → Recipients of the Gifts per Line*, with the number or share of
  the gifts of the line). The 50 € limit counts all gifts of the year to the
  recipient, paid or not, by the date the gift is handed over: enter *Date of the
  Gift* if it differs from the date of supply (bought in December, handed over
  in January). Gifts booked on the account for non-deductible gifts (4635, SKR04
  6620) stay non-deductible without recipient.
- SKR03 4980: in DATEV it is *Sonstiger Betriebsbedarf* (work equipment,
  Kz 229, like SKR04 6850). The German name of the Odoo template wrongly says
  *Mietleasing ... technische Anlagen*; the assignment must therefore be
  confirmed once the account is used.
- Categories and their fields per form year are delivered as data and can only
  be changed by system administrators. The maintainers generate the defaults
  with `_dev/werkzeuge/gen_mapping.py` from a classification of every account
  of the SKR03 and SKR04 templates of Odoo (not part of the repository; option
  `--spec`) and the template files of `l10n_de` (option `--templates`).
