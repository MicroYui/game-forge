"""BenchReport v2 composition from typed, hash-bound evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

import gameforge.bench.run_bench as run_bench_module
from gameforge.bench.cost_latency import (
    SampleTrace,
    aggregate_workload,
    seal_agent_cost_evidence,
)
from gameforge.bench.corpus import build_corpus
from gameforge.bench.external_cases.qualify import load_manifest as load_external
from gameforge.bench.hed.contracts import load_evidence as load_hed
from gameforge.bench.hed.protocol import load_protocol as load_hed_protocol
from gameforge.bench.metrics import FPReport, Metric, SeededScore, default_constraints
from gameforge.bench.narrative.corpus import load_manifest as load_narrative_corpus
from gameforge.bench.narrative.harness import load_evidence as load_narrative
from gameforge.bench.narrative.protocol import load_protocol as load_narrative_protocol
from gameforge.bench.qa.protocol import load_protocol as load_qa_protocol
from gameforge.bench.report import ReportEvidenceBundle, build_bench_report
from gameforge.bench.report_contracts import EvidenceArtifactRef
from gameforge.bench.runtime_evidence import measure_runtime
from gameforge.bench.taxonomy import CLASS_META, Bucket, DefectClass
from gameforge.contracts.cassette import CassetteRecord
from gameforge.contracts.model_router import ModelResponse, ModelSnapshot
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.spine.stats import wilson_ci

_EXTERNAL_ROOT = Path("scenarios/external_cases/endless_sky")
_NARRATIVE_ROOT = Path("scenarios/narrative_bench")

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


def test_report_qa_loader_propagates_source_specific_replay_failure(
    tmp_path: Path,
    monkeypatch,
):
    evidence_path = tmp_path / "qa-evidence.json"
    evidence_path.touch()
    protocol_path = tmp_path / "qa-protocol.json"

    def reject(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise ValueError("deterministic verdict replay mismatch")

    monkeypatch.setattr(
        run_bench_module,
        "validate_imported_qa_evidence",
        reject,
    )

    with pytest.raises(ValueError, match="verdict replay mismatch"):
        run_bench_module._load_qa_evidence(evidence_path, protocol_path)


def _metric(defect_class: DefectClass, *, n: int = 1, k: int = 1) -> Metric:
    low, high = wilson_ci(k, n)
    return Metric(
        name="bdr",
        defect_class=defect_class.value,
        n=n,
        k=k,
        rate=k / n,
        ci_low=low,
        ci_high=high,
        bucket=CLASS_META[defect_class].bucket.value,
    )


def _score() -> SeededScore:
    deterministic = tuple(
        defect for defect in DefectClass if CLASS_META[defect].bucket is not Bucket.llm_assisted
    )
    low, high = wilson_ci(0, 1)
    cross_low, cross_high = wilson_ci(0, len(deterministic))
    return SeededScore(
        bdr=[_metric(defect) for defect in deterministic],
        oracle_fp=FPReport(1, 0, 0.0, low, high),
        constraint_fp=FPReport(
            len(deterministic),
            0,
            0.0,
            cross_low,
            cross_high,
        ),
    )


def _record(root: Path, request_hash: str, snapshot: ModelSnapshot) -> None:
    usage = (
        {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        if snapshot == GPT_56
        else {"input": 10, "output": 5}
    )
    CassetteStore(root).record(
        CassetteRecord(
            request_hash=request_hash,
            agent_node_id="test.report",
            model_snapshot=snapshot,
            response=ModelResponse(
                response_normalized="{}",
                latency_ms=100,
                token_usage=usage,
            ),
            transport_attempts=1 if snapshot == GPT_56 else None,
            transport_retries=0 if snapshot == GPT_56 else None,
        )
    )


def _cost_evidence(tmp_path: Path):
    root = tmp_path / "cassettes"
    gpt_hash = "sha256:" + "a" * 64
    opus_hash = "sha256:" + "b" * 64
    _record(root, gpt_hash, GPT_56)
    _record(root, opus_hash, OPUS_M2)
    workloads = []
    for workload_id, snapshot, request_hash in (
        ("narrative-verification", GPT_56, gpt_hash),
        ("external-hed", GPT_56, gpt_hash),
        ("repair-search", GPT_56, gpt_hash),
        ("playtest-layered", OPUS_M2, opus_hash),
        ("playtest-flat", OPUS_M2, opus_hash),
        ("playtest-memory-on", OPUS_M2, opus_hash),
    ):
        workloads.append(
            aggregate_workload(
                workload_id=workload_id,
                model_snapshot=snapshot,
                cassette_root=root,
                cassette_root_ref=f"cassettes/{workload_id}",
                protocol_id=f"{workload_id}@1",
                source_evidence_sha256="c" * 64,
                planned_n=1,
                traces=(
                    SampleTrace(
                        sample_id=f"sample-{workload_id}",
                        request_hashes=(request_hash,),
                    ),
                ),
            )
        )
    return seal_agent_cost_evidence(workloads)


def _clock():
    current = -10

    def read() -> int:
        nonlocal current
        current += 10
        return current

    return read


def _artifacts() -> tuple[EvidenceArtifactRef, ...]:
    available = (
        ("agent-cost", "scenarios/bench/agent-cost-latency-evidence.json"),
        ("external", "scenarios/external_cases/endless_sky/external-corpus-manifest.json"),
        ("hed", "scenarios/external_cases/endless_sky/hed-evidence.json"),
        ("narrative", "scenarios/narrative_bench/verification-evidence.json"),
        ("runtime", "scenarios/bench/deterministic-runtime-evidence.json"),
        ("seeded", "gameforge/bench/corpus.py"),
    )
    rows = [
        EvidenceArtifactRef(
            evidence_id=evidence_id,
            path=path,
            sha256=f"{index:x}" * 64,
            schema_version=f"{evidence_id}@1",
            available=True,
        )
        for index, (evidence_id, path) in enumerate(available, start=1)
    ]
    rows.append(
        EvidenceArtifactRef(
            evidence_id="qa",
            path="scenarios/external_cases/endless_sky/qa-evidence.json",
            sha256=None,
            schema_version="qa-evidence@2",
            available=False,
        )
    )
    return tuple(rows)


def _inputs(tmp_path: Path):
    per_class_n = {
        defect: (20 if CLASS_META[defect].bucket is Bucket.llm_assisted else 1)
        for defect in DefectClass
    }
    corpus = build_corpus(seed=0, per_class_n=per_class_n, n_clean=2)
    runtime = measure_runtime(
        corpus,
        default_constraints(),
        seed=0,
        clock_ns=_clock(),
    )
    bundle = ReportEvidenceBundle(
        external=load_external(_EXTERNAL_ROOT / "external-corpus-manifest.json"),
        narrative_protocol=load_narrative_protocol(_NARRATIVE_ROOT / "protocol.json"),
        narrative_corpus=load_narrative_corpus(_NARRATIVE_ROOT / "corpus-manifest.json"),
        narrative=load_narrative(_NARRATIVE_ROOT / "verification-evidence.json"),
        hed_protocol=load_hed_protocol(_EXTERNAL_ROOT / "hed-protocol.json"),
        hed=load_hed(_EXTERNAL_ROOT / "hed-evidence.json"),
        qa_protocol=load_qa_protocol(_EXTERNAL_ROOT / "qa-protocol.json"),
        qa=None,
        agent_cost=_cost_evidence(tmp_path),
        deterministic_runtime=runtime,
        evidence=_artifacts(),
    )
    agent = (
        Metric("fix_pass_rate", None, 10, 10, 1.0, *wilson_ci(10, 10), "agent"),
        Metric(
            "playtest_completion_layered",
            None,
            20,
            14,
            0.7,
            *wilson_ci(14, 20),
            "agent",
        ),
    )
    return per_class_n, bundle, agent


def test_v2_report_contains_all_fifteen_measured_class_metrics(tmp_path: Path):
    per_class_n, bundle, agent = _inputs(tmp_path)
    report = build_bench_report(
        seed=0,
        corpus_size=sum(per_class_n.values()),
        per_class_n=per_class_n,
        seeded_score=_score(),
        agent_metrics=agent,
        evidence_bundle=bundle,
    )

    classes = {row.defect_class for row in (*report.seeded, *report.narrative.bdr)}
    assert classes == set(DefectClass)
    assert all(row.evaluated_n > 0 for row in report.narrative.bdr)
    assert report.schema_version == "bench-report@2"
    assert report.meta.corpus_size == 91
    assert any(row.status == "underpowered" for row in report.power)


def test_report_preserves_external_hed_cost_and_model_evidence(tmp_path: Path):
    per_class_n, bundle, agent = _inputs(tmp_path)
    report = build_bench_report(
        seed=0,
        corpus_size=sum(per_class_n.values()),
        per_class_n=per_class_n,
        seeded_score=_score(),
        agent_metrics=agent,
        evidence_bundle=bundle,
    )

    assert report.external.source_id == "endless_sky"
    assert len(report.external.development) == len(report.external.verification) == 4
    dispositions = {row.name: row.k for row in report.hed.dispositions}
    assert dispositions["hed_unusable"] == 2
    assert len(report.cost_latency.agent.workloads) == 6
    models = {item.model_snapshot.model for item in report.cost_latency.agent.workloads}
    assert models == {"gpt-5.6-sol", "claude-opus-4-8"}
    assert report.cost_latency.deterministic.evidence_ref == "runtime"
    assert {item.name for item in report.false_positives} == {
        "constraint_fp",
        "external_after_oracle_fp",
        "narrative_clean_fp",
        "oracle_fp",
    }
    assert tuple(item.component for item in report.versions) == tuple(
        sorted(item.component for item in report.versions)
    )
    assert tuple(item.evidence_id for item in report.evidence) == tuple(
        sorted(item.evidence_id for item in report.evidence)
    )


def test_missing_qa_evidence_is_pending_and_never_fake_zero(tmp_path: Path):
    per_class_n, bundle, agent = _inputs(tmp_path)
    report = build_bench_report(
        seed=0,
        corpus_size=sum(per_class_n.values()),
        per_class_n=per_class_n,
        seeded_score=_score(),
        agent_metrics=agent,
        evidence_bundle=bundle,
    )

    assert report.qa.conclusion == "pending"
    assert report.qa.paired_saved_minutes.status == "pending"
    assert report.qa.paired_saved_minutes.mean is None
    assert report.qa.manual_success.rate is None
    assert report.qa.evidence_ref is None
