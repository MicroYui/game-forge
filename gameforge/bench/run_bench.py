"""Load frozen evidence, recompute bounded metrics, and emit BenchReport v2."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Sequence

from gameforge.bench.agent_metrics import aggregate_agent_metrics
from gameforge.bench.cost_latency import (
    load_evidence as load_agent_cost,
    validate_agent_cost_evidence,
)
from gameforge.bench.corpus import build_corpus
from gameforge.bench.external_cases.qualify import load_manifest as load_external
from gameforge.bench.hed.contracts import (
    load_evidence as load_hed,
    validate_evidence_manifest as validate_hed,
)
from gameforge.bench.hed.protocol import (
    assert_protocol_ready as assert_hed_protocol,
    load_protocol as load_hed_protocol,
)
from gameforge.bench.metrics import Metric, default_constraints, score_seeded
from gameforge.bench.narrative.corpus import load_cases
from gameforge.bench.narrative.evidence import (
    validate_evidence_manifest as validate_narrative,
)
from gameforge.bench.narrative.harness import load_evidence as load_narrative
from gameforge.bench.narrative.protocol import load_frozen_protocol
from gameforge.bench.qa.protocol import (
    assert_qa_protocol_ready,
    load_protocol as load_qa_protocol,
)
from gameforge.bench.qa.score import load_evidence as load_qa
from gameforge.bench.report import (
    ReportEvidenceBundle,
    build_bench_report as compose_bench_report,
    write_report_bundle,
)
from gameforge.bench.report_contracts import (
    BenchReport,
    EvidenceArtifactRef,
)
from gameforge.bench.runtime_evidence import (
    load_runtime_evidence,
    validate_runtime_evidence,
)
from gameforge.bench.taxonomy import DefectClass

_EXTERNAL_ROOT = Path("scenarios/external_cases/endless_sky")
_NARRATIVE_ROOT = Path("scenarios/narrative_bench")
_DEFAULT_AGENT_COST = Path("scenarios/bench/agent-cost-latency-evidence.json")
_DEFAULT_RUNTIME = Path("scenarios/bench/deterministic-runtime-evidence.json")
_DEFAULT_QA = _EXTERNAL_ROOT / "qa-evidence.json"


def _resolve(root: Path, path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else root / candidate


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("report evidence paths must remain inside the repository") from exc


def _artifact(
    root: Path,
    *,
    evidence_id: str,
    path: Path,
    schema_version: str,
    available: bool = True,
) -> EvidenceArtifactRef:
    return EvidenceArtifactRef(
        evidence_id=evidence_id,
        path=_relative(root, path),
        sha256=(hashlib.sha256(path.read_bytes()).hexdigest() if available else None),
        schema_version=schema_version,
        available=available,
    )


def load_report_evidence(
    *,
    repo_root: str | Path = ".",
    agent_cost_path: str | Path = _DEFAULT_AGENT_COST,
    runtime_path: str | Path = _DEFAULT_RUNTIME,
    qa_path: str | Path = _DEFAULT_QA,
) -> ReportEvidenceBundle:
    """Load and cross-validate every non-seeded input from explicit paths."""

    root = Path(repo_root)
    external_path = root / _EXTERNAL_ROOT / "external-corpus-manifest.json"
    hed_protocol_path = root / _EXTERNAL_ROOT / "hed-protocol.json"
    hed_path = root / _EXTERNAL_ROOT / "hed-evidence.json"
    qa_protocol_path = root / _EXTERNAL_ROOT / "qa-protocol.json"
    narrative_protocol_path = root / _NARRATIVE_ROOT / "protocol.json"
    narrative_manifest_path = root / _NARRATIVE_ROOT / "corpus-manifest.json"
    narrative_evidence_path = root / _NARRATIVE_ROOT / "verification-evidence.json"
    narrative_cases_path = root / _NARRATIVE_ROOT / "verification.jsonl"
    cost_path = _resolve(root, agent_cost_path)
    measured_runtime_path = _resolve(root, runtime_path)
    measured_qa_path = _resolve(root, qa_path)

    external = load_external(external_path)
    narrative_protocol, narrative_corpus = load_frozen_protocol(
        narrative_protocol_path,
        narrative_manifest_path,
    )
    narrative = load_narrative(narrative_evidence_path)
    validate_narrative(
        narrative,
        load_cases(narrative_cases_path),
        corpus_manifest_sha256=narrative_corpus.manifest_sha256,
        protocol_sha256=narrative_protocol.protocol_sha256,
        protocol_model_snapshot=narrative_protocol.model_snapshot,
    )
    hed_protocol = load_hed_protocol(hed_protocol_path)
    assert_hed_protocol(hed_protocol, external, manifest_path=external_path)
    hed = load_hed(hed_path)
    validate_hed(hed, protocol=hed_protocol, external_manifest=external)
    qa_protocol = load_qa_protocol(qa_protocol_path)
    assert_qa_protocol_ready(qa_protocol, external, hed)
    qa = load_qa(measured_qa_path) if measured_qa_path.is_file() else None
    agent_cost = load_agent_cost(cost_path)
    validate_agent_cost_evidence(agent_cost, repo_root=root)
    runtime = load_runtime_evidence(measured_runtime_path)
    constraints = default_constraints(str(root / "scenarios/constraints"))
    validate_runtime_evidence(runtime, constraints=constraints)

    evidence = [
        _artifact(
            root,
            evidence_id="agent-cost",
            path=cost_path,
            schema_version=agent_cost.schema_version,
        ),
        _artifact(
            root,
            evidence_id="external",
            path=external_path,
            schema_version=external.schema_version,
        ),
        _artifact(
            root,
            evidence_id="hed",
            path=hed_path,
            schema_version=hed.schema_version,
        ),
        _artifact(
            root,
            evidence_id="hed-protocol",
            path=hed_protocol_path,
            schema_version=hed_protocol.schema_version,
        ),
        _artifact(
            root,
            evidence_id="narrative",
            path=narrative_evidence_path,
            schema_version=narrative.schema_version,
        ),
        _artifact(
            root,
            evidence_id="narrative-corpus",
            path=narrative_manifest_path,
            schema_version=narrative_corpus.schema_version,
        ),
        _artifact(
            root,
            evidence_id="narrative-cases",
            path=narrative_cases_path,
            schema_version="narrative-corpus-jsonl@1",
        ),
        _artifact(
            root,
            evidence_id="narrative-protocol",
            path=narrative_protocol_path,
            schema_version=narrative_protocol.schema_version,
        ),
        _artifact(
            root,
            evidence_id="qa",
            path=measured_qa_path,
            schema_version=(qa.schema_version if qa is not None else "qa-evidence@1"),
            available=qa is not None,
        ),
        _artifact(
            root,
            evidence_id="qa-protocol",
            path=qa_protocol_path,
            schema_version=qa_protocol.schema_version,
        ),
        _artifact(
            root,
            evidence_id="runtime",
            path=measured_runtime_path,
            schema_version=runtime.schema_version,
        ),
        _artifact(
            root,
            evidence_id="seeded",
            path=root / "gameforge/bench/corpus.py",
            schema_version="seeded-corpus-code@1",
        ),
    ]
    return ReportEvidenceBundle(
        external=external,
        narrative_protocol=narrative_protocol,
        narrative_corpus=narrative_corpus,
        narrative=narrative,
        hed_protocol=hed_protocol,
        hed=hed,
        qa_protocol=qa_protocol,
        qa=qa,
        agent_cost=agent_cost,
        deterministic_runtime=runtime,
        evidence=tuple(sorted(evidence, key=lambda item: item.evidence_id)),
    )


def build_bench_report(
    *,
    seed: int = 0,
    per_class_n: dict[DefectClass, int] | None = None,
    n_clean: int = 40,
    constraints=None,
    agent_metrics: Sequence[Metric] | None = None,
    evidence_bundle: ReportEvidenceBundle | None = None,
    repo_root: str | Path = ".",
    agent_cost_path: str | Path = _DEFAULT_AGENT_COST,
    runtime_path: str | Path = _DEFAULT_RUNTIME,
    qa_path: str | Path = _DEFAULT_QA,
) -> BenchReport:
    bundle = evidence_bundle or load_report_evidence(
        repo_root=repo_root,
        agent_cost_path=agent_cost_path,
        runtime_path=runtime_path,
        qa_path=qa_path,
    )
    corpus = build_corpus(seed=seed, per_class_n=per_class_n, n_clean=n_clean)
    active_constraints = (
        constraints
        if constraints is not None
        else default_constraints(str(Path(repo_root) / "scenarios/constraints"))
    )
    score = score_seeded(corpus, active_constraints)
    metrics = tuple(agent_metrics) if agent_metrics is not None else aggregate_agent_metrics()
    return compose_bench_report(
        seed=seed,
        corpus_size=len(corpus.samples),
        per_class_n=corpus.per_class_n,
        seeded_score=score,
        agent_metrics=metrics,
        evidence_bundle=bundle,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--agent-cost", type=Path, default=_DEFAULT_AGENT_COST)
    parser.add_argument("--runtime", type=Path, default=_DEFAULT_RUNTIME)
    parser.add_argument("--qa", type=Path, default=_DEFAULT_QA)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)
    report = build_bench_report(
        seed=args.seed,
        repo_root=args.repo_root,
        agent_cost_path=args.agent_cost,
        runtime_path=args.runtime,
        qa_path=args.qa,
    )
    write_report_bundle(report, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_bench_report",
    "load_report_evidence",
    "main",
]
