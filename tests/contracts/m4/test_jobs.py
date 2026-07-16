from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from gameforge.contracts.execution_profiles import (
    RunKindRef as ProfileRunKindRef,
)
from gameforge.contracts.jobs import (
    AttemptLeasedDataV1,
    ArtifactCountBindingV1,
    ArtifactMigrationPayloadV1,
    BenchRunPayloadV1,
    CancelRunPayloadV1,
    CheckerRunPayloadV1,
    CompletionOracleRegistryRefV1,
    ConstraintProposalProposePayloadV1,
    ConstraintValidationPayloadV1,
    DrDrillPayloadV1,
    ExecutionIdentityCountBindingV1,
    GraphSelectionV1,
    JsonCollectionCountBindingV1,
    GenerationProposePayloadV1,
    DependencyFailureV1,
    FailureClassificationRuleV1,
    FailureClassifierRefV1,
    FailureClassifierV1,
    ExecutionVersionPlanV1,
    PlannedAgentNodeVersionV1,
    PatchRepairPayloadV1,
    PatchValidationPayloadV1,
    PlaytestProvideInputPayloadV1,
    PlaytestEpisodeBindingV1,
    PlaytestRunPayloadV1,
    PromptGoalBindingV1,
    ProfileRefV1,
    PreparedArtifact,
    PreparedRunResult,
    PreparedRunResultSummaryV1,
    RequirementDispositionV1,
    RefReadBindingV1,
    ReviewRunPayloadV1,
    RollbackValidationPayloadV1,
    RetryDecisionV1,
    RetryPolicyRefV1,
    RunAttempt,
    RunCommandAckV1,
    RunCommandRecordV1,
    RunCommandV1,
    RunCommandViewV1,
    RunDispatchTraceCarrierV1,
    RunEvent,
    RunEventDefinitionV1,
    RunEventRegistryV1,
    RunKindRef,
    RunKindPayload,
    RunManifestParentBindingV1,
    RunManifestVersionProjectionV1,
    RunPayloadEnvelope,
    RunRecord,
    RunQueuedDataV1,
    RunResultSummaryV1,
    RunResultV1,
    SimulationRunPayloadV1,
    SolverEngineRefV1,
    TaskSuiteDerivePayloadV1,
    ValidationSubjectBindingV1,
    VersionTransitionPolicyRefV1,
    canonical_payload_hash,
    run_event_registry_digest,
    failure_classifier_digest,
    execution_version_plan_digest,
)
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.lineage import AuditActor, ObjectLocation, ObjectRef, VersionTuple
from gameforge.contracts.storage import RefValue
from gameforge.contracts.workflow import FindingEvidenceBindingV1


_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64


def _classifier_ref() -> FailureClassifierRefV1:
    return FailureClassifierRefV1(classifier_version=1, classifier_digest=_HASH_A)


def _retry_ref() -> RetryPolicyRefV1:
    return RetryPolicyRefV1(
        retry_policy_id="default",
        retry_policy_version=1,
        retry_policy_digest=_HASH_B,
    )


def _profile(profile_id: str) -> ProfileRefV1:
    return ProfileRefV1(profile_id=profile_id, version=1)


def _target() -> RefReadBindingV1:
    return RefReadBindingV1(
        ref_name="refs/content/current",
        expected_ref=RefValue(artifact_id="artifact:base", revision=1),
    )


def _subject() -> ValidationSubjectBindingV1:
    return ValidationSubjectBindingV1(
        approval_id="approval:1",
        expected_workflow_revision=2,
        subject_head_revision=1,
        subject_artifact_id="artifact:subject",
        subject_digest=_HASH_A,
        active_validation_run_id="run:validation",
    )


def _finding() -> FindingEvidenceBindingV1:
    return FindingEvidenceBindingV1(
        finding_id="finding:1",
        finding_revision=1,
        evidence_artifact_id="artifact:finding-evidence",
        finding_digest=_HASH_B,
    )


