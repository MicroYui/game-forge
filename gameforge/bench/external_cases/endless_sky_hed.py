"""Compose the frozen Endless Sky corpus with the source-neutral HED harness."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from gameforge.bench.external_cases.contracts import ExternalCorpusManifest
from gameforge.bench.external_cases.endless_sky_runner import load_case_runtime
from gameforge.bench.external_cases.qualify import load_manifest
from gameforge.bench.hed.contracts import (
    HedCaseOutcome,
    HedEvidenceManifest,
    load_evidence,
    write_evidence,
)
from gameforge.bench.hed.harness import (
    HedCaseInput,
    build_hed_evidence,
    record_router,
    replay_router,
    run_hed_cases,
    validate_hed_evidence,
)
from gameforge.bench.hed.protocol import (
    HedProtocol,
    assert_protocol_ready,
    load_protocol,
    seal_protocol,
    write_protocol,
)

_CORPUS_ROOT = Path("scenarios/external_cases/endless_sky")
_MANIFEST_PATH = _CORPUS_ROOT / "external-corpus-manifest.json"
_PROTOCOL_PATH = _CORPUS_ROOT / "hed-protocol.json"
_EVIDENCE_PATH = _CORPUS_ROOT / "hed-evidence.json"
_CASSETTE_ROOT = Path("cassettes/hed/pre-m4-1")


def load_endless_sky_hed_inputs(
    corpus_root: str | Path,
    manifest: ExternalCorpusManifest,
) -> tuple[HedCaseInput, ...]:
    cases: list[HedCaseInput] = []
    for evidence in sorted(manifest.cases, key=lambda item: item.spec.case_id):
        runtime = load_case_runtime(corpus_root, evidence.spec)
        cases.append(
            HedCaseInput(
                case_id=evidence.spec.case_id,
                external_case_evidence_sha256=evidence.evidence_sha256,
                before_snapshot=runtime.before_snapshot,
                human_target_snapshot=runtime.human_target_snapshot,
                target_finding=runtime.target_finding,
            )
        )
    return tuple(cases)


def seal_endless_sky_hed_protocol(
    *,
    corpus_root: str | Path = _CORPUS_ROOT,
    manifest_path: str | Path = _MANIFEST_PATH,
    output: str | Path = _PROTOCOL_PATH,
) -> HedProtocol:
    manifest = load_manifest(manifest_path)
    cases = load_endless_sky_hed_inputs(corpus_root, manifest)
    protocol = seal_protocol(manifest)
    if tuple(item.case_id for item in cases) != protocol.external_case_ids:
        raise ValueError("reconstructable cases differ from the frozen HED protocol")
    assert_protocol_ready(protocol, manifest, manifest_path=manifest_path)
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_protocol(destination, protocol)
    return protocol


def build_endless_sky_hed_evidence(
    cases: Sequence[HedCaseInput],
    outcomes: Sequence[HedCaseOutcome],
    protocol: HedProtocol,
    manifest: ExternalCorpusManifest,
) -> HedEvidenceManifest:
    return build_hed_evidence(cases, outcomes, protocol, manifest)


def _load_frozen(
    *,
    corpus_root: str | Path = _CORPUS_ROOT,
    manifest_path: str | Path = _MANIFEST_PATH,
    protocol_path: str | Path = _PROTOCOL_PATH,
) -> tuple[HedProtocol, ExternalCorpusManifest, tuple[HedCaseInput, ...]]:
    manifest = load_manifest(manifest_path)
    protocol = load_protocol(protocol_path)
    assert_protocol_ready(protocol, manifest, manifest_path=manifest_path)
    cases = load_endless_sky_hed_inputs(corpus_root, manifest)
    return protocol, manifest, cases


def _run(
    *,
    record: bool,
    output: Path | None,
    cassettes_root: Path,
) -> HedEvidenceManifest:
    protocol, manifest, cases = _load_frozen()
    router = record_router(cassettes_root) if record else replay_router(cassettes_root)
    outcomes = run_hed_cases(cases, router, protocol)
    evidence = build_endless_sky_hed_evidence(
        cases,
        outcomes,
        protocol,
        manifest,
    )
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        write_evidence(output, evidence)
    return evidence


def _validate(path: Path) -> HedEvidenceManifest:
    protocol, manifest, cases = _load_frozen()
    evidence = load_evidence(path)
    validate_hed_evidence(evidence, cases, protocol, manifest)
    return evidence


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--seal-protocol", action="store_true")
    actions.add_argument("--record", action="store_true")
    actions.add_argument("--replay", action="store_true")
    actions.add_argument("--validate-evidence", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cassette-root", type=Path, default=_CASSETTE_ROOT)
    args = parser.parse_args()

    if args.seal_protocol:
        protocol = seal_endless_sky_hed_protocol(output=args.output or _PROTOCOL_PATH)
        print(protocol.protocol_sha256)
        return
    if args.validate_evidence is not None:
        evidence = _validate(args.validate_evidence)
        print(evidence.evidence_sha256)
        return
    evidence = _run(
        record=args.record,
        output=(args.output or _EVIDENCE_PATH) if args.record else args.output,
        cassettes_root=args.cassette_root,
    )
    print(evidence.evidence_sha256)


if __name__ == "__main__":
    _main()


__all__ = [
    "build_endless_sky_hed_evidence",
    "load_endless_sky_hed_inputs",
    "seal_endless_sky_hed_protocol",
]
