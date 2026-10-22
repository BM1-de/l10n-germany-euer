

# Deutsche EÜR für Odoo
<!-- /!\ Non OCA Context : Set here the badge of your runbot / runboat instance. -->
[![Pre-commit Status](https://github.com/BM1-de/l10n-germany-euer/actions/workflows/pre-commit.yml/badge.svg?branch=19.0)](https://github.com/BM1-de/l10n-germany-euer/actions/workflows/pre-commit.yml?query=branch%3A19.0)
[![Build Status](https://github.com/BM1-de/l10n-germany-euer/actions/workflows/test.yml/badge.svg?branch=19.0)](https://github.com/BM1-de/l10n-germany-euer/actions/workflows/test.yml?query=branch%3A19.0)
[![codecov](https://codecov.io/gh/BM1-de/l10n-germany-euer/branch/19.0/graph/badge.svg)](https://codecov.io/gh/BM1-de/l10n-germany-euer)
<!-- /!\ Non OCA Context : Set here the badge of your translation instance. -->

<!-- /!\ do not modify above this line -->

Einnahmenüberschussrechnung (Anlage EÜR) und Datenübernahme aus Papierkram für Odoo

**Alpha, nicht unabhängig geprüft:** Berechnung, Musterfälle und Formulardaten hat bisher
nur der Maintainer erstellt und geprüft; eine Prüfung durch eine Steuerfachkraft steht
noch aus. Die Formularjahrgänge werden als Entwurf ausgeliefert, eine Erklärung lässt sich
erst nach ihrer Freigabe durch eine fachkundige Person abschließen. Das Modul ersetzt
keine steuerliche Beratung; die Software wird ohne Gewährleistung bereitgestellt
(AGPL-3.0, Abschnitte 15 und 16).

*Alpha, not independently checked: calculation, reference cases and form data were
created and checked by the maintainer only. Form years ship as drafts. No tax advice,
no warranty.*

<!-- /!\ do not modify below this line -->

<!-- prettier-ignore-start -->

[//]: # (addons)

Available addons
----------------
addon | version | maintainers | summary
--- | --- | --- | ---
[l10n_de_euer](l10n_de_euer/) | 19.0.1.0.0 | <a href='https://github.com/bm1-phillip'><img src='https://github.com/bm1-phillip.png' width='32' height='32' style='border-radius:50%;' alt='bm1-phillip'/></a> | Cash-basis profit statement (Anlage EÜR, AVEÜR) according to § 4 (3) EStG
[l10n_de_papierkram_import](l10n_de_papierkram_import/) | 19.0.1.0.0 | <a href='https://github.com/bm1-phillip'><img src='https://github.com/bm1-phillip.png' width='32' height='32' style='border-radius:50%;' alt='bm1-phillip'/></a> | Import contacts, invoices, vouchers, payments and assets from Papierkram
[l10n_de_papierkram_import_euer](l10n_de_papierkram_import_euer/) | 19.0.1.0.0 | <a href='https://github.com/bm1-phillip'><img src='https://github.com/bm1-phillip.png' width='32' height='32' style='border-radius:50%;' alt='bm1-phillip'/></a> | Take over the fixed assets of Papierkram into the EÜR asset register
[l10n_de_papierkram_import_timesheet](l10n_de_papierkram_import_timesheet/) | 19.0.1.0.0 | <a href='https://github.com/bm1-phillip'><img src='https://github.com/bm1-phillip.png' width='32' height='32' style='border-radius:50%;' alt='bm1-phillip'/></a> | Import products, time entries and unbilled work from Papierkram

[//]: # (end addons)

<!-- prettier-ignore-end -->

## Licenses

This repository is licensed under [AGPL-3.0](LICENSE).

However, each module can have a totally different license, as long as they adhere to Baumgärtner Marketing GmbH
policy. Consult each module's `__manifest__.py` file, which contains a `license` key
that explains its license.

----
<!-- /!\ Non OCA Context : Set here the full description of your organization. -->
