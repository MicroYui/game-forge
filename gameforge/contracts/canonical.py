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
import math
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


def _typed_json_projection(value: Any) -> list[Any]:
    """Project a JSON value into a collision-free, explicitly tagged tree."""

    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("typed canonical JSON requires finite floats")
        return ["float64", value.hex()]
    if isinstance(value, str):
        return ["string", value]
    if isinstance(value, Mapping):
        entries: list[list[Any]] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("typed canonical JSON object keys must be strings")
            entries.append([key, _typed_json_projection(item)])
        entries.sort(key=lambda entry: entry[0])
        return ["object", entries]
    if isinstance(value, (list, tuple)):
        return ["array", [_typed_json_projection(item) for item in value]]
    raise TypeError(f"typed canonical JSON does not support {type(value).__qualname__}")


def typed_canonical_json(payload: Any) -> str:
    """Canonicalize JSON without collapsing type, presence, or signed zero.

    This additive M4 encoding is deliberately separate from ``canonical_json``:
    historical snapshot and artifact identities retain their frozen null-dropping
    and decimal-float behavior. Every JSON value is projected to a disjoint type
    tag; object entries are key-sorted and finite floats use Python's stable
    IEEE-754 hexadecimal representation.
    """

    return json.dumps(
        _typed_json_projection(payload),
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_lowerhex(payload: bytes | bytearray | memoryview) -> str:
    """Return the canonical bare SHA-256 representation used by M4 digests.

    Artifact ids and other namespaced ids keep their historical ``sha256:``
    prefix.  M4 integrity fields such as ``ObjectRef.sha256`` and audit content
    hashes use exactly 64 lowercase hexadecimal characters instead.
    """

    return hashlib.sha256(bytes(payload)).hexdigest()


def canonical_sha256(payload: Any) -> str:
    """Hash canonical JSON as a bare, lowercase SHA-256 digest."""

    return sha256_lowerhex(canonical_json(payload).encode("utf-8"))


def compute_snapshot_id(content_payload: Mapping) -> str:
    digest = canonical_sha256(content_payload)
    return f"sha256:{digest}"
