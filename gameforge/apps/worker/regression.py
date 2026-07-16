"""Production regression-suite authority and deterministic Agent-Env replay.

The platform owns only the :class:`RegressionRunner` port.  This composition-layer
implementation verifies the exact committed suite Artifact, resolves its historical
environment profile, dispatches to one versioned trusted adapter, and executes the
ephemeral repair candidate bytes supplied in ``RegressionRunRequest``.  Unknown or
unavailable authority is always ``unproven``; no default path fabricates a pass.
"""

from __future__ import annotations

import json
import math
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from threading import RLock
from typing import Protocol

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.env_types import Observation, StepResult, parse_action
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    EnvironmentProfileDetailsV1,
    ExecutionProfileDefinitionV1,
    MAX_REPAIR_REGRESSION_SUITE_BYTES_V1,
    ProfileRefV1,
    RunKindRef,
    execution_profile_payload_hash,
)
from gameforge.contracts.findings import Finding
from gameforge.contracts.lineage import ArtifactV2
from gameforge.contracts.regression import (
    AgentEnvRegressionFindingTemplateV1,
    AgentEnvRegressionPayloadV1,
    RegressionCaseSeedManifestV1,
    RegressionCaseSeedV1,
    RegressionSuiteAdapterRefV1,
    RegressionSuiteDispatchV1,
)
from gameforge.env.base import Environment
from gameforge.platform.registry.repository import ImmutablePlatformRegistry
from gameforge.platform.run_handlers.base import ArtifactBlobReader
from gameforge.platform.run_handlers.validation_common import (
    REGRESSION_EVIDENCE_SCHEMA_ID,
    RegressionRunRequest,
    RegressionRunner,
    RegressionSuiteResultV1,
    derive_validation_subseed,
)
from gameforge.spine.ir.snapshot import Snapshot


AGENT_ENV_REPLAY_ADAPTER = RegressionSuiteAdapterRefV1(
    adapter_id="agent-env-action-replay",
    version=1,
)
MAX_REGRESSION_SUITE_WIRE_BYTES = MAX_REPAIR_REGRESSION_SUITE_BYTES_V1
MAX_REGRESSION_ENV_OUTPUT_BYTES = 1024 * 1024
MAX_REGRESSION_AUTHORITY_CACHE_BYTES = 64 * 1024 * 1024


class RegressionArtifactReader(ArtifactBlobReader, Protocol):
    """Identity-aware Artifact reader required by production suite execution."""

    def load_artifact(self, artifact_id: str) -> ArtifactV2: ...

    def read_bytes_bounded(self, artifact_id: str, *, max_bytes: int) -> bytes: ...


class CompletionOracleExecutor(Protocol):
    def evaluate(self, env: object, params: Mapping[str, object]) -> bool: ...


EnvironmentFactory = Callable[[], Environment]


@dataclass(frozen=True, slots=True)
class RegressionEnvironmentPlanV1:
    """Profile-handler static work plan, resolved before any environment exists.

    The built-in Aureus handler derives these values from the exact candidate world.
    Keeping them outside the mutable environment lets the adapter reject an
    oversized grid or Run-total before reset/step can enter a pathfinding BFS.
    """

    factory: EnvironmentFactory
    reset_work_units: int
    step_observation_work_units: int
    navigation_work_units: int


EnvironmentBuilder = Callable[
    [Snapshot, ExecutionProfileDefinitionV1],
    RegressionEnvironmentPlanV1,
]


@dataclass(slots=True)
class ProfileBoundEnvironment(Environment):
    """Explicitly project a low-level env through one M4 profile contract."""

    delegate: Environment
    env_contract_version: str

    def __getattr__(self, name: str) -> object:
        return getattr(self.delegate, name)

    def reset(self, scenario: str, seed: int) -> Observation:
        return self.delegate.reset(scenario, seed)

    def step(self, action) -> StepResult:
        return self.delegate.step(action)

    def state_hash(self) -> str:
        return self.delegate.state_hash()


@dataclass(frozen=True, slots=True)
class RegressionAdapterRequestV1:
    suite_artifact_id: str
    dispatch: RegressionSuiteDispatchV1
    snapshot: Snapshot
    seed: int
    root_seed: int
    run_kind: RunKindRef
    profile: ProfileRefV1
    max_action_work_units: int
    environment_definition: ExecutionProfileDefinitionV1


class RegressionSuiteAdapter(Protocol):
    adapter_ref: RegressionSuiteAdapterRefV1

    def run(self, request: RegressionAdapterRequestV1) -> RegressionSuiteResultV1: ...


