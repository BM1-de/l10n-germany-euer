# Copyright 2026 Baumgärtner Marketing GmbH
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Formula evaluation for the official forms (spec 3.2 and 3.3).

Pure Python without any ORM access, so it can be tested without a database.

A formula is an expression over field codes of the same assessment year
(``EUER.20.111``, ``AVEUER.40.490``, ``EUER.G.UEB`` ...), non-negative decimal
literals (``2050.00``), the operators ``+`` and ``-`` (binary and unary), the
multiplication ``*`` by a constant factor (``0.06 * SZ.43.X``; a product of
two fields is not allowed, so every formula stays linear), the functions
``min()`` and ``max()`` and parentheses. Nothing else is accepted. Formulas
are tokenised and parsed by a small recursive-descent parser into a tree of
tuples; there is no ``eval``.

Empty values: a field without a value is ``None``, never ``0`` (official
instructions: fields that do not apply stay empty). Empty operands count as
0, but a formula stays empty as long as all fields it refers to are empty.
Literals and constant fields (formulas without any reference, e.g. the
pre-printed 2,050.00 of Anlage SZ) never make a result non-empty on their own.
"""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

#: Field code: form, box and key, e.g. ``EUER.20.111`` or ``EUER.25.UEB1``.
CODE_PATTERN = r"[A-Z][A-Z0-9]*\.[A-Z0-9]+\.[A-Z0-9_]+"
#: ASCII only: ``\d`` would also accept other Unicode digits
CODE_RE = re.compile(CODE_PATTERN, re.ASCII)

MAX_FORMULA_LENGTH = 2000
MAX_NESTING = 50
#: Digits before the decimal point of a literal (amounts up to 10^15 - 1)
MAX_INTEGER_DIGITS = 15

_TOKEN_RE = re.compile(
    r"\s*(?:"
    rf"(?P<code>{CODE_PATTERN})"
    r"|(?P<number>\d+(?:\.\d+)?)"
    r"|(?P<func>min|max)(?![A-Za-z0-9_])"
    r"|(?P<op>[-+(),*])"
    r")",
    re.ASCII,
)


class FormulaError(ValueError):
    """Raised for formulas that cannot be parsed or evaluated."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _tokenize(formula):
    if not isinstance(formula, str):
        raise FormulaError(f"Formula must be a string, got {type(formula).__name__}")
    if len(formula) > MAX_FORMULA_LENGTH:
        raise FormulaError(f"Formula longer than {MAX_FORMULA_LENGTH} characters")
    tokens = []
    pos = 0
    end = len(formula.rstrip())
    while pos < end:
        match = _TOKEN_RE.match(formula, pos)
        if not match or match.end() == pos:
            raise FormulaError(
                f"Unexpected character {formula[pos:].strip()[:20]!r} at position {pos}"
            )
        kind = match.lastgroup
        tokens.append((kind, match.group(kind)))
        pos = match.end()
    if not tokens:
        raise FormulaError("Empty formula")
    return tokens


