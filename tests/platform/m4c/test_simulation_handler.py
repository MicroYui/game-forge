"""Task 11a — ``simulation_runner@1`` handler (deterministic economy sim adapter)."""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef
from gameforge.contracts.identity import DomainScope
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.contracts.jobs import PreparedRunResult, SimulationRunPayloadV1
from gameforge.contracts.lineage import VersionTuple
from gameforge.contracts.playtest import ScenarioResetBindingV1, ScenarioSpecV1
from gameforge.platform.run_handlers.simulation import (
    SIMULATION_RESULT_SCHEMA_ID,
    SimulationExecutionBudget,
    SimulationRunHandler,
    derive_simulation_kwargs,
    validate_economy_simulation_work_budget,
)
from gameforge.platform.run_handlers.validation_common import derive_validation_subseed
from gameforge.platform.publication.payload_schema import decode_and_validate_artifact_payload
from gameforge.spine.sim.economy import (
    CollapseReport,
    EconomyModel,
    EconomySimulator,
    InvariantCheck,
    SimResult,
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


@pytest.mark.parametrize(
    "field_path",
    ("/params/simulation_profile", "/params/workload_profile"),
)
def test_simulation_rejects_mismatched_exact_profile_binding_before_execution(
    field_path: str,
) -> None:
    store = FakeArtifactStore()
    context = _context(store, _sim_payload(replication_count=1, horizon_steps=1))
    bindings = tuple(
        resolved_binding(
            binding.field_path,
            profile_id=(
                "other" if binding.field_path == field_path else binding.profile.profile_id
            ),
            version=(9 if binding.field_path == field_path else binding.profile.version),
            kind=("checker" if binding.field_path == field_path else binding.expected_profile_kind),
        )
        for binding in context.payload.resolved_profiles
    )
    context = replace(
        context,
        payload=context.payload.model_copy(update={"resolved_profiles": bindings}),
    )

    with pytest.raises(IntegrityViolation, match="exact Run binding"):
        _handler(store)(context)

    assert store.put_count == 0


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


def test_derive_kwargs_maps_root_seed_replications_and_horizon() -> None:
    payload = _sim_payload(replication_count=12, horizon_steps=33)
    envelope = build_envelope(params=payload, seed=99)
    kwargs = derive_simulation_kwargs(envelope, payload)
    assert kwargs == type(kwargs)(root_seed=99, replication_count=12, n_ticks=33)


def test_every_replication_executes_with_its_exact_subseed() -> None:
    class RecordingSimulator:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int, int]] = []

        def run(self, model, seed, n_agents, n_ticks):
            self.calls.append((seed, n_agents, n_ticks))
            return EconomySimulator().run(model, seed, n_agents, n_ticks)

    store = FakeArtifactStore()
    payload = _sim_payload(replication_count=3, horizon_steps=7)
    simulator = RecordingSimulator()
    outcome = SimulationRunHandler(
        blobs=store,
        store=store,
        simulator=simulator,
    )(_context(store, payload, seed=13))
    report = json.loads(store.read_prepared(outcome.artifacts[0].object_ref))
    snapshot_id = report["snapshot_id"]
    case_id = f"simulation:{snapshot_id}"
    expected = [
        derive_validation_subseed(
            root_seed=13,
            run_kind=SIM_KIND,
            profile=payload.simulation_profile,
            case_id=case_id,
            replication_index=index,
        )
        for index in range(3)
    ]

    assert simulator.calls == [(seed, 1, 7) for seed in expected]
    assert report["seed"] == 13
    assert report["replication_count"] == 3
    assert report["sensitivity"]["seed_binding"] == {
        "root_seed": 13,
        "run_kind": {"kind": "simulation.run", "version": 1},
        "profile_id": "sim",
        "profile_version": 1,
        "case_id": case_id,
        "replication_count": 3,
        "first_child_seed": expected[0],
        "last_child_seed": expected[-1],
        "child_seed_digest": canonical_sha256(
            {
                "seed_derivation_version": "subseed@1",
                "child_seeds": expected,
            }
        ),
        "seed_derivation_version": "subseed@1",
    }


