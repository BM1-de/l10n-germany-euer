# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Unit tests of the finalisation engine, no database (spec 7.7, G-04).

Canonical JSON (sorted keys, compact, amounts as cent integers, no floats),
the hashes and the chain of finalisations, its verification and the
differences of C-60 and of the correction report. Synthetic values (D6).
"""

import hashlib
from decimal import Decimal as D

from odoo.tests import BaseCase

from ..models.engine import integrity


def _link(index, prev_hash, content, payload_sha256=None, chain=None):
    payload_sha256 = payload_sha256 or integrity.sha256_hex(content)
    return integrity.Link(
        index=index,
        prev_hash=prev_hash,
        payload_sha256=payload_sha256,
        chain_hash=chain or integrity.chain_hash(prev_hash, payload_sha256),
        content_sha256=integrity.sha256_hex(content) if content is not None else None,
    )


def _chain(*contents):
    links, prev = [], integrity.GENESIS
    for index, content in enumerate(contents, start=1):
        link = _link(index, prev, content)
        links.append(link)
        prev = link.chain_hash
    return links


class TestIntegrityEngine(BaseCase):
    # ------------------------------------------------------------ canonical
    def test_canonical_json(self):
        """Sorted keys, no blanks, UTF-8; the same data gives the same bytes."""
        data = {"b": [3, {"z": None, "a": True}], "a": "Bürokosten €", "c": -12}
        raw = integrity.canonical_json(data)
        self.assertEqual(
            raw,
            '{"a":"Bürokosten €","b":[3,{"a":true,"z":null}],"c":-12}'.encode(),
        )
        self.assertEqual(integrity.canonical_json(integrity.load(raw)), raw)

    def test_canonical_json_refuses_floats(self):
        """Amounts are cent integers: a float, a Decimal or a key that is no
        text is refused with its path."""
        for value in (
            {"amount": 1.5},
            {"rows": [{"amount": D("1.50")}]},
            {1: "a"},
            {"date": object()},
        ):
            with self.subTest(value=value):
                with self.assertRaises(integrity.IntegrityError):
                    integrity.canonical_json(value)
        with self.assertRaises(integrity.IntegrityError) as error:
            integrity.canonical_json({"rows": [{"amount": 1.5}]})
        self.assertEqual(error.exception.params["path"], "rows[0].amount")

    def test_cents_and_numbers(self):
        """Euro to cent, rounded half up; other numbers as text."""
        self.assertEqual(integrity.cents(1234.5), 123450)
        self.assertEqual(integrity.cents("0.005"), 1)
        self.assertEqual(integrity.cents("-0.005"), -1)
        self.assertEqual(integrity.cents(D("153.57")), 15357)
        self.assertEqual(integrity.cents(0.1 + 0.2), 30)
        self.assertEqual(integrity.cents(None), 0)
        self.assertEqual(integrity.cents(False), 0)
        self.assertEqual(integrity.decimal_text(0.8), "0.8")
        self.assertEqual(integrity.decimal_text(40.0), "40")
        self.assertEqual(integrity.decimal_text(D("1.2500")), "1.25")
        self.assertIsNone(integrity.decimal_text(None))
        self.assertEqual(
            integrity.normalize({"share": 0.7, 3: (1, D("2.10"))}),
            {"share": "0.7", "3": [1, "2.1"]},
        )

    # ------------------------------------------------------------ hashes
    def test_chain_hash(self):
        """chain_hash = SHA-256(prev_hash ‖ payload_sha256) of the texts."""
        payload = integrity.sha256_hex(b"{}")
        self.assertEqual(payload, hashlib.sha256(b"{}").hexdigest())
        self.assertEqual(
            integrity.chain_hash(integrity.GENESIS, payload),
            hashlib.sha256((integrity.GENESIS + payload).encode()).hexdigest(),
        )
        for wrong in ("", "abc", "G" * 64, payload.upper()):
            with self.subTest(value=wrong):
                with self.assertRaises(integrity.IntegrityError):
                    integrity.chain_hash(wrong, payload)

    def test_verify_chain_intact(self):
        self.assertEqual(integrity.verify_chain(_chain(b"one", b"two", b"three")), [])
        self.assertEqual(integrity.verify_chain([]), [])

    def test_verify_chain_problems(self):
        """A changed or missing payload, a wrong chain hash, a broken link
        and a gap are found at their position."""
        one, two, three = _chain(b"one", b"two", b"three")

        def keys(links):
            return [(p.key, p.index) for p in integrity.verify_chain(links)]

        changed = integrity.Link(**{**vars(two), "content_sha256": "0" * 64})
        self.assertEqual(keys([one, changed, three]), [("payload_changed", 2)])
        missing = integrity.Link(**{**vars(two), "content_sha256": None})
        self.assertEqual(keys([one, missing, three]), [("payload_missing", 2)])
        # the payload hash was replaced together with the payload: the chain
        # hash no longer follows, and the next link points to the old one
        forged_hash = integrity.sha256_hex(b"forged")
        forged = integrity.Link(
            **{
                **vars(two),
                "payload_sha256": forged_hash,
                "content_sha256": forged_hash,
            }
        )
        self.assertEqual(keys([one, forged, three]), [("chain_hash", 2)])
        # everything after it computed again: the link to the first one breaks
        relinked = _link(2, "1" * 64, b"forged")
        after = _link(3, relinked.chain_hash, b"three")
        self.assertEqual(keys([one, relinked, after]), [("broken_link", 2)])
        # a finalisation removed from the chain
        self.assertEqual(keys([one, three]), [("gap", 3), ("broken_link", 3)])
        # the first link must follow the genesis value
        self.assertEqual(keys([_link(1, "1" * 64, b"one")]), [("broken_link", 1)])

    # ------------------------------------------------------------ differences
    def test_diff(self):
        """C-60 and correction report: changed amounts and texts by code; a
        missing amount is 0, a missing text empty."""
        self.assertEqual(
            integrity.diff(
                {"EUER.20.111": 100000, "EUER.25.171": 1400, "EUER.15.120": "2"},
                {"EUER.20.111": 130000, "EUER.25.171": 1400, "EUER.20.112": 500},
            ),
            [
                ("EUER.15.120", "2", ""),
                ("EUER.20.111", 100000, 130000),
                ("EUER.20.112", 0, 500),
            ],
        )
        self.assertEqual(integrity.diff({"A": 0}, {}), [])

    def test_changed_sections(self):
        stored = {"fields": [{"code": "A", "amount": 1}], "run": {"hash": "x"}}
        current = {"fields": [{"amount": 1, "code": "A"}], "run": {"hash": "y"}}
        self.assertEqual(
            integrity.changed_sections(stored, current, ("fields", "run")), ["run"]
        )
