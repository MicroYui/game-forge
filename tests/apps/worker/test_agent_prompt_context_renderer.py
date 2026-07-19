from __future__ import annotations

from dataclasses import replace

import pytest

from gameforge.apps.worker.agent_prompt_context import (
    agent_prompt_context_binding_plan_keys,
    bind_production_agent_prompt_context_authority,
    build_agent_prompt_context_source_renderer,
    build_builtin_agent_prompt_context_authority,
    render_agent_prompt_context_sources,
)
from gameforge.apps.worker.prompt_rendering import (
    CanonicalPolicyInjectedParamV1,
    CanonicalPromptBindingV1,
    CanonicalPromptRendererAuthority,
    CanonicalPromptSourceShapeV1,
    CanonicalPromptSourceSlotV1,
    CanonicalPromptSourceV1,
    CanonicalToolSchemaSetRefV1,
    build_tool_schema_set,
)
from gameforge.contracts.canonical import canonical_json, sha256_lowerhex
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import (
    AgentPromptArtifactBindingV1,
    AgentPromptContextV1,
    AgentPromptContextKind,
    AgentPromptSourceMessageV1,
)
from gameforge.contracts.lineage import VersionTuple, build_artifact_v2, object_ref_for_bytes
from gameforge.contracts.model_router import Message, ModelRequestV2, ModelSnapshot
from gameforge.contracts.provenance import OriginRefV1, ProvenanceV1
from gameforge.agents.prompts.registry import get_prompt
from gameforge.platform.provenance import build_source_kind_registry


NODE = "review-triage"
PROMPT = "review-triage@1"


def _context_source(
    *,
    node: str = NODE,
    prompt: str = PROMPT,
    user: str = "Triage this exact finding.",
    include_tool: bool = True,
    context_kind: AgentPromptContextKind | None = None,
) -> tuple[CanonicalPromptSourceV1, ...]:
    upstream_hash = "a" * 64
    context = AgentPromptContextV1(
        context_kind=(
            context_kind
            if context_kind is not None
            else "repair_initial"
            if node == "repair"
            else "review_triage"
        ),
        run_id="run:1",
        attempt_no=1,
        target_call_ordinal=1,
        agent_node_id=node,
        prompt_version=prompt,
        messages=(
            AgentPromptSourceMessageV1(
                role="user",
                content=user,
                purpose="context",
            ),
            *(
                (
                    AgentPromptSourceMessageV1(
                        role="tool",
                        content="checker: dangling_ref",
                        tool_calls=({"name": "checker_result", "status": "failed"},),
                        purpose="tool_output",
                    ),
                )
                if include_tool
                else ()
            ),
        ),
        upstream_artifacts=(
            AgentPromptArtifactBindingV1(
                binding_key="source:0001",
                artifact_id="artifact:upstream",
                artifact_kind="ir_snapshot",
                payload_schema_id="ir-core@1",
                payload_hash=upstream_hash,
            ),
        ),
    )
    payload = canonical_json(context.model_dump(mode="json")).encode()
    digest = sha256_lowerhex(payload)
    provenance = ProvenanceV1(
        source_kind_registry_version=1,
        source_kind_id="tool_output",
        origin_ref=OriginRefV1(
            opaque_source_id="agent-context:run:1:1",
            source_revision=digest,
        ),
        parent_source_artifact_ids=("artifact:upstream",),
        connector_id="agent-prompt-context@1",
        connector_version="1",
        trust="trusted_internal",
        source_hash=digest,
    )
    artifact = build_artifact_v2(
        kind="source_raw",
        version_tuple=VersionTuple(doc_version=digest, tool_version="agent-prompt-context@1"),
        lineage=("artifact:upstream",),
        payload_hash=digest,
        object_ref=object_ref_for_bytes(payload),
        meta={
            "payload_schema_id": "agent-prompt-context@1",
            "provenance": provenance.model_dump(mode="json"),
            "producer_run_id": context.run_id,
            "producer_attempt_no": context.attempt_no,
            "target_call_ordinal": context.target_call_ordinal,
            "agent_node_id": node,
            "prompt_version": prompt,
            "replayability": "online_only",
        },
    )
    return (
        CanonicalPromptSourceV1(
            slot_id="agent_context",
            artifact=artifact,
            payload=payload,
            provenance=provenance,
            source_kind=build_source_kind_registry().get("tool_output"),  # type: ignore[arg-type]
        ),
    )


