from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from gameforge.contracts.execution_profiles import (
    ProfileRefV1,
    RunKindRef,
    VersionTransitionPolicyRefV1,
)
from gameforge.contracts.findings import FindingPayloadV1
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    AgentPromptArtifactBindingV1,
    AgentPromptContextDraftV1,
    AgentPromptContextV1,
    AgentPromptPriorConsumptionV1,
    AgentPromptSourceMessageV1,
    CheckerRunPayloadV1,
    DependencyFailureV1,
    ExecutionVersionPlanV1,
    FailureClassifierRefV1,
    GenerationProposePayloadV1,
    GraphSelectionV1,
    MAX_PREPARED_FINDINGS,
    MAX_PLAYTEST_DIRECT_INPUT_ARTIFACTS,
    MAX_PLAYTEST_PROMPT_SOURCE_ARTIFACTS,
    MAX_PLAYTEST_PROMPT_UPSTREAM_ARTIFACTS,
    MAX_PLAYTEST_RUN_INPUT_ARTIFACTS,
    MAX_PLAYTEST_TRACE_LINEAGE_PARENTS,
    MAX_PREPARED_DOMAIN_ARTIFACTS,
    MAX_RUN_MANIFEST_PARENT_BINDINGS,
    PatchRepairPayloadV1,
    PlaytestEpisodeBindingV1,
    PlaytestRunPayloadV1,
    PlannedAgentNodeVersionV1,
    PreparedArtifact,
    PreparedFindingV1,
    PreparedRunFailure,
    PreparedRunResult,
    PreparedRunResultSummaryV1,
    Problem,
    PromptGoalBindingV1,
    RefReadBindingV1,
    ResolvedArtifactRequirementV1,
    ResolvedPolicySnapshotV1,
    RequirementDispositionV1,
    RetryDecisionV1,
    RetryPolicyRefV1,
    RunPayloadEnvelope,
    RunManifestParentBindingV1,
    RunManifestVersionProjectionV1,
    RuntimeParentRuleV1,
    RunFailureV1,
    RunResultSummaryV1,
    RunResultV1,
    execution_version_plan_digest,
    referenced_input_artifact_ids,
)
from gameforge.contracts.lineage import ObjectLocation, ObjectRef, VersionTuple
from gameforge.contracts.storage import RefValue


_HASH_A = "a" * 64
_HASH_B = "b" * 64
_OBJECT_KEY = f"objects/v1/sha256/{_HASH_A[:2]}/{_HASH_A}"
_MAX_ITEMS = 1024
_MAX_FINDINGS = MAX_PREPARED_FINDINGS
_MAX_MANIFEST_ITEMS = MAX_RUN_MANIFEST_PARENT_BINDINGS
_MAX_STRING_LENGTH = 4096
_MAX_ID_LENGTH = 512
_MAX_SEED = (1 << 64) - 1


def _profile(profile_id: str = "profile") -> ProfileRefV1:
    return ProfileRefV1(profile_id=profile_id, version=1)


def _target() -> RefReadBindingV1:
    return RefReadBindingV1(
        ref_name="refs/content/current",
        expected_ref=RefValue(artifact_id="artifact:base", revision=1),
    )


def _generation_payload(
    *,
    constraint_snapshot_artifact_id: str | None,
    candidate_export_profiles: tuple[ProfileRefV1, ...],
) -> GenerationProposePayloadV1:
    return GenerationProposePayloadV1(
        base_snapshot_artifact_id="artifact:base",
        constraint_snapshot_artifact_id=constraint_snapshot_artifact_id,
        findings=(),
        objective_goal=PromptGoalBindingV1(
            source_artifact_id="artifact:goal",
            expected_payload_hash=_HASH_A,
        ),
        domain_scope=DomainScope(domain_ids=("quests",)),
        target=_target(),
        generation_policy=_profile("generation"),
        candidate_export_profiles=candidate_export_profiles,
    )


def _patch_repair_payload(
    *,
    constraint_snapshot_artifact_id: str | None,
    candidate_export_profiles: tuple[ProfileRefV1, ...],
) -> PatchRepairPayloadV1:
    return PatchRepairPayloadV1(
        subject_patch_artifact_id="artifact:patch",
        expected_subject_head_revision=1,
        expected_workflow_revision=1,
        base_snapshot_artifact_id="artifact:base",
        preview_snapshot_artifact_id="artifact:preview",
        constraint_snapshot_artifact_id=constraint_snapshot_artifact_id,
        validation_evidence_artifact_id="artifact:validation",
        findings=(),
        target=_target(),
        repair_policy=_profile("repair"),
        checker_profiles=(),
        simulation_profiles=(),
        regression_suite_artifact_ids=(),
        candidate_export_profiles=candidate_export_profiles,
    )


