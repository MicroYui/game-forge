import pytest

from gameforge.game.aureus.formula import safe_eval, FormulaError


def test_safe_eval_arithmetic():
    assert safe_eval("max(1, atk*power//100 - defense)", {"atk": 10, "power": 120, "defense": 3}) == 9


def test_safe_eval_rejects_non_whitelisted():
    with pytest.raises(FormulaError):
        safe_eval("__import__('os').system('x')", {})
    with pytest.raises(FormulaError):
        safe_eval("atk.__class__", {"atk": 1})


def test_safe_eval_rejects_unbounded_pow_tower():
    # Right-associative: 9**(9**(9**9)) -> the middle exponent alone (9**9 =
    # 387420489) blows the exponent bound long before any huge int is built.
    with pytest.raises(FormulaError):
        safe_eval("9**9**9**9", {})


def test_safe_eval_rejects_deeply_nested_expression_not_recursionerror():
    # Plain parens don't nest in the AST (they're pure grouping), so use a
    # genuinely nested construct (chained unary minus) that would blow the
    # Python recursion limit in a naive recursive walker.
    expr = "-" * 2000 + "1"
    with pytest.raises(FormulaError):
        safe_eval(expr, {})


def test_safe_eval_rejects_floordiv_by_zero():
    with pytest.raises(FormulaError):
        safe_eval("atk // 0", {"atk": 1})


def test_safe_eval_rejects_mod_by_zero():
    with pytest.raises(FormulaError):
        safe_eval("atk % 0", {"atk": 1})


def test_safe_eval_rejects_noarg_max():
    with pytest.raises(FormulaError):
        safe_eval("max()", {})


def test_safe_eval_bounded_pow_still_works():
    # Pow stays allowed for well-behaved formulas — only degenerate towers
    # are rejected.
    assert safe_eval("2**8", {}) == 256
    assert safe_eval("atk**2", {"atk": 4}) == 16
