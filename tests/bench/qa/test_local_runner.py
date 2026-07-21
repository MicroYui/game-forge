from __future__ import annotations

import json
import os
import subprocess
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import gameforge.bench.external_cases.endless_sky_qa_runner as runner_module
from gameforge.bench.qa.contracts import QA_ACTIVE_CAP_NS, load_session
from gameforge.bench.qa.protocol import seal_qa_protocol, write_protocol
from gameforge.bench.external_cases.endless_sky_qa_runner import (
    _completed_protocol_status,
    create_runner_app,
)


_MANUAL_FORBIDDEN = (
    "case_id",
    "defect_class",
    "gameforge",
    "finding",
    "agent_patch",
    "target_locator",
    "predicate",
    "upstream.patch",
    "/eval",
    "/patches",
)


def _assert_ok(response):  # noqa: ANN001
    assert response.status_code == 200, response.text
    return response.json()


def _finish_current(client: TestClient):
    return _assert_ok(
        client.post(
            "/api/finish",
            json={"participant_attested_no_contamination": True},
        )
    )


class _Clock:
    def __init__(self, value: int = 100) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class _FakeTimer:
    def __init__(self, delay: float, callback) -> None:  # noqa: ANN001
        self.delay = delay
        self.callback = callback
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        assert self.started
        assert not self.cancelled
        self.callback()


class _TimerFactory:
    def __init__(self) -> None:
        self.created: list[_FakeTimer] = []

    def __call__(self, delay: float, callback) -> _FakeTimer:  # noqa: ANN001
        timer = _FakeTimer(delay, callback)
        self.created.append(timer)
        return timer


def test_manual_runner_hides_material_until_start_and_withholds_verdict(
    tmp_path: Path,
):
    workspace = tmp_path / "participant-workspace"
    opened: list[Path] = []
    app = create_runner_app(workspace, editor_opener=opened.append)

    with TestClient(app) as client:
        assert _assert_ok(client.get("/api/current"))["phase"] == "ready"

        prepared = _assert_ok(client.post("/api/next"))
        assert prepared["phase"] == "prepared"
        assert prepared["order"] == 1
        assert prepared["arm"] == "manual"
        assert "task" not in prepared
        prepared_text = json.dumps(prepared, sort_keys=True).casefold()
        assert all(value not in prepared_text for value in _MANUAL_FORBIDDEN)

        running = _assert_ok(client.post("/api/start"))
        assert running["phase"] == "running"
        assert running["task"]["changed_paths"]
        running_text = json.dumps(running, sort_keys=True).casefold()
        assert all(value not in running_text for value in _MANUAL_FORBIDDEN)

        opened_state = _assert_ok(client.post("/api/open-editor"))
        assert opened_state["opened"] is True
        assert opened == [Path(running["task"]["work_path"])]

        syntax = _assert_ok(client.post("/api/syntax-check"))
        assert syntax["exit_code"] == 0

        recorded = _finish_current(client)
        assert recorded["phase"] == "recorded"
        recorded_text = json.dumps(recorded, sort_keys=True).casefold()
        assert "correct" not in recorded_text
        assert "verdict" not in recorded_text
        assert (workspace / "sessions/qa-session-01/session-evidence.json").is_file()


def test_assisted_runner_exposes_only_frozen_payload_after_start(tmp_path: Path):
    workspace = tmp_path / "participant-workspace"
    app = create_runner_app(workspace, editor_opener=lambda _path: None)

    with TestClient(app) as client:
        _assert_ok(client.post("/api/next"))
        _assert_ok(client.post("/api/start"))
        _finish_current(client)

        prepared = _assert_ok(client.post("/api/next"))
        assert prepared["order"] == 2
        assert prepared["arm"] == "assisted"
        assert "assistance" not in prepared

        running = _assert_ok(client.post("/api/start"))
        assert set(running["assistance"]) == {
            "finding",
            "agent_patch",
            "passed_verification",
            "disposition",
        }
        paused = _assert_ok(client.post("/api/pause"))
        assert "task" not in paused
        assert "assistance" not in paused
        resumed = _assert_ok(client.post("/api/resume"))
        assert set(resumed["assistance"]) == {
            "finding",
            "agent_patch",
            "passed_verification",
            "disposition",
        }


