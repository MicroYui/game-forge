from __future__ import annotations

import pytest
from pydantic import ValidationError

from gameforge.bench.qa.contracts import (
    QA_ACTIVE_CAP_NS,
    QaCorrectnessVerdict,
    QaEvent,
    QaSessionEvidence,
    canonical_session_bytes,
    content_sha256,
    load_session,
    seal_qa_verdict,
)

_PROTOCOL_SHA = "a" * 64


def _verdict(*, correct: bool = True) -> QaCorrectnessVerdict:
    return seal_qa_verdict(
        correct=correct,
        reader_round_trip=correct,
        native_exit_code=0 if correct else 1,
        predicate_status="clear" if correct else "violation",
        target_finding_clear=correct,
        target_entities_preserved=correct,
        new_deterministic_findings=(),
        submitted_tree_sha256="b" * 64,
        failure_reason=None if correct else "submission remains invalid",
    )


def _session_values():
    return {
        "protocol_sha256": _PROTOCOL_SHA,
        "session_id": "qa-session-01",
        "participant_id": "participant-01",
        "case_id": "case-01",
        "pair_id": "pair-01",
        "arm": "manual",
        "order": 1,
        "events": (
            QaEvent(kind="start", monotonic_ns=100),
            QaEvent(kind="finish", monotonic_ns=700),
        ),
        "final_patch_path": "final.patch",
        "final_patch_sha256": "c" * 64,
        "participant_attested_no_contamination": True,
        "verdict": _verdict(),
    }


def test_session_events_rederive_active_and_elapsed_time():
    values = _session_values()
    values["events"] = (
        QaEvent(kind="start", monotonic_ns=100),
        QaEvent(kind="pause", monotonic_ns=200),
        QaEvent(kind="resume", monotonic_ns=400),
        QaEvent(kind="finish", monotonic_ns=700),
    )

    session = QaSessionEvidence.seal(**values)

    assert session.active_ns == 400
    assert session.elapsed_ns == 600
    assert session.capped_active_ns == 400
    assert session.timed_out is False
    assert session.protocol_valid is True
    assert session.failure_reasons == ()


@pytest.mark.parametrize(
    "events, message",
    [
        ((QaEvent(kind="resume", monotonic_ns=100),), "start"),
        (
            (
                QaEvent(kind="start", monotonic_ns=100),
                QaEvent(kind="resume", monotonic_ns=200),
                QaEvent(kind="finish", monotonic_ns=300),
            ),
            "transition",
        ),
        (
            (
                QaEvent(kind="start", monotonic_ns=100),
                QaEvent(kind="finish", monotonic_ns=100),
            ),
            "increasing",
        ),
        (
            (
                QaEvent(kind="start", monotonic_ns=100),
                QaEvent(kind="pause", monotonic_ns=200),
                QaEvent(kind="finish", monotonic_ns=300),
            ),
            "transition",
        ),
    ],
)
def test_session_events_reject_invalid_monotonic_state_machine(events, message):
    values = _session_values()
    values["events"] = events

    with pytest.raises((ValidationError, ValueError), match=message):
        QaSessionEvidence.seal(**values)


def test_timeout_caps_active_time_without_invalidating_the_outcome():
    values = _session_values()
    values["events"] = (
        QaEvent(kind="start", monotonic_ns=1),
        QaEvent(kind="finish", monotonic_ns=QA_ACTIVE_CAP_NS + 101),
    )

    session = QaSessionEvidence.seal(**values)

    assert session.active_ns == QA_ACTIVE_CAP_NS + 100
    assert session.capped_active_ns == QA_ACTIVE_CAP_NS
    assert session.timed_out is True
    assert session.protocol_valid is True


def test_contamination_attestation_cannot_be_marked_protocol_valid():
    values = _session_values()
    values["participant_attested_no_contamination"] = False
    session = QaSessionEvidence.seal(**values)
    assert session.protocol_valid is False
    assert session.failure_reasons == ("participant attested arm contamination",)

    payload = session.model_dump(mode="json")
    payload["protocol_valid"] = True
    payload["evidence_sha256"] = content_sha256(
        payload,
        exclude={"evidence_sha256"},
    )
    with pytest.raises(ValidationError, match="protocol_valid"):
        QaSessionEvidence.model_validate(payload)


def test_session_rejects_tampered_derived_durations_and_missing_finish_material():
    session = QaSessionEvidence.seal(**_session_values())
    payload = session.model_dump(mode="json")
    payload["active_ns"] += 1
    payload["evidence_sha256"] = content_sha256(
        payload,
        exclude={"evidence_sha256"},
    )
    with pytest.raises(ValidationError, match="active_ns"):
        QaSessionEvidence.model_validate(payload)

    values = _session_values()
    values.pop("final_patch_path")
    with pytest.raises((ValidationError, TypeError), match="final_patch_path"):
        QaSessionEvidence.seal(**values)

    values = _session_values()
    values.pop("verdict")
    with pytest.raises((ValidationError, TypeError), match="verdict"):
        QaSessionEvidence.seal(**values)


def test_correctness_verdict_rederives_the_full_shared_oracle():
    assert _verdict().correct is True

    payload = _verdict().model_dump(mode="json")
    payload["target_entities_preserved"] = False
    payload["verdict_sha256"] = content_sha256(
        payload,
        exclude={"verdict_sha256"},
    )
    with pytest.raises(ValidationError, match="correct"):
        QaCorrectnessVerdict.model_validate(payload)


def test_session_round_trips_as_canonical_hash_bound_json(tmp_path):
    session = QaSessionEvidence.seal(**_session_values())
    path = tmp_path / "session-evidence.json"
    path.write_bytes(canonical_session_bytes(session))

    assert load_session(path) == session


def test_session_accepts_a_new_stable_participant_identity():
    values = _session_values()
    values["participant_id"] = "participant-02"

    session = QaSessionEvidence.seal(**values)

    assert session.schema_version == "qa-session@1"
    assert session.participant_id == "participant-02"


def test_session_rejects_an_unstable_participant_identity():
    values = _session_values()
    values["participant_id"] = "participant 02"

    with pytest.raises(ValidationError, match="participant_id"):
        QaSessionEvidence.seal(**values)


def test_session_contract_forbids_extra_fields():
    payload = QaSessionEvidence.seal(**_session_values()).model_dump(mode="json")
    payload["approval_nonce"] = "not-required"

    with pytest.raises(ValidationError, match="Extra inputs"):
        QaSessionEvidence.model_validate(payload)
