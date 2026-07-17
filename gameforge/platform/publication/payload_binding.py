"""Authoritative semantic bindings for terminal domain Artifact payloads.

Schema validation proves that a blob has the right *shape*.  Typed lineage and
VersionTuple projection prove which immutable parents and versions were used.
Neither proof alone establishes that duplicated semantic fields inside the
payload (``snapshot_id``, Patch bases/targets, profiles, references, and so on)
tell the same story.  This module closes that last worker-controlled gap and also
confines authority-like Artifact metadata to facts that can be re-derived from
the Run, typed parents, projected tuple, or canonical payload.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from gameforge.contracts.canonical import (
    canonical_sha256,
    compute_snapshot_id,
    typed_canonical_json,
)
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import (
    ArtifactMigrationPayloadV1,
    BenchRunPayloadV1,
    CheckerRunPayloadV1,
    ConstraintProposalProposePayloadV1,
    ConstraintValidationPayloadV1,
    GenerationProposePayloadV1,
    OutcomeArtifactPolicyV1,
    OutcomeArtifactRuleV1,
    PatchRepairPayloadV1,
    PatchValidationPayloadV1,
    PlaytestRunPayloadV1,
    RequirementDispositionV1,
    ResolvedPolicySubsetCountBindingV1,
    ReviewRunPayloadV1,
    RollbackValidationPayloadV1,
    RunRecord,
    SimulationRunPayloadV1,
    TaskSuiteDerivePayloadV1,
)
from gameforge.contracts.lineage import VersionTuple
from gameforge.contracts.playtest import PlaytestTraceV1, ScenarioSpecV1, TaskSuiteV1
from gameforge.contracts.workflow import (
    CONSTRAINT_COMPILE_REASON_CODES_V1,
    CONSTRAINT_COMPILE_REQUIREMENT_KIND,
)
from gameforge.platform.publication.lineage import ParentInfo, TypedLineage
from gameforge.platform.publication.payload_schema import ARTIFACT_PAYLOAD_VALIDATORS
from gameforge.platform.publication.producer import BUILTIN_DOMAIN_PRODUCER_FACT_ENTRIES
from gameforge.platform.run_handlers.validation_common import (
    PATCH_SIMULATION_EXECUTION_MODE_V1,
    VALIDATION_SEED_DERIVATION_VERSION,
    derive_validation_subseed,
    regression_suite_execution_coverage_binding,
)


SemanticSelector = tuple[str, int, str, int, str, str, str]
_EXTERNAL_PAYLOAD_SCHEMAS = frozenset({"bench-report@2"})


_SUPPORTED_SELECTORS: frozenset[SemanticSelector] = frozenset(
    (
        facts.key.run_kind,
        facts.key.run_kind_version,
        facts.key.policy_id,
        facts.key.policy_version,
        facts.key.outcome_rule_id,
        facts.key.artifact_kind,
        facts.key.payload_schema_id,
    )
    for facts in BUILTIN_DOMAIN_PRODUCER_FACT_ENTRIES
    if ARTIFACT_PAYLOAD_VALIDATORS[facts.key.payload_schema_id].is_available
    or facts.key.payload_schema_id in _EXTERNAL_PAYLOAD_SCHEMAS
)

_BASE_META_KEYS = frozenset({"payload_schema_id", "replayability", "execution_identity"})


@dataclass(frozen=True, slots=True)
class FinalRequirementDispositionFact:
    """One exact EvidenceRequirement disposition proved by a final sibling."""

    applicability: str
    status: str
    reason_code: str | None
    tool_version: str


@dataclass(frozen=True, slots=True)
class FinalSiblingFact:
    """Publisher-derived facts for one already allocated sibling Artifact.

    These facts are deliberately constructed only after the sibling payload has
    passed schema/semantic validation and the immutable Artifact has been stored.
    A later same-publication payload may therefore bind not merely an Artifact id,
    but the exact outcome rule, requirement identity/kind and payload digest that
    id represents.
    """

    artifact_id: str
    outcome_rule_id: str
    artifact_kind: str
    payload_schema_id: str
    payload_hash: str
    requirement_id: str | None
    requirement_kind: str | None
    requirement_dispositions: tuple[FinalRequirementDispositionFact, ...] = ()


class DomainPayloadBindingRegistry(Protocol):
    def list_run_kinds(self) -> tuple[object, ...]: ...


def _selector(
    run: RunRecord,
    policy: OutcomeArtifactPolicyV1,
    rule: OutcomeArtifactRuleV1,
    payload_schema_id: str,
) -> SemanticSelector:
    return (
        run.kind.kind,
        run.kind.version,
        policy.policy_id,
        policy.policy_version,
        rule.rule_id,
        rule.artifact_kind,
        payload_schema_id,
    )


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def _same(left: object, right: object) -> bool:
    try:
        return typed_canonical_json(_json_value(left)) == typed_canonical_json(_json_value(right))
    except (TypeError, ValueError):
        return False


def _fail(message: str, *, field: str | None = None) -> None:
    details = {"field": field} if field is not None else {}
    raise IntegrityViolation(message, **details)


def _expect(actual: object, expected: object, *, field: str) -> None:
    if not _same(actual, expected):
        _fail("domain payload differs from its authoritative semantic binding", field=field)


def _optional_field(payload: Mapping[str, object], field: str) -> object | None:
    return payload.get(field)


def _required_field(payload: Mapping[str, object], field: str) -> object:
    if field not in payload:
        _fail("domain payload omits an authoritative semantic field", field=field)
    return payload[field]


def _role_parents(typed: TypedLineage, role: str) -> tuple[ParentInfo, ...]:
    return tuple(typed.parents_by_role.get(role, ()))


def _role_ids(typed: TypedLineage, role: str) -> tuple[str, ...]:
    return tuple(parent.artifact_id for parent in _role_parents(typed, role))


def _expect_role_ids(typed: TypedLineage, role: str, expected: Sequence[str | None]) -> None:
    expected_ids = tuple(value for value in expected if value is not None)
    actual_ids = _role_ids(typed, role)
    if len(actual_ids) != len(set(actual_ids)) or sorted(actual_ids) != sorted(expected_ids):
        raise IntegrityViolation(
            "typed lineage role differs from the exact Run semantic binding",
            parent_role=role,
            expected=tuple(sorted(expected_ids)),
            actual=tuple(sorted(actual_ids)),
        )


def _authoritative_parent_payload(
    payloads: Mapping[str, Mapping[str, object]] | None,
    artifact_id: str,
) -> Mapping[str, object]:
    if payloads is None or artifact_id not in payloads:
        raise IntegrityViolation(
            "exact parent Artifact payload is unavailable for semantic binding",
            artifact_id=artifact_id,
        )
    return payloads[artifact_id]


def _constraint_ids_from_parent(
    payloads: Mapping[str, Mapping[str, object]] | None,
    artifact_id: str,
) -> tuple[str, ...]:
    payload = _authoritative_parent_payload(payloads, artifact_id)
    constraints = payload.get("constraints")
    if not isinstance(constraints, Sequence) or isinstance(constraints, (str, bytes, bytearray)):
        raise IntegrityViolation(
            "authoritative constraint parent has no exact constraint array",
            artifact_id=artifact_id,
        )
    ids: list[str] = []
    for constraint in constraints:
        constraint_id = constraint.get("id") if isinstance(constraint, Mapping) else None
        if not isinstance(constraint_id, str) or not constraint_id:
            raise IntegrityViolation(
                "authoritative constraint parent has an invalid constraint id",
                artifact_id=artifact_id,
            )
        ids.append(constraint_id)
    if len(ids) != len(set(ids)):
        raise IntegrityViolation(
            "authoritative constraint parent repeats a constraint id",
            artifact_id=artifact_id,
        )
    return tuple(sorted(ids))


def _llm_constraint_ids_from_parent(
    payloads: Mapping[str, Mapping[str, object]] | None,
    artifact_id: str,
) -> frozenset[str]:
    """Resolve the exact constraints whose retained DSL needs LLM fallback."""

    payload = _authoritative_parent_payload(payloads, artifact_id)
    raw_constraints = payload.get("constraints")
    if not isinstance(raw_constraints, Sequence) or isinstance(
        raw_constraints, (str, bytes, bytearray)
    ):
        raise IntegrityViolation(
            "authoritative constraint parent has no exact constraint array",
            artifact_id=artifact_id,
        )
    values: set[str] = set()
    try:
        constraints = tuple(Constraint.model_validate(item) for item in raw_constraints)
    except (TypeError, ValueError) as exc:
        raise IntegrityViolation(
            "authoritative constraint parent has invalid DSL",
            artifact_id=artifact_id,
        ) from exc
    for constraint in constraints:
        if constraint.has_llm_predicate():
            values.add(constraint.id)
    return frozenset(values)


def _validate_constraint_compile_execution(
    *,
    params: ConstraintValidationPayloadV1,
    payload: Mapping[str, object],
) -> None:
    """Bind the frozen V1 stage set to the exact Run engine/golden request."""

    raw_stages = payload.get("stages")
    if not isinstance(raw_stages, Sequence) or isinstance(raw_stages, (str, bytes, bytearray)):
        _fail("constraint compile evidence has no exact stage array")
    for stage in raw_stages:
        if not isinstance(stage, Mapping):
            _fail("constraint compile evidence contains an invalid stage")
        status = stage.get("status")
        if status == "passed":
            continue
        stage_kind = stage.get("stage")
        engine_id = stage.get("engine_id") if stage_kind == "differential" else None
        identity = (stage_kind, status, engine_id, stage.get("reason_code"))
        wildcard_identity = (stage_kind, status, "*", stage.get("reason_code"))
        if (
            identity not in CONSTRAINT_COMPILE_REASON_CODES_V1
            and wildcard_identity not in CONSTRAINT_COMPILE_REASON_CODES_V1
        ):
            _fail("constraint compile evidence reason is not allowlisted")
    differential_stages = tuple(
        stage
        for stage in raw_stages
        if isinstance(stage, Mapping) and stage.get("stage") == "differential"
    )
    if len(differential_stages) < 2:
        _fail("constraint compile evidence requires at least two engines")
    golden_stages = tuple(
        stage
        for stage in raw_stages
        if isinstance(stage, Mapping) and stage.get("stage") == "golden"
    )
    if len(golden_stages) != 1:
        _fail("constraint compile evidence requires one exact golden stage")
    golden_stage = golden_stages[0]
    if params.golden_suite_artifact_id is None:
        if (
            golden_stage.get("status") != "not_applicable"
            or golden_stage.get("reason_code") != "golden_suite_absent"
        ):
            _fail("constraint compile evidence golden absence is not exact")
    elif golden_stage.get("status") == "not_applicable":
        _fail("constraint compile evidence skipped the bound golden suite")

    expected_engines = {
        (engine.engine_id, engine.version) for engine in params.differential_engines
    }
    actual_engines: set[tuple[str, int]] = set()
    for stage in differential_stages:
        engine_id = stage.get("engine_id")
        engine_version = stage.get("engine_version")
        if not isinstance(engine_id, str) or not engine_id:
            _fail("constraint compile evidence stage has no exact engine id")
        if (
            not isinstance(engine_version, str)
            or not engine_version.isdecimal()
            or engine_version != str(int(engine_version))
            or int(engine_version) < 1
        ):
            _fail("constraint compile evidence engine version is not canonical")
        engine_key = (engine_id, int(engine_version))
        if engine_key in actual_engines:
            _fail("constraint compile evidence repeats an engine identity")
        actual_engines.add(engine_key)
    if actual_engines != expected_engines:
        _fail("constraint compile evidence engines differ from the exact Run")


def _scenario_id_from_parent(
    payloads: Mapping[str, Mapping[str, object]] | None,
    artifact_id: str,
) -> str:
    payload = _authoritative_parent_payload(payloads, artifact_id)
    scenario_id = payload.get("scenario_id")
    if not isinstance(scenario_id, str) or not scenario_id:
        raise IntegrityViolation(
            "authoritative scenario parent has no exact scenario id",
            artifact_id=artifact_id,
        )
    return scenario_id


def _one_role_id(typed: TypedLineage, role: str) -> str:
    values = _role_ids(typed, role)
    if len(values) != 1:
        raise IntegrityViolation(
            "semantic payload binding requires exactly one typed parent",
            parent_role=role,
            actual=len(values),
        )
    return values[0]


def _profile_key(profile: object) -> str:
    profile_id = getattr(profile, "profile_id")
    version = getattr(profile, "version")
    return f"{profile_id}@{version}"


def _validate_typed_run_parents(
    *,
    run: RunRecord,
    policy: OutcomeArtifactPolicyV1,
    rule: OutcomeArtifactRuleV1,
    typed: TypedLineage,
) -> None:
    params = run.payload.params
    rule_id = rule.rule_id
    if isinstance(params, GenerationProposePayloadV1):
        if rule_id == "primary":
            _expect_role_ids(typed, "snapshot", (params.base_snapshot_artifact_id,))
            _expect_role_ids(typed, "constraint", (params.constraint_snapshot_artifact_id,))
            _expect_role_ids(typed, "goal", (params.objective_goal.source_artifact_id,))
            _expect_role_ids(
                typed,
                "supporting_evidence",
                tuple(item.evidence_artifact_id for item in params.findings),
            )
        elif rule_id == "preview":
            _expect_role_ids(typed, "base", (params.base_snapshot_artifact_id,))
        else:
            _expect_role_ids(typed, "constraint", (params.constraint_snapshot_artifact_id,))
        return
    if isinstance(params, PatchRepairPayloadV1):
        if rule_id == "primary":
            _expect_role_ids(typed, "base", (params.base_snapshot_artifact_id,))
            _expect_role_ids(typed, "preview", (params.preview_snapshot_artifact_id,))
            _expect_role_ids(typed, "subject", (params.subject_patch_artifact_id,))
            _expect_role_ids(typed, "validation", (params.validation_evidence_artifact_id,))
            _expect_role_ids(
                typed,
                "supporting_evidence",
                tuple(item.evidence_artifact_id for item in params.findings),
            )
            _expect_role_ids(typed, "constraint", (params.constraint_snapshot_artifact_id,))
        elif rule_id == "preview":
            _expect_role_ids(typed, "base", (params.base_snapshot_artifact_id,))
        elif rule_id in {"checker", "simulation", "regression"}:
            if policy.policy_id == "repair-unverified":
                _expect_role_ids(typed, "preview", (params.preview_snapshot_artifact_id,))
            elif policy.policy_id == "repair-verified":
                _one_role_id(typed, "preview")
            else:
                raise IntegrityViolation(
                    "repair evidence policy has no registered preview authority",
                    policy_id=policy.policy_id,
                )
            _expect_role_ids(typed, "constraint", (params.constraint_snapshot_artifact_id,))
        else:
            _expect_role_ids(typed, "constraint", (params.constraint_snapshot_artifact_id,))
        return
    if isinstance(params, ConstraintProposalProposePayloadV1):
        # The authoring goal is authenticated source_raw authority and the
        # handler includes it as a direct parent. ConstraintProposalV1's
        # ``source_bindings`` intentionally covers only the design-document
        # sources, but typed lineage must still account for the exact goal input.
        _expect_role_ids(
            typed,
            "source",
            (*params.source_artifact_ids, params.authoring_goal.source_artifact_id),
        )
        _expect_role_ids(typed, "base_constraint", (params.base_constraint_snapshot_artifact_id,))
        return
    if isinstance(params, ReviewRunPayloadV1):
        _expect_role_ids(typed, "snapshot", (params.snapshot_artifact_id,))
        _expect_role_ids(typed, "constraint", (params.constraint_snapshot_artifact_id,))
        return
    if isinstance(params, CheckerRunPayloadV1):
        _expect_role_ids(typed, "snapshot", (params.snapshot_artifact_id,))
        _expect_role_ids(typed, "constraint", (params.constraint_snapshot_artifact_id,))
        return
    if isinstance(params, SimulationRunPayloadV1):
        _expect_role_ids(typed, "snapshot", (params.snapshot_artifact_id,))
        _expect_role_ids(typed, "constraint", (params.constraint_snapshot_artifact_id,))
        _expect_role_ids(typed, "scenario", (params.scenario_artifact_id,))
        return
    if isinstance(params, TaskSuiteDerivePayloadV1):
        _expect_role_ids(typed, "preview", (params.source_preview_artifact_id,))
        _expect_role_ids(typed, "config", (params.config_artifact_id,))
        _expect_role_ids(typed, "constraint", (params.constraint_snapshot_artifact_id,))
        return
    if isinstance(params, PlaytestRunPayloadV1):
        _expect_role_ids(typed, "config", (params.config_artifact_id,))
        _expect_role_ids(typed, "constraint", (params.constraint_snapshot_artifact_id,))
        _expect_role_ids(typed, "task_suite", (params.task_suite_artifact_id,))
        _expect_role_ids(
            typed,
            "selected_scenarios",
            tuple(item.scenario_spec_artifact_id for item in params.episodes),
        )
        return
    if isinstance(params, PatchValidationPayloadV1):
        _expect_role_ids(typed, "subject", (params.subject.subject_artifact_id,))
        _expect_role_ids(typed, "target", (params.preview_snapshot_artifact_id,))
        _expect_role_ids(typed, "constraint", (params.constraint_snapshot_artifact_id,))
        _expect_role_ids(typed, "candidate_config", params.candidate_config_export_artifact_ids)
        _expect_role_ids(
            typed,
            "supporting_evidence",
            tuple(
                sorted(
                    {
                        *params.review_artifact_ids,
                        *params.playtest_trace_artifact_ids,
                        *(item.evidence_artifact_id for item in params.expected_findings),
                        *(item.evidence_artifact_id for item in params.findings),
                    }
                )
            ),
        )
        return
    if isinstance(params, ConstraintValidationPayloadV1):
        _expect_role_ids(typed, "proposal", (params.subject.subject_artifact_id,))
        _expect_role_ids(typed, "base_constraint", (params.base_constraint_snapshot_artifact_id,))
        return
    if isinstance(params, RollbackValidationPayloadV1):
        _expect_role_ids(typed, "subject", (params.subject.subject_artifact_id,))
        _expect_role_ids(typed, "target", (params.target_artifact_id,))
        _expect_role_ids(typed, "current", (params.expected_current_ref.artifact_id,))
        return
    if isinstance(params, BenchRunPayloadV1):
        _expect_role_ids(typed, "dataset", (params.dataset_artifact_id,))
        _expect_role_ids(typed, "benchmark_spec", (params.benchmark_spec_artifact_id,))
        _expect_role_ids(typed, "case_results", params.case_result_artifact_ids)
        return
    if isinstance(params, ArtifactMigrationPayloadV1):
        _expect_role_ids(typed, "source", (params.source_artifact_id,))


def _expect_snapshot(payload: Mapping[str, object], projected: VersionTuple) -> None:
    expected = projected.ir_snapshot_id
    if expected is None:
        _fail("snapshot-bearing payload has no projected IR snapshot identity")
    _expect(_required_field(payload, "snapshot_id"), expected, field="snapshot_id")


def _validate_profile_member(
    payload: Mapping[str, object], field: str, profiles: Sequence[object]
) -> None:
    if field not in payload:
        return
    if not any(_same(payload[field], profile) for profile in profiles):
        _fail("payload profile is not one of the frozen Run profiles", field=field)


def _checker_finding_execution_keys(
    payload: Mapping[str, object],
) -> tuple[tuple[str, str | None], ...]:
    raw_findings = payload.get("findings")
    if raw_findings is None:
        detail = payload.get("detail")
        if isinstance(detail, Mapping):
            raw_findings = detail.get("findings")
    if not isinstance(raw_findings, Sequence) or isinstance(raw_findings, (str, bytes, bytearray)):
        return ()
    keys: list[tuple[str, str | None]] = []
    for finding in raw_findings:
        if not isinstance(finding, Mapping):
            continue
        if finding.get("source") != "checker" or finding.get("oracle_type") != "deterministic":
            continue
        producer_id = finding.get("producer_id")
        constraint_id = finding.get("constraint_id")
        if not isinstance(producer_id, str) or not producer_id:
            _fail("checker Finding has no exact producer", field="findings.producer_id")
        if constraint_id is not None and (not isinstance(constraint_id, str) or not constraint_id):
            _fail("checker Finding has an invalid constraint", field="findings.constraint_id")
        keys.append((producer_id.removeprefix("checker:"), constraint_id))
    return tuple(keys)


def _validate_review_checker_companion(
    *,
    params: ReviewRunPayloadV1,
    payload: Mapping[str, object],
    authoritative_parent_payloads: Mapping[str, Mapping[str, object]] | None,
) -> None:
    """Close Review's checker evidence over profile, input and real executors."""

    _validate_profile_member(payload, "profile", params.checker_profiles)
    _expect(
        _required_field(payload, "checker_profile"),
        _required_field(payload, "profile"),
        field="checker_profile",
    )
    bindings = _required_field(payload, "checker_execution_bindings")
    applications = _required_field(payload, "constraint_application")
    if not isinstance(bindings, Sequence) or isinstance(bindings, (str, bytes, bytearray)):
        _fail("review checker execution bindings are not an array")
    if not isinstance(applications, Sequence) or isinstance(applications, (str, bytes, bytearray)):
        _fail("review checker constraint applications are not an array")

    binding_keys: set[tuple[object, object]] = set()
    scoped_ids: list[str] = []
    for item in bindings:
        if not isinstance(item, Mapping):
            _fail("review checker execution binding is not an object")
        native_id = item.get("native_id")
        constraint_id = item.get("constraint_id")
        if not isinstance(native_id, str) or not native_id:
            _fail("review checker execution binding has no native id")
        binding_keys.add((native_id, constraint_id))
        if constraint_id is not None:
            if not isinstance(constraint_id, str) or not constraint_id:
                _fail("review checker execution binding has an invalid constraint id")
            scoped_ids.append(constraint_id)
    if len(binding_keys) != len(bindings) or len(scoped_ids) != len(set(scoped_ids)):
        _fail("review checker execution bindings are not exact-unique")

    application_keys = {
        (item.get("checker_id"), item.get("constraint_id"))
        for item in applications
        if isinstance(item, Mapping) and item.get("status") == "executed"
    }
    expected_application_keys = {
        (native_id, constraint_id)
        for native_id, constraint_id in binding_keys
        if constraint_id is not None
    }
    if application_keys != expected_application_keys or len(application_keys) != len(applications):
        _fail("review checker applications differ from trusted execution bindings")

    constraint_artifact_id = params.constraint_snapshot_artifact_id
    expected_status = "not_applicable" if constraint_artifact_id is None else "bound"
    _expect(
        _required_field(payload, "constraint_snapshot_binding_status"),
        expected_status,
        field="constraint_snapshot_binding_status",
    )
    _expect(
        payload.get("constraint_snapshot_artifact_id"),
        constraint_artifact_id,
        field="constraint_snapshot_artifact_id",
    )
    expected_constraint_ids = (
        ()
        if constraint_artifact_id is None
        else _constraint_ids_from_parent(authoritative_parent_payloads, constraint_artifact_id)
    )
    if not set(scoped_ids).issubset(expected_constraint_ids):
        _fail("review checker binding names a constraint outside the exact Run input")

    llm_placeholder_ids: set[str] = set()
    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, Sequence) or isinstance(raw_findings, (str, bytes, bytearray)):
        _fail("review checker findings are not an array")
    for finding in raw_findings:
        if not isinstance(finding, Mapping):
            _fail("review checker finding is not an object")
        constraint_id = finding.get("constraint_id")
        if constraint_id is not None and constraint_id not in expected_constraint_ids:
            _fail("review checker Finding names a constraint outside the exact Run input")
        if finding.get("source") == "checker" and finding.get("oracle_type") == "deterministic":
            producer_id = finding.get("producer_id")
            if (producer_id, constraint_id) not in binding_keys:
                _fail("review checker Finding differs from its trusted execution binding")
        elif (
            finding.get("source") == "llm"
            and finding.get("oracle_type") == "llm-assisted"
            and isinstance(constraint_id, str)
        ):
            llm_placeholder_ids.add(constraint_id)
        elif constraint_id is not None:
            _fail("review checker constrained Finding has no exact execution authority")

    if set(scoped_ids) | llm_placeholder_ids != set(expected_constraint_ids):
        _fail("review checker evidence does not close the exact constraint input")


