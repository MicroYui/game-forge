"""Hash-bound runtime evidence for the deterministic seeded pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from gameforge.bench.corpus import build_corpus
from gameforge.bench.metrics import default_constraints
from gameforge.bench.runtime_evidence import (
    DeterministicRuntimeEvidence,
    canonical_runtime_evidence_bytes,
    capture_runtime_environment,
    constraints_sha256,
    load_runtime_evidence,
    measure_runtime,
    validate_runtime_evidence,
    write_runtime_evidence,
)
from gameforge.bench.taxonomy import DefectClass


def _small_corpus():
    per_class_n = dict.fromkeys(DefectClass, 0)
    per_class_n[DefectClass.dangling_reference] = 1
    per_class_n[DefectClass.economy_collapse] = 1
    per_class_n[DefectClass.spoiler] = 1
    return build_corpus(seed=7, per_class_n=per_class_n, n_clean=3)


def _clock(step: int = 10):
    value = -step

    def read() -> int:
        nonlocal value
        value += step
        return value

    return read


def _evidence():
    return measure_runtime(
        _small_corpus(),
        default_constraints(),
        seed=7,
        clock_ns=_clock(),
    )


def test_runtime_measurement_times_setup_once_and_each_sample_once():
    evidence = _evidence()

    assert evidence.setup_elapsed_ns == 10
    assert len(evidence.samples) == 3
    assert tuple(item.elapsed_ns for item in evidence.samples) == (10, 10, 10)
    assert tuple(item.sample_id for item in evidence.samples) == (
        "dangling_reference-0000",
        "economy_collapse-0000",
        "clean-0000",
    )
    assert tuple(item.bucket for item in evidence.samples) == (
        "deterministic",
        "simulation",
        "clean",
    )
    assert evidence.per_sample_ms.evaluated_n == 3
    assert evidence.per_sample_ms.mean == pytest.approx(0.00001)
    assert evidence.per_sample_ms.status == "measured"


def test_runtime_excludes_narrative_and_deduplicates_clean_snapshots():
    evidence = _evidence()

    assert evidence.per_class_n[DefectClass.spoiler] == 0
    assert evidence.per_class_n[DefectClass.dangling_reference] == 1
    assert evidence.per_class_n[DefectClass.economy_collapse] == 1
    assert evidence.distinct_clean_n == 1
    assert all(
        item.defect_class is not DefectClass.spoiler for item in evidence.samples
    )


def test_constraints_hash_is_content_bound_order_and_path_independent():
    constraints = default_constraints()
    expected = constraints_sha256(constraints)

    assert constraints_sha256(tuple(reversed(constraints))) == expected
    changed = list(constraints)
    changed[0] = changed[0].model_copy(update={"note": "changed"})
    assert constraints_sha256(changed) != expected


def test_runtime_environment_binds_required_solver_versions():
    environment = capture_runtime_environment()
    versions = {item.component: item.version for item in environment.package_versions}

    assert {"clingo", "pydantic", "z3-solver"} <= set(versions)
    assert environment.perf_counter_resolution_ns > 0
    assert environment.tool_version == "gameforge@0.0.0"


def test_runtime_manifest_rejects_environment_sample_and_duration_tampering():
    evidence = _evidence()

    sample_payload = evidence.model_dump(mode="json")
    sample_payload["samples"][0]["elapsed_ns"] += 1
    with pytest.raises(ValidationError):
        DeterministicRuntimeEvidence.model_validate(sample_payload)

    environment_payload = evidence.model_dump(mode="json")
    environment_payload["environment"]["machine"] = "tampered"
    with pytest.raises(ValidationError):
        DeterministicRuntimeEvidence.model_validate(environment_payload)

    zero_payload = evidence.model_dump(mode="json")
    zero_payload["samples"][0]["elapsed_ns"] = 0
    with pytest.raises(ValidationError):
        DeterministicRuntimeEvidence.model_validate(zero_payload)


def test_runtime_evidence_round_trips_and_revalidates_constraints(tmp_path: Path):
    constraints = default_constraints()
    evidence = measure_runtime(
        _small_corpus(),
        constraints,
        seed=7,
        clock_ns=_clock(),
    )
    destination = tmp_path / "runtime-evidence.json"
    write_runtime_evidence(destination, evidence)

    loaded = load_runtime_evidence(destination)
    validate_runtime_evidence(loaded, constraints=constraints)

    assert loaded == evidence
    assert destination.read_bytes() == canonical_runtime_evidence_bytes(evidence)

    changed = list(constraints)
    changed[0] = changed[0].model_copy(update={"note": "changed"})
    with pytest.raises(ValueError, match="constraints"):
        validate_runtime_evidence(loaded, constraints=changed)
