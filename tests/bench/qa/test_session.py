from __future__ import annotations

import pytest

import gameforge.bench.qa.session as session_module
from gameforge.bench.qa.protocol import load_protocol
from gameforge.bench.qa.session import (
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
    bundle.mkdir()
    return protocol, spec, bundle, initialize_session(bundle, protocol, spec)


def test_timer_runs_start_pause_resume_finish_with_injected_clock(tmp_path):
    _, _, bundle, prepared = _prepared(tmp_path)
    assert prepared.status == "prepared"

    running = transition_session(bundle, "start", clock=_Clock(100))
    paused = transition_session(bundle, "pause", clock=_Clock(200))
    resumed = transition_session(bundle, "resume", clock=_Clock(400))
    finished = transition_session(bundle, "finish", clock=_Clock(700))

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
