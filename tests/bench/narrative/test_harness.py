from __future__ import annotations

import json

import pytest

from gameforge.agents.base import DEFAULT_SNAPSHOT
from gameforge.bench.narrative.corpus import load_cases, load_manifest
from gameforge.bench.narrative.generator import generate_case
from gameforge.bench.narrative.harness import (
    build_evidence,
    record_router,
    replay_router,
    run_cases,
)
from gameforge.bench.narrative.protocol import seal_protocol
from gameforge.bench.narrative.score import score_case
from gameforge.bench.taxonomy import DefectClass
from gameforge.contracts.agent_io import AgentNodeResult, ConsistencyHint
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.runtime.model_router.openai_responses_transport import (
    OpenAIResponsesTransport,
)

_CORPUS_ROOT = "scenarios/narrative_bench"


def _protocol():
    return seal_protocol(load_manifest(f"{_CORPUS_ROOT}/corpus-manifest.json"))


def _case(case_id: str = "harness-positive", *, clean: bool = False):
    return generate_case(
        split="development",
        defect_class=DefectClass.character_violation,
        is_clean=clean,
        seed=51 if not clean else 52,
        case_id=case_id,
    )


def _correct_hint(case):
    assert case.target_span is not None and case.defect_class is not None
    return ConsistencyHint(
        defect_class=case.defect_class.value,
        entity_ids=list(case.target_entities),
        constraint_ids=list(case.target_constraint_ids),
        span=case.target_span.text,
        rationale="The event conflicts with the supplied rule.",
    )


