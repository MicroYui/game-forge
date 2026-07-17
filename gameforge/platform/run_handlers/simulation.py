"""``simulation_runner@1`` — the deterministic economy-simulation handler.

Thin adapter over ``gameforge.spine.sim.economy``: it loads the input IR snapshot,
extracts the economy (``EconomyModel.from_snapshot``), runs the seeded
Monte-Carlo/agent-based simulation, and turns violated invariants + any detected
collapse into ``simulation``-oracle Findings (``to_findings``). The primary
artifact is a bounded ``simulation_run[simulation-result@1]`` carrying the
invariant verdicts + sensitivity (never the full per-tick trajectories); findings
are sealed under the frozen ``simulation-findings`` policy.

Seed / population / horizon are derived deterministically from the frozen Run
payload (``seed`` + ``replication_count`` + ``horizon_steps``) so the same inputs
+ seed always reproduce the byte-identical outcome. ``outcome_code=simulation_completed``.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Protocol

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import (
    ProfileRefV1,
    ResolvedExecutionProfileBindingV1,
    RunKindRef,
)
from gameforge.contracts.findings import Finding
from gameforge.contracts.jobs import (
    PreparedRunOutcome,
    RunPayloadEnvelope,
    SimulationRunPayloadV1,
)
from gameforge.contracts.playtest import ScenarioSpecV1
from gameforge.spine.sim.economy import (
    EconomyModel,
    EconomySimulator,
    InvariantCheck,
    SimResult,
    detect_collapse,
    to_findings,
)

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExactProfileBindingValidator,
    ExecutorContextLike,
    FindingEvidence,
    FindingHeadRevisionResolver,
    PreparedArtifactStore,
    build_prepared_findings,
    build_success_result,
    load_json_blob,
    prepared_version_tuple,
    require_exact_profile_bindings,
    rebind_finding_producers,
    store_prepared_artifact,
    trust_typed_profile_binding,
)
from gameforge.platform.run_handlers.readers import (
    ConstraintLoader,
    SnapshotLoader,
    load_constraints,
    load_snapshot,
)
from gameforge.platform.run_handlers.validation_common import derive_validation_subseed

SIMULATION_TOOL_VERSION = "economy-sim@1"
SIMULATION_RESULT_SCHEMA_ID = "simulation-result@1"


@dataclass(frozen=True, slots=True)
class SimulationKwargs:
    """The exact Monte-Carlo dimensions derived from the Run payload."""

    root_seed: int
    replication_count: int
    n_ticks: int


@dataclass(frozen=True, slots=True)
class SimulationExecutionBudget:
    """Exact profile-owned limits checked before trajectory allocation."""

    max_replication_count: int
    max_horizon_steps: int
    max_output_ticks: int
    max_total_replication_ticks: int
    max_total_work_units: int


class SimulationExecutionBudgetResolver(Protocol):
    def __call__(
        self,
        simulation_profile: ResolvedExecutionProfileBindingV1,
        workload_profile: ResolvedExecutionProfileBindingV1,
    ) -> SimulationExecutionBudget: ...


def default_simulation_execution_budget(
    _simulation_profile: ResolvedExecutionProfileBindingV1,
    _workload_profile: ResolvedExecutionProfileBindingV1,
) -> SimulationExecutionBudget:
    """Unit/default wiring matching the frozen built-in profile pair."""

    return SimulationExecutionBudget(
        max_replication_count=10_000,
        max_horizon_steps=100_000,
        max_output_ticks=100_000,
        max_total_replication_ticks=2_000_000,
        max_total_work_units=10_000_000,
    )


def validate_economy_simulation_work_budget(
    model: EconomyModel,
    *,
    n_agents: int,
    n_ticks: int,
    replication_count: int,
    max_work_units: int,
) -> int:
    """Bound the real nested-loop cost before allocating trajectories.

    A replication-tick count alone is not a work bound: every simulated agent
    also iterates every sink and every configured source's ``kills_per_tick``.
    The constant term conservatively covers balance and three trajectory/
    aggregation operations.  Arithmetic is checked by division before the final
    multiplication so adversarial JSON integers cannot manufacture a giant
    intermediate and then rely on a self-declared profile maximum.
    """

    if min(n_agents, n_ticks, replication_count, max_work_units) < 1:
        raise IntegrityViolation("simulation work dimensions must be positive")
    outer_units = n_agents * n_ticks * replication_count
    if outer_units > max_work_units:
        raise IntegrityViolation("simulation exceeds the exact profile work budget")
    max_factor = max_work_units // outer_units
    # Every source incurs fixed lookup/coercion work even when it performs zero
    # draws; then ``kills_per_tick`` adds the inner RNG loop.
    fixed_factor = 4 + len(model.sinks) + len(model.sources)
    if fixed_factor > max_factor:
        raise IntegrityViolation("simulation exceeds the exact profile work budget")
    source_draws = 0
    for source in model.sources:
        try:
            kills = int(source.get("kills_per_tick", 1))
        except (TypeError, ValueError, OverflowError) as exc:
            raise IntegrityViolation("simulation source work factor is malformed") from exc
        source_draws += max(0, kills)
        if fixed_factor + source_draws > max_factor:
            raise IntegrityViolation("simulation exceeds the exact profile work budget")
    return outer_units * (fixed_factor + source_draws)


class EconomySimulatorPort(Protocol):
    """Structural view of ``EconomySimulator`` (``run(model, seed, n_agents, n_ticks)``)."""

    def run(self, model: EconomyModel, seed: int, n_agents: int, n_ticks: int) -> SimResult: ...


def derive_simulation_kwargs(
    envelope: RunPayloadEnvelope, payload: SimulationRunPayloadV1
) -> SimulationKwargs:
    """Translate the resolved simulation/workload profiles + seed → run kwargs.

    ``simulation.run`` has ``seed_policy=required``, so ``envelope.seed`` is the
    authoritative root seed. ``replication_count`` means independent Monte-Carlo
    children (never a disguised agent population); each child receives the frozen
    ``subseed@1`` derivation in :func:`run_simulation_replications`. The horizon
    remains the typed ``horizon_steps`` value.
    """

    if envelope.seed is None:
        raise ValueError("simulation.run requires a seeded Run payload")
    return SimulationKwargs(
        root_seed=int(envelope.seed),
        replication_count=int(payload.replication_count),
        n_ticks=int(payload.horizon_steps),
    )


@dataclass(frozen=True, slots=True)
class SimulationReplicationAggregate:
    """Bounded aggregate plus the exact deterministic child-seed closure."""

    result: SimResult
    case_id: str
    child_seed_digest: str
    first_child_seed: int
    last_child_seed: int
    child_collapse_digest: str
    collapsed_replication_count: int
    first_collapsed_replication_index: int | None
    first_collapsed_child_seed: int | None
    earliest_collapse_tick: int | None
    earliest_warning_tick: int | None


def run_simulation_replications(
    *,
    simulator: EconomySimulatorPort,
    model: EconomyModel,
    kwargs: SimulationKwargs,
    run_kind: RunKindRef,
    simulation_profile: ProfileRefV1,
    case_id: str,
) -> SimulationReplicationAggregate:
    """Execute every declared replication with its own ``subseed@1`` child.

    One EconomySimulator child models one independent agent trajectory. The
    returned result contains only a bounded aggregate; individual trajectories
    and the potentially large seed vector are committed by a canonical digest.
    The common derivation tuple (root/run/profile/case/count/version) is written
    into the result payload by the caller, so the complete vector is recomputable.
    """

    # Incrementally hash the exact canonical seed-vector wire without retaining
    # a potentially 100k-item list. Integers have the same direct representation
    # under ``canonical_json``, and keys are emitted in sorted order.
    seed_digest = hashlib.sha256()
    seed_digest.update(b'{"child_seeds":[')
    collapse_digest = hashlib.sha256()
    collapse_digest.update(b'{"child_collapse_verdicts":[')
    first_child_seed: int | None = None
    last_child_seed: int | None = None
    collapsed_replication_count = 0
    first_collapsed_replication_index: int | None = None
    first_collapsed_child_seed: int | None = None
    earliest_collapse_tick: int | None = None
    earliest_warning_tick: int | None = None

    # Streaming aggregates: O(horizon + invariant_count), never O(R * horizon).
    balance_sums = [0.0] * kwargs.n_ticks
    source_sums = [0.0] * kwargs.n_ticks
    sink_sums = [0.0] * kwargs.n_ticks
    invariant_names: tuple[str, ...] | None = None
    invariant_thresholds: list[float] = []
    invariant_observed_sums: list[float] = []
    invariant_observed_mins: list[float] = []
    invariant_observed_maxes: list[float] = []
    invariant_failed_counts: list[int] = []

    for replication_index in range(kwargs.replication_count):
        child_seed = derive_validation_subseed(
            root_seed=kwargs.root_seed,
            run_kind=run_kind,
            profile=simulation_profile,
            case_id=case_id,
            replication_index=replication_index,
        )
        if replication_index:
            seed_digest.update(b",")
        seed_digest.update(str(child_seed).encode("ascii"))
        if first_child_seed is None:
            first_child_seed = child_seed
        last_child_seed = child_seed

        result = simulator.run(
            model,
            seed=child_seed,
            n_agents=1,
            n_ticks=kwargs.n_ticks,
        )
        trajectories = tuple(
            result.distributions.get(metric_name)
            for metric_name in (
                "avg_balance_per_tick",
                "total_source_per_tick",
                "total_sink_per_tick",
            )
        )
        if any(
            not isinstance(values, list) or len(values) != kwargs.n_ticks for values in trajectories
        ):
            raise IntegrityViolation("simulation replication returned a malformed trajectory")
        for accumulator, values in zip(
            (balance_sums, source_sums, sink_sums), trajectories, strict=True
        ):
            assert isinstance(values, list)
            for tick, raw_value in enumerate(values):
                if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool):
                    raise IntegrityViolation("simulation replication trajectory is not numeric")
                value = float(raw_value)
                combined = accumulator[tick] + value
                if not math.isfinite(value) or not math.isfinite(combined):
                    raise IntegrityViolation("simulation replication trajectory is not finite")
                accumulator[tick] = combined

        names = tuple(check.name for check in result.invariants)
        if invariant_names is None:
            if len(names) != len(set(names)):
                raise IntegrityViolation("simulation replication invariant names are not unique")
            invariant_names = names
            invariant_thresholds = [check.threshold for check in result.invariants]
            invariant_observed_sums = [0.0] * len(result.invariants)
            invariant_observed_mins = [math.inf] * len(result.invariants)
            invariant_observed_maxes = [-math.inf] * len(result.invariants)
            invariant_failed_counts = [0] * len(result.invariants)
        elif names != invariant_names:
            raise IntegrityViolation("simulation replications disagree on their invariant set")
        for ordinal, check in enumerate(result.invariants):
            if (
                not math.isfinite(check.observed)
                or not math.isfinite(check.threshold)
                or check.threshold != invariant_thresholds[ordinal]
            ):
                raise IntegrityViolation(
                    "simulation replication invariant is non-finite or unstable",
                    invariant=check.name,
                )
            observed_sum = invariant_observed_sums[ordinal] + check.observed
            if not math.isfinite(observed_sum):
                raise IntegrityViolation(
                    "simulation invariant aggregate exceeds the finite evidence domain",
                    invariant=check.name,
                )
            invariant_observed_sums[ordinal] = observed_sum
            invariant_observed_mins[ordinal] = min(invariant_observed_mins[ordinal], check.observed)
            invariant_observed_maxes[ordinal] = max(
                invariant_observed_maxes[ordinal], check.observed
            )
            invariant_failed_counts[ordinal] += int(not check.ok)

        collapse = detect_collapse(result)
        collapse_verdict = {
            "replication_index": replication_index,
            "child_seed": child_seed,
            "collapse_tick": None if collapse is None else collapse.collapse_tick,
            "early_warning_tick": (None if collapse is None else collapse.early_warning_tick),
        }
        if replication_index:
            collapse_digest.update(b",")
        collapse_digest.update(canonical_json(collapse_verdict).encode("utf-8"))
        if collapse is not None:
            collapsed_replication_count += 1
            if first_collapsed_replication_index is None:
                first_collapsed_replication_index = replication_index
                first_collapsed_child_seed = child_seed
            if earliest_collapse_tick is None or collapse.collapse_tick < earliest_collapse_tick:
                earliest_collapse_tick = collapse.collapse_tick
            if earliest_warning_tick is None or collapse.early_warning_tick < earliest_warning_tick:
                earliest_warning_tick = collapse.early_warning_tick

    seed_digest.update(b'],"seed_derivation_version":"subseed@1"}')
    collapse_digest.update(b'],"verdict_version":"child-collapse@1"}')
    assert first_child_seed is not None and last_child_seed is not None
    assert invariant_names is not None
    replication_count = kwargs.replication_count
    distributions = {
        "avg_balance_per_tick": [value / replication_count for value in balance_sums],
        "total_source_per_tick": source_sums,
        "total_sink_per_tick": sink_sums,
    }
    checks = [
        InvariantCheck(
            name=name,
            ok=invariant_failed_counts[ordinal] == 0,
            observed=invariant_observed_sums[ordinal] / replication_count,
            threshold=invariant_thresholds[ordinal],
            evidence={
                "aggregation": "all_replications_must_pass@1",
                "replication_count": replication_count,
                "failed_replication_count": invariant_failed_counts[ordinal],
                "observed_min": invariant_observed_mins[ordinal],
                "observed_max": invariant_observed_maxes[ordinal],
            },
        )
        for ordinal, name in enumerate(invariant_names)
    ]
    source_total = sum(source_sums)
    sink_total = sum(sink_sums)
    if not math.isfinite(source_total) or not math.isfinite(sink_total):
        raise IntegrityViolation("simulation sensitivity aggregate is not finite")
    sink_source_ratio = sink_total / source_total if source_total else None
    if sink_source_ratio is not None and not math.isfinite(sink_source_ratio):
        raise IntegrityViolation("simulation sensitivity ratio is not finite")
    aggregate = SimResult(
        distributions=distributions,
        invariants=checks,
        sensitivity={
            "source_total": source_total,
            "sink_total": sink_total,
            "sink_source_ratio": sink_source_ratio,
            "replication_count": replication_count,
        },
    )
    return SimulationReplicationAggregate(
        result=aggregate,
        case_id=case_id,
        child_seed_digest=seed_digest.hexdigest(),
        first_child_seed=first_child_seed,
        last_child_seed=last_child_seed,
        child_collapse_digest=collapse_digest.hexdigest(),
        collapsed_replication_count=collapsed_replication_count,
        first_collapsed_replication_index=first_collapsed_replication_index,
        first_collapsed_child_seed=first_collapsed_child_seed,
        earliest_collapse_tick=earliest_collapse_tick,
        earliest_warning_tick=earliest_warning_tick,
    )


@dataclass(frozen=True, slots=True)
class SimulationRunHandler:
    """A ``RunExecutor`` producing the primary simulation result + findings."""

    blobs: ArtifactBlobReader
    store: PreparedArtifactStore
    snapshot_loader: SnapshotLoader = load_snapshot
    constraint_loader: ConstraintLoader = load_constraints
    simulator: EconomySimulatorPort = field(default_factory=EconomySimulator)
    execution_budget_resolver: SimulationExecutionBudgetResolver = (
        default_simulation_execution_budget
    )
    finding_head_revision: FindingHeadRevisionResolver | None = None
    profile_binding_validator: ExactProfileBindingValidator = trust_typed_profile_binding

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, SimulationRunPayloadV1):
            raise TypeError("simulation_runner@1 requires a simulation-run@1 payload")

        profile_bindings = require_exact_profile_bindings(
            context,
            expected={
                "/params/simulation_profile": (payload.simulation_profile, "simulation"),
                "/params/workload_profile": (payload.workload_profile, "workload"),
            },
            validator=self.profile_binding_validator,
        )

        kwargs = derive_simulation_kwargs(context.payload, payload)
        snapshot = self.snapshot_loader(self.blobs, payload.snapshot_artifact_id)
        constraints = self._constraints(payload)
        scenario = self._scenario(context, payload)
        model = EconomyModel.from_snapshot(snapshot)
        self._validate_execution_budget(profile_bindings, kwargs, model)
        replication = run_simulation_replications(
            simulator=self.simulator,
            model=model,
            kwargs=kwargs,
            run_kind=context.run.kind,
            simulation_profile=payload.simulation_profile,
            case_id=(
                f"scenario:{scenario.scenario_id}"
                if scenario is not None
                else f"simulation:{snapshot.snapshot_id}"
            ),
        )
        findings = to_findings(replication.result, snapshot.snapshot_id, model)
        findings = _preserve_child_collapse_finding(
            findings,
            replication=replication,
            snapshot_id=snapshot.snapshot_id,
            model=model,
        )
        findings.extend(
            unproven_input_application_findings(
                snapshot_id=snapshot.snapshot_id,
                constraints=constraints,
                scenario=scenario,
            )
        )
        findings = rebind_finding_producers(findings, run_id=context.run.run_id)

        primary = store_prepared_artifact(
            self.store,
            kind="simulation_run",
            payload_schema_id=SIMULATION_RESULT_SCHEMA_ID,
            version_tuple=prepared_version_tuple(
                context,
                tool_version=SIMULATION_TOOL_VERSION,
                projected_fields=("constraint_snapshot_id", "env_contract_version"),
                overrides={
                    "ir_snapshot_id": snapshot.snapshot_id,
                    "seed": kwargs.root_seed,
                },
            ),
            lineage=_simulation_lineage(payload),
            payload=_simulation_result_payload(
                payload,
                snapshot.snapshot_id,
                kwargs,
                replication,
                findings,
                constraints=constraints,
                scenario=scenario,
                run_kind=context.run.kind,
            ),
        )
        prepared_findings = build_prepared_findings(
            tuple(
                FindingEvidence(finding=finding, evidence_artifact_index=0) for finding in findings
            ),
            run_id=context.run.run_id,
            head_revision_resolver=self.finding_head_revision,
        )
        return build_success_result(
            run=context.run,
            attempt=context.attempt,
            outcome_code="simulation_completed",
            primary_index=0,
            artifacts=(primary,),
            findings=prepared_findings,
        )

    def _constraints(self, payload: SimulationRunPayloadV1) -> tuple[Constraint, ...]:
        if payload.constraint_snapshot_artifact_id is None:
            return ()
        return tuple(self.constraint_loader(self.blobs, payload.constraint_snapshot_artifact_id))

    def _validate_execution_budget(
        self,
        profile_bindings: dict[str, ResolvedExecutionProfileBindingV1],
        kwargs: SimulationKwargs,
        model: EconomyModel,
    ) -> None:
        budget = self.execution_budget_resolver(
            profile_bindings["/params/simulation_profile"],
            profile_bindings["/params/workload_profile"],
        )
        if (
            kwargs.replication_count > budget.max_replication_count
            or kwargs.n_ticks > budget.max_horizon_steps
            or kwargs.n_ticks > budget.max_output_ticks
            or kwargs.replication_count * kwargs.n_ticks > budget.max_total_replication_ticks
        ):
            raise IntegrityViolation(
                "simulation request exceeds the exact profile execution budget"
            )
        validate_economy_simulation_work_budget(
            model,
            n_agents=1,
            n_ticks=kwargs.n_ticks,
            replication_count=kwargs.replication_count,
            max_work_units=budget.max_total_work_units,
        )

    def _scenario(
        self,
        context: ExecutorContextLike,
        payload: SimulationRunPayloadV1,
    ) -> ScenarioSpecV1 | None:
        if payload.scenario_artifact_id is None:
            return None
        raw = load_json_blob(self.blobs, payload.scenario_artifact_id)
        if not isinstance(raw, dict):
            raise IntegrityViolation("simulation scenario payload must be an object")
        scenario = ScenarioSpecV1.model_validate(raw)
        if (
            scenario.source_preview_artifact_id != payload.snapshot_artifact_id
            or scenario.constraint_snapshot_artifact_id != payload.constraint_snapshot_artifact_id
            or context.payload.version_tuple.env_contract_version != scenario.env_contract_version
        ):
            raise IntegrityViolation("simulation scenario differs from exact Run inputs")
        return scenario


def _simulation_lineage(payload: SimulationRunPayloadV1) -> tuple[str, ...]:
    lineage = [payload.snapshot_artifact_id]
    if payload.constraint_snapshot_artifact_id is not None:
        lineage.append(payload.constraint_snapshot_artifact_id)
    if payload.scenario_artifact_id is not None:
        lineage.append(payload.scenario_artifact_id)
    return tuple(lineage)


def _child_collapse_binding(
    replication: SimulationReplicationAggregate,
) -> dict[str, object]:
    return {
        "aggregation": "any_replication_collapse@1",
        "replication_count": replication.result.sensitivity["replication_count"],
        "collapsed_replication_count": replication.collapsed_replication_count,
        "child_collapse_digest": replication.child_collapse_digest,
        "child_collapse_verdict_version": "child-collapse@1",
        "first_collapsed_replication_index": (replication.first_collapsed_replication_index),
        "first_collapsed_child_seed": replication.first_collapsed_child_seed,
        "earliest_collapse_tick": replication.earliest_collapse_tick,
        "earliest_warning_tick": replication.earliest_warning_tick,
    }


def _preserve_child_collapse_finding(
    findings: list[Finding],
    *,
    replication: SimulationReplicationAggregate,
    snapshot_id: str,
    model: EconomyModel,
) -> list[Finding]:
    """Keep an any-child collapse verdict even when averaging masks it.

    ``detect_collapse`` is nonlinear, so applying it only to the mean trajectory
    is unsound.  The replication loop records every child verdict; this helper
    either enriches the one aggregate-trajectory finding or emits exactly one
    standard ``economy_collapse`` finding when the aggregate stays below the
    threshold.
    """

    if replication.collapsed_replication_count == 0:
        return findings

    binding = _child_collapse_binding(replication)
    existing_index = next(
        (
            index
            for index, finding in enumerate(findings)
            if finding.defect_class == "economy_collapse"
        ),
        None,
    )
    if existing_index is not None:
        existing = findings[existing_index]
        updated = existing.model_copy(
            update={"evidence": {**existing.evidence, "replication_collapse": binding}}
        )
        return [
            updated if index == existing_index else finding
            for index, finding in enumerate(findings)
        ]

    assert replication.earliest_collapse_tick is not None
    assert replication.earliest_warning_tick is not None
    run_id = f"sim@{snapshot_id[:23]}"
    entities = sorted(
        {source["producer"] for source in model.sources} | {sink["shop"] for sink in model.sinks}
    )
    relations = sorted({source["relation_id"] for source in model.sources})
    evidence: dict[str, object] = {
        "collapse_tick": replication.earliest_collapse_tick,
        "early_warning_tick": replication.earliest_warning_tick,
        "replication_collapse": binding,
        "faucets": [
            {
                "producer": source["producer"],
                "gold_min": source["gold_min"],
                "gold_max": source["gold_max"],
            }
            for source in model.sources
        ],
        "sinks": [{"shop": sink["shop"], "price": sink["price"]} for sink in model.sinks],
    }
    return [
        *findings,
        Finding(
            id=f"{run_id}#{len(findings)}",
            source="sim",
            producer_id="economy_sim",
            producer_run_id=run_id,
            oracle_type="simulation",
            defect_class="economy_collapse",
            severity="critical",
            snapshot_id=snapshot_id,
            entities=entities,
            relations=relations,
            evidence=evidence,
            status="confirmed",
            message=(
                "At least one independent simulated economy trajectory collapsed "
                f"({replication.collapsed_replication_count}/"
                f"{replication.result.sensitivity['replication_count']} replications); "
                "the mean trajectory is not permitted to mask that failure. "
                "Descriptive what-if only — no prescriptive fix given."
            ),
        ),
    ]


def _simulation_result_payload(
    payload: SimulationRunPayloadV1,
    snapshot_id: str,
    kwargs: SimulationKwargs,
    replication: SimulationReplicationAggregate,
    findings: list[Finding],
    *,
    constraints: tuple[Constraint, ...],
    scenario: ScenarioSpecV1 | None,
    run_kind: RunKindRef,
) -> dict[str, object]:
    result = replication.result
    constraint_ids = sorted(constraint.id for constraint in constraints)
    return {
        "payload_schema_version": SIMULATION_RESULT_SCHEMA_ID,
        "profile": payload.simulation_profile.model_dump(mode="json"),
        "snapshot_id": snapshot_id,
        # The Artifact tuple and payload top level bind the Run root seed. Every
        # actual child seed is committed by the complete derivation manifest.
        "seed": kwargs.root_seed,
        "replication_count": kwargs.replication_count,
        "horizon_steps": kwargs.n_ticks,
        "invariants": [
            {
                "name": check.name,
                "ok": check.ok,
                "observed": check.observed,
                "threshold": check.threshold,
                "evidence": check.evidence,
            }
            for check in result.invariants
        ],
        "sensitivity": {
            **result.sensitivity,
            "child_collapse_binding": _child_collapse_binding(replication),
            "seed_binding": {
                "root_seed": kwargs.root_seed,
                "run_kind": run_kind.model_dump(mode="json"),
                "profile_id": payload.simulation_profile.profile_id,
                "profile_version": payload.simulation_profile.version,
                "case_id": replication.case_id,
                "replication_count": kwargs.replication_count,
                "first_child_seed": replication.first_child_seed,
                "last_child_seed": replication.last_child_seed,
                "child_seed_digest": replication.child_seed_digest,
                "seed_derivation_version": "subseed@1",
            },
            "execution_binding": {
                "simulation_profile": payload.simulation_profile.model_dump(mode="json"),
                "workload_profile": payload.workload_profile.model_dump(mode="json"),
                "constraint_snapshot_artifact_id": payload.constraint_snapshot_artifact_id,
                "scenario_artifact_id": payload.scenario_artifact_id,
                "constraint_ids": constraint_ids,
                "scenario_id": None if scenario is None else scenario.scenario_id,
                "constraint_application": {
                    "status": (
                        "not_applicable"
                        if payload.constraint_snapshot_artifact_id is None
                        else "unproven"
                    ),
                    "reason_code": (
                        None
                        if payload.constraint_snapshot_artifact_id is None
                        else "constraint_profile_not_executable"
                    ),
                },
                "scenario_application": {
                    "status": "not_applicable" if scenario is None else "unproven",
                    "reason_code": (None if scenario is None else "scenario_reset_not_executable"),
                },
            },
        },
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }


def unproven_input_application_findings(
    *,
    snapshot_id: str,
    constraints: tuple[Constraint, ...],
    scenario: ScenarioSpecV1 | None,
) -> list[Finding]:
    """Make unsupported profile input semantics explicit instead of silent pass."""

    run_id = f"simulation-inputs@{snapshot_id[:19]}"
    findings: list[Finding] = []
    if constraints:
        constraint_ids = sorted(constraint.id for constraint in constraints)
        findings.append(
            Finding(
                id=f"{run_id}#constraints",
                source="sim",
                producer_id="economy_sim",
                producer_run_id=run_id,
                oracle_type="simulation",
                defect_class="simulation_constraint_unproven",
                severity="major",
                snapshot_id=snapshot_id,
                evidence={
                    "reason": "constraint_profile_not_executable",
                    "constraint_ids": constraint_ids,
                },
                status="unproven",
                message=(
                    "the built-in economy simulation profile does not interpret the "
                    "declared DSL constraints; their simulation verdict is unproven"
                ),
            )
        )
    if scenario is not None:
        findings.append(
            Finding(
                id=f"{run_id}#scenario",
                source="sim",
                producer_id="economy_sim",
                producer_run_id=run_id,
                oracle_type="simulation",
                defect_class="simulation_scenario_unproven",
                severity="major",
                snapshot_id=snapshot_id,
                evidence={
                    "reason": "scenario_reset_not_executable",
                    "scenario_id": scenario.scenario_id,
                    "reset_schema_id": scenario.reset_binding.reset_schema_id,
                    "reset_payload_hash": scenario.reset_binding.payload_hash,
                },
                status="unproven",
                message=(
                    "the built-in economy simulation profile cannot execute the "
                    "declared scenario reset; its scenario-specific verdict is unproven"
                ),
            )
        )
    return findings


__all__ = [
    "SIMULATION_RESULT_SCHEMA_ID",
    "EconomySimulatorPort",
    "SimulationKwargs",
    "SimulationExecutionBudget",
    "SimulationExecutionBudgetResolver",
    "SimulationReplicationAggregate",
    "SimulationRunHandler",
    "derive_simulation_kwargs",
    "default_simulation_execution_budget",
    "run_simulation_replications",
    "unproven_input_application_findings",
    "validate_economy_simulation_work_budget",
]
