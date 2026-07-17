"""Production Patch auto-apply proof construction and exact guard adapters."""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from gameforge.contracts.auto_apply_ownership import auto_apply_ir_classifier_binding
from gameforge.contracts.canonical import sha256_lowerhex
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ExecutionProfileDefinitionV1,
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    ValidationProfileDetailsV1,
    execution_profile_payload_hash,
)
from gameforge.contracts.identity import DomainRegistryRefV1, DomainRegistryV1, DomainScope
from gameforge.contracts.jobs import (
    PatchValidationPayloadV1,
    ResolvedPolicyCountBindingV1,
    ResolvedPolicySubsetCountBindingV1,
    RunRecord,
)
from gameforge.contracts.lineage import ArtifactV2
from gameforge.contracts.playtest import PlaytestTraceV1, TaskSuiteV1
from gameforge.contracts.storage import RefValue, UtcClock
from gameforge.contracts.workflow import (
    AutoApplyOracleEvidenceBindingV1,
    AutoApplyOutcomeEvidenceBindingV1,
    AutoApplyPolicyRefV1,
    AutoApplyPolicyRegistryRefV1,
    AutoApplyPolicyRegistryV1,
    AutoApplyPolicyV1,
    AutoApplyProofV1,
    AutoApplyValidationProfileBindingV1,
    DeterministicOracleRegistryRefV1,
    DeterministicOracleRegistryV1,
    DeterministicOracleDefinitionV1,
    QualifiedOutcomeRuleRefV1,
    compute_auto_apply_policy_digest,
)
from gameforge.platform.approvals.auto_apply import (
    ResolvedArtifactPayload,
    is_auto_apply_candidate_eligible,
)
from gameforge.platform.approvals.auto_apply_runtime import (
    AutoApplyChangeAssessor,
    CanonicalIrAutoApplyChangeAssessor,
    ExactAutoApplyAuthority,
    ExactAutoApplyEligibilityRequest,
    ExactAutoApplyEligibilityService,
)
from gameforge.platform.publication.effects import (
    AutoApplyValidationRequest,
    AutoApplyValidationPort,
)
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.platform.run_handlers.patch_validation import (
    AutoApplyEvaluationRequest,
    AutoApplyEvidenceCandidate,
    AutoApplyPreparationRequest,
    AutoApplyQualificationPlan,
)
from gameforge.runtime.persistence.policies import SqlPolicySnapshotRepository


class AutoApplyPolicyRegistryResolver(Protocol):
    def resolve(self, ref: AutoApplyPolicyRegistryRefV1) -> AutoApplyPolicyRegistryV1 | None: ...


class DomainRegistryResolver(Protocol):
    def resolve(self, ref: DomainRegistryRefV1) -> DomainRegistryV1 | None: ...


class DeterministicOracleRegistryResolver(Protocol):
    def resolve(
        self, ref: DeterministicOracleRegistryRefV1
    ) -> DeterministicOracleRegistryV1 | None: ...


class AutoApplyArtifactReader(Protocol):
    def load_artifact(self, artifact_id: str) -> ArtifactV2: ...

    def read_bytes(self, artifact_id: str) -> bytes: ...

    def get_ref(self, ref_name: str) -> RefValue | None: ...


@dataclass(frozen=True, slots=True)
class SqlAutoApplyPolicyRegistryResolver:
    engine: Engine
    clock: UtcClock

    def resolve(self, ref: AutoApplyPolicyRegistryRefV1) -> AutoApplyPolicyRegistryV1 | None:
        with Session(self.engine) as session:
            return SqlPolicySnapshotRepository(
                session, clock=self.clock
            ).get_auto_apply_policy_registry(ref)


@dataclass(frozen=True, slots=True)
class SqlDomainRegistryResolver:
    engine: Engine
    clock: UtcClock

    def resolve(self, ref: DomainRegistryRefV1) -> DomainRegistryV1 | None:
        with Session(self.engine) as session:
            return SqlPolicySnapshotRepository(session, clock=self.clock).get_domain_registry(ref)


@dataclass(frozen=True, slots=True)
class SqlDeterministicOracleRegistryResolver:
    engine: Engine
    clock: UtcClock

    def resolve(
        self, ref: DeterministicOracleRegistryRefV1
    ) -> DeterministicOracleRegistryV1 | None:
        with Session(self.engine) as session:
            return SqlPolicySnapshotRepository(
                session, clock=self.clock
            ).get_deterministic_oracle_registry(ref)


class _UnavailableRegistryResolver:
    def resolve(self, ref: object) -> None:
        del ref
        return None


class _UnavailableArtifactReader:
    def load_artifact(self, artifact_id: str) -> ArtifactV2:
        raise IntegrityViolation(
            "auto-apply Artifact authority is unavailable", artifact_id=artifact_id
        )

    def read_bytes(self, artifact_id: str) -> bytes:
        raise IntegrityViolation(
            "auto-apply Artifact authority is unavailable", artifact_id=artifact_id
        )

    def get_ref(self, ref_name: str) -> RefValue | None:
        raise IntegrityViolation("auto-apply Ref authority is unavailable", ref_name=ref_name)


