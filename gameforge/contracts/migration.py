"""Artifact-migration result and report wire contracts.

Execution is deliberately deferred to M4e.  These models freeze the bounded,
fail-closed publication contract consumed by M4c jobs and APIs.
"""

from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.execution_profiles import (
    ArtifactLineagePolicyRefV1,
    ProfileRefV1,
    VersionTransitionPolicyRefV1,
)
from gameforge.contracts.lineage import ArtifactKind


MAX_MIGRATION_ITEMS = 1024
MAX_MIGRATION_COUNT = 2_147_483_647

BoundedId = Annotated[str, StringConstraints(min_length=1, max_length=512)]
BoundedText = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NonNegativeCount = Annotated[int, Field(ge=0, le=MAX_MIGRATION_COUNT)]
MigrationVerdict: TypeAlias = Literal[True, False, "unavailable"]
MigrationCheckType: TypeAlias = Literal[
    "source_readable",
    "target_reader_resolved",
    "migration_path_resolved",
    "target_payload_valid",
    "canonical_round_trip",
    "semantic_invariants",
    "golden_replay",
    "publish_binding",
]
MigrationCheckStatus: TypeAlias = Literal["passed", "failed", "unproven", "not_applicable"]

MIGRATION_CHECK_ORDER: tuple[MigrationCheckType, ...] = (
    "source_readable",
    "target_reader_resolved",
    "migration_path_resolved",
    "target_payload_valid",
    "canonical_round_trip",
    "semantic_invariants",
    "golden_replay",
    "publish_binding",
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


def _stable_unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set(values)))


class SourceReadableCheckResultV1(_FrozenModel):
    result_schema_version: Literal["migration-source-readable-result@1"] = (
        "migration-source-readable-result@1"
    )
    source_payload_hash: Sha256Hex
    reader_schema_id: BoundedId | None = None
    canonical_payload_hash: Sha256Hex | None = None
    readable: MigrationVerdict


class TargetReaderResolvedCheckResultV1(_FrozenModel):
    result_schema_version: Literal["migration-target-reader-result@1"] = (
        "migration-target-reader-result@1"
    )
    target_payload_schema_id: BoundedId
    reader_schema_id: BoundedId | None = None
    registry_entry_digest: Sha256Hex | None = None
    resolved: MigrationVerdict


