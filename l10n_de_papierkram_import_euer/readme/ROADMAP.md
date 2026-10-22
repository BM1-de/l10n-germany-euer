- The takeover date is always a 31 December. The years up to it stay as filed
  with Papierkram; the register does not compute them again.
- Assets taken over keep the memo value of 1 € that Papierkram applies (warning
  C-63 of `l10n_de_euer`); assets entered in the register as purchases are
  depreciated in full.
- An asset whose voucher or asset category is missing is not taken over and
  has to be entered in the register by hand; the log lists it.
- The register import checks all rows together: one row that cannot be taken
  over (e.g. an asset pool whose book value does not follow one fifth per year)
  stops the takeover of all assets acquired up to the takeover date.
