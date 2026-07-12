"""Source-neutral GPT-5.6 RECORD/REPLAY harness for HED evidence."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from gameforge.agents.base import DEFAULT_SNAPSHOT
from gameforge.agents.repair.search import repair_search
from gameforge.agents.repair.verify import verify_patch
from gameforge.bench.hed.contracts import (
    AtomicDeltaModel,
    HedCaseOutcome,
    HedEvidenceManifest,
    seal_evidence_manifest,
    seal_outcome,
    validate_evidence_manifest,
)
from gameforge.bench.hed.delta import semantic_delta
from gameforge.bench.hed.protocol import HedProtocol, assert_protocol_ready
from gameforge.contracts.findings import Finding, Patch
from gameforge.contracts.model_router import ModelRequest, ModelResponse, request_hash
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import (
    CassetteReplayMiss,
    ModelRouter,
    RouterMode,
)
from gameforge.spine.checkers.asp import ASPChecker
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import PatchRejected, apply_patch

_CASSETTE_ROOT = Path("cassettes/hed/pre-m4-1")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class HedCaseInput:
    case_id: str
    external_case_evidence_sha256: str
    before_snapshot: Snapshot
    human_target_snapshot: Snapshot
    target_finding: Finding

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("HED case_id must not be blank")
        if _SHA256.fullmatch(self.external_case_evidence_sha256) is None:
            raise ValueError("HED external case evidence hash must be SHA-256")
        if self.before_snapshot.snapshot_id == self.human_target_snapshot.snapshot_id:
            raise ValueError("HED human target must differ from the before snapshot")
        if self.target_finding.snapshot_id != self.before_snapshot.snapshot_id:
            raise ValueError("HED target Finding must belong to the before snapshot")
        if (
            self.target_finding.oracle_type != "deterministic"
            or self.target_finding.status != "confirmed"
        ):
            raise ValueError("HED target Finding must be confirmed and deterministic")
        if not semantic_delta(self.before_snapshot, self.human_target_snapshot):
            raise ValueError("HED upstream human target has no semantic delta")


class TrackingRouter:
    """Record every logical Agent request without changing the Repair API."""

    def __init__(self, router: ModelRouter) -> None:
        self._router = router
        self.request_hashes: list[str] = []

    @property
    def default_model_snapshot(self):  # noqa: ANN201 - mirrors ModelRouter
        return self._router.default_model_snapshot

    def call(self, request: ModelRequest) -> ModelResponse:
        self.request_hashes.append(request_hash(request))
        return self._router.call(request)


class _NoLiveTransport:
    def complete(self, request):  # noqa: ANN001, ANN201 - transport protocol
        raise RuntimeError(
            "HED REPLAY cannot perform a live call; cassette misses must surface"
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
            "HED RECORD requires GAMEFORGE_LLM_LIVE=1 and GAMEFORGE_LLM_KEY"
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


def _checkers():
    return [GraphChecker(), ASPChecker()]


def _delta_models(before: Snapshot, after: Snapshot) -> tuple[AtomicDeltaModel, ...]:
    return tuple(AtomicDeltaModel.from_delta(item) for item in semantic_delta(before, after))


def _protocol_failure(
    case: HedCaseInput,
    protocol: HedProtocol,
    tracker: TrackingRouter,
    human_delta: tuple[AtomicDeltaModel, ...],
    reason: str,
    *,
    patch: Patch | None = None,
) -> HedCaseOutcome:
    return seal_outcome(
        case_id=case.case_id,
        external_case_evidence_sha256=case.external_case_evidence_sha256,
        protocol_sha256=protocol.protocol_sha256,
        status="protocol_failure",
        before_snapshot_id=case.before_snapshot.snapshot_id,
        human_target_snapshot_id=case.human_target_snapshot.snapshot_id,
        target_finding=case.target_finding,
        request_hashes=tuple(tracker.request_hashes),
        search_steps=len(tracker.request_hashes),
        patch=patch,
        passed_verification=False,
        agent_target_snapshot_id=None,
        human_delta=human_delta,
        agent_delta=(),
        failure_reason=reason,
    )


def run_hed_case(
    case: HedCaseInput,
    router: ModelRouter | TrackingRouter,
    protocol: HedProtocol,
) -> HedCaseOutcome:
    """Run one bounded Repair search and independently reverify its final Patch."""

    tracker = router if isinstance(router, TrackingRouter) else TrackingRouter(router)
    human_delta = _delta_models(case.before_snapshot, case.human_target_snapshot)
    patch: Patch | None = None
    if case.case_id not in protocol.external_case_ids:
        return _protocol_failure(
            case,
            protocol,
            tracker,
            human_delta,
            "case ID is absent from the frozen HED denominator",
        )
    if tracker.default_model_snapshot != protocol.model_snapshot:
        return _protocol_failure(
            case,
            protocol,
            tracker,
            human_delta,
            "router model snapshot differs from the frozen HED protocol",
        )

    try:
        checkers = _checkers()
        draft = repair_search(
            case.target_finding,
            case.before_snapshot,
            checkers,
            tracker,  # type: ignore[arg-type] - facade intentionally matches ModelRouter
            max_steps=protocol.max_steps,
            run_regression=protocol.run_regression,
        )
        patch = draft.patch
        if draft.search_steps != len(tracker.request_hashes):
            raise RuntimeError("Repair search step count differs from request trace")

        try:
            agent_target = apply_patch(case.before_snapshot, patch)
        except PatchRejected as exc:
            if draft.passed_verification:
                raise RuntimeError(
                    f"Repair search marked an inapplicable Patch verified: {exc.reason}"
                ) from exc
            return seal_outcome(
                case_id=case.case_id,
                external_case_evidence_sha256=case.external_case_evidence_sha256,
                protocol_sha256=protocol.protocol_sha256,
                status="agent_unusable",
                before_snapshot_id=case.before_snapshot.snapshot_id,
                human_target_snapshot_id=case.human_target_snapshot.snapshot_id,
                target_finding=case.target_finding,
                request_hashes=tuple(tracker.request_hashes),
                search_steps=draft.search_steps,
                patch=patch,
                passed_verification=False,
                agent_target_snapshot_id=None,
                human_delta=human_delta,
                agent_delta=(),
                failure_reason=f"Patch rejected: {exc.reason}",
            )

        verification = verify_patch(
            case.before_snapshot,
            agent_target,
            checkers,
            case.target_finding.defect_class,
            run_regression=protocol.run_regression,
        )
        if draft.passed_verification != verification.ok:
            raise RuntimeError(
                "Repair search result disagrees with independent deterministic verification"
            )
        if not verification.ok:
            return seal_outcome(
                case_id=case.case_id,
                external_case_evidence_sha256=case.external_case_evidence_sha256,
                protocol_sha256=protocol.protocol_sha256,
                status="agent_unusable",
                before_snapshot_id=case.before_snapshot.snapshot_id,
                human_target_snapshot_id=case.human_target_snapshot.snapshot_id,
                target_finding=case.target_finding,
                request_hashes=tuple(tracker.request_hashes),
                search_steps=draft.search_steps,
                patch=patch,
                passed_verification=False,
                agent_target_snapshot_id=None,
                human_delta=human_delta,
                agent_delta=(),
                failure_reason=verification.detail,
            )

        return seal_outcome(
            case_id=case.case_id,
            external_case_evidence_sha256=case.external_case_evidence_sha256,
            protocol_sha256=protocol.protocol_sha256,
            status="evaluated",
            before_snapshot_id=case.before_snapshot.snapshot_id,
            human_target_snapshot_id=case.human_target_snapshot.snapshot_id,
            target_finding=case.target_finding,
            request_hashes=tuple(tracker.request_hashes),
            search_steps=draft.search_steps,
            patch=patch,
            passed_verification=True,
            agent_target_snapshot_id=agent_target.snapshot_id,
            human_delta=human_delta,
            agent_delta=_delta_models(case.before_snapshot, agent_target),
        )
    except CassetteReplayMiss as exc:
        return _protocol_failure(
            case,
            protocol,
            tracker,
            human_delta,
            str(exc),
            patch=patch,
        )
    except Exception as exc:  # noqa: BLE001 - infrastructure stays in denominator
        return _protocol_failure(
            case,
            protocol,
            tracker,
            human_delta,
            f"{type(exc).__name__}: {exc}",
            patch=patch,
        )


def run_hed_cases(
    cases: Sequence[HedCaseInput],
    router: ModelRouter,
    protocol: HedProtocol,
) -> tuple[HedCaseOutcome, ...]:
    ordered = tuple(sorted(cases, key=lambda item: item.case_id))
    if len(ordered) != len({item.case_id for item in ordered}):
        raise ValueError("HED case inputs must have unique case IDs")
    return tuple(run_hed_case(case, router, protocol) for case in ordered)


def _validate_denominator(
    cases: Sequence[HedCaseInput],
    outcomes: Sequence[HedCaseOutcome],
    protocol: HedProtocol,
) -> tuple[tuple[HedCaseInput, ...], tuple[HedCaseOutcome, ...]]:
    ordered_cases = tuple(sorted(cases, key=lambda item: item.case_id))
    ordered_outcomes = tuple(sorted(outcomes, key=lambda item: item.case_id))
    case_ids = tuple(item.case_id for item in ordered_cases)
    outcome_ids = tuple(item.case_id for item in ordered_outcomes)
    if len(case_ids) != 8 or len(case_ids) != len(set(case_ids)):
        raise ValueError("HED requires exactly eight unique case inputs")
    if outcome_ids != case_ids or case_ids != protocol.external_case_ids:
        raise ValueError("HED cases, outcomes, and protocol denominator differ")
    return ordered_cases, ordered_outcomes


def validate_hed_evidence(
    evidence: HedEvidenceManifest,
    cases: Sequence[HedCaseInput],
    protocol: HedProtocol,
    external_manifest,
) -> None:  # noqa: ANN001 - source-neutral external manifest contract
    validate_evidence_manifest(
        evidence,
        protocol=protocol,
        external_manifest=external_manifest,
    )
    ordered_cases, ordered_outcomes = _validate_denominator(
        cases,
        evidence.outcomes,
        protocol,
    )
    for case, outcome in zip(ordered_cases, ordered_outcomes, strict=True):
        if outcome.before_snapshot_id != case.before_snapshot.snapshot_id:
            raise ValueError(f"before snapshot mismatch for {case.case_id}")
        if outcome.human_target_snapshot_id != case.human_target_snapshot.snapshot_id:
            raise ValueError(f"human target snapshot mismatch for {case.case_id}")
        if outcome.target_finding != case.target_finding:
            raise ValueError(f"target Finding mismatch for {case.case_id}")
        if outcome.human_delta != _delta_models(
            case.before_snapshot,
            case.human_target_snapshot,
        ):
            raise ValueError(f"human delta does not rederive for {case.case_id}")
        if outcome.status == "agent_unusable":
            if outcome.patch is None:
                raise ValueError(f"unusable outcome lacks Patch for {case.case_id}")
            try:
                unusable_target = apply_patch(case.before_snapshot, outcome.patch)
            except PatchRejected:
                continue
            unusable_verification = verify_patch(
                case.before_snapshot,
                unusable_target,
                _checkers(),
                case.target_finding.defect_class,
                run_regression=protocol.run_regression,
            )
            if unusable_verification.ok:
                raise ValueError(
                    f"unusable outcome passes verification for {case.case_id}"
                )
            continue
        if outcome.status == "protocol_failure":
            continue
        if outcome.patch is None:
            raise ValueError(f"evaluated outcome lacks Patch for {case.case_id}")
        try:
            agent_target = apply_patch(case.before_snapshot, outcome.patch)
        except PatchRejected as exc:
            raise ValueError(
                f"stored Agent Patch is inapplicable for {case.case_id}: {exc.reason}"
            ) from exc
        verification = verify_patch(
            case.before_snapshot,
            agent_target,
            _checkers(),
            case.target_finding.defect_class,
            run_regression=protocol.run_regression,
        )
        if not verification.ok:
            raise ValueError(f"stored Agent Patch fails verification for {case.case_id}")
        if outcome.agent_target_snapshot_id != agent_target.snapshot_id:
            raise ValueError(f"Agent target snapshot mismatch for {case.case_id}")
        if outcome.agent_delta != _delta_models(case.before_snapshot, agent_target):
            raise ValueError(f"Agent delta does not rederive for {case.case_id}")


def build_hed_evidence(
    cases: Sequence[HedCaseInput],
    outcomes: Sequence[HedCaseOutcome],
    protocol: HedProtocol,
    external_manifest,
) -> HedEvidenceManifest:  # noqa: ANN001 - source-neutral external manifest contract
    assert_protocol_ready(protocol, external_manifest)
    ordered_cases, ordered_outcomes = _validate_denominator(cases, outcomes, protocol)
    evidence = seal_evidence_manifest(
        protocol_sha256=protocol.protocol_sha256,
        external_manifest_sha256=external_manifest.manifest_sha256,
        model_snapshot=protocol.model_snapshot,
        outcomes=ordered_outcomes,
    )
    validate_hed_evidence(
        evidence,
        ordered_cases,
        protocol,
        external_manifest,
    )
    return evidence


__all__ = [
    "HedCaseInput",
    "TrackingRouter",
    "build_hed_evidence",
    "record_router",
    "replay_router",
    "run_hed_case",
    "run_hed_cases",
    "validate_hed_evidence",
]
