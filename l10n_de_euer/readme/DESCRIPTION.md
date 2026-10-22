This module computes the German cash-basis profit statement
(*Einnahmenüberschussrechnung*, § 4 (3) EStG) with the official forms
*Anlage EÜR* and *Anlage AVEÜR*. Income and expenses count in the year the money
comes in or goes out (§ 11 EStG), traced from the bank and cash lines in Odoo back
to the original document.

The module provides:

- the official forms 2024, 2025 and 2026 (*Anlage EÜR*, *AVEÜR* and *SZ*) as
  data, every line with a stable code `FORM.BOX.KZ` (e.g. `EUER.20.111`), its
  line number per year, the official wording, sign rules and formulas for all
  sums and transfers, and the rule parameters per year with their legal source;
- the EÜR categories with their form fields per year and the default account
  assignment for SKR03 and SKR04 (with proposals for accounts you create);
- the computation of a year: every receipt and payment is traced over the
  reconciliations to its document and split into slices with cash date, year
  and form field (§ 11 EStG, ten-day rule, VAT of small businesses and standard
  taxation, private withdrawals and contributions, gifts and entertainment);
- the asset register with depreciation, low-value assets, asset pools and
  *Anlage AVEÜR*;
- the statement of the year with the questionnaire on the program scope,
  profit corrections with own receipts, the bank reconciliation of 31
  December, more than 60 checks, the finalisation with a SHA-256 hash chain and
  corrections as new versions;
- the outputs: an accessible fill-in aid for *Mein ELSTER* (HTML and PDF), the
  evidence per Kennzahl down to the receipt, a workbook with the registers of
  § 4 (7) EStG, the list of the Kennzahlen as CSV, a machine readable data set
  (JSON), the data access package Z3 with `index.xml` (§ 147 (6) AO), the check
  protocol and a template of the procedure documentation;
- the setup of a business and the takeover of open items from a previous
  system.

**Important notice:** This module is a tool for preparing the cash-basis profit
statement according to § 4 (3) EStG and the form *Anlage EÜR*. It does not
replace tax advice. The taxpayer alone is responsible for the correctness and
completeness of the tax return and of the data transmitted to the tax office.
Check all values before transmission, in particular for new form years. The
module does not transmit any data to the tax authorities and does not change any
journal entries. The program scope and the cases that are not supported are
listed in the section "Known issues / Roadmap".

The software is provided without any warranty (sections 15 and 16 of the GNU
AGPL-3.0). Mandatory statutory liability remains unaffected.

**Development status Alpha, not independently checked:** the calculation, the two
reference cases and the data of the form years were created and checked by the
maintainer only; an independent check by a tax expert is still pending. The form
years are delivered as drafts: you can compute and compare a statement, but you can
only finalize it after a qualified person who did not create the data has released
the form year (see "Configuration").