def _run_payload(**changes: Any) -> RunPayloadEnvelope:
    snapshot_artifact_id = changes.pop("snapshot_artifact_id", "artifact:snapshot")
    params = CheckerRunPayloadV1(
        snapshot_artifact_id=snapshot_artifact_id,
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        checker_profile=_profile("checker"),
        checker_ids=(),
        defect_classes=(),
    )
    values: dict[str, Any] = {
        "payload_schema_version": "checker-run@1",
        "input_artifact_ids": (snapshot_artifact_id,),
        "version_tuple": VersionTuple(
            ir_snapshot_id="snapshot:1",
            tool_version="checker@1",
        ),
        "policy_bindings": (),
        "schema_bindings": (),
        "execution_profile_catalog_version": 1,
        "execution_profile_catalog_digest": _HASH_A,
        "resolved_profiles": (),
        "resolved_policy_snapshots": (),
        "budget_set_snapshot_id": "budget:1",
        "llm_execution_mode": "not_applicable",
        "params": params,
    }
    values.update(changes)
    return RunPayloadEnvelope.model_validate(values)


def _playtest_episode_bindings(count: int) -> tuple[PlaytestEpisodeBindingV1, ...]:
    return tuple(
        PlaytestEpisodeBindingV1(
            episode_id=f"episode:{index:04d}",
            scenario_spec_artifact_id=f"artifact:scenario:{index:04d}",
        )
        for index in range(count)
    )


def _playtest_payload(*, episode_count: int) -> PlaytestRunPayloadV1:
    return PlaytestRunPayloadV1(
        config_artifact_id="artifact:config",
        constraint_snapshot_artifact_id="artifact:constraint",
        task_suite_artifact_id="artifact:suite",
        episodes=_playtest_episode_bindings(episode_count),
        environment_profile=_profile("environment"),
        planner_policy=_profile("planner"),
        max_steps_per_episode=1,
        interaction_mode="autonomous",
    )


def _playtest_execution_plan() -> ExecutionVersionPlanV1:
    values = {
        "agent_graph_version": "playtest-graph@1",
        "nodes": (
            PlannedAgentNodeVersionV1(
                agent_node_id="playtest.planner",
                prompt_version="playtest@1",
                tool_version="playtest.planner@1",
                allowed_model_snapshots=("provider/model@1",),
            ),
        ),
        "model_catalog_version": 1,
        "model_catalog_digest": _HASH_A,
        "routing_policy_version": 1,
        "routing_policy_digest": _HASH_B,
    }
    return ExecutionVersionPlanV1(
        **values,
        plan_digest=execution_version_plan_digest(values),
    )


@pytest.mark.parametrize(
    "factory",
    [_generation_payload, _patch_repair_payload],
    ids=["generation", "patch-repair"],
)
def test_candidate_exports_require_constraint_snapshot(
    factory: Callable[..., BaseModel],
) -> None:
    with pytest.raises(ValidationError, match="constraint_snapshot_artifact_id"):
        factory(
            constraint_snapshot_artifact_id=None,
            candidate_export_profiles=(_profile("export"),),
        )


@pytest.mark.parametrize(
    "factory",
    [_generation_payload, _patch_repair_payload],
    ids=["generation", "patch-repair"],
)
def test_empty_candidate_exports_allow_missing_constraint_snapshot(
    factory: Callable[..., BaseModel],
) -> None:
    payload = factory(
        constraint_snapshot_artifact_id=None,
        candidate_export_profiles=(),
    )

    assert payload.constraint_snapshot_artifact_id is None
    assert payload.candidate_export_profiles == ()


def test_run_payload_envelope_publishes_direct_hard_bounds() -> None:
    properties = RunPayloadEnvelope.model_json_schema()["properties"]

    assert properties["payload_schema_version"]["maxLength"] == _MAX_STRING_LENGTH
    assert properties["input_artifact_ids"]["maxItems"] == MAX_PLAYTEST_RUN_INPUT_ARTIFACTS
    assert properties["input_artifact_ids"]["items"]["maxLength"] == _MAX_ID_LENGTH
    assert properties["budget_set_snapshot_id"]["maxLength"] == _MAX_ID_LENGTH
    cassette_schema = next(
        branch
        for branch in properties["cassette_artifact_id"]["anyOf"]
        if branch.get("type") == "string"
    )
    assert cassette_schema["maxLength"] == _MAX_ID_LENGTH
    seed_schema = next(
        branch for branch in properties["seed"]["anyOf"] if branch.get("type") == "integer"
    )
    assert seed_schema == {
        "maximum": _MAX_SEED,
        "minimum": 0,
        "type": "integer",
    }


