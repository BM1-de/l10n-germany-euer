# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Tests of the tools of the test protocol (spec 9.9, § 87c (3) AO).

They run with pytest without Odoo (``pytest`` in the repository root, job
``test-engine`` of the workflow ``tests``).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "werkzeuge"))

import check_forms  # noqa: E402
import odoo_junit  # noqa: E402
import po_fill  # noqa: E402
import protocol_summary  # noqa: E402

ADDON = "odoo.addons.l10n_de_example.tests.test_example"


class TestSplitId(unittest.TestCase):
    def test_test_method(self):
        self.assertEqual(
            odoo_junit.split_id(f"{ADDON}.TestExample.test_one"),
            ("l10n_de_example", "tests.test_example.TestExample", "test_one"),
        )

    def test_class_fixture_belongs_to_its_module(self):
        """An error in setUpClass is counted for the module of the class."""
        self.assertEqual(
            odoo_junit.split_id(f"setUpClass ({ADDON}.TestExample)"),
            ("l10n_de_example", "tests.test_example.TestExample", "setUpClass"),
        )

    def test_module_fixture_belongs_to_its_module(self):
        self.assertEqual(
            odoo_junit.split_id(f"tearDownModule ({ADDON})"),
            ("l10n_de_example", "tests.test_example", "tearDownModule"),
        )

    def test_other_ids_go_to_odoo(self):
        self.assertEqual(
            odoo_junit.split_id("setUpClass (some.other.Class)"),
            ("odoo", "some.other.Class", "setUpClass"),
        )
        self.assertEqual(odoo_junit.split_id("loading"), ("odoo", "", "loading"))


