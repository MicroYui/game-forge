from __future__ import annotations

import os

import pytest

import gameforge.bench.qa.session as session_module
from gameforge.bench.qa.protocol import load_protocol
from gameforge.bench.qa.session import (
    freeze_session_submission,
    initialize_session,
    load_state,
    transition_session,
)

_PROTOCOL = "scenarios/external_cases/endless_sky/qa-protocol.json"


class _Clock:
    def __init__(self, *values: int) -> None:
        self._values = iter(values)

    def __call__(self) -> int:
        return next(self._values)


def _prepared(tmp_path):
    protocol = load_protocol(_PROTOCOL)
    spec = protocol.sessions[0]
    bundle = tmp_path / spec.session_id
    (bundle / "work").mkdir(parents=True)
    (bundle / "work/example.txt").write_bytes(b"frozen submission\n")
    return protocol, spec, bundle, initialize_session(bundle, protocol, spec)


def test_timer_runs_start_pause_resume_finish_with_injected_clock(tmp_path):
    _, _, bundle, prepared = _prepared(tmp_path)
    assert prepared.status == "prepared"

    running = transition_session(bundle, "start", clock=_Clock(100))
    paused = transition_session(bundle, "pause", clock=_Clock(200))
    resumed = transition_session(bundle, "resume", clock=_Clock(400))
    finished = freeze_session_submission(bundle, clock=_Clock(700))

    assert running.status == "running"
    assert paused.status == "paused"
    assert resumed.status == "running"
    assert finished.status == "finished"
    assert [item.kind for item in finished.events] == [
        "start",
        "pause",
        "resume",
        "finish",
    ]
    assert load_state(bundle) == finished
    assert not (bundle / "work").exists()
    assert (bundle / finished.submission.directory / "example.txt").read_bytes() == (
        b"frozen submission\n"
    )


@pytest.mark.parametrize(
    "first, second",
    [
        ("pause", None),
        ("resume", None),
        ("finish", None),
        ("start", "start"),
        ("start", "resume"),
    ],
)
def test_timer_rejects_invalid_or_duplicate_transitions(tmp_path, first, second):
    _, _, bundle, _ = _prepared(tmp_path)
    clock = _Clock(100, 200)
    if first == "start":
        transition_session(bundle, first, clock=clock)
        invalid = second
    else:
        invalid = first

    with pytest.raises(ValueError, match="transition"):
        transition_session(bundle, invalid, clock=clock)


def test_timer_rejects_non_increasing_clock(tmp_path):
    _, _, bundle, _ = _prepared(tmp_path)
    transition_session(bundle, "start", clock=_Clock(200))

    with pytest.raises(ValueError, match="increasing"):
        transition_session(bundle, "pause", clock=_Clock(200))


def test_state_updates_use_atomic_replace(tmp_path, monkeypatch):
    _, _, bundle, _ = _prepared(tmp_path)
    calls: list[tuple[object, object]] = []
    real_replace = session_module.os.replace

    def recording_replace(source, destination):  # noqa: ANN001
        calls.append((source, destination))
        return real_replace(source, destination)

    monkeypatch.setattr(session_module.os, "replace", recording_replace)

    transition_session(bundle, "start", clock=_Clock(100))

    assert calls
    assert calls[-1][1] == bundle / "session-state.json"
    assert not (bundle / "session-state.json.tmp").exists()