def test_playtest_accepts_exact_1024_episode_input_closure_and_rejects_cap_plus_one() -> None:
    params = _playtest_payload(episode_count=_MAX_ITEMS)
    direct_inputs = referenced_input_artifact_ids(params)

    assert len(params.episodes) == _MAX_ITEMS
    assert len(direct_inputs) == MAX_PLAYTEST_DIRECT_INPUT_ARTIFACTS == _MAX_ITEMS + 3
    envelope = RunPayloadEnvelope(
        payload_schema_version=params.schema_version,
        input_artifact_ids=direct_inputs,
        version_tuple=VersionTuple(
            ir_snapshot_id="snapshot:1",
            constraint_snapshot_id="constraint:1",
            env_contract_version="env@1",
            seed=1,
            tool_version="playtest@1",
        ),
        execution_version_plan=_playtest_execution_plan(),
        policy_bindings=(),
        schema_bindings=(),
        execution_profile_catalog_version=1,
        execution_profile_catalog_digest=_HASH_A,
        resolved_profiles=(),
        resolved_policy_snapshots=(),
        budget_set_snapshot_id="budget:1",
        seed=1,
        llm_execution_mode="record",
        params=params,
    )
    assert envelope.input_artifact_ids == direct_inputs

    replay_inputs = tuple(sorted((*direct_inputs, "artifact:cassette")))
    replay = envelope.model_copy(
        update={
            "input_artifact_ids": replay_inputs,
            "llm_execution_mode": "replay",
            "cassette_artifact_id": "artifact:cassette",
        }
    )
    replay = RunPayloadEnvelope.model_validate(replay.model_dump(mode="python"))
    assert len(replay.input_artifact_ids) == MAX_PLAYTEST_RUN_INPUT_ARTIFACTS

    with pytest.raises(ValidationError, match="episodes"):
        _playtest_payload(episode_count=_MAX_ITEMS + 1)