def _all_run_kind_payloads() -> tuple[object, ...]:
    selection = GraphSelectionV1(mode="full", entity_ids=(), relation_ids=())
    return (
        GenerationProposePayloadV1(
            base_snapshot_artifact_id="artifact:base",
            constraint_snapshot_artifact_id="artifact:constraints",
            findings=(_finding(),),
            objective_goal=PromptGoalBindingV1(
                source_artifact_id="artifact:goal", expected_payload_hash=_HASH_A
            ),
            domain_scope=DomainScope(domain_ids=("quests",)),
            target=_target(),
            generation_policy=_profile("generation"),
            candidate_export_profiles=(_profile("export"),),
        ),
        PatchRepairPayloadV1(
            subject_patch_artifact_id="artifact:patch",
            expected_subject_head_revision=1,
            expected_workflow_revision=2,
            base_snapshot_artifact_id="artifact:base",
            preview_snapshot_artifact_id="artifact:preview",
            validation_evidence_artifact_id="artifact:validation",
            findings=(_finding(),),
            target=_target(),
            repair_policy=_profile("repair"),
            checker_profiles=(_profile("checker"),),
            simulation_profiles=(),
            regression_suite_artifact_ids=("artifact:regression",),
            candidate_export_profiles=(),
        ),
        ConstraintProposalProposePayloadV1(
            source_artifact_ids=("artifact:source",),
            domain_scope=DomainScope(domain_ids=("quests",)),
            authoring_goal=PromptGoalBindingV1(
                source_artifact_id="artifact:goal", expected_payload_hash=_HASH_A
            ),
            dsl_grammar_version="dsl@1",
            extraction_policy=_profile("extract"),
        ),
        ReviewRunPayloadV1(
            snapshot_artifact_id="artifact:snapshot",
            selection=selection,
            review_profile=_profile("review"),
            checker_profiles=(_profile("checker"),),
            simulation_profiles=(),
        ),
        CheckerRunPayloadV1(
            snapshot_artifact_id="artifact:snapshot",
            selection=selection,
            checker_profile=_profile("checker"),
            checker_ids=("graph",),
            defect_classes=("dangling_ref",),
        ),
        SimulationRunPayloadV1(
            snapshot_artifact_id="artifact:snapshot",
            simulation_profile=_profile("simulation"),
            workload_profile=_profile("workload"),
            replication_count=10,
            horizon_steps=100,
        ),
        PlaytestRunPayloadV1(
            config_artifact_id="artifact:config",
            constraint_snapshot_artifact_id="artifact:constraints",
            task_suite_artifact_id="artifact:suite",
            episodes=(
                PlaytestEpisodeBindingV1(
                    episode_id="episode:1",
                    scenario_spec_artifact_id="artifact:scenario",
                ),
            ),
            environment_profile=_profile("environment"),
            planner_policy=_profile("planner"),
            max_steps_per_episode=100,
            interaction_mode="autonomous",
        ),
        TaskSuiteDerivePayloadV1(
            source_preview_artifact_id="artifact:preview",
            config_artifact_id="artifact:config",
            constraint_snapshot_artifact_id="artifact:constraints",
            derivation_profile=_profile("derive"),
            environment_profile=_profile("environment"),
            completion_oracle_registry_ref=CompletionOracleRegistryRefV1(
                registry_version=1, digest=_HASH_A
            ),
        ),
        PatchValidationPayloadV1(
            subject=_subject(),
            base_snapshot_artifact_id="artifact:base",
            preview_snapshot_artifact_id="artifact:preview",
            candidate_config_export_artifact_ids=("artifact:config",),
            target=_target(),
            validation_policy=_profile("validation"),
            checker_profiles=(_profile("checker"),),
            simulation_profiles=(),
            findings=(_finding(),),
            review_artifact_ids=("artifact:review",),
            playtest_trace_artifact_ids=("artifact:playtest",),
            regression_suite_artifact_ids=("artifact:regression",),
        ),
        ConstraintValidationPayloadV1(
            subject=_subject(),
            target=_target(),
            dsl_grammar_version="dsl@1",
            compiler_profile=_profile("compiler"),
            differential_engines=(
                SolverEngineRefV1(engine_id="clingo", version=1),
                SolverEngineRefV1(engine_id="z3", version=1),
            ),
            regression_suite_artifact_ids=(),
            validation_policy=_profile("validation"),
        ),
        RollbackValidationPayloadV1(
            subject=_subject(),
            ref_name="refs/content/current",
            expected_current_ref=RefValue(artifact_id="artifact:current", revision=3),
            target_artifact_id="artifact:historical",
            target_history_revision=1,
            rollback_profile=_profile("rollback"),
            schema_compatibility_policy=_profile("schema-compat"),
            impact_profiles=(),
            regression_suite_artifact_ids=(),
        ),
        BenchRunPayloadV1(
            dataset_artifact_id="artifact:dataset",
            benchmark_spec_artifact_id="artifact:benchmark-spec",
            partition_ids=("deterministic",),
            evaluator_profile=_profile("bench"),
            repetition_count=1,
            execution_scope="execute_cases",
            case_result_artifact_ids=(),
        ),
        ArtifactMigrationPayloadV1(
            source_artifact_id="artifact:legacy",
            target_payload_schema_id="ir-snapshot@2",
            target_meta_schema_version="meta@2",
            migrator=_profile("migrator"),
            publish_mode="report_only",
        ),
        DrDrillPayloadV1(
            dr_plan=_profile("dr-plan"),
            recovery_catalog_entry_id="recovery:1",
            expected_checkpoint_id="checkpoint:1",
            restore_target_profile=_profile("restore"),
            verification_profile=_profile("verify"),
            destroy_restored_target_after_verification=True,
        ),
    )


def _payload() -> RunPayloadEnvelope:
    params = CheckerRunPayloadV1(
        snapshot_artifact_id="artifact:input",
        selection=GraphSelectionV1(mode="full", entity_ids=(), relation_ids=()),
        checker_profile=ProfileRefV1(profile_id="checker", version=1),
        checker_ids=(),
        defect_classes=(),
    )
    return RunPayloadEnvelope(
        payload_schema_version="checker-run@1",
        input_artifact_ids=("artifact:input",),
        version_tuple=VersionTuple(ir_snapshot_id="snapshot:1", tool_version="checker@1"),
        execution_version_plan=None,
        policy_bindings=(),
        schema_bindings=(),
        execution_profile_catalog_version=1,
        execution_profile_catalog_digest=_HASH_A,
        resolved_profiles=(),
        resolved_policy_snapshots=(),
        budget_set_snapshot_id="budget-set:1",
        llm_execution_mode="not_applicable",
        params=params,
    )


def _execution_plan() -> ExecutionVersionPlanV1:
    node = PlannedAgentNodeVersionV1(
        agent_node_id="checker",
        prompt_version="checker@1",
        tool_version="checker-tool@1",
        allowed_model_snapshots=("model:a",),
    )
    raw_plan = {
        "plan_schema_version": "execution-version-plan@1",
        "agent_graph_version": "graph@1",
        "nodes": (node,),
        "model_catalog_version": 1,
        "model_catalog_digest": _HASH_A,
        "routing_policy_version": 1,
        "routing_policy_digest": _HASH_B,
    }
    return ExecutionVersionPlanV1(
        **raw_plan,
        plan_digest=execution_version_plan_digest(raw_plan),
    )


