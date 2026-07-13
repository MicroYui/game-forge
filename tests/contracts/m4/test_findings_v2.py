from __future__ import annotations

import pytest
from pydantic import ValidationError

from gameforge.contracts.findings import (
    Finding,
    FindingPayloadV1,
    FindingRevisionV1,
    Patch,
    PatchV2,
    TypedOp,
    finding_revision_digest,
    parse_finding,
    parse_patch,
)


def _legacy_finding() -> Finding:
    return Finding(
        id="F1",
        source="checker",
        producer_id="graph",
        producer_run_id="run-legacy",
        oracle_type="deterministic",
        defect_class="missing_reference",
        severity="major",
        snapshot_id="sha256:legacy",
        entities=["quest:1"],
        relations=[],
        evidence={"path": ["quest:1"]},
        minimal_repro={"entity_id": "quest:1"},
        status="confirmed",
        message="missing reference",
        created_at="2026-07-01T00:00:00Z",
    )


def _payload(*, message: str = "missing reference") -> FindingPayloadV1:
    return FindingPayloadV1(
        source="checker",
        producer_id="graph",
        producer_run_id="run-1",
        oracle_type="deterministic",
        defect_class="missing_reference",
        severity="major",
        snapshot_id="sha256:snapshot",
        entities=["quest:1"],
        relations=[],
        evidence={"path": ["quest:1"]},
        minimal_repro={"entity_id": "quest:1"},
        status="confirmed",
        message=message,
    )


def _op() -> TypedOp:
    return TypedOp(
        op_id="op-1",
        op="set_entity_attr",
        target="quest:1.reward_gold",
        old_value=100,
        new_value=80,
    )


def test_legacy_finding_and_patch_wire_shapes_remain_unchanged() -> None:
    finding = _legacy_finding()
    patch = Patch(
        id="P1",
        base_snapshot_id="sha256:base",
        target_snapshot_id="sha256:target",
        expected_to_fix=["F1"],
        preconditions=[],
        side_effect_risk="low",
        ops=[_op()],
        produced_by="agent",
        producer_run_id="run-legacy",
        rationale="legacy",
        validation_status="passed",
        regression_status="passed",
        approval_status="approved",
        created_at="2026-07-01T00:00:00Z",
    )

    assert finding.finding_schema_version == "finding@1"
    assert patch.patch_schema_version == "patch@1"
    assert "revision_schema_version" not in finding.model_dump()
    assert "revision" not in patch.model_dump()
    assert patch.model_dump()["approval_status"] == "approved"
    assert parse_finding(finding.model_dump()) == finding
    assert parse_patch(patch.model_dump()) == patch


def test_finding_revision_digest_excludes_created_at_but_binds_semantics() -> None:
    first = FindingRevisionV1(
        finding_id="finding:1",
        revision=1,
        payload=_payload(),
        created_at="2026-07-13T00:00:00Z",
    )
    replay = first.model_copy(update={"created_at": "2026-07-13T01:00:00Z"})
    changed = first.model_copy(update={"payload": _payload(message="changed")})

    assert finding_revision_digest(first) == finding_revision_digest(replay)
    assert finding_revision_digest(first) != finding_revision_digest(changed)
    assert len(finding_revision_digest(first)) == 64
    assert finding_revision_digest(first) == finding_revision_digest(first.model_dump())


def test_finding_revision_is_strict_frozen_and_uses_its_own_discriminator() -> None:
    revision = FindingRevisionV1(
        finding_id="finding:1",
        revision=2,
        supersedes_revision=1,
        payload=_payload(),
        created_at="2026-07-13T00:00:00Z",
    )

    assert revision.revision_schema_version == "finding-revision@1"
    assert revision.payload.payload_schema_version == "finding-payload@1"
    assert parse_finding(revision.model_dump()) == revision
    with pytest.raises(ValidationError):
        revision.revision = 3
    with pytest.raises(ValidationError):
        FindingRevisionV1.model_validate({**revision.model_dump(), "unknown": True})
    with pytest.raises(ValidationError):
        FindingRevisionV1.model_validate({**revision.model_dump(), "revision": "2"})
    with pytest.raises(ValidationError):
        parse_finding({**revision.model_dump(), "revision_schema_version": "finding@1"})


def test_patch_v2_is_an_immutable_revision_without_workflow_status_fields() -> None:
    patch = PatchV2(
        revision=2,
        supersedes_artifact_id="artifact:previous",
        base_snapshot_id="sha256:base",
        target_snapshot_id="sha256:target",
        expected_to_fix=["finding:1"],
        preconditions=[],
        side_effect_risk="low",
        ops=[_op()],
        produced_by="agent",
        producer_run_id="run-1",
        rationale="reduce reward",
    )

    wire = patch.model_dump()
    assert patch.patch_schema_version == "patch@2"
    assert parse_patch(wire) == patch
    assert "id" not in wire
    assert "created_at" not in wire
    assert "validation_status" not in wire
    assert "regression_status" not in wire
    assert "approval_status" not in wire
    with pytest.raises(ValidationError):
        patch.revision = 3
    with pytest.raises(ValidationError):
        PatchV2.model_validate({**wire, "approval_status": "approved"})


@pytest.mark.parametrize(
    ("produced_by", "producer_run_id"),
    [("agent", None), ("human", "run-1")],
)
def test_patch_v2_producer_binding_is_unambiguous(
    produced_by: str, producer_run_id: str | None
) -> None:
    with pytest.raises(ValidationError):
        PatchV2(
            revision=1,
            base_snapshot_id="sha256:base",
            target_snapshot_id="sha256:target",
            side_effect_risk="low",
            ops=[_op()],
            produced_by=produced_by,
            producer_run_id=producer_run_id,
            rationale="test",
        )


def test_patch_parser_never_treats_malformed_v2_as_legacy() -> None:
    with pytest.raises(ValidationError):
        parse_patch(
            {
                "patch_schema_version": "patch@2",
                "revision": 1,
                "base_snapshot_id": "sha256:base",
                "target_snapshot_id": "sha256:target",
                "side_effect_risk": "low",
                "ops": [_op().model_dump()],
                "produced_by": "agent",
                "producer_run_id": None,
                "rationale": "invalid agent binding",
            }
        )