@pytest.mark.parametrize(
    "changes",
    [
        {"payload_schema_version": "x" * (_MAX_STRING_LENGTH + 1)},
        {"snapshot_artifact_id": "x" * (_MAX_ID_LENGTH + 1)},
        {"budget_set_snapshot_id": "x" * (_MAX_ID_LENGTH + 1)},
        {"cassette_artifact_id": "x" * (_MAX_ID_LENGTH + 1)},
        {"seed": -1},
        {"seed": _MAX_SEED + 1},
    ],
    ids=[
        "payload-schema-version",
        "input-artifact-id",
        "budget-set-id",
        "cassette-artifact-id",
        "negative-seed",
        "oversized-seed",
    ],
)
def test_run_payload_envelope_rejects_unbounded_direct_fields(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        _run_payload(**changes)


def _prepared_artifact(**changes: Any) -> PreparedArtifact:
    values: dict[str, Any] = {
        "kind": "checker_run",
        "payload_schema_id": "checker-run@1",
        "version_tuple": VersionTuple(
            ir_snapshot_id="snapshot:1",
            tool_version="checker@1",
        ),
        "lineage": ("artifact:input",),
        "payload_hash": _HASH_A,
        "meta": {"source": "historical", "nested": {"items": [1, None, True]}},
        "object_ref": ObjectRef(
            key=_OBJECT_KEY,
            sha256=_HASH_A,
            size_bytes=1,
        ),
        "location": ObjectLocation(
            store_id="local",
            key=_OBJECT_KEY,
            backend_generation="generation:1",
        ),
    }
    values.update(changes)
    return PreparedArtifact.model_validate(values)


def _prompt_source_ids(count: int) -> tuple[str, ...]:
    return tuple(f"artifact:prompt-source:{index:04d}" for index in range(count))


def _prompt_message() -> AgentPromptSourceMessageV1:
    return AgentPromptSourceMessageV1(
        role="user",
        content="exact playtest context",
        purpose="context",
    )


def _prompt_binding(*, binding_key: str, artifact_id: str) -> AgentPromptArtifactBindingV1:
    return AgentPromptArtifactBindingV1(
        binding_key=binding_key,
        artifact_id=artifact_id,
        artifact_kind="scenario_spec",
        payload_schema_id="scenario-spec@1",
        payload_hash=_HASH_A,
    )


def _prior_consumption() -> AgentPromptPriorConsumptionV1:
    return AgentPromptPriorConsumptionV1(
        attempt_no=1,
        call_ordinal=1,
        route_ordinal=1,
        prompt_artifact_id="artifact:prior-prompt",
        request_hash=_HASH_A,
        routing_decision_kind="native",
        routing_decision_id="routing:1",
        execution_source="online",
        reservation_group_id="reservation:1",
        transport_attempt=1,
        cassette_shard_artifact_id="artifact:prior-shard",
        cassette_source_artifact_id="artifact:prior-shard",
        response_digest=_HASH_B,
    )


def _prepared_finding(index: int = 0) -> PreparedFindingV1:
    return PreparedFindingV1(
        finding_id=f"finding:{index}",
        expected_previous_revision=None,
        evidence_artifact_index=0,
        payload=FindingPayloadV1(
            source="checker",
            producer_id="checker:graph",
            producer_run_id="run:1",
            oracle_type="deterministic",
            defect_class="quest_unreachable",
            severity="major",
            snapshot_id="snapshot:1",
            status="confirmed",
            message="unreachable quest",
        ),
    )


def _disposition(index: int = 0) -> RequirementDispositionV1:
    return RequirementDispositionV1(
        resolved_policy_id="policy:1",
        outcome_rule_id="rule:1",
        requirement_id=f"requirement:{index}",
        status="produced",
    )


def _prepared_result(**changes: Any) -> PreparedRunResult:
    artifact = _prepared_artifact()
    values: dict[str, Any] = {
        "run_id": "run:1",
        "attempt_no": 1,
        "run_kind": RunKindRef(kind="checker.run", version=1),
        "primary_index": 0,
        "artifacts": (artifact,),
        "findings": (),
        "requirement_dispositions": (),
        "summary": PreparedRunResultSummaryV1(
            outcome_code="passed",
            primary_artifact_kind="checker_run",
            prepared_domain_artifact_count=1,
            prepared_finding_count=0,
        ),
    }
    values.update(changes)
    return PreparedRunResult.model_validate(values)


def _classifier_ref() -> FailureClassifierRefV1:
    return FailureClassifierRefV1(
        classifier_version=1,
        classifier_digest=_HASH_A,
    )


def _retry_policy_ref() -> RetryPolicyRefV1:
    return RetryPolicyRefV1(
        retry_policy_id="retry-policy:1",
        retry_policy_version=1,
        retry_policy_digest=_HASH_B,
    )


def _retry_decision(**changes: Any) -> RetryDecisionV1:
    values: dict[str, Any] = {
        "cause_code": "validation_failed",
        "failure_class": "validation",
        "intrinsic_retry_eligible": False,
        "decision": "terminal",
        "reason_code": "not_retry_eligible",
        "classifier": _classifier_ref(),
        "retry_policy": _retry_policy_ref(),
        "evaluated_at_utc": "2026-07-14T00:00:00Z",
    }
    values.update(changes)
    return RetryDecisionV1.model_validate(values)


def _prepared_failure(**changes: Any) -> PreparedRunFailure:
    values: dict[str, Any] = {
        "run_id": "run:1",
        "attempt_no": 1,
        "run_kind": RunKindRef(kind="checker.run", version=1),
        "artifacts": (),
        "requirement_dispositions": (),
        "cause_code": "validation_failed",
        "failure_class": "validation",
        "intrinsic_retry_eligible": False,
        "classifier": _classifier_ref(),
        "redacted_message": "deterministic validation failed",
    }
    values.update(changes)
    return PreparedRunFailure.model_validate(values)


def _run_result(**changes: Any) -> RunResultV1:
    run_kind = RunKindRef(kind="checker.run", version=1)
    version_tuple = VersionTuple(
        ir_snapshot_id="snapshot:1",
        tool_version="checker@1",
    )
    values: dict[str, Any] = {
        "run_id": "run:1",
        "attempt_no": 1,
        "run_kind": run_kind,
        "primary_artifact_id": "artifact:output",
        "produced_artifact_ids": ("artifact:output",),
        "finding_count": 0,
        "outcome_code": "passed",
        "summary": RunResultSummaryV1(
            outcome_code="passed",
            primary_artifact_kind="checker_run",
            produced_artifact_count=1,
            finding_count=0,
        ),
        "requirement_dispositions": (),
        "version_projection": RunManifestVersionProjectionV1(
            manifest_scope="run",
            attempt_no=1,
            run_kind=run_kind,
            run_payload_hash=_HASH_A,
            frozen_input_version_tuple=version_tuple,
            terminal_version_tuple=version_tuple,
            version_transition_policy_ref=VersionTransitionPolicyRefV1(
                policy_id="deterministic",
                policy_version=1,
                digest=_HASH_B,
            ),
            parents=(
                RunManifestParentBindingV1(
                    artifact_id="artifact:output",
                    role="output",
                    publication="run_published",
                    ordinal=1,
                ),
            ),
        ),
    }
    values.update(changes)
    return RunResultV1.model_validate(values)


def _run_failure(**changes: Any) -> RunFailureV1:
    run_kind = RunKindRef(kind="checker.run", version=1)
    version_tuple = VersionTuple(
        ir_snapshot_id="snapshot:1",
        tool_version="checker@1",
    )
    values: dict[str, Any] = {
        "run_id": "run:1",
        "attempt_no": 1,
        "run_kind": run_kind,
        "cause_code": "validation_failed",
        "failure_class": "validation",
        "retryable": False,
        "retry_decision": _retry_decision(),
        "redacted_message": "deterministic validation failed",
        "evidence_artifact_ids": (),
        "requirement_dispositions": (),
        "occurred_at": "2026-07-14T00:00:00Z",
        "version_projection": RunManifestVersionProjectionV1(
            manifest_scope="run",
            attempt_no=1,
            run_kind=run_kind,
            run_payload_hash=_HASH_A,
            frozen_input_version_tuple=version_tuple,
            terminal_version_tuple=version_tuple,
            version_transition_policy_ref=VersionTransitionPolicyRefV1(
                policy_id="deterministic",
                policy_version=1,
                digest=_HASH_B,
            ),
            parents=(),
        ),
    }
    values.update(changes)
    return RunFailureV1.model_validate(values)


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (PlannedAgentNodeVersionV1, "allowed_model_snapshots"),
        (ExecutionVersionPlanV1, "nodes"),
        (ResolvedPolicySnapshotV1, "requirements"),
        (PreparedRunResult, "requirement_dispositions"),
        (PreparedRunFailure, "artifacts"),
        (PreparedRunFailure, "requirement_dispositions"),
        (RunResultV1, "requirement_dispositions"),
        (RunFailureV1, "requirement_dispositions"),
    ],
)
def test_run_result_collections_publish_frozen_max_items(
    model: type[BaseModel], field: str
) -> None:
    property_schema = model.model_json_schema()["properties"][field]

    assert property_schema["maxItems"] == _MAX_ITEMS


