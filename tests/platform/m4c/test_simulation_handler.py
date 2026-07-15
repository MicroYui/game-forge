"""Task 11a — ``simulation_runner@1`` handler (deterministic economy sim adapter)."""

from __future__ import annotations

import json

import pytest

from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.contracts.jobs import PreparedRunResult, SimulationRunPayloadV1
from gameforge.platform.run_handlers.simulation import (
    SIMULATION_RESULT_SCHEMA_ID,
    SimulationRunHandler,
    derive_simulation_kwargs,
)
from tests.platform.m4c.handler_support import (
    FakeArtifactStore,
    build_context,
    build_envelope,
    resolved_binding,
    snapshot_bytes,
)

SIM_KIND = RunKindRef(kind="simulation.run", version=1)
SNAPSHOT_ID = "artifact:snapshot"


def _runaway_economy() -> bytes:
    # A high-yield faucet with no sink -> sink/source imbalance + collapse.
    currency = Entity(id="gold", type=NodeType.CURRENCY, attrs={})
    monster = Entity(
        id="mob",
        type=NodeType.MONSTER,
        attrs={"gold_min": 50, "gold_max": 150, "kills_per_tick": 10},
    )
    drop = Relation(id="d1", type=EdgeType.DROPS_FROM, src_id="mob", dst_id="gold")
    return snapshot_bytes([currency, monster], [drop])


def _sim_payload(*, replication_count=20, horizon_steps=40) -> SimulationRunPayloadV1:
    return SimulationRunPayloadV1(
        snapshot_artifact_id=SNAPSHOT_ID,
        simulation_profile=ProfileRefV1(profile_id="sim", version=1),
        workload_profile=ProfileRefV1(profile_id="wl", version=1),
        replication_count=replication_count,
        horizon_steps=horizon_steps,
    )


def _context(store: FakeArtifactStore, payload: SimulationRunPayloadV1, *, seed=7):
    store.register(SNAPSHOT_ID, _runaway_economy())
    return build_context(
        params=payload,
        kind=SIM_KIND,
        seed=seed,
        resolved_profiles=(
            resolved_binding(
                "/params/simulation_profile", profile_id="sim", version=1, kind="simulation"
            ),
            resolved_binding(
                "/params/workload_profile", profile_id="wl", version=1, kind="workload"
            ),
        ),
    )


def _handler(store: FakeArtifactStore) -> SimulationRunHandler:
    return SimulationRunHandler(blobs=store, store=store)


def test_derive_kwargs_maps_seed_population_and_horizon() -> None:
    payload = _sim_payload(replication_count=12, horizon_steps=33)
    envelope = build_envelope(params=payload, seed=99)
    kwargs = derive_simulation_kwargs(envelope, payload)
    assert kwargs == type(kwargs)(seed=99, n_agents=12, n_ticks=33)


def test_simulation_handler_seals_result_and_simulation_findings() -> None:
    store = FakeArtifactStore()
    outcome = _handler(store)(_context(store, _sim_payload()))

    assert isinstance(outcome, PreparedRunResult)
    assert outcome.summary.outcome_code == "simulation_completed"
    assert outcome.summary.primary_artifact_kind == "simulation_run"
    primary = outcome.artifacts[outcome.primary_index]
    assert primary.payload_schema_id == SIMULATION_RESULT_SCHEMA_ID
    assert primary.version_tuple.seed == 7
    assert outcome.findings, "the runaway economy must produce simulation findings"
    for finding in outcome.findings:
        assert finding.payload.oracle_type == "simulation"
        assert finding.payload.source == "sim"
        assert finding.payload.producer_run_id == "run:1"
        assert finding.evidence_artifact_index == 0
    assert outcome.summary.prepared_finding_count == len(outcome.findings)


def test_simulation_result_payload_is_bounded_to_verdicts() -> None:
    store = FakeArtifactStore()
    outcome = _handler(store)(_context(store, _sim_payload()))
    payload = json.loads(store.read_prepared(outcome.artifacts[0].object_ref))
    # invariant verdicts + sensitivity travel; the full per-tick trajectory does not.
    assert {"invariants", "sensitivity", "findings", "seed"} <= set(payload)
    assert "avg_balance_per_tick" not in payload
    assert payload["seed"] == 7


def test_same_seed_is_byte_identical_different_seed_diverges() -> None:
    a, b, c = FakeArtifactStore(), FakeArtifactStore(), FakeArtifactStore()
    same_a = _handler(a)(_context(a, _sim_payload(), seed=7))
    same_b = _handler(b)(_context(b, _sim_payload(), seed=7))
    other = _handler(c)(_context(c, _sim_payload(), seed=8))
    assert same_a.artifacts[0].payload_hash == same_b.artifacts[0].payload_hash
    assert same_a.artifacts[0].payload_hash != other.artifacts[0].payload_hash


def test_missing_seed_fails_closed() -> None:
    store = FakeArtifactStore()
    store.register(SNAPSHOT_ID, _runaway_economy())
    context = build_context(params=_sim_payload(), kind=SIM_KIND, seed=None)
    with pytest.raises(ValueError):
        _handler(store)(context)
