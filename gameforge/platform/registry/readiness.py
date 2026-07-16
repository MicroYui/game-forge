"""Fail-closed validation for the complete M4c platform registry."""

from __future__ import annotations

from typing import Any, get_args

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_graphs import AgentExecutionProfileSelectorV1
from gameforge.contracts.execution_profiles import (
    ConfigExportProfileDetailsV1,
    EnvironmentProfileDetailsV1,
    ExecutionProfileCatalogSnapshotV1,
    MigrationProfileDetailsV1,
    PlaytestPlannerProfileConfigV1,
    RunKindRef,
)
from gameforge.contracts.jobs import (
    ArtifactLineagePolicyV1,
    ExecutionIdentityCountBindingV1,
    OutcomeArtifactRuleV1,
    RunKindDefinition,
    run_kind_definition_digest,
)
from gameforge.contracts.playtest import CompletionOracleRegistryRefV1
from gameforge.platform.lineage.validation import PRODUCER_RULES
from gameforge.platform.publication.payload_binding import (
    validate_domain_payload_binding_registry,
)
from gameforge.platform.publication.producer import (
    BUILTIN_DOMAIN_PRODUCER_FACTS_RESOLVER,
)
from gameforge.platform.registry.model import (
    FROZEN_ACTIVE_RUN_KIND_IDENTITIES,
    FROZEN_PROFILE_REQUIREMENT_SHAPES,
    FROZEN_RUN_KIND_DEFINITION_DIGESTS,
    FROZEN_RUN_KIND_SHAPES,
    PlatformReadinessReport,
    TrustedComponentMaps,
)
from gameforge.platform.registry.repository import ImmutablePlatformRegistry


def _contains_wildcard(value: str) -> bool:
    return "*" in value


def _require_exact_keys(*, label: str, actual: set[str], expected: set[str]) -> None:
    if actual == expected:
        return
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    raise IntegrityViolation(
        f"{label} trusted key set does not close exactly; missing={missing!r}, extra={extra!r}"
    )


_PROFILE_SELECTOR_ENUMS = {
    ("playtest_planner", "/memory_mode"): frozenset(
        value
        for value in get_args(PlaytestPlannerProfileConfigV1.model_fields["memory_mode"].annotation)
        if isinstance(value, str)
    ),
}


def _resolve_config_pointer(document: Any, pointer: str) -> Any:
    current = document
    for token in pointer.removeprefix("/").split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or token not in current:
            raise IntegrityViolation(
                "Agent graph selector config pointer is absent from a retained profile"
            )
        current = current[token]
    return current


