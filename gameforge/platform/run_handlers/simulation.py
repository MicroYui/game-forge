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

from dataclasses import dataclass, field
from typing import Protocol

from gameforge.contracts.findings import Finding
from gameforge.contracts.jobs import (
    PreparedRunOutcome,
    RunPayloadEnvelope,
    SimulationRunPayloadV1,
)
from gameforge.contracts.lineage import VersionTuple
from gameforge.spine.sim.economy import (
    EconomyModel,
    EconomySimulator,
    SimResult,
    to_findings,
)

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExecutorContextLike,
    FindingEvidence,
    PreparedArtifactStore,
    build_prepared_findings,
    build_success_result,
    store_prepared_artifact,
)
from gameforge.platform.run_handlers.readers import SnapshotLoader, load_snapshot

SIMULATION_TOOL_VERSION = "economy-sim@1"
SIMULATION_RESULT_SCHEMA_ID = "simulation-result@1"


@dataclass(frozen=True, slots=True)
class SimulationKwargs:
    """The exact seeded simulation arguments derived from the Run payload."""

    seed: int
    n_agents: int
    n_ticks: int


class EconomySimulatorPort(Protocol):
    """Structural view of ``EconomySimulator`` (``run(model, seed, n_agents, n_ticks)``)."""

    def run(self, model: EconomyModel, seed: int, n_agents: int, n_ticks: int) -> SimResult: ...


def derive_simulation_kwargs(
    envelope: RunPayloadEnvelope, payload: SimulationRunPayloadV1
) -> SimulationKwargs:
    """Translate the resolved simulation/workload profiles + seed → run kwargs.

    ``simulation.run`` has ``seed_policy=required``, so ``envelope.seed`` is the
    authoritative subseed; the population (``n_agents``) is the workload's
    ``replication_count`` and the horizon (``n_ticks``) is ``horizon_steps``. The
    resolved ``simulation_profile`` / ``workload_profile`` bindings identify the
    dynamics/workload; their concrete magnitudes travel on the typed payload, so
    the translation is fully deterministic and needs no catalog read.
    """

    if envelope.seed is None:
        raise ValueError("simulation.run requires a seeded Run payload")
    return SimulationKwargs(
        seed=int(envelope.seed),
        n_agents=int(payload.replication_count),
        n_ticks=int(payload.horizon_steps),
    )


@dataclass(frozen=True, slots=True)
class SimulationRunHandler:
    """A ``RunExecutor`` producing the primary simulation result + findings."""

    blobs: ArtifactBlobReader
    store: PreparedArtifactStore
    snapshot_loader: SnapshotLoader = load_snapshot
    simulator: EconomySimulatorPort = field(default_factory=EconomySimulator)

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, SimulationRunPayloadV1):
            raise TypeError("simulation_runner@1 requires a simulation-run@1 payload")

        snapshot = self.snapshot_loader(self.blobs, payload.snapshot_artifact_id)
        model = EconomyModel.from_snapshot(snapshot)
        kwargs = derive_simulation_kwargs(context.payload, payload)
        result = self.simulator.run(
            model,
            seed=kwargs.seed,
            n_agents=kwargs.n_agents,
            n_ticks=kwargs.n_ticks,
        )
        findings = to_findings(result, snapshot.snapshot_id, model)

        primary = store_prepared_artifact(
            self.store,
            kind="simulation_run",
            payload_schema_id=SIMULATION_RESULT_SCHEMA_ID,
            version_tuple=VersionTuple(
                ir_snapshot_id=snapshot.snapshot_id,
                constraint_snapshot_id=payload.constraint_snapshot_artifact_id,
                tool_version=SIMULATION_TOOL_VERSION,
                seed=kwargs.seed,
            ),
            lineage=_simulation_lineage(payload),
            payload=_simulation_result_payload(
                payload, snapshot.snapshot_id, kwargs, result, findings
            ),
        )
        prepared_findings = build_prepared_findings(
            tuple(
                FindingEvidence(finding=finding, evidence_artifact_index=0) for finding in findings
            ),
            run_id=context.run.run_id,
        )
        return build_success_result(
            run=context.run,
            attempt=context.attempt,
            outcome_code="simulation_completed",
            primary_index=0,
            artifacts=(primary,),
            findings=prepared_findings,
        )


def _simulation_lineage(payload: SimulationRunPayloadV1) -> tuple[str, ...]:
    lineage = [payload.snapshot_artifact_id]
    if payload.constraint_snapshot_artifact_id is not None:
        lineage.append(payload.constraint_snapshot_artifact_id)
    if payload.scenario_artifact_id is not None:
        lineage.append(payload.scenario_artifact_id)
    return tuple(lineage)


def _simulation_result_payload(
    payload: SimulationRunPayloadV1,
    snapshot_id: str,
    kwargs: SimulationKwargs,
    result: SimResult,
    findings: list[Finding],
) -> dict[str, object]:
    return {
        "payload_schema_version": SIMULATION_RESULT_SCHEMA_ID,
        "snapshot_id": snapshot_id,
        "seed": kwargs.seed,
        "replication_count": kwargs.n_agents,
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
        "sensitivity": result.sensitivity,
        "findings": [finding.model_dump(mode="json") for finding in findings],
    }


__all__ = [
    "SIMULATION_RESULT_SCHEMA_ID",
    "EconomySimulatorPort",
    "SimulationKwargs",
    "SimulationRunHandler",
    "derive_simulation_kwargs",
]
