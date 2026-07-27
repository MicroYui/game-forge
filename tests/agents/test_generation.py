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
from gameforge.contracts.ir import EdgeType, Entity, NodeType, Relation
from gameforge.contracts.model_router import ModelResponse
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.spine.dsl.compile import compile_all
from gameforge.spine.ir.snapshot import Snapshot


class _FixedTransport:
    """Returns a canned response for any request (agent-logic test double, no network)."""

    def __init__(self, text, *, finish_reason="stop"):
        self.text = text
        self.finish_reason = finish_reason
        self.calls = []

    def complete(self, req):
        self.calls.append(req)
        return ModelResponse(response_normalized=self.text, finish_reason=self.finish_reason)


class _SequencedTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, req):
        self.calls.append(req)
        return ModelResponse(
            response_normalized=self.responses[len(self.calls) - 1],
            finish_reason="stop",
        )


def _router(text, tmp_path, *, finish_reason="stop"):
    return ModelRouter(
        _FixedTransport(text, finish_reason=finish_reason),
        CassetteStore(tmp_path),
        mode=RouterMode.PASSTHROUGH,
    )


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
    ops = [
        {
            "op": "set_entity_attr",
            "target": "quest:q1.reward_gold",
            "old_value": 50,
            "new_value": 999,
        }
    ]  # busts the <=80 cap

    passed, blocking = gate_proposal(base, ops, checkers)

    assert passed is False
    assert any(f.defect_class == "reward_out_of_range" for f in blocking)


def test_gate_rejects_nested_reward_path_used_by_the_aureus_quest_ir():
    base = Snapshot.from_entities_relations(
        [Entity(id="quest:missing_caravan", type=NodeType.QUEST, attrs={"reward": {"gold": 60}})],
        [],
    )
    constraints = Constraint.from_yaml(
        """
- id: C-side-quest-reward-cap
  kind: numeric
  oracle: deterministic
  scope:
    var: q
    node_type: QUEST
  assert: reward.gold <= 80
  severity: major
"""
    )
    ops = [
        {
            "op": "set_entity_attr",
            "target": "quest:missing_caravan.reward.gold",
            "old_value": 60,
            "new_value": 150,
        }
    ]

    passed, blocking = gate_proposal(base, ops, compile_all(constraints))

    assert passed is False
    assert [(finding.defect_class, finding.status) for finding in blocking] == [
        ("reward_out_of_range", "confirmed")
    ]


def test_gate_passes_benign_in_range_proposal():
    base = _base_snapshot()
    checkers = _checkers()
    ops = [
        {
            "op": "set_entity_attr",
            "target": "quest:q1.reward_gold",
            "old_value": 50,
            "new_value": 70,
        }
    ]  # stays within the <=80 cap

    passed, blocking = gate_proposal(base, ops, checkers)

    assert passed is True
    assert blocking == []


def test_gate_rejects_stale_patch_as_not_passed():
    base = _base_snapshot()
    checkers = _checkers()
    # old_value no longer matches -> apply_patch raises PatchRejected
    ops = [
        {
            "op": "set_entity_attr",
            "target": "quest:q1.reward_gold",
            "old_value": 999,
            "new_value": 70,
        }
    ]

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
    payload = json.dumps(
        [
            {
                "op": "set_entity_attr",
                "target": "quest:q1.reward_gold",
                "old_value": 50,
                "new_value": 999,
            },
        ]
    )

    res = ContentGenerator(base, checkers).run(_goal_input(base), _router(payload, tmp_path))

    assert res.role == "generation"
    assert res.fallback_taken is False
    assert res.produced["proposal"]["passed_gate"] is False
    assert "reward_out_of_range" in res.produced["blocking"]


def test_generator_accepts_benign_proposal(tmp_path):
    base = _base_snapshot()
    checkers = _checkers()
    payload = json.dumps(
        [
            {
                "op": "set_entity_attr",
                "target": "quest:q1.reward_gold",
                "old_value": 50,
                "new_value": 70,
            },
        ]
    )

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


def test_generation_v4_delegates_output_budget_to_the_frozen_model_route(tmp_path):
    base = _base_snapshot()
    transport = _FixedTransport("[]")
    router = ModelRouter(transport, CassetteStore(tmp_path), mode=RouterMode.PASSTHROUGH)

    ContentGenerator(base, _checkers()).run(
        _goal_input(base),
        router,
    )

    assert transport.calls[0].params == {}


def test_generator_fails_closed_when_provider_reports_output_truncation(tmp_path):
    base = _base_snapshot()
    payload = json.dumps(
        [{"op": "add_entity", "target": "event:letters", "new_value": {"type": "EVENT"}}]
    )

    result = ContentGenerator(base, _checkers()).run(
        _goal_input(base),
        _router(payload, tmp_path, finish_reason="max_output_tokens"),
    )

    assert result.fallback_taken is True
    assert result.produced["proposal"]["proposed_ops"] == []
    assert result.produced["error"] == "model_output_truncated"


