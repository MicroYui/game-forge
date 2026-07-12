from gameforge.contracts.agent_io import (
    AgentNodeResult,
    M2_AGENT_IO_SCHEMA_VERSION,
    PatchDraft,
    TriagedFindings,
)


def test_agent_node_result_tracks_llm_calls():
    r = AgentNodeResult(role="triage", model_run_id="run1", request_hashes=["sha256:a", "sha256:b"])
    assert r.agent_io_schema_version == "agent-io@2"
    assert M2_AGENT_IO_SCHEMA_VERSION == "agent-io@1"
    assert r.request_hashes == ["sha256:a", "sha256:b"]
    assert r.fallback_taken is False


def test_all_six_roles_have_output_models():
    # 字段一次定全：即便 M2a 不实现 playtest，其 I/O 也可构造
    from gameforge.contracts.agent_io import (
        ConsistencyHints, ContentProposal, EntityConstraintProposals,
        PlaytestReport,
    )
    assert EntityConstraintProposals().proposals == []
    assert TriagedFindings().clusters == []
    assert PatchDraft.model_fields["passed_verification"].default is False
    assert ConsistencyHints().hints == []
    assert ContentProposal().passed_gate is False
    assert PlaytestReport().completed is False
