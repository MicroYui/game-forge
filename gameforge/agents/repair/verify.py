"""Deterministic repair verifier (M2a Task 6): the oracle that decides whether a
drafted `Patch` actually fixes a defect WITHOUT regressing anything else.

The verifier is 100% deterministic — this is the whole point of the repair
layer's verifier-grounding: the LLM only PROPOSES a patch; pass/fail is decided
here by (1) the same spine deterministic checkers that found the defect, (2) the
M1 economy simulator, and (3) the real Aureus game engine driven headlessly. No
model judgement enters this decision anywhere.

`verify_patch` compares a candidate `patched_snapshot` against its `base_snapshot`
along these axes:

  1. target_resolved — the target defect_class is GENUINELY gone. That means two
     things, both required:
       a. ABSENCE across every *proven* bucket of the patched review —
          deterministic AND simulation AND unproven findings (contract §6:
          llm-assisted findings are never counted as a proven defect and so can
          neither mask nor manufacture resolution, but simulation and unproven
          MUST be scanned). A `simulation`-oracle target such as
          `economy_collapse` lives only in `simulation_findings`, so scanning
          deterministic findings alone would call a still-collapsing economy
          "resolved" (Hole A). Degrading a proven defect into an *unproven*
          finding is likewise not a fix, so unproven is scanned too.
       b. CONTENT PRESERVATION (Hole B): every entity implicated by the target
          defect in the BASE review that still exists in the base snapshot must
          still exist in the patched snapshot. `IRGraph.remove_entity` cascades,
          so `delete_entity` of the offending subject silences the checker with
          no new finding — a "repair" that destroys the quest to fix its reward.
          The missing target of a dangling reference is (by definition) absent
          from the base, so this guard still ALLOWS the legitimate "remove the
          dangling relation" fix — only base-present subjects are protected.
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

`regression_ran` / `economy_ran` (Hole C) expose *coverage* separately from the
pass/fail booleans: a skipped gate leaves its `*_ran` flag False, so a caller
(and the Task 8 harness) can tell a genuinely-vetted pass from one where the
gate never actually ran. Neither flag being False ever means "failed" — that is
what `regression_ok` is for.

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
    # --- coverage flags (Hole C): True only when the gate actually ran ---
    regression_ran: bool = False
    economy_ran: bool = False


def _det_key(f: Finding) -> tuple[str, tuple[str, ...]]:
    """Identity of a deterministic finding for base/patched set-difference:
    its defect class plus the (order-independent) set of entities it implicates.
    """
    return (f.defect_class, tuple(sorted(f.entities)))


def _economy_findings(snapshot: Snapshot) -> tuple[list[Finding], bool]:
    """Run the M1 economy sim on `snapshot`; return `(findings, ran)`.

    `ran` is True iff the snapshot actually modelled an economy (had at least
    one source or sink) and the simulator executed without error. A snapshot
    with no economic entities, or one the sim can't model, yields `([], False)`
    — an un-modelable economy is never a regression and never counts as covered.
    """
    try:
        model = EconomyModel.from_snapshot(snapshot)
        if not model.sources and not model.sinks:
            return [], False  # nothing economic to simulate
        result = EconomySimulator().run(
            model, seed=_SIM_SEED, n_agents=_SIM_N_AGENTS, n_ticks=_SIM_N_TICKS
        )
        return to_findings(result, snapshot.snapshot_id, model=model), True
    except Exception:  # noqa: BLE001 — an un-modelable economy is not a regression
        return [], False


def _has_collapse(findings: list[Finding]) -> bool:
    return any(f.defect_class == "economy_collapse" for f in findings)


def _aureus_regression(snapshot: Snapshot) -> tuple[bool, str | None]:
    """Drive the real Aureus engine on the patched snapshot.

    Returns `(ran, detail)`:
      * world won't build   -> `(False, None)`  — regression *skipped* (a
        still-invalid snapshot legitimately won't compile to a WorldConfig).
      * built + stepped OK   -> `(True, None)`   — the gate genuinely ran clean.
      * built but then crashed -> `(False, detail)` — a real regression; `ran`
        stays False because the gate did not complete a clean pass (Hole C:
        `regression_ran` means "built + reset/stepped without error").
    """
    try:
        world_config = snapshot_to_world(snapshot)
    except Exception:  # noqa: BLE001 — world won't build → regression not applicable
        return False, None
    try:
        env = AureusEnv(world_config)
        env.reset(world_config.scenario.scenario_id, 0)
        env.observe()
        env.step(Observe())
        env.step(Wait(ticks=1))
        env.observe()
    except Exception as exc:  # noqa: BLE001 — a built world that then crashes IS a regression
        return False, f"Aureus regression crashed after world build: {exc}"
    return True, None


def _target_entities_in_base(report, target_defect_class: str) -> set[str]:
    """Entity ids implicated by the target defect across the base review's
    proven buckets (deterministic + simulation + unproven). These are the
    subjects a genuine fix must NOT destroy (Hole B)."""
    return {
        e
        for f in (
            report.deterministic_findings + report.simulation_findings + report.unproven_findings
        )
        if f.defect_class == target_defect_class
        for e in f.entities
    }


def verify_patch(
    base_snapshot: Snapshot,
    patched_snapshot: Snapshot,
    checkers: list[Checker],
    target_defect_class: str,
    *,
    run_regression: bool = True,
    run_economy: bool = True,
) -> VerifyResult:
    # M2 callers use the frozen local economy budget by default. M4 callers can
    # disable it when simulation authority comes from exact admitted profiles;
    # those profiles are executed and evidenced by the outer handler instead.
    if run_economy:
        base_sim_findings, _ = _economy_findings(base_snapshot)
        patched_sim_findings, economy_ran = _economy_findings(patched_snapshot)
    else:
        base_sim_findings, patched_sim_findings, economy_ran = [], [], False

    base_report = build_review_report(
        base_snapshot, checkers, sim_findings=tuple(base_sim_findings)
    )
    patched_report = build_review_report(
        patched_snapshot, checkers, sim_findings=tuple(patched_sim_findings)
    )

    # --- target resolution (Hole A): absent from every PROVEN bucket ---
    patched_proven = (
        patched_report.deterministic_findings
        + patched_report.simulation_findings
        + patched_report.unproven_findings
    )
    target_absent = not any(f.defect_class == target_defect_class for f in patched_proven)

    # --- content preservation (Hole B): base-present subjects must survive ---
    base_graph = base_snapshot.to_graph()
    patched_graph = patched_snapshot.to_graph()
    deleted_subjects = sorted(
        e
        for e in _target_entities_in_base(base_report, target_defect_class)
        if base_graph.get_node(e) is not None and patched_graph.get_node(e) is None
    )

    # Deleting the offending subject makes the defect "vanish" from the patched
    # review, so `target_absent` is True — but that is delete-to-silence, not a
    # fix. Fold the guard into `target_resolved` so it means GENUINELY resolved.
    target_resolved = target_absent and not deleted_subjects

    base_keys = {_det_key(f) for f in base_report.deterministic_findings}
    new_deterministic = [
        f for f in patched_report.deterministic_findings if _det_key(f) not in base_keys
    ]

    regression_ok = True
    regression_ran = False
    details: list[str] = []

    # --- economy regression: a NEW collapse the base didn't have ---
    if _has_collapse(patched_sim_findings) and not _has_collapse(base_sim_findings):
        regression_ok = False
        details.append("economy sim reproduces a NEW collapse in the patched snapshot")

    # --- Aureus runtime regression ---
    if run_regression:
        regression_ran, aureus_detail = _aureus_regression(patched_snapshot)
        if aureus_detail is not None:
            regression_ok = False
            details.append(aureus_detail)

    ok = target_resolved and not new_deterministic and regression_ok

    if ok:
        detail = f"verified: target {target_defect_class!r} resolved, no new defects, no regression"
    else:
        summary: list[str] = []
        if not target_absent:
            summary.append(f"target {target_defect_class!r} still present")
        elif deleted_subjects:
            summary.append(
                f"target {target_defect_class!r} only silenced by deleting base "
                f"entities {deleted_subjects} (delete-to-silence, not a genuine fix)"
            )
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
        regression_ran=regression_ran,
        economy_ran=economy_ran,
    )
