from pathlib import Path

from gameforge.bench.external_cases.endless_sky_qa import (
    validate_imported_evidence,
)
from gameforge.bench.external_cases.qualify import load_manifest
from gameforge.bench.hed.contracts import load_evidence as load_hed_evidence


_ROOT = Path("scenarios/external_cases/endless_sky")
_EXTERNAL = _ROOT / "external-corpus-manifest.json"
_HED = _ROOT / "hed-evidence.json"
_QA_PROTOCOL = _ROOT / "qa-protocol-participant-04.json"
_QA_EVIDENCE = _ROOT / "qa-evidence.json"


def test_accepted_human_evidence_slice_is_complete_and_honest():
    external = load_manifest(_EXTERNAL)
    hed = load_hed_evidence(_HED)
    qa = validate_imported_evidence(
        _QA_EVIDENCE,
        protocol_path=_QA_PROTOCOL,
    )

    assert len(external.cases) == len(hed.outcomes) == len(qa.sessions) == 8
    assert hed.metric.planned_n == hed.metric.evaluated_n == 8
    assert qa.participant_id == "participant-04"
    assert {session.participant_id for session in qa.sessions} == {"participant-04"}
    assert qa.score.planned_pairs == qa.score.evaluated_pairs == 4
    assert qa.score.protocol_failure_pairs == 0
    assert qa.score.manual_success.n == qa.score.assisted_success.n == 4
    assert all(session.protocol_valid for session in qa.sessions)
    assert all(session.participant_attested_no_contamination for session in qa.sessions)
    assert all(session.verdict is not None for session in qa.sessions)
