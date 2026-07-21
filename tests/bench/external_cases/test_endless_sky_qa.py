from __future__ import annotations

import json
from pathlib import Path

import pytest

from gameforge.bench.external_cases.endless_sky_qa import (
    _bound_workspace,
    _validate_workspace_session,
    evaluate_submission,
    import_workspace_evidence,
    materialize_case,
    next_session,
    validate_imported_evidence,
)
from gameforge.bench.external_cases.endless_sky_runner import load_case_runtime
from gameforge.bench.external_cases.qualify import load_manifest
from gameforge.bench.hed.contracts import load_evidence
from gameforge.bench.qa.protocol import load_protocol, seal_qa_protocol, write_protocol
from gameforge.bench.qa.harness import finalize_session
from gameforge.bench.qa.session import (
    QaSessionState,
    canonical_state_bytes,
    load_state,
    transition_session,
)

_ROOT = Path("scenarios/external_cases/endless_sky")
_EXTERNAL = _ROOT / "external-corpus-manifest.json"
_HED = _ROOT / "hed-evidence.json"
_PROTOCOL = _ROOT / "qa-protocol.json"


def _inputs():
    return load_manifest(_EXTERNAL), load_evidence(_HED), load_protocol(_PROTOCOL)


def _text_payload(bundle: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in bundle.rglob("*")
        if path.is_file() and path.suffix in {".json", ".txt"}
    )


def test_manual_bundle_contains_no_gameforge_or_answer_material(tmp_path):
    external, hed, protocol = _inputs()
    session = next(item for item in protocol.sessions if item.arm == "manual")

    bundle = materialize_case(
        session.case_id,
        tmp_path / "manual",
        session=session,
        assisted=None,
        external=external,
        protocol=protocol,
    )

    names = {path.relative_to(bundle).as_posix() for path in bundle.rglob("*") if path.is_file()}
    assert "TASK.json" in names
    assert "session-state.json" in names
    assert "tools/syntax-checker" in names
    assert all("finding" not in name.casefold() for name in names)
    assert all("agent" not in name.casefold() for name in names)
    payload = _text_payload(bundle).casefold()
    assert "target_locators" not in payload
    assert "predicate" not in payload
    assert "upstream.patch" not in payload
    assert "human_target" not in payload
    assert "gameforge.json" not in {name.casefold() for name in names}
    assert hed.evidence_sha256 not in payload
    assert session.case_id.casefold() not in payload
    assert session.defect_class.value.casefold() not in payload


def test_assisted_bundle_adds_only_finding_and_agent_proposal(tmp_path):
    external, hed, protocol = _inputs()
    session = next(item for item in protocol.sessions if item.arm == "assisted")
    outcome = next(item for item in hed.outcomes if item.case_id == session.case_id)

    bundle = materialize_case(
        session.case_id,
        tmp_path / "assisted",
        session=session,
        assisted=outcome,
        external=external,
        protocol=protocol,
    )

    assistance = json.loads((bundle / "GAMEFORGE.json").read_text())
    assert set(assistance) == {
        "finding",
        "agent_patch",
        "passed_verification",
        "disposition",
    }
    assert assistance["finding"] == outcome.target_finding.model_dump(
        mode="json",
        exclude_none=True,
    )
    assert assistance["passed_verification"] == outcome.passed_verification


def test_bundle_work_tree_is_the_exact_frozen_before_side(tmp_path):
    external, _, protocol = _inputs()
    session = protocol.sessions[0]
    evidence = next(item for item in external.cases if item.spec.case_id == session.case_id)
    runtime = load_case_runtime(_ROOT, evidence.spec)
    bundle = materialize_case(
        session.case_id,
        tmp_path / "bundle",
        session=session,
        assisted=None,
        external=external,
        protocol=protocol,
    )

    assert {
        path: (bundle / "work" / path).read_bytes() for path in runtime.spec.changed_paths
    } == runtime.before_raw


def test_both_before_and_human_target_use_the_same_submission_oracle(tmp_path):
    external, _, protocol = _inputs()
    session = protocol.sessions[0]
    evidence = next(item for item in external.cases if item.spec.case_id == session.case_id)
    runtime = load_case_runtime(_ROOT, evidence.spec)
    bundle = materialize_case(
        session.case_id,
        tmp_path / "bundle",
        session=session,
        assisted=None,
        external=external,
        protocol=protocol,
    )

    _, before_verdict = evaluate_submission(
        session.case_id,
        bundle / "work",
        external=external,
    )
    assert before_verdict.correct is False
    for path, raw in runtime.human_target_raw.items():
        (bundle / "work" / path).write_bytes(raw)
    patch, after_verdict = evaluate_submission(
        session.case_id,
        bundle / "work",
        external=external,
    )
    assert patch
    assert after_verdict.correct is True


def test_next_exposes_only_one_session_until_current_finishes(tmp_path):
    _, _, protocol = _inputs()
    workspace = tmp_path / "qa-workspace"

    first = next_session(workspace)
    assert first.name == protocol.sessions[0].session_id
    assert {path.name for path in (workspace / "sessions").iterdir()} == {
        protocol.sessions[0].session_id
    }
    with pytest.raises(ValueError, match="not finished"):
        next_session(workspace)


def test_workspace_is_bound_to_the_frozen_protocol_before_materialization(tmp_path):
    _, _, protocol = _inputs()
    workspace = tmp_path / "qa-workspace"

    next_session(workspace)

    marker = json.loads((workspace / ".qa-workspace.json").read_text(encoding="utf-8"))
    assert marker == {
        "participant_id": protocol.participant_id,
        "protocol_sha256": protocol.protocol_sha256,
        "schema_version": "qa-workspace@1",
    }


