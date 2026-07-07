"""Repair verifier tests (M2a-part2 Task 6): the deterministic verifier is the
oracle — spine checkers + economy sim + Aureus regression, NEVER the LLM.

A minimal in-memory `reward_out_of_range` scenario (a QUEST with a numeric gold
reward exceeding its cap) exercises every branch of `verify_patch`:
  (a) a correct patch (reward → in-range) verifies ok,
  (b) a patch that leaves the target defect present is NOT target_resolved,
  (c) a patch that ALSO introduces a dangling reference is rejected because it
      adds a NEW deterministic finding not present in the base.

Plus three soundness-hole regression guards (each would let the Fix-Pass-Rate
inflate with a non-fix):
  A. simulation-class targets (economy_collapse) must be checked against the
     patched *simulation* findings, not silently "resolved" because they never
     appear among deterministic findings.
  B. deleting the offending entity ("delete-to-silence") must NOT count as a
     fix — any base-present finding entity must survive into the patched snapshot.
  C. regression_ran / economy_ran expose whether the runtime gates actually ran
     vs. were skipped (so a skip can't masquerade as coverage).
"""
import glob
import json

from gameforge.agents.repair.verify import verify_patch
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.findings import Patch, TypedOp
from gameforge.contracts.ir import Entity, NodeType
from gameforge.spine.dsl.compile import compile_all
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter
from gameforge.spine.ingestion.csv_format import read_workbook
from gameforge.spine.ingestion.format_schema import FormatSchema
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import apply_patch

_SCENARIOS = "scenarios/defects"
_CONSTRAINTS = "scenarios/constraints"


def _load_scenario(name: str) -> Snapshot:
    """Real CSV workbook -> IR snapshot (same path as run_review), so the
    soundness tests bite on real content, not a hand-built toy."""
    d = f"{_SCENARIOS}/{name}"
    with open(f"{d}/format_schema.json", encoding="utf-8") as fh:
        schema = FormatSchema.model_validate(json.load(fh))
    return AureusCsvAdapter().to_ir(read_workbook(d, schema), file_ref=d)


def _real_checkers():
    constraints: list[Constraint] = []
    for path in sorted(glob.glob(f"{_CONSTRAINTS}/*.yaml")):
        with open(path, encoding="utf-8") as fh:
            constraints.extend(Constraint.from_yaml(fh.read()))
    return compile_all(constraints)

_CONSTRAINTS_YAML = """
- id: C-test-reward-cap
  kind: numeric
  oracle: deterministic
  scope:
    var: q
    node_type: QUEST
  assert: reward_gold <= 80
  severity: major
- id: C-test-graph-structural
  kind: structural
  oracle: deterministic
  assert: relation_endpoints_exist
  severity: critical
"""


def _base_snapshot() -> Snapshot:
    quest = Entity(id="quest:q1", type=NodeType.QUEST, attrs={"reward_gold": 120})
    return Snapshot.from_entities_relations([quest], [])


def _checkers():
    return compile_all(Constraint.from_yaml(_CONSTRAINTS_YAML))


def _patch(ops: list[TypedOp]) -> Patch:
    return Patch(
        id="test-patch",
        base_snapshot_id="base",
        target_snapshot_id="",
        side_effect_risk="low",
        ops=ops,
        produced_by="human",
        producer_run_id="test",
        rationale="hand-built test patch",
    )


def test_correct_patch_verifies_ok():
    base = _base_snapshot()
    checkers = _checkers()
    patch = _patch([
        TypedOp(op_id="o1", op="set_entity_attr", target="quest:q1.reward_gold",
                old_value=120, new_value=50),
    ])
    patched = apply_patch(base, patch)

    result = verify_patch(base, patched, checkers, "reward_out_of_range")

    assert result.target_resolved is True
    assert result.new_deterministic == []
    assert result.regression_ok is True
    assert result.ok is True


def test_out_of_range_new_value_not_resolved():
    base = _base_snapshot()
    checkers = _checkers()
    patch = _patch([
        TypedOp(op_id="o1", op="set_entity_attr", target="quest:q1.reward_gold",
                old_value=120, new_value=200),  # still > 80: defect persists
    ])
    patched = apply_patch(base, patch)

    result = verify_patch(base, patched, checkers, "reward_out_of_range")

    assert result.target_resolved is False
    assert result.ok is False


