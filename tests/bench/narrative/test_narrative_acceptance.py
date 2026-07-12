from __future__ import annotations

from pathlib import Path

from gameforge.agents.consistency.assistant import ConsistencyAssistant
from gameforge.agents.harness import historical_replay_router
from gameforge.bench.narrative.contracts import to_agent_input
from gameforge.bench.narrative.corpus import load_cases
from gameforge.bench.narrative.harness import load_evidence
from gameforge.contracts.agent_io import DialogueNarrativeInput
from gameforge.contracts.cassette import CassetteRecord
from gameforge.contracts.dsl import Constraint

_ROOT = Path("scenarios/narrative_bench")
_CASSETTE_ROOT = Path("cassettes/narrative/pre-m4-1")
_HIDDEN_ANSWER_FIELDS = {
    "benchmark_family",
    "case_id",
    "case_sha256",
    "defect_class",
    "facts",
    "is_clean",
    "seed",
    "split",
    "target_constraint_ids",
    "target_entities",
    "target_span",
}
_EXPECTED_M2_REQUEST_HASHES = [
    "sha256:2505227517ccf16feee1e234803c82d68bcb08760ee5343884a6179d4a19f98c",
    "sha256:f8104d12520d9ca612f9830bdfd7ed4fd9b88bca15e014221e4a94db4a06a10e",
    "sha256:7a6c972043a623a804660d3dec00ccf36dca28365d64565dea8e80ec89ab8068",
    "sha256:4487a0f2bb67077d63807a29a469a1435ba46cb9185f84b9c8e4d62030fae641",
    "sha256:6ad2d900d55ba8b79f2fc3b8e073140abaa589b03b3c8f6e746c1e6e8d06f9ed",
    "sha256:c3f7299b8e33851fc5c5d745d1207be046c45214b23620d888961d266069b63f",
]


def _all_mapping_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        keys = set(value)
        for item in value.values():
            keys.update(_all_mapping_keys(item))
        return keys
    if isinstance(value, list):
        keys: set[str] = set()
        for item in value:
            keys.update(_all_mapping_keys(item))
        return keys
    return set()


def test_verification_model_inputs_exclude_every_hidden_answer_field():
    cases = load_cases(_ROOT / "verification.jsonl")

    for case in cases:
        model_payload = to_agent_input(case).model_dump(mode="json")
        assert _all_mapping_keys(model_payload).isdisjoint(_HIDDEN_ANSWER_FIELDS)
        assert set(model_payload) == {
            "dialogue",
            "narrative_constraints",
            "narrative_constraint_ids",
        }


def test_every_recorded_verification_request_is_a_gpt56_consistency_cassette():
    evidence = load_evidence(_ROOT / "verification-evidence.json")
    referenced_hashes = {
        request_hash
        for outcome in evidence.outcomes
        for request_hash in outcome.request_hashes
    }
    records = {
        record.request_hash: record
        for path in _CASSETTE_ROOT.glob("*.json")
        if (
            record := CassetteRecord.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        )
    }

    assert referenced_hashes
    assert referenced_hashes <= set(records)
    for request_hash in referenced_hashes:
        record = records[request_hash]
        assert record.agent_node_id == "consistency"
        assert record.model_snapshot.model_dump() == {
            "provider": "openai",
            "model": "gpt-5.6-sol",
            "snapshot_tag": "pre-m4@1",
        }


def test_historical_m2_opus_cassettes_still_replay_with_their_original_hashes():
    dialogue = Path("scenarios/agents/dialogue.txt").read_text(encoding="utf-8")
    constraints = Constraint.from_yaml(
        Path("scenarios/agents/narrative.yaml").read_text(encoding="utf-8")
    )
    result = ConsistencyAssistant().run_legacy_m2(
        DialogueNarrativeInput(
            dialogue=dialogue,
            narrative_constraint_ids=[item.id for item in constraints],
        ),
        historical_replay_router(),
    )

    assert result.request_hashes == _EXPECTED_M2_REQUEST_HASHES
    assert result.fallback_taken is False
