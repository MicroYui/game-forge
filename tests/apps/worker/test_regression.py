"""Production regression runner executes exact suites; unavailable authority never passes."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from hashlib import sha256
from weakref import ref

import pytest

import gameforge.apps.worker.regression as worker_regression
from gameforge.apps.worker.regression import (
    AGENT_ENV_REPLAY_ADAPTER,
    MAX_REGRESSION_ENV_OUTPUT_BYTES,
    RegressionEnvironmentPlanV1,
    WorkerRegressionRunner,
    build_worker_regression_runner,
)
from gameforge.apps.worker.app import LocalWorkerConfig
from gameforge.apps.worker.components import _build_builtin_environment
from gameforge.apps.worker.completion_oracles import build_completion_oracle_executors
from gameforge.apps.worker.dispatch import build_worker_process
from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.env_types import Observation, StepResult
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    MAX_REPAIR_REGRESSION_WORK_UNITS_V1,
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
    execution_profile_payload_hash,
)
from gameforge.contracts.ir import Entity, NodeType
from gameforge.contracts.lineage import (
    ArtifactV2,
    ObjectRef,
    VersionTuple,
    build_artifact_v2,
    object_key_for_sha256,
)
from gameforge.contracts.playtest import CompletionOracleRefV1
from gameforge.contracts.regression import (
    AgentEnvRegressionCaseV1,
    AgentEnvRegressionFindingTemplateV1,
    AgentEnvRegressionPayloadV1,
    AgentEnvRegressionStepV1,
    RegressionSuiteAdapterRefV1,
    RegressionSuiteDispatchV1,
)
from gameforge.platform.registry.defaults import build_builtin_registry
from gameforge.platform.publication.payload_schema import validate_artifact_payload
from gameforge.platform.run_handlers.repair import RepairSearchHandler
from gameforge.platform.run_handlers.validation_common import (
    ConstraintRegressionCandidateV1,
    RegressionRunRequest,
    derive_validation_subseed,
)
from gameforge.runtime.persistence import migrations_api
from gameforge.spine.ir.snapshot import Snapshot


class _Artifacts:
    def __init__(self) -> None:
        self.artifacts: dict[str, ArtifactV2] = {}
        self.blobs: dict[str, bytes] = {}
        self.bounded_reads: list[tuple[str, int]] = []

    def put(
        self,
        *,
        kind: str,
        schema: str,
        payload: object,
        version_tuple: VersionTuple,
        lineage: tuple[str, ...] = (),
    ) -> ArtifactV2:
        blob = canonical_json(payload).encode("utf-8")
        digest = sha256(blob).hexdigest()
        ref = ObjectRef(
            key=object_key_for_sha256(digest),
            sha256=digest,
            size_bytes=len(blob),
        )
        artifact = build_artifact_v2(
            kind=kind,  # type: ignore[arg-type]
            version_tuple=version_tuple,
            lineage=lineage,
            payload_hash=digest,
            object_ref=ref,
            meta={"payload_schema_id": schema},
        )
        self.artifacts[artifact.artifact_id] = artifact
        self.blobs[artifact.artifact_id] = blob
        return artifact

    def load_artifact(self, artifact_id: str) -> ArtifactV2:
        return self.artifacts[artifact_id]

    def read_bytes(self, artifact_id: str) -> bytes:
        raise AssertionError(f"unbounded read forbidden for {artifact_id}")

    def read_bytes_bounded(self, artifact_id: str, *, max_bytes: int) -> bytes:
        self.bounded_reads.append((artifact_id, max_bytes))
        blob = self.blobs[artifact_id]
        if len(blob) > max_bytes:
            raise ValueError("fixture exceeds bounded read")
        return blob


@dataclass
class _Env:
    tick: int = 0
    last_result: str = "reset"
    env_contract_version: str = "generic-agent-env@1"

    def reset(self, scenario: str, seed: int) -> Observation:
        del scenario, seed
        self.tick = 0
        self.last_result = "reset"
        return self._observation()

    def step(self, action) -> StepResult:
        self.tick += int(getattr(action, "ticks", 1))
        self.last_result = "waited"
        return StepResult(
            observation=self._observation(),
            reward=0.0,
            done=self.tick >= 1,
            info={},
        )

    def state_hash(self) -> str:
        return f"sha256:{sha256(str(self.tick).encode()).hexdigest()}"

    def _observation(self) -> Observation:
        return Observation(
            tick=self.tick,
            player_pos=(0, 0),
            last_action_result=self.last_result,
        )


class _TickOracle:
    def evaluate(self, env: _Env, params) -> bool:
        del params
        return env.tick >= 1


class _NonFiniteRewardEnv(_Env):
    def step(self, action) -> StepResult:
        result = super().step(action)
        return result.model_copy(update={"reward": float("inf")})


class _OversizedObservationEnv(_Env):
    def reset(self, scenario: str, seed: int) -> Observation:
        observation = super().reset(scenario, seed)
        return observation.model_copy(
            update={"logs": ["x" * (MAX_REGRESSION_ENV_OUTPUT_BYTES + 1)]}
        )


def _environment_plan(factory, *, navigation_work_units: int = 1):
    return RegressionEnvironmentPlanV1(
        factory=factory,
        reset_work_units=0,
        step_observation_work_units=0,
        navigation_work_units=navigation_work_units,
    )


def _fixture(
    *,
    expected_result: str = "waited",
    adapter=None,
    finding_template=None,
    case_count: int = 1,
    environment_builder=None,
    wait_ticks: int = 1,
    action: dict[str, object] | None = None,
    expected_done: bool = True,
    expected_completed: bool = True,
    candidate_snapshot: Snapshot | None = None,
    source_snapshot: Snapshot | None = None,
    oracle_executors=None,
    oracle_executor=None,
):
    registry = build_builtin_registry()
    catalog = registry.list_execution_profile_catalogs()[0]
    definition = next(item for item in catalog.definitions if item.profile_kind == "environment")
    binding = ResolvedExecutionProfileBindingV1(
        field_path="/environment_profile",
        profile=definition.profile,
        expected_profile_kind="environment",
        profile_payload_hash=execution_profile_payload_hash(definition),
        catalog_version=catalog.catalog_version,
        catalog_digest=catalog.catalog_digest,
    )
    oracle_registry = registry.completion_oracle_registries[0]
    oracle_definition = oracle_registry.definitions[0]
    oracle = CompletionOracleRefV1(
        oracle_id=oracle_definition.oracle_id,
        version=oracle_definition.version,
        params_schema_id=oracle_definition.params_schema_id,
        params={"min_completed_quest_fraction": 1},
    )

    source = source_snapshot or Snapshot({}, {})
    candidate = candidate_snapshot or Snapshot.from_entities_relations(
        [Entity(id="region:candidate", type=NodeType.REGION)], []
    )
    artifacts = _Artifacts()
    source_artifact = artifacts.put(
        kind="ir_snapshot",
        schema="ir-core@1",
        payload=source.content_payload,
        version_tuple=VersionTuple(ir_snapshot_id=source.snapshot_id, tool_version="source@1"),
    )
    adapter_payload = AgentEnvRegressionPayloadV1(
        completion_oracle_registry_ref={
            "registry_version": oracle_registry.registry_version,
            "digest": oracle_registry.registry_digest,
        },
        cases=tuple(
            AgentEnvRegressionCaseV1(
                case_id=f"case:wait:{index}",
                scenario_id=f"scenario:wait:{index}",
                steps=(
                    AgentEnvRegressionStepV1(
                        action=action or {"kind": "wait", "ticks": wait_ticks},
                        expected_last_action_result=expected_result,
                        expected_done=expected_done,
                        failure_finding=finding_template,
                    ),
                ),
                completion_oracle=oracle,
                expected_completed=expected_completed,
            )
            for index in range(case_count)
        ),
    )
    dispatch = RegressionSuiteDispatchV1(
        adapter=adapter or AGENT_ENV_REPLAY_ADAPTER,
        environment_profile=binding,
        env_contract_version=definition.details.contract.env_contract_version,  # type: ignore[union-attr]
        adapter_payload=adapter_payload.model_dump(mode="json"),
    )
    suite_artifact = artifacts.put(
        kind="regression_suite",
        schema="regression-suite@1",
        payload=dispatch.model_dump(mode="json"),
        version_tuple=VersionTuple(
            ir_snapshot_id=source.snapshot_id,
            env_contract_version=dispatch.env_contract_version,
            tool_version="suite@1",
        ),
        lineage=(source_artifact.artifact_id,),
    )
    runner = build_worker_regression_runner(
        artifacts=artifacts,
        registry=registry,
        environment_builders={
            definition.handler_key: environment_builder
            or (lambda _snapshot, _definition: _environment_plan(lambda: _Env()))
        },
        oracle_executors=oracle_executors
        or {oracle_definition.executor_key: oracle_executor or _TickOracle()},
    )
    root_seed = 41
    run_kind = RunKindRef(kind="patch.repair", version=1)
    regression_profile = ProfileRefV1(profile_id="builtin.patch_repair", version=1)
    execution_seed = derive_validation_subseed(
        root_seed=root_seed,
        run_kind=run_kind,
        profile=regression_profile,
        case_id=suite_artifact.artifact_id,
        replication_index=0,
    )
    request = RegressionRunRequest(
        suite_artifact_id=suite_artifact.artifact_id,
        snapshot_id=candidate.snapshot_id,
        seed=execution_seed,
        snapshot=candidate,
        root_seed=root_seed,
        run_kind=run_kind,
        profile=regression_profile,
        max_action_work_units=MAX_REPAIR_REGRESSION_WORK_UNITS_V1,
    )
    return artifacts, suite_artifact, runner, request


def _constraint_candidate_request(
    request: RegressionRunRequest,
    constraints: tuple[Constraint, ...],
) -> RegressionRunRequest:
    wire = {
        "dsl_grammar_version": "dsl@1",
        "constraints": [
            constraint.model_dump(mode="json", by_alias=True) for constraint in constraints
        ],
    }
    digest = canonical_sha256(wire)
    candidate = ConstraintRegressionCandidateV1(
        candidate_snapshot_id=f"candidate:{digest[:32]}",
        dsl_grammar_version="dsl@1",
        constraints=constraints,
    )
    return replace(
        request,
        snapshot_id=candidate.candidate_snapshot_id,
        snapshot=None,
        constraint_candidate=candidate,
    )


def _structural_constraint(constraint_id: str, expression: str) -> Constraint:
    return Constraint(
        id=constraint_id,
        dsl_grammar_version="dsl@1",
        kind="structural",
        oracle="deterministic",
        **{"assert": expression},
        severity="major",
    )


def test_worker_regression_runner_executes_ephemeral_candidate() -> None:
    artifacts, suite, runner, request = _fixture()

    result = runner.run(request)

    assert result.status == "passed"
    assert result.payload["snapshot_id"] == request.snapshot.snapshot_id
    assert result.payload["seed"] == request.seed
    assert result.env_contract_version == "generic-agent-env@1"
    assert result.action_work_units == 1
    manifest = result.payload["case_seed_manifest"]
    assert manifest["root_seed"] == 41
    assert "findings" not in result.payload
    assert artifacts.bounded_reads == [(suite.artifact_id, 17 * 1024 * 1024)]


def test_constraint_regression_executes_suite_source_and_exact_candidate() -> None:
    artifacts, suite, runner, snapshot_request = _fixture()
    adapter = next(iter(runner.adapters.values()))
    adapter_limits: list[int] = []

    class RecordingAdapter:
        adapter_ref = adapter.adapter_ref

        def run(self, request):
            adapter_limits.append(request.max_action_work_units)
            return adapter.run(request)

    runner = replace(
        runner,
        adapters={
            (RecordingAdapter.adapter_ref.adapter_id, RecordingAdapter.adapter_ref.version): (
                RecordingAdapter()
            )
        },
    )
    request = _constraint_candidate_request(
        snapshot_request,
        (_structural_constraint("C_acyclic", "acyclic(quest_steps)"),),
    )
    source_artifact = artifacts.load_artifact(suite.lineage[0])

    result = runner.run(request)

    assert result.status == "passed"
    assert result.reason_code is None
    assert result.payload["snapshot_id"] == source_artifact.version_tuple.ir_snapshot_id
    assert result.constraint_candidate_snapshot_id == request.snapshot_id
    assert result.constraint_candidate_digest == request.constraint_candidate.candidate_digest
    assert result.constraint_source_snapshot_id == source_artifact.version_tuple.ir_snapshot_id
    assert result.action_work_units == 2  # one adapter action + one compiled-checker work unit
    assert adapter_limits == [request.max_action_work_units - 1]
    assert artifacts.bounded_reads == [
        (suite.artifact_id, 17 * 1024 * 1024),
        (source_artifact.artifact_id, 96 * 1024 * 1024),
    ]


def test_constraint_regression_returns_fresh_source_bound_findings() -> None:
    source = Snapshot.from_entities_relations(
        [
            Entity(
                id="step:collect",
                type=NodeType.QUEST_STEP,
                attrs={"kind": "collect", "item": "item:missing-source"},
            ),
            Entity(id="item:missing-source", type=NodeType.ITEM),
        ],
        [],
    )
    _artifacts, _suite, runner, snapshot_request = _fixture(source_snapshot=source)
    request = _constraint_candidate_request(
        snapshot_request,
        (
            _structural_constraint(
                "C_collect_source",
                "every_collect_step_has_a_drop_source",
            ),
        ),
    )

    result = runner.run(request)

    assert result.status == "failed"
    findings = result.payload["findings"]
    assert isinstance(findings, list) and findings
    finding = findings[0]
    assert finding["status"] == "confirmed"
    assert finding["snapshot_id"] == source.snapshot_id
    assert finding["constraint_id"] == "C_collect_source"
    assert finding["evidence"]["constraint_regression_binding"] == {
        "candidate_snapshot_id": request.constraint_candidate.candidate_snapshot_id,
        "candidate_digest": request.constraint_candidate.candidate_digest,
        "source_snapshot_id": source.snapshot_id,
    }


def test_regression_target_forms_are_mutually_exclusive() -> None:
    _artifacts, _suite, runner, snapshot_request = _fixture()
    request = _constraint_candidate_request(
        snapshot_request,
        (_structural_constraint("C_acyclic", "acyclic(quest_steps)"),),
    )

    with pytest.raises(IntegrityViolation, match="cannot combine"):
        runner.run(replace(request, snapshot=snapshot_request.snapshot))
    with pytest.raises(IntegrityViolation, match="differs from its candidate target"):
        runner.run(replace(request, snapshot_id="candidate:another"))


def test_constraint_candidate_cannot_mutate_after_request_binding() -> None:
    _artifacts, _suite, runner, snapshot_request = _fixture()
    request = _constraint_candidate_request(
        snapshot_request,
        (_structural_constraint("C_acyclic", "acyclic(quest_steps)"),),
    )
    assert request.constraint_candidate is not None
    request.constraint_candidate.constraints[0].note = "mutated after identity derivation"

    with pytest.raises(IntegrityViolation, match="changed after request binding"):
        runner.run(request)


def test_constraint_compiler_failure_preserves_known_adapter_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _artifacts, _suite, runner, snapshot_request = _fixture(expected_result="arrived")
    request = _constraint_candidate_request(
        snapshot_request,
        (_structural_constraint("C_acyclic", "acyclic(quest_steps)"),),
    )

    def unavailable_compiler(_constraints):
        raise RuntimeError("compiler unavailable")

    monkeypatch.setattr(worker_regression, "compile_all", unavailable_compiler)

    result = runner.run(request)

    assert result.status == "failed"
    assert result.reason_code is None
    assert result.action_work_units == 2
    assert "case_seed_manifest" in result.payload
    findings = result.payload["findings"]
    assert isinstance(findings, list) and len(findings) == 1
    assert findings[0]["status"] == "confirmed"
    assert findings[0]["evidence"]["constraint_regression_binding"] == {
        "candidate_snapshot_id": request.constraint_candidate.candidate_snapshot_id,
        "candidate_digest": request.constraint_candidate.candidate_digest,
        "source_snapshot_id": result.constraint_source_snapshot_id,
    }


def test_constraint_compiler_failure_is_unproven_without_a_known_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _artifacts, _suite, runner, snapshot_request = _fixture()
    request = _constraint_candidate_request(
        snapshot_request,
        (_structural_constraint("C_acyclic", "acyclic(quest_steps)"),),
    )
    monkeypatch.setattr(
        worker_regression,
        "compile_all",
        lambda _constraints: (_ for _ in ()).throw(RuntimeError("compiler unavailable")),
    )

    result = runner.run(request)

    assert result.status == "unproven"
    assert result.reason_code == "constraint_candidate_execution_unavailable"
    assert "findings" not in result.payload


def test_repeated_candidate_execution_reuses_exact_parsed_suite_authority(monkeypatch) -> None:
    artifacts, suite, runner, request = _fixture()
    original_validate = AgentEnvRegressionPayloadV1.model_validate
    parse_calls = 0

    def recording_validate(value):
        nonlocal parse_calls
        parse_calls += 1
        return original_validate(value)

    monkeypatch.setattr(
        AgentEnvRegressionPayloadV1,
        "model_validate",
        staticmethod(recording_validate),
    )

    first = runner.run(request)
    second = runner.run(request)

    assert first.status == second.status == "passed"
    assert parse_calls == 1
    assert artifacts.bounded_reads == [(suite.artifact_id, 17 * 1024 * 1024)]


def test_multi_case_suite_compiles_snapshot_once_and_uses_distinct_subseeds() -> None:
    compiled = 0
    instantiated = 0

    def builder(_snapshot, _definition):
        nonlocal compiled
        compiled += 1

        def create():
            nonlocal instantiated
            instantiated += 1
            return _Env()

        return _environment_plan(create)

    _artifacts, _suite, runner, request = _fixture(
        case_count=2,
        environment_builder=builder,
    )

    result = runner.run(request)

    assert result.status == "passed"
    case_seeds = [item["seed"] for item in result.payload["case_seed_manifest"]["cases"]]
    assert len(set(case_seeds)) == 2
    assert compiled == 1
    assert instantiated == 2


def test_multi_case_suite_retains_prior_environment_instances_until_completion() -> None:
    instances = []

    def builder(_snapshot, _definition):
        def create():
            env = _Env()
            instances.append(ref(env))
            return env

        return _environment_plan(create)

    class AllInstancesAliveOracle:
        def evaluate(self, env, params) -> bool:
            del env, params
            return all(instance() is not None for instance in instances)

    _artifacts, _suite, runner, request = _fixture(
        case_count=2,
        environment_builder=builder,
        oracle_executor=AllInstancesAliveOracle(),
    )

    result = runner.run(request)

    assert result.status == "passed"
    assert len(instances) == 2


def test_builtin_profile_factory_executes_real_wrapped_aureus_environment() -> None:
    candidate = Snapshot.from_entities_relations(
        [
            Entity(
                id="region:runtime",
                type=NodeType.REGION,
                attrs={
                    "grid": {"width": 2, "height": 2, "blocked": []},
                    "start_pos": [0, 0],
                    "scenario_id": "scenario:runtime",
                },
            )
        ],
        [],
    )
    _artifacts, _suite, runner, request = _fixture(
        environment_builder=_build_builtin_environment,
        oracle_executors=build_completion_oracle_executors(),
        candidate_snapshot=candidate,
        expected_done=False,
        expected_completed=False,
    )

    result = runner.run(request)

    assert result.status == "passed"


def test_builtin_navigation_debits_exact_candidate_bfs_upper_bound() -> None:
    candidate = Snapshot.from_entities_relations(
        [
            Entity(
                id="region:runtime",
                type=NodeType.REGION,
                attrs={
                    "grid": {"width": 4, "height": 5, "blocked": []},
                    "start_pos": [0, 0],
                    "scenario_id": "scenario:runtime",
                },
            ),
            Entity(id="npc:target", type=NodeType.NPC, attrs={"pos": [3, 0]}),
        ],
        [],
    )
    _artifacts, _suite, runner, request = _fixture(
        environment_builder=_build_builtin_environment,
        oracle_executors=build_completion_oracle_executors(),
        candidate_snapshot=candidate,
        action={"kind": "navigate_to", "target": "npc:target"},
        expected_result="moving",
        expected_done=False,
        expected_completed=False,
    )

    result = runner.run(request)

    assert result.status == "passed"
    # 20 cells * (reset observation + navigate BFS + step observation).
    assert result.action_work_units == 60


def test_builtin_grid_bound_rejects_before_environment_or_bfs() -> None:
    candidate = Snapshot.from_entities_relations(
        [
            Entity(
                id="region:oversized",
                type=NodeType.REGION,
                attrs={
                    "grid": {"width": 65_537, "height": 1, "blocked": []},
                    "start_pos": [0, 0],
                    "scenario_id": "scenario:oversized",
                },
            )
        ],
        [],
    )
    _artifacts, _suite, runner, request = _fixture(
        environment_builder=_build_builtin_environment,
        oracle_executors=build_completion_oracle_executors(),
        candidate_snapshot=candidate,
        expected_done=False,
        expected_completed=False,
    )

    result = runner.run(request)

    assert result.status == "unproven"
    assert result.reason_code == "regression_authority_unavailable"
    assert result.action_work_units is None


def test_case_seed_manifest_is_strict_recomputable_terminal_evidence() -> None:
    _artifacts, _suite, runner, request = _fixture(case_count=2)
    result = runner.run(request)
    terminal_payload = {
        **dict(result.payload),
        "root_seed": request.root_seed,
        "run_kind": request.run_kind.model_dump(mode="json"),
        "profile_id": request.profile.profile_id,
        "profile_version": request.profile.version,
        "case_id": request.suite_artifact_id,
        "replication_index": 0,
        "seed_derivation_version": "subseed@1",
    }
    terminal_payload.pop("reason_code", None)

    assert (
        validate_artifact_payload(
            payload_schema_id="regression-evidence@1",
            payload=terminal_payload,
        )
        == terminal_payload
    )

    tampered = deepcopy(terminal_payload)
    tampered["case_seed_manifest"]["cases"][0]["seed"] += 1
    with pytest.raises(IntegrityViolation, match="exact registered schema"):
        validate_artifact_payload(
            payload_schema_id="regression-evidence@1",
            payload=tampered,
        )


def test_worker_regression_runner_emits_exact_finding_on_failed_assertion() -> None:
    _artifacts, _suite, runner, request = _fixture(expected_result="arrived")

    result = runner.run(request)

    assert result.status == "failed"
    findings = result.payload["findings"]
    assert isinstance(findings, list) and len(findings) == 1
    assert findings[0]["snapshot_id"] == request.snapshot.snapshot_id
    assert findings[0]["evidence"]["execution_observation"]["assertion"] == "last_action_result"


def test_failed_assertion_preserves_suite_bound_target_predicate_identity() -> None:
    template = AgentEnvRegressionFindingTemplateV1(
        defect_class="unreachable_target",
        severity="critical",
        entities=("npc:blocked",),
        relations=("relation:path",),
        evidence={"target": "npc:blocked", "scenario_id": "scenario:wait"},
        minimal_repro={"case_id": "case:wait:0", "target": "npc:blocked"},
        message="target remains unreachable",
    )
    _artifacts, _suite, runner, request = _fixture(
        expected_result="arrived",
        finding_template=template,
    )

    result = runner.run(request)

    finding = result.payload["findings"][0]
    assert finding["defect_class"] == "unreachable_target"
    assert finding["entities"] == ["npc:blocked"]
    assert finding["relations"] == ["relation:path"]
    assert finding["minimal_repro"] == template.minimal_repro
    assert finding["evidence"]["target"] == "npc:blocked"


def test_unknown_adapter_and_missing_candidate_are_unproven_never_passed() -> None:
    _artifacts, _suite, runner, request = _fixture(
        adapter=RegressionSuiteAdapterRefV1(adapter_id="missing", version=1)
    )
    unknown = runner.run(request)
    missing = runner.run(replace(request, snapshot=None))

    assert (unknown.status, unknown.reason_code) == (
        "unproven",
        "regression_adapter_unavailable",
    )
    assert (missing.status, missing.reason_code) == (
        "unproven",
        "candidate_snapshot_unavailable",
    )

    wrong_seed = runner.run(replace(request, seed=request.seed + 1))
    assert (wrong_seed.status, wrong_seed.reason_code) == (
        "unproven",
        "regression_seed_binding_mismatch",
    )
    assert unknown.env_contract_version == "generic-agent-env@1"
    assert missing.env_contract_version == "generic-agent-env@1"
    assert wrong_seed.env_contract_version == "generic-agent-env@1"


def test_wrong_environment_contract_or_nonfinite_output_is_unproven() -> None:
    _artifacts, _suite, wrong_contract_runner, request = _fixture(
        environment_builder=lambda _snapshot, _definition: _environment_plan(
            lambda: _Env(env_contract_version="env@1")
        )
    )
    wrong_contract = wrong_contract_runner.run(request)
    _artifacts, _suite, nonfinite_runner, nonfinite_request = _fixture(
        environment_builder=lambda _snapshot, _definition: _environment_plan(
            lambda: _NonFiniteRewardEnv()
        )
    )
    nonfinite = nonfinite_runner.run(nonfinite_request)
    _artifacts, _suite, oversized_runner, oversized_request = _fixture(
        environment_builder=lambda _snapshot, _definition: _environment_plan(
            lambda: _OversizedObservationEnv()
        )
    )
    oversized = oversized_runner.run(oversized_request)

    assert wrong_contract.status == "unproven"
    assert nonfinite.status == "unproven"
    assert oversized.status == "unproven"


def test_action_resource_budget_is_fail_closed() -> None:
    _artifacts, _suite, runner, request = _fixture(wait_ticks=1_000_001)

    result = runner.run(request)

    assert result.status == "unproven"
    assert result.reason_code == "regression_authority_unavailable"


def test_total_action_work_budget_is_checked_before_environment_compilation() -> None:
    compiled = 0

    def builder(_snapshot, _definition):
        nonlocal compiled
        compiled += 1
        return _environment_plan(lambda: _Env())

    run_budget = 1_000_000
    per_case_work = run_budget // 2 + 1
    _artifacts, _suite, runner, request = _fixture(
        case_count=2,
        wait_ticks=per_case_work,
        environment_builder=builder,
    )

    result = runner.run(replace(request, max_action_work_units=run_budget))

    assert result.status == "unproven"
    assert result.reason_code == "regression_authority_unavailable"
    assert compiled == 0


def test_run_ledger_remaining_is_checked_before_environment_compilation() -> None:
    compiled = 0

    def builder(_snapshot, _definition):
        nonlocal compiled
        compiled += 1
        return _environment_plan(lambda: _Env())

    _artifacts, _suite, runner, request = _fixture(
        wait_ticks=2,
        environment_builder=builder,
    )

    result = runner.run(replace(request, max_action_work_units=1))

    assert result.status == "unproven"
    assert result.reason_code == "regression_authority_unavailable"
    assert result.action_work_units is None
    assert compiled == 0


def test_tampered_direct_lineage_is_unproven() -> None:
    artifacts, suite, runner, request = _fixture()
    wrong_parent = artifacts.put(
        kind="constraint_snapshot",
        schema="constraint-snapshot@1",
        payload={"constraints": []},
        version_tuple=VersionTuple(constraint_snapshot_id="constraint:1", tool_version="x@1"),
    )
    bad_suite = build_artifact_v2(
        kind=suite.kind,
        version_tuple=suite.version_tuple,
        lineage=(wrong_parent.artifact_id,),
        payload_hash=suite.payload_hash,
        object_ref=suite.object_ref,
        meta=suite.meta,
    )
    artifacts.artifacts[bad_suite.artifact_id] = bad_suite
    artifacts.blobs[bad_suite.artifact_id] = artifacts.blobs[suite.artifact_id]

    bad_seed = derive_validation_subseed(
        root_seed=request.root_seed,
        run_kind=request.run_kind,
        profile=request.profile,
        case_id=bad_suite.artifact_id,
        replication_index=0,
    )
    result = runner.run(replace(request, suite_artifact_id=bad_suite.artifact_id, seed=bad_seed))

    assert result.status == "unproven"
    assert result.reason_code == "regression_authority_unavailable"

    hidden_parent_suite = build_artifact_v2(
        kind=suite.kind,
        version_tuple=suite.version_tuple,
        lineage=(*suite.lineage, wrong_parent.artifact_id),
        payload_hash=suite.payload_hash,
        object_ref=suite.object_ref,
        meta=suite.meta,
    )
    artifacts.artifacts[hidden_parent_suite.artifact_id] = hidden_parent_suite
    artifacts.blobs[hidden_parent_suite.artifact_id] = artifacts.blobs[suite.artifact_id]
    hidden_seed = derive_validation_subseed(
        root_seed=request.root_seed,
        run_kind=request.run_kind,
        profile=request.profile,
        case_id=hidden_parent_suite.artifact_id,
        replication_index=0,
    )
    hidden = runner.run(
        replace(
            request,
            suite_artifact_id=hidden_parent_suite.artifact_id,
            seed=hidden_seed,
        )
    )
    assert hidden.status == "unproven"


def test_typed_artifact_integrity_failure_is_not_downgraded_to_unproven(
    monkeypatch,
) -> None:
    artifacts, _suite, runner, request = _fixture()

    def corrupt_authority(_artifact_id: str):
        raise IntegrityViolation("object binding digest mismatch")

    monkeypatch.setattr(artifacts, "load_artifact", corrupt_authority)

    with pytest.raises(IntegrityViolation, match="object binding digest mismatch"):
        runner.run(request)


def test_mutated_candidate_payload_cannot_reuse_its_old_snapshot_id() -> None:
    _artifacts, _suite, runner, request = _fixture()
    assert request.snapshot is not None
    request.snapshot.entities["region:mutated"] = Entity(
        id="region:mutated",
        type=NodeType.REGION,
    )

    result = runner.run(request)

    assert result.status == "unproven"
    assert result.reason_code == "candidate_snapshot_mismatch"


def test_worker_composition_injects_real_repair_regression_runner(tmp_path) -> None:
    config = LocalWorkerConfig(
        database_url=f"sqlite:///{tmp_path / 'worker.db'}",
        object_store_root=tmp_path / "objects",
        object_store_id="local:default",
        telemetry_db_path=tmp_path / "telemetry.sqlite3",
        worker_principal_id="service:worker:1",
        reaper_principal_id="system:lease-reaper",
        root_secret=b"0" * 32,
    )
    migrations_api.upgrade(config.database_url, "head")
    process = build_worker_process(config)
    try:
        handler = process.components.executors["repair_search@1"]
        assert isinstance(handler, RepairSearchHandler)
        assert isinstance(handler.agent_runner.regression_runner, WorkerRegressionRunner)
    finally:
        process.close()
