"""Atomic monotonic timer state for one QA session at a time."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

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


class QaSessionState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["qa-session-state@1"] = "qa-session-state@1"
    protocol_sha256: Sha256
    session_id: StableId
    pair_id: StableId
    arm: Literal["manual", "assisted"]
    order: int
    status: Literal["prepared", "running", "paused", "finished"]
    events: tuple[QaEvent, ...]
    state_sha256: Sha256

    @classmethod
    def seal(cls, **values: Any) -> QaSessionState:
        payload = dict(values)
        payload.pop("state_sha256", None)
        payload.setdefault("schema_version", "qa-session-state@1")
        payload["state_sha256"] = content_sha256(payload)
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def validate_state(self) -> QaSessionState:
        expected_status = _status_for_events(self.events)
        if self.status != expected_status:
            raise ValueError("QA timer status does not match its events")
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
    path = Path(bundle) / STATE_NAME
    state = load_state(bundle)
    if kind not in _ALLOWED[state.status]:
        raise ValueError(f"invalid QA timer transition: {state.status} -> {kind}")
    now = clock()
    if type(now) is not int or now < 0:
        raise ValueError("QA monotonic clock must return a nonnegative integer")
    if state.events and now <= state.events[-1].monotonic_ns:
        raise ValueError("QA timer monotonic values must be strictly increasing")
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
    "STATE_NAME",
    "QaSessionState",
    "canonical_state_bytes",
    "initialize_session",
    "load_state",
    "transition_session",
]
