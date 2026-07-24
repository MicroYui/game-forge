from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from gameforge.contracts.api import (
    ExecutionOptionResolveRequestV1,
    ProspectiveConstraintProposeRequestV1,
    ProspectiveGenerationProposeRequestV1,
    ProspectiveGenericAgentRunRequestV1,
    ProspectivePatchRepairRequestV1,
    ProspectivePlaytestRunRequestV1,
)
from gameforge.contracts.execution_profiles import ProfileRefV1, canonical_config_hash
from gameforge.contracts.errors import (
    DependencyUnavailable,
    Forbidden,
    IntegrityViolation,
    WorkflowGuard,
)
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.jobs import (
    BenchRunPayloadV1,
    GraphSelectionV1,
    PatchRepairPayloadV1,
    PlaytestEpisodeBindingV1,
    RefReadBindingV1,
)
from gameforge.contracts.lineage import VersionTuple
from gameforge.platform.runs.execution_plan import ExecutionVersionPlanResolver
from gameforge.runtime.cost.ledger import SqlCostLedger
from gameforge.runtime.persistence.models import (
    RunAttemptRow,
    RunRow,
)
from tests.platform.m4c import test_run_admission as kit


def _enable_execution_options(harness: kit.Harness) -> None:
    _catalog, routing = kit._model_authorities()

    @contextmanager
    def authority_scope():
        with Session(harness.engine) as session:
            yield SqlCostLedger(session, clock=harness.clock)

    harness.engine_admission._execution_version_plans = ExecutionVersionPlanResolver(  # noqa: SLF001
        authority_scope=authority_scope,
        routing_policy_version=routing.policy_version,
        routing_policy_digest=routing.routing_policy_digest,
    )


def _resolve_request(
    *,
    operation_id: str,
    run_kind: str,
    prospective_request: dict[str, object],
    replay_source_run_id: str | None = None,
) -> ExecutionOptionResolveRequestV1:
    return ExecutionOptionResolveRequestV1.model_validate(
        {
            "request_schema_version": "execution-option-resolve-request@1",
            "resource_operation_id": operation_id,
            "run_kind": {"kind": run_kind, "version": 1},
            "llm_execution_mode": prospective_request["llm_execution_mode"],
            "prospective_request": prospective_request,
            "replay_source_run_id": replay_source_run_id,
        }
    )


def _generation_request(
    harness: kit.Harness,
    *,
    mode: str,
    base_artifact_id: str | None = None,
    replay_source_run_id: str | None = None,
) -> tuple[ExecutionOptionResolveRequestV1, str]:
    economy = DomainScope(domain_ids=("economy",))
    base = base_artifact_id or harness.seed_artifact(
        kind="ir_snapshot",
        tool_version="generation-option-base@1",
        domain_scope=economy,
    )
    return (
        _resolve_request(
            operation_id="propose_generation_api_v1_generation_propose_post",
            run_kind="generation.propose",
            prospective_request={
                "request_schema_version": "generation-propose-request@1",
                "base_snapshot_artifact_id": base,
                "constraint_snapshot_artifact_id": None,
                "findings": [],
                "objective_goal_text": "Propose an exact bounded economy adjustment.",
                "domain_scope": economy.model_dump(mode="json"),
                "target": {"ref_name": "content/head", "expected_ref": None},
                "generation_policy": kit.GENERATION_PROFILE.model_dump(mode="json"),
                "candidate_export_profiles": [],
                "llm_execution_mode": mode,
                "execution_version_plan": None,
                "cassette_artifact_id": None,
            },
            replay_source_run_id=replay_source_run_id,
        ),
        base,
    )


def _database_projection(
    harness: kit.Harness,
) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    table_names = tuple(sorted(inspect(harness.engine).get_table_names()))
    with harness.engine.connect() as connection:
        return tuple(
            (
                table_name,
                tuple(
                    tuple(row)
                    for row in connection.exec_driver_sql(
                        f'SELECT * FROM "{table_name}"'  # noqa: S608 - inspected table name
                    )
                ),
            )
            for table_name in table_names
        )


