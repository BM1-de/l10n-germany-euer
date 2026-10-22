**Program scope (§ 87c (2) AO).** Whatever is not supported is detected and
blocks the finalisation of the statement; nothing is left out silently. Version 1
has this scope:

| Area | Computed automatically | Manual input with formula help | Detected and blocked |
|---|---|---|---|
| Business | sole proprietorship, fiscal year = calendar year, EUR | header data (type of business, legal form, type of income, owner, real estate) | partnerships and corporations, deviating fiscal year, agriculture and forestry (§ 24 UStG, *Anlage LuF*), sale or closure of the business |
| Timing | inflow and outflow per payment, partial payments, discounts, credit notes, write-offs, set-off, foreign currency, credit card, payment providers, cash, money in transit | deviating payment date with reason, decisions of the ten-day review, income in kind and barter | crypto assets, valuation of foreign currency holdings, advance payments for a use of more than five years spread over the period (§ 11 (1) sentence 3, (2) sentence 3 EStG; questionnaire) |
| VAT | Kz 140/141/185/186, reverse charge and intra-EU acquisitions, small businesses (gross), Kz 111/119, status periods, limit monitoring | VAT on free supplies with their VAT base (standard taxation only), § 15a UStG adjustment | – |
| Fixed assets | straight-line, declining balance, electric vehicles § 7 (2a), digital assets, low-value assets, asset pools, disposals, contributions, § 7g (5) and § 7b special depreciation | manual depreciation method (e.g. buildings § 7 (5a)) | investment deductions § 7g (1)–(4), sale against instalments under § 4 (3) sentence 4 (R 4.5 (5) EStR), subsidies before the acquisition (R 6.5 (4) EStR), contributions after use for other income (§ 7 (1) sentence 5) |
| Limited expenses | gifts per recipient, entertainment 70/30, non-deductible expenses | home office allowance, home office room, meal allowances, commuting, flat-rate expenses, tax-free income | – |
| Withdrawals and contributions | Kz 122/123 from private accounts and private shares | withdrawals in kind, private use of vehicles and assets | *Anlage SZ* (required when other interest exceeds 2,050 €) |
| Supplementary data | questionnaire per statement | Kz 15.120 (real estate) | reserves § 6b/§ 6c/§ 4g (box 27), profit surcharge, investment funds and partial income method, partnership shares, change of profit method |
| Transmission | – | entering the values in *Mein ELSTER* | transmission via ERiC |

Each form field carries its scope (supported, manual input, not supported).

**Supported form years:** 2024, 2025 and 2026 (*Anlage EÜR*, *AVEÜR*, *SZ*), all
still in state *Draft* until verified. Without the form year of the assessment
year (e.g. 2027 before the BMF letter) the module computes provisionally with the
previous form year and blocks the finalisation.

**Known uncertainties:**

- The profit determination ("Ermittlung des Gewinns") has no printed box; the
  module uses box `G`. The assignment in the ELSTER data set is not confirmed.
- ELSTER field ids are not part of the public data yet; their publication is
  being clarified.
- *Anlage SZ* is computed with all its formulas, but filled in only if the
  other interest on debt (Kz 25.234) exceeds 2,050 €. Then the finalisation is
  blocked (C-41) until the carry-forward values of earlier years are supported.
- *Anlage LuF* is not delivered; its transfer lines in *Anlage EÜR* are not
  supported.
- A direct debit of the tax office collected on the working day after a due
  date on a weekend (10 January 2026 and 2027): the module proposes the year of
  payment (§ 108 (3), § 224 (2) no. 3 AO, H 11 EStH); the opposite decision
  (*Apply*, previous year) is possible with a warning (C-26) that must be
  acknowledged. The legal question is open with the tax advisor.
- Rule parameters cover the supported years; low-value assets and asset pools
  from acquisitions in 2010, declining balance from 2020. Earlier periods are
  added when needed: a takeover of a declining balance asset delivered before
  2020 needs the year of the switch to linear depreciation (or the parameters
  of that time).

**Account assignment:** the defaults for SKR03 and SKR04 cover all template
accounts. Ambiguous accounts (cases A01–A34, e.g. software subscriptions, DATEV
collective accounts 4500/4670, reverse charge purchase accounts) have low
confidence and must be confirmed when used. The DATEV small business revenue
accounts 8192/4184 are not part of the Odoo chart templates; they get their
default assignment when you create them. DATEV auxiliary accounts of the EÜR
(e.g. 8580–8589, 1449/1609 or SKR04 4580–4589) are reported as not provided for.
Corrections of the delivered categories, mappings and defaults reach existing
databases only through migration scripts (the data is loaded once).

