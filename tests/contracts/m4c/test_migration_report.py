from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError
import pytest

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.execution_profiles import (
    ArtifactLineagePolicyRefV1,
    ProfileRefV1,
)
from gameforge.contracts.jobs import VersionTransitionPolicyRefV1
from gameforge.contracts.migration import (
    MAX_MIGRATION_ITEMS,
    CanonicalRoundTripCheckResultV1,
    GoldenReplayCheckResultV1,
    MigrationCheckV1,
    MigrationPathResolvedCheckResultV1,
    MigrationReportV1,
    PublishBindingCheckResultV1,
    SemanticInvariantsCheckResultV1,
    SourceReadableCheckResultV1,
    TargetPayloadValidCheckResultV1,
    TargetReaderResolvedCheckResultV1,
)


_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64
_ORDER = (
    "source_readable",
    "target_reader_resolved",
    "migration_path_resolved",
    "target_payload_valid",
    "canonical_round_trip",
    "semantic_invariants",
    "golden_replay",
    "publish_binding",
)


def _passed_results() -> dict[str, object]:
    return {
        "source_readable": SourceReadableCheckResultV1(
            source_payload_hash=_HASH_A,
            reader_schema_id="reader:ir@1",
            canonical_payload_hash=_HASH_B,
            readable=True,
        ),
        "target_reader_resolved": TargetReaderResolvedCheckResultV1(
            target_payload_schema_id="ir@2",
            reader_schema_id="reader:ir@2",
            registry_entry_digest=_HASH_A,
            resolved=True,
        ),
        "migration_path_resolved": MigrationPathResolvedCheckResultV1(
            source_payload_schema_id="ir@1",
            target_payload_schema_id="ir@2",
            migration_registry_digest=_HASH_A,
            edge_ids=("edge:b", "edge:a"),
            path_digest=_HASH_B,
            resolved=True,
        ),
        "target_payload_valid": TargetPayloadValidCheckResultV1(
            target_payload_hash=_HASH_B,
            target_payload_schema_id="ir@2",
            validator_tool_version="validator@3",
            valid=True,
        ),
        "canonical_round_trip": CanonicalRoundTripCheckResultV1(
            first_canonical_hash=_HASH_B,
            round_trip_canonical_hash=_HASH_B,
            equal=True,
        ),
        "semantic_invariants": SemanticInvariantsCheckResultV1(
            invariant_profile=ProfileRefV1(profile_id="invariants:ir", version=2),
            invariant_set_digest=_HASH_A,
            evaluated_count=7,
            evaluation_complete=True,
            failed_invariant_ids=(),
        ),
        "golden_replay": GoldenReplayCheckResultV1(
            fixture_set_digest=_HASH_A,
            case_count=4,
            replay_complete=True,
            failed_case_ids=(),
            replay_result_digest=_HASH_B,
            comparison_digest=_HASH_C,
        ),
        "publish_binding": PublishBindingCheckResultV1(
            source_kind="ir_snapshot",
            target_kind="ir_snapshot",
            target_payload_schema_id="ir@2",
            lineage_policy_ref=ArtifactLineagePolicyRefV1(
                policy_id="lineage:migration",
                policy_version=1,
                digest=_HASH_A,
            ),
            version_transition_policy_ref=VersionTransitionPolicyRefV1(
                policy_id="version:migration",
                policy_version=1,
                digest=_HASH_B,
            ),
            binding_valid=True,
        ),
    }


def _check(
    check_type: str,
    *,
    status: str = "passed",
    result: object | None = None,
    reason_code: str | None = None,
) -> MigrationCheckV1:
    if result is None and status != "not_applicable":
        result = _passed_results()[check_type]
    if reason_code is None and status != "passed":
        reason_code = f"migration.{check_type}.{status}@1"
    return MigrationCheckV1.model_validate(
        {
            "check_id": check_type,
            "check_type": check_type,
            "status": status,
            "reason_code": reason_code,
            "result": result,
        }
    )


def _checks(statuses: Mapping[str, str]) -> tuple[MigrationCheckV1, ...]:
    return tuple(_check(check_type, status=statuses[check_type]) for check_type in _ORDER)


