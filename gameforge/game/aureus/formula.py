"""Safe formula evaluation for damage/curve expressions (Aureus M0b).

Design content authors damage/curve formulas as plain-text expressions (e.g.
``"max(1, atk*power//100 - defense)"``) that the checker/DSL layer compiles
and the Aureus kernel evaluates at runtime. This is a security-critical
boundary (PRD §12A.5 — DSL→solver sandbox: 防畸形约束致资源耗尽 DoS):
expressions come from content data, not trusted source, so we NEVER use
Python's ``eval``/``exec``.

Instead we parse with ``ast.parse(expr, mode="eval")`` and walk the AST
ourselves, allowing only a small whitelist of node types. Any other node
(attribute access, subscripts, comprehensions, imports, arbitrary calls...)
raises ``FormulaError`` immediately — fail closed, not open.

The whitelist alone is not sufficient: well-typed-but-degenerate content can
still exhaust CPU/memory or leak a raw (non-``FormulaError``) Python
exception out of this boundary — an unbounded ``**`` tower, expressions
nested past the recursion limit, division/modulo by zero, or a no-arg
``max()``/``min()``. All of those are bounded/caught below so the contract
holds: malformed formula *content* always raises `FormulaError`, never a
raw `RecursionError`/`ZeroDivisionError`/`TypeError`/`OverflowError`, and
never runs away with CPU or memory.
"""

from __future__ import annotations

import ast

_ALLOWED_BINOPS: dict[type, object] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.FloorDiv: lambda a, b: a // b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a**b,
}
_ALLOWED_CALLS: dict[str, object] = {"max": max, "min": min}

# Fail-closed resource bounds (PRD §12A.5). These do not remove any
# whitelisted node/operator — they just cap how expensive evaluating it can
# get before we bail out with `FormulaError`.
_MAX_DEPTH = 64
"""Max recursive AST-walk depth. Comfortably below Python's own recursion
limit, so we always raise `FormulaError` before a raw `RecursionError` could
occur (e.g. from a pathologically deep-nested expression)."""

_MAX_POW_EXPONENT = 64
"""Max magnitude for a `**` exponent. Blocks unbounded towers like
``9**9**9**9`` (right-associative, so the *innermost* result already
overflows this bound) before Python attempts the (potentially astronomical)
power."""

_MAX_POW_BASE = 2**32
"""Max magnitude for a `**` base. Even with the exponent bounded, chaining
bounded powers (`(x**64)**64`) can still blow up bit-length across levels;
bounding the base too means the *next* level's bound check trips first."""


class FormulaError(Exception):
    """Raised when a formula expression contains a non-whitelisted construct,
    or a whitelisted-but-degenerate operation (out-of-bounds `**`, division
    or modulo by zero, a no-arg `max`/`min`, or excessive nesting depth) that
    would otherwise leak a raw Python exception or exhaust CPU/memory."""


def safe_eval(expr: str, names: dict[str, int]) -> int:
    """Evaluate `expr` using only integer arithmetic over `names`.

    Whitelisted grammar: ``+ - * // % ** ( )`` (binary + unary minus),
    integer literals, bare names resolved from `names`, and calls to
    `max`/`min` with all-integer arguments. Anything else raises
    `FormulaError`. NO python `eval`/`exec` is ever invoked.
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise FormulaError(f"invalid formula syntax: {expr!r}") from exc
    try:
        body = tree.body if isinstance(tree, ast.Expression) else tree
        return int(_ev(body, names, 0))
    except RecursionError as exc:
        # Belt-and-braces: `_MAX_DEPTH` below should always trip first, but
        # never let a raw RecursionError escape this boundary regardless.
        raise FormulaError(f"expression too deeply nested: {expr!r}") from exc


def _ev(node: ast.AST, names: dict[str, int], depth: int) -> int:
    if depth > _MAX_DEPTH:
        raise FormulaError(f"expression too deeply nested (max depth {_MAX_DEPTH})")
    if isinstance(node, ast.Expression):
        return _ev(node.body, names, depth + 1)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int):
            raise FormulaError(f"only int literals allowed, got {node.value!r}")
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in names:
            raise FormulaError(f"unknown name: {node.id!r}")
        return int(names[node.id])
    if isinstance(node, ast.BinOp):
        op_fn = _ALLOWED_BINOPS.get(type(node.op))
        if op_fn is None:
            raise FormulaError(f"operator not allowed: {type(node.op).__name__}")
        left = _ev(node.left, names, depth + 1)
        right = _ev(node.right, names, depth + 1)
        if isinstance(node.op, ast.Pow):
            if right < 0 or right > _MAX_POW_EXPONENT:
                raise FormulaError(f"** exponent out of bounds: {right!r}")
            if abs(left) > _MAX_POW_BASE:
                raise FormulaError(f"** base out of bounds: {left!r}")
        try:
            return int(op_fn(left, right))
        except ZeroDivisionError as exc:
            raise FormulaError(
                f"division/modulo by zero: {type(node.op).__name__}"
            ) from exc
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, ast.USub):
            raise FormulaError(f"unary operator not allowed: {type(node.op).__name__}")
        return -_ev(node.operand, names, depth + 1)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_CALLS:
            raise FormulaError("only max()/min() calls are allowed")
        if node.keywords:
            raise FormulaError("keyword arguments are not allowed")
        args = [_ev(a, names, depth + 1) for a in node.args]
        try:
            return int(_ALLOWED_CALLS[node.func.id](*args))
        except (TypeError, ValueError) as exc:
            raise FormulaError(
                f"invalid arguments to {node.func.id}(): {args!r}"
            ) from exc
    raise FormulaError(f"node not allowed: {type(node).__name__}")