class _Parser:
    """Recursive-descent parser.

    Grammar::

        expr    := term (("+" | "-") term)*
        term    := unary ("*" unary)*
        unary   := ("+" | "-") unary | primary
        primary := NUMBER | CODE | FUNC "(" expr ("," expr)* ")" | "(" expr ")"

    In a product at least one factor must be constant (no field code).
    """

    def __init__(self, formula):
        self.formula = formula
        self.tokens = _tokenize(formula)
        self.pos = 0
        self.depth = 0

    def _peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else (None, None)

    def _take(self):
        token = self._peek()
        self.pos += 1
        return token

    def _expect_op(self, value):
        kind, text = self._take()
        if kind != "op" or text != value:
            found = text if text is not None else "end of formula"
            raise FormulaError(f"Expected {value!r}, found {found!r}")

    def parse(self):
        node = self._expr()
        if self.pos != len(self.tokens):
            raise FormulaError(f"Unexpected {self._peek()[1]!r} after complete term")
        return node

    def _expr(self):
        self.depth += 1
        if self.depth > MAX_NESTING:
            raise FormulaError("Formula nested too deeply")
        node = self._term()
        while True:
            kind, text = self._peek()
            if kind == "op" and text in "+-":
                self._take()
                node = ("add" if text == "+" else "sub", node, self._term())
            else:
                break
        self.depth -= 1
        return node

    def _term(self):
        node = self._unary()
        while self._peek() == ("op", "*"):
            self._take()
            factor = self._unary()
            if not (_is_constant(node) or _is_constant(factor)):
                raise FormulaError("A product needs a constant factor")
            node = ("mul", node, factor)
        return node

    def _unary(self):
        kind, text = self._peek()
        if kind == "op" and text in "+-":
            self._take()
            self.depth += 1
            if self.depth > MAX_NESTING:
                raise FormulaError("Formula nested too deeply")
            operand = self._unary()
            self.depth -= 1
            return operand if text == "+" else ("neg", operand)
        return self._primary()

    def _primary(self):
        kind, text = self._take()
        if kind == "number":
            if len(text.split(".", 1)[0]) > MAX_INTEGER_DIGITS:
                raise FormulaError(f"Number {text[:20]}… is too large")
            return ("num", Decimal(text))
        if kind == "code":
            return ("ref", text)
        if kind == "func":
            self._expect_op("(")
            args = [self._expr()]
            while self._peek() == ("op", ","):
                self._take()
                args.append(self._expr())
            self._expect_op(")")
            return (text, tuple(args))
        if kind == "op" and text == "(":
            node = self._expr()
            self._expect_op(")")
            return node
        found = text if text is not None else "end of formula"
        raise FormulaError(f"Unexpected {found!r}")


@lru_cache(maxsize=4096)
def parse(formula):
    """Return the syntax tree of ``formula``; raise :class:`FormulaError`."""
    return _Parser(formula).parse()


def _is_constant(node):
    """Return whether the subtree ``node`` refers to no field."""
    refs = set()
    _collect_refs(node, refs)
    return not refs


def _collect_refs(node, out):
    kind = node[0]
    if kind == "ref":
        out.add(node[1])
    elif kind == "neg":
        _collect_refs(node[1], out)
    elif kind in ("add", "sub", "mul"):
        _collect_refs(node[1], out)
        _collect_refs(node[2], out)
    elif kind in ("min", "max"):
        for arg in node[1]:
            _collect_refs(arg, out)


def references(formula):
    """Return the set of field codes used in ``formula``."""
    refs = set()
    _collect_refs(parse(formula), refs)
    return frozenset(refs)


