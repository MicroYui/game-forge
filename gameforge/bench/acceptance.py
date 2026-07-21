"""Deterministic, source-cross-checked M3 product acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Sequence

from pydantic import model_validator

from gameforge.bench.cost_latency import (
    AgentCostLatencyEvidence,
    canonical_evidence_bytes as canonical_cost_bytes,
    load_evidence as load_agent_cost,
)
from gameforge.bench.external_cases.contracts import (
    ExternalCorpusManifest,
    canonical_bytes as canonical_external_bytes,
)
from gameforge.bench.external_cases.qualify import load_manifest as load_external
from gameforge.bench.hed.contracts import (
    HedEvidenceManifest,
    canonical_evidence_bytes as canonical_hed_bytes,
    load_evidence as load_hed,
)
from gameforge.bench.narrative.contracts import NARRATIVE_CLASSES
from gameforge.bench.narrative.evidence import (
    NarrativeEvidenceManifest,
    canonical_evidence_bytes as canonical_narrative_bytes,
)
from gameforge.bench.narrative.harness import load_evidence as load_narrative
from gameforge.bench.qa.score import (
    QaEvidenceManifest,
    canonical_evidence_bytes as canonical_qa_bytes,
    load_evidence as load_qa,
    score_sessions,
    validate_qa_evidence,
)
from gameforge.bench.qa.protocol import (
    QaProtocol,
    canonical_protocol_bytes as canonical_qa_protocol_bytes,
    load_protocol as load_qa_protocol,
)
from gameforge.bench.report import build_qa_section, format_text
from gameforge.bench.report_contracts import (
    BenchReport,
    BinaryMetric,
    EvidenceArtifactRef,
    NonEmptyStr,
    Sha256,
    StableId,
    StrictModel,
    canonical_report_bytes,
    load_bench_report,
)
from gameforge.bench.runtime_evidence import (
    DeterministicRuntimeEvidence,
    canonical_runtime_evidence_bytes,
    load_runtime_evidence,
)
from gameforge.bench.taxonomy import CLASS_META, Bucket, DefectClass
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.model_router import ModelSnapshot

_MINIMUM_CORPUS_SIZE = 500
_NARRATIVE_PER_CLASS_N = 381
_NARRATIVE_CLEAN_N = 381
_POWER_TARGET = 0.05
_EXTERNAL_CASES = 8
_EXTERNAL_CLASSES = 4
_HED_CASES = 8
_QA_SESSIONS = 8
_QA_PAIRS = 4

_CURRENT_MODEL = ModelSnapshot(
    provider="openai",
    model="gpt-5.6-sol",
    snapshot_tag="pre-m4@1",
)
_HISTORICAL_PLAYTEST_MODEL = ModelSnapshot(
    provider="anthropic",
    model="claude-opus-4-8",
    snapshot_tag="m2a@1",
)
_WORKLOAD_DENOMINATORS = {
    "external-hed": 8,
    "narrative-verification": 1905,
    "playtest-flat": 20,
    "playtest-layered": 20,
    "playtest-memory-on": 20,
    "repair-search": 10,
}
_CURRENT_MODEL_WORKLOADS = {
    "external-hed",
    "narrative-verification",
    "repair-search",
}
_HISTORICAL_MODEL_WORKLOADS = {
    "playtest-flat",
    "playtest-layered",
    "playtest-memory-on",
}


class GateFailure(StrictModel):
    code: StableId
    path: NonEmptyStr
    message: NonEmptyStr


class ReportViewHashes(StrictModel):
    json_sha256: Sha256 | None
    text_sha256: Sha256 | None
    html_sha256: Sha256 | None


class M3EvidenceBundle(StrictModel):
    external: ExternalCorpusManifest
    narrative: NarrativeEvidenceManifest
    hed: HedEvidenceManifest
    qa_protocol: QaProtocol
    qa: QaEvidenceManifest | None
    agent_cost: AgentCostLatencyEvidence | None
    deterministic_runtime: DeterministicRuntimeEvidence | None
    artifacts: tuple[EvidenceArtifactRef, ...]
    views: ReportViewHashes

    @model_validator(mode="after")
    def validate_artifact_ids(self) -> M3EvidenceBundle:
        ids = tuple(item.evidence_id for item in self.artifacts)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("acceptance artifacts must be unique and sorted")
        return self


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def build_report_view_hashes(report: BenchReport) -> ReportViewHashes:
    """Hash the three authoritative projections generated from one report."""

    from gameforge.bench.panel import render_html

    return ReportViewHashes(
        json_sha256=_sha256(canonical_report_bytes(report)),
        text_sha256=_sha256((format_text(report) + "\n").encode("utf-8")),
        html_sha256=_sha256(render_html(report).encode("utf-8")),
    )


def _add(
    failures: list[GateFailure],
    code: str,
    path: str,
    message: str,
) -> None:
    failures.append(GateFailure(code=code, path=path, message=message))


def _binary_complete(metric: BinaryMetric) -> bool:
    return bool(
        metric.status in {"measured", "underpowered", "inconclusive"}
        and metric.evaluated_n > 0
        and metric.rate is not None
        and metric.ci_low is not None
        and metric.ci_high is not None
        and metric.ci_method is not None
    )


def _binary_matches(
    report_metric: BinaryMetric,
    *,
    n: int,
    k: int,
    rate: float,
    ci_low: float,
    ci_high: float,
) -> bool:
    return bool(
        report_metric.planned_n == n
        and report_metric.evaluated_n == n
        and report_metric.k == k
        and report_metric.rate == rate
        and report_metric.ci_low == ci_low
        and report_metric.ci_high == ci_high
        and report_metric.ci_method == "wilson95"
    )


def _by_class(rows: Sequence[BinaryMetric]) -> dict[DefectClass, BinaryMetric]:
    return {item.defect_class: item for item in rows if item.defect_class is not None}


def _validate_corpus(report: BenchReport, failures: list[GateFailure]) -> None:
    if report.meta.corpus_size < _MINIMUM_CORPUS_SIZE:
        _add(
            failures,
            "corpus.size_below_minimum",
            "report.meta.corpus_size",
            f"seeded corpus has {report.meta.corpus_size}; at least 500 are required",
        )

    rows = (*report.seeded, *report.narrative.bdr)
    grouped: dict[DefectClass, list[BinaryMetric]] = {}
    for row in rows:
        if row.defect_class is not None:
            grouped.setdefault(row.defect_class, []).append(row)
    for defect in DefectClass:
        metrics = grouped.get(defect, [])
        if len(metrics) != 1:
            _add(
                failures,
                "corpus.class_missing",
                f"report.bdr.{defect.value}",
                "each taxonomy class must have exactly one BDR metric",
            )
            continue
        metric = metrics[0]
        if not _binary_complete(metric) or metric.evaluated_n != metric.planned_n:
            _add(
                failures,
                "corpus.metric_not_evaluated",
                f"report.bdr.{defect.value}",
                "BDR must retain and evaluate its complete planned denominator",
            )
        if metric.bucket != CLASS_META[defect].bucket.value:
            _add(
                failures,
                "corpus.bucket_mismatch",
                f"report.bdr.{defect.value}.bucket",
                "BDR bucket differs from the frozen taxonomy",
            )


def _validate_narrative(
    report: BenchReport,
    evidence: NarrativeEvidenceManifest,
    failures: list[GateFailure],
) -> None:
    if evidence.model_snapshot != _CURRENT_MODEL:
        _add(
            failures,
            "model.relabeling",
            "evidence.narrative.model_snapshot",
            "current narrative evidence must use openai/gpt-5.6-sol/pre-m4@1",
        )
    if report.narrative.model_snapshot != evidence.model_snapshot:
        _add(
            failures,
            "model.relabeling",
            "report.narrative.model_snapshot",
            "report narrative model differs from the recorded source evidence",
        )
    if (
        report.narrative.protocol_sha256 != evidence.protocol_sha256
        or report.narrative.corpus_manifest_sha256 != evidence.corpus_manifest_sha256
    ):
        _add(
            failures,
            "narrative.binding_mismatch",
            "report.narrative",
            "report narrative hashes differ from the source manifest",
        )

    source_by_class = {item.defect_class: item for item in evidence.by_class}
    report_by_class = _by_class(report.narrative.bdr)
    for defect in NARRATIVE_CLASSES:
        source = source_by_class.get(defect)
        metric = report_by_class.get(defect)
        path = f"evidence.narrative.by_class.{defect.value}"
        if source is None or source.n != _NARRATIVE_PER_CLASS_N:
            _add(
                failures,
                "narrative.positive_denominator",
                path,
                "each narrative class requires 381 verification positives",
            )
        if source is None or metric is None:
            continue
        if not _binary_matches(
            metric,
            n=source.n,
            k=source.k,
            rate=source.rate,
            ci_low=source.ci_low,
            ci_high=source.ci_high,
        ):
            _add(
                failures,
                "narrative.report_mismatch",
                f"report.narrative.bdr.{defect.value}",
                "report BDR does not reproduce the narrative source metric",
            )
        if (
            metric.ci_low is None
            or metric.ci_high is None
            or (metric.ci_high - metric.ci_low) / 2 > _POWER_TARGET
        ):
            _add(
                failures,
                "narrative.power_under_target",
                f"report.narrative.bdr.{defect.value}",
                "narrative Wilson interval half-width exceeds 0.05",
            )

    clean = evidence.clean_fp
    if clean.n != _NARRATIVE_CLEAN_N:
        _add(
            failures,
            "narrative.clean_denominator",
            "evidence.narrative.clean_fp.n",
            "narrative verification requires 381 clean controls",
        )
    if not _binary_matches(
        report.narrative.clean_fp,
        n=clean.n,
        k=clean.count,
        rate=clean.rate,
        ci_low=clean.ci_low,
        ci_high=clean.ci_high,
    ):
        _add(
            failures,
            "narrative.report_mismatch",
            "report.narrative.clean_fp",
            "report clean FP does not reproduce the narrative source metric",
        )
    expected_outcomes = sum(item.n for item in evidence.by_class) + clean.n
    if len(evidence.outcomes) != expected_outcomes:
        _add(
            failures,
            "narrative.outcome_denominator",
            "evidence.narrative.outcomes",
            "all positives, controls, and execution failures must remain in evidence",
        )

    power = {item.defect_class: item for item in report.power}
    for defect in NARRATIVE_CLASSES:
        row = power.get(defect)
        if row is None or row.achieved_half_width > _POWER_TARGET or row.status != "measured":
            _add(
                failures,
                "narrative.power_under_target",
                f"report.power.{defect.value}",
                "narrative power row must meet the 0.05 half-width target",
            )


def _validate_false_positives(
    report: BenchReport,
    failures: list[GateFailure],
) -> None:
    by_name = {item.name: item for item in report.false_positives}
    required = {
        "oracle_fp",
        "constraint_fp",
        "narrative_clean_fp",
        "external_after_oracle_fp",
    }
    if set(by_name) != required or len(report.false_positives) != len(required):
        _add(
            failures,
            "false_positive.partition_missing",
            "report.false_positives",
            "four distinct deterministic, constraint, narrative, and external FP rows are required",
        )
    oracle = by_name.get("oracle_fp")
    if oracle is None or not _binary_complete(oracle) or oracle.k != 0:
        _add(
            failures,
            "false_positive.deterministic_nonzero",
            "report.false_positives.oracle_fp",
            "deterministic oracle false positives must be measured and equal zero",
        )
    constraint = by_name.get("constraint_fp")
    if constraint is None or not _binary_complete(constraint):
        _add(
            failures,
            "false_positive.constraint_unmeasured",
            "report.false_positives.constraint_fp",
            "constraint FP must retain a measured denominator and interval",
        )


def _validate_external(
    report: BenchReport,
    evidence: ExternalCorpusManifest,
    failures: list[GateFailure],
) -> None:
    section = report.external
    bindings_match = bool(
        section.source_id == evidence.source_id
        and section.repository == evidence.repository_url
        and section.manifest_sha256 == evidence.manifest_sha256
        and section.reader_version == evidence.reader_version
        and section.adapter_version == evidence.adapter_version
        and section.mapping_spec_sha256 == evidence.mapping_spec_sha256
    )
    if not bindings_match:
        _add(
            failures,
            "external.report_mismatch",
            "report.external",
            "external report metadata differs from the typed source manifest",
        )

    cases = evidence.cases
    if len(cases) != _EXTERNAL_CASES or section.total_cases != len(cases):
        _add(
            failures,
            "external.case_count",
            "evidence.external.cases",
            "external evidence requires exactly eight cases",
        )
    classes = {item.spec.defect_class for item in cases}
    verification_classes = {
        item.spec.defect_class for item in cases if item.spec.split == "verification"
    }
    development_classes = {
        item.spec.defect_class for item in cases if item.spec.split == "development"
    }
    if (
        len(classes) != _EXTERNAL_CLASSES
        or len(verification_classes) != _EXTERNAL_CLASSES
        or len(development_classes) != _EXTERNAL_CLASSES
    ):
        _add(
            failures,
            "external.class_coverage",
            "evidence.external.cases",
            "four classes need both development and held-out verification cases",
        )

    qualified = sum(item.qualification_status == "qualified" for item in cases)
    if qualified != _EXTERNAL_CASES or section.qualified_cases != qualified:
        _add(
            failures,
            "external.qualification_incomplete",
            "evidence.external.cases",
            "all eight external cases must remain fully qualified",
        )

    for case in cases:
        path = f"evidence.external.cases.{case.spec.case_id}"
        before_hit = any(
            item.status == "confirmed" and item.defect_class == case.spec.defect_class.value
            for item in case.findings_before
        )
        if (
            case.qualification_status != "qualified"
            or case.predicate_before.status != "violation"
            or not before_hit
        ):
            _add(
                failures,
                "external.before_hit_missing",
                path,
                "qualified external case requires predicate and checker hits before the fix",
            )
        if case.predicate_after.status != "clear" or case.findings_after:
            _add(
                failures,
                "external.after_clear_missing",
                path,
                "external predicate and deterministic findings must be clear after the fix",
            )

    report_development = _by_class(section.development)
    report_verification = _by_class(section.verification)
    for split, sources, rendered in (
        ("development", evidence.development, report_development),
        ("verification", evidence.verification, report_verification),
    ):
        for source in sources:
            metric = rendered.get(source.defect_class)
            if metric is None or not _binary_matches(
                metric,
                n=source.n,
                k=source.k,
                rate=source.rate,
                ci_low=source.ci_low,
                ci_high=source.ci_high,
            ):
                _add(
                    failures,
                    "external.report_mismatch",
                    f"report.external.{split}.{source.defect_class.value}",
                    "report external metric differs from the source manifest",
                )
            if split == "verification" and source.k < 1:
                _add(
                    failures,
                    "external.verification_hit_missing",
                    f"evidence.external.verification.{source.defect_class.value}",
                    "each held-out class needs at least one before-hit/after-clear success",
                )

    source_fp = evidence.after_oracle_fp
    if source_fp.count != 0 or section.after_oracle_fp.k != 0:
        _add(
            failures,
            "external.after_oracle_fp_nonzero",
            "evidence.external.after_oracle_fp",
            "external after snapshots must have zero deterministic oracle false positives",
        )
    if not _binary_matches(
        section.after_oracle_fp,
        n=source_fp.n,
        k=source_fp.count,
        rate=source_fp.rate,
        ci_low=source_fp.ci_low,
        ci_high=source_fp.ci_high,
    ):
        _add(
            failures,
            "external.report_mismatch",
            "report.external.after_oracle_fp",
            "report external FP differs from the source manifest",
        )


def _validate_hed(
    report: BenchReport,
    evidence: HedEvidenceManifest,
    external: ExternalCorpusManifest,
    failures: list[GateFailure],
) -> None:
    if len(evidence.outcomes) != _HED_CASES:
        _add(
            failures,
            "hed.outcome_count",
            "evidence.hed.outcomes",
            "HED requires all eight external cases in its denominator",
        )
    expected_ids = {item.spec.case_id for item in external.cases}
    actual_ids = {item.case_id for item in evidence.outcomes}
    if expected_ids != actual_ids or evidence.external_manifest_sha256 != external.manifest_sha256:
        _add(
            failures,
            "hed.external_binding",
            "evidence.hed.external_manifest_sha256",
            "HED evidence must bind the exact external case denominator",
        )
    if evidence.model_snapshot != _CURRENT_MODEL:
        _add(
            failures,
            "model.relabeling",
            "evidence.hed.model_snapshot",
            "current HED evidence must use openai/gpt-5.6-sol/pre-m4@1",
        )
    if report.hed.model_snapshot != evidence.model_snapshot:
        _add(
            failures,
            "model.relabeling",
            "report.hed.model_snapshot",
            "report HED model differs from the recorded source evidence",
        )

    for outcome in evidence.outcomes:
        path = f"evidence.hed.outcomes.{outcome.case_id}"
        if outcome.status == "protocol_failure" or outcome.disposition == "protocol_failure":
            _add(
                failures,
                "hed.protocol_failure",
                path,
                "any HED protocol failure blocks acceptance",
            )
        if not outcome.human_delta or not outcome.human_target_snapshot_id:
            _add(
                failures,
                "hed.human_target_missing",
                path,
                "every HED case needs its retained upstream human target",
            )
        if outcome.status == "agent_unusable" and outcome.patch is None:
            _add(
                failures,
                "hed.failed_patch_dropped",
                path,
                "an unusable Agent patch must remain in evidence",
            )

    metric = evidence.metric
    if metric.protocol_failure_count:
        _add(
            failures,
            "hed.protocol_failure",
            "evidence.hed.metric.protocol_failure_count",
            "HED protocol failure count must be zero",
        )
    dispositions = {item.name: item for item in report.hed.dispositions}
    expected_counts = {
        "hed_unchanged": metric.unchanged_count,
        "hed_edited": metric.edited_count,
        "hed_unusable": metric.unusable_count,
        "hed_protocol_failure": metric.protocol_failure_count,
    }
    if any(
        name not in dispositions
        or dispositions[name].planned_n != _HED_CASES
        or dispositions[name].evaluated_n != _HED_CASES
        or dispositions[name].k != count
        for name, count in expected_counts.items()
    ):
        _add(
            failures,
            "hed.report_mismatch",
            "report.hed.dispositions",
            "report HED dispositions differ from all eight source outcomes",
        )
    if (
        report.hed.normalized_distance.planned_n != _HED_CASES
        or report.hed.normalized_distance.evaluated_n != metric.evaluated_n
        or report.hed.normalized_distance.mean != metric.mean_normalized_distance
        or report.hed.raw_distance.evaluated_n != metric.evaluated_n
        or report.hed.raw_distance.mean != metric.mean_raw_distance
    ):
        _add(
            failures,
            "hed.report_mismatch",
            "report.hed.distance",
            "report HED distributions differ from the source metric",
        )


def _validate_qa(
    report: BenchReport,
    protocol: QaProtocol,
    evidence: QaEvidenceManifest | None,
    failures: list[GateFailure],
) -> None:
    if evidence is None:
        _add(
            failures,
            "qa.evidence_missing",
            "evidence.qa",
            "eight real participant sessions and four matched pairs are still missing",
        )
        if report.qa != build_qa_section(protocol, None):
            _add(
                failures,
                "qa.report_mismatch",
                "report.qa",
                "missing QA evidence must remain explicitly pending",
            )
        return

    if (
        evidence.protocol_sha256 != protocol.protocol_sha256
        or evidence.participant_id != protocol.participant_id
        or any(item.participant_id != protocol.participant_id for item in evidence.sessions)
    ):
        _add(
            failures,
            "qa.protocol_binding",
            "evidence.qa",
            "QA evidence and every session must bind the supplied participant protocol",
        )
    if evidence.score != score_sessions(protocol, evidence.sessions):
        _add(
            failures,
            "qa.score_mismatch",
            "evidence.qa.score",
            "QA score must rederive exactly from the protocol-bound sessions",
        )

    score = evidence.score
    if (
        score.evaluated_pairs != _QA_PAIRS
        or score.protocol_failure_pairs != 0
        or len(score.pairs) != _QA_PAIRS
    ):
        _add(
            failures,
            "qa.valid_pairs",
            "evidence.qa.score",
            "QA requires four complete matched pairs; negative results are allowed",
        )
    if len(evidence.sessions) != _QA_SESSIONS:
        _add(
            failures,
            "qa.session_count",
            "evidence.qa.sessions",
            "QA requires exactly eight participant-generated sessions",
        )
    if any(
        not item.protocol_valid or not item.participant_attested_no_contamination
        for item in evidence.sessions
    ):
        _add(
            failures,
            "qa.session_invalid",
            "evidence.qa.sessions",
            "every QA arm needs valid timed evidence and participant attestation",
        )

    sessions = {item.session_id: item for item in evidence.sessions}
    for pair in score.pairs:
        manual = sessions.get(pair.manual_session_id)
        assisted = sessions.get(pair.assisted_session_id)
        if (
            manual is None
            or assisted is None
            or manual.arm != "manual"
            or assisted.arm != "assisted"
            or manual.pair_id != pair.pair_id
            or assisted.pair_id != pair.pair_id
            or manual.verdict.correct != pair.manual_correct
            or assisted.verdict.correct != pair.assisted_correct
        ):
            _add(
                failures,
                "qa.arm_binding",
                f"evidence.qa.pairs.{pair.pair_id}",
                "matched QA result must bind both timed arms and their correctness verdicts",
            )

    try:
        expected_section = build_qa_section(protocol, evidence)
    except ValueError:
        expected_section = None
    if report.qa != expected_section:
        _add(
            failures,
            "qa.report_mismatch",
            "report.qa",
            "QA report metrics do not reproduce the source session score",
        )


def _expected_workload_model(workload_id: str) -> ModelSnapshot | None:
    if workload_id in _CURRENT_MODEL_WORKLOADS:
        return _CURRENT_MODEL
    if workload_id in _HISTORICAL_MODEL_WORKLOADS:
        return _HISTORICAL_PLAYTEST_MODEL
    return None


def _validate_cost(
    report: BenchReport,
    evidence: AgentCostLatencyEvidence | None,
    narrative: NarrativeEvidenceManifest,
    hed: HedEvidenceManifest,
    failures: list[GateFailure],
) -> None:
    if evidence is None:
        _add(
            failures,
            "cost.evidence_missing",
            "evidence.agent_cost",
            "Agent token and record-time latency evidence is required",
        )
        return

    source = {item.workload_id: item for item in evidence.workloads}
    rendered = {item.workload_id: item for item in report.cost_latency.agent.workloads}
    expected_ids = set(_WORKLOAD_DENOMINATORS)
    if set(source) != expected_ids or set(rendered) != expected_ids:
        _add(
            failures,
            "cost.workload_coverage",
            "evidence.agent_cost.workloads",
            "all six bounded Agent workloads must be measured separately",
        )

    for workload_id, expected_n in _WORKLOAD_DENOMINATORS.items():
        workload = source.get(workload_id)
        row = rendered.get(workload_id)
        path = f"evidence.agent_cost.workloads.{workload_id}"
        if workload is None:
            continue
        if (
            workload.planned_n != expected_n
            or workload.evaluated_n != expected_n
            or len(workload.samples) != expected_n
        ):
            _add(
                failures,
                "cost.workload_denominator",
                path,
                "workload evaluated denominator differs from its frozen corpus",
            )
        if workload_id == "narrative-verification" and workload.logical_requests != 5715:
            _add(
                failures,
                "cost.workload_denominator",
                path,
                "narrative verification must retain all 5,715 logical requests",
            )
        if workload_id == "external-hed" and (
            workload.logical_requests != 14 or workload.recorded_requests != 10
        ):
            _add(
                failures,
                "cost.workload_denominator",
                path,
                "HED must retain 14 logical and 10 recorded requests",
            )

        expected_model = _expected_workload_model(workload_id)
        if workload.model_snapshot != expected_model:
            _add(
                failures,
                "model.relabeling",
                f"{path}.model_snapshot",
                "workload model differs from its recorded current or historical snapshot",
            )
        if row is not None and row.model_snapshot != workload.model_snapshot:
            _add(
                failures,
                "model.relabeling",
                f"report.cost_latency.agent.{workload_id}.model_snapshot",
                "report relabelled the recorded workload model",
            )
        if workload_id == "narrative-verification" and (
            workload.source_evidence_sha256 != narrative.evidence_sha256
        ):
            _add(
                failures,
                "cost.source_binding",
                f"{path}.source_evidence_sha256",
                "narrative cost trace differs from its source evidence",
            )
        if workload_id == "external-hed" and (
            workload.source_evidence_sha256 != hed.evidence_sha256
        ):
            _add(
                failures,
                "cost.source_binding",
                f"{path}.source_evidence_sha256",
                "HED cost trace differs from its source evidence",
            )

        if workload.tokens.reported_total_tokens <= 0:
            _add(
                failures,
                "cost.tokens_missing",
                f"{path}.tokens",
                "provider-reported token totals must be present and nonzero",
            )
        token_metric = workload.tokens_per_sample
        if (
            token_metric.status != "measured"
            or token_metric.evaluated_n != workload.evaluated_n
            or token_metric.mean is None
        ):
            _add(
                failures,
                "cost.tokens_missing",
                f"{path}.tokens_per_sample",
                "per-sample token distribution is missing or has the wrong denominator",
            )
        latency = workload.request_latency_ms
        if (
            workload.recorded_requests <= 0
            or latency.status != "measured"
            or latency.evaluated_n != workload.recorded_requests
            or latency.mean is None
            or latency.mean <= 0
        ):
            _add(
                failures,
                "cost.latency_missing",
                f"{path}.request_latency_ms",
                "positive record-time latency is required for every recorded request",
            )
        for sample in workload.samples:
            if (
                sample.recorded_requests <= 0
                or len(sample.recorded_request_hashes) != sample.recorded_requests
                or len(sample.cassette_sha256s) != sample.recorded_requests
                or len(sample.recorded_request_latencies_ms) != sample.recorded_requests
                or any(value <= 0 for value in sample.recorded_request_latencies_ms)
            ):
                _add(
                    failures,
                    "cost.cassette_missing",
                    f"{path}.samples.{sample.sample_id}",
                    "a workload sample is missing a recorded cassette or its latency",
                )

        if row is None:
            continue
        if (
            row.planned_n != workload.planned_n
            or row.evaluated_n != workload.evaluated_n
            or row.tokens != workload.tokens
            or row.logical_requests != workload.logical_requests
            or row.recorded_requests != workload.recorded_requests
            or row.session_cache_reuses != workload.session_cache_reuses
            or row.known_transport_attempts != workload.known_transport_attempts
            or row.known_transport_retries != workload.known_transport_retries
            or row.unknown_transport_attempt_records != workload.unknown_transport_attempt_records
            or row.tokens_per_sample.evaluated_n != workload.tokens_per_sample.evaluated_n
            or row.tokens_per_sample.mean != workload.tokens_per_sample.mean
            or row.request_latency_ms.evaluated_n != workload.request_latency_ms.evaluated_n
            or row.request_latency_ms.mean != workload.request_latency_ms.mean
        ):
            _add(
                failures,
                "cost.report_mismatch",
                f"report.cost_latency.agent.{workload_id}",
                "report cost and latency row differs from source evidence",
            )


def _environment_sha256(runtime: DeterministicRuntimeEvidence) -> str:
    raw = canonical_json(runtime.environment.model_dump(mode="json")).encode("utf-8")
    return _sha256(raw)


def _validate_runtime(
    report: BenchReport,
    evidence: DeterministicRuntimeEvidence | None,
    failures: list[GateFailure],
) -> None:
    if evidence is None:
        _add(
            failures,
            "runtime.evidence_missing",
            "evidence.deterministic_runtime",
            "controlled deterministic pipeline runtime evidence is required",
        )
        return

    metric = evidence.per_sample_ms
    expected_n = sum(evidence.per_class_n.values()) + evidence.distinct_clean_n
    if (
        metric.status != "measured"
        or metric.evaluated_n != expected_n
        or metric.mean is None
        or metric.mean <= 0
        or evidence.setup_elapsed_ns <= 0
    ):
        _add(
            failures,
            "runtime.measurement_incomplete",
            "evidence.deterministic_runtime",
            "runtime setup and every deterministic/simulation sample must be measured",
        )

    seeded = _by_class(report.seeded)
    for defect in DefectClass:
        count = evidence.per_class_n.get(defect)
        if CLASS_META[defect].bucket is Bucket.llm_assisted:
            if count != 0:
                _add(
                    failures,
                    "runtime.narrative_included",
                    f"evidence.deterministic_runtime.per_class_n.{defect.value}",
                    "LLM-assisted cases cannot enter deterministic timing evidence",
                )
            continue
        report_metric = seeded.get(defect)
        if report_metric is None or count != report_metric.evaluated_n:
            _add(
                failures,
                "runtime.denominator_mismatch",
                f"evidence.deterministic_runtime.per_class_n.{defect.value}",
                "runtime denominator differs from the seeded report",
            )

    oracle = next(
        (item for item in report.false_positives if item.name == "oracle_fp"),
        None,
    )
    if oracle is None or evidence.distinct_clean_n != oracle.evaluated_n:
        _add(
            failures,
            "runtime.denominator_mismatch",
            "evidence.deterministic_runtime.distinct_clean_n",
            "runtime clean denominator differs from deterministic oracle FP",
        )

    section = report.cost_latency.deterministic
    if (
        section.workload_id != evidence.workload_id
        or section.setup_ms != evidence.setup_elapsed_ns / 1_000_000
        or section.per_sample_ms.evaluated_n != metric.evaluated_n
        or section.per_sample_ms.mean != metric.mean
        or section.per_sample_ms.median != metric.median
        or section.per_sample_ms.p95 != metric.p95
        or section.environment_sha256 != _environment_sha256(evidence)
    ):
        _add(
            failures,
            "runtime.report_mismatch",
            "report.cost_latency.deterministic",
            "report runtime row differs from the environment-bound source evidence",
        )


def _canonical_source_hashes(
    report: BenchReport,
    evidence: M3EvidenceBundle,
) -> dict[str, str]:
    values = {
        report.external.evidence_ref: _sha256(canonical_external_bytes(evidence.external)),
        report.narrative.evidence_ref: _sha256(canonical_narrative_bytes(evidence.narrative)),
        report.hed.evidence_ref: _sha256(canonical_hed_bytes(evidence.hed)),
        "qa-protocol": _sha256(canonical_qa_protocol_bytes(evidence.qa_protocol)),
    }
    if evidence.qa is not None and report.qa.evidence_ref is not None:
        values[report.qa.evidence_ref] = _sha256(canonical_qa_bytes(evidence.qa))
    if evidence.agent_cost is not None:
        values[report.cost_latency.agent.evidence_ref] = _sha256(
            canonical_cost_bytes(evidence.agent_cost)
        )
    if evidence.deterministic_runtime is not None:
        values[report.cost_latency.deterministic.evidence_ref] = _sha256(
            canonical_runtime_evidence_bytes(evidence.deterministic_runtime)
        )
    return values


def _validate_artifacts_and_views(
    report: BenchReport,
    evidence: M3EvidenceBundle,
    failures: list[GateFailure],
) -> None:
    expected_views = build_report_view_hashes(report)
    if evidence.views != expected_views:
        _add(
            failures,
            "view.hash_mismatch",
            "evidence.views",
            "JSON, text, and HTML bytes must all project the same BenchReport",
        )

    report_artifacts = {item.evidence_id: item for item in report.evidence}
    observed = {item.evidence_id: item for item in evidence.artifacts}
    if report_artifacts != observed:
        _add(
            failures,
            "evidence.ref_mismatch",
            "report.evidence",
            "report evidence path, availability, schema, or byte hash differs from observed artifacts",
        )
    for evidence_id, expected_sha in _canonical_source_hashes(report, evidence).items():
        artifact = report_artifacts.get(evidence_id)
        if artifact is None or not artifact.available or artifact.sha256 != expected_sha:
            _add(
                failures,
                "evidence.hash_mismatch",
                f"report.evidence.{evidence_id}",
                "report artifact hash does not bind the typed source manifest bytes",
            )


def validate_m3_acceptance(
    report: BenchReport,
    evidence: M3EvidenceBundle,
) -> tuple[GateFailure, ...]:
    """Return every unmet M3 product gate in deterministic order."""

    failures: list[GateFailure] = []
    _validate_corpus(report, failures)
    _validate_narrative(report, evidence.narrative, failures)
    _validate_false_positives(report, failures)
    _validate_external(report, evidence.external, failures)
    _validate_hed(report, evidence.hed, evidence.external, failures)
    _validate_qa(report, evidence.qa_protocol, evidence.qa, failures)
    _validate_cost(
        report,
        evidence.agent_cost,
        evidence.narrative,
        evidence.hed,
        failures,
    )
    _validate_runtime(report, evidence.deterministic_runtime, failures)
    _validate_artifacts_and_views(report, evidence, failures)
    unique = {(item.code, item.path, item.message): item for item in failures}
    return tuple(unique[key] for key in sorted(unique))


def _observed_artifact(root: Path, item: EvidenceArtifactRef) -> EvidenceArtifactRef:
    path = root / item.path
    available = path.is_file()
    return EvidenceArtifactRef(
        evidence_id=item.evidence_id,
        path=item.path,
        sha256=_sha256(path.read_bytes()) if available else None,
        schema_version=item.schema_version,
        available=available,
    )


def _artifact_path(
    root: Path,
    artifacts: dict[str, EvidenceArtifactRef],
    evidence_id: str,
) -> Path:
    item = artifacts.get(evidence_id)
    if item is None:
        raise ValueError(f"report omits required evidence ref: {evidence_id}")
    return root / item.path


def _file_sha256(path: Path) -> str | None:
    return _sha256(path.read_bytes()) if path.is_file() else None


def load_m3_evidence_bundle(
    report: BenchReport,
    *,
    report_path: str | Path,
    repo_root: str | Path = ".",
) -> M3EvidenceBundle:
    """Load typed source manifests and observe the report's referenced bytes."""

    root = Path(repo_root)
    refs = {item.evidence_id: item for item in report.evidence}
    external = load_external(_artifact_path(root, refs, report.external.evidence_ref))
    narrative = load_narrative(_artifact_path(root, refs, report.narrative.evidence_ref))
    hed = load_hed(_artifact_path(root, refs, report.hed.evidence_ref))
    qa_protocol = load_qa_protocol(_artifact_path(root, refs, "qa-protocol"))
    qa_ref = refs.get(report.qa.evidence_ref or "qa")
    qa_path = root / qa_ref.path if qa_ref is not None else None
    qa = load_qa(qa_path) if qa_path is not None and qa_path.is_file() else None
    if qa is not None:
        validate_qa_evidence(qa, qa_protocol, qa_path.parent)
    agent_cost = load_agent_cost(_artifact_path(root, refs, report.cost_latency.agent.evidence_ref))
    runtime = load_runtime_evidence(
        _artifact_path(root, refs, report.cost_latency.deterministic.evidence_ref)
    )
    observed = tuple(
        sorted(
            (_observed_artifact(root, item) for item in report.evidence),
            key=lambda item: item.evidence_id,
        )
    )
    json_path = Path(report_path)
    views = ReportViewHashes(
        json_sha256=_file_sha256(json_path),
        text_sha256=_file_sha256(json_path.with_suffix(".txt")),
        html_sha256=_file_sha256(json_path.with_suffix(".html")),
    )
    return M3EvidenceBundle(
        external=external,
        narrative=narrative,
        hed=hed,
        qa_protocol=qa_protocol,
        qa=qa,
        agent_cost=agent_cost,
        deterministic_runtime=runtime,
        artifacts=observed,
        views=views,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    report = load_bench_report(args.report)
    evidence = load_m3_evidence_bundle(
        report,
        report_path=args.report,
        repo_root=args.repo_root,
    )
    failures = validate_m3_acceptance(report, evidence)
    print(
        json.dumps(
            [item.model_dump(mode="json") for item in failures],
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover - exercised by measured CLI run
    raise SystemExit(main())


__all__ = [
    "GateFailure",
    "M3EvidenceBundle",
    "ReportViewHashes",
    "build_report_view_hashes",
    "load_m3_evidence_bundle",
    "main",
    "validate_m3_acceptance",
]
