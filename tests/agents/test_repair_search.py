"""Repair search tests (M2a-part2 Task 6): propose → verify → refine converges,
driven by the DETERMINISTIC verifier (spine checkers), never by the LLM.

`_SequenceTransport` returns a BAD ops array first (leaves the defect present)
and a GOOD ops array second (resolves it). The refine round's prompt carries the
counterexample, so its request_hash differs and the transport advances — proving
the loop actually re-drafts against verifier feedback. Aureus regression is off
here to keep the unit hermetic (no world build); the checker oracle still fully
decides pass/fail.
"""
import json

from gameforge.agents.repair.search import repair_search
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.findings import Finding
from gameforge.contracts.model_router import ModelResponse
from gameforge.contracts.ir import Entity, NodeType
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.spine.dsl.compile import compile_all
from gameforge.spine.ir.snapshot import Snapshot

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


class _SequenceTransport:
    """Returns canned responses in order; clamps at the last one (no network)."""

    def __init__(self, responses):
        self._seq = responses
        self._i = 0
        self.calls = []

    def complete(self, req):
        self.calls.append(req)
        resp = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return resp


def _ops(new_value: int) -> str:
    return json.dumps([
        {"op": "set_entity_attr", "op_id": "o1", "target": "quest:q1.reward_gold",
         "old_value": 120, "new_value": new_value},
    ])


def _finding() -> Finding:
    return Finding(
        id="F-reward", source="checker", producer_id="smt", producer_run_id="r1",
        oracle_type="deterministic", defect_class="reward_out_of_range",
        severity="major", snapshot_id="snap1", entities=["quest:q1"],
        status="confirmed", message="quest:q1 reward_gold exceeds cap",
    )


def test_repair_search_converges_after_one_refine(tmp_path):
    quest = Entity(id="quest:q1", type=NodeType.QUEST, attrs={"reward_gold": 120})
    snapshot = Snapshot.from_entities_relations([quest], [])
    checkers = compile_all(Constraint.from_yaml(_CONSTRAINTS_YAML))

    transport = _SequenceTransport([
        ModelResponse(response_normalized=_ops(200)),  # BAD: still > 80
        ModelResponse(response_normalized=_ops(50)),   # GOOD: within the cap
    ])
    router = ModelRouter(transport, CassetteStore(tmp_path), mode=RouterMode.PASSTHROUGH)

    draft = repair_search(
        _finding(), snapshot, checkers, router, max_steps=4, run_regression=False
    )

    assert draft.passed_verification is True
    assert draft.search_steps == 2  # bad round + refined good round
    assert len(transport.calls) == 2  # the refine round really re-called the model
    assert draft.patch.produced_by == "agent"
    assert draft.patch.expected_to_fix == ["F-reward"]


def test_repair_search_exhausts_when_never_resolved(tmp_path):
    quest = Entity(id="quest:q1", type=NodeType.QUEST, attrs={"reward_gold": 120})
    snapshot = Snapshot.from_entities_relations([quest], [])
    checkers = compile_all(Constraint.from_yaml(_CONSTRAINTS_YAML))

    # Every round proposes an out-of-range value: the verifier never passes.
    transport = _SequenceTransport([ModelResponse(response_normalized=_ops(300))])
    router = ModelRouter(transport, CassetteStore(tmp_path), mode=RouterMode.PASSTHROUGH)

    draft = repair_search(
        _finding(), snapshot, checkers, router, max_steps=3, run_regression=False
    )

    assert draft.passed_verification is False
    assert draft.search_steps == 3