@dataclass(frozen=True, slots=True)
class _CachedSuiteAuthority:
    artifact: ArtifactV2
    dispatch: RegressionSuiteDispatchV1
    environment_definition: ExecutionProfileDefinitionV1


class _AgentPayloadCache:
    """Bound parsed adapter payloads by their exact canonical wire sizes."""

    def __init__(self, max_wire_bytes: int = MAX_REGRESSION_AUTHORITY_CACHE_BYTES) -> None:
        self._max_wire_bytes = max_wire_bytes
        self._wire_bytes = 0
        self._entries: OrderedDict[str, tuple[AgentEnvRegressionPayloadV1, int]] = OrderedDict()
        self._lock = RLock()

    def get(self, artifact_id: str) -> AgentEnvRegressionPayloadV1 | None:
        with self._lock:
            entry = self._entries.get(artifact_id)
            if entry is None:
                return None
            self._entries.move_to_end(artifact_id)
            return entry[0]

    def put(
        self,
        artifact_id: str,
        payload: AgentEnvRegressionPayloadV1,
        *,
        wire_bytes: int,
    ) -> None:
        if wire_bytes > self._max_wire_bytes:
            return
        with self._lock:
            previous = self._entries.pop(artifact_id, None)
            if previous is not None:
                self._wire_bytes -= previous[1]
            while self._entries and self._wire_bytes > self._max_wire_bytes - wire_bytes:
                _old_id, (_old_payload, old_size) = self._entries.popitem(last=False)
                self._wire_bytes -= old_size
            self._entries[artifact_id] = (payload, wire_bytes)
            self._wire_bytes += wire_bytes


class _SuiteAuthorityCache:
    """Size-bounded LRU for immutable parsed suite authority.

    Repair verifies the same exact suite against a baseline and several candidate
    snapshots.  Re-reading and reparsing a multi-megabyte immutable suite for every
    candidate is pure I/O amplification; only candidate execution must repeat.
    """

    def __init__(self, max_wire_bytes: int = MAX_REGRESSION_AUTHORITY_CACHE_BYTES) -> None:
        self._max_wire_bytes = max_wire_bytes
        self._wire_bytes = 0
        self._entries: OrderedDict[str, _CachedSuiteAuthority] = OrderedDict()
        self._lock = RLock()

    def get(self, artifact_id: str) -> _CachedSuiteAuthority | None:
        with self._lock:
            entry = self._entries.get(artifact_id)
            if entry is not None:
                self._entries.move_to_end(artifact_id)
            return entry

    def put(self, artifact_id: str, entry: _CachedSuiteAuthority) -> None:
        size = entry.artifact.object_ref.size_bytes
        if size > self._max_wire_bytes:
            return
        with self._lock:
            previous = self._entries.pop(artifact_id, None)
            if previous is not None:
                self._wire_bytes -= previous.artifact.object_ref.size_bytes
            while self._entries and self._wire_bytes > self._max_wire_bytes - size:
                _old_id, old = self._entries.popitem(last=False)
                self._wire_bytes -= old.artifact.object_ref.size_bytes
            self._entries[artifact_id] = entry
            self._wire_bytes += size


