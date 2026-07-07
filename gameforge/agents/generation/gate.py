"""Generation gate (M2a-part2 Task 7): the deterministic oracle that decides
whether a Content Generator proposal may become a candidate.

Generated content is ALWAYS a proposal (contract §7.5, `ContentProposal.
passed_gate`) — `gate_proposal` is the only thing allowed to flip that bit, and
it never trusts the model's own claim about what its ops do. The check mirrors
`agents.repair.verify.verify_patch`'s already-solved new-finding diff exactly:
build a `Patch` from the proposed ops, `apply_patch` it, and compare the
patched review against the base review by the same
`(defect_class, tuple(sorted(entities)))` identity key. The one thing this gate
does NOT need (unlike repair's verifier) is target resolution — a generation
proposal has no target defect to resolve, only new defects to avoid introducing.

Fail-closed at every step:
  - a proposed op that isn't a well-formed `TypedOp` (unknown op kind, missing/
    non-string target) means the WHOLE proposal is rejected outright (`(False,
    [])`) — same structural drop-or-keep-nothing posture as `repair.drafter`,
    except here a partially-malformed ops array can't be silently truncated
    and re-gated, since that would gate a DIFFERENT patch than the model
    actually proposed.
  - `apply_patch` raising `PatchRejected` (stale `old_value`, bad precondition,
    inapplicable op) also means the gate cannot pass: `(False, [])`.
  - any NEW deterministic finding (present in the patched review, absent from
    the base review) blocks the proposal.
  - a NEW `economy_collapse` (present in the patched sim findings, absent from
    the base sim findings) blocks the proposal — same boolean-regression check
    `verify.py` runs, but here the collapse Finding itself is folded into the
    blocking list (repair's verifier only needs a boolean; generation surfaces
    the concrete Finding so the caller can report *why* it was blocked).

`passed = (no new deterministic findings) and (no new economy_collapse)`.
"""

from __future__ import annotations

from gameforge.contracts.findings import Finding, Patch, TypedOp
from gameforge.spine.checkers.base import Checker
from gameforge.spine.checkers.report import build_review_report
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import PatchRejected, apply_patch
from gameforge.spine.sim.economy import EconomyModel, EconomySimulator, to_findings

_VALID_OP_KINDS = {
    "add_entity", "delete_entity", "set_entity_attr",
    "add_relation", "delete_relation", "set_relation_attr", "replace_subgraph",
}

# Economy-sim gate budget: small, deterministic (seed-fixed) — same knobs as
# the repair verifier's regression budget (`agents.repair.verify`).
_SIM_SEED = 0
_SIM_N_AGENTS = 30
_SIM_N_TICKS = 120


def _det_key(f: Finding) -> tuple[str, tuple[str, ...]]:
    """Identity of a deterministic finding for base/patched set-difference:
    its defect class plus the (order-independent) set of entities it implicates.
    Mirrors `agents.repair.verify._det_key` exactly — same key, same purpose."""
    return (f.defect_class, tuple(sorted(f.entities)))


def _build_ops(proposed_ops: list[dict]) -> list[TypedOp] | None:
    """Structural fail-closed oracle: every element must be a well-formed
    `TypedOp` (known op kind, non-empty string target). Returns `None` (never
    a partial list) if ANY element fails to validate — the gate must judge the
    patch the model actually proposed, not a silently-truncated stand-in."""
    ops: list[TypedOp] = []
    for i, item in enumerate(proposed_ops):
        if not isinstance(item, dict):
            return None
        op_kind = item.get("op")
        if op_kind not in _VALID_OP_KINDS:
            return None
        target = item.get("target")
        if not isinstance(target, str) or not target:
            return None
        ops.append(
            TypedOp(
                op_id=str(item.get("op_id", f"op{i}")),
                op=op_kind,  # type: ignore[arg-type]  # validated against _VALID_OP_KINDS
                target=target,
                old_value=item.get("old_value"),
                new_value=item.get("new_value"),
                source_ref=item.get("source_ref"),
            )
        )
    return ops


def _economy_findings(snapshot: Snapshot) -> list[Finding]:
    """Run the M1 economy sim on `snapshot`; `[]` if there's nothing economic
    to simulate or the sim can't model it (an un-modelable economy is never a
    gate failure). Mirrors `agents.repair.verify._economy_findings`."""
    try:
        model = EconomyModel.from_snapshot(snapshot)
        if not model.sources and not model.sinks:
            return []
        result = EconomySimulator().run(
            model, seed=_SIM_SEED, n_agents=_SIM_N_AGENTS, n_ticks=_SIM_N_TICKS
        )
        return to_findings(result, snapshot.snapshot_id, model=model)
    except Exception:  # noqa: BLE001 — an un-modelable economy is not a gate failure
        return []


def gate_proposal(
    base_snapshot: Snapshot,
    proposed_ops: list[dict],
    checkers: list[Checker],
) -> tuple[bool, list[Finding]]:
    """Gate a Content Generator proposal against `base_snapshot`.

    Returns `(passed, blocking_findings)`. `blocking_findings` is always the
    complete set of NEW findings (deterministic + a new economy_collapse, if
    any) that block the proposal — empty whenever `passed` is True, and also
    empty for a structurally-rejected proposal (fail-closed rejections have no
    single finding to point at).
    """
    ops = _build_ops(proposed_ops)
    if ops is None:
        return False, []  # malformed ops -> fail-closed, never a candidate

    patch = Patch(
        id=f"generation@{base_snapshot.snapshot_id[:16]}",
        base_snapshot_id=base_snapshot.snapshot_id,
        target_snapshot_id="",
        side_effect_risk="low",
        ops=ops,
        produced_by="agent",
        producer_run_id="generation",
        rationale="generated content proposal",
    )
    try:
        patched_snapshot = apply_patch(base_snapshot, patch)
    except PatchRejected:
        return False, []  # rejected proposals never pass the gate

    base_sim_findings = _economy_findings(base_snapshot)
    patched_sim_findings = _economy_findings(patched_snapshot)

    base_report = build_review_report(
        base_snapshot, checkers, sim_findings=tuple(base_sim_findings)
    )
    patched_report = build_review_report(
        patched_snapshot, checkers, sim_findings=tuple(patched_sim_findings)
    )

    base_keys = {_det_key(f) for f in base_report.deterministic_findings}
    new_deterministic = [
        f for f in patched_report.deterministic_findings
        if _det_key(f) not in base_keys
    ]

    base_had_collapse = any(f.defect_class == "economy_collapse" for f in base_sim_findings)
    new_collapse_findings = [
        f for f in patched_sim_findings
        if f.defect_class == "economy_collapse" and not base_had_collapse
    ]

    blocking = new_deterministic + new_collapse_findings
    passed = not blocking
    return passed, blocking
