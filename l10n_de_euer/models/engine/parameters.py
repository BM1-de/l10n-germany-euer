# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Access to the rule parameters of the engine (spec 3.2, rule set)."""

import datetime
from decimal import Decimal


class Parameters:
    """Rule parameters with validity, as plain data.

    ``values``: :class:`~.model.ParameterValue` records. ``on`` is a date or
    a year (then 1 January of that year).
    """

    def __init__(self, values):
        self._values = {}
        for value in values:
            self._values.setdefault(value.code, []).append(value)

    def get(self, code, on, default=None):
        if isinstance(on, int):
            on = datetime.date(on, 1, 1)
        for value in self._values.get(code, ()):
            if value.date_from and value.date_from > on:
                continue
            if value.date_to and value.date_to < on:
                continue
            return value.value
        return default

    def cents(self, code, on, default=None):
        """Value of a euro parameter in cent."""
        value = self.get(code, on)
        if value is None:
            return default
        return int((Decimal(str(value)) * 100).to_integral_value())

    def month_day(self, code, on, default):
        """Value ``MM-DD`` as tuple ``(month, day)``."""
        value = self.get(code, on)
        if not value:
            return default
        month, day = str(value).strip("'\"").split("-")
        return int(month), int(day)