class MigrationPathResolvedCheckResultV1(_FrozenModel):
    result_schema_version: Literal["migration-path-result@1"] = "migration-path-result@1"
    source_payload_schema_id: BoundedId
    target_payload_schema_id: BoundedId
    migration_registry_digest: Sha256Hex
    edge_ids: tuple[BoundedId, ...] = Field(max_length=MAX_MIGRATION_ITEMS)
    path_digest: Sha256Hex | None = None
    resolved: MigrationVerdict

    @field_validator("edge_ids")
    @classmethod
    def _ordered_unique_edges(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for edge_id in value:
            if edge_id in seen:
                raise ValueError("edge_ids must be unique")
            seen[edge_id] = None
        return value


class TargetPayloadValidCheckResultV1(_FrozenModel):
    result_schema_version: Literal["migration-target-valid-result@1"] = (
        "migration-target-valid-result@1"
    )
    target_payload_hash: Sha256Hex | None = None
    target_payload_schema_id: BoundedId
    validator_tool_version: BoundedId
    valid: MigrationVerdict


class CanonicalRoundTripCheckResultV1(_FrozenModel):
    result_schema_version: Literal["migration-round-trip-result@1"] = (
        "migration-round-trip-result@1"
    )
    first_canonical_hash: Sha256Hex | None = None
    round_trip_canonical_hash: Sha256Hex | None = None
    equal: MigrationVerdict


class SemanticInvariantsCheckResultV1(_FrozenModel):
    result_schema_version: Literal["migration-semantic-result@1"] = "migration-semantic-result@1"
    invariant_profile: ProfileRefV1
    invariant_set_digest: Sha256Hex
    evaluated_count: NonNegativeCount
    evaluation_complete: bool
    failed_invariant_ids: tuple[BoundedId, ...] = Field(max_length=MAX_MIGRATION_ITEMS)

    @field_validator("failed_invariant_ids")
    @classmethod
    def _canonical_failed_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _stable_unique(value)

    @model_validator(mode="after")
    def _failure_count(self) -> "SemanticInvariantsCheckResultV1":
        if len(self.failed_invariant_ids) > self.evaluated_count:
            raise ValueError("failed invariant count cannot exceed evaluated_count")
        return self


class GoldenReplayCheckResultV1(_FrozenModel):
    result_schema_version: Literal["migration-golden-replay-result@1"] = (
        "migration-golden-replay-result@1"
    )
    fixture_set_digest: Sha256Hex
    case_count: NonNegativeCount
    replay_complete: bool
    failed_case_ids: tuple[BoundedId, ...] = Field(max_length=MAX_MIGRATION_ITEMS)
    replay_result_digest: Sha256Hex | None = None
    comparison_digest: Sha256Hex | None = None

    @field_validator("failed_case_ids")
    @classmethod
    def _canonical_failed_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _stable_unique(value)

    @model_validator(mode="after")
    def _failure_count(self) -> "GoldenReplayCheckResultV1":
        if len(self.failed_case_ids) > self.case_count:
            raise ValueError("failed case count cannot exceed case_count")
        return self


class PublishBindingCheckResultV1(_FrozenModel):
    result_schema_version: Literal["migration-publish-binding-result@1"] = (
        "migration-publish-binding-result@1"
    )
    source_kind: ArtifactKind
    target_kind: ArtifactKind
    target_payload_schema_id: BoundedId
    lineage_policy_ref: ArtifactLineagePolicyRefV1 | None = None
    version_transition_policy_ref: VersionTransitionPolicyRefV1 | None = None
    binding_valid: MigrationVerdict


MigrationCheckResultV1: TypeAlias = Annotated[
    SourceReadableCheckResultV1
    | TargetReaderResolvedCheckResultV1
    | MigrationPathResolvedCheckResultV1
    | TargetPayloadValidCheckResultV1
    | CanonicalRoundTripCheckResultV1
    | SemanticInvariantsCheckResultV1
    | GoldenReplayCheckResultV1
    | PublishBindingCheckResultV1,
    Field(discriminator="result_schema_version"),
]


_RESULT_TYPE_BY_CHECK: dict[MigrationCheckType, type[_FrozenModel]] = {
    "source_readable": SourceReadableCheckResultV1,
    "target_reader_resolved": TargetReaderResolvedCheckResultV1,
    "migration_path_resolved": MigrationPathResolvedCheckResultV1,
    "target_payload_valid": TargetPayloadValidCheckResultV1,
    "canonical_round_trip": CanonicalRoundTripCheckResultV1,
    "semantic_invariants": SemanticInvariantsCheckResultV1,
    "golden_replay": GoldenReplayCheckResultV1,
    "publish_binding": PublishBindingCheckResultV1,
}


def _result_status(result: MigrationCheckResultV1) -> MigrationCheckStatus:
    if isinstance(result, SemanticInvariantsCheckResultV1):
        if not result.evaluation_complete:
            return "unproven"
        return "failed" if result.failed_invariant_ids else "passed"
    if isinstance(result, GoldenReplayCheckResultV1):
        if not result.replay_complete:
            return "unproven"
        return "failed" if result.failed_case_ids else "passed"

    verdict = (
        result.readable
        if isinstance(result, SourceReadableCheckResultV1)
        else result.resolved
        if isinstance(
            result,
            (TargetReaderResolvedCheckResultV1, MigrationPathResolvedCheckResultV1),
        )
        else result.valid
        if isinstance(result, TargetPayloadValidCheckResultV1)
        else result.equal
        if isinstance(result, CanonicalRoundTripCheckResultV1)
        else result.binding_valid
    )
    if verdict == "unavailable":
        return "unproven"
    return "passed" if verdict is True else "failed"


class MigrationCheckV1(_FrozenModel):
    check_schema_version: Literal["migration-check@1"] = "migration-check@1"
    check_id: MigrationCheckType
    check_type: MigrationCheckType
    status: MigrationCheckStatus
    reason_code: BoundedText | None = None
    result: MigrationCheckResultV1 | None = None

    @model_validator(mode="after")
    def _closed_check(self) -> "MigrationCheckV1":
        if self.check_id != self.check_type:
            raise ValueError("check_id must equal check_type")
        if self.status == "passed":
            if self.reason_code is not None:
                raise ValueError("passed check cannot have a reason_code")
        elif self.reason_code is None:
            raise ValueError(f"{self.status} check requires a reason_code")

        if self.status == "not_applicable":
            if self.result is not None:
                raise ValueError("not_applicable check cannot have a result")
            return self
        if self.result is None:
            raise ValueError("executed check requires a typed result")

        expected_type = _RESULT_TYPE_BY_CHECK[self.check_type]
        if not isinstance(self.result, expected_type):
            raise ValueError("result discriminator does not match check_type")
        if _result_status(self.result) != self.status:
            raise ValueError("status does not match the typed result verdict")

        if self.status == "passed":
            if isinstance(self.result, SourceReadableCheckResultV1) and (
                self.result.reader_schema_id is None or self.result.canonical_payload_hash is None
            ):
                raise ValueError("passed source-readable check requires reader and canonical hash")
            if isinstance(self.result, TargetReaderResolvedCheckResultV1) and (
                self.result.reader_schema_id is None or self.result.registry_entry_digest is None
            ):
                raise ValueError("passed target-reader check requires reader and registry digest")
            if isinstance(self.result, MigrationPathResolvedCheckResultV1) and (
                not self.result.edge_ids or self.result.path_digest is None
            ):
                raise ValueError("passed migration path requires edges and path digest")
            if (
                isinstance(self.result, TargetPayloadValidCheckResultV1)
                and self.result.target_payload_hash is None
            ):
                raise ValueError("passed target validation requires target payload hash")
            if isinstance(self.result, GoldenReplayCheckResultV1) and (
                self.result.replay_result_digest is None or self.result.comparison_digest is None
            ):
                raise ValueError("passed golden replay requires replay and comparison digests")
            if isinstance(self.result, PublishBindingCheckResultV1) and (
                self.result.lineage_policy_ref is None
                or self.result.version_transition_policy_ref is None
                or self.result.source_kind != self.result.target_kind
            ):
                raise ValueError(
                    "passed publish binding requires same-kind exact lineage and version policies"
                )
        return self


MigrationReportStatus: TypeAlias = Literal[
    "compatible",
    "migration_available",
    "migrated",
    "needs_re_extract",
    "needs_re_compile",
]


class MigrationReportV1(_FrozenModel):
    report_schema_version: Literal["migration-report@1"] = "migration-report@1"
    source_artifact_id: BoundedId
    source_kind: ArtifactKind
    source_payload_schema_id: BoundedId
    target_payload_schema_id: BoundedId
    target_meta_schema_version: BoundedId
    target_dsl_grammar_version: BoundedId | None = None
    migrator: ProfileRefV1
    requested_publish_mode: Literal["report_only", "publish_migrated_artifact"]
    status: MigrationReportStatus
    migrated_artifact_id: BoundedId | None = None
    reason_code: BoundedText | None = None
    checks: tuple[MigrationCheckV1, ...] = Field(
        min_length=len(MIGRATION_CHECK_ORDER),
        max_length=len(MIGRATION_CHECK_ORDER),
    )

    @model_validator(mode="after")
    def _closed_report(self) -> "MigrationReportV1":
        actual_order = tuple(check.check_type for check in self.checks)
        if actual_order != MIGRATION_CHECK_ORDER:
            raise ValueError("checks must contain the fixed eight check types in order")

        migrated = self.status == "migrated"
        if migrated != (self.migrated_artifact_id is not None):
            raise ValueError("only migrated reports require migrated_artifact_id")
        reason_required = self.status in {
            "migration_available",
            "needs_re_extract",
            "needs_re_compile",
        }
        if reason_required != (self.reason_code is not None):
            raise ValueError("report reason_code presence does not match status")
        if self.status == "migrated" and self.requested_publish_mode != (
            "publish_migrated_artifact"
        ):
            raise ValueError("migrated status requires publish_migrated_artifact mode")
        if self.status == "migration_available" and self.requested_publish_mode != "report_only":
            raise ValueError("migration_available status requires report_only mode")

        statuses = {check.check_type: check.status for check in self.checks}
        if self.status == "compatible":
            required_passed = {
                "source_readable",
                "target_reader_resolved",
                "target_payload_valid",
                "canonical_round_trip",
                "semantic_invariants",
            }
            expected = {
                check_type: ("passed" if check_type in required_passed else "not_applicable")
                for check_type in MIGRATION_CHECK_ORDER
            }
            if statuses != expected:
                raise ValueError("compatible report has an invalid check outcome matrix")
        elif self.status == "migration_available":
            required_passed = {
                "source_readable",
                "target_reader_resolved",
                "migration_path_resolved",
            }
            expected = {
                check_type: ("passed" if check_type in required_passed else "not_applicable")
                for check_type in MIGRATION_CHECK_ORDER
            }
            if statuses != expected:
                raise ValueError("migration_available report has an invalid check outcome matrix")
        elif self.status == "migrated":
            for check_type in MIGRATION_CHECK_ORDER:
                allowed = (
                    {"passed", "not_applicable"} if check_type == "golden_replay" else {"passed"}
                )
                if statuses[check_type] not in allowed:
                    raise ValueError("migrated report has an invalid check outcome matrix")
        else:
            if statuses["source_readable"] != "passed" or statuses[
                "migration_path_resolved"
            ] not in {"failed", "unproven"}:
                raise ValueError(f"{self.status} requires a failed or unproven migration path")
            for check_type in MIGRATION_CHECK_ORDER:
                if check_type not in {"source_readable", "migration_path_resolved"} and (
                    statuses[check_type] != "not_applicable"
                ):
                    raise ValueError(f"{self.status} has an executed non-path check")
            path_check = self.checks[MIGRATION_CHECK_ORDER.index("migration_path_resolved")]
            if path_check.reason_code != self.reason_code:
                raise ValueError("path and report reason_code must identify the same outcome")
        return self


__all__ = [
    "MAX_MIGRATION_COUNT",
    "MAX_MIGRATION_ITEMS",
    "MIGRATION_CHECK_ORDER",
    "CanonicalRoundTripCheckResultV1",
    "GoldenReplayCheckResultV1",
    "MigrationCheckResultV1",
    "MigrationCheckStatus",
    "MigrationCheckType",
    "MigrationCheckV1",
    "MigrationPathResolvedCheckResultV1",
    "MigrationReportStatus",
    "MigrationReportV1",
    "PublishBindingCheckResultV1",
    "SemanticInvariantsCheckResultV1",
    "SourceReadableCheckResultV1",
    "TargetPayloadValidCheckResultV1",
    "TargetReaderResolvedCheckResultV1",
]
