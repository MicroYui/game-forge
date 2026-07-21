from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from gameforge.bench.external_cases.qualify import load_manifest as load_external
from gameforge.bench.hed.contracts import load_evidence
from gameforge.bench.qa.protocol import (
    QaProtocol,
    assert_qa_protocol_ready,
    canonical_protocol_bytes,
    load_protocol,
    seal_qa_protocol,
)

_ROOT = "scenarios/external_cases/endless_sky"
_EXTERNAL = f"{_ROOT}/external-corpus-manifest.json"
_HED = f"{_ROOT}/hed-evidence.json"
_QA_PROTOCOL = f"{_ROOT}/qa-protocol.json"
_RETEST_PROTOCOL = f"{_ROOT}/qa-protocol-participant-02.json"
_RETEST_PROTOCOL_03 = f"{_ROOT}/qa-protocol-participant-03.json"
_RETEST_PROTOCOL_04 = f"{_ROOT}/qa-protocol-participant-04.json"
_QA_PROTOCOL_SHA256 = "7e5f68640101dbad25083abfa8c37dbe47cc1a49a4200b4aca69ce6e3a331d48"
_RETEST_PROTOCOL_SHA256 = "3dafdf76e8c8fda3302394e973bc88309fb718ed7b7b80e84bec38c6f3703b1a"
_RETEST_PROTOCOL_03_SHA256 = "a60dd9cc65313651b09a6279cbe1a2e7b3af67a31376e671d8936795fd60d43f"
_RETEST_PROTOCOL_04_SHA256 = "40afa46f4be87f2573148ce3ff12254e3761e51cf8f849fbeb59c2e585ff6cee"


def _inputs():
    return load_external(_EXTERNAL), load_evidence(_HED)


def _protocol():
    return seal_qa_protocol(*_inputs())


def test_schedule_has_four_complete_counterbalanced_pairs():
    protocol = _protocol()

    assert len(protocol.sessions) == 8
    assert len({item.pair_id for item in protocol.sessions}) == 4
    for pair_id in {item.pair_id for item in protocol.sessions}:
        pair = [item for item in protocol.sessions if item.pair_id == pair_id]
        assert {item.arm for item in pair} == {"manual", "assisted"}
        assert len({item.defect_class for item in pair}) == 1
        assert {item.split for item in pair} == {"development", "verification"}
    assert (
        sum(item.arm == "assisted" and item.split == "development" for item in protocol.sessions)
        == 2
    )
    assert sum(item.arm == "assisted" and item.order <= 4 for item in protocol.sessions) == 2
    assert tuple(item.order for item in protocol.sessions) == tuple(range(1, 9))
    assert protocol.participant_id == "participant-01"
    assert protocol.active_cap_ns == 480_000_000_000
    assert protocol.total_active_cap_ns == 3_840_000_000_000


def test_schedule_uses_the_exact_frozen_four_row_pattern():
    protocol = _protocol()
    rows = [(item.split, item.arm) for item in protocol.sessions]
    assert rows == [
        ("development", "manual"),
        ("verification", "assisted"),
        ("development", "assisted"),
        ("verification", "manual"),
        ("verification", "manual"),
        ("development", "assisted"),
        ("verification", "assisted"),
        ("development", "manual"),
    ]


def test_protocol_binds_external_and_measured_hed_evidence():
    external, hed = _inputs()
    protocol = _protocol()

    assert protocol.external_manifest_sha256 == external.manifest_sha256
    assert protocol.hed_evidence_sha256 == hed.evidence_sha256
    assert protocol.correctness_protocol_id == "external-submission-verdict@1"
    assert protocol.frozen is True
    assert_qa_protocol_ready(protocol, external, hed)


def test_protocol_rejects_hed_protocol_failure_or_missing_outcome():
    external, hed = _inputs()
    failed_metric = hed.metric.model_copy(update={"evaluated_n": 7, "protocol_failure_count": 1})
    failed = hed.model_copy(update={"metric": failed_metric})
    with pytest.raises(ValueError, match="protocol failure|evaluated"):
        seal_qa_protocol(external, failed)

    missing = hed.model_copy(update={"outcomes": hed.outcomes[:-1]})
    with pytest.raises(ValueError, match="denominator|outcome"):
        seal_qa_protocol(external, missing)


def test_protocol_rejects_external_or_hed_hash_mismatch():
    external, hed = _inputs()
    mismatched = hed.model_copy(update={"external_manifest_sha256": "f" * 64})

    with pytest.raises(ValueError, match="external manifest"):
        seal_qa_protocol(external, mismatched)


