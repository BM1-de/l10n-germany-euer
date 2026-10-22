# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Finalisation of a statement: canonical payload, hashes, chain (spec 7.7, G-04).

Pure Python without ORM access. The ORM collects the data of a statement as
plain values (amounts as cent integers, dates as ISO text); this module

* serialises it as canonical JSON: sorted keys, no blanks, UTF-8, only
  ``None``, booleans, integers, text, lists and objects with text keys. A
  float or a ``Decimal`` is refused, so amounts never depend on the binary
  representation of a number (:func:`canonical_json`);
* hashes the payload (SHA-256) and chains the finalisations of a business:
  ``chain_hash = SHA-256(prev_hash ‖ payload_sha256)`` over the hexadecimal
  texts; the first link follows :data:`GENESIS` (:func:`chain_hash`);
* verifies a chain (:func:`verify_chain`) and compares values of two
  versions or runs (:func:`diff`, C-60 and the correction report).

The hash makes later changes visible, it does not prevent them: a database
administrator can still change the data with SQL (spec 7.7).
"""

import hashlib
import json
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

#: version of the payload layout; a new layout gets a new number
SCHEMA = "l10n_de_euer.statement/1"
#: previous hash of the first finalisation of a business
GENESIS = "0" * 64
_HEX = frozenset("0123456789abcdef")


class IntegrityError(Exception):
    """A value that cannot be part of the canonical payload."""

    def __init__(self, key, **params):
        super().__init__(key)
        self.key = key
        self.params = params


# ------------------------------------------------------------ values
def cents(value):
    """Amount in euro (float, ``Decimal``, text or ``None``) as cent integer,
    rounded half up."""
    if value in (None, False, ""):
        return 0
    try:
        amount = Decimal(str(value))
    except InvalidOperation as error:
        raise IntegrityError("not_an_amount", value=value) from error
    return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def decimal_text(value):
    """A number that is no amount (quantity, share, rate) as text without
    trailing zeros, e.g. ``0.8`` or ``40``; ``None`` stays ``None``."""
    if value is None or value is False:
        return None
    number = Decimal(str(value))
    if number == number.to_integral_value():
        return str(number.quantize(Decimal(1)))
    return format(number.normalize(), "f")


def normalize(value):
    """Turn data of a JSON field into canonical values: floats and
    ``Decimal`` become text (:func:`decimal_text`), tuples lists, keys text."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | str):
        return value
    if isinstance(value, float | Decimal):
        return decimal_text(value)
    if isinstance(value, dict):
        return {str(key): normalize(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [normalize(item) for item in value]
    raise IntegrityError("not_canonical", path="", type=type(value).__name__)


def _check(value, path):
    if value is None or isinstance(value, bool | str):
        return
    if isinstance(value, int):
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise IntegrityError("not_canonical", path=path, type="key")
            _check(item, f"{path}.{key}" if path else key)
        return
    raise IntegrityError("not_canonical", path=path, type=type(value).__name__)


def canonical_json(data):
    """Canonical JSON of ``data`` as UTF-8 bytes (sorted keys, compact)."""
    _check(data, "")
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def load(raw):
    """Payload from its bytes."""
    return json.loads(raw.decode("utf-8"))


def sha256_hex(raw):
    return hashlib.sha256(raw).hexdigest()


def _is_hash(value):
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


def chain_hash(prev_hash, payload_sha256):
    """``SHA-256(prev_hash ‖ payload_sha256)`` over the hexadecimal texts."""
    for value in (prev_hash, payload_sha256):
        if not _is_hash(value):
            raise IntegrityError("not_a_hash", value=value)
    return sha256_hex(f"{prev_hash}{payload_sha256}".encode("ascii"))


# ------------------------------------------------------------ chain
@dataclass(frozen=True, kw_only=True)
class Link:
    """A finalisation in the chain of a business.

    ``index``: position in the chain (1, 2, …); ``content_sha256``: hash of
    the stored payload as read now, ``None`` if the payload is missing.
    """

    index: int
    prev_hash: str
    payload_sha256: str
    chain_hash: str
    content_sha256: str | None
    reference: object = None


@dataclass(frozen=True, kw_only=True)
class Problem:
    """A finding of the integrity check."""

    key: str
    index: int
    reference: object = None
    params: dict = field(default_factory=dict)


def verify_chain(links):
    """Problems of a chain of finalisations, in the order of the chain.

    * ``payload_missing`` / ``payload_changed``: the stored payload is gone
      or its hash differs from the hash recorded at the finalisation;
    * ``chain_hash``: the chain hash does not follow from the previous hash
      and the payload hash;
    * ``gap``: a position of the chain is missing (a finalisation removed);
    * ``broken_link``: the previous hash is not the chain hash of the
      finalisation before (or :data:`GENESIS` for the first one).
    """
    problems = []
    expected_prev = GENESIS
    expected_index = 1
    for link in sorted(links, key=lambda item: item.index):
        if link.index != expected_index:
            problems.append(
                Problem(
                    key="gap",
                    index=link.index,
                    reference=link.reference,
                    params={"expected": expected_index},
                )
            )
        if link.content_sha256 is None:
            problems.append(
                Problem(
                    key="payload_missing", index=link.index, reference=link.reference
                )
            )
        elif link.content_sha256 != link.payload_sha256:
            problems.append(
                Problem(
                    key="payload_changed", index=link.index, reference=link.reference
                )
            )
        try:
            recomputed = chain_hash(link.prev_hash, link.payload_sha256)
        except IntegrityError:
            recomputed = None
        if recomputed != link.chain_hash:
            problems.append(
                Problem(key="chain_hash", index=link.index, reference=link.reference)
            )
        if link.prev_hash != expected_prev:
            problems.append(
                Problem(key="broken_link", index=link.index, reference=link.reference)
            )
        expected_prev = link.chain_hash
        expected_index = link.index + 1
    return problems


def changed_sections(stored, current, names):
    """Names of the sections of the payload whose stored content differs
    from the content rebuilt from the records now."""
    return [
        name
        for name in names
        if canonical_json(stored.get(name)) != canonical_json(current.get(name))
    ]


# ------------------------------------------------------------ differences
def diff(old, new):
    """Differences between two value sets ``{code: value}``.

    Returns ``[(code, old, new)]`` sorted by code for every code whose value
    differs; a missing amount counts as 0, a missing text as empty.
    """
    result = []
    for code in sorted(set(old) | set(new)):
        before, after = old.get(code), new.get(code)
        if isinstance(before, str) or isinstance(after, str):
            before, after = before or "", after or ""
        else:
            before, after = before or 0, after or 0
        if before != after:
            result.append((code, before, after))
    return result
