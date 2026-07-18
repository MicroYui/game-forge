"""Exact payload-schema verification for terminal Artifact publication.

``PreparedArtifact.payload_schema_id`` is worker-supplied routing metadata.  It is
not proof that the referenced blob actually implements that wire contract.  This
module provides the versioned, closed registry used to turn an already decoded
JSON object into a canonical, schema-checked mapping before any identity or
lineage pointer is evaluated.

Some entries in :data:`ARTIFACT_PAYLOAD_SCHEMAS` deliberately are not ordinary
terminal-domain JSON payloads.  Raw/rendered sources and cassette bundles have
dedicated byte-level/runtime publication paths; regression/golden inputs remain
adapter-owned; the remaining deferred M4e/operations formats do not yet have an
authoritative wire contract.  They are retained as explicit fail-closed registry
entries rather than being accepted as arbitrary mappings.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from types import MappingProxyType
from typing import Annotated, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from gameforge.contracts.benchmark import BenchmarkDatasetV1, BenchmarkSpecV1
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.config_export import (
    ConfigExportPackageV1,
    canonical_config_export_bytes,
    decode_config_export_bytes,
)
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.findings import Finding, PatchV2
from gameforge.contracts.jobs import (
    AgentPromptContextV1,
    MAX_AGENT_PROMPT_CONTEXT_BYTES,
    MAX_PREPARED_ARTIFACT_BYTES,
    RunFailureV1,
    RunResultV1,
)
from gameforge.contracts.migration import MigrationReportV1
from gameforge.contracts.playtest import PlaytestTraceV1, ScenarioSpecV1, TaskSuiteV1
from gameforge.contracts.regression import RegressionCaseSeedManifestV1
from gameforge.contracts.versions import DSL_GRAMMAR_VERSION, META_SCHEMA_VERSION
from gameforge.contracts.workflow import (
    AutoApplyEvidenceContextV1,
    AutoApplyOracleAttestationV1,
    AutoApplyOutcomeAttestationV1,
    AutoApplyProofV1,
    ConstraintCompileEvidenceV1,
    ConstraintProposalV1,
    EvidenceSet,
    RollbackRequestV1,
)
from gameforge.platform.diff.ir_rebase import snapshot_from_canonical_view
from gameforge.platform.registry.defaults import ARTIFACT_PAYLOAD_SCHEMAS
from gameforge.platform.run_handlers.validation_common import deterministic_finding_status


MAX_PAYLOAD_JSON_BYTES = 96 * 1024 * 1024
MAX_PAYLOAD_JSON_DEPTH = 64
MAX_PAYLOAD_COLLECTION_ITEMS = 16_384
MAX_PAYLOAD_STRING_LENGTH = 24 * 1024 * 1024


def _payload_json_byte_limit(payload_schema_id: str) -> int:
    if payload_schema_id == "agent-prompt-context@1":
        return MAX_AGENT_PROMPT_CONTEXT_BYTES
    return MAX_PAYLOAD_JSON_BYTES


def _prepared_blob_byte_limit(payload_schema_id: str) -> int:
    if payload_schema_id == "agent-prompt-context@1":
        return MAX_AGENT_PROMPT_CONTEXT_BYTES
    return MAX_PREPARED_ARTIFACT_BYTES


BoundedText = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
PayloadParser = Callable[[Mapping[str, object]], dict[str, object]]
PayloadBlobDecoder = Callable[[bytes], Mapping[str, object]]


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object key is forbidden: {key}")
        value[key] = item
    return value


class _StrictWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class _CheckerFindingsPayload(_StrictWireModel):
    payload_schema_version: Literal["checker-report@1"]
    snapshot_id: BoundedText
    findings: tuple[Finding, ...] = Field(max_length=MAX_PAYLOAD_COLLECTION_ITEMS)

    @field_validator("findings", mode="before")
    @classmethod
    def _canonical_finding_numbers(cls, value: object) -> object:
        return _decode_finding_confidences(value)


class _CheckerProfilePayload(_CheckerFindingsPayload):
    profile: ProfileRefV1


class _CheckerStandalonePayload(_CheckerFindingsPayload):
    checker_ids: tuple[BoundedText, ...] = Field(max_length=MAX_PAYLOAD_COLLECTION_ITEMS)
    defect_classes: tuple[BoundedText, ...] = Field(max_length=MAX_PAYLOAD_COLLECTION_ITEMS)

    @field_validator("checker_ids", "defect_classes")
    @classmethod
    def _unique_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("checker report selectors must be unique")
        return value


class _CheckerConstraintExecutedApplication(_StrictWireModel):
    constraint_id: BoundedText
    checker_id: Literal["graph", "asp", "smt"]
    status: Literal["executed"]


class _CheckerConstraintUnprovenApplication(_StrictWireModel):
    constraint_id: BoundedText
    checker_id: Literal["graph", "asp", "smt"]
    status: Literal["unproven"]
    reason_code: Literal["navigation_ground_truth_unavailable"]


_CheckerConstraintApplication = Annotated[
    _CheckerConstraintExecutedApplication | _CheckerConstraintUnprovenApplication,
    Field(discriminator="status"),
]


class _CheckerStandaloneBoundPayload(_CheckerStandalonePayload):
    constraint_application: tuple[_CheckerConstraintApplication, ...] = Field(
        max_length=MAX_PAYLOAD_COLLECTION_ITEMS
    )

    @field_validator("constraint_application")
    @classmethod
    def _stable_unique_applications(
        cls,
        value: tuple[_CheckerConstraintApplication, ...],
    ) -> tuple[_CheckerConstraintApplication, ...]:
        keys = tuple((item.constraint_id, item.checker_id) for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("checker constraint applications must be stable-unique")
        return value


class _CheckerStandaloneProfileBoundPayload(_CheckerStandaloneBoundPayload):
    checker_profile: ProfileRefV1


class _CheckerStandaloneProfileNoConstraintPayload(_CheckerStandaloneProfileBoundPayload):
    constraint_snapshot_binding_status: Literal["not_applicable"]


class _CheckerStandaloneProfileConstraintPayload(_CheckerStandaloneProfileBoundPayload):
    constraint_snapshot_binding_status: Literal["bound"]
    constraint_snapshot_artifact_id: BoundedText


class _CheckerExecutionBindingPayload(_StrictWireModel):
    wrapper_id: BoundedText
    native_id: BoundedText
    constraint_id: BoundedText | None = None

    @field_validator("wrapper_id", "native_id", "constraint_id")
    @classmethod
    def _nonblank_identity(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("checker execution identities must be non-blank")
        return value


class _CheckerReviewCompanionPayload(_CheckerProfilePayload):
    checker_profile: ProfileRefV1
    checker_execution_bindings: tuple[_CheckerExecutionBindingPayload, ...] = Field(
        max_length=MAX_PAYLOAD_COLLECTION_ITEMS,
    )
    constraint_application: tuple[_CheckerConstraintApplication, ...] = Field(
        max_length=MAX_PAYLOAD_COLLECTION_ITEMS
    )

    @field_validator("checker_execution_bindings")
    @classmethod
    def _stable_unique_bindings(
        cls,
        value: tuple[_CheckerExecutionBindingPayload, ...],
    ) -> tuple[_CheckerExecutionBindingPayload, ...]:
        keys = tuple((item.native_id, item.constraint_id or "", item.wrapper_id) for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("checker execution bindings must be stable-unique")
        return value

    @field_validator("constraint_application")
    @classmethod
    def _stable_unique_applications(
        cls,
        value: tuple[_CheckerConstraintApplication, ...],
    ) -> tuple[_CheckerConstraintApplication, ...]:
        keys = tuple((item.constraint_id, item.checker_id) for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("checker constraint applications must be stable-unique")
        return value

    @model_validator(mode="after")
    def _profile_and_application_closure(self) -> "_CheckerReviewCompanionPayload":
        if self.profile != self.checker_profile:
            raise ValueError("review checker profile identities differ")
        scoped = tuple(
            (item.constraint_id, item.native_id)
            for item in self.checker_execution_bindings
            if item.constraint_id is not None
        )
        applied = tuple(
            (item.constraint_id, item.checker_id) for item in self.constraint_application
        )
        if tuple(sorted(scoped)) != tuple(sorted(applied)):
            raise ValueError("review checker applications differ from execution bindings")
        return self


class _CheckerReviewCompanionNoConstraintPayload(_CheckerReviewCompanionPayload):
    constraint_snapshot_binding_status: Literal["not_applicable"]

    @model_validator(mode="after")
    def _no_scoped_constraints(self) -> "_CheckerReviewCompanionNoConstraintPayload":
        if self.constraint_application or any(
            item.constraint_id is not None for item in self.checker_execution_bindings
        ):
            raise ValueError("unbound review checker carries constraint execution")
        return self


class _CheckerReviewCompanionConstraintPayload(_CheckerReviewCompanionPayload):
    constraint_snapshot_binding_status: Literal["bound"]
    constraint_snapshot_artifact_id: BoundedText


class _SimulationInvariant(_StrictWireModel):
    name: BoundedText
    ok: bool
    observed: float = Field(allow_inf_nan=False)
    threshold: float = Field(allow_inf_nan=False)
    evidence: dict[str, JsonValue]

    @field_validator("observed", "threshold", mode="before")
    @classmethod
    def _canonical_numbers(cls, value: object) -> object:
        return _decode_canonical_float(value)


class _SimulationFindingsPayload(_StrictWireModel):
    payload_schema_version: Literal["simulation-result@1"]
    snapshot_id: BoundedText
    findings: tuple[Finding, ...] = Field(max_length=MAX_PAYLOAD_COLLECTION_ITEMS)

    @field_validator("findings", mode="before")
    @classmethod
    def _canonical_finding_numbers(cls, value: object) -> object:
        return _decode_finding_confidences(value)


class _SimulationBudgetPayload(_SimulationFindingsPayload):
    seed: int
    replication_count: int = Field(ge=1)
    horizon_steps: int = Field(ge=1)


class _SimulationConstraintApplication(_StrictWireModel):
    status: Literal["not_applicable", "unproven"]
    reason_code: Literal["constraint_profile_not_executable"] | None = None

    @model_validator(mode="after")
    def _reason_shape(self) -> "_SimulationConstraintApplication":
        if (self.status == "not_applicable") != (self.reason_code is None):
            raise ValueError("simulation constraint application reason differs from status")
        return self


class _SimulationScenarioApplication(_StrictWireModel):
    status: Literal["not_applicable", "unproven"]
    reason_code: Literal["scenario_reset_not_executable"] | None = None

    @model_validator(mode="after")
    def _reason_shape(self) -> "_SimulationScenarioApplication":
        if (self.status == "not_applicable") != (self.reason_code is None):
            raise ValueError("simulation scenario application reason differs from status")
        return self


class _SimulationExecutionBinding(_StrictWireModel):
    simulation_profile: ProfileRefV1
    workload_profile: ProfileRefV1
    constraint_snapshot_artifact_id: BoundedText | None = None
    scenario_artifact_id: BoundedText | None = None
    constraint_ids: tuple[BoundedText, ...] = Field(max_length=MAX_PAYLOAD_COLLECTION_ITEMS)
    scenario_id: BoundedText | None = None
    constraint_application: _SimulationConstraintApplication
    scenario_application: _SimulationScenarioApplication

    @field_validator("constraint_ids")
    @classmethod
    def _unique_constraint_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("simulation constraint ids must be unique")
        return value

    @model_validator(mode="after")
    def _input_application_shape(self) -> "_SimulationExecutionBinding":
        if self.constraint_snapshot_artifact_id is None:
            if self.constraint_ids or self.constraint_application.status != "not_applicable":
                raise ValueError("simulation constraint application has no bound Artifact")
        elif self.constraint_application.status != "unproven":
            raise ValueError("simulation constraint Artifact is not marked unproven")

        if self.scenario_artifact_id is None:
            if self.scenario_id is not None or self.scenario_application.status != "not_applicable":
                raise ValueError("simulation scenario application has no bound Artifact")
        elif self.scenario_id is None or self.scenario_application.status != "unproven":
            raise ValueError("simulation scenario Artifact is not exactly represented")
        return self


class _ReviewSimulationExecutionBinding(_StrictWireModel):
    simulation_profile: ProfileRefV1
    constraint_snapshot_artifact_id: BoundedText | None = None
    constraint_ids: tuple[BoundedText, ...] = Field(max_length=MAX_PAYLOAD_COLLECTION_ITEMS)
    constraint_application: _SimulationConstraintApplication

    @field_validator("constraint_ids")
    @classmethod
    def _unique_constraint_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("review simulation constraint ids must be unique")
        return value

    @model_validator(mode="after")
    def _input_application_shape(self) -> "_ReviewSimulationExecutionBinding":
        if self.constraint_snapshot_artifact_id is None:
            if self.constraint_ids or self.constraint_application.status != "not_applicable":
                raise ValueError("review simulation has no bound constraint Artifact")
        elif self.constraint_application.status != "unproven":
            raise ValueError("review simulation constraint Artifact is not marked unproven")
        return self


class _SimulationFullPayload(_SimulationBudgetPayload):
    invariants: tuple[_SimulationInvariant, ...] = Field(max_length=MAX_PAYLOAD_COLLECTION_ITEMS)
    sensitivity: dict[str, JsonValue]

    @field_validator("invariants")
    @classmethod
    def _unique_invariants(
        cls, value: tuple[_SimulationInvariant, ...]
    ) -> tuple[_SimulationInvariant, ...]:
        names = [item.name for item in value]
        if len(names) != len(set(names)):
            raise ValueError("simulation invariant names must be unique")
        return value

    @field_validator("sensitivity", mode="before")
    @classmethod
    def _finite_numeric_sensitivity(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        decoded = dict(value)
        for key in ("source_total", "sink_total", "sink_source_ratio"):
            if key not in decoded:
                continue
            numeric = _decode_canonical_float(decoded[key])
            if (
                isinstance(numeric, bool)
                or not isinstance(numeric, (int, float))
                or not math.isfinite(float(numeric))
            ):
                raise ValueError("simulation sensitivity numeric field must be finite")
            decoded[key] = float(numeric)
        return decoded

    @field_validator("sensitivity")
    @classmethod
    def _exact_execution_binding(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        execution = value.get("execution_binding")
        if execution is None:
            return value
        if not isinstance(execution, Mapping):
            raise ValueError("simulation execution binding must be an object")
        binding_model = (
            _SimulationExecutionBinding
            if "workload_profile" in execution
            else _ReviewSimulationExecutionBinding
        )
        parsed = binding_model.model_validate(execution)
        canonical = _json_mapping(parsed.model_dump(mode="json", exclude_none=True))
        if not _same_exact_json(canonical, execution):
            raise ValueError("simulation execution binding is not its exact canonical wire shape")
        return value


class _SimulationProfilePayload(_SimulationFullPayload):
    profile: ProfileRefV1


class _ValidationSeedBinding(_StrictWireModel):
    root_seed: int = Field(ge=0, le=(1 << 64) - 1)
    run_kind: RunKindRef
    profile_id: BoundedText
    profile_version: int = Field(ge=1)
    case_id: BoundedText
    replication_index: int = Field(ge=0)
    seed: int = Field(ge=0, le=(1 << 64) - 1)
    seed_derivation_version: Literal["subseed@1"]

    @model_validator(mode="after")
    def _derived_seed(self) -> "_ValidationSeedBinding":
        expected = int(
            sha256(
                canonical_json(
                    {
                        "root_seed": self.root_seed,
                        "run_kind": self.run_kind.model_dump(mode="json"),
                        "profile_id": self.profile_id,
                        "profile_version": self.profile_version,
                        "case_id": self.case_id,
                        "replication_index": self.replication_index,
                    }
                ).encode("utf-8")
            ).hexdigest()[:16],
            16,
        )
        if self.seed != expected:
            raise ValueError("validation child seed differs from subseed@1")
        return self


class _PatchSimulationExecutionBinding(_StrictWireModel):
    binding_schema_version: Literal["simulation-expected-finding-binding@1"]
    producer_id: Literal["economy_sim"]
    simulation_profile: ProfileRefV1
    execution_mode: Literal["single_population@1"]
    seed_binding: _ValidationSeedBinding
    constraint_snapshot_binding_status: Literal["not_applicable", "bound"]
    constraint_snapshot_artifact_id: BoundedText | None = None
    constraint_ids: tuple[BoundedText, ...] = Field(max_length=MAX_PAYLOAD_COLLECTION_ITEMS)
    constraint_application: _SimulationConstraintApplication
    n_agents: int = Field(ge=1)
    n_ticks: int = Field(ge=1)

    @field_validator("constraint_ids")
    @classmethod
    def _stable_unique_constraint_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("patch simulation constraint ids must be stable-unique")
        return value

    @model_validator(mode="after")
    def _constraint_binding_shape(self) -> "_PatchSimulationExecutionBinding":
        if (
            self.seed_binding.run_kind != RunKindRef(kind="patch.validate", version=1)
            or self.seed_binding.profile_id != self.simulation_profile.profile_id
            or self.seed_binding.profile_version != self.simulation_profile.version
            or self.seed_binding.case_id
            != (
                f"simulation:{self.simulation_profile.profile_id}@{self.simulation_profile.version}"
            )
            or self.seed_binding.replication_index != 0
        ):
            raise ValueError("patch simulation seed binding differs from its execution identity")
        if self.constraint_snapshot_binding_status == "not_applicable":
            if (
                self.constraint_snapshot_artifact_id is not None
                or self.constraint_ids
                or self.constraint_application.status != "not_applicable"
            ):
                raise ValueError("unbound patch simulation carries constraint authority")
        elif (
            self.constraint_snapshot_artifact_id is None
            or self.constraint_application.status != "unproven"
        ):
            raise ValueError("bound patch simulation is not explicitly unproven")
        return self


class _RegressionSuiteEvidence(_StrictWireModel):
    payload_schema_version: Literal["regression-evidence@1"]
    suite_artifact_id: BoundedText
    snapshot_id: BoundedText | None
    status: Literal["passed", "failed", "unproven", "not_executed"]
    reason_code: BoundedText | None = None

    @model_validator(mode="after")
    def _reason_shape(self) -> "_RegressionSuiteEvidence":
        unproven = self.status in {"unproven", "not_executed"}
        if unproven != (self.reason_code is not None):
            raise ValueError("regression suite reason_code differs from its status")
        return self


class _RegressionSuiteSeedEvidence(_RegressionSuiteEvidence):
    seed: int


class _RegressionSuiteCaseSeedEvidence(_RegressionSuiteSeedEvidence):
    case_seed_manifest: RegressionCaseSeedManifestV1

    @model_validator(mode="after")
    def _suite_manifest(self) -> "_RegressionSuiteCaseSeedEvidence":
        if self.case_seed_manifest.suite_artifact_id != self.suite_artifact_id:
            raise ValueError("regression case seed manifest is bound to another suite")
        return self


class _RegressionSuiteFindingEvidence(_RegressionSuiteEvidence):
    findings: tuple[Finding, ...] = Field(max_length=MAX_PAYLOAD_COLLECTION_ITEMS)

    @model_validator(mode="after")
    def _finding_verdict(self) -> "_RegressionSuiteFindingEvidence":
        if self.findings and self.status != deterministic_finding_status(self.findings):
            raise ValueError("regression suite status contradicts exact Finding verdicts")
        if self.status == "failed" and not self.findings:
            raise ValueError("failed regression suite requires exact findings")
        return self


class _RegressionSuiteFindingCaseSeedEvidence(_RegressionSuiteFindingEvidence):
    seed: int
    case_seed_manifest: RegressionCaseSeedManifestV1

    @model_validator(mode="after")
    def _suite_manifest(self) -> "_RegressionSuiteFindingCaseSeedEvidence":
        if self.case_seed_manifest.suite_artifact_id != self.suite_artifact_id:
            raise ValueError("regression case seed manifest is bound to another suite")
        return self


class _RegressionSuiteExecutionCoverageBinding(_StrictWireModel):
    binding_schema_version: Literal["regression-suite-expected-finding-binding@1"]
    suite_artifact_id: BoundedText
    validation_profile: ProfileRefV1
    constraint_snapshot_artifact_id: BoundedText | None = None
    env_contract_version: BoundedText
    root_seed: int = Field(ge=0, le=(1 << 64) - 1)
    run_kind: RunKindRef
    case_id: BoundedText
    replication_index: Literal[0]
    execution_seed: int = Field(ge=0, le=(1 << 64) - 1)
    seed_derivation_version: Literal["subseed@1"]

    @model_validator(mode="after")
    def _exact_seed_and_suite(self) -> "_RegressionSuiteExecutionCoverageBinding":
        if self.case_id != self.suite_artifact_id:
            raise ValueError("regression suite coverage case differs from its suite")
        expected = int(
            sha256(
                canonical_json(
                    {
                        "root_seed": self.root_seed,
                        "run_kind": self.run_kind.model_dump(mode="json"),
                        "profile_id": self.validation_profile.profile_id,
                        "profile_version": self.validation_profile.version,
                        "case_id": self.case_id,
                        "replication_index": self.replication_index,
                    }
                ).encode("utf-8")
            ).hexdigest()[:16],
            16,
        )
        if self.execution_seed != expected:
            raise ValueError("regression suite coverage seed differs from subseed@1")
        return self


class _RegressionSuiteBoundEvidence(_RegressionSuiteEvidence, _ValidationSeedBinding):
    pass


class _RegressionSuiteFindingBoundEvidence(_RegressionSuiteFindingEvidence, _ValidationSeedBinding):
    pass


class _RegressionSuiteCaseSeedBoundEvidence(
    _RegressionSuiteCaseSeedEvidence,
    _ValidationSeedBinding,
):
    @model_validator(mode="after")
    def _outer_seed_binding(self) -> "_RegressionSuiteCaseSeedBoundEvidence":
        _validate_case_seed_outer_binding(self)
        return self


class _RegressionSuiteFindingCaseSeedBoundEvidence(
    _RegressionSuiteFindingCaseSeedEvidence,
    _ValidationSeedBinding,
):
    @model_validator(mode="after")
    def _outer_seed_binding(self) -> "_RegressionSuiteFindingCaseSeedBoundEvidence":
        _validate_case_seed_outer_binding(self)
        return self


def _validate_case_seed_outer_binding(value: object) -> None:
    manifest = value.case_seed_manifest
    if (
        manifest.root_seed != value.root_seed
        or manifest.run_kind != value.run_kind
        or manifest.profile.profile_id != value.profile_id
        or manifest.profile.version != value.profile_version
        or value.case_id != value.suite_artifact_id
    ):
        raise ValueError("regression case seed manifest differs from outer seed binding")


class _RegressionFindingEvidence(_StrictWireModel):
    payload_schema_version: Literal["regression-evidence@1"]
    requirement_id: BoundedText
    dimension: BoundedText
    snapshot_id: BoundedText
    status: Literal["passed", "failed", "unproven"]
    findings: tuple[Finding, ...] = Field(max_length=MAX_PAYLOAD_COLLECTION_ITEMS)

    @field_validator("findings", mode="before")
    @classmethod
    def _canonical_finding_numbers(cls, value: object) -> object:
        return _decode_finding_confidences(value)

    @model_validator(mode="after")
    def _finding_verdict(self) -> "_RegressionFindingEvidence":
        if self.status != deterministic_finding_status(self.findings):
            raise ValueError("regression dimension status contradicts exact Finding verdicts")
        return self


class _RegressionFindingBoundEvidence(_RegressionFindingEvidence, _ValidationSeedBinding):
    pass


class _RegressionDimensionEvidence(_StrictWireModel):
    payload_schema_version: Literal["regression-evidence@1"]
    requirement_id: BoundedText
    dimension: BoundedText
    status: Literal["passed", "failed", "unproven"]
    reason_code: BoundedText | None = None
    detail: dict[str, JsonValue]

    @model_validator(mode="after")
    def _reason_shape(self) -> "_RegressionDimensionEvidence":
        if (self.status == "passed") != (self.reason_code is None):
            raise ValueError("regression dimension reason_code differs from its status")
        raw_findings = self.detail.get("findings")
        if raw_findings is not None:
            if not isinstance(raw_findings, list):
                raise ValueError("regression dimension findings must be an array")
            findings = tuple(Finding.model_validate(item) for item in raw_findings)
            if self.status != deterministic_finding_status(findings):
                raise ValueError("regression dimension status contradicts exact Finding verdicts")
            snapshot_id = self.detail.get("snapshot_id")
            if isinstance(snapshot_id, str):
                _validate_findings_snapshot(
                    snapshot_id=snapshot_id,
                    findings=findings,
                    label="regression dimension",
                )
        return self


@dataclass(frozen=True, slots=True)
class PayloadSchemaValidator:
    """One exact versioned schema entry.

    ``parser=None`` is a deliberate fail-closed entry, never a wildcard.
    """

    payload_schema_id: str
    discriminator_field: str | None
    discriminator_values: tuple[str, ...]
    parser: PayloadParser | None
    unavailable_reason: str | None = None

    @property
    def is_available(self) -> bool:
        return self.parser is not None


def _json_mapping(value: object) -> dict[str, object]:
    projected = _canonical_wire_projection(value)
    if not isinstance(projected, dict):  # pragma: no cover - guarded by callers
        raise TypeError("canonical payload is not an object")
    return cast(dict[str, object], projected)


def _canonical_wire_projection(value: object) -> object:
    """Project a parsed model to the frozen canonical-JSON wire without text copies."""

    if isinstance(value, Mapping):
        return {
            key: _canonical_wire_projection(item) for key, item in value.items() if item is not None
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical_wire_projection(item) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, Enum):
        return _canonical_wire_projection(value.value)
    if isinstance(value, float):
        return "f:" + format(Decimal(str(value)).normalize(), "f")
    return value


def _same_exact_json(left: object, right: object) -> bool:
    """Compare JSON trees without collapsing bool/int, float tags, or signed zero."""

    if isinstance(left, Mapping):
        if not isinstance(right, Mapping) or set(left) != set(right):
            return False
        return all(_same_exact_json(value, right[key]) for key, value in left.items())
    if isinstance(left, Sequence) and not isinstance(left, (str, bytes, bytearray)):
        if not isinstance(right, Sequence) or isinstance(right, (str, bytes, bytearray)):
            return False
        return len(left) == len(right) and all(
            _same_exact_json(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    if isinstance(left, bool):
        return isinstance(right, bool) and left == right
    if isinstance(left, int):
        return isinstance(right, int) and not isinstance(right, bool) and left == right
    if isinstance(left, float):  # pragma: no cover - projections tag every float above
        return isinstance(right, float) and left.hex() == right.hex()
    if isinstance(left, str):
        return isinstance(right, str) and left == right
    if left is None:
        return right is None
    return type(left) is type(right) and left == right


def _decode_canonical_float(value: object) -> object:
    if isinstance(value, str) and value.startswith("f:"):
        try:
            return float(value[2:])
        except ValueError as exc:
            raise ValueError("canonical float field is malformed") from exc
    return value


def _decode_finding_confidences(value: object) -> object:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return value
    decoded: list[object] = []
    for item in value:
        if isinstance(item, Mapping) and "confidence" in item:
            confidence = _decode_canonical_float(item["confidence"])
            if isinstance(confidence, float) and not math.isfinite(confidence):
                raise ValueError("finding confidence must be finite in published Artifacts")
            decoded.append(
                {
                    **dict(item),
                    "confidence": confidence,
                }
            )
        else:
            decoded.append(item)
    return decoded


def _require_exact_model(
    model: type[BaseModel], payload: Mapping[str, object]
) -> dict[str, object]:
    parsed = model.model_validate(payload)
    projected = _canonical_wire_projection(parsed.model_dump(mode="json"))
    if not isinstance(projected, dict):  # pragma: no cover - every registered model is an object
        raise TypeError("canonical payload is not an object")
    if not _same_exact_json(projected, payload):
        raise ValueError("payload is not the exact canonical model wire shape")
    return cast(dict[str, object], projected)


def _validate_ir_snapshot(payload: Mapping[str, object]) -> dict[str, object]:
    if payload.get("meta_schema_version") != META_SCHEMA_VERSION:
        raise ValueError("IR snapshot uses an unsupported meta schema")
    snapshot = snapshot_from_canonical_view(payload)
    canonical = _json_mapping(snapshot.content_payload)
    if not _same_exact_json(canonical, payload):
        raise ValueError("IR snapshot is not its exact canonical wire shape")
    return canonical


def _validate_constraint_snapshot(payload: Mapping[str, object]) -> dict[str, object]:
    if set(payload) != {"dsl_grammar_version", "constraints"}:
        raise ValueError("constraint snapshot has the wrong top-level shape")
    if payload.get("dsl_grammar_version") != DSL_GRAMMAR_VERSION:
        raise ValueError("constraint snapshot uses an unsupported DSL grammar")
    raw_constraints = payload.get("constraints")
    if not isinstance(raw_constraints, Sequence) or isinstance(raw_constraints, (str, bytes)):
        raise ValueError("constraint snapshot constraints must be an array")
    if len(raw_constraints) > MAX_PAYLOAD_COLLECTION_ITEMS:
        raise ValueError("constraint snapshot contains too many constraints")
    constraints = tuple(Constraint.model_validate(item) for item in raw_constraints)
    ids = [item.id for item in constraints]
    if len(ids) != len(set(ids)):
        raise ValueError("constraint snapshot ids must be unique")
    if any(item.dsl_grammar_version != DSL_GRAMMAR_VERSION for item in constraints):
        raise ValueError("constraint grammar differs from snapshot grammar")
    canonical = _json_mapping(
        {
            "dsl_grammar_version": DSL_GRAMMAR_VERSION,
            "constraints": [item.model_dump(mode="json", by_alias=True) for item in constraints],
        }
    )
    if not _same_exact_json(canonical, payload):
        raise ValueError("constraint snapshot is not its exact canonical wire shape")
    return canonical


def _validate_findings_snapshot(
    *, snapshot_id: str, findings: Sequence[Finding], label: str
) -> None:
    if any(item.snapshot_id != snapshot_id for item in findings):
        raise ValueError(f"{label} finding snapshot differs from the report")


def _split_requirement_id(
    payload: Mapping[str, object],
) -> tuple[dict[str, object], str | None]:
    body = dict(payload)
    requirement_id = body.pop("requirement_id", None)
    if requirement_id is not None and (
        not isinstance(requirement_id, str) or not requirement_id or len(requirement_id) > 4096
    ):
        raise ValueError("requirement_id is not a bounded non-empty string")
    return body, requirement_id


def _restore_requirement_id(
    canonical: dict[str, object], requirement_id: str | None
) -> dict[str, object]:
    return canonical if requirement_id is None else {**canonical, "requirement_id": requirement_id}


def _validate_checker_report(payload: Mapping[str, object]) -> dict[str, object]:
    body, requirement_id = _split_requirement_id(payload)
    fields = set(body)
    if fields == {"payload_schema_version", "snapshot_id", "findings"}:
        model: type[BaseModel] = _CheckerFindingsPayload
    elif fields == {"payload_schema_version", "profile", "snapshot_id", "findings"}:
        model = _CheckerProfilePayload
    elif fields == {
        "payload_schema_version",
        "snapshot_id",
        "checker_ids",
        "defect_classes",
        "findings",
    }:
        model = _CheckerStandalonePayload
    elif fields == {
        "payload_schema_version",
        "snapshot_id",
        "checker_ids",
        "defect_classes",
        "constraint_application",
        "findings",
    }:
        model = _CheckerStandaloneBoundPayload
    elif fields == {
        "payload_schema_version",
        "checker_profile",
        "snapshot_id",
        "checker_ids",
        "defect_classes",
        "constraint_application",
        "findings",
    }:
        model = _CheckerStandaloneProfileBoundPayload
    elif fields == {
        "payload_schema_version",
        "checker_profile",
        "constraint_snapshot_binding_status",
        "snapshot_id",
        "checker_ids",
        "defect_classes",
        "constraint_application",
        "findings",
    }:
        model = _CheckerStandaloneProfileNoConstraintPayload
    elif fields == {
        "payload_schema_version",
        "checker_profile",
        "constraint_snapshot_binding_status",
        "constraint_snapshot_artifact_id",
        "snapshot_id",
        "checker_ids",
        "defect_classes",
        "constraint_application",
        "findings",
    }:
        model = _CheckerStandaloneProfileConstraintPayload
    elif fields == {
        "payload_schema_version",
        "profile",
        "checker_profile",
        "checker_execution_bindings",
        "constraint_snapshot_binding_status",
        "snapshot_id",
        "constraint_application",
        "findings",
    }:
        model = _CheckerReviewCompanionNoConstraintPayload
    elif fields == {
        "payload_schema_version",
        "profile",
        "checker_profile",
        "checker_execution_bindings",
        "constraint_snapshot_binding_status",
        "constraint_snapshot_artifact_id",
        "snapshot_id",
        "constraint_application",
        "findings",
    }:
        model = _CheckerReviewCompanionConstraintPayload
    else:
        raise ValueError("checker report does not match a registered exact variant")
    parsed = model.model_validate(body)
    _validate_findings_snapshot(
        snapshot_id=parsed.snapshot_id,
        findings=parsed.findings,
        label="checker report",
    )
    return _restore_requirement_id(_require_exact_model(model, body), requirement_id)


def _validate_simulation_result(payload: Mapping[str, object]) -> dict[str, object]:
    body, requirement_id = _split_requirement_id(payload)
    fields = set(body)
    base = {"payload_schema_version", "snapshot_id", "findings"}
    budget = base | {"seed", "replication_count", "horizon_steps"}
    full = budget | {"invariants", "sensitivity"}
    if fields == base:
        model: type[BaseModel] = _SimulationFindingsPayload
    elif fields == budget:
        model = _SimulationBudgetPayload
    elif fields == full:
        model = _SimulationFullPayload
    elif fields == full | {"profile"}:
        model = _SimulationProfilePayload
    else:
        raise ValueError("simulation result does not match a registered exact variant")
    parsed = model.model_validate(body)
    _validate_findings_snapshot(
        snapshot_id=parsed.snapshot_id,
        findings=parsed.findings,
        label="simulation result",
    )
    return _restore_requirement_id(_require_exact_model(model, body), requirement_id)


def _validate_regression_evidence(payload: Mapping[str, object]) -> dict[str, object]:
    body = dict(payload)
    suite_coverage_raw = body.pop("execution_coverage_binding", None)
    suite_coverage = (
        None
        if suite_coverage_raw is None
        else _RegressionSuiteExecutionCoverageBinding.model_validate(suite_coverage_raw)
    )
    simulation_execution_raw = body.pop("simulation_execution_binding", None)
    simulation_execution = (
        None
        if simulation_execution_raw is None
        else _PatchSimulationExecutionBinding.model_validate(simulation_execution_raw)
    )
    checker_profile_raw = body.pop("checker_profile", None)
    checker_bindings_raw = body.pop("checker_execution_bindings", None)
    checker_constraint_status = body.pop("constraint_snapshot_binding_status", None)
    checker_constraint_id = body.pop("constraint_snapshot_artifact_id", None)
    checker_present = any(
        value is not None
        for value in (
            checker_profile_raw,
            checker_bindings_raw,
            checker_constraint_status,
            checker_constraint_id,
        )
    )
    checker_profile: ProfileRefV1 | None = None
    checker_bindings: tuple[_CheckerExecutionBindingPayload, ...] = ()
    if checker_present:
        checker_profile = ProfileRefV1.model_validate(checker_profile_raw)
        if not isinstance(checker_bindings_raw, Sequence) or isinstance(
            checker_bindings_raw, (str, bytes, bytearray)
        ):
            raise ValueError("checker execution bindings must be an array")
        checker_bindings = tuple(
            _CheckerExecutionBindingPayload.model_validate(value) for value in checker_bindings_raw
        )
        binding_keys = tuple(
            (item.native_id, item.constraint_id or "", item.wrapper_id) for item in checker_bindings
        )
        if not checker_bindings or binding_keys != tuple(sorted(set(binding_keys))):
            raise ValueError("checker execution bindings must be stable-unique")
        if checker_constraint_status == "bound":
            if not isinstance(checker_constraint_id, str) or not checker_constraint_id:
                raise ValueError("bound checker constraint snapshot has no Artifact ID")
        elif checker_constraint_status == "not_applicable":
            if checker_constraint_id is not None:
                raise ValueError("unbound checker constraint snapshot carries an Artifact ID")
        else:
            raise ValueError("checker constraint snapshot binding status is invalid")
    lineage_suite_present = "lineage_suite_artifact_ids" in body
    lineage_suite_raw = body.pop("lineage_suite_artifact_ids", None)
    lineage_suite_artifact_ids: tuple[str, ...] = ()
    if lineage_suite_present:
        if not isinstance(lineage_suite_raw, Sequence) or isinstance(
            lineage_suite_raw, (str, bytes, bytearray)
        ):
            raise ValueError("regression evidence lineage suite binding must be an array")
        if any(not isinstance(value, str) or not value for value in lineage_suite_raw):
            raise ValueError("regression evidence lineage suite binding contains a non-ID")
        lineage_suite_artifact_ids = tuple(lineage_suite_raw)
        if len(lineage_suite_artifact_ids) > 1 or len(lineage_suite_artifact_ids) != len(
            set(lineage_suite_artifact_ids)
        ):
            raise ValueError(
                "regression evidence lineage suite binding must contain at most one exact ID"
            )
        semantic_suite_id = body.get("suite_artifact_id")
        if semantic_suite_id is None and body.get("dimension") == "regression":
            detail = body.get("detail")
            if isinstance(detail, Mapping):
                semantic_suite_id = detail.get("suite_artifact_id")
        expected_lineage_suite_ids = () if semantic_suite_id is None else (semantic_suite_id,)
        if lineage_suite_artifact_ids != expected_lineage_suite_ids:
            raise ValueError(
                "regression evidence lineage suite binding differs from its semantic suite"
            )
    auto_apply_context_raw = body.pop("auto_apply_context", None)
    oracle_attestations_raw = body.pop("oracle_attestations", None)
    outcome_attestations_raw = body.pop("outcome_attestations", None)
    present = tuple(
        value is not None
        for value in (
            auto_apply_context_raw,
            oracle_attestations_raw,
            outcome_attestations_raw,
        )
    )
    if any(present) and not all(present):
        raise ValueError(
            "auto-apply regression evidence requires context plus both attestation sets"
        )
    auto_apply_context: AutoApplyEvidenceContextV1 | None = None
    oracle_attestations: tuple[AutoApplyOracleAttestationV1, ...] = ()
    outcome_attestations: tuple[AutoApplyOutcomeAttestationV1, ...] = ()
    if auto_apply_context_raw is not None:
        auto_apply_context = AutoApplyEvidenceContextV1.model_validate(auto_apply_context_raw)
        if any(
            not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray))
            for raw in (oracle_attestations_raw, outcome_attestations_raw)
        ):
            raise ValueError("auto-apply attestation sets must be arrays")
        oracle_attestations = tuple(
            AutoApplyOracleAttestationV1.model_validate(value) for value in oracle_attestations_raw
        )
        outcome_attestations = tuple(
            AutoApplyOutcomeAttestationV1.model_validate(value)
            for value in outcome_attestations_raw
        )
        oracle_keys = tuple(
            (item.oracle.oracle_id, item.oracle.oracle_version) for item in oracle_attestations
        )
        outcome_keys = tuple(
            (
                item.rule.resolved_policy_id,
                item.rule.outcome_rule_id,
                item.requirement_id,
            )
            for item in outcome_attestations
        )
        if oracle_keys != tuple(sorted(set(oracle_keys))):
            raise ValueError("auto-apply oracle attestations must be stable-unique")
        if outcome_keys != tuple(sorted(set(outcome_keys))):
            raise ValueError("auto-apply outcome attestations must be stable-unique")
    suite_requirement_id: str | None = None
    if "suite_artifact_id" in body:
        body, suite_requirement_id = _split_requirement_id(body)
    fields = set(body)
    frozen_fields = frozenset(fields)
    suite = {"payload_schema_version", "suite_artifact_id", "snapshot_id", "status"}
    suite_with_reason = suite | {"reason_code"}
    suite_findings = suite | {"findings"}
    suite_findings_with_reason = suite_findings | {"reason_code"}
    finding = {
        "payload_schema_version",
        "requirement_id",
        "dimension",
        "snapshot_id",
        "status",
        "findings",
    }
    dimension = {
        "payload_schema_version",
        "requirement_id",
        "dimension",
        "status",
        "reason_code",
        "detail",
    }
    seed_binding = set(_ValidationSeedBinding.model_fields)
    case_seed_manifest = {"case_seed_manifest"}
    if frozen_fields in {frozenset(suite), frozenset(suite_with_reason)}:
        model: type[BaseModel] = _RegressionSuiteEvidence
    elif frozen_fields in {
        frozenset(suite | {"seed"}),
        frozenset(suite_with_reason | {"seed"}),
    }:
        model = _RegressionSuiteSeedEvidence
    elif frozen_fields in {
        frozenset(suite | {"seed"} | case_seed_manifest),
        frozenset(suite_with_reason | {"seed"} | case_seed_manifest),
    }:
        model = _RegressionSuiteCaseSeedEvidence
    elif frozen_fields in {
        frozenset(suite | seed_binding),
        frozenset(suite_with_reason | seed_binding),
    }:
        model = _RegressionSuiteBoundEvidence
    elif frozen_fields in {
        frozenset(suite | seed_binding | case_seed_manifest),
        frozenset(suite_with_reason | seed_binding | case_seed_manifest),
    }:
        model = _RegressionSuiteCaseSeedBoundEvidence
    elif frozen_fields in {
        frozenset(suite_findings),
        frozenset(suite_findings_with_reason),
    }:
        model = _RegressionSuiteFindingEvidence
    elif frozen_fields in {
        frozenset(suite_findings | {"seed"} | case_seed_manifest),
        frozenset(suite_findings_with_reason | {"seed"} | case_seed_manifest),
    }:
        model = _RegressionSuiteFindingCaseSeedEvidence
    elif frozen_fields in {
        frozenset(suite_findings | seed_binding),
        frozenset(suite_findings_with_reason | seed_binding),
    }:
        model = _RegressionSuiteFindingBoundEvidence
    elif frozen_fields in {
        frozenset(suite_findings | seed_binding | case_seed_manifest),
        frozenset(suite_findings_with_reason | seed_binding | case_seed_manifest),
    }:
        model = _RegressionSuiteFindingCaseSeedBoundEvidence
    elif fields == finding:
        model = _RegressionFindingEvidence
    elif fields == finding | seed_binding:
        model = _RegressionFindingBoundEvidence
    elif frozen_fields in {
        frozenset(dimension),
        frozenset(dimension - {"reason_code"}),
    }:
        model = _RegressionDimensionEvidence
    else:
        raise ValueError("regression evidence does not match a registered exact variant")
    parsed = model.model_validate(body)
    if suite_coverage is not None:
        if not isinstance(
            parsed,
            (
                _RegressionSuiteEvidence,
                _RegressionSuiteSeedEvidence,
                _RegressionSuiteCaseSeedEvidence,
                _RegressionSuiteFindingEvidence,
                _RegressionSuiteFindingCaseSeedEvidence,
                _RegressionSuiteBoundEvidence,
                _RegressionSuiteFindingBoundEvidence,
                _RegressionSuiteCaseSeedBoundEvidence,
                _RegressionSuiteFindingCaseSeedBoundEvidence,
            ),
        ):
            raise ValueError("suite execution coverage is attached to another dimension")
        if parsed.status not in {"passed", "failed"}:
            raise ValueError("suite execution coverage cannot attest an unproven result")
        if suite_coverage.suite_artifact_id != parsed.suite_artifact_id:
            raise ValueError("suite execution coverage names another suite")
        if isinstance(parsed, _ValidationSeedBinding) and (
            suite_coverage.root_seed != parsed.root_seed
            or suite_coverage.run_kind != parsed.run_kind
            or suite_coverage.validation_profile.profile_id != parsed.profile_id
            or suite_coverage.validation_profile.version != parsed.profile_version
            or suite_coverage.execution_seed != parsed.seed
        ):
            raise ValueError("suite execution coverage differs from outer seed authority")
    if simulation_execution is not None:
        if getattr(parsed, "dimension", None) != "simulation":
            raise ValueError("simulation execution binding is attached to another dimension")
        if isinstance(parsed, _ValidationSeedBinding):
            outer_seed = _ValidationSeedBinding.model_validate(
                {field: getattr(parsed, field) for field in _ValidationSeedBinding.model_fields}
            )
        elif isinstance(parsed, _RegressionDimensionEvidence):
            outer_seed = _ValidationSeedBinding.model_validate(
                {field: parsed.detail.get(field) for field in _ValidationSeedBinding.model_fields}
            )
        else:
            raise ValueError("simulation execution binding has no outer seed authority")
        if (
            simulation_execution.seed_binding != outer_seed
            or simulation_execution.seed_binding.case_id != getattr(parsed, "requirement_id", None)
        ):
            raise ValueError("simulation execution binding differs from outer seed authority")
        if (
            simulation_execution.constraint_snapshot_binding_status == "bound"
            and getattr(parsed, "status", None) != "unproven"
        ):
            raise ValueError("bound patch simulation cannot claim a proven verdict")
    if isinstance(
        parsed,
        (
            _RegressionFindingEvidence,
            _RegressionFindingBoundEvidence,
            _RegressionSuiteFindingEvidence,
            _RegressionSuiteFindingBoundEvidence,
            _RegressionSuiteFindingCaseSeedEvidence,
            _RegressionSuiteFindingCaseSeedBoundEvidence,
        ),
    ):
        _validate_findings_snapshot(
            snapshot_id=parsed.snapshot_id,
            findings=parsed.findings,
            label="regression evidence",
        )
    canonical = _restore_requirement_id(_require_exact_model(model, body), suite_requirement_id)
    if checker_profile is not None:
        canonical["checker_profile"] = checker_profile.model_dump(mode="json")
        canonical["checker_execution_bindings"] = [
            _json_mapping(item.model_dump(mode="json")) for item in checker_bindings
        ]
        canonical["constraint_snapshot_binding_status"] = checker_constraint_status
        if checker_constraint_id is not None:
            canonical["constraint_snapshot_artifact_id"] = checker_constraint_id
    if simulation_execution is not None:
        canonical["simulation_execution_binding"] = _json_mapping(
            simulation_execution.model_dump(mode="json", exclude_none=True)
        )
    if suite_coverage is not None:
        canonical["execution_coverage_binding"] = _json_mapping(
            suite_coverage.model_dump(mode="json", exclude_none=True)
        )
    if lineage_suite_present:
        canonical["lineage_suite_artifact_ids"] = list(lineage_suite_artifact_ids)
    if auto_apply_context is not None:
        canonical["auto_apply_context"] = _json_mapping(auto_apply_context.model_dump(mode="json"))
        canonical["oracle_attestations"] = [
            item.model_dump(mode="json") for item in oracle_attestations
        ]
        canonical["outcome_attestations"] = [
            item.model_dump(mode="json") for item in outcome_attestations
        ]
    if not _same_exact_json(canonical, payload):
        raise ValueError("regression evidence is not its exact canonical wire shape")
    return canonical


def _validate_review_report(payload: Mapping[str, object]) -> dict[str, object]:
    # The legacy ReviewReport model predates ConfigDict(extra="forbid").  Exact
    # canonical round-trip closes its outer and nested Finding shapes without
    # duplicating that public contract here.
    from gameforge.contracts.review import ReviewReport

    validation_payload, requirement_id = _split_requirement_id(payload)
    for field_name in (
        "deterministic_findings",
        "llm_assisted_findings",
        "simulation_findings",
        "unproven_findings",
    ):
        if field_name in validation_payload:
            validation_payload[field_name] = _decode_finding_confidences(
                validation_payload[field_name]
            )
    parsed = ReviewReport.model_validate(validation_payload)
    canonical = _json_mapping(parsed.model_dump(mode="json"))
    canonical = _restore_requirement_id(canonical, requirement_id)
    if not _same_exact_json(canonical, payload):
        raise ValueError("review report is not the exact canonical wire shape")
    buckets = {
        "deterministic": parsed.deterministic_findings,
        "llm": parsed.llm_assisted_findings,
        "simulation": parsed.simulation_findings,
        "unproven": parsed.unproven_findings,
    }
    all_findings = [item for values in buckets.values() for item in values]
    ids = [item.id for item in all_findings]
    if len(ids) != len(set(ids)):
        raise ValueError("review report finding ids must be unique across buckets")
    for bucket, values in buckets.items():
        for finding in values:
            expected = (
                "llm"
                if finding.oracle_type == "llm-assisted"
                else "unproven"
                if finding.status == "unproven"
                else "simulation"
                if finding.oracle_type == "simulation"
                else "deterministic"
            )
            if bucket != expected:
                raise ValueError("review report finding is in the wrong oracle bucket")
    expected_counts = Counter((item.defect_class, item.severity) for item in all_findings)
    count_identities = [(item.defect_class, item.severity) for item in parsed.by_defect_class]
    if len(count_identities) != len(set(count_identities)):
        raise ValueError("review report defect-class count identities must be unique")
    actual_counts = Counter(
        {identity: item.count for identity, item in zip(count_identities, parsed.by_defect_class)}
    )
    if actual_counts != expected_counts:
        raise ValueError("review report defect-class counts differ from its findings")
    return canonical


def _model_parser(model: type[BaseModel]) -> PayloadParser:
    return lambda payload: _require_exact_model(model, payload)


def _available(
    schema_id: str,
    discriminator_field: str,
    discriminator_values: str | tuple[str, ...],
    parser: PayloadParser,
) -> PayloadSchemaValidator:
    values = (
        (discriminator_values,) if isinstance(discriminator_values, str) else discriminator_values
    )
    return PayloadSchemaValidator(schema_id, discriminator_field, values, parser)


def _unavailable(schema_id: str, reason: str) -> PayloadSchemaValidator:
    return PayloadSchemaValidator(schema_id, None, (), None, reason)


_SCHEMA_VALIDATORS = {
    "source-raw@1": _unavailable(
        "source-raw@1", "source_raw is a dedicated raw UTF-8 byte payload"
    ),
    "agent-prompt-context@1": _available(
        "agent-prompt-context@1",
        "context_schema_version",
        "agent-prompt-context@1",
        _model_parser(AgentPromptContextV1),
    ),
    "source-rendered@1": _unavailable(
        "source-rendered@1",
        "source_rendered is validated as an exact model-router request by its runtime publisher",
    ),
    "ir-core@1": _available(
        "ir-core@1", "meta_schema_version", META_SCHEMA_VERSION, _validate_ir_snapshot
    ),
    "constraint-snapshot@1": _available(
        "constraint-snapshot@1",
        "dsl_grammar_version",
        DSL_GRAMMAR_VERSION,
        _validate_constraint_snapshot,
    ),
    "constraint-proposal@1": _available(
        "constraint-proposal@1",
        "proposal_schema_version",
        "constraint-proposal@1",
        _model_parser(ConstraintProposalV1),
    ),
    "config-export-package@1": _available(
        "config-export-package@1",
        "package_schema_version",
        "config-export-package@1",
        _model_parser(ConfigExportPackageV1),
    ),
    "scenario-spec@1": _available(
        "scenario-spec@1",
        "scenario_spec_schema_version",
        "scenario-spec@1",
        _model_parser(ScenarioSpecV1),
    ),
    "task-suite@1": _available(
        "task-suite@1",
        "task_suite_schema_version",
        "task-suite@1",
        _model_parser(TaskSuiteV1),
    ),
    "regression-suite@1": _unavailable(
        "regression-suite@1", "regression suite payload is owned by the injected game adapter"
    ),
    "golden-suite@1": _unavailable(
        "golden-suite@1", "golden suite payload is owned by the migration fixture adapter"
    ),
    "bench-dataset@1": _available(
        "bench-dataset@1",
        "benchmark_dataset_schema_version",
        "bench-dataset@1",
        _model_parser(BenchmarkDatasetV1),
    ),
    "benchmark-spec@1": _available(
        "benchmark-spec@1",
        "benchmark_spec_schema_version",
        "benchmark-spec@1",
        _model_parser(BenchmarkSpecV1),
    ),
    "review@1": _available(
        "review@1", "review_schema_version", "review@1", _validate_review_report
    ),
    "checker-report@1": _available(
        "checker-report@1",
        "payload_schema_version",
        "checker-report@1",
        _validate_checker_report,
    ),
    "simulation-result@1": _available(
        "simulation-result@1",
        "payload_schema_version",
        "simulation-result@1",
        _validate_simulation_result,
    ),
    "playtest-trace@1": _available(
        "playtest-trace@1",
        "playtest_trace_schema_version",
        "playtest-trace@1",
        _model_parser(PlaytestTraceV1),
    ),
    "patch@2": _available("patch@2", "patch_schema_version", "patch@2", _model_parser(PatchV2)),
    "auto-apply-proof@1": _available(
        "auto-apply-proof@1",
        "proof_schema_version",
        "auto-apply-proof@1",
        _model_parser(AutoApplyProofV1),
    ),
    "constraint-compile-evidence@1": _available(
        "constraint-compile-evidence@1",
        "evidence_schema_version",
        "constraint-compile-evidence@1",
        _model_parser(ConstraintCompileEvidenceV1),
    ),
    "evidence-set@1": _available(
        "evidence-set@1",
        "evidence_schema_version",
        "evidence-set@1",
        _model_parser(EvidenceSet),
    ),
    "regression-evidence@1": _available(
        "regression-evidence@1",
        "payload_schema_version",
        "regression-evidence@1",
        _validate_regression_evidence,
    ),
    "rollback-request@1": _available(
        "rollback-request@1",
        "rollback_schema_version",
        "rollback-request@1",
        _model_parser(RollbackRequestV1),
    ),
    "run-result@1": _available(
        "run-result@1",
        "result_schema_version",
        "run-result@1",
        _model_parser(RunResultV1),
    ),
    "run-failure@1": _available(
        "run-failure@1",
        "failure_schema_version",
        "run-failure@1",
        _model_parser(RunFailureV1),
    ),
    "cassette-bundle@1": _unavailable(
        "cassette-bundle@1",
        "cassette bundles require the runtime's scope, record, and retained-authority verifier",
    ),
    "cassette-record-shard@1": _unavailable(
        "cassette-record-shard@1",
        "cassette shards require the runtime's call-link and retained-authority verifier",
    ),
    "migration-report@1": _available(
        "migration-report@1",
        "report_schema_version",
        "migration-report@1",
        _model_parser(MigrationReportV1),
    ),
    "bench-report@2": _unavailable(
        "bench-report@2",
        "the strict BenchReport parser is app-owned and must be supplied as an external decoder",
    ),
    "backup-object-manifest@1": _unavailable(
        "backup-object-manifest@1", "backup object manifest has no retained payload contract"
    ),
    "dr-drill-evidence@1": _unavailable(
        "dr-drill-evidence@1", "DR drill evidence has no retained payload contract"
    ),
}

_DECLARED_SCHEMA_IDS = frozenset(
    schema_id for schema_ids in ARTIFACT_PAYLOAD_SCHEMAS.values() for schema_id in schema_ids
)
if frozenset(_SCHEMA_VALIDATORS) != _DECLARED_SCHEMA_IDS:  # pragma: no cover - import invariant
    missing = sorted(_DECLARED_SCHEMA_IDS - set(_SCHEMA_VALIDATORS))
    extra = sorted(set(_SCHEMA_VALIDATORS) - _DECLARED_SCHEMA_IDS)
    raise RuntimeError(
        f"artifact payload validator registry differs from declared schemas: "
        f"missing={missing}, extra={extra}"
    )

ARTIFACT_PAYLOAD_VALIDATORS: Mapping[str, PayloadSchemaValidator] = MappingProxyType(
    _SCHEMA_VALIDATORS
)
UNAVAILABLE_ARTIFACT_PAYLOAD_SCHEMAS: Mapping[str, str] = MappingProxyType(
    {
        schema_id: validator.unavailable_reason or "payload validator unavailable"
        for schema_id, validator in _SCHEMA_VALIDATORS.items()
        if not validator.is_available
    }
)


def _validate_json_bounds(
    payload: Mapping[str, object],
    *,
    payload_schema_id: str,
    canonical_size_bytes: int | None = None,
) -> None:
    stack: list[tuple[object, int]] = [(payload, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_PAYLOAD_JSON_DEPTH:
            raise IntegrityViolation("artifact payload exceeds the publication depth bound")
        if isinstance(item, str):
            if len(item) > MAX_PAYLOAD_STRING_LENGTH:
                raise IntegrityViolation("artifact payload contains an oversized string")
        elif isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise IntegrityViolation("artifact payload object key is not a string")
                if len(key) > 4096:
                    raise IntegrityViolation("artifact payload contains an oversized object key")
                stack.append((child, depth + 1))
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            stack.extend((child, depth + 1) for child in item)

    if canonical_size_bytes is None:
        try:
            encoded_size = len(canonical_json(dict(payload)).encode("utf-8"))
        except (RecursionError, TypeError, ValueError, UnicodeError) as exc:
            raise IntegrityViolation("artifact payload is not canonical JSON data") from exc
    else:
        encoded_size = canonical_size_bytes
    byte_limit = _payload_json_byte_limit(payload_schema_id)
    if encoded_size > byte_limit:
        raise IntegrityViolation(
            "artifact payload exceeds the publication byte bound",
            max_bytes=byte_limit,
        )


def _validate_artifact_payload(
    *,
    payload_schema_id: str,
    payload: Mapping[str, object],
    canonical_size_bytes: int | None,
) -> dict[str, object]:
    validator = ARTIFACT_PAYLOAD_VALIDATORS.get(payload_schema_id)
    if validator is None:
        raise IntegrityViolation(
            "artifact payload schema is not registered",
            payload_schema_id=payload_schema_id,
        )
    if not isinstance(payload, Mapping):
        raise IntegrityViolation("artifact payload must be a JSON object")
    _validate_json_bounds(
        payload,
        payload_schema_id=payload_schema_id,
        canonical_size_bytes=canonical_size_bytes,
    )
    if validator.parser is None:
        raise IntegrityViolation(
            "artifact payload schema is not valid on the terminal domain publication path",
            payload_schema_id=payload_schema_id,
            reason=validator.unavailable_reason,
        )

    discriminator_field = validator.discriminator_field
    if discriminator_field is None:  # pragma: no cover - available entries always declare one
        raise IntegrityViolation("artifact payload validator has no discriminator")
    discriminator = payload.get(discriminator_field)
    if discriminator is None:
        raise IntegrityViolation(
            "artifact payload discriminator is missing",
            payload_schema_id=payload_schema_id,
            discriminator_field=discriminator_field,
        )
    if discriminator not in validator.discriminator_values:
        raise IntegrityViolation(
            "artifact payload discriminator differs from its schema",
            payload_schema_id=payload_schema_id,
            discriminator_field=discriminator_field,
        )
    try:
        return validator.parser(payload)
    except IntegrityViolation:
        raise
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "artifact payload violates its exact registered schema",
            payload_schema_id=payload_schema_id,
        ) from exc


def validate_artifact_payload(
    *, payload_schema_id: str, payload: Mapping[str, object]
) -> dict[str, object]:
    """Return the canonical parsed mapping for one exact Artifact payload schema.

    Unknown schemas, explicit unavailable entries, missing/wrong discriminators,
    non-canonical/coercive model inputs, and any malformed nested payload fail
    closed as :class:`IntegrityViolation`.
    """

    return _validate_artifact_payload(
        payload_schema_id=payload_schema_id,
        payload=payload,
        canonical_size_bytes=None,
    )


def decode_and_validate_artifact_payload(
    *,
    payload_schema_id: str,
    blob: bytes,
    external_decoders: Mapping[str, PayloadBlobDecoder] | None = None,
) -> dict[str, object]:
    """Decode one prepared blob through its exact schema-owned wire format.

    Config exports use canonical binary framing. Other domain payloads use
    canonical UTF-8 JSON; duplicate keys, NaN/Infinity, non-object roots and a
    second non-canonical byte representation all fail before schema validation.
    """

    if not isinstance(blob, bytes):
        raise IntegrityViolation("prepared artifact blob must be bytes")
    blob_byte_limit = _prepared_blob_byte_limit(payload_schema_id)
    if len(blob) > blob_byte_limit:
        raise IntegrityViolation(
            "prepared artifact blob exceeds the publication byte bound",
            payload_schema_id=payload_schema_id,
            max_bytes=blob_byte_limit,
        )

    validator = ARTIFACT_PAYLOAD_VALIDATORS.get(payload_schema_id)
    decoder = (external_decoders or {}).get(payload_schema_id)
    if decoder is not None:
        if validator is None or validator.is_available:
            raise IntegrityViolation(
                "external payload decoder may bind only an explicitly deferred schema",
                payload_schema_id=payload_schema_id,
            )
        try:
            decoded_external = decoder(blob)
        except IntegrityViolation:
            raise
        except (TypeError, ValueError, ValidationError, UnicodeError) as exc:
            raise IntegrityViolation(
                "external payload codec rejected the exact prepared bytes",
                payload_schema_id=payload_schema_id,
            ) from exc
        if not isinstance(decoded_external, Mapping):
            raise IntegrityViolation(
                "external payload codec did not return a semantic mapping",
                payload_schema_id=payload_schema_id,
            )
        payload = dict(decoded_external)
        _validate_json_bounds(payload, payload_schema_id=payload_schema_id)
        return payload
    if validator is not None and not validator.is_available:
        raise IntegrityViolation(
            "artifact payload schema is not valid on the terminal domain publication path",
            payload_schema_id=payload_schema_id,
            reason=validator.unavailable_reason,
        )

    if payload_schema_id == "config-export-package@1":
        try:
            package = decode_config_export_bytes(blob)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise IntegrityViolation(
                "config export payload violates its canonical binary framing",
                payload_schema_id=payload_schema_id,
            ) from exc
        payload = _json_mapping(package.model_dump(mode="json"))
        return validate_artifact_payload(
            payload_schema_id=payload_schema_id,
            payload=payload,
        )

    try:
        decoded = json.loads(
            blob.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise IntegrityViolation(
            "prepared artifact blob is not strict UTF-8 JSON",
            payload_schema_id=payload_schema_id,
        ) from exc
    if not isinstance(decoded, dict):
        raise IntegrityViolation(
            "prepared artifact payload must be a JSON object",
            payload_schema_id=payload_schema_id,
        )
    try:
        canonical = canonical_json(decoded).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise IntegrityViolation(
            "prepared artifact payload is not canonical JSON data",
            payload_schema_id=payload_schema_id,
        ) from exc
    if canonical != blob:
        raise IntegrityViolation(
            "prepared artifact JSON bytes are not canonical",
            payload_schema_id=payload_schema_id,
        )
    return _validate_artifact_payload(
        payload_schema_id=payload_schema_id,
        payload=cast(dict[str, object], decoded),
        canonical_size_bytes=len(blob),
    )


def encode_validated_artifact_payload(
    *, payload_schema_id: str, payload: Mapping[str, object]
) -> bytes:
    """Encode a validated semantic mapping in its schema-owned wire format.

    This is the inverse used when the terminal publisher authoritatively re-seals
    a same-publication reference after the referenced Artifact's final identity is
    known.  It never turns config exports into JSON: their binary package framing
    (including raw file bytes) remains the one canonical wire representation.
    """

    canonical = validate_artifact_payload(
        payload_schema_id=payload_schema_id,
        payload=payload,
    )
    if payload_schema_id == "config-export-package@1":
        try:
            package = ConfigExportPackageV1.model_validate(canonical)
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityViolation(
                "validated config export mapping cannot be re-encoded",
                payload_schema_id=payload_schema_id,
            ) from exc
        blob = canonical_config_export_bytes(package)
    else:
        try:
            blob = canonical_json(canonical).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise IntegrityViolation(
                "validated Artifact mapping cannot be canonically encoded",
                payload_schema_id=payload_schema_id,
            ) from exc
    blob_byte_limit = _prepared_blob_byte_limit(payload_schema_id)
    if len(blob) > blob_byte_limit:
        raise IntegrityViolation(
            "encoded artifact blob exceeds the publication byte bound",
            payload_schema_id=payload_schema_id,
            max_bytes=blob_byte_limit,
        )
    return blob


__all__ = [
    "ARTIFACT_PAYLOAD_VALIDATORS",
    "MAX_PAYLOAD_COLLECTION_ITEMS",
    "MAX_PREPARED_ARTIFACT_BYTES",
    "MAX_PAYLOAD_JSON_BYTES",
    "MAX_PAYLOAD_JSON_DEPTH",
    "MAX_PAYLOAD_STRING_LENGTH",
    "PayloadSchemaValidator",
    "PayloadBlobDecoder",
    "UNAVAILABLE_ARTIFACT_PAYLOAD_SCHEMAS",
    "decode_and_validate_artifact_payload",
    "encode_validated_artifact_payload",
    "validate_artifact_payload",
]
