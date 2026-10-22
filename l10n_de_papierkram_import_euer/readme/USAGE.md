1. Enable the EÜR for the company (*Settings → Accounting*) before the import.
2. In the Papierkram import, *Take Over Fixed Assets (EÜR)* is then switched
   on. Set the *Takeover Date (Asset Register)* to 31 December of the last year
   computed (and filed) with Papierkram. To let the register compute the whole
   history instead, choose the 31 December before the first acquisition.
3. Check and run the import as usual. The log lists the assets taken over and
   anything to enter by hand; *Assets* opens them in the register.

If a row cannot be taken over (e.g. an asset pool whose book value does not
follow one fifth per year), nothing is taken over; the log shows the rows.
Correct them and take the assets over with *Asset Register → Take Over Asset
Register*.

An import made before the EÜR was enabled keeps its depreciation entries; the
button *Take Over Fixed Assets (EÜR)* on the import takes its assets over
afterwards with the takeover date set there.
