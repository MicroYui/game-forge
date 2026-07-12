from __future__ import annotations

import json

import pytest

from gameforge.agents.base import DEFAULT_SNAPSHOT
from gameforge.agents.consistency.assistant import ConsistencyAssistant
from gameforge.agents.consistency.checker import ConsistencyChecker
from gameforge.agents.prompts.library import register_all_prompts
from gameforge.agents.prompts.registry import get_prompt
from gameforge.contracts.agent_io import (
    DialogueNarrativeInput,
    NarrativeConstraintInput,
)
from gameforge.contracts.ir import Entity, NodeType
from gameforge.contracts.model_router import ModelResponse
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.spine.checkers.report import build_review_report
from gameforge.spine.ir.snapshot import Snapshot


class _PerVariantTransport:
    def __init__(self, by_prompt_version: dict[str, str]):
        self._by = by_prompt_version
        self.calls = []

    def complete(self, req):
        self.calls.append(req)
        return ModelResponse(response_normalized=self._by.get(req.prompt_version, "[]"))


def _router(by_prompt_version, tmp_path):
    return ModelRouter(
        _PerVariantTransport(by_prompt_version),
        CassetteStore(tmp_path),
        mode=RouterMode.PASSTHROUGH,
    )


def _dialogue_input() -> DialogueNarrativeInput:
    return DialogueNarrativeInput(
        dialogue=(
            "The archive remains sealed. "
            "Qi says the Warden is Mara. "
            "The bells continue through dusk."
        ),
        narrative_constraints=[
            NarrativeConstraintInput(
                constraint_id="C-warden-reveal",
                entity_ids=["npc:qi", "secret:warden"],
                statement="Qi may name the Warden's identity only after the archive opens.",
            ),
            NarrativeConstraintInput(
                constraint_id="C-qi-loyalty",
                entity_ids=["npc:qi"],
                statement="Qi does not betray entrusted allies.",
            ),
        ],
    )


def _hint(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "defect_class": "spoiler",
        "entity_ids": ["npc:qi", "secret:warden"],
        "constraint_ids": ["C-warden-reveal"],
        "span": "Qi says the Warden is Mara.",
        "rationale": "The line names a gated identity before the archive opens.",
    }
    value.update(changes)
    return value


def _variants(**values: str) -> dict[str, str]:
    return {f"consistency@3#{name}": value for name, value in values.items()}


def test_quorum_uses_grounded_identity_not_free_text_rationale(tmp_path):
    first = _hint(rationale="directly names the still-gated identity")
    second = _hint(
        entity_ids=["secret:warden", "npc:qi"],
        span="the Warden is Mara",
        rationale="world state still marks the archive as sealed",
    )
    by_variant = _variants(
        p_constraint_matching=json.dumps([first]),
        p_causal_world_state=json.dumps([second]),
        p_adversarial_falsification="[]",
    )
    transport = _PerVariantTransport(by_variant)
    router = ModelRouter(
        transport,
        CassetteStore(tmp_path),
        mode=RouterMode.PASSTHROUGH,
    )

    result = ConsistencyAssistant().run(_dialogue_input(), router)

    assert result.produced["hints"] == [
        {
            **first,
            "entity_ids": ["npc:qi", "secret:warden"],
            "span": "Qi says the Warden is Mara.",
            "is_suggestion": True,
        }
    ]
    assert result.produced["threshold"] == 2
    assert result.produced["matcher_version"] == "narrative-span@1"
    assert result.produced["rebuttal_enabled"] is True
    assert [item["name"] for item in result.produced["perspectives"]] == [
        "constraint_matching",
        "causal_world_state",
        "adversarial_falsification",
    ]
    assert result.fallback_taken is False
    assert len(result.request_hashes) == 3
    assert all("#p_" in call.prompt_version for call in transport.calls)
    assert all(call.model_snapshot == DEFAULT_SNAPSHOT for call in transport.calls)