class TestJunitReport(unittest.TestCase):
    def test_fixture_error_counts_in_module_suite(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "junit.xml"
            recorder = odoo_junit.Recorder(str(path))
            recorder.record(_Test(f"{ADDON}.TestExample.test_one"))
            failing = recorder.record(_Test(f"setUpClass ({ADDON}.TestOther)"))
            failing["errors"].append("Traceback\nValueError: broken fixture")
            skipped = recorder.record(_Test(f"{ADDON}.TestExample.test_two"))
            skipped["skipped"] = "not installed"
            recorder.write_report()
            root = ET.parse(path).getroot()
        suites = {suite.get("name"): suite for suite in root.iter("testsuite")}
        self.assertEqual(list(suites), ["l10n_de_example"])
        suite = suites["l10n_de_example"]
        self.assertEqual(
            [suite.get(key) for key in ("tests", "failures", "errors", "skipped")],
            ["3", "0", "1", "1"],
        )
        error = suite.find("testcase[@name='setUpClass']/error")
        self.assertEqual(error.get("message"), "ValueError: broken fixture")
        self.assertEqual(root.get("errors"), "1")


class TestSummary(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        base = Path(self.folder.name)
        self.repo, self.out = base / "repo", base / "out"
        (self.repo / "l10n_de_example").mkdir(parents=True)
        (self.repo / "l10n_de_example" / "__manifest__.py").write_text(
            '{"name": "Example", "version": "19.0.1.0.0"}', encoding="utf-8"
        )
        self.out.mkdir()
        (self.out / "coverage.json").write_text(
            json.dumps(
                {
                    "files": {
                        "/x/l10n_de_example/models/a.py": {
                            "summary": {
                                "covered_lines": 19,
                                "num_statements": 20,
                                "covered_branches": 3,
                                "num_branches": 4,
                            }
                        },
                        "/x/l10n_de_other/models/b.py": {
                            "summary": {"covered_lines": 0, "num_statements": 9}
                        },
                    }
                }
            ),
            encoding="utf-8",
        )
        (self.out / "postgres_version").write_text("13.20\n", encoding="utf-8")

    def write_junit(self, suites):
        root = ET.Element("testsuites")
        for name, counts in suites.items():
            ET.SubElement(
                root,
                "testsuite",
                name=name,
                **{key: str(value) for key, value in counts.items()},
            )
        ET.ElementTree(root).write(self.out / "junit.xml")

    def summary(self, **kwargs):
        return protocol_summary.summary(
            self.out, self.repo, ["l10n_de_example"], "protocol_example", **kwargs
        )

    def test_failed_fixture_and_other_suite(self):
        (self.out / "odoo_status").write_text("1\n", encoding="utf-8")
        self.write_junit(
            {
                "l10n_de_example": {"tests": 5, "failures": 0, "errors": 1},
                "odoo": {"tests": 1, "errors": 1},
            }
        )
        lines, passed = self.summary()
        self.assertFalse(passed)
        self.assertIn(
            "| l10n_de_example | 19.0.1.0.0 | 5 | 0 | 1 | 0 | 95.00 % | 75.00 % |",
            lines,
        )
        self.assertIn("| other (odoo) | – | 1 | 0 | 1 | 0 | – | – |", lines)
        self.assertIn("| **Total** | – | 5 | 0 | 1 | 0 | 95.00 % | 75.00 % |", lines)
        self.assertIn("Coverage of lines and branches together: 91.67 %", lines)
        self.assertIn("Result: **FAILED**", lines)
        self.assertIn("- PostgreSQL: 13.20", lines)
        self.assertIn("- Odoo: unknown, image unknown", lines)

    def test_passed_run_outside_git_is_no_release_protocol(self):
        (self.out / "odoo_status").write_text("0\n", encoding="utf-8")
        self.write_junit({"l10n_de_example": {"tests": 5, "skipped": 2}})
        lines, passed = self.summary(tag="19.0.1.0.0", commit="abc", run="url")
        self.assertTrue(passed)
        self.assertIn("- Tag: 19.0.1.0.0", lines)
        self.assertIn("- Commit: abc", lines)
        self.assertIn("- Run: url", lines)
        self.assertIn("- Working tree: unknown (git not available)", lines)
        self.assertTrue(any(line.startswith("Not a release") for line in lines))

    def test_without_results_the_run_failed(self):
        lines, passed = self.summary()
        self.assertFalse(passed)
        self.assertIn("- Odoo exit status: unknown", lines)
        self.assertIn(
            "| l10n_de_example | 19.0.1.0.0 | 0 | 0 | 0 | 0 | 95.00 % | 75.00 % |",
            lines,
        )

    def test_main_writes_the_file(self):
        (self.out / "odoo_status").write_text("0\n", encoding="utf-8")
        self.write_junit({"l10n_de_example": {"tests": 1}})
        status = protocol_summary.main(
            [
                str(self.out),
                "--repo",
                str(self.repo),
                "--modules",
                "l10n_de_example",
                "--database",
                "protocol_example",
            ]
        )
        self.assertEqual(status, 0)
        text = (self.out / "summary.md").read_text(encoding="utf-8")
        self.assertIn("Result: **passed**", text)


class TestPoFill(unittest.TestCase):
    """``de.po`` from the template and the translation table."""

    def test_fill(self):
        with tempfile.TemporaryDirectory() as folder:
            pot, out = Path(folder) / "x.pot", Path(folder) / "de.po"
            pot.write_text(
                'msgid ""\nmsgstr ""\n"Content-Transfer-Encoding: \\n"\n\n'
                "#. module: x\n"
                'msgid "Amount"\nmsgstr ""\n\n'
                'msgid ""\n"Two "\n"lines"\nmsgstr ""\n',
                encoding="utf-8",
            )
            missing = po_fill.fill(pot, {"Amount": "Betrag"}, out)
            text = out.read_text(encoding="utf-8")
        self.assertEqual(missing, ["Two lines"])
        self.assertIn('"Language: de\\n"', text)
        self.assertIn('#. module: x\nmsgid "Amount"\nmsgstr "Betrag"', text)
        self.assertIn('"lines"\nmsgstr ""', text)


class TestCheckFormsLabels(unittest.TestCase):
    """Labels of fields with Kennzahl (F-11, E5 re-check): one sequence of
    words in the text of the line, before their own Kennzahl."""

    block = check_forms.Block(
        4,
        own=[
            "davon abweichender Beginn 131    T T M M 2025   "
            "davon abweichendes Ende 132   T T M M J J J J"
        ],
    )
    # a label over several printed lines with barcode, operator, cent comma,
    # footnote mark and the reference to a line
    long_block = check_forms.Block(
        19,
        before=["Entnahmen der vorange-", "202500382002", "gangenen Wirtschaftsjah-"],
        own=["re (Betrag aus Zeile 20 der Anlage SZ des Vorjahres3))   325 +"],
        after=["     ,       EUR     Ct"],
    )

    def test_label_as_one_sequence(self):
        sequence = check_forms.label_sequence
        self.assertTrue(sequence("davon abweichendes Ende", self.block))
        label = (
            "Entnahmen der vorangegangenen Wirtschaftsjahre (Betrag aus Zeile 20 "
            "der Anlage SZ des Vorjahres)"
        )
        self.assertTrue(sequence(label, self.long_block))
        # a word missing in the middle, a wrong line reference
        self.assertFalse(
            sequence(label.replace("vorangegangenen ", ""), self.long_block)
        )
        self.assertFalse(
            sequence(label.replace("Zeile 20", "Zeile 21"), self.long_block)
        )
        self.assertFalse(
            sequence(label.replace("Zeile 20 ", "Zeile "), self.long_block)
        )
        self.assertIsNone(sequence("", self.block))
        self.assertIsNone(sequence("Ende", None))

    def test_label_before_its_kennzahl(self):
        before = check_forms.label_before_kz
        self.assertTrue(before("davon abweichender Beginn", "131", ["132"], self.block))
        self.assertTrue(before("davon abweichendes Ende", "132", ["131"], self.block))
        # swapped labels
        self.assertFalse(before("davon abweichendes Ende", "131", ["132"], self.block))
        self.assertFalse(
            before("davon abweichender Beginn", "132", ["131"], self.block)
        )
        self.assertIsNone(before("Ende", "133", ["131"], self.block))
        self.assertIsNone(before("", "131", [], self.block))

    def test_problems_of_a_field(self):
        """Through the check of a field: the labels of a line swapped give a
        problem, the right ones none."""

        def field(kz, label):
            return check_forms.FormField(
                xmlid=f"f{kz}",
                year=2025,
                form_type="euer",
                code=f"EUER.15.{kz}",
                box="15",
                kz=kz,
                line=4,
                label=label,
                field_type="date",
            )

        blocks = {kind: {"euer": {4: self.block}} for kind in check_forms.SOURCES}
        right = [
            field("131", "davon abweichender Beginn"),
            field("132", "davon abweichendes Ende"),
        ]
        wrong = [
            field("131", "davon abweichendes Ende"),
            field("132", "davon abweichender Beginn"),
        ]
        problems = check_forms._kz_label_problems
        for f in right:
            self.assertEqual(problems(f, blocks, ["layout"], right), [])
        self.assertIn(
            "labels of the line swapped",
            problems(wrong[0], blocks, ["layout"], wrong)[0],
        )
        missing = field("131", "davon abweichender Anfang")
        self.assertIn(
            "not found as one sequence",
            problems(missing, blocks, ["layout"], [missing])[0],
        )


class _Test:
    """Stand-in for a test case or an error holder of unittest."""

    def __init__(self, test_id):
        self._id = test_id

    def id(self):
        return self._id