def _validate_patch_payload(
    *,
    run: RunRecord,
    payload: Mapping[str, object],
    projected: VersionTuple,
    related_payloads_by_rule: Mapping[str, Sequence[Mapping[str, object]]],
) -> None:
    if projected.ir_snapshot_id is None:
        _fail("Patch has no projected base snapshot identity", field="base_snapshot_id")
    _expect(
        _required_field(payload, "base_snapshot_id"),
        projected.ir_snapshot_id,
        field="base_snapshot_id",
    )
    _expect(_required_field(payload, "produced_by"), "agent", field="produced_by")
    _expect(_required_field(payload, "producer_run_id"), run.run_id, field="producer_run_id")
    previews = tuple(related_payloads_by_rule.get("preview", ()))
    if len(previews) != 1:
        raise IntegrityViolation(
            "Patch target binding requires exactly one prepared preview payload",
            actual=len(previews),
        )
    _expect(
        _required_field(payload, "target_snapshot_id"),
        compute_snapshot_id(previews[0]),
        field="target_snapshot_id",
    )
    params = run.payload.params
    if isinstance(params, GenerationProposePayloadV1):
        _expect(_required_field(payload, "revision"), 1, field="revision")
        if _optional_field(payload, "supersedes_artifact_id") is not None:
            _fail("initial generation Patch cannot supersede an Artifact")
    elif isinstance(params, PatchRepairPayloadV1):
        _expect(
            _required_field(payload, "supersedes_artifact_id"),
            params.subject_patch_artifact_id,
            field="supersedes_artifact_id",
        )


def _validate_config_payload(
    *,
    run: RunRecord,
    payload: Mapping[str, object],
    typed: TypedLineage,
    projected: VersionTuple,
) -> None:
    _expect(
        _required_field(payload, "source_preview_artifact_id"),
        _one_role_id(typed, "preview"),
        field="source_preview_artifact_id",
    )
    _expect(
        _required_field(payload, "constraint_snapshot_artifact_id"),
        _one_role_id(typed, "constraint"),
        field="constraint_snapshot_artifact_id",
    )
    _expect(
        _required_field(payload, "env_contract_version"),
        projected.env_contract_version,
        field="env_contract_version",
    )
    params = run.payload.params
    profiles = getattr(params, "candidate_export_profiles", ())
    _validate_profile_member(payload, "export_profile", profiles)


def _expected_requirement_ids(run: RunRecord) -> frozenset[str]:
    params = run.payload.params
    if isinstance(params, PatchValidationPayloadV1):
        values = {
            *(f"checker:{_profile_key(profile)}" for profile in params.checker_profiles),
            *(f"simulation:{_profile_key(profile)}" for profile in params.simulation_profiles),
            *(f"regression:{artifact_id}" for artifact_id in params.regression_suite_artifact_ids),
            *(
                f"expected-finding:{binding.finding_id}@{binding.finding_revision}"
                for binding in params.expected_findings
            ),
            *(
                f"finding:{binding.finding_id}@{binding.finding_revision}"
                for binding in params.findings
            ),
            *(f"review:{artifact_id}" for artifact_id in params.review_artifact_ids),
            *(f"playtest:{artifact_id}" for artifact_id in params.playtest_trace_artifact_ids),
        }
        if not values:
            values.add("validation:required-dimension")
        return frozenset(values)
    if isinstance(params, ConstraintValidationPayloadV1):
        return frozenset(
            {
                "compile",
                *(
                    f"regression:{artifact_id}"
                    for artifact_id in params.regression_suite_artifact_ids
                ),
            }
        )
    if isinstance(params, RollbackValidationPayloadV1):
        return frozenset(
            {
                "history",
                "artifact",
                "schema",
                "profile",
                *(f"impact:{_profile_key(profile)}" for profile in params.impact_profiles),
                *(
                    f"regression:{artifact_id}"
                    for artifact_id in params.regression_suite_artifact_ids
                ),
            }
        )
    return frozenset(
        requirement.requirement_id
        for snapshot in run.payload.resolved_policy_snapshots
        for requirement in snapshot.requirements
    )


def _require_dimension_detail(payload: Mapping[str, object]) -> Mapping[str, object]:
    detail = _required_field(payload, "detail")
    if not isinstance(detail, Mapping):
        _fail("validation requirement detail is not an object", field="detail")
    return detail


def _validate_patch_requirement_descriptor(
    *, run: RunRecord, payload: Mapping[str, object], requirement_id: str
) -> None:
    params = run.payload.params
    assert isinstance(params, PatchValidationPayloadV1)

    descriptors: dict[str, tuple[str | None, object | None]] = {
        f"checker:{_profile_key(profile)}": ("checker", profile)
        for profile in params.checker_profiles
    }
    descriptors.update(
        {
            f"simulation:{_profile_key(profile)}": ("simulation", profile)
            for profile in params.simulation_profiles
        }
    )
    descriptors.update(
        {
            f"regression:{artifact_id}": (None, artifact_id)
            for artifact_id in params.regression_suite_artifact_ids
        }
    )
    descriptors.update(
        {
            f"expected-finding:{binding.finding_id}@{binding.finding_revision}": (
                "expected_finding_reverification",
                binding,
            )
            for binding in params.expected_findings
        }
    )
    descriptors.update(
        {
            f"finding:{binding.finding_id}@{binding.finding_revision}": ("finding", binding)
            for binding in params.findings
        }
    )
    descriptors.update(
        {
            f"review:{artifact_id}": ("review", artifact_id)
            for artifact_id in params.review_artifact_ids
        }
    )
    descriptors.update(
        {
            f"playtest:{artifact_id}": ("playtest", artifact_id)
            for artifact_id in params.playtest_trace_artifact_ids
        }
    )
    if not descriptors:
        descriptors["validation:required-dimension"] = ("validation_input", None)

    descriptor = descriptors.get(requirement_id)
    if descriptor is None:
        _fail(
            "patch validation requirement has no exact frozen descriptor",
            field="requirement_id",
        )
    expected_dimension, authority = descriptor
    _expect(payload.get("dimension"), expected_dimension, field="dimension")

    if expected_dimension is None:
        _expect(
            payload.get("suite_artifact_id"),
            authority,
            field="suite_artifact_id",
        )
        return
    if expected_dimension in {"checker", "simulation"}:
        return

    detail = _require_dimension_detail(payload)
    if expected_dimension in {"review", "playtest"}:
        _expect(
            _required_field(detail, "source_artifact_id"),
            authority,
            field="detail.source_artifact_id",
        )
        return
    if expected_dimension in {"finding", "expected_finding_reverification"}:
        binding = authority
        _expect(
            _required_field(detail, "finding_id"),
            binding.finding_id,
            field="detail.finding_id",
        )
        _expect(
            _required_field(detail, "finding_revision"),
            binding.finding_revision,
            field="detail.finding_revision",
        )
        _expect(
            _required_field(detail, "finding_digest"),
            binding.finding_digest,
            field="detail.finding_digest",
        )
        _expect(
            _required_field(detail, "source_artifact_id"),
            binding.evidence_artifact_id,
            field="detail.source_artifact_id",
        )
        return
    if expected_dimension == "validation_input":
        _expect(
            _required_field(detail, "selected_dimension_count"),
            0,
            field="detail.selected_dimension_count",
        )


