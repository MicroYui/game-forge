"""TDD: playtest planner/executor/reflect prompts registered with prompt_version."""
from __future__ import annotations

from gameforge.agents.playtest.prompts import register_playtest_prompts
from gameforge.agents.prompts.registry import get_prompt

_NAMES = ("playtest.planner", "playtest.executor", "playtest.reflect")


def test_playtest_prompts_registered_with_version_and_json():
    register_playtest_prompts()
    for name in _NAMES:
        version, template = get_prompt(name)
        assert version == "playtest@1"
        assert "JSON" in template


def test_playtest_prompts_state_engine_is_authoritative():
    register_playtest_prompts()
    for name in _NAMES:
        _, template = get_prompt(name)
        assert "AureusEnv" in template or "deterministic" in template


def test_register_playtest_prompts_is_idempotent():
    register_playtest_prompts()
    register_playtest_prompts()
    for name in _NAMES:
        version, template = get_prompt(name)
        assert version == "playtest@1"
        assert isinstance(template, str) and template