@dataclass(frozen=True, slots=True)
class RegistryResolvedAutoApplyEvaluator:
    """Construct a proof only from complete Run-frozen deterministic closure.

    This prepublication component never substitutes for eligibility validation.
    It resolves historical authority and constructs the exact proof projection;
    the same-UoW :class:`ExactAutoApplyEligibilityService` remains the sole final
    policy/subject/diff/evidence decision before workflow CAS.
    """

    profiles: ImmutablePlatformRegistry
    policy_registries: AutoApplyPolicyRegistryResolver
    domain_registries: DomainRegistryResolver = field(default_factory=_UnavailableRegistryResolver)
    oracle_registries: DeterministicOracleRegistryResolver = field(
        default_factory=_UnavailableRegistryResolver
    )
    artifacts: AutoApplyArtifactReader = field(default_factory=_UnavailableArtifactReader)
    change_assessor: AutoApplyChangeAssessor = field(
        default_factory=CanonicalIrAutoApplyChangeAssessor
    )

    def prepare(self, request: AutoApplyPreparationRequest) -> AutoApplyQualificationPlan | None:
        """Resolve immutable policy/executor/scope authority before evidence runs."""

        definition = _resolve_validation_profile(
            self.profiles,
            run=request.run,
            profile=request.validation_profile,
            expected_payload_hash=request.validation_profile_payload_hash,
        )
        details = definition.details
        assert isinstance(details, ValidationProfileDetailsV1)
        policy_ref = details.auto_apply_policy
        if policy_ref is None:
            return None
        params = _validate_preparation_request(request)
        policy_registry, policy = _resolve_policy_registry(self.policy_registries, policy_ref)
        domain_registry = _resolve_domain_registry(self.domain_registries, policy)
        oracle_registry = _resolve_oracle_registry(self.oracle_registries, policy)
        records = {
            artifact_id: _load_record(self.artifacts, artifact_id)
            for artifact_id in (
                params.base_snapshot_artifact_id,
                request.subject_artifact_id,
                request.target_binding.target_artifact_id,
            )
        }
        assessment = self.change_assessor.assess(
            base=records[params.base_snapshot_artifact_id],
            subject=records[request.subject_artifact_id],
            target=records[request.target_binding.target_artifact_id],
            domain_registry=domain_registry,
        )
        if request.run.resource_domain_scope != assessment.affected_domain_scope:
            raise IntegrityViolation(
                "auto-apply canonical diff scope differs from frozen Run authority"
            )
        if not is_auto_apply_candidate_eligible(
            subject=records[request.subject_artifact_id],
            target=records[request.target_binding.target_artifact_id],
            target_binding=request.target_binding,
            policy_ref=policy_ref,
            domain_registry=domain_registry,
            policy_registry=policy_registry,
            oracle_registry=oracle_registry,
            validation_profile=definition,
            change_assessment=assessment,
            current_ref=self.artifacts.get_ref(request.target_binding.ref_name),
        ):
            return None

        deterministic_oracles = _supported_oracle_definitions(
            policy=policy,
            registry=oracle_registry,
            params=params,
        )
        outcome_rules = _outcome_rules_by_requirement(
            policy=policy,
            run=request.run,
            validation_profile_payload_hash=request.validation_profile_payload_hash,
        )
        scopes = _evaluated_scopes_by_requirement(
            artifacts=self.artifacts,
            params=params,
            affected_scope=assessment.affected_domain_scope,
        )
        return AutoApplyQualificationPlan(
            policy=policy_ref,
            affected_domain_scope=assessment.affected_domain_scope,
            deterministic_oracles=deterministic_oracles,
            outcome_rules_by_requirement=outcome_rules,
            evaluated_scopes_by_requirement=scopes,
        )

    def evaluate(self, request: AutoApplyEvaluationRequest) -> AutoApplyProofV1 | None:
        definition = _resolve_validation_profile(
            self.profiles,
            run=request.run,
            profile=request.validation_profile,
            expected_payload_hash=request.validation_profile_payload_hash,
        )
        details = definition.details
        assert isinstance(details, ValidationProfileDetailsV1)
        policy_ref = details.auto_apply_policy
        if policy_ref is None:
            return None

        params = request.run.payload.params
        if (
            request.run.kind != RunKindRef(kind="patch.validate", version=1)
            or not isinstance(params, PatchValidationPayloadV1)
            or params.subject.subject_artifact_id != request.subject_artifact_id
            or params.subject.subject_digest != request.subject_digest
            or params.preview_snapshot_artifact_id != request.target_binding.target_artifact_id
            or params.target.ref_name != request.target_binding.ref_name
            or params.target.expected_ref != request.target_binding.expected_ref
        ):
            raise IntegrityViolation("auto-apply evaluation request differs from its frozen Run")
        bindings = tuple(
            binding
            for binding in request.run.payload.resolved_profiles
            if binding.field_path == "/params/validation_policy"
        )
        if (
            len(bindings) != 1
            or bindings[0].profile != request.validation_profile
            or bindings[0].profile_payload_hash != request.validation_profile_payload_hash
        ):
            raise IntegrityViolation("auto-apply Run lacks its exact validation profile binding")
        if request.run.resource_domain_scope is None:
            raise IntegrityViolation("auto-apply Run lacks an exact resource domain scope")

        policy_registry, policy = _resolve_policy_registry(self.policy_registries, policy_ref)
        domain_registry = self.domain_registries.resolve(policy.domain_registry)
        if (
            domain_registry is None
            or DomainRegistryRefV1(
                registry_version=domain_registry.registry_version,
                registry_digest=domain_registry.registry_digest,
            )
            != policy.domain_registry
        ):
            raise IntegrityViolation("auto-apply domain registry history is unavailable")
        oracle_registry = self.oracle_registries.resolve(policy.deterministic_oracle_registry)
        if (
            oracle_registry is None
            or DeterministicOracleRegistryRefV1(
                registry_version=oracle_registry.registry_version,
                registry_digest=oracle_registry.registry_digest,
            )
            != policy.deterministic_oracle_registry
        ):
            raise IntegrityViolation(
                "auto-apply deterministic oracle registry history is unavailable"
            )

        candidates = _exact_candidates(request)
        records = {
            artifact_id: _load_record(self.artifacts, artifact_id)
            for artifact_id in (
                params.base_snapshot_artifact_id,
                request.subject_artifact_id,
                request.target_binding.target_artifact_id,
            )
        }
        assessment = self.change_assessor.assess(
            base=records[params.base_snapshot_artifact_id],
            subject=records[request.subject_artifact_id],
            target=records[request.target_binding.target_artifact_id],
            domain_registry=domain_registry,
        )
        if assessment.affected_domain_scope != request.run.resource_domain_scope:
            raise IntegrityViolation(
                "auto-apply canonical diff scope differs from frozen Run authority"
            )
        known_domains = {item.domain_id for item in domain_registry.definitions}
        if not set(assessment.affected_domain_scope.domain_ids).issubset(known_domains):
            raise IntegrityViolation("auto-apply assessment references an unknown domain")
        if not is_auto_apply_candidate_eligible(
            subject=records[request.subject_artifact_id],
            target=records[request.target_binding.target_artifact_id],
            target_binding=request.target_binding,
            policy_ref=policy_ref,
            domain_registry=domain_registry,
            policy_registry=policy_registry,
            oracle_registry=oracle_registry,
            validation_profile=definition,
            change_assessment=assessment,
            current_ref=self.artifacts.get_ref(request.target_binding.ref_name),
        ):
            return None

        oracle_bindings = _oracle_bindings(
            policy=policy,
            registry=oracle_registry,
            candidates=candidates,
            affected_scope=assessment.affected_domain_scope,
            params=params,
        )
        if oracle_bindings is None:
            return None
        outcome_bindings = _outcome_bindings(
            policy=policy,
            request=request,
            candidates=candidates,
        )
        if outcome_bindings is None:
            return None

        return AutoApplyProofV1(
            subject_artifact_id=request.subject_artifact_id,
            subject_digest=request.subject_digest,
            target_binding=request.target_binding,
            affected_domain_scope=assessment.affected_domain_scope,
            validation_evidence_artifact_id=request.validation_evidence_artifact_id,
            regression_evidence_artifact_ids=request.regression_evidence_artifact_ids,
            validation_profile_binding=AutoApplyValidationProfileBindingV1(
                validation_profile=request.validation_profile,
                validation_profile_payload_hash=request.validation_profile_payload_hash,
                policy=policy_ref,
            ),
            deterministic_oracle_evidence=oracle_bindings,
            required_outcome_evidence=outcome_bindings,
            policy=policy_ref,
        )


