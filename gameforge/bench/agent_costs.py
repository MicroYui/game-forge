"""Agent-only composition bridge for replay-derived cost and latency traces."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
from typing import Any, Callable, Literal, Sequence

from gameforge.agents import harness as _repair
from gameforge.agents import playtest_harness as _playtest
from gameforge.bench.cost_latency import (
    AgentCostLatencyEvidence,
    AgentWorkloadEvidence,
    SampleTrace,
    aggregate_workload,
    seal_agent_cost_evidence,
    write_evidence,
)
from gameforge.bench.hed.contracts import (
    HedEvidenceManifest,
    load_evidence as load_hed_evidence,
)
from gameforge.bench.narrative.evidence import NarrativeEvidenceManifest
from gameforge.bench.narrative.harness import load_evidence as load_narrative_evidence
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.cassette import CASSETTE_MISS
from gameforge.contracts.model_router import (
    ModelRequest,
    ModelResponse,
    ModelSnapshot,
    request_hash,
)
from gameforge.runtime.cassette.store import CassetteStore

PlaytestVariant = Literal["layered", "flat", "memory_on"]

_NARRATIVE_EVIDENCE = Path("scenarios/narrative_bench/verification-evidence.json")
_HED_EVIDENCE = Path("scenarios/external_cases/endless_sky/hed-evidence.json")
_NARRATIVE_CASSETTES = Path("cassettes/narrative/pre-m4-1")
_HED_CASSETTES = Path("cassettes/hed/pre-m4-1")
_REPAIR_CASSETTES = Path("cassettes")
_PLAYTEST_CASSETTES = Path("cassettes/playtest")
_CONSTRAINTS = Path("scenarios/constraints")


class RequestTrackingRouter:
    """Record logical request order while delegating to a shared router."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        self.request_hashes: list[str] = []

    @property
    def default_model_snapshot(self) -> ModelSnapshot | None:
        return getattr(self._delegate, "default_model_snapshot", None)

    def call(self, request: ModelRequest) -> ModelResponse:
        self.request_hashes.append(request_hash(request))
        return self._delegate.call(request)


def narrative_verification_traces(
    evidence: NarrativeEvidenceManifest,
) -> tuple[SampleTrace, ...]:
    if evidence.split != "verification":
        raise ValueError("narrative cost evidence requires the verification split")
    return tuple(
        SampleTrace(sample_id=outcome.case_id, request_hashes=outcome.request_hashes)
        for outcome in evidence.outcomes
    )


def hed_traces(evidence: HedEvidenceManifest) -> tuple[SampleTrace, ...]:
    return tuple(
        SampleTrace(sample_id=outcome.case_id, request_hashes=outcome.request_hashes)
        for outcome in evidence.outcomes
    )


def repair_replay_traces(
    *,
    scenario_dirs: Sequence[str] | None = None,
    constraints_path: str = str(_CONSTRAINTS),
    router: Any | None = None,
    run_corpus: Callable[..., Any] | None = None,
    max_steps: int = 4,
) -> tuple[SampleTrace, ...]:
    """Replay each repair scenario separately while sharing one router cache."""

    directories = tuple(
        sorted(
            scenario_dirs
            if scenario_dirs is not None
            else _repair.default_scenario_dirs()
        )
    )
    sample_ids = tuple(os.path.basename(os.path.normpath(path)) for path in directories)
    if not directories or len(sample_ids) != len(set(sample_ids)):
        raise ValueError("repair scenarios must be nonempty with unique basenames")
    delegate = router or _repair.replay_router()
    runner = run_corpus or _repair.run_repair_corpus
    traces: list[SampleTrace] = []
    for scenario_dir, sample_id in zip(directories, sample_ids, strict=True):
        tracker = RequestTrackingRouter(delegate)
        result = runner(
            [scenario_dir],
            constraints_path,
            tracker,
            max_steps=max_steps,
        )
        if result.attempted != 1:
            raise ValueError(f"repair trace did not evaluate exactly one sample: {sample_id}")
        if not tracker.request_hashes:
            raise ValueError(f"repair trace contains no model request: {sample_id}")
        traces.append(
            SampleTrace(
                sample_id=sample_id,
                request_hashes=tuple(tracker.request_hashes),
            )
        )
    return tuple(traces)


def playtest_replay_traces(
    variant: PlaytestVariant,
    *,
    chain_snapshots: Sequence[Any] | None = None,
    router: Any | None = None,
    run_corpus: Callable[..., Any] | None = None,
    seed: int = 0,
    max_steps: int = _playtest.RECORD_MAX_STEPS,
) -> tuple[SampleTrace, ...]:
    """Replay one chain at a time with the exact frozen ablation shape."""

    if variant not in {"layered", "flat", "memory_on"}:
        raise ValueError(f"unknown playtest variant: {variant}")
    chains = tuple(
        chain_snapshots
        if chain_snapshots is not None
        else _playtest.default_chain_snapshots(seed=seed, n=20)
    )
    if not chains:
        raise ValueError("playtest trace corpus must be nonempty")
    delegate = router or _playtest.replay_router()
    runner = run_corpus or _playtest.run_playtest_corpus
    traces: list[SampleTrace] = []
    for index, chain in enumerate(chains):
        tracker = RequestTrackingRouter(delegate)
        kwargs: dict[str, Any] = {
            "use_planner": variant != "flat",
            "seed": seed,
            "max_steps": max_steps,
        }
        if variant == "memory_on":
            kwargs["memory_factory"] = _playtest.MemTrace
        result = runner([chain], tracker, **kwargs)
        sample_id = f"chain-{index:03d}"
        if result.n_chains != 1:
            raise ValueError(
                f"playtest trace did not evaluate exactly one sample: {sample_id}"
            )
        if not tracker.request_hashes:
            raise ValueError(f"playtest trace contains no model request: {sample_id}")
        traces.append(
            SampleTrace(
                sample_id=sample_id,
                request_hashes=tuple(tracker.request_hashes),
            )
        )
    return tuple(traces)


