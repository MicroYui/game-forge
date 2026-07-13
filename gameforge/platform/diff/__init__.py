"""Product-level field diff services."""

from gameforge.platform.diff.engine import (
    CollectionIdentity,
    CollectionIdentityInput,
    iter_snapshot_diff_entries,
)
from gameforge.platform.diff.service import (
    SnapshotCanonicalViewRepository,
    SnapshotDiffService,
    SnapshotDiffSlice,
)

__all__ = [
    "CollectionIdentity",
    "CollectionIdentityInput",
    "SnapshotCanonicalViewRepository",
    "SnapshotDiffService",
    "SnapshotDiffSlice",
    "iter_snapshot_diff_entries",
]
