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


def _semantic_revision() -> FindingRevisionV1:
    return FindingRevisionV1(
        finding_id="finding:semantic",
        revision=3,
        supersedes_revision=2,
        payload=_payload(),
        created_at="2026-07-13T00:00:00Z",
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


def test_finding_revision_digest_has_a_domain_separated_golden_value() -> None:
    assert finding_revision_digest(_semantic_revision()) == (
        "2cf3a627dd0ee048ab75e8b46a3a19d59d9e074adb59da2307fd915f6c7eb52c"
    )


@pytest.mark.parametrize(
    ("field_name", "changed_value"),
    [
        ("finding_id", "finding:other"),
        ("revision", 4),
        ("supersedes_revision", 1),
        ("source", "sim"),
        ("producer_id", "simulator"),
        ("producer_run_id", "run:other"),
        ("oracle_type", "simulation"),
        ("defect_class", "economy_collapse"),
        ("severity", "minor"),
        ("snapshot_id", "sha256:other"),
        ("entities", ["quest:2"]),
        ("relations", ["relation:1"]),
        ("constraint_id", "constraint:1"),
        ("evidence", {"path": ["quest:2"]}),
        ("minimal_repro", {"entity_id": "quest:2"}),
        ("status", "dismissed"),
        ("confidence", 0.5),
        ("message", "changed"),
    ],
)
def test_finding_revision_digest_binds_every_variable_semantic_field(
    field_name: str,
    changed_value: object,
) -> None:
    base = _semantic_revision()
    if field_name in {"finding_id", "revision", "supersedes_revision"}:
        changed = base.model_copy(update={field_name: changed_value})
    else:
        changed_payload = base.payload.model_copy(update={field_name: changed_value})
        changed = base.model_copy(update={"payload": changed_payload})

    assert finding_revision_digest(changed) != finding_revision_digest(base)


@pytest.mark.parametrize(
    ("left_evidence", "right_evidence"),
    [
        ({}, {"x": None}),
        ({"x": 1.0}, {"x": "f:1"}),
        ({"x": True}, {"x": 1}),
        ({"x": -0.0}, {"x": 0.0}),
    ],
)
def test_finding_revision_digest_preserves_typed_json_semantics(
    left_evidence: dict[str, object],
    right_evidence: dict[str, object],
) -> None:
    base = _semantic_revision()
    left = base.model_copy(
        update={"payload": base.payload.model_copy(update={"evidence": left_evidence})}
    )
    right = base.model_copy(
        update={"payload": base.payload.model_copy(update={"evidence": right_evidence})}
    )

    assert finding_revision_digest(left) != finding_revision_digest(right)


def test_finding_revision_digest_ignores_map_insertion_order() -> None:
    base = _semantic_revision()
    first = base.model_copy(
        update={
            "payload": base.payload.model_copy(
                update={"evidence": {"alpha": 1, "beta": [None, False, 2.5]}}
            )
        }
    )
    reordered = base.model_copy(
        update={
            "payload": base.payload.model_copy(
                update={"evidence": {"beta": [None, False, 2.5], "alpha": 1}}
            )
        }
    )

    assert finding_revision_digest(first) == finding_revision_digest(reordered)


@pytest.mark.parametrize("nonfinite", [float("inf"), float("-inf"), float("nan")])
@pytest.mark.parametrize("location", ["evidence", "confidence"])
def test_finding_revision_digest_rejects_nonfinite_values_instead_of_null(
    nonfinite: float,
    location: str,
) -> None:
    base = _semantic_revision()
    if location == "evidence":
        invalid_payload = base.payload.model_copy(update={"evidence": {"value": nonfinite}})
        null_payload = base.payload.model_copy(update={"evidence": {"value": None}})
    else:
        invalid_payload = base.payload.model_copy(update={"confidence": nonfinite})
        null_payload = base.payload.model_copy(update={"confidence": None})
    invalid = base.model_copy(update={"payload": invalid_payload})
    null_value = base.model_copy(update={"payload": null_payload})

    assert len(finding_revision_digest(null_value)) == 64
    with pytest.raises(ValueError, match="finite floats"):
        finding_revision_digest(invalid)


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
    ("revision", "supersedes_artifact_id", "produced_by", "producer_run_id"),
    [
        (1, "artifact:previous", "agent", "run-1"),
        (2, None, "agent", "run-1"),
        (1, None, "agent", None),
        (1, None, "human", "run-1"),
    ],
)
def test_patch_v2_revision_and_producer_binding_are_unambiguous(
    revision: int,
    supersedes_artifact_id: str | None,
    produced_by: str,
    producer_run_id: str | None,
) -> None:
    with pytest.raises(ValidationError):
        PatchV2(
            revision=revision,
            supersedes_artifact_id=supersedes_artifact_id,
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