def test_playtest_closure_fields_publish_dedicated_capacities() -> None:
    assert (
        PreparedArtifact.model_json_schema()["properties"]["lineage"]["maxItems"]
        == MAX_PLAYTEST_TRACE_LINEAGE_PARENTS
        == MAX_PLAYTEST_DIRECT_INPUT_ARTIFACTS
    )
    assert (
        PreparedRunResult.model_json_schema()["properties"]["artifacts"]["maxItems"]
        == MAX_PREPARED_DOMAIN_ARTIFACTS
        == _MAX_ITEMS + 1
    )
    assert (
        AgentPromptContextDraftV1.model_json_schema()["properties"]["source_artifact_ids"][
            "maxItems"
        ]
        == MAX_PLAYTEST_PROMPT_SOURCE_ARTIFACTS
        == MAX_PLAYTEST_DIRECT_INPUT_ARTIFACTS
    )
    assert (
        AgentPromptContextV1.model_json_schema()["properties"]["upstream_artifacts"]["maxItems"]
        == MAX_PLAYTEST_PROMPT_UPSTREAM_ARTIFACTS
        == MAX_PLAYTEST_DIRECT_INPUT_ARTIFACTS + 2
    )


def test_prepared_findings_publish_the_dedicated_finding_capacity() -> None:
    assert PreparedRunResult.model_json_schema()["properties"]["findings"]["maxItems"] == (
        _MAX_FINDINGS
    )


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (RunManifestVersionProjectionV1, "parents"),
        (RunResultV1, "produced_artifact_ids"),
        (RunFailureV1, "evidence_artifact_ids"),
    ],
)
def test_run_manifest_collections_publish_dedicated_capacity(
    model: type[BaseModel], field: str
) -> None:
    property_schema = model.model_json_schema()["properties"][field]

    assert _MAX_MANIFEST_ITEMS == 32_768
    assert property_schema["maxItems"] == _MAX_MANIFEST_ITEMS