def test_protocol_round_trips_as_canonical_hash_bound_json(tmp_path):
    protocol = _protocol()
    path = tmp_path / "qa-protocol.json"
    path.write_bytes(canonical_protocol_bytes(protocol))

    assert load_protocol(path) == protocol


def test_committed_protocol_is_the_exact_frozen_schedule():
    protocol = load_protocol(_QA_PROTOCOL)

    assert protocol == _protocol()
    assert protocol.protocol_sha256 == _QA_PROTOCOL_SHA256


def test_committed_retest_protocol_is_bound_to_the_new_participant():
    external, hed = _inputs()
    protocol = load_protocol(_RETEST_PROTOCOL)

    assert protocol == seal_qa_protocol(
        external,
        hed,
        participant_id="participant-02",
        id_namespace="qa-retest-02",
    )
    assert protocol.participant_id == "participant-02"
    assert protocol.protocol_sha256 == _RETEST_PROTOCOL_SHA256
    assert canonical_protocol_bytes(_protocol()) == Path(_QA_PROTOCOL).read_bytes()


def test_committed_retest_protocol_03_replaces_the_consumed_preflight_identity():
    external, hed = _inputs()
    protocol = load_protocol(_RETEST_PROTOCOL_03)

    assert protocol == seal_qa_protocol(
        external,
        hed,
        participant_id="participant-03",
        id_namespace="qa-retest-03",
    )
    assert protocol.participant_id == "participant-03"
    assert protocol.protocol_sha256 == _RETEST_PROTOCOL_03_SHA256


def test_committed_retest_protocol_04_replaces_the_editor_failure_identity():
    external, hed = _inputs()
    protocol = load_protocol(_RETEST_PROTOCOL_04)

    assert protocol == seal_qa_protocol(
        external,
        hed,
        participant_id="participant-04",
        id_namespace="qa-retest-04",
    )
    assert protocol.participant_id == "participant-04"
    assert protocol.protocol_sha256 == _RETEST_PROTOCOL_04_SHA256


def test_new_participant_and_namespace_seal_an_independent_frozen_schedule():
    external, hed = _inputs()

    protocol = seal_qa_protocol(
        external,
        hed,
        participant_id="participant-02",
        id_namespace="qa-retest-02",
    )

    assert protocol.participant_id == "participant-02"
    assert tuple(item.session_id for item in protocol.sessions) == tuple(
        f"qa-retest-02-session-{order:02d}" for order in range(1, 9)
    )
    assert sorted({item.pair_id for item in protocol.sessions}) == [
        f"qa-retest-02-pair-{pair:02d}" for pair in range(1, 5)
    ]
    assert protocol.protocol_sha256 != _protocol().protocol_sha256
    assert_qa_protocol_ready(protocol, external, hed)


def test_protocol_rejects_a_mixed_session_or_pair_namespace():
    external, hed = _inputs()
    protocol = seal_qa_protocol(
        external,
        hed,
        participant_id="participant-02",
        id_namespace="qa-retest-02",
    )

    mixed_sessions = list(protocol.sessions)
    mixed_sessions[0] = mixed_sessions[0].model_copy(update={"session_id": "qa-session-01"})
    with pytest.raises(ValidationError, match="namespace|session ID"):
        QaProtocol.seal(
            participant_id=protocol.participant_id,
            external_manifest_sha256=protocol.external_manifest_sha256,
            hed_evidence_sha256=protocol.hed_evidence_sha256,
            sessions=mixed_sessions,
        )

    mixed_pairs = [
        item.model_copy(update={"pair_id": "qa-pair-01"})
        if item.pair_id == "qa-retest-02-pair-01"
        else item
        for item in protocol.sessions
    ]
    with pytest.raises(ValidationError, match="namespace|pair ID"):
        QaProtocol.seal(
            participant_id=protocol.participant_id,
            external_manifest_sha256=protocol.external_manifest_sha256,
            hed_evidence_sha256=protocol.hed_evidence_sha256,
            sessions=mixed_pairs,
        )


def test_protocol_rejects_tampered_order_and_extra_fields():
    payload = _protocol().model_dump(mode="json")
    payload["sessions"][0]["order"] = 8
    payload["protocol_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="order|protocol_sha256"):
        QaProtocol.model_validate(payload)

    payload = _protocol().model_dump(mode="json")
    payload["reviewer_id"] = "not-required"
    with pytest.raises(ValidationError, match="Extra inputs"):
        QaProtocol.model_validate(payload)
