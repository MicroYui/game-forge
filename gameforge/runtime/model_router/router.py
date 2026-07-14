"""Model Router (contract §7) — RECORD/REPLAY/PASSTHROUGH over a transport + cassette.

REPLAY is the CI/test mode: zero live calls, deterministic. RECORD hits the live
transport and writes cassettes. Reproducibility = same request_hash + REPLAY ->
same ModelResponse (PRD §5.5: 只承诺回放复现).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from enum import Enum

from gameforge.contracts.cassette import CASSETTE_MISS, CassetteRecord
from gameforge.contracts.errors import QuotaExceeded
from gameforge.contracts.model_router import (
    ModelRequest,
    ModelResponse,
    ModelSnapshot,
    request_hash,
)
from gameforge.runtime.cassette.store import CassetteStore
from gameforge.runtime.model_router.transport import LlmTransport
from gameforge.runtime.reliability.breaker import BreakerPermit, CircuitBreaker
from gameforge.runtime.reliability.retry import RetryAttemptResult, RetryExecutor


class RouterMode(str, Enum):
    RECORD = "record"
    REPLAY = "replay"
    PASSTHROUGH = "passthrough"


class CassetteReplayMiss(Exception):
    def __init__(self, request_hash: str) -> None:
        super().__init__(f"cassette miss on REPLAY for {request_hash}")
        self.request_hash = request_hash


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
        retry_executor: RetryExecutor[ModelResponse] | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        attempt_admission: Callable[[int], None] | None = None,
        attempt_cancellation: Callable[[int], None] | None = None,
        attempt_observer: Callable[[RetryAttemptResult], None] | None = None,
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
        self._retry_executor = retry_executor
        self._circuit_breaker = circuit_breaker
        self._attempt_admission = attempt_admission
        self._attempt_cancellation = attempt_cancellation
        self._attempt_observer = attempt_observer
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

    def call(
        self,
        req: ModelRequest,
        *,
        deadline_utc: datetime | None = None,
    ) -> ModelResponse:
        if self._retry_executor is not None and deadline_utc is None:
            raise ValueError("typed retry execution requires a total UTC deadline")
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
        if self._retry_executor is None:
            resp, transport_attempts = self._complete_with_retry(req)
        else:
            assert deadline_utc is not None
            resp, transport_attempts = self._complete_with_typed_retry(
                req,
                deadline_utc=deadline_utc,
            )

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

    def _complete_with_typed_retry(
        self,
        req: ModelRequest,
        *,
        deadline_utc: datetime,
    ) -> tuple[ModelResponse, int]:
        assert self._retry_executor is not None
        permits: dict[int, BreakerPermit] = {}
        attempts_started = 0

        def admit(attempt_no: int) -> None:
            nonlocal attempts_started
            permit = (
                self._circuit_breaker.before_call() if self._circuit_breaker is not None else None
            )
            if permit is not None:
                permits[attempt_no] = permit
            try:
                if self._max_calls is not None and self._live_calls >= self._max_calls:
                    raise QuotaExceeded(f"live-call quota {self._max_calls} exhausted")
                if self._attempt_admission is not None:
                    self._attempt_admission(attempt_no)
            except BaseException:
                retained = permits.pop(attempt_no, None)
                if retained is not None and self._circuit_breaker is not None:
                    self._circuit_breaker.cancel(retained)
                raise

        def cancel(attempt_no: int) -> None:
            permit = permits.pop(attempt_no, None)
            if permit is not None and self._circuit_breaker is not None:
                self._circuit_breaker.cancel(permit)
            if self._attempt_cancellation is not None:
                self._attempt_cancellation(attempt_no)

        def complete(attempt_no: int) -> ModelResponse:
            nonlocal attempts_started
            self._live_calls += 1
            attempts_started += 1
            complete_with_timeout = getattr(self._transport, "complete_with_timeout", None)
            if callable(complete_with_timeout):
                timeout_s = self._retry_executor.remaining_deadline_s(deadline_utc)
                return complete_with_timeout(req, timeout_s=timeout_s)
            return self._transport.complete(req)

        def observe(result: RetryAttemptResult) -> None:
            permit = permits.pop(result.attempt_no, None)
            if permit is not None and self._circuit_breaker is not None:
                if result.succeeded:
                    self._circuit_breaker.record_success(permit)
                else:
                    assert result.classification is not None
                    self._circuit_breaker.record_failure(permit, result.classification)
            if self._attempt_observer is not None:
                self._attempt_observer(result)

        response = self._retry_executor.run(
            complete,
            idempotent=True,
            deadline_utc=deadline_utc,
            reserve_attempt=admit,
            cancel_attempt=cancel,
            observe_attempt=observe,
        )
        return response, attempts_started

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
        raise RuntimeError(f"transport failed after {self._max_retries + 1} attempts: {last}")
