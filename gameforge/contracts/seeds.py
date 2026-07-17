"""Canonical deterministic child-seed derivation contracts.

The M4 design freezes one ``subseed@1`` formula for every stochastic child
execution.  Keep the implementation in ``contracts`` so benchmark sampling,
validation handlers, and future producers cannot drift into look-alike hashes.
"""

from __future__ import annotations

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.execution_profiles import ProfileRefV1, RunKindRef

SUBSEED_DERIVATION_VERSION_V1 = "subseed@1"
_UINT64_MAX = (1 << 64) - 1


def derive_subseed_v1(
    *,
    root_seed: int,
    run_kind: RunKindRef,
    profile: ProfileRefV1,
    case_id: str,
    replication_index: int,
) -> int:
    """Return the exact unsigned big-endian first-eight-byte ``subseed@1`` value."""

    if isinstance(root_seed, bool) or not isinstance(root_seed, int):
        raise ValueError("root seed must be an unsigned 64-bit integer")
    if not 0 <= root_seed <= _UINT64_MAX:
        raise ValueError("root seed must be an unsigned 64-bit integer")
    if not isinstance(run_kind, RunKindRef):
        raise TypeError("subseed Run kind must be a RunKindRef")
    if not isinstance(profile, ProfileRefV1):
        raise TypeError("subseed profile must be a ProfileRefV1")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError("subseed case_id must be non-empty")
    if isinstance(replication_index, bool) or not isinstance(replication_index, int):
        raise ValueError("subseed replication_index must be a non-negative integer")
    if replication_index < 0:
        raise ValueError("subseed replication_index must be a non-negative integer")
    digest = canonical_sha256(
        {
            "root_seed": root_seed,
            "run_kind": run_kind.model_dump(mode="json"),
            "profile_id": profile.profile_id,
            "profile_version": profile.version,
            "case_id": case_id,
            "replication_index": replication_index,
        }
    )
    return int.from_bytes(bytes.fromhex(digest[:16]), byteorder="big", signed=False)


__all__ = ["SUBSEED_DERIVATION_VERSION_V1", "derive_subseed_v1"]
