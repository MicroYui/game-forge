"""Pure canonical renderer for persisted per-call Agent prompt contexts."""

from __future__ import annotations

from dataclasses import replace
import json

from gameforge.agents.playtest.prompts import register_playtest_prompts
from gameforge.agents.prompts.library import register_all_prompts
from gameforge.agents.prompts.registry import get_prompt
from gameforge.apps.worker.prompt_rendering import (
    CanonicalPolicyInjectedParamV1,
    CanonicalPromptBindingV1,
    CanonicalPromptRendererAuthority,
    CanonicalPromptSourceShapeV1,
    CanonicalPromptSourceSlotV1,
    CanonicalPromptSourceV1,
    CanonicalSourceMessageV1,
    CanonicalToolSchemaSetRefV1,
    RetainedTemplateMessageV1,
    SourceRenderer,
    build_tool_schema_set,
)
from gameforge.contracts.canonical import canonical_json, sha256_lowerhex
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.jobs import MAX_AGENT_PROMPT_CONTEXT_BYTES, AgentPromptContextV1
from gameforge.contracts.provenance import OriginRefV1, PromptPartV1, ProvenanceV1
from gameforge.platform.provenance import build_source_kind_registry


AGENT_PROMPT_CONTEXT_RENDERER_VERSION = "agent-prompt-context-renderer@1"

# Exact system template used by each built-in active graph node.  ``None`` is
# itself retained authority: the corresponding production request has no system
# message and the renderer must not manufacture one.
_BUILTIN_SYSTEM_PROMPTS: dict[tuple[str, str], str | None] = {
    ("generation", "generation@1"): "generation.system",
    ("repair", "repair@4"): "repair.system",
    ("extraction", "extraction@1"): "extraction.system",
    ("review-triage", "review-triage@1"): None,
    ("bench-agent-case", "bench-agent@1"): None,
    ("playtest.planner", "playtest@1"): "playtest.planner",
    ("playtest.executor", "playtest@2"): "playtest.executor",
    ("playtest.reflect", "playtest@1"): "playtest.reflect",
    ("playtest.memory", "playtest.memory.compact@1"): None,
}