def test_one_collapsed_child_cannot_be_masked_by_a_clean_child_average() -> None:
    class MaskingSimulator:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, model, seed, n_agents, n_ticks):
            del model, seed, n_agents
            assert n_ticks == 10
            self.calls += 1
            # Child one crosses 8x its baseline. Child two's high, flat
            # trajectory makes their pointwise mean stay far below 8x its own
            # baseline, so aggregate-only collapse detection would miss it.
            balances = [1.0] * 5 + [2.0, 3.0, 4.0, 5.0, 9.0] if self.calls == 1 else [100.0] * 10
            return SimResult(
                distributions={
                    "avg_balance_per_tick": balances,
                    "total_source_per_tick": [0.0] * 10,
                    "total_sink_per_tick": [0.0] * 10,
                },
                invariants=[
                    InvariantCheck(
                        name="stable_child_invariant",
                        ok=True,
                        observed=0.0,
                        threshold=1.0,
                    )
                ],
            )

    store = FakeArtifactStore()
    simulator = MaskingSimulator()
    outcome = SimulationRunHandler(
        blobs=store,
        store=store,
        simulator=simulator,
    )(
        _context(
            store,
            _sim_payload(replication_count=2, horizon_steps=10),
            seed=13,
        )
    )

    collapses = [
        finding
        for finding in outcome.findings
        if finding.payload.defect_class == "economy_collapse"
    ]
    assert len(collapses) == 1
    binding = collapses[0].payload.evidence["replication_collapse"]
    assert binding["aggregation"] == "any_replication_collapse@1"
    assert binding["collapsed_replication_count"] == 1
    assert binding["first_collapsed_replication_index"] == 0
    assert binding["earliest_collapse_tick"] == 9
    report = json.loads(store.read_prepared(outcome.artifacts[0].object_ref))
    assert report["sensitivity"]["child_collapse_binding"] == binding


def test_child_collapse_binding_tracks_earliest_warning_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StableSimulator:
        def run(self, model, seed, n_agents, n_ticks):
            del model, seed, n_agents
            return SimResult(
                distributions={
                    "avg_balance_per_tick": [1.0] * n_ticks,
                    "total_source_per_tick": [0.0] * n_ticks,
                    "total_sink_per_tick": [0.0] * n_ticks,
                },
                invariants=[
                    InvariantCheck(
                        name="stable_child_invariant",
                        ok=True,
                        observed=0.0,
                        threshold=1.0,
                    )
                ],
            )

    collapse_reports = iter(
        (
            CollapseReport(
                collapse_tick=4,
                early_warning_tick=3,
                reason="first collapse",
            ),
            CollapseReport(
                collapse_tick=8,
                early_warning_tick=1,
                reason="earlier warning",
            ),
        )
    )
    monkeypatch.setattr(
        "gameforge.platform.run_handlers.simulation.detect_collapse",
        lambda _result: next(collapse_reports),
    )

    store = FakeArtifactStore()
    outcome = SimulationRunHandler(
        blobs=store,
        store=store,
        simulator=StableSimulator(),
    )(
        _context(
            store,
            _sim_payload(replication_count=2, horizon_steps=10),
            seed=13,
        )
    )

    report = json.loads(store.read_prepared(outcome.artifacts[0].object_ref))
    binding = report["sensitivity"]["child_collapse_binding"]
    assert binding["earliest_collapse_tick"] == 4
    assert binding["earliest_warning_tick"] == 1


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
    assert payload["profile"] == {"profile_id": "sim", "version": 1}
    assert payload["sensitivity"]["execution_binding"] == {
        "simulation_profile": {"profile_id": "sim", "version": 1},
        "workload_profile": {"profile_id": "wl", "version": 1},
        "constraint_ids": [],
        "constraint_application": {"status": "not_applicable"},
        "scenario_application": {"status": "not_applicable"},
    }