def _admit_resolved_record(
    harness: kit.Harness,
    *,
    request: ExecutionOptionResolveRequestV1,
    plan,
    actor,
    key: str,
):
    prospective = request.prospective_request
    server = kit._server(key)
    if isinstance(prospective, ProspectiveGenericAgentRunRequestV1):
        return harness.engine_admission.admit_generic_run(
            params=prospective.params,
            actor=actor,
            server=server,
            llm_execution_mode="record",
            seed=prospective.seed,
            execution_version_plan=plan,
        )
    if isinstance(prospective, ProspectivePatchRepairRequestV1):
        return harness.engine_admission.admit_resource_run(
            params=prospective.params,
            actor=actor,
            server=server,
            llm_execution_mode="record",
            seed=prospective.seed,
            execution_version_plan=plan,
        )
    if isinstance(prospective, ProspectivePlaytestRunRequestV1):
        return harness.engine_admission.admit_resource_run(
            params=prospective.params,
            actor=actor,
            server=server,
            llm_execution_mode="record",
            seed=prospective.seed,
            execution_version_plan=plan,
        )
    if isinstance(prospective, ProspectiveGenerationProposeRequestV1):
        return harness.engine_admission.admit_generation(
            base_snapshot_artifact_id=prospective.base_snapshot_artifact_id,
            constraint_snapshot_artifact_id=prospective.constraint_snapshot_artifact_id,
            findings=prospective.findings,
            objective_goal_text=prospective.objective_goal_text,
            domain_scope=prospective.domain_scope,
            target=prospective.target,
            generation_policy=prospective.generation_policy,
            candidate_export_profiles=prospective.candidate_export_profiles,
            actor=actor,
            server=server,
            llm_execution_mode="record",
            execution_version_plan=plan,
        )
    if isinstance(prospective, ProspectiveConstraintProposeRequestV1):
        return harness.engine_admission.admit_constraint_proposal(
            source_artifact_ids=prospective.source_artifact_ids,
            base_constraint_snapshot_artifact_id=(prospective.base_constraint_snapshot_artifact_id),
            authoring_goal_text=prospective.authoring_goal_text,
            domain_scope=prospective.domain_scope,
            dsl_grammar_version=prospective.dsl_grammar_version,
            extraction_policy=prospective.extraction_policy,
            actor=actor,
            server=server,
            llm_execution_mode="record",
            execution_version_plan=plan,
        )
    raise AssertionError("unhandled prospective request")