def test_finish_rename_survives_state_write_failure_without_retiming(
    tmp_path,
    monkeypatch,
):
    _, _, bundle, _ = _prepared(tmp_path)
    transition_session(bundle, "start", clock=_Clock(100))
    real_atomic_write = session_module._atomic_write
    failed = False

    def fail_finished_state(path, raw):  # noqa: ANN001
        nonlocal failed
        if path.name == "session-state.json" and not failed:
            failed = True
            raise OSError("state disk unavailable")
        return real_atomic_write(path, raw)

    monkeypatch.setattr(session_module, "_atomic_write", fail_finished_state)
    with pytest.raises(OSError, match="state disk unavailable"):
        freeze_session_submission(bundle, clock=_Clock(200))

    assert not (bundle / "work").exists()
    frozen = tuple(bundle.glob(".qa-frozen-submission-*"))
    assert len(frozen) == 1
    assert frozen[0].name.endswith("200")
    assert load_state(bundle).status == "running"
    with pytest.raises(ValueError, match="already frozen"):
        transition_session(bundle, "pause", clock=_Clock(300))

    recovered = freeze_session_submission(bundle, clock=_Clock())

    assert recovered.status == "finished"
    assert recovered.events[-1].monotonic_ns == 200
    assert recovered.submission.directory == frozen[0].name
    assert (frozen[0] / "example.txt").read_bytes() == b"frozen submission\n"


def test_open_editor_file_descriptor_cannot_mutate_frozen_submission(tmp_path):
    _, _, bundle, _ = _prepared(tmp_path)
    transition_session(bundle, "start", clock=_Clock(100))
    editor_file = (bundle / "work/example.txt").open("r+b", buffering=0)
    try:
        frozen = freeze_session_submission(bundle, clock=_Clock(200))
        editor_file.seek(0)
        editor_file.write(b"late editor bytes\n")
        editor_file.truncate()
        os.fsync(editor_file.fileno())
    finally:
        editor_file.close()

    submission = bundle / frozen.submission.directory
    assert (submission / "example.txt").read_bytes() == b"frozen submission\n"
    assert freeze_session_submission(bundle, clock=_Clock()) == frozen


def test_submission_copy_failure_recovers_same_finish_time(tmp_path, monkeypatch):
    _, _, bundle, _ = _prepared(tmp_path)
    transition_session(bundle, "start", clock=_Clock(100))
    real_copytree = session_module.shutil.copytree
    failed = False

    def fail_once(source, destination):  # noqa: ANN001
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("submission copy unavailable")
        return real_copytree(source, destination)

    monkeypatch.setattr(session_module.shutil, "copytree", fail_once)
    with pytest.raises(OSError, match="submission copy unavailable"):
        freeze_session_submission(bundle, clock=_Clock(200))

    assert not (bundle / "work").exists()
    assert len(tuple(bundle.glob(".qa-submission-capture-200"))) == 1
    assert (bundle / ".qa-finish-manifest-200.json").is_file()
    assert load_state(bundle).status == "running"

    recovered = freeze_session_submission(bundle, clock=_Clock())

    assert recovered.status == "finished"
    assert recovered.events[-1].monotonic_ns == 200
    assert (bundle / recovered.submission.directory / "example.txt").read_bytes() == (
        b"frozen submission\n"
    )
    assert not (bundle / ".qa-finish-manifest-200.json").exists()


def test_copy_failure_recovery_rejects_late_open_descriptor_write(
    tmp_path,
    monkeypatch,
):
    _, _, bundle, _ = _prepared(tmp_path)
    transition_session(bundle, "start", clock=_Clock(100))
    editor_file = (bundle / "work/example.txt").open("r+b", buffering=0)

    def fail_copy(_source, _destination):  # noqa: ANN001
        raise OSError("submission copy unavailable")

    monkeypatch.setattr(session_module.shutil, "copytree", fail_copy)
    try:
        with pytest.raises(OSError, match="submission copy unavailable"):
            freeze_session_submission(bundle, clock=_Clock(200))
        editor_file.seek(0)
        editor_file.write(b"late editor bytes\n")
        editor_file.truncate()
        os.fsync(editor_file.fileno())
    finally:
        editor_file.close()

    with pytest.raises(ValueError, match="changed after finish"):
        freeze_session_submission(bundle, clock=_Clock())

    assert load_state(bundle).status == "running"
    assert not tuple(bundle.glob(".qa-frozen-submission-*"))
    assert (bundle / ".qa-finish-manifest-200.json").is_file()