@dataclass(frozen=True, slots=True)
class AgentEnvActionReplayAdapter:
    """Replay bounded atomic actions, then evaluate exact deterministic oracles."""

    registry: ImmutablePlatformRegistry
    environment_builders: Mapping[str, EnvironmentBuilder]
    oracle_executors: Mapping[str, CompletionOracleExecutor]
    adapter_ref: RegressionSuiteAdapterRefV1 = AGENT_ENV_REPLAY_ADAPTER
    _payload_cache: _AgentPayloadCache = field(
        default_factory=_AgentPayloadCache,
        compare=False,
        repr=False,
    )

    def run(self, request: RegressionAdapterRequestV1) -> RegressionSuiteResultV1:
        raw_payload = request.dispatch.adapter_payload
        payload = self._payload_cache.get(request.suite_artifact_id)
        if payload is None:
            payload = AgentEnvRegressionPayloadV1.model_validate(raw_payload)
            canonical_wire = canonical_json(payload.model_dump(mode="json"))
            if canonical_wire != canonical_json(raw_payload):
                raise ValueError("regression adapter payload is not its exact wire shape")
            self._payload_cache.put(
                request.suite_artifact_id,
                payload,
                wire_bytes=len(canonical_wire.encode("utf-8")),
            )
        action_work_plan = _plan_total_action_work(
            payload,
            max_action_work_units=request.max_action_work_units,
        )

        oracle_registry = self.registry.get_completion_oracle_registry(
            payload.completion_oracle_registry_ref
        )
        if oracle_registry is None:
            raise ValueError("regression completion-oracle registry is unavailable")
        builder = self.environment_builders.get(request.environment_definition.handler_key)
        if builder is None:
            raise ValueError("regression environment factory is unavailable")
        details = request.environment_definition.details
        if not isinstance(details, EnvironmentProfileDetailsV1):
            raise ValueError("regression environment profile has no exact contract")
        contract = details.contract
        if (
            contract.env_contract_version != request.dispatch.env_contract_version
            or contract.reset_schema_id != "generic-env-reset@1"
            or contract.action_schema_id != "generic-env-action@1"
            or contract.observation_schema_id != "generic-env-observation@1"
        ):
            raise ValueError("regression adapter does not implement the bound environment schema")
        environment_plan = builder(request.snapshot, request.environment_definition)
        _validate_environment_plan(environment_plan, details=details)
        action_work_units = action_work_plan.total(
            environment_plan,
            max_action_work_units=request.max_action_work_units,
        )
        environment_factory = environment_plan.factory
        case_seed_manifest = RegressionCaseSeedManifestV1(
            suite_artifact_id=request.suite_artifact_id,
            root_seed=request.root_seed,
            run_kind=request.run_kind,
            profile=request.profile,
            cases=tuple(
                RegressionCaseSeedV1(
                    case_id=case.case_id,
                    derivation_case_id=f"{request.suite_artifact_id}:{case.case_id}",
                    seed=derive_validation_subseed(
                        root_seed=request.root_seed,
                        run_kind=request.run_kind,
                        profile=request.profile,
                        case_id=f"{request.suite_artifact_id}:{case.case_id}",
                        replication_index=0,
                    ),
                )
                for case in payload.cases
            ),
        )
        seeds_by_case = {item.case_id: item.seed for item in case_seed_manifest.cases}

        findings: list[Finding] = []
        # Keep strong references for the whole run.  Retaining only ``id(env)`` lets
        # CPython reclaim a completed case and reuse its address for a later fresh
        # instance, which would be misdiagnosed as mutable-state reuse.
        environment_instances: list[Environment] = []
        for case in payload.cases:
            env = environment_factory()
            if any(env is existing for existing in environment_instances):
                raise ValueError("regression environment factory reused mutable case state")
            environment_instances.append(env)
            if getattr(env, "env_contract_version", None) != request.dispatch.env_contract_version:
                raise ValueError("regression environment instance has another contract version")
            case_seed = seeds_by_case[case.case_id]
            initial_observation = env.reset(case.scenario_id, case_seed)
            _validate_observation(initial_observation)
            initial_hash = _bounded_state_hash(env.state_hash())
            if (
                case.expected_initial_state_hash is not None
                and initial_hash != case.expected_initial_state_hash
            ):
                findings.append(
                    _mismatch_finding(
                        request=request,
                        case_id=case.case_id,
                        assertion="initial_state_hash",
                        expected=case.expected_initial_state_hash,
                        actual=initial_hash,
                        template=case.failure_finding,
                        execution_seed=case_seed,
                    )
                )
                continue

            case_failed = False
            for step_index, step in enumerate(case.steps):
                action = parse_action(step.action)
                if canonical_json(action.model_dump(mode="json")) != canonical_json(step.action):
                    raise ValueError("regression action is not its exact atomic wire shape")
                _validate_action_resource_bounds(action.model_dump(mode="json"))
                result = env.step(action)
                if (
                    type(result) is not StepResult
                    or type(result.observation) is not Observation
                    or type(result.done) is not bool
                    or type(result.reward) is not float
                    or not math.isfinite(result.reward)
                ):
                    raise ValueError("regression step returned another Agent-Env contract")
                _validate_observation(result.observation)
                _validate_bounded_json(
                    result.info,
                    label="regression environment step info",
                    max_bytes=MAX_REGRESSION_ENV_OUTPUT_BYTES,
                )
                state_hash = _bounded_state_hash(env.state_hash())
                if len(result.observation.last_action_result) > 4_096:
                    raise ValueError("regression environment result exceeds its string bound")
                assertions = (
                    (
                        "last_action_result",
                        step.expected_last_action_result,
                        result.observation.last_action_result,
                    ),
                    ("done", step.expected_done, result.done),
                    ("state_hash", step.expected_state_hash, state_hash),
                )
                for assertion, expected, actual in assertions:
                    if expected is not None and actual != expected:
                        findings.append(
                            _mismatch_finding(
                                request=request,
                                case_id=case.case_id,
                                assertion=assertion,
                                expected=expected,
                                actual=actual,
                                step_index=step_index,
                                template=step.failure_finding or case.failure_finding,
                                execution_seed=case_seed,
                            )
                        )
                        case_failed = True
                        break
                if case_failed:
                    break
            if case_failed:
                continue

            final_hash = _bounded_state_hash(env.state_hash())
            if (
                case.expected_final_state_hash is not None
                and final_hash != case.expected_final_state_hash
            ):
                findings.append(
                    _mismatch_finding(
                        request=request,
                        case_id=case.case_id,
                        assertion="final_state_hash",
                        expected=case.expected_final_state_hash,
                        actual=final_hash,
                        template=case.failure_finding,
                        execution_seed=case_seed,
                    )
                )
                continue

            definition = next(
                (
                    item
                    for item in oracle_registry.definitions
                    if (item.oracle_id, item.version)
                    == (case.completion_oracle.oracle_id, case.completion_oracle.version)
                ),
                None,
            )
            if (
                definition is None
                or definition.params_schema_id != case.completion_oracle.params_schema_id
            ):
                raise ValueError("regression completion oracle does not resolve exactly")
            executor = self.oracle_executors.get(definition.executor_key)
            if executor is None:
                raise ValueError("regression completion-oracle executor is unavailable")
            if not isinstance(case.completion_oracle.params, Mapping):
                raise ValueError("regression completion-oracle params must be an object")
            completed = executor.evaluate(env, case.completion_oracle.params)
            if type(completed) is not bool:
                raise ValueError("regression completion oracle returned a non-boolean verdict")
            if completed != case.expected_completed:
                findings.append(
                    _mismatch_finding(
                        request=request,
                        case_id=case.case_id,
                        assertion="completion_oracle",
                        expected=case.expected_completed,
                        actual=completed,
                        template=case.failure_finding,
                        execution_seed=case_seed,
                    )
                )

        status = "failed" if findings else "passed"
        wire: dict[str, object] = {
            "payload_schema_version": REGRESSION_EVIDENCE_SCHEMA_ID,
            "suite_artifact_id": request.suite_artifact_id,
            "snapshot_id": request.snapshot.snapshot_id,
            "seed": request.seed,
            "case_seed_manifest": case_seed_manifest.model_dump(mode="json"),
            "status": status,
            "reason_code": None,
        }
        if findings:
            wire["findings"] = [finding.model_dump(mode="json") for finding in findings]
        return RegressionSuiteResultV1(
            suite_artifact_id=request.suite_artifact_id,
            status=status,
            payload=wire,
            env_contract_version=request.dispatch.env_contract_version,
            action_work_units=action_work_units,
        )