@pytest.mark.parametrize(
    ("model", "field"),
    [
        (PlannedAgentNodeVersionV1, "agent_node_id"),
        (PlannedAgentNodeVersionV1, "prompt_version"),
        (PlannedAgentNodeVersionV1, "tool_version"),
        (ExecutionVersionPlanV1, "agent_graph_version"),
        (ResolvedArtifactRequirementV1, "requirement_id"),
        (ResolvedArtifactRequirementV1, "outcome_rule_id"),
        (ResolvedArtifactRequirementV1, "payload_schema_id"),
        (ResolvedPolicySnapshotV1, "resolved_policy_id"),
        (DependencyFailureV1, "dependency_id"),
        (DependencyFailureV1, "operation_code"),
        (DependencyFailureV1, "classifier_code"),
    ],
)
def test_nested_run_payload_and_failure_strings_publish_max_length(
    model: type[BaseModel],
    field: str,
) -> None:
    property_schema = model.model_json_schema()["properties"][field]

    assert property_schema["maxLength"] == _MAX_STRING_LENGTH


def test_prepared_artifact_accepts_exact_playtest_lineage_and_rejects_cap_plus_one() -> None:
    lineage = _prompt_source_ids(MAX_PLAYTEST_TRACE_LINEAGE_PARENTS)
    assert _prepared_artifact(lineage=lineage).lineage == lineage

    with pytest.raises(ValidationError):
        _prepared_artifact(lineage=_prompt_source_ids(MAX_PLAYTEST_TRACE_LINEAGE_PARENTS + 1))


def test_playtest_prompt_sources_accept_exact_direct_input_closure_and_reject_extra() -> None:
    source_ids = _prompt_source_ids(MAX_PLAYTEST_PROMPT_SOURCE_ARTIFACTS)
    draft = AgentPromptContextDraftV1(
        context_kind="playtest",
        messages=(_prompt_message(),),
        source_artifact_ids=source_ids,
    )
    assert draft.source_artifact_ids == source_ids

    with pytest.raises(ValidationError, match="source_artifact_ids"):
        AgentPromptContextDraftV1(
            context_kind="playtest",
            messages=(_prompt_message(),),
            source_artifact_ids=_prompt_source_ids(MAX_PLAYTEST_PROMPT_SOURCE_ARTIFACTS + 1),
        )


def test_record_prompt_upstream_accepts_direct_inputs_plus_prior_prompt_and_shard() -> None:
    source_ids = _prompt_source_ids(MAX_PLAYTEST_PROMPT_SOURCE_ARTIFACTS)
    prior = _prior_consumption()
    upstream = tuple(
        _prompt_binding(binding_key=f"source:{index:04d}", artifact_id=artifact_id)
        for index, artifact_id in enumerate(source_ids, start=1)
    ) + (
        _prompt_binding(binding_key="prior.prompt", artifact_id=prior.prompt_artifact_id),
        _prompt_binding(
            binding_key="prior.cassette_source",
            artifact_id=prior.cassette_source_artifact_id or "",
        ),
    )
    context = AgentPromptContextV1(
        context_kind="playtest",
        run_id="run:1",
        attempt_no=1,
        target_call_ordinal=2,
        agent_node_id="playtest.executor",
        prompt_version="playtest@2",
        messages=(_prompt_message(),),
        upstream_artifacts=upstream,
        prior_consumption=prior,
    )
    assert len(context.upstream_artifacts) == MAX_PLAYTEST_PROMPT_UPSTREAM_ARTIFACTS

    with pytest.raises(ValidationError, match="upstream_artifacts"):
        AgentPromptContextV1(
            context_kind="playtest",
            run_id="run:1",
            attempt_no=1,
            target_call_ordinal=2,
            agent_node_id="playtest.executor",
            prompt_version="playtest@2",
            messages=(_prompt_message(),),
            upstream_artifacts=upstream
            + (
                _prompt_binding(
                    binding_key="source:extra",
                    artifact_id="artifact:prompt-source:extra",
                ),
            ),
            prior_consumption=prior,
        )


def test_prepared_result_rejects_oversized_artifact_collection() -> None:
    artifact = _prepared_artifact()
    with pytest.raises(ValidationError):
        _prepared_result(
            artifacts=(artifact,) * (MAX_PREPARED_DOMAIN_ARTIFACTS + 1),
            summary=PreparedRunResultSummaryV1(
                outcome_code="passed",
                primary_artifact_kind="checker_run",
                prepared_domain_artifact_count=MAX_PREPARED_DOMAIN_ARTIFACTS + 1,
                prepared_finding_count=0,
            ),
        )


