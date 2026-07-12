"""Endless Sky composition and CLI for the frozen QA-hours protocol."""

from __future__ import annotations

import argparse
import hashlib
import tempfile
from pathlib import Path

from gameforge.bench.external_cases.contracts import ExternalCorpusManifest
from gameforge.bench.external_cases.endless_sky_runner import (
    load_case_runtime,
    validate_submitted_tree,
)
from gameforge.bench.external_cases.native import (
    NativeParserBinary,
    compile_native_parser,
)
from gameforge.bench.external_cases.qualify import load_manifest
from gameforge.bench.hed.contracts import HedCaseOutcome, HedEvidenceManifest, load_evidence
from gameforge.bench.qa.contracts import QaSessionSpec, seal_qa_verdict
from gameforge.bench.qa.harness import (
    QaBundleMaterial,
    finalize_session,
    read_exact_changed_paths,
    unified_submission_patch,
    write_arm_bundle,
)
from gameforge.bench.qa.protocol import (
    QaProtocol,
    assert_qa_protocol_ready,
    load_protocol,
)
from gameforge.bench.qa.session import load_state, transition_session
from gameforge.contracts.canonical import canonical_json

_ROOT = Path("scenarios/external_cases/endless_sky")
_EXTERNAL_PATH = _ROOT / "external-corpus-manifest.json"
_HED_PATH = _ROOT / "hed-evidence.json"
_PROTOCOL_PATH = _ROOT / "qa-protocol.json"
_NATIVE_SOURCE = _ROOT / "native/endless_sky_data_parser.cpp"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _frozen_inputs() -> tuple[
    ExternalCorpusManifest,
    HedEvidenceManifest,
    QaProtocol,
]:
    external = load_manifest(_EXTERNAL_PATH)
    hed = load_evidence(_HED_PATH)
    protocol = load_protocol(_PROTOCOL_PATH)
    assert_qa_protocol_ready(protocol, external, hed)
    return external, hed, protocol


def _case_evidence(external: ExternalCorpusManifest, case_id: str):
    matches = [item for item in external.cases if item.spec.case_id == case_id]
    if len(matches) != 1:
        raise ValueError(f"unknown or duplicate QA case ID: {case_id}")
    return matches[0]


def materialize_case(
    case_id: str,
    destination: Path,
    *,
    session: QaSessionSpec,
    assisted: HedCaseOutcome | None,
    external: ExternalCorpusManifest | None = None,
    protocol: QaProtocol | None = None,
) -> Path:
    active_external, active_hed, active_protocol = _frozen_inputs()
    external = external or active_external
    protocol = protocol or active_protocol
    if protocol != active_protocol or external != active_external:
        assert_qa_protocol_ready(protocol, external, active_hed)
    if session not in protocol.sessions or session.case_id != case_id:
        raise ValueError("QA materialization session/case differs from protocol")
    evidence = _case_evidence(external, case_id)
    runtime = load_case_runtime(_ROOT, evidence.spec)

    assistance = None
    if session.arm == "assisted":
        if assisted is None or assisted.case_id != case_id:
            raise ValueError("assisted QA session requires its matching HED outcome")
        assistance = {
            "finding": assisted.target_finding.model_dump(mode="json"),
            "agent_patch": (
                assisted.patch.model_dump(mode="json")
                if assisted.patch is not None
                else None
            ),
            "passed_verification": assisted.passed_verification,
            "disposition": assisted.disposition,
        }
    elif assisted is not None:
        raise ValueError("manual QA session cannot receive HED assistance")

    with tempfile.TemporaryDirectory(prefix="gameforge-qa-native-") as temporary:
        native = compile_native_parser(_NATIVE_SOURCE, Path(temporary))
        return write_arm_bundle(
            protocol,
            QaBundleMaterial(
                session=session,
                upstream_subject=evidence.spec.upstream_subject,
                before_files=runtime.before_raw,
                native_tool=native.path,
                assistance=assistance,
            ),
            destination,
        )


