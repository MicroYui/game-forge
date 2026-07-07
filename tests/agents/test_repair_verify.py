"""Repair verifier tests (M2a-part2 Task 6): the deterministic verifier is the
oracle — spine checkers + economy sim + Aureus regression, NEVER the LLM.

A minimal in-memory `reward_out_of_range` scenario (a QUEST with a numeric gold
reward exceeding its cap) exercises every branch of `verify_patch`:
  (a) a correct patch (reward → in-range) verifies ok,
  (b) a patch that leaves the target defect present is NOT target_resolved,
  (c) a patch that ALSO introduces a dangling reference is rejected because it
      adds a NEW deterministic finding not present in the base.
"""
from gameforge.agents.repair.verify import verify_patch
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.findings import Patch, TypedOp
from gameforge.contracts.ir import Entity, NodeType
from gameforge.spine.dsl.compile import compile_all
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.patch import apply_patch

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
