**Course of the year.** Book as usual during the year; the module only
reads the bookings. For the statement of a year:

1. Import the bank and cash statements up to 31 December, including the last
   December lines, before you set a lock date (Odoo moves the date of an entry
   in a locked period).
2. Create the statement of the business and year (*Accounting → Reporting →
   EÜR → Statements*, Community: *Invoicing → Reporting → EÜR*) and answer the
   *Questionnaire*; an accounting manager confirms it.
3. *Compute* the statement.
4. *Classification of Rests*: classify every payment the computation cannot
   assign to a document (advance payment, refund, write-off, returned direct
   debit; C-20, C-36, C-37).
5. *Turn of the Year*: decide every item of the review list with a reason:
   regularly recurring payments within ten days around 31 December, such as
   rent or VAT returns (C-26), and transfers ordered in December but debited
   in the first days of January (C-23); then compute again. *Apply* needs the
   economic year the payment belongs to (the year before for a payment in
   January, the year after for one in December): with the year of payment the
   decision moves nothing and is reported (C-26). For a direct debit of the
   tax office whose due date falls on a weekend (10 January 2026 and 2027) the
   module proposes the year of payment: it reads the period for the payment as
   ending on the next working day (§ 108 (3), § 224 (2) no. 3 AO, H 11 EStH).
   *Apply* stays possible, but raises a warning (C-26) that must be
   acknowledged with a reason before the finalisation. The legal question is
   open (see *Known issues / Roadmap*).
6. *Bank Reconciliation 31 December*: enter the balance per statement of
   account of every money journal and attach the statement (C-38).
7. *Profit Corrections*: enter the values without bookings (private use,
   flat rates, home office) with a reason and an own receipt.
8. *Run Checks*: solve the blockers and acknowledge the warnings with a reason.
9. Set the global lock date (fiscal year lock date) to 31 December and
   *Finalise* (accounting managers).
10. Enter the values in *Mein ELSTER* with the *Fill-In Aid* and keep the
    outputs with the receipts.
11. Enter the date and the transfer ticket of the submission on the tab
    *Finalisation*.

**Booking rules.** The computation follows the money through the
reconciliations. Book so that every payment reaches its document:

- Book a returned direct debit as a statement line of its own and reconcile
  it with the receivable account of the customer: it creates a new open item.
  Do not undo the reconciliation of the original payment. Reconcile the next
  payment of the customer with this new item and book the fee on the costs
  of money transfer (SKR03 4970, SKR04 6855). Write off an amount the customer
  never pays only through the classification (C-36).
