"""End-to-end perspective quorum regression over four structured identities."""

from __future__ import annotations

import json

from gameforge.agents.consistency.assistant import ConsistencyAssistant
from gameforge.contracts.agent_io import (
    DialogueNarrativeInput,
    NarrativeConstraintInput,
)
from gameforge.contracts.model_router import ModelResponse
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode


def _hint(label: str, defect_class: str, constraint_id: str) -> dict[str, object]:
    return {
        "defect_class": defect_class,
        "entity_ids": ["npc:qi"],
        "constraint_ids": [constraint_id],
        "span": f"{label} sentence.",
        "rationale": f"{label} rationale",
    }


_A = _hint("A", "spoiler", "C-a")
_B = _hint("B", "character_violation", "C-b")
_C = _hint("C", "faction_violation", "C-c")
_D = _hint("D", "uniqueness_violation", "C-d")

_RESPONSES = {
    "consistency@2#p_constraint_matching": json.dumps([_A, _B, _C]),
    "consistency@2#p_causal_world_state": json.dumps([_A, _B, _D]),
    "consistency@2#p_adversarial_falsification": json.dumps([_A]),
    "consistency@2#r_constraint_matching": json.dumps([_C]),
    "consistency@2#r_causal_world_state": json.dumps([_C, _D]),
    "consistency@2#r_adversarial_falsification": json.dumps([_C]),
}


class _StubTransport:
    def __init__(self) -> None:
        self.calls = []

    def complete(self, req):
        self.calls.append(req)
        return ModelResponse(response_normalized=_RESPONSES.get(req.prompt_version, "[]"))


def _router(path) -> ModelRouter:
    return ModelRouter(
        _StubTransport(),
        CassetteStore(path),
        mode=RouterMode.PASSTHROUGH,
    )


def _input() -> DialogueNarrativeInput:
    return DialogueNarrativeInput(
        dialogue="A sentence. B sentence. C sentence. D sentence.",
        narrative_constraints=[
            NarrativeConstraintInput(
                constraint_id=f"C-{label.lower()}",
                entity_ids=["npc:qi"],
                statement=f"Constraint {label}.",
            )
            for label in "ABCD"
        ],
    )


def test_perspective_quorum_and_rebuttal_keep_exactly_a_b_c(tmp_path):
    result = ConsistencyAssistant().run(_input(), _router(tmp_path))
    assert [hint["span"] for hint in result.produced["hints"]] == [
        "A sentence.",
        "B sentence.",
        "C sentence.",
    ]
    assert len(result.request_hashes) == 6
    assert result.fallback_taken is False


def test_perspective_quorum_is_reproducible_across_process_equivalent_runs(tmp_path):
    first = ConsistencyAssistant().run(_input(), _router(tmp_path / "first"))
    second = ConsistencyAssistant().run(_input(), _router(tmp_path / "second"))
    assert first == second
