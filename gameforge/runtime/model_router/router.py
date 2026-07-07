"""Model Router (contract §7) — RECORD/REPLAY/PASSTHROUGH over a transport + cassette.

REPLAY is the CI/test mode: zero live calls, deterministic. RECORD hits the live
transport and writes cassettes. Reproducibility = same request_hash + REPLAY ->
same ModelResponse (PRD §5.5: 只承诺回放复现).
"""
from __future__ import annotations

from enum import Enum

from gameforge.contracts.cassette import CASSETTE_MISS, CassetteRecord
from gameforge.contracts.model_router import ModelRequest, ModelResponse, request_hash
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
    ) -> None:
        self._transport = transport
        self._store = store
        self._mode = mode
        self._max_retries = max_retries
        self._max_calls = max_calls
        self._live_calls = 0
        self._session_cache: dict[str, ModelResponse] = {}

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
        resp = self._complete_with_retry(req)

        if self._mode is RouterMode.RECORD:
            self._store.record(
                CassetteRecord(
                    request_hash=h,
                    agent_node_id=req.agent_node_id,
                    model_snapshot=req.model_snapshot,
                    response=resp,
                )
            )
        self._session_cache[h] = resp
        return resp

    def _complete_with_retry(self, req: ModelRequest) -> ModelResponse:
        last: Exception | None = None
        for _ in range(self._max_retries + 1):
            if self._max_calls is not None and self._live_calls >= self._max_calls:
                raise QuotaExceeded(f"live-call quota {self._max_calls} exhausted")
            self._live_calls += 1  # count EVERY real transport attempt (retries included)
            try:
                return self._transport.complete(req)
            except Exception as exc:  # transient gateway errors → retry then degrade
                last = exc
        raise RuntimeError(
            f"transport failed after {self._max_retries + 1} attempts: {last}"
        )