def _payload_for_mode(
    mode: str,
    *,
    cassette_artifact_id: str | None = None,
) -> RunPayloadEnvelope:
    base = _payload().model_dump(mode="json")
    if mode == "not_applicable":
        return RunPayloadEnvelope.model_validate(base)
    base["llm_execution_mode"] = mode
    base["execution_version_plan"] = _execution_plan().model_dump(mode="json")
    if mode == "replay":
        assert cassette_artifact_id is not None
        base["cassette_artifact_id"] = cassette_artifact_id
        base["input_artifact_ids"] = ("artifact:input", cassette_artifact_id)
    return RunPayloadEnvelope.model_validate(base)


def _run_record_fields(payload: RunPayloadEnvelope) -> dict[str, object]:
    return {
        "run_schema_version": "run@1",
        "run_id": "run:1",
        "kind": RunKindRef(kind="checker.run", version=1),
        "status": "queued",
        "revision": 1,
        "idempotency_scope": "principal:human:a",
        "idempotency_key": "request:1",
        "request_hash": _HASH_A,
        "payload": payload,
        "payload_hash": canonical_payload_hash(payload),
        "run_kind_definition_digest": _HASH_A,
        "outcome_policy_set_digest": _HASH_B,
        "failure_classifier": _classifier_ref(),
        "initiated_by": AuditActor(principal_id="human:a", principal_kind="human"),
        "queue_deadline_utc": "2026-07-13T12:10:00Z",
        "attempt_timeout_ns": 1_000_000_000,
        "overall_deadline_utc": "2026-07-13T13:00:00Z",
        "next_attempt_no": 1,
        "next_fencing_token": 1,
        "next_event_seq": 2,
        "budget_set_snapshot_id": "budget-set:1",
        "run_budget_hold_group_id": "hold:1",
        "retry_policy": _retry_ref(),
        "max_attempts": 3,
        "created_at": "2026-07-13T12:00:00Z",
        "updated_at": "2026-07-13T12:00:00Z",
    }


def test_failure_classifier_digest_and_rule_identity_are_closed() -> None:
    rule = FailureClassificationRuleV1(
        cause_code="provider_timeout",
        failure_class="transient_dependency",
        intrinsic_retry_eligible=True,
        dependency_required=True,
        allowed_dependency_kinds=("model_provider",),
    )
    payload = {
        "classifier_schema_version": "failure-classifier@1",
        "classifier_version": 1,
        "rules": (rule,),
    }
    classifier = FailureClassifierV1(
        **payload,
        classifier_digest=failure_classifier_digest(payload),
    )
    assert classifier.rules == (rule,)
    with pytest.raises(ValidationError):
        FailureClassifierV1(
            **payload,
            classifier_digest=_HASH_C,
        )


def test_execution_mode_requires_exact_plan_and_cassette_shape() -> None:
    payload = _payload()
    with pytest.raises(ValidationError):
        RunPayloadEnvelope.model_validate({**payload.model_dump(), "llm_execution_mode": "replay"})
    with pytest.raises(ValidationError):
        RunPayloadEnvelope.model_validate(
            {
                **payload.model_dump(),
                "llm_execution_mode": "not_applicable",
                "cassette_artifact_id": "cassette:1",
            }
        )
    node = PlannedAgentNodeVersionV1(
        agent_node_id="review",
        prompt_version="review@1",
        tool_version="review-tool@1",
        allowed_model_snapshots=("model:a",),
    )
    with pytest.raises(ValidationError):
        RunPayloadEnvelope.model_validate(
            {
                **payload.model_dump(),
                "llm_execution_mode": "live",
                "execution_version_plan": {
                    "plan_schema_version": "execution-version-plan@1",
                    "agent_graph_version": "graph@1",
                    "nodes": (node.model_dump(),),
                    "model_catalog_version": 1,
                    "model_catalog_digest": _HASH_A,
                    "routing_policy_version": 1,
                    "routing_policy_digest": _HASH_B,
                    "plan_digest": _HASH_C,
                },
            }
        )


def test_jobs_reexports_the_execution_profile_run_kind_ref() -> None:
    assert RunKindRef is ProfileRunKindRef


