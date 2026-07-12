"""M2a-part2 §16 acceptance (Task 8): the 6 anchors, ALL zero-live-network.

Tests 1/2/5 depend on the repair cassettes that a human records with

    GAMEFORGE_LLM_LIVE=1 GAMEFORGE_LLM_KEY=<key> uv run python -m gameforge.agents.harness --record

Before those cassettes exist, running the corpus under REPLAY raises
`CassetteReplayMiss` (the CORRECT pre-record RED state — it proves the harness is
wired to real cassettes, not a stub). They are `skipif`-guarded on the presence
of a non-empty `cassettes/` dir so the suite stays GREEN before recording and the
three activate automatically the moment cassettes land.

Tests 3/4/6 need NO repair cassettes — they exercise the deterministic
partition, the generation gate, and the extraction/triage agents with a
deterministic fixed transport (PASSTHROUGH, no network), so they pass now.
"""

import json
import os

import pytest

from gameforge.agents.consistency.assistant import ConsistencyAssistant
from gameforge.agents.consistency.checker import ConsistencyChecker
from gameforge.agents.extraction.proposer import ExtractionProposer
from gameforge.agents.generation.generator import ContentGenerator
from gameforge.agents.harness import (
    default_scenario_dirs,
    replay_router,
    run_repair_corpus,
)
from gameforge.agents.triage.triager import DefectTriager
from gameforge.contracts.agent_io import (
    DesignDocInput,
    DesignGoalInput,
    DialogueNarrativeInput,
    FindingsInput,
    NarrativeConstraintInput,
)
from gameforge.contracts.dsl import Constraint
from gameforge.contracts.findings import Finding
from gameforge.contracts.ir import Entity, NodeType
from gameforge.contracts.model_router import ModelResponse
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.spine.checkers.report import build_review_report
from gameforge.spine.dsl.compile import compile_all
from gameforge.spine.ir.snapshot import Snapshot

_CONSTRAINTS = "scenarios/constraints"
_CASSETTES = "cassettes"
_AGENTS = "scenarios/agents"

# Repair cassettes are recorded by the human via `harness --record`; until then
# the corpus can't run under REPLAY. Skip (not fail) the three cassette-dependent
# anchors so the suite is GREEN pre-record and they auto-activate once recorded.
needs_cassettes = pytest.mark.skipif(
    not os.path.isdir(_CASSETTES) or not os.listdir(_CASSETTES),
    reason="repair cassettes not recorded yet — run "
    "`GAMEFORGE_LLM_LIVE=1 GAMEFORGE_LLM_KEY=<key> "
    "uv run python -m gameforge.agents.harness --record`",
)


# --------------------------------------------------------------------------
# Deterministic fixed transports (agent-logic doubles, no network)
# --------------------------------------------------------------------------
class _FixedTransport:
    """Returns one canned response for any request."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list = []

    def complete(self, req):
        self.calls.append(req)
        return ModelResponse(response_normalized=self.text)


class _PerVariantTransport:
    """Returns a canned response per `prompt_version` — lets each of the 3
    perspective-diverse consistency samples get their own answer."""

    def __init__(self, by_prompt_version: dict[str, str]) -> None:
        self._by = by_prompt_version
        self.calls: list = []

    def complete(self, req):
        self.calls.append(req)
        return ModelResponse(response_normalized=self._by.get(req.prompt_version, "[]"))


def _passthrough(transport, tmp_path) -> ModelRouter:
    return ModelRouter(transport, CassetteStore(tmp_path), mode=RouterMode.PASSTHROUGH)


# --------------------------------------------------------------------------
# 1. Fix Pass Rate >= 70% (REPLAY; skipped until cassettes recorded)
# --------------------------------------------------------------------------
@needs_cassettes
def test_fix_pass_rate_ge_70pct():
    result = run_repair_corpus(default_scenario_dirs(), _CONSTRAINTS, replay_router())
    assert result.attempted == 10
    assert result.fix_pass_rate >= 0.70
    # The economy-sink-adapter increment lifted this to a genuine 10/10: the
    # plumbed SELLS sink made economy_collapse economically fixable, and the
    # delete-op old_value drop (drafter._build_ops) stopped a summarized
    # old_value from spuriously rejecting unsatisfiable_completion's patch.
    # Deterministic REPLAY, so pin the win as a regression lock.
    by_class = {r["defect_class"]: r["passed"] for r in result.per_scenario}
    assert by_class["economy_collapse"] is True
    assert by_class["unsatisfiable_completion"] is True
    assert result.fix_pass_rate == 1.0


# --------------------------------------------------------------------------
# 2. Repair search is reproducible under REPLAY (field-equal across two runs)
# --------------------------------------------------------------------------
@needs_cassettes
def test_repair_search_reproducible():
    r1 = run_repair_corpus(default_scenario_dirs(), _CONSTRAINTS, replay_router())
    r2 = run_repair_corpus(default_scenario_dirs(), _CONSTRAINTS, replay_router())
    assert r1 == r2


# --------------------------------------------------------------------------
# 3. Deterministic vs llm-assisted findings are STRICTLY partitioned
#    (no repair cassettes needed — fixed transport for the consistency samples)
# --------------------------------------------------------------------------
_MAJORITY_HINT = {
    "defect_class": "spoiler",
    "entity_ids": ["npc:guard", "secret:warden"],
    "constraint_ids": ["C-warden-reveal"],
    "span": "The guard names the Warden as Mara.",
    "rationale": "The identity is named before its reveal gate.",
}

_DET_CONSTRAINT_YAML = """
- id: C-test-reward-cap
  kind: numeric
  oracle: deterministic
  scope:
    var: q
    node_type: QUEST
  assert: reward_gold <= 80
  severity: major
