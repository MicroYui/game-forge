"""Endless Sky composition and CLI for the frozen QA-hours protocol."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
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
from gameforge.bench.qa.contracts import (
    QaSessionSpec,
    canonical_session_bytes,
    content_sha256,
    load_session,
    seal_qa_verdict,
)
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
from gameforge.bench.qa.score import (
    QaEvidenceManifest,
    load_evidence as load_qa_evidence,
    seal_qa_evidence,
    validate_qa_evidence,
    write_evidence as write_qa_evidence,
)
from gameforge.bench.qa.session import (
    frozen_submission_root,
    load_state,
    transition_session,
)
from gameforge.contracts.canonical import canonical_json

_ROOT = Path("scenarios/external_cases/endless_sky")
_EXTERNAL_PATH = _ROOT / "external-corpus-manifest.json"
_HED_PATH = _ROOT / "hed-evidence.json"
_PROTOCOL_PATH = _ROOT / "qa-protocol.json"
_NATIVE_SOURCE = _ROOT / "native/endless_sky_data_parser.cpp"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _frozen_inputs(
    protocol_path: str | Path = _PROTOCOL_PATH,
) -> tuple[
    ExternalCorpusManifest,
    HedEvidenceManifest,
    QaProtocol,
]:
    external = load_manifest(_EXTERNAL_PATH)
    hed = load_evidence(_HED_PATH)
    protocol = load_protocol(protocol_path)
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
    protocol_path: str | Path = _PROTOCOL_PATH,
) -> Path:
    active_external, active_hed, active_protocol = _frozen_inputs(protocol_path)
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
                assisted.patch.model_dump(mode="json") if assisted.patch is not None else None
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


def _bound_workspace(value: str | Path, protocol: QaProtocol) -> Path:
    workspace = _workspace_root(value)
    marker = workspace / ".qa-workspace.json"
    expected = (
        canonical_json(
            {
                "schema_version": "qa-workspace@1",
                "participant_id": protocol.participant_id,
                "protocol_sha256": protocol.protocol_sha256,
            }
        )
        + "\n"
    ).encode("utf-8")
    if marker.is_file():
        if marker.read_bytes() != expected:
            raise ValueError("QA workspace protocol marker is not canonical or does not match")
        unexpected = {
            path.name for path in workspace.iterdir() if path.name not in {marker.name, "sessions"}
        }
        if unexpected:
            raise ValueError("QA workspace contains files outside its bound protocol")
        return workspace
    if any(workspace.iterdir()):
        raise ValueError("nonempty QA workspace is not bound to a frozen protocol")
    temporary = marker.with_suffix(".tmp")
    temporary.write_bytes(expected)
    os.replace(temporary, marker)
    return workspace


def next_session(
    workspace: str | Path,
    *,
    protocol_path: str | Path = _PROTOCOL_PATH,
) -> Path:
    external, hed, protocol = _frozen_inputs(protocol_path)
    root = _bound_workspace(workspace, protocol)
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
            protocol_path=protocol_path,
        )
    raise ValueError("all frozen QA sessions are finished")


def _session_bundle(
    workspace: str | Path,
    session_id: str,
    *,
    protocol_path: str | Path = _PROTOCOL_PATH,
):
    external, _, protocol = _frozen_inputs(protocol_path)
    root = _bound_workspace(workspace, protocol)
    matches = [item for item in protocol.sessions if item.session_id == session_id]
    if len(matches) != 1:
        raise ValueError(f"unknown QA session ID: {session_id}")
    bundle = root / "sessions" / session_id
    if not bundle.is_dir():
        raise ValueError("QA session has not been materialized by next")
    return external, protocol, matches[0], bundle


def _status(
    workspace: str | Path,
    *,
    protocol_path: str | Path = _PROTOCOL_PATH,
) -> dict[str, object]:
    _, _, protocol = _frozen_inputs(protocol_path)
    root = _bound_workspace(workspace, protocol)
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


def _validate_workspace_session(
    bundle: Path,
    spec: QaSessionSpec,
    protocol: QaProtocol,
    external: ExternalCorpusManifest,
):
    evidence_path = bundle / "session-evidence.json"
    patch_path = bundle / "final.patch"
    if not evidence_path.is_file() or not patch_path.is_file():
        raise ValueError(f"missing QA session evidence for {spec.session_id}")
    session = load_session(evidence_path)
    if (
        session.protocol_sha256 != protocol.protocol_sha256
        or session.session_id != spec.session_id
        or session.participant_id != protocol.participant_id
        or session.case_id != spec.case_id
        or session.pair_id != spec.pair_id
        or session.arm != spec.arm
        or session.order != spec.order
    ):
        raise ValueError(f"QA session differs from protocol for {spec.session_id}")
    expected_patch_path = f"qa-patches/{spec.session_id}.patch"
    if session.final_patch_path != expected_patch_path:
        raise ValueError(f"QA final patch path mismatch for {spec.session_id}")
    patch = patch_path.read_bytes()
    if hashlib.sha256(patch).hexdigest() != session.final_patch_sha256:
        raise ValueError(f"QA final patch hash mismatch for {spec.session_id}")
    state = load_state(bundle)
    if (
        state.status != "finished"
        or state.protocol_sha256 != protocol.protocol_sha256
        or state.session_id != spec.session_id
        or state.pair_id != spec.pair_id
        or state.arm != spec.arm
        or state.order != spec.order
        or state.events != session.events
        or state.participant_attested_no_contamination
        != session.participant_attested_no_contamination
    ):
        raise ValueError(f"QA frozen session state mismatch for {spec.session_id}")
    submission_root = frozen_submission_root(bundle, state)
    rebuilt_patch, verdict = evaluate_submission(
        spec.case_id,
        submission_root,
        external=external,
    )
    if rebuilt_patch != patch:
        raise ValueError(f"QA final patch does not match frozen submission for {spec.session_id}")
    if content_sha256(verdict) != content_sha256(session.verdict):
        raise ValueError(f"QA correctness verdict mismatch for {spec.session_id}")
    return session, patch


def import_workspace_evidence(
    workspace: str | Path,
    output: str | Path,
    *,
    protocol_path: str | Path = _PROTOCOL_PATH,
) -> QaEvidenceManifest:
    external, _, protocol = _frozen_inputs(protocol_path)
    root = _bound_workspace(workspace, protocol)
    sessions_root = root / "sessions"
    validated = []
    for spec in protocol.sessions:
        bundle = sessions_root / spec.session_id
        if not bundle.is_dir():
            raise ValueError(f"missing QA session evidence for {spec.session_id}")
        validated.append(
            (
                spec,
                *_validate_workspace_session(bundle, spec, protocol, external),
            )
        )

    artifact_root = Path(output)
    session_output = artifact_root / "qa-sessions"
    patch_output = artifact_root / "qa-patches"
    evidence_output = artifact_root / "qa-evidence.json"
    if session_output.exists() or patch_output.exists() or evidence_output.exists():
        raise ValueError("QA evidence output already exists")
    session_output.mkdir(parents=True)
    patch_output.mkdir()
    for spec, session, patch in validated:
        (session_output / f"{spec.session_id}.json").write_bytes(canonical_session_bytes(session))
        (patch_output / f"{spec.session_id}.patch").write_bytes(patch)
    evidence = seal_qa_evidence(
        protocol,
        tuple(item[1] for item in validated),
    )
    write_qa_evidence(evidence_output, evidence)
    validate_qa_evidence(evidence, protocol, artifact_root)
    return evidence


def _reconstruct_submission(
    runtime,
    patch: bytes,
) -> dict[str, bytes]:  # noqa: ANN001 - source-specific runtime boundary
    with tempfile.TemporaryDirectory(prefix="gameforge-qa-replay-") as temporary:
        root = Path(temporary)
        for relative, raw in runtime.before_raw.items():
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        if patch:
            patch_path = root / "submission.patch"
            patch_path.write_bytes(patch)
            completed = subprocess.run(
                ["git", "apply", "--unsafe-paths", str(patch_path)],
                cwd=root,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", errors="replace")
                raise ValueError(f"QA final patch cannot be replayed: {detail}")
        return {relative: (root / relative).read_bytes() for relative in runtime.spec.changed_paths}


def validate_imported_evidence(
    path: str | Path,
    *,
    protocol_path: str | Path = _PROTOCOL_PATH,
) -> QaEvidenceManifest:
    evidence_path = Path(path)
    artifact_root = evidence_path.parent
    external, _, protocol = _frozen_inputs(protocol_path)
    evidence = load_qa_evidence(evidence_path)
    validate_qa_evidence(evidence, protocol, artifact_root)
    sessions_by_id = {item.session_id: item for item in evidence.sessions}
    with tempfile.TemporaryDirectory(prefix="gameforge-qa-validator-") as temporary:
        native = compile_native_parser(_NATIVE_SOURCE, Path(temporary) / "native")
        for spec in protocol.sessions:
            session_file = artifact_root / "qa-sessions" / f"{spec.session_id}.json"
            stored_session = load_session(session_file)
            if stored_session != sessions_by_id.get(spec.session_id):
                raise ValueError(f"QA stored session mismatch for {spec.session_id}")
            patch_path = artifact_root / stored_session.final_patch_path
            patch = patch_path.read_bytes()
            case = _case_evidence(external, spec.case_id)
            runtime = load_case_runtime(_ROOT, case.spec)
            submitted = _reconstruct_submission(runtime, patch)
            if unified_submission_patch(runtime.before_raw, submitted) != patch:
                raise ValueError(f"QA patch is not canonical for {spec.session_id}")
            verdict = seal_qa_verdict(
                validate_submitted_tree(
                    runtime,
                    submitted,
                    native_binary=native,
                )
            )
            if content_sha256(verdict) != content_sha256(stored_session.verdict):
                raise ValueError(f"QA verdict does not rederive for {spec.session_id}")
    return evidence


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("next", "status"):
        child = subparsers.add_parser(command)
        child.add_argument("--workspace", type=Path, required=True)
        child.add_argument("--protocol", type=Path, default=_PROTOCOL_PATH)
    for command in ("start", "pause", "resume"):
        child = subparsers.add_parser(command)
        child.add_argument("--workspace", type=Path, required=True)
        child.add_argument("--session", required=True)
        child.add_argument("--protocol", type=Path, default=_PROTOCOL_PATH)
    finish = subparsers.add_parser("finish")
    finish.add_argument("--workspace", type=Path, required=True)
    finish.add_argument("--session", required=True)
    finish.add_argument("--protocol", type=Path, default=_PROTOCOL_PATH)
    finish.add_argument("--attest-no-contamination", action="store_true")
    importer = subparsers.add_parser("import-evidence")
    importer.add_argument("--workspace", type=Path, required=True)
    importer.add_argument("--output", type=Path, required=True)
    importer.add_argument("--protocol", type=Path, default=_PROTOCOL_PATH)
    validator = subparsers.add_parser("validate-evidence")
    validator.add_argument("evidence", type=Path)
    validator.add_argument("--protocol", type=Path, default=_PROTOCOL_PATH)
    server = subparsers.add_parser("serve")
    server.add_argument("--workspace", type=Path, required=True)
    server.add_argument("--protocol", type=Path, default=_PROTOCOL_PATH)
    server.add_argument("--port", type=int, default=4187)
    args = parser.parse_args()

    if args.command == "next":
        bundle = next_session(args.workspace, protocol_path=args.protocol)
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
        print(canonical_json(_status(args.workspace, protocol_path=args.protocol)))
        return
    if args.command == "import-evidence":
        evidence = import_workspace_evidence(
            args.workspace,
            args.output,
            protocol_path=args.protocol,
        )
        print(evidence.evidence_sha256)
        return
    if args.command == "validate-evidence":
        evidence = validate_imported_evidence(
            args.evidence,
            protocol_path=args.protocol,
        )
        print(evidence.evidence_sha256)
        return
    if args.command == "serve":
        import uvicorn

        from gameforge.bench.external_cases.endless_sky_qa_runner import (
            create_runner_app,
        )

        uvicorn.run(
            create_runner_app(args.workspace, protocol_path=args.protocol),
            host="127.0.0.1",
            port=args.port,
            log_level="warning",
        )
        return

    external, protocol, session, bundle = _session_bundle(
        args.workspace,
        args.session,
        protocol_path=args.protocol,
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
    "import_workspace_evidence",
    "materialize_case",
    "next_session",
    "validate_imported_evidence",
]
