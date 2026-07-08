"""TDD: playtest planner/executor/reflect prompts registered with prompt_version."""
from __future__ import annotations

from gameforge.agents.playtest.prompts import register_playtest_prompts
from gameforge.agents.prompts.registry import get_prompt

_NAMES = ("playtest.planner", "playtest.executor", "playtest.reflect")

# Executor was bumped to @2 (fight protocol + drive-the-active-step guidance);
# planner/reflect are untouched at @1.
_EXPECTED_VERSIONS = {
    "playtest.planner": "playtest@1",
    "playtest.executor": "playtest@2",
    "playtest.reflect": "playtest@1",
}


def test_playtest_prompts_registered_with_version_and_json():
    register_playtest_prompts()
    for name in _NAMES:
        version, template = get_prompt(name)
        assert version == _EXPECTED_VERSIONS[name]
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
        assert version == _EXPECTED_VERSIONS[name]
        assert isinstance(template, str) and template


def test_executor_prompt_teaches_fight_protocol():
    register_playtest_prompts()
    _, template = get_prompt("playtest.executor")
    assert "FIGHT PROTOCOL" in template
    assert "navigate_to" in template and "arrived" in template
    assert "not_in_combat" in template
    assert "pending_fight_targets" in template
    assert "advance" in template