def test_patch_introducing_dangling_reference_is_rejected():
    base = _base_snapshot()
    checkers = _checkers()
    patch = _patch([
        TypedOp(op_id="o1", op="set_entity_attr", target="quest:q1.reward_gold",
                old_value=120, new_value=50),  # resolves the target defect...
        TypedOp(op_id="o2", op="add_relation", target="rel:dangle",
                new_value={"type": "REFERENCES", "src_id": "quest:q1",
                           "dst_id": "missing:ghost"}),  # ...but adds a dangling ref
    ])
    patched = apply_patch(base, patch)

    result = verify_patch(base, patched, checkers, "reward_out_of_range")

    assert result.target_resolved is True  # reward is now in range
    assert result.new_deterministic != []  # dangling_reference is NEW vs. base
    assert any(f.defect_class == "dangling_reference" for f in result.new_deterministic)
    assert result.ok is False  # a new deterministic defect blocks the patch


# --------------------------------------------------------------------------
# HOLE A — simulation-class targets must not "resolve" trivially. economy_collapse
# is routed to simulation_findings; the old verifier only scanned
# deterministic_findings, so a NO-OP patch falsely "resolved" a collapsing economy.
# --------------------------------------------------------------------------
def test_hole_a_simulation_target_noop_does_not_resolve():
    base = _load_scenario("economy_collapse")
    checkers = _real_checkers()
    noop = _patch([])  # empty ops -> content-identical snapshot (a genuine no-op)
    patched = apply_patch(base, noop)

    result = verify_patch(base, patched, checkers, "economy_collapse", run_regression=False)

    assert result.economy_ran is True         # the sim actually ran on the patched snapshot
    assert result.target_resolved is False    # the collapse is still reproduced -> NOT resolved
    assert result.ok is False                  # (was falsely True before the fix)


# --------------------------------------------------------------------------
# HOLE B — delete-to-silence. Deleting the offending quest cascade-drops the
# finding without fixing anything; the content-preservation guard must reject it,
# while the legitimate in-range reward fix must still pass.
# --------------------------------------------------------------------------
def test_hole_b_delete_entity_to_silence_is_rejected():
    base = _load_scenario("reward_out_of_range")
    checkers = _real_checkers()
    delete = _patch([
        TypedOp(op_id="d1", op="delete_entity", target="quest:outpost"),
    ])
    patched = apply_patch(base, delete)

    result = verify_patch(base, patched, checkers, "reward_out_of_range", run_regression=False)

    assert result.ok is False                       # content-preservation guard trips
    assert "quest:outpost" in result.detail          # detail names the destroyed subject


def test_hole_b_legit_reward_fix_still_passes():
    base = _load_scenario("reward_out_of_range")
    checkers = _real_checkers()
    fix = _patch([
        TypedOp(op_id="r1", op="set_entity_attr", target="quest:outpost.reward.gold",
                old_value=500, new_value=100),  # in range (<=150); quest preserved
    ])
    patched = apply_patch(base, fix)

    result = verify_patch(base, patched, checkers, "reward_out_of_range", run_regression=False)

    assert result.target_resolved is True   # genuinely fixed, subject preserved
    assert result.ok is True                 # guard does NOT over-block the legit fix


# --------------------------------------------------------------------------
# HOLE C — regression-skip observability. regression_ran / economy_ran must
# distinguish "gate ran & passed" from "gate skipped".
# --------------------------------------------------------------------------
def test_hole_c_gates_report_false_when_not_applicable():
    base = _base_snapshot()  # minimal in-memory: no region/grid, no economy entities
    checkers = _checkers()
    patch = _patch([
        TypedOp(op_id="o1", op="set_entity_attr", target="quest:q1.reward_gold",
                old_value=120, new_value=50),
    ])
    patched = apply_patch(base, patch)

    result = verify_patch(base, patched, checkers, "reward_out_of_range")  # run_regression True

    assert result.regression_ran is False   # snapshot_to_world can't build -> Aureus skipped
    assert result.economy_ran is False       # no economy entities -> sim skipped
    assert result.regression_ok is True      # a skip is not a failure
    assert result.ok is True


def test_hole_c_regression_ran_true_on_buildable_scenario():
    base = _load_scenario("clean")
    checkers = _real_checkers()
    patched = apply_patch(base, _patch([]))  # no-op

    result = verify_patch(base, patched, checkers, "reward_out_of_range")  # run_regression True

    assert result.regression_ran is True    # Aureus world built + reset/stepped clean
    assert result.regression_ok is True
    assert result.ok is True