- Wages, advance payments received and other amounts booked on a balance
  account that counts at payment (payroll liabilities SKR03 1740–1742,
  advance payments 1710–1720 and the like) count when they are paid: the
  entry "4120 to 1740" is only documented (C-03), the payment "1740 to bank"
  is the expense. Non-cash benefits booked against revenue (e.g. "4120 to
  8590") count on the date of the entry.
- Do not book bills against a liability account that cannot be reconciled
  (e.g. "4930/1576 to 1700" and later "1700 to bank"): such an expense would
  get lost (blocker C-18, C-04). Enter the bill as a vendor bill on the
  payable account of the partner, or make the account reconcilable.
- Transfers between tax accounts (e.g. the VAT settlement "1776 to 1789") and
  between balance accounts (e.g. a loan "1700 to 0630") count nothing: VAT
  counts when it is paid to or refunded by the tax office (Kz 186, 141).
- Reclassify an expense in the year it was paid: a transfer between expense
  accounts counts on its date (C-03 points out a different year).
- Do not book depreciation in the ledger: the asset register is the only
  source of depreciation, low-value assets and asset pools (C-68 compares the
  ledger with it).
- Enter the recipient of gifts on the bill or the bank entry (C-51), and book
  a private share of a purchase with the private share account of the
  purchase journal (*EÜR Setup*).

**Form data and account assignment** (*Accounting*, Community: *Invoicing*,
*→ Configuration → EÜR*):

1. *Form Years* shows the official forms per assessment year with their
   source (BMF letter, form ids, checksum of the evaluated PDF).
2. Open *Form Fields* to look up a line: the stable code, the Kennzahl, the line
   of the respective year, the official wording, the formula and the program
   scope (supported, manual input or not supported).
3. *Rule Parameters* lists the amounts and rates of the tax rules with their
   period of validity and legal source.
4. *Account Assignment* lists the EÜR category of every account with period,
   confidence and confirmation; the account form shows the current category.
5. *Check Account Assignment*: choose the business and year. *Show Accounts*
   lists every account with posted lines in the year with category, confidence,
   confirmation, proposal and amount; filter for accounts that are not assigned,
   with low confidence and unconfirmed, or not provided for in the EÜR. Apply the
   proposal or confirm the assignment from the list. *Run Checks* stores the
   checks C-01 to C-09, C-30, C-31 and C-50 under *EÜR Checks*; warnings are
   acknowledged with a reason.

**Asset register** (*Accounting → Asset Register (EÜR)*): the register is the
only source of depreciation, low-value assets, asset pools and *Anlage AVEÜR*;
depreciation booked in the ledger is compared with it, never counted twice.

1. Create an asset from the journal items of a purchase (list of journal items,
   action *Create EÜR Asset*): delivery date, net cost, input tax and AVEÜR group
   are proposed. Check the kind (depreciable, low-value asset, asset pool …),
   the method and the useful life, then *Enter in Register*.
2. Additional costs, subsidies, special depreciation (§ 7g (5) and § 7b EStG),
   extraordinary depreciation and changes of the useful life or the business
   share are events of the asset. *Yearly Values* shows the calculation per
   year; a use withdrawal for private use is confirmed or rejected there. A
   building with linear depreciation keeps its percentage (§ 7 (4) sentence 1
   EStG): later costs, subsidies and extraordinary depreciation change its
   basis; enter a remaining useful life with such an event only to spread the
   book value over it. A contributed asset is depreciated over its remaining
   useful life, which is proposed from the private acquisition.
3. A sale, withdrawal or scrapping is entered on the asset and recorded with
   *Record Disposal*.
4. *Take Over Asset Register* (accounting managers) imports the register of a
   previous system by CSV with the book values at the takeover date; the years
   before are not calculated again. The preview tries every row and shows all
   errors before anything is imported. Special depreciation under § 7g EStG of
   the previous system belongs in the columns *Sonder-AfA § 7g* and *Jahr
   Sonder-AfA § 7g*: after its period the register spreads the book value over
   the remaining useful life (§ 7a (9) EStG); the preview points out book values
   below the linear depreciation. Enter the book values per group of the filed
   *Anlage AVEÜR* to have them checked in the following year.
5. *Check Asset Register* runs the checks C-30, C-40, C-42 and C-61 to C-69.
   *Compute Year* runs them as well, with the computed form values, so C-62
   compares the transfers into *Anlage EÜR* as computed.

**Computation of the year** (*Accounting → Reporting → EÜR → Compute Year*,
Community: *Invoicing → Reporting → EÜR*): choose the business and the year.
The computation reads the bookings, assigns every inflow and outflow to its
year (§ 11 EStG) and stores the result under *EÜR Calculations* with its
slices and checks. The form values combine the payments with the amounts of
the asset register of the same business (depreciation, low-value assets,
asset pools, withdrawals and contributions of assets): the purchase of an
asset is neutral, and depreciation booked in the ledger is not counted. A
branch marked as separate business has its own computation and its own asset
register.

**Statement of the year** in detail (*Accounting → Reporting → EÜR →
Statements*, Community: *Invoicing → Reporting → EÜR*): one statement per
business and year.

1. *Questionnaire*: answer the questions on the program scope; an accounting
   manager confirms them (*Confirm Questionnaire*). The answers of the previous
   year are proposed, but count only after the confirmation. A case outside the
   program scope blocks the finalisation.
2. *Profit Corrections*: values without bookings, i.e. private use of vehicles
   (Kz 106), withdrawals of goods, use or services (Kz 108) and the VAT on them,
   business trips with a private vehicle (Kz 147), commuting allowance (Kz 176
   and 142), daily home office allowance (Kz 163), home office (Kz 172 and 162),
   additional meal expenses (Kz 171), flat rate of business expenses (Kz 195),
   tax-free income and § 3c expenses (Kz 240 to 245), income in kind and
   barter (Kz 111, 112 or 103). The amounts are computed with the rule
   parameters of the year; the yearly maximums hold for all corrections of
   the year together; withdrawals and contributions (Kz 122 and 123) follow
   automatically. A small business enters no VAT; a taxable free supply
   (input tax deducted on the good or its costs) needs its VAT base, which
   counts in the total turnover of the small business limit. Every
   correction needs a reason; attach the own receipt. Use withdrawals
   proposed by the asset register can be taken over as corrections; the
   register then leaves them out.
3. *Compute* runs the computation of the year with the corrections and fills
   *Anlage EÜR* and *AVEÜR* with line, Kz, previous year and source. From a
   value, *Show records* leads to its slices. A value can be overridden with a
   reason.
4. *Bank Reconciliation 31 December*: enter the balance per statement of
   account for every money journal and attach the statement.
5. *Run Checks* runs all checks (account assignment, computation, asset
   register, statement); blockers and warnings are listed on the statement.
   Set the global lock date of the year before the finalisation.
6. *Finalise* (accounting managers) runs all checks again and finalises the
   statement only without blockers, with all warnings acknowledged, with the
   questionnaire confirmed, the bank reconciled at 31 December, a lock date
   that covers 31 December, released form years and a computation that
   matches the current bookings. The statement, its values, slices, profit
   corrections, answers and bank reconciliation, the asset years of the year
   and the form years it uses can then no longer be changed or deleted, also
   not by an administrator. The data of the statement is stored as canonical
   JSON (amounts in cent) with its SHA-256, chained to the previous
   finalisation of the business. Keep finalised statements with their
   receipts for at least 10 years (§ 147 (3) AO).
7. *Check Integrity* computes the hashes again, follows the chain and
   compares the finalised records and the content of their receipts with the
   stored data; the result is logged in the chatter. Changes made directly in
   the database cannot be prevented, but they become visible. Keep the payload
   SHA-256 and the chain hash of each finalisation also outside the database
   (e.g. the PDF of the fill-in aid): a consistent change of the latest
   finalisation with its hashes is visible only against them.
8. *Compare with Bookings* computes a finalised year again into a comparison
   run next to the finalised one (not while a correction of the year is in
   progress: compute the correction instead). Every deviation per field, per
   sum of slices and of the bank balance at 31 December is the warning C-60;
   the finalised statement does not change. Every other computation of the
   year shows the deviations as well.
9. *Create Correction* (accounting managers) creates a new version with a
   reason. It starts with the values, overrides, profit corrections and the
   bank reconciliation of the finalised version; the questionnaire is
   confirmed again. The tab *Differences to the Previous Version* lists what
   changed. When the correction is finalised, the previous version is
   superseded and stays readable.
10. Enter the date and the transfer ticket of the submission on the tab
    *Finalisation*; these two fields can be entered after the finalisation.

**Drilldown to the receipt.** From a value of the statement, *Show records*
lists its slices (group them by account, partner or month). A slice opens its
money entry, its document, the attached receipts and the partial
reconciliations the money passed; a value of the asset register opens the
assets with their yearly values and journal items (*Show assets*); a value of
a profit correction opens the correction and its own receipt. Backwards, the
smart button *EÜR Slices* of a journal entry lists its slices with year and Kz.

**Outputs** (tab *Outputs* of a computed statement, also in the *Print* menu).
All outputs show the values of the statement and only the data of its
business; auditors (read-only accounting) can print and download them.

- *Fill-In Aid Mein ELSTER* (HTML, PDF): the fields with a value and the
  mandatory header fields in the order of the official form, with line, Kz,
  official wording, value and previous year; sums are control values to
  compare with Mein ELSTER and to enter where Mein ELSTER does not compute them
  itself (not confirmed); *Anlage AVEÜR* per group and per asset; state, hash
  and versions; Kz 15.120 only after the questionnaire is confirmed. Enter
  the values in Mein ELSTER yourself: the module transmits nothing, and the
  fill-in aid is no form for a paper filing. The HTML view is accessible
  (tables with caption and header cells, the minus as text).
- *Evidence per Kennzahl* (PDF, XLSX): every slice of every field with cash
  date, year, document, money entry, partner, account, tax, share, amount and
  source; the sum per field is the value of the statement.
- *Workbook* (XLSX): EÜR, AVEÜR, evidence, asset register, low-value assets,
  asset pools, withdrawals and contributions, gifts per recipient (give-aways
  separately), business entertainment, check protocol and profit corrections.
- *List of the Kennzahlen for ELSTER* (CSV, UTF-8, semicolon): year, form,
  version, box, line, Kz, code, official wording, amount, ELSTER field id,
  XPath, verified, source; only fields with a value. The ELSTER field ids are
  marked as not verified until they are checked against the official
  documentation.
- *Data Set* (JSON, schema `l10n_de_euer/1`): year, form versions, business,
  fields, AVEÜR per group and per asset, state and the hash of the
  finalisation. Amounts are texts with a decimal point.
- *Data Access Z3* (ZIP, § 147 (6) AO): slices, field values, account
  assignment and the registers as CSV with header line, semicolon, decimal
  comma and CRLF, an `index.xml` after the description standard for the data
  transfer and the description of the fields as JSON.
- *Check Protocol* (PDF, XLSX): all checks with their state and
  acknowledgement.
- *Procedure Documentation* (HTML, PDF): a template from the data of the
  module (data flow, calculation rules, account assignment, settings, roles,
  lock concept, versions); complete it with the organisation of your business.

**Setup and takeover** (*Accounting → Configuration → EÜR*, accounting
managers):

1. *EÜR Setup*: enable cash basis accounting, enter the header values of the
   form, create the VAT status period (with the turnover of the previous year
   for the small business thresholds after a takeover), enter the start of the
   activity of a business founded recently, for small businesses switch the
   default accounts and taxes to the accounts without VAT (8195/3200, SKR04
   4185/5200), set the private share account of the purchase journals, mark
   the money journals of a private account and choose the treatment of
   business entertainment. The wizard recommends the fiscal year lock date
   but never sets a lock date.
2. *Takeover from a Previous System*: *Read Opening Entries* lists the open
   receivables and payables of the opening entries (opening move or entries
   with an opening balance account). Enter the category, the VAT treatment and
   the VAT rate of each original invoice and *Take Over*; without them the
   payment of the item is blocked (C-35). The turnover of the previous year
   goes into the VAT status period with the year it is entered for; without an
   amount nothing is stored unless *No Turnover in the Previous Year* confirms 0
   (C-32). *Take Over Asset Register* opens the CSV takeover of the asset
   register.
