from gameforge.agents.prompts.library import register_all_prompts
from gameforge.agents.prompts.registry import get_prompt, render


def test_all_agent_prompts_registered():
    register_all_prompts()
    for name, ver in [
        ("extraction.system", "extraction@1"),
        ("triage.system", "triage@1"),
        ("repair.system", "repair@4"),
        ("repair.refine", "repair@4"),
        ("consistency.system", "consistency@3"),
        ("consistency.legacy.system", "consistency@1"),
        ("generation.system", "generation@8"),
        ("generation.v7.system", "generation@7"),
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


def test_generation_prompt_declares_the_exact_typed_op_target_contract():
    register_all_prompts()

    version, text = get_prompt("generation.system")

    assert version == "generation@8"
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


def test_generation_prompt_freezes_ir_types_and_material_extraction_shape():
    register_all_prompts()

    version, prompt = get_prompt("generation.system")

    assert version == "generation@8"
    assert "EVENT" in prompt
    assert "QUEST_STEP" in prompt
    assert "LOCATED_IN" in prompt
    assert "Material extraction mode" in prompt
    assert "do not use replace_subgraph" in prompt
    assert "Every QUEST must have" in prompt
    assert "outgoing STARTS_AT" in prompt
    assert "outgoing HAS_STEP" in prompt
    assert "Referenced prerequisite quest titles" in prompt
    assert "Do not create a CURRENCY" in prompt
    assert "REWARD_TABLE to every explicit reward ITEM" in prompt
    assert "availability_phase reward_claim" in prompt
    assert "no additional keys" in prompt
    assert "availability_schema_version is the exact string event-availability@1" in prompt
    assert "gameplay_window" in prompt