def _validate_preparation_request(
    request: AutoApplyPreparationRequest,
) -> PatchValidationPayloadV1:
    params = request.run.payload.params
    if (
        request.run.kind != RunKindRef(kind="patch.validate", version=1)
        or not isinstance(params, PatchValidationPayloadV1)
        or params.subject.subject_artifact_id != request.subject_artifact_id
        or params.subject.subject_digest != request.subject_digest
        or params.preview_snapshot_artifact_id != request.target_binding.target_artifact_id
        or params.target.ref_name != request.target_binding.ref_name
        or params.target.expected_ref != request.target_binding.expected_ref
    ):
        raise IntegrityViolation("auto-apply preparation differs from its frozen Run")
    bindings = tuple(
        binding
        for binding in request.run.payload.resolved_profiles
        if binding.field_path == "/params/validation_policy"
    )
    if (
        len(bindings) != 1
        or bindings[0].profile != request.validation_profile
        or bindings[0].profile_payload_hash != request.validation_profile_payload_hash
    ):
        raise IntegrityViolation("auto-apply Run lacks its exact validation profile binding")
    if request.run.resource_domain_scope is None:
        raise IntegrityViolation("auto-apply Run lacks an exact resource domain scope")
    return params


def _resolve_domain_registry(
    resolver: DomainRegistryResolver,
    policy: AutoApplyPolicyV1,
) -> DomainRegistryV1:
    registry = resolver.resolve(policy.domain_registry)
    if (
        registry is None
        or DomainRegistryRefV1(
            registry_version=registry.registry_version,
            registry_digest=registry.registry_digest,
        )
        != policy.domain_registry
    ):
        raise IntegrityViolation("auto-apply domain registry history is unavailable")
    return registry


def _resolve_oracle_registry(
    resolver: DeterministicOracleRegistryResolver,
    policy: AutoApplyPolicyV1,
) -> DeterministicOracleRegistryV1:
    registry = resolver.resolve(policy.deterministic_oracle_registry)
    if (
        registry is None
        or DeterministicOracleRegistryRefV1(
            registry_version=registry.registry_version,
            registry_digest=registry.registry_digest,
        )
        != policy.deterministic_oracle_registry
    ):
        raise IntegrityViolation("auto-apply deterministic oracle registry history is unavailable")
    return registry


def _supported_oracle_definitions(
    *,
    policy: AutoApplyPolicyV1,
    registry: DeterministicOracleRegistryV1,
    params: PatchValidationPayloadV1,
) -> tuple[DeterministicOracleDefinitionV1, ...]:
    definitions = {(item.oracle_id, item.oracle_version): item for item in registry.definitions}
    supported: list[DeterministicOracleDefinitionV1] = []
    seen_executors: set[str] = set()
    for oracle in policy.required_deterministic_oracles:
        definition = definitions.get((oracle.oracle_id, oracle.oracle_version))
        if definition is None or definition.oracle_digest != oracle.oracle_digest:
            raise IntegrityViolation("auto-apply oracle ref does not resolve exactly")
        _validate_supported_oracle_definition(definition, policy)
        executable = (
            bool(params.checker_profiles)
            if definition.engine_kind in {"graph", "asp", "smt"}
            else bool(params.simulation_profiles)
            if definition.engine_kind == "simulation"
            else bool(params.playtest_trace_artifact_ids)
        )
        native = _native_engine_id(definition.engine_kind)
        if not executable or native in seen_executors:
            raise IntegrityViolation(
                "configured auto-apply oracle lacks one supported exact executor binding",
                oracle_id=definition.oracle_id,
                oracle_version=definition.oracle_version,
            )
        seen_executors.add(native)
        supported.append(definition)
    return tuple(supported)