def test_record_router_is_live_gated_and_uses_only_gpt56_responses(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("GAMEFORGE_LLM_LIVE", raising=False)
    monkeypatch.setenv("GAMEFORGE_LLM_KEY", "test-key")
    with pytest.raises(RuntimeError, match="GAMEFORGE_LLM_LIVE=1"):
        record_router(tmp_path)

    monkeypatch.setenv("GAMEFORGE_LLM_LIVE", "1")
    router = record_router(tmp_path)

    assert router.default_model_snapshot == ModelSnapshot(
        provider="openai",
        model="gpt-5.6-sol",
        snapshot_tag="pre-m4@1",
    )
    assert isinstance(router._transport, OpenAIResponsesTransport)
    assert router._resume is True
    assert router._max_retries == 8
    assert router._retry_backoff_s == 3.0


def test_record_router_requires_the_gateway_key(monkeypatch, tmp_path):
    monkeypatch.setenv("GAMEFORGE_LLM_LIVE", "1")
    monkeypatch.delenv("GAMEFORGE_LLM_KEY", raising=False)

    with pytest.raises(RuntimeError, match="GAMEFORGE_LLM_KEY"):
        record_router(tmp_path)


def test_replay_transport_is_incapable_of_network_calls(tmp_path):
    router = replay_router(tmp_path)

    assert router.default_model_snapshot == DEFAULT_SNAPSHOT
    with pytest.raises(RuntimeError, match="REPLAY"):
        router._transport.complete(None)


def test_replay_miss_becomes_an_outcome_instead_of_aborting_denominator(tmp_path):
    case = _case()
    outcomes = run_cases([case], replay_router(tmp_path), _protocol())

    assert len(outcomes) == 1
    assert outcomes[0].case_id == case.case_id
    assert outcomes[0].status == "cassette_miss"
    assert outcomes[0].detected is False
    assert outcomes[0].request_hashes


class _SpyAssistant:
    def __init__(self, responses=None, error: BaseException | None = None):
        self.calls = []
        self._responses = list(responses or [])
        self._error = error

    def run(self, input_value, router, **kwargs):
        self.calls.append((input_value, router, kwargs))
        if self._error is not None:
            raise self._error
        return self._responses.pop(0)


def _result(case, *, malformed: bool = False) -> AgentNodeResult:
    hint = None if case.is_clean else _correct_hint(case)
    perspectives = [
        {
            "name": name,
            "request_hash": f"sha256:{index:064x}",
            "parse_ok": not (malformed and index == 3),
            "raw_items": (2 if index == 1 else 1) if hint is not None else 0,
            "accepted_items": 1 if hint is not None else 0,
        }
        for index, name in enumerate(
            (
                "constraint_matching",
                "causal_world_state",
                "adversarial_falsification",
            ),
            start=1,
        )
    ]
    hints = [hint.model_dump()] if hint is not None else []
    if malformed:
        hints.append({"span": "legacy", "issue": "invalid"})
    return AgentNodeResult(
        role="consistency",
        model_run_id="sha256:" + "1" * 64,
        request_hashes=[f"sha256:{index:064x}" for index in range(1, 4)],
        produced={
            "hints": hints,
            "perspectives": perspectives,
            "samples": 3,
            "threshold": 2,
            "matcher_version": "narrative-span@1",
            "rebuttal_enabled": False,
        },
    )


def test_run_cases_passes_the_frozen_three_method_configuration_and_sorts_cases(tmp_path):
    later = _case("z-case")
    earlier = _case("a-case")
    assistant = _SpyAssistant([_result(earlier), _result(later)])
    router = replay_router(tmp_path)

    outcomes = run_cases(
        [later, earlier],
        router,
        _protocol(),
        assistant=assistant,
    )

    assert [item.case_id for item in outcomes] == ["a-case", "z-case"]
    assert [call[0].dialogue for call in assistant.calls] == [
        earlier.dialogue,
        later.dialogue,
    ]
    assert all(
        call[2]
        == {
            "perspectives": _protocol().perspectives,
            "threshold": 2,
            "rebut": False,
            "model_snapshot": DEFAULT_SNAPSHOT,
        }
        for call in assistant.calls
    )
    assert all(item.detected for item in outcomes)


def test_run_cases_revalidates_hints_and_preserves_partial_parse_diagnostics(tmp_path):
    case = _case()
    assistant = _SpyAssistant([_result(case, malformed=True)])

    outcome = run_cases(
        [case],
        replay_router(tmp_path),
        _protocol(),
        assistant=assistant,
    )[0]

    assert outcome.status == "partial_parse_failure"
    assert outcome.parse_failures == 1
    assert outcome.invalid_hint_items == 2
    assert len(outcome.hints) == 1
    assert outcome.detected is True


def test_runner_error_is_retained_but_keyboard_interrupt_is_not_swallowed(tmp_path):
    case = _case()
    failed = _SpyAssistant(error=RuntimeError("forced failure"))
    outcome = run_cases(
        [case],
        replay_router(tmp_path),
        _protocol(),
        assistant=failed,
    )[0]

    assert outcome.status == "runner_error"
    assert "forced failure" in outcome.failure_reason

    interrupted = _SpyAssistant(error=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        run_cases(
            [case],
            replay_router(tmp_path),
            _protocol(),
            assistant=interrupted,
        )


def test_build_evidence_binds_protocol_corpus_snapshot_and_complete_denominator():
    cases = load_cases(f"{_CORPUS_ROOT}/development.jsonl")
    protocol = _protocol()
    outcomes = tuple(
        score_case(
            case,
            [],
            protocol_sha256=protocol.protocol_sha256,
        )
        for case in cases
    )
    manifest = load_manifest(f"{_CORPUS_ROOT}/corpus-manifest.json")

    evidence = build_evidence(cases, outcomes, protocol, manifest)

    assert evidence.protocol_sha256 == protocol.protocol_sha256
    assert evidence.corpus_manifest_sha256 == manifest.manifest_sha256
    assert evidence.model_snapshot == DEFAULT_SNAPSHOT
    assert len(evidence.outcomes) == 160
    assert {item.n for item in evidence.by_class} == {20}
    assert evidence.clean_fp.n == 80


def test_no_harness_source_references_anthropic_transport():
    from gameforge.bench.narrative import harness

    source = open(harness.__file__, encoding="utf-8").read()
    assert "AnthropicMessagesTransport" not in source
    assert "claude-opus" not in source
    assert json.loads(json.dumps(DEFAULT_SNAPSHOT.model_dump()))["model"] == "gpt-5.6-sol"
