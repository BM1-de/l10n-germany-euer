# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Setup of pytest for the unit tests of the calculation engine (spec 9.9).

The engine under ``l10n_de_euer/models/engine`` is pure Python without access
to the ORM (spec 7.2), so its tests give fast feedback without Odoo, without a
database and without installing the addon::

    pytest                                      # all files of pytest.ini
    pytest l10n_de_euer/tests/test_engine_units.py

Two things make that work. The packages of the addon are registered without
running their ``__init__.py``, which would import the ORM models; and
``odoo.tests.BaseCase`` falls back to :class:`unittest.TestCase` where Odoo is
not installed. Where it is installed, nothing is replaced.

``pytest.ini`` lists the test files without database (the engine and the
tools of the test protocol in ``_dev/tests``). The tests of the ORM need a
database and run with Odoo as usual, see ``_dev/werkzeuge/test_protocol.sh``.
"""

import importlib.util
import pathlib
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent
ADDON = "l10n_de_euer"
#: packages whose ``__init__.py`` imports the ORM and is skipped here; the
#: engine itself keeps its own ``__init__.py`` (pure Python)
PACKAGES = (ADDON, f"{ADDON}.models", f"{ADDON}.tests")


def _stub_odoo():
    """Register a minimal ``odoo.tests`` where Odoo is not installed."""
    if importlib.util.find_spec("odoo") is not None:
        return
    odoo = types.ModuleType("odoo")
    tests = types.ModuleType("odoo.tests")
    tests.BaseCase = unittest.TestCase
    tests.tagged = lambda *tags: (lambda cls: cls)
    odoo.tests = tests
    sys.modules["odoo"] = odoo
    sys.modules["odoo.tests"] = tests


def _register_packages():
    """Register the packages of the addon on their folder, nothing else."""
    for name in PACKAGES:
        if name in sys.modules:
            continue
        package = types.ModuleType(name)
        package.__path__ = [str(ROOT.joinpath(*name.split(".")))]
        package.__package__ = name
        sys.modules[name] = package


if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_stub_odoo()
_register_packages()