def test_material_extraction_v4_splits_large_sources_and_combines_all_ops(tmp_path):
    base = Snapshot.from_entities_relations([], [])
    first = json.dumps(
        [{"op": "add_entity", "target": "event:first", "new_value": {"type": "EVENT"}}]
    )
    second = json.dumps(
        [{"op": "add_entity", "target": "event:second", "new_value": {"type": "EVENT"}}]
    )
    transport = _SequencedTransport((first, second))
    router = ModelRouter(transport, CassetteStore(tmp_path), mode=RouterMode.PASSTHROUGH)

    result = ContentGenerator(base, []).run_from_materials(
        DesignGoalInput(goal="提取活动实体", grounding_snapshot_id=base.snapshot_id),
        router,
        materials=(("artifact:material", "第一段活动。\n\n第二段活动。"),),
        max_chunk_bytes=len("第一段活动。\n\n".encode("utf-8")),
        execute_local_gate=False,
    )

    assert result.fallback_taken is False
    assert [item["target"] for item in result.produced["proposal"]["proposed_ops"]] == [
        "event:first",
        "event:second",
    ]
    assert len(transport.calls) == 2
    assert all(call.params == {} for call in transport.calls)
    assert all("Material extraction mode" in call.messages[-1].content for call in transport.calls)
    assert "chunk 1/2" in transport.calls[0].messages[-1].content
    assert "chunk 2/2" in transport.calls[1].messages[-1].content


def _balanced_economy_snapshot() -> Snapshot:
    # A faucet (~5 gold/tick) matched by a sink (price 5 @ buy_prob 1.0) → the
    # simulated economy stays flat, no collapse over the horizon.
    ents = [
        Entity(id="gold", type=NodeType.CURRENCY, attrs={}),
        Entity(
            id="m:test",
            type=NodeType.MONSTER,
            attrs={"gold_min": 5, "gold_max": 5, "kills_per_tick": 1},
        ),
        Entity(id="shop:s", type=NodeType.SHOP, attrs={}),
        Entity(id="item:i", type=NodeType.ITEM, attrs={}),
    ]
    rels = [
        Relation(id="r_drop", type=EdgeType.DROPS_FROM, src_id="m:test", dst_id="gold"),
        Relation(
            id="r_sells",
            type=EdgeType.SELLS,
            src_id="shop:s",
            dst_id="item:i",
            attrs={"price": 5, "buy_prob": 1.0, "currency": "gold"},
        ),
    ]
    return Snapshot.from_entities_relations(ents, rels)


def test_gate_rejects_proposal_that_causes_economy_collapse():
    # Spiking the faucet's gold_max turns a balanced economy into a runaway
    # collapse; the sim gate must block it (the economy_collapse branch, distinct
    # from the deterministic-checker branch).
    base = _balanced_economy_snapshot()
    spike = [
        {"op": "set_entity_attr", "target": "m:test.gold_max", "old_value": 5, "new_value": 5000}
    ]
    passed, blocking = gate_proposal(base, spike, [])
    assert passed is False
    assert any(f.defect_class == "economy_collapse" for f in blocking)


def test_gate_passes_economy_neutral_proposal():
    base = _balanced_economy_snapshot()
    benign = [{"op": "set_entity_attr", "target": "item:i.tier", "old_value": None, "new_value": 1}]
    passed, blocking = gate_proposal(base, benign, [])
    assert passed is True
    assert blocking == []


def test_grounding_shows_existing_relations_not_only_entities() -> None:
    """Without them the model re-proposes a relation the graph already has.

    It cannot avoid duplicating "老陶 LOCATED_IN 锻造区" if it never sees that the
    edge is already there — and no lexical rule can merge two spellings of the
    same edge id afterwards.
    """

    from gameforge.contracts.ir import Entity, NodeType, Relation, EdgeType
    from gameforge.spine.ir.snapshot import Snapshot

    snapshot = Snapshot(
        entities={
            "npc:tao": Entity(id="npc:tao", type=NodeType("NPC"), attrs={"name": "老陶"}),
            "region:forge": Entity(
                id="region:forge", type=NodeType("REGION"), attrs={"name": "锻造区"}
            ),
        },
        relations={
            "rel:tao_in_forge": Relation(
                id="rel:tao_in_forge",
                type=EdgeType("LOCATED_IN"),
                src_id="npc:tao",
                dst_id="region:forge",
            )
        },
    )

    grounding = ContentGenerator(snapshot, ())._grounding("老陶还在锻造区吗？")

    assert "npc:tao" in grounding
    assert "rel:tao_in_forge" in grounding
    assert "LOCATED_IN" in grounding


def test_grounding_carries_the_named_entity_and_leaves_the_rest_out() -> None:
    """The prompt used to carry the entire graph, re-sent once per material chunk.

    Both halves matter and only one of them is loud: too little grounding makes
    the model duplicate what already exists, and too much makes the request
    unsendable. So this asserts a size AND an absence, not merely that nothing
    raised — a regression that quietly went back to dumping everything would
    otherwise pass.
    """

    from gameforge.contracts.ir import Entity, NodeType
    from gameforge.spine.ir.snapshot import Snapshot

    snapshot = Snapshot.from_entities_relations(
        [Entity(id="npc:tao", type=NodeType("NPC"), attrs={"name": "老陶"})]
        + [
            Entity(
                id=f"quest:q{index:03d}",
                type=NodeType("QUEST"),
                attrs={"name": f"任务{index}", "lore": "背景" * 200},
            )
            for index in range(500)
        ],
        [],
    )

    grounding = ContentGenerator(snapshot, ())._grounding("老陶接手了新的订单。")

    assert "npc:tao" in grounding
    assert len(grounding.encode("utf-8")) < 8 * 1024
    # The catalog still names the other quests, but none of their content is here.
    assert "背景背景" not in grounding
