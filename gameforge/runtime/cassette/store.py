"""Cassette store — deterministic record/replay of LLM responses (contract §7).

Flat layout: <root>/<hex>.json where hex = request_hash without the "sha256:"
prefix. request_hash already encodes agent_node_id, so the hash alone is a
unique O(1) key; the record body keeps agent_node_id for human browsing.
"""
from __future__ import annotations

import json
from pathlib import Path

from gameforge.contracts.cassette import CASSETTE_MISS, CassetteRecord, _CassetteMiss


class CassetteStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def _path(self, request_hash: str) -> Path:
        hex_part = request_hash.split(":", 1)[-1]
        return self._root / f"{hex_part}.json"

    def record(self, rec: CassetteRecord) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(rec.request_hash)
        path.write_text(
            json.dumps(rec.model_dump(), sort_keys=True, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def replay(self, request_hash: str) -> CassetteRecord | _CassetteMiss:
        path = self._path(request_hash)
        if not path.exists():
            return CASSETTE_MISS
        return CassetteRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))
