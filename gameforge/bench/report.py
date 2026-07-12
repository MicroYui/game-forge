"""Pure translation from typed benchmark evidence to BenchReport v2."""

from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass
from typing import Literal, Mapping, Sequence

from gameforge.bench.cost_latency import AgentCostLatencyEvidence
from gameforge.bench.external_cases.contracts import ExternalCorpusManifest
from gameforge.bench.hed.contracts import HedEvidenceManifest
from gameforge.bench.hed.protocol import HedProtocol
from gameforge.bench.metrics import FPReport, Metric, SeededScore
from gameforge.bench.narrative.corpus import NarrativeCorpusManifest
from gameforge.bench.narrative.evidence import NarrativeEvidenceManifest
from gameforge.bench.narrative.protocol import NarrativeProtocol
from gameforge.bench.power import achieved_half_width
from gameforge.bench.qa.protocol import QaProtocol
from gameforge.bench.qa.score import QaEvidenceManifest
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
    VersionRef,
)
from gameforge.bench.runtime_evidence import DeterministicRuntimeEvidence
from gameforge.bench.stats import percentile, percentile_bootstrap_ci
from gameforge.bench.taxonomy import CLASS_META, Bucket, DefectClass
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.versions import (
    AGENT_IO_SCHEMA_VERSION,
    CASSETTE_SCHEMA_VERSION,
    DSL_GRAMMAR_VERSION,
    ENV_CONTRACT_VERSION,
    FINDING_SCHEMA_VERSION,
    IR_SCHEMA_VERSION,
    MODEL_ROUTER_SCHEMA_VERSION,
    PATCH_SCHEMA_VERSION,
    REVIEW_SCHEMA_VERSION,
    TOOL_VERSION,
)

_POWER_TARGET = 0.05
_EXPECTED_COST_WORKLOADS = {
    "external-hed",
    "narrative-verification",
    "playtest-flat",
    "playtest-layered",
    "playtest-memory-on",
    "repair-search",
}
_REQUIRED_EVIDENCE = {
    "agent-cost",
    "external",
    "hed",
    "narrative",
    "qa",
    "runtime",
    "seeded",
}


@dataclass(frozen=True)
class ReportEvidenceBundle:
    external: ExternalCorpusManifest
    narrative_protocol: NarrativeProtocol
    narrative_corpus: NarrativeCorpusManifest
    narrative: NarrativeEvidenceManifest
    hed_protocol: HedProtocol
    hed: HedEvidenceManifest
    qa_protocol: QaProtocol
    qa: QaEvidenceManifest | None
    agent_cost: AgentCostLatencyEvidence
    deterministic_runtime: DeterministicRuntimeEvidence
    evidence: tuple[EvidenceArtifactRef, ...]


def _protocol_id(name: str, sha256: str) -> str:
    return f"{name}@1:{sha256}"


def _power_status(k: int, n: int) -> Literal["measured", "underpowered"]:
    return "measured" if achieved_half_width(k, n) <= _POWER_TARGET else "underpowered"


def _binary(
    *,
    name: str,
    bucket: str,
    planned_n: int,
    n: int,
    k: int,
    status: Literal["measured", "underpowered", "inconclusive"],
    protocol_id: str,
    evidence_ref: str,
    defect_class: DefectClass | None = None,
) -> BinaryMetric:
    return BinaryMetric.wilson(
        name=name,
        defect_class=defect_class,
        bucket=bucket,
        planned_n=planned_n,
        evaluated_n=n,
        k=k,
        status=status,
        protocol_id=protocol_id,
        evidence_ref=evidence_ref,
    )


def _fp_metric(
    source: FPReport,
    *,
    name: str,
    bucket: str,
    protocol_id: str,
    evidence_ref: str,
) -> BinaryMetric:
    return _binary(
        name=name,
        bucket=bucket,
        planned_n=source.n,
        n=source.n,
        k=source.count,
        status="measured",
        protocol_id=protocol_id,
        evidence_ref=evidence_ref,
    )


def _copy_distribution(
    metric: DistributionMetric,
    *,
    protocol_id: str,
    evidence_ref: str,
) -> DistributionMetric:
    return DistributionMetric.model_validate(
        {
            **metric.model_dump(mode="json"),
            "protocol_id": protocol_id,
            "evidence_ref": evidence_ref,
        }
    )