def test_nonempty_unbound_or_wrong_protocol_workspace_is_rejected(tmp_path):
    unbound = tmp_path / "unbound"
    unbound.mkdir()
    (unbound / "old-session.txt").write_text("old", encoding="utf-8")
    with pytest.raises(ValueError, match="workspace.*bound|nonempty"):
        next_session(unbound)

    bound = tmp_path / "bound"
    next_session(bound)
    marker_path = bound / ".qa-workspace.json"
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["protocol_sha256"] = "f" * 64
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    with pytest.raises(ValueError, match="workspace.*protocol|canonical"):
        next_session(bound)


def test_new_participant_protocol_uses_an_isolated_workspace_namespace(tmp_path):
    external, hed, _ = _inputs()
    protocol = seal_qa_protocol(
        external,
        hed,
        participant_id="participant-02",
        id_namespace="qa-retest-02",
    )
    protocol_path = tmp_path / "qa-protocol-participant-02.json"
    write_protocol(protocol_path, protocol)
    workspace = tmp_path / "participant-02-workspace"

    bundle = next_session(workspace, protocol_path=protocol_path)

    assert bundle.name == "qa-retest-02-session-01"
    marker = json.loads((workspace / ".qa-workspace.json").read_text(encoding="utf-8"))
    assert marker["participant_id"] == "participant-02"
    assert marker["protocol_sha256"] == protocol.protocol_sha256
    with pytest.raises(ValueError, match="workspace.*protocol|canonical"):
        next_session(workspace)


class _Clock:
    def __init__(self, value: int) -> None:
        self._value = value

    def __call__(self) -> int:
        return self._value


def test_import_revalidates_and_packages_complete_synthetic_workspace(tmp_path):
    external, hed, protocol = _inputs()
    workspace = tmp_path / "synthetic-workspace"
    _bound_workspace(workspace, protocol)
    sessions_root = workspace / "sessions"
    sessions_root.mkdir(parents=True)
    outcomes = {item.case_id: item for item in hed.outcomes}
    evidence_by_id = {item.spec.case_id: item for item in external.cases}

    for spec in protocol.sessions:
        bundle = materialize_case(
            spec.case_id,
            sessions_root / spec.session_id,
            session=spec,
            assisted=outcomes[spec.case_id] if spec.arm == "assisted" else None,
            external=external,
            protocol=protocol,
        )
        runtime = load_case_runtime(_ROOT, evidence_by_id[spec.case_id].spec)
        for path, raw in runtime.human_target_raw.items():
            (bundle / "work" / path).write_bytes(raw)
        started = spec.order * 10_000
        transition_session(bundle, "start", clock=_Clock(started))
        finalize_session(
            protocol,
            spec,
            bundle,
            evaluator=lambda work, case_id=spec.case_id: evaluate_submission(
                case_id,
                work,
                external=external,
            ),
            participant_attested_no_contamination=True,
            clock=_Clock(started + 1_000),
        )
        late_save = bundle / "work" / runtime.spec.changed_paths[0]
        late_save.parent.mkdir(parents=True, exist_ok=True)
        late_save.write_bytes(b"late editor save must not replace the frozen submission\n")

    artifact_root = tmp_path / "artifacts"
    evidence = import_workspace_evidence(workspace, artifact_root)

    assert evidence.score.evaluated_pairs == 4
    assert evidence.score.protocol_failure_pairs == 0
    assert len(list((artifact_root / "qa-sessions").glob("*.json"))) == 8
    assert len(list((artifact_root / "qa-patches").glob("*.patch"))) == 8
    assert validate_imported_evidence(artifact_root / "qa-evidence.json") == evidence

    first, second = protocol.sessions[:2]
    (artifact_root / "qa-sessions" / f"{first.session_id}.json").write_bytes(
        (artifact_root / "qa-sessions" / f"{second.session_id}.json").read_bytes()
    )
    with pytest.raises(ValueError, match="stored session mismatch"):
        validate_imported_evidence(artifact_root / "qa-evidence.json")


def test_import_refuses_missing_session_evidence(tmp_path):
    workspace = tmp_path / "incomplete"
    workspace.mkdir()

    with pytest.raises(ValueError, match="missing QA session"):
        import_workspace_evidence(workspace, tmp_path / "artifacts")


def test_workspace_validation_rejects_frozen_state_identity_mismatch(tmp_path):
    external, _, protocol = _inputs()
    spec = protocol.sessions[0]
    bundle = materialize_case(
        spec.case_id,
        tmp_path / spec.session_id,
        session=spec,
        assisted=None,
        external=external,
        protocol=protocol,
    )
    transition_session(bundle, "start", clock=_Clock(100))
    finalize_session(
        protocol,
        spec,
        bundle,
        evaluator=lambda work: evaluate_submission(
            spec.case_id,
            work,
            external=external,
        ),
        participant_attested_no_contamination=True,
        clock=_Clock(200),
    )
    state = load_state(bundle)
    tampered = QaSessionState.seal(
        protocol_sha256=state.protocol_sha256,
        session_id="qa-session-wrong",
        pair_id=state.pair_id,
        arm=state.arm,
        order=state.order,
        status=state.status,
        events=state.events,
        submission=state.submission,
        participant_attested_no_contamination=(state.participant_attested_no_contamination),
    )
    (bundle / "session-state.json").write_bytes(canonical_state_bytes(tampered))

    with pytest.raises(ValueError, match="frozen session state mismatch"):
        _validate_workspace_session(bundle, spec, protocol, external)
