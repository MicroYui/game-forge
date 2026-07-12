"""Strict BenchReport v2 contracts and canonical JSON behavior."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from gameforge.bench.report_contracts import (
    AgentCostSection,
    AgentCostWorkload,
    BenchMeta,
    BenchReport,
    BinaryMetric,
    CostLatencySection,
    DeterministicRuntimeSection,
    DistributionMetric,
    EvidenceArtifactRef,
    ExternalSection,
    HedSection,
    NarrativeSection,
    PowerMetric,
    QaSection,
    TokenTotals,
    VersionRef,
    canonical_report_bytes,
    load_bench_report,
    write_bench_report,
)
from gameforge.bench.taxonomy import CLASS_META, DefectClass
from gameforge.contracts.model_router import ModelSnapshot


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


def _binary(
    name: str,
    *,
    defect_class: DefectClass | None = None,
    bucket: str,
    n: int = 10,
    k: int | None = None,
    status: str = "measured",
    evidence_ref: str | None = None,
) -> BinaryMetric:
    if k is None:
        k = n
    return BinaryMetric.wilson(
        name=name,
        defect_class=defect_class,
        bucket=bucket,
        planned_n=n,
        evaluated_n=n,
        k=k,
        status=status,
        protocol_id="protocol@1",
        evidence_ref=evidence_ref,
    )


def _distribution(
    name: str,
    unit: str,
    bucket: str,
    *,
    n: int = 8,
    mean: float = 1.0,
    evidence_ref: str | None = None,
) -> DistributionMetric:
    return DistributionMetric.measured(
        name=name,
        unit=unit,
        bucket=bucket,
        planned_n=n,
        evaluated_n=n,
        mean=mean,
        median=mean,
        p95=mean,
        primary_estimate=mean,
        ci_low=mean,
        ci_high=mean,
        ci_method="percentile-bootstrap95",
        status="measured",
        protocol_id="protocol@1",
        evidence_ref=evidence_ref,
    )


def _sample_report() -> BenchReport:
    narrative_classes = {
        DefectClass.character_violation,
        DefectClass.spoiler,
        DefectClass.faction_violation,
        DefectClass.uniqueness_violation,
    }
    seeded = tuple(
        _binary(
            "bdr",
            defect_class=defect_class,
            bucket=CLASS_META[defect_class].bucket.value,
            evidence_ref="seeded",
        )
        for defect_class in DefectClass
        if defect_class not in narrative_classes
    )
    narrative_bdr = tuple(
        _binary(
            "bdr",
            defect_class=defect_class,
            bucket="llm_assisted",
            n=381,
            evidence_ref="narrative",
        )
        for defect_class in DefectClass
        if defect_class in narrative_classes
    )
    external_classes = (
        DefectClass.cyclic_dependency,
        DefectClass.dangling_reference,
        DefectClass.dead_quest,
        DefectClass.unreachable_target,
    )
    qa_time = DistributionMetric.pending(
        name="paired_saved_minutes",
        unit="minutes",
        bucket="qa",
        planned_n=4,
        protocol_id="qa-protocol@1",
    )
    qa_fraction = DistributionMetric.pending(
        name="paired_saved_fraction",
        unit="fraction",
        bucket="qa",
        planned_n=4,
        protocol_id="qa-protocol@1",
    )
    qa_success = BinaryMetric.pending(
        name="manual_success",
        bucket="qa",
        planned_n=4,
        protocol_id="qa-protocol@1",
    )
    token_distribution = _distribution(
        "tokens_per_sample",
        "tokens",
        "agent_cost",
        evidence_ref="cost",
    )
    latency_distribution = _distribution(
        "request_latency_ms",
        "milliseconds",
        "agent_latency",
        evidence_ref="cost",
    )
    runtime_distribution = _distribution(
        "deterministic_per_sample_ms",
        "milliseconds",
        "deterministic_runtime",
        n=903,
        evidence_ref="runtime",
    )
    return BenchReport(
        seeded=seeded,
        false_positives=(
            _binary("oracle_fp", bucket="deterministic_fp", n=1, k=0),
            _binary("constraint_fp", bucket="constraint_fp", n=902, k=0),
        ),
        agent=(
            _binary("fix_pass_rate", bucket="agent", n=10, evidence_ref="cost"),
        ),
        power=tuple(
            PowerMetric(
                defect_class=defect_class,
                bucket=CLASS_META[defect_class].bucket.value,
                evaluated_n=381 if defect_class in narrative_classes else 82,
                achieved_half_width=0.04,
                target_half_width=0.05,
                status="measured",
                evidence_ref=(
                    "narrative" if defect_class in narrative_classes else "seeded"
                ),
            )
            for defect_class in DefectClass
        ),
        external=ExternalSection(
            source_id="endless_sky",
            repository="https://github.com/endless-sky/endless-sky",
            manifest_sha256="a" * 64,
            reader_version="endless-sky-reader@1",
            adapter_version="endless-sky-adapter@1",
            mapping_spec_sha256="b" * 64,
            total_cases=8,
            qualified_cases=8,
            development=tuple(
                _binary(
                    "external_bdr",
                    defect_class=defect_class,
                    bucket="external_development",
                    n=1,
                    status="underpowered",
                    evidence_ref="external",
                )
                for defect_class in external_classes
            ),
            verification=tuple(
                _binary(
                    "external_bdr",
                    defect_class=defect_class,
                    bucket="external_verification",
                    n=1,
                    status="underpowered",
                    evidence_ref="external",
                )
                for defect_class in external_classes
            ),
            after_oracle_fp=_binary(
                "external_after_oracle_fp",
                bucket="external_fp",
                n=8,
                k=0,
                evidence_ref="external",
            ),
            evidence_ref="external",
        ),
        narrative=NarrativeSection(
            model_snapshot=GPT_56,
            protocol_sha256="c" * 64,
            corpus_manifest_sha256="d" * 64,
            bdr=narrative_bdr,
            clean_fp=_binary(
                "narrative_clean_fp",
                bucket="llm_assisted_fp",
                n=381,
                k=6,
                evidence_ref="narrative",
            ),
            evidence_ref="narrative",
        ),
        hed=HedSection(
            model_snapshot=GPT_56,
            normalized_distance=_distribution(
                "hed_normalized",
                "normalized_distance",
                "hed",
                mean=0.9,
                evidence_ref="hed",
            ),
            raw_distance=_distribution(
                "hed_raw",
                "atomic_changes",
                "hed",
                mean=9.0,
                evidence_ref="hed",
            ),
            dispositions=(
                _binary("hed_unchanged", bucket="hed", n=8, k=0),
                _binary("hed_edited", bucket="hed", n=8, k=6),
                _binary("hed_unusable", bucket="hed", n=8, k=2),
                _binary("hed_protocol_failure", bucket="hed", n=8, k=0),
            ),
            evidence_ref="hed",
        ),
        qa=QaSection(
            scope="single-participant-eight-session-case-study",
            protocol_sha256="e" * 64,
            paired_saved_minutes=qa_time,
            paired_saved_fraction=qa_fraction,
            manual_success=qa_success,
            assisted_success=qa_success.model_copy(update={"name": "assisted_success"}),
            conclusion="pending",
            evidence_ref=None,
        ),
        cost_latency=CostLatencySection(
            agent=AgentCostSection(
                workloads=(
                    AgentCostWorkload(
                        workload_id="narrative-verification",
                        model_snapshot=GPT_56,
                        planned_n=8,
                        evaluated_n=8,
                        tokens=TokenTotals(
                            input_tokens=80,
                            output_tokens=20,
                            cache_read_tokens=0,
                            cache_write_tokens=40,
                            reported_total_tokens=100,
                        ),
                        tokens_per_sample=token_distribution,
                        request_latency_ms=latency_distribution,
                        logical_requests=8,
                        recorded_requests=8,
                        session_cache_reuses=0,
                        known_transport_attempts=0,
                        known_transport_retries=0,
                        unknown_transport_attempt_records=8,
                        evidence_ref="cost",
                    ),
                ),
                evidence_ref="cost",
            ),
            deterministic=DeterministicRuntimeSection(
                workload_id="seeded-checker-sim-pipeline",
                setup_ms=10.0,
                per_sample_ms=runtime_distribution,
                environment_sha256="f" * 64,
                evidence_ref="runtime",
            ),
        ),
        versions=(
            VersionRef(component="model.current", version="openai/gpt-5.6-sol/pre-m4@1"),
            VersionRef(component="model.historical", version="anthropic/claude-opus-4-8/m2a@1"),
        ),
        evidence=(
            EvidenceArtifactRef(
                evidence_id="cost",
                path="scenarios/bench/agent-cost-latency-evidence.json",
                sha256="1" * 64,
                schema_version="agent-cost-latency-evidence@1",
                available=True,
            ),
            EvidenceArtifactRef(
                evidence_id="external",
                path="scenarios/external_cases/endless_sky/external-corpus-manifest.json",
                sha256="2" * 64,
                schema_version="external-corpus-manifest@1",
                available=True,
            ),
            EvidenceArtifactRef(
                evidence_id="hed",
                path="scenarios/external_cases/endless_sky/hed-evidence.json",
                sha256="3" * 64,
                schema_version="hed-evidence@1",
                available=True,
            ),
            EvidenceArtifactRef(
                evidence_id="narrative",
                path="scenarios/narrative_bench/verification-evidence.json",
                sha256="4" * 64,
                schema_version="narrative-evidence@1",
                available=True,
            ),
            EvidenceArtifactRef(
                evidence_id="runtime",
                path="scenarios/bench/deterministic-runtime-evidence.json",
                sha256="5" * 64,
                schema_version="deterministic-runtime-evidence@1",
                available=True,
            ),
            EvidenceArtifactRef(
                evidence_id="seeded",
                path="gameforge/bench/corpus.py",
                sha256="6" * 64,
                schema_version="seeded-corpus@1",
                available=True,
            ),
            EvidenceArtifactRef(
                evidence_id="qa",
                path="scenarios/external_cases/endless_sky/qa-evidence.json",
                sha256=None,
                schema_version="qa-evidence@1",
                available=False,
            ),
        ),
        meta=BenchMeta(seed=0, corpus_size=982, report_builder_version="bench-report-builder@2"),
    )


def test_pending_metrics_have_null_estimates_not_fake_zero():
    binary = BinaryMetric.pending(
        name="qa_manual_success",
        bucket="qa",
        planned_n=4,
        protocol_id="qa-protocol@1",
    )
    distribution = DistributionMetric.pending(
        name="qa_saved_minutes",
        unit="minutes",
        bucket="qa",
        planned_n=4,
        protocol_id="qa-protocol@1",
    )

    assert binary.evaluated_n == binary.k == 0
    assert binary.rate is binary.ci_low is binary.ci_high is None
    assert distribution.evaluated_n == 0
    assert distribution.mean is distribution.primary_estimate is None
    assert binary.status == distribution.status == "pending"


def test_measured_binary_metric_rederives_rate_and_wilson_interval():
    metric = _binary(
        "bdr",
        defect_class=DefectClass.spoiler,
        bucket="llm_assisted",
        n=381,
        k=381,
    )

    assert metric.rate == 1.0
    assert metric.ci_method == "wilson95"
    assert metric.ci_low == pytest.approx(0.9900177111829906)


@pytest.mark.parametrize(
    "changes",
    [
        {"evaluated_n": 5, "planned_n": 4},
        {"k": 5, "evaluated_n": 4},
        {"rate": 0.5},
        {"ci_low": 0.5},
        {"status": "pending"},
    ],
)
def test_binary_metric_rejects_inconsistent_counts_estimates_and_status(changes):
    payload = _binary("bdr", bucket="deterministic", n=4, k=4).model_dump()
    payload.update(changes)
    with pytest.raises(ValidationError):
        BinaryMetric.model_validate(payload)


def test_distribution_metric_requires_all_or_no_estimates():
    payload = _distribution("hed", "distance", "hed").model_dump()
    payload["median"] = None
    with pytest.raises(ValidationError):
        DistributionMetric.model_validate(payload)


def test_power_status_matches_achieved_half_width():
    measured = PowerMetric(
        defect_class=DefectClass.spoiler,
        bucket="llm_assisted",
        evaluated_n=381,
        achieved_half_width=0.04,
        target_half_width=0.05,
        status="measured",
    )
    assert measured.status == "measured"
    with pytest.raises(ValidationError):
        measured.model_copy(update={"status": "underpowered"}, deep=True).__class__.model_validate(
            {**measured.model_dump(), "status": "underpowered"}
        )


def test_evidence_reference_requires_hash_exactly_when_available():
    with pytest.raises(ValidationError):
        EvidenceArtifactRef(
            evidence_id="qa",
            path="scenarios/qa.json",
            sha256=None,
            schema_version="qa-evidence@1",
            available=True,
        )
    with pytest.raises(ValidationError):
        EvidenceArtifactRef(
            evidence_id="qa",
            path="../outside.json",
            sha256="a" * 64,
            schema_version="qa-evidence@1",
            available=True,
        )


def test_bench_report_v2_round_trips_canonical_json(tmp_path):
    report = _sample_report()
    path = tmp_path / "bench-report.json"

    write_bench_report(path, report)
    loaded = load_bench_report(path)

    assert loaded == report
    assert path.read_bytes() == canonical_report_bytes(report)
    assert loaded.schema_version == "bench-report@2"


def test_report_requires_all_fifteen_classes_once():
    report = _sample_report()
    with pytest.raises(ValidationError, match="15 defect classes"):
        BenchReport.model_validate(
            {
                **report.model_dump(),
                "seeded": report.seeded[:-1],
            }
        )


def test_report_rejects_unknown_evidence_reference():
    report = _sample_report()
    bad_seeded = list(report.seeded)
    bad_seeded[0] = bad_seeded[0].model_copy(update={"evidence_ref": "missing"})
    with pytest.raises(ValidationError, match="unknown evidence ref"):
        BenchReport.model_validate({**report.model_dump(), "seeded": bad_seeded})


def test_load_report_rejects_v1_with_clear_schema_error(tmp_path):
    path = tmp_path / "v1.json"
    path.write_text(json.dumps({"seeded": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="bench-report@2"):
        load_bench_report(path)


def test_report_preserves_current_and_historical_model_versions():
    report = _sample_report()
    assert report.narrative.model_snapshot == GPT_56
    assert report.cost_latency.agent.workloads[0].model_snapshot == GPT_56
    versions = {item.version for item in report.versions}
    assert "openai/gpt-5.6-sol/pre-m4@1" in versions
    assert "anthropic/claude-opus-4-8/m2a@1" in versions
    assert OPUS_M2.model == "claude-opus-4-8"
