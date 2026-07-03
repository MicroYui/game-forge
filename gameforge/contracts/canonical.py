"""Canonical JSON + content-addressed snapshot id (contract §2.4).

canonical_json rules:
  1. object keys sorted lexicographically;
  2. drop null (None) optional fields;
  3. ordered-semantic arrays keep order; unordered collections are represented as
     dicts keyed by id upstream, so `sort_keys` orders them deterministically;
  4. floats rendered as a stable decimal string (no scientific/platform drift);
  5. the content_payload passed in must already exclude non-content fields
     (created_at / author / snapshot_id / parent_id).
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Mapping


def _canon(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {k: _canon(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [_canon(v) for v in obj]
    if isinstance(obj, bool):  # bool is a subclass of int; keep it a JSON bool
        return obj
    if isinstance(obj, float):
        # Rule 4: stable decimal string; 1.10 == 1.1, and floats are tagged so
        # 1 (int) and 1.0 (float) never collide.
        return "f:" + format(Decimal(str(obj)).normalize(), "f")
    return obj


def canonical_json(payload: Any) -> str:
    return json.dumps(
        _canon(payload),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def compute_snapshot_id(content_payload: Mapping) -> str:
    digest = hashlib.sha256(canonical_json(content_payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"
