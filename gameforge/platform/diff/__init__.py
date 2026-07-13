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
from gameforge.platform.diff.three_way import (
    ThreeWayMergePlan,
    compute_three_way_merge,
    resolve_three_way_merge,
)

__all__ = [
    "CollectionIdentity",
    "CollectionIdentityInput",
    "SnapshotCanonicalViewRepository",
    "SnapshotDiffService",
    "SnapshotDiffSlice",
    "ThreeWayMergePlan",
    "compute_three_way_merge",
    "iter_snapshot_diff_entries",
    "resolve_three_way_merge",
]