def _measured_distribution(
    values: Sequence[float],
    *,
    name: str,
    unit: str,
    bucket: str,
    planned_n: int,
    protocol_id: str,
    evidence_ref: str,
    mean: float | None = None,
    median: float | None = None,
    ci_low: float | None = None,
    ci_high: float | None = None,
) -> DistributionMetric:
    sample = tuple(float(value) for value in values)
    if not sample:
        return DistributionMetric(
            name=name,
            unit=unit,
            bucket=bucket,
            planned_n=planned_n,
            evaluated_n=0,
            status="failed",
            protocol_id=protocol_id,
            evidence_ref=evidence_ref,
        )
    interval = percentile_bootstrap_ci(sample, statistics.fmean)
    actual_mean = statistics.fmean(sample)
    actual_median = percentile(sample, 0.5)
    return DistributionMetric.measured(
        name=name,
        unit=unit,
        bucket=bucket,
        planned_n=planned_n,
        evaluated_n=len(sample),
        mean=actual_mean if mean is None else mean,
        median=actual_median if median is None else median,
        p95=percentile(sample, 0.95),
        primary_estimate=actual_mean if mean is None else mean,
        ci_low=interval.low if ci_low is None else ci_low,
        ci_high=interval.high if ci_high is None else ci_high,
        ci_method=interval.method,
        status="measured",
        protocol_id=protocol_id,
        evidence_ref=evidence_ref,
    )


def _validate_bundle(bundle: ReportEvidenceBundle) -> None:
    external = bundle.external
    narrative = bundle.narrative
    hed = bundle.hed
    if narrative.protocol_sha256 != bundle.narrative_protocol.protocol_sha256:
        raise ValueError("narrative evidence differs from the frozen protocol")
    if narrative.model_snapshot != bundle.narrative_protocol.model_snapshot:
        raise ValueError("narrative evidence model snapshot differs from protocol")
    if narrative.corpus_manifest_sha256 != bundle.narrative_corpus.manifest_sha256:
        raise ValueError("narrative evidence differs from the frozen corpus")
    if bundle.hed_protocol.external_manifest_sha256 != external.manifest_sha256:
        raise ValueError("HED protocol differs from the external manifest")
    if hed.external_manifest_sha256 != external.manifest_sha256:
        raise ValueError("HED evidence differs from the external manifest")
    if hed.protocol_sha256 != bundle.hed_protocol.protocol_sha256:
        raise ValueError("HED evidence differs from the frozen protocol")
    if hed.model_snapshot != bundle.hed_protocol.model_snapshot:
        raise ValueError("HED evidence model snapshot differs from protocol")
    if bundle.qa_protocol.external_manifest_sha256 != external.manifest_sha256:
        raise ValueError("QA protocol differs from the external manifest")
    if bundle.qa_protocol.hed_evidence_sha256 != hed.evidence_sha256:
        raise ValueError("QA protocol differs from HED evidence")
    if bundle.qa is not None and (
        bundle.qa.protocol_sha256 != bundle.qa_protocol.protocol_sha256
    ):
        raise ValueError("QA evidence differs from the frozen protocol")
    workload_ids = {item.workload_id for item in bundle.agent_cost.workloads}
    if workload_ids != _EXPECTED_COST_WORKLOADS:
        raise ValueError("Agent cost evidence must contain the six frozen workloads")
    artifacts = {item.evidence_id: item for item in bundle.evidence}
    if not _REQUIRED_EVIDENCE.issubset(artifacts):
        raise ValueError("report evidence refs omit a required artifact")
    if artifacts["qa"].available != (bundle.qa is not None):
        raise ValueError("QA artifact availability differs from typed evidence")


def _seeded_metrics(
    score: SeededScore,
    per_class_n: Mapping[DefectClass, int],
) -> tuple[BinaryMetric, ...]:
    by_class = {metric.defect_class: metric for metric in score.bdr}
    expected = tuple(
        defect
        for defect in DefectClass
        if CLASS_META[defect].bucket is not Bucket.llm_assisted
    )
    if set(by_class) != {item.value for item in expected}:
        raise ValueError("seeded score must cover all eleven deterministic/simulation classes")
    rows = []
    for defect in expected:
        source = by_class[defect.value]
        rows.append(
            _binary(
                name="bdr",
                defect_class=defect,
                bucket=CLASS_META[defect].bucket.value,
                planned_n=per_class_n[defect],
                n=source.n,
                k=source.k,
                status=_power_status(source.k, source.n),
                protocol_id="seeded-checker-sim@1",
                evidence_ref="seeded",
            )
        )
    return tuple(rows)


