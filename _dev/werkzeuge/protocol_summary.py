#!/usr/bin/env python3
# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Write ``summary.md`` of a test protocol (spec 9.9, § 87c (3) AO).

Reads the results of a run in the output directory and writes the summary
next to them:

* ``junit.xml`` (``odoo_junit.py``): tests, failures, errors and skips per
  module; results outside the modules appear as "other (...)" rows
* ``coverage.json`` (coverage.py): lines and branches per module
* ``odoo_status``, ``odoo_version``, ``odoo_image``, ``postgres_version``:
  one line each, written by the runner (missing files show "unknown")
* ``runs.json`` (only if the tests ran more than once, e.g. a second time with
  the OCA modules installed): one entry per run with ``label``, ``junit``
  (its JUnit file), ``database`` and ``status``. The coverage is then the
  combined one of all runs; a test counts as failed or erroneous if it was in
  any run and as skipped only if every run skipped it

Tag, commit and the state of the working tree come from git; the release
workflow, whose checkout may not allow git, passes them with ``--tag`` and
``--commit``. A protocol is a release protocol only for a tag and a clean
working tree.

Usage::

    python3 protocol_summary.py OUT_DIR --repo REPO --modules A,B,C \
        --database DB [--tag TAG --commit SHA] [--run URL]

Exit status 0 if every test passed and Odoo ended without error, else 1.
"""

# pylint: disable=print-used

import argparse
import ast
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

COUNTS = ("tests", "failures", "errors", "skipped")


def git(repo, *args):
    """Output of a git command, ``None`` if git fails."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def read(out, name):
    """First line of a result file, "unknown" if missing or empty."""
    path = out / name
    text = path.read_text(encoding="utf-8").strip() if path.exists() else ""
    return text.splitlines()[0] if text else "unknown"


def runs_of(out, database):
    """Runs of the protocol: ``runs.json`` of the runner, else the one run."""
    path = out / "runs.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return [{"label": "", "junit": "junit.xml", "database": database}]


def coverage_version(out):
    """Version of coverage.py that measured the run, from ``coverage.json``."""
    path = out / "coverage.json"
    if not path.exists():
        return "unknown"
    meta = json.loads(path.read_text(encoding="utf-8")).get("meta", {})
    return meta.get("version", "unknown")


def suites_of(path):
    """``{suite name: element}`` of a JUnit file (empty without the file)."""
    if not path.exists():
        return {}
    return {
        suite.get("name"): suite for suite in ET.parse(path).getroot().iter("testsuite")
    }


def coverage_of(out, module):
    """Covered and total lines and branches of the files of a module."""
    path = out / "coverage.json"
    stats = [0, 0, 0, 0]
    if not path.exists():
        return stats
    for name, data in json.loads(path.read_text(encoding="utf-8"))["files"].items():
        if f"/{module}/" in f"/{name}":
            summary = data["summary"]
            stats[0] += summary["covered_lines"]
            stats[1] += summary["num_statements"]
            stats[2] += summary.get("covered_branches", 0)
            stats[3] += summary.get("num_branches", 0)
    return stats


def rate(covered, total):
    return f"{100 * covered / total:.2f} %" if total else "–"


def counts_of(suite):
    return [int(suite.get(key, 0)) if suite is not None else 0 for key in COUNTS]


def outcome(case):
    """``failure``, ``error``, ``skipped`` or ``passed`` of a test case."""
    for kind in ("failure", "error", "skipped"):
        if case.find(kind) is not None:
            return kind
    return "passed"


def merged_counts(suites):
    """Counts of the tests of one module over several runs."""
    results = {}
    for suite in suites:
        for case in suite.iter("testcase"):
            key = (case.get("classname"), case.get("name"))
            results.setdefault(key, []).append(outcome(case))
    counts = [len(results), 0, 0, 0]
    for outcomes in results.values():
        if "failure" in outcomes:
            counts[1] += 1
        elif "error" in outcomes:
            counts[2] += 1
        elif all(kind == "skipped" for kind in outcomes):
            counts[3] += 1
    return counts


def git_state(repo, tag, commit):
    """Tag, description, commit and working tree as shown in the summary."""
    exact = git(repo, "describe", "--tags", "--exact-match")
    status = git(repo, "status", "--porcelain")
    if status is None:
        tree = "unknown (git not available)"
    else:
        tree = "with uncommitted changes" if status else "clean"
    return {
        "tag": tag or exact or "none (not a release tag)",
        "description": git(repo, "describe", "--tags", "--always", "--dirty")
        or "unknown",
        "commit": commit
        or git(repo, "log", "-1", "--format=%H %cd", "--date=iso")
        or "unknown",
        "tree": tree,
        "release": bool(tag or exact) and status == "",
    }