def signed_references(formula):
    """Return ``{code: sign}`` for formulas that are plain sums.

    ``sign`` is ``1`` or ``-1``. Raises :class:`FormulaError` if the formula
    uses ``min``/``max``, literals or refers to a code more than once, because
    then no single sign can be given.
    """
    result = {}

    def walk(node, sign):
        kind = node[0]
        if kind == "ref":
            if node[1] in result:
                raise FormulaError(f"Code {node[1]} used more than once")
            result[node[1]] = sign
        elif kind == "neg":
            walk(node[1], -sign)
        elif kind == "add":
            walk(node[1], sign)
            walk(node[2], sign)
        elif kind == "sub":
            walk(node[1], sign)
            walk(node[2], -sign)
        else:
            raise FormulaError("Formula is not a plain sum of codes")

    walk(parse(formula), 1)
    return result


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def to_amount(value):
    """Convert an input value to a :class:`~decimal.Decimal` in euro and cent.

    ``None`` and ``False`` (Odoo's empty value) stay ``None``. Floats are
    converted via their shortest string representation, so ``0.1 + 0.2``
    becomes ``0.30``.
    """
    if value is None or value is False:
        return None
    if isinstance(value, bool):
        raise FormulaError("Boolean is not an amount")
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
        if not amount.is_finite():
            raise InvalidOperation
        return amount.quantize(CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as error:
        raise FormulaError(f"Invalid amount {value!r}") from error


def _combine(results, function):
    """Apply ``function`` to (value, has_data) pairs; empty values count as 0."""
    present = [value for value, _has_data in results if value is not None]
    if not present:
        return None, False
    value = function(ZERO if value is None else value for value, _has in results)
    return value, any(has_data for _value, has_data in results)


def _evaluate(node, values, weak):
    """Return ``(value, has_data)`` of ``node``.

    ``has_data`` tells whether a non-empty, non-constant field contributed.
    """
    kind = node[0]
    if kind == "num":
        return node[1], False
    if kind == "ref":
        value = values.get(node[1])
        return value, value is not None and node[1] not in weak
    if kind == "neg":
        value, has_data = _evaluate(node[1], values, weak)
        return (None if value is None else -value), has_data
    if kind in ("add", "sub"):
        left, right = _evaluate(node[1], values, weak), _evaluate(node[2], values, weak)
        if kind == "sub":
            right = (None if right[0] is None else -right[0], right[1])
        return _combine([left, right], sum)
    if kind == "mul":
        left, right = _evaluate(node[1], values, weak), _evaluate(node[2], values, weak)
        return _combine([left, right], _product)
    if kind in ("min", "max"):
        function = min if kind == "min" else max
        return _combine([_evaluate(arg, values, weak) for arg in node[1]], function)
    raise FormulaError(f"Unknown node {kind!r}")  # pragma: no cover


def _product(factors):
    result = Decimal(1)
    for factor in factors:
        result *= factor
    return result


def evaluate(formula, values, weak=frozenset()):
    """Evaluate ``formula`` with ``values`` (code → Decimal or None).

    ``weak`` are codes of constant fields: their values alone do not make
    the result non-empty. A formula without any reference is a constant and
    always returns its value.
    """
    node = parse(formula)
    try:
        value, has_data = _evaluate(node, values, weak)
        if value is None or (not has_data and references(formula)):
            return None
        return value.quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation as error:
        raise FormulaError(f"Result of {formula[:40]!r} is out of range") from error


# ---------------------------------------------------------------------------
# Whole form
# ---------------------------------------------------------------------------


def _find_cycle(graph, nodes):
    """Return one cycle (list of codes) among ``nodes`` of ``graph``."""
    state = {}
    stack = []

    def visit(node):
        state[node] = "open"
        stack.append(node)
        for dep in sorted(graph.get(node, ())):
            if dep not in nodes:
                continue
            if state.get(dep) == "open":
                return stack[stack.index(dep) :] + [dep]
            if dep not in state:
                cycle = visit(dep)
                if cycle:
                    return cycle
        stack.pop()
        state[node] = "done"
        return None

    for node in sorted(nodes):
        if node not in state:
            cycle = visit(node)
            if cycle:
                return cycle
    return []  # pragma: no cover


def calculation_order(formulas):
    """Return the codes of ``formulas`` so that each comes after its inputs.

    ``formulas`` maps code → formula. Codes that are only referenced are
    inputs and not part of the result. Raises :class:`FormulaError` on cycles.
    """
    graph = {code: references(formula) for code, formula in formulas.items()}
    pending = {
        code: {dep for dep in deps if dep in graph} for code, deps in graph.items()
    }
    order = []
    ready = sorted(code for code, deps in pending.items() if not deps)
    while ready:
        code = ready.pop(0)
        order.append(code)
        released = []
        for other, deps in pending.items():
            if code in deps:
                deps.discard(code)
                if not deps:
                    released.append(other)
        if released:
            ready = sorted(ready + released)
    if len(order) != len(graph):
        remaining = set(graph) - set(order)
        cycle = _find_cycle(graph, remaining)
        raise FormulaError("Cyclic formulas: " + " -> ".join(cycle))
    return order


def validate(formulas, known_codes):
    """Return a list of error messages for ``formulas``.

    Checks syntax, that every referenced code is in ``known_codes`` or is
    itself computed by one of ``formulas``, and that there is no cycle.
    """
    known = set(known_codes) | set(formulas)
    errors = []
    parsed = {}
    for code in sorted(formulas):
        try:
            refs = references(formulas[code])
        except FormulaError as error:
            errors.append(f"{code}: {error}")
            continue
        parsed[code] = formulas[code]
        unknown = sorted(refs - known)
        if unknown:
            errors.append(f"{code}: unknown codes {', '.join(unknown)}")
    try:
        calculation_order(parsed)
    except FormulaError as error:
        errors.append(str(error))
    return errors


def condition_met(condition, values, weak=frozenset()):
    """Return whether a fill condition is met: its result is above zero."""
    value = evaluate(condition, values, weak)
    return value is not None and value > 0


def compute(formulas, amounts, overrides=None, conditions=None, known=None):
    """Compute all fields of a form year.

    :param formulas: code → formula of the computed fields
    :param amounts: code → amount of the input fields (numbers, ``Decimal``
        or ``None``); codes of computed fields are not allowed here
    :param overrides: code → amount that replaces the result of a formula
        (manual override with reason, recorded by the caller)
    :param conditions: code → fill condition of the field (formula over
        other codes). If its result is not above zero, the field stays empty,
        whether it is an input or computed (e.g. Anlage SZ only with other
        interest above 2,050 €).
    :param known: all codes of the form year; a formula or condition that
        refers to another code raises :class:`FormulaError` (a renamed or
        deleted field must not silently count as empty)
    :return: dict code → ``Decimal`` or ``None`` for all inputs and formulas
    """
    overrides = overrides or {}
    conditions = conditions or {}
    unknown_overrides = sorted(set(overrides) - set(formulas))
    if unknown_overrides:
        raise FormulaError(
            "Overrides for fields without formula: " + ", ".join(unknown_overrides)
        )
    if known is not None:
        dangling = sorted(
            codes_of([*formulas.values(), *conditions.values()])
            - set(known)
            - set(formulas)
        )
        if dangling:
            raise FormulaError(
                "Formulas refer to unknown codes: " + ", ".join(dangling)
            )
    values = {}
    for code, amount in amounts.items():
        value = to_amount(amount)
        if code in formulas and value is not None:
            raise FormulaError(f"{code} is computed by a formula; use an override")
        values[code] = value
    constants = frozenset(
        code for code, formula in formulas.items() if not references(formula)
    )
    # a field comes after the inputs of its formula and of its condition
    graph = {
        code: " + ".join(
            f"({part})" for part in (formulas.get(code), conditions.get(code)) if part
        )
        for code in set(formulas) | set(conditions)
    }
    for code in calculation_order(graph):
        condition = conditions.get(code)
        if condition and not condition_met(condition, values, constants):
            values[code] = None
        elif code not in formulas:
            values.setdefault(code, None)
        elif code in overrides:
            values[code] = to_amount(overrides[code])
        else:
            values[code] = evaluate(formulas[code], values, constants)
    return values


def negative_codes(values, allow_negative):
    """Return the codes with a negative value that may not be negative (F-06)."""
    allowed = set(allow_negative)
    return sorted(
        code
        for code, value in values.items()
        if value is not None and value < 0 and code not in allowed
    )


def drop_empty(values, keep=()):
    """Return ``values`` without empty and zero amounts (R-46, FT-07).

    Codes in ``keep`` (mandatory fields) are kept even when they are zero.
    """
    keep = set(keep)
    return {
        code: value
        for code, value in values.items()
        if value is not None and (value != 0 or code in keep)
    }


def is_code(text):
    """Return whether ``text`` is a syntactically valid field code."""
    return bool(isinstance(text, str) and CODE_RE.fullmatch(text))


def codes_of(formulas):
    """Return all codes referenced by the given formulas."""
    result = set()
    for formula in formulas:
        result |= references(formula)
    return frozenset(result)
