from gameforge.agents.prompts.library import register_all_prompts
from gameforge.agents.prompts.registry import get_prompt, render
from gameforge.contracts.canonical import sha256_lowerhex


_GENERATION_V1_SHA256 = "1536235b60735dee75fe656953c5e7f7446313b3b5c5d9b1349cf558927f2479"


def test_all_agent_prompts_registered():
    register_all_prompts()
    for name, ver in [
        ("extraction.system", "extraction@1"),
        ("triage.system", "triage@1"),
        ("repair.system", "repair@4"),
        ("repair.refine", "repair@4"),
        ("consistency.system", "consistency@3"),
        ("consistency.legacy.system", "consistency@1"),
        ("generation.system", "generation@1"),
        ("generation.v2.system", "generation@2"),
    ]:
        v, tmpl = get_prompt(name)
        assert v == ver
        assert "JSON" in tmpl


def test_each_prompt_declares_propose_only_and_json_only():
    register_all_prompts()
    for name in (
        "extraction.system",
        "triage.system",
        "repair.system",
        "consistency.system",
        "generation.system",
    ):
        _, tmpl = get_prompt(name)
        assert "ONLY" in tmpl  # "Output ONLY a JSON ..."


def test_refine_prompt_renders_counterexample_without_brace_crash():
    # render() uses str.format — any unescaped literal brace in ANY template would crash here.
    register_all_prompts()
    v, text = render("repair.refine", counterexample="reward_gold still 120")
    assert v == "repair@4"
    assert "reward_gold still 120" in text


def test_generation_v1_prompt_bytes_remain_frozen_for_replay():
    register_all_prompts()

    version, text = get_prompt("generation.system")

    assert version == "generation@1"
    assert sha256_lowerhex(text.encode("utf-8")) == _GENERATION_V1_SHA256


def test_generation_v2_prompt_declares_the_exact_typed_op_target_contract():
    register_all_prompts()

    version, text = get_prompt("generation.v2.system")

    assert version == "generation@2"
    for op in (
        "add_entity",
        "delete_entity",
        "set_entity_attr",
        "add_relation",
        "delete_relation",
        "set_relation_attr",
        "replace_subgraph",
    ):
        assert op in text
    assert "quest:missing_caravan.reward.gold" in text
    assert "Do NOT include the literal segment attrs" in text
    assert "Do NOT use JSON Patch op names replace, add, or remove" in text