def _validate_supported_oracle_definition(
    definition: DeterministicOracleDefinitionV1,
    policy: AutoApplyPolicyV1,
) -> None:
    expected_tool = {
        "graph": "checker@1",
        "asp": "checker@1",
        "smt": "checker@1",
        "simulation": "economy-sim@1",
        "playtest_completion": "playtest@1",
    }[definition.engine_kind]
    if (
        definition.domain_registry != policy.domain_registry
        or definition.tool_version != expected_tool
        or definition.predicate_schema_id != "gameforge-dimension-status@1"
        or "regression_evidence" not in definition.evidence_artifact_kinds
        or "regression-evidence@1" not in definition.evidence_payload_schema_ids
    ):
        raise IntegrityViolation(
            "configured auto-apply oracle lacks one supported exact executor binding",
            oracle_id=definition.oracle_id,
            oracle_version=definition.oracle_version,
        )


def _outcome_rules_by_requirement(
    *,
    policy: AutoApplyPolicyV1,
    run: RunRecord,
    validation_profile_payload_hash: str,
) -> tuple[tuple[str, tuple[QualifiedOutcomeRuleRefV1, ...]], ...]:
    snapshots = {item.resolved_policy_id: item for item in run.payload.resolved_policy_snapshots}
    by_requirement: dict[str, list[QualifiedOutcomeRuleRefV1]] = {}
    for rule in policy.required_outcome_rules:
        snapshot = snapshots.get(rule.resolved_policy_id)
        if snapshot is None:
            raise IntegrityViolation("auto-apply outcome policy history is unavailable")
        if (
            snapshot.source_profile_field_path != "/params/validation_policy"
            or snapshot.source_profile_payload_hash != validation_profile_payload_hash
        ):
            raise IntegrityViolation(
                "auto-apply outcome policy differs from frozen validation profile"
            )
        requirements = tuple(
            item for item in snapshot.requirements if item.outcome_rule_id == rule.outcome_rule_id
        )
        if not requirements:
            raise IntegrityViolation("auto-apply outcome rule resolves no requirements")
        for requirement in requirements:
            if requirement.requirement_id in by_requirement:
                raise IntegrityViolation(
                    "auto-apply outcome requirements repeat an EvidenceSet identity"
                )
            by_requirement[requirement.requirement_id] = [rule]
    return tuple(
        (requirement_id, tuple(rules)) for requirement_id, rules in sorted(by_requirement.items())
    )


def _evaluated_scopes_by_requirement(
    *,
    artifacts: AutoApplyArtifactReader,
    params: PatchValidationPayloadV1,
    affected_scope: DomainScope,
) -> tuple[tuple[str, DomainScope], ...]:
    scopes: dict[str, DomainScope] = {}
    # Built-in checker/simulation executors consume the complete exact preview;
    # the admitted Run scope is therefore their actual evaluated scope.
    for profile in params.checker_profiles:
        scopes[f"checker:{profile.profile_id}@{profile.version}"] = affected_scope
    for profile in params.simulation_profiles:
        scopes[f"simulation:{profile.profile_id}@{profile.version}"] = affected_scope
    for suite_id in params.regression_suite_artifact_ids:
        scope = _retained_artifact_scope(artifacts, suite_id, expected_kind="regression_suite")
        if scope == affected_scope:
            scopes[f"regression:{suite_id}"] = scope
    for trace_id in params.playtest_trace_artifact_ids:
        scope = _playtest_trace_scope(artifacts, trace_id)
        if scope == affected_scope:
            scopes[f"playtest:{trace_id}"] = scope
    return tuple(sorted(scopes.items()))


def _retained_artifact_scope(
    artifacts: AutoApplyArtifactReader,
    artifact_id: str,
    *,
    expected_kind: str,
) -> DomainScope | None:
    try:
        artifact = artifacts.load_artifact(artifact_id)
        if artifact.kind != expected_kind:
            return None
        raw = artifact.meta.get("domain_scope")
        scope = DomainScope.model_validate(raw)
        if raw != scope.model_dump(mode="json"):
            return None
        return scope
    except (KeyError, TypeError, ValueError, IntegrityViolation):
        return None


def _playtest_trace_scope(
    artifacts: AutoApplyArtifactReader,
    trace_id: str,
) -> DomainScope | None:
    try:
        trace_record = _load_record(artifacts, trace_id)
        if (
            trace_record.artifact.kind != "playtest_trace"
            or trace_record.payload_schema_id != "playtest-trace@1"
        ):
            return None
        trace = PlaytestTraceV1.model_validate(_record_payload(trace_record))
        suite_record = _load_record(artifacts, trace.task_suite_artifact_id)
        if (
            suite_record.artifact.kind != "task_suite"
            or suite_record.payload_schema_id != "task-suite@1"
            or trace.task_suite_artifact_id not in trace_record.artifact.lineage
        ):
            return None
        suite = TaskSuiteV1.model_validate(_record_payload(suite_record))
        suite_episodes = {
            (item.episode_id, item.scenario_spec_artifact_id): item for item in suite.episodes
        }
        selected = tuple(
            suite_episodes[(item.episode_id, item.scenario_spec_artifact_id)]
            for item in trace.episodes
        )
        if any(
            item.scenario_spec_artifact_id not in trace_record.artifact.lineage
            for item in trace.episodes
        ):
            return None
        return DomainScope(
            domain_ids=tuple(
                sorted(
                    {
                        domain_id
                        for episode in selected
                        for domain_id in episode.domain_scope.domain_ids
                    }
                )
            )
        )
    except (KeyError, TypeError, ValueError, IntegrityViolation):
        return None