@dataclass(frozen=True, slots=True)
class WorkerRegressionRunner(RegressionRunner):
    """Verify suite authority and dispatch one real, bounded regression execution."""

    artifacts: RegressionArtifactReader
    registry: ImmutablePlatformRegistry
    adapters: Mapping[tuple[str, int], RegressionSuiteAdapter]
    _authority_cache: _SuiteAuthorityCache = field(
        default_factory=_SuiteAuthorityCache,
        compare=False,
        repr=False,
    )

    def run(self, request: RegressionRunRequest) -> RegressionSuiteResultV1:
        try:
            return self._run_exact(request)
        except IntegrityViolation:
            # Corrupt/tampered retained authority is an operator-visible terminal
            # integrity failure, never an ordinary oracle ``unproven`` result.
            raise
        except Exception:  # noqa: BLE001 - absence/unknown execution proves nothing
            return _unproven(request, reason_code="regression_authority_unavailable")

    def _run_exact(self, request: RegressionRunRequest) -> RegressionSuiteResultV1:
        if isinstance(request.seed, bool) or not isinstance(request.seed, int):
            raise ValueError("regression seed must be an unsigned integer")
        if not 0 <= request.seed <= (1 << 64) - 1:
            raise ValueError("regression seed is outside uint64")
        if (
            request.root_seed is None
            or request.run_kind is None
            or request.profile is None
            or isinstance(request.root_seed, bool)
            or not isinstance(request.root_seed, int)
            or not 0 <= request.root_seed <= (1 << 64) - 1
        ):
            return _unproven(request, reason_code="regression_seed_binding_unavailable")
        if (
            request.max_action_work_units is None
            or isinstance(request.max_action_work_units, bool)
            or not isinstance(request.max_action_work_units, int)
            or request.max_action_work_units < 0
        ):
            return _unproven(request, reason_code="regression_work_budget_unavailable")
        expected_suite_seed = derive_validation_subseed(
            root_seed=request.root_seed,
            run_kind=request.run_kind,
            profile=request.profile,
            case_id=request.suite_artifact_id,
            replication_index=0,
        )
        if request.seed != expected_suite_seed:
            return _unproven(request, reason_code="regression_seed_binding_mismatch")
        if request.snapshot is None or request.snapshot_id is None:
            return _unproven(request, reason_code="candidate_snapshot_unavailable")
        if request.snapshot.snapshot_id != request.snapshot_id:
            return _unproven(request, reason_code="candidate_snapshot_mismatch")

        authority = self._load_suite_authority(request.suite_artifact_id)
        dispatch = authority.dispatch
        definition = authority.environment_definition
        adapter = self.adapters.get((dispatch.adapter.adapter_id, dispatch.adapter.version))
        if adapter is None or adapter.adapter_ref != dispatch.adapter:
            return _unproven(
                request,
                reason_code="regression_adapter_unavailable",
                env_contract_version=dispatch.env_contract_version,
            )
        candidate = _freeze_snapshot(request.snapshot)
        if candidate.snapshot_id != request.snapshot_id:
            return _unproven(request, reason_code="candidate_snapshot_mismatch")
        result = adapter.run(
            RegressionAdapterRequestV1(
                suite_artifact_id=request.suite_artifact_id,
                dispatch=dispatch,
                snapshot=candidate,
                seed=request.seed,
                root_seed=request.root_seed,
                run_kind=request.run_kind,
                profile=request.profile,
                max_action_work_units=request.max_action_work_units,
                environment_definition=definition,
            )
        )
        return _validate_adapter_result(
            request,
            result,
            env_contract_version=dispatch.env_contract_version,
        )

    def _load_suite_authority(self, suite_artifact_id: str) -> _CachedSuiteAuthority:
        cached = self._authority_cache.get(suite_artifact_id)
        if cached is not None:
            return cached

        artifact = self.artifacts.load_artifact(suite_artifact_id)
        if (
            artifact.kind != "regression_suite"
            or artifact.meta.get("payload_schema_id") != "regression-suite@1"
            or artifact.object_ref.size_bytes > MAX_REGRESSION_SUITE_WIRE_BYTES
            or artifact.version_tuple.ir_snapshot_id is None
            or artifact.version_tuple.env_contract_version is None
            or artifact.version_tuple.tool_version is None
        ):
            raise ValueError("regression suite Artifact authority is incomplete")
        blob = self.artifacts.read_bytes_bounded(
            suite_artifact_id,
            max_bytes=MAX_REGRESSION_SUITE_WIRE_BYTES,
        )
        if (
            len(blob) != artifact.object_ref.size_bytes
            or sha256(blob).hexdigest() != artifact.payload_hash
        ):
            raise ValueError("regression suite bytes differ from its ObjectRef")
        raw = json.loads(blob.decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("regression suite payload must be an object")
        dispatch = RegressionSuiteDispatchV1.model_validate(raw)
        if canonical_json(dispatch.model_dump(mode="json")).encode("utf-8") != blob:
            raise ValueError("regression suite payload is not canonical")
        if artifact.version_tuple.env_contract_version != dispatch.env_contract_version:
            raise ValueError("regression suite environment version differs from its Artifact")

        self._validate_direct_lineage(artifact)
        definition = self._resolve_environment_definition(dispatch)
        authority = _CachedSuiteAuthority(
            artifact=artifact,
            dispatch=dispatch,
            environment_definition=definition,
        )
        self._authority_cache.put(suite_artifact_id, authority)
        return authority

    def _validate_direct_lineage(self, suite: ArtifactV2) -> None:
        if len(suite.lineage) != 1:
            raise ValueError("Agent-Env regression suite requires one exact source parent")
        parent = self.artifacts.load_artifact(suite.lineage[0])
        if (
            parent.kind != "ir_snapshot"
            or parent.version_tuple.ir_snapshot_id != suite.version_tuple.ir_snapshot_id
            or parent.version_tuple.doc_version != suite.version_tuple.doc_version
            or parent.version_tuple.constraint_snapshot_id
            != suite.version_tuple.constraint_snapshot_id
        ):
            raise ValueError("regression suite source snapshot lineage is not exact")

    def _resolve_environment_definition(
        self, dispatch: RegressionSuiteDispatchV1
    ) -> ExecutionProfileDefinitionV1:
        binding = dispatch.environment_profile
        catalog = self.registry.get_execution_profile_catalog(
            binding.catalog_version,
            binding.catalog_digest,
        )
        if catalog is None:
            raise ValueError("regression environment profile catalog is unavailable")
        definition = next(
            (item for item in catalog.definitions if item.profile == binding.profile),
            None,
        )
        lifecycle = next(
            (item for item in catalog.lifecycle if item.profile == binding.profile),
            None,
        )
        if (
            definition is None
            or lifecycle is None
            or lifecycle.state == "disabled"
            or definition.profile_kind != "environment"
            or execution_profile_payload_hash(definition) != binding.profile_payload_hash
            or not isinstance(definition.details, EnvironmentProfileDetailsV1)
            or definition.details.contract.env_contract_version != dispatch.env_contract_version
        ):
            raise ValueError("regression environment profile binding is unavailable")
        return definition


def _mismatch_finding(
    *,
    request: RegressionAdapterRequestV1,
    case_id: str,
    assertion: str,
    expected: object,
    actual: object,
    step_index: int | None = None,
    template: AgentEnvRegressionFindingTemplateV1 | None = None,
    execution_seed: int,
) -> Finding:
    identity = canonical_json(
        {
            "suite_artifact_id": request.suite_artifact_id,
            "snapshot_id": request.snapshot.snapshot_id,
            "case_id": case_id,
            "assertion": assertion,
            "step_index": step_index,
        }
    )
    finding_id = f"regression:{sha256(identity.encode('utf-8')).hexdigest()}"
    observation: dict[str, object] = {
        "assertion": assertion,
        "expected": expected,
        "actual": actual,
        "seed": execution_seed,
    }
    if step_index is not None:
        observation["step_index"] = step_index
    evidence = dict(template.evidence) if template is not None else {"case": case_id}
    # This key is deliberately outside the repair target's stable locator-key set:
    # the exact target identity comes from the suite's template, while observed
    # values may change between the failed preview and a candidate.
    evidence["execution_observation"] = observation
    return Finding(
        id=finding_id,
        source="playtest",
        producer_id=f"{request.dispatch.adapter.adapter_id}@{request.dispatch.adapter.version}",
        producer_run_id="regression-runner",
        oracle_type="deterministic",
        defect_class=(
            template.defect_class if template is not None else "regression_expectation_mismatch"
        ),
        severity=template.severity if template is not None else "major",
        snapshot_id=request.snapshot.snapshot_id,
        entities=list(template.entities) if template is not None else [],
        relations=list(template.relations) if template is not None else [],
        constraint_id=template.constraint_id if template is not None else None,
        evidence=evidence,
        minimal_repro=(
            dict(template.minimal_repro)
            if template is not None
            else {"case_id": case_id, "step_index": step_index}
        ),
        status="confirmed",
        message=(
            template.message
            if template is not None
            else f"regression case {case_id!r} failed {assertion}"
        ),
    )


def _freeze_snapshot(snapshot: Snapshot) -> Snapshot:
    """Make the exact canonical candidate bytes independent of caller mutation."""

    return Snapshot(
        entities={
            entity_id: entity.model_copy(deep=True)
            for entity_id, entity in snapshot.entities.items()
        },
        relations={
            relation_id: relation.model_copy(deep=True)
            for relation_id, relation in snapshot.relations.items()
        },
        meta_schema_version=snapshot.meta_schema_version,
    )


def _bounded_state_hash(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 512:
        raise ValueError("regression environment state hash is outside its wire bound")
    return value


def _validate_observation(value: object) -> None:
    if (
        type(value) is not Observation
        or type(value.tick) is not int
        or not 0 <= value.tick <= (1 << 64) - 1
    ):
        raise ValueError("regression environment returned another observation contract")
    _validate_bounded_json(
        value.model_dump(mode="json"),
        label="regression environment observation",
        max_bytes=MAX_REGRESSION_ENV_OUTPUT_BYTES,
    )


def _validate_bounded_json(value: object, *, label: str, max_bytes: int) -> None:
    try:
        encoded = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} is not canonical JSON") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} exceeds its byte limit")
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > 32:
            raise ValueError(f"{label} exceeds its depth limit")
        if item is None or type(item) is bool:
            continue
        if type(item) is int:
            if not -(1 << 63) <= item <= (1 << 64) - 1:
                raise ValueError(f"{label} contains an out-of-range integer")
            continue
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError(f"{label} contains a non-finite float")
            continue
        if isinstance(item, str):
            if len(item) > 4_096:
                raise ValueError(f"{label} contains an oversized string")
            continue
        if isinstance(item, Mapping):
            if len(item) > 4_096:
                raise ValueError(f"{label} contains an oversized object")
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 4_096:
                    raise ValueError(f"{label} contains an invalid object key")
                stack.append((child, depth + 1))
            continue
        if isinstance(item, (tuple, list)):
            if len(item) > 4_096:
                raise ValueError(f"{label} contains an oversized array")
            stack.extend((child, depth + 1) for child in item)
            continue
        raise ValueError(f"{label} contains a non-JSON value")