**Fixed assets:** a new asset is always depreciated in full (R 7.4 (3) EStR);
only an asset taken over from another system may keep its memo value (warning
C-63). The month of disposal counts for the depreciation by default (setting).
The choice between low-value assets and an asset pool is derived from the
assets of the year (C-66): in a pool year every movable asset above 250 € up to
1,000 € goes into the pool. The use withdrawal for private use and the § 15a UStG
correction are proposals that you confirm; the VAT on a use withdrawal spreads
a cost of at least 500 € over the § 15a period. The private use of vehicles is
not proposed by the register: it is the private use value of Kz 106 (profit
corrections). § 7g (5) special depreciation needs a business use of at least
90 % in the year of delivery and the following year and the confirmation of the
profit limit; § 7b special depreciation is limited to 5 % of the cost per year
(the limits per square metre of § 7b (3) are your responsibility). Locked years
keep their values: the form values of a locked year come from its stored yearly
values, and what they depend on can no longer change.

A finalised statement is protected against changes through the user
interface and the ORM of Odoo, also for administrators; database
administrators can still change the data with SQL. *Check Integrity* finds a
changed record or receipt; a consistent change of the latest finalisation
together with its hashes is visible only against the hashes kept outside the
database (e.g. with the PDF of the fill-in aid). It does not prevent
changes. Locked asset years stay locked also for a correction of the year:
correct their effect with an override of the value and a reason.

**Small business limit:** the limits of § 19 UStG apply to the whole enterprise
of the owner (§ 2 (1) sentence 2 UStG). The module knows the turnover of the
Odoo business only: turnover of other businesses outside Odoo (e.g. a
photovoltaic system feeding into the grid) is not added, and the start of the
activity is the start of the entrepreneurial activity as a whole. An owner with
another business checks both limits himself with the total turnover of all
businesses; the warnings of the module cover the Odoo turnover only. The
turnover of the previous
year comes from the bookings; after a takeover from another system it is
entered at the VAT status period for the first year in Odoo (warning C-32 while
it is missing; an empty amount is no input, a turnover of 0 is confirmed
explicitly). Books that start with an opening entry during the previous year
hold only part of it: the turnover of the whole year is entered the same way. A
status period holds this input for one year; a second year that needs it gets a
status period of its own from 1 January. Without an opening entry the module
takes the books as complete: books that start during the previous year without
one (e.g. bank statements imported from July) need an opening entry on their
first day, otherwise the module compares the turnover of the part of the year
in Odoo. Until 2024 the turnover of the year of foundation is converted
into a turnover of the full year (§ 19 (3) sentences 3 and 4 UStG in the
version until 2024); the law from 2025 has no conversion. The total turnover
counts free supplies (Kz 106/108)
only when they are taxable, at their VAT base (enter it on the profit
correction; the asset register proposes it from the input tax deducted), never
at their value for income tax. Free supplies booked on accounts count under
standard taxation only when booked with output VAT (the net amount is the
base); booked by a small business they do not count (hint C-32): enter the VAT
base of a taxable one on a profit correction and leave its value empty. Income
in kind that is tax-free or not taxable (Kz 103) does not count, like booked
revenue of Kz 103.

**Profit corrections:** the yearly maximums (daily home office allowance, yearly
flat rate of a home office, flat rate of business expenses per group,
commuting allowance without own vehicle) are shared by all corrections of a
year, the earlier ones first.

**Outputs:** the data access package refers to the document type definition
of the description standard (`gdpdu-01-09-2004.dtd`), which is not shipped:
its source and licence are being clarified. Add it to the package if your
auditor needs it. The fill-in aid shows sums as control values because it is
not confirmed whether Mein ELSTER computes them itself.

**Reference cases:** `tests/data/pt01.json` (small business) and
`tests/data/rt01.json` (standard taxation) are synthetic cases with expected
values calculated by hand and the derivation of every field; the tests book them
and compare every field of both years. The values of RT-01 are not yet confirmed
by a tax advisor (`confirmation` is empty). Open for that confirmation: flat
rates without payment from the business (meals, daily home office allowance,
home office, commuting allowance) count as contribution in Kz 123; a refund of
income of a previous year reduces the income of its kind (Kz 112, 140) instead
of being an expense (Kz 183).

**Development status:** Alpha. The module becomes Beta after a tax advisor
has confirmed the expected values of the reference case RT-01 and has checked
the first real statement computed with the module against the receipts and
the statements of account (RT-04). Until a form year is verified by a person
who did not create its data, a statement on it cannot be finalised (C-72).

**Planned:** *Anlage SZ* with the carry-forward values of earlier years,
investment deductions (§ 7g (1)–(4) EStG), the private use of vehicles with
the 1 % method and the cost cap, reserves (box 27), the form year 2027, draft
entries for the depreciation of the asset register, a bridge to the asset
management of the OCA, Odoo 20 and the contribution to OCA/l10n-germany;
optionally the transmission via ERiC as a separate module. A blocked case is
released only together with its tests.