def _promote_record_source_to_terminal(
    harness: kit.Harness,
    *,
    run_id: str,
) -> str:
    source = harness.run_record(run_id)
    assert source is not None
    plan = source.payload.execution_version_plan
    assert plan is not None

    def seed_bundle(bundle, *, lineage: tuple[str, ...], scope: str):
        identity = kit.build_execution_identity(
            scope=scope,
            bindings=(),
            agent_graph_version=plan.agent_graph_version,
        )
        blob = kit.canonical_json(bundle.model_dump(mode="json")).encode("utf-8")
        digest = kit.sha256_lowerhex(blob)
        return harness.seed_payload_artifact(
            kind="cassette_bundle",
            payload=blob,
            version_tuple=VersionTuple(
                prompt_version=identity.prompt_projection.tuple_value,
                model_snapshot=identity.model_projection.tuple_value,
                agent_graph_version=identity.agent_graph_version,
                tool_version="cassette-bundle@1",
                cassette_id=f"sha256:{digest}",
            ),
            lineage=lineage,
            payload_schema_id="cassette-bundle@1",
            meta_extra={"execution_identity": identity.model_dump(mode="json")},
        )

    attempt = seed_bundle(
        kit.CassetteBundleV1(scope="attempt", run_id=source.run_id, attempt_no=1),
        lineage=(),
        scope="attempt",
    )
    root = seed_bundle(
        kit.CassetteBundleV1(
            scope="run",
            run_id=source.run_id,
            child_bundle_artifact_ids=(attempt.artifact_id,),
            outcome_code="completed",
        ),
        lineage=(attempt.artifact_id,),
        scope="run",
    )
    terminal_tuple = source.payload.version_tuple.model_copy(
        update={"cassette_id": root.version_tuple.cassette_id}
    )
    primary = harness.seed_payload_artifact(
        kind="review_report",
        payload={"status": "completed"},
        version_tuple=terminal_tuple,
        payload_schema_id="review@1",
        domain_scope=source.resource_domain_scope,
    )
    projection = kit.RunManifestVersionProjectionV1(
        manifest_scope="run",
        attempt_no=1,
        run_kind=source.kind,
        run_payload_hash=source.payload_hash,
        frozen_input_version_tuple=source.payload.version_tuple,
        terminal_version_tuple=terminal_tuple,
        version_transition_policy_ref=kit.VersionTransitionPolicyRefV1(
            policy_id="run-manifest-transition",
            policy_version=1,
            digest="a" * 64,
        ),
        parents=(
            kit.RunManifestParentBindingV1(
                artifact_id=primary.artifact_id,
                role="output",
                publication="run_published",
            ),
            kit.RunManifestParentBindingV1(
                artifact_id=root.artifact_id,
                role="intermediate",
                publication="run_published",
                cassette_scope="run_bundle",
            ),
        ),
    )
    result_payload = kit.RunResultV1(
        run_id=source.run_id,
        attempt_no=1,
        run_kind=source.kind,
        primary_artifact_id=primary.artifact_id,
        produced_artifact_ids=(primary.artifact_id, root.artifact_id),
        finding_count=0,
        outcome_code="completed",
        summary=kit.RunResultSummaryV1(
            outcome_code="completed",
            primary_artifact_kind="review_report",
            produced_artifact_count=2,
            finding_count=0,
        ),
        requirement_dispositions=(),
        version_projection=projection,
    )
    result = harness.seed_payload_artifact(
        kind="run_result",
        payload=result_payload.model_dump(mode="json"),
        version_tuple=terminal_tuple,
        lineage=tuple(sorted((primary.artifact_id, root.artifact_id))),
        payload_schema_id="run-result@1",
    )
    with Session(harness.engine) as session, session.begin():
        row = session.get(RunRow, source.run_id)
        assert row is not None
        row.status = "succeeded"
        row.revision = 8
        row.current_attempt_no = 1
        row.next_attempt_no = 2
        row.next_fencing_token = 2
        row.next_event_seq = 2
        row.result_artifact_id = result.artifact_id
        row.terminal_cassette_artifact_id = root.artifact_id
        row.updated_at = kit.NOW
        session.add(
            RunAttemptRow(
                run_id=source.run_id,
                attempt_no=1,
                status="succeeded",
                fencing_token=1,
                worker_principal_id="service:worker",
                trace_id=None,
                next_call_ordinal=1,
                started_at=kit.NOW,
                attempt_deadline_utc="2026-07-15T12:30:00Z",
                ended_at=kit.NOW,
                failure_class=None,
                retryable=None,
                failure_artifact_id=None,
                cassette_bundle_artifact_id=attempt.artifact_id,
            )
        )
    return root.artifact_id


