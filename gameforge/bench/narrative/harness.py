"""GPT-5.6 RECORD/REPLAY harness for frozen narrative benchmark evidence."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from gameforge.agents.base import DEFAULT_SNAPSHOT
from gameforge.agents.consistency.assistant import ConsistencyAssistant
from gameforge.bench.narrative.contracts import (
    NarrativeCase,
    canonical_case_bytes,
    to_agent_input,
)
from gameforge.bench.narrative.corpus import (
    NarrativeCorpusManifest,
    load_cases,
    load_manifest,
)
from gameforge.bench.narrative.evidence import (
    NarrativeCaseOutcome,
    NarrativeEvidenceManifest,
    canonical_evidence_bytes,
    seal_evidence_manifest,
    validate_evidence_manifest,
)
from gameforge.bench.narrative.protocol import (
    NarrativeProtocol,
    assert_verification_ready,
    canonical_protocol_bytes,
    load_protocol,
    seal_protocol,
)
from gameforge.bench.narrative.score import score_case, score_outcomes
from gameforge.contracts.agent_io import ConsistencyHint
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import (
    CassetteReplayMiss,
    ModelRouter,
    RouterMode,
)

_CORPUS_ROOT = Path("scenarios/narrative_bench")
_CORPUS_MANIFEST = _CORPUS_ROOT / "corpus-manifest.json"
_PROTOCOL_PATH = _CORPUS_ROOT / "protocol.json"
_CASSETTE_ROOT = Path("cassettes/narrative/pre-m4-1")


class _NoLiveTransport:
    def complete(self, request):  # noqa: ANN001, ANN201 - transport protocol
        raise RuntimeError(
            "REPLAY transport cannot perform a live call; cassette misses must "
            "surface as CassetteReplayMiss"
        )


def replay_router(cassettes_root: str | Path = _CASSETTE_ROOT) -> ModelRouter:
    return ModelRouter(
        _NoLiveTransport(),
        CassetteStore(cassettes_root),
        mode=RouterMode.REPLAY,
        default_model_snapshot=DEFAULT_SNAPSHOT,
    )


def record_router(cassettes_root: str | Path = _CASSETTE_ROOT) -> ModelRouter:
    if os.environ.get("GAMEFORGE_LLM_LIVE") != "1":
        raise RuntimeError(
            "narrative RECORD requires GAMEFORGE_LLM_LIVE=1 and a gateway key"
        )
    from gameforge.runtime.model_router.openai_responses_transport import (
        OpenAIResponsesTransport,
    )
    from gameforge.runtime.secrets.env import get_llm_key

    return ModelRouter(
        OpenAIResponsesTransport(
            base_url="http://localhost:4141",
            api_key=get_llm_key(),
        ),
        CassetteStore(cassettes_root),
        mode=RouterMode.RECORD,
        resume=True,
        max_retries=8,
        retry_backoff_s=3.0,
        default_model_snapshot=DEFAULT_SNAPSHOT,
    )


def _diagnostics(result) -> tuple[int, int]:  # noqa: ANN001
    perspectives = result.produced.get("perspectives", [])
    if not isinstance(perspectives, list):
        return 0, 1
    parse_failures = 0
    invalid_items = 0
    for item in perspectives:
        if not isinstance(item, dict):
            invalid_items += 1
            continue
        if item.get("parse_ok") is False:
            parse_failures += 1
        raw_items = item.get("raw_items", 0)
        accepted_items = item.get("accepted_items", 0)
        if type(raw_items) is int and type(accepted_items) is int:
            invalid_items += max(0, raw_items - accepted_items)
        else:
            invalid_items += 1
    return parse_failures, invalid_items


def _validated_hints(result) -> tuple[tuple[ConsistencyHint, ...], int]:  # noqa: ANN001
    raw_hints = result.produced.get("hints", [])
    if not isinstance(raw_hints, list):
        return (), 1
    hints: list[ConsistencyHint] = []
    invalid = 0
    for item in raw_hints:
        try:
            hints.append(ConsistencyHint.model_validate(item))
        except (TypeError, ValidationError):
            invalid += 1
    return tuple(hints), invalid


def run_cases(
    cases: Sequence[NarrativeCase],
    router: ModelRouter,
    protocol: NarrativeProtocol,
    *,
    assistant: ConsistencyAssistant | None = None,
) -> tuple[NarrativeCaseOutcome, ...]:
    """Run every case in stable ID order without dropping failed executions."""

    active_assistant = assistant or ConsistencyAssistant()
    outcomes: list[NarrativeCaseOutcome] = []
    for case in sorted(cases, key=lambda item: item.case_id):
        try:
            result = active_assistant.run(
                to_agent_input(case),
                router,
                perspectives=protocol.perspectives,
                threshold=protocol.threshold,
                rebut=protocol.rebuttal_enabled,
                model_snapshot=protocol.model_snapshot,
            )
            parse_failures, diagnostic_invalid = _diagnostics(result)
            hints, produced_invalid = _validated_hints(result)
            invalid_hint_items = diagnostic_invalid + produced_invalid
            if result.fallback_taken:
                outcome = score_case(
                    case,
                    [],
                    protocol_sha256=protocol.protocol_sha256,
                    status="fallback",
                    request_hashes=tuple(result.request_hashes),
                    parse_failures=parse_failures,
                    invalid_hint_items=invalid_hint_items,
                    failure_reason="all consistency perspectives failed to parse",
                )
            else:
                status = (
                    "partial_parse_failure"
                    if parse_failures or invalid_hint_items
                    else "evaluated"
                )
                outcome = score_case(
                    case,
                    hints,
                    protocol_sha256=protocol.protocol_sha256,
                    status=status,
                    request_hashes=tuple(result.request_hashes),
                    parse_failures=parse_failures,
                    invalid_hint_items=invalid_hint_items,
                )
        except CassetteReplayMiss as exc:
            outcome = score_case(
                case,
                [],
                protocol_sha256=protocol.protocol_sha256,
                status="cassette_miss",
                request_hashes=(exc.request_hash,),
                failure_reason=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - failure remains in denominator
            outcome = score_case(
                case,
                [],
                protocol_sha256=protocol.protocol_sha256,
                status="runner_error",
                failure_reason=f"{type(exc).__name__}: {exc}",
            )
        outcomes.append(outcome)
    return tuple(outcomes)


def _manifest_file(
    manifest: NarrativeCorpusManifest,
    split: str,
):
    for item in manifest.files:
        if item.split == split:
            return item
    raise ValueError(f"corpus manifest has no {split} file")


def _validate_frozen_cases(
    cases: Sequence[NarrativeCase],
    protocol: NarrativeProtocol,
    corpus_manifest: NarrativeCorpusManifest,
) -> tuple[NarrativeCase, ...]:
    ordered = tuple(sorted(cases, key=lambda item: item.case_id))
    if not ordered:
        raise ValueError("narrative evidence requires a nonempty frozen denominator")
    splits = {case.split for case in ordered}
    if len(splits) != 1:
        raise ValueError("narrative evidence cannot mix corpus splits")
    split = splits.pop()
    file_entry = _manifest_file(corpus_manifest, split)
    raw = b"".join(canonical_case_bytes(case) for case in ordered)
    if len(ordered) != file_entry.case_count:
        raise ValueError("case denominator count differs from frozen corpus")
    if hashlib.sha256(raw).hexdigest() != file_entry.sha256:
        raise ValueError("case denominator bytes differ from frozen corpus")
    protocol_sha = (
        protocol.development_corpus_sha256
        if split == "development"
        else protocol.verification_corpus_sha256
    )
    if file_entry.sha256 != protocol_sha:
        raise ValueError("case denominator differs from frozen protocol")
    return ordered


def build_evidence(
    cases: Sequence[NarrativeCase],
    outcomes: Sequence[NarrativeCaseOutcome],
    protocol: NarrativeProtocol,
    corpus_manifest: NarrativeCorpusManifest,
    *,
    corpus_root: str | Path = _CORPUS_ROOT,
) -> NarrativeEvidenceManifest:
    assert_verification_ready(protocol, corpus_manifest, corpus_root=corpus_root)
    ordered_cases = _validate_frozen_cases(cases, protocol, corpus_manifest)
    score = score_outcomes(outcomes, ordered_cases)
    evidence = seal_evidence_manifest(
        split=ordered_cases[0].split,
        protocol_sha256=protocol.protocol_sha256,
        corpus_manifest_sha256=corpus_manifest.manifest_sha256,
        model_snapshot=protocol.model_snapshot,
        outcomes=tuple(sorted(outcomes, key=lambda item: item.case_id)),
        by_class=score.by_class,
        clean_fp=score.clean_fp,
    )
    validate_evidence_manifest(
        evidence,
        ordered_cases,
        corpus_manifest_sha256=corpus_manifest.manifest_sha256,
        protocol_sha256=protocol.protocol_sha256,
        protocol_model_snapshot=protocol.model_snapshot,
    )
    return evidence


def write_evidence(
    path: str | Path,
    evidence: NarrativeEvidenceManifest,
) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical_evidence_bytes(evidence))


def load_evidence(path: str | Path) -> NarrativeEvidenceManifest:
    raw = Path(path).read_bytes()
    evidence = NarrativeEvidenceManifest.model_validate_json(raw)
    if canonical_evidence_bytes(evidence) != raw:
        raise ValueError("narrative evidence is not canonical JSON")
    return evidence


def _load_split(split: str) -> tuple[NarrativeCase, ...]:
    return load_cases(_CORPUS_ROOT / f"{split}.jsonl")


def _draft_protocol() -> tuple[NarrativeProtocol, NarrativeCorpusManifest]:
    manifest = load_manifest(_CORPUS_MANIFEST)
    protocol = seal_protocol(manifest)
    assert_verification_ready(protocol, manifest)
    return protocol, manifest


def _sealed_protocol() -> tuple[NarrativeProtocol, NarrativeCorpusManifest]:
    manifest = load_manifest(_CORPUS_MANIFEST)
    protocol = load_protocol(_PROTOCOL_PATH)
    assert_verification_ready(protocol, manifest)
    return protocol, manifest


def _run_split(
    split: str,
    *,
    record: bool,
    output: Path | None,
    verification: bool,
) -> NarrativeEvidenceManifest:
    protocol, manifest = _sealed_protocol() if verification else _draft_protocol()
    cases = _load_split(split)
    router = record_router() if record else replay_router()
    outcomes = run_cases(cases, router, protocol)
    evidence = build_evidence(cases, outcomes, protocol, manifest)
    if output is not None:
        write_evidence(output, evidence)
    return evidence


def _validate_evidence_file(path: Path) -> NarrativeEvidenceManifest:
    evidence = load_evidence(path)
    protocol, manifest = _sealed_protocol()
    cases = _load_split(evidence.split)
    validate_evidence_manifest(
        evidence,
        cases,
        corpus_manifest_sha256=manifest.manifest_sha256,
        protocol_sha256=protocol.protocol_sha256,
        protocol_model_snapshot=protocol.model_snapshot,
    )
    return evidence


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--record-development", action="store_true")
    actions.add_argument("--replay-development", action="store_true")
    actions.add_argument("--seal-protocol", action="store_true")
    actions.add_argument("--record-verification", action="store_true")
    actions.add_argument("--replay-verification", action="store_true")
    actions.add_argument("--validate-evidence", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.seal_protocol:
        protocol, _ = _draft_protocol()
        destination = args.output or _PROTOCOL_PATH
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(canonical_protocol_bytes(protocol))
        print(protocol.protocol_sha256)
        return
    if args.validate_evidence is not None:
        evidence = _validate_evidence_file(args.validate_evidence)
        print(evidence.evidence_sha256)
        return

    if args.record_development or args.replay_development:
        evidence = _run_split(
            "development",
            record=args.record_development,
            output=args.output,
            verification=False,
        )
    else:
        evidence = _run_split(
            "verification",
            record=args.record_verification,
            output=args.output,
            verification=True,
        )
    print(evidence.evidence_sha256)


if __name__ == "__main__":
    _main()


__all__ = [
    "build_evidence",
    "load_evidence",
    "record_router",
    "replay_router",
    "run_cases",
    "write_evidence",
]
