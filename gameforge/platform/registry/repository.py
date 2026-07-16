"""Immutable, exact-version platform registry storage."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import TypeVar

from pydantic import BaseModel

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_graphs import AgentExecutionGraphV1
from gameforge.contracts.execution_profiles import (
    ArtifactLineagePolicyRefV1,
    ExecutionProfileCatalogSnapshotV1,
    MigrationCapabilityMatrixRefV1,
    MigrationCapabilityMatrixRegistryV1,
    MigrationCapabilityMatrixV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    VersionTransitionPolicyRefV1,
    execution_profile_payload_hash,
)
from gameforge.contracts.jobs import (
    ArtifactLineagePolicyV1,
    BenchRunPayloadV1,
    FailureClassifierRefV1,
    FailureClassifierV1,
    FindingOutputPolicyRefV1,
    FindingOutputPolicyV1,
    RetryPolicyRefV1,
    RetryPolicySnapshot,
    ReviewRunPayloadV1,
    RunEventRegistryV1,
    RunKindDefinition,
    RunPolicyBindingV1,
    RunPayloadEnvelope,
    RunSchemaBindingV1,
    RuntimeParentRuleSetRef,
    RuntimeParentRuleSetV1,
    VersionTransitionPolicyV1,
    artifact_lineage_policy_digest,
    patch_repair_requires_root_seed,
)
from gameforge.contracts.playtest import (
    CompletionOracleRegistryRefV1,
    CompletionOracleRegistryV1,
)
from gameforge.platform.registry.model import (
    ProfileRequirement,
    RunKindIdentity,
    run_kind_identity,
)


_T = TypeVar("_T", bound=BaseModel)


def _wire(value: BaseModel) -> dict[str, object]:
    return value.model_dump(mode="json")


def _digest(value: BaseModel) -> str:
    return canonical_sha256(_wire(value))


def _resolve_json_pointer(value: object, pointer: str) -> object:
    current = value
    for raw_token in pointer.split("/")[1:]:
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise IntegrityViolation("profile requirement does not resolve in the Run payload")
            current = current[token]
            continue
        if isinstance(current, (tuple, list)) and token.isdecimal():
            index = int(token)
            if str(index) != token or index >= len(current):
                raise IntegrityViolation("profile requirement has an invalid array index")
            current = current[index]
            continue
        raise IntegrityViolation("profile requirement does not resolve in the Run payload")
    return current


def _index_exact(
    values: Iterable[_T],
    *,
    identity,
    label: str,
) -> Mapping[object, _T]:
    indexed: dict[object, _T] = {}
    for value in values:
        key = identity(value)
        if key in indexed:
            qualifier = "with conflicting content" if indexed[key] != value else "more than once"
            raise IntegrityViolation(f"{label} identity {key!r} is registered {qualifier}")
        indexed[key] = value
    return MappingProxyType(indexed)


def _normalize_profile_metadata(
    values: Mapping[RunKindRef | RunKindIdentity, Iterable[ProfileRequirement]],
) -> Mapping[RunKindIdentity, tuple[ProfileRequirement, ...]]:
    normalized: dict[RunKindIdentity, tuple[ProfileRequirement, ...]] = {}
    for raw_key, raw_requirements in values.items():
        key = run_kind_identity(raw_key)
        if key in normalized:
            raise IntegrityViolation(f"profile requirements repeat Run kind {key!r}")
        requirements = tuple(raw_requirements)
        paths = tuple(item.field_path for item in requirements)
        if len(paths) != len(set(paths)):
            raise IntegrityViolation(f"profile requirements repeat a field path for {key!r}")
        if any(
            left != right and (left.startswith(f"{right}/") or right.startswith(f"{left}/"))
            for left in paths
            for right in paths
        ):
            raise IntegrityViolation(f"profile requirements overlap field paths for {key!r}")
        normalized[key] = tuple(sorted(requirements, key=lambda item: item.field_path))
    return MappingProxyType(normalized)


def _normalize_key_metadata(
    values: Mapping[RunKindRef | RunKindIdentity, str],
    *,
    label: str,
) -> Mapping[RunKindIdentity, str]:
    normalized: dict[RunKindIdentity, str] = {}
    for raw_key, value in values.items():
        key = run_kind_identity(raw_key)
        if key in normalized:
            raise IntegrityViolation(f"{label} repeats Run kind {key!r}")
        if not value:
            raise IntegrityViolation(f"{label} contains an empty component key")
        normalized[key] = value
    return MappingProxyType(normalized)


class ImmutablePlatformRegistry:
    """Retain exact registry objects without a mutable ``current`` alias."""

    def __init__(
        self,
        *,
        run_kinds: Iterable[RunKindDefinition],
        retry_policies: Iterable[RetryPolicySnapshot],
        failure_classifiers: Iterable[FailureClassifierV1],
        lineage_policies: Iterable[ArtifactLineagePolicyV1],
        version_transition_policies: Iterable[VersionTransitionPolicyV1],
        runtime_parent_rule_sets: Iterable[RuntimeParentRuleSetV1],
        finding_output_policies: Iterable[FindingOutputPolicyV1],
        run_event_registries: Iterable[RunEventRegistryV1],
        completion_oracle_registries: Iterable[CompletionOracleRegistryV1],
        agent_execution_graphs: Iterable[AgentExecutionGraphV1] = (),
        execution_profile_catalogs: Iterable[ExecutionProfileCatalogSnapshotV1] = (),
        migration_capability_matrices: Iterable[MigrationCapabilityMatrixV1] = (),
        migration_capability_registries: Iterable[MigrationCapabilityMatrixRegistryV1] = (),
        profile_requirements: Mapping[
            RunKindRef | RunKindIdentity, Iterable[ProfileRequirement]
        ] = MappingProxyType({}),
        permission_resolver_keys: Mapping[RunKindRef | RunKindIdentity, str] = MappingProxyType({}),
    ) -> None:
        self._run_kinds = _index_exact(
            run_kinds,
            identity=lambda item: (item.kind, item.version),
            label="Run kind",
        )
        self._retry_policies = _index_exact(
            retry_policies,
            identity=lambda item: (item.retry_policy_id, item.retry_policy_version),
            label="retry policy",
        )
        self._failure_classifiers = _index_exact(
            failure_classifiers,
            identity=lambda item: item.classifier_version,
            label="failure classifier",
        )
        self._lineage_policies = _index_exact(
            lineage_policies,
            identity=lambda item: (item.policy_id, item.policy_version),
            label="lineage policy",
        )
        self._version_transition_policies = _index_exact(
            version_transition_policies,
            identity=lambda item: (item.policy_id, item.policy_version),
            label="version-transition policy",
        )
        self._runtime_parent_rule_sets = _index_exact(
            runtime_parent_rule_sets,
            identity=lambda item: (item.rule_set_id, item.version),
            label="runtime-parent rule set",
        )
        self._finding_output_policies = _index_exact(
            finding_output_policies,
            identity=lambda item: (item.policy_id, item.policy_version),
            label="Finding-output policy",
        )
        self._run_event_registries = _index_exact(
            run_event_registries,
            identity=lambda item: item.registry_version,
            label="Run-event registry",
        )
        self._completion_oracle_registries = _index_exact(
            completion_oracle_registries,
            identity=lambda item: item.registry_version,
            label="completion-oracle registry",
        )
        self._agent_execution_graphs = _index_exact(
            agent_execution_graphs,
            identity=lambda item: (
                item.run_kind.kind,
                item.run_kind.version,
                item.agent_graph_version,
            ),
            label="Agent execution graph",
        )
        graph_versions = [
            item.agent_graph_version for item in self._agent_execution_graphs.values()
        ]
        if len(graph_versions) != len(set(graph_versions)):
            raise IntegrityViolation("Agent execution graph versions must be globally unique")
        self._execution_profile_catalogs = _index_exact(
            execution_profile_catalogs,
            identity=lambda item: item.catalog_version,
            label="execution-profile catalog",
        )
        matrices = list(migration_capability_matrices)
        registries = tuple(migration_capability_registries)
        _index_exact(
            registries,
            identity=lambda item: item.registry_digest,
            label="migration-capability registry",
        )
        for registry in registries:
            matrices.extend(registry.matrices)
        self._migration_capability_matrices = _index_exact(
            matrices,
            identity=lambda item: item.matrix_version,
            label="migration-capability matrix",
        )
        self._profile_requirements = _normalize_profile_metadata(profile_requirements)
        self._permission_resolver_keys = _normalize_key_metadata(
            permission_resolver_keys,
            label="permission resolver metadata",
        )

    def list_run_kinds(self) -> tuple[RunKindDefinition, ...]:
        return tuple(self._run_kinds[key] for key in sorted(self._run_kinds))

    def get_run_kind(self, kind: RunKindRef) -> RunKindDefinition | None:
        return self._run_kinds.get((kind.kind, kind.version))

    def get_retry_policy(self, ref: RetryPolicyRefV1) -> RetryPolicySnapshot | None:
        value = self._retry_policies.get((ref.retry_policy_id, ref.retry_policy_version))
        return (
            value
            if value is not None and value.retry_policy_digest == ref.retry_policy_digest
            else None
        )

    def get_failure_classifier(
        self, ref: FailureClassifierRefV1 | object
    ) -> FailureClassifierV1 | None:
        if not isinstance(ref, FailureClassifierRefV1):
            return None
        value = self._failure_classifiers.get(ref.classifier_version)
        return (
            value
            if value is not None and value.classifier_digest == ref.classifier_digest
            else None
        )

    def get_lineage_policy(self, ref: ArtifactLineagePolicyRefV1) -> ArtifactLineagePolicyV1 | None:
        value = self._lineage_policies.get((ref.policy_id, ref.policy_version))
        return (
            value
            if value is not None and artifact_lineage_policy_digest(value) == ref.digest
            else None
        )

    def get_artifact_lineage_policy(
        self, ref: ArtifactLineagePolicyRefV1
    ) -> ArtifactLineagePolicyV1 | None:
        return self.get_lineage_policy(ref)

    def get_version_transition_policy(
        self, ref: VersionTransitionPolicyRefV1
    ) -> VersionTransitionPolicyV1 | None:
        value = self._version_transition_policies.get((ref.policy_id, ref.policy_version))
        return value if value is not None and _digest(value) == ref.digest else None

    def get_runtime_parent_rule_set(
        self, ref: RuntimeParentRuleSetRef
    ) -> RuntimeParentRuleSetV1 | None:
        value = self._runtime_parent_rule_sets.get((ref.rule_set_id, ref.version))
        return value if value is not None and _digest(value) == ref.digest else None

    def get_finding_output_policy(
        self, ref: FindingOutputPolicyRefV1
    ) -> FindingOutputPolicyV1 | None:
        value = self._finding_output_policies.get((ref.policy_id, ref.policy_version))
        return value if value is not None and _digest(value) == ref.digest else None

    def get_run_event_registry(
        self,
        registry_version: int | RunEventRegistryV1,
        registry_digest: str | None = None,
    ) -> RunEventRegistryV1 | None:
        if isinstance(registry_version, RunEventRegistryV1):
            registry_digest = registry_version.registry_digest
            version = registry_version.registry_version
        else:
            version = registry_version
        if registry_digest is None:
            return None
        value = self._run_event_registries.get(version)
        return value if value is not None and value.registry_digest == registry_digest else None

    def get_event_registry(
        self, registry_version: int, registry_digest: str
    ) -> RunEventRegistryV1 | None:
        return self.get_run_event_registry(registry_version, registry_digest)

    def get_completion_oracle_registry(
        self, ref: CompletionOracleRegistryRefV1
    ) -> CompletionOracleRegistryV1 | None:
        value = self._completion_oracle_registries.get(ref.registry_version)
        return value if value is not None and value.registry_digest == ref.digest else None

    def get_agent_execution_graph(
        self,
        run_kind: RunKindRef,
        agent_graph_version: str,
    ) -> AgentExecutionGraphV1 | None:
        """Resolve retained graph authority without a mutable current alias."""

        return self._agent_execution_graphs.get(
            (run_kind.kind, run_kind.version, agent_graph_version)
        )

    def get_execution_profile_catalog(
        self, catalog_version: int, catalog_digest: str
    ) -> ExecutionProfileCatalogSnapshotV1 | None:
        value = self._execution_profile_catalogs.get(catalog_version)
        return value if value is not None and value.catalog_digest == catalog_digest else None

    def get_migration_capability_matrix(
        self, ref: MigrationCapabilityMatrixRefV1
    ) -> MigrationCapabilityMatrixV1 | None:
        value = self._migration_capability_matrices.get(ref.matrix_version)
        return value if value is not None and value.matrix_digest == ref.matrix_digest else None

    def get_migration_matrix(
        self, ref: MigrationCapabilityMatrixRefV1
    ) -> MigrationCapabilityMatrixV1 | None:
        return self.get_migration_capability_matrix(ref)

    def get_profile_requirements(self, kind: RunKindRef) -> tuple[ProfileRequirement, ...] | None:
        return self._profile_requirements.get((kind.kind, kind.version))

    def get_permission_resolver_key(self, kind: RunKindRef) -> str | None:
        return self._permission_resolver_keys.get((kind.kind, kind.version))

    def resolve_required_run_bindings(
        self,
        *,
        definition: RunKindDefinition,
        resolved_profiles: tuple[ResolvedExecutionProfileBindingV1, ...],
    ) -> tuple[tuple[RunPolicyBindingV1, ...], tuple[RunSchemaBindingV1, ...]]:
        """Resolve the complete typed policy/schema binding key set for a Run.

        M4c's built-in Run kinds have no additional free-standing policy bindings:
        their policy authorities are already embedded in the exact RunKindDefinition
        or execution-profile payload hash.  Every kind does, however, bind its
        executable payload schema under the stable ``run_payload`` key.  Keeping
        this resolver on the immutable registry makes the empty/non-empty sets
        explicit and gives future versioned profiles one authority point for adding
        keys without trusting a client map.
        """

        retained = self._run_kinds.get((definition.kind, definition.version))
        if retained != definition:
            raise IntegrityViolation("Run binding resolution uses an unretained definition")
        for binding in resolved_profiles:
            catalog = self.get_execution_profile_catalog(
                binding.catalog_version,
                binding.catalog_digest,
            )
            profile = next(
                (
                    item
                    for item in (() if catalog is None else catalog.definitions)
                    if item.profile == binding.profile
                ),
                None,
            )
            if (
                profile is None
                or profile.profile_kind != binding.expected_profile_kind
                or execution_profile_payload_hash(profile) != binding.profile_payload_hash
            ):
                raise IntegrityViolation(
                    "Run binding resolution received an unretained execution profile"
                )
        return (
            (),
            (
                RunSchemaBindingV1(
                    binding_key="run_payload",
                    schema_id=definition.payload_schema_id,
                ),
            ),
        )

    def validate_payload_bindings(
        self,
        *,
        payload: RunPayloadEnvelope,
        definition: RunKindDefinition,
    ) -> None:
        retained = self._run_kinds.get((definition.kind, definition.version))
        if retained is None or retained != definition:
            raise IntegrityViolation("Run payload uses an unretained Run kind definition")
        if payload.payload_schema_version != definition.payload_schema_id:
            raise IntegrityViolation("Run payload schema differs from its Run kind definition")
        if payload.llm_execution_mode not in definition.allowed_llm_execution_modes:
            raise IntegrityViolation("Run payload execution mode is not allowed")
        if definition.seed_policy == "required" and payload.seed is None:
            raise IntegrityViolation("Run payload requires an explicit seed")
        if definition.seed_policy == "forbidden" and payload.seed is not None:
            raise IntegrityViolation("Run payload forbids an explicit seed")

        requirements = self._profile_requirements.get((definition.kind, definition.version))
        if requirements is None:
            raise IntegrityViolation("Run kind has no exact profile requirement metadata")
        actual = {item.field_path: item for item in payload.resolved_profiles}
        payload_wire = payload.model_dump(mode="python")
        consumed: set[str] = set()
        for requirement in requirements:
            payload_value = _resolve_json_pointer(payload_wire, requirement.field_path)
            if requirement.cardinality in {"one", "optional"}:
                matched_paths = (
                    (requirement.field_path,) if requirement.field_path in actual else ()
                )
                value_present = payload_value is not None
                if requirement.cardinality == "one" and (not matched_paths or not value_present):
                    raise IntegrityViolation("Run payload omits a required profile binding")
                if requirement.cardinality == "optional" and value_present != bool(matched_paths):
                    raise IntegrityViolation(
                        "optional profile binding differs from its payload field"
                    )
            else:
                if not isinstance(payload_value, (tuple, list)):
                    raise IntegrityViolation("many profile requirement must bind an array field")
                prefix = f"{requirement.field_path}/"
                indexed: list[tuple[int, str]] = []
                for path in actual:
                    if not path.startswith(prefix):
                        continue
                    suffix = path[len(prefix) :]
                    if not suffix.isdecimal() or str(int(suffix)) != suffix:
                        raise IntegrityViolation(
                            "profile-array binding path requires a canonical decimal index"
                        )
                    indexed.append((int(suffix), path))
                indexed.sort()
                if [index for index, _ in indexed] != list(range(len(indexed))):
                    raise IntegrityViolation("profile-array binding indexes must be contiguous")
                if len(indexed) != len(payload_value):
                    raise IntegrityViolation(
                        "profile-array bindings must cover every payload array item"
                    )
                matched_paths = tuple(path for _, path in indexed)
            consumed.update(matched_paths)
            for ordinal, path in enumerate(matched_paths):
                if actual[path].expected_profile_kind != requirement.expected_profile_kind:
                    raise IntegrityViolation(
                        "Run payload profile kind differs from registry metadata"
                    )
                expected_profile = (
                    payload_value[ordinal] if requirement.cardinality == "many" else payload_value
                )
                if actual[path].profile.model_dump(mode="python") != expected_profile:
                    raise IntegrityViolation(
                        "Run payload profile binding differs from the exact payload ProfileRef"
                    )
        if consumed != set(actual):
            raise IntegrityViolation("Run payload contains an unregistered profile binding")

        catalog = self.get_execution_profile_catalog(
            payload.execution_profile_catalog_version,
            payload.execution_profile_catalog_digest,
        )
        if catalog is None:
            raise IntegrityViolation(
                "Run payload execution-profile catalog is not retained exactly"
            )
        definitions_by_ref = {
            (item.profile.profile_id, item.profile.version): item for item in catalog.definitions
        }
        lifecycle_by_ref = {
            (item.profile.profile_id, item.profile.version): item.state
            for item in catalog.lifecycle
        }
        run_kind_ref = RunKindRef(kind=definition.kind, version=definition.version)
        for binding in actual.values():
            if (
                binding.catalog_version != payload.execution_profile_catalog_version
                or binding.catalog_digest != payload.execution_profile_catalog_digest
            ):
                raise IntegrityViolation("profile binding differs from the Run catalog binding")
            profile_ref = (binding.profile.profile_id, binding.profile.version)
            profile = definitions_by_ref.get(profile_ref)
            if profile is None:
                raise IntegrityViolation("profile binding does not resolve in the exact catalog")
            if profile.profile_kind != binding.expected_profile_kind:
                raise IntegrityViolation("Run payload profile kind differs from registry metadata")
            if binding.profile_payload_hash != execution_profile_payload_hash(profile):
                raise IntegrityViolation("profile binding payload hash differs from the catalog")
            if run_kind_ref not in profile.compatible_run_kinds:
                raise IntegrityViolation("profile binding is incompatible with the Run kind")
            state = lifecycle_by_ref[profile_ref]
            if state == "disabled" or (
                state == "replay_only" and payload.llm_execution_mode != "replay"
            ):
                raise IntegrityViolation("profile lifecycle state forbids this Run")

        expected_policy_bindings, expected_schema_bindings = self.resolve_required_run_bindings(
            definition=definition,
            resolved_profiles=payload.resolved_profiles,
        )
        if payload.policy_bindings != expected_policy_bindings:
            raise IntegrityViolation(
                "Run payload policy bindings differ from the complete registry key set"
            )
        if payload.schema_bindings != expected_schema_bindings:
            raise IntegrityViolation(
                "Run payload schema bindings differ from the complete registry key set"
            )

        if definition.seed_policy == "profile_dependent":
            stochastic = any(
                definitions_by_ref[(binding.profile.profile_id, binding.profile.version)].stochastic
                for binding in actual.values()
            ) or patch_repair_requires_root_seed(payload.params)
            if stochastic != (payload.seed is not None):
                raise IntegrityViolation(
                    "profile-dependent seed must match the resolved stochastic profiles"
                )

        params = payload.params
        if isinstance(params, ReviewRunPayloadV1):
            has_llm_triage = params.llm_triage_policy is not None
            if has_llm_triage == (payload.llm_execution_mode == "not_applicable"):
                raise IntegrityViolation(
                    "review LLM mode must match the exact triage profile binding"
                )
        elif (
            isinstance(params, BenchRunPayloadV1)
            and params.execution_scope == "aggregate_results"
            and payload.llm_execution_mode != "not_applicable"
        ):
            raise IntegrityViolation("Bench aggregation forbids LLM execution")

        if (
            definition.migration_capability_matrix is not None
            and self.get_migration_capability_matrix(definition.migration_capability_matrix) is None
        ):
            raise IntegrityViolation("Run payload migration matrix is not retained exactly")

    @property
    def profile_requirement_identities(self) -> frozenset[RunKindIdentity]:
        return frozenset(self._profile_requirements)

    @property
    def permission_resolver_identities(self) -> frozenset[RunKindIdentity]:
        return frozenset(self._permission_resolver_keys)

    @property
    def completion_oracle_registries(self) -> tuple[CompletionOracleRegistryV1, ...]:
        return tuple(
            self._completion_oracle_registries[key]
            for key in sorted(self._completion_oracle_registries)
        )

    @property
    def run_event_registries(self) -> tuple[RunEventRegistryV1, ...]:
        return tuple(self._run_event_registries[key] for key in sorted(self._run_event_registries))

    def with_execution_profile_catalogs(
        self,
        catalogs: Iterable[ExecutionProfileCatalogSnapshotV1],
        *,
        replace: bool = False,
    ) -> "ImmutablePlatformRegistry":
        """Clone this registry with an exact catalog-history set.

        Persisted historical catalogs are needed to validate old REPLAY Run payloads,
        while all non-profile platform registries remain the immutable process
        authority. ``replace=True`` selects a provisioned deployment's complete
        persisted history instead of the development built-in catalog; otherwise
        equal versions must be byte-for-byte equal and cannot override one another.
        """

        merged = {} if replace else dict(self._execution_profile_catalogs)
        for catalog in catalogs:
            if type(catalog) is not ExecutionProfileCatalogSnapshotV1:
                raise IntegrityViolation("execution-profile catalog history is invalid")
            retained = merged.get(catalog.catalog_version)
            if retained is not None and retained != catalog:
                raise IntegrityViolation(
                    "execution-profile catalog version has conflicting history",
                    catalog_version=catalog.catalog_version,
                )
            merged[catalog.catalog_version] = catalog
        return ImmutablePlatformRegistry(
            run_kinds=self._run_kinds.values(),
            retry_policies=self._retry_policies.values(),
            failure_classifiers=self._failure_classifiers.values(),
            lineage_policies=self._lineage_policies.values(),
            version_transition_policies=self._version_transition_policies.values(),
            runtime_parent_rule_sets=self._runtime_parent_rule_sets.values(),
            finding_output_policies=self._finding_output_policies.values(),
            run_event_registries=self._run_event_registries.values(),
            completion_oracle_registries=self._completion_oracle_registries.values(),
            agent_execution_graphs=self._agent_execution_graphs.values(),
            execution_profile_catalogs=merged.values(),
            migration_capability_matrices=self._migration_capability_matrices.values(),
            profile_requirements=self._profile_requirements,
            permission_resolver_keys=self._permission_resolver_keys,
        )

    def list_execution_profile_catalogs(
        self,
    ) -> tuple[ExecutionProfileCatalogSnapshotV1, ...]:
        return tuple(
            self._execution_profile_catalogs[key]
            for key in sorted(self._execution_profile_catalogs)
        )

    def list_agent_execution_graphs(self) -> tuple[AgentExecutionGraphV1, ...]:
        return tuple(
            self._agent_execution_graphs[key] for key in sorted(self._agent_execution_graphs)
        )

    def list_completion_oracle_registries(self) -> tuple[CompletionOracleRegistryV1, ...]:
        return self.completion_oracle_registries

    def list_run_event_registries(self) -> tuple[RunEventRegistryV1, ...]:
        return self.run_event_registries


__all__ = ["ImmutablePlatformRegistry"]