def _validate_constraint_requirement_descriptor(
    *, run: RunRecord, payload: Mapping[str, object], requirement_id: str
) -> None:
    params = run.payload.params
    assert isinstance(params, ConstraintValidationPayloadV1)
    suites_by_requirement = {
        f"regression:{artifact_id}": artifact_id
        for artifact_id in params.regression_suite_artifact_ids
    }
    suite_id = suites_by_requirement.get(requirement_id)
    if suite_id is None:
        _fail(
            "constraint validation requirement has no exact frozen descriptor",
            field="requirement_id",
        )
    _expect(payload.get("dimension"), None, field="dimension")
    _expect(payload.get("suite_artifact_id"), suite_id, field="suite_artifact_id")


def _validate_rollback_requirement_descriptor(
    *, run: RunRecord, payload: Mapping[str, object], requirement_id: str
) -> None:
    params = run.payload.params
    assert isinstance(params, RollbackValidationPayloadV1)
    descriptors: dict[str, tuple[str, object | None]] = {
        "history": ("history", None),
        "artifact": ("artifact", None),
        "schema": ("schema", None),
        "profile": ("profile", None),
    }
    descriptors.update(
        {
            f"impact:{_profile_key(profile)}": ("impact", index)
            for index, profile in enumerate(params.impact_profiles)
        }
    )
    descriptors.update(
        {
            f"regression:{artifact_id}": ("regression", artifact_id)
            for artifact_id in params.regression_suite_artifact_ids
        }
    )
    descriptor = descriptors.get(requirement_id)
    if descriptor is None:
        _fail(
            "rollback validation requirement has no exact frozen descriptor",
            field="requirement_id",
        )
    expected_dimension, authority = descriptor
    _expect(payload.get("dimension"), expected_dimension, field="dimension")
    detail = _require_dimension_detail(payload)

    if expected_dimension == "impact":
        field_path = f"/params/impact_profiles/{authority}"
        bindings = tuple(
            binding for binding in run.payload.resolved_profiles if binding.field_path == field_path
        )
        if len(bindings) != 1:
            _fail(
                "rollback impact requirement has no exact frozen profile binding",
                field="detail.impact_profile_binding",
            )
        _expect(
            _required_field(detail, "impact_profile_binding"),
            bindings[0],
            field="detail.impact_profile_binding",
        )
    elif expected_dimension == "regression":
        _expect(
            _required_field(detail, "suite_artifact_id"),
            authority,
            field="detail.suite_artifact_id",
        )


def _validate_validation_requirement_descriptor(
    *, run: RunRecord, payload: Mapping[str, object]
) -> None:
    requirement_id = _required_field(payload, "requirement_id")
    if not isinstance(requirement_id, str):
        _fail("validation requirement identity is not a string", field="requirement_id")
    params = run.payload.params
    if isinstance(params, PatchValidationPayloadV1):
        _validate_patch_requirement_descriptor(
            run=run,
            payload=payload,
            requirement_id=requirement_id,
        )
    elif isinstance(params, ConstraintValidationPayloadV1):
        _validate_constraint_requirement_descriptor(
            run=run,
            payload=payload,
            requirement_id=requirement_id,
        )
    elif isinstance(params, RollbackValidationPayloadV1):
        _validate_rollback_requirement_descriptor(
            run=run,
            payload=payload,
            requirement_id=requirement_id,
        )


def _validate_regression_payload(
    *,
    run: RunRecord,
    payload: Mapping[str, object],
    typed: TypedLineage,
    projected: VersionTuple,
    authoritative_parent_payloads: Mapping[str, Mapping[str, object]] | None,
) -> None:
    if "snapshot_id" in payload:
        _expect(payload["snapshot_id"], projected.ir_snapshot_id, field="snapshot_id")
    if "root_seed" in payload:
        _expect(payload["root_seed"], projected.seed, field="root_seed")
    if "run_kind" in payload:
        _expect(payload["run_kind"], run.kind, field="run_kind")
    params = run.payload.params
    profile = None
    if isinstance(params, PatchValidationPayloadV1):
        if payload.get("dimension") != "simulation":
            profile = params.validation_policy
    elif isinstance(params, ConstraintValidationPayloadV1):
        profile = params.validation_policy
    elif isinstance(params, RollbackValidationPayloadV1):
        profile = params.rollback_profile
    elif isinstance(params, PatchRepairPayloadV1):
        profile = params.repair_policy
    if profile is not None:
        if "profile_id" in payload:
            _expect(payload["profile_id"], profile.profile_id, field="profile_id")
        if "profile_version" in payload:
            _expect(payload["profile_version"], profile.version, field="profile_version")
    suite_id = payload.get("suite_artifact_id")
    semantic_suite_id = suite_id
    if isinstance(params, RollbackValidationPayloadV1):
        detail = payload.get("detail")
        if payload.get("dimension") == "regression" and isinstance(detail, Mapping):
            semantic_suite_id = detail.get("suite_artifact_id")
    if isinstance(params, (PatchValidationPayloadV1, RollbackValidationPayloadV1)):
        lineage_suite_raw = payload.get("lineage_suite_artifact_ids")
        if not isinstance(lineage_suite_raw, Sequence) or isinstance(
            lineage_suite_raw, (str, bytes, bytearray)
        ):
            _fail(
                "validation regression evidence lacks its exact suite lineage selector",
                field="lineage_suite_artifact_ids",
            )
        lineage_suite_ids = tuple(lineage_suite_raw)
        expected_suite_ids = () if semantic_suite_id is None else (semantic_suite_id,)
        if lineage_suite_ids != expected_suite_ids:
            _fail(
                "regression suite lineage selector differs from its semantic suite",
                field="lineage_suite_artifact_ids",
            )
        typed_suite_ids = _role_ids(typed, "regression_suite")
        if typed_suite_ids != expected_suite_ids:
            _fail(
                "regression suite lineage selector differs from its exact typed parent",
                field="lineage_suite_artifact_ids",
            )
    regression_ids = tuple(getattr(params, "regression_suite_artifact_ids", ()))
    if suite_id is not None and suite_id not in regression_ids:
        _fail("regression payload suite is not frozen in the Run", field="suite_artifact_id")
    if isinstance(params, PatchValidationPayloadV1) and isinstance(suite_id, str):
        coverage = payload.get("execution_coverage_binding")
        status = payload.get("status")
        if status in {"passed", "failed"}:
            root_seed = _required_field(payload, "root_seed")
            execution_seed = _required_field(payload, "seed")
            if (
                isinstance(root_seed, bool)
                or not isinstance(root_seed, int)
                or isinstance(execution_seed, bool)
                or not isinstance(execution_seed, int)
                or projected.env_contract_version is None
            ):
                _fail("executed regression suite lacks exact coverage authority")
            expected_coverage = regression_suite_execution_coverage_binding(
                suite_artifact_id=suite_id,
                validation_profile=params.validation_policy,
                constraint_snapshot_artifact_id=(params.constraint_snapshot_artifact_id),
                env_contract_version=projected.env_contract_version,
                root_seed=root_seed,
                run_kind=run.kind,
                execution_seed=execution_seed,
            )
            _expect(
                _required_field(payload, "execution_coverage_binding"),
                expected_coverage,
                field="execution_coverage_binding",
            )
        elif coverage is not None:
            _fail("unproven regression suite carries deterministic execution coverage")
    requirement_id = payload.get("requirement_id")
    if requirement_id is not None and requirement_id not in _expected_requirement_ids(run):
        _fail("regression payload requirement is not frozen in the Run", field="requirement_id")
    if isinstance(
        params,
        (
            PatchValidationPayloadV1,
            ConstraintValidationPayloadV1,
            RollbackValidationPayloadV1,
        ),
    ):
        _validate_validation_requirement_descriptor(run=run, payload=payload)
    checker_authority_fields = frozenset(
        {
            "checker_profile",
            "checker_execution_bindings",
            "constraint_snapshot_binding_status",
            "constraint_snapshot_artifact_id",
        }
    )
    checker_authority_present = any(field in payload for field in checker_authority_fields)
    checker_dimension = (
        isinstance(params, PatchValidationPayloadV1) and payload.get("dimension") == "checker"
    )
    if checker_dimension:
        profile_raw = _required_field(payload, "checker_profile")
        selected_profiles = tuple(
            profile for profile in params.checker_profiles if _same(profile_raw, profile)
        )
        if len(selected_profiles) != 1:
            _fail(
                "checker evidence profile is not one exact frozen Run profile",
                field="checker_profile",
            )
        selected_profile = selected_profiles[0]
        _expect(
            requirement_id,
            f"checker:{_profile_key(selected_profile)}",
            field="requirement_id",
        )
        expected_constraint_status = (
            "not_applicable" if params.constraint_snapshot_artifact_id is None else "bound"
        )
        _expect(
            _required_field(payload, "constraint_snapshot_binding_status"),
            expected_constraint_status,
            field="constraint_snapshot_binding_status",
        )
        _expect(
            payload.get("constraint_snapshot_artifact_id"),
            params.constraint_snapshot_artifact_id,
            field="constraint_snapshot_artifact_id",
        )
        exact_constraint_ids = (
            frozenset()
            if params.constraint_snapshot_artifact_id is None
            else frozenset(
                _constraint_ids_from_parent(
                    authoritative_parent_payloads,
                    params.constraint_snapshot_artifact_id,
                )
            )
        )
        bindings = _required_field(payload, "checker_execution_bindings")
        if (
            not isinstance(bindings, Sequence)
            or isinstance(bindings, (str, bytes, bytearray))
            or not bindings
        ):
            _fail(
                "checker evidence lacks deterministic execution bindings",
                field="checker_execution_bindings",
            )
        execution_keys: set[tuple[str, str | None]] = set()
        for index, binding in enumerate(bindings):
            if not isinstance(binding, Mapping):
                _fail(
                    "checker execution binding is not an object",
                    field=f"checker_execution_bindings/{index}",
                )
            wrapper_id = binding.get("wrapper_id")
            native_id = binding.get("native_id")
            constraint_id = binding.get("constraint_id")
            if not isinstance(wrapper_id, str) or not wrapper_id:
                _fail(
                    "checker execution binding has no wrapper identity",
                    field=f"checker_execution_bindings/{index}/wrapper_id",
                )
            if not isinstance(native_id, str) or not native_id:
                _fail(
                    "checker execution binding has no native identity",
                    field=f"checker_execution_bindings/{index}/native_id",
                )
            if constraint_id is not None and (
                not isinstance(constraint_id, str) or not constraint_id
            ):
                _fail(
                    "checker execution binding has an invalid constraint identity",
                    field=f"checker_execution_bindings/{index}/constraint_id",
                )
            if constraint_id is not None and constraint_id not in exact_constraint_ids:
                _fail(
                    "checker execution binding names a constraint outside the exact Run input",
                    field=f"checker_execution_bindings/{index}/constraint_id",
                )
            execution_keys.add((native_id, constraint_id))
        deterministic_finding_keys = _checker_finding_execution_keys(payload)
        for finding_key in deterministic_finding_keys:
            if finding_key not in execution_keys:
                _fail(
                    "checker Finding differs from its trusted execution binding",
                    field="findings.producer_id",
                )
        raw_findings = payload.get("findings")
        if raw_findings is None:
            detail = payload.get("detail")
            raw_findings = detail.get("findings") if isinstance(detail, Mapping) else None
        if not isinstance(raw_findings, Sequence) or isinstance(
            raw_findings, (str, bytes, bytearray)
        ):
            _fail("checker evidence findings are not an array")
        llm_constraint_ids = (
            frozenset()
            if params.constraint_snapshot_artifact_id is None
            else _llm_constraint_ids_from_parent(
                authoritative_parent_payloads,
                params.constraint_snapshot_artifact_id,
            )
        )
        for finding in raw_findings:
            if not isinstance(finding, Mapping):
                _fail("checker evidence Finding is not an object")
            if finding.get("source") == "checker" and finding.get("oracle_type") == "deterministic":
                continue
            if not (
                finding.get("source") == "llm"
                and finding.get("oracle_type") == "llm-assisted"
                and finding.get("status") == "unproven"
                and finding.get("producer_id") == "llm-routed"
                and finding.get("defect_class") == "llm_assisted_predicate"
                and finding.get("constraint_id") in llm_constraint_ids
            ):
                _fail("checker evidence Finding differs from exact execution authority")
    elif checker_authority_present:
        _fail(
            "non-checker regression evidence carries checker execution authority",
            field="checker_execution_bindings",
        )
    simulation_authority = payload.get("simulation_execution_binding")
    simulation_dimension = (
        isinstance(params, PatchValidationPayloadV1) and payload.get("dimension") == "simulation"
    )
    if simulation_dimension:
        if not isinstance(simulation_authority, Mapping):
            _fail("simulation evidence lacks its exact execution binding")
        profile_raw = _required_field(simulation_authority, "simulation_profile")
        selected_profiles = tuple(
            profile for profile in params.simulation_profiles if _same(profile_raw, profile)
        )
        if len(selected_profiles) != 1:
            _fail("simulation evidence profile is not one exact frozen Run profile")
        selected_profile = selected_profiles[0]
        simulation_requirement_id = f"simulation:{_profile_key(selected_profile)}"
        _expect(requirement_id, simulation_requirement_id, field="requirement_id")
        root_seed = projected.seed
        if root_seed is None:
            _fail(
                "simulation evidence has no frozen Run root seed",
                field="simulation_execution_binding.seed_binding.root_seed",
            )
        execution_seed = derive_validation_subseed(
            root_seed=root_seed,
            run_kind=run.kind,
            profile=selected_profile,
            case_id=simulation_requirement_id,
            replication_index=0,
        )
        expected_seed_binding = {
            "root_seed": root_seed,
            "run_kind": run.kind.model_dump(mode="json"),
            "profile_id": selected_profile.profile_id,
            "profile_version": selected_profile.version,
            "case_id": simulation_requirement_id,
            "replication_index": 0,
            "seed": execution_seed,
            "seed_derivation_version": VALIDATION_SEED_DERIVATION_VERSION,
        }
        _expect(
            _required_field(simulation_authority, "execution_mode"),
            PATCH_SIMULATION_EXECUTION_MODE_V1,
            field="simulation_execution_binding.execution_mode",
        )
        _expect(
            _required_field(simulation_authority, "seed_binding"),
            expected_seed_binding,
            field="simulation_execution_binding.seed_binding",
        )
        seed_authority: Mapping[str, object] = payload
        if "root_seed" not in seed_authority:
            detail = payload.get("detail")
            if not isinstance(detail, Mapping):
                _fail("simulation evidence has no outer seed authority")
            seed_authority = detail
        for field, expected in expected_seed_binding.items():
            _expect(
                _required_field(seed_authority, field),
                expected,
                field=field,
            )
        _expect(
            _required_field(simulation_authority, "producer_id"),
            "economy_sim",
            field="simulation_execution_binding.producer_id",
        )
        constraint_artifact_id = params.constraint_snapshot_artifact_id
        expected_constraint_status = "not_applicable" if constraint_artifact_id is None else "bound"
        _expect(
            _required_field(simulation_authority, "constraint_snapshot_binding_status"),
            expected_constraint_status,
            field="simulation_execution_binding.constraint_snapshot_binding_status",
        )
        _expect(
            simulation_authority.get("constraint_snapshot_artifact_id"),
            constraint_artifact_id,
            field="simulation_execution_binding.constraint_snapshot_artifact_id",
        )
        expected_constraint_ids = (
            ()
            if constraint_artifact_id is None
            else _constraint_ids_from_parent(
                authoritative_parent_payloads,
                constraint_artifact_id,
            )
        )
        _expect(
            _required_field(simulation_authority, "constraint_ids"),
            expected_constraint_ids,
            field="simulation_execution_binding.constraint_ids",
        )
        expected_application = (
            {"status": "not_applicable"}
            if constraint_artifact_id is None
            else {
                "status": "unproven",
                "reason_code": "constraint_profile_not_executable",
            }
        )
        _expect(
            _required_field(simulation_authority, "constraint_application"),
            expected_application,
            field="simulation_execution_binding.constraint_application",
        )
        if constraint_artifact_id is not None:
            _expect(payload.get("status"), "unproven", field="status")
        raw_findings = payload.get("findings")
        if raw_findings is None:
            detail = payload.get("detail")
            raw_findings = detail.get("findings") if isinstance(detail, Mapping) else None
        if not isinstance(raw_findings, Sequence) or isinstance(
            raw_findings, (str, bytes, bytearray)
        ):
            _fail("simulation evidence findings are not an array")
        for finding in raw_findings:
            if not isinstance(finding, Mapping) or (
                finding.get("source") != "sim"
                or finding.get("oracle_type") != "simulation"
                or finding.get("producer_id") != "economy_sim"
            ):
                _fail("simulation Finding differs from its exact execution binding")
    elif simulation_authority is not None:
        _fail("non-simulation regression evidence carries simulation execution authority")
    if isinstance(params, RollbackValidationPayloadV1):
        detail = _required_field(payload, "detail")
        if not isinstance(detail, Mapping):
            _fail("rollback validation evidence detail is not an object", field="detail")
        _expect(
            _required_field(detail, "current_artifact_id"),
            params.expected_current_ref.artifact_id,
            field="detail.current_artifact_id",
        )
        _expect(
            _required_field(detail, "target_artifact_id"),
            params.target_artifact_id,
            field="detail.target_artifact_id",
        )
        rollback_bindings = tuple(
            item
            for item in run.payload.resolved_profiles
            if item.field_path == "/params/rollback_profile"
        )
        if len(rollback_bindings) != 1:
            _fail("rollback Run has no exact rollback profile binding")
        rollback_binding = rollback_bindings[0]
        dimension = _required_field(payload, "dimension")
        if dimension == "profile":
            _expect(
                _required_field(detail, "rollback_profile_binding"),
                rollback_binding,
                field="detail.rollback_profile_binding",
            )
        elif dimension == "schema":
            schema_bindings = tuple(
                item
                for item in run.payload.resolved_profiles
                if item.field_path == "/params/schema_compatibility_policy"
            )
            if len(schema_bindings) != 1:
                _fail("rollback Run has no exact schema profile binding")
            _expect(
                _required_field(detail, "schema_profile_binding"),
                schema_bindings[0],
                field="detail.schema_profile_binding",
            )
            _expect(
                _required_field(detail, "rollback_profile_binding"),
                rollback_binding,
                field="detail.rollback_profile_binding",
            )
        elif dimension == "impact":
            _expect(
                _required_field(detail, "current_artifact_id"),
                params.expected_current_ref.artifact_id,
                field="detail.current_artifact_id",
            )
            _expect(
                _required_field(detail, "current_ref_revision"),
                params.expected_current_ref.revision,
                field="detail.current_ref_revision",
            )
            impact_binding = detail.get("impact_profile_binding")
            if not any(
                impact_binding == item.model_dump(mode="json")
                for item in run.payload.resolved_profiles
                if item.field_path.startswith("/params/impact_profiles/")
            ):
                _fail(
                    "rollback impact evidence uses an unfrozen profile binding",
                    field="detail.impact_profile_binding",
                )
            _expect(
                _required_field(detail, "rollback_profile_binding"),
                rollback_binding,
                field="detail.rollback_profile_binding",
            )
        elif dimension == "regression":
            suite = _required_field(detail, "suite_artifact_id")
            if suite not in params.regression_suite_artifact_ids:
                _fail(
                    "rollback regression evidence uses an unfrozen suite",
                    field="detail.suite_artifact_id",
                )
            if not isinstance(suite, str):
                _fail(
                    "rollback regression evidence suite is not an Artifact ID",
                    field="detail.suite_artifact_id",
                )
            _expect(
                _required_field(payload, "requirement_id"),
                f"regression:{suite}",
                field="requirement_id",
            )
            _expect(
                _required_field(detail, "payload_schema_version"),
                "regression-evidence@1",
                field="detail.payload_schema_version",
            )
            _expect(
                _required_field(detail, "snapshot_id"),
                projected.ir_snapshot_id,
                field="detail.snapshot_id",
            )
            _expect(
                _required_field(detail, "status"),
                _required_field(payload, "status"),
                field="detail.status",
            )
            root_seed = _required_field(detail, "root_seed")
            _expect(root_seed, projected.seed, field="detail.root_seed")
            _expect(
                _required_field(detail, "run_kind"),
                run.kind,
                field="detail.run_kind",
            )
            _expect(
                _required_field(detail, "profile_id"),
                params.rollback_profile.profile_id,
                field="detail.profile_id",
            )
            _expect(
                _required_field(detail, "profile_version"),
                params.rollback_profile.version,
                field="detail.profile_version",
            )
            _expect(_required_field(detail, "case_id"), suite, field="detail.case_id")
            _expect(
                _required_field(detail, "replication_index"),
                0,
                field="detail.replication_index",
            )
            if isinstance(root_seed, bool) or not isinstance(root_seed, int):
                _fail(
                    "rollback regression evidence root seed is not an integer",
                    field="detail.root_seed",
                )
            expected_seed = derive_validation_subseed(
                root_seed=root_seed,
                run_kind=run.kind,
                profile=params.rollback_profile,
                case_id=suite,
                replication_index=0,
            )
            _expect(
                _required_field(detail, "seed"),
                expected_seed,
                field="detail.seed",
            )
            _expect(
                _required_field(detail, "seed_derivation_version"),
                VALIDATION_SEED_DERIVATION_VERSION,
                field="detail.seed_derivation_version",
            )
            _expect(
                _required_field(detail, "rollback_profile_binding"),
                rollback_binding,
                field="detail.rollback_profile_binding",
            )


