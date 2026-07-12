from __future__ import annotations

from collections.abc import Sequence

import pytest
from pydantic import ValidationError

from gameforge.bench.hed.contracts import (
    AtomicDeltaModel,
    HedCaseOutcome,
    HedEvidenceManifest,
    canonical_evidence_bytes,
    content_sha256,
    derive_hed_metric,
    load_evidence,
    seal_evidence_manifest,
    seal_outcome,
)
from gameforge.bench.hed.delta import AtomicDelta
from gameforge.contracts.findings import Finding, Patch, TypedOp
from gameforge.contracts.model_router import ModelSnapshot

_PROTOCOL_SHA = "a" * 64
_EXTERNAL_MANIFEST_SHA = "b" * 64
_SNAPSHOT = ModelSnapshot(
    provider="openai",
    model="gpt-5.6-sol",
    snapshot_tag="pre-m4@1",
)


def _finding(case_id: str = "case-00") -> Finding:
    return Finding(
        id=f"finding:{case_id}",
        source="checker",
        producer_id="graph",
        producer_run_id="hed-test",
        oracle_type="deterministic",
        defect_class="dangling_reference",
        severity="major",
        snapshot_id="sha256:" + "1" * 64,
        entities=["quest:alpha"],
        relations=["relation:missing"],
        evidence={"relation_id": "relation:missing"},
        minimal_repro={"entities": ["quest:alpha"]},
        status="confirmed",
        message="A quest references a missing target.",
    )


def _patch(case_id: str = "case-00") -> Patch:
    return Patch(
        id="sha256:" + "2" * 64,
        base_snapshot_id="sha256:" + "1" * 64,
        target_snapshot_id="",
        expected_to_fix=[f"finding:{case_id}"],
        side_effect_risk="low",
        ops=[
            TypedOp(
                op_id="remove-missing-edge",
                op="delete_relation",
                target="relation:missing",
            )
        ],
        produced_by="agent",
        producer_run_id="sha256:" + "3" * 64,
        rationale="Remove the invalid reference.",
    )


def _delete_relation(target: str = "relation:missing") -> AtomicDelta:
    return AtomicDelta(
        kind="delete_relation",
        target=target,
        field=None,
        old_json='{"dst":"missing","src":"quest:alpha","type":"requires"}',
        new_json=None,
    )


def _add_relation(target: str = "relation:replacement") -> AtomicDelta:
    return AtomicDelta(
        kind="add_relation",
        target=target,
        field=None,
        old_json=None,
        new_json='{"dst":"gate:real","src":"quest:alpha","type":"requires"}',
    )


def _evaluated(case_id: str, *, edited: bool = False) -> HedCaseOutcome:
    human_delta = (_delete_relation(),)
    agent_delta = (_add_relation(),) if edited else human_delta
    return seal_outcome(
        case_id=case_id,
        external_case_evidence_sha256=content_sha256({"case_id": case_id}),
        protocol_sha256=_PROTOCOL_SHA,
        status="evaluated",
        before_snapshot_id="sha256:" + "1" * 64,
        human_target_snapshot_id="sha256:" + "4" * 64,
        target_finding=_finding(case_id),
        request_hashes=("sha256:" + "5" * 64,),
        search_steps=1,
        patch=_patch(case_id),
        passed_verification=True,
        agent_target_snapshot_id="sha256:" + "6" * 64,
        human_delta=human_delta,
        agent_delta=agent_delta,
    )


def _unusable(case_id: str) -> HedCaseOutcome:
    return seal_outcome(
        case_id=case_id,
        external_case_evidence_sha256=content_sha256({"case_id": case_id}),
        protocol_sha256=_PROTOCOL_SHA,
        status="agent_unusable",
        before_snapshot_id="sha256:" + "1" * 64,
        human_target_snapshot_id="sha256:" + "4" * 64,
        target_finding=_finding(case_id),
        request_hashes=("sha256:" + "7" * 64,),
        search_steps=1,
        patch=_patch(case_id),
        passed_verification=False,
        agent_target_snapshot_id=None,
        human_delta=(_delete_relation(),),
        agent_delta=(),
        failure_reason="bounded repair search exhausted",
    )


def _protocol_failure(case_id: str) -> HedCaseOutcome:
    return seal_outcome(
        case_id=case_id,
        external_case_evidence_sha256=content_sha256({"case_id": case_id}),
        protocol_sha256=_PROTOCOL_SHA,
        status="protocol_failure",
        before_snapshot_id="sha256:" + "1" * 64,
        human_target_snapshot_id="sha256:" + "4" * 64,
        target_finding=_finding(case_id),
        request_hashes=("sha256:" + "8" * 64,),
        search_steps=1,
        patch=None,
        passed_verification=False,
        agent_target_snapshot_id=None,
        human_delta=(_delete_relation(),),
        agent_delta=(),
        failure_reason="cassette miss",
    )


def _eight_outcomes() -> tuple[HedCaseOutcome, ...]:
    rows = [
        _evaluated("case-00"),
        _evaluated("case-01", edited=True),
        _unusable("case-02"),
        _evaluated("case-03"),
        _evaluated("case-04"),
        _evaluated("case-05"),
        _evaluated("case-06"),
        _protocol_failure("case-07"),
    ]
    return tuple(rows)


def _evidence(outcomes: Sequence[HedCaseOutcome] | None = None) -> HedEvidenceManifest:
    return seal_evidence_manifest(
        protocol_sha256=_PROTOCOL_SHA,
        external_manifest_sha256=_EXTERNAL_MANIFEST_SHA,
        model_snapshot=_SNAPSHOT,
        outcomes=tuple(outcomes or _eight_outcomes()),
    )