def test_pause_resume_and_current_only_actions_use_server_timer(tmp_path: Path):
    workspace = tmp_path / "participant-workspace"
    app = create_runner_app(workspace, editor_opener=lambda _path: None)

    with TestClient(app) as client:
        _assert_ok(client.post("/api/next"))
        running = _assert_ok(client.post("/api/start"))
        assert running["timer"]["active_cap_ns"] == 480_000_000_000

        paused = _assert_ok(client.post("/api/pause"))
        assert paused["phase"] == "paused"
        assert "task" not in paused
        assert "assistance" not in paused
        assert "work_path" not in json.dumps(paused, sort_keys=True)
        assert client.post("/api/syntax-check").status_code == 409
        assert client.post("/api/open-editor").status_code == 409

        resumed = _assert_ok(client.post("/api/resume"))
        assert resumed["phase"] == "running"
        assert resumed["task"]["changed_paths"]
        assert resumed["timer"]["active_ns"] >= paused["timer"]["active_ns"]


def test_runner_surface_is_separate_from_the_normal_console(tmp_path: Path):
    app = create_runner_app(tmp_path / "participant-workspace")

    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "QA Session Runner" in page.text
        assert '<link rel="stylesheet" href="/runner.css"' in page.text
        assert 'name="viewport"' in page.text
        assert "/specs" not in page.text
        assert "/reviews" not in page.text
        assert "/eval" not in page.text
        assert "/approvals" not in page.text
        assert client.get("/runner.css").status_code == 200
        assert client.get("/runner.js").status_code == 200
        assert client.get("/openapi.json").status_code == 404

        for response in (page, client.get("/runner.css"), client.get("/runner.js")):
            assert response.headers["cache-control"] == "no-store"
            assert "default-src 'self'" in response.headers["content-security-policy"]


def test_runner_projects_study_identity_from_server_payload(tmp_path: Path):
    app = create_runner_app(tmp_path / "participant-workspace")
    _, _, protocol = runner_module._frozen_inputs()

    with TestClient(app) as client:
        ready = _assert_ok(client.get("/api/current"))
        prepared = _assert_ok(client.post("/api/next"))

    expected = {
        "participant_id": protocol.participant_id,
        "protocol_summary": (f"{protocol.schema_version} · {protocol.protocol_sha256[:12]}"),
        "study_label": "正式重测 V2",
    }
    assert {field: ready[field] for field in expected} == expected
    assert {field: prepared[field] for field in expected} == expected

    with TestClient(app) as client:
        page = client.get("/").text
        script = client.get("/runner.js").text
    assert "participant-01" not in page
    assert "正式重测 V2" not in page
    assert 'id="study-label"' in page
    assert 'id="participant-id"' in page
    assert 'id="protocol-summary"' in page
    for field in expected:
        assert field in script


def test_runner_uses_the_selected_new_participant_protocol(tmp_path: Path):
    external, hed, _ = runner_module._frozen_inputs()
    protocol = seal_qa_protocol(
        external,
        hed,
        participant_id="participant-02",
        id_namespace="qa-retest-02",
    )
    protocol_path = tmp_path / "qa-protocol-participant-02.json"
    write_protocol(protocol_path, protocol)
    workspace = tmp_path / "participant-02-workspace"
    app = create_runner_app(workspace, protocol_path=protocol_path)

    with TestClient(app) as client:
        ready = _assert_ok(client.get("/api/current"))
        prepared = _assert_ok(client.post("/api/next"))

    assert ready["participant_id"] == "participant-02"
    assert ready["protocol_summary"] == (
        f"{protocol.schema_version} · {protocol.protocol_sha256[:12]}"
    )
    assert prepared["participant_id"] == "participant-02"
    assert (workspace / "sessions/qa-retest-02-session-01").is_dir()