def _validate_target_binding(
    *, run: RunRecord, target: Mapping[str, object], typed: TypedLineage, projected: VersionTuple
) -> None:
    params = run.payload.params
    if isinstance(params, PatchValidationPayloadV1):
        _expect(
            _required_field(target, "target_artifact_id"),
            params.preview_snapshot_artifact_id,
            field="target_binding.target_artifact_id",
        )
        _expect(
            _required_field(target, "target_snapshot_id"),
            projected.ir_snapshot_id,
            field="target_binding.target_snapshot_id",
        )
        _expect(
            _required_field(target, "ref_name"),
            params.target.ref_name,
            field="target_binding.ref_name",
        )
        _expect(
            target.get("expected_ref"),
            params.target.expected_ref,
            field="target_binding.expected_ref",
        )
    elif isinstance(params, ConstraintValidationPayloadV1):
        _expect(
            _required_field(target, "target_artifact_id"),
            _one_role_id(typed, "candidate"),
            field="target_binding.target_artifact_id",
        )
        _expect(
            _required_field(target, "target_snapshot_id"),
            projected.constraint_snapshot_id,
            field="target_binding.target_snapshot_id",
        )
        _expect(
            _required_field(target, "ref_name"),
            params.target.ref_name,
            field="target_binding.ref_name",
        )
        _expect(
            target.get("expected_ref"),
            params.target.expected_ref,
            field="target_binding.expected_ref",
        )
    elif isinstance(params, RollbackValidationPayloadV1):
        _expect(
            _required_field(target, "target_artifact_id"),
            params.target_artifact_id,
            field="target_binding.target_artifact_id",
        )
        _expect(
            _required_field(target, "ref_name"), params.ref_name, field="target_binding.ref_name"
        )
        _expect(
            _required_field(target, "expected_ref"),
            params.expected_current_ref,
            field="target_binding.expected_ref",
        )


def _expected_evidence_supporting_ids(
    *,
    run: RunRecord,
    final_artifact_ids_by_rule: Mapping[str, Sequence[str]],
) -> tuple[str, ...]:
    """Derive the complete EvidenceSet support closure from frozen authorities."""

    params = run.payload.params

    def sibling_ids(rule_id: str) -> tuple[str, ...]:
        return tuple(final_artifact_ids_by_rule.get(rule_id, ()))

    if isinstance(params, PatchValidationPayloadV1):
        values = (
            *sibling_ids("regression"),
            *(
                (params.constraint_snapshot_artifact_id,)
                if params.constraint_snapshot_artifact_id is not None
                else ()
            ),
            *params.candidate_config_export_artifact_ids,
            *params.review_artifact_ids,
            *params.playtest_trace_artifact_ids,
            *params.regression_suite_artifact_ids,
            *(item.evidence_artifact_id for item in params.expected_findings),
            *(item.evidence_artifact_id for item in params.findings),
        )
    elif isinstance(params, ConstraintValidationPayloadV1):
        values = (
            *sibling_ids("candidate"),
            *sibling_ids("compile-evidence"),
            *sibling_ids("regression"),
            *(
                (params.base_constraint_snapshot_artifact_id,)
                if params.base_constraint_snapshot_artifact_id is not None
                else ()
            ),
            *params.regression_suite_artifact_ids,
            *(
                (params.golden_suite_artifact_id,)
                if params.golden_suite_artifact_id is not None
                else ()
            ),
        )
    elif isinstance(params, RollbackValidationPayloadV1):
        values = (
            *sibling_ids("regression"),
            params.expected_current_ref.artifact_id,
            params.target_artifact_id,
            *params.regression_suite_artifact_ids,
        )
    else:
        raise IntegrityViolation("EvidenceSet is bound to a non-validation Run payload")
    # One immutable Artifact can legitimately satisfy more than one frozen
    # supporting source (for example, a finding's evidence may also be one of the
    # explicitly bound review Artifacts).  EvidenceSet canonicalizes the resulting
    # support closure as a stable unique set; uniqueness *within* each source field
    # remains enforced by that field's own closed payload model.
    return tuple(sorted(set(values)))


def _validate_evidence_finding_bindings(*, run: RunRecord, payload: Mapping[str, object]) -> None:
    params = run.payload.params
    expected = (
        tuple(
            item.model_dump(mode="json")
            for item in sorted(
                (*params.expected_findings, *params.findings),
                key=lambda binding: (binding.finding_id, binding.finding_revision),
            )
        )
        if isinstance(params, PatchValidationPayloadV1)
        else ()
    )
    _expect(
        _required_field(payload, "finding_bindings"),
        expected,
        field="finding_bindings",
    )


def _validate_exact_evidence_support(
    *,
    run: RunRecord,
    payload: Mapping[str, object],
    final_artifact_ids_by_rule: Mapping[str, Sequence[str]],
) -> None:
    expected = _expected_evidence_supporting_ids(
        run=run,
        final_artifact_ids_by_rule=final_artifact_ids_by_rule,
    )
    supporting = _required_field(payload, "supporting_artifact_ids")
    if not isinstance(supporting, Sequence) or isinstance(supporting, (str, bytes)):
        _fail("EvidenceSet supporting_artifact_ids are not an array")
    if tuple(supporting) != expected:
        raise IntegrityViolation(
            "EvidenceSet supporting_artifact_ids differ from the exact frozen closure",
            expected=expected,
            actual=tuple(supporting),
        )
    _validate_evidence_finding_bindings(run=run, payload=payload)


def _validate_evidence_set(
    *, run: RunRecord, payload: Mapping[str, object], typed: TypedLineage, projected: VersionTuple
) -> None:
    params = run.payload.params
    subject = getattr(params, "subject")
    _expect(
        _required_field(payload, "subject_artifact_id"),
        subject.subject_artifact_id,
        field="subject_artifact_id",
    )
    _expect(
        _required_field(payload, "subject_digest"),
        subject.subject_digest,
        field="subject_digest",
    )
    _expect(_required_field(payload, "validation_run_id"), run.run_id, field="validation_run_id")
    profile = (
        params.rollback_profile
        if isinstance(params, RollbackValidationPayloadV1)
        else params.validation_policy
    )
    _expect(
        _required_field(payload, "policy_version"),
        _profile_key(profile),
        field="policy_version",
    )
    target = payload.get("target_binding")
    if isinstance(params, ConstraintValidationPayloadV1) and not _role_ids(typed, "candidate"):
        if target is not None:
            _fail("candidate-free constraint validation cannot claim a target binding")
    else:
        if not isinstance(target, Mapping):
            _fail("validation EvidenceSet requires its authoritative target binding")
        _validate_target_binding(run=run, target=target, typed=typed, projected=projected)

    requirements = payload.get("requirements")
    if not isinstance(requirements, Sequence) or isinstance(requirements, (str, bytes)):
        _fail("EvidenceSet requirements are not an array")
    actual_ids = tuple(
        item.get("requirement_id") if isinstance(item, Mapping) else None for item in requirements
    )
    expected_ids = _expected_requirement_ids(run)
    if (
        None in actual_ids
        or len(actual_ids) != len(set(actual_ids))
        or set(actual_ids) != expected_ids
    ):
        raise IntegrityViolation(
            "EvidenceSet requirements differ from the frozen Run dimensions",
            expected=tuple(sorted(expected_ids)),
            actual=tuple(sorted(value for value in actual_ids if isinstance(value, str))),
        )
    evidence_parent_ids = {
        parent.artifact_id
        for role in ("regression", "compile_evidence")
        for parent in _role_parents(typed, role)
    }
    claimed_evidence_ids = {
        item["evidence_artifact_id"]
        for item in requirements
        if isinstance(item, Mapping) and item.get("evidence_artifact_id") is not None
    }
    if not claimed_evidence_ids.issubset(evidence_parent_ids):
        _fail("EvidenceSet requirement references a non-lineage evidence Artifact")
    _validate_exact_evidence_support(
        run=run,
        payload=payload,
        final_artifact_ids_by_rule={
            "candidate": _role_ids(typed, "candidate"),
            "compile-evidence": _role_ids(typed, "compile_evidence"),
            "regression": _role_ids(typed, "regression"),
        },
    )