def _narrative_section(bundle: ReportEvidenceBundle) -> NarrativeSection:
    protocol_id = _protocol_id("narrative", bundle.narrative.protocol_sha256)
    bdr = tuple(
        _binary(
            name="bdr",
            defect_class=metric.defect_class,
            bucket=Bucket.llm_assisted.value,
            planned_n=metric.n,
            n=metric.n,
            k=metric.k,
            status=_power_status(metric.k, metric.n),
            protocol_id=protocol_id,
            evidence_ref="narrative",
        )
        for metric in bundle.narrative.by_class
    )
    clean = bundle.narrative.clean_fp
    clean_fp = _binary(
        name="narrative_clean_fp",
        bucket="llm_assisted_fp",
        planned_n=clean.n,
        n=clean.n,
        k=clean.count,
        status="measured",
        protocol_id=protocol_id,
        evidence_ref="narrative",
    )
    return NarrativeSection(
        model_snapshot=bundle.narrative.model_snapshot,
        protocol_sha256=bundle.narrative.protocol_sha256,
        corpus_manifest_sha256=bundle.narrative.corpus_manifest_sha256,
        bdr=bdr,
        clean_fp=clean_fp,
        evidence_ref="narrative",
    )


def _external_section(bundle: ReportEvidenceBundle) -> ExternalSection:
    external = bundle.external
    protocol_id = _protocol_id("external-corpus", external.manifest_sha256)

    def rows(metrics) -> tuple[BinaryMetric, ...]:  # noqa: ANN001
        return tuple(
            _binary(
                name="external_bdr",
                defect_class=metric.defect_class,
                bucket=f"external_{metric.split}",
                planned_n=metric.n,
                n=metric.n,
                k=metric.k,
                status=_power_status(metric.k, metric.n),
                protocol_id=protocol_id,
                evidence_ref="external",
            )
            for metric in metrics
        )

    fp = external.after_oracle_fp
    return ExternalSection(
        source_id=external.source_id,
        repository=external.repository_url,
        manifest_sha256=external.manifest_sha256,
        reader_version=external.reader_version,
        adapter_version=external.adapter_version,
        mapping_spec_sha256=external.mapping_spec_sha256,
        total_cases=len(external.cases),
        qualified_cases=sum(
            item.qualification_status == "qualified" for item in external.cases
        ),
        development=rows(external.development),
        verification=rows(external.verification),
        after_oracle_fp=_binary(
            name="external_after_oracle_fp",
            bucket="external_fp",
            planned_n=fp.n,
            n=fp.n,
            k=fp.count,
            status="measured",
            protocol_id=protocol_id,
            evidence_ref="external",
        ),
        evidence_ref="external",
    )


def _hed_section(bundle: ReportEvidenceBundle) -> HedSection:
    evidence = bundle.hed
    metric = evidence.metric
    protocol_id = _protocol_id("hed", evidence.protocol_sha256)
    normalized = tuple(
        item.normalized_distance
        for item in evidence.outcomes
        if item.normalized_distance is not None
    )
    raw = tuple(
        float(item.raw_distance)
        for item in evidence.outcomes
        if item.raw_distance is not None
    )
    dispositions = tuple(
        _binary(
            name=name,
            bucket="hed",
            planned_n=metric.planned_n,
            n=metric.planned_n,
            k=count,
            status="measured",
            protocol_id=protocol_id,
            evidence_ref="hed",
        )
        for name, count in (
            ("hed_unchanged", metric.unchanged_count),
            ("hed_edited", metric.edited_count),
            ("hed_unusable", metric.unusable_count),
            ("hed_protocol_failure", metric.protocol_failure_count),
        )
    )
    return HedSection(
        model_snapshot=evidence.model_snapshot,
        normalized_distance=_measured_distribution(
            normalized,
            name="hed_normalized_distance",
            unit="normalized_distance",
            bucket="hed",
            planned_n=metric.planned_n,
            protocol_id=protocol_id,
            evidence_ref="hed",
            mean=metric.mean_normalized_distance,
            median=metric.median_normalized_distance,
            ci_low=metric.ci_low,
            ci_high=metric.ci_high,
        ),
        raw_distance=_measured_distribution(
            raw,
            name="hed_raw_distance",
            unit="atomic_changes",
            bucket="hed",
            planned_n=metric.planned_n,
            protocol_id=protocol_id,
            evidence_ref="hed",
            mean=metric.mean_raw_distance,
            median=metric.median_raw_distance,
        ),
        dispositions=dispositions,
        evidence_ref="hed",
    )