def _report(status: str) -> MigrationReportV1:
    if status == "compatible":
        statuses = {
            check_type: (
                "passed"
                if check_type
                in {
                    "source_readable",
                    "target_reader_resolved",
                    "target_payload_valid",
                    "canonical_round_trip",
                    "semantic_invariants",
                }
                else "not_applicable"
            )
            for check_type in _ORDER
        }
    elif status == "migration_available":
        statuses = {
            check_type: (
                "passed"
                if check_type
                in {
                    "source_readable",
                    "target_reader_resolved",
                    "migration_path_resolved",
                }
                else "not_applicable"
            )
            for check_type in _ORDER
        }
    elif status == "migrated":
        statuses = {check_type: "passed" for check_type in _ORDER}
    else:
        statuses = {
            check_type: ("passed" if check_type == "source_readable" else "not_applicable")
            for check_type in _ORDER
        }

    checks = list(_checks(statuses))
    if status in {"needs_re_extract", "needs_re_compile"}:
        reason = f"migration.path.{status}@1"
        checks[2] = _check(
            "migration_path_resolved",
            status="failed",
            reason_code=reason,
            result=MigrationPathResolvedCheckResultV1(
                source_payload_schema_id="ir@1",
                target_payload_schema_id="ir@2",
                migration_registry_digest=_HASH_A,
                edge_ids=(),
                resolved=False,
            ),
        )
    else:
        reason = "migration.available@1" if status == "migration_available" else None

    return MigrationReportV1(
        source_artifact_id="artifact:source",
        source_kind="ir_snapshot",
        source_payload_schema_id="ir@1",
        target_payload_schema_id="ir@2",
        target_meta_schema_version="meta@2",
        target_dsl_grammar_version="dsl@2",
        migrator=ProfileRefV1(profile_id="migrator:ir", version=2),
        requested_publish_mode=(
            "publish_migrated_artifact" if status == "migrated" else "report_only"
        ),
        status=status,
        migrated_artifact_id="artifact:migrated" if status == "migrated" else None,
        reason_code=reason,
        checks=tuple(checks),
    )


@pytest.mark.parametrize(
    "status",
    (
        "compatible",
        "migration_available",
        "migrated",
        "needs_re_extract",
        "needs_re_compile",
    ),
)
def test_all_report_outcomes_have_exact_order_and_round_trip(status: str) -> None:
    report = _report(status)

    assert tuple(check.check_type for check in report.checks) == _ORDER
    assert MigrationReportV1.model_validate_json(report.model_dump_json()) == report
    with pytest.raises(ValidationError):
        report.status = "compatible"


def test_check_enforces_id_result_discriminator_reason_and_presence() -> None:
    source = _passed_results()["source_readable"]
    target = _passed_results()["target_reader_resolved"]

    invalid = (
        {"check_id": "target_reader_resolved"},
        {"result": target},
        {"result": None},
        {"reason_code": "unexpected@1"},
    )
    valid = _check("source_readable")
    for change in invalid:
        with pytest.raises(ValidationError):
            MigrationCheckV1.model_validate({**valid.model_dump(mode="python"), **change})

    for status in ("failed", "unproven"):
        with pytest.raises(ValidationError, match="reason"):
            MigrationCheckV1(
                check_id="source_readable",
                check_type="source_readable",
                status=status,
                result=source,
            )
    with pytest.raises(ValidationError):
        MigrationCheckV1(
            check_id="source_readable",
            check_type="source_readable",
            status="not_applicable",
            reason_code="migration.not_applicable@1",
            result=source,
        )


@pytest.mark.parametrize(
    ("result", "status"),
    [
        (SourceReadableCheckResultV1(source_payload_hash=_HASH_A, readable=False), "passed"),
        (
            TargetReaderResolvedCheckResultV1(
                target_payload_schema_id="ir@2", resolved="unavailable"
            ),
            "failed",
        ),
        (
            MigrationPathResolvedCheckResultV1(
                source_payload_schema_id="ir@1",
                target_payload_schema_id="ir@2",
                migration_registry_digest=_HASH_A,
                edge_ids=(),
                resolved=True,
            ),
            "passed",
        ),
        (
            TargetPayloadValidCheckResultV1(
                target_payload_schema_id="ir@2",
                validator_tool_version="validator@1",
                valid=True,
            ),
            "passed",
        ),
        (
            CanonicalRoundTripCheckResultV1(equal="unavailable"),
            "failed",
        ),
        (
            PublishBindingCheckResultV1(
                source_kind="ir_snapshot",
                target_kind="ir_snapshot",
                target_payload_schema_id="ir@2",
                binding_valid=True,
            ),
            "passed",
        ),
    ],
)
def test_boolean_verdict_and_passed_evidence_presence_are_closed(
    result: object, status: str
) -> None:
    discriminator = result.result_schema_version  # type: ignore[attr-defined]
    check_type = {
        "migration-source-readable-result@1": "source_readable",
        "migration-target-reader-result@1": "target_reader_resolved",
        "migration-path-result@1": "migration_path_resolved",
        "migration-target-valid-result@1": "target_payload_valid",
        "migration-round-trip-result@1": "canonical_round_trip",
        "migration-publish-binding-result@1": "publish_binding",
    }[discriminator]
    with pytest.raises(ValidationError):
        _check(check_type, status=status, result=result)


