import pytest

from gameforge.game.aureus.formula import safe_eval, FormulaError


def test_safe_eval_arithmetic():
    assert safe_eval("max(1, atk*power//100 - defense)", {"atk": 10, "power": 120, "defense": 3}) == 9


def test_safe_eval_rejects_non_whitelisted():
    with pytest.raises(FormulaError):
        safe_eval("__import__('os').system('x')", {})
    with pytest.raises(FormulaError):
        safe_eval("atk.__class__", {"atk": 1})
