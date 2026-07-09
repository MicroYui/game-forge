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
    double, no network) — lets a test give each perspective sample (and each
    rebuttal round query) its own scripted answer. Any variant not explicitly
    scripted defaults to "[]" (an empty, well-formed sample)."""

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
    # 2/3 perspectives report the hint directly (no dispute — the third is
    # simply silent), so it must pass WITHOUT any rebuttal round at all.
    by_variant = {
        "consistency@1#p_temporal": json.dumps([_MAJORITY_HINT]),
        "consistency@1#p_identity": json.dumps([_MAJORITY_HINT]),
        "consistency@1#p_spoiler": "[]",
    }
    transport = _PerVariantTransport(by_variant)
    router = ModelRouter(transport, CassetteStore(tmp_path), mode=RouterMode.PASSTHROUGH)
    res = ConsistencyAssistant().run(_dialogue_input(), router)

    hints = res.produced["hints"]
    assert res.produced["samples"] == 3
    assert len(hints) == 1
    assert hints[0]["span"] == _MAJORITY_HINT["span"]
    assert hints[0]["issue"] == _MAJORITY_HINT["issue"]
    assert hints[0]["is_suggestion"] is True

    assert res.fallback_taken is False
    assert res.role == "consistency"
    assert len(res.request_hashes) == 3
    assert len(set(res.request_hashes)) == 3  # 3 distinct perspective variants

    # Nothing was disputed (no hint in [1, threshold)) so no rebuttal round
    # (`#r_*` variants) was ever queried — exactly 3 calls total.
    assert len(transport.calls) == 3
    assert all("#p_" in c.prompt_version for c in transport.calls)


def test_quorum_drops_hint_reported_by_only_one_sample(tmp_path):
    by_variant = {
        "consistency@1#p_temporal": json.dumps([_MAJORITY_HINT]),
        "consistency@1#p_identity": "[]",
        "consistency@1#p_spoiler": "[]",
    }
    res = ConsistencyAssistant().run(_dialogue_input(), _router(by_variant, tmp_path))
    assert res.produced["hints"] == []
    assert res.fallback_taken is False  # 3/3 samples parsed fine (2 just reported nothing)


def test_all_samples_unparseable_falls_back_to_empty_hints(tmp_path):
    by_variant = {
        "consistency@1#p_temporal": "not json at all",
        "consistency@1#p_identity": "still not json",
        "consistency@1#p_spoiler": "nope",
    }
    res = ConsistencyAssistant().run(_dialogue_input(), _router(by_variant, tmp_path))
    assert res.fallback_taken is True
    assert res.produced["hints"] == []
    assert len(res.request_hashes) == 3


# --------------------------------------------------------------------------
# Rebuttal round (Part C / Task 9): a hint reported by only 1/3 perspectives
# is DISPUTED and must go through exactly one rebuttal round; it survives
# only if the rebuttal round lifts confirmations to >= threshold. These two
# tests are the discriminators: if the rebuttal round (or its threshold
# check) were removed, one of the two would flip.
# --------------------------------------------------------------------------
def test_disputed_hint_survives_rebuttal_when_confirmed(tmp_path):
    # Only 'temporal' reports the hint in round 1 -> count=1 < threshold(2) ->
    # disputed -> rebuttal round triggered. In the rebuttal round, 'identity'
    # and 'spoiler' both confirm -> confirmations=2 >= threshold(2) -> keep.
    by_variant = {
        "consistency@1#p_temporal": json.dumps([_MINORITY_HINT]),
        "consistency@1#p_identity": "[]",
        "consistency@1#p_spoiler": "[]",
        "consistency@1#r_temporal": "[]",
        "consistency@1#r_identity": json.dumps([_MINORITY_HINT]),
        "consistency@1#r_spoiler": json.dumps([_MINORITY_HINT]),
    }
    transport = _PerVariantTransport(by_variant)
    router = ModelRouter(transport, CassetteStore(tmp_path), mode=RouterMode.PASSTHROUGH)
    res = ConsistencyAssistant().run(_dialogue_input(), router)

    assert res.produced["hints"] == [
        {**_MINORITY_HINT, "is_suggestion": True}
    ]
    assert res.fallback_taken is False
    # 3 first-round + 3 rebuttal-round calls, all distinct request_hashes.
    assert len(res.request_hashes) == 6
    assert len(set(res.request_hashes)) == 6
    rebuttal_calls = [c for c in transport.calls if "#r_" in c.prompt_version]
    assert len(rebuttal_calls) == 3


def test_disputed_hint_dropped_when_rebuttal_refutes(tmp_path):
    # Same first round (1/3 reports it), but the rebuttal round only lifts
    # confirmations to 1 (< threshold 2) -> stays dropped. If the rebuttal
    # round's threshold check were removed (e.g. "any confirmation keeps the
    # hint"), this test would incorrectly see the hint survive.
    by_variant = {
        "consistency@1#p_temporal": json.dumps([_MINORITY_HINT]),
        "consistency@1#p_identity": "[]",
        "consistency@1#p_spoiler": "[]",
        "consistency@1#r_temporal": "[]",
        "consistency@1#r_identity": json.dumps([_MINORITY_HINT]),
        "consistency@1#r_spoiler": "[]",
    }
    transport = _PerVariantTransport(by_variant)
    router = ModelRouter(transport, CassetteStore(tmp_path), mode=RouterMode.PASSTHROUGH)
    res = ConsistencyAssistant().run(_dialogue_input(), router)

    assert res.produced["hints"] == []
    assert res.fallback_taken is False
    assert len(res.request_hashes) == 6
    rebuttal_calls = [c for c in transport.calls if "#r_" in c.prompt_version]
    assert len(rebuttal_calls) == 3


def test_threshold_is_honored_unanimity_required(tmp_path):
    # threshold=3 over 3 perspectives requires unanimity. The hint is reported
    # by 2/3 in round 1 (disputed under threshold=3, since 2 < 3); with
    # rebut=False no rebuttal round runs at all, so it cannot be rescued and
    # must be dropped. If `threshold` were ignored (hardcoded to 2), this
    # hint would incorrectly survive from round 1 alone.
    by_variant = {
        "consistency@1#p_temporal": json.dumps([_MAJORITY_HINT]),
        "consistency@1#p_identity": json.dumps([_MAJORITY_HINT]),
        "consistency@1#p_spoiler": "[]",
    }
    transport = _PerVariantTransport(by_variant)
    router = ModelRouter(transport, CassetteStore(tmp_path), mode=RouterMode.PASSTHROUGH)
    res = ConsistencyAssistant().run(_dialogue_input(), router, threshold=3, rebut=False)

    assert res.produced["hints"] == []
    assert len(res.request_hashes) == 3  # rebut=False -> no rebuttal calls at all
    assert all("#r_" not in c.prompt_version for c in transport.calls)


def test_threshold_unanimity_hint_passes_when_all_three_agree(tmp_path):
    by_variant = {
        "consistency@1#p_temporal": json.dumps([_MAJORITY_HINT]),
        "consistency@1#p_identity": json.dumps([_MAJORITY_HINT]),
        "consistency@1#p_spoiler": json.dumps([_MAJORITY_HINT]),
    }
    res = ConsistencyAssistant().run(
        _dialogue_input(), _router(by_variant, tmp_path), threshold=3, rebut=False
    )
    assert len(res.produced["hints"]) == 1
    assert res.produced["hints"][0]["span"] == _MAJORITY_HINT["span"]


def test_consistency_checker_findings_are_strictly_llm_assisted_partitioned(tmp_path):
    """THE key acceptance anchor: llm-assisted Findings from ConsistencyChecker
    must land in report.llm_assisted_findings and NEVER in
    report.deterministic_findings, regardless of what other checkers produce.
    This is the real evaluation of M1's LlmRoutedChecker placeholder."""
    by_variant = {
        "consistency@1#p_temporal": json.dumps([_MAJORITY_HINT]),
        "consistency@1#p_identity": json.dumps([_MAJORITY_HINT]),
        "consistency@1#p_spoiler": "[]",
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
        "consistency@1#p_temporal": json.dumps([_MAJORITY_HINT]),
        "consistency@1#p_identity": json.dumps([_MAJORITY_HINT]),
        "consistency@1#p_spoiler": json.dumps([_MAJORITY_HINT]),
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