def _validate_payload_semantics(
    *,
    run: RunRecord,
    rule: OutcomeArtifactRuleV1,
    payload_schema_id: str,
    payload: Mapping[str, object],
    typed: TypedLineage,
    projected: VersionTuple,
    related_payloads_by_rule: Mapping[str, Sequence[Mapping[str, object]]],
    authoritative_parent_payloads: Mapping[str, Mapping[str, object]] | None,
) -> None:
    params = run.payload.params
    if payload_schema_id == "patch@2":
        _validate_patch_payload(
            run=run,
            payload=payload,
            projected=projected,
            related_payloads_by_rule=related_payloads_by_rule,
        )
    elif payload_schema_id == "config-export-package@1":
        _validate_config_payload(run=run, payload=payload, typed=typed, projected=projected)
    elif payload_schema_id == "checker-report@1":
        _expect_snapshot(payload, projected)
        if isinstance(params, CheckerRunPayloadV1):
            _expect(
                _required_field(payload, "checker_profile"),
                params.checker_profile,
                field="checker_profile",
            )
            expected_constraint_status = (
                "not_applicable" if params.constraint_snapshot_artifact_id is None else "bound"
            )
            _expect(
                _required_field(payload, "constraint_snapshot_binding_status"),
                expected_constraint_status,
                field="constraint_snapshot_binding_status",
            )
            _expect(
                payload.get("constraint_snapshot_artifact_id"),
                params.constraint_snapshot_artifact_id,
                field="constraint_snapshot_artifact_id",
            )
            _expect(
                _required_field(payload, "checker_ids"), params.checker_ids, field="checker_ids"
            )
            _expect(
                _required_field(payload, "defect_classes"),
                params.defect_classes,
                field="defect_classes",
            )
            applications = _required_field(payload, "constraint_application")
            if not isinstance(applications, Sequence) or isinstance(
                applications, (str, bytes, bytearray)
            ):
                _fail("checker constraint_application is not an array")
            if params.constraint_snapshot_artifact_id is None:
                _expect(applications, (), field="constraint_application")
            else:
                expected_constraint_ids = _constraint_ids_from_parent(
                    authoritative_parent_payloads,
                    params.constraint_snapshot_artifact_id,
                )
                actual_constraint_ids = tuple(
                    item.get("constraint_id") if isinstance(item, Mapping) else None
                    for item in applications
                )
                _expect(
                    actual_constraint_ids,
                    expected_constraint_ids,
                    field="constraint_application.constraint_ids",
                )
            direct_ids = {value for value in params.checker_ids if isinstance(value, str)}
            application_keys = {
                (item.get("checker_id"), item.get("constraint_id"))
                for item in applications
                if isinstance(item, Mapping)
            }
            for producer_id, constraint_id in _checker_finding_execution_keys(payload):
                if constraint_id is None:
                    if producer_id not in direct_ids:
                        _fail(
                            "checker Finding producer is not a selected direct executor",
                            field="findings.producer_id",
                        )
                elif (producer_id, constraint_id) not in application_keys:
                    _fail(
                        "checker Finding differs from its exact constraint execution",
                        field="findings.constraint_id",
                    )
        elif isinstance(params, ReviewRunPayloadV1):
            _validate_review_checker_companion(
                params=params,
                payload=payload,
                authoritative_parent_payloads=authoritative_parent_payloads,
            )
    elif payload_schema_id == "simulation-result@1":
        _expect_snapshot(payload, projected)
        if "seed" in payload:
            _expect(payload["seed"], projected.seed, field="seed")
        if isinstance(params, SimulationRunPayloadV1):
            _expect(
                _required_field(payload, "profile"),
                params.simulation_profile,
                field="profile",
            )
            _expect(_required_field(payload, "seed"), projected.seed, field="seed")
            _expect(
                _required_field(payload, "replication_count"),
                params.replication_count,
                field="replication_count",
            )
            _expect(
                _required_field(payload, "horizon_steps"),
                params.horizon_steps,
                field="horizon_steps",
            )
            _required_field(payload, "invariants")
            sensitivity = _required_field(payload, "sensitivity")
            if not isinstance(sensitivity, Mapping):
                _fail("simulation sensitivity is not an object")
            execution = sensitivity.get("execution_binding")
            if not isinstance(execution, Mapping):
                _fail("simulation result lacks exact execution binding")
            _expect(
                execution.get("simulation_profile"),
                params.simulation_profile,
                field="execution_binding.simulation_profile",
            )
            _expect(
                execution.get("workload_profile"),
                params.workload_profile,
                field="execution_binding.workload_profile",
            )
            _expect(
                execution.get("constraint_snapshot_artifact_id"),
                params.constraint_snapshot_artifact_id,
                field="execution_binding.constraint_snapshot_artifact_id",
            )
            _expect(
                execution.get("scenario_artifact_id"),
                params.scenario_artifact_id,
                field="execution_binding.scenario_artifact_id",
            )
            constraint_ids = execution.get("constraint_ids")
            if params.constraint_snapshot_artifact_id is None:
                _expect(constraint_ids, (), field="execution_binding.constraint_ids")
                expected_constraint_application = {"status": "not_applicable"}
            else:
                if not isinstance(constraint_ids, Sequence) or isinstance(
                    constraint_ids, (str, bytes, bytearray)
                ):
                    _fail("simulation constraint input has no exact application evidence")
                expected_constraint_application = {
                    "status": "unproven",
                    "reason_code": "constraint_profile_not_executable",
                }
                _expect(
                    tuple(constraint_ids),
                    _constraint_ids_from_parent(
                        authoritative_parent_payloads,
                        params.constraint_snapshot_artifact_id,
                    ),
                    field="execution_binding.constraint_ids",
                )
            _expect(
                execution.get("constraint_application"),
                expected_constraint_application,
                field="execution_binding.constraint_application",
            )
            if params.scenario_artifact_id is None:
                _expect(execution.get("scenario_id"), None, field="execution_binding.scenario_id")
                expected_scenario_application = {"status": "not_applicable"}
            else:
                scenario_id = execution.get("scenario_id")
                if not isinstance(scenario_id, str) or not scenario_id:
                    _fail("simulation scenario input has no exact application evidence")
                scenario_payload = _authoritative_parent_payload(
                    authoritative_parent_payloads,
                    params.scenario_artifact_id,
                )
                _expect(
                    scenario_id,
                    _scenario_id_from_parent(
                        authoritative_parent_payloads,
                        params.scenario_artifact_id,
                    ),
                    field="execution_binding.scenario_id",
                )
                _expect(
                    scenario_payload.get("source_preview_artifact_id"),
                    params.snapshot_artifact_id,
                    field="scenario.source_preview_artifact_id",
                )
                _expect(
                    scenario_payload.get("constraint_snapshot_artifact_id"),
                    params.constraint_snapshot_artifact_id,
                    field="scenario.constraint_snapshot_artifact_id",
                )
                _expect(
                    scenario_payload.get("env_contract_version"),
                    projected.env_contract_version,
                    field="scenario.env_contract_version",
                )
                expected_scenario_application = {
                    "status": "unproven",
                    "reason_code": "scenario_reset_not_executable",
                }
            _expect(
                execution.get("scenario_application"),
                expected_scenario_application,
                field="execution_binding.scenario_application",
            )
        elif isinstance(params, ReviewRunPayloadV1):
            _validate_profile_member(payload, "profile", params.simulation_profiles)
            profile = _required_field(payload, "profile")
            sensitivity = _required_field(payload, "sensitivity")
            if not isinstance(sensitivity, Mapping):
                _fail("review simulation sensitivity is not an object")
            execution = sensitivity.get("execution_binding")
            if not isinstance(execution, Mapping):
                _fail("review simulation lacks exact constraint application evidence")
            _expect(
                execution.get("simulation_profile"),
                profile,
                field="execution_binding.simulation_profile",
            )
            _expect(
                execution.get("constraint_snapshot_artifact_id"),
                params.constraint_snapshot_artifact_id,
                field="execution_binding.constraint_snapshot_artifact_id",
            )
            constraint_ids = execution.get("constraint_ids")
            if params.constraint_snapshot_artifact_id is None:
                _expect(constraint_ids, (), field="execution_binding.constraint_ids")
                expected_constraint_application = {"status": "not_applicable"}
            else:
                if not isinstance(constraint_ids, Sequence) or isinstance(
                    constraint_ids, (str, bytes, bytearray)
                ):
                    _fail("review simulation constraint input has no exact application evidence")
                _expect(
                    tuple(constraint_ids),
                    _constraint_ids_from_parent(
                        authoritative_parent_payloads,
                        params.constraint_snapshot_artifact_id,
                    ),
                    field="execution_binding.constraint_ids",
                )
                expected_constraint_application = {
                    "status": "unproven",
                    "reason_code": "constraint_profile_not_executable",
                }
            _expect(
                execution.get("constraint_application"),
                expected_constraint_application,
                field="execution_binding.constraint_application",
            )
    elif payload_schema_id == "review@1":
        _expect_snapshot(payload, projected)
    elif payload_schema_id == "constraint-proposal@1":
        assert isinstance(params, ConstraintProposalProposePayloadV1)
        _expect(
            payload.get("base_constraint_snapshot_id"),
            projected.constraint_snapshot_id,
            field="base_constraint_snapshot_id",
        )
        _expect(
            _required_field(payload, "dsl_grammar_version"),
            params.dsl_grammar_version,
            field="dsl_grammar_version",
        )
        _expect(_required_field(payload, "domain_scope"), params.domain_scope, field="domain_scope")
        source_bindings = _required_field(payload, "source_bindings")
        if not isinstance(source_bindings, Sequence) or isinstance(source_bindings, (str, bytes)):
            _fail("constraint proposal source_bindings are not an array")
        source_ids = tuple(
            item.get("source_artifact_id") if isinstance(item, Mapping) else None
            for item in source_bindings
        )
        _expect(tuple(sorted(source_ids)), params.source_artifact_ids, field="source_bindings")
        source_parents = {parent.artifact_id: parent for parent in _role_parents(typed, "source")}
        for item in source_bindings:
            if not isinstance(item, Mapping):
                _fail("constraint proposal source binding is not an object")
            source_id = item.get("source_artifact_id")
            parent = source_parents.get(source_id) if isinstance(source_id, str) else None
            if parent is None or parent.payload_hash is None:
                _fail(
                    "constraint proposal source parent has no payload-hash authority",
                    field="source_bindings.provenance_hash",
                )
            _expect(
                item.get("provenance_hash"),
                parent.payload_hash,
                field="source_bindings.provenance_hash",
            )
        _expect(_required_field(payload, "produced_by"), "agent", field="produced_by")
        _expect(_required_field(payload, "producer_run_id"), run.run_id, field="producer_run_id")
    elif payload_schema_id == "scenario-spec@1":
        assert isinstance(params, TaskSuiteDerivePayloadV1)
        for field, role in (
            ("source_preview_artifact_id", "preview"),
            ("config_export_artifact_id", "config"),
            ("constraint_snapshot_artifact_id", "constraint"),
        ):
            _expect(_required_field(payload, field), _one_role_id(typed, role), field=field)
        _expect(
            _required_field(payload, "environment_profile"),
            params.environment_profile,
            field="environment_profile",
        )
        _expect(
            _required_field(payload, "env_contract_version"),
            projected.env_contract_version,
            field="env_contract_version",
        )
        _expect(
            _required_field(payload, "domain_scope"),
            run.resource_domain_scope,
            field="domain_scope",
        )
    elif payload_schema_id == "task-suite@1":
        assert isinstance(params, TaskSuiteDerivePayloadV1)
        for field, role in (
            ("source_preview_artifact_id", "preview"),
            ("config_export_artifact_id", "config"),
            ("constraint_snapshot_artifact_id", "constraint"),
        ):
            _expect(_required_field(payload, field), _one_role_id(typed, role), field=field)
        _expect(
            _required_field(payload, "suite_profile"),
            params.derivation_profile,
            field="suite_profile",
        )
        _expect(
            _required_field(payload, "environment_profile"),
            params.environment_profile,
            field="environment_profile",
        )
        _expect(
            _required_field(payload, "completion_oracle_registry_ref"),
            params.completion_oracle_registry_ref,
            field="completion_oracle_registry_ref",
        )
        _expect(
            _required_field(payload, "env_contract_version"),
            projected.env_contract_version,
            field="env_contract_version",
        )
        episodes = _required_field(payload, "episodes")
        if not isinstance(episodes, Sequence) or isinstance(episodes, (str, bytes)):
            _fail("TaskSuite episodes are not an array")
        scenario_ids = tuple(
            item.get("scenario_spec_artifact_id") if isinstance(item, Mapping) else None
            for item in episodes
        )
        _expect(
            tuple(sorted(scenario_ids)),
            tuple(sorted(_role_ids(typed, "scenarios"))),
            field="episodes.scenario_spec_artifact_id",
        )
        scenario_payloads_by_hash: dict[str, Mapping[str, object]] = {}
        scenario_ids: list[object] = []
        for scenario_payload in related_payloads_by_rule.get("scenario", ()):
            payload_hash = canonical_sha256(scenario_payload)
            if payload_hash in scenario_payloads_by_hash:
                _fail("TaskSuite outcome contains duplicate scenario payloads")
            scenario_payloads_by_hash[payload_hash] = scenario_payload
            scenario_ids.append(_required_field(scenario_payload, "scenario_id"))
        if len(scenario_ids) != len(set(scenario_ids)):
            _fail("TaskSuite outcome contains duplicate scenario ids")
        scenario_parents = {
            parent.artifact_id: parent for parent in _role_parents(typed, "scenarios")
        }
        if len(scenario_payloads_by_hash) != len(scenario_parents):
            _fail("TaskSuite scenario payload set differs from its typed lineage")
        for index, episode in enumerate(episodes):
            if not isinstance(episode, Mapping):
                _fail("TaskSuite episode is not an object", field=f"episodes/{index}")
            scenario_id = episode.get("scenario_spec_artifact_id")
            parent = scenario_parents.get(scenario_id) if isinstance(scenario_id, str) else None
            if parent is None or parent.payload_hash is None:
                _fail(
                    "TaskSuite episode has no exact scenario payload authority",
                    field=f"episodes/{index}/scenario_spec_artifact_id",
                )
            scenario_payload = scenario_payloads_by_hash.get(parent.payload_hash)
            if scenario_payload is None:
                _fail(
                    "TaskSuite scenario payload hash differs from its typed parent",
                    field=f"episodes/{index}/scenario_spec_artifact_id",
                )
            _expect(
                episode.get("domain_scope"),
                _required_field(scenario_payload, "domain_scope"),
                field=f"episodes/{index}/domain_scope",
            )
            _expect(
                episode.get("reset_binding"),
                _required_field(scenario_payload, "reset_binding"),
                field=f"episodes/{index}/reset_binding",
            )
    elif payload_schema_id == "playtest-trace@1":
        assert isinstance(params, PlaytestRunPayloadV1)
        try:
            trace = PlaytestTraceV1.model_validate(payload)
            suite = TaskSuiteV1.model_validate(
                _authoritative_parent_payload(
                    authoritative_parent_payloads,
                    params.task_suite_artifact_id,
                )
            )
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "playtest trace or exact TaskSuite authority is invalid"
            ) from exc
        for field, role in (
            ("config_artifact_id", "config"),
            ("constraint_snapshot_artifact_id", "constraint"),
            ("task_suite_artifact_id", "task_suite"),
        ):
            _expect(_required_field(payload, field), _one_role_id(typed, role), field=field)
        for field in ("environment_profile", "planner_policy", "interaction_mode"):
            _expect(_required_field(payload, field), getattr(params, field), field=field)
        _expect(
            _required_field(payload, "env_contract_version"),
            projected.env_contract_version,
            field="env_contract_version",
        )
        _expect(_required_field(payload, "seed"), projected.seed, field="seed")
        _expect(
            trace.requested_max_steps_per_episode,
            params.max_steps_per_episode,
            field="requested_max_steps_per_episode",
        )
        planner_binding = next(
            (
                item
                for item in run.payload.resolved_profiles
                if item.field_path == "/params/planner_policy"
            ),
            None,
        )
        if planner_binding is None:
            _fail("playtest Run lacks its exact planner profile binding")
        _expect(
            trace.execution_envelope.planner_profile_payload_hash,
            planner_binding.profile_payload_hash,
            field="execution_envelope.planner_profile_payload_hash",
        )
        expected_selected = {
            item.episode_id: item.scenario_spec_artifact_id for item in params.episodes
        }
        suite_episodes = {item.episode_id: item for item in suite.episodes}
        actual_selected: dict[str, str] = {}
        for index, episode_trace in enumerate(trace.episodes):
            scenario_id = expected_selected.get(episode_trace.episode_id)
            suite_episode = suite_episodes.get(episode_trace.episode_id)
            if (
                scenario_id is None
                or suite_episode is None
                or scenario_id != episode_trace.scenario_spec_artifact_id
            ):
                _fail("playtest trace episode differs from the exact Run selection")
            actual_selected[episode_trace.episode_id] = episode_trace.scenario_spec_artifact_id
            _expect(
                episode_trace.step_budget,
                suite_episode.step_budget,
                field=f"episodes/{index}/step_budget",
            )
            _expect(
                episode_trace.execution_step_limit,
                params.max_steps_per_episode,
                field=f"episodes/{index}/execution_step_limit",
            )
            _expect(
                episode_trace.completion_oracle,
                suite_episode.completion_oracle,
                field=f"episodes/{index}/completion_oracle",
            )
            try:
                scenario = ScenarioSpecV1.model_validate(
                    _authoritative_parent_payload(
                        authoritative_parent_payloads,
                        episode_trace.scenario_spec_artifact_id,
                    )
                )
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation("selected ScenarioSpec authority is invalid") from exc
            for field, actual_value, expected_value in (
                ("domain_scope", scenario.domain_scope, suite_episode.domain_scope),
                ("reset_binding", scenario.reset_binding, suite_episode.reset_binding),
                (
                    "config_export_artifact_id",
                    scenario.config_export_artifact_id,
                    params.config_artifact_id,
                ),
                (
                    "constraint_snapshot_artifact_id",
                    scenario.constraint_snapshot_artifact_id,
                    params.constraint_snapshot_artifact_id,
                ),
                ("environment_profile", scenario.environment_profile, params.environment_profile),
                ("env_contract_version", scenario.env_contract_version, trace.env_contract_version),
            ):
                _expect(
                    actual_value,
                    expected_value,
                    field=f"episodes/{index}/scenario/{field}",
                )
        _expect(actual_selected, expected_selected, field="episodes")
    elif payload_schema_id == "constraint-snapshot@1" and isinstance(
        params, ConstraintValidationPayloadV1
    ):
        proposal_payload = _authoritative_parent_payload(
            authoritative_parent_payloads,
            params.subject.subject_artifact_id,
        )
        candidate_dsl = _required_field(payload, "dsl_grammar_version")
        proposal_dsl = _required_field(proposal_payload, "dsl_grammar_version")
        candidate_constraints = _required_field(payload, "constraints")
        proposal_constraints = _required_field(proposal_payload, "constraints")
        if (
            not isinstance(candidate_constraints, Sequence)
            or isinstance(candidate_constraints, (str, bytes, bytearray))
            or not isinstance(proposal_constraints, Sequence)
            or isinstance(proposal_constraints, (str, bytes, bytearray))
        ):
            _fail("constraint candidate exact proposal has no constraint array")
        try:
            normalized_candidate_constraints = tuple(
                Constraint.model_validate(constraint) for constraint in candidate_constraints
            )
            normalized_proposal_constraints = tuple(
                Constraint.model_validate(constraint).model_dump(mode="python")
                for constraint in proposal_constraints
            )
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "constraint candidate exact proposal contains invalid DSL"
            ) from exc
        normalized_candidate_payloads = tuple(
            constraint.model_dump(mode="python") for constraint in normalized_candidate_constraints
        )
        if (
            not _same(candidate_dsl, params.dsl_grammar_version)
            or not _same(candidate_dsl, proposal_dsl)
            or normalized_candidate_payloads != normalized_proposal_constraints
        ):
            _fail("constraint candidate differs from exact proposal")
    elif payload_schema_id == "regression-evidence@1":
        _validate_regression_payload(
            run=run,
            payload=payload,
            typed=typed,
            projected=projected,
            authoritative_parent_payloads=authoritative_parent_payloads,
        )
    elif payload_schema_id == "constraint-compile-evidence@1":
        assert isinstance(params, ConstraintValidationPayloadV1)
        _expect(
            _required_field(payload, "proposal_artifact_id"),
            params.subject.subject_artifact_id,
            field="proposal_artifact_id",
        )
        _expect(
            payload.get("base_constraint_snapshot_artifact_id"),
            params.base_constraint_snapshot_artifact_id,
            field="base_constraint_snapshot_artifact_id",
        )
        candidate_ids = _role_ids(typed, "candidate")
        _expect(
            payload.get("candidate_constraint_snapshot_artifact_id"),
            candidate_ids[0] if candidate_ids else None,
            field="candidate_constraint_snapshot_artifact_id",
        )
        _expect(
            _required_field(payload, "dsl_grammar_version"),
            params.dsl_grammar_version,
            field="dsl_grammar_version",
        )
        _expect(
            _required_field(payload, "compiler_profile"),
            params.compiler_profile,
            field="compiler_profile",
        )
        _validate_constraint_compile_execution(
            params=params,
            payload=payload,
        )
    elif payload_schema_id == "evidence-set@1":
        _validate_evidence_set(run=run, payload=payload, typed=typed, projected=projected)
    elif payload_schema_id == "auto-apply-proof@1":
        assert isinstance(params, PatchValidationPayloadV1)
        _expect(
            _required_field(payload, "subject_artifact_id"),
            params.subject.subject_artifact_id,
            field="subject_artifact_id",
        )
        _expect(
            _required_field(payload, "subject_digest"),
            params.subject.subject_digest,
            field="subject_digest",
        )
        target = _required_field(payload, "target_binding")
        if not isinstance(target, Mapping):
            _fail("auto-apply proof target_binding is not an object")
        _validate_target_binding(run=run, target=target, typed=typed, projected=projected)
        _expect(
            _required_field(payload, "validation_evidence_artifact_id"),
            _one_role_id(typed, "evidence_set"),
            field="validation_evidence_artifact_id",
        )
        _expect(
            _required_field(payload, "regression_evidence_artifact_ids"),
            _role_ids(typed, "regression"),
            field="regression_evidence_artifact_ids",
        )
        binding = _required_field(payload, "validation_profile_binding")
        if not isinstance(binding, Mapping):
            _fail("auto-apply validation_profile_binding is not an object")
        _expect(
            _required_field(binding, "validation_profile"),
            params.validation_policy,
            field="validation_profile_binding.validation_profile",
        )
    elif payload_schema_id == "bench-report@2":
        assert isinstance(params, BenchRunPayloadV1)
        report_meta = _required_field(payload, "meta")
        if not isinstance(report_meta, Mapping):
            _fail("BenchReport meta is not an object", field="meta")
        _expect(_required_field(report_meta, "seed"), projected.seed, field="meta.seed")
    elif payload_schema_id == "migration-report@1":
        assert isinstance(params, ArtifactMigrationPayloadV1)
        source = _role_parents(typed, "source")
        if len(source) != 1:
            _fail("migration report lacks one exact source parent")
        for field, expected in (
            ("source_artifact_id", params.source_artifact_id),
            ("source_kind", source[0].kind),
            ("source_payload_schema_id", source[0].payload_schema_id),
            ("target_payload_schema_id", params.target_payload_schema_id),
            ("target_meta_schema_version", params.target_meta_schema_version),
            ("target_dsl_grammar_version", params.target_dsl_grammar_version),
            ("migrator", params.migrator),
            ("requested_publish_mode", params.publish_mode),
        ):
            _expect(payload.get(field), expected, field=field)