def test_prepared_result_rejects_oversized_finding_collection() -> None:
    finding = _prepared_finding()
    with pytest.raises(ValidationError):
        _prepared_result(
            findings=(finding,) * (_MAX_FINDINGS + 1),
            summary=PreparedRunResultSummaryV1(
                outcome_code="passed",
                primary_artifact_kind="checker_run",
                prepared_domain_artifact_count=1,
                prepared_finding_count=_MAX_FINDINGS + 1,
            ),
        )


def test_prepared_result_rejects_oversized_disposition_collection() -> None:
    with pytest.raises(ValidationError):
        _prepared_result(
            requirement_dispositions=tuple(_disposition(index) for index in range(_MAX_ITEMS + 1))
        )


def test_run_result_rejects_oversized_produced_artifact_collection() -> None:
    artifact_ids = tuple(f"artifact:{index}" for index in range(_MAX_MANIFEST_ITEMS + 1))
    with pytest.raises(ValidationError):
        _run_result(
            primary_artifact_id=artifact_ids[0],
            produced_artifact_ids=artifact_ids,
            summary=RunResultSummaryV1(
                outcome_code="passed",
                primary_artifact_kind="checker_run",
                produced_artifact_count=len(artifact_ids),
                finding_count=0,
            ),
        )


def test_run_result_rejects_oversized_disposition_collection() -> None:
    with pytest.raises(ValidationError):
        _run_result(
            requirement_dispositions=tuple(_disposition(index) for index in range(_MAX_ITEMS + 1))
        )


def test_prepared_failure_rejects_oversized_artifact_collection() -> None:
    artifact = _prepared_artifact()
    with pytest.raises(ValidationError):
        _prepared_failure(artifacts=(artifact,) * (_MAX_ITEMS + 1))


def test_prepared_failure_rejects_oversized_disposition_collection() -> None:
    with pytest.raises(ValidationError):
        _prepared_failure(
            requirement_dispositions=tuple(_disposition(index) for index in range(_MAX_ITEMS + 1))
        )


def test_manifest_projection_rejects_oversized_parent_collection() -> None:
    projection = _run_result().version_projection
    with pytest.raises(ValidationError):
        RunManifestVersionProjectionV1.model_validate(
            {
                **projection.model_dump(mode="python"),
                "parents": tuple(
                    RunManifestParentBindingV1(
                        artifact_id=f"artifact:{index}",
                        role="evidence",
                        publication="existing",
                    )
                    for index in range(_MAX_MANIFEST_ITEMS + 1)
                ),
            }
        )


def test_run_failure_rejects_oversized_evidence_collection() -> None:
    with pytest.raises(ValidationError):
        _run_failure(
            evidence_artifact_ids=tuple(
                f"artifact:{index}" for index in range(_MAX_MANIFEST_ITEMS + 1)
            )
        )


def test_run_manifest_projection_accepts_more_than_legacy_collection_bound() -> None:
    projection = _run_result().version_projection
    parent_count = _MAX_ITEMS + 1
    parents = tuple(
        RunManifestParentBindingV1(
            artifact_id=f"artifact:{index}",
            role="intermediate",
            publication="run_published",
        )
        for index in range(parent_count)
    )

    retained = RunManifestVersionProjectionV1.model_validate(
        {
            **projection.model_dump(mode="python"),
            "parents": parents,
        }
    )

    assert len(retained.parents) == parent_count


def test_run_failure_rejects_oversized_disposition_collection() -> None:
    with pytest.raises(ValidationError):
        _run_failure(
            requirement_dispositions=tuple(_disposition(index) for index in range(_MAX_ITEMS + 1))
        )


def _nested_meta(depth: int) -> dict[str, Any]:
    value: Any = "leaf"
    for _ in range(depth - 1):
        value = {"child": value}
    return value