def summary(out, repo, modules, database, tag=None, commit=None, run=None):
    """Lines of ``summary.md`` and whether the run passed."""
    state = git_state(repo, tag, commit)
    status = read(out, "odoo_status")
    runs = runs_of(out, database)
    several = len(runs) > 1
    if several:
        databases = ", ".join(f"{one['database']} ({one['label']})" for one in runs)
        database_line = f"- Databases: {databases}"
    else:
        database_line = f"- Database: {database}"
    lines = [
        "# Test protocol",
        "",
        f"- Date: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"- Tag: {state['tag']}",
        f"- Description: {state['description']}",
        f"- Commit: {state['commit']}",
        f"- Working tree: {state['tree']}",
        f"- Odoo: {read(out, 'odoo_version')}, image {read(out, 'odoo_image')}",
        f"- PostgreSQL: {read(out, 'postgres_version')}",
        f"- Coverage: coverage.py {coverage_version(out)}",
        f"{database_line} (fresh, dropped after the run unless kept)",
        f"- Odoo exit status: {status}",
    ]
    if run:
        lines.append(f"- Run: {run}")
    lines += [
        "",
        "| Module | Version | Tests | Failures | Errors | Skipped | Lines | Branches |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    run_suites = [suites_of(out / one["junit"]) for one in runs]
    failed = 0
    totals = [0] * 8
    for module in modules:
        manifest = ast.literal_eval(
            (repo / module / "__manifest__.py").read_text(encoding="utf-8")
        )
        if several:
            counts = merged_counts(
                [suites[module] for suites in run_suites if module in suites]
            )
        else:
            counts = counts_of(run_suites[0].get(module))
        failed += counts[1] + counts[2]
        stats = coverage_of(out, module)
        totals = [a + b for a, b in zip(totals, counts + stats, strict=True)]
        lines.append(
            f"| {module} | {manifest['version']} | {counts[0]} | {counts[1]} | "
            f"{counts[2]} | {counts[3]} | {rate(stats[0], stats[1])} | "
            f"{rate(stats[2], stats[3])} |"
        )
    lines.append(
        f"| **Total** | – | {totals[0]} | {totals[1]} | {totals[2]} | {totals[3]} | "
        f"{rate(totals[4], totals[5])} | {rate(totals[6], totals[7])} |"
    )
    # results outside the modules (e.g. an error of Odoo without module)
    for one, suites in zip(runs, run_suites, strict=True):
        label = f", {one['label']}" if several else ""
        for name, suite in suites.items():
            if name in modules:
                continue
            counts = counts_of(suite)
            failed += counts[1] + counts[2]
            lines.append(
                f"| other ({name}{label}) | – | {counts[0]} | {counts[1]} | "
                f"{counts[2]} | {counts[3]} | – | – |"
            )
    passed = not failed and status == "0" and all(run_suites)
    if several:
        lines += [
            "",
            "The tests ran more than once; the coverage above combines the runs, "
            "and a test counts as skipped above only if every run skipped it.",
            "",
            "| Run | Database | Tests | Failures | Errors | Skipped "
            "| Odoo exit status |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
        for one, suites in zip(runs, run_suites, strict=True):
            counts = [0] * 4
            for module in modules:
                counts = [
                    a + b
                    for a, b in zip(counts, counts_of(suites.get(module)), strict=True)
                ]
            lines.append(
                f"| {one['label']} | {one['database']} | {counts[0]} | {counts[1]} | "
                f"{counts[2]} | {counts[3]} | {one.get('status', 'unknown')} |"
            )
    covered, total = totals[4] + totals[6], totals[5] + totals[7]
    lines += [
        "",
        f"Coverage of lines and branches together: {rate(covered, total)}",
        "",
        f"Result: **{'passed' if passed else 'FAILED'}**",
        "",
    ]
    if not state["release"]:
        lines += [
            "Not a release protocol: create it on the tagged commit with a clean "
            "working tree (RELEASE.md).",
            "",
        ]
    return lines, passed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("out", type=Path, help="output directory of the run")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--modules", required=True, help="comma-separated")
    parser.add_argument("--database", required=True)
    parser.add_argument("--tag", help="release tag (default: from git)")
    parser.add_argument("--commit", help="commit (default: from git)")
    parser.add_argument("--run", help="URL of the CI run")
    args = parser.parse_args(argv)
    lines, passed = summary(
        args.out,
        args.repo,
        args.modules.split(","),
        args.database,
        tag=args.tag,
        commit=args.commit,
        run=args.run,
    )
    (args.out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
