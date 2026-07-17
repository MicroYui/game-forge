"""WebSocket ``WS /api/v1/runs/{id}/commands`` transport tests (M4c Task 15b).

Drives the durable command channel through the FastAPI ``TestClient`` WebSocket over the
DB-backed :class:`CommandAppHarness`. Client command frames are full ``RunCommandV1``
envelopes; server frames are ``RunCommandServerFrame`` (``RunCommandAckV1`` /
``RunCommandProblemV1``) — the browser NEVER sees ``RunCommandRecordV1`` lease/fencing
tokens. The channel authenticates the handshake, validates Origin (middleware), bounds
frame size + per-connection command budget, processes one frame at a time (backpressure),
and REAUTHORIZES the real Run on every message. All offline (no network, no LLM).
"""

from __future__ import annotations

from pathlib import Path
import threading

from fastapi.testclient import TestClient
import pytest
from starlette.concurrency import run_in_threadpool as actual_run_in_threadpool
from starlette.websockets import WebSocketDisconnect

import gameforge.apps.api.commands as command_transport
from gameforge.contracts.api import RunCommandServerFrame
from gameforge.contracts.jobs import RunCommandAckV1, RunCommandProblemV1
from pydantic import TypeAdapter
from tests.apps.api.run_command_testkit import (
    API_KEY,
    ORIGIN,
    SESSION_COOKIE,
    SESSION_TOKEN,
    CommandAppHarness,
    build_cancel_command,
    human_actor,
)

_FRAME = TypeAdapter(RunCommandServerFrame)
_WS_PATH = "/api/v1/runs/{run_id}/commands"
_ORIGIN_HEADERS = {"origin": ORIGIN}


def _command_frame(
    *,
    command_id: str,
    idempotency_key: str,
    expected_run_revision: int,
    client_id: str = "browser:a",
    client_seq: int = 1,
    reason_code: str = "user_requested",
) -> str:
    return build_cancel_command(
        command_id=command_id,
        client_id=client_id,
        client_seq=client_seq,
        idempotency_key=idempotency_key,
        expected_run_revision=expected_run_revision,
        reason_code=reason_code,
    ).model_dump_json()


def _session_client(harness: CommandAppHarness) -> TestClient:
    client = TestClient(harness.app, base_url=ORIGIN)
    client.cookies.set(SESSION_COOKIE, SESSION_TOKEN)
    return client