def test_all_six_agent_run_kinds_resolve_exact_options_without_writes(tmp_path: Path) -> None:
    harness = kit.Harness(tmp_path)
    _enable_execution_options(harness)
    actor = kit._tooling_actor()
    economy = DomainScope(domain_ids=("economy",))

    generation_base = harness.seed_artifact(
        kind="ir_snapshot",
        tool_version="generation-base@1",
        domain_scope=economy,
    )
    generation = _resolve_request(
        operation_id="propose_generation_api_v1_generation_propose_post",
        run_kind="generation.propose",
        prospective_request={
            "request_schema_version": "generation-propose-request@1",
            "base_snapshot_artifact_id": generation_base,
            "constraint_snapshot_artifact_id": None,
            "findings": [],
            "objective_goal_text": "Propose an exact bounded economy adjustment.",
            "domain_scope": economy.model_dump(mode="json"),
            "target": {"ref_name": "generation/head", "expected_ref": None},
            "generation_policy": kit.GENERATION_PROFILE.model_dump(mode="json"),
            "candidate_export_profiles": [],
            "llm_execution_mode": "record",
            "execution_version_plan": None,
            "cassette_artifact_id": None,
        },
    )

    source_document = harness.seed_payload_artifact(
        kind="source_raw",
        payload={"doc_text": "Rewards must remain non-negative."},
        version_tuple=VersionTuple(doc_version="design@1", tool_version="source@1"),
        payload_schema_id="source-raw@1",
        domain_scope=economy,
    )
    constraint = _resolve_request(
        operation_id="propose_constraint_api_v1_constraint_proposals_propose_post",
        run_kind="constraint_proposal.propose",
        prospective_request={
            "request_schema_version": "constraint-propose-request@1",
            "source_artifact_ids": [source_document.artifact_id],
            "base_constraint_snapshot_artifact_id": None,
            "authoring_goal_text": "Extract deterministic constraints.",
            "domain_scope": economy.model_dump(mode="json"),
            "dsl_grammar_version": "constraint-dsl@1",
            "extraction_policy": {
                "profile_id": "builtin.constraint_extraction",
                "version": 1,
            },
            "llm_execution_mode": "record",
            "execution_version_plan": None,
            "cassette_artifact_id": None,
        },
    )

    review_snapshot = harness.seed_artifact(
        kind="ir_snapshot",
        tool_version="review-snapshot@1",
        domain_scope=economy,
    )
    review = _resolve_request(
        operation_id="submit_run_api_v1_runs_post",
        run_kind="review.run",
        prospective_request={
            "request_schema_version": "run-submission-request@1",
            "params": {
                "schema_version": "review-run@1",
                "snapshot_artifact_id": review_snapshot,
                "constraint_snapshot_artifact_id": None,
                "selection": GraphSelectionV1(
                    mode="full", entity_ids=(), relation_ids=()
                ).model_dump(mode="json"),
                "review_profile": kit.REVIEW_PROFILE.model_dump(mode="json"),
                "checker_profiles": [],
                "simulation_profiles": [],
                "llm_triage_policy": kit.LLM_TRIAGE_PROFILE.model_dump(mode="json"),
            },
            "llm_execution_mode": "record",
            "seed": None,
            "execution_version_plan": None,
            "cassette_artifact_id": None,
        },
    )

    subject, base, preview, evidence, base_ref, _item = kit._seed_failed_repair_case(harness)
    regression = harness.seed_artifact(
        kind="regression_suite",
        tool_version="repair-regression@1",
        domain_scope=economy,
    )
    repair_params = PatchRepairPayloadV1(
        subject_patch_artifact_id=subject,
        expected_subject_head_revision=1,
        expected_workflow_revision=2,
        base_snapshot_artifact_id=base,
        preview_snapshot_artifact_id=preview,
        validation_evidence_artifact_id=evidence,
        findings=(),
        target=RefReadBindingV1(ref_name="content/head", expected_ref=base_ref),
        repair_policy=ProfileRefV1(profile_id="builtin.patch_repair", version=1),
        checker_profiles=(kit.CHECKER_PROFILE,),
        simulation_profiles=(),
        regression_suite_artifact_ids=(regression,),
        candidate_export_profiles=(),
    )
    repair = _resolve_request(
        operation_id="repair_patch_api_v1_patches__artifact_id__repair_post",
        run_kind="patch.repair",
        prospective_request={
            "request_schema_version": "patch-repair-request@1",
            "params": repair_params.model_dump(mode="json"),
            "llm_execution_mode": "record",
            "seed": 7,
            "execution_version_plan": None,
            "cassette_artifact_id": None,
        },
    )

    playtest_constraint = kit._seed_constraint(harness)
    playtest_preview = kit._seed_preview(harness, label="option-playtest")
    playtest_config = kit._seed_config(
        harness,
        label="option-playtest",
        preview=playtest_preview,
        constraint=playtest_constraint,
    )
    suite, scenario, episode = kit._seed_task_suite(
        harness,
        preview=playtest_preview,
        config=playtest_config,
        constraint=playtest_constraint,
    )
    playtest = _resolve_request(
        operation_id="run_playtest_api_v1_playtest_run_post",
        run_kind="playtest.run",
        prospective_request={
            "request_schema_version": "playtest-run-request@1",
            "params": {
                "schema_version": "playtest-run@1",
                "config_artifact_id": playtest_config.artifact_id,
                "constraint_snapshot_artifact_id": playtest_constraint.artifact_id,
                "task_suite_artifact_id": suite.artifact_id,
                "episodes": [
                    PlaytestEpisodeBindingV1(
                        episode_id=episode.episode_id,
                        scenario_spec_artifact_id=scenario.artifact_id,
                    ).model_dump(mode="json")
                ],
                "environment_profile": kit.ENVIRONMENT_PROFILE.model_dump(mode="json"),
                "planner_policy": kit.PLAYTEST_PLANNER_PROFILE.model_dump(mode="json"),
                "max_steps_per_episode": 16,
                "interaction_mode": "autonomous",
            },
            "llm_execution_mode": "record",
            "seed": 7,
            "execution_version_plan": None,
            "cassette_artifact_id": None,
        },
    )

    bench_dataset = harness.seed_artifact(
        kind="bench_dataset",
        tool_version="option-dataset@1",
        domain_scope=economy,
    )
    bench_spec = kit._seed_benchmark_spec(
        harness,
        dataset_artifact_id=bench_dataset,
        domain_scope=economy,
        partitions=(kit._benchmark_partition("agent", ("case:agent", "agent")),),
    )
    bench_params = BenchRunPayloadV1(
        dataset_artifact_id=bench_spec.lineage[0],
        benchmark_spec_artifact_id=bench_spec.artifact_id,
        partition_ids=("agent",),
        evaluator_profile=kit.BENCH_EVALUATOR_PROFILE,
        repetition_count=1,
        execution_scope="execute_cases",
        case_result_artifact_ids=(),
    )
    bench = _resolve_request(
        operation_id="submit_run_api_v1_runs_post",
        run_kind="bench.run",
        prospective_request={
            "request_schema_version": "run-submission-request@1",
            "params": bench_params.model_dump(mode="json"),
            "llm_execution_mode": "record",
            "seed": None,
            "execution_version_plan": None,
            "cassette_artifact_id": None,
        },
    )

    requests = (generation, constraint, review, repair, playtest, bench)
    before_database = _database_projection(harness)
    before_objects = tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")))
    options = tuple(
        harness.engine_admission.resolve_execution_option(request=item, actor=actor)
        for item in requests
    )

    assert {option.run_kind.kind for option in options} == {
        "generation.propose",
        "constraint_proposal.propose",
        "review.run",
        "patch.repair",
        "playtest.run",
        "bench.run",
    }
    domains = {option.run_kind.kind: option.domain_scope for option in options}
    assert domains == {
        "generation.propose": economy,
        "constraint_proposal.propose": economy,
        "review.run": economy,
        "patch.repair": economy,
        "playtest.run": DomainScope(domain_ids=("builtin",)),
        "bench.run": economy,
    }
    assert all(
        option.prospective_request_hash != option.resolved_request_hash for option in options
    )
    assert all(option.resolved_profile_binding_digests for option in options)
    assert all(option.source_run_id is None for option in options)
    assert all(option.cassette_artifact_id is None for option in options)
    assert "Propose an exact bounded economy adjustment" not in options[0].model_dump_json()
    assert "profile_payload_hash" not in options[0].model_dump_json()
    assert _database_projection(harness) == before_database
    assert tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))) == (
        before_objects
    )

    sources = tuple(
        _admit_resolved_record(
            harness,
            request=request,
            plan=option.execution_version_plan,
            actor=actor,
            key=f"execution-option:record:{index}",
        )
        for index, (request, option) in enumerate(zip(requests, options, strict=True), start=1)
    )
    cassette_ids = tuple(
        _promote_record_source_to_terminal(harness, run_id=source.run_id) for source in sources
    )
    replay_requests = tuple(
        ExecutionOptionResolveRequestV1.model_validate(
            {
                **request.model_dump(mode="json"),
                "llm_execution_mode": "replay",
                "prospective_request": {
                    **request.prospective_request.model_dump(mode="json"),
                    "llm_execution_mode": "replay",
                },
                "replay_source_run_id": source.run_id,
            }
        )
        for request, source in zip(requests, sources, strict=True)
    )
    before_replay_resolution = _database_projection(harness)
    before_replay_objects = tuple(
        sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    )

    replay_options = tuple(
        harness.engine_admission.resolve_execution_option(request=request, actor=actor)
        for request in replay_requests
    )

    assert tuple(option.source_run_id for option in replay_options) == tuple(
        source.run_id for source in sources
    )
    assert tuple(option.cassette_artifact_id for option in replay_options) == cassette_ids
    assert tuple(option.execution_version_plan for option in replay_options) == tuple(
        option.execution_version_plan for option in options
    )
    assert _database_projection(harness) == before_replay_resolution
    assert tuple(sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))) == (
        before_replay_objects
    )

    generation_replay = replay_requests[0].prospective_request
    assert isinstance(generation_replay, ProspectiveGenerationProposeRequestV1)
    accepted = harness.engine_admission.admit_generation(
        base_snapshot_artifact_id=generation_replay.base_snapshot_artifact_id,
        constraint_snapshot_artifact_id=generation_replay.constraint_snapshot_artifact_id,
        findings=generation_replay.findings,
        objective_goal_text=generation_replay.objective_goal_text,
        domain_scope=generation_replay.domain_scope,
        target=generation_replay.target,
        generation_policy=generation_replay.generation_policy,
        candidate_export_profiles=generation_replay.candidate_export_profiles,
        actor=actor,
        server=kit._server("execution-option:replay:bridge"),
        llm_execution_mode="replay",
        execution_version_plan=replay_options[0].execution_version_plan,
        cassette_artifact_id=replay_options[0].cassette_artifact_id,
    )
    replay_run = harness.run_record(accepted.run_id)
    assert replay_run is not None
    assert replay_run.payload.execution_version_plan == replay_options[0].execution_version_plan
    assert replay_run.payload.cassette_artifact_id == replay_options[0].cassette_artifact_id