def test_content_hash_exclusion_applies_to_raw_payloads_and_models_equally():
    outcome = _evaluated("case-00")
    payload = outcome.model_dump(mode="json")

    assert content_sha256(payload, exclude={"outcome_sha256"}) == content_sha256(
        outcome,
        exclude={"outcome_sha256"},
    )


def test_evaluated_outcome_seals_full_patch_delta_distance_and_hash():
    outcome = _evaluated("case-00")

    assert outcome.disposition == "unchanged"
    assert outcome.patch_sha256 == content_sha256(outcome.patch)
    assert outcome.raw_distance == 0
    assert outcome.normalized_distance == 0.0
    assert outcome.outcome_sha256 == content_sha256(
        outcome,
        exclude={"outcome_sha256"},
    )
    assert outcome.human_delta == (
        AtomicDeltaModel.from_delta(_delete_relation()),
    )


def test_unusable_agent_is_measured_as_empty_delta_not_dropped():
    outcome = _unusable("case-00")

    assert outcome.status == "agent_unusable"
    assert outcome.disposition == "unusable"
    assert outcome.patch is not None
    assert outcome.agent_target_snapshot_id is None
    assert outcome.agent_delta == ()
    assert outcome.raw_distance == 1
    assert outcome.normalized_distance == 1.0


def test_protocol_failure_cannot_carry_a_fake_distance_or_agent_target():
    payload = _protocol_failure("case-00").model_dump(mode="json")
    payload.update(
        normalized_distance=0.0,
        raw_distance=0,
        agent_target_snapshot_id="sha256:" + "9" * 64,
    )
    payload["outcome_sha256"] = content_sha256(
        payload,
        exclude={"outcome_sha256"},
    )

    with pytest.raises(ValidationError, match="protocol_failure"):
        HedCaseOutcome.model_validate(payload)


def test_outcome_rejects_unsorted_or_duplicate_semantic_deltas():
    values = _evaluated("case-00").model_dump(mode="python")
    values.pop("outcome_sha256")
    values["human_delta"] = (
        AtomicDeltaModel.from_delta(_delete_relation("relation:z")),
        AtomicDeltaModel.from_delta(_delete_relation("relation:a")),
    )
    values["agent_delta"] = values["human_delta"]

    with pytest.raises((ValidationError, ValueError), match="sorted"):
        seal_outcome(**values)

    values["human_delta"] = (
        AtomicDeltaModel.from_delta(_delete_relation()),
        AtomicDeltaModel.from_delta(_delete_relation()),
    )
    values["agent_delta"] = values["human_delta"]
    with pytest.raises((ValidationError, ValueError), match="duplicate|unique"):
        seal_outcome(**values)


def test_outcome_rejects_unverified_usable_patch_and_patch_hash_drift():
    payload = _evaluated("case-00").model_dump(mode="json")
    payload["passed_verification"] = False
    payload["outcome_sha256"] = content_sha256(
        payload,
        exclude={"outcome_sha256"},
    )
    with pytest.raises(ValidationError, match="passed verification"):
        HedCaseOutcome.model_validate(payload)

    payload = _evaluated("case-00").model_dump(mode="json")
    payload["patch_sha256"] = "f" * 64
    payload["outcome_sha256"] = content_sha256(
        payload,
        exclude={"outcome_sha256"},
    )
    with pytest.raises(ValidationError, match="patch_sha256"):
        HedCaseOutcome.model_validate(payload)


def test_outcome_contract_forbids_extra_fields():
    payload = _evaluated("case-00").model_dump(mode="json")
    payload["reviewer_approval"] = True

    with pytest.raises(ValidationError, match="Extra inputs"):
        HedCaseOutcome.model_validate(payload)


def test_metric_keeps_unusable_measured_and_protocol_failures_in_denominator():
    metric = derive_hed_metric(_eight_outcomes())

    assert metric.planned_n == 8
    assert metric.evaluated_n == 7
    assert metric.unchanged_count == 5
    assert metric.edited_count == 1
    assert metric.unusable_count == 1
    assert metric.protocol_failure_count == 1
    assert metric.primary_estimate == metric.mean_normalized_distance
    assert metric.ci_method == "percentile-bootstrap95"
    assert metric.ci_low is not None and metric.ci_high is not None


def test_manifest_requires_exact_sorted_denominator_and_rederived_metric():
    outcomes = _eight_outcomes()
    manifest = _evidence(outcomes)
    assert manifest.metric == derive_hed_metric(outcomes)

    with pytest.raises((ValidationError, ValueError), match="eight outcomes"):
        seal_evidence_manifest(
            protocol_sha256=_PROTOCOL_SHA,
            external_manifest_sha256=_EXTERNAL_MANIFEST_SHA,
            model_snapshot=_SNAPSHOT,
            outcomes=outcomes[:-1],
        )

    with pytest.raises(ValidationError, match="sorted"):
        seal_evidence_manifest(
            protocol_sha256=_PROTOCOL_SHA,
            external_manifest_sha256=_EXTERNAL_MANIFEST_SHA,
            model_snapshot=_SNAPSHOT,
            outcomes=tuple(reversed(outcomes)),
        )

    payload = manifest.model_dump(mode="json")
    payload["metric"]["unchanged_count"] = 0
    payload["evidence_sha256"] = content_sha256(
        payload,
        exclude={"evidence_sha256"},
    )
    with pytest.raises(ValidationError, match="metric"):
        HedEvidenceManifest.model_validate(payload)


def test_evidence_round_trips_as_canonical_hash_bound_json(tmp_path):
    manifest = _evidence()
    path = tmp_path / "hed-evidence.json"
    path.write_bytes(canonical_evidence_bytes(manifest))

    assert load_evidence(path) == manifest
    assert b'"mean_normalized_distance":"f:' in path.read_bytes()
