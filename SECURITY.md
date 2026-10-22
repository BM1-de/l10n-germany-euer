# Security Policy

## Supported versions

Security fixes are made on the branch `19.0` and released as a new version `19.0.x.y.z`
of the affected modules. Older versions are not maintained.

| Branch | Supported |
| ------ | --------- |
| 19.0   | yes       |

## Reporting a vulnerability

Please do not report security problems in public issues, pull requests or discussions.

- Preferably use the private vulnerability reporting of GitHub: tab _Security_, _Report
  a vulnerability_ in
  [BM1-de/odoo-l10n-germany-euer](https://github.com/BM1-de/odoo-l10n-germany-euer/security).
- Or write an e-mail to the maintainer, Phillip Baumgärtner <baumgaertner@bm1.de>, with
  the subject "Security odoo-l10n-germany-euer".

Describe the problem, the affected module and version, how to reproduce it and its
possible impact. Do not send real tax or personal data; use synthetic data for the
example.

We confirm the receipt, examine the report, keep you informed and publish a fix together
with a security notice in the changelog (`HISTORY.md` of the module). We credit you
unless you prefer not to be named.

## Scope

In scope are the modules of this repository, in particular:

- access of a user to data of a business (company) the user is not allowed to see, also
  through the outputs and downloads;
- changes of a finalised statement, its values, receipts or hashes that _Check
  Integrity_ does not detect;
- injection through imported files (CSV takeover of the asset register) or through the
  generated files (CSV, XLSX, ZIP).

Out of scope are vulnerabilities of Odoo itself or of other modules (please report them
to their maintainers) and wrong tax results without a security impact (please open a
normal issue, without real data).

## Note

The modules do not transmit any data to the tax authorities. They are provided free of
charge and without warranty under the GNU AGPL-3.0 (see `LICENSE`).