def test_live_resolution_uses_d1_and_missing_pointer_fails_closed(tmp_path: Path) -> None:
    harness = kit.Harness(tmp_path)
    request, _base = _generation_request(harness, mode="live")

    @contextmanager
    def authority_scope():
        with Session(harness.engine) as session:
            yield SqlCostLedger(session, clock=harness.clock)

    harness.engine_admission._execution_version_plans = ExecutionVersionPlanResolver(  # noqa: SLF001
        authority_scope=authority_scope,
        routing_policy_version=None,
        routing_policy_digest=None,
    )

    with pytest.raises(DependencyUnavailable, match="routing-policy pointer"):
        harness.engine_admission.resolve_execution_option(
            request=request,
            actor=kit._tooling_actor(),
        )

    _enable_execution_options(harness)
    resolved = harness.engine_admission.resolve_execution_option(
        request=request,
        actor=kit._tooling_actor(),
    )

    assert resolved.llm_execution_mode == "live"
    assert resolved.execution_version_plan.agent_graph_version == "generation-graph@2"


def test_final_admission_revalidates_authority_after_option_resolution(tmp_path: Path) -> None:
    harness = kit.Harness(tmp_path)
    _enable_execution_options(harness)
    actor = kit._tooling_actor()
    request, _base = _generation_request(harness, mode="record")
    option = harness.engine_admission.resolve_execution_option(request=request, actor=actor)
    prospective = request.prospective_request
    assert isinstance(prospective, ProspectiveGenerationProposeRequestV1)

    registry = harness.engine_admission._registry  # noqa: SLF001 - simulate authority drift
    registry._agent_execution_graphs = {  # noqa: SLF001
        key: graph
        for key, graph in registry._agent_execution_graphs.items()  # noqa: SLF001
        if graph.agent_graph_version != option.execution_version_plan.agent_graph_version
    }
    before = _database_projection(harness)

    with pytest.raises(IntegrityViolation, match="not retained"):
        harness.engine_admission.admit_generation(
            base_snapshot_artifact_id=prospective.base_snapshot_artifact_id,
            constraint_snapshot_artifact_id=prospective.constraint_snapshot_artifact_id,
            findings=prospective.findings,
            objective_goal_text=prospective.objective_goal_text,
            domain_scope=prospective.domain_scope,
            target=prospective.target,
            generation_policy=prospective.generation_policy,
            candidate_export_profiles=prospective.candidate_export_profiles,
            actor=actor,
            server=kit._server("execution-option:authority-drift"),
            llm_execution_mode="record",
            execution_version_plan=option.execution_version_plan,
        )

    assert _database_projection(harness) == before


