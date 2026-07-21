"""Game-neutral QA bundle, submission diff, and finish helpers."""

from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from gameforge.bench.qa.contracts import (
    QA_ACTIVE_CAP_NS,
    QaCorrectnessVerdict,
    QaSessionEvidence,
    QaSessionSpec,
    canonical_session_bytes,
    load_session,
)
from gameforge.bench.qa.protocol import QaProtocol
from gameforge.bench.qa.session import (
    QaSessionState,
    bind_session_attestation,
    freeze_session_submission,
    frozen_submission_root,
    initialize_session,
    load_state,
)
from gameforge.contracts.canonical import canonical_json


@dataclass(frozen=True)
class QaBundleMaterial:
    session: QaSessionSpec
    upstream_subject: str
    before_files: dict[str, bytes]
    native_tool: Path
    assistance: dict[str, object] | None


Evaluator = Callable[[Path], tuple[bytes, QaCorrectnessVerdict]]


def _normalized_relative(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise ValueError("changed_paths must be normalized relative POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts or str(path) != value:
        raise ValueError("changed_paths must be normalized relative POSIX paths")
    return value


def _write_canonical(path: Path, payload: object) -> None:
    path.write_bytes((canonical_json(payload) + "\n").encode("utf-8"))


def write_arm_bundle(
    protocol: QaProtocol,
    material: QaBundleMaterial,
    destination: str | Path,
) -> Path:
    session = material.session
    if session not in protocol.sessions:
        raise ValueError("QA bundle session is absent from the frozen protocol")
    if not material.upstream_subject.strip():
        raise ValueError("QA bundle requires a nonblank upstream subject")
    expected_assistance = session.arm == "assisted"
    if expected_assistance != (material.assistance is not None):
        raise ValueError("QA assistance payload does not match the frozen arm")
    if material.assistance is not None and set(material.assistance) != {
        "finding",
        "agent_patch",
        "passed_verification",
        "disposition",
    }:
        raise ValueError("QA assistance payload has an unexpected field")
    paths = tuple(sorted(material.before_files))
    if not paths or len(paths) != len(set(paths)):
        raise ValueError("QA bundle requires unique before files")
    for path in paths:
        _normalized_relative(path)
        if not isinstance(material.before_files[path], bytes):
            raise ValueError("QA before file values must be bytes")
    if not material.native_tool.is_file():
        raise ValueError("QA bundle native syntax tool is missing")

    bundle = Path(destination)
    if bundle.exists():
        raise ValueError("QA bundle destination already exists")
    (bundle / "work").mkdir(parents=True)
    (bundle / "tools").mkdir()
    for relative in paths:
        output = bundle / "work" / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(material.before_files[relative])
    syntax_checker = bundle / "tools" / "syntax-checker"
    shutil.copyfile(material.native_tool, syntax_checker)
    syntax_checker.chmod(0o755)

    _write_canonical(
        bundle / "TASK.json",
        {
            "schema_version": "qa-task@1",
            "session_id": session.session_id,
            "pair_id": session.pair_id,
            "case_ref": f"case-{session.order:02d}",
            "order": session.order,
            "arm": session.arm,
            "upstream_subject": material.upstream_subject,
            "changed_paths": paths,
            "active_cap_ns": QA_ACTIVE_CAP_NS,
            "syntax_check_argv": (
                "tools/syntax-checker",
                *(f"work/{path}" for path in paths),
            ),
        },
    )
    if material.assistance is not None:
        _write_canonical(bundle / "GAMEFORGE.json", material.assistance)
    initialize_session(bundle, protocol, session)
    return bundle


def read_exact_changed_paths(
    work_root: str | Path,
    changed_paths: tuple[str, ...],
) -> dict[str, bytes]:
    root = Path(work_root).resolve(strict=True)
    expected = tuple(sorted(_normalized_relative(path) for path in changed_paths))
    if not expected or len(expected) != len(set(expected)):
        raise ValueError("changed_paths must be nonempty and unique")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("QA submission must not contain symlinks")
    actual = tuple(
        sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())
    )
    if actual != expected:
        raise ValueError("QA submission files differ from exact changed_paths")
    return {relative: (root / relative).read_bytes() for relative in expected}


def unified_submission_patch(
    before: dict[str, bytes],
    submitted: dict[str, bytes],
) -> bytes:
    if set(before) != set(submitted):
        raise ValueError("before and submitted trees must contain identical paths")
    output: list[str] = []
    for path in sorted(before):
        _normalized_relative(path)
        old_lines = before[path].decode("utf-8", errors="surrogateescape").splitlines(keepends=True)
        new_lines = (
            submitted[path].decode("utf-8", errors="surrogateescape").splitlines(keepends=True)
        )
        diff = difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="\n",
        )
        for line in diff:
            if line.endswith("\n"):
                output.append(line)
                continue
            output.append(f"{line}\n")
            output.append("\\ No newline at end of file\n")
    return "".join(output).encode("utf-8", errors="surrogateescape")