def render_agent_prompt_context_sources(
    sources: tuple[CanonicalPromptSourceV1, ...],
    *,
    agent_node_id: str,
    prompt_version: str,
) -> tuple[CanonicalSourceMessageV1, ...]:
    """Render one exact ``agent-prompt-context@1`` into request messages.

    The retained prompt binding supplies ``agent_node_id`` and ``prompt_version``;
    the outer fenced source authority independently binds the same pair to the
    current logical call.  No system instruction is read from the context payload.
    """

    if len(sources) != 1:
        raise IntegrityViolation("Agent prompt-context renderer requires exactly one source")
    source = sources[0]
    artifact = source.artifact
    if (
        artifact.kind != "source_raw"
        or artifact.meta.get("payload_schema_id") != "agent-prompt-context@1"
        or artifact.object_ref.sha256 != artifact.payload_hash
        or artifact.object_ref.size_bytes != len(source.payload)
        or sha256_lowerhex(source.payload) != artifact.payload_hash
    ):
        raise IntegrityViolation("Agent prompt-context renderer received another source shape")
    try:
        decoded = json.loads(source.payload)
        context = AgentPromptContextV1.model_validate(decoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise IntegrityViolation("Agent prompt-context renderer payload is invalid") from exc
    if source.payload != canonical_json(context.model_dump(mode="json")).encode("utf-8"):
        raise IntegrityViolation("Agent prompt-context renderer payload is not canonical")
    upstream_ids = tuple(sorted(item.artifact_id for item in context.upstream_artifacts))
    if (
        context.agent_node_id != agent_node_id
        or context.prompt_version != prompt_version
        or artifact.meta.get("producer_run_id") != context.run_id
        or artifact.meta.get("producer_attempt_no") != context.attempt_no
        or artifact.meta.get("target_call_ordinal") != context.target_call_ordinal
        or artifact.meta.get("agent_node_id") != agent_node_id
        or artifact.meta.get("prompt_version") != prompt_version
        or tuple(artifact.lineage) != upstream_ids
    ):
        raise IntegrityViolation("Agent prompt-context renderer binding/lineage differs")

    return tuple(
        CanonicalSourceMessageV1(
            role=message.role,
            text=message.content,
            purpose=message.purpose,
            source_artifact_ids=(artifact.artifact_id,),
            rendered_source_kind_registry_version=1,
            rendered_source_kind_id="tool_output",
            tool_calls=tuple(dict(item) for item in message.tool_calls),
        )
        for message in context.messages
    )


def build_agent_prompt_context_source_renderer(
    *,
    agent_node_id: str,
    prompt_version: str,
) -> SourceRenderer:
    """Bind the generic renderer to one exact retained node/prompt identity."""

    if not agent_node_id or not prompt_version:
        raise ValueError("Agent prompt-context renderer identity must be non-empty")

    def render(
        sources: tuple[CanonicalPromptSourceV1, ...],
    ) -> tuple[CanonicalSourceMessageV1, ...]:
        return render_agent_prompt_context_sources(
            sources,
            agent_node_id=agent_node_id,
            prompt_version=prompt_version,
        )

    return render


def _retained_system_messages(
    *, agent_node_id: str, prompt_version: str
) -> tuple[RetainedTemplateMessageV1, ...]:
    prompt_name = _BUILTIN_SYSTEM_PROMPTS[(agent_node_id, prompt_version)]
    if prompt_name is None:
        return ()
    register_all_prompts()
    register_playtest_prompts()
    retained_version, text = get_prompt(prompt_name)
    if retained_version != prompt_version:
        raise IntegrityViolation(
            "built-in system template version differs from Agent graph",
            agent_node_id=agent_node_id,
            prompt_version=prompt_version,
        )
    digest = sha256_lowerhex(text.encode("utf-8"))
    return (
        RetainedTemplateMessageV1(
            role="system",
            part=PromptPartV1(
                text=text,
                purpose="instruction",
                provenance=ProvenanceV1(
                    source_kind_registry_version=1,
                    source_kind_id="trusted_prompt_template",
                    origin_ref=OriginRefV1(
                        opaque_source_id=f"builtin-agent-template:{prompt_name}",
                        source_revision=digest,
                    ),
                    connector_id="builtin-agent-prompt-template-registry@1",
                    connector_version=prompt_version,
                    trust="trusted_internal",
                    source_hash=digest,
                ),
            ),
        ),
    )


def bind_production_agent_prompt_context_authority(
    authority: CanonicalPromptRendererAuthority,
    *,
    required_plan_keys: tuple[tuple[str, str, str], ...],
) -> CanonicalPromptRendererAuthority:
    """Rebind every retained built-in graph prompt to its fenced context Artifact.

    Deployment authority still owns exact request params, tool schemas, and prefix
    policy.  Production composition owns the source transport and immutable built-in
    system templates, so a legacy binding cannot continue reading arbitrary Run
    inputs after per-call context publication is enabled.  Missing graph bindings are
    deliberately left missing for the existing readiness closure to reject.
    """

    if not isinstance(authority, CanonicalPromptRendererAuthority):
        raise TypeError("production prompt authority has an invalid type")
    required_by_prompt: dict[tuple[str, str], str] = {}
    for agent_node_id, prompt_version, tool_version in required_plan_keys:
        key = (agent_node_id, prompt_version)
        if key not in _BUILTIN_SYSTEM_PROMPTS:
            raise IntegrityViolation(
                "active Agent graph has no production prompt-context binding",
                agent_node_id=agent_node_id,
                prompt_version=prompt_version,
            )
        previous = required_by_prompt.get(key)
        if previous is not None and previous != tool_version:
            raise IntegrityViolation("Agent graph prompt has conflicting tool versions")
        required_by_prompt[key] = tool_version

    rebound: list[CanonicalPromptBindingV1] = []
    for binding in authority.retained_bindings:
        key = (binding.agent_node_id, binding.prompt_version)
        required_tool_version = required_by_prompt.get(key)
        if required_tool_version is None:
            rebound.append(binding)
            continue
        if binding.tool_schema_set_ref.tool_version != required_tool_version:
            # Preserve the mismatch so the exact readiness plan-key comparison
            # reports the missing frozen node/tool closure.
            rebound.append(binding)
            continue
        rebound.append(
            replace(
                binding,
                binding_id=(
                    f"agent-prompt-context:{binding.agent_node_id}:{binding.prompt_version}@1"
                ),
                renderer_version=AGENT_PROMPT_CONTEXT_RENDERER_VERSION,
                source_slots=(
                    CanonicalPromptSourceSlotV1(
                        slot_id="agent_context",
                        min_count=1,
                        max_count=1,
                        max_bytes=MAX_AGENT_PROMPT_CONTEXT_BYTES,
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
                max_source_bytes=MAX_AGENT_PROMPT_CONTEXT_BYTES,
                doc_version_source_slot_id="agent_context",
                rendered_artifact_source_kind_registry_version=1,
                rendered_artifact_source_kind_id="tool_output",
                template_messages=_retained_system_messages(
                    agent_node_id=binding.agent_node_id,
                    prompt_version=binding.prompt_version,
                ),
                source_renderer=build_agent_prompt_context_source_renderer(
                    agent_node_id=binding.agent_node_id,
                    prompt_version=binding.prompt_version,
                ),
            )
        )
    return CanonicalPromptRendererAuthority(
        source_kind_registries=authority.retained_source_kind_registries,
        tool_schema_sets=authority.retained_tool_schema_sets,
        prefix_cache_policies=authority.retained_prefix_cache_policies,
        bindings=tuple(rebound),
    )


def build_builtin_agent_prompt_context_authority(
    *,
    required_plan_keys: tuple[tuple[str, str, str], ...],
) -> CanonicalPromptRendererAuthority:
    """Build the exact prompt authority implemented by the built-in Agent nodes."""

    canonical_keys = tuple(sorted(set(required_plan_keys)))
    if canonical_keys != tuple(sorted(required_plan_keys)):
        raise IntegrityViolation("Agent prompt plan keys must be stable-unique")
    tool_schema_sets = {
        tool_version: build_tool_schema_set(tool_version=tool_version, tool_schemas=())
        for _, _, tool_version in canonical_keys
    }
    direct_empty_params = {"review-triage", "bench-agent-case"}
    bindings: list[CanonicalPromptBindingV1] = []
    for agent_node_id, prompt_version, tool_version in canonical_keys:
        if (agent_node_id, prompt_version) not in _BUILTIN_SYSTEM_PROMPTS:
            raise IntegrityViolation(
                "active Agent graph has no built-in prompt authority",
                agent_node_id=agent_node_id,
                prompt_version=prompt_version,
            )
        params = (
            {}
            if agent_node_id in direct_empty_params
            else {"max_tokens": 512 if agent_node_id == "playtest.memory" else 2048}
        )
        schema_set = tool_schema_sets[tool_version]
        bindings.append(
            CanonicalPromptBindingV1(
                binding_id=f"builtin-agent-context:{agent_node_id}:{prompt_version}@1",
                agent_node_id=agent_node_id,
                prompt_version=prompt_version,
                renderer_version=AGENT_PROMPT_CONTEXT_RENDERER_VERSION,
                source_slots=(
                    CanonicalPromptSourceSlotV1(
                        slot_id="agent_context",
                        min_count=1,
                        max_count=1,
                        max_bytes=MAX_AGENT_PROMPT_CONTEXT_BYTES,
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
                max_source_bytes=MAX_AGENT_PROMPT_CONTEXT_BYTES,
                doc_version_source_slot_id="agent_context",
                rendered_artifact_source_kind_registry_version=1,
                rendered_artifact_source_kind_id="tool_output",
                request_params_canonical_json=canonical_json(params),
                model_request_schema_versions=("model-router@2",),
                policy_injected_params=(
                    ()
                    if agent_node_id in direct_empty_params
                    else (
                        CanonicalPolicyInjectedParamV1(
                            name="temperature",
                            minimum=0,
                            maximum=0,
                        ),
                    )
                ),
                tool_schema_set_ref=CanonicalToolSchemaSetRefV1(
                    tool_version=tool_version,
                    digest=schema_set.digest,
                ),
                prefix_cache_policy_ref=None,
                template_messages=(),
                source_renderer=build_agent_prompt_context_source_renderer(
                    agent_node_id=agent_node_id,
                    prompt_version=prompt_version,
                ),
            )
        )
    base = CanonicalPromptRendererAuthority(
        source_kind_registries=(build_source_kind_registry(),),
        tool_schema_sets=tuple(tool_schema_sets.values()),
        bindings=tuple(bindings),
    )
    return bind_production_agent_prompt_context_authority(
        base,
        required_plan_keys=canonical_keys,
    )


def agent_prompt_context_binding_plan_keys(
    authority: CanonicalPromptRendererAuthority,
) -> tuple[tuple[str, str, str], ...]:
    """Return only bindings proven to use the production context source shape."""

    keys: list[tuple[str, str, str]] = []
    for binding in authority.retained_bindings:
        if (binding.agent_node_id, binding.prompt_version) not in _BUILTIN_SYSTEM_PROMPTS:
            continue
        expected_binding_id = (
            f"agent-prompt-context:{binding.agent_node_id}:{binding.prompt_version}@1"
        )
        if (
            binding.binding_id != expected_binding_id
            or binding.renderer_version != AGENT_PROMPT_CONTEXT_RENDERER_VERSION
            or len(binding.source_slots) != 1
            or binding.max_source_count != 1
            or binding.max_source_bytes != MAX_AGENT_PROMPT_CONTEXT_BYTES
            or binding.doc_version_source_slot_id != "agent_context"
            or binding.rendered_artifact_source_kind_registry_version != 1
            or binding.rendered_artifact_source_kind_id != "tool_output"
        ):
            continue
        slot = binding.source_slots[0]
        expected_shape = CanonicalPromptSourceShapeV1(
            artifact_kind="source_raw",
            payload_schema_id="agent-prompt-context@1",
            source_kind_registry_version=1,
            source_kind_id="tool_output",
        )
        if (
            slot.slot_id != "agent_context"
            or slot.min_count != 1
            or slot.max_count != 1
            or slot.max_bytes != MAX_AGENT_PROMPT_CONTEXT_BYTES
            or slot.allowed_shapes != (expected_shape,)
            or slot.allowed_prompt_purposes != ("context", "tool_output")
            or binding.template_messages
            != _retained_system_messages(
                agent_node_id=binding.agent_node_id,
                prompt_version=binding.prompt_version,
            )
        ):
            continue
        keys.append(
            (
                binding.agent_node_id,
                binding.prompt_version,
                binding.tool_schema_set_ref.tool_version,
            )
        )
    return tuple(sorted(keys))


__all__ = [
    "AGENT_PROMPT_CONTEXT_RENDERER_VERSION",
    "agent_prompt_context_binding_plan_keys",
    "bind_production_agent_prompt_context_authority",
    "build_builtin_agent_prompt_context_authority",
    "build_agent_prompt_context_source_renderer",
    "render_agent_prompt_context_sources",
]