def test_semantic_and_golden_statuses_are_derived_from_complete_evidence() -> None:
    semantic_failed = SemanticInvariantsCheckResultV1(
        invariant_profile=ProfileRefV1(profile_id="invariants:ir", version=1),
        invariant_set_digest=_HASH_A,
        evaluated_count=3,
        evaluation_complete=True,
        failed_invariant_ids=("inv:b", "inv:a"),
    )
    golden_unproven = GoldenReplayCheckResultV1(
        fixture_set_digest=_HASH_A,
        case_count=3,
        replay_complete=False,
        failed_case_ids=(),
    )

    semantic = _check("semantic_invariants", status="failed", result=semantic_failed)
    golden = _check("golden_replay", status="unproven", result=golden_unproven)
    assert semantic.result.failed_invariant_ids == ("inv:a", "inv:b")
    assert golden.status == "unproven"

    with pytest.raises(ValidationError):
        _check("semantic_invariants", status="passed", result=semantic_failed)
    with pytest.raises(ValidationError):
        _check("golden_replay", status="passed", result=golden_unproven)
    with pytest.raises(ValidationError):
        _check(
            "golden_replay",
            status="passed",
            result=GoldenReplayCheckResultV1(
                fixture_set_digest=_HASH_A,
                case_count=3,
                replay_complete=True,
                failed_case_ids=(),
            ),
        )


def test_migration_path_preserves_order_and_rejects_duplicate_edges() -> None:
    path = _passed_results()["migration_path_resolved"]
    reverse_path = MigrationPathResolvedCheckResultV1(
        source_payload_schema_id="ir@1",
        target_payload_schema_id="ir@2",
        migration_registry_digest=_HASH_A,
        edge_ids=tuple(reversed(path.edge_ids)),
        path_digest=_HASH_B,
        resolved=True,
    )

    assert path.edge_ids == ("edge:b", "edge:a")
    assert reverse_path.edge_ids == ("edge:a", "edge:b")
    assert path.model_dump(mode="json") != reverse_path.model_dump(mode="json")
    assert canonical_sha256(path.model_dump(mode="json")) != canonical_sha256(
        reverse_path.model_dump(mode="json")
    )

    with pytest.raises(ValidationError, match="unique"):
        MigrationPathResolvedCheckResultV1(
            source_payload_schema_id="ir@1",
            target_payload_schema_id="ir@2",
            migration_registry_digest=_HASH_A,
            edge_ids=("edge:a", "edge:b", "edge:a"),
            resolved=False,
        )


def test_migration_path_edges_are_hard_bounded() -> None:
    with pytest.raises(ValidationError):
        MigrationPathResolvedCheckResultV1(
            source_payload_schema_id="ir@1",
            target_payload_schema_id="ir@2",
            migration_registry_digest=_HASH_A,
            edge_ids=tuple(f"edge:{index:04d}" for index in range(MAX_MIGRATION_ITEMS + 1)),
            resolved=False,
        )


def test_report_rejects_non_exact_check_set_order_or_outcome_matrix() -> None:
    report = _report("compatible")
    payload = report.model_dump(mode="python")

    for checks in (report.checks[:-1], tuple(reversed(report.checks))):
        with pytest.raises(ValidationError):
            MigrationReportV1.model_validate({**payload, "checks": checks})

    changed = list(report.checks)
    changed[2] = _check("migration_path_resolved")
    with pytest.raises(ValidationError):
        MigrationReportV1.model_validate({**payload, "checks": changed})


def test_report_rejects_outcome_field_and_publish_mode_inconsistency() -> None:
    migrated = _report("migrated")
    available = _report("migration_available")

    for report, change in (
        (migrated, {"migrated_artifact_id": None}),
        (migrated, {"reason_code": "unexpected@1"}),
        (migrated, {"requested_publish_mode": "report_only"}),
        (available, {"reason_code": None}),
        (available, {"requested_publish_mode": "publish_migrated_artifact"}),
        (available, {"migrated_artifact_id": "artifact:unexpected"}),
    ):
        with pytest.raises(ValidationError):
            MigrationReportV1.model_validate({**report.model_dump(mode="python"), **change})


def test_jobs_compatibility_exports_are_single_class_objects() -> None:
    from gameforge.contracts.jobs import MigrationReportV1 as JobsMigrationReportV1

    assert JobsMigrationReportV1 is MigrationReportV1
