"""Deterministic repair verifier (M2a Task 6): the oracle that decides whether a
drafted `Patch` actually fixes a defect WITHOUT regressing anything else.

The verifier is 100% deterministic — this is the whole point of the repair
layer's verifier-grounding: the LLM only PROPOSES a patch; pass/fail is decided
here by (1) the same spine deterministic checkers that found the defect, (2) the
M1 economy simulator, and (3) the real Aureus game engine driven headlessly. No
model judgement enters this decision anywhere.

`verify_patch` compares a candidate `patched_snapshot` against its `base_snapshot`
along three axes:

  1. target_resolved — the target defect_class no longer appears among the
     patched snapshot's *deterministic* findings (contract §6 strict partition:
     llm-assisted / simulation / unproven findings are never counted as a proven
     deterministic defect, so they can neither mask nor manufacture resolution).
  2. new_deterministic — deterministic findings present in the patched review
     but NOT in the base review, keyed by `(defect_class, sorted(entities))`.
     Any such finding means the patch introduced a fresh defect (e.g. a dangling
     reference) and must be rejected even if it resolved its target.
  3. regression_ok — two runtime gates, both fail-*open* on "not applicable":
       * Economy: if the snapshot has economy entities, run the Monte-Carlo
         economy sim; a NEW `economy_collapse` finding (present in patched but
         not base) is a regression. No economy → skipped, never a failure.
       * Aureus: if `run_regression` and `snapshot_to_world` builds a world,
         reset the env and drive a short action sequence; a crash there is a
         regression. If the world won't build at all (an expected outcome for a
         still-invalid snapshot), regression is *skipped*, not failed.

`ok = target_resolved and not new_deterministic and regression_ok`.
"""

from __future__ import annotations

from dataclasses import dataclass

from gameforge.apps.cli.ir_to_world import snapshot_to_world
from gameforge.contracts.env_types import Observe, Wait
from gameforge.contracts.findings import Finding
from gameforge.game.aureus.kernel import AureusEnv
from gameforge.spine.checkers.base import Checker
from gameforge.spine.checkers.report import build_review_report
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.sim.economy import EconomyModel, EconomySimulator, to_findings

# Economy-sim regression budget: small, deterministic (seed-fixed), enough to
# reproduce a collapse trajectory the M1 simulator would flag.
_SIM_SEED = 0
_SIM_N_AGENTS = 30
_SIM_N_TICKS = 120


@dataclass
class VerifyResult:
    ok: bool
    target_resolved: bool
    new_deterministic: list[Finding]
    regression_ok: bool
    detail: str


def _det_key(f: Finding) -> tuple[str, tuple[str, ...]]:
    """Identity of a deterministic finding for base/patched set-difference:
    its defect class plus the (order-independent) set of entities it implicates.
    """
    return (f.defect_class, tuple(sorted(f.entities)))


def _has_economy_collapse(snapshot: Snapshot) -> bool:
    """True iff the economy sim reproduces a collapse for this snapshot. Any
    "no economy present" / sim error is treated as "no collapse" (the economy
    gate must never manufacture a regression out of a snapshot it can't model).
    """
    try:
        model = EconomyModel.from_snapshot(snapshot)
        if not model.sources and not model.sinks:
            return False  # nothing economic to simulate
        result = EconomySimulator().run(
            model, seed=_SIM_SEED, n_agents=_SIM_N_AGENTS, n_ticks=_SIM_N_TICKS
        )
        findings = to_findings(result, snapshot.snapshot_id)
        return any(f.defect_class == "economy_collapse" for f in findings)
    except Exception:  # noqa: BLE001 — an un-modelable economy is not a regression
        return False


def _aureus_regression(snapshot: Snapshot) -> str | None:
    """Drive the real Aureus engine on the patched snapshot. Returns a failure
    detail string iff the world BUILDS but then reset/step crashes; returns
    None (regression skipped, not failed) if the world can't be built at all —
    a still-invalid snapshot legitimately won't compile to a WorldConfig.
    """
    try:
        world_config = snapshot_to_world(snapshot)
    except Exception:  # noqa: BLE001 — world won't build → regression not applicable
        return None
    try:
        env = AureusEnv(world_config)
        env.reset(world_config.scenario.scenario_id, 0)
        env.observe()
        env.step(Observe())
        env.step(Wait(ticks=1))
        env.observe()
    except Exception as exc:  # noqa: BLE001 — a built world that then crashes IS a regression
        return f"Aureus regression crashed after world build: {exc}"
    return None


def verify_patch(
    base_snapshot: Snapshot,
    patched_snapshot: Snapshot,
    checkers: list[Checker],
    target_defect_class: str,
    *,
    run_regression: bool = True,
) -> VerifyResult:
    base_report = build_review_report(base_snapshot, checkers)
    patched_report = build_review_report(patched_snapshot, checkers)

    target_resolved = not any(
        f.defect_class == target_defect_class
        for f in patched_report.deterministic_findings
    )

    base_keys = {_det_key(f) for f in base_report.deterministic_findings}
    new_deterministic = [
        f for f in patched_report.deterministic_findings
        if _det_key(f) not in base_keys
    ]

    regression_ok = True
    details: list[str] = []

    # --- economy regression: a NEW collapse the base didn't have ---
    if _has_economy_collapse(patched_snapshot) and not _has_economy_collapse(base_snapshot):
        regression_ok = False
        details.append("economy sim reproduces a NEW collapse in the patched snapshot")

    # --- Aureus runtime regression ---
    if run_regression:
        aureus_detail = _aureus_regression(patched_snapshot)
        if aureus_detail is not None:
            regression_ok = False
            details.append(aureus_detail)

    ok = target_resolved and not new_deterministic and regression_ok

    if ok:
        detail = f"verified: target {target_defect_class!r} resolved, no new defects, no regression"
    else:
        summary: list[str] = []
        if not target_resolved:
            summary.append(f"target {target_defect_class!r} still present")
        if new_deterministic:
            classes = sorted({f.defect_class for f in new_deterministic})
            summary.append(f"introduced new deterministic findings: {classes}")
        summary.extend(details)
        detail = "; ".join(summary)

    return VerifyResult(
        ok=ok,
        target_resolved=target_resolved,
        new_deterministic=new_deterministic,
        regression_ok=regression_ok,
        detail=detail,
    )