def _expected_domain_scope(run: RunRecord, payload: Mapping[str, object]) -> object | None:
    params = run.payload.params
    if isinstance(params, GenerationProposePayloadV1):
        return params.domain_scope
    if isinstance(params, ConstraintProposalProposePayloadV1):
        return params.domain_scope
    if isinstance(params, PatchRepairPayloadV1):
        return run.resource_domain_scope
    return None


def _validate_authoritative_meta(
    *,
    run: RunRecord,
    rule: OutcomeArtifactRuleV1,
    payload_schema_id: str,
    payload: Mapping[str, object],
    prepared_meta: Mapping[str, object],
) -> dict[str, object]:
    meta = dict(prepared_meta)
    _expect(meta.get("payload_schema_id"), payload_schema_id, field="meta.payload_schema_id")
    if "provenance" in meta:
        _fail("terminal domain Artifact cannot carry worker-authored provenance")

    allowed = set(_BASE_META_KEYS)
    if payload_schema_id == "config-export-package@1":
        allowed.add("export_profile")
        _expect(
            meta.get("export_profile"),
            _required_field(payload, "export_profile"),
            field="meta.export_profile",
        )
    if rule.rule_id in {"checker", "simulation", "review", "regression"}:
        allowed.add("requirement_id")
        if "requirement_id" in meta:
            payload_requirement = payload.get("requirement_id")
            if payload_requirement is not None:
                _expect(
                    meta["requirement_id"],
                    payload_requirement,
                    field="meta.requirement_id",
                )
            elif meta["requirement_id"] not in _expected_requirement_ids(run):
                _fail(
                    "Artifact requirement metadata is not frozen in the Run",
                    field="meta.requirement_id",
                )
    if payload_schema_id == "constraint-proposal@1":
        allowed.add("dropped_proposal_count")
    if run.kind.kind == "review.run" and rule.rule_id == "primary":
        allowed.update({"llm_execution_mode", "llm_triage_applied"})
        params = run.payload.params
        assert isinstance(params, ReviewRunPayloadV1)
        triage_applied = params.llm_triage_policy is not None
        expected_mode = run.payload.llm_execution_mode if triage_applied else "not_applicable"
        _expect(meta.get("llm_triage_applied"), triage_applied, field="meta.llm_triage_applied")
        _expect(meta.get("llm_execution_mode"), expected_mode, field="meta.llm_execution_mode")
    if run.kind.kind == "bench.run":
        allowed.add("execution_scope")
        params = run.payload.params
        assert isinstance(params, BenchRunPayloadV1)
        _expect(
            meta.get("execution_scope"),
            params.execution_scope,
            field="meta.execution_scope",
        )
    expected_scope = _expected_domain_scope(run, payload)
    if expected_scope is not None:
        allowed.add("domain_scope")
        if "domain_scope" in meta:
            _expect(meta["domain_scope"], expected_scope, field="meta.domain_scope")
        meta["domain_scope"] = _json_value(expected_scope)
    elif "domain_scope" in meta:
        _fail(
            "worker domain_scope metadata has no Run authority",
            field="meta.domain_scope",
        )

    unknown = set(meta) - allowed
    if unknown:
        raise IntegrityViolation(
            "worker metadata contains an unknown key",
            metadata_keys=tuple(sorted(unknown)),
        )
    return meta


def _requirement_dispositions_for(
    *,
    run: RunRecord,
    outcome_rule: OutcomeArtifactRuleV1,
    payload_schema_id: str,
    payload: Mapping[str, object],
) -> tuple[FinalRequirementDispositionFact, ...]:
    """Project the EvidenceRequirement facts encoded by one final evidence payload.

    The outcome rule calls all validation-dimension siblings ``regression`` even
    when their deterministic dimension is checker/simulation/history/etc.  The
    requirement kind/tool therefore come from the exact Run kind plus the strict
    payload variant, not from the broad outcome-rule name.
    """

    params = run.payload.params
    if payload_schema_id == "constraint-compile-evidence@1":
        if not isinstance(params, ConstraintValidationPayloadV1):
            return ()
        raw_status = payload.get("overall_status")
        if raw_status not in {"passed", "failed", "unproven"}:
            _fail("compile sibling has no exact overall status", field="overall_status")
        tool = _profile_key(params.compiler_profile)
        return (
            FinalRequirementDispositionFact(
                applicability="required",
                status=raw_status,
                reason_code=("compile_evidence_not_passed" if raw_status == "unproven" else None),
                tool_version=tool,
            ),
        )

    if payload_schema_id != "regression-evidence@1" or outcome_rule.rule_id != "regression":
        return ()
    raw_status = payload.get("status")
    if raw_status not in {"passed", "failed", "unproven", "not_executed"}:
        _fail("regression sibling has no exact status", field="status")
    status = "unproven" if raw_status in {"unproven", "not_executed"} else raw_status
    reason_code: str | None = None
    if status == "unproven":
        if "suite_artifact_id" in payload:
            reason = payload.get("reason_code")
            if not isinstance(reason, str) or not reason:
                _fail(
                    "unproven regression suite evidence has no exact reason",
                    field="reason_code",
                )
            reason_code = reason
        elif isinstance(params, PatchValidationPayloadV1) and "dimension" in payload:
            reason = payload.get("reason_code")
            if not isinstance(reason, str) or not reason:
                _fail(
                    "unproven patch validation evidence has no exact reason",
                    field="reason_code",
                )
            reason_code = reason
        elif isinstance(params, RollbackValidationPayloadV1) and "dimension" in payload:
            reason = payload.get("reason_code")
            if not isinstance(reason, str) or not reason:
                _fail(
                    "unproven rollback evidence has no exact reason",
                    field="reason_code",
                )
            reason_code = reason
        else:
            _fail(
                "unproven regression evidence has no registered exact reason source",
                field="reason_code",
            )

    if isinstance(params, PatchValidationPayloadV1):
        if "suite_artifact_id" in payload:
            tool = "regression@1"
        else:
            tool = {
                "checker": "checker@1",
                "simulation": "economy-sim@1",
                "expected_finding_reverification": "finding@1",
                "finding": "finding@1",
                "review": "review@1",
                "playtest": "playtest@1",
                "validation_input": "patch-validation@1",
            }.get(str(payload.get("dimension")))
            if tool is None:
                _fail(
                    "patch validation evidence has an unknown deterministic dimension",
                    field="dimension",
                )
    elif isinstance(params, ConstraintValidationPayloadV1):
        tool = "regression@1"
    elif isinstance(params, RollbackValidationPayloadV1):
        tool = "rollback-validation@1"
    else:
        return ()
    return (
        FinalRequirementDispositionFact(
            applicability="required",
            status=status,
            reason_code=reason_code,
            tool_version=tool,
        ),
    )


