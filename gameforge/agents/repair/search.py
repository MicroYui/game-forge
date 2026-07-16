"""Verifier-guided repair search (M2a Task 6): propose → verify → refine loop.

This is the core of the repair layer's Fix-Pass-Rate story. Each round the
`RepairDrafter` PROPOSES a typed patch; the patch is applied deterministically
(`spine.patch.apply_patch`) and judged by the deterministic verifier
(`agents.repair.verify.verify_patch` — spine checkers + economy sim + Aureus).
The LLM never decides success: only the verifier does. When a round fails, a
concrete counterexample (target still present / new deterministic findings /
regression) is fed back into the next `draft` so the model re-drafts against
ground truth, not vibes.

The loop is bounded by `max_steps`. It returns the FIRST patch that passes the
verifier (`passed_verification=True`); if the budget is exhausted it returns the
last patch actually attempted (or a minimal empty patch if the model never
produced a valid one), with `passed_verification=False` — an honest "no verified
fix found", never a silently-unverified patch dressed up as a pass.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from gameforge.agents.repair.drafter import RepairDrafter, build_repair_user_prompt
from gameforge.agents.repair.verify import VerifyResult, verify_patch
from gameforge.contracts.agent_io import PatchDraft
from gameforge.contracts.findings import Finding, Patch
from gameforge.runtime.model_router.router import ModelRouter
from gameforge.spine.checkers.base import Checker
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import PatchRejected, apply_patch


@dataclass(frozen=True, slots=True)
class RepairPromptRoundContext:
    """Exact semantic state used to assemble one initial/refine model request.

    M4 supplies a callback that turns this deterministic state into an immutable
    ``agent-prompt-context@1`` before the corresponding model call.  Legacy M2
    harnesses omit the callback and preserve their historical request identity.
    """

    phase: Literal["initial", "refine"]
    finding: Finding
    snapshot: Snapshot
    counterexample: str | None
    previous_patch: Patch | None
    previous_verdict: VerifyResult | None
    user_prompt: str


RepairPromptContextHook = Callable[[RepairPromptRoundContext], None]


def _empty_patch(finding: Finding, snapshot: Snapshot) -> Patch:
    """Fallback when the search never produced a single applicable patch: a
    well-formed no-op patch that carries the target finding it failed to fix."""
    return Patch(
        id=f"repair-empty@{snapshot.snapshot_id[:16]}",
        base_snapshot_id=snapshot.snapshot_id,
        target_snapshot_id="",
        side_effect_risk="low",
        ops=[],
        produced_by="agent",
        producer_run_id="repair-search",
        rationale="verifier-guided repair exhausted with no passing patch",
        expected_to_fix=[finding.id],
    )


def _summarize_failure(result: VerifyResult, target_defect_class: str) -> str:
    parts: list[str] = []
    if not result.target_resolved:
        # `result.detail` distinguishes "still present" from delete-to-silence,
        # so the drafter re-drafts against the precise reason (e.g. don't delete
        # the offending subject to make the defect vanish).
        parts.append(
            f"the target defect {target_defect_class!r} is not genuinely resolved ({result.detail})"
        )
    if result.new_deterministic:
        classes = sorted({f.defect_class for f in result.new_deterministic})
        parts.append(f"the patch introduced new deterministic defects: {classes}")
    if not result.regression_ok:
        parts.append(f"the patch caused a runtime regression ({result.detail})")
    return "; ".join(parts) if parts else "the patch failed deterministic verification"


def repair_search(
    finding: Finding,
    snapshot: Snapshot,
    checkers: list[Checker],
    router: ModelRouter,
    *,
    max_steps: int = 4,
    run_regression: bool = True,
    run_economy: bool = True,
    candidate_verifier: Callable[[Snapshot, Snapshot, list[Checker], str], VerifyResult]
    | None = None,
    prompt_context_hook: RepairPromptContextHook | None = None,
) -> PatchDraft:
    drafter = RepairDrafter()
    counterexample: str | None = None
    last_patch: Patch | None = None
    previous_patch: Patch | None = None
    previous_verdict: VerifyResult | None = None

    for i in range(max_steps):
        if prompt_context_hook is not None:
            prompt_context_hook(
                RepairPromptRoundContext(
                    phase="initial" if counterexample is None else "refine",
                    finding=finding,
                    snapshot=snapshot,
                    counterexample=counterexample,
                    previous_patch=previous_patch,
                    previous_verdict=previous_verdict,
                    user_prompt=build_repair_user_prompt(
                        finding,
                        snapshot,
                        counterexample=counterexample,
                    ),
                )
            )
        patch = drafter.draft(finding, snapshot, router, counterexample=counterexample)
        if patch is None:
            counterexample = "model produced no valid ops"
            previous_patch = None
            previous_verdict = None
            continue
        last_patch = patch

        try:
            patched = apply_patch(snapshot, patch)
        except PatchRejected as exc:
            counterexample = f"the patch was rejected as inapplicable: {exc.reason}"
            previous_patch = patch
            previous_verdict = None
            continue

        result = (
            candidate_verifier(snapshot, patched, checkers, finding.defect_class)
            if candidate_verifier is not None
            else verify_patch(
                snapshot,
                patched,
                checkers,
                finding.defect_class,
                run_regression=run_regression,
                run_economy=run_economy,
            )
        )
        if result.ok:
            return PatchDraft(patch=patch, search_steps=i + 1, passed_verification=True)
        counterexample = _summarize_failure(result, finding.defect_class)
        previous_patch = patch
        previous_verdict = result

    return PatchDraft(
        patch=last_patch if last_patch is not None else _empty_patch(finding, snapshot),
        search_steps=max_steps,
        passed_verification=False,
    )
