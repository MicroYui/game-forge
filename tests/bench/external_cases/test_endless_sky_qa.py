from __future__ import annotations

import json
from pathlib import Path

import pytest

from gameforge.bench.external_cases.endless_sky_qa import (
    evaluate_submission,
    materialize_case,
    next_session,
)
from gameforge.bench.external_cases.endless_sky_runner import load_case_runtime
from gameforge.bench.external_cases.qualify import load_manifest
from gameforge.bench.hed.contracts import load_evidence
from gameforge.bench.qa.protocol import load_protocol

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

    names = {
        path.relative_to(bundle).as_posix()
        for path in bundle.rglob("*")
        if path.is_file()
    }
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
        path: (bundle / "work" / path).read_bytes()
        for path in runtime.spec.changed_paths
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