def _record_payload(record: ResolvedArtifactPayload) -> dict[str, object]:
    if len(
        record.payload_bytes
    ) != record.artifact.object_ref.size_bytes or not hmac.compare_digest(
        sha256_lowerhex(record.payload_bytes), record.artifact.payload_hash
    ):
        raise IntegrityViolation("retained auto-apply input bytes differ from Artifact")
    value = json.loads(record.payload_bytes)
    if not isinstance(value, dict):
        raise ValueError("retained auto-apply input is not an object")
    return value


def _native_engine_id(engine_kind: str) -> str:
    return {
        "graph": "graph",
        "asp": "asp",
        "smt": "smt",
        "simulation": "economy_sim",
        "playtest_completion": "playtest_completion",
    }[engine_kind]


def _load_record(reader: AutoApplyArtifactReader, artifact_id: str) -> ResolvedArtifactPayload:
    artifact = reader.load_artifact(artifact_id)
    if not isinstance(artifact, ArtifactV2) or artifact.artifact_id != artifact_id:
        raise IntegrityViolation(
            "auto-apply Artifact authority returned another envelope",
            artifact_id=artifact_id,
        )
    schema = artifact.meta.get("payload_schema_id")
    if not isinstance(schema, str) or not schema:
        raise IntegrityViolation(
            "auto-apply Artifact has no exact payload schema", artifact_id=artifact_id
        )
    return ResolvedArtifactPayload(
        artifact=artifact,
        payload_schema_id=schema,
        payload_bytes=reader.read_bytes(artifact_id),
    )


def _exact_candidates(
    request: AutoApplyEvaluationRequest,
) -> dict[str, AutoApplyEvidenceCandidate]:
    candidates = request.evidence_candidates
    requirement_ids = tuple(item.requirement.requirement_id for item in candidates)
    if len(requirement_ids) != len(set(requirement_ids)):
        raise IntegrityViolation("auto-apply evidence candidates repeat a requirement")
    by_requirement = {item.requirement.requirement_id: item for item in candidates}
    if tuple(item.requirement for item in candidates) != request.requirements:
        raise IntegrityViolation("auto-apply candidate requirements differ from EvidenceSet")
    for candidate in candidates:
        requirement = candidate.requirement
        if (
            requirement.evidence_artifact_id != candidate.artifact_id
            or requirement.kind != "regression"
            or requirement.applicability != "required"
            or requirement.status != "passed"
            or not candidate.payload_hash
            or request.subject_artifact_id not in candidate.direct_parent_artifact_ids
            or request.target_binding.target_artifact_id not in candidate.direct_parent_artifact_ids
        ):
            raise IntegrityViolation(
                "auto-apply evidence candidate is not exact passed deterministic evidence",
                requirement_id=requirement.requirement_id,
            )
    candidate_ids = tuple(sorted(item.artifact_id for item in candidates))
    if candidate_ids != tuple(sorted(request.regression_evidence_artifact_ids)):
        raise IntegrityViolation(
            "auto-apply evidence candidates differ from complete regression closure"
        )
    return by_requirement


def _oracle_bindings(
    *,
    policy: AutoApplyPolicyV1,
    registry: DeterministicOracleRegistryV1,
    candidates: dict[str, AutoApplyEvidenceCandidate],
    affected_scope: DomainScope,
    params: PatchValidationPayloadV1,
) -> tuple[AutoApplyOracleEvidenceBindingV1, ...] | None:
    definitions = {(item.oracle_id, item.oracle_version): item for item in registry.definitions}
    bindings: list[AutoApplyOracleEvidenceBindingV1] = []
    used_artifact_ids: set[str] = set()
    for oracle in policy.required_deterministic_oracles:
        definition = definitions.get((oracle.oracle_id, oracle.oracle_version))
        if definition is None or definition.oracle_digest != oracle.oracle_digest:
            raise IntegrityViolation("auto-apply oracle ref does not resolve exactly")
        matches = tuple(
            (candidate, attestation)
            for candidate in candidates.values()
            for attestation in candidate.oracle_attestations
            if attestation.oracle == oracle
            and candidate.requirement.tool_version == definition.tool_version
            and attestation.engine_kind == definition.engine_kind
            and attestation.engine_id == _native_engine_id(definition.engine_kind)
            and attestation.tool_version == definition.tool_version
            and attestation.predicate_schema_id == definition.predicate_schema_id
            and attestation.evaluated_domain_scope == affected_scope
            and attestation.verdict == "passed"
            and attestation.verdict_authority == "deterministic"
            and attestation.direct_parent_artifact_ids == candidate.direct_parent_artifact_ids
            and attestation.predicate
            == {
                "kind": "dimension_status",
                "requirement_id": candidate.requirement.requirement_id,
                "engine_id": attestation.engine_id,
                "engine_version": attestation.engine_version,
                "status": "passed",
            }
            and _attested_executor_version_is_frozen(
                candidate=candidate,
                engine_kind=attestation.engine_kind,
                engine_version=attestation.engine_version,
                params=params,
            )
        )
        if len(matches) != 1 or matches[0][0].artifact_id in used_artifact_ids:
            return None
        candidate, _ = matches[0]
        used_artifact_ids.add(candidate.artifact_id)
        bindings.append(
            AutoApplyOracleEvidenceBindingV1(
                oracle=oracle,
                evaluated_domain_scope=affected_scope,
                evidence_artifact_id=candidate.artifact_id,
                evidence_payload_hash=candidate.payload_hash,
            )
        )
    return tuple(bindings)