def test_run_record_budget_projection_matches_payload_and_carrier_is_noncanonical() -> None:
    payload = _payload()
    common = _run_record_fields(payload)
    without_carrier = RunRecord(**common)
    with_carrier = RunRecord(
        **common,
        dispatch_trace_carrier=RunDispatchTraceCarrierV1(
            carrier_schema_version="run-dispatch-trace@1",
            traceparent="00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        ),
    )
    assert without_carrier.payload_hash == with_carrier.payload_hash
    assert RunRecord.model_validate(with_carrier.model_dump(mode="json")) == with_carrier

    with pytest.raises(ValidationError):
        RunRecord(**{**common, "budget_set_snapshot_id": "budget-set:other"})
    with pytest.raises(ValidationError):
        RunRecord(
            **{
                **common,
                "kind": RunKindRef(kind="review.run", version=1),
            }
        )


@pytest.mark.parametrize("status", ("queued", "leased", "running", "retry_wait"))
def test_nonterminal_run_forbids_terminal_cassette_projection(status: str) -> None:
    payload = _payload_for_mode("record")
    raw = {
        **_run_record_fields(payload),
        "status": status,
        "terminal_cassette_artifact_id": "artifact:cassette-bundle",
    }
    if status == "retry_wait":
        raw["retry_not_before_utc"] = "2026-07-13T12:02:00Z"
    with pytest.raises(ValidationError, match="non-terminal Run cannot publish a cassette"):
        RunRecord.model_validate(raw)


@pytest.mark.parametrize("status", ("succeeded", "failed", "cancelled", "timed_out"))
def test_terminal_record_run_requires_a_published_cassette(status: str) -> None:
    payload = _payload_for_mode("record")
    raw = {**_run_record_fields(payload), "status": status}
    if status == "succeeded":
        raw["result_artifact_id"] = "artifact:result"
    else:
        raw["failure_artifact_id"] = "artifact:failure"

    with pytest.raises(ValidationError, match="record Run requires a terminal cassette"):
        RunRecord.model_validate(raw)

    record = RunRecord.model_validate(
        {**raw, "terminal_cassette_artifact_id": "artifact:cassette-bundle"}
    )
    assert record.terminal_cassette_artifact_id == "artifact:cassette-bundle"


def test_terminal_replay_run_projects_its_exact_input_cassette() -> None:
    payload = _payload_for_mode("replay", cassette_artifact_id="artifact:cassette-input")
    raw = {
        **_run_record_fields(payload),
        "status": "succeeded",
        "result_artifact_id": "artifact:result",
    }
    with pytest.raises(ValidationError, match="replay Run requires its exact input cassette"):
        RunRecord.model_validate(raw)
    with pytest.raises(ValidationError, match="replay Run requires its exact input cassette"):
        RunRecord.model_validate(
            {**raw, "terminal_cassette_artifact_id": "artifact:other-cassette"}
        )

    record = RunRecord.model_validate(
        {**raw, "terminal_cassette_artifact_id": "artifact:cassette-input"}
    )
    assert record.terminal_cassette_artifact_id == payload.cassette_artifact_id


@pytest.mark.parametrize("mode", ("live", "not_applicable"))
def test_terminal_nonrecording_run_forbids_a_cassette_projection(mode: str) -> None:
    payload = _payload_for_mode(mode)
    raw = {
        **_run_record_fields(payload),
        "status": "succeeded",
        "result_artifact_id": "artifact:result",
    }
    assert RunRecord.model_validate(raw).terminal_cassette_artifact_id is None
    with pytest.raises(ValidationError, match="does not publish a terminal cassette"):
        RunRecord.model_validate({**raw, "terminal_cassette_artifact_id": "artifact:cassette"})


def test_attempt_terminal_failure_projection_is_all_or_none() -> None:
    attempt = RunAttempt(
        run_id="run:1",
        attempt_no=1,
        status="failed",
        fencing_token=1,
        worker_principal_id="service:worker",
        next_call_ordinal=1,
        started_at="2026-07-13T12:00:00Z",
        attempt_deadline_utc="2026-07-13T12:01:00Z",
        ended_at="2026-07-13T12:00:30Z",
        failure_class="transient_dependency",
        retryable=True,
        failure_artifact_id="artifact:attempt-failure",
        cassette_bundle_artifact_id="artifact:attempt-cassette",
    )
    assert attempt.failure_artifact_id == "artifact:attempt-failure"
    assert attempt.cassette_bundle_artifact_id == "artifact:attempt-cassette"
    with pytest.raises(ValidationError):
        RunAttempt.model_validate({**attempt.model_dump(), "failure_artifact_id": None})


@pytest.mark.parametrize(
    ("status", "started_at", "attempt_deadline_utc", "message"),
    (
        (
            "leased",
            "2026-07-13T12:00:00Z",
            "2026-07-13T12:01:00Z",
            "leased attempt cannot contain start timing",
        ),
        (
            "running",
            None,
            None,
            "running attempt requires start timing",
        ),
        (
            "running",
            "2026-07-13T12:00:00Z",
            None,
            "attempt start timing fields are all-or-none",
        ),
    ),
)
def test_attempt_status_closes_over_start_timing_projection(
    status: str,
    started_at: str | None,
    attempt_deadline_utc: str | None,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        RunAttempt.model_validate(
            {
                "run_id": "run:1",
                "attempt_no": 1,
                "status": status,
                "fencing_token": 1,
                "worker_principal_id": "service:worker",
                "next_call_ordinal": 1,
                "started_at": started_at,
                "attempt_deadline_utc": attempt_deadline_utc,
            }
        )


@pytest.mark.parametrize("status", ("leased", "running"))
def test_active_attempt_forbids_a_cassette_bundle(status: str) -> None:
    raw = {
        "run_id": "run:1",
        "attempt_no": 1,
        "status": status,
        "fencing_token": 1,
        "worker_principal_id": "service:worker",
        "next_call_ordinal": 1,
        "cassette_bundle_artifact_id": "artifact:attempt-cassette",
    }
    if status == "running":
        raw["started_at"] = "2026-07-13T12:00:00Z"
        raw["attempt_deadline_utc"] = "2026-07-13T12:01:00Z"
    with pytest.raises(ValidationError, match="active attempt cannot publish a cassette bundle"):
        RunAttempt.model_validate(raw)


def test_succeeded_attempt_may_publish_or_omit_a_cassette_bundle() -> None:
    raw = {
        "run_id": "run:1",
        "attempt_no": 1,
        "status": "succeeded",
        "fencing_token": 1,
        "worker_principal_id": "service:worker",
        "next_call_ordinal": 1,
        "started_at": "2026-07-13T12:00:00Z",
        "attempt_deadline_utc": "2026-07-13T12:01:00Z",
        "ended_at": "2026-07-13T12:00:30Z",
    }
    assert RunAttempt.model_validate(raw).cassette_bundle_artifact_id is None
    assert (
        RunAttempt.model_validate(
            {**raw, "cassette_bundle_artifact_id": "artifact:attempt-cassette"}
        ).cassette_bundle_artifact_id
        == "artifact:attempt-cassette"
    )


def test_run_event_attempt_number_is_null_or_positive() -> None:
    event = RunEvent(
        event_schema_version="run-event@1",
        run_id="run:1",
        seq=1,
        event_type="run.queued",
        attempt_no=None,
        occurred_at="2026-07-13T12:00:00Z",
        data_schema_version="run-queued@1",
        data=RunQueuedDataV1(
            run_kind=RunKindRef(kind="checker.run", version=1),
            queue_deadline_utc="2026-07-13T12:10:00Z",
            overall_deadline_utc="2026-07-13T13:00:00Z",
        ),
    )
    assert event.attempt_no is None
    with pytest.raises(ValidationError):
        RunEvent.model_validate({**event.model_dump(), "attempt_no": 0})


def test_run_kind_payload_union_round_trips_and_rejects_unknown_discriminators() -> None:
    payload = _payload()
    assert RunPayloadEnvelope.model_validate(payload.model_dump(mode="json")) == payload
    assert isinstance(payload.params, CheckerRunPayloadV1)

    malformed = payload.model_dump(mode="json")
    malformed["params"]["schema_version"] = "arbitrary-worker-call@1"
    with pytest.raises(ValidationError):
        RunPayloadEnvelope.model_validate(malformed)

    with pytest.raises(ValidationError):
        RunPayloadEnvelope.model_validate(
            {**payload.model_dump(mode="json"), "payload_schema_version": "review-run@1"}
        )


def test_every_frozen_run_kind_payload_has_a_json_round_trip() -> None:
    adapter = TypeAdapter(RunKindPayload)
    payloads = _all_run_kind_payloads()
    assert len(payloads) == 14
    for payload in payloads:
        encoded = adapter.dump_python(payload, mode="json")
        assert adapter.validate_python(encoded) == payload


def test_run_payload_inputs_exactly_cover_every_referenced_artifact() -> None:
    expected_by_schema = {
        "generation-propose@1": {
            "artifact:base",
            "artifact:constraints",
            "artifact:finding-evidence",
            "artifact:goal",
        },
        "patch-repair@1": {
            "artifact:patch",
            "artifact:base",
            "artifact:preview",
            "artifact:validation",
            "artifact:finding-evidence",
            "artifact:regression",
        },
        "constraint-proposal-propose@1": {"artifact:source", "artifact:goal"},
        "review-run@1": {"artifact:snapshot"},
        "checker-run@1": {"artifact:snapshot"},
        "simulation-run@1": {"artifact:snapshot"},
        "playtest-run@1": {
            "artifact:config",
            "artifact:constraints",
            "artifact:suite",
            "artifact:scenario",
        },
        "task-suite-derive@1": {
            "artifact:preview",
            "artifact:config",
            "artifact:constraints",
        },
        "patch-validation@1": {
            "artifact:subject",
            "artifact:base",
            "artifact:preview",
            "artifact:config",
            "artifact:finding-evidence",
            "artifact:review",
            "artifact:playtest",
            "artifact:regression",
        },
        "constraint-validation@1": {"artifact:subject", "artifact:base"},
        "rollback-validation@1": {
            "artifact:subject",
            "artifact:current",
            "artifact:historical",
        },
        "bench-run@1": {"artifact:dataset", "artifact:benchmark-spec"},
        "artifact-migration@1": {"artifact:legacy"},
        "dr-drill@1": {"artifact:backup-manifest"},
    }
    for params in _all_run_kind_payloads():
        exact_inputs = tuple(sorted(expected_by_schema[params.schema_version]))
        envelope = RunPayloadEnvelope(
            payload_schema_version=params.schema_version,
            input_artifact_ids=exact_inputs,
            version_tuple=VersionTuple(tool_version="runner@1"),
            policy_bindings=(),
            schema_bindings=(),
            execution_profile_catalog_version=1,
            execution_profile_catalog_digest=_HASH_A,
            resolved_profiles=(),
            resolved_policy_snapshots=(),
            budget_set_snapshot_id="budget-set:1",
            llm_execution_mode="not_applicable",
            params=params,
        )
        assert set(envelope.input_artifact_ids) == expected_by_schema[params.schema_version]

        missing_or_extra = tuple(exact_inputs[1:]) if exact_inputs else ("artifact:extra",)
        with pytest.raises(ValidationError, match="input_artifact_ids"):
            RunPayloadEnvelope.model_validate(
                {**envelope.model_dump(mode="json"), "input_artifact_ids": missing_or_extra}
            )
        with pytest.raises(ValidationError, match="input_artifact_ids"):
            RunPayloadEnvelope.model_validate(
                {
                    **envelope.model_dump(mode="json"),
                    "input_artifact_ids": (*exact_inputs, "artifact:extra"),
                }
            )


def test_replay_cassette_is_part_of_the_exact_input_artifact_set() -> None:
    base = _payload()
    node = PlannedAgentNodeVersionV1(
        agent_node_id="checker",
        prompt_version="checker@1",
        tool_version="checker-tool@1",
        allowed_model_snapshots=("model:a",),
    )
    raw_plan = {
        "plan_schema_version": "execution-version-plan@1",
        "agent_graph_version": "graph@1",
        "nodes": (node,),
        "model_catalog_version": 1,
        "model_catalog_digest": _HASH_A,
        "routing_policy_version": 1,
        "routing_policy_digest": _HASH_B,
    }
    plan = ExecutionVersionPlanV1(
        **raw_plan,
        plan_digest=execution_version_plan_digest(raw_plan),
    )
    replay = {
        **base.model_dump(mode="json"),
        "execution_version_plan": plan.model_dump(mode="json"),
        "llm_execution_mode": "replay",
        "cassette_artifact_id": "artifact:cassette",
    }
    with pytest.raises(ValidationError, match="input_artifact_ids"):
        RunPayloadEnvelope.model_validate(replay)

    replay["input_artifact_ids"] = ("artifact:input", "artifact:cassette")
    assert RunPayloadEnvelope.model_validate(replay).cassette_artifact_id == "artifact:cassette"


def test_payload_collections_are_canonical_bounded_and_semantically_closed() -> None:
    checker = CheckerRunPayloadV1(
        snapshot_artifact_id="snapshot:1",
        selection=GraphSelectionV1(
            mode="ids",
            entity_ids=("entity:b", "entity:a", "entity:a"),
            relation_ids=(),
        ),
        checker_profile=ProfileRefV1(profile_id="checker", version=1),
        checker_ids=("reachability", "reachability"),
        defect_classes=("dangling_ref", "cycle", "cycle"),
    )
    assert checker.selection.entity_ids == ("entity:a", "entity:b")
    assert checker.checker_ids == ("reachability",)
    assert checker.defect_classes == ("cycle", "dangling_ref")

    with pytest.raises(ValidationError):
        GraphSelectionV1(mode="full", entity_ids=("entity:a",), relation_ids=())
    with pytest.raises(ValidationError):
        GraphSelectionV1(mode="ids", entity_ids=(), relation_ids=())
    with pytest.raises(ValidationError):
        GraphSelectionV1(
            mode="ids",
            entity_ids=tuple(f"entity:{index}" for index in range(1025)),
            relation_ids=(),
        )
    with pytest.raises(ValidationError):
        JsonCollectionCountBindingV1(
            source="run_payload",
            collection_pointer="/params/bad~2escape",
        )
    with pytest.raises(ValidationError):
        BenchRunPayloadV1(
            dataset_artifact_id="dataset:1",
            benchmark_spec_artifact_id="spec:1",
            partition_ids=("p1",),
            evaluator_profile=ProfileRefV1(profile_id="bench", version=1),
            repetition_count=1,
            execution_scope="execute_cases",
            case_result_artifact_ids=("result:1",),
        )


def test_execution_identity_count_binding_is_discriminated_and_round_trips() -> None:
    adapter = TypeAdapter(ArtifactCountBindingV1)
    binding = ExecutionIdentityCountBindingV1(scope="current_attempt")

    assert adapter.validate_json(adapter.dump_json(binding)) == binding
    mapping = adapter.json_schema()["discriminator"]["mapping"]
    assert "execution_identity" in mapping

    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "source": "execution_identity",
                "response_consumed": False,
                "scope": "current_attempt",
            }
        )


