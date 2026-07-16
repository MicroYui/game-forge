from __future__ import annotations

import pytest
from pydantic import ValidationError

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.jobs import (
    AgentPromptArtifactBindingV1,
    AgentPromptContextDraftV1,
    AgentPromptContextV1,
    AgentPromptPriorConsumptionV1,
    AgentPromptSemanticBindingV1,
    AgentPromptSourceMessageV1,
    MAX_AGENT_PROMPT_CONTEXT_BYTES,
    MAX_AGENT_PROMPT_CONTEXT_MESSAGE_BYTES,
    RunToolIntermediateLinkV1,
)


_DIGEST = "a" * 64


def _message(content: str = "repair this finding") -> AgentPromptSourceMessageV1:
    return AgentPromptSourceMessageV1(
        role="user",
        content=content,
        purpose="context",
    )


def _source() -> AgentPromptArtifactBindingV1:
    return AgentPromptArtifactBindingV1(
        binding_key="source:0",
        artifact_id="artifact:source",
        artifact_kind="ir_snapshot",
        payload_schema_id="ir-core@1",
        payload_hash=_DIGEST,
    )


def _prior() -> AgentPromptPriorConsumptionV1:
    return AgentPromptPriorConsumptionV1(
        attempt_no=1,
        call_ordinal=1,
        route_ordinal=1,
        prompt_artifact_id="artifact:prompt:1",
        request_hash="b" * 64,
        routing_decision_kind="native",
        routing_decision_id="route:1",
        execution_source="cassette_replay",
        reservation_group_id="reservation:1",
        cassette_shard_artifact_id="artifact:shard:1",
        cassette_source_artifact_id="artifact:cassette:root",
        response_digest="c" * 64,
    )


def _prior_parents() -> tuple[AgentPromptArtifactBindingV1, ...]:
    return (
        AgentPromptArtifactBindingV1(
            binding_key="prior.prompt",
            artifact_id="artifact:prompt:1",
            artifact_kind="source_rendered",
            payload_schema_id="source-rendered@1",
            payload_hash="e" * 64,
        ),
        AgentPromptArtifactBindingV1(
            binding_key="prior.cassette_source",
            artifact_id="artifact:cassette:root",
            artifact_kind="cassette_bundle",
            payload_schema_id="cassette-bundle@1",
            payload_hash="f" * 64,
        ),
    )


def test_context_is_canonical_bounded_and_closes_refine_consumption() -> None:
    binding = AgentPromptSemanticBindingV1(
        binding_key="repair.finding",
        subject_id="finding:1",
        subject_revision=2,
        subject_digest="d" * 64,
    )
    context = AgentPromptContextV1(
        context_kind="repair_refine",
        run_id="run:1",
        attempt_no=1,
        target_call_ordinal=2,
        agent_node_id="repair",
        prompt_version="repair@1",
        messages=(_message(),),
        upstream_artifacts=(_source(), *_prior_parents()),
        semantic_bindings=(binding,),
        prior_consumption=_prior(),
    )

    assert context.context_schema_version == "agent-prompt-context@1"
    assert context.semantic_bindings == (binding,)

    with pytest.raises(ValidationError, match="requires prior"):
        AgentPromptContextV1.model_validate(
            {
                **context.model_dump(mode="python"),
                "prior_consumption": None,
            }
        )


def test_draft_rejects_system_message_and_duplicate_bindings() -> None:
    with pytest.raises(ValidationError):
        AgentPromptSourceMessageV1(role="system", content="hidden", purpose="context")

    duplicate = AgentPromptSemanticBindingV1(
        binding_key="same",
        subject_id="finding:1",
        subject_digest=_DIGEST,
    )
    with pytest.raises(ValidationError, match="keys must be unique"):
        AgentPromptContextDraftV1(
            context_kind="repair_initial",
            messages=(_message(),),
            source_artifact_ids=("artifact:source",),
            semantic_bindings=(duplicate, duplicate),
        )


def test_escape_heavy_extraction_message_fits_the_canonical_context_envelope() -> None:
    message = _message("\x00" * (16 * 1024 * 1024))
    context = AgentPromptContextV1(
        context_kind="constraint_extraction",
        run_id="run:1",
        attempt_no=1,
        target_call_ordinal=1,
        agent_node_id="extraction",
        prompt_version="extraction@1",
        messages=(message,),
        upstream_artifacts=(_source(),),
    )
    canonical_size = len(canonical_json(context.model_dump(mode="json")).encode("utf-8"))

    assert canonical_size > 96 * 1024 * 1024
    assert canonical_size <= MAX_AGENT_PROMPT_CONTEXT_BYTES


def test_prompt_message_rejects_more_than_the_global_utf8_byte_cap() -> None:
    with pytest.raises(ValidationError, match="UTF-8 byte limit"):
        _message("界" * (MAX_AGENT_PROMPT_CONTEXT_MESSAGE_BYTES // 3 + 1))


@pytest.mark.parametrize(
    ("agent_node_id", "context_kind"),
    (
        ("generation", "review_triage"),
        ("review-triage", "generation"),
        ("bench-agent-case", "constraint_extraction"),
        ("extraction", "bench_agent_case"),
        ("playtest.planner", "generation"),
    ),
)
def test_context_rejects_node_kind_substitution(
    agent_node_id: str,
    context_kind: str,
) -> None:
    with pytest.raises(ValidationError, match="context kind"):
        AgentPromptContextV1(
            context_kind=context_kind,
            run_id="run:1",
            attempt_no=1,
            target_call_ordinal=1,
            agent_node_id=agent_node_id,
            prompt_version="prompt@1",
            messages=(_message(),),
            upstream_artifacts=(_source(),),
        )


def test_later_repair_initial_still_requires_immediate_prior_consumption() -> None:
    with pytest.raises(ValidationError, match="later repair context requires prior"):
        AgentPromptContextV1(
            context_kind="repair_initial",
            run_id="run:1",
            attempt_no=1,
            target_call_ordinal=2,
            agent_node_id="repair",
            prompt_version="repair@1",
            messages=(_message(),),
            upstream_artifacts=(_source(),),
        )


@pytest.mark.parametrize(
    ("agent_node_id", "context_kind"),
    (
        ("generation", "generation"),
        ("review-triage", "review_triage"),
        ("extraction", "constraint_extraction"),
    ),
)
def test_single_call_context_rejects_later_ordinal_and_prior_consumption(
    agent_node_id: str,
    context_kind: str,
) -> None:
    with pytest.raises(ValidationError, match="single-call"):
        AgentPromptContextV1(
            context_kind=context_kind,
            run_id="run:1",
            attempt_no=1,
            target_call_ordinal=2,
            agent_node_id=agent_node_id,
            prompt_version="prompt@1",
            messages=(_message(),),
            upstream_artifacts=(_source(), *_prior_parents()),
            prior_consumption=_prior(),
        )


def test_tool_link_contains_only_fenced_locator_authority() -> None:
    link = RunToolIntermediateLinkV1(
        run_id="run:1",
        attempt_no=1,
        target_call_ordinal=2,
        artifact_id="artifact:context:2",
        agent_node_id="repair",
        prompt_version="repair@1",
        payload_hash=_DIGEST,
        fencing_token=9,
        published_at="2026-07-17T00:00:00Z",
    )

    assert link.role == "agent_prompt_context"
    assert "trust" not in link.model_dump(mode="json")
    assert "source_artifact_ids" not in link.model_dump(mode="json")