def _attested_executor_version_is_frozen(
    *,
    candidate: AutoApplyEvidenceCandidate,
    engine_kind: str,
    engine_version: str,
    params: PatchValidationPayloadV1,
) -> bool:
    requirement_id = candidate.requirement.requirement_id
    if engine_kind in {"graph", "asp", "smt"}:
        return any(
            requirement_id == f"checker:{profile.profile_id}@{profile.version}"
            and engine_version == str(profile.version)
            for profile in params.checker_profiles
        )
    if engine_kind == "simulation":
        return any(
            requirement_id == f"simulation:{profile.profile_id}@{profile.version}"
            and engine_version == str(profile.version)
            for profile in params.simulation_profiles
        )
    if engine_kind == "playtest_completion":
        return (
            engine_version == "1"
            and requirement_id.startswith("playtest:")
            and requirement_id.removeprefix("playtest:") in params.playtest_trace_artifact_ids
        )
    return False


def _outcome_bindings(
    *,
    policy: AutoApplyPolicyV1,
    request: AutoApplyEvaluationRequest,
    candidates: dict[str, AutoApplyEvidenceCandidate],
) -> tuple[AutoApplyOutcomeEvidenceBindingV1, ...] | None:
    snapshots = {
        item.resolved_policy_id: item for item in request.run.payload.resolved_policy_snapshots
    }
    bindings: list[AutoApplyOutcomeEvidenceBindingV1] = []
    for rule in policy.required_outcome_rules:
        snapshot = snapshots.get(rule.resolved_policy_id)
        if snapshot is None:
            raise IntegrityViolation("auto-apply outcome policy history is unavailable")
        if (
            snapshot.source_profile_field_path != "/params/validation_policy"
            or snapshot.source_profile_payload_hash != request.validation_profile_payload_hash
        ):
            raise IntegrityViolation(
                "auto-apply outcome policy differs from frozen validation profile"
            )
        requirements = tuple(
            item for item in snapshot.requirements if item.outcome_rule_id == rule.outcome_rule_id
        )
        if not requirements:
            raise IntegrityViolation("auto-apply outcome rule resolves no requirements")
        for requirement in requirements:
            candidate = candidates.get(requirement.requirement_id)
            if (
                candidate is None
                or requirement.artifact_kind != "regression_evidence"
                or requirement.payload_schema_id != "regression-evidence@1"
            ):
                return None
            attestations = tuple(
                item
                for item in candidate.outcome_attestations
                if item.rule == rule and item.requirement_id == requirement.requirement_id
            )
            if len(attestations) != 1:
                return None
            attestation = attestations[0]
            if (
                attestation.evaluated_domain_scope != request.run.resource_domain_scope
                or attestation.verdict != "passed"
                or attestation.verdict_authority != "deterministic"
                or attestation.direct_parent_artifact_ids != candidate.direct_parent_artifact_ids
            ):
                return None
            bindings.append(
                AutoApplyOutcomeEvidenceBindingV1(
                    rule=rule,
                    requirement_id=requirement.requirement_id,
                    evidence_artifact_id=candidate.artifact_id,
                    evidence_payload_hash=candidate.payload_hash,
                )
            )
    if {item.evidence_artifact_id for item in bindings} != set(
        request.regression_evidence_artifact_ids
    ):
        return None
    return tuple(bindings)


@dataclass(frozen=True, slots=True)
class TransactionBoundAutoApplyAuthority(ExactAutoApplyAuthority):
    transaction: object
    object_store: object
    profiles: ImmutablePlatformRegistry

    def load_artifact(self, artifact_id: str) -> ResolvedArtifactPayload:
        artifact = self.transaction.artifacts.get(artifact_id)  # type: ignore[attr-defined]
        if not isinstance(artifact, ArtifactV2):
            raise IntegrityViolation(
                "auto-apply retained Artifact is unavailable", artifact_id=artifact_id
            )
        binding = self.transaction.object_bindings.resolve(  # type: ignore[attr-defined]
            artifact.object_ref
        )
        stat = self.object_store.stat(binding.location)  # type: ignore[attr-defined]
        if stat.ref != artifact.object_ref or stat.location != binding.location:
            raise IntegrityViolation(
                "auto-apply ObjectBinding differs from retained Artifact",
                artifact_id=artifact_id,
            )
        with self.object_store.open(binding.location) as stream:  # type: ignore[attr-defined]
            payload = stream.read()
        schema = artifact.meta.get("payload_schema_id")
        if not isinstance(schema, str) or not schema:
            raise IntegrityViolation(
                "auto-apply Artifact payload schema is unavailable",
                artifact_id=artifact_id,
            )
        return ResolvedArtifactPayload(
            artifact=artifact,
            payload_schema_id=schema,
            payload_bytes=payload,
        )

    def get_domain_registry(self, ref: DomainRegistryRefV1) -> DomainRegistryV1 | None:
        return self.transaction.policies.get_domain_registry(ref)  # type: ignore[attr-defined,no-any-return]

    def get_auto_apply_policy_registry(
        self, ref: AutoApplyPolicyRegistryRefV1
    ) -> AutoApplyPolicyRegistryV1 | None:
        return self.transaction.policies.get_auto_apply_policy_registry(ref)  # type: ignore[attr-defined,no-any-return]

    def get_deterministic_oracle_registry(
        self, ref: DeterministicOracleRegistryRefV1
    ) -> DeterministicOracleRegistryV1 | None:
        return self.transaction.policies.get_deterministic_oracle_registry(ref)  # type: ignore[attr-defined,no-any-return]

    def resolve_execution_profile(
        self, binding: ResolvedExecutionProfileBindingV1
    ) -> ExecutionProfileDefinitionV1 | None:
        try:
            definition, lifecycle = self.profiles.resolve_execution_profile_binding(binding)
        except IntegrityViolation:
            return None
        if lifecycle.state != "active":
            return None
        return definition

    def get_ref(self, ref_name: str):
        return self.transaction.refs.get(ref_name)  # type: ignore[attr-defined,no-any-return]