def test_clean_editor_uses_empty_short_session_scoped_profile_directories(
    tmp_path: Path,
    monkeypatch,
):
    profile_root = tmp_path / "profiles"
    monkeypatch.setattr(runner_module, "_VSCODE_PROFILE_ROOT", profile_root)
    work_root = (
        tmp_path
        / "replacement-workspace-with-a-realistically-long-participant-name"
        / "sessions"
        / "qa-retest-participant-session-01"
        / "work"
    )
    work_root.mkdir(parents=True)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(argv, **kwargs):  # noqa: ANN001
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner_module.subprocess, "run", run)

    runner_module._open_clean_vscode(work_root)
    runner_module._open_clean_vscode(work_root)
    other_work_root = work_root.parent.parent / "qa-retest-participant-session-02" / "work"
    other_work_root.mkdir(parents=True)
    runner_module._open_clean_vscode(other_work_root)

    assert len(calls) == 3
    argv, _ = calls[0]
    assert argv[0] == ("/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code")
    assert "open" not in argv
    user_data_flag = next(value for value in argv if value.startswith("--user-data-dir="))
    extensions_flag = next(value for value in argv if value.startswith("--extensions-dir="))
    user_data_dir = Path(user_data_flag.partition("=")[2])
    extensions_dir = Path(extensions_flag.partition("=")[2])
    assert user_data_dir.parent == extensions_dir.parent
    assert user_data_dir.parent.parent == profile_root
    assert user_data_dir.parent.name != ".qa-vscode-profile"
    assert len(user_data_dir.parent.name) == 24
    assert user_data_dir.is_dir() and list(user_data_dir.iterdir()) == []
    assert extensions_dir.is_dir() and list(extensions_dir.iterdir()) == []
    assert "--disable-extensions" in argv
    assert argv[-1] == str(work_root)
    assert calls[1][0] == argv
    other_user_data_flag = next(
        value for value in calls[2][0] if value.startswith("--user-data-dir=")
    )
    assert Path(other_user_data_flag.partition("=")[2]).parent != user_data_dir.parent
    assert calls[0][1]["check"] is True
    assert calls[0][1]["timeout"] == 10


def test_clean_editor_default_profile_stays_under_macos_ipc_path_limit():
    socket_path = runner_module._VSCODE_PROFILE_ROOT / ("0" * 24) / "user-data" / "1.12-main.sock"

    assert len(os.fsencode(socket_path.resolve())) < 104


def test_server_deadline_freezes_submission_and_rejects_late_work(tmp_path: Path):
    workspace = tmp_path / "participant-workspace"
    clock = _Clock()
    timers = _TimerFactory()
    app = create_runner_app(
        workspace,
        editor_opener=lambda _path: None,
        clock=clock,
        timer_factory=timers,
    )

    with TestClient(app) as client:
        _assert_ok(client.post("/api/next"))
        running = _assert_ok(client.post("/api/start"))
        assert len(timers.created) == 1
        deadline_timer = timers.created[-1]
        assert deadline_timer.delay == 480.0

        clock.value += QA_ACTIVE_CAP_NS
        deadline_timer.fire()

        frozen = _assert_ok(client.get("/api/current"))
        assert frozen["phase"] == "frozen"
        assert frozen["timer"]["timed_out"] is True
        assert frozen["timer"]["remaining_ns"] == 0
        assert "task" not in frozen
        assert "assistance" not in frozen
        assert "work_path" not in json.dumps(frozen, sort_keys=True)

        for action in ("pause", "resume", "open-editor", "syntax-check"):
            response = client.post(f"/api/{action}")
            assert response.status_code == 409

        # Simulate the still-open external editor recreating and changing the old path.
        late_path = workspace / "sessions/qa-session-01/work" / running["task"]["changed_paths"][0]
        late_path.parent.mkdir(parents=True)
        late_path.write_text("late editor save must not count\n", encoding="utf-8")

        recorded = _finish_current(client)
        assert recorded["phase"] == "recorded"
        assert "correct" not in json.dumps(recorded, sort_keys=True).casefold()

    evidence = load_session(workspace / "sessions/qa-session-01/session-evidence.json")
    assert evidence.active_ns == QA_ACTIVE_CAP_NS
    assert evidence.timed_out is True
    assert (workspace / "sessions/qa-session-01/final.patch").read_bytes() == b""