def test_replay_source_is_authorized_before_lookup_and_queued_source_is_409(
    tmp_path: Path,
) -> None:
    harness = kit.Harness(tmp_path)
    _enable_execution_options(harness)
    actor = kit._tooling_actor()
    record_request, base = _generation_request(harness, mode="record")
    record_option = harness.engine_admission.resolve_execution_option(
        request=record_request,
        actor=actor,
    )
    source = harness.engine_admission.admit_generation(
        base_snapshot_artifact_id=base,
        constraint_snapshot_artifact_id=None,
        findings=(),
        objective_goal_text="Propose an exact bounded economy adjustment.",
        domain_scope=DomainScope(domain_ids=("economy",)),
        target=RefReadBindingV1(ref_name="content/head", expected_ref=None),
        generation_policy=kit.GENERATION_PROFILE,
        candidate_export_profiles=(),
        actor=actor,
        server=kit._server("execution-option:queued-source"),
        llm_execution_mode="record",
        execution_version_plan=record_option.execution_version_plan,
    )
    replay_request, _ = _generation_request(
        harness,
        mode="replay",
        base_artifact_id=base,
        replay_source_run_id=source.run_id,
    )
    before = _database_projection(harness)

    with pytest.raises(WorkflowGuard, match="not compatible"):
        harness.engine_admission.resolve_execution_option(
            request=replay_request,
            actor=actor,
        )

    assert _database_projection(harness) == before

    missing_source_request, _ = _generation_request(
        harness,
        mode="replay",
        base_artifact_id=base,
        replay_source_run_id="run:not-visible",
    )
    with pytest.raises(Forbidden):
        harness.engine_admission.resolve_execution_option(
            request=missing_source_request,
            actor=kit._actor("human"),
        )


