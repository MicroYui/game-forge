"""Acceptance for committed Agent-cost and deterministic-runtime evidence."""

from __future__ import annotations

from pathlib import Path

from gameforge.bench.cost_latency import (
    canonical_evidence_bytes,
    load_evidence as load_agent_cost,
    validate_agent_cost_evidence,
)
from gameforge.bench.metrics import default_constraints
from gameforge.bench.runtime_evidence import (
    canonical_runtime_evidence_bytes,
    load_runtime_evidence,
    main as runtime_main,
    validate_runtime_evidence,
)
from gameforge.bench.taxonomy import CLASS_META, Bucket, DefectClass
from gameforge.contracts.model_router import ModelSnapshot

_ROOT = Path(__file__).parents[2]
_COST = _ROOT / "scenarios/bench/agent-cost-latency-evidence.json"
_RUNTIME = _ROOT / "scenarios/bench/deterministic-runtime-evidence.json"

_CURRENT = ModelSnapshot(
    provider="openai",
    model="gpt-5.6-sol",
    snapshot_tag="pre-m4@1",
)
_HISTORICAL = ModelSnapshot(
    provider="anthropic",
    model="claude-opus-4-8",
    snapshot_tag="m2a@1",
)


def test_measured_agent_cost_reaggregates_every_referenced_cassette():
    evidence = load_agent_cost(_COST)

    validate_agent_cost_evidence(evidence, repo_root=_ROOT)

    assert _COST.read_bytes() == canonical_evidence_bytes(evidence)
    workloads = {item.workload_id: item for item in evidence.workloads}
    assert set(workloads) == {
        "external-hed",
        "narrative-verification",
        "playtest-flat",
        "playtest-layered",
        "playtest-memory-on",
        "repair-search",
    }
    assert workloads["narrative-verification"].evaluated_n == 1905
    assert workloads["narrative-verification"].logical_requests == 5715
    assert workloads["external-hed"].evaluated_n == 8
    assert workloads["external-hed"].logical_requests == 14
    assert workloads["external-hed"].recorded_requests == 10
    assert workloads["repair-search"].evaluated_n == 10
    for workload_id in (
        "narrative-verification",
        "external-hed",
        "repair-search",
    ):
        assert workloads[workload_id].model_snapshot == _CURRENT
    for workload_id in (
        "playtest-layered",
        "playtest-flat",
        "playtest-memory-on",
    ):
        assert workloads[workload_id].evaluated_n == 20
        assert workloads[workload_id].model_snapshot == _HISTORICAL
    assert all(item.tokens.reported_total_tokens > 0 for item in workloads.values())
    assert all(item.request_latency_ms.mean > 0 for item in workloads.values())


def test_measured_runtime_binds_full_controlled_workload_and_environment():
    evidence = load_runtime_evidence(_RUNTIME)
    constraints = default_constraints(str(_ROOT / "scenarios/constraints"))

    validate_runtime_evidence(evidence, constraints=constraints)

    assert _RUNTIME.read_bytes() == canonical_runtime_evidence_bytes(evidence)
    deterministic_n = sum(
        count
        for defect, count in evidence.per_class_n.items()
        if CLASS_META[defect].bucket is not Bucket.llm_assisted
    )
    assert deterministic_n == 902
    assert all(
        evidence.per_class_n[defect] == 0
        for defect in DefectClass
        if CLASS_META[defect].bucket is Bucket.llm_assisted
    )
    assert evidence.distinct_clean_n == 1
    assert len(evidence.samples) == 903
    assert evidence.per_sample_ms.evaluated_n == 903
    assert evidence.setup_elapsed_ns > 0
    versions = {
        item.component: item.version
        for item in evidence.environment.package_versions
    }
    assert {"clingo", "pydantic", "z3-solver"} <= set(versions)


def test_runtime_cli_revalidates_existing_measurement_without_retiming():
    assert (
        runtime_main(
            [
                "--validate",
                str(_RUNTIME),
                "--constraints",
                str(_ROOT / "scenarios/constraints"),
            ]
        )
        == 0
    )