class PlatformReadinessValidator:
    """Validate registry references and trusted component maps as one closure."""

    def __init__(
        self,
        *,
        registry: ImmutablePlatformRegistry,
        components: TrustedComponentMaps,
    ) -> None:
        self._registry = registry
        self._components = components

    def validate(self) -> PlatformReadinessReport:
        definitions = self._registry.list_run_kinds()
        active = tuple(item for item in definitions if item.status == "active")
        active_ids = {(item.kind, item.version) for item in active}
        if active_ids != FROZEN_ACTIVE_RUN_KIND_IDENTITIES or len(active) != 14:
            raise IntegrityViolation("registry must contain exactly the 14 frozen active Run kinds")
        agent_graphs = self._registry.list_agent_execution_graphs()
        llm_kind_ids = {
            (item.kind, item.version)
            for item in active
            if any(mode != "not_applicable" for mode in item.allowed_llm_execution_modes)
        }
        active_graph_selectors: dict[
            tuple[str, int], list[AgentExecutionProfileSelectorV1 | None]
        ] = {identity: [] for identity in llm_kind_ids}
        definitions_by_id = {(item.kind, item.version): item for item in definitions}
        for graph in agent_graphs:
            retained = self._registry.get_agent_execution_graph(
                graph.run_kind,
                graph.agent_graph_version,
            )
            if retained != graph:
                raise IntegrityViolation("Agent execution graph does not resolve exactly")
            identity = (graph.run_kind.kind, graph.run_kind.version)
            definition = definitions_by_id.get(identity)
            retained_definition = self._registry.get_run_kind(graph.run_kind)
            if definition is None or retained_definition != definition:
                raise IntegrityViolation(
                    "Agent execution graph does not bind an exact retained Run kind definition"
                )
            if definition.executor_key != graph.executor_key:
                raise IntegrityViolation("Agent execution graph executor differs from its Run kind")
            if not any(mode != "not_applicable" for mode in definition.allowed_llm_execution_modes):
                raise IntegrityViolation(
                    "Agent execution graph binds a Run kind without LLM execution"
                )
            if graph.status == "active":
                if definition.status != "active" or identity not in active_graph_selectors:
                    raise IntegrityViolation(
                        "active Agent execution graph requires an active LLM-capable Run kind"
                    )
                active_graph_selectors[identity].append(graph.profile_selector)
            elif (
                graph.status == "replay_only"
                and "replay" not in definition.allowed_llm_execution_modes
            ):
                raise IntegrityViolation(
                    "replay-only Agent execution graph requires a replay-capable Run kind"
                )
            # Selector metadata is creation-time authority. A disabled Run kind's
            # retained graph is historical evidence only: admission cannot select
            # that definition, and active-only profile metadata need not be kept as
            # a mutable/current alias merely to preserve exact old graph bytes.
            if (
                graph.profile_selector is not None
                and definition.status == "active"
                and graph.status in {"active", "replay_only"}
            ):
                requirements = self._registry.get_profile_requirements(graph.run_kind)
                if requirements is None or graph.profile_selector.profile_field_path not in {
                    item.field_path for item in requirements
                }:
                    raise IntegrityViolation(
                        "Agent graph selector does not name a registered profile field"
                    )
            if any(
                "*" in value
                for node in graph.nodes
                for value in (
                    node.agent_node_id,
                    node.prompt_version,
                    node.tool_version,
                    *node.required_capabilities,
                )
            ):
                raise IntegrityViolation("Agent execution graph fields forbid wildcards")
        catalogs = self._registry.list_execution_profile_catalogs()
        for identity, selectors in active_graph_selectors.items():
            if not selectors:
                raise IntegrityViolation(
                    "active Agent graph selectors must be non-overlapping and complete"
                )
            unconditional = [selector for selector in selectors if selector is None]
            if unconditional:
                if len(selectors) != 1:
                    raise IntegrityViolation(
                        "an unconditional active Agent graph must be the only branch"
                    )
                continue

            conditional = tuple(selector for selector in selectors if selector is not None)
            selector_keys = {
                (selector.profile_field_path, selector.config_pointer) for selector in conditional
            }
            if len(selector_keys) != 1:
                raise IntegrityViolation(
                    "active Agent graph selectors must use one exact profile config enum"
                )
            field_path, config_pointer = next(iter(selector_keys))
            expected_values = [selector.expected_value for selector in conditional]
            if len(expected_values) != len(set(expected_values)):
                raise IntegrityViolation("active Agent graph selector branches overlap")

            requirements = self._registry.get_profile_requirements(
                RunKindRef(kind=identity[0], version=identity[1])
            )
            requirement = next(
                (item for item in requirements or () if item.field_path == field_path),
                None,
            )
            if requirement is None:
                raise IntegrityViolation(
                    "Agent graph selector does not name a registered profile field"
                )
            enum_values = _PROFILE_SELECTOR_ENUMS.get(
                (requirement.expected_profile_kind, config_pointer)
            )
            if not enum_values or not set(expected_values).issubset(enum_values):
                raise IntegrityViolation(
                    "Agent graph selector values are outside the versioned profile config enum"
                )

            run_kind = RunKindRef(kind=identity[0], version=identity[1])
            reachable_values: set[str] = set()
            for catalog in catalogs:
                lifecycle = {item.profile: item.state for item in catalog.lifecycle}
                for profile in catalog.definitions:
                    if (
                        profile.profile_kind != requirement.expected_profile_kind
                        or run_kind not in profile.compatible_run_kinds
                        or lifecycle.get(profile.profile) not in {"active", "replay_only"}
                    ):
                        continue
                    actual = _resolve_config_pointer(profile.config, config_pointer)
                    if not isinstance(actual, str) or actual not in enum_values:
                        raise IntegrityViolation(
                            "retained profile config is outside its selector enum"
                        )
                    reachable_values.add(actual)
            if not reachable_values or not reachable_values.issubset(expected_values):
                raise IntegrityViolation(
                    "active Agent graph selectors do not cover reachable profile config values"
                )
        if self._registry.profile_requirement_identities != active_ids:
            raise IntegrityViolation("profile requirement metadata does not cover active Run kinds")
        expected_resolver_identities = {
            (item.kind, item.version)
            for item in active
            if item.required_permission.domain_scope == "all"
        }
        if self._registry.permission_resolver_identities != expected_resolver_identities:
            raise IntegrityViolation(
                "permission resolver metadata does not cover the dynamic permission templates"
            )

        event_registries = self._registry.list_run_event_registries()
        if not event_registries:
            raise IntegrityViolation("Run-event registry is not retained")
        for event_registry in event_registries:
            if (
                self._registry.get_run_event_registry(
                    event_registry.registry_version,
                    event_registry.registry_digest,
                )
                is None
            ):
                raise IntegrityViolation("Run-event registry does not resolve exactly")
            if any(
                _contains_wildcard(definition.data_schema_id)
                for definition in event_registry.definitions
            ):
                raise IntegrityViolation("Run-event schemas forbid wildcards")

        oracle_registries = self._registry.list_completion_oracle_registries()
        if not oracle_registries:
            raise IntegrityViolation("completion-oracle registry is not retained")
        for oracle_registry in oracle_registries:
            ref = CompletionOracleRegistryRefV1(
                registry_version=oracle_registry.registry_version,
                digest=oracle_registry.registry_digest,
            )
            if self._registry.get_completion_oracle_registry(ref) is None:
                raise IntegrityViolation("completion-oracle registry does not resolve exactly")
            if any(
                _contains_wildcard(schema_id)
                for definition in oracle_registry.definitions
                for schema_id in (
                    definition.params_schema_id,
                    definition.result_schema_id,
                )
            ):
                raise IntegrityViolation("completion-oracle schemas forbid wildcards")

        profile_catalogs = self._registry.list_execution_profile_catalogs()
        if not profile_catalogs:
            raise IntegrityViolation("execution-profile catalog is not retained")
        retained_migration_edges: set[tuple[str, str, str, str, str | None]] = set()
        for definition in active:
            if definition.migration_capability_matrix is None:
                continue
            matrix = self._registry.get_migration_capability_matrix(
                definition.migration_capability_matrix
            )
            if matrix is None:
                raise IntegrityViolation("active migration Run kind has an unretained matrix")
            retained_migration_edges.update(
                (
                    edge.source_kind,
                    edge.source_payload_schema_id,
                    edge.target_payload_schema_id,
                    edge.target_meta_schema_version,
                    edge.target_dsl_grammar_version,
                )
                for edge in matrix.edges
            )
        for catalog in profile_catalogs:
            if (
                self._registry.get_execution_profile_catalog(
                    catalog.catalog_version,
                    catalog.catalog_digest,
                )
                is None
            ):
                raise IntegrityViolation("execution-profile catalog does not resolve exactly")
            profiles_by_ref = {profile.profile: profile for profile in catalog.definitions}
            for profile in catalog.definitions:
                if any(
                    _contains_wildcard(schema_id)
                    for schema_id in (
                        *profile.input_schema_ids,
                        *profile.output_schema_ids,
                        *profile.required_capabilities,
                    )
                ):
                    raise IntegrityViolation("execution-profile schema allowlists forbid wildcards")
                for compatible_kind in profile.compatible_run_kinds:
                    if self._registry.get_run_kind(compatible_kind) is None:
                        raise IntegrityViolation(
                            "execution profile names an unretained compatible Run kind"
                        )
                if isinstance(profile.details, ConfigExportProfileDetailsV1):
                    environment = profiles_by_ref.get(profile.details.target_environment_profile)
                    if (
                        environment is None
                        or environment.profile_kind != "environment"
                        or not isinstance(environment.details, EnvironmentProfileDetailsV1)
                        or environment.details.contract.env_contract_version
                        != profile.details.env_contract_version
                    ):
                        raise IntegrityViolation(
                            "config-export profile has an unclosed environment contract"
                        )
                if isinstance(profile.details, MigrationProfileDetailsV1):
                    for edge in profile.details.edges:
                        identity = (
                            edge.source_kind,
                            edge.source_payload_schema_id,
                            edge.target_payload_schema_id,
                            edge.target_meta_schema_version,
                            edge.target_dsl_grammar_version,
                        )
                        if identity not in retained_migration_edges:
                            raise IntegrityViolation(
                                "migration profile edge lacks a retained matrix capability"
                            )

        expected_executors: set[str] = set()
        expected_terminal_hooks: set[str] = set()
        expected_workflow_effects: set[str] = set()
        expected_profile_handlers: set[str] = set()
        expected_permission_resolvers: set[str] = set()
        reference_checks = sum(len(graph.nodes) + 1 for graph in agent_graphs)

        for definition in active:
            identity = (definition.kind, definition.version)
            if run_kind_definition_digest(definition) != FROZEN_RUN_KIND_DEFINITION_DIGESTS.get(
                identity
            ):
                raise IntegrityViolation(
                    "Run kind definition digest differs from the frozen M4c contract"
                )
            self._validate_frozen_shape(definition)
            reference_checks += self._validate_definition(definition)
            expected_executors.add(definition.executor_key)
            expected_terminal_hooks.update(
                {
                    definition.terminal_hooks.on_success,
                    definition.terminal_hooks.on_failure,
                    definition.terminal_hooks.on_cancel,
                    definition.terminal_hooks.on_timeout,
                }
            )
            expected_workflow_effects.update(
                policy.workflow_effect_key for policy in definition.outcome_policies
            )
            requirements = self._registry.get_profile_requirements(
                RunKindRef(kind=definition.kind, version=definition.version)
            )
            if requirements is None:
                raise IntegrityViolation("active Run kind lacks profile requirement metadata")
            actual_requirement_shape = {
                (item.field_path, item.expected_profile_kind, item.cardinality)
                for item in requirements
            }
            expected_requirement_shape = set(
                FROZEN_PROFILE_REQUIREMENT_SHAPES[(definition.kind, definition.version)]
            )
            if actual_requirement_shape != expected_requirement_shape:
                raise IntegrityViolation(
                    "Run kind profile requirement metadata differs from the frozen table"
                )
            for requirement in requirements:
                if not self._profile_requirement_is_satisfiable(
                    definition=definition,
                    profile_kind=requirement.expected_profile_kind,
                    catalogs=profile_catalogs,
                ):
                    raise IntegrityViolation(
                        "profile requirement has no retained compatible profile definition"
                    )
            resolver_key = self._registry.get_permission_resolver_key(
                RunKindRef(kind=definition.kind, version=definition.version)
            )
            if definition.required_permission.domain_scope == "all":
                if resolver_key is None:
                    raise IntegrityViolation("dynamic permission template lacks its resolver")
                expected_permission_resolvers.add(resolver_key)
            elif resolver_key is not None:
                raise IntegrityViolation("fixed permission template cannot bind a domain resolver")

        reference_checks += BUILTIN_DOMAIN_PRODUCER_FACTS_RESOLVER.validate_registry(self._registry)
        reference_checks += validate_domain_payload_binding_registry(self._registry)

        for catalog in profile_catalogs:
            lifecycle = {
                (item.profile.profile_id, item.profile.version): item.state
                for item in catalog.lifecycle
            }
            for profile in catalog.definitions:
                identity = (profile.profile.profile_id, profile.profile.version)
                if lifecycle[identity] in {"active", "replay_only"}:
                    expected_profile_handlers.add(profile.handler_key)

        expected_completion_oracles = {
            definition.executor_key
            for registry in oracle_registries
            for definition in registry.definitions
        }
        _require_exact_keys(
            label="executor",
            actual=set(self._components.executors),
            expected=expected_executors,
        )
        _require_exact_keys(
            label="terminal hook",
            actual=set(self._components.terminal_hooks),
            expected=expected_terminal_hooks,
        )
        _require_exact_keys(
            label="workflow effect",
            actual=set(self._components.workflow_effects),
            expected=expected_workflow_effects,
        )
        _require_exact_keys(
            label="completion oracle",
            actual=set(self._components.completion_oracles),
            expected=expected_completion_oracles,
        )
        _require_exact_keys(
            label="profile handler",
            actual=set(self._components.profile_handlers),
            expected=expected_profile_handlers,
        )
        _require_exact_keys(
            label="permission-domain resolver",
            actual=set(self._components.permission_domain_resolvers),
            expected=expected_permission_resolvers,
        )

        ordered_refs = tuple(
            RunKindRef(kind=item.kind, version=item.version)
            for item in sorted(active, key=lambda value: (value.kind, value.version))
        )
        component_counts = tuple(
            (name, len(getattr(self._components, name)))
            for name in (
                "executors",
                "terminal_hooks",
                "workflow_effects",
                "completion_oracles",
                "profile_handlers",
                "permission_domain_resolvers",
            )
        )
        return PlatformReadinessReport(
            ready=True,
            active_run_kinds=ordered_refs,
            checked_run_kind_count=len(ordered_refs),
            deferred_executor_keys=("artifact_migrator@1", "dr_drill_runner@1"),
            reference_checks=reference_checks,
            component_key_counts=component_counts,
        )

    @staticmethod
    def _validate_frozen_shape(definition: RunKindDefinition) -> None:
        identity = (definition.kind, definition.version)
        expected = FROZEN_RUN_KIND_SHAPES[identity]
        finding_policy_id = (
            definition.finding_output_policy_ref.policy_id
            if definition.finding_output_policy_ref is not None
            else None
        )
        dynamic_domain = definition.required_permission.domain_scope == "all"
        actual = (
            definition.payload_schema_id,
            definition.creation_mode,
            definition.allowed_command_schema_ids,
            definition.required_permission.action,
            definition.required_permission.resource_kind,
            dynamic_domain,
            definition.executor_key,
            definition.terminal_hooks.on_success,
            definition.retry_policy.retry_policy_id,
            definition.retry_policy.retry_policy_version,
            definition.allowed_llm_execution_modes,
            definition.seed_policy,
            definition.seed_derivation_version,
            finding_policy_id,
            definition.migration_capability_matrix is not None,
        )
        frozen = (
            expected.payload_schema_id,
            expected.creation_mode,
            expected.command_schema_ids,
            expected.permission_action,
            expected.permission_resource_kind,
            expected.dynamic_domain_permission,
            expected.executor_key,
            expected.success_hook,
            expected.retry_policy_id,
            1,
            expected.llm_modes,
            expected.seed_policy,
            expected.seed_derivation_version,
            expected.finding_policy_id,
            expected.migration_matrix_required,
        )
        if actual != frozen:
            raise IntegrityViolation("Run kind definition differs from the frozen M4c table")
        if expected.dynamic_domain_permission:
            if definition.required_permission.domain_scope != "all":
                raise IntegrityViolation("Run kind requires a dynamic domain permission template")
        elif definition.required_permission.domain_scope is not None:
            raise IntegrityViolation("Run kind requires a global permission template")

        validation_kind = identity in {
            ("patch.validate", 1),
            ("constraint_proposal.validate", 1),
            ("rollback.validate", 1),
        }
        expected_non_success = (
            (
                "publish_validation_non_success@1",
                "publish_validation_non_success@1",
                "publish_validation_non_success@1",
            )
            if validation_kind
            else (
                "publish_run_failure@1",
                "publish_run_cancel@1",
                "publish_run_timeout@1",
            )
        )
        actual_non_success = (
            definition.terminal_hooks.on_failure,
            definition.terminal_hooks.on_cancel,
            definition.terminal_hooks.on_timeout,
        )
        if actual_non_success != expected_non_success:
            raise IntegrityViolation("Run kind non-success hooks differ from the frozen stage")
        if not any(policy.prepared_outcome == "success" for policy in definition.outcome_policies):
            raise IntegrityViolation("Run kind lacks a stage-correct success outcome policy")

    def _validate_definition(self, definition: RunKindDefinition) -> int:
        checks = 0
        if _contains_wildcard(definition.payload_schema_id):
            raise IntegrityViolation("Run payload schema cannot be a wildcard")
        if any(_contains_wildcard(item) for item in definition.allowed_command_schema_ids):
            raise IntegrityViolation("Run command schema allowlists forbid wildcards")
        if self._registry.get_retry_policy(definition.retry_policy) is None:
            raise IntegrityViolation("Run kind retry policy does not resolve exactly")
        checks += 1
        if self._registry.get_failure_classifier(definition.failure_classifier) is None:
            raise IntegrityViolation("Run kind failure classifier does not resolve exactly")
        checks += 1
        if self._registry.get_runtime_parent_rule_set(definition.runtime_parent_rule_set) is None:
            raise IntegrityViolation("Run kind runtime-parent rules do not resolve exactly")
        checks += 1
        if definition.finding_output_policy_ref is not None:
            if (
                self._registry.get_finding_output_policy(definition.finding_output_policy_ref)
                is None
            ):
                raise IntegrityViolation("Run kind Finding-output policy does not resolve exactly")
            checks += 1
        if definition.migration_capability_matrix is not None:
            migration_matrix = self._registry.get_migration_capability_matrix(
                definition.migration_capability_matrix
            )
            if migration_matrix is None:
                raise IntegrityViolation("Run kind migration matrix does not resolve exactly")
            checks += 1
            for edge in migration_matrix.edges:
                schema_fields = (
                    edge.source_payload_schema_id,
                    edge.target_payload_schema_id,
                    edge.target_meta_schema_version,
                    edge.target_dsl_grammar_version or "",
                )
                if any(_contains_wildcard(value) for value in schema_fields):
                    raise IntegrityViolation("migration capability selectors forbid wildcards")
                if edge.publication_lineage_policy_ref is not None:
                    if (
                        self._registry.get_lineage_policy(edge.publication_lineage_policy_ref)
                        is None
                    ):
                        raise IntegrityViolation(
                            "migration publication lineage policy does not resolve exactly"
                        )
                    checks += 1

        selectors: set[tuple[object, ...]] = set()
        policy_refs: set[tuple[str, int]] = set()
        for policy in definition.outcome_policies:
            policy_rule_ids = {item.rule_id for item in policy.artifact_rules}
            policy_ref = (policy.policy_id, policy.policy_version)
            selector = (
                policy.outcome_code,
                policy.prepared_outcome,
                policy.publication_scope,
                policy.attempt_terminal_status,
                policy.run_status_after_publication,
                policy.failure_class,
                policy.retry_disposition,
            )
            if policy_ref in policy_refs or selector in selectors:
                raise IntegrityViolation("Run outcome policies overlap or repeat")
            if _contains_wildcard(policy.outcome_code):
                raise IntegrityViolation("Run outcome policy cannot use a wildcard selector")
            policy_refs.add(policy_ref)
            selectors.add(selector)
            transition_policy = self._registry.get_version_transition_policy(
                policy.version_transition_policy_ref
            )
            if transition_policy is None:
                raise IntegrityViolation(
                    "outcome version-transition policy does not resolve exactly"
                )
            if transition_policy.manifest_scope != policy.publication_scope:
                raise IntegrityViolation(
                    "outcome publication scope differs from its transition policy"
                )
            checks += 1
            for artifact_rule in policy.artifact_rules:
                if any(_contains_wildcard(item) for item in artifact_rule.payload_schema_ids):
                    raise IntegrityViolation("outcome Artifact schema allowlists forbid wildcards")
                lineage_policy = self._registry.get_lineage_policy(artifact_rule.lineage_policy_ref)
                if lineage_policy is None:
                    raise IntegrityViolation("outcome lineage policy does not resolve exactly")
                self._validate_lineage_policy(
                    artifact_rule=artifact_rule,
                    lineage_policy=lineage_policy,
                    policy_rule_ids=policy_rule_ids,
                )
                if any(
                    _contains_wildcard(schema_id)
                    for schema_id in lineage_policy.child_payload_schema_ids
                ) or any(
                    _contains_wildcard(schema_id)
                    for parent_rule in lineage_policy.parent_rules
                    for schema_id in parent_rule.payload_schema_ids
                ):
                    raise IntegrityViolation("lineage schema allowlists forbid wildcards")
                checks += 1

        runtime_rules = self._registry.get_runtime_parent_rule_set(
            definition.runtime_parent_rule_set
        )
        if runtime_rules is None:
            raise IntegrityViolation("Run kind runtime-parent rules disappeared during readiness")
        if any(
            _contains_wildcard(schema_id)
            for rule in runtime_rules.rules
            for schema_id in rule.payload_schema_ids
        ):
            raise IntegrityViolation("runtime-parent schema allowlists forbid wildcards")
        for rule in runtime_rules.rules:
            if rule.source != "record_shard":
                continue
            binding = rule.count_binding
            expected_scope = (
                "current_attempt"
                if rule.manifest_scope == "attempt"
                else "all_attempts"
                if rule.manifest_scope == "run"
                else None
            )
            if (
                not isinstance(binding, ExecutionIdentityCountBindingV1)
                or expected_scope is None
                or binding.scope != expected_scope
            ):
                raise IntegrityViolation(
                    "record-shard runtime parent must count consumed execution-identity calls"
                )
        return checks

    @staticmethod
    def _validate_lineage_policy(
        *,
        artifact_rule: OutcomeArtifactRuleV1,
        lineage_policy: ArtifactLineagePolicyV1,
        policy_rule_ids: set[str],
    ) -> None:
        if lineage_policy.child_kind != artifact_rule.artifact_kind or set(
            lineage_policy.child_payload_schema_ids
        ) != set(artifact_rule.payload_schema_ids):
            raise IntegrityViolation(
                "outcome Artifact rule differs from its lineage child selector"
            )
        if not lineage_policy.parent_rules or not any(
            item.min_count > 0 for item in lineage_policy.parent_rules
        ):
            raise IntegrityViolation("outcome Artifact lineage requires typed parent rules")

        parent_rules_by_role = {item.parent_role: item for item in lineage_policy.parent_rules}
        parent_roles = set(parent_rules_by_role)
        for parent_rule in lineage_policy.parent_rules:
            if parent_rule.source == "prepared_rule" and (
                parent_rule.source_rule_id not in policy_rule_ids
                or parent_rule.source_rule_id == artifact_rule.rule_id
            ):
                raise IntegrityViolation(
                    "lineage prepared-rule parent must reference a known sibling outcome rule"
                )

        producer_rule = PRODUCER_RULES.get(lineage_policy.child_kind)
        if producer_rule is None:
            raise IntegrityViolation("lineage child kind lacks a frozen producer-matrix rule")
        projections = {item.field: item for item in lineage_policy.version_projection}
        if lineage_policy.policy_id.startswith("task-suite-derived/"):
            doc_projection = projections.get("doc_version")
            expected_doc_equality = (
                {"config", "scenarios"} if lineage_policy.child_kind == "task_suite" else {"config"}
            )
            if (
                lineage_policy.child_kind not in {"task_suite", "scenario_spec"}
                or doc_projection is None
                or doc_projection.source != "parent_role"
                or doc_projection.parent_role != "preview"
                or set(doc_projection.equality_parent_roles) != expected_doc_equality
            ):
                raise IntegrityViolation(
                    "TaskSuite lineage doc version must close preview, config, and scenarios"
                )
        inherited_fields = {
            field for field, item in projections.items() if item.source == "parent_role"
        }
        exact_projection_fields = {
            field for field, item in projections.items() if item.source != "constant_null"
        }
        if producer_rule.projection_required and not inherited_fields:
            raise IntegrityViolation("producer matrix requires an inherited version projection")
        if not producer_rule.required_projected_fields.issubset(exact_projection_fields):
            raise IntegrityViolation("lineage omits a producer-matrix required exact projection")
        if producer_rule.requires_one_of_projected_fields and not (
            producer_rule.requires_one_of_projected_fields & exact_projection_fields
        ):
            raise IntegrityViolation("lineage omits every producer-matrix alternative exact field")
        if not inherited_fields.issubset(producer_rule.projected_fields):
            raise IntegrityViolation("lineage inherits a field forbidden by the producer matrix")
        for projection in projections.values():
            if not set(projection.equality_parent_roles).issubset(parent_roles):
                raise IntegrityViolation(
                    "lineage version equality references an unknown parent role"
                )
            if projection.source != "parent_role":
                continue
            if projection.parent_role not in parent_roles:
                raise IntegrityViolation(
                    "lineage version projection references an unknown parent role"
                )
            parent_rule = parent_rules_by_role[projection.parent_role]
            if (
                parent_rule.max_count != 1
                and projection.parent_role not in projection.equality_parent_roles
            ):
                raise IntegrityViolation(
                    "multi-valued lineage projection requires equality across its parent role"
                )

    @staticmethod
    def _profile_requirement_is_satisfiable(
        *,
        definition: RunKindDefinition,
        profile_kind: str,
        catalogs: tuple[ExecutionProfileCatalogSnapshotV1, ...],
    ) -> bool:
        run_kind = RunKindRef(kind=definition.kind, version=definition.version)
        for catalog in catalogs:
            lifecycle = {
                (item.profile.profile_id, item.profile.version): item.state
                for item in catalog.lifecycle
            }
            for profile in catalog.definitions:
                identity = (profile.profile.profile_id, profile.profile.version)
                if (
                    profile.profile_kind == profile_kind
                    and run_kind in profile.compatible_run_kinds
                    and lifecycle[identity] in {"active", "replay_only"}
                ):
                    return True
        return False


__all__ = ["PlatformReadinessValidator"]
