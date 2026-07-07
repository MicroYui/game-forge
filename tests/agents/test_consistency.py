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


class _PerVariantTransport:
    """Returns a different canned response per prompt_version (agent-logic test
    double, no network) — lets a test give each of the 3 quorum samples its
    own answer."""

    def __init__(self, by_prompt_version: dict[str, str]):
        self._by = by_prompt_version
        self.calls = []

    def complete(self, req):
        self.calls.append(req)
        return ModelResponse(response_normalized=self._by.get(req.prompt_version, "[]"))


def _router(by_prompt_version, tmp_path):
    return ModelRouter(
        _PerVariantTransport(by_prompt_version), CassetteStore(tmp_path), mode=RouterMode.PASSTHROUGH
    )


def _dialogue_input():
    return DialogueNarrativeInput(dialogue="The hero reveals the ending twist early.")


_MAJORITY_HINT = {"span": "reveals the ending twist", "issue": "spoiler"}
_MINORITY_HINT = {"span": "the hero", "issue": "off-topic"}


def test_quorum_keeps_majority_hint_drops_minority(tmp_path):
    by_variant = {
        "consistency@1#s0": json.dumps([_MAJORITY_HINT]),
        "consistency@1#s1": json.dumps([_MAJORITY_HINT]),
        "consistency@1#s2": json.dumps([_MINORITY_HINT]),
    }
    res = ConsistencyAssistant().run(_dialogue_input(), _router(by_variant, tmp_path))

    hints = res.produced["hints"]
    assert res.produced["samples"] == 3
    assert len(hints) == 1
    assert hints[0]["span"] == _MAJORITY_HINT["span"]
    assert hints[0]["issue"] == _MAJORITY_HINT["issue"]
    assert hints[0]["is_suggestion"] is True

    assert res.fallback_taken is False
    assert res.role == "consistency"
    assert len(res.request_hashes) == 3
    assert len(set(res.request_hashes)) == 3  # 3 distinct variants -> 3 distinct request_hashes


def test_quorum_drops_hint_reported_by_only_one_sample(tmp_path):
    by_variant = {
        "consistency@1#s0": json.dumps([_MAJORITY_HINT]),
        "consistency@1#s1": "[]",
        "consistency@1#s2": "[]",
    }
    res = ConsistencyAssistant().run(_dialogue_input(), _router(by_variant, tmp_path))
    assert res.produced["hints"] == []
    assert res.fallback_taken is False  # 2/3 samples parsed fine (just reported nothing)


def test_all_samples_unparseable_falls_back_to_empty_hints(tmp_path):
    by_variant = {
        "consistency@1#s0": "not json at all",
        "consistency@1#s1": "still not json",
        "consistency@1#s2": "nope",
    }
    res = ConsistencyAssistant().run(_dialogue_input(), _router(by_variant, tmp_path))
    assert res.fallback_taken is True
    assert res.produced["hints"] == []
    assert len(res.request_hashes) == 3


def test_consistency_checker_findings_are_strictly_llm_assisted_partitioned(tmp_path):
    """THE key acceptance anchor: llm-assisted Findings from ConsistencyChecker
    must land in report.llm_assisted_findings and NEVER in
    report.deterministic_findings, regardless of what other checkers produce.
    This is the real evaluation of M1's LlmRoutedChecker placeholder."""
    by_variant = {
        "consistency@1#s0": json.dumps([_MAJORITY_HINT]),
        "consistency@1#s1": json.dumps([_MAJORITY_HINT]),
        "consistency@1#s2": "[]",
    }
    router = _router(by_variant, tmp_path)
    assistant = ConsistencyAssistant()
    checker = ConsistencyChecker(assistant, router, _dialogue_input())

    snap = Snapshot.from_entities_relations([Entity(id="q", type=NodeType.QUEST)], [])
    report = build_review_report(snap, [checker])

    assert report.llm_assisted_findings != []
    assert report.deterministic_findings == []
    for f in report.llm_assisted_findings:
        assert f.oracle_type == "llm-assisted"
        assert f.source == "llm"
        assert f.status == "unproven"
        assert f.defect_class == "narrative_inconsistency"
        assert f.snapshot_id == snap.snapshot_id


def test_consistency_checker_check_directly(tmp_path):
    by_variant = {
        "consistency@1#s0": json.dumps([_MAJORITY_HINT]),
        "consistency@1#s1": json.dumps([_MAJORITY_HINT]),
        "consistency@1#s2": json.dumps([_MAJORITY_HINT]),
    }
    router = _router(by_variant, tmp_path)
    checker = ConsistencyChecker(ConsistencyAssistant(), router, _dialogue_input())
    snap = Snapshot.from_entities_relations([Entity(id="q", type=NodeType.QUEST)], [])

    findings = checker.check(snap)
    assert len(findings) == 1
    f = findings[0]
    assert f.oracle_type == "llm-assisted"
    assert f.producer_id == "consistency"
    assert f.evidence == {"span": _MAJORITY_HINT["span"]}
    assert f.message == _MAJORITY_HINT["issue"]
