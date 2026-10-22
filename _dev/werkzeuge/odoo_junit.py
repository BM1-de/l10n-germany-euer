#!/usr/bin/env python3
# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Run Odoo and write the results of its tests as JUnit XML.

Odoo logs its test results but writes no report file. This wrapper records
every test of the run (success, failure, error, skip, duration) through the
test result class of Odoo and writes one ``<testsuite>`` per addon when the
process ends.

Usage (inside the Odoo container, all further arguments go to Odoo)::

    python3 odoo_junit.py --junit-xml /out/junit.xml \
        -c /etc/odoo/odoo.conf -d DB -i MODULE --test-tags /MODULE \
        --stop-after-init

It can run under ``coverage run`` as well.
"""

import atexit
import re
import socket
import sys
import time
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

_TEST_ID = re.compile(r"^odoo\.addons\.(?P<module>[^.]+)\.(?P<rest>.+)$")
#: errors of class or module fixtures: unittest reports them as
#: ``setUpClass (odoo.addons.MODULE.tests.FILE.CLASS)``
_FIXTURE_ID = re.compile(
    r"^(?P<fixture>setUpClass|tearDownClass|setUpModule|tearDownModule)"
    r" \((?P<target>[^)]+)\)$"
)


def split_id(test_id):
    """``(module, classname, name)`` of a test id; a fixture error belongs to
    the module and class it was raised for, anything else to ``odoo``."""
    fixture = _FIXTURE_ID.match(test_id)
    if fixture:
        match = _TEST_ID.match(fixture["target"])
        if match:
            return match["module"], match["rest"], fixture["fixture"]
        return "odoo", fixture["target"], fixture["fixture"]
    match = _TEST_ID.match(test_id)
    if match:
        module, rest = match["module"], match["rest"]
    else:
        module, rest = "odoo", test_id
    classname, _sep, name = rest.rpartition(".")
    return module, classname, name or rest


class Recorder:
    """Collect the results per test in the order the tests started."""

    def __init__(self, path):
        self.path = path
        self.records = {}
        self.started = time.time()

    def record(self, test):
        """Return the record of a test (subtests count for their test)."""
        test = getattr(test, "test_case", None) or test
        test_id = test.id() if hasattr(test, "id") else str(test)
        rec = self.records.get(test_id)
        if rec is None:
            rec = self.records[test_id] = {
                "id": test_id,
                "start": None,
                "time": 0.0,
                "failures": [],
                "errors": [],
                "skipped": None,
            }
        return rec

    def write_report(self):
        """Write the JUnit XML file."""
        suites = {}
        for rec in self.records.values():
            module, classname, name = split_id(rec["id"])
            suites.setdefault(module, []).append((classname, name, rec))
        root = ET.Element("testsuites")
        totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
        stamp = datetime.fromtimestamp(self.started, timezone.utc).isoformat()
        for module in sorted(suites):
            cases = suites[module]
            suite = ET.SubElement(
                root,
                "testsuite",
                name=module,
                timestamp=stamp,
                hostname=socket.gethostname(),
            )
            counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
            duration = 0.0
            for classname, name, rec in cases:
                case = ET.SubElement(
                    suite,
                    "testcase",
                    classname=f"{module}.{classname}" if classname else module,
                    name=name,
                    time=f"{rec['time']:.3f}",
                )
                counts["tests"] += 1
                duration += rec["time"]
                for text in rec["failures"]:
                    node = ET.SubElement(case, "failure", message=_first_line(text))
                    node.text = text
                for text in rec["errors"]:
                    node = ET.SubElement(case, "error", message=_first_line(text))
                    node.text = text
                if rec["failures"]:
                    counts["failures"] += 1
                elif rec["errors"]:
                    counts["errors"] += 1
                elif rec["skipped"] is not None:
                    ET.SubElement(case, "skipped", message=rec["skipped"])
                    counts["skipped"] += 1
            for key, value in counts.items():
                suite.set(key, str(value))
                totals[key] += value
            suite.set("time", f"{duration:.3f}")
        for key, value in totals.items():
            root.set(key, str(value))
        root.set("time", f"{time.time() - self.started:.3f}")
        ET.indent(root)
        ET.ElementTree(root).write(self.path, encoding="utf-8", xml_declaration=True)


def _first_line(text):
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1][:500] if lines else ""


def install(recorder):
    """Wrap the methods of the Odoo test result class."""
    from odoo.tests import result as result_module

    cls = result_module.OdooTestResult
    start_test, stop_test = cls.startTest, cls.stopTest
    add_error, add_failure, add_skip = cls.addError, cls.addFailure, cls.addSkip

    def startTest(self, test):
        start_test(self, test)
        recorder.record(test)["start"] = time.time()

    def stopTest(self, test):
        stop_test(self, test)
        rec = recorder.record(test)
        if rec["start"] is not None:
            rec["time"] += time.time() - rec["start"]
            rec["start"] = None

    def _text(self, test, err):
        text = self._exc_info_to_string(err, test)
        if getattr(test, "test_case", None) is not None:
            text = f"{self.getDescription(test)}\n{text}"
        return text

    def addError(self, test, err):
        soft = self._soft_fail
        add_error(self, test, err)
        if not soft:
            recorder.record(test)["errors"].append(_text(self, test, err))

    def addFailure(self, test, err):
        soft = self._soft_fail
        add_failure(self, test, err)
        if not soft:
            recorder.record(test)["failures"].append(_text(self, test, err))

    def addSkip(self, test, reason):
        add_skip(self, test, reason)
        recorder.record(test)["skipped"] = str(reason)

    cls.startTest = startTest
    cls.stopTest = stopTest
    cls.addError = addError
    cls.addFailure = addFailure
    cls.addSkip = addSkip


def main():
    args = sys.argv[1:]
    if len(args) < 2 or args[0] != "--junit-xml":
        sys.exit(__doc__)
    recorder = Recorder(args[1])
    install(recorder)
    atexit.register(recorder.write_report)
    sys.argv = ["odoo", *args[2:]]
    import odoo.cli

    odoo.cli.main()


if __name__ == "__main__":
    main()
