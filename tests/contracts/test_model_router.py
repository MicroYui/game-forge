from gameforge.contracts.model_router import (
    Message, ModelRequest, ModelSnapshot, ToolSchemaRef, request_hash,
)


def _req(**over):
    base = dict(
        model_snapshot=ModelSnapshot(provider="anthropic", model="opus4.8", snapshot_tag="s1"),
        messages=[Message(role="user", content="hi")],
        params={"temperature": 0.0},
        agent_node_id="triage",
        prompt_version="triage@1",
    )
    base.update(over)
    return ModelRequest(**base)


def test_request_hash_is_deterministic_and_prefixed():
    assert request_hash(_req()) == request_hash(_req())
    assert request_hash(_req()).startswith("sha256:")


def test_request_hash_excludes_cache_key_and_schema_version():
    # cache_key is a routing hint, not part of what determines the model output
    assert request_hash(_req(cache_key="abc")) == request_hash(_req(cache_key=None))
    assert request_hash(_req(model_router_schema_version="model-router@99")) == request_hash(_req())


def test_request_hash_changes_with_semantic_fields():
    assert request_hash(_req()) != request_hash(_req(prompt_version="triage@2"))
    assert request_hash(_req()) != request_hash(
        _req(messages=[Message(role="user", content="different")])
    )
    assert request_hash(_req()) != request_hash(
        _req(tool_schemas=[ToolSchemaRef(name="patch", version="patch@1")])
    )
    from gameforge.contracts.model_router import ModelSnapshot
    assert request_hash(_req()) != request_hash(
        _req(model_snapshot=ModelSnapshot(provider="anthropic", model="opus4.8", snapshot_tag="s2"))
    )
    assert request_hash(_req()) != request_hash(_req(params={"temperature": 0.7}))
    assert request_hash(_req()) != request_hash(_req(agent_node_id="repair"))


def test_model_response_defaults():
    from gameforge.contracts.model_router import ModelResponse
    r = ModelResponse(response_normalized="x")
    assert r.token_usage == {} and r.tool_calls == [] and r.finish_reason == ""
