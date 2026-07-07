import pytest

from gameforge.runtime.secrets.env import get_llm_key


def test_get_llm_key_reads_env(monkeypatch):
    monkeypatch.setenv("GAMEFORGE_LLM_KEY", "sk-test")
    assert get_llm_key() == "sk-test"


def test_get_llm_key_raises_when_absent(monkeypatch):
    monkeypatch.delenv("GAMEFORGE_LLM_KEY", raising=False)
    with pytest.raises(RuntimeError):
        get_llm_key()