def build_qa_section(
    protocol: QaProtocol,
    evidence: QaEvidenceManifest | None,
) -> QaSection:
    protocol_id = _protocol_id("qa", protocol.protocol_sha256)
    if evidence is None:
        return QaSection(
            scope="single-participant-eight-session-case-study",
            protocol_sha256=protocol.protocol_sha256,
            paired_saved_minutes=DistributionMetric.pending(
                name="paired_saved_minutes",
                unit="minutes",
                bucket="qa",
                planned_n=4,
                protocol_id=protocol_id,
            ),
            paired_saved_fraction=DistributionMetric.pending(
                name="paired_saved_fraction",
                unit="fraction",
                bucket="qa",
                planned_n=4,
                protocol_id=protocol_id,
            ),
            manual_success=BinaryMetric.pending(
                name="manual_success",
                bucket="qa",
                planned_n=4,
                protocol_id=protocol_id,
            ),
            assisted_success=BinaryMetric.pending(
                name="assisted_success",
                bucket="qa",
                planned_n=4,
                protocol_id=protocol_id,
            ),
            conclusion="pending",
            evidence_ref=None,
        )
    if evidence.protocol_sha256 != protocol.protocol_sha256:
        raise ValueError("QA evidence differs from the supplied protocol")
    score = evidence.score
    saved_minutes = tuple(item.saved_minutes for item in score.pairs)
    saved_fractions = tuple(item.saved_fraction for item in score.pairs)
    if score.evaluated_pairs:
        metric_status: Literal["measured", "inconclusive"] = (
            "measured" if score.protocol_failure_pairs == 0 else "inconclusive"
        )
        manual = _binary(
            name="manual_success",
            bucket="qa",
            planned_n=4,
            n=score.manual_success.n,
            k=score.manual_success.k,
            status=metric_status,
            protocol_id=protocol_id,
            evidence_ref="qa",
        )
        assisted = _binary(
            name="assisted_success",
            bucket="qa",
            planned_n=4,
            n=score.assisted_success.n,
            k=score.assisted_success.k,
            status=metric_status,
            protocol_id=protocol_id,
            evidence_ref="qa",
        )
        minutes = _measured_distribution(
            saved_minutes,
            name="paired_saved_minutes",
            unit="minutes",
            bucket="qa",
            planned_n=4,
            protocol_id=protocol_id,
            evidence_ref="qa",
            mean=score.mean_saved_minutes,
            median=score.median_saved_minutes,
            ci_low=score.saved_minutes_ci_low,
            ci_high=score.saved_minutes_ci_high,
        )
        fraction = _measured_distribution(
            saved_fractions,
            name="paired_saved_fraction",
            unit="fraction",
            bucket="qa",
            planned_n=4,
            protocol_id=protocol_id,
            evidence_ref="qa",
            mean=score.mean_saved_fraction,
            median=score.median_saved_fraction,
            ci_low=score.saved_fraction_ci_low,
            ci_high=score.saved_fraction_ci_high,
        )
    else:
        failed_distribution = {
            "planned_n": 4,
            "evaluated_n": 0,
            "status": "failed",
            "protocol_id": protocol_id,
            "evidence_ref": "qa",
        }
        minutes = DistributionMetric(
            name="paired_saved_minutes",
            unit="minutes",
            bucket="qa",
            **failed_distribution,
        )
        fraction = DistributionMetric(
            name="paired_saved_fraction",
            unit="fraction",
            bucket="qa",
            **failed_distribution,
        )
        manual = BinaryMetric(
            name="manual_success",
            bucket="qa",
            planned_n=4,
            evaluated_n=0,
            k=0,
            status="failed",
            protocol_id=protocol_id,
            evidence_ref="qa",
        )
        assisted = manual.model_copy(update={"name": "assisted_success"})
    return QaSection(
        scope="single-participant-eight-session-case-study",
        protocol_sha256=protocol.protocol_sha256,
        paired_saved_minutes=minutes,
        paired_saved_fraction=fraction,
        manual_success=manual,
        assisted_success=assisted,
        conclusion=score.conclusion,
        evidence_ref="qa",
    )


