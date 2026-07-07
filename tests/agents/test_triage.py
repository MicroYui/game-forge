import json

from gameforge.agents.triage.triager import DefectTriager
from gameforge.contracts.agent_io import FindingsInput
from gameforge.contracts.findings import Finding
from gameforge.contracts.model_router import ModelResponse
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.router import ModelRouter, RouterMode


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


def _finding(fid: str, defect_class: str) -> Finding:
    return Finding(
        id=fid,
        source="checker",
        producer_id="p",
        producer_run_id="r1",
        oracle_type="deterministic",
        defect_class=defect_class,
        severity="major",
        snapshot_id="snap1",
        status="confirmed",
        message=f"finding {fid}",
    )


def test_triage_drops_invented_ids_and_bad_priority(tmp_path):
    f1, f2, f3 = _finding("f1", "dangling_reference"), _finding("f2", "reward_out_of_range"), _finding("f3", "cycle")
    payload = json.dumps([
        {
            "cluster_id": "C1",
            "finding_ids": ["f1", "f2", "f9"],  # f9 is invented — must be dropped
            "priority": "p1",
            "suspected_root_cause": "shared root cause",
        },
        {
            "cluster_id": "C2",
            "finding_ids": ["f3"],
            "priority": "p9",  # invalid priority — whole cluster dropped
            "suspected_root_cause": "bogus",
        },
    ])
    res = DefectTriager().run(FindingsInput(findings=[f1, f2, f3]), _router(payload, tmp_path))

    triaged = res.produced["triaged"]
    clusters = triaged["clusters"]
    assert len(clusters) == 1  # C2 dropped for invalid priority
    assert clusters[0]["cluster_id"] == "C1"
    assert set(clusters[0]["finding_ids"]) == {"f1", "f2"}  # f9 removed
    assert res.produced["dropped_ids"] >= 1

    all_ids = {fid for c in clusters for fid in c["finding_ids"]}
    assert all_ids <= {"f1", "f2", "f3"}

    # the triager only groups — it must never restate/re-judge a Finding's verdict
    dumped = json.dumps(res.produced)
    assert "status" not in dumped
    assert "oracle_type" not in dumped
    assert "defect_class" not in dumped

    assert res.fallback_taken is False
    assert res.role == "triage"
    assert len(res.request_hashes) == 1


def test_triage_fallback_on_unparseable_output(tmp_path):
    f1 = _finding("f1", "dangling_reference")
    res = DefectTriager().run(FindingsInput(findings=[f1]), _router("not json at all", tmp_path))
    assert res.fallback_taken is True
    assert res.produced["triaged"]["clusters"] == []