def test_running_session_is_frozen_on_server_restart_after_deadline(tmp_path: Path):
    workspace = tmp_path / "participant-workspace"
    clock = _Clock()
    first_timers = _TimerFactory()
    first_app = create_runner_app(
        workspace,
        editor_opener=lambda _path: None,
        clock=clock,
        timer_factory=first_timers,
    )
    with TestClient(first_app) as client:
        _assert_ok(client.post("/api/next"))
        _assert_ok(client.post("/api/start"))
    assert first_timers.created[-1].cancelled is True

    clock.value += QA_ACTIVE_CAP_NS + 5
    recovered_timers = _TimerFactory()
    recovered_app = create_runner_app(
        workspace,
        editor_opener=lambda _path: None,
        clock=clock,
        timer_factory=recovered_timers,
    )
    with TestClient(recovered_app) as client:
        current = _assert_ok(client.get("/api/current"))
        assert current["phase"] == "frozen"
        assert current["timer"]["timed_out"] is True
    assert recovered_timers.created == []


def test_mutating_action_reconciles_cap_before_delayed_timer_callback(tmp_path: Path):
    workspace = tmp_path / "participant-workspace"
    clock = _Clock()
    timers = _TimerFactory()
    app = create_runner_app(
        workspace,
        editor_opener=lambda _path: None,
        clock=clock,
        timer_factory=timers,
    )

    with TestClient(app) as client:
        _assert_ok(client.post("/api/next"))
        _assert_ok(client.post("/api/start"))
        clock.value += QA_ACTIVE_CAP_NS

        assert client.post("/api/pause").status_code == 409
        frozen = _assert_ok(client.get("/api/current"))
        assert frozen["phase"] == "frozen"
        assert "task" not in frozen
        assert timers.created[-1].cancelled is True


def test_syntax_timeout_discards_output_if_deadline_freezes_during_command(
    tmp_path: Path,
    monkeypatch,
):
    workspace = tmp_path / "participant-workspace"
    clock = _Clock()
    timers = _TimerFactory()
    app = create_runner_app(
        workspace,
        editor_opener=lambda _path: None,
        clock=clock,
        timer_factory=timers,
    )

    with TestClient(app) as client:
        _assert_ok(client.post("/api/next"))
        _assert_ok(client.post("/api/start"))

        def timeout(argv, **_kwargs):  # noqa: ANN001
            clock.value += QA_ACTIVE_CAP_NS
            timers.created[-1].fire()
            raise subprocess.TimeoutExpired(argv, 30)

        monkeypatch.setattr(
            "gameforge.bench.external_cases.endless_sky_qa_runner.subprocess.run",
            timeout,
        )
        response = client.post("/api/syntax-check")
        assert response.status_code == 409
        assert "timed out" not in response.text
        assert _assert_ok(client.get("/api/current"))["phase"] == "frozen"


def test_contamination_is_visible_as_protocol_failure_without_verdict(tmp_path: Path):
    workspace = tmp_path / "participant-workspace"
    app = create_runner_app(workspace, editor_opener=lambda _path: None)

    with TestClient(app) as client:
        _assert_ok(client.post("/api/next"))
        _assert_ok(client.post("/api/start"))
        response = _assert_ok(
            client.post(
                "/api/finish",
                json={"participant_attested_no_contamination": False},
            )
        )
        assert response["phase"] == "recorded"
        assert response["protocol_status"] == "failure"
        serialized = json.dumps(response, sort_keys=True).casefold()
        assert "correct" not in serialized
        assert "verdict" not in serialized

        reloaded = _assert_ok(client.get("/api/current"))
        assert reloaded["protocol_status"] == "failure"


def test_frozen_attestation_cannot_change_during_finish_recovery(
    tmp_path: Path,
    monkeypatch,
):
    workspace = tmp_path / "participant-workspace"
    app = create_runner_app(workspace, editor_opener=lambda _path: None)

    with TestClient(app) as client:
        _assert_ok(client.post("/api/next"))
        _assert_ok(client.post("/api/start"))
        with monkeypatch.context() as active:
            active.setattr(
                "gameforge.bench.external_cases.endless_sky_qa_runner.evaluate_submission",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    ValueError("case_id=secret target_locator=hidden")
                ),
            )
            failed = client.post(
                "/api/finish",
                json={"participant_attested_no_contamination": False},
            )
        assert failed.status_code == 409
        assert "case_id" not in failed.text
        frozen = _assert_ok(client.get("/api/current"))
        assert frozen["phase"] == "frozen"
        assert "task" not in frozen
        assert "assistance" not in frozen

        changed = client.post(
            "/api/finish",
            json={"participant_attested_no_contamination": True},
        )
        assert changed.status_code == 409
        recovered = _assert_ok(
            client.post(
                "/api/finish",
                json={"participant_attested_no_contamination": False},
            )
        )
        assert recovered["protocol_status"] == "failure"