def _atomic_write(path: Path, raw: bytes) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_bytes(raw)
    os.replace(temporary, path)


def _assert_state_matches_session(
    state: QaSessionState,
    protocol: QaProtocol,
    session: QaSessionSpec,
) -> None:
    if (
        state.protocol_sha256 != protocol.protocol_sha256
        or state.session_id != session.session_id
        or state.pair_id != session.pair_id
        or state.arm != session.arm
        or state.order != session.order
    ):
        raise ValueError("QA timer state differs from the frozen session")


def _validate_completed_evidence(
    evidence: QaSessionEvidence,
    protocol: QaProtocol,
    session: QaSessionSpec,
    state: QaSessionState,
    patch: bytes,
) -> None:
    if (
        evidence.protocol_sha256 != protocol.protocol_sha256
        or evidence.session_id != session.session_id
        or evidence.participant_id != protocol.participant_id
        or evidence.case_id != session.case_id
        or evidence.pair_id != session.pair_id
        or evidence.arm != session.arm
        or evidence.order != session.order
        or evidence.events != state.events
        or evidence.participant_attested_no_contamination
        != state.participant_attested_no_contamination
        or evidence.final_patch_path != f"qa-patches/{session.session_id}.patch"
        or evidence.final_patch_sha256 != hashlib.sha256(patch).hexdigest()
    ):
        raise ValueError("completed QA evidence differs from its frozen session")


def finalize_session(
    protocol: QaProtocol,
    session: QaSessionSpec,
    bundle: str | Path,
    *,
    evaluator: Evaluator,
    participant_attested_no_contamination: bool,
    clock: Callable[[], int] = time.monotonic_ns,
) -> QaSessionEvidence:
    root = Path(bundle)
    evidence_path = root / "session-evidence.json"
    patch_path = root / "final.patch"
    state = load_state(root)
    _assert_state_matches_session(state, protocol, session)

    if evidence_path.exists():
        if not patch_path.is_file():
            raise ValueError("completed QA evidence is missing its final patch")
        state = bind_session_attestation(
            root,
            participant_attested_no_contamination,
        )
        frozen_submission_root(root, state)
        evidence = load_session(evidence_path)
        patch = patch_path.read_bytes()
        _validate_completed_evidence(evidence, protocol, session, state, patch)
        return evidence

    state = freeze_session_submission(root, clock=clock)
    _assert_state_matches_session(state, protocol, session)
    state = bind_session_attestation(
        root,
        participant_attested_no_contamination,
    )
    submission_root = frozen_submission_root(root, state)
    patch, verdict = evaluator(submission_root)
    if not isinstance(patch, bytes) or not isinstance(verdict, QaCorrectnessVerdict):
        raise TypeError("QA evaluator must return patch bytes and a correctness verdict")
    if patch_path.exists():
        if not patch_path.is_file() or patch_path.read_bytes() != patch:
            raise ValueError("QA final patch differs from the frozen submission")
    else:
        _atomic_write(patch_path, patch)
    evidence = QaSessionEvidence.seal(
        protocol_sha256=protocol.protocol_sha256,
        session_id=session.session_id,
        participant_id=protocol.participant_id,
        case_id=session.case_id,
        pair_id=session.pair_id,
        arm=session.arm,
        order=session.order,
        events=state.events,
        final_patch_path=f"qa-patches/{session.session_id}.patch",
        final_patch_sha256=hashlib.sha256(patch).hexdigest(),
        participant_attested_no_contamination=participant_attested_no_contamination,
        verdict=verdict,
    )
    evidence_bytes = canonical_session_bytes(evidence)
    if evidence_path.exists():
        if not evidence_path.is_file() or evidence_path.read_bytes() != evidence_bytes:
            raise ValueError("QA session evidence differs from the frozen submission")
    else:
        _atomic_write(evidence_path, evidence_bytes)
    return evidence


__all__ = [
    "QaBundleMaterial",
    "finalize_session",
    "read_exact_changed_paths",
    "unified_submission_patch",
    "write_arm_bundle",
]
