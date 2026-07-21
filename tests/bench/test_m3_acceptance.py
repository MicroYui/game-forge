"""Combined, source-cross-checked M3 product acceptance gates."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from gameforge.bench.acceptance import (
    M3EvidenceBundle,
    build_report_view_hashes,
    validate_m3_acceptance,
)
from gameforge.bench.cost_latency import (
    AgentCostLatencyEvidence,
    AgentRequestSample,
    AgentWorkloadEvidence,
    canonical_evidence_bytes as canonical_cost_bytes,
)
from gameforge.bench.corpus import default_per_class_n
from gameforge.bench.external_cases.contracts import canonical_bytes as external_bytes
from gameforge.bench.external_cases.qualify import load_manifest as load_external
from gameforge.bench.hed.contracts import (
    canonical_evidence_bytes as canonical_hed_bytes,
    load_evidence as load_hed,
)
from gameforge.bench.hed.protocol import load_protocol as load_hed_protocol
from gameforge.bench.metrics import FPReport, Metric, SeededScore
from gameforge.bench.narrative.corpus import load_manifest as load_narrative_corpus
from gameforge.bench.narrative.evidence import (
    canonical_evidence_bytes as canonical_narrative_bytes,
)
from gameforge.bench.narrative.harness import load_evidence as load_narrative
from gameforge.bench.narrative.protocol import load_protocol as load_narrative_protocol
from gameforge.bench.qa.contracts import QaEvent, QaSessionEvidence, seal_qa_verdict
from gameforge.bench.qa.protocol import (
    canonical_protocol_bytes as canonical_qa_protocol_bytes,
    load_protocol as load_qa_protocol,
)
from gameforge.bench.qa.score import (
    canonical_evidence_bytes as canonical_qa_bytes,
    seal_qa_evidence,
)
from gameforge.bench.report import (
    ReportEvidenceBundle,
    build_bench_report,
    build_qa_section,
)
from gameforge.bench.report_contracts import (
    BinaryMetric,
    DistributionMetric,
    EvidenceArtifactRef,
    TokenTotals,
)
from gameforge.bench.runtime_evidence import (
    DeterministicRuntimeEvidence,
    canonical_runtime_evidence_bytes,
    capture_runtime_environment,
)
from gameforge.bench.taxonomy import CLASS_META, Bucket, DefectClass
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.spine.stats import wilson_ci

_ROOT = Path(__file__).parents[2]
_EXTERNAL_ROOT = _ROOT / "scenarios/external_cases/endless_sky"
_NARRATIVE_ROOT = _ROOT / "scenarios/narrative_bench"
_SECOND = 1_000_000_000

GPT_56 = ModelSnapshot(
    provider="openai",
    model="gpt-5.6-sol",
    snapshot_tag="pre-m4@1",
)
OPUS_M2 = ModelSnapshot(
    provider="anthropic",
    model="claude-opus-4-8",
    snapshot_tag="m2a@1",
)

_WORKLOADS = {
    "external-hed": (8, 14, 10, GPT_56),
    "narrative-verification": (1905, 5715, 5715, GPT_56),
    "playtest-flat": (20, 20, 20, OPUS_M2),
    "playtest-layered": (20, 20, 20, OPUS_M2),
    "playtest-memory-on": (20, 20, 20, OPUS_M2),
    "repair-search": (10, 10, 10, GPT_56),
}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _distribution(
    name: str,
    unit: str,
    bucket: str,
    *,
    n: int,
    value: float,
) -> DistributionMetric:
    return DistributionMetric.measured(
        name=name,
        unit=unit,
        bucket=bucket,
        planned_n=n,
        evaluated_n=n,
        mean=value,
        median=value,
        p95=value,
        primary_estimate=value,
        ci_low=value,
        ci_high=value,
        ci_method="percentile-bootstrap95",
        status="measured",
    )


def _counts(total: int, n: int) -> list[int]:
    values = [total // n] * n
    for index in range(total % n):
        values[index] += 1
    return values


def _request_hash(workload_id: str, sample_index: int, request_index: int) -> str:
    raw = f"{workload_id}:{sample_index}:{request_index}".encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _workload(
    workload_id: str,
    *,
    source_sha256: str,
) -> AgentWorkloadEvidence:
    n, logical_total, recorded_total, snapshot = _WORKLOADS[workload_id]
    logical_counts = _counts(logical_total, n)
    recorded_counts = _counts(recorded_total, n)
    samples: list[AgentRequestSample] = []
    for index, (logical_n, recorded_n) in enumerate(
        zip(logical_counts, recorded_counts, strict=True)
    ):
        hashes = tuple(
            _request_hash(workload_id, index, request_index) for request_index in range(recorded_n)
        )
        logical = (*hashes, *((hashes[0],) * (logical_n - recorded_n)))
        historical = snapshot == OPUS_M2
        samples.append(
            AgentRequestSample(
                sample_id=f"{workload_id}-{index:04d}",
                logical_request_hashes=logical,
                recorded_request_hashes=hashes,
                cassette_sha256s=tuple("a" * 64 for _ in hashes),
                recorded_request_latencies_ms=tuple(1 for _ in hashes),
                logical_requests=logical_n,
                recorded_requests=recorded_n,
                session_cache_reuses=logical_n - recorded_n,
                tokens=TokenTotals(
                    input_tokens=8,
                    output_tokens=2,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    reported_total_tokens=10,
                ),
                recorded_latency_ms=recorded_n,
                known_transport_attempts=0 if historical else recorded_n,
                known_transport_retries=0,
                unknown_transport_attempt_records=recorded_n if historical else 0,
            )
        )
    return AgentWorkloadEvidence.model_construct(
        workload_id=workload_id,
        model_snapshot=snapshot,
        cassette_root="cassettes/synthetic-acceptance",
        protocol_id=f"{workload_id}@1",
        source_evidence_sha256=source_sha256,
        planned_n=n,
        evaluated_n=n,
        samples=tuple(samples),
        tokens=TokenTotals(
            input_tokens=8 * n,
            output_tokens=2 * n,
            cache_read_tokens=0,
            cache_write_tokens=0,
            reported_total_tokens=10 * n,
        ),
        tokens_per_sample=_distribution(
            "tokens_per_sample",
            "tokens",
            "agent_cost",
            n=n,
            value=10.0,
        ),
        request_latency_ms=_distribution(
            "request_latency_ms",
            "milliseconds",
            "agent_latency",
            n=recorded_total,
            value=1.0,
        ),
        logical_requests=logical_total,
        recorded_requests=recorded_total,
        session_cache_reuses=logical_total - recorded_total,
        known_transport_attempts=0 if snapshot == OPUS_M2 else recorded_total,
        known_transport_retries=0,
        unknown_transport_attempt_records=(recorded_total if snapshot == OPUS_M2 else 0),
        monetary_status="unavailable",
        price_book_ref=None,
        workload_sha256="b" * 64,
    )


def _cost_evidence(narrative_sha256: str, hed_sha256: str):
    rows = []
    for workload_id in sorted(_WORKLOADS):
        source_sha256 = (
            narrative_sha256
            if workload_id == "narrative-verification"
            else hed_sha256
            if workload_id == "external-hed"
            else "c" * 64
        )
        rows.append(_workload(workload_id, source_sha256=source_sha256))
    return AgentCostLatencyEvidence.model_construct(
        schema_version="agent-cost-latency-evidence@1",
        workloads=tuple(rows),
        evidence_sha256="d" * 64,
    )


def _runtime_evidence() -> DeterministicRuntimeEvidence:
    per_class_n = {
        defect: (
            0 if CLASS_META[defect].bucket is Bucket.llm_assisted else default_per_class_n()[defect]
        )
        for defect in DefectClass
    }
    n = sum(per_class_n.values()) + 1
    return DeterministicRuntimeEvidence.model_construct(
        schema_version="deterministic-runtime-evidence@1",
        workload_id="seeded-checker-sim-pipeline",
        seed=0,
        per_class_n=per_class_n,
        distinct_clean_n=1,
        constraints_sha256="e" * 64,
        setup_elapsed_ns=1_000_000,
        samples=(),
        per_sample_ms=_distribution(
            "deterministic_pipeline_runtime_ms",
            "milliseconds",
            "deterministic_runtime",
            n=n,
            value=1.0,
        ),
        environment=capture_runtime_environment(),
        evidence_sha256="f" * 64,
    )


def _qa_evidence(protocol):  # noqa: ANN001
    def verdict():
        return seal_qa_verdict(
            correct=True,
            reader_round_trip=True,
            native_exit_code=0,
            predicate_status="clear",
            target_finding_clear=True,
            target_entities_preserved=True,
            new_deterministic_findings=(),
            submitted_tree_sha256="1" * 64,
            failure_reason=None,
        )

    sessions = []
    for spec in protocol.sessions:
        seconds = 300 if spec.arm == "manual" else 60
        sessions.append(
            QaSessionEvidence.seal(
                protocol_sha256=protocol.protocol_sha256,
                session_id=spec.session_id,
                participant_id="participant-01",
                case_id=spec.case_id,
                pair_id=spec.pair_id,
                arm=spec.arm,
                order=spec.order,
                events=(
                    QaEvent(kind="start", monotonic_ns=1),
                    QaEvent(kind="finish", monotonic_ns=seconds * _SECOND + 1),
                ),
                final_patch_path=f"qa-patches/{spec.session_id}.patch",
                final_patch_sha256=f"{spec.order:064x}",
                participant_attested_no_contamination=True,
                verdict=verdict(),
            )
        )
    return seal_qa_evidence(protocol, sessions)


def _seeded_score() -> SeededScore:
    rows = []
    for defect in DefectClass:
        if CLASS_META[defect].bucket is Bucket.llm_assisted:
            continue
        n = default_per_class_n()[defect]
        low, high = wilson_ci(n, n)
        rows.append(
            Metric(
                name="bdr",
                defect_class=defect.value,
                n=n,
                k=n,
                rate=1.0,
                ci_low=low,
                ci_high=high,
                bucket=CLASS_META[defect].bucket.value,
            )
        )
    oracle_low, oracle_high = wilson_ci(0, 1)
    constraint_low, constraint_high = wilson_ci(0, 902)
    return SeededScore(
        bdr=rows,
        oracle_fp=FPReport(1, 0, 0.0, oracle_low, oracle_high),
        constraint_fp=FPReport(
            902,
            0,
            0.0,
            constraint_low,
            constraint_high,
        ),
    )


def _artifact(
    evidence_id: str,
    path: str,
    schema_version: str,
    raw: bytes,
) -> EvidenceArtifactRef:
    return EvidenceArtifactRef(
        evidence_id=evidence_id,
        path=path,
        sha256=_sha256(raw),
        schema_version=schema_version,
        available=True,
    )


@pytest.fixture(scope="module")
def complete():
    external = load_external(_EXTERNAL_ROOT / "external-corpus-manifest.json")
    narrative_protocol = load_narrative_protocol(_NARRATIVE_ROOT / "protocol.json")
    narrative_corpus = load_narrative_corpus(_NARRATIVE_ROOT / "corpus-manifest.json")
    narrative = load_narrative(_NARRATIVE_ROOT / "verification-evidence.json")
    hed_protocol = load_hed_protocol(_EXTERNAL_ROOT / "hed-protocol.json")
    hed = load_hed(_EXTERNAL_ROOT / "hed-evidence.json")
    qa_protocol = load_qa_protocol(_EXTERNAL_ROOT / "qa-protocol.json")
    qa = _qa_evidence(qa_protocol)
    cost = _cost_evidence(narrative.evidence_sha256, hed.evidence_sha256)
    runtime = _runtime_evidence()
    artifacts = (
        _artifact(
            "agent-cost",
            "scenarios/bench/agent-cost-latency-evidence.json",
            cost.schema_version,
            canonical_cost_bytes(cost),
        ),
        _artifact(
            "external",
            "scenarios/external_cases/source/external-corpus-manifest.json",
            external.schema_version,
            external_bytes(external),
        ),
        _artifact(
            "hed",
            "scenarios/external_cases/source/hed-evidence.json",
            hed.schema_version,
            canonical_hed_bytes(hed),
        ),
        _artifact(
            "narrative",
            "scenarios/narrative_bench/verification-evidence.json",
            narrative.schema_version,
            canonical_narrative_bytes(narrative),
        ),
        _artifact(
            "qa",
            "scenarios/external_cases/source/qa-evidence.json",
            qa.schema_version,
            canonical_qa_bytes(qa),
        ),
        _artifact(
            "qa-protocol",
            "scenarios/external_cases/source/qa-protocol.json",
            qa_protocol.schema_version,
            canonical_qa_protocol_bytes(qa_protocol),
        ),
        _artifact(
            "runtime",
            "scenarios/bench/deterministic-runtime-evidence.json",
            runtime.schema_version,
            canonical_runtime_evidence_bytes(runtime),
        ),
        _artifact(
            "seeded",
            "gameforge/bench/corpus.py",
            "seeded-corpus-code@1",
            (_ROOT / "gameforge/bench/corpus.py").read_bytes(),
        ),
    )
    report_bundle = ReportEvidenceBundle(
        external=external,
        narrative_protocol=narrative_protocol,
        narrative_corpus=narrative_corpus,
        narrative=narrative,
        hed_protocol=hed_protocol,
        hed=hed,
        qa_protocol=qa_protocol,
        qa=qa,
        agent_cost=cost,
        deterministic_runtime=runtime,
        evidence=tuple(sorted(artifacts, key=lambda item: item.evidence_id)),
    )
    per_class_n = default_per_class_n()
    report = build_bench_report(
        seed=0,
        corpus_size=sum(per_class_n.values()),
        per_class_n=per_class_n,
        seeded_score=_seeded_score(),
        agent_metrics=(
            Metric(
                "fix_pass_rate",
                None,
                10,
                10,
                1.0,
                *wilson_ci(10, 10),
                "agent",
            ),
        ),
        evidence_bundle=report_bundle,
    )
    # Cost/runtime internals are contract-tested separately. This fixture keeps
    # their full acceptance denominators without replaying 1,905 model samples
    # or timing 903 checker samples in every unit-test process.
    evidence = M3EvidenceBundle.model_construct(
        external=external,
        narrative=narrative,
        hed=hed,
        qa_protocol=qa_protocol,
        qa=qa,
        agent_cost=cost,
        deterministic_runtime=runtime,
        artifacts=tuple(sorted(artifacts, key=lambda item: item.evidence_id)),
        views=build_report_view_hashes(report),
    )
    return report, evidence, qa_protocol


def _codes(report, evidence):  # noqa: ANN001
    return {item.code for item in validate_m3_acceptance(report, evidence)}


def _with_report(evidence, report):  # noqa: ANN001
    return evidence.model_copy(update={"views": build_report_view_hashes(report)})


def test_complete_m3_bundle_has_no_gate_failures(complete):
    report, evidence, _ = complete

    assert validate_m3_acceptance(report, evidence) == ()


def test_current_missing_real_qa_evidence_is_one_specific_failure(complete):
    report, evidence, qa_protocol = complete
    qa_ref = next(item for item in report.evidence if item.evidence_id == "qa")
    pending_ref = qa_ref.model_copy(update={"available": False, "sha256": None})
    artifacts = tuple(
        pending_ref if item.evidence_id == "qa" else item for item in evidence.artifacts
    )
    report_refs = tuple(
        pending_ref if item.evidence_id == "qa" else item for item in report.evidence
    )
    pending = report.model_copy(
        update={
            "qa": build_qa_section(qa_protocol, None),
            "evidence": report_refs,
        }
    )
    missing = evidence.model_copy(
        update={
            "qa": None,
            "artifacts": artifacts,
            "views": build_report_view_hashes(pending),
        }
    )

    failures = validate_m3_acceptance(pending, missing)

    assert [item.code for item in failures] == ["qa.evidence_missing"]


def test_seeded_corpus_size_and_complete_evaluated_taxonomy_are_gated(complete):
    report, evidence, _ = complete
    too_small = report.model_copy(
        update={"meta": report.meta.model_copy(update={"corpus_size": 499})}
    )
    missing_class = report.model_copy(update={"seeded": report.seeded[1:]})
    pending = report.model_copy(
        update={
            "seeded": (
                BinaryMetric.pending(
                    name="bdr",
                    defect_class=report.seeded[0].defect_class,
                    bucket=report.seeded[0].bucket,
                    planned_n=report.seeded[0].planned_n,
                    protocol_id=report.seeded[0].protocol_id,
                    evidence_ref=report.seeded[0].evidence_ref,
                ),
                *report.seeded[1:],
            )
        }
    )

    assert "corpus.size_below_minimum" in _codes(too_small, _with_report(evidence, too_small))
    assert "corpus.class_missing" in _codes(missing_class, _with_report(evidence, missing_class))
    assert "corpus.metric_not_evaluated" in _codes(pending, _with_report(evidence, pending))


def test_narrative_denominators_clean_controls_and_power_are_gated(complete):
    report, evidence, _ = complete
    short_class = evidence.narrative.by_class[0].model_copy(update={"n": 380})
    short_narrative = evidence.narrative.model_copy(
        update={"by_class": (short_class, *evidence.narrative.by_class[1:])}
    )
    short_clean = evidence.narrative.model_copy(
        update={"clean_fp": evidence.narrative.clean_fp.model_copy(update={"n": 380})}
    )
    narrative_power_index = next(
        index
        for index, item in enumerate(report.power)
        if CLASS_META[item.defect_class].bucket is Bucket.llm_assisted
    )
    weak = report.power[narrative_power_index].model_copy(
        update={"achieved_half_width": 0.06, "status": "underpowered"}
    )
    weak_power = report.model_copy(
        update={
            "power": (
                *report.power[:narrative_power_index],
                weak,
                *report.power[narrative_power_index + 1 :],
            )
        }
    )

    assert "narrative.positive_denominator" in _codes(
        report, evidence.model_copy(update={"narrative": short_narrative})
    )
    assert "narrative.clean_denominator" in _codes(
        report, evidence.model_copy(update={"narrative": short_clean})
    )
    assert "narrative.power_under_target" in _codes(weak_power, _with_report(evidence, weak_power))


def test_deterministic_oracle_false_positive_must_be_zero(complete):
    report, evidence, _ = complete
    rows = list(report.false_positives)
    index = next(i for i, item in enumerate(rows) if item.name == "oracle_fp")
    source = rows[index]
    rows[index] = BinaryMetric.wilson(
        name=source.name,
        bucket=source.bucket,
        planned_n=source.planned_n,
        evaluated_n=source.evaluated_n,
        k=1,
        status="measured",
        protocol_id=source.protocol_id,
        evidence_ref=source.evidence_ref,
    )
    changed = report.model_copy(update={"false_positives": tuple(rows)})

    assert "false_positive.deterministic_nonzero" in _codes(
        changed, _with_report(evidence, changed)
    )


def test_external_case_class_hit_clear_and_after_fp_gates(complete):
    report, evidence, _ = complete
    short = evidence.external.model_copy(update={"cases": evidence.external.cases[:-1]})
    miss = evidence.external.verification[0].model_copy(update={"k": 0, "rate": 0.0})
    missing_hit = evidence.external.model_copy(
        update={"verification": (miss, *evidence.external.verification[1:])}
    )
    verification_index = next(
        index
        for index, item in enumerate(evidence.external.cases)
        if item.spec.split == "verification"
    )
    case = evidence.external.cases[verification_index]
    not_clear = case.model_copy(update={"findings_after": case.findings_before})
    cases = list(evidence.external.cases)
    cases[verification_index] = not_clear
    uncleared = evidence.external.model_copy(update={"cases": tuple(cases)})
    nonzero_fp = evidence.external.model_copy(
        update={
            "after_oracle_fp": evidence.external.after_oracle_fp.model_copy(
                update={"count": 1, "rate": 0.125}
            )
        }
    )

    assert "external.case_count" in _codes(report, evidence.model_copy(update={"external": short}))
    assert "external.verification_hit_missing" in _codes(
        report, evidence.model_copy(update={"external": missing_hit})
    )
    assert "external.after_clear_missing" in _codes(
        report, evidence.model_copy(update={"external": uncleared})
    )
    assert "external.after_oracle_fp_nonzero" in _codes(
        report, evidence.model_copy(update={"external": nonzero_fp})
    )


def test_hed_full_denominator_protocol_and_human_target_are_gated(complete):
    report, evidence, _ = complete
    short = evidence.hed.model_copy(update={"outcomes": evidence.hed.outcomes[:-1]})
    failed_outcome = evidence.hed.outcomes[0].model_copy(
        update={"status": "protocol_failure", "disposition": "protocol_failure"}
    )
    protocol_failed = evidence.hed.model_copy(
        update={"outcomes": (failed_outcome, *evidence.hed.outcomes[1:])}
    )
    missing_delta = evidence.hed.outcomes[0].model_copy(update={"human_delta": ()})
    missing_human = evidence.hed.model_copy(
        update={"outcomes": (missing_delta, *evidence.hed.outcomes[1:])}
    )

    assert "hed.outcome_count" in _codes(report, evidence.model_copy(update={"hed": short}))
    assert "hed.protocol_failure" in _codes(
        report, evidence.model_copy(update={"hed": protocol_failed})
    )
    assert "hed.human_target_missing" in _codes(
        report, evidence.model_copy(update={"hed": missing_human})
    )


def test_qa_requires_four_valid_pairs_and_both_real_arms(complete):
    report, evidence, _ = complete
    score = evidence.qa.score.model_copy(
        update={
            "evaluated_pairs": 3,
            "protocol_failure_pairs": 1,
            "pairs": evidence.qa.score.pairs[:3],
        }
    )
    invalid = evidence.qa.model_copy(update={"score": score})
    contaminated = evidence.qa.sessions[0].model_copy(
        update={
            "participant_attested_no_contamination": False,
            "protocol_valid": False,
            "failure_reasons": ("participant attested arm contamination",),
        }
    )
    sessions = evidence.qa.model_copy(
        update={"sessions": (contaminated, *evidence.qa.sessions[1:])}
    )

    assert "qa.valid_pairs" in _codes(report, evidence.model_copy(update={"qa": invalid}))
    assert "qa.session_invalid" in _codes(report, evidence.model_copy(update={"qa": sessions}))


def test_qa_participant_must_match_protocol_and_report_is_exact_projection(complete):
    report, evidence, _ = complete
    relabelled_sessions = tuple(
        session.model_copy(update={"participant_id": "participant-99"})
        for session in evidence.qa.sessions
    )
    relabelled = evidence.qa.model_copy(
        update={
            "participant_id": "participant-99",
            "sessions": relabelled_sessions,
        }
    )
    wrong_protocol = evidence.qa.model_copy(update={"protocol_sha256": "0" * 64})
    minutes = report.qa.paired_saved_minutes.model_copy(
        update={"median": report.qa.paired_saved_minutes.median + 1.0}
    )
    altered_report = report.model_copy(
        update={"qa": report.qa.model_copy(update={"paired_saved_minutes": minutes})}
    )

    assert "qa.protocol_binding" in _codes(
        report,
        evidence.model_copy(update={"qa": relabelled}),
    )
    assert "qa.protocol_binding" in _codes(
        report,
        evidence.model_copy(update={"qa": wrong_protocol}),
    )
    assert "qa.report_mismatch" in _codes(
        altered_report,
        _with_report(evidence, altered_report),
    )


def test_agent_tokens_latency_workloads_and_cassette_denominators_are_gated(complete):
    report, evidence, _ = complete
    workload = evidence.agent_cost.workloads[0]
    no_tokens = workload.model_copy(
        update={
            "tokens": TokenTotals(
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                reported_total_tokens=0,
            )
        }
    )
    pending_latency = DistributionMetric.pending(
        name="request_latency_ms",
        unit="milliseconds",
        bucket="agent_latency",
        planned_n=workload.recorded_requests,
    )
    no_latency = workload.model_copy(update={"request_latency_ms": pending_latency})
    short = workload.model_copy(update={"evaluated_n": workload.evaluated_n - 1})
    sample = workload.samples[0].model_copy(
        update={
            "recorded_request_hashes": (),
            "cassette_sha256s": (),
            "recorded_request_latencies_ms": (),
            "recorded_requests": 0,
            "recorded_latency_ms": 0,
        }
    )
    missing_cassette = workload.model_copy(update={"samples": (sample, *workload.samples[1:])})

    def changed(row):  # noqa: ANN001
        return evidence.agent_cost.model_copy(
            update={"workloads": (row, *evidence.agent_cost.workloads[1:])}
        )

    assert "cost.tokens_missing" in _codes(
        report, evidence.model_copy(update={"agent_cost": changed(no_tokens)})
    )
    assert "cost.latency_missing" in _codes(
        report, evidence.model_copy(update={"agent_cost": changed(no_latency)})
    )
    assert "cost.workload_denominator" in _codes(
        report, evidence.model_copy(update={"agent_cost": changed(short)})
    )
    assert "cost.cassette_missing" in _codes(
        report, evidence.model_copy(update={"agent_cost": changed(missing_cassette)})
    )


def test_deterministic_runtime_is_required_and_cross_checked(complete):
    report, evidence, _ = complete

    assert "runtime.evidence_missing" in _codes(
        report, evidence.model_copy(update={"deterministic_runtime": None})
    )


def test_report_view_and_evidence_path_hash_mismatches_are_gated(complete):
    report, evidence, _ = complete
    views = evidence.views.model_copy(update={"html_sha256": "0" * 64})
    artifact = evidence.artifacts[0].model_copy(update={"path": "tampered.json"})
    artifacts = (artifact, *evidence.artifacts[1:])

    assert "view.hash_mismatch" in _codes(report, evidence.model_copy(update={"views": views}))
    assert "evidence.ref_mismatch" in _codes(
        report, evidence.model_copy(update={"artifacts": artifacts})
    )


def test_current_and_historical_models_cannot_be_relabelled(complete):
    report, evidence, _ = complete
    relabelled = report.model_copy(
        update={"narrative": report.narrative.model_copy(update={"model_snapshot": OPUS_M2})}
    )

    assert "model.relabeling" in _codes(relabelled, _with_report(evidence, relabelled))


def test_gate_failures_are_stably_sorted(complete):
    report, evidence, _ = complete
    broken = report.model_copy(update={"meta": report.meta.model_copy(update={"corpus_size": 1})})
    failures = validate_m3_acceptance(
        broken,
        evidence.model_copy(update={"deterministic_runtime": None}),
    )

    keys = [(item.code, item.path, item.message) for item in failures]
    assert keys == sorted(keys)
