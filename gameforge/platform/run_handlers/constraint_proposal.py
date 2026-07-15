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
from gameforge.contracts.lineage import VersionTuple
from gameforge.contracts.workflow import ConstraintProposalV1, ConstraintSourceBinding

from gameforge.platform.run_handlers.base import (
    ArtifactBlobReader,
    ExecutorContextLike,
    PreparedArtifactStore,
    build_success_result,
    load_json_blob,
    store_prepared_artifact,
)
from gameforge.platform.run_handlers.model_routing import (
    BridgeModelRouter,
    build_bridge_router,
)

EXTRACTION_AGENT_NODE_ID = "extraction"
CONSTRAINT_PROPOSAL_SCHEMA_ID = "constraint-proposal@1"

_VALID_KINDS: frozenset[str] = frozenset(("structural", "numeric", "narrative"))


@dataclass(frozen=True, slots=True)
class ConstraintProposalRunRequest:
    """Fully-resolved inputs for one constraint-extraction invocation."""

    doc: DesignDocInput
    router: BridgeModelRouter


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


DocLoader = Callable[[ArtifactBlobReader, ConstraintProposalProposePayloadV1], DesignDocInput]


def load_design_doc(
    blobs: ArtifactBlobReader, payload: ConstraintProposalProposePayloadV1
) -> DesignDocInput:
    """Concatenate the bound source design docs into one ``DesignDocInput``.

    Each source artifact is canonical JSON ``{"doc_text": <text>, "doc_version":
    <ver>?}``; ``doc_version`` defaults to the frozen ``dsl_grammar_version``.
    """

    parts: list[str] = []
    doc_version = payload.dsl_grammar_version
    for source_id in payload.source_artifact_ids:
        blob = load_json_blob(blobs, source_id)
        if not isinstance(blob, dict) or not isinstance(blob.get("doc_text"), str):
            raise ValueError("constraint source artifact must carry a doc_text string")
        parts.append(blob["doc_text"])
        version = blob.get("doc_version")
        if isinstance(version, str) and version:
            doc_version = version
    return DesignDocInput(doc_text="\n\n".join(parts), doc_version=doc_version)


@dataclass(frozen=True, slots=True)
class ConstraintProposalHandler:
    """A ``RunExecutor`` for ``constraint_proposer@1``."""

    blobs: ArtifactBlobReader
    store: PreparedArtifactStore
    agent_runner: ConstraintProposalAgentRunner
    doc_loader: DocLoader = load_design_doc

    def __call__(self, context: ExecutorContextLike) -> PreparedRunOutcome:
        payload = context.payload.params
        if not isinstance(payload, ConstraintProposalProposePayloadV1):
            raise TypeError(
                "constraint_proposer@1 requires a constraint-proposal-propose@1 payload"
            )

        doc = self.doc_loader(self.blobs, payload)
        router = build_bridge_router(context=context, agent_node_id=EXTRACTION_AGENT_NODE_ID)
        outcome = self.agent_runner.run(ConstraintProposalRunRequest(doc=doc, router=router))

        proposal = self._build_proposal(payload, outcome, run_id=context.run.run_id)
        primary = store_prepared_artifact(
            self.store,
            kind="constraint_proposal",
            payload_schema_id=CONSTRAINT_PROPOSAL_SCHEMA_ID,
            version_tuple=VersionTuple(
                constraint_snapshot_id=payload.base_constraint_snapshot_artifact_id,
                tool_version="extraction@1",
            ),
            lineage=self._lineage(payload),
            payload=proposal.model_dump(mode="json"),
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
    ) -> ConstraintProposalV1:
        constraints = tuple(
            self._to_constraint(proposal, index, payload.dsl_grammar_version)
            for index, proposal in enumerate(outcome.proposals)
        )
        return ConstraintProposalV1(
            revision=1,
            base_constraint_snapshot_id=payload.base_constraint_snapshot_artifact_id,
            dsl_grammar_version=payload.dsl_grammar_version,
            domain_scope=payload.domain_scope,
            constraints=constraints,
            source_bindings=self._source_bindings(payload),
            produced_by="agent",
            producer_run_id=run_id,
            rationale="extracted constraint proposal draft (LLM proposed; human authors authoritative)",
        )

    def _to_constraint(
        self, proposal: ConstraintProposal, index: int, dsl_grammar_version: str
    ) -> Constraint:
        kind: ConstraintKind = (
            proposal.kind if proposal.kind in _VALID_KINDS else "structural"  # type: ignore[assignment]
        )
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
        self, payload: ConstraintProposalProposePayloadV1
    ) -> tuple[ConstraintSourceBinding, ...]:
        return tuple(
            ConstraintSourceBinding(
                source_artifact_id=source_id,
                provenance_hash=hashlib.sha256(self.blobs.read_bytes(source_id)).hexdigest(),
            )
            for source_id in payload.source_artifact_ids
        )

    def _lineage(self, payload: ConstraintProposalProposePayloadV1) -> tuple[str, ...]:
        lineage = list(payload.source_artifact_ids)
        if payload.base_constraint_snapshot_artifact_id is not None:
            lineage.append(payload.base_constraint_snapshot_artifact_id)
        return tuple(lineage)


__all__ = [
    "CONSTRAINT_PROPOSAL_SCHEMA_ID",
    "EXTRACTION_AGENT_NODE_ID",
    "ConstraintProposalAgentRunner",
    "ConstraintProposalHandler",
    "ConstraintProposalOutcomeV1",
    "ConstraintProposalRunRequest",
    "load_design_doc",
]
