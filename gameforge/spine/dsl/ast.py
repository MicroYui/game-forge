"""Safe Selector + assert-expression typed AST (contract §3 — DSL *parsing*).

`contracts/dsl.py` defines the DSL *grammar* (machine-readable schema:
`Constraint`/`Predicate`/`Selector`). This module is where a `Constraint`'s
`assert` mini-expression (plain text, authored as content — e.g.
``"reward_gold <= 80"``) gets turned into a typed AST that Task 6/7 compilers
(SMT/ASP/Graph) can consume, and where a `Selector` gets evaluated against an
`IRGraph`.

The `assert` text is content, not trusted source, so — exactly like
``game/aureus/formula.py``'s ``safe_eval`` — we NEVER use Python's
``eval``/``exec``. We parse with ``ast.parse(expr, mode="eval")`` and walk the
AST ourselves, allowing only a small whitelist of node types; anything else
raises `DslError` immediately (fail closed, not open).

This module cannot import `game.aureus.formula` (dependency direction is
`agents -> spine`, never the reverse, and `spine` never imports `game`), so
the fail-closed whitelist-walk pattern is re-implemented here, spine-local,
over a different (assert-expression, not integer-only) grammar:

- comparisons: ``== != < <= > >=`` (including chained, e.g. ``0 <= x <= 1``,
  desugared to an `and`-`BoolOp` of pairwise `Compare` nodes)
- boolean: ``and or not``
- arithmetic: ``+ - * / // %`` (binary) and unary ``-``
- constants: `int` / `float` / `str` literals (NOT `bool`/`None`/bytes/etc.)
- bare names (`Field`) and dotted attribute access `a.b` (`Field("a.b")`) —
  EXCEPT dunder attributes (`x.__class__`), which are always rejected
- whitelisted function calls, parsed into a `Call` node but NOT evaluated
  here: `max, min, exists, forall, reachable_in, sum, prob_sum, monotonic,
  in_range, count`, plus the llm-assisted placeholder
  `semantically_reveals_identity` (M1 parses it; M2 evaluates it via the
  agent layer — 不简化只延后).

Anything else — comprehensions, lambdas, subscripts, starred/keyword args,
`__import__`, walrus, f-strings, ternaries, etc. — is not in the whitelist
and so falls through to `DslError`.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field

from gameforge.contracts.dsl import Selector
from gameforge.contracts.ir import Entity, NodeType
from gameforge.spine.ir.store import IRGraph


class DslError(Exception):
    """Raised when an assert-expression uses a non-whitelisted construct, or
    a `Selector.node_type` does not name a valid `NodeType` — fail closed."""


# --- typed AST -------------------------------------------------------------
#
# Each node carries exactly what a downstream compiler (Task 6 SMT / Task 5
# ASP / Task 4 Graph) needs: the operator plus its operands/children, or —
# for `Call` — the whitelisted function name plus parsed (not evaluated)
# argument nodes. Frozen dataclasses: the tree is a pure, immutable value.


@dataclass(frozen=True)
class Const:
    """A literal `int` / `float` / `str` constant."""

    value: int | float | str


@dataclass(frozen=True)
class Field:
    """A bare name (`x` -> `Field("x")`) or dotted attribute chain
    (`a.b` -> `Field("a.b")`). Downstream compilers resolve `path` against a
    bound entity's `attrs` (or a selector-bound variable for the dotted
    case)."""

    path: str


@dataclass(frozen=True)
class UnaryOp:
    op: str  # "-" | "not"
    operand: "AssertNode"


@dataclass(frozen=True)
class BinOp:
    op: str  # "+" | "-" | "*" | "/" | "//" | "%"
    left: "AssertNode"
    right: "AssertNode"


@dataclass(frozen=True)
class Compare:
    op: str  # "==" | "!=" | "<" | "<=" | ">" | ">="
    left: "AssertNode"
    right: "AssertNode"


@dataclass(frozen=True)
class BoolOp:
    op: str  # "and" | "or"
    values: tuple["AssertNode", ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Call:
    """A whitelisted function call, e.g. `exists(...)`, `reachable_in(...)`,
    or the llm-assisted placeholder `semantically_reveals_identity(...)`.
    Parsed into this node only — NOT evaluated in M1 (compilers/M2 agent
    layer decide what to do with `func`/`args`)."""

    func: str
    args: tuple["AssertNode", ...] = field(default_factory=tuple)


AssertNode = Const | Field | UnaryOp | BinOp | Compare | BoolOp | Call


# --- whitelist ---------------------------------------------------------

_COMPARE_OPS: dict[type, str] = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
}

_BOOL_OPS: dict[type, str] = {ast.And: "and", ast.Or: "or"}

_BIN_OPS: dict[type, str] = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
    ast.FloorDiv: "//",
    ast.Mod: "%",
}

WHITELISTED_CALLS = frozenset(
    {
        "max",
        "min",
        "exists",
        "forall",
        "reachable_in",
        "sum",
        "prob_sum",
        "monotonic",
        "in_range",
        "count",
        "semantically_reveals_identity",  # llm-assisted placeholder; not evaluated in M1
    }
)

_MAX_DEPTH = 64
"""Max recursive AST-walk depth (mirrors `game/aureus/formula.py`'s bound):
comfortably below Python's own recursion limit so a pathologically
deep-nested assert-expression always raises `DslError`, never a raw
`RecursionError`."""


def parse_assert(expr: str) -> AssertNode:
    """Parse a DSL `assert` mini-expression into a typed `AssertNode` tree.

    Uses `ast.parse(expr, mode="eval")` + a fail-closed recursive-descent
    whitelist walk. NO `eval`/`exec` is ever invoked. Raises `DslError` for
    any syntax error or non-whitelisted construct.
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise DslError(f"invalid assert-expression syntax: {expr!r}") from exc
    try:
        return _build(tree.body, 0)
    except RecursionError as exc:
        # Belt-and-braces: `_MAX_DEPTH` below should always trip first, but
        # never let a raw RecursionError escape this boundary regardless.
        raise DslError(f"expression too deeply nested: {expr!r}") from exc


def _build(node: ast.AST, depth: int) -> AssertNode:
    if depth > _MAX_DEPTH:
        raise DslError(f"expression too deeply nested (max depth {_MAX_DEPTH})")

    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise DslError(f"unsupported constant: {value!r}")
        return Const(value=value)

    if isinstance(node, ast.Name):
        return Field(path=node.id)

    if isinstance(node, ast.Attribute):
        return Field(path=_attr_path(node))

    if isinstance(node, ast.BoolOp):
        op = _BOOL_OPS.get(type(node.op))
        if op is None:
            raise DslError(f"boolean operator not allowed: {type(node.op).__name__}")
        return BoolOp(op=op, values=tuple(_build(v, depth + 1) for v in node.values))

    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return UnaryOp(op="not", operand=_build(node.operand, depth + 1))
        if isinstance(node.op, ast.USub):
            return UnaryOp(op="-", operand=_build(node.operand, depth + 1))
        raise DslError(f"unary operator not allowed: {type(node.op).__name__}")

    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise DslError(f"arithmetic operator not allowed: {type(node.op).__name__}")
        return BinOp(
            op=op, left=_build(node.left, depth + 1), right=_build(node.right, depth + 1)
        )

    if isinstance(node, ast.Compare):
        return _build_compare(node, depth)

    if isinstance(node, ast.Call):
        return _build_call(node, depth)

    raise DslError(f"node not allowed: {type(node).__name__}")


def _build_compare(node: ast.Compare, depth: int) -> AssertNode:
    left = node.left
    parts: list[Compare] = []
    for op_node, right in zip(node.ops, node.comparators):
        op = _COMPARE_OPS.get(type(op_node))
        if op is None:
            raise DslError(f"comparison operator not allowed: {type(op_node).__name__}")
        parts.append(
            Compare(op=op, left=_build(left, depth + 1), right=_build(right, depth + 1))
        )
        left = right
    if len(parts) == 1:
        return parts[0]
    # chained comparison (e.g. `0 <= x <= 1`) desugars to an `and` of the
    # pairwise comparisons — no new node shape needed.
    return BoolOp(op="and", values=tuple(parts))


def _build_call(node: ast.Call, depth: int) -> AssertNode:
    if not isinstance(node.func, ast.Name):
        raise DslError("only whitelisted function-name calls are allowed")
    name = node.func.id
    if name not in WHITELISTED_CALLS:
        raise DslError(f"call not allowed: {name}()")
    if node.keywords:
        raise DslError("keyword arguments are not allowed")
    if any(isinstance(a, ast.Starred) for a in node.args):
        raise DslError("starred arguments are not allowed")
    return Call(func=name, args=tuple(_build(a, depth + 1) for a in node.args))


def _attr_path(node: ast.Attribute) -> str:
    """Walk a (possibly dotted) `ast.Attribute` chain down to its root
    `ast.Name`, rejecting dunder segments (`x.__class__`) and any chain that
    doesn't bottom out in a plain name (e.g. `a[0].b`)."""
    parts: list[str] = []
    cur: ast.expr = node
    while isinstance(cur, ast.Attribute):
        if cur.attr.startswith("__") and cur.attr.endswith("__"):
            raise DslError(f"dunder attribute access not allowed: {cur.attr!r}")
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        raise DslError("only simple dotted attribute chains (a.b.c) are allowed")
    parts.append(cur.id)
    return ".".join(reversed(parts))


def free_names(node: AssertNode) -> set[str]:
    """Return the set of `Field` paths (bare names and dotted attribute
    paths, e.g. `{"reward_gold"}` or `{"step.item_id"}`) referenced anywhere
    in `node`'s tree — the graph attributes a downstream compiler needs to
    bind."""
    out: set[str] = set()
    _collect_field_paths(node, out)
    return out


def _collect_field_paths(node: AssertNode, out: set[str]) -> None:
    if isinstance(node, Field):
        out.add(node.path)
    elif isinstance(node, Const):
        pass
    elif isinstance(node, UnaryOp):
        _collect_field_paths(node.operand, out)
    elif isinstance(node, BinOp):
        _collect_field_paths(node.left, out)
        _collect_field_paths(node.right, out)
    elif isinstance(node, Compare):
        _collect_field_paths(node.left, out)
        _collect_field_paths(node.right, out)
    elif isinstance(node, BoolOp):
        for v in node.values:
            _collect_field_paths(v, out)
    elif isinstance(node, Call):
        for a in node.args:
            _collect_field_paths(a, out)
    else:  # pragma: no cover - exhaustive over AssertNode's closed union
        raise DslError(f"unknown AssertNode: {type(node).__name__}")


# --- Selector ---------------------------------------------------------


def select(graph: IRGraph, selector: Selector) -> list[Entity]:
    """Evaluate a `Selector` against `graph`: entities of `selector.node_type`
    whose `attrs` match every `selector.where` key/value pair.

    Raises `DslError` if `selector.node_type` does not name a valid
    `NodeType` enum member.
    """
    try:
        node_type = NodeType[selector.node_type]
    except KeyError as exc:
        raise DslError(f"selector.node_type is not a valid NodeType: {selector.node_type!r}") from exc
    entities = graph.nodes_of_type(node_type)
    if not selector.where:
        return list(entities)
    return [
        e for e in entities if all(e.attrs.get(k) == v for k, v in selector.where.items())
    ]