def final_sibling_fact_for(
    *,
    run: RunRecord,
    artifact_id: str,
    outcome_rule: OutcomeArtifactRuleV1,
    payload_schema_id: str,
    canonical_payload: Mapping[str, object],
    payload_hash: str,
    authoritative_meta: Mapping[str, object],
) -> FinalSiblingFact:
    """Derive the exact semantic identity exposed to downstream siblings."""

    payload_requirement = canonical_payload.get("requirement_id")
    meta_requirement = authoritative_meta.get("requirement_id")
    if payload_requirement is not None and not isinstance(payload_requirement, str):
        _fail("sibling payload requirement_id is not a string", field="requirement_id")
    if meta_requirement is not None and not isinstance(meta_requirement, str):
        _fail("sibling metadata requirement_id is not a string", field="meta.requirement_id")
    if (
        payload_requirement is not None
        and meta_requirement is not None
        and payload_requirement != meta_requirement
    ):
        _fail("sibling payload and metadata requirement identities differ")

    requirement_id = payload_requirement or meta_requirement
    requirement_kind: str | None = None
    if outcome_rule.rule_id == "compile-evidence":
        if requirement_id not in {None, "compile"}:
            _fail("compile-evidence sibling claims a non-compile requirement")
        requirement_id = "compile"
        requirement_kind = CONSTRAINT_COMPILE_REQUIREMENT_KIND
    elif outcome_rule.rule_id == "regression":
        if isinstance(run.payload.params, RollbackValidationPayloadV1):
            dimension = canonical_payload.get("dimension")
            if not isinstance(dimension, str) or not dimension:
                _fail("rollback evidence sibling has no requirement kind", field="dimension")
            requirement_kind = dimension
        else:
            requirement_kind = "regression"

    return FinalSiblingFact(
        artifact_id=artifact_id,
        outcome_rule_id=outcome_rule.rule_id,
        artifact_kind=outcome_rule.artifact_kind,
        payload_schema_id=payload_schema_id,
        payload_hash=payload_hash,
        requirement_id=requirement_id,
        requirement_kind=requirement_kind,
        requirement_dispositions=_requirement_dispositions_for(
            run=run,
            outcome_rule=outcome_rule,
            payload_schema_id=payload_schema_id,
            payload=canonical_payload,
        ),
    )


def _final_sibling_fact(
    artifact_id: object,
    *,
    final_sibling_facts_by_id: Mapping[str, FinalSiblingFact],
    source_rule_ids: Sequence[str],
    field: str,
) -> FinalSiblingFact:
    if not isinstance(artifact_id, str):
        _fail("same-publication Artifact reference is not a string", field=field)
    fact = final_sibling_facts_by_id.get(artifact_id)
    if fact is None or fact.artifact_id != artifact_id:
        _fail("same-publication Artifact reference has no authoritative final fact", field=field)
    if fact.outcome_rule_id not in source_rule_ids:
        _fail("same-publication Artifact fact belongs to the wrong outcome rule", field=field)
    return fact


def _rule_aliases(
    *,
    source_rule_ids: Sequence[str],
    final_artifact_ids_by_rule: Mapping[str, Sequence[str]],
    prepared_to_final_artifact_ids_by_rule: Mapping[str, Mapping[str, str]],
) -> tuple[dict[str, str], frozenset[str]]:
    aliases: dict[str, str] = {}
    final_ids: set[str] = set()
    for rule_id in source_rule_ids:
        rule_finals = tuple(final_artifact_ids_by_rule.get(rule_id, ()))
        if len(rule_finals) != len(set(rule_finals)):
            raise IntegrityViolation(
                "same-publication rule contains duplicate final Artifact ids",
                outcome_rule_id=rule_id,
            )
        final_ids.update(rule_finals)
        for prepared_id, final_id in prepared_to_final_artifact_ids_by_rule.get(
            rule_id, {}
        ).items():
            if final_id not in rule_finals:
                raise IntegrityViolation(
                    "prepared Artifact alias resolves outside its exact outcome rule",
                    outcome_rule_id=rule_id,
                    prepared_artifact_id=prepared_id,
                )
            retained = aliases.get(prepared_id)
            if retained is not None and retained != final_id:
                raise IntegrityViolation(
                    "prepared Artifact alias resolves to more than one final Artifact",
                    prepared_artifact_id=prepared_id,
                )
            aliases[prepared_id] = final_id
    return aliases, frozenset(final_ids)


def _bind_sibling_id(
    value: object,
    *,
    source_rule_ids: Sequence[str],
    final_artifact_ids_by_rule: Mapping[str, Sequence[str]],
    prepared_to_final_artifact_ids_by_rule: Mapping[str, Mapping[str, str]],
    field: str,
    required: bool,
) -> object:
    if not isinstance(value, str) or not value:
        if required:
            _fail("same-publication Artifact reference is missing or invalid", field=field)
        return value
    aliases, final_ids = _rule_aliases(
        source_rule_ids=source_rule_ids,
        final_artifact_ids_by_rule=final_artifact_ids_by_rule,
        prepared_to_final_artifact_ids_by_rule=prepared_to_final_artifact_ids_by_rule,
    )
    if value in final_ids:
        return value
    final_id = aliases.get(value)
    if final_id is not None:
        return final_id
    if required:
        _fail(
            "same-publication Artifact reference does not identify an exact allocated sibling",
            field=field,
        )
    return value


def _subset_disposition_rows(
    *,
    run: RunRecord,
    outcome_policy: OutcomeArtifactPolicyV1,
    dispositions: Sequence[RequirementDispositionV1],
) -> dict[str, tuple[str, RequirementDispositionV1]]:
    """Resolve the exact prepared disposition closure for subset-bound rules."""

    subset_rules = tuple(
        (rule.rule_id, rule.count_binding)
        for rule in outcome_policy.artifact_rules
        if isinstance(rule.count_binding, ResolvedPolicySubsetCountBindingV1)
    )
    expected_keys: set[tuple[str, str, str]] = set()
    for rule_id, binding in subset_rules:
        if rule_id != binding.outcome_rule_id:
            raise IntegrityViolation(
                "subset-bound outcome rule differs from its disposition selector",
                rule_id=rule_id,
                disposition_outcome_rule_id=binding.outcome_rule_id,
            )
        snapshots = tuple(
            snapshot
            for snapshot in run.payload.resolved_policy_snapshots
            if snapshot.resolved_policy_id == binding.resolved_policy_id
        )
        if len(snapshots) != 1:
            raise IntegrityViolation(
                "subset disposition closure requires one exact resolved-policy snapshot",
                resolved_policy_id=binding.resolved_policy_id,
                actual=len(snapshots),
            )
        requirement_ids = tuple(
            requirement.requirement_id
            for requirement in snapshots[0].requirements
            if requirement.outcome_rule_id == binding.outcome_rule_id
        )
        if len(requirement_ids) != len(set(requirement_ids)):
            raise IntegrityViolation(
                "subset disposition requirements reuse an identity",
                resolved_policy_id=binding.resolved_policy_id,
                outcome_rule_id=binding.outcome_rule_id,
            )
        expected_keys.update(
            (binding.resolved_policy_id, binding.outcome_rule_id, requirement_id)
            for requirement_id in requirement_ids
        )

    actual_keys = tuple(
        (row.resolved_policy_id, row.outcome_rule_id, row.requirement_id) for row in dispositions
    )
    if len(actual_keys) != len(set(actual_keys)) or set(actual_keys) != expected_keys:
        raise IntegrityViolation(
            "prepared dispositions do not exactly cover EvidenceSet subset requirements",
            expected=tuple(sorted(expected_keys)),
            actual=tuple(sorted(actual_keys)),
        )

    rows: dict[str, tuple[str, RequirementDispositionV1]] = {}
    for row in dispositions:
        retained = rows.get(row.requirement_id)
        if retained is not None:
            raise IntegrityViolation(
                "EvidenceSet subset rules reuse a requirement identity",
                requirement_id=row.requirement_id,
            )
        rows[row.requirement_id] = (row.outcome_rule_id, row)
    return rows


def _not_executed_requirement_tool(*, run: RunRecord, outcome_rule_id: str) -> str:
    params = run.payload.params
    if outcome_rule_id == "regression" and isinstance(
        params,
        (
            PatchValidationPayloadV1,
            ConstraintValidationPayloadV1,
            RollbackValidationPayloadV1,
        ),
    ):
        return "regression@1"
    raise IntegrityViolation(
        "subset not-executed requirement has no registered exact tool",
        outcome_rule_id=outcome_rule_id,
    )


def _validate_subset_requirement_closure(
    *,
    run: RunRecord,
    outcome_policy: OutcomeArtifactPolicyV1,
    requirements: Sequence[object],
    dispositions: Sequence[RequirementDispositionV1],
    final_sibling_facts_by_id: Mapping[str, FinalSiblingFact],
) -> None:
    rows = _subset_disposition_rows(
        run=run,
        outcome_policy=outcome_policy,
        dispositions=dispositions,
    )
    requirements_by_id: dict[str, Mapping[str, object]] = {}
    for requirement in requirements:
        if not isinstance(requirement, Mapping):
            continue
        requirement_id = requirement.get("requirement_id")
        if isinstance(requirement_id, str):
            requirements_by_id[requirement_id] = requirement

    for requirement_id, (outcome_rule_id, disposition) in rows.items():
        requirement = requirements_by_id.get(requirement_id)
        if requirement is None:
            raise IntegrityViolation(
                "EvidenceSet omits a prepared subset disposition",
                requirement_id=requirement_id,
            )
        evidence_id = requirement.get("evidence_artifact_id")
        if disposition.status == "produced":
            if not isinstance(evidence_id, str) or not evidence_id:
                raise IntegrityViolation(
                    "produced subset disposition has no exact EvidenceSet sibling",
                    requirement_id=requirement_id,
                )
            fact = final_sibling_facts_by_id.get(evidence_id)
            if (
                fact is None
                or fact.requirement_id != requirement_id
                or fact.outcome_rule_id != outcome_rule_id
            ):
                raise IntegrityViolation(
                    "produced subset disposition differs from its final sibling",
                    requirement_id=requirement_id,
                    evidence_artifact_id=evidence_id,
                )
            continue

        expected_tool = _not_executed_requirement_tool(
            run=run,
            outcome_rule_id=outcome_rule_id,
        )
        expected = {
            "kind": outcome_rule_id,
            "applicability": "required",
            "status": "unproven",
            "evidence_artifact_id": None,
            "reason_code": disposition.reason_code,
            "tool_version": expected_tool,
        }
        for field, value in expected.items():
            _expect(
                requirement.get(field),
                value,
                field=f"requirements/{requirement_id}/{field}",
            )


