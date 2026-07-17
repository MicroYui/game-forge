"""Pure M4 Artifact producer-matrix validation.

This module validates an already constructed ``lineage@2`` Artifact against
the frozen VersionTuple producer matrix.  Parent-role resolution belongs to
the versioned publication policy; its exact result is passed here through
``expected_versions``.  The validator never merges parent tuples or reads a
process default.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, TypeAlias

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import RunManifestVersionProjectionV1
from gameforge.contracts.lineage import ArtifactV1, ArtifactV2, ExecutionIdentityV1

VersionFieldName = Literal[
    "doc_version",
    "ir_snapshot_id",
    "constraint_snapshot_id",
    "prompt_version",
    "model_snapshot",
    "agent_graph_version",
    "tool_version",
    "env_contract_version",
    "seed",
    "cassette_id",
]
VersionValue: TypeAlias = str | int | None
LlmExecutionMode = Literal["not_applicable", "live", "record", "replay"]
Replayability = Literal[
    "online_only",
    "cassette_replay",
    "deterministic_recompute",
    "operational_observation",
]
ValidationStatus = Literal["valid", "evidence_missing"]
ConditionName = Literal[
    "tool_output",
    "rendered_prompt_evidence",
    "uses_dsl",
    "uses_environment",
    "llm_invocations",
    "record_or_replay",
]

VERSION_FIELD_ORDER: tuple[VersionFieldName, ...] = (
    "doc_version",
    "ir_snapshot_id",
    "constraint_snapshot_id",
    "prompt_version",
    "model_snapshot",
    "agent_graph_version",
    "tool_version",
    "env_contract_version",
    "seed",
    "cassette_id",
)
_VERSION_FIELD_SET = frozenset(VERSION_FIELD_ORDER)
_LLM_FIELDS: tuple[VersionFieldName, ...] = (
    "prompt_version",
    "model_snapshot",
    "agent_graph_version",
)
_NON_TOOL_FIELDS = frozenset(field for field in VERSION_FIELD_ORDER if field != "tool_version")
_SNAPSHOT_FIELDS = frozenset({"doc_version", "ir_snapshot_id", "constraint_snapshot_id"})
_LLM_AND_CASSETTE_FIELDS = frozenset((*_LLM_FIELDS, "cassette_id"))
_REPLAYABILITY_VALUES = frozenset(
    {
        "online_only",
        "cassette_replay",
        "deterministic_recompute",
        "operational_observation",
    }
)


@dataclass(frozen=True)
class ProducerRule:
    required_fields: tuple[VersionFieldName, ...]
    projected_fields: frozenset[VersionFieldName]
    projection_required: bool = False
    required_projected_fields: frozenset[VersionFieldName] = frozenset()
    requires_one_of_projected_fields: frozenset[VersionFieldName] = frozenset()
    conditional_fields: tuple[tuple[ConditionName, tuple[VersionFieldName, ...]], ...] = ()
    supports_llm_mode: bool = False
    accepts_execution_identity: bool = False
    allow_zero_invocations: bool = False
    identity_scopes: frozenset[str] = frozenset({"artifact"})
    supports_operational_observation: bool = False

    @property
    def condition_names(self) -> frozenset[ConditionName]:
        return frozenset(condition for condition, _fields in self.conditional_fields)


def _rule(
    *required_fields: VersionFieldName,
    projected_fields: frozenset[VersionFieldName] = frozenset(),
    projection_required: bool = False,
    required_projected_fields: frozenset[VersionFieldName] = frozenset(),
    requires_one_of_projected_fields: frozenset[VersionFieldName] = frozenset(),
    conditional_fields: tuple[tuple[ConditionName, tuple[VersionFieldName, ...]], ...] = (),
    supports_llm_mode: bool = False,
    accepts_execution_identity: bool = False,
    allow_zero_invocations: bool = False,
    identity_scopes: frozenset[str] = frozenset({"artifact"}),
    supports_operational_observation: bool = False,
) -> ProducerRule:
    return ProducerRule(
        required_fields=required_fields,
        projected_fields=projected_fields,
        projection_required=projection_required,
        required_projected_fields=required_projected_fields,
        requires_one_of_projected_fields=requires_one_of_projected_fields,
        conditional_fields=conditional_fields,
        supports_llm_mode=supports_llm_mode,
        accepts_execution_identity=accepts_execution_identity,
        allow_zero_invocations=allow_zero_invocations,
        identity_scopes=identity_scopes,
        supports_operational_observation=supports_operational_observation,
    )


_LLM_CONDITIONS: tuple[tuple[ConditionName, tuple[VersionFieldName, ...]], ...] = (
    ("llm_invocations", _LLM_FIELDS),
    ("record_or_replay", ("cassette_id",)),
)
_DSL_CONDITION: tuple[ConditionName, tuple[VersionFieldName, ...]] = (
    "uses_dsl",
    ("constraint_snapshot_id",),
)
_ENV_CONDITION: tuple[ConditionName, tuple[VersionFieldName, ...]] = (
    "uses_environment",
    ("env_contract_version",),
)


PRODUCER_RULES: Mapping[str, ProducerRule] = MappingProxyType(
    {
        "source_raw": _rule(
            "doc_version",
            projected_fields=frozenset({"doc_version"}),
            conditional_fields=(("tool_output", ("tool_version",)),),
            # Per-call governed prompt contexts are source_raw tool outputs
            # published before the corresponding model invocation.
            supports_llm_mode=True,
            allow_zero_invocations=True,
        ),
        "source_rendered": _rule(
            "doc_version",
            "tool_version",
            projected_fields=frozenset({"doc_version", "prompt_version", "agent_graph_version"}),
            projection_required=True,
            required_projected_fields=frozenset({"doc_version"}),
            conditional_fields=(
                (
                    "rendered_prompt_evidence",
                    ("prompt_version", "agent_graph_version"),
                ),
            ),
            supports_llm_mode=True,
            allow_zero_invocations=True,
            identity_scopes=frozenset(),
        ),
        "ir_snapshot": _rule(
            "ir_snapshot_id",
            "tool_version",
            projected_fields=frozenset({"doc_version", "ir_snapshot_id"})
            | _LLM_AND_CASSETTE_FIELDS,
            projection_required=True,
            conditional_fields=_LLM_CONDITIONS,
            supports_llm_mode=True,
            accepts_execution_identity=True,
        ),
        "constraint_snapshot": _rule(
            "constraint_snapshot_id",
            "tool_version",
            projected_fields=frozenset({"doc_version", "ir_snapshot_id", "constraint_snapshot_id"})
            | _LLM_AND_CASSETTE_FIELDS,
            projection_required=True,
            conditional_fields=_LLM_CONDITIONS,
            supports_llm_mode=True,
            accepts_execution_identity=True,
        ),
        "constraint_proposal": _rule(
            "tool_version",
            projected_fields=_SNAPSHOT_FIELDS | _LLM_AND_CASSETTE_FIELDS,
            projection_required=True,
            requires_one_of_projected_fields=_SNAPSHOT_FIELDS,
            conditional_fields=_LLM_CONDITIONS,
            supports_llm_mode=True,
            accepts_execution_identity=True,
        ),
        "config_export": _rule(
            "ir_snapshot_id",
            "constraint_snapshot_id",
            "tool_version",
            projected_fields=frozenset(
                {
                    "doc_version",
                    "ir_snapshot_id",
                    "constraint_snapshot_id",
                    "env_contract_version",
                }
            ),
            projection_required=True,
            required_projected_fields=frozenset({"ir_snapshot_id", "constraint_snapshot_id"}),
            conditional_fields=(_ENV_CONDITION,),
        ),
        "scenario_spec": _rule(
            "tool_version",
            projected_fields=_SNAPSHOT_FIELDS | frozenset({"env_contract_version"}),
            projection_required=True,
            conditional_fields=(_ENV_CONDITION,),
        ),
        "task_suite": _rule(
            "tool_version",
            projected_fields=_SNAPSHOT_FIELDS | frozenset({"env_contract_version"}),
            projection_required=True,
            conditional_fields=(_ENV_CONDITION,),
        ),
        "regression_suite": _rule(
            "tool_version",
            projected_fields=_SNAPSHOT_FIELDS | frozenset({"env_contract_version"}),
            projection_required=True,
            conditional_fields=(_ENV_CONDITION,),
        ),
        "golden_suite": _rule(
            "tool_version",
            projected_fields=_SNAPSHOT_FIELDS | frozenset({"env_contract_version"}),
            projection_required=True,
            conditional_fields=(_ENV_CONDITION,),
        ),
        "bench_dataset": _rule(
            "tool_version",
            projected_fields=_SNAPSHOT_FIELDS | frozenset({"env_contract_version"}),
            projection_required=True,
            conditional_fields=(_ENV_CONDITION,),
        ),
        "benchmark_spec": _rule(
            "tool_version",
            projected_fields=frozenset({"constraint_snapshot_id", "env_contract_version"}),
            conditional_fields=(_ENV_CONDITION,),
        ),
        "review_report": _rule(
            "ir_snapshot_id",
            "tool_version",
            projected_fields=frozenset({"ir_snapshot_id", "constraint_snapshot_id"})
            | _LLM_AND_CASSETTE_FIELDS,
            projection_required=True,
            required_projected_fields=frozenset({"ir_snapshot_id"}),
            conditional_fields=(_DSL_CONDITION, *_LLM_CONDITIONS),
            supports_llm_mode=True,
            accepts_execution_identity=True,
        ),
        "checker_run": _rule(
            "ir_snapshot_id",
            "tool_version",
            projected_fields=frozenset({"ir_snapshot_id", "constraint_snapshot_id"})
            | _LLM_AND_CASSETTE_FIELDS,
            projection_required=True,
            required_projected_fields=frozenset({"ir_snapshot_id"}),
            conditional_fields=(_DSL_CONDITION, *_LLM_CONDITIONS),
            supports_llm_mode=True,
            accepts_execution_identity=True,
        ),
        "simulation_run": _rule(
            "ir_snapshot_id",
            "tool_version",
            "seed",
            projected_fields=frozenset(
                {
                    "ir_snapshot_id",
                    "constraint_snapshot_id",
                    "env_contract_version",
                    "seed",
                }
            ),
            projection_required=True,
            required_projected_fields=frozenset({"ir_snapshot_id", "seed"}),
            conditional_fields=(_DSL_CONDITION, _ENV_CONDITION),
        ),
        "playtest_trace": _rule(
            "ir_snapshot_id",
            "constraint_snapshot_id",
            "tool_version",
            "env_contract_version",
            "seed",
            projected_fields=frozenset(
                {
                    "ir_snapshot_id",
                    "constraint_snapshot_id",
                    "env_contract_version",
                    "seed",
                }
            )
            | _LLM_AND_CASSETTE_FIELDS,
            projection_required=True,
            required_projected_fields=frozenset(
                {
                    "ir_snapshot_id",
                    "constraint_snapshot_id",
                    "env_contract_version",
                    "seed",
                }
            ),
            conditional_fields=(
                _DSL_CONDITION,
                _ENV_CONDITION,
                *_LLM_CONDITIONS,
            ),
            supports_llm_mode=True,
            accepts_execution_identity=True,
        ),
        "patch": _rule(
            "ir_snapshot_id",
            "tool_version",
            projected_fields=_SNAPSHOT_FIELDS | _LLM_AND_CASSETTE_FIELDS,
            projection_required=True,
            required_projected_fields=frozenset({"ir_snapshot_id"}),
            conditional_fields=_LLM_CONDITIONS,
            supports_llm_mode=True,
            accepts_execution_identity=True,
        ),
        "validation_evidence": _rule(
            "tool_version",
            projected_fields=_NON_TOOL_FIELDS,
            projection_required=True,
            requires_one_of_projected_fields=_SNAPSHOT_FIELDS,
            conditional_fields=(
                _DSL_CONDITION,
                _ENV_CONDITION,
                *_LLM_CONDITIONS,
            ),
            supports_llm_mode=True,
            accepts_execution_identity=True,
        ),
        "regression_evidence": _rule(
            "tool_version",
            projected_fields=_NON_TOOL_FIELDS,
            projection_required=True,
            requires_one_of_projected_fields=_SNAPSHOT_FIELDS,
            conditional_fields=(
                _DSL_CONDITION,
                _ENV_CONDITION,
                *_LLM_CONDITIONS,
            ),
            supports_llm_mode=True,
            accepts_execution_identity=True,
        ),
        "rollback_request": _rule(
            "tool_version",
            projected_fields=_NON_TOOL_FIELDS,
            projection_required=True,
        ),
        "run_result": _rule(
            "tool_version",
            projected_fields=frozenset(),
            conditional_fields=_LLM_CONDITIONS,
            supports_llm_mode=True,
            accepts_execution_identity=True,
            allow_zero_invocations=True,
            identity_scopes=frozenset({"run"}),
            supports_operational_observation=True,
        ),
        "run_failure": _rule(
            "tool_version",
            projected_fields=frozenset(),
            conditional_fields=_LLM_CONDITIONS,
            supports_llm_mode=True,
            accepts_execution_identity=True,
            allow_zero_invocations=True,
            identity_scopes=frozenset({"attempt", "run"}),
            supports_operational_observation=True,
        ),
        "cassette_bundle": _rule(
            "tool_version",
            "cassette_id",
            projected_fields=_LLM_AND_CASSETTE_FIELDS,
            conditional_fields=(("llm_invocations", _LLM_FIELDS),),
            supports_llm_mode=True,
            accepts_execution_identity=True,
            allow_zero_invocations=True,
            identity_scopes=frozenset({"record_shard", "attempt", "run"}),
        ),
        "migration_report": _rule(
            "tool_version",
            projected_fields=_NON_TOOL_FIELDS,
            projection_required=True,
        ),
        "bench_report": _rule(
            "tool_version",
            projected_fields=_NON_TOOL_FIELDS,
            projection_required=True,
            conditional_fields=_LLM_CONDITIONS,
            supports_llm_mode=True,
            accepts_execution_identity=True,
        ),
        "operational_evidence": _rule(
            "tool_version",
            projected_fields=_NON_TOOL_FIELDS,
            projection_required=True,
            conditional_fields=_LLM_CONDITIONS,
            supports_llm_mode=True,
            accepts_execution_identity=True,
            supports_operational_observation=True,
        ),
    }
)


@dataclass(frozen=True)
class ProducerValidationContext:
    """Trusted producer facts resolved before Artifact publication.

    ``expected_versions`` is the exact result of a typed parent-role/version
    projection for ordinary artifacts.  ``None`` means no projection evidence
    was supplied, while an empty mapping means the policy explicitly proved
    every inherited field not applicable.  Run manifests instead require both
    their payload projection and the independently frozen expected projection.
    """

    expected_versions: Mapping[str, VersionValue] | None = None
    llm_execution_mode: LlmExecutionMode = "not_applicable"
    has_llm_invocations: bool = False
    produced_by_agent: bool = False
    rendered_prompt_evidence: bool = False
    tool_output: bool = False
    uses_dsl: bool = False
    uses_environment: bool = False
    operational_observation: bool = False
    verified_legacy_import: bool = False
    run_manifest_projection: RunManifestVersionProjectionV1 | None = None
    expected_run_manifest_projection: RunManifestVersionProjectionV1 | None = None

    def __post_init__(self) -> None:
        if self.expected_versions is not None:
            object.__setattr__(
                self,
                "expected_versions",
                MappingProxyType(dict(self.expected_versions)),
            )


@dataclass(frozen=True)
class ProducerValidationReport:
    status: ValidationStatus
    artifact_id: str
    artifact_kind: str
    checked_fields: tuple[VersionFieldName, ...]
    missing_evidence: tuple[str, ...] = ()


def _is_present(value: object) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value))


def _same_typed_value(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right


def _condition_values(context: ProducerValidationContext) -> Mapping[ConditionName, bool]:
    return {
        "tool_output": context.tool_output,
        "rendered_prompt_evidence": context.rendered_prompt_evidence,
        "uses_dsl": context.uses_dsl,
        "uses_environment": context.uses_environment,
        "llm_invocations": context.has_llm_invocations,
        "record_or_replay": isinstance(context.llm_execution_mode, str)
        and context.llm_execution_mode in {"record", "replay"},
    }


def _append_context_violations(
    *,
    artifact: ArtifactV2,
    rule: ProducerRule,
    context: ProducerValidationContext,
    violations: list[str],
) -> None:
    mode = context.llm_execution_mode
    if not isinstance(mode, str) or mode not in {
        "not_applicable",
        "live",
        "record",
        "replay",
    }:
        violations.append(f"llm_execution_mode: unsupported value {mode!r}")
        return

    condition_values = _condition_values(context)
    for condition in (
        "tool_output",
        "rendered_prompt_evidence",
        "uses_dsl",
        "uses_environment",
    ):
        if condition_values[condition] and condition not in rule.condition_names:
            violations.append(
                f"producer condition {condition!r} is unsupported for {artifact.kind}"
            )

    if mode != "not_applicable" and not rule.supports_llm_mode:
        violations.append(f"llm_execution_mode {mode!r} is unsupported for {artifact.kind}")
    if context.has_llm_invocations and not rule.accepts_execution_identity:
        violations.append(f"execution_identity is unsupported for {artifact.kind}")
    if context.has_llm_invocations and mode == "not_applicable":
        violations.append("has_llm_invocations requires a live, record, or replay mode")
    if context.produced_by_agent and not context.has_llm_invocations:
        violations.append("produced_by_agent requires actual LLM invocations")
    if context.produced_by_agent and mode == "not_applicable":
        violations.append("produced_by_agent cannot use llm_execution_mode=not_applicable")
    if context.rendered_prompt_evidence and mode == "not_applicable":
        violations.append("rendered_prompt_evidence requires a live, record, or replay mode")
    if (
        mode != "not_applicable"
        and not context.has_llm_invocations
        and not context.rendered_prompt_evidence
        and not rule.allow_zero_invocations
    ):
        violations.append(f"{mode} producer requires at least one LLM invocation")

    if context.verified_legacy_import and artifact.kind != "cassette_bundle":
        violations.append("verified_legacy_import is valid only for cassette_bundle")
    if context.verified_legacy_import and mode != "replay":
        violations.append("verified_legacy_import requires llm_execution_mode=replay")
    if artifact.kind == "cassette_bundle":
        if mode not in {"record", "replay"}:
            violations.append(
                "cassette_bundle is produced only by record or verified replay import"
            )
        if mode == "replay" and not context.verified_legacy_import:
            violations.append("replay cassette_bundle requires verified_legacy_import")


def _append_projection_violations(
    *,
    artifact: ArtifactV2,
    rule: ProducerRule,
    context: ProducerValidationContext,
    violations: list[str],
) -> tuple[set[VersionFieldName], set[VersionFieldName]]:
    required_fields = set(rule.required_fields)
    declared_fields = set(required_fields)
    condition_values = _condition_values(context)
    conditional = dict(rule.conditional_fields)
    for condition, fields in rule.conditional_fields:
        if condition_values[condition]:
            required_fields.update(fields)
            declared_fields.update(fields)

    expected = context.expected_versions
    if rule.projection_required and expected is None:
        violations.append("version projection evidence is required for this artifact kind")
    for field in sorted(rule.required_projected_fields):
        if expected is None or field not in expected or not _is_present(expected[field]):
            violations.append(f"expected_versions.{field}: exact inherited projection is required")
    if rule.requires_one_of_projected_fields and (
        expected is None
        or not any(
            field in expected and _is_present(expected[field])
            for field in rule.requires_one_of_projected_fields
        )
    ):
        required = ", ".join(sorted(rule.requires_one_of_projected_fields))
        violations.append(
            f"version projection requires at least one non-null field from: {required}"
        )
    if artifact.kind in {"run_result", "run_failure"} and expected:
        violations.append("run manifests use RunManifestVersionProjectionV1, not expected_versions")
    if expected is not None:
        for raw_field, expected_value in expected.items():
            if raw_field not in _VERSION_FIELD_SET:
                violations.append(f"expected_versions.{raw_field}: unknown VersionTuple field")
                continue
            field = raw_field
            if field not in rule.projected_fields:
                violations.append(f"unsupported projection field {field!r} for {artifact.kind}")
                continue
            declared_fields.add(field)
            actual_value = getattr(artifact.version_tuple, field)
            if not _same_typed_value(actual_value, expected_value):
                violations.append(
                    f"expected_versions.{field}: expected {expected_value!r}, got {actual_value!r}"
                )

    for condition in (
        "rendered_prompt_evidence",
        "uses_dsl",
        "uses_environment",
    ):
        if not condition_values[condition]:
            continue
        for field in conditional.get(condition, ()):
            if expected is None or field not in expected:
                violations.append(
                    f"expected_versions.{field}: exact {condition} projection is required"
                )

    if artifact.kind in {"run_result", "run_failure"}:
        declared_fields.update(VERSION_FIELD_ORDER)

    for field in VERSION_FIELD_ORDER:
        value = getattr(artifact.version_tuple, field)
        if value is not None and field not in declared_fields:
            violations.append(
                f"version_tuple.{field}: non-null field has no producer or projection rule"
            )

    return declared_fields, required_fields


def _append_identity_violations(
    *,
    artifact: ArtifactV2,
    rule: ProducerRule,
    context: ProducerValidationContext,
    violations: list[str],
) -> None:
    mode = context.llm_execution_mode
    identity = artifact.meta.get("execution_identity")
    if identity is not None and not isinstance(identity, ExecutionIdentityV1):
        violations.append("meta.execution_identity is not an ExecutionIdentityV1")
        return

    if context.has_llm_invocations:
        if identity is None:
            violations.append("meta.execution_identity is required for actual LLM invocations")
            return
        if not identity.bindings:
            violations.append("meta.execution_identity bindings cannot be empty")
    elif identity is not None:
        if mode == "not_applicable":
            violations.append("not_applicable producer cannot carry execution_identity")
        elif identity.bindings:
            violations.append(
                "has_llm_invocations must be true when execution_identity has bindings"
            )
        elif not rule.allow_zero_invocations:
            violations.append("empty execution_identity is unsupported for this artifact kind")

    if identity is None:
        return
    if identity.scope not in rule.identity_scopes:
        allowed = ",".join(sorted(rule.identity_scopes)) or "none"
        violations.append(
            f"execution_identity.scope {identity.scope!r} is invalid; expected {allowed}"
        )
    if identity.scope == "artifact" and any(
        not binding.response_consumed for binding in identity.bindings
    ):
        violations.append("artifact execution_identity may contain only response_consumed routes")
    if mode == "replay" and any(
        binding.execution_source != "cassette_replay" for binding in identity.bindings
    ):
        violations.append("replay execution_identity requires cassette_replay execution_source")
    if mode in {"live", "record"} and any(
        binding.execution_source == "cassette_replay" for binding in identity.bindings
    ):
        violations.append(f"{mode} execution_identity cannot use cassette_replay execution_source")


def _append_run_manifest_violations(
    *,
    artifact: ArtifactV2,
    context: ProducerValidationContext,
    violations: list[str],
) -> None:
    actual = context.run_manifest_projection
    expected = context.expected_run_manifest_projection
    is_run_manifest = artifact.kind in {"run_result", "run_failure"}

    if not is_run_manifest:
        if actual is not None or expected is not None:
            violations.append(
                "RunManifestVersionProjectionV1 is valid only for run_result/run_failure"
            )
        return

    if actual is None:
        violations.append("run manifest requires payload RunManifestVersionProjectionV1 evidence")
    if expected is None:
        violations.append(
            "run manifest requires exact frozen RunManifestVersionProjectionV1 evidence"
        )
    if actual is None or expected is None:
        return

    if actual.version_transition_policy_ref != expected.version_transition_policy_ref:
        violations.append("run manifest transition policy ref does not match the exact frozen ref")
    if actual != expected:
        violations.append("run manifest projection does not match the exact frozen projection")
    if actual.terminal_version_tuple != artifact.version_tuple:
        violations.append(
            "run manifest terminal_version_tuple does not match Artifact VersionTuple"
        )

    projected_parent_ids = {parent.artifact_id for parent in actual.parents}
    if projected_parent_ids != set(artifact.lineage):
        violations.append("run manifest parent bindings do not match Artifact lineage exactly")
    if artifact.kind == "run_result" and actual.manifest_scope != "run":
        violations.append("run_result requires run manifest scope")

    if actual.manifest_scope == "attempt":
        for parent in actual.parents:
            if (
                parent.role in {"intermediate", "evidence"}
                and parent.attempt_no is not None
                and parent.attempt_no != actual.attempt_no
            ):
                violations.append(
                    "attempt manifest parent attempt_no does not match manifest scope"
                )

    if not context.has_llm_invocations:
        terminal = actual.terminal_version_tuple
        frozen = actual.frozen_input_version_tuple
        if terminal.prompt_version is not None:
            violations.append("zero-invocation run manifest prompt_version must be null")
        if terminal.model_snapshot is not None:
            violations.append("zero-invocation run manifest model_snapshot must be null")
        if terminal.agent_graph_version != frozen.agent_graph_version:
            violations.append(
                "zero-invocation run manifest agent_graph_version must copy the "
                "exact frozen plan projection"
            )

    cassette_scopes = tuple(
        parent.cassette_scope for parent in actual.parents if parent.cassette_scope is not None
    )
    mode = context.llm_execution_mode
    if mode == "record":
        required_scope = "attempt_bundle" if actual.manifest_scope == "attempt" else "run_bundle"
        if cassette_scopes.count(required_scope) != 1:
            violations.append(f"record run manifest requires exactly one {required_scope} parent")
    elif mode == "replay":
        if cassette_scopes.count("replay_input") != 1:
            violations.append("replay run manifest requires exactly one replay_input parent")
    elif cassette_scopes:
        violations.append(f"{mode} run manifest cannot carry cassette-scoped parents")


def _append_replayability_violations(
    *,
    artifact: ArtifactV2,
    rule: ProducerRule,
    context: ProducerValidationContext,
    violations: list[str],
) -> None:
    mode = context.llm_execution_mode
    marker = artifact.meta.get("replayability")
    if marker is not None and (not isinstance(marker, str) or marker not in _REPLAYABILITY_VALUES):
        violations.append(f"meta.replayability: unsupported value {marker!r}")
        return

    if mode == "live" and marker != "online_only":
        violations.append("live Artifact must declare replayability=online_only")
    elif mode in {"record", "replay"} and marker != "cassette_replay":
        violations.append(f"{mode} Artifact must declare replayability=cassette_replay")
    elif mode == "not_applicable" and marker in {"online_only", "cassette_replay"}:
        violations.append(f"not_applicable Artifact cannot declare replayability={marker}")

    if marker == "operational_observation" and not context.operational_observation:
        violations.append(
            "replayability=operational_observation requires trusted operational context"
        )
    if context.operational_observation:
        if not rule.supports_operational_observation:
            violations.append(f"operational observation is unsupported for {artifact.kind}")
        if mode != "not_applicable":
            violations.append("operational observation requires llm_execution_mode=not_applicable")
        if marker != "operational_observation":
            violations.append(
                "operational observation must declare replayability=operational_observation"
            )


def _append_version_field_violations(
    *,
    artifact: ArtifactV2,
    rule: ProducerRule,
    context: ProducerValidationContext,
    declared_fields: set[VersionFieldName],
    required_fields: set[VersionFieldName],
    violations: list[str],
) -> None:
    for field in VERSION_FIELD_ORDER:
        if field not in declared_fields:
            continue
        if not _is_present(getattr(artifact.version_tuple, field)):
            expected = context.expected_versions
            if field not in required_fields and (
                expected is None or field not in expected or expected[field] is None
            ):
                continue
            violations.append(f"version_tuple.{field}: required by producer matrix")

    cassette_id = artifact.version_tuple.cassette_id
    if cassette_id is not None:
        is_namespaced_sha = (
            isinstance(cassette_id, str)
            and cassette_id.startswith("sha256:")
            and len(cassette_id) == 71
            and all(character in "0123456789abcdef" for character in cassette_id[7:])
        )
        if not is_namespaced_sha:
            violations.append("version_tuple.cassette_id must be sha256:<64 lowercase hex>")

    mode = context.llm_execution_mode
    if mode == "not_applicable":
        for field in (*_LLM_FIELDS, "cassette_id"):
            if getattr(artifact.version_tuple, field) is not None:
                violations.append(
                    f"version_tuple.{field}: must be null for not_applicable execution"
                )
    elif mode == "live" and cassette_id is not None:
        violations.append("version_tuple.cassette_id must be null for live execution")

    if artifact.kind == "cassette_bundle":
        expected_bundle_id = f"sha256:{artifact.payload_hash}"
        if cassette_id != expected_bundle_id:
            violations.append("version_tuple.cassette_id does not match bundle_payload_hash")


def validate_artifact_producer(
    artifact: ArtifactV1 | ArtifactV2,
    context: ProducerValidationContext,
) -> ProducerValidationReport:
    """Validate a frozen Artifact producer projection or fail closed.

    Legacy ``lineage@1`` rows cannot prove M4 ObjectRef, typed projection, or
    execution identity.  They return an honest evidence-missing report and are
    never mutated or upgraded in memory.
    """

    if isinstance(artifact, ArtifactV1):
        return ProducerValidationReport(
            status="evidence_missing",
            artifact_id=artifact.artifact_id,
            artifact_kind=artifact.kind,
            checked_fields=(),
            missing_evidence=(
                "/object_ref",
                "/producer_version_projection",
                "/meta/execution_identity",
            ),
        )

    rule = PRODUCER_RULES[artifact.kind]
    violations: list[str] = []
    _append_context_violations(
        artifact=artifact,
        rule=rule,
        context=context,
        violations=violations,
    )
    declared_fields, required_fields = _append_projection_violations(
        artifact=artifact,
        rule=rule,
        context=context,
        violations=violations,
    )
    _append_identity_violations(
        artifact=artifact,
        rule=rule,
        context=context,
        violations=violations,
    )
    _append_run_manifest_violations(
        artifact=artifact,
        context=context,
        violations=violations,
    )
    _append_replayability_violations(
        artifact=artifact,
        rule=rule,
        context=context,
        violations=violations,
    )
    _append_version_field_violations(
        artifact=artifact,
        rule=rule,
        context=context,
        declared_fields=declared_fields,
        required_fields=required_fields,
        violations=violations,
    )

    if violations:
        raise IntegrityViolation(
            "ArtifactV2 producer matrix validation failed",
            artifact_id=artifact.artifact_id,
            artifact_kind=artifact.kind,
            violations=tuple(dict.fromkeys(violations)),
        )

    checked_fields = tuple(field for field in VERSION_FIELD_ORDER if field in declared_fields)
    return ProducerValidationReport(
        status="valid",
        artifact_id=artifact.artifact_id,
        artifact_kind=artifact.kind,
        checked_fields=checked_fields,
    )