@dataclass(frozen=True, slots=True)
class GuardedAutoApplyValidationPort(AutoApplyValidationPort):
    eligibility: ExactAutoApplyEligibilityService

    def validate_completion(self, request: AutoApplyValidationRequest) -> None:
        if (
            request.proof_artifact_id != request.proof_artifact.artifact_id
            or request.evidence_artifact_id != request.evidence_artifact.artifact_id
            or request.projected_item.auto_apply_proof is None
            or request.projected_item.auto_apply_proof.proof_artifact_id
            != request.proof_artifact_id
            or request.proof
            != AutoApplyProofV1.model_validate(request.proof.model_dump(mode="python"))
        ):
            raise IntegrityViolation(
                "terminal auto-apply request differs from final publication authority"
            )
        self.eligibility.validate_eligibility(
            ExactAutoApplyEligibilityRequest(
                run=request.run,
                item=request.projected_item,
                outcome_code=request.policy.outcome_code,
                proof_artifact_id=request.proof_artifact_id,
                evidence_set_artifact_id=request.evidence_artifact_id,
            )
        )


def build_transaction_auto_apply_validation_port(
    *,
    transaction: object,
    object_store: object,
    registry: ImmutablePlatformRegistry,
) -> GuardedAutoApplyValidationPort:
    authority = TransactionBoundAutoApplyAuthority(
        transaction=transaction,
        object_store=object_store,
        profiles=registry,
    )
    return GuardedAutoApplyValidationPort(ExactAutoApplyEligibilityService(authority=authority))


def ensure_worker_auto_apply_catalog_supported(
    registry: ImmutablePlatformRegistry,
    *,
    policy_registries: AutoApplyPolicyRegistryResolver | None = None,
    domain_registries: DomainRegistryResolver | None = None,
    oracle_registries: DeterministicOracleRegistryResolver | None = None,
) -> None:
    """Close every configured retained profile against exact registry history."""

    for definition in _unique_validation_definitions(registry):
        details = definition.details
        if not isinstance(details, ValidationProfileDetailsV1):
            raise IntegrityViolation("retained validation profile lacks exact details")
        policy_ref = details.auto_apply_policy
        if policy_ref is None:
            continue
        if policy_registries is None:
            raise IntegrityViolation("configured auto-apply policy registry is unavailable")
        policy = _resolve_policy(policy_registries, policy_ref)
        if domain_registries is None:
            raise IntegrityViolation("configured auto-apply domain registry is unavailable")
        domain_registry = _resolve_domain_registry(domain_registries, policy)
        try:
            ownership = auto_apply_ir_classifier_binding(domain_registry).ownership
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "configured auto-apply domain ownership tags are invalid"
            ) from exc
        if not ownership.complete:
            raise IntegrityViolation("configured auto-apply domain ownership is incomplete")
        if oracle_registries is None:
            raise IntegrityViolation(
                "configured auto-apply deterministic oracle registry is unavailable"
            )
        oracle_registry = _resolve_oracle_registry(oracle_registries, policy)
        definitions = {
            (item.oracle_id, item.oracle_version): item for item in oracle_registry.definitions
        }
        seen_executors: set[str] = set()
        for oracle in policy.required_deterministic_oracles:
            resolved = definitions.get((oracle.oracle_id, oracle.oracle_version))
            if resolved is None or resolved.oracle_digest != oracle.oracle_digest:
                raise IntegrityViolation("configured auto-apply oracle does not resolve exactly")
            _validate_supported_oracle_definition(resolved, policy)
            native = _native_engine_id(resolved.engine_kind)
            if native in seen_executors:
                raise IntegrityViolation(
                    "configured auto-apply oracle executor mapping is ambiguous"
                )
            seen_executors.add(native)
        _validate_auto_apply_outcome_rule_coverage(
            registry=registry,
            definition=definition,
            policy=policy,
        )


def _validate_auto_apply_outcome_rule_coverage(
    *,
    registry: ImmutablePlatformRegistry,
    definition: ExecutionProfileDefinitionV1,
    policy: AutoApplyPolicyV1,
) -> None:
    policies = []
    for run_kind in definition.compatible_run_kinds:
        retained = registry.get_run_kind(run_kind)
        if retained is None:
            raise IntegrityViolation("configured auto-apply compatible Run kind is unavailable")
        policies.extend(
            candidate
            for candidate in retained.outcome_policies
            if candidate.outcome_code == "patch_validation_auto_eligible"
            and candidate.prepared_outcome == "success"
            and candidate.publication_scope == "run"
            and candidate.run_status_after_publication == "succeeded"
        )
    if len(policies) != 1:
        raise IntegrityViolation(
            "configured auto-apply outcome publication policy does not resolve exactly"
        )
    covered: list[tuple[str, str]] = []
    for rule in policies[0].artifact_rules:
        binding = rule.count_binding
        if isinstance(
            binding,
            (ResolvedPolicyCountBindingV1, ResolvedPolicySubsetCountBindingV1),
        ):
            covered.append((binding.resolved_policy_id, binding.outcome_rule_id))
    required = [
        (rule.resolved_policy_id, rule.outcome_rule_id) for rule in policy.required_outcome_rules
    ]
    if any(covered.count(identity) != 1 for identity in required):
        raise IntegrityViolation(
            "configured auto-apply outcome rules lack exact publication coverage"
        )


