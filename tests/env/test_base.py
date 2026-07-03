import pytest

from gameforge.env.base import Environment


def test_environment_is_abstract():
    with pytest.raises(TypeError):
        Environment()  # abstract methods unimplemented


def test_contract_version_pinned():
    assert Environment.env_contract_version == "env@1"


def test_subclass_must_implement_all():
    class Partial(Environment):
        def reset(self, scenario, seed):  # missing step + state_hash
            ...

    with pytest.raises(TypeError):
        Partial()