def _authority() -> CanonicalPromptRendererAuthority:
    schemas = build_tool_schema_set(tool_version="review-triage@1", tool_schemas=())
    return CanonicalPromptRendererAuthority(
        source_kind_registries=(build_source_kind_registry(),),
        tool_schema_sets=(schemas,),
        bindings=(
            CanonicalPromptBindingV1(
                binding_id="review-triage-context@1",
                agent_node_id=NODE,
                prompt_version=PROMPT,
                renderer_version="agent-prompt-context-renderer@1",
                source_slots=(
                    CanonicalPromptSourceSlotV1(
                        slot_id="agent_context",
                        min_count=1,
                        max_count=1,
                        max_bytes=1024 * 1024,
                        allowed_shapes=(
                            CanonicalPromptSourceShapeV1(
                                artifact_kind="source_raw",
                                payload_schema_id="agent-prompt-context@1",
                                source_kind_registry_version=1,
                                source_kind_id="tool_output",
                            ),
                        ),
                        allowed_prompt_purposes=("context", "tool_output"),
                    ),
                ),
                max_source_count=1,
                max_source_bytes=1024 * 1024,
                doc_version_source_slot_id="agent_context",
                rendered_artifact_source_kind_registry_version=1,
                rendered_artifact_source_kind_id="tool_output",
                request_params_canonical_json="{}",
                model_request_schema_versions=("model-router@2",),
                policy_injected_params=(),
                tool_schema_set_ref=CanonicalToolSchemaSetRefV1(
                    tool_version=schemas.tool_version,
                    digest=schemas.digest,
                ),
                prefix_cache_policy_ref=None,
                template_messages=(),
                source_renderer=build_agent_prompt_context_source_renderer(
                    agent_node_id=NODE,
                    prompt_version=PROMPT,
                ),
            ),
        ),
    )


def _request() -> ModelRequestV2:
    return ModelRequestV2(
        model_snapshot=ModelSnapshot(provider="test", model="model", snapshot_tag="1"),
        messages=(
            Message(role="user", content="Triage this exact finding."),
            Message(
                role="tool",
                content="checker: dangling_ref",
                tool_calls=[{"name": "checker_result", "status": "failed"}],
            ),
        ),
        agent_node_id=NODE,
        prompt_version=PROMPT,
    )


def test_context_renderer_preserves_user_tool_roles_calls_and_has_no_system_template() -> None:
    sources = _context_source()

    rendered = _authority().require_model_request(
        model_request=_request(),
        sources=((sources[0].artifact, sources[0].payload),),
    )

    assert rendered.messages == tuple(_request().messages)
    assert [message.role for message in rendered.messages] == ["user", "tool"]
    assert all(message.role != "system" for message in rendered.messages)
    assert rendered.messages[1].tool_calls == [{"name": "checker_result", "status": "failed"}]


def test_direct_empty_builtin_prompts_require_the_routed_output_token_bound() -> None:
    authority = build_builtin_agent_prompt_context_authority(
        required_plan_keys=(
            ("bench-agent-case", "bench-agent@1", "bench@1"),
            ("review-triage", "review-triage@1", "review-triage@1"),
        )
    )

    for binding in authority.retained_bindings:
        assert binding.request_params_canonical_json == "{}"
        assert len(binding.policy_injected_params) == 1
        injected = binding.policy_injected_params[0]
        assert injected.name == "max_output_tokens"
        assert injected.minimum == 1
        assert injected.maximum == 1_000_000
        assert injected.required is True

    handler_request = _request()
    routed_request = handler_request.model_copy(update={"params": {"max_output_tokens": 32_000}})
    authority.require_replay_source_semantics(
        handler_request=handler_request,
        source_request=routed_request,
    )

    with pytest.raises(
        IntegrityViolation,
        match="lacks required policy-injected parameter",
    ):
        authority.require_replay_source_semantics(
            handler_request=handler_request,
            source_request=handler_request,
        )

    with pytest.raises(
        IntegrityViolation,
        match="params differ from canonical prompt authority",
    ):
        authority.require_replay_source_semantics(
            handler_request=handler_request.model_copy(
                update={"params": {"handler_owned_escape": 1}}
            ),
            source_request=routed_request,
        )