def _event_definitions() -> tuple[RunEventDefinitionV1, ...]:
    return (
        RunEventDefinitionV1(
            event_type="run.queued",
            data_schema_id="run-queued@1",
            attempt_scope="run",
            terminal=False,
            allowed_from_statuses=("create",),
        ),
        RunEventDefinitionV1(
            event_type="run.cancel_requested",
            data_schema_id="cancel-requested@1",
            attempt_scope="run",
            terminal=False,
            allowed_from_statuses=("queued", "leased", "running", "retry_wait"),
        ),
        RunEventDefinitionV1(
            event_type="run.command_accepted",
            data_schema_id="command-accepted@1",
            attempt_scope="run",
            terminal=False,
            allowed_from_statuses=("leased", "running"),
        ),
        RunEventDefinitionV1(
            event_type="attempt.leased",
            data_schema_id="attempt-leased@1",
            attempt_scope="attempt",
            terminal=False,
            allowed_from_statuses=("queued", "retry_wait"),
        ),
        RunEventDefinitionV1(
            event_type="attempt.started",
            data_schema_id="attempt-started@1",
            attempt_scope="attempt",
            terminal=False,
            allowed_from_statuses=("leased",),
        ),
        RunEventDefinitionV1(
            event_type="attempt.progress",
            data_schema_id="attempt-progress@1",
            attempt_scope="attempt",
            terminal=False,
            allowed_from_statuses=("running",),
        ),
        RunEventDefinitionV1(
            event_type="attempt.lease_expired",
            data_schema_id="lease-expired@1",
            attempt_scope="attempt",
            terminal=False,
            allowed_from_statuses=("leased", "running"),
        ),
        RunEventDefinitionV1(
            event_type="attempt.retry_scheduled",
            data_schema_id="retry-scheduled@1",
            attempt_scope="attempt",
            terminal=False,
            allowed_from_statuses=("leased", "running"),
        ),
        RunEventDefinitionV1(
            event_type="run.command_applied",
            data_schema_id="command-outcome@1",
            attempt_scope="either",
            terminal=False,
            allowed_from_statuses=("queued", "leased", "running", "retry_wait"),
        ),
        RunEventDefinitionV1(
            event_type="run.command_rejected",
            data_schema_id="command-outcome@1",
            attempt_scope="either",
            terminal=False,
            allowed_from_statuses=("queued", "leased", "running", "retry_wait"),
        ),
        RunEventDefinitionV1(
            event_type="run.succeeded",
            data_schema_id="run-succeeded@1",
            attempt_scope="attempt",
            terminal=True,
            allowed_from_statuses=("running",),
        ),
        RunEventDefinitionV1(
            event_type="run.failed",
            data_schema_id="run-terminated@1",
            attempt_scope="either",
            terminal=True,
            allowed_from_statuses=("queued", "leased", "running", "retry_wait"),
        ),
        RunEventDefinitionV1(
            event_type="run.cancelled",
            data_schema_id="run-terminated@1",
            attempt_scope="either",
            terminal=True,
            allowed_from_statuses=("queued", "leased", "running", "retry_wait"),
        ),
        RunEventDefinitionV1(
            event_type="run.timed_out",
            data_schema_id="run-terminated@1",
            attempt_scope="either",
            terminal=True,
            allowed_from_statuses=("queued", "leased", "running", "retry_wait"),
        ),
    )