def test_wrong_class_entity_and_constraint_do_not_form_quorum(tmp_path):
    by_variant = _variants(
        p_constraint_matching=json.dumps(
            [_hint(defect_class="character_violation")]
        ),
        p_causal_world_state=json.dumps(
            [_hint(entity_ids=["npc:qi"], constraint_ids=["C-qi-loyalty"])]
        ),
        p_adversarial_falsification=json.dumps(
            [_hint(constraint_ids=["C-qi-loyalty"])]
        ),
    )

    result = ConsistencyAssistant().run(
        _dialogue_input(),
        _router(by_variant, tmp_path),
        rebut=False,
    )

    assert result.produced["hints"] == []
    assert result.fallback_taken is False


def test_one_malformed_perspective_does_not_erase_two_valid_votes(tmp_path):
    by_variant = _variants(
        p_constraint_matching=json.dumps([_hint()]),
        p_causal_world_state=json.dumps([_hint(rationale="same grounded issue")]),
        p_adversarial_falsification="not json",
    )

    result = ConsistencyAssistant().run(_dialogue_input(), _router(by_variant, tmp_path))

    assert len(result.produced["hints"]) == 1
    assert [item["parse_ok"] for item in result.produced["perspectives"]] == [
        True,
        True,
        False,
    ]
    assert result.fallback_taken is False


def test_all_malformed_perspectives_take_empty_fallback(tmp_path):
    by_variant = _variants(
        p_constraint_matching="not json",
        p_causal_world_state="still not json",
        p_adversarial_falsification='{"wrong": "top level"}',
    )

    result = ConsistencyAssistant().run(_dialogue_input(), _router(by_variant, tmp_path))

    assert result.produced["hints"] == []
    assert result.fallback_taken is True
    assert len(result.request_hashes) == 3


def test_invalid_items_are_dropped_without_losing_valid_items(tmp_path):
    payload = [
        _hint(),
        {"span": "Qi says the Warden is Mara.", "issue": "legacy shape"},
        _hint(entity_ids=["npc:invented"]),
    ]
    by_variant = _variants(
        p_constraint_matching=json.dumps(payload),
        p_causal_world_state=json.dumps([_hint(rationale="second vote")]),
        p_adversarial_falsification="[]",
    )

    result = ConsistencyAssistant().run(_dialogue_input(), _router(by_variant, tmp_path))

    diagnostic = result.produced["perspectives"][0]
    assert diagnostic["raw_items"] == 3
    assert diagnostic["accepted_items"] == 1
    assert len(result.produced["hints"]) == 1


def test_disputed_hint_can_only_be_confirmed_by_structured_rebuttal(tmp_path):
    by_variant = {
        **_variants(
            p_constraint_matching=json.dumps([_hint()]),
            p_causal_world_state="[]",
            p_adversarial_falsification="[]",
        ),
        **_variants(
            r_constraint_matching="[]",
            r_causal_world_state=json.dumps(
                [_hint(span="the Warden is Mara", rationale="confirmed causally")]
            ),
            r_adversarial_falsification=json.dumps(
                [_hint(rationale="no consistent alternative reading remains")]
            ),
        ),
    }

    result = ConsistencyAssistant().run(_dialogue_input(), _router(by_variant, tmp_path))

    assert len(result.produced["hints"]) == 1
    assert len(result.request_hashes) == 6


def test_rebuttal_cannot_introduce_a_new_identity(tmp_path):
    introduced = _hint(defect_class="character_violation")
    by_variant = {
        **_variants(
            p_constraint_matching=json.dumps([_hint()]),
            p_causal_world_state="[]",
            p_adversarial_falsification="[]",
        ),
        **_variants(
            r_constraint_matching=json.dumps([introduced]),
            r_causal_world_state=json.dumps([introduced]),
            r_adversarial_falsification=json.dumps([introduced]),
        ),
    }

    result = ConsistencyAssistant().run(_dialogue_input(), _router(by_variant, tmp_path))

    assert result.produced["hints"] == []


