"""Content Generator + generation gate tests (M2a-part2 Task 7): a generated
proposal is ALWAYS just a proposal — `passed_gate` is decided entirely by the
deterministic checker+economy-sim gate (`agents.generation.gate.gate_proposal`),
never by the model's own claim. Mirrors the `agents.repair.verify` new-finding
diff pattern (same `(defect_class, sorted(entities))` key), applied to a
generated `Patch` instead of a repair `Patch`.
"""
import json

from gameforge.agents.generation.gate import gate_proposal
from gameforge.agents.generation.generator import ContentGenerator
from gameforge.contracts.agent_io import DesignGoalInput
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.ir import Entity, NodeType
from gameforge.contracts.model_router import ModelResponse
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.spine.dsl.compile import compile_all
from gameforge.spine.ir.snapshot import Snapshot


class _FixedTransport:
    """Returns a canned response for any request (agent-logic test double, no network)."""

    def __init__(self, text):
        self.text = text
        self.calls = []

    def complete(self, req):
        self.calls.append(req)
        return ModelResponse(response_normalized=self.text)


def _router(text, tmp_path):
    return ModelRouter(_FixedTransport(text), CassetteStore(tmp_path), mode=RouterMode.PASSTHROUGH)


_CONSTRAINTS_YAML = """
- id: C-test-reward-cap
  kind: numeric
  oracle: deterministic
  scope:
    var: q
    node_type: QUEST
  assert: reward_gold <= 80
  severity: major
"""


def _base_snapshot() -> Snapshot:
    quest = Entity(id="quest:q1", type=NodeType.QUEST, attrs={"reward_gold": 50})
    return Snapshot.from_entities_relations([quest], [])


def _checkers():
    return compile_all(Constraint.from_yaml(_CONSTRAINTS_YAML))


def _goal_input(snapshot: Snapshot) -> DesignGoalInput:
    return DesignGoalInput(goal="tweak a quest reward", grounding_snapshot_id=snapshot.snapshot_id)


# --------------------------------------------------------------------------
# gate.py direct unit tests
# --------------------------------------------------------------------------
def test_gate_rejects_proposal_introducing_new_deterministic_defect():
    base = _base_snapshot()
    checkers = _checkers()
    ops = [{"op": "set_entity_attr", "target": "quest:q1.reward_gold",
            "old_value": 50, "new_value": 999}]  # busts the <=80 cap

    passed, blocking = gate_proposal(base, ops, checkers)

    assert passed is False
    assert any(f.defect_class == "reward_out_of_range" for f in blocking)


def test_gate_passes_benign_in_range_proposal():
    base = _base_snapshot()
    checkers = _checkers()
    ops = [{"op": "set_entity_attr", "target": "quest:q1.reward_gold",
            "old_value": 50, "new_value": 70}]  # stays within the <=80 cap

    passed, blocking = gate_proposal(base, ops, checkers)

    assert passed is True
    assert blocking == []


def test_gate_rejects_stale_patch_as_not_passed():
    base = _base_snapshot()
    checkers = _checkers()
    # old_value no longer matches -> apply_patch raises PatchRejected
    ops = [{"op": "set_entity_attr", "target": "quest:q1.reward_gold",
            "old_value": 999, "new_value": 70}]

    passed, blocking = gate_proposal(base, ops, checkers)

    assert passed is False
    assert blocking == []


def test_gate_rejects_malformed_ops_fail_closed():
    base = _base_snapshot()
    checkers = _checkers()
    ops = [{"op": "not_a_real_op", "target": "quest:q1.reward_gold", "new_value": 1}]

    passed, blocking = gate_proposal(base, ops, checkers)

    assert passed is False
    assert blocking == []


# --------------------------------------------------------------------------
# ContentGenerator.run — Tests A/B/C from the task brief
# --------------------------------------------------------------------------
def test_generator_rejects_out_of_range_proposal(tmp_path):
    base = _base_snapshot()
    checkers = _checkers()
    payload = json.dumps([
        {"op": "set_entity_attr", "target": "quest:q1.reward_gold",
         "old_value": 50, "new_value": 999},
    ])

    res = ContentGenerator(base, checkers).run(_goal_input(base), _router(payload, tmp_path))

    assert res.role == "generation"
    assert res.fallback_taken is False
    assert res.produced["proposal"]["passed_gate"] is False
    assert "reward_out_of_range" in res.produced["blocking"]


def test_generator_accepts_benign_proposal(tmp_path):
    base = _base_snapshot()
    checkers = _checkers()
    payload = json.dumps([
        {"op": "set_entity_attr", "target": "quest:q1.reward_gold",
         "old_value": 50, "new_value": 70},
    ])

    res = ContentGenerator(base, checkers).run(_goal_input(base), _router(payload, tmp_path))

    assert res.produced["proposal"]["passed_gate"] is True
    assert res.produced["blocking"] == []
    assert res.produced["proposal"]["proposed_ops"] == json.loads(payload)


def test_generator_fallback_on_unparseable_output(tmp_path):
    base = _base_snapshot()
    checkers = _checkers()

    res = ContentGenerator(base, checkers).run(
        _goal_input(base), _router("sorry, no json here", tmp_path)
    )

    assert res.fallback_taken is True
    assert res.produced["proposal"]["passed_gate"] is False
    assert res.produced["proposal"]["proposed_ops"] == []
