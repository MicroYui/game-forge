from gameforge.agents.prompts.library import register_all_prompts
from gameforge.agents.prompts.registry import get_prompt, render


def test_all_agent_prompts_registered():
    register_all_prompts()
    for name, ver in [
        ("extraction.system", "extraction@1"),
        ("triage.system", "triage@1"),
        ("repair.system", "repair@4"),
        ("repair.refine", "repair@4"),
        ("consistency.system", "consistency@1"),
        ("generation.system", "generation@1"),
    ]:
        v, tmpl = get_prompt(name)
        assert v == ver
        assert "JSON" in tmpl


def test_each_prompt_declares_propose_only_and_json_only():
    register_all_prompts()
    for name in ("extraction.system", "triage.system", "repair.system",
                 "consistency.system", "generation.system"):
        _, tmpl = get_prompt(name)
        assert "ONLY" in tmpl  # "Output ONLY a JSON ..."


def test_refine_prompt_renders_counterexample_without_brace_crash():
    # render() uses str.format — any unescaped literal brace in ANY template would crash here.
    register_all_prompts()
    v, text = render("repair.refine", counterexample="reward_gold still 120")
    assert v == "repair@4"
    assert "reward_gold still 120" in text