def test_benchmark_mode_disables_rebuttal_and_honors_threshold(tmp_path):
    by_variant = _variants(
        p_constraint_matching=json.dumps([_hint()]),
        p_causal_world_state=json.dumps([_hint(rationale="second vote")]),
        p_adversarial_falsification="[]",
    )
    transport = _PerVariantTransport(by_variant)
    router = ModelRouter(
        transport,
        CassetteStore(tmp_path),
        mode=RouterMode.PASSTHROUGH,
    )

    result = ConsistencyAssistant().run(
        _dialogue_input(),
        router,
        threshold=3,
        rebut=False,
    )

    assert result.produced["hints"] == []
    assert result.produced["rebuttal_enabled"] is False
    assert len(transport.calls) == 3


@pytest.mark.parametrize(
    ("perspectives", "threshold"),
    [
        ((), 1),
        (("constraint_matching", "constraint_matching"), 1),
        (("constraint_matching",), 0),
        (("constraint_matching",), 2),
    ],
)
def test_invalid_quorum_configuration_fails_before_model_calls(
    tmp_path, perspectives, threshold
):
    transport = _PerVariantTransport({})
    router = ModelRouter(
        transport,
        CassetteStore(tmp_path),
        mode=RouterMode.PASSTHROUGH,
    )

    with pytest.raises(ValueError):
        ConsistencyAssistant().run(
            _dialogue_input(),
            router,
            perspectives=perspectives,
            threshold=threshold,
        )
    assert transport.calls == []


def test_current_method_prompts_all_cover_all_four_classes():
    register_all_prompts()
    for method in (
        "constraint_matching",
        "causal_world_state",
        "adversarial_falsification",
    ):
        version, prompt = get_prompt(f"consistency.perspective.{method}")
        assert version == "consistency@3"
        assert method.replace("_", " ") in prompt.lower()
        for defect_class in (
            "character_violation",
            "spoiler",
            "faction_violation",
            "uniqueness_violation",
        ):
            assert defect_class in prompt


def test_current_prompt_has_operational_class_boundaries_and_clean_controls():
    register_all_prompts()
    _, prompt = get_prompt("consistency.system")
    normalized = " ".join(prompt.lower().split())

    for phrase in (
        "character_violation applies when",
        "spoiler applies only when",
        "at the permitted reveal stage or later is compliant",
        "faction_violation applies only when",
        "a third neutral faction",
        "uniqueness_violation applies when",
        "complete your own exhaustive pass",
        "fulfills a rule",
    ):
        assert phrase in normalized


def test_consistency_checker_writes_grounded_llm_assisted_finding(tmp_path):
    by_variant = _variants(
        p_constraint_matching=json.dumps([_hint()]),
        p_causal_world_state=json.dumps([_hint(rationale="second vote")]),
        p_adversarial_falsification="[]",
    )
    checker = ConsistencyChecker(
        ConsistencyAssistant(),
        _router(by_variant, tmp_path),
        _dialogue_input(),
    )
    snapshot = Snapshot.from_entities_relations(
        [Entity(id="q", type=NodeType.QUEST)],
        [],
    )

    findings = checker.check(snapshot)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.defect_class == "spoiler"
    assert finding.entities == ["npc:qi", "secret:warden"]
    assert finding.constraint_id == "C-warden-reveal"
    assert finding.evidence == {
        "span": "Qi says the Warden is Mara.",
        "rationale": _hint()["rationale"],
        "constraint_ids": ["C-warden-reveal"],
    }
    assert finding.message == _hint()["rationale"]
    assert finding.source == "llm"
    assert finding.oracle_type == "llm-assisted"
    assert finding.status == "unproven"

    report = build_review_report(snapshot, [checker])
    assert report.llm_assisted_findings
    assert report.deterministic_findings == []
