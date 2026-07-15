"""REST ``POST /api/v1/runs/{id}:cancel`` transport tests (M4c Task 15b).

Drives the real durable ``RunCommandService.submit`` path through the FastAPI
``TestClient`` over the DB-backed :class:`CommandAppHarness`. The REST cancel builds a
``RunCommandV1(type="cancel", …)`` and shares the SAME submit path the WebSocket uses.
Every assertion is offline (checker Runs are ``not_applicable``; no network, no LLM).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from gameforge.contracts.api import RunCommandAckV1
from gameforge.contracts.jobs import Problem
from tests.apps.api.run_command_testkit import (
    ORIGIN,
    CommandAppHarness,
    human_actor,
)


def _headers(key: str) -> dict[str, str]:
    return {"Idempotency-Key": key}


# ── happy path: queued checker Run cancel persists to terminal before ACK ─────
def test_cancel_persists_run_event_and_audit_before_ack(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    assert harness.run_record(run_id).revision == 1
    body = harness.cancel_body(command_id="cmd:1", expected_run_revision=1)
    with TestClient(harness.app, base_url=ORIGIN) as client:
        response = client.post(f"/api/v1/runs/{run_id}:cancel", json=body, headers=_headers("k:1"))
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    ack = RunCommandAckV1.model_validate(response.json())
    assert ack.status == "accepted"
    assert ack.persisted_status == "applied"  # cancel is atomically applied
    assert ack.command_id == "cmd:1" and ack.client_seq == 1
    # The Run mutation + terminal event + audit were durably committed BEFORE the ACK.
    run = harness.run_record(run_id)
    assert run.status == "cancelled"
    assert ack.run_revision == run.revision == 2
    actions = harness.audit_actions(run_id)
    assert "run.command_submitted" in actions and "run.terminal" in actions


def test_duplicate_exact_cancel_replays_stable_ack(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    body = harness.cancel_body(command_id="cmd:dup", expected_run_revision=1)
    with TestClient(harness.app, base_url=ORIGIN) as client:
        first = client.post(f"/api/v1/runs/{run_id}:cancel", json=body, headers=_headers("k:dup"))
        # Exact same command envelope + idempotency key -> committed result is replayed.
        second = client.post(f"/api/v1/runs/{run_id}:cancel", json=body, headers=_headers("k:dup"))
    assert first.status_code == 200 and second.status_code == 200, second.text
    first_ack = RunCommandAckV1.model_validate(first.json())
    second_ack = RunCommandAckV1.model_validate(second.json())
    assert first_ack.status == "accepted"
    assert second_ack.status == "duplicate"
    # A stable ACK: same persisted status / command / run revision.
    assert second_ack.persisted_status == first_ack.persisted_status == "applied"
    assert second_ack.command_revision == first_ack.command_revision
    assert second_ack.run_revision == first_ack.run_revision == 2


def test_same_idempotency_key_changed_payload_is_409_idempotency_conflict(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    first_body = harness.cancel_body(command_id="cmd:a", expected_run_revision=1)
    with TestClient(harness.app, base_url=ORIGIN) as client:
        first = client.post(
            f"/api/v1/runs/{run_id}:cancel", json=first_body, headers=_headers("shared-key")
        )
        assert first.status_code == 200, first.text
        # SAME idempotency key, DIFFERENT command identity/payload -> idempotency conflict.
        changed = harness.cancel_body(
            command_id="cmd:b",
            client_seq=2,
            expected_run_revision=2,
            reason_code="operator_requested",
        )
        conflict = client.post(
            f"/api/v1/runs/{run_id}:cancel", json=changed, headers=_headers("shared-key")
        )
    assert conflict.status_code == 409, conflict.text
    problem = Problem.model_validate(conflict.json()["problem"])
    assert problem.code == "idempotency_conflict"
    assert conflict.json()["command_id"] == "cmd:b"


def test_stale_expected_run_revision_is_409_revision_conflict(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()  # revision 1
    body = harness.cancel_body(command_id="cmd:stale", expected_run_revision=7)
    with TestClient(harness.app, base_url=ORIGIN) as client:
        response = client.post(
            f"/api/v1/runs/{run_id}:cancel", json=body, headers=_headers("k:stale")
        )
    assert response.status_code == 409, response.text
    problem = Problem.model_validate(response.json()["problem"])
    assert problem.code == "revision_conflict"


def test_terminal_run_rejects_further_commands(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    with TestClient(harness.app, base_url=ORIGIN) as client:
        first = client.post(
            f"/api/v1/runs/{run_id}:cancel",
            json=harness.cancel_body(command_id="cmd:1", expected_run_revision=1),
            headers=_headers("k:1"),
        )
        assert first.status_code == 200, first.text
        assert harness.run_record(run_id).status == "cancelled"
        # A brand-new command against the now-terminal Run is refused.
        rejected = client.post(
            f"/api/v1/runs/{run_id}:cancel",
            json=harness.cancel_body(command_id="cmd:2", client_seq=2, expected_run_revision=2),
            headers=_headers("k:2"),
        )
    assert rejected.status_code == 409, rejected.text
    problem = Problem.model_validate(rejected.json()["problem"])
    assert problem.code == "invalid_state_transition"


def test_missing_run_is_404(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    body = harness.cancel_body(command_id="cmd:x", expected_run_revision=1)
    with TestClient(harness.app, base_url=ORIGIN) as client:
        response = client.post(
            "/api/v1/runs/run:missing:cancel", json=body, headers=_headers("k:missing")
        )
    assert response.status_code == 404, response.text


def test_unauthorized_actor_is_forbidden(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    harness.set_http_actor(human_actor(authorized=False))
    body = harness.cancel_body(command_id="cmd:noauth", expected_run_revision=1)
    with TestClient(harness.app, base_url=ORIGIN) as client:
        response = client.post(
            f"/api/v1/runs/{run_id}:cancel", json=body, headers=_headers("k:noauth")
        )
    assert response.status_code == 403, response.text


def test_narrow_domain_actor_forbidden_on_all_active_checker_run(tmp_path: Path) -> None:
    # A checker Run's write permission fails closed to authority over ALL active domains,
    # exactly as admission derives it; a "narrative"-only principal is correctly forbidden.
    from gameforge.contracts.identity import DomainScope

    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    harness.set_http_actor(human_actor(scope=DomainScope(domain_ids=("narrative",))))
    body = harness.cancel_body(command_id="cmd:narrow", expected_run_revision=1)
    with TestClient(harness.app, base_url=ORIGIN) as client:
        response = client.post(
            f"/api/v1/runs/{run_id}:cancel", json=body, headers=_headers("k:narrow")
        )
    assert response.status_code == 403, response.text


def test_missing_idempotency_key_is_rejected(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    body = harness.cancel_body(command_id="cmd:nokey", expected_run_revision=1)
    with TestClient(harness.app, base_url=ORIGIN) as client:
        response = client.post(f"/api/v1/runs/{run_id}:cancel", json=body)
    assert response.status_code == 422, response.text


def test_cancel_ack_never_exposes_worker_fencing_tokens(tmp_path: Path) -> None:
    harness = CommandAppHarness(tmp_path)
    run_id = harness.admit_checker_run()
    body = harness.cancel_body(command_id="cmd:safe", expected_run_revision=1)
    with TestClient(harness.app, base_url=ORIGIN) as client:
        response = client.post(f"/api/v1/runs/{run_id}:cancel", json=body, headers=_headers("k:s"))
    payload = response.json()
    for leaked in ("claimed_fencing_token", "claimed_attempt_no", "claimed_at", "fencing"):
        assert leaked not in payload
        assert leaked not in response.text
    # The ACK validates as the exact browser-facing contract (no RunCommandRecordV1).
    RunCommandAckV1.model_validate(payload)