def test_playtest_profile_selector_resolves_the_memory_graph(tmp_path: Path) -> None:
    registry = kit.build_builtin_registry()
    catalog = max(
        registry.list_execution_profile_catalogs(),
        key=lambda item: item.catalog_version,
    )
    planner = next(
        item for item in catalog.definitions if item.profile == kit.PLAYTEST_PLANNER_PROFILE
    )
    memory_config = {
        **planner.config,
        "memory_mode": "llm_compaction",
        "max_episode_count": 512,
        "max_steps_per_episode": 512,
        "max_total_steps": 512,
        "max_total_model_calls": 3072,
    }
    harness = kit.Harness(
        tmp_path,
        profile_updates={
            kit.PLAYTEST_PLANNER_PROFILE.profile_id: {
                "config": memory_config,
                "config_hash": canonical_config_hash(memory_config),
            }
        },
    )
    _enable_execution_options(harness)
    constraint = kit._seed_constraint(harness)
    preview = kit._seed_preview(harness, label="option-playtest-memory")
    config = kit._seed_config(
        harness,
        label="option-playtest-memory",
        preview=preview,
        constraint=constraint,
    )
    suite, scenario, episode = kit._seed_task_suite(
        harness,
        preview=preview,
        config=config,
        constraint=constraint,
    )
    request = _resolve_request(
        operation_id="run_playtest_api_v1_playtest_run_post",
        run_kind="playtest.run",
        prospective_request={
            "request_schema_version": "playtest-run-request@1",
            "params": {
                "schema_version": "playtest-run@1",
                "config_artifact_id": config.artifact_id,
                "constraint_snapshot_artifact_id": constraint.artifact_id,
                "task_suite_artifact_id": suite.artifact_id,
                "episodes": [
                    PlaytestEpisodeBindingV1(
                        episode_id=episode.episode_id,
                        scenario_spec_artifact_id=scenario.artifact_id,
                    ).model_dump(mode="json")
                ],
                "environment_profile": kit.ENVIRONMENT_PROFILE.model_dump(mode="json"),
                "planner_policy": kit.PLAYTEST_PLANNER_PROFILE.model_dump(mode="json"),
                "max_steps_per_episode": 16,
                "interaction_mode": "autonomous",
            },
            "llm_execution_mode": "record",
            "seed": 7,
            "execution_version_plan": None,
            "cassette_artifact_id": None,
        },
    )

    option = harness.engine_admission.resolve_execution_option(
        request=request,
        actor=kit._tooling_actor(),
    )

    assert option.execution_version_plan.agent_graph_version == "playtest-memory-graph@1"
    assert {node.agent_node_id for node in option.execution_version_plan.nodes} == {
        "playtest.planner",
        "playtest.executor",
        "playtest.reflect",
        "playtest.memory",
    }