@pytest.mark.parametrize(
    "meta",
    [
        {"value": "x" * (_MAX_STRING_LENGTH + 1)},
        {"x" * (_MAX_STRING_LENGTH + 1): "value"},
        {"items": list(range(_MAX_ITEMS + 1))},
        _nested_meta(33),
        {f"key:{index}": "x" * 64 for index in range(_MAX_ITEMS)},
    ],
    ids=[
        "string-length",
        "key-length",
        "nested-collection-size",
        "depth",
        "canonical-byte-size",
    ],
)
def test_prepared_artifact_meta_rejects_unbounded_json(meta: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _prepared_artifact(meta=meta)


def test_prepared_artifact_meta_preserves_ordinary_historical_payload() -> None:
    meta = {"source": "historical", "nested": {"items": [1, None, True]}}

    assert _prepared_artifact(meta=meta).meta == meta


@pytest.mark.parametrize(
    "build",
    [
        lambda value: _prepared_artifact(payload_schema_id=value),
        lambda value: _prepared_artifact(lineage=(value,)),
        lambda value: RequirementDispositionV1.model_validate(
            {**_disposition().model_dump(), "requirement_id": value}
        ),
        lambda value: _run_result(
            primary_artifact_id=value,
            produced_artifact_ids=(value,),
        ),
    ],
    ids=[
        "payload-schema-id",
        "lineage-id",
        "requirement-id",
        "produced-artifact-id",
    ],
)
def test_direct_run_result_strings_are_bounded(
    build: Callable[[str], BaseModel],
) -> None:
    with pytest.raises(ValidationError):
        build("x" * (_MAX_STRING_LENGTH + 1))


@pytest.mark.parametrize(
    "build",
    [
        lambda value: _prepared_failure(run_id=value),
        lambda value: _prepared_failure(cause_code=value),
        lambda value: _prepared_failure(redacted_message=value),
        lambda value: _run_failure(run_id=value),
        lambda value: _run_failure(
            cause_code=value,
            retry_decision=_retry_decision(cause_code=value),
        ),
        lambda value: _run_failure(redacted_message=value),
        lambda value: _run_failure(occurred_at=value),
        lambda value: _run_failure(evidence_artifact_ids=(value,)),
        lambda value: RunManifestParentBindingV1(
            artifact_id=value,
            role="input",
            publication="existing",
        ),
    ],
    ids=[
        "prepared-run-id",
        "prepared-cause-code",
        "prepared-message",
        "failure-run-id",
        "failure-cause-code",
        "failure-message",
        "failure-occurred-at",
        "failure-evidence-id",
        "manifest-parent-id",
    ],
)
def test_failure_and_manifest_strings_are_bounded(
    build: Callable[[str], BaseModel],
) -> None:
    with pytest.raises(ValidationError):
        build("x" * (_MAX_STRING_LENGTH + 1))


@pytest.mark.parametrize(
    "errors",
    [
        ({"message": "x" * (_MAX_STRING_LENGTH + 1)},),
        ({"x" * (_MAX_STRING_LENGTH + 1): "value"},),
        ({"items": list(range(_MAX_ITEMS + 1))},),
        (_nested_meta(33),),
        ({f"key:{index}": "x" * 64 for index in range(_MAX_ITEMS)},),
        tuple({"message": "x" * 64} for _ in range(_MAX_ITEMS)),
    ],
    ids=[
        "string-length",
        "key-length",
        "nested-collection-size",
        "depth",
        "canonical-byte-size",
        "aggregate-canonical-byte-size",
    ],
)
def test_problem_errors_reject_unbounded_json(
    errors: tuple[dict[str, Any], ...],
) -> None:
    with pytest.raises(ValidationError):
        Problem(
            type="https://gameforge.dev/problems/invalid-request",
            title="Invalid request",
            status=422,
            detail="Request validation failed",
            instance="/api/v1/runs",
            code="request.invalid",
            request_id="request:1",
            errors=errors,
        )


def test_problem_errors_preserve_ordinary_bounded_json() -> None:
    errors = ({"location": ["body", "kind"], "message": "required"},)

    problem = Problem(
        type="https://gameforge.dev/problems/invalid-request",
        title="Invalid request",
        status=422,
        detail="Request validation failed",
        instance="/api/v1/runs",
        code="request.invalid",
        request_id="request:1",
        errors=errors,
    )

    assert problem.errors == errors


def test_runtime_parent_execution_modes_are_nonempty_unique_and_canonical() -> None:
    rule = RuntimeParentRuleV1(
        rule_id="record-shards",
        manifest_scope="attempt",
        source="record_shard",
        parent_role="intermediate",
        artifact_kind="cassette_bundle",
        payload_schema_ids=("cassette-record-shard@1",),
        attempt_selector="current",
        enabled_execution_modes=("replay", "record"),
        min_count=0,
        max_count=None,
    )
    assert rule.enabled_execution_modes == ("record", "replay")

    for invalid in ((), ("record", "record")):
        with pytest.raises(ValidationError):
            RuntimeParentRuleV1(
                **{
                    **rule.model_dump(mode="python"),
                    "enabled_execution_modes": invalid,
                }
            )
