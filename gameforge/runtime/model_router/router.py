"""Model Router (contract §7) — RECORD/REPLAY/PASSTHROUGH over a transport + cassette.

REPLAY is the CI/test mode: zero live calls, deterministic. RECORD hits the live
transport and writes cassettes. Reproducibility = same request_hash + REPLAY ->
same ModelResponse (PRD §5.5: 只承诺回放复现).
"""
from __future__ import annotations

import time
from enum import Enum

from gameforge.contracts.cassette import CASSETTE_MISS, CassetteRecord
from gameforge.contracts.model_router import (
    ModelRequest,
    ModelResponse,
    ModelSnapshot,
    request_hash,
)
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.transport import LlmTransport


class RouterMode(str, Enum):
    RECORD = "record"
    REPLAY = "replay"
    PASSTHROUGH = "passthrough"


class CassetteReplayMiss(Exception):
    def __init__(self, request_hash: str) -> None:
        super().__init__(f"cassette miss on REPLAY for {request_hash}")
        self.request_hash = request_hash


class QuotaExceeded(Exception):
    pass


class ModelRouter:
    def __init__(
        self,
        transport: LlmTransport,
        store: CassetteStore,
        mode: RouterMode = RouterMode.REPLAY,
        max_retries: int = 2,
        max_calls: int | None = None,
        resume: bool = False,
        retry_backoff_s: float = 0.0,
        default_model_snapshot: ModelSnapshot | None = None,
    ) -> None:
        self._transport = transport
        self._store = store
        self._mode = mode
        self._max_retries = max_retries
        self._max_calls = max_calls
        # Exponential wait (retry_backoff_s * 2**attempt) between transport
        # retries. 0.0 (default) = immediate retries, unchanged behavior. A
        # positive value lets a long RECORD ride through a flapping gateway
        # (transient 500s) instead of aborting the whole run.
        self._retry_backoff_s = retry_backoff_s
        self._default_model_snapshot = default_model_snapshot
        # RECORD + resume: reuse any cassette already on disk instead of
        # re-calling the transport, so an interrupted multi-thousand-call record
        # pass can be restarted and only records the still-missing requests.
        self._resume = resume
        self._live_calls = 0
        self._session_cache: dict[str, ModelResponse] = {}

    @property
    def default_model_snapshot(self) -> ModelSnapshot | None:
        """Session-level model policy used when an Agent node has no override."""
        return self._default_model_snapshot

    def call(self, req: ModelRequest) -> ModelResponse:
        h = request_hash(req)
        if h in self._session_cache:
            return self._session_cache[h]

        if self._mode is RouterMode.REPLAY:
            rec = self._store.replay(h)
            if rec is CASSETTE_MISS:
                raise CassetteReplayMiss(h)
            self._session_cache[h] = rec.response
            return rec.response

        # RECORD / PASSTHROUGH → live transport
        if self._mode is RouterMode.RECORD and self._resume:
            rec = self._store.replay(h)
            if rec is not CASSETTE_MISS:
                self._session_cache[h] = rec.response
                return rec.response
        resp, transport_attempts = self._complete_with_retry(req)

        if self._mode is RouterMode.RECORD:
            self._store.record(
                CassetteRecord(
                    request_hash=h,
                    agent_node_id=req.agent_node_id,
                    model_snapshot=req.model_snapshot,
                    response=resp,
                    transport_attempts=transport_attempts,
                    transport_retries=transport_attempts - 1,
                )
            )
        self._session_cache[h] = resp
        return resp

    def _complete_with_retry(self, req: ModelRequest) -> tuple[ModelResponse, int]:
        last: Exception | None = None
        attempts = self._max_retries + 1
        for attempt in range(attempts):
            if self._max_calls is not None and self._live_calls >= self._max_calls:
                raise QuotaExceeded(f"live-call quota {self._max_calls} exhausted")
            self._live_calls += 1  # count EVERY real transport attempt (retries included)
            try:
                return self._transport.complete(req), attempt + 1
            except Exception as exc:  # transient gateway errors → retry then degrade
                last = exc
                if self._retry_backoff_s > 0 and attempt < attempts - 1:
                    time.sleep(self._retry_backoff_s * (2**attempt))
        raise RuntimeError(
            f"transport failed after {self._max_retries + 1} attempts: {last}"
        )