"""


def test_deterministic_and_llm_strictly_partitioned(tmp_path):
    # A deterministic checker that WILL fire (reward_gold=120 busts the <=80 cap)
    # alongside an llm-assisted ConsistencyChecker — both buckets populated, and
    # the partition must keep them strictly separate.
    quest = Entity(id="quest:q1", type=NodeType.QUEST, attrs={"reward_gold": 120})
    snap = Snapshot.from_entities_relations([quest], [])
    det_checkers = compile_all(Constraint.from_yaml(_DET_CONSTRAINT_YAML))

    by_variant = {
        "consistency@2#p_constraint_matching": json.dumps([_MAJORITY_HINT]),
        "consistency@2#p_causal_world_state": json.dumps([_MAJORITY_HINT]),
        "consistency@2#p_adversarial_falsification": "[]",
    }
    router = _passthrough(_PerVariantTransport(by_variant), tmp_path)
    consistency_checker = ConsistencyChecker(
        ConsistencyAssistant(),
        router,
        DialogueNarrativeInput(
            dialogue="The archive is sealed. The guard names the Warden as Mara.",
            narrative_constraints=[
                NarrativeConstraintInput(
                    constraint_id="C-warden-reveal",
                    entity_ids=["npc:guard", "secret:warden"],
                    statement="The Warden's identity may be named only after the archive opens.",
                )
            ],
        ),
    )

    report = build_review_report(snap, [*det_checkers, consistency_checker])

    # The core anchor: llm-assisted findings exist, and NOTHING llm-assisted
    # leaked into the deterministic bucket.
    assert report.llm_assisted_findings != []
    assert all(f.oracle_type != "llm-assisted" for f in report.deterministic_findings)
    # Both buckets are genuinely populated (a real partition, not a vacuous one).
    assert report.deterministic_findings != []
    assert all(f.oracle_type == "llm-assisted" for f in report.llm_assisted_findings)
    assert all(f.status == "unproven" for f in report.llm_assisted_findings)


# --------------------------------------------------------------------------
# 4. Generation gate BLOCKS a defective proposal (fixed transport, no network)
# --------------------------------------------------------------------------
def test_generation_gate_blocks_defective_proposal(tmp_path):
    quest = Entity(id="quest:q1", type=NodeType.QUEST, attrs={"reward_gold": 50})
    base = Snapshot.from_entities_relations([quest], [])
    checkers = compile_all(Constraint.from_yaml(_DET_CONSTRAINT_YAML))

    # Model proposes an op that busts the <=80 cap → the deterministic gate,
    # not the model's own claim, must refuse it.
    payload = json.dumps(
        [{"op": "set_entity_attr", "target": "quest:q1.reward_gold",
          "old_value": 50, "new_value": 999}]
    )
    res = ContentGenerator(base, checkers).run(
        DesignGoalInput(goal="crank the reward", grounding_snapshot_id=base.snapshot_id),
        _passthrough(_FixedTransport(payload), tmp_path),
    )

    assert res.produced["proposal"]["passed_gate"] is False
    assert "reward_out_of_range" in res.produced["blocking"]


# --------------------------------------------------------------------------
# 5. Search-efficiency report present + in sane ranges (REPLAY; skipped
#    until cassettes recorded)
# --------------------------------------------------------------------------
@needs_cassettes
def test_search_efficiency_report_present():
    result = run_repair_corpus(default_scenario_dirs(), _CONSTRAINTS, replay_router())

    assert 0.0 <= result.first_pass_rate <= 1.0
    # >=70% pass → at least one passing scenario → avg over passed is in [1, max_steps].
    assert 1.0 <= result.avg_steps <= 4.0
    # Runtime coverage is honest: never more than the number of passes.
    assert 0 <= result.runtime_vetted <= result.passed
    assert result.per_scenario  # per-scenario breakdown is present


# --------------------------------------------------------------------------
# 6. Extraction + triage produce structured output; fallback path never crashes
#    (fixed transport, no network)
# --------------------------------------------------------------------------
def _finding(fid: str, defect_class: str) -> Finding:
    return Finding(
        id=fid, source="checker", producer_id="p", producer_run_id="r1",
        oracle_type="deterministic", defect_class=defect_class, severity="major",
        snapshot_id="snap1", status="confirmed", message=f"finding {fid}",
    )


def test_extraction_and_triage_smoke(tmp_path):
    with open(os.path.join(_AGENTS, "extraction_doc.md"), encoding="utf-8") as fh:
        doc_text = fh.read()

    # Extraction: one compilable + one uncompilable proposal → the oracle keeps
    # only the compilable one; structured output is well-formed.
    extraction_payload = json.dumps([
        {"proposed_id": "C_side_reward_cap", "kind": "numeric",
         "assert_expr": "reward_gold <= 100", "rationale": "side-quest cap from doc"},
        {"proposed_id": "C_bad", "kind": "numeric",
         "assert_expr": "__import__('os').system('x')", "rationale": "not compilable"},
    ])
    ex = ExtractionProposer().run(
        DesignDocInput(doc_text=doc_text, doc_version="v3"),
        _passthrough(_FixedTransport(extraction_payload), tmp_path),
    )
    assert ex.role == "extraction"
    assert ex.fallback_taken is False
    assert len(ex.produced["proposals"]) == 1
    assert ex.produced["dropped"] == 1

    # Extraction fallback path (unparseable output) must not crash.
    ex_fb = ExtractionProposer().run(
        DesignDocInput(doc_text=doc_text, doc_version="v3"),
        _passthrough(_FixedTransport("sorry, no json"), tmp_path),
    )
    assert ex_fb.fallback_taken is True
    assert ex_fb.produced["proposals"] == []

    # Triage: cluster two real findings; invented ids dropped; verdicts never restated.
    f1, f2 = _finding("f1", "dangling_reference"), _finding("f2", "reward_out_of_range")
    triage_payload = json.dumps([
        {"cluster_id": "C1", "finding_ids": ["f1", "f2", "f9"], "priority": "p1",
         "suspected_root_cause": "shared outpost config edit"},
    ])
    tr = DefectTriager().run(
        FindingsInput(findings=[f1, f2]),
        _passthrough(_FixedTransport(triage_payload), tmp_path),
    )
    assert tr.role == "triage"
    assert tr.fallback_taken is False
    clusters = tr.produced["triaged"]["clusters"]
    assert len(clusters) == 1
    assert set(clusters[0]["finding_ids"]) == {"f1", "f2"}
    assert tr.produced["dropped_ids"] >= 1

    # Triage fallback path (unparseable output) must not crash.
    tr_fb = DefectTriager().run(
        FindingsInput(findings=[f1]),
        _passthrough(_FixedTransport("not json"), tmp_path),
    )
    assert tr_fb.fallback_taken is True
    assert tr_fb.produced["triaged"]["clusters"] == []