def evaluate_submission(
    case_id: str,
    work_root: Path,
    *,
    external: ExternalCorpusManifest | None = None,
):
    active_external = external or load_manifest(_EXTERNAL_PATH)
    evidence = _case_evidence(active_external, case_id)
    runtime = load_case_runtime(_ROOT, evidence.spec)
    submitted = read_exact_changed_paths(work_root, runtime.spec.changed_paths)
    patch = unified_submission_patch(runtime.before_raw, submitted)
    binary_path = work_root.parent / "tools" / "syntax-checker"
    binary = NativeParserBinary(
        path=binary_path,
        compiler="bundled QA syntax witness",
        source_sha256=hashlib.sha256(_NATIVE_SOURCE.read_bytes()).hexdigest(),
        binary_sha256=hashlib.sha256(binary_path.read_bytes()).hexdigest(),
    )
    verdict = validate_submitted_tree(
        runtime,
        submitted,
        native_binary=binary,
    )
    return patch, seal_qa_verdict(verdict)


def _workspace_root(value: str | Path) -> Path:
    workspace = Path(value).expanduser().resolve()
    if workspace == _REPOSITORY_ROOT or workspace.is_relative_to(_REPOSITORY_ROOT):
        raise ValueError("QA workspace must be outside the repository")
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def next_session(workspace: str | Path) -> Path:
    root = _workspace_root(workspace)
    external, hed, protocol = _frozen_inputs()
    sessions_root = root / "sessions"
    sessions_root.mkdir(exist_ok=True)
    outcomes = {item.case_id: item for item in hed.outcomes}
    for session in protocol.sessions:
        bundle = sessions_root / session.session_id
        if (bundle / "session-evidence.json").is_file():
            continue
        if bundle.exists():
            raise ValueError(f"current QA session {session.session_id} is not finished")
        assisted = outcomes[session.case_id] if session.arm == "assisted" else None
        return materialize_case(
            session.case_id,
            bundle,
            session=session,
            assisted=assisted,
            external=external,
            protocol=protocol,
        )
    raise ValueError("all frozen QA sessions are finished")


def _session_bundle(workspace: str | Path, session_id: str):
    root = _workspace_root(workspace)
    external, _, protocol = _frozen_inputs()
    matches = [item for item in protocol.sessions if item.session_id == session_id]
    if len(matches) != 1:
        raise ValueError(f"unknown QA session ID: {session_id}")
    bundle = root / "sessions" / session_id
    if not bundle.is_dir():
        raise ValueError("QA session has not been materialized by next")
    return external, protocol, matches[0], bundle


def _status(workspace: str | Path) -> dict[str, object]:
    root = _workspace_root(workspace)
    sessions_root = root / "sessions"
    rows: list[dict[str, object]] = []
    if sessions_root.exists():
        for bundle in sorted(path for path in sessions_root.iterdir() if path.is_dir()):
            state = load_state(bundle)
            rows.append(
                {
                    "session_id": state.session_id,
                    "order": state.order,
                    "arm": state.arm,
                    "status": state.status,
                    "evidence_written": (bundle / "session-evidence.json").is_file(),
                }
            )
    return {"schema_version": "qa-workspace-status@1", "sessions": rows}


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("next", "status"):
        child = subparsers.add_parser(command)
        child.add_argument("--workspace", type=Path, required=True)
    for command in ("start", "pause", "resume"):
        child = subparsers.add_parser(command)
        child.add_argument("--workspace", type=Path, required=True)
        child.add_argument("--session", required=True)
    finish = subparsers.add_parser("finish")
    finish.add_argument("--workspace", type=Path, required=True)
    finish.add_argument("--session", required=True)
    finish.add_argument("--attest-no-contamination", action="store_true")
    args = parser.parse_args()

    if args.command == "next":
        bundle = next_session(args.workspace)
        state = load_state(bundle)
        print(
            canonical_json(
                {
                    "bundle": str(bundle.resolve()),
                    "session_id": state.session_id,
                    "arm": state.arm,
                    "status": state.status,
                }
            )
        )
        return
    if args.command == "status":
        print(canonical_json(_status(args.workspace)))
        return

    external, protocol, session, bundle = _session_bundle(
        args.workspace,
        args.session,
    )
    if args.command in {"start", "pause", "resume"}:
        state = transition_session(bundle, args.command)
        print(canonical_json(state.model_dump(mode="json")))
        return
    evidence = finalize_session(
        protocol,
        session,
        bundle,
        evaluator=lambda work: evaluate_submission(
            session.case_id,
            work,
            external=external,
        ),
        participant_attested_no_contamination=args.attest_no_contamination,
    )
    print(canonical_json(evidence.model_dump(mode="json")))


if __name__ == "__main__":
    _main()


__all__ = [
    "evaluate_submission",
    "materialize_case",
    "next_session",
]
