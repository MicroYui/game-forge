import pytest
from gameforge.agents.prompts.registry import get_prompt, register_prompt, render


def test_register_and_render():
    register_prompt("triage.system", "triage@1", "Triage {n} findings.")
    assert get_prompt("triage.system") == ("triage@1", "Triage {n} findings.")
    assert render("triage.system", n=3) == ("triage@1", "Triage 3 findings.")


def test_unregistered_raises():
    with pytest.raises(KeyError):
        get_prompt("nope")