def _trace_source_sha256(
    *,
    protocol_id: str,
    traces: Sequence[SampleTrace],
    parameters: dict[str, Any],
) -> str:
    payload = {
        "parameters": parameters,
        "protocol_id": protocol_id,
        "traces": [trace.model_dump(mode="json") for trace in traces],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _recorded_model_snapshot(
    cassette_root: Path,
    traces: Sequence[SampleTrace],
) -> ModelSnapshot:
    store = CassetteStore(cassette_root)
    snapshots: dict[tuple[str, str, str], ModelSnapshot] = {}
    for trace in traces:
        for logical_hash in dict.fromkeys(trace.request_hashes):
            record = store.replay(logical_hash)
            if record is CASSETTE_MISS:
                raise ValueError(f"missing cassette for {logical_hash}")
            snapshot = record.model_snapshot
            key = (snapshot.provider, snapshot.model, snapshot.snapshot_tag)
            snapshots[key] = snapshot
    if len(snapshots) != 1:
        raise ValueError("one Agent workload must reference exactly one model snapshot")
    return next(iter(snapshots.values()))


def _aggregate(
    *,
    repo_root: Path,
    workload_id: str,
    cassette_root_ref: Path,
    protocol_id: str,
    source_evidence_sha256: str,
    traces: Sequence[SampleTrace],
    expected_snapshot: ModelSnapshot | None = None,
) -> AgentWorkloadEvidence:
    cassette_root = repo_root / cassette_root_ref
    recorded_snapshot = _recorded_model_snapshot(cassette_root, traces)
    if expected_snapshot is not None and recorded_snapshot != expected_snapshot:
        raise ValueError(
            f"{workload_id} cassette snapshot differs from its source evidence"
        )
    return aggregate_workload(
        workload_id=workload_id,
        model_snapshot=recorded_snapshot,
        cassette_root=cassette_root,
        cassette_root_ref=cassette_root_ref.as_posix(),
        protocol_id=protocol_id,
        source_evidence_sha256=source_evidence_sha256,
        planned_n=len(traces),
        traces=traces,
    )


def build_agent_cost_evidence(
    repo_root: str | Path = ".",
) -> AgentCostLatencyEvidence:
    """Rebuild all six measured Agent workloads under zero-network REPLAY."""

    root = Path(repo_root)
    narrative = load_narrative_evidence(root / _NARRATIVE_EVIDENCE)
    hed = load_hed_evidence(root / _HED_EVIDENCE)
    narrative_rows = narrative_verification_traces(narrative)
    hed_rows = hed_traces(hed)

    repair_rows = repair_replay_traces(
        scenario_dirs=tuple(
            str(root / path) for path in _repair.default_scenario_dirs()
        ),
        constraints_path=str(root / _CONSTRAINTS),
        router=_repair.replay_router(str(root / _REPAIR_CASSETTES)),
    )
    playtest_rows = {
        variant: playtest_replay_traces(
            variant,
            router=_playtest.replay_router(str(root / _PLAYTEST_CASSETTES)),
        )
        for variant in ("layered", "flat", "memory_on")
    }

    workloads: list[AgentWorkloadEvidence] = [
        _aggregate(
            repo_root=root,
            workload_id="narrative-verification",
            cassette_root_ref=_NARRATIVE_CASSETTES,
            protocol_id=f"narrative-protocol@1:{narrative.protocol_sha256}",
            source_evidence_sha256=narrative.evidence_sha256,
            traces=narrative_rows,
            expected_snapshot=narrative.model_snapshot,
        ),
        _aggregate(
            repo_root=root,
            workload_id="external-hed",
            cassette_root_ref=_HED_CASSETTES,
            protocol_id=f"hed-protocol@1:{hed.protocol_sha256}",
            source_evidence_sha256=hed.evidence_sha256,
            traces=hed_rows,
            expected_snapshot=hed.model_snapshot,
        ),
    ]

    repair_protocol = "repair-search@1"
    workloads.append(
        _aggregate(
            repo_root=root,
            workload_id="repair-search",
            cassette_root_ref=_REPAIR_CASSETTES,
            protocol_id=repair_protocol,
            source_evidence_sha256=_trace_source_sha256(
                protocol_id=repair_protocol,
                traces=repair_rows,
                parameters={"max_steps": 4, "scenario_count": 10},
            ),
            traces=repair_rows,
        )
    )

    for variant, workload_id in (
        ("layered", "playtest-layered"),
        ("flat", "playtest-flat"),
        ("memory_on", "playtest-memory-on"),
    ):
        protocol_id = f"playtest-{variant}@1"
        rows = playtest_rows[variant]
        workloads.append(
            _aggregate(
                repo_root=root,
                workload_id=workload_id,
                cassette_root_ref=_PLAYTEST_CASSETTES,
                protocol_id=protocol_id,
                source_evidence_sha256=_trace_source_sha256(
                    protocol_id=protocol_id,
                    traces=rows,
                    parameters={
                        "max_steps": _playtest.RECORD_MAX_STEPS,
                        "sample_count": 20,
                        "seed": 0,
                    },
                ),
                traces=rows,
            )
        )
    return seal_agent_cost_evidence(workloads)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    write_evidence(args.output, build_agent_cost_evidence(args.repo_root))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by measured CLI replay
    raise SystemExit(main())


__all__ = [
    "RequestTrackingRouter",
    "build_agent_cost_evidence",
    "hed_traces",
    "main",
    "narrative_verification_traces",
    "playtest_replay_traces",
    "repair_replay_traces",
]
