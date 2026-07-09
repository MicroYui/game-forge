"""M2b-2 Part C (Task 10): perspective-diverse quorum + rebuttal acceptance.

Focused, StubTransport-driven (no live call, no cassette record) — consistency
is stub-tested per the M2a part2 precedent, not cassette-tested. Drives the
FULL perspective-diverse flow (3 perspectives -> tally -> one rebuttal round
for disputed hints -> re-tally) over a single scripted dialogue with four
candidate hints exercising every path:

  - A: unanimous (3/3)              -> passes directly
  - B: 2/3                          -> passes directly
  - C: 1/3, rebuttal confirms it    -> disputed, then SURVIVES via rebuttal
  - D: 1/3, rebuttal fails to lift  -> disputed, then DROPPED

and asserts the surviving hints are EXACTLY {A, B, C} — reproducible across
two independent runs — with every survivor routed `oracle_type="llm-assisted"`
/ `status="unproven"` via `ConsistencyChecker`.
"""
from __future__ import annotations

import json

from gameforge.agents.consistency.assistant import ConsistencyAssistant
from gameforge.agents.consistency.checker import ConsistencyChecker
from gameforge.contracts.agent_io import DialogueNarrativeInput
from gameforge.contracts.ir import Entity, NodeType
from gameforge.contracts.model_router import ModelResponse
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode
from gameforge.spine.checkers.report import build_review_report
from gameforge.spine.ir.snapshot import Snapshot

_HINT_A = {"span": "A", "issue": "unanimous timeline break"}
_HINT_B = {"span": "B", "issue": "2-of-3 identity clash"}
_HINT_C = {"span": "C", "issue": "disputed spoiler, rebuttal confirms"}
_HINT_D = {"span": "D", "issue": "disputed spoiler, rebuttal fails to confirm"}

# Round 1 (per-perspective) samples:
#   temporal -> A, B, C   (A:1 B:1 C:1)
#   identity -> A, B, D   (A:2 B:2 D:1)
#   spoiler  -> A         (A:3)
# Tally: A=3, B=2, C=1, D=1 -> threshold=2 default: A,B pass directly;
# C,D are disputed (1 <= count < 2).
_ROUND1 = {
    "consistency@1#p_temporal": json.dumps([_HINT_A, _HINT_B, _HINT_C]),
    "consistency@1#p_identity": json.dumps([_HINT_A, _HINT_B, _HINT_D]),
    "consistency@1#p_spoiler": json.dumps([_HINT_A]),
}

# Rebuttal round, shown only the disputed [C, D]:
#   temporal -> confirms C only
#   identity -> confirms C and D
#   spoiler  -> confirms C only
# Confirmation tally: C=3 (>=2 -> survives), D=1 (<2 -> stays dropped).
_ROUND2 = {
    "consistency@1#r_temporal": json.dumps([_HINT_C]),
    "consistency@1#r_identity": json.dumps([_HINT_C, _HINT_D]),
    "consistency@1#r_spoiler": json.dumps([_HINT_C]),
}

_ALL_VARIANTS = {**_ROUND1, **_ROUND2}


class _StubTransport:
    """Scripted per-`prompt_version` transport (agent-logic test double, no
    network, no cassette record needed)."""

    def __init__(self, by_prompt_version: dict[str, str]) -> None:
        self._by = by_prompt_version
        self.calls: list = []

    def complete(self, req):
        self.calls.append(req)
        return ModelResponse(response_normalized=self._by.get(req.prompt_version, "[]"))


def _router(tmp_path) -> ModelRouter:
    return ModelRouter(
        _StubTransport(_ALL_VARIANTS), CassetteStore(tmp_path), mode=RouterMode.PASSTHROUGH
    )


def _dialogue_input() -> DialogueNarrativeInput:
    return DialogueNarrativeInput(dialogue="A. B. C. D. — four candidate narrative issues.")


def test_perspective_diverse_quorum_plus_rebuttal_keeps_exactly_the_survivors(tmp_path):
    res = ConsistencyAssistant().run(_dialogue_input(), _router(tmp_path))

    spans = [h["span"] for h in res.produced["hints"]]
    assert spans == ["A", "B", "C"]  # exactly the quorum-passing hints, in order
    assert all(h["is_suggestion"] is True for h in res.produced["hints"])
    assert res.fallback_taken is False

    # 3 first-round + 3 rebuttal-round calls = 6 distinct request_hashes.
    assert len(res.request_hashes) == 6
    assert len(set(res.request_hashes)) == 6


def test_perspective_diverse_quorum_is_reproducible_across_two_runs(tmp_path):
    r1 = ConsistencyAssistant().run(_dialogue_input(), _router(tmp_path / "run1"))
    r2 = ConsistencyAssistant().run(_dialogue_input(), _router(tmp_path / "run2"))

    assert r1.produced == r2.produced
    assert r1.request_hashes == r2.request_hashes
    assert r1.fallback_taken == r2.fallback_taken


def test_every_survivor_is_llm_assisted_and_unproven_via_checker(tmp_path):
    checker = ConsistencyChecker(ConsistencyAssistant(), _router(tmp_path), _dialogue_input())
    snap = Snapshot.from_entities_relations([Entity(id="q", type=NodeType.QUEST)], [])

    findings = checker.check(snap)
    assert {f.evidence["span"] for f in findings} == {"A", "B", "C"}
    for f in findings:
        assert f.oracle_type == "llm-assisted"
        assert f.status == "unproven"
        assert f.source == "llm"
        assert f.defect_class == "narrative_inconsistency"

    report = build_review_report(snap, [checker])
    assert report.llm_assisted_findings != []
    assert report.deterministic_findings == []
    assert {f.evidence["span"] for f in report.llm_assisted_findings} == {"A", "B", "C"}