def test_run_event_registry_is_complete_canonical_and_digest_bound() -> None:
    definitions = tuple(reversed(_event_definitions()))
    raw = {
        "registry_schema_version": "run-event-registry@1",
        "registry_version": 1,
        "definitions": definitions,
    }
    registry = RunEventRegistryV1(
        **raw,
        registry_digest=run_event_registry_digest(raw),
    )
    assert registry.definitions[0].event_type == "attempt.lease_expired"
    assert RunEventRegistryV1.model_validate(registry.model_dump(mode="json")) == registry

    with pytest.raises(ValidationError):
        RunEventRegistryV1(
            **{**raw, "definitions": definitions[:-1]},
            registry_digest=run_event_registry_digest({**raw, "definitions": definitions[:-1]}),
        )
    with pytest.raises(ValidationError):
        RunEventRegistryV1(**raw, registry_digest=_HASH_C)


def test_run_event_data_discriminator_scope_and_projection_are_closed() -> None:
    leased = RunEvent(
        run_id="run:1",
        seq=2,
        event_type="attempt.leased",
        attempt_no=1,
        occurred_at="2026-07-13T12:00:00Z",
        data_schema_version="attempt-leased@1",
        data=AttemptLeasedDataV1(
            attempt_no=1,
            lease_expires_at="2026-07-13T12:00:30Z",
        ),
    )
    assert RunEvent.model_validate(leased.model_dump(mode="json")) == leased

    with pytest.raises(ValidationError):
        RunEvent.model_validate({**leased.model_dump(mode="json"), "event_type": "run.queued"})
    with pytest.raises(ValidationError):
        RunEvent.model_validate(
            {
                **leased.model_dump(mode="json"),
                "data": {
                    "data_schema_version": "unknown-event-data@1",
                    "attempt_no": 1,
                },
            }
        )


