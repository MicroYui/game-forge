"""Cassette record schema (contract §7) — record/replay isolates nondeterminism."""
from __future__ import annotations

from pydantic import BaseModel

from gameforge.contracts.model_router import ModelResponse, ModelSnapshot
from gameforge.contracts.versions import CASSETTE_SCHEMA_VERSION


class CassetteRecord(BaseModel):
    cassette_schema_version: str = CASSETTE_SCHEMA_VERSION
    request_hash: str
    agent_node_id: str
    model_snapshot: ModelSnapshot
    response: ModelResponse
    recorded_at: str | None = None


class _CassetteMiss:
    """Sentinel returned by CassetteStore.replay when no record exists."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "CASSETTE_MISS"


CASSETTE_MISS = _CassetteMiss()