# ── handshake auth + happy cancel over the durable submit path ────────────────
def test_session_handshake_cancel_returns_ack_frame(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    with _session_client(harness) as client:
        with client.websocket_connect(
            _WS_PATH.format(run_id=run_id), headers=_ORIGIN_HEADERS
        ) as ws:
            ws.send_text(
                _command_frame(command_id="cmd:1", idempotency_key="k:1", expected_run_revision=1)
            )
            frame = ws.receive_json()
    ack = RunCommandAckV1.model_validate(frame)
    assert ack.status == "accepted"
    assert ack.persisted_status == "applied"
    assert ack.command_id == "cmd:1"
    # Durable: the Run reached terminal before the ACK was framed.
    assert harness.run_record(run_id).status == "cancelled"


def test_ws_authentication_and_submit_leave_the_event_loop_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    observed: list[tuple[str, int, int]] = []

    async def observing_threadpool(function, *args, **kwargs):  # type: ignore[no-untyped-def]
        event_loop_thread = threading.get_ident()

        def invoke():  # type: ignore[no-untyped-def]
            worker_thread = threading.get_ident()
            observed.append((function.__name__, event_loop_thread, worker_thread))
            return function(*args, **kwargs)

        return await actual_run_in_threadpool(invoke)

    monkeypatch.setattr(
        command_transport,
        "run_in_threadpool",
        observing_threadpool,
        raising=False,
    )

    with _session_client(harness) as client:
        with client.websocket_connect(
            _WS_PATH.format(run_id=run_id), headers=_ORIGIN_HEADERS
        ) as ws:
            ws.send_text(
                _command_frame(
                    command_id="cmd:threadpool",
                    idempotency_key="k:threadpool",
                    expected_run_revision=1,
                )
            )
            assert RunCommandAckV1.model_validate(ws.receive_json()).status == "accepted"

    calls = [name for name, _loop, _worker in observed]
    assert calls.count("_resolve_websocket_actor") >= 2  # handshake + current message
    assert calls.count("_submit_command") == 1
    assert all(event_loop != worker for _name, event_loop, worker in observed)


def test_api_key_handshake_cancel_returns_ack_frame(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    with TestClient(harness.app, base_url=ORIGIN) as client:
        with client.websocket_connect(
            _WS_PATH.format(run_id=run_id),
            headers={**_ORIGIN_HEADERS, "authorization": f"ApiKey {API_KEY}"},
        ) as ws:
            ws.send_text(
                _command_frame(
                    command_id="cmd:svc", idempotency_key="k:svc", expected_run_revision=1
                )
            )
            frame = ws.receive_json()
    assert RunCommandAckV1.model_validate(frame).status == "accepted"


def test_offered_subprotocols_negotiate_base_and_carry_csrf(tmp_path: Path) -> None:
    # A browser offers the command subprotocol plus a CSRF token subprotocol; the server
    # echoes ONLY the base command subprotocol (never one the client did not offer) and
    # threads the CSRF token into session resolution.
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    with _session_client(harness) as client:
        with client.websocket_connect(
            _WS_PATH.format(run_id=run_id),
            headers=_ORIGIN_HEADERS,
            subprotocols=["gameforge.run-commands.v1", "gameforge.csrf.tok-123"],
        ) as ws:
            assert ws.accepted_subprotocol == "gameforge.run-commands.v1"
            ws.send_text(
                _command_frame(command_id="cmd:1", idempotency_key="k:1", expected_run_revision=1)
            )
            assert RunCommandAckV1.model_validate(ws.receive_json()).status == "accepted"


# ── Origin + credential gating on the handshake ───────────────────────────────
def test_disallowed_origin_is_rejected_before_accept(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    with _session_client(harness) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                _WS_PATH.format(run_id=run_id),
                headers={"origin": "https://evil.example"},
            ):
                pass


def test_missing_credentials_close_the_handshake(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    with TestClient(harness.app, base_url=ORIGIN) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(_WS_PATH.format(run_id=run_id), headers=_ORIGIN_HEADERS):
                pass


# ── reauthorize the real Run on EVERY message ─────────────────────────────────
def test_revoked_session_between_messages_stops_the_channel(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_a = harness.admit_checker_run("a")
    run_b = harness.admit_checker_run("b")
    with _session_client(harness) as client:
        with client.websocket_connect(_WS_PATH.format(run_id=run_a), headers=_ORIGIN_HEADERS) as ws:
            ws.send_text(
                _command_frame(command_id="cmd:1", idempotency_key="k:1", expected_run_revision=1)
            )
            assert RunCommandAckV1.model_validate(ws.receive_json()).status == "accepted"
            # Session revoked mid-connection; the next message must re-resolve and fail.
            harness.revoke_session()
            ws.send_text(
                _command_frame(command_id="cmd:2", idempotency_key="k:2", expected_run_revision=1)
            )
            problem = RunCommandProblemV1.model_validate(ws.receive_json())
            assert problem.problem.status == 401
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
    # The second command's Run was never mutated (auth failed before submit).
    assert harness.run_record(run_b).status == "queued"


def test_revoked_role_between_messages_is_forbidden_and_closes(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    with _session_client(harness) as client:
        with client.websocket_connect(
            _WS_PATH.format(run_id=run_id), headers=_ORIGIN_HEADERS
        ) as ws:
            # First message succeeds; then the principal loses its tooling role.
            ws.send_text(
                _command_frame(command_id="cmd:1", idempotency_key="k:1", expected_run_revision=1)
            )
            assert RunCommandAckV1.model_validate(ws.receive_json()).status == "accepted"
            harness.set_session_actor(human_actor(authorized=False))
            ws.send_text(
                _command_frame(command_id="cmd:2", idempotency_key="k:2", expected_run_revision=2)
            )
            problem = RunCommandProblemV1.model_validate(ws.receive_json())
            assert problem.problem.status == 403
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()


# ── idempotency / conflict framing (channel stays open) ───────────────────────
def test_duplicate_exact_command_frame_replays_stable_ack(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    frame = _command_frame(command_id="cmd:d", idempotency_key="k:d", expected_run_revision=1)
    with _session_client(harness) as client:
        with client.websocket_connect(
            _WS_PATH.format(run_id=run_id), headers=_ORIGIN_HEADERS
        ) as ws:
            ws.send_text(frame)
            first = RunCommandAckV1.model_validate(ws.receive_json())
            ws.send_text(frame)  # exact resend on the same channel
            second = RunCommandAckV1.model_validate(ws.receive_json())
    assert first.status == "accepted"
    assert second.status == "duplicate"
    assert second.command_revision == first.command_revision
    assert second.run_revision == first.run_revision


def test_idempotency_conflict_frame_keeps_channel_open(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    with _session_client(harness) as client:
        with client.websocket_connect(
            _WS_PATH.format(run_id=run_id), headers=_ORIGIN_HEADERS
        ) as ws:
            ws.send_text(
                _command_frame(
                    command_id="cmd:a", idempotency_key="reused", expected_run_revision=1
                )
            )
            assert RunCommandAckV1.model_validate(ws.receive_json()).status == "accepted"
            # Same idempotency key, different command identity -> conflict frame.
            ws.send_text(
                _command_frame(
                    command_id="cmd:b",
                    client_seq=2,
                    idempotency_key="reused",
                    expected_run_revision=2,
                )
            )
            problem = RunCommandProblemV1.model_validate(ws.receive_json())
            assert problem.problem.status == 409
            assert problem.problem.code == "idempotency_conflict"
            assert problem.command_id == "cmd:b"
            # The channel is still usable: a valid duplicate of the first command replays.
            ws.send_text(
                _command_frame(
                    command_id="cmd:a", idempotency_key="reused", expected_run_revision=1
                )
            )
            assert RunCommandAckV1.model_validate(ws.receive_json()).status == "duplicate"


def test_malformed_frame_returns_problem_and_keeps_channel(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    with _session_client(harness) as client:
        with client.websocket_connect(
            _WS_PATH.format(run_id=run_id), headers=_ORIGIN_HEADERS
        ) as ws:
            ws.send_text("{not a command}")
            problem = RunCommandProblemV1.model_validate(ws.receive_json())
            assert problem.problem.code == "request_schema_invalid"
            assert problem.command_id is None
            # A well-formed command still works afterward.
            ws.send_text(
                _command_frame(command_id="cmd:ok", idempotency_key="k:ok", expected_run_revision=1)
            )
            assert RunCommandAckV1.model_validate(ws.receive_json()).status == "accepted"


# ── bounded frame size + per-connection command budget ────────────────────────
def test_oversized_frame_is_rejected_and_closes(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    with _session_client(harness) as client:
        with client.websocket_connect(
            _WS_PATH.format(run_id=run_id), headers=_ORIGIN_HEADERS
        ) as ws:
            ws.send_text("x" * 5000)  # exceeds the 4096-byte harness frame bound
            problem = RunCommandProblemV1.model_validate(ws.receive_json())
            assert problem.problem.status == 413
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()


def test_binary_frame_is_closed_with_unsupported_data(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    with _session_client(harness) as client:
        with client.websocket_connect(
            _WS_PATH.format(run_id=run_id), headers=_ORIGIN_HEADERS
        ) as ws:
            ws.send_bytes(b"{}")
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
    assert closed.value.code == 1003


def test_command_budget_bound_closes_after_limit(tmp_path: Path) -> None:
    from gameforge.apps.api.dependencies import RunCommandWebSocketConfig

    harness = CommandAppHarness(
        tmp_path,
        ws_config=RunCommandWebSocketConfig(max_frame_bytes=4096, max_commands_per_connection=1),
    )
    run_id = harness.admit_checker_run()
    with _session_client(harness) as client:
        with client.websocket_connect(
            _WS_PATH.format(run_id=run_id), headers=_ORIGIN_HEADERS
        ) as ws:
            ws.send_text(
                _command_frame(command_id="cmd:1", idempotency_key="k:1", expected_run_revision=1)
            )
            assert RunCommandAckV1.model_validate(ws.receive_json()).status == "accepted"
            # The 2nd command exceeds the per-connection budget -> problem + close.
            ws.send_text(
                _command_frame(command_id="cmd:2", idempotency_key="k:2", expected_run_revision=2)
            )
            RunCommandProblemV1.model_validate(ws.receive_json())
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()


# ── browser never receives lease/fencing tokens; frames are the server-frame union ─
def test_server_frames_are_lease_free_and_validate_as_server_frame(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    with _session_client(harness) as client:
        with client.websocket_connect(
            _WS_PATH.format(run_id=run_id), headers=_ORIGIN_HEADERS
        ) as ws:
            ws.send_text(
                _command_frame(command_id="cmd:1", idempotency_key="k:1", expected_run_revision=1)
            )
            frame = ws.receive_json()
    for leaked in ("claimed_fencing_token", "claimed_attempt_no", "claimed_at", "fencing", "lease"):
        assert leaked not in frame
    # The frame validates as the exact server-frame union (Ack | Problem), never a record.
    parsed = _FRAME.validate_python(frame)
    assert isinstance(parsed, RunCommandAckV1)


# ── shared submit path: a WS cancel is durable + visible to a REST duplicate ──
def test_ws_cancel_and_rest_cancel_share_one_durable_submit_path(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    with _session_client(harness) as client:
        with client.websocket_connect(
            _WS_PATH.format(run_id=run_id), headers=_ORIGIN_HEADERS
        ) as ws:
            ws.send_text(
                _command_frame(
                    command_id="cmd:shared", idempotency_key="shared", expected_run_revision=1
                )
            )
            assert RunCommandAckV1.model_validate(ws.receive_json()).status == "accepted"
    # The SAME command identity submitted via REST replays the WS-committed result,
    # proving both surfaces persist through one RunCommandService.submit path.
    with TestClient(harness.app, base_url=ORIGIN) as client:
        response = client.post(
            f"/api/v1/runs/{run_id}:cancel",
            json=harness.cancel_body(
                command_id="cmd:shared",
                idempotency_key="shared",
                expected_run_revision=1,
            ),
        )
    assert response.status_code == 200, response.text
    assert RunCommandAckV1.model_validate(response.json()).status == "duplicate"


# ── disconnect recovery uses persisted command views + SSE events ─────────────
def test_disconnect_recovery_via_persisted_command_and_sse_events(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    with _session_client(harness) as client:
        with client.websocket_connect(
            _WS_PATH.format(run_id=run_id), headers=_ORIGIN_HEADERS
        ) as ws:
            ws.send_text(
                _command_frame(command_id="cmd:1", idempotency_key="k:1", expected_run_revision=1)
            )
            assert RunCommandAckV1.model_validate(ws.receive_json()).status == "accepted"
        # Simulated disconnect. Recovery #1: the persisted command survives — a reconnect
        # resubmitting the SAME command replays its committed result rather than double-acting.
        with client.websocket_connect(
            _WS_PATH.format(run_id=run_id), headers=_ORIGIN_HEADERS
        ) as ws:
            ws.send_text(
                _command_frame(command_id="cmd:1", idempotency_key="k:1", expected_run_revision=1)
            )
            replay = RunCommandAckV1.model_validate(ws.receive_json())
        assert replay.status == "duplicate"
        assert replay.persisted_status == "applied"
        # Recovery #2: the resumable SSE stream re-delivers the durable command events.
        events = client.get(f"/api/v1/runs/{run_id}/events")
    assert events.status_code == 200, events.text
    event_types = [
        line[len("event:") :] for line in events.text.splitlines() if line.startswith("event:")
    ]
    assert "run.cancel_requested" in event_types
    assert "run.cancelled" in event_types
