from __future__ import annotations

from pathlib import Path

from gameforge.agents.consistency.assistant import ConsistencyAssistant
from gameforge.agents.harness import historical_replay_router
from gameforge.contracts.agent_io import DialogueNarrativeInput
from gameforge.contracts.dsl import Constraint


_EXPECTED_REQUEST_HASHES = [
    "sha256:2505227517ccf16feee1e234803c82d68bcb08760ee5343884a6179d4a19f98c",
    "sha256:f8104d12520d9ca612f9830bdfd7ed4fd9b88bca15e014221e4a94db4a06a10e",
    "sha256:7a6c972043a623a804660d3dec00ccf36dca28365d64565dea8e80ec89ab8068",
    "sha256:4487a0f2bb67077d63807a29a469a1435ba46cb9185f84b9c8e4d62030fae641",
    "sha256:6ad2d900d55ba8b79f2fc3b8e073140abaa589b03b3c8f6e746c1e6e8d06f9ed",
    "sha256:c3f7299b8e33851fc5c5d745d1207be046c45214b23620d888961d266069b63f",
]


def _historical_input() -> DialogueNarrativeInput:
    dialogue = Path("scenarios/agents/dialogue.txt").read_text(encoding="utf-8")
    constraints = Constraint.from_yaml(
        Path("scenarios/agents/narrative.yaml").read_text(encoding="utf-8")
    )
    return DialogueNarrativeInput(
        dialogue=dialogue,
        narrative_constraint_ids=[item.id for item in constraints],
    )


def test_m2_opus_consistency_cassettes_replay_without_rewrite():
    result = ConsistencyAssistant().run_legacy_m2(
        _historical_input(),
        historical_replay_router(),
    )

    assert result.agent_io_schema_version == "agent-io@1"
    assert result.request_hashes == _EXPECTED_REQUEST_HASHES
    assert result.fallback_taken is False
    assert result.produced["samples"] == 3
    assert len(result.produced["hints"]) == 4
    assert set(result.produced["hints"][0]) == {"span", "issue", "is_suggestion"}