def build_worker_auto_apply_evaluator(
    *,
    registry: ImmutablePlatformRegistry,
    engine: Engine,
    clock: UtcClock,
    artifacts: AutoApplyArtifactReader | None = None,
) -> RegistryResolvedAutoApplyEvaluator:
    policies = SqlAutoApplyPolicyRegistryResolver(engine=engine, clock=clock)
    domains = SqlDomainRegistryResolver(engine=engine, clock=clock)
    oracles = SqlDeterministicOracleRegistryResolver(engine=engine, clock=clock)
    ensure_worker_auto_apply_catalog_supported(
        registry,
        policy_registries=policies,
        domain_registries=domains,
        oracle_registries=oracles,
    )
    return RegistryResolvedAutoApplyEvaluator(
        profiles=registry,
        policy_registries=policies,
        domain_registries=domains,
        oracle_registries=oracles,
        artifacts=artifacts or _UnavailableArtifactReader(),
    )


def _unique_validation_definitions(
    registry: ImmutablePlatformRegistry,
) -> tuple[ExecutionProfileDefinitionV1, ...]:
    definitions: list[ExecutionProfileDefinitionV1] = []
    for catalog in registry.list_execution_profile_catalogs():
        for definition in catalog.definitions:
            if definition.profile_kind != "validation" or definition in definitions:
                continue
            definitions.append(definition)
    return tuple(
        sorted(
            definitions,
            key=lambda definition: (
                definition.profile.profile_id,
                definition.profile.version,
                execution_profile_payload_hash(definition),
            ),
        )
    )


def _resolve_validation_profile(
    registry: ImmutablePlatformRegistry,
    *,
    run: RunRecord,
    profile: ProfileRefV1,
    expected_payload_hash: str,
) -> ExecutionProfileDefinitionV1:
    bindings = tuple(
        binding
        for binding in run.payload.resolved_profiles
        if binding.field_path == "/params/validation_policy"
    )
    if (
        len(bindings) != 1
        or bindings[0].profile != profile
        or bindings[0].expected_profile_kind != "validation"
    ):
        raise IntegrityViolation(
            "auto-apply Run lacks its exact validation profile binding",
            profile_id=profile.profile_id,
            profile_version=profile.version,
        )
    binding = bindings[0]
    if not hmac.compare_digest(binding.profile_payload_hash, expected_payload_hash):
        raise IntegrityViolation(
            "auto-apply validation profile payload hash differs from frozen Run binding",
            profile_id=profile.profile_id,
            profile_version=profile.version,
        )
    definition, lifecycle = registry.resolve_execution_profile_binding(binding)
    if (
        lifecycle.state != "active"
        or definition.profile != binding.profile
        or definition.profile_kind != "validation"
        or definition.handler_key != "builtin_validation_profile@1"
        or definition.config_schema_id != "validation-profile-config@1"
        or set(definition.input_schema_ids) != {"constraint-validation@1", "patch-validation@1"}
        or set(definition.output_schema_ids) != {"auto-apply-proof@1", "evidence-set@1"}
        or definition.config != {}
        or definition.stochastic
        or definition.required_capabilities
        or not isinstance(definition.details, ValidationProfileDetailsV1)
        or definition.details.subject_kinds != ("patch", "constraint_proposal", "rollback_request")
        or set(definition.compatible_run_kinds)
        != {
            RunKindRef(kind="constraint_proposal.validate", version=1),
            RunKindRef(kind="patch.validate", version=1),
        }
        or run.kind != RunKindRef(kind="patch.validate", version=1)
    ):
        raise IntegrityViolation(
            "auto-apply validation profile does not authorize the built-in adapter"
        )
    actual_payload_hash = execution_profile_payload_hash(definition)
    if not hmac.compare_digest(actual_payload_hash, binding.profile_payload_hash):
        raise IntegrityViolation(
            "auto-apply validation profile payload hash differs from frozen Run binding",
            profile_id=profile.profile_id,
            profile_version=profile.version,
        )
    return definition


def _resolve_policy(
    resolver: AutoApplyPolicyRegistryResolver,
    ref: AutoApplyPolicyRefV1,
) -> AutoApplyPolicyV1:
    return _resolve_policy_registry(resolver, ref)[1]


def _resolve_policy_registry(
    resolver: AutoApplyPolicyRegistryResolver,
    ref: AutoApplyPolicyRefV1,
) -> tuple[AutoApplyPolicyRegistryV1, AutoApplyPolicyV1]:
    registry = resolver.resolve(ref.registry)
    if registry is None:
        raise IntegrityViolation(
            "auto-apply policy registry history is unavailable",
            registry_version=ref.registry.registry_version,
        )
    if registry.registry_version != ref.registry.registry_version or not hmac.compare_digest(
        registry.registry_digest, ref.registry.registry_digest
    ):
        raise IntegrityViolation("auto-apply policy registry differs from its exact ref")
    matches = tuple(
        policy
        for policy in registry.policies
        if (policy.policy_id, policy.policy_version) == (ref.policy_id, ref.policy_version)
    )
    if len(matches) != 1:
        raise IntegrityViolation(
            "auto-apply policy ref does not resolve exactly once",
            policy_id=ref.policy_id,
            policy_version=ref.policy_version,
        )
    policy = matches[0]
    if not hmac.compare_digest(compute_auto_apply_policy_digest(policy), ref.policy_digest):
        raise IntegrityViolation(
            "auto-apply policy digest differs from its exact ref",
            policy_id=ref.policy_id,
            policy_version=ref.policy_version,
        )
    return registry, policy


__all__ = [
    "AutoApplyArtifactReader",
    "AutoApplyPolicyRegistryResolver",
    "GuardedAutoApplyValidationPort",
    "RegistryResolvedAutoApplyEvaluator",
    "SqlAutoApplyPolicyRegistryResolver",
    "TransactionBoundAutoApplyAuthority",
    "build_transaction_auto_apply_validation_port",
    "build_worker_auto_apply_evaluator",
    "ensure_worker_auto_apply_catalog_supported",
]
