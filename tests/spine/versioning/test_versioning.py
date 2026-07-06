from gameforge.contracts.lineage import Artifact
from gameforge.spine.versioning.version_tuple import build_version_tuple, artifact_id_for
from gameforge.spine.versioning.store import InMemoryArtifactStore, LineageGraph, RefStore


def _art(kind, vt, lineage, payload="p"):
    aid = artifact_id_for(kind, vt, payload_hash=payload)
    return Artifact(artifact_id=aid, kind=kind, version_tuple=vt, lineage=lineage, payload_hash=payload)


def test_artifact_traces_full_version_tuple():
    vt = build_version_tuple(ir_snapshot_id="sha256:s", seed=0)
    a = _art("ir_snapshot", vt, [])
    store = InMemoryArtifactStore()
    store.put(a)
    prov = LineageGraph(store).provenance(a.artifact_id)
    assert prov.env_contract_version == "env@1" and prov.tool_version.startswith("gameforge@")
    assert prov.ir_snapshot_id == "sha256:s" and prov.seed == 0


def test_lineage_ancestors_transitive():
    store = InMemoryArtifactStore()
    vt = build_version_tuple(ir_snapshot_id="sha256:s")
    ir = _art("ir_snapshot", vt, [])
    store.put(ir)
    cfg = _art("config_export", vt, [ir.artifact_id], payload="c")
    store.put(cfg)
    chk = _art("checker_run", vt, [cfg.artifact_id], payload="k")
    store.put(chk)
    anc = LineageGraph(store).ancestors(chk.artifact_id)
    assert set(anc) == {cfg.artifact_id, ir.artifact_id}


def test_rollback_repoints_and_lineage_still_traceable():
    store = InMemoryArtifactStore()
    refs = RefStore()
    vt = build_version_tuple(ir_snapshot_id="sha256:s")
    v1 = _art("ir_snapshot", vt, [], payload="v1")
    store.put(v1)
    v2 = _art("ir_snapshot", vt, [v1.artifact_id], payload="v2")
    store.put(v2)
    refs.set("head", v2.artifact_id)
    refs.rollback("head", v1.artifact_id)     # pointer re-point (contract §5)
    assert refs.get("head") == v1.artifact_id
    assert store.get(v2.artifact_id) is not None  # immutable, not deleted
    assert refs.history("head") == [v2.artifact_id, v1.artifact_id]
