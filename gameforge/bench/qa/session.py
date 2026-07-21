"""Atomic monotonic timer state for one QA session at a time."""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from gameforge.bench.hed.contracts import content_sha256
from gameforge.bench.qa.contracts import QaEvent, QaSessionSpec
from gameforge.bench.qa.protocol import QaProtocol
from gameforge.contracts.canonical import canonical_json

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
StableId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$",
    ),
]
Clock = Callable[[], int]
STATE_NAME = "session-state.json"
FROZEN_SUBMISSION_PREFIX = ".qa-frozen-submission-"
_CAPTURE_PREFIX = ".qa-submission-capture-"
_STAGING_PREFIX = ".qa-submission-staging-"
_CAPTURE_MANIFEST_PREFIX = ".qa-finish-manifest-"


def _normalized_relative(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise ValueError("QA submission paths must be normalized relative POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts or str(path) != value:
        raise ValueError("QA submission paths must be normalized relative POSIX paths")
    return value


class QaSubmissionFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    size_bytes: int = Field(ge=0)
    sha256: Sha256

    @model_validator(mode="after")
    def validate_path(self) -> QaSubmissionFile:
        _normalized_relative(self.path)
        return self


class QaSubmissionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["qa-submission-snapshot@1"] = "qa-submission-snapshot@1"
    directory: str
    files: tuple[QaSubmissionFile, ...]
    snapshot_sha256: Sha256

    @classmethod
    def seal(
        cls,
        *,
        directory: str,
        files: tuple[QaSubmissionFile, ...],
    ) -> QaSubmissionSnapshot:
        payload = {
            "schema_version": "qa-submission-snapshot@1",
            "directory": directory,
            "files": files,
        }
        return cls.model_validate({**payload, "snapshot_sha256": content_sha256(payload)})

    @model_validator(mode="after")
    def validate_snapshot(self) -> QaSubmissionSnapshot:
        suffix = self.directory.removeprefix(FROZEN_SUBMISSION_PREFIX)
        if not self.directory.startswith(FROZEN_SUBMISSION_PREFIX) or not suffix.isdigit():
            raise ValueError("QA frozen submission directory is invalid")
        paths = tuple(item.path for item in self.files)
        if not paths or paths != tuple(sorted(set(paths))):
            raise ValueError("QA submission files must be nonempty, unique, and sorted")
        expected_hash = content_sha256(self, exclude={"snapshot_sha256"})
        if self.snapshot_sha256 != expected_hash:
            raise ValueError("snapshot_sha256 does not bind the frozen QA submission")
        return self


class QaSessionState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["qa-session-state@2"] = "qa-session-state@2"
    protocol_sha256: Sha256
    session_id: StableId
    pair_id: StableId
    arm: Literal["manual", "assisted"]
    order: int
    status: Literal["prepared", "running", "paused", "finished"]
    events: tuple[QaEvent, ...]
    submission: QaSubmissionSnapshot | None = None
    participant_attested_no_contamination: bool | None = None
    state_sha256: Sha256

    @classmethod
    def seal(cls, **values: Any) -> QaSessionState:
        payload = dict(values)
        payload.pop("state_sha256", None)
        payload.setdefault("schema_version", "qa-session-state@2")
        payload["state_sha256"] = content_sha256(payload)
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def validate_state(self) -> QaSessionState:
        expected_status = _status_for_events(self.events)
        if self.status != expected_status:
            raise ValueError("QA timer status does not match its events")
        if self.status == "finished":
            if self.submission is None:
                raise ValueError("finished QA state requires a frozen submission")
            finish_ns = self.events[-1].monotonic_ns
            expected_directory = f"{FROZEN_SUBMISSION_PREFIX}{finish_ns}"
            if self.submission.directory != expected_directory:
                raise ValueError("QA submission directory differs from frozen finish time")
        elif self.submission is not None or self.participant_attested_no_contamination is not None:
            raise ValueError("unfinished QA state cannot carry finalization data")
        expected_hash = content_sha256(self, exclude={"state_sha256"})
        if self.state_sha256 != expected_hash:
            raise ValueError("state_sha256 does not bind QA timer state")
        return self


def _status_for_events(events: tuple[QaEvent, ...]) -> str:
    if not events:
        return "prepared"
    if events[0].kind != "start":
        raise ValueError("QA timer events must begin with start")
    state = "running"
    previous = events[0].monotonic_ns
    for event in events[1:]:
        if event.monotonic_ns <= previous:
            raise ValueError("QA timer monotonic values must be strictly increasing")
        previous = event.monotonic_ns
        if state == "running" and event.kind == "pause":
            state = "paused"
        elif state == "paused" and event.kind == "resume":
            state = "running"
        elif state == "running" and event.kind == "finish":
            state = "finished"
        else:
            raise ValueError(f"invalid QA timer transition: {state} -> {event.kind}")
    return state


def canonical_state_bytes(state: QaSessionState) -> bytes:
    return (canonical_json(state.model_dump(mode="json")) + "\n").encode("utf-8")


def _atomic_write(path: Path, raw: bytes) -> None:
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_bytes(raw)
    os.replace(temporary, path)


def _read_clock(clock: Clock, previous_ns: int | None) -> int:
    now = clock()
    if type(now) is not int or now < 0:
        raise ValueError("QA monotonic clock must return a nonnegative integer")
    if previous_ns is not None and now <= previous_ns:
        raise ValueError("QA timer monotonic values must be strictly increasing")
    return now


def _submission_files(path: Path) -> tuple[QaSubmissionFile, ...]:
    if not path.is_dir() or path.is_symlink():
        raise ValueError("QA submission directory is missing or invalid")
    files: list[QaSubmissionFile] = []
    for candidate in path.rglob("*"):
        if candidate.is_symlink():
            raise ValueError("QA frozen submission must not contain symlinks")
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise ValueError("QA frozen submission contains a non-regular file")
        relative = _normalized_relative(candidate.relative_to(path).as_posix())
        raw = candidate.read_bytes()
        files.append(
            QaSubmissionFile(
                path=relative,
                size_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        )
    result = tuple(sorted(files, key=lambda item: item.path))
    if not result:
        raise ValueError("QA submission must contain at least one file")
    return result


def _snapshot_directory(path: Path) -> QaSubmissionSnapshot:
    return QaSubmissionSnapshot.seal(
        directory=path.name,
        files=_submission_files(path),
    )


def _discover_submission_directory(bundle: Path, prefix: str) -> Path | None:
    matches = tuple(sorted(bundle.glob(f"{prefix}*")))
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("QA bundle contains multiple submission freeze directories")
    path = matches[0]
    suffix = path.name.removeprefix(prefix)
    if not suffix.isdigit() or not path.is_dir() or path.is_symlink():
        raise ValueError("QA bundle submission freeze directory is invalid")
    return path


def _discover_frozen_submission(bundle: Path) -> Path | None:
    return _discover_submission_directory(bundle, FROZEN_SUBMISSION_PREFIX)


def _discover_submission_capture(bundle: Path) -> Path | None:
    return _discover_submission_directory(bundle, _CAPTURE_PREFIX)


def _finish_ns_from(path: Path, prefix: str) -> int:
    return int(path.name.removeprefix(prefix))


def _canonical_snapshot_bytes(snapshot: QaSubmissionSnapshot) -> bytes:
    return (canonical_json(snapshot.model_dump(mode="json")) + "\n").encode("utf-8")


def _capture_manifest_path(bundle: Path, finish_ns: int) -> Path:
    return bundle / f"{_CAPTURE_MANIFEST_PREFIX}{finish_ns}.json"


def _load_capture_manifest(
    bundle: Path,
    capture: Path | None,
    finish_ns: int,
    *,
    create: bool,
) -> tuple[Path, QaSubmissionSnapshot]:
    path = _capture_manifest_path(bundle, finish_ns)
    expected_directory = f"{FROZEN_SUBMISSION_PREFIX}{finish_ns}"
    if path.exists():
        raw = path.read_bytes()
        manifest = QaSubmissionSnapshot.model_validate_json(raw)
        if _canonical_snapshot_bytes(manifest) != raw:
            raise ValueError("QA capture manifest is not canonical JSON")
    else:
        if not create or capture is None:
            raise ValueError("QA capture manifest is missing; finish recovery is unsafe")
        manifest = QaSubmissionSnapshot.seal(
            directory=expected_directory,
            files=_submission_files(capture),
        )
        _atomic_write(path, _canonical_snapshot_bytes(manifest))
    if manifest.directory != expected_directory:
        raise ValueError("QA capture manifest differs from frozen finish time")
    if capture is not None and _submission_files(capture) != manifest.files:
        raise ValueError("QA submission changed after finish")
    return path, manifest


def _publish_frozen_submission(
    bundle: Path,
    capture: Path,
    finish_ns: int,
    manifest: QaSubmissionSnapshot,
) -> Path:
    final = bundle / f"{FROZEN_SUBMISSION_PREFIX}{finish_ns}"
    if final.exists():
        if not final.is_dir() or final.is_symlink():
            raise ValueError("QA frozen submission path is invalid")
        if _submission_files(final) != manifest.files:
            raise ValueError("QA frozen submission differs from its capture manifest")
        if capture.exists():
            shutil.rmtree(capture)
        return final

    staging = bundle / f"{_STAGING_PREFIX}{finish_ns}"
    if staging.exists():
        if not staging.is_dir() or staging.is_symlink():
            raise ValueError("QA submission staging path is invalid")
        shutil.rmtree(staging)
    if _submission_files(capture) != manifest.files:
        raise ValueError("QA submission changed after finish")
    shutil.copytree(capture, staging)
    if _submission_files(staging) != manifest.files:
        shutil.rmtree(staging)
        raise ValueError("QA submission changed while it was being frozen")
    os.replace(staging, final)
    shutil.rmtree(capture)
    return final


def frozen_submission_root(
    bundle: str | Path,
    state: QaSessionState | None = None,
) -> Path:
    root = Path(bundle)
    active = state or load_state(root)
    if active.status != "finished" or active.submission is None:
        raise ValueError("QA session has no frozen submission")
    submission = root / active.submission.directory
    if _snapshot_directory(submission) != active.submission:
        raise ValueError("QA frozen submission differs from its sealed manifest")
    return submission


def freeze_session_submission(
    bundle: str | Path,
    *,
    clock: Clock = time.monotonic_ns,
) -> QaSessionState:
    root = Path(bundle)
    state = load_state(root)
    if state.status == "finished":
        frozen_submission_root(root, state)
        return state
    if state.status != "running":
        raise ValueError(f"invalid QA timer transition: {state.status} -> finish")

    submission = _discover_frozen_submission(root)
    capture = _discover_submission_capture(root)
    capture_manifest_path: Path
    capture_manifest: QaSubmissionSnapshot
    if submission is not None:
        finish_ns = _finish_ns_from(submission, FROZEN_SUBMISSION_PREFIX)
        capture_manifest_path, capture_manifest = _load_capture_manifest(
            root,
            None,
            finish_ns,
            create=False,
        )
        if _submission_files(submission) != capture_manifest.files:
            raise ValueError("QA frozen submission differs from its capture manifest")
        if capture is not None:
            capture_ns = _finish_ns_from(capture, _CAPTURE_PREFIX)
            if capture_ns != finish_ns:
                raise ValueError("QA submission capture differs from frozen finish time")
            shutil.rmtree(capture)
    elif capture is not None:
        finish_ns = _finish_ns_from(capture, _CAPTURE_PREFIX)
        capture_manifest_path, capture_manifest = _load_capture_manifest(
            root,
            capture,
            finish_ns,
            create=False,
        )
        submission = _publish_frozen_submission(
            root,
            capture,
            finish_ns,
            capture_manifest,
        )
    else:
        previous_ns = state.events[-1].monotonic_ns if state.events else None
        finish_ns = _read_clock(clock, previous_ns)
        capture = root / f"{_CAPTURE_PREFIX}{finish_ns}"
        work = root / "work"
        if not work.is_dir() or work.is_symlink():
            raise ValueError("QA session work directory is missing or invalid")
        os.replace(work, capture)
        capture_manifest_path, capture_manifest = _load_capture_manifest(
            root,
            capture,
            finish_ns,
            create=True,
        )
        submission = _publish_frozen_submission(
            root,
            capture,
            finish_ns,
            capture_manifest,
        )

    previous_ns = state.events[-1].monotonic_ns if state.events else None
    if previous_ns is not None and finish_ns <= previous_ns:
        raise ValueError("QA frozen finish time is not strictly increasing")

    snapshot = _snapshot_directory(submission)
    events = (*state.events, QaEvent(kind="finish", monotonic_ns=finish_ns))
    updated = QaSessionState.seal(
        protocol_sha256=state.protocol_sha256,
        session_id=state.session_id,
        pair_id=state.pair_id,
        arm=state.arm,
        order=state.order,
        status="finished",
        events=events,
        submission=snapshot,
        participant_attested_no_contamination=None,
    )
    _atomic_write(root / STATE_NAME, canonical_state_bytes(updated))
    capture_manifest_path.unlink()
    return updated


def bind_session_attestation(
    bundle: str | Path,
    participant_attested_no_contamination: bool,
) -> QaSessionState:
    if type(participant_attested_no_contamination) is not bool:
        raise TypeError("QA contamination attestation must be a boolean")
    root = Path(bundle)
    state = load_state(root)
    if state.status != "finished" or state.submission is None:
        raise ValueError("QA contamination attestation requires a frozen submission")
    current = state.participant_attested_no_contamination
    if current is not None:
        if current != participant_attested_no_contamination:
            raise ValueError("QA contamination attestation is already frozen")
        return state
    updated = QaSessionState.seal(
        protocol_sha256=state.protocol_sha256,
        session_id=state.session_id,
        pair_id=state.pair_id,
        arm=state.arm,
        order=state.order,
        status=state.status,
        events=state.events,
        submission=state.submission,
        participant_attested_no_contamination=(participant_attested_no_contamination),
    )
    _atomic_write(root / STATE_NAME, canonical_state_bytes(updated))
    return updated


def load_state(bundle: str | Path) -> QaSessionState:
    path = Path(bundle) / STATE_NAME
    raw = path.read_bytes()
    state = QaSessionState.model_validate_json(raw)
    if canonical_state_bytes(state) != raw:
        raise ValueError("QA session state is not canonical JSON")
    return state


def initialize_session(
    bundle: str | Path,
    protocol: QaProtocol,
    session: QaSessionSpec,
) -> QaSessionState:
    if session not in protocol.sessions:
        raise ValueError("QA session is absent from the frozen protocol")
    destination = Path(bundle) / STATE_NAME
    if destination.exists():
        raise ValueError("QA session state already exists")
    state = QaSessionState.seal(
        protocol_sha256=protocol.protocol_sha256,
        session_id=session.session_id,
        pair_id=session.pair_id,
        arm=session.arm,
        order=session.order,
        status="prepared",
        events=(),
    )
    _atomic_write(destination, canonical_state_bytes(state))
    return state


_ALLOWED = {
    "prepared": {"start"},
    "running": {"pause", "finish"},
    "paused": {"resume"},
    "finished": set(),
}


def transition_session(
    bundle: str | Path,
    kind: Literal["start", "pause", "resume", "finish"],
    *,
    clock: Clock = time.monotonic_ns,
) -> QaSessionState:
    if kind == "finish":
        return freeze_session_submission(bundle, clock=clock)
    root = Path(bundle)
    path = root / STATE_NAME
    state = load_state(root)
    if (
        _discover_frozen_submission(root) is not None
        or _discover_submission_capture(root) is not None
    ):
        raise ValueError("QA submission is already frozen; finish recovery is required")
    if kind not in _ALLOWED[state.status]:
        raise ValueError(f"invalid QA timer transition: {state.status} -> {kind}")
    previous_ns = state.events[-1].monotonic_ns if state.events else None
    now = _read_clock(clock, previous_ns)
    events = (*state.events, QaEvent(kind=kind, monotonic_ns=now))
    updated = QaSessionState.seal(
        protocol_sha256=state.protocol_sha256,
        session_id=state.session_id,
        pair_id=state.pair_id,
        arm=state.arm,
        order=state.order,
        status=_status_for_events(events),
        events=events,
    )
    _atomic_write(path, canonical_state_bytes(updated))
    return updated


__all__ = [
    "FROZEN_SUBMISSION_PREFIX",
    "STATE_NAME",
    "QaSessionState",
    "QaSubmissionFile",
    "QaSubmissionSnapshot",
    "bind_session_attestation",
    "canonical_state_bytes",
    "freeze_session_submission",
    "frozen_submission_root",
    "initialize_session",
    "load_state",
    "transition_session",
]