def _validate_action_resource_bounds(action: Mapping[str, object]) -> None:
    kind = action.get("kind")
    if kind == "wait":
        ticks = action.get("ticks")
        if isinstance(ticks, bool) or not isinstance(ticks, int) or not 0 <= ticks <= 1_000_000:
            raise ValueError("regression wait ticks exceed the execution bound")
    elif kind in {"buy", "sell"}:
        count = action.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 1_000_000:
            raise ValueError("regression economy count exceeds the execution bound")


@dataclass(frozen=True, slots=True)
class _RegressionActionWorkPlan:
    base_action_work_units: int
    case_count: int
    step_count: int
    navigation_count: int

    def total(
        self,
        environment: RegressionEnvironmentPlanV1,
        *,
        max_action_work_units: int,
    ) -> int:
        effective_limit = max_action_work_units
        total = (
            self.base_action_work_units
            + self.case_count * environment.reset_work_units
            + self.step_count * environment.step_observation_work_units
            + self.navigation_count * environment.navigation_work_units
        )
        if total > effective_limit:
            raise ValueError("regression suite exceeds its total action-work budget")
        return total


def _plan_total_action_work(
    payload: AgentEnvRegressionPayloadV1,
    *,
    max_action_work_units: int,
) -> _RegressionActionWorkPlan:
    """Parse every action and preflight non-environment work before compilation."""

    if (
        isinstance(max_action_work_units, bool)
        or not isinstance(max_action_work_units, int)
        or max_action_work_units < 0
    ):
        raise ValueError("regression action-work authority is invalid")
    effective_limit = max_action_work_units
    base_total = 0
    step_count = 0
    navigation_count = 0
    for case in payload.cases:
        for step in case.steps:
            step_count += 1
            action = parse_action(step.action)
            wire = action.model_dump(mode="json")
            if canonical_json(wire) != canonical_json(step.action):
                raise ValueError("regression action is not its exact atomic wire shape")
            _validate_action_resource_bounds(wire)
            kind = wire.get("kind")
            if kind == "navigate_to":
                # The exact candidate/profile-selected traversal cost is supplied
                # by the static environment plan. Count one unit here so an
                # already-exhausted Run ledger still fails before compilation.
                navigation_count += 1
                work = 0
            elif kind == "wait":
                value = wire.get("ticks")
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError("regression wait work is not an integer")
                work = max(1, value)
            elif kind in {"buy", "sell"}:
                value = wire.get("count")
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError("regression economy work is not an integer")
                work = value
            else:
                work = 1
            if base_total > effective_limit - work:
                raise ValueError("regression suite exceeds its total action-work budget")
            base_total += work
            if base_total > effective_limit - navigation_count:
                raise ValueError("regression suite exceeds its total action-work budget")
    return _RegressionActionWorkPlan(
        base_action_work_units=base_total,
        case_count=len(payload.cases),
        step_count=step_count,
        navigation_count=navigation_count,
    )


