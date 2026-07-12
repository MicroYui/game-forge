"""Cassette record schema (contract §7) — record/replay isolates nondeterminism."""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from gameforge.contracts.model_router import ModelResponse, ModelSnapshot
from gameforge.contracts.versions import CASSETTE_SCHEMA_VERSION


class CassetteRecord(BaseModel):
    cassette_schema_version: str = CASSETTE_SCHEMA_VERSION
    request_hash: str
    agent_node_id: str
    model_snapshot: ModelSnapshot
    response: ModelResponse
    transport_attempts: int | None = Field(default=None, ge=1)
    transport_retries: int | None = Field(default=None, ge=0)
    recorded_at: str | None = None

    @model_validator(mode="after")
    def validate_transport_attempts(self) -> CassetteRecord:
        attempts_missing = self.transport_attempts is None
        retries_missing = self.transport_retries is None
        if attempts_missing != retries_missing:
            raise ValueError("cassette transport attempts and retries must appear together")
        if (
            self.transport_attempts is not None
            and self.transport_retries != self.transport_attempts - 1
        ):
            raise ValueError("cassette transport retries must equal attempts - 1")
        return self


class _CassetteMiss:
    """Sentinel returned by CassetteStore.replay when no record exists."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "CASSETTE_MISS"


CASSETTE_MISS = _CassetteMiss()
