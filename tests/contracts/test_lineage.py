from gameforge.contracts.lineage import VersionTuple, Artifact, AuditRecord


def test_version_tuple_all_fields_present_optional():
    vt = VersionTuple(ir_snapshot_id="sha256:x", env_contract_version="env@1", seed=0)
    d = vt.model_dump()
    for f in ["doc_version", "ir_snapshot_id", "constraint_snapshot_id", "prompt_version",
              "model_snapshot", "agent_graph_version", "tool_version",
              "env_contract_version", "seed", "cassette_id"]:
        assert f in d
    assert d["constraint_snapshot_id"] is None  # not produced until M1 — declared, not cut


def test_artifact_defaults_and_lineage():
    a = Artifact(artifact_id="a1", kind="ir_snapshot", version_tuple=VersionTuple(),
                 lineage=["parent1"])
    assert a.lineage_schema_version == "lineage@1" and a.lineage == ["parent1"]


def test_audit_record_hash_chain_fields():
    r = AuditRecord(seq=1, actor="cli", action="record_artifact", artifact_id="a1",
                    ts="2026-07-06T00:00:00Z", content_hash="sha256:h", prev_hash=None)
    assert r.audit_schema_version == "audit@1" and r.seq == 1
