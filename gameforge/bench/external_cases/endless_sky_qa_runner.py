"""Loopback-only participant surface for the frozen Endless Sky QA study."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

from gameforge.bench.external_cases.endless_sky_qa import (
    _PROTOCOL_PATH,
    _bound_workspace,
    _frozen_inputs,
    _session_bundle,
    _workspace_root,
    evaluate_submission,
    next_session,
)
from gameforge.bench.qa.contracts import QA_ACTIVE_CAP_NS, load_session
from gameforge.bench.qa.harness import finalize_session
from gameforge.bench.qa.protocol import QaProtocol
from gameforge.bench.qa.session import (
    QaSessionState,
    freeze_session_submission,
    load_state,
    transition_session,
)


EditorOpener = Callable[[Path], None]
Clock = Callable[[], int]


class DeadlineTimer(Protocol):
    def start(self) -> None: ...

    def cancel(self) -> None: ...


TimerFactory = Callable[[float, Callable[[], None]], DeadlineTimer]


_LOGGER = logging.getLogger(__name__)
_ASSETS = Path(__file__).resolve().parents[1] / "qa" / "runner_assets"
_VSCODE_CLI = Path("/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code")
_VSCODE_PROFILE_ROOT = Path("/tmp/gf-qa-vscode")
_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'none'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


def _threading_timer(
    delay_seconds: float,
    callback: Callable[[], None],
) -> DeadlineTimer:
    timer = threading.Timer(delay_seconds, callback)
    timer.daemon = True
    return timer


class FinishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    participant_attested_no_contamination: bool


def _open_clean_vscode(work_root: Path) -> None:
    if not _VSCODE_CLI.is_file():
        raise FileNotFoundError("Visual Studio Code CLI is unavailable")
    isolation_key = hashlib.sha256(work_root.resolve().as_posix().encode("utf-8")).hexdigest()[:24]
    isolation_root = _VSCODE_PROFILE_ROOT / isolation_key
    user_data_dir = isolation_root / "user-data"
    extensions_dir = isolation_root / "extensions"
    user_data_dir.mkdir(parents=True, exist_ok=True)
    extensions_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603 - fixed local application and arguments
        [
            str(_VSCODE_CLI),
            "--disable-extensions",
            "--disable-extension=github.copilot",
            "--disable-extension=github.copilot-chat",
            f"--user-data-dir={user_data_dir}",
            f"--extensions-dir={extensions_dir}",
            "--reuse-window",
            str(work_root),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=10,
    )


def _current_bundle(
    workspace: Path,
    protocol_path: str | Path = _PROTOCOL_PATH,
):  # noqa: ANN202
    root = _workspace_root(workspace)
    _, _, protocol = _frozen_inputs(protocol_path)
    sessions_root = root / "sessions"
    completed = 0
    for spec in protocol.sessions:
        bundle = sessions_root / spec.session_id
        if (bundle / "session-evidence.json").is_file():
            completed += 1
            continue
        if bundle.is_dir():
            return protocol, spec, bundle, completed
        return protocol, None, None, completed
    return protocol, None, None, completed


def _active_ns(state: QaSessionState, now_ns: int) -> int:
    active = 0
    running_since: int | None = None
    for event in state.events:
        if event.kind in {"start", "resume"}:
            running_since = event.monotonic_ns
        elif event.kind == "pause":
            if running_since is None:
                raise ValueError("QA timer state has no running interval")
            active += event.monotonic_ns - running_since
            running_since = None
        elif event.kind == "finish":
            if running_since is None:
                raise ValueError("QA timer state has no running interval")
            active += event.monotonic_ns - running_since
            running_since = None
    if state.status == "running":
        if running_since is None or now_ns < running_since:
            raise ValueError("QA timer state has an invalid running interval")
        active += now_ns - running_since
    return active


def _running_deadline_ns(state: QaSessionState) -> int:
    if state.status != "running":
        raise ValueError("QA deadline requires a running timer")
    active_before_current = 0
    running_since: int | None = None
    for event in state.events:
        if event.kind in {"start", "resume"}:
            running_since = event.monotonic_ns
        elif event.kind == "pause":
            if running_since is None:
                raise ValueError("QA timer state has no running interval")
            active_before_current += event.monotonic_ns - running_since
            running_since = None
    if running_since is None:
        raise ValueError("QA timer state has no current running interval")
    remaining = QA_ACTIVE_CAP_NS - active_before_current
    if remaining <= 0:
        raise ValueError("QA timer resumed after reaching its active cap")
    return running_since + remaining


class _DeadlineGuard:
    def __init__(
        self,
        workspace: Path,
        *,
        protocol_path: str | Path,
        clock: Clock,
        timer_factory: TimerFactory,
    ) -> None:
        self.workspace = workspace
        self.protocol_path = protocol_path
        self.clock = clock
        self.timer_factory = timer_factory
        self.lock = threading.RLock()
        self._timer: DeadlineTimer | None = None
        self._generation = 0
        self._closed = False

    def _cancel_locked(self) -> None:
        self._generation += 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _freeze_if_due_locked(
        self,
        bundle: Path,
        state: QaSessionState,
    ) -> QaSessionState:
        if state.status != "running":
            return state
        deadline_ns = _running_deadline_ns(state)
        # Recover the narrow rename-before-state-write crash window immediately.
        if not (bundle / "work").is_dir():
            self._cancel_locked()
            return freeze_session_submission(bundle, clock=lambda: deadline_ns)
        if self.clock() < deadline_ns:
            return state
        self._cancel_locked()
        return freeze_session_submission(bundle, clock=lambda: deadline_ns)

    def reconcile_current_locked(self) -> tuple[Path, QaSessionState] | None:
        _, _, bundle, _ = _current_bundle(self.workspace, self.protocol_path)
        if bundle is None:
            return None
        state = self._freeze_if_due_locked(bundle, load_state(bundle))
        return bundle, state

    def _schedule_locked(self, bundle: Path, state: QaSessionState) -> QaSessionState:
        self._cancel_locked()
        state = self._freeze_if_due_locked(bundle, state)
        if state.status != "running" or self._closed:
            return state
        deadline_ns = _running_deadline_ns(state)
        delay_seconds = (deadline_ns - self.clock()) / 1_000_000_000
        if delay_seconds <= 0:
            return self._freeze_if_due_locked(bundle, state)
        token = self._generation

        def on_deadline() -> None:
            with self.lock:
                if self._closed or token != self._generation:
                    return
                self._timer = None
                try:
                    active = load_state(bundle)
                    if active.status != "running" or _running_deadline_ns(active) != deadline_ns:
                        return
                    if self.clock() < deadline_ns:
                        self._schedule_locked(bundle, active)
                        return
                    freeze_session_submission(bundle, clock=lambda: deadline_ns)
                    self._generation += 1
                except (OSError, TypeError, ValueError):
                    _LOGGER.exception("QA deadline freeze failed")

        timer = self.timer_factory(delay_seconds, on_deadline)
        self._timer = timer
        timer.start()
        return state

    def schedule_locked(self, bundle: Path, state: QaSessionState) -> QaSessionState:
        return self._schedule_locked(bundle, state)

    def cancel_locked(self) -> None:
        self._cancel_locked()

    def startup(self) -> None:
        with self.lock:
            self._closed = False
            current = self.reconcile_current_locked()
            if current is not None:
                bundle, state = current
                if state.status == "running":
                    self._schedule_locked(bundle, state)

    def shutdown(self) -> None:
        with self.lock:
            self._closed = True
            self._cancel_locked()


def _load_task(bundle: Path) -> dict[str, object]:
    value = json.loads((bundle / "TASK.json").read_text(encoding="utf-8"))
    if value.get("schema_version") != "qa-task@1":
        raise ValueError("QA task has an unsupported schema")
    return value


def _completed_protocol_status(
    workspace: Path,
    protocol: QaProtocol,
    completed: int,
) -> str:
    sessions_root = _workspace_root(workspace) / "sessions"
    protocol_valid = True
    for session in protocol.sessions[:completed]:
        evidence = load_session(sessions_root / session.session_id / "session-evidence.json")
        protocol_valid = protocol_valid and evidence.protocol_valid
    return "valid" if protocol_valid else "failure"


def _current_payload(
    workspace: Path,
    *,
    protocol_path: str | Path = _PROTOCOL_PATH,
    clock: Clock = time.monotonic_ns,
) -> dict[str, object]:
    protocol, spec, bundle, completed = _current_bundle(workspace, protocol_path)
    total = len(protocol.sessions)
    study = {
        "participant_id": protocol.participant_id,
        "protocol_summary": (f"{protocol.schema_version} · {protocol.protocol_sha256[:12]}"),
        "study_label": "正式重测 V2",
    }
    if spec is None:
        phase = "complete" if completed == total else ("recorded" if completed else "ready")
        payload: dict[str, object] = {
            "schema_version": "qa-runner-view@1",
            "phase": phase,
            "completed": completed,
            "total": total,
            **study,
        }
        if completed:
            payload["protocol_status"] = _completed_protocol_status(
                workspace,
                protocol,
                completed,
            )
        return payload

    state = load_state(bundle)
    now_ns = clock()
    active_ns = _active_ns(state, now_ns)
    phase = "frozen" if state.status == "finished" else state.status
    payload: dict[str, object] = {
        "schema_version": "qa-runner-view@1",
        "phase": phase,
        "completed": completed,
        "total": total,
        **study,
        "order": state.order,
        "arm": state.arm,
        "timer": {
            "active_ns": active_ns,
            "active_cap_ns": QA_ACTIVE_CAP_NS,
            "remaining_ns": max(0, QA_ACTIVE_CAP_NS - active_ns),
            "timed_out": active_ns >= QA_ACTIVE_CAP_NS,
            "running": state.status == "running",
        },
    }
    if state.status != "running":
        return payload

    task = _load_task(bundle)
    changed_paths = task.get("changed_paths")
    if not isinstance(changed_paths, list) or not all(
        isinstance(path, str) for path in changed_paths
    ):
        raise ValueError("QA task changed_paths are invalid")
    payload["task"] = {
        "subject": task["upstream_subject"],
        "changed_paths": changed_paths,
        "work_path": str((bundle / "work").resolve()),
    }
    if state.arm == "assisted":
        assistance = json.loads((bundle / "GAMEFORGE.json").read_text(encoding="utf-8"))
        if set(assistance) != {
            "finding",
            "agent_patch",
            "passed_verification",
            "disposition",
        }:
            raise ValueError("QA assistance payload differs from the frozen schema")
        payload["assistance"] = assistance
    return payload


def _active_session(
    workspace: Path,
    protocol_path: str | Path = _PROTOCOL_PATH,
):  # noqa: ANN202
    _, spec, bundle, _ = _current_bundle(workspace, protocol_path)
    if spec is None or bundle is None:
        raise HTTPException(status_code=409, detail="当前没有可操作的 QA 场次。")
    return spec, bundle, load_state(bundle)


def create_runner_app(
    workspace: str | Path,
    *,
    protocol_path: str | Path = _PROTOCOL_PATH,
    editor_opener: EditorOpener = _open_clean_vscode,
    clock: Clock = time.monotonic_ns,
    timer_factory: TimerFactory = _threading_timer,
) -> FastAPI:
    _, _, active_protocol = _frozen_inputs(protocol_path)
    active_workspace = _bound_workspace(workspace, active_protocol)
    deadlines = _DeadlineGuard(
        active_workspace,
        protocol_path=protocol_path,
        clock=clock,
        timer_factory=timer_factory,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        deadlines.startup()
        try:
            yield
        finally:
            deadlines.shutdown()

    app = FastAPI(
        title="QA Session Runner",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        _LOGGER.error(
            "Unhandled QA Runner request failure",
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "QA Runner 遇到本机错误；请停止操作并联系主持人。"},
        )

    @app.middleware("http")
    async def study_headers(request, call_next):  # noqa: ANN001, ANN202
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = _CSP
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_ASSETS / "index.html", media_type="text/html")

    @app.get("/runner.css", include_in_schema=False)
    def stylesheet() -> FileResponse:
        return FileResponse(_ASSETS / "runner.css", media_type="text/css")

    @app.get("/runner.js", include_in_schema=False)
    def script() -> FileResponse:
        return FileResponse(_ASSETS / "runner.js", media_type="text/javascript")

    @app.get("/api/current")
    def current() -> dict[str, object]:
        try:
            with deadlines.lock:
                deadlines.reconcile_current_locked()
                return _current_payload(
                    active_workspace,
                    protocol_path=protocol_path,
                    clock=clock,
                )
        except (OSError, TypeError, ValueError) as exc:
            _LOGGER.exception("QA Runner could not read current state")
            raise HTTPException(
                status_code=409,
                detail="当前场次状态无法读取；请停止操作并联系主持人。",
            ) from exc

    @app.post("/api/next")
    def prepare_next() -> dict[str, object]:
        try:
            with deadlines.lock:
                deadlines.reconcile_current_locked()
                next_session(active_workspace, protocol_path=protocol_path)
                return _current_payload(
                    active_workspace,
                    protocol_path=protocol_path,
                    clock=clock,
                )
        except (OSError, TypeError, ValueError) as exc:
            _LOGGER.exception("QA Runner could not prepare the next session")
            raise HTTPException(
                status_code=409,
                detail="当前场次无法准备；请停止操作并联系主持人。",
            ) from exc

    def timing_transition(kind: str) -> dict[str, object]:
        try:
            with deadlines.lock:
                deadlines.reconcile_current_locked()
                _, bundle, _ = _active_session(active_workspace, protocol_path)
                updated = transition_session(bundle, kind, clock=clock)
                if kind == "pause":
                    deadlines.cancel_locked()
                else:
                    deadlines.schedule_locked(bundle, updated)
                return _current_payload(
                    active_workspace,
                    protocol_path=protocol_path,
                    clock=clock,
                )
        except (OSError, TypeError, ValueError) as exc:
            _LOGGER.exception("QA Runner timer transition failed: %s", kind)
            raise HTTPException(
                status_code=409,
                detail="当前计时状态不允许此操作；请刷新后重试。",
            ) from exc

    @app.post("/api/start")
    def start() -> dict[str, object]:
        return timing_transition("start")

    @app.post("/api/pause")
    def pause() -> dict[str, object]:
        return timing_transition("pause")

    @app.post("/api/resume")
    def resume() -> dict[str, object]:
        return timing_transition("resume")

    @app.post("/api/open-editor")
    def open_editor() -> dict[str, object]:
        try:
            with deadlines.lock:
                deadlines.reconcile_current_locked()
                _, bundle, state = _active_session(active_workspace, protocol_path)
                if state.status != "running":
                    raise HTTPException(
                        status_code=409,
                        detail="编辑器只能在本场计时中打开。",
                    )
                work_root = (bundle / "work").resolve()
            editor_opener(work_root)
        except HTTPException:
            raise
        except (OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
            _LOGGER.exception("QA Runner could not open the clean editor")
            raise HTTPException(
                status_code=500,
                detail="无扩展编辑器无法打开；请停止操作并联系主持人。",
            ) from exc
        return {"opened": True}

    @app.post("/api/syntax-check")
    def syntax_check() -> dict[str, object]:
        try:
            with deadlines.lock:
                deadlines.reconcile_current_locked()
                _, bundle, state = _active_session(active_workspace, protocol_path)
                if state.status != "running":
                    raise HTTPException(
                        status_code=409,
                        detail="原生语法检查只能在本场计时中运行。",
                    )
                task = _load_task(bundle)
                argv = task.get("syntax_check_argv")
                if (
                    not isinstance(argv, list)
                    or not argv
                    or not all(isinstance(value, str) for value in argv)
                ):
                    raise ValueError("QA syntax command is invalid")
            completed = subprocess.run(  # noqa: S603 - frozen bundle command
                argv,
                cwd=bundle,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            completed = subprocess.CompletedProcess(
                argv,
                124,
                stdout=b"",
                stderr=b"syntax check timed out",
            )
        except HTTPException:
            raise
        except (OSError, TypeError, ValueError) as exc:
            _LOGGER.exception("QA Runner native syntax check failed")
            raise HTTPException(
                status_code=409,
                detail="原生语法检查无法运行；请停止操作并联系主持人。",
            ) from exc
        with deadlines.lock:
            current = deadlines.reconcile_current_locked()
            if current is None or current[0] != bundle or current[1].status != "running":
                raise HTTPException(
                    status_code=409,
                    detail="本场提交已冻结；语法检查结果已丢弃。",
                )
        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout.decode("utf-8", errors="replace"),
            "stderr": completed.stderr.decode("utf-8", errors="replace"),
        }

    @app.post("/api/finish")
    def finish(values: FinishRequest) -> dict[str, object]:
        try:
            with deadlines.lock:
                deadlines.reconcile_current_locked()
                spec, bundle, state = _active_session(active_workspace, protocol_path)
                if state.status not in {"running", "finished"}:
                    raise HTTPException(
                        status_code=409,
                        detail="结束操作只允许在计时中或提交已冻结后执行。",
                    )
                external, protocol, checked_spec, checked_bundle = _session_bundle(
                    active_workspace,
                    spec.session_id,
                    protocol_path=protocol_path,
                )
                if checked_spec != spec or checked_bundle != bundle:
                    raise ValueError("current QA session changed")
                if state.status == "running":
                    freeze_session_submission(bundle, clock=clock)
                deadlines.cancel_locked()
                finalize_session(
                    protocol,
                    spec,
                    bundle,
                    evaluator=lambda work: evaluate_submission(
                        spec.case_id,
                        work,
                        external=external,
                    ),
                    participant_attested_no_contamination=(
                        values.participant_attested_no_contamination
                    ),
                    clock=clock,
                )
                return _current_payload(
                    active_workspace,
                    protocol_path=protocol_path,
                    clock=clock,
                )
        except HTTPException:
            raise
        except (OSError, TypeError, ValueError) as exc:
            _LOGGER.exception("QA Runner could not finalize the frozen submission")
            raise HTTPException(
                status_code=409,
                detail="当前提交无法记录；若已冻结，请保持同一污染声明并刷新后重试。",
            ) from exc

    return app


__all__ = ["create_runner_app"]