def _cancel_command() -> RunCommandV1:
    return RunCommandV1(
        command_id="command:1",
        client_id="browser:1",
        client_seq=1,
        idempotency_key="cancel:1",
        expected_run_revision=3,
        type="cancel",
        payload_schema_id="run-cancel@1",
        payload=CancelRunPayloadV1(reason_code="user_requested"),
    )


def test_run_command_union_record_and_ack_are_closed_and_json_shaped() -> None:
    command = _cancel_command()
    assert RunCommandV1.model_validate(command.model_dump(mode="json")) == command
    with pytest.raises(ValidationError):
        RunCommandV1.model_validate({**command.model_dump(mode="json"), "type": "provide_input"})
    with pytest.raises(ValidationError):
        RunCommandV1.model_validate(
            {**command.model_dump(mode="json"), "payload_schema_id": "wrong@1"}
        )

    record = RunCommandRecordV1(
        run_id="run:1",
        command=command,
        request_hash=canonical_payload_hash(command),
        actor=AuditActor(principal_id="human:a", principal_kind="human"),
        status="applied",
        revision=1,
        created_at="2026-07-13T12:00:00Z",
        applied_at="2026-07-13T12:00:01Z",
        result_event_seq=2,
    )
    assert RunCommandRecordV1.model_validate(record.model_dump(mode="json")) == record
    with pytest.raises(ValidationError):
        RunCommandRecordV1.model_validate({**record.model_dump(), "request_hash": _HASH_C})

    ack = RunCommandAckV1(
        command_id=command.command_id,
        client_id=command.client_id,
        client_seq=command.client_seq,
        status="accepted",
        persisted_status="applied",
        command_revision=1,
        run_revision=4,
    )
    assert "claimed_fencing_token" not in ack.model_dump(mode="json")


def test_run_command_record_enforces_type_specific_fencing_lifecycle() -> None:
    provide_input = RunCommandV1(
        command_id="command:input",
        client_id="browser:1",
        client_seq=2,
        idempotency_key="input:1",
        expected_run_revision=3,
        type="provide_input",
        payload_schema_id="playtest-provide-input@1",
        payload=PlaytestProvideInputPayloadV1(
            interaction_id="interaction:1",
            expected_state_hash=_HASH_A,
            choice_id="choice:a",
        ),
    )
    common = {
        "run_id": "run:1",
        "command": provide_input,
        "request_hash": canonical_payload_hash(provide_input),
        "actor": AuditActor(principal_id="human:a", principal_kind="human"),
        "revision": 2,
        "created_at": "2026-07-13T12:00:00Z",
    }
    with pytest.raises(ValidationError, match="claim"):
        RunCommandRecordV1(
            **common,
            status="applied",
            applied_at="2026-07-13T12:00:01Z",
            result_event_seq=3,
        )

    applied = RunCommandRecordV1(
        **common,
        status="applied",
        claimed_at="2026-07-13T12:00:00.100Z",
        claimed_attempt_no=1,
        claimed_fencing_token=7,
        applied_at="2026-07-13T12:00:01Z",
        result_event_seq=3,
    )
    assert applied.claimed_fencing_token == 7

    run_terminal = RunCommandRecordV1(
        **common,
        status="rejected",
        applied_at="2026-07-13T12:00:01Z",
        result_event_seq=3,
        rejection_code="run_terminal",
    )
    assert run_terminal.claimed_fencing_token is None

    with pytest.raises(ValidationError, match="cancel"):
        RunCommandRecordV1(
            run_id="run:1",
            command=_cancel_command(),
            request_hash=canonical_payload_hash(_cancel_command()),
            actor=AuditActor(principal_id="human:a", principal_kind="human"),
            status="claimed",
            revision=2,
            created_at="2026-07-13T12:00:00Z",
            claimed_at="2026-07-13T12:00:00.100Z",
            claimed_attempt_no=1,
            claimed_fencing_token=7,
        )