def _validate_environment_plan(
    plan: RegressionEnvironmentPlanV1,
    *,
    details: EnvironmentProfileDetailsV1,
) -> None:
    if not isinstance(plan, RegressionEnvironmentPlanV1) or not callable(plan.factory):
        raise IntegrityViolation("regression environment handler returned another work contract")
    for label, value in (
        ("reset", plan.reset_work_units),
        ("step observation", plan.step_observation_work_units),
        ("navigation", plan.navigation_work_units),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise IntegrityViolation(
                "regression environment handler returned invalid static work",
                work_dimension=label,
            )
    if not 1 <= plan.navigation_work_units <= details.contract.max_navigation_grid_cells:
        raise IntegrityViolation(
            "regression environment navigation work escapes its frozen profile contract"
        )


def _validate_adapter_result(
    request: RegressionRunRequest,
    result: RegressionSuiteResultV1,
    *,
    env_contract_version: str,
) -> RegressionSuiteResultV1:
    """Do not let an adapter's claim escape its exact suite/snapshot/seed closure."""

    payload = dict(result.payload)
    allowed_fields = {
        "payload_schema_version",
        "suite_artifact_id",
        "snapshot_id",
        "seed",
        "status",
        "reason_code",
        "case_seed_manifest",
        "findings",
    }
    if set(payload) - allowed_fields or "case_seed_manifest" not in payload:
        raise ValueError("regression adapter result has another wire shape")
    manifest = RegressionCaseSeedManifestV1.model_validate(payload["case_seed_manifest"])
    findings_raw = payload.get("findings", ())
    if not isinstance(findings_raw, (tuple, list)):
        raise ValueError("regression adapter findings are not a collection")
    findings = tuple(Finding.model_validate(item) for item in findings_raw)
    unavailable = result.status in {"unproven", "not_executed"}
    if (
        result.status not in {"passed", "failed", "unproven", "not_executed"}
        or result.suite_artifact_id != request.suite_artifact_id
        or payload.get("payload_schema_version") != REGRESSION_EVIDENCE_SCHEMA_ID
        or payload.get("suite_artifact_id") != request.suite_artifact_id
        or payload.get("snapshot_id") != request.snapshot_id
        or payload.get("seed") != request.seed
        or payload.get("status") != result.status
        or payload.get("reason_code") != result.reason_code
        or result.env_contract_version != env_contract_version
        or request.max_action_work_units is None
        or isinstance(result.action_work_units, bool)
        or not isinstance(result.action_work_units, int)
        or not 0 <= result.action_work_units <= request.max_action_work_units
        or request.root_seed is None
        or request.run_kind is None
        or request.profile is None
        or manifest.suite_artifact_id != request.suite_artifact_id
        or manifest.root_seed != request.root_seed
        or manifest.run_kind != request.run_kind
        or manifest.profile != request.profile
        or unavailable != bool(result.reason_code)
        or (result.status == "passed" and findings)
        or (result.status == "failed" and not findings)
        or any(finding.snapshot_id != request.snapshot_id for finding in findings)
    ):
        raise ValueError("regression adapter result escaped its exact execution binding")
    return result


def _unproven(
    request: RegressionRunRequest,
    *,
    reason_code: str,
    env_contract_version: str | None = None,
) -> RegressionSuiteResultV1:
    return RegressionSuiteResultV1(
        suite_artifact_id=request.suite_artifact_id,
        status="unproven",
        reason_code=reason_code,
        env_contract_version=env_contract_version,
        payload={
            "payload_schema_version": REGRESSION_EVIDENCE_SCHEMA_ID,
            "suite_artifact_id": request.suite_artifact_id,
            "snapshot_id": request.snapshot_id,
            "seed": request.seed,
            "status": "unproven",
            "reason_code": reason_code,
        },
    )


def build_worker_regression_runner(
    *,
    artifacts: RegressionArtifactReader,
    registry: ImmutablePlatformRegistry,
    environment_builders: Mapping[str, EnvironmentBuilder],
    oracle_executors: Mapping[str, CompletionOracleExecutor],
) -> WorkerRegressionRunner:
    """Build the trusted adapter map; caller supplies profile-handler factories."""

    adapter = AgentEnvActionReplayAdapter(
        registry=registry,
        environment_builders=environment_builders,
        oracle_executors=oracle_executors,
    )
    return WorkerRegressionRunner(
        artifacts=artifacts,
        registry=registry,
        adapters={(adapter.adapter_ref.adapter_id, adapter.adapter_ref.version): adapter},
    )


__all__ = [
    "AGENT_ENV_REPLAY_ADAPTER",
    "AgentEnvActionReplayAdapter",
    "EnvironmentBuilder",
    "EnvironmentFactory",
    "MAX_REGRESSION_ENV_OUTPUT_BYTES",
    "ProfileBoundEnvironment",
    "RegressionAdapterRequestV1",
    "RegressionArtifactReader",
    "RegressionEnvironmentPlanV1",
    "RegressionSuiteAdapter",
    "WorkerRegressionRunner",
    "build_worker_regression_runner",
]