def test_context_renderer_rejects_node_binding_or_one_byte_message_drift() -> None:
    sources = _context_source()
    with pytest.raises(IntegrityViolation, match="binding/lineage"):
        render_agent_prompt_context_sources(
            sources,
            agent_node_id="repair",
            prompt_version=PROMPT,
        )

    changed = _request().model_copy(
        update={
            "messages": (
                Message(role="user", content="Triage this exact finding!"),
                _request().messages[1],
            )
        }
    )
    with pytest.raises(IntegrityViolation, match="messages differ"):
        _authority().require_model_request(
            model_request=changed,
            sources=((sources[0].artifact, sources[0].payload),),
        )


def test_production_binding_renders_exact_repair_system_plus_persisted_context() -> None:
    schema_set = build_tool_schema_set(tool_version="repair@1", tool_schemas=())
    base = CanonicalPromptRendererAuthority(
        source_kind_registries=(build_source_kind_registry(),),
        tool_schema_sets=(schema_set,),
        bindings=(
            CanonicalPromptBindingV1(
                binding_id="legacy-repair-binding@1",
                agent_node_id="repair",
                prompt_version="repair@4",
                renderer_version="legacy-renderer@1",
                source_slots=(
                    CanonicalPromptSourceSlotV1(
                        slot_id="agent_context",
                        min_count=1,
                        max_count=1,
                        max_bytes=1024 * 1024,
                        allowed_shapes=(
                            CanonicalPromptSourceShapeV1(
                                artifact_kind="source_raw",
                                payload_schema_id="agent-prompt-context@1",
                                source_kind_registry_version=1,
                                source_kind_id="tool_output",
                            ),
                        ),
                        allowed_prompt_purposes=("context", "tool_output"),
                    ),
                ),
                max_source_count=1,
                max_source_bytes=1024 * 1024,
                doc_version_source_slot_id="agent_context",
                rendered_artifact_source_kind_registry_version=1,
                rendered_artifact_source_kind_id="tool_output",
                request_params_canonical_json=canonical_json({"max_tokens": 2048}),
                model_request_schema_versions=("model-router@2",),
                policy_injected_params=(
                    CanonicalPolicyInjectedParamV1(
                        name="temperature",
                        minimum=0,
                        maximum=0,
                    ),
                ),
                tool_schema_set_ref=CanonicalToolSchemaSetRefV1(
                    tool_version=schema_set.tool_version,
                    digest=schema_set.digest,
                ),
                prefix_cache_policy_ref=None,
                template_messages=(),
                source_renderer=build_agent_prompt_context_source_renderer(
                    agent_node_id="repair",
                    prompt_version="repair@4",
                ),
            ),
        ),
    )
    production = bind_production_agent_prompt_context_authority(
        base,
        required_plan_keys=(("repair", "repair@4", "repair@1"),),
    )
    _, system = get_prompt("repair.system")
    user = "Repair this exact finding and frozen IR context."
    source = _context_source(
        node="repair",
        prompt="repair@4",
        user=user,
        include_tool=False,
    )[0]
    request = ModelRequestV2(
        model_snapshot=ModelSnapshot(provider="test", model="model", snapshot_tag="1"),
        messages=(
            Message(role="system", content=system),
            Message(role="user", content=user),
        ),
        params={"max_tokens": 2048, "temperature": 0},
        agent_node_id="repair",
        prompt_version="repair@4",
    )

    rendered = production.require_model_request(
        model_request=request,
        sources=((source.artifact, source.payload),),
    )

    assert rendered.messages == tuple(request.messages)
    assert agent_prompt_context_binding_plan_keys(production) == (
        ("repair", "repair@4", "repair@1"),
    )

    forged = CanonicalPromptRendererAuthority(
        source_kind_registries=production.retained_source_kind_registries,
        tool_schema_sets=production.retained_tool_schema_sets,
        prefix_cache_policies=production.retained_prefix_cache_policies,
        bindings=(replace(production.retained_bindings[0], template_messages=()),),
    )
    # ``validate_worker_readiness`` consumes this exact proof projection; a
    # right-key/right-slot binding with a missing repair.system is not ready.
    assert agent_prompt_context_binding_plan_keys(forged) == ()