def _bind_evidence_set_references(
    *,
    run: RunRecord,
    outcome_policy: OutcomeArtifactPolicyV1,
    payload: dict[str, object],
    dispositions: Sequence[RequirementDispositionV1],
    final_artifact_ids_by_rule: Mapping[str, Sequence[str]],
    prepared_to_final_artifact_ids_by_rule: Mapping[str, Mapping[str, str]],
    final_sibling_facts_by_id: Mapping[str, FinalSiblingFact],
) -> None:
    params = run.payload.params
    evidence_rules: tuple[str, ...]
    if isinstance(params, ConstraintValidationPayloadV1):
        evidence_rules = ("compile-evidence", "regression")
        target = payload.get("target_binding")
        if target is not None:
            if not isinstance(target, Mapping):
                _fail("EvidenceSet target_binding is not an object", field="target_binding")
            rebound = dict(target)
            rebound["target_artifact_id"] = _bind_sibling_id(
                rebound.get("target_artifact_id"),
                source_rule_ids=("candidate",),
                final_artifact_ids_by_rule=final_artifact_ids_by_rule,
                prepared_to_final_artifact_ids_by_rule=(prepared_to_final_artifact_ids_by_rule),
                field="target_binding.target_artifact_id",
                required=True,
            )
            candidate_fact = _final_sibling_fact(
                rebound["target_artifact_id"],
                final_sibling_facts_by_id=final_sibling_facts_by_id,
                source_rule_ids=("candidate",),
                field="target_binding.target_artifact_id",
            )
            _expect(
                rebound.get("target_digest"),
                candidate_fact.payload_hash,
                field="target_binding.target_digest",
            )
            payload["target_binding"] = rebound
    else:
        evidence_rules = ("regression",)

    requirements = payload.get("requirements")
    if not isinstance(requirements, Sequence) or isinstance(requirements, (str, bytes)):
        _fail("EvidenceSet requirements are not an array", field="requirements")
    rebound_requirements: list[object] = []
    claimed_final_evidence_ids: list[str] = []
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, Mapping):
            _fail("EvidenceSet requirement is not an object", field=f"requirements/{index}")
        rebound = dict(requirement)
        evidence_id = rebound.get("evidence_artifact_id")
        if evidence_id is not None:
            rebound["evidence_artifact_id"] = _bind_sibling_id(
                evidence_id,
                source_rule_ids=evidence_rules,
                final_artifact_ids_by_rule=final_artifact_ids_by_rule,
                prepared_to_final_artifact_ids_by_rule=(prepared_to_final_artifact_ids_by_rule),
                field=f"requirements/{index}/evidence_artifact_id",
                required=True,
            )
            evidence_fact = _final_sibling_fact(
                rebound["evidence_artifact_id"],
                final_sibling_facts_by_id=final_sibling_facts_by_id,
                source_rule_ids=evidence_rules,
                field=f"requirements/{index}/evidence_artifact_id",
            )
            _expect(
                rebound.get("requirement_id"),
                evidence_fact.requirement_id,
                field=f"requirements/{index}/requirement_id",
            )
            _expect(
                rebound.get("kind"),
                evidence_fact.requirement_kind,
                field=f"requirements/{index}/kind",
            )
            disposition = FinalRequirementDispositionFact(
                applicability=str(rebound.get("applicability")),
                status=str(rebound.get("status")),
                reason_code=(
                    rebound.get("reason_code")
                    if isinstance(rebound.get("reason_code"), str)
                    else None
                ),
                tool_version=str(rebound.get("tool_version")),
            )
            if disposition not in evidence_fact.requirement_dispositions:
                raise IntegrityViolation(
                    "EvidenceSet requirement disposition differs from final evidence",
                    requirement_id=rebound.get("requirement_id"),
                    evidence_artifact_id=evidence_fact.artifact_id,
                )
            claimed_final_evidence_ids.append(evidence_fact.artifact_id)
        rebound_requirements.append(rebound)
    payload["requirements"] = rebound_requirements

    _validate_subset_requirement_closure(
        run=run,
        outcome_policy=outcome_policy,
        requirements=rebound_requirements,
        dispositions=dispositions,
        final_sibling_facts_by_id=final_sibling_facts_by_id,
    )

    expected_final_evidence_ids = tuple(
        artifact_id
        for rule_id in evidence_rules
        for artifact_id in final_artifact_ids_by_rule.get(rule_id, ())
    )
    if (
        len(claimed_final_evidence_ids) != len(set(claimed_final_evidence_ids))
        or set(claimed_final_evidence_ids) != set(expected_final_evidence_ids)
        or len(expected_final_evidence_ids) != len(set(expected_final_evidence_ids))
    ):
        raise IntegrityViolation(
            "EvidenceSet requirements do not exactly cover final evidence siblings",
            expected=tuple(sorted(expected_final_evidence_ids)),
            actual=tuple(sorted(claimed_final_evidence_ids)),
        )

    required_statuses = tuple(
        item.get("status")
        for item in rebound_requirements
        if isinstance(item, Mapping) and item.get("applicability") == "required"
    )
    derived_overall = (
        "failed"
        if "failed" in required_statuses
        else "unproven"
        if "unproven" in required_statuses
        else "passed"
    )
    _expect(payload.get("overall_status"), derived_overall, field="overall_status")

    supporting = payload.get("supporting_artifact_ids")
    if not isinstance(supporting, Sequence) or isinstance(supporting, (str, bytes)):
        _fail(
            "EvidenceSet supporting_artifact_ids are not an array",
            field="supporting_artifact_ids",
        )
    sibling_rules = (*evidence_rules, "candidate")
    rebound_supporting = [
        _bind_sibling_id(
            value,
            source_rule_ids=sibling_rules,
            final_artifact_ids_by_rule=final_artifact_ids_by_rule,
            prepared_to_final_artifact_ids_by_rule=prepared_to_final_artifact_ids_by_rule,
            field=f"supporting_artifact_ids/{index}",
            required=False,
        )
        for index, value in enumerate(supporting)
    ]
    if len(rebound_supporting) != len(set(rebound_supporting)):
        _fail(
            "EvidenceSet sibling reseal creates duplicate supporting Artifact ids",
            field="supporting_artifact_ids",
        )
    payload["supporting_artifact_ids"] = sorted(rebound_supporting)
    _validate_exact_evidence_support(
        run=run,
        payload=payload,
        final_artifact_ids_by_rule=final_artifact_ids_by_rule,
    )


def _bind_auto_apply_references(
    *,
    payload: dict[str, object],
    final_artifact_ids_by_rule: Mapping[str, Sequence[str]],
    prepared_to_final_artifact_ids_by_rule: Mapping[str, Mapping[str, str]],
    final_sibling_facts_by_id: Mapping[str, FinalSiblingFact],
) -> None:
    payload["validation_evidence_artifact_id"] = _bind_sibling_id(
        payload.get("validation_evidence_artifact_id"),
        source_rule_ids=("primary",),
        final_artifact_ids_by_rule=final_artifact_ids_by_rule,
        prepared_to_final_artifact_ids_by_rule=prepared_to_final_artifact_ids_by_rule,
        field="validation_evidence_artifact_id",
        required=True,
    )
    regression_ids = payload.get("regression_evidence_artifact_ids")
    if not isinstance(regression_ids, Sequence) or isinstance(regression_ids, (str, bytes)):
        _fail(
            "auto-apply regression_evidence_artifact_ids are not an array",
            field="regression_evidence_artifact_ids",
        )
    rebound_regression_ids = [
        _bind_sibling_id(
            value,
            source_rule_ids=("regression",),
            final_artifact_ids_by_rule=final_artifact_ids_by_rule,
            prepared_to_final_artifact_ids_by_rule=prepared_to_final_artifact_ids_by_rule,
            field=f"regression_evidence_artifact_ids/{index}",
            required=True,
        )
        for index, value in enumerate(regression_ids)
    ]
    if len(rebound_regression_ids) != len(set(rebound_regression_ids)):
        _fail(
            "auto-apply sibling reseal creates duplicate regression Artifact ids",
            field="regression_evidence_artifact_ids",
        )
    payload["regression_evidence_artifact_ids"] = sorted(rebound_regression_ids)
    for collection_name in (
        "deterministic_oracle_evidence",
        "required_outcome_evidence",
    ):
        collection = payload.get(collection_name)
        if not isinstance(collection, Sequence) or isinstance(collection, (str, bytes)):
            _fail(f"auto-apply {collection_name} is not an array", field=collection_name)
        rebound_collection: list[object] = []
        for index, binding in enumerate(collection):
            if not isinstance(binding, Mapping):
                _fail(
                    "auto-apply evidence binding is not an object",
                    field=f"{collection_name}/{index}",
                )
            rebound = dict(binding)
            rebound["evidence_artifact_id"] = _bind_sibling_id(
                rebound.get("evidence_artifact_id"),
                source_rule_ids=("regression",),
                final_artifact_ids_by_rule=final_artifact_ids_by_rule,
                prepared_to_final_artifact_ids_by_rule=(prepared_to_final_artifact_ids_by_rule),
                field=f"{collection_name}/{index}/evidence_artifact_id",
                required=True,
            )
            evidence_fact = _final_sibling_fact(
                rebound["evidence_artifact_id"],
                final_sibling_facts_by_id=final_sibling_facts_by_id,
                source_rule_ids=("regression",),
                field=f"{collection_name}/{index}/evidence_artifact_id",
            )
            _expect(
                rebound.get("evidence_payload_hash"),
                evidence_fact.payload_hash,
                field=f"{collection_name}/{index}/evidence_payload_hash",
            )
            if collection_name == "required_outcome_evidence":
                _expect(
                    rebound.get("requirement_id"),
                    evidence_fact.requirement_id,
                    field=f"{collection_name}/{index}/requirement_id",
                )
            rebound_collection.append(rebound)
        payload[collection_name] = rebound_collection


def bind_final_payload_references(
    *,
    run: RunRecord,
    outcome_policy: OutcomeArtifactPolicyV1,
    outcome_rule: OutcomeArtifactRuleV1,
    payload_schema_id: str,
    canonical_payload: Mapping[str, object],
    projected_tuple: VersionTuple,
    final_artifact_ids_by_rule: Mapping[str, Sequence[str]],
    final_sibling_facts_by_id: Mapping[str, FinalSiblingFact],
    prepared_to_final_artifact_ids_by_rule: Mapping[str, Mapping[str, str]] | None = None,
    requirement_dispositions: Sequence[RequirementDispositionV1] = (),
) -> dict[str, object]:
    """Bind references whose final content-addressed sibling id was unknowable.

    Generation/repair handlers know the preview's content-derived snapshot id but
    cannot know its final Artifact id because terminal producer metadata is added
    only after execution closes.  Config packages therefore carry that exact
    logical snapshot id in their prepared envelope; the publisher verifies it and
    replaces it with the one final ``preview`` Artifact id before re-encoding the
    package.  No arbitrary worker value is silently repaired.
    """

    selector = _selector(run, outcome_policy, outcome_rule, payload_schema_id)
    if selector not in _SUPPORTED_SELECTORS:
        raise IntegrityViolation(
            "final payload-reference binding is not registered for the exact selector",
            selector=selector,
        )
    retained_rule = next(
        (rule for rule in outcome_policy.artifact_rules if rule.rule_id == outcome_rule.rule_id),
        None,
    )
    if retained_rule != outcome_rule:
        _fail("outcome rule is not exact in the selected final-binding policy")
    payload = dict(canonical_payload)
    aliases = prepared_to_final_artifact_ids_by_rule or {}
    if payload_schema_id == "config-export-package@1":
        preview_ids = tuple(final_artifact_ids_by_rule.get("preview", ()))
        if len(preview_ids) != 1 or not preview_ids[0]:
            raise IntegrityViolation(
                "config export final binding requires exactly one preview Artifact id",
                actual=len(preview_ids),
            )
        logical_snapshot_id = projected_tuple.ir_snapshot_id
        if logical_snapshot_id is None:
            _fail("config export has no projected preview snapshot identity")
        current = payload.get("source_preview_artifact_id")
        if current not in {logical_snapshot_id, preview_ids[0]}:
            _fail(
                "prepared config export preview reference differs from its logical snapshot",
                field="source_preview_artifact_id",
            )
        payload["source_preview_artifact_id"] = preview_ids[0]
    elif payload_schema_id == "constraint-compile-evidence@1":
        candidate_id = payload.get("candidate_constraint_snapshot_artifact_id")
        if candidate_id is not None:
            payload["candidate_constraint_snapshot_artifact_id"] = _bind_sibling_id(
                candidate_id,
                source_rule_ids=("candidate",),
                final_artifact_ids_by_rule=final_artifact_ids_by_rule,
                prepared_to_final_artifact_ids_by_rule=aliases,
                field="candidate_constraint_snapshot_artifact_id",
                required=True,
            )
    elif payload_schema_id == "evidence-set@1":
        _bind_evidence_set_references(
            run=run,
            outcome_policy=outcome_policy,
            payload=payload,
            dispositions=requirement_dispositions,
            final_artifact_ids_by_rule=final_artifact_ids_by_rule,
            prepared_to_final_artifact_ids_by_rule=aliases,
            final_sibling_facts_by_id=final_sibling_facts_by_id,
        )
    elif payload_schema_id == "auto-apply-proof@1":
        _bind_auto_apply_references(
            payload=payload,
            final_artifact_ids_by_rule=final_artifact_ids_by_rule,
            prepared_to_final_artifact_ids_by_rule=aliases,
            final_sibling_facts_by_id=final_sibling_facts_by_id,
        )
    elif payload_schema_id == "task-suite@1":
        episodes = payload.get("episodes")
        if not isinstance(episodes, Sequence) or isinstance(episodes, (str, bytes)):
            _fail("TaskSuite episodes are not an array", field="episodes")
        rebound_episodes: list[object] = []
        for index, episode in enumerate(episodes):
            if not isinstance(episode, Mapping):
                _fail("TaskSuite episode is not an object", field=f"episodes/{index}")
            rebound = dict(episode)
            rebound["scenario_spec_artifact_id"] = _bind_sibling_id(
                rebound.get("scenario_spec_artifact_id"),
                source_rule_ids=("scenario",),
                final_artifact_ids_by_rule=final_artifact_ids_by_rule,
                prepared_to_final_artifact_ids_by_rule=aliases,
                field=f"episodes/{index}/scenario_spec_artifact_id",
                required=True,
            )
            rebound_episodes.append(rebound)
        payload["episodes"] = rebound_episodes
    return payload


def validate_domain_payload_bindings(
    *,
    run: RunRecord,
    outcome_policy: OutcomeArtifactPolicyV1,
    outcome_rule: OutcomeArtifactRuleV1,
    payload_schema_id: str,
    canonical_payload: Mapping[str, object],
    typed_lineage: TypedLineage,
    projected_tuple: VersionTuple,
    prepared_meta: Mapping[str, object],
    related_payloads_by_rule: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
    authoritative_parent_payloads: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Validate all duplicated semantic authorities and return checked metadata.

    ``related_payloads_by_rule`` is the already schema-validated prepared payload
    set for this outcome.  It is mandatory in practice for Patch publication so
    ``target_snapshot_id`` is bound to the exact preview content even though the
    preview Artifact is minted later in the lineage topological order.

    ``authoritative_parent_payloads`` contains schema-decoded bytes read from the
    already-published Artifacts selected by typed lineage. Standalone checker and
    simulation outputs use it to prove worker-reported constraint/scenario ids
    against immutable parent content rather than trusting duplicated payload data.
    """

    selector = _selector(run, outcome_policy, outcome_rule, payload_schema_id)
    if selector not in _SUPPORTED_SELECTORS:
        raise IntegrityViolation(
            "domain payload semantic bindings are not registered for the exact selector",
            selector=selector,
        )
    if payload_schema_id not in outcome_rule.payload_schema_ids:
        _fail("payload schema differs from the selected exact outcome rule")
    retained_rule = next(
        (rule for rule in outcome_policy.artifact_rules if rule.rule_id == outcome_rule.rule_id),
        None,
    )
    if retained_rule != outcome_rule:
        _fail("outcome rule is not exact in the selected payload-binding policy")
    _validate_typed_run_parents(
        run=run,
        policy=outcome_policy,
        rule=outcome_rule,
        typed=typed_lineage,
    )
    _validate_payload_semantics(
        run=run,
        rule=outcome_rule,
        payload_schema_id=payload_schema_id,
        payload=canonical_payload,
        typed=typed_lineage,
        projected=projected_tuple,
        related_payloads_by_rule=related_payloads_by_rule or {},
        authoritative_parent_payloads=authoritative_parent_payloads,
    )
    return _validate_authoritative_meta(
        run=run,
        rule=outcome_rule,
        payload_schema_id=payload_schema_id,
        payload=canonical_payload,
        prepared_meta=prepared_meta,
    )


def validate_domain_payload_binding_registry(registry: DomainPayloadBindingRegistry) -> int:
    """Readiness-close every active schema-valid domain outcome selector."""

    expected: set[SemanticSelector] = set()
    for definition in registry.list_run_kinds():
        if getattr(definition, "status") != "active":
            continue
        for policy in getattr(definition, "outcome_policies"):
            for rule in policy.artifact_rules:
                for schema in rule.payload_schema_ids:
                    validator = ARTIFACT_PAYLOAD_VALIDATORS.get(schema)
                    if validator is not None and (
                        validator.is_available or schema in _EXTERNAL_PAYLOAD_SCHEMAS
                    ):
                        expected.add(
                            (
                                definition.kind,
                                definition.version,
                                policy.policy_id,
                                policy.policy_version,
                                rule.rule_id,
                                rule.artifact_kind,
                                schema,
                            )
                        )
    if expected != set(_SUPPORTED_SELECTORS):
        raise IntegrityViolation(
            "domain payload semantic binding registry is not an exact active closure",
            missing=tuple(sorted(expected - set(_SUPPORTED_SELECTORS))),
            extra=tuple(sorted(set(_SUPPORTED_SELECTORS) - expected)),
        )
    return len(expected)


__all__ = [
    "FinalSiblingFact",
    "bind_final_payload_references",
    "final_sibling_fact_for",
    "validate_domain_payload_binding_registry",
    "validate_domain_payload_bindings",
]