def test_concurrent_finish_requests_serialize_different_attestations(
    tmp_path: Path,
    monkeypatch,
):
    workspace = tmp_path / "participant-workspace"
    app = create_runner_app(workspace, editor_opener=lambda _path: None)
    entered_evaluator = threading.Event()
    release_evaluator = threading.Event()
    second_started = threading.Event()
    actual_evaluator = runner_module.evaluate_submission

    def blocking_evaluator(*args, **kwargs):  # noqa: ANN002, ANN003
        entered_evaluator.set()
        assert release_evaluator.wait(timeout=5)
        return actual_evaluator(*args, **kwargs)

    monkeypatch.setattr(runner_module, "evaluate_submission", blocking_evaluator)

    def submit_opposite(client: TestClient):
        second_started.set()
        return client.post(
            "/api/finish",
            json={"participant_attested_no_contamination": True},
        )

    with TestClient(app) as client:
        _assert_ok(client.post("/api/next"))
        _assert_ok(client.post("/api/start"))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                client.post,
                "/api/finish",
                json={"participant_attested_no_contamination": False},
            )
            assert entered_evaluator.wait(timeout=5)
            second = pool.submit(submit_opposite, client)
            assert second_started.wait(timeout=5)
            with pytest.raises(FutureTimeoutError):
                second.result(timeout=0.1)
            release_evaluator.set()
            first_response = first.result(timeout=10)
            second_response = second.result(timeout=10)

    assert first_response.status_code == 200
    assert first_response.json()["protocol_status"] == "failure"
    assert second_response.status_code == 409
    assert "verdict" not in second_response.text.casefold()


def test_protocol_status_aggregates_every_completed_session(tmp_path: Path, monkeypatch):
    protocol = SimpleNamespace(
        sessions=(
            SimpleNamespace(session_id="qa-session-01"),
            SimpleNamespace(session_id="qa-session-02"),
            SimpleNamespace(session_id="qa-session-03"),
        )
    )
    statuses = {
        "qa-session-01": SimpleNamespace(protocol_valid=False),
        "qa-session-02": SimpleNamespace(protocol_valid=True),
        "qa-session-03": SimpleNamespace(protocol_valid=True),
    }
    loaded: list[str] = []

    def load(path):  # noqa: ANN001
        loaded.append(path.parent.name)
        return statuses[path.parent.name]

    monkeypatch.setattr(
        "gameforge.bench.external_cases.endless_sky_qa_runner.load_session",
        load,
    )

    assert _completed_protocol_status(tmp_path, protocol, 3) == "failure"
    assert loaded == ["qa-session-01", "qa-session-02", "qa-session-03"]


def test_participant_errors_are_stable_and_hide_internal_context(
    tmp_path: Path,
    monkeypatch,
    caplog,
):
    secret = "case_id=secret target_locator=hidden predicate=context"

    def fail(_workspace, **_kwargs):  # noqa: ANN001
        raise ValueError(secret)

    monkeypatch.setattr(
        "gameforge.bench.external_cases.endless_sky_qa_runner.next_session",
        fail,
    )
    app = create_runner_app(tmp_path / "participant-workspace")

    with TestClient(app) as client:
        first = client.post("/api/next")
        second = client.post("/api/next")

    assert first.status_code == 409
    assert first.json() == second.json() == {"detail": "当前场次无法准备；请停止操作并联系主持人。"}
    assert secret not in first.text
    assert secret in caplog.text


def test_unexpected_participant_error_is_generic_and_logged_locally(
    tmp_path: Path,
    monkeypatch,
    caplog,
):
    secret = "case_id=secret target_locator=hidden runtime-context"

    def fail(_workspace, **_kwargs):  # noqa: ANN001
        raise RuntimeError(secret)

    monkeypatch.setattr(runner_module, "next_session", fail)
    app = create_runner_app(tmp_path / "participant-workspace")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api/next")

    assert response.status_code == 500
    assert response.json() == {"detail": "QA Runner 遇到本机错误；请停止操作并联系主持人。"}
    assert secret not in response.text
    assert secret in caplog.text


