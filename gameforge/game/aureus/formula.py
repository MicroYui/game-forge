"""Safe formula evaluation for damage/curve expressions (Aureus M0b).

Design content authors damage/curve formulas as plain-text expressions (e.g.
``"max(1, atk*power//100 - defense)"``) that the checker/DSL layer compiles
and the Aureus kernel evaluates at runtime. This is a security-critical
boundary (PRD §12A.5 — DSL→solver sandbox): expressions come from content
data, not trusted source, so we NEVER use Python's ``eval``/``exec``.

Instead we parse with ``ast.parse(expr, mode="eval")`` and walk the AST
ourselves, allowing only a small whitelist of node types. Any other node
(attribute access, subscripts, comprehensions, imports, arbitrary calls...)
raises ``FormulaError`` immediately — fail closed, not open.
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


class FormulaError(Exception):
    """Raised when a formula expression contains a non-whitelisted construct."""


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
    return int(_ev(tree.body if isinstance(tree, ast.Expression) else tree, names))


def _ev(node: ast.AST, names: dict[str, int]) -> int:
    if isinstance(node, ast.Expression):
        return _ev(node.body, names)
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
        return int(op_fn(_ev(node.left, names), _ev(node.right, names)))
    if isinstance(node, ast.UnaryOp):
        if not isinstance(node.op, ast.USub):
            raise FormulaError(f"unary operator not allowed: {type(node.op).__name__}")
        return -_ev(node.operand, names)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_CALLS:
            raise FormulaError("only max()/min() calls are allowed")
        if node.keywords:
            raise FormulaError("keyword arguments are not allowed")
        args = [_ev(a, names) for a in node.args]
        return int(_ALLOWED_CALLS[node.func.id](*args))
    raise FormulaError(f"node not allowed: {type(node).__name__}")
