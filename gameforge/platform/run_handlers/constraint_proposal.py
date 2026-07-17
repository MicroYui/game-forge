"""``constraint_proposer@1`` — the bounded constraint-extraction handler.

The LLM only PROPOSES constraints; the deterministic oracle keeps a proposal only
when its ``assert_expr`` compiles under the restricted DSL grammar (``parse_assert``).
That agent work lives in ``gameforge.agents.extraction.proposer``; ``platform``
cannot import it, so it is driven through an injected :class:`ConstraintProposalAgentRunner`
port with the LLM routed through the 11a ``ModelBridgeAgentAdapter``.

Success (``constraint_proposal_drafted``, run/succeeded): a ``PreparedRunResult``
whose primary ``constraint_proposal[constraint-proposal@1]`` is only a DRAFT. It can
NEVER become authoritative without a superseding human-authored revision and an
independent approval — the terminal publisher's workflow effect
``create_constraint_subject_head_and_draft@1`` creates only a draft ApprovalItem.
The proposal is ``produced_by=agent`` so ``producer_run_id == run_id`` (the
generation-invariant on ``ConstraintProposalV1.produced_by``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Protocol

from gameforge.contracts.agent_io import ConstraintProposal, DesignDocInput
from gameforge.contracts.dsl import Constraint, ConstraintKind
from gameforge.contracts.jobs import (
    ConstraintProposalProposePayloadV1,
    PreparedRunOutcome,
)
from gameforge.contracts.errors import IntegrityViolation
from gameforge.contracts.execution_profiles import ResolvedExecutionProfileBindingV1
from gameforge.contracts.workflow import ConstraintProposalV1, ConstraintSourceBinding
from gameforge.spine.dsl.ast import parse_assert

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExactProfileBindingValidator,
    ExecutorContextLike,
    PreparedArtifactStore,
    build_success_result,
    canonical_payload_bytes,
    prepared_version_tuple,
    require_exact_profile_bindings,
    store_prepared_artifact,
    trust_typed_profile_binding,
)
from gameforge.platform.run_handlers.model_routing import (
    BridgeModelRouter,
    build_bridge_router,
)

EXTRACTION_AGENT_NODE_ID = "extraction"
CONSTRAINT_PROPOSAL_SCHEMA_ID = "constraint-proposal@1"
EXTRACTION_TOOL_VERSION = "extraction@1"

_VALID_KINDS: frozenset[str] = frozenset(("structural", "numeric", "narrative"))


@dataclass(frozen=True, slots=True)
class ConstraintProposalRunRequest:
    """Fully-resolved inputs for one constraint-extraction invocation."""

    doc: DesignDocInput
    router: BridgeModelRouter


@dataclass(frozen=True, slots=True)
class ConstraintExtractionExecutionConfig:
    max_prompt_message_bytes: int = 17 * 1024 * 1024
    max_source_artifact_count: int = 64
    max_source_artifact_bytes: int = 4 * 1024 * 1024
    max_total_input_bytes: int = 16 * 1024 * 1024
    max_proposal_count: int = 256
    max_output_bytes: int = 8 * 1024 * 1024


class ConstraintExtractionExecutionConfigResolver(Protocol):
    def __call__(
        self, binding: ResolvedExecutionProfileBindingV1
    ) -> ConstraintExtractionExecutionConfig: ...


def default_constraint_extraction_execution_config(
    _binding: ResolvedExecutionProfileBindingV1,
) -> ConstraintExtractionExecutionConfig:
    return ConstraintExtractionExecutionConfig()


@dataclass(frozen=True, slots=True)
class LoadedDesignDocV1:
    doc: DesignDocInput
    source_hashes: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ConstraintProposalOutcomeV1:
    """The deterministic result of one extraction run.

    ``proposals`` are the compile-verified drafts the deterministic oracle kept;
    ``dropped`` counts the proposals rejected because ``assert_expr`` did not
    compile (recorded, never surfaced as real proposals).
    """

    proposals: tuple[ConstraintProposal, ...]
    dropped: int = 0


class ConstraintProposalAgentRunner(Protocol):
    """Drive the M2 extraction proposer for one authoring goal."""

    def run(self, request: ConstraintProposalRunRequest) -> ConstraintProposalOutcomeV1: ...


DocLoader = Callable[
    [
        ArtifactBlobReader,
        ConstraintProposalProposePayloadV1,
        ConstraintExtractionExecutionConfig,
    ],
    LoadedDesignDocV1,
]


def _read_bounded(
    blobs: ArtifactBlobReader,
    artifact_id: str,
    *,
    max_bytes: int,
) -> bytes:
    bounded = getattr(blobs, "read_bytes_bounded", None)
    raw = (
        bounded(artifact_id, max_bytes=max_bytes)
        if callable(bounded)
        else blobs.read_bytes(artifact_id)
    )
    if not isinstance(raw, bytes) or len(raw) > max_bytes:
        raise IntegrityViolation(
            "constraint extraction input exceeds its per-source byte budget",
            artifact_id=artifact_id,
        )
    return raw


def load_design_doc(
    blobs: ArtifactBlobReader,
    payload: ConstraintProposalProposePayloadV1,
    config: ConstraintExtractionExecutionConfig,
) -> LoadedDesignDocV1:
    """Decode exact authenticated source/goal bytes into one bounded agent input."""

    if len(payload.source_artifact_ids) > config.max_source_artifact_count:
        raise IntegrityViolation("constraint extraction source count exceeds its profile budget")
    parts: list[str] = []
    source_hashes: list[tuple[str, str]] = []
    total_input_bytes = 0
    for source_id in payload.source_artifact_ids:
        raw = _read_bounded(
            blobs,
            source_id,
            max_bytes=config.max_source_artifact_bytes,
        )
        total_input_bytes += len(raw)
        if total_input_bytes > config.max_total_input_bytes:
            raise IntegrityViolation("constraint extraction input exceeds its total byte budget")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("constraint source artifact must be UTF-8 text") from exc
        if not text:
            raise ValueError("constraint source artifact must be non-empty")
        parts.append(text)
        source_hashes.append((source_id, hashlib.sha256(raw).hexdigest()))
    goal_raw = _read_bounded(
        blobs,
        payload.authoring_goal.source_artifact_id,
        max_bytes=config.max_source_artifact_bytes,
    )
    total_input_bytes += len(goal_raw)
    if total_input_bytes > config.max_total_input_bytes:
        raise IntegrityViolation("constraint extraction input exceeds its total byte budget")
    try:
        authoring_goal = goal_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("constraint authoring goal must be UTF-8 text") from exc
    if not authoring_goal:
        raise ValueError("constraint authoring goal must be non-empty")
    parts.append(f"Authoring goal:\n{authoring_goal}")
    joined = "\n\n".join(parts)
    if len(joined.encode("utf-8")) > config.max_total_input_bytes:
        raise IntegrityViolation("constraint extraction joined input exceeds its byte budget")
    return LoadedDesignDocV1(
        doc=DesignDocInput(doc_text=joined, doc_version="pending-frozen-source"),
        source_hashes=tuple(source_hashes),
    )


@dataclass(frozen=True, slots=True)
class ConstraintProposalHandler:
    """A ``RunExecutor`` for ``constraint_proposer@1``."""

    blobs: ArtifactBlobReader
    store: PreparedArtifactStore
    agent_runner: ConstraintProposalAgentRunner
    doc_loader: DocLoader = load_design_doc
    execution_config_resolver: ConstraintExtractionExecutionConfigResolver = (
        default_constraint_extraction_execution_config
    )
    profile_binding_validator: ExactProfileBindingValidator = trust_typed_profile_binding

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, ConstraintProposalProposePayloadV1):
            raise TypeError(
                "constraint_proposer@1 requires a constraint-proposal-propose@1 payload"
            )

        profile_binding = require_exact_profile_bindings(
            context,
            expected={
                "/params/extraction_policy": (
                    payload.extraction_policy,
                    "constraint_extraction",
                ),
            },
            validator=self.profile_binding_validator,
        )["/params/extraction_policy"]
        config = self.execution_config_resolver(profile_binding)
        frozen_doc_version = context.payload.version_tuple.doc_version
        if frozen_doc_version is None:
            raise IntegrityViolation("constraint proposal Run lacks frozen source doc_version")
        loaded = self.doc_loader(self.blobs, payload, config)
        doc = loaded.doc.model_copy(update={"doc_version": frozen_doc_version})
        router = build_bridge_router(
            context=context,
            agent_node_id=EXTRACTION_AGENT_NODE_ID,
            max_prompt_message_bytes=config.max_prompt_message_bytes,
            source_artifact_ids=tuple(
                sorted(
                    (
                        *payload.source_artifact_ids,
                        payload.authoring_goal.source_artifact_id,
                    )
                )
            ),
        )
        outcome = self.agent_runner.run(ConstraintProposalRunRequest(doc=doc, router=router))
        if (
            isinstance(outcome.dropped, bool)
            or not isinstance(outcome.dropped, int)
            or not 0 <= outcome.dropped <= config.max_proposal_count
        ):
            raise ValueError("constraint proposal dropped count is outside the profile budget")
        if len(outcome.proposals) + outcome.dropped > config.max_proposal_count:
            raise ValueError("constraint proposal count exceeds the profile budget")
        if not outcome.proposals:
            # A fallback/fully-rejected model response is not a draft.  In
            # particular, publishing an empty proposal would create workflow
            # authority with no deterministically accepted content.
            raise ValueError("constraint proposal contains no compile-valid constraints")

        proposal = self._build_proposal(
            payload,
            outcome,
            run_id=context.run.run_id,
            base_constraint_snapshot_id=(context.payload.version_tuple.constraint_snapshot_id),
            source_hashes=dict(loaded.source_hashes),
        )
        proposal_payload = proposal.model_dump(mode="json")
        if len(canonical_payload_bytes(proposal_payload)) > config.max_output_bytes:
            raise IntegrityViolation("constraint proposal output exceeds its profile byte budget")
        primary = store_prepared_artifact(
            self.store,
            kind="constraint_proposal",
            payload_schema_id=CONSTRAINT_PROPOSAL_SCHEMA_ID,
            version_tuple=prepared_version_tuple(
                context,
                tool_version=EXTRACTION_TOOL_VERSION,
                projected_fields=(
                    "doc_version",
                    "ir_snapshot_id",
                    "constraint_snapshot_id",
                ),
            ),
            lineage=self._lineage(payload),
            payload=proposal_payload,
            extra_meta={"dropped_proposal_count": outcome.dropped},
        )
        return build_success_result(
            run=context.run,
            attempt=context.attempt,
            outcome_code="constraint_proposal_drafted",
            primary_index=0,
            artifacts=(primary,),
            findings=(),
        )

    def _build_proposal(
        self,
        payload: ConstraintProposalProposePayloadV1,
        outcome: ConstraintProposalOutcomeV1,
        *,
        run_id: str,
        base_constraint_snapshot_id: str | None,
        source_hashes: dict[str, str],
    ) -> ConstraintProposalV1:
        constraints = tuple(
            self._to_constraint(proposal, index, payload.dsl_grammar_version)
            for index, proposal in enumerate(outcome.proposals)
        )
        return ConstraintProposalV1(
            revision=1,
            base_constraint_snapshot_id=base_constraint_snapshot_id,
            dsl_grammar_version=payload.dsl_grammar_version,
            domain_scope=payload.domain_scope,
            constraints=constraints,
            source_bindings=self._source_bindings(payload, source_hashes),
            produced_by="agent",
            producer_run_id=run_id,
            rationale="extracted constraint proposal draft (LLM proposed; human authors authoritative)",
        )

    def _to_constraint(
        self, proposal: ConstraintProposal, index: int, dsl_grammar_version: str
    ) -> Constraint:
        if not isinstance(proposal, ConstraintProposal):
            raise ValueError("constraint proposal runner returned an invalid proposal type")
        if proposal.kind not in _VALID_KINDS:
            raise ValueError("constraint proposal kind is outside the frozen DSL")
        try:
            parse_assert(proposal.assert_expr)
        except Exception as exc:  # noqa: BLE001 — runner claims are not authority
            raise ValueError("constraint proposal assert does not compile") from exc
        kind: ConstraintKind = proposal.kind  # type: ignore[assignment]
        constraint_id = proposal.proposed_id or f"proposed-constraint-{index}"
        return Constraint(
            id=constraint_id,
            dsl_grammar_version=dsl_grammar_version,
            kind=kind,
            oracle="deterministic",
            assert_=proposal.assert_expr,
            severity="major",
            note=proposal.rationale or None,
        )

    def _source_bindings(
        self,
        payload: ConstraintProposalProposePayloadV1,
        source_hashes: dict[str, str],
    ) -> tuple[ConstraintSourceBinding, ...]:
        return tuple(
            ConstraintSourceBinding(
                source_artifact_id=source_id,
                provenance_hash=source_hashes[source_id],
            )
            for source_id in payload.source_artifact_ids
        )

    def _lineage(self, payload: ConstraintProposalProposePayloadV1) -> tuple[str, ...]:
        # The authoring goal is authenticated source_raw authority and is part of
        # the rendered prompt just like the design documents. It therefore must
        # remain a direct source parent even though ConstraintProposalV1's frozen
        # source_bindings field intentionally enumerates design-document sources.
        lineage = [*payload.source_artifact_ids, payload.authoring_goal.source_artifact_id]
        if payload.base_constraint_snapshot_artifact_id is not None:
            lineage.append(payload.base_constraint_snapshot_artifact_id)
        return tuple(lineage)


__all__ = [
    "CONSTRAINT_PROPOSAL_SCHEMA_ID",
    "EXTRACTION_AGENT_NODE_ID",
    "ConstraintProposalAgentRunner",
    "ConstraintExtractionExecutionConfig",
    "ConstraintExtractionExecutionConfigResolver",
    "ConstraintProposalHandler",
    "ConstraintProposalOutcomeV1",
    "ConstraintProposalRunRequest",
    "LoadedDesignDocV1",
    "default_constraint_extraction_execution_config",
    "load_design_doc",
]
