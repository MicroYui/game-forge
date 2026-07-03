from gameforge.contracts.findings import Finding, Patch, TypedOp


def test_finding_schema_version_default_and_fields():
    f = Finding(
        id="F1", source="checker", producer_id="structural", producer_run_id="run1",
        oracle_type="deterministic", defect_class="missing_drop_source",
        severity="critical", snapshot_id="sha256:x", evidence={}, minimal_repro={},
        status="confirmed", message="m",
    )
    assert f.finding_schema_version == "finding@1"
    assert f.entities == [] and f.relations == [] and f.constraint_id is None


def test_patch_optimistic_concurrency_fields():
    op = TypedOp(
        op_id="o1", op="set_relation_attr", target="r1",
        old_value={"probability": 0.1}, new_value={"probability": 0.2},
    )
    p = Patch(
        id="P1", base_snapshot_id="sha256:a", target_snapshot_id="sha256:b",
        expected_to_fix=["F1"], preconditions=[], side_effect_risk="low",
        ops=[op], produced_by="agent", producer_run_id="run1", rationale="r",
    )
    assert p.patch_schema_version == "patch@1"
    assert p.ops[0].old_value["probability"] == 0.1


def test_finding_status_lifecycle_values_accepted():
    for status in ["confirmed", "unproven", "dismissed", "fixed", "accepted_risk"]:
        f = Finding(
            id="F", source="checker", producer_id="p", producer_run_id="r",
            oracle_type="deterministic", defect_class="d", severity="minor",
            snapshot_id="sha256:x", evidence={}, minimal_repro={}, status=status,
            message="m",
        )
        assert f.status == status
