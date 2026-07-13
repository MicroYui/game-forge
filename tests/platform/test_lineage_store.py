"""SQLAlchemy-backed artifact/ref store tests (contract §5, §12A.3, Task 13).

Mirrors `tests/spine/versioning/test_versioning.py`'s behavioral contract
(`InMemoryArtifactStore`/`RefStore`) but drives the SQL-backed
`SqlArtifactStore`/`SqlRefStore` against a real (tmp file) sqlite DB, proving
the full `Artifact` (including `version_tuple`/`lineage`) round-trips through
the Task 12 schema and that `ancestors` matches `spine`'s in-memory
`LineageGraph` semantics.
"""

import pytest

from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.lineage import (
    Artifact,
    VersionTuple,
    build_artifact_v2,
    object_ref_for_bytes,
)
from gameforge.platform.lineage.store import SqlArtifactStore, SqlRefStore
from gameforge.runtime.persistence.engine import get_engine, get_sessionmaker
from gameforge.runtime.persistence.models import Base, RefHistoryRow, RefRow


def _sf(tmp_path):
    url = f"sqlite:///{tmp_path / 'l.db'}"
    Base.metadata.create_all(get_engine(url))
    return get_sessionmaker(get_engine(url))


def test_sql_artifact_put_get_and_ancestors(tmp_path):
    sf = _sf(tmp_path)
    store = SqlArtifactStore(sf)
    vt = VersionTuple(ir_snapshot_id="sha256:s")
    ir = Artifact(artifact_id="a_ir", kind="ir_snapshot", version_tuple=vt, lineage=[])
    cfg = Artifact(artifact_id="a_cfg", kind="config_export", version_tuple=vt, lineage=["a_ir"])
    store.put(ir)
    store.put(cfg)
    assert store.get("a_cfg").lineage == ["a_ir"]
    assert store.ancestors("a_cfg") == ["a_ir"]


def test_sql_artifact_put_is_idempotent(tmp_path):
    sf = _sf(tmp_path)
    store = SqlArtifactStore(sf)
    vt = VersionTuple(ir_snapshot_id="sha256:s")
    ir = Artifact(artifact_id="a_ir", kind="ir_snapshot", version_tuple=vt, lineage=[])
    store.put(ir)
    store.put(ir)  # re-put same artifact_id must not raise / must not duplicate
    assert len(store.all()) == 1


def test_sql_artifact_put_rejects_same_id_with_changed_content(tmp_path):
    sf = _sf(tmp_path)
    store = SqlArtifactStore(sf)
    vt = VersionTuple(ir_snapshot_id="sha256:s")
    original = Artifact(
        artifact_id="a_ir",
        kind="ir_snapshot",
        version_tuple=vt,
        lineage=[],
        meta={"revision": 1},
    )
    changed = original.model_copy(update={"meta": {"revision": 2}})
    store.put(original)

    with pytest.raises(IntegrityViolation, match="immutable content"):
        store.put(changed)

    assert store.get("a_ir") == original


def test_sql_artifact_store_fails_closed_for_v2_without_object_store_binding(tmp_path):
    store = SqlArtifactStore(_sf(tmp_path))
    ref = object_ref_for_bytes(b"payload")
    artifact = build_artifact_v2(
        kind="ir_snapshot",
        version_tuple=VersionTuple(ir_snapshot_id=f"sha256:{ref.sha256}"),
        lineage=(),
        payload_hash=ref.sha256,
        object_ref=ref,
    )

    with pytest.raises(IntegrityViolation, match="active ObjectBinding"):
        store.put(artifact)

    assert store.get(artifact.artifact_id) is None


def test_sql_artifact_round_trips_full_fields(tmp_path):
    sf = _sf(tmp_path)
    store = SqlArtifactStore(sf)
    vt = VersionTuple(
        ir_snapshot_id="sha256:s",
        doc_version="doc@1",
        seed=42,
    )
    artifact = Artifact(
        artifact_id="a_full",
        kind="checker_run",
        version_tuple=vt,
        lineage=["a_ir"],
        payload_hash="sha256:payload",
        created_at="2026-07-06T00:00:00Z",
        meta={"note": "hello"},
    )
    store.put(artifact)
    got = store.get("a_full")
    assert got == artifact
    assert got.version_tuple.seed == 42
    assert got.lineage_schema_version == artifact.lineage_schema_version


def test_sql_artifact_get_missing_returns_none(tmp_path):
    store = SqlArtifactStore(_sf(tmp_path))
    assert store.get("nope") is None


def test_sql_artifact_ancestors_transitive(tmp_path):
    sf = _sf(tmp_path)
    store = SqlArtifactStore(sf)
    vt = VersionTuple(ir_snapshot_id="sha256:s")
    a = Artifact(artifact_id="a", kind="ir_snapshot", version_tuple=vt, lineage=[])
    b = Artifact(artifact_id="b", kind="config_export", version_tuple=vt, lineage=["a"])
    c = Artifact(artifact_id="c", kind="checker_run", version_tuple=vt, lineage=["b"])
    for art in (a, b, c):
        store.put(art)
    assert store.ancestors("c") == ["a", "b"]


def test_sql_ref_rollback_keeps_history(tmp_path):
    refs = SqlRefStore(_sf(tmp_path))
    refs.set("head", "v2")
    refs.rollback("head", "v1")
    assert refs.get("head") == "v1" and refs.history("head") == ["v2", "v1"]


def test_sql_ref_get_missing_returns_none(tmp_path):
    refs = SqlRefStore(_sf(tmp_path))
    assert refs.get("nope") is None


def test_sql_ref_persists_across_store_instances(tmp_path):
    sf = _sf(tmp_path)
    SqlRefStore(sf).set("head", "v1")
    # A brand new SqlRefStore over the same session factory sees the write.
    assert SqlRefStore(sf).get("head") == "v1"


def test_sql_ref_legacy_facade_keeps_revision_equal_to_history_sequence(tmp_path):
    sf = _sf(tmp_path)
    refs = SqlRefStore(sf)
    refs.set("head", "v1")
    refs.set("head", "v2")

    with sf() as session:
        current = session.get(RefRow, "head")
        history = (
            session.query(RefHistoryRow)
            .filter(RefHistoryRow.name == "head")
            .order_by(RefHistoryRow.seq)
            .all()
        )

    assert current is not None
    assert (current.artifact_id, current.revision) == ("v2", 2)
    assert [(row.artifact_id, row.seq) for row in history] == [("v1", 1), ("v2", 2)]
