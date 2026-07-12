from __future__ import annotations

import pytest
from pydantic import ValidationError

from gameforge.bench.qa.contracts import QaEvent, QaSessionEvidence, seal_qa_verdict
from gameforge.bench.qa.protocol import load_protocol
from gameforge.bench.qa.score import (
    QaEvidenceManifest,
    canonical_evidence_bytes,
    content_sha256,
    load_evidence,
    score_sessions,
    seal_qa_evidence,
)

_PROTOCOL = "scenarios/external_cases/endless_sky/qa-protocol.json"
_SECOND = 1_000_000_000


def _verdict(correct: bool):
    return seal_qa_verdict(
        correct=correct,
        reader_round_trip=correct,
        native_exit_code=0 if correct else 1,
        predicate_status="clear" if correct else "violation",
        target_finding_clear=correct,
        target_entities_preserved=correct,
        new_deterministic_findings=(),
        submitted_tree_sha256="a" * 64,
        failure_reason=None if correct else "incorrect final submission",
    )


def _session(spec, *, seconds: int, correct: bool = True, valid: bool = True):
    values = {
        "protocol_sha256": load_protocol(_PROTOCOL).protocol_sha256,
        "session_id": spec.session_id,
        "participant_id": "participant-01",
        "case_id": spec.case_id,
        "pair_id": spec.pair_id,
        "arm": spec.arm,
        "order": spec.order,
        "events": (
            QaEvent(kind="start", monotonic_ns=1),
            QaEvent(kind="finish", monotonic_ns=seconds * _SECOND + 1),
        ),
        "final_patch_path": f"qa-patches/{spec.session_id}.patch",
        "final_patch_sha256": f"{spec.order:064x}",
        "participant_attested_no_contamination": valid,
        "verdict": _verdict(correct),
    }
    return QaSessionEvidence.seal(**values)


def _sessions(
    *,
    manual_seconds=(300, 300, 300, 300),
    assisted_seconds=(60, 60, 60, 60),
    manual_correct=(True, True, True, True),
    assisted_correct=(True, True, True, True),
):
    protocol = load_protocol(_PROTOCOL)
    sessions = []
    for pair_index, pair_id in enumerate(
        sorted({item.pair_id for item in protocol.sessions})
    ):
        pair = [item for item in protocol.sessions if item.pair_id == pair_id]
        for spec in pair:
            if spec.arm == "manual":
                seconds = manual_seconds[pair_index]
                correct = manual_correct[pair_index]
            else:
                seconds = assisted_seconds[pair_index]
                correct = assisted_correct[pair_index]
            sessions.append(_session(spec, seconds=seconds, correct=correct))
    return tuple(sorted(sessions, key=lambda item: item.order))


def test_score_uses_all_four_pairs_and_same_correctness_contract():
    protocol = load_protocol(_PROTOCOL)
    score = score_sessions(protocol, _sessions())

    assert score.planned_pairs == 4
    assert score.evaluated_pairs == 4
    assert score.protocol_failure_pairs == 0
    assert len(score.pairs) == 4
    assert score.manual_success.n == score.assisted_success.n == 4
    assert score.manual_success.k == score.assisted_success.k == 4
    assert score.conclusion == "savings"


def test_savings_claim_requires_positive_lower_bound_and_no_success_regression():
    protocol = load_protocol(_PROTOCOL)
    clearly_faster = score_sessions(protocol, _sessions())
    wide_interval = score_sessions(
        protocol,
        _sessions(assisted_seconds=(60, 60, 420, 420)),
    )
    less_correct = score_sessions(
        protocol,
        _sessions(assisted_correct=(True, True, True, False)),
    )
    slower = score_sessions(
        protocol,
        _sessions(manual_seconds=(60, 60, 60, 60), assisted_seconds=(120, 120, 120, 120)),
    )

    assert clearly_faster.conclusion == "savings"
    assert clearly_faster.saved_minutes_ci_low > 0
    assert wide_interval.conclusion == "inconclusive"
    assert wide_interval.saved_minutes_ci_low <= 0
    assert less_correct.conclusion == "inconclusive"
    assert slower.conclusion == "negative"


def test_incorrect_and_timed_out_sessions_remain_valid_outcomes():
    protocol = load_protocol(_PROTOCOL)
    sessions = list(
        _sessions(
            manual_seconds=(481, 300, 300, 300),
            manual_correct=(False, True, True, True),
        )
    )

    score = score_sessions(protocol, sessions)

    assert score.evaluated_pairs == 4
    assert score.protocol_failure_pairs == 0
    assert score.manual_success.k == 3
    assert any(item.timed_out for item in sessions)


def test_invalid_session_or_zero_manual_time_makes_its_pair_unevaluated():
    protocol = load_protocol(_PROTOCOL)
    sessions = list(_sessions())
    manual_index = next(index for index, item in enumerate(sessions) if item.arm == "manual")
    sessions[manual_index] = sessions[manual_index].model_copy(
        update={"protocol_valid": False, "failure_reasons": ("contaminated",)}
    )
    invalid = score_sessions(protocol, sessions)
    assert invalid.evaluated_pairs == 3
    assert invalid.protocol_failure_pairs == 1
    assert invalid.conclusion == "failed"

    sessions = list(_sessions())
    sessions[manual_index] = sessions[manual_index].model_copy(
        update={"capped_active_ns": 0}
    )
    zero = score_sessions(protocol, sessions)
    assert zero.evaluated_pairs == 3
    assert zero.protocol_failure_pairs == 1
    assert zero.conclusion == "failed"


def test_session_order_case_and_arm_must_match_frozen_protocol():
    protocol = load_protocol(_PROTOCOL)
    sessions = list(_sessions())
    sessions[0] = sessions[0].model_copy(update={"case_id": sessions[1].case_id})

    score = score_sessions(protocol, sessions)

    assert score.protocol_failure_pairs == 1
    assert score.conclusion == "failed"

    unexpected = (*_sessions(), _sessions()[0].model_copy(update={"session_id": "extra"}))
    extra_score = score_sessions(protocol, unexpected)
    assert extra_score.protocol_failure_pairs == 4
    assert extra_score.conclusion == "failed"


def test_qa_evidence_round_trips_and_rejects_metric_or_hash_tampering(tmp_path):
    protocol = load_protocol(_PROTOCOL)
    evidence = seal_qa_evidence(protocol, _sessions())
    path = tmp_path / "qa-evidence.json"
    path.write_bytes(canonical_evidence_bytes(evidence))

    assert load_evidence(path) == evidence
    payload = evidence.model_dump(mode="json")
    payload["score"]["evaluated_pairs"] = 3
    payload["evidence_sha256"] = content_sha256(
        payload,
        exclude={"evidence_sha256"},
    )
    with pytest.raises(ValidationError, match="score|evaluated"):
        QaEvidenceManifest.model_validate(payload)