def test_simulation_result_passes_real_terminal_codec_with_only_finite_numbers() -> None:
    store = FakeArtifactStore()
    outcome = _handler(store)(_context(store, _sim_payload(replication_count=2)))
    blob = store.read_prepared(outcome.artifacts[0].object_ref)

    decoded = decode_and_validate_artifact_payload(
        payload_schema_id=SIMULATION_RESULT_SCHEMA_ID,
        blob=blob,
    )
    assert decoded["payload_schema_version"] == SIMULATION_RESULT_SCHEMA_ID
    assert b"Infinity" not in blob

    invalid = json.loads(blob)
    invalid["invariants"][0]["threshold"] = "f:Infinity"
    with pytest.raises(IntegrityViolation, match="exact registered schema"):
        decode_and_validate_artifact_payload(
            payload_schema_id=SIMULATION_RESULT_SCHEMA_ID,
            blob=canonical_json(invalid).encode("utf-8"),
        )

    invalid = json.loads(blob)
    invalid["sensitivity"]["sink_source_ratio"] = "f:Infinity"
    with pytest.raises(IntegrityViolation, match="exact registered schema"):
        decode_and_validate_artifact_payload(
            payload_schema_id=SIMULATION_RESULT_SCHEMA_ID,
            blob=canonical_json(invalid).encode("utf-8"),
        )


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
    context = build_context(
        params=_sim_payload(),
        kind=SIM_KIND,
        seed=None,
        resolved_profiles=(
            resolved_binding(
                "/params/simulation_profile", profile_id="sim", version=1, kind="simulation"
            ),
            resolved_binding(
                "/params/workload_profile", profile_id="wl", version=1, kind="workload"
            ),
        ),
    )
    with pytest.raises(ValueError):
        _handler(store)(context)


def test_profile_execution_budget_rejects_extreme_legal_shape_before_simulator() -> None:
    class NeverCalledSimulator:
        calls = 0

        def run(self, model, seed, n_agents, n_ticks):
            self.calls += 1
            raise AssertionError("simulator must not run")

    store = FakeArtifactStore()
    simulator = NeverCalledSimulator()
    handler = SimulationRunHandler(
        blobs=store,
        store=store,
        simulator=simulator,
    )
    payload = _sim_payload(
        replication_count=100_000,
        horizon_steps=100_000_000,
    )

    with pytest.raises(IntegrityViolation, match="execution budget"):
        handler(_context(store, payload, seed=7))

    assert simulator.calls == 0
    assert store.put_count == 0


def test_simulation_work_budget_accounts_for_source_inner_loop_before_simulator() -> None:
    class NeverCalledSimulator:
        calls = 0

        def run(self, model, seed, n_agents, n_ticks):
            del model, seed, n_agents, n_ticks
            self.calls += 1
            raise AssertionError("simulator must not run")

    currency = Entity(id="gold", type=NodeType.CURRENCY, attrs={})
    monster = Entity(
        id="mob",
        type=NodeType.MONSTER,
        attrs={"gold_min": 1, "gold_max": 1, "kills_per_tick": 1_000},
    )
    drop = Relation(id="d1", type=EdgeType.DROPS_FROM, src_id="mob", dst_id="gold")
    store = FakeArtifactStore()
    context = _context(store, _sim_payload(replication_count=1, horizon_steps=10))
    store.register(SNAPSHOT_ID, snapshot_bytes([currency, monster], [drop]))
    simulator = NeverCalledSimulator()
    handler = SimulationRunHandler(
        blobs=store,
        store=store,
        simulator=simulator,
        execution_budget_resolver=lambda _simulation, _workload: SimulationExecutionBudget(
            max_replication_count=100,
            max_horizon_steps=100,
            max_output_ticks=100,
            max_total_replication_ticks=10_000,
            max_total_work_units=1_000,
        ),
    )

    with pytest.raises(IntegrityViolation, match="work budget"):
        handler(context)

    assert simulator.calls == 0
    assert store.put_count == 0


def test_simulation_work_budget_counts_zero_kill_source_iteration() -> None:
    model = EconomyModel(
        sources=[{"kills_per_tick": 0} for _index in range(1_001)],
    )

    with pytest.raises(IntegrityViolation, match="work budget"):
        validate_economy_simulation_work_budget(
            model,
            n_agents=1,
            n_ticks=1,
            replication_count=1,
            max_work_units=1_000,
        )