def _agent_cost_section(bundle: ReportEvidenceBundle) -> AgentCostSection:
    workloads = tuple(
        AgentCostWorkload(
            workload_id=item.workload_id,
            model_snapshot=item.model_snapshot,
            planned_n=item.planned_n,
            evaluated_n=item.evaluated_n,
            tokens=item.tokens,
            tokens_per_sample=_copy_distribution(
                item.tokens_per_sample,
                protocol_id=item.protocol_id,
                evidence_ref="agent-cost",
            ),
            request_latency_ms=_copy_distribution(
                item.request_latency_ms,
                protocol_id=item.protocol_id,
                evidence_ref="agent-cost",
            ),
            logical_requests=item.logical_requests,
            recorded_requests=item.recorded_requests,
            session_cache_reuses=item.session_cache_reuses,
            known_transport_attempts=item.known_transport_attempts,
            known_transport_retries=item.known_transport_retries,
            unknown_transport_attempt_records=item.unknown_transport_attempt_records,
            evidence_ref="agent-cost",
        )
        for item in bundle.agent_cost.workloads
    )
    return AgentCostSection(workloads=workloads, evidence_ref="agent-cost")


def _runtime_section(bundle: ReportEvidenceBundle) -> DeterministicRuntimeSection:
    evidence = bundle.deterministic_runtime
    environment_sha256 = hashlib.sha256(
        canonical_json(evidence.environment.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()
    return DeterministicRuntimeSection(
        workload_id=evidence.workload_id,
        setup_ms=evidence.setup_elapsed_ns / 1_000_000,
        per_sample_ms=_copy_distribution(
            evidence.per_sample_ms,
            protocol_id="seeded-runtime@1",
            evidence_ref="runtime",
        ),
        environment_sha256=environment_sha256,
        evidence_ref="runtime",
    )


def _agent_metrics(metrics: Sequence[Metric]) -> tuple[BinaryMetric, ...]:
    rows = []
    for metric in sorted(metrics, key=lambda item: item.name):
        rows.append(
            _binary(
                name=metric.name,
                bucket=metric.bucket,
                planned_n=metric.n,
                n=metric.n,
                k=metric.k,
                status="measured",
                protocol_id="bounded-agent-replay@1",
                evidence_ref="agent-cost",
            )
        )
    return tuple(rows)


def _versions(bundle: ReportEvidenceBundle) -> tuple[VersionRef, ...]:
    narrative = bundle.narrative_protocol
    hed = bundle.hed_protocol
    versions = [
        VersionRef(component="constraints", version="constraint-bundle@1", sha256=bundle.deterministic_runtime.constraints_sha256),
        VersionRef(component="external.adapter", version=bundle.external.adapter_version),
        VersionRef(component="external.reader", version=bundle.external.reader_version),
        VersionRef(component="external.source-revision", version=bundle.external.pinned_head),
        VersionRef(component="model.current", version=_snapshot_version(narrative.model_snapshot)),
        VersionRef(component="narrative.generator", version=narrative.generator_version),
        VersionRef(component="narrative.matcher", version=narrative.matcher_version),
        VersionRef(component="narrative.oracle", version=narrative.oracle_version),
        VersionRef(component="narrative.renderer", version=narrative.renderer_version),
        VersionRef(component="prompt.hed", version=hed.repair_prompt_version, sha256=hed.repair_prompt_bundle_sha256),
        VersionRef(component="prompt.narrative", version=narrative.prompt_version, sha256=narrative.prompt_bundle_sha256),
        VersionRef(component="protocol.hed", version=hed.schema_version, sha256=hed.protocol_sha256),
        VersionRef(component="protocol.narrative", version=narrative.schema_version, sha256=narrative.protocol_sha256),
        VersionRef(component="protocol.qa", version=bundle.qa_protocol.schema_version, sha256=bundle.qa_protocol.protocol_sha256),
        VersionRef(component="schema.agent-cost", version=bundle.agent_cost.schema_version),
        VersionRef(component="schema.agent-io", version=AGENT_IO_SCHEMA_VERSION),
        VersionRef(component="schema.cassette", version=CASSETTE_SCHEMA_VERSION),
        VersionRef(component="schema.dsl", version=DSL_GRAMMAR_VERSION),
        VersionRef(component="schema.env", version=ENV_CONTRACT_VERSION),
        VersionRef(component="schema.finding", version=FINDING_SCHEMA_VERSION),
        VersionRef(component="schema.ir", version=IR_SCHEMA_VERSION),
        VersionRef(component="schema.model-router", version=MODEL_ROUTER_SCHEMA_VERSION),
        VersionRef(component="schema.patch", version=PATCH_SCHEMA_VERSION),
        VersionRef(component="schema.report", version="bench-report@2"),
        VersionRef(component="schema.review", version=REVIEW_SCHEMA_VERSION),
        VersionRef(component="schema.runtime", version=bundle.deterministic_runtime.schema_version),
        VersionRef(component="tool.gameforge", version=TOOL_VERSION),
    ]
    historical = sorted(
        {
            _snapshot_version(item.model_snapshot)
            for item in bundle.agent_cost.workloads
            if item.model_snapshot != narrative.model_snapshot
        }
    )
    for index, version in enumerate(historical):
        component = "model.historical" if len(historical) == 1 else f"model.historical.{index + 1:02d}"
        versions.append(VersionRef(component=component, version=version))
    for item in bundle.deterministic_runtime.environment.package_versions:
        versions.append(
            VersionRef(component=f"runtime.{item.component}", version=item.version)
        )
    return tuple(sorted(versions, key=lambda item: item.component))


def _snapshot_version(snapshot) -> str:  # noqa: ANN001
    return f"{snapshot.provider}/{snapshot.model}/{snapshot.snapshot_tag}"


def build_bench_report(
    *,
    seed: int,
    corpus_size: int,
    per_class_n: Mapping[DefectClass, int],
    seeded_score: SeededScore,
    agent_metrics: Sequence[Metric],
    evidence_bundle: ReportEvidenceBundle,
) -> BenchReport:
    """Compose one authoritative report without file IO or live model calls."""

    _validate_bundle(evidence_bundle)
    if set(per_class_n) != set(DefectClass):
        raise ValueError("per_class_n must cover the complete taxonomy")
    if corpus_size != sum(per_class_n.values()):
        raise ValueError("report corpus_size differs from per-class denominators")
    runtime = evidence_bundle.deterministic_runtime
    if runtime.seed != seed:
        raise ValueError("runtime evidence seed differs from seeded report")
    for defect in DefectClass:
        if CLASS_META[defect].bucket is not Bucket.llm_assisted:
            if runtime.per_class_n[defect] != per_class_n[defect]:
                raise ValueError(f"runtime denominator differs for {defect.value}")
    if runtime.distinct_clean_n != seeded_score.oracle_fp.n:
        raise ValueError("runtime clean denominator differs from seeded score")

    seeded = _seeded_metrics(seeded_score, per_class_n)
    narrative = _narrative_section(evidence_bundle)
    external = _external_section(evidence_bundle)
    false_positives = (
        _fp_metric(
            seeded_score.oracle_fp,
            name="oracle_fp",
            bucket="deterministic_fp",
            protocol_id="seeded-checker-sim@1",
            evidence_ref="seeded",
        ),
        _fp_metric(
            seeded_score.constraint_fp,
            name="constraint_fp",
            bucket="constraint_fp",
            protocol_id="seeded-checker-sim@1",
            evidence_ref="seeded",
        ),
        narrative.clean_fp,
        external.after_oracle_fp,
    )
    by_class = {
        metric.defect_class: metric for metric in (*seeded, *narrative.bdr)
    }
    power = tuple(
        PowerMetric(
            defect_class=defect,
            bucket=CLASS_META[defect].bucket.value,
            evaluated_n=by_class[defect].evaluated_n,
            achieved_half_width=achieved_half_width(
                by_class[defect].k,
                by_class[defect].evaluated_n,
            ),
            target_half_width=_POWER_TARGET,
            status=_power_status(
                by_class[defect].k,
                by_class[defect].evaluated_n,
            ),
            evidence_ref=(
                "narrative"
                if CLASS_META[defect].bucket is Bucket.llm_assisted
                else "seeded"
            ),
        )
        for defect in DefectClass
    )
    return BenchReport(
        seeded=seeded,
        false_positives=false_positives,
        agent=_agent_metrics(agent_metrics),
        power=power,
        external=external,
        narrative=narrative,
        hed=_hed_section(evidence_bundle),
        qa=build_qa_section(evidence_bundle.qa_protocol, evidence_bundle.qa),
        cost_latency=CostLatencySection(
            agent=_agent_cost_section(evidence_bundle),
            deterministic=_runtime_section(evidence_bundle),
        ),
        versions=_versions(evidence_bundle),
        evidence=tuple(
            sorted(evidence_bundle.evidence, key=lambda item: item.evidence_id)
        ),
        meta=BenchMeta(
            seed=seed,
            corpus_size=corpus_size,
            report_builder_version="bench-report-builder@2",
        ),
    )


__all__ = [
    "ReportEvidenceBundle",
    "build_bench_report",
    "build_qa_section",
]
