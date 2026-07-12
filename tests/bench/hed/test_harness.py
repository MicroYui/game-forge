from __future__ import annotations

import json

import pytest

from gameforge.agents.base import DEFAULT_SNAPSHOT
from gameforge.bench.hed.contracts import (
    seal_evidence_manifest,
    seal_outcome,
    validate_evidence_manifest,
)
from gameforge.bench.hed.harness import (
    HedCaseInput,
    TrackingRouter,
    build_hed_evidence,
    record_router,
    replay_router,
    run_hed_case,
    run_hed_cases,
    validate_hed_evidence,
)
from gameforge.bench.hed.protocol import HedProtocol
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.contracts.model_router import ModelResponse
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.spine.ir.snapshot import Snapshot


class _SequenceTransport:
    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self.calls = []

    def complete(self, request):  # noqa: ANN001
        self.calls.append(request)
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return ModelResponse(response_normalized=self._responses[index])


def _response_add_missing() -> str:
    return json.dumps(
        [
            {
                "op": "add_entity",
                "op_id": "add-real-step",
                "target": "step:missing",
                "new_value": {"type": "QUEST_STEP"},
            }
        ]
    )


def _response_add_wrong() -> str:
    return json.dumps(
        [
            {
                "op": "add_entity",
                "op_id": "add-wrong-step",
                "target": "step:wrong",
                "new_value": {"type": "QUEST_STEP"},
            }
        ]
    )


def _case(case_id: str = "case-00") -> HedCaseInput:
    quest = Entity(id="quest:q", type=NodeType.QUEST)
    relation = Relation(
        id="relation:step",
        type=EdgeType.HAS_STEP,
        src_id=quest.id,
        dst_id="step:missing",
    )
    before = Snapshot.from_entities_relations([quest], [relation])
    human_target = Snapshot.from_entities_relations(
        [quest, Entity(id="step:missing", type=NodeType.QUEST_STEP)],
        [relation],
        parent_id=before.snapshot_id,
    )
    finding = next(
        item
        for item in GraphChecker().check(before)
        if item.defect_class == "dangling_reference"
    )
    return HedCaseInput(
        case_id=case_id,
        external_case_evidence_sha256=f"{int(case_id[-2:]) + 1:064x}",
        before_snapshot=before,
        human_target_snapshot=human_target,
        target_finding=finding,
    )


def _protocol() -> HedProtocol:
    return HedProtocol.seal(
        external_manifest_sha256="a" * 64,
        external_case_ids=tuple(f"case-{index:02d}" for index in range(8)),
    )


class _ExternalCase:
    def __init__(self, case: HedCaseInput) -> None:
        self.spec = type("Spec", (), {"case_id": case.case_id})()
        self.evidence_sha256 = case.external_case_evidence_sha256
        self.qualification_status = "qualified"


def _external_manifest(cases):  # noqa: ANN001
    return type(
        "ExternalManifest",
        (),
        {
            "manifest_sha256": "a" * 64,
            "cases": tuple(_ExternalCase(case) for case in cases),
        },
    )()


def _router(tmp_path, responses: list[str], *, mode=RouterMode.PASSTHROUGH):
    transport = _SequenceTransport(responses)
    router = ModelRouter(
        transport,
        CassetteStore(tmp_path),
        mode=mode,
        default_model_snapshot=DEFAULT_SNAPSHOT,
    )
    return router, transport


def test_verified_patch_becomes_a_measured_agent_target(tmp_path):
    router, transport = _router(tmp_path, [_response_add_missing()])

    outcome = run_hed_case(_case(), router, _protocol())

    assert outcome.status == "evaluated"
    assert outcome.disposition == "unchanged"
    assert outcome.passed_verification is True
    assert outcome.agent_target_snapshot_id is not None
    assert outcome.request_hashes
    assert outcome.normalized_distance == 0.0
    assert len(transport.calls) == 1
    assert transport.calls[0].model_snapshot == DEFAULT_SNAPSHOT


def test_failed_search_retains_final_patch_and_scores_empty_agent_delta(tmp_path):
    router, _ = _router(tmp_path, [_response_add_wrong()])

    outcome = run_hed_case(_case(), router, _protocol())

    assert outcome.status == "agent_unusable"
    assert outcome.patch is not None
    assert outcome.patch.ops[0].target == "step:wrong"
    assert outcome.passed_verification is False
    assert outcome.agent_delta == ()
    assert outcome.raw_distance == len(outcome.human_delta)
    assert outcome.normalized_distance == 1.0
    assert len(outcome.request_hashes) == 4
    assert outcome.request_hashes[0] != outcome.request_hashes[1]
    assert outcome.request_hashes[1:] == (outcome.request_hashes[1],) * 3


def test_malformed_agent_output_never_becomes_a_target(tmp_path):
    router, _ = _router(tmp_path, ["not-json"])

    outcome = run_hed_case(_case(), router, _protocol())

    assert outcome.status == "agent_unusable"
    assert outcome.agent_target_snapshot_id is None
    assert outcome.agent_delta == ()
    assert outcome.patch is not None and outcome.patch.ops == []


