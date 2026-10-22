Takes over the fixed assets of a Papierkram import into the asset register of
the cash-basis profit computation (`l10n_de_euer`). Installed automatically
together with both modules.

With the register, the depreciation of the EÜR comes only from the register;
depreciation posted as journal entries is not counted. The import therefore
splits the assets at a takeover date, the 31 December of the last year computed
in Papierkram:

- Assets acquired up to the takeover date are taken over with the register
  import of `l10n_de_euer`: cost, useful life and the Papierkram book value at
  the takeover date, including Papierkram's memo value of 1 €. Their
  depreciation is posted as journal entries only up to the takeover date, as
  history; the years up to then stay as filed with Papierkram.
- Assets acquired later are entered in the register as purchases, linked to the
  journal items of their voucher. The register depreciates them in full.

Without a VAT treatment of the business, the import enters the treatment of the
import (small business or standard taxation) from the first exported year on.
