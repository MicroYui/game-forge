"""Cassette store — deterministic record/replay of LLM responses (contract §7).

Flat layout: <root>/<hex>.json where hex = request_hash without the "sha256:"
prefix. request_hash already encodes agent_node_id, so the hash alone is a
unique O(1) key; the record body keeps agent_node_id for human browsing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal, overload

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.cassette import (
    CASSETTE_MISS,
    CassetteRecord,
    CassetteRecordV2,
    _CassetteMiss,
    parse_cassette_record,
)
from gameforge.contracts.errors import IntegrityViolation


CassetteWireRecord = CassetteRecord | CassetteRecordV2
NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=512)]


class CassetteRouteKey(BaseModel):
    """Operational address for one M4-native route recording.

    The public cassette payload remains ``cassette@2``. This key prevents the
    legacy request-hash layout from collapsing repeated calls or independent
    Runs onto the same mutable file.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    key_schema_version: Literal["cassette-route-key@1"] = "cassette-route-key@1"
    run_id: NonEmptyStr
    attempt_no: int = Field(ge=1)
    call_ordinal: int = Field(ge=1)
    route_ordinal: int = Field(ge=1)
    routing_decision_id: NonEmptyStr


class CassetteStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def _path(self, request_hash: str) -> Path:
        hex_part = request_hash.split(":", 1)[-1]
        return self._root / f"{hex_part}.json"

    def _native_path(self, key: CassetteRouteKey) -> Path:
        digest = canonical_sha256(key.model_dump(mode="json"))
        return self._root / "native" / f"{digest}.json"

    @overload
    def record(self, rec: CassetteWireRecord) -> None: ...

    @overload
    def record(self, request_hash: str, rec: CassetteWireRecord) -> None: ...

    def record(
        self,
        request_hash: str | CassetteWireRecord,
        rec: CassetteWireRecord | None = None,
    ) -> None:
        if rec is None:
            if isinstance(request_hash, str):
                raise TypeError("record payload is required when request_hash is explicit")
            record = request_hash
            key = record.request_hash
        else:
            if not isinstance(request_hash, str):
                raise TypeError("explicit cassette request_hash must be a string")
            record = rec
            key = request_hash
        if key != record.request_hash:
            raise IntegrityViolation(
                "cassette record request hash differs from storage key",
                storage_key=key,
                record_request_hash=record.request_hash,
            )
        self._root.mkdir(parents=True, exist_ok=True)
        self._path(key).write_text(
            json.dumps(
                record.model_dump(mode="json"),
                sort_keys=True,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def replay(self, request_hash: str) -> CassetteWireRecord | _CassetteMiss:
        path = self._path(request_hash)
        if not path.exists():
            return CASSETTE_MISS
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = parse_cassette_record(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise IntegrityViolation("unsupported or invalid cassette wire record") from exc
        if record.request_hash != request_hash:
            raise IntegrityViolation(
                "cassette wire request hash differs from lookup key",
                lookup_key=request_hash,
                record_request_hash=record.request_hash,
            )
        return record

    def record_native(self, key: CassetteRouteKey, record: CassetteRecordV2) -> None:
        self._validate_native_binding(key, record)
        self._write_immutable(
            self._native_path(key),
            {
                "storage_schema_version": "native-cassette-record@1",
                "route_key": key.model_dump(mode="json"),
                "record": record.model_dump(mode="json"),
            },
            conflict_message="native cassette route already has conflicting content",
        )

    def replay_native(
        self,
        key: CassetteRouteKey,
    ) -> CassetteRecordV2 | _CassetteMiss:
        path = self._native_path(key)
        if not path.exists():
            return CASSETTE_MISS
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("native cassette envelope must be an object")
            if payload.get("storage_schema_version") != "native-cassette-record@1":
                raise ValueError("unsupported native cassette envelope")
            stored_key = CassetteRouteKey.model_validate(payload.get("route_key"))
            record_payload = payload.get("record")
            if not isinstance(record_payload, dict):
                raise ValueError("native cassette record must be an object")
            record = parse_cassette_record(record_payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise IntegrityViolation("unsupported or invalid native cassette record") from exc
        if stored_key != key:
            raise IntegrityViolation("native cassette route key differs from lookup key")
        if not isinstance(record, CassetteRecordV2):
            raise IntegrityViolation("native cassette authority requires cassette@2")
        self._validate_native_binding(key, record)
        return record

    @staticmethod
    def _validate_native_binding(
        key: CassetteRouteKey,
        record: CassetteRecordV2,
    ) -> None:
        decision = record.routing_decision
        if (
            key.run_id != decision.run_id
            or key.attempt_no != decision.attempt_no
            or key.routing_decision_id != decision.decision_id
        ):
            raise IntegrityViolation("native cassette route key differs from record decision")

    @staticmethod
    def _write_immutable(
        path: Path,
        payload: object,
        *,
        conflict_message: str,
    ) -> None:
        wire = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            indent=2,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(wire)
        except FileExistsError:
            try:
                existing = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise IntegrityViolation("existing cassette content is unreadable") from exc
            if existing != wire:
                raise IntegrityViolation(conflict_message)


__all__ = ["CassetteRouteKey", "CassetteStore", "CassetteWireRecord"]