def test_cassette_miss_remains_in_the_case_denominator(tmp_path):
    outcome = run_hed_case(_case(), replay_router(tmp_path), _protocol())

    assert outcome.status == "protocol_failure"
    assert outcome.disposition == "protocol_failure"
    assert outcome.normalized_distance is None
    assert outcome.agent_target_snapshot_id is None
    assert len(outcome.request_hashes) == 1
    assert outcome.failure_reason is not None
    assert "cassette miss" in outcome.failure_reason


def test_tracking_router_preserves_request_hashes_in_call_order(tmp_path):
    router, _ = _router(tmp_path, [_response_add_missing()])
    tracker = TrackingRouter(router)
    case = _case()

    run_hed_case(case, tracker, _protocol())

    assert len(tracker.request_hashes) == 1
    assert tracker.request_hashes[0].startswith("sha256:")
    assert tracker.default_model_snapshot == DEFAULT_SNAPSHOT


def test_record_then_replay_is_identical_without_a_live_transport(tmp_path):
    record, transport = _router(
        tmp_path,
        [_response_add_missing()],
        mode=RouterMode.RECORD,
    )
    recorded = run_hed_case(_case(), record, _protocol())
    assert len(transport.calls) == 1

    replayed = run_hed_case(_case(), replay_router(tmp_path), _protocol())

    assert replayed == recorded


def test_run_cases_sorts_ids_and_never_drops_protocol_failures(tmp_path):
    cases = tuple(_case(f"case-{index:02d}") for index in reversed(range(8)))

    outcomes = run_hed_cases(cases, replay_router(tmp_path), _protocol())

    assert tuple(item.case_id for item in outcomes) == tuple(
        f"case-{index:02d}" for index in range(8)
    )
    assert all(item.status == "protocol_failure" for item in outcomes)


def test_build_and_validate_evidence_rederive_agent_and_human_deltas(
    tmp_path,
    monkeypatch,
):
    cases = tuple(_case(f"case-{index:02d}") for index in range(8))
    protocol = _protocol()
    outcomes = run_hed_cases(cases, replay_router(tmp_path), protocol)

    external = _external_manifest(cases)
    monkeypatch.setattr(
        "gameforge.bench.hed.harness.assert_protocol_ready",
        lambda protocol, manifest: None,
    )

    evidence = build_hed_evidence(cases, outcomes, protocol, external)

    assert evidence.metric.planned_n == 8
    assert evidence.metric.protocol_failure_count == 8
    validate_hed_evidence(evidence, cases, protocol, external)
    validate_evidence_manifest(
        evidence,
        protocol=protocol,
        external_manifest=external,
    )


def test_validation_rejects_an_unusable_label_on_a_passing_patch(tmp_path):
    cases = tuple(_case(f"case-{index:02d}") for index in range(8))
    protocol = _protocol()
    outcomes = list(run_hed_cases(cases, replay_router(tmp_path / "empty"), protocol))
    live_router, _ = _router(tmp_path / "passing", [_response_add_missing()])
    passing = run_hed_case(cases[0], live_router, protocol)
    assert passing.patch is not None
    outcomes[0] = seal_outcome(
        case_id=passing.case_id,
        external_case_evidence_sha256=passing.external_case_evidence_sha256,
        protocol_sha256=passing.protocol_sha256,
        status="agent_unusable",
        before_snapshot_id=passing.before_snapshot_id,
        human_target_snapshot_id=passing.human_target_snapshot_id,
        target_finding=passing.target_finding,
        request_hashes=passing.request_hashes,
        search_steps=passing.search_steps,
        patch=passing.patch,
        passed_verification=False,
        agent_target_snapshot_id=None,
        human_delta=passing.human_delta,
        agent_delta=(),
        failure_reason="tampered unusable label",
    )
    evidence = seal_evidence_manifest(
        protocol_sha256=protocol.protocol_sha256,
        external_manifest_sha256="a" * 64,
        model_snapshot=protocol.model_snapshot,
        outcomes=tuple(outcomes),
    )

    with pytest.raises(ValueError, match="unusable.*passes verification"):
        validate_hed_evidence(
            evidence,
            cases,
            protocol,
            _external_manifest(cases),
        )


def test_record_router_requires_explicit_live_gate_and_key(monkeypatch, tmp_path):
    monkeypatch.delenv("GAMEFORGE_LLM_LIVE", raising=False)
    monkeypatch.delenv("GAMEFORGE_LLM_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GAMEFORGE_LLM_LIVE=1"):
        record_router(tmp_path)

    monkeypatch.setenv("GAMEFORGE_LLM_LIVE", "1")
    with pytest.raises(RuntimeError, match="GAMEFORGE_LLM_KEY"):
        record_router(tmp_path)

    monkeypatch.setenv("GAMEFORGE_LLM_KEY", "test-key")
    router = record_router(tmp_path)
    assert router.default_model_snapshot == DEFAULT_SNAPSHOT
    assert router._resume is True
    assert router._max_retries == 8
    assert router._retry_backoff_s == 3.0