def test_run_command_view_closes_type_to_payload_schema_projection() -> None:
    common = {
        "run_id": "run:1",
        "command_id": "command:1",
        "client_id": "browser:1",
        "client_seq": 1,
        "type": "cancel",
        "status": "applied",
        "revision": 1,
        "created_at": "2026-07-13T12:00:00Z",
        "applied_at": "2026-07-13T12:00:01Z",
        "result_event_seq": 2,
    }
    assert RunCommandViewV1(**common, payload_schema_id="run-cancel@1").type == "cancel"
    with pytest.raises(ValidationError, match="payload schema"):
        RunCommandViewV1(**common, payload_schema_id="playtest-provide-input@1")


def test_retry_decision_reasons_partition_retry_from_terminal() -> None:
    common = {
        "cause_code": "provider_timeout",
        "failure_class": "transient_dependency",
        "classifier": _classifier_ref(),
        "retry_policy": _retry_ref(),
        "evaluated_at_utc": "2026-07-13T12:00:00Z",
    }
    with pytest.raises(ValidationError):
        RetryDecisionV1(
            **common,
            intrinsic_retry_eligible=True,
            decision="terminal",
            reason_code="retry_after",
        )
    with pytest.raises(ValidationError):
        RetryDecisionV1(
            **common,
            intrinsic_retry_eligible=False,
            decision="terminal",
            reason_code="budget_exhausted",
        )
    terminal = RetryDecisionV1(
        **common,
        intrinsic_retry_eligible=False,
        decision="terminal",
        reason_code="not_retry_eligible",
    )
    assert terminal.retry_not_before_utc is None
    deadline_terminal = RetryDecisionV1(
        **common,
        intrinsic_retry_eligible=False,
        decision="terminal",
        reason_code="attempt_deadline_exhausted",
    )
    assert deadline_terminal.retry_not_before_utc is None


def test_manifest_and_result_cross_fields_are_exact() -> None:
    vt = VersionTuple(ir_snapshot_id="snapshot:1", tool_version="checker@1")
    projection = RunManifestVersionProjectionV1(
        projection_schema_version="run-manifest-version-projection@1",
        manifest_scope="run",
        attempt_no=1,
        run_kind=RunKindRef(kind="checker.run", version=1),
        run_payload_hash=_HASH_A,
        frozen_input_version_tuple=vt,
        terminal_version_tuple=vt,
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
    )
    summary = RunResultSummaryV1(
        summary_schema_version="run-result-summary@1",
        outcome_code="passed",
        primary_artifact_kind="checker_run",
        produced_artifact_count=1,
        finding_count=0,
    )
    result = RunResultV1(
        result_schema_version="run-result@1",
        run_id="run:1",
        attempt_no=1,
        run_kind=RunKindRef(kind="checker.run", version=1),
        primary_artifact_id="artifact:output",
        produced_artifact_ids=("artifact:output",),
        finding_count=0,
        outcome_code="passed",
        summary=summary,
        requirement_dispositions=(),
        version_projection=projection,
    )
    assert result.primary_artifact_id in result.produced_artifact_ids
    with pytest.raises(ValidationError):
        RunResultV1.model_validate({**result.model_dump(), "primary_artifact_id": "artifact:other"})


def test_prepared_result_primary_index_and_counts_match() -> None:
    artifact = PreparedArtifact(
        kind="checker_run",
        payload_schema_id="checker-run@1",
        version_tuple=VersionTuple(ir_snapshot_id="snapshot:1", tool_version="checker@1"),
        lineage=("artifact:input",),
        payload_hash=_HASH_A,
        meta={},
        object_ref=ObjectRef(
            object_ref_schema_version="object-ref@1",
            key=f"objects/v1/sha256/{_HASH_A[:2]}/{_HASH_A}",
            sha256=_HASH_A,
            size_bytes=1,
        ),
        location=ObjectLocation(
            location_schema_version="object-location@1",
            store_id="local",
            key=f"objects/v1/sha256/{_HASH_A[:2]}/{_HASH_A}",
            backend_generation="g1",
        ),
    )
    summary = PreparedRunResultSummaryV1(
        summary_schema_version="prepared-run-result-summary@1",
        outcome_code="passed",
        primary_artifact_kind="checker_run",
        prepared_domain_artifact_count=1,
        prepared_finding_count=0,
    )
    prepared = PreparedRunResult(
        prepared_schema_version="prepared-run-result@1",
        run_id="run:1",
        attempt_no=1,
        run_kind=RunKindRef(kind="checker.run", version=1),
        primary_index=0,
        artifacts=(artifact,),
        findings=(),
        requirement_dispositions=(
            RequirementDispositionV1(
                resolved_policy_id="checker",
                outcome_rule_id="checker-output",
                requirement_id="req:1",
                status="not_executed",
                reason_code="not_applicable",
            ),
        ),
        summary=summary,
    )
    assert prepared.artifacts[prepared.primary_index].kind == summary.primary_artifact_kind
    with pytest.raises(ValidationError):
        PreparedRunResult.model_validate({**prepared.model_dump(), "primary_index": 1})


def test_retry_decision_dependency_shape_is_typed() -> None:
    dependency = DependencyFailureV1(
        dependency_schema_version="dependency-failure@1",
        dependency_kind="model_provider",
        dependency_id="provider:1",
        operation_code="responses.create",
        classifier_code="provider_timeout",
        retry_after_ms=100,
    )
    decision = RetryDecisionV1(
        decision_schema_version="retry-decision@1",
        cause_code="provider_timeout",
        failure_class="transient_dependency",
        intrinsic_retry_eligible=True,
        decision="retry",
        reason_code="retry_after",
        retry_not_before_utc="2026-07-13T12:00:00.100Z",
        classifier=_classifier_ref(),
        retry_policy=_retry_ref(),
        evaluated_at_utc="2026-07-13T12:00:00Z",
    )
    assert dependency.retry_after_ms == 100
    assert decision.retry_not_before_utc is not None