def test_finite_child_values_whose_ratio_overflows_fail_before_blob_write() -> None:
    class OverflowRatioSimulator:
        def run(self, model, seed, n_agents, n_ticks):
            del model, seed, n_agents
            assert n_ticks == 1
            return SimResult(
                distributions={
                    "avg_balance_per_tick": [0.0],
                    "total_source_per_tick": [1e-308],
                    "total_sink_per_tick": [1e308],
                },
                invariants=[
                    InvariantCheck(
                        name="finite",
                        ok=True,
                        observed=0.0,
                        threshold=1.0,
                    )
                ],
                sensitivity={},
            )

    store = FakeArtifactStore()
    handler = SimulationRunHandler(
        blobs=store,
        store=store,
        simulator=OverflowRatioSimulator(),
    )

    with pytest.raises(IntegrityViolation, match="ratio is not finite"):
        handler(_context(store, _sim_payload(replication_count=1, horizon_steps=1)))

    assert store.put_count == 0


def test_constraint_and_scenario_inputs_are_parsed_and_bound_into_evidence() -> None:
    constraint_id = "artifact:constraint"
    scenario_id = "artifact:scenario"
    store = FakeArtifactStore()
    store.register(SNAPSHOT_ID, _runaway_economy())
    store.register(
        constraint_id,
        {
            "dsl_grammar_version": "dsl@1",
            "constraints": [
                {
                    "id": "C_gold_cap",
                    "dsl_grammar_version": "dsl@1",
                    "kind": "numeric",
                    "oracle": "deterministic",
                    "predicates": [],
                    "assert": "reward_gold <= 80",
                    "severity": "major",
                }
            ],
        },
    )
    reset_payload = {"spawn": "outpost"}
    scenario = ScenarioSpecV1(
        scenario_id="scenario:economy",
        source_preview_artifact_id=SNAPSHOT_ID,
        config_export_artifact_id="artifact:config",
        constraint_snapshot_artifact_id=constraint_id,
        environment_profile=ProfileRefV1(profile_id="env", version=1),
        env_contract_version="env@1",
        domain_scope=DomainScope(domain_ids=("economy",)),
        reset_binding=ScenarioResetBindingV1(
            reset_schema_id="reset@1",
            payload_hash=canonical_sha256(reset_payload),
            payload=reset_payload,
        ),
    )
    store.register(scenario_id, scenario.model_dump(mode="json"))
    params = _sim_payload().model_copy(
        update={
            "constraint_snapshot_artifact_id": constraint_id,
            "scenario_artifact_id": scenario_id,
        }
    )
    context = build_context(
        params=params,
        kind=SIM_KIND,
        seed=7,
        resolved_profiles=(
            resolved_binding(
                "/params/simulation_profile", profile_id="sim", version=1, kind="simulation"
            ),
            resolved_binding(
                "/params/workload_profile", profile_id="wl", version=1, kind="workload"
            ),
        ),
        version_tuple=VersionTuple(
            constraint_snapshot_id="constraint:semantic:1",
            env_contract_version="env@1",
            seed=7,
            tool_version="handler@1",
        ),
    )
    outcome = _handler(store)(context)
    report = json.loads(store.read_prepared(outcome.artifacts[0].object_ref))
    assert report["sensitivity"]["execution_binding"]["constraint_ids"] == ["C_gold_cap"]
    assert report["sensitivity"]["execution_binding"]["scenario_id"] == "scenario:economy"
    assert report["sensitivity"]["execution_binding"]["constraint_snapshot_artifact_id"] == (
        constraint_id
    )
    assert report["sensitivity"]["execution_binding"]["scenario_artifact_id"] == scenario_id
    assert report["sensitivity"]["execution_binding"]["constraint_application"] == {
        "status": "unproven",
        "reason_code": "constraint_profile_not_executable",
    }
    assert report["sensitivity"]["execution_binding"]["scenario_application"] == {
        "status": "unproven",
        "reason_code": "scenario_reset_not_executable",
    }
    assert {
        (finding.payload.defect_class, finding.payload.status) for finding in outcome.findings
    } >= {
        ("simulation_constraint_unproven", "unproven"),
        ("simulation_scenario_unproven", "unproven"),
    }
    assert outcome.artifacts[0].version_tuple.constraint_snapshot_id == "constraint:semantic:1"
    assert outcome.artifacts[0].version_tuple.env_contract_version == "env@1"
