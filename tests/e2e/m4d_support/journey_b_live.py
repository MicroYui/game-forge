"""Loopback-only real API/worker launcher for the M4d Journey-B browser suite."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import ipaddress
from pathlib import Path
import socket
import threading
from urllib.parse import urlsplit

import uvicorn

from gameforge.apps.worker.dispatch import build_worker_process
from gameforge.platform.registry import build_builtin_registry
from gameforge.runtime.clock import SystemUtcClock
from tests.e2e.m4c.test_journey_b import (
    _Harness,
    _registry,
    _role_policy,
    _route,
)
from tests.platform.m4 import apply_testkit


def _retained_harness(workspace: Path) -> _Harness:
    harness = object.__new__(_Harness)
    harness.tmp_path = workspace
    harness.database_url = f"sqlite:///{workspace / 'journey-b.db'}"
    harness.object_root = workspace / "objects"
    harness.telemetry_path = workspace / "telemetry.sqlite3"
    harness.clock = SystemUtcClock()
    harness.registry = _registry()
    harness.route = _route(harness.registry)
    harness.approval_policy = apply_testkit._approval_policy()
    harness.role_policy = _role_policy(harness.registry)
    harness.catalog = build_builtin_registry().list_execution_profile_catalogs()[0]
    return harness


def _load_or_seed(workspace: Path) -> _Harness:
    workspace.mkdir(parents=True, exist_ok=True)
    if (workspace / "journey-b.db").exists():
        return _retained_harness(workspace)
    harness = _Harness(workspace)
    harness.seed_base_snapshot()
    return harness


def _is_loopback_host(host: object) -> bool:
    if host == "localhost":
        return True
    if not isinstance(host, str):
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _install_loopback_egress_guard() -> None:
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection
    original_getaddrinfo = socket.getaddrinfo

    def require_loopback(address: object) -> None:
        if isinstance(address, str):
            return
        if not isinstance(address, tuple) or not address or not _is_loopback_host(address[0]):
            raise RuntimeError("Journey B external network egress is disabled")

    def guarded_connect(instance: socket.socket, address: object) -> None:
        require_loopback(address)
        return original_connect(instance, address)  # type: ignore[arg-type, return-value]

    def guarded_connect_ex(instance: socket.socket, address: object) -> int:
        require_loopback(address)
        return original_connect_ex(instance, address)  # type: ignore[arg-type]

    def guarded_create_connection(address: tuple[str, int], *args: object, **kwargs: object):
        require_loopback(address)
        return original_create_connection(address, *args, **kwargs)

    def guarded_getaddrinfo(host: object, *args: object, **kwargs: object):
        if host is not None and not _is_loopback_host(host):
            raise RuntimeError("Journey B external DNS resolution is disabled")
        return original_getaddrinfo(host, *args, **kwargs)

    def deny_datagram(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("Journey B datagram egress is disabled")

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
    socket.socket.sendmsg = deny_datagram  # type: ignore[method-assign]
    socket.socket.sendto = deny_datagram  # type: ignore[method-assign]
    socket.create_connection = guarded_create_connection
    socket.getaddrinfo = guarded_getaddrinfo


def _run_worker(harness: _Harness, stop: threading.Event) -> None:
    process = build_worker_process(harness.worker_config())

    async def drive() -> None:
        while not stop.is_set():
            claimed = await process.dispatcher.dispatch_once()
            if not claimed:
                await asyncio.sleep(0.05)

    try:
        asyncio.run(drive())
    finally:
        process.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--worker", choices=("disabled", "enabled"), default="enabled")
    parser.add_argument("--web-origin", default="https://127.0.0.1:4173")
    parser.add_argument("--workspace", required=True, type=Path)
    args = parser.parse_args()
    if not _is_loopback_host(args.host):
        raise SystemExit("Journey B launcher accepts only a loopback host")
    web_origin = urlsplit(args.web_origin)
    if (
        web_origin.scheme != "https"
        or not _is_loopback_host(web_origin.hostname)
        or web_origin.port is None
        or web_origin.path not in ("", "/")
        or web_origin.query
        or web_origin.fragment
    ):
        raise SystemExit("Journey B launcher accepts only one loopback HTTPS web origin")

    _install_loopback_egress_guard()
    harness = _load_or_seed(args.workspace.resolve())
    api_config = replace(
        harness.api_config(),
        allowed_websocket_origins=frozenset({args.web_origin.rstrip("/")}),
    )
    from gameforge.apps.api.local import create_readiness_closed_local_app

    app = create_readiness_closed_local_app(api_config)

    stop = threading.Event()
    worker = None
    if args.worker == "enabled":
        worker = threading.Thread(
            target=_run_worker,
            args=(harness, stop),
            daemon=True,
            name="journey-b-worker",
        )
        worker.start()
    try:
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            access_log=False,
            log_level="warning",
            timeout_graceful_shutdown=1,
        )
    finally:
        stop.set()
        if worker is not None:
            worker.join(timeout=30)
            if worker.is_alive():
                raise RuntimeError("Journey B worker did not stop")


if __name__ == "__main__":
    main()