def test_runner_dom_destroys_prior_assistance_and_moves_focus(tmp_path: Path):
    app = create_runner_app(tmp_path / "participant-workspace")
    with TestClient(app) as client:
        html = client.get("/").text
        script = client.get("/runner.js").text

    node_program = textwrap.dedent(
        """
        const fs = require("node:fs");
        const { JSDOM } = require("jsdom");
        const html = fs.readFileSync(process.argv[1], "utf8");
        const script = fs.readFileSync(process.argv[2], "utf8");
        const assisted = {
          schema_version: "qa-runner-view@1", phase: "running", completed: 1, total: 8,
          order: 2, arm: "assisted",
          timer: {active_ns: 1, active_cap_ns: 480000000000, remaining_ns: 479999999999,
                  timed_out: false, running: true},
          task: {subject: "Assisted secret", changed_paths: ["data/a.txt"], work_path: "/tmp/a"},
          assistance: {finding: {message: "SECRET FINDING", minimal_repro: {target: "SECRET"}},
                       agent_patch: {value: "SECRET PATCH"}, passed_verification: true,
                       disposition: "edited"}
        };
        const manual = {
          ...assisted, order: 3, arm: "manual",
          task: {subject: "Manual task", changed_paths: ["data/b.txt"], work_path: "/tmp/b"}
        };
        delete manual.assistance;
        const recorded = {schema_version: "qa-runner-view@1", phase: "recorded",
                          completed: 2, total: 8, protocol_status: "valid"};
        const paused = {schema_version: "qa-runner-view@1", phase: "paused",
                        completed: 1, total: 8, order: 2, arm: "assisted",
                        timer: {active_ns: 1, active_cap_ns: 480000000000,
                                remaining_ns: 479999999999, timed_out: false, running: false}};
        const protocolFailure = {...recorded, protocol_status: "failure"};
        const prepared = {schema_version: "qa-runner-view@1", phase: "prepared",
                          completed: 2, total: 8, order: 3, arm: "manual"};
        const frozen = {schema_version: "qa-runner-view@1", phase: "frozen",
                        completed: 2, total: 8, order: 3, arm: "manual",
                        timer: {active_ns: 480000000000, active_cap_ns: 480000000000,
                                remaining_ns: 0, timed_out: true, running: false}};
        const dom = new JSDOM(html, {runScripts: "outside-only", url: "http://127.0.0.1/"});
        const { window } = dom;
        window.setInterval = () => 0;
        window.fetch = async () => ({ok: true, json: async () => assisted});
        window.eval(script);
        setImmediate(async () => {
          let authority = paused;
          window.fetch = async (path) => path === "/api/current"
            ? {ok: true, json: async () => authority}
            : {ok: false, json: async () => ({detail: "ambiguous response"})};
          await window.action("/api/pause");
          if (window.document.getElementById("paused-panel").hidden ||
              window.document.body.textContent.includes("SECRET")) {
            throw new Error("ambiguous pause did not reconcile to hidden paused authority");
          }
          authority = assisted;
          await window.action("/api/resume");
          if (window.document.getElementById("task-panel").hidden ||
              !window.document.getElementById("finding-message").textContent.includes("SECRET")) {
            throw new Error("ambiguous resume did not reconcile to running authority");
          }
          window.document.getElementById("syntax-output").textContent = "SECRET SYNTAX";
          window.document.getElementById("syntax-output").hidden = false;
          window.document.getElementById("notice").textContent = "SECRET NOTICE";
          window.render(paused);
          if (window.document.body.textContent.includes("SECRET")) {
            throw new Error("paused state retained task or assistance content");
          }
          window.render(recorded);
          if (!window.document.querySelector(".timer").hidden) {
            throw new Error("recorded state retained a misleading active timer");
          }
          const staleIds = ["finding-message", "finding-repro", "agent-patch",
                            "assistance-status", "syntax-output", "notice", "task-mode",
                            "prepared-mode", "prepared-order", "prepared-guidance"];
          for (const id of staleIds) {
            if (window.document.getElementById(id).textContent.includes("SECRET")) {
              throw new Error(`stale DOM leaked from ${id}`);
            }
          }
          window.render(protocolFailure);
          if (!window.document.getElementById("recorded-protocol").textContent.includes("协议失败")) {
            throw new Error("protocol failure was not displayed");
          }
          window.render(prepared);
          if (window.document.querySelector(".timer").hidden) {
            throw new Error("prepared state hid the active cap timer");
          }
          if (window.document.getElementById("prepared-mode").textContent !== "手工排查") {
            throw new Error("manual preparation mode was not rendered");
          }
          window.render(manual);
          const body = window.document.body.textContent;
          if (body.includes("SECRET")) throw new Error(`stale DOM leaked: ${body}`);
          if (window.document.getElementById("syntax-output").textContent !== "") {
            throw new Error("syntax output was not destroyed");
          }
          if (window.document.activeElement.id !== "task-subject") {
            throw new Error(`unexpected focus: ${window.document.activeElement.id}`);
          }
          window.render(assisted);
          window.confirm = () => true;
          window.fetch = async (path) => path === "/api/current"
            ? {ok: true, json: async () => frozen}
            : {ok: false, json: async () => ({detail: "safe failure"})};
          const selection = window.document.getElementById("attestation-select");
          const button = window.document.getElementById("finish-button");
          selection.value = "clear";
          selection.dispatchEvent(new window.Event("change"));
          button.click();
          await new Promise((resolve) => setImmediate(resolve));
          await new Promise((resolve) => setImmediate(resolve));
          const retrySelection = window.document.getElementById("frozen-attestation-select");
          const retryButton = window.document.getElementById("frozen-finish-button");
          if (window.document.body.textContent.includes("SECRET")) {
            throw new Error("failed finish retained task or assistance content");
          }
          if (window.document.getElementById("frozen-panel").hidden) {
            throw new Error("failed finish did not reconcile to authoritative frozen state");
          }
          if (retrySelection.value !== "clear" || retryButton.disabled) {
            throw new Error("failed finish did not preserve the attestation for retry");
          }
        });
        """
    )
    html_path = tmp_path / "runner.html"
    script_path = tmp_path / "runner.js"
    html_path.write_text(html, encoding="utf-8")
    script_path.write_text(script, encoding="utf-8")
    completed = subprocess.run(
        ["node", "-e", node_program, str(html_path), str(script_path)],
        cwd="web",
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_runner_markup_avoids_timer_live_spam_and_labels_scroll_regions(tmp_path: Path):
    app = create_runner_app(tmp_path / "participant-workspace")

    with TestClient(app) as client:
        page = client.get("/").text

    assert 'class="timer" aria-live=' not in page
    for element_id in ("finding-repro", "agent-patch", "syntax-output"):
        assert f'id="{element_id}"' in page
        fragment = page.split(f'id="{element_id}"', maxsplit=1)[1].split(">", maxsplit=1)[0]
        assert 'tabindex="0"' in fragment


def test_runner_copy_names_the_complete_allowed_and_forbidden_boundary(tmp_path: Path):
    app = create_runner_app(tmp_path / "participant-workspace")

    with TestClient(app) as client:
        page = client.get("/").text
        script = client.get("/runner.js").text
    rendered_page = " ".join(page.split())

    prepared_copy = (
        "我确认：本场从开始到记录完成，只使用 Runner 打开的隔离编辑器和原生语法检查；"
        "辅助场可另使用本页显示的 GameForge 建议。我不会使用 Copilot、Codex/ChatGPT 或其他外部 AI，"
        "不会使用网络搜索、普通 GameForge Console、旧 QA 工作区或其他资料。编辑器内查找（⌘F）允许。"
        "错误、超时或无法完成都如实保留，不重做。"
    )
    task_copy = (
        "允许：隔离编辑器、原生语法检查；辅助场另含本页 GameForge 建议。"
        "禁止：Copilot、任何外部 AI、网络搜索、普通 Console、旧 QA 内容。"
    )
    assert prepared_copy in rendered_page
    assert task_copy in rendered_page
    assert page.count("确认本场全程仅使用允许材料") == 2
    assert page.count("使用过禁止材料，或无法确认（本场记为 protocol_failure）") == 2
    assert "确认冻结并记录本场？提交后不能修改或重做；不会立即显示正确性。" in script
