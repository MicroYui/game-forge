"""Extraction Proposer (PRD §7.5): design doc → proposed typed constraints.

The LLM only PROPOSES; a human authors the authoritative constraint. The
deterministic oracle here: a proposal is kept only if its assert_expr compiles
under the same restricted DSL grammar the checkers use (parse_assert) — anything
that fails to compile is dropped fail-closed, never surfaced as a real proposal.
"""
from __future__ import annotations

from gameforge.agents.base import AgentParseError, call_model, parse_json_block
from gameforge.agents.prompts.library import register_all_prompts
from gameforge.agents.prompts.registry import get_prompt
from gameforge.contracts.agent_io import (
    AgentNodeResult,
    ConstraintProposal,
    DesignDocInput,
)
from gameforge.runtime.model_router.router import ModelRouter
from gameforge.spine.dsl.ast import parse_assert

register_all_prompts()


class ExtractionProposer:
    node_id = "extraction"

    def run(self, input: object, router: ModelRouter) -> AgentNodeResult:
        doc = input if isinstance(input, DesignDocInput) else DesignDocInput(**input)  # type: ignore[arg-type]
        version, system = get_prompt("extraction.system")
        user = f"Design document (version {doc.doc_version}):\n\n{doc.doc_text}"

        request_hashes: list[str] = []
        try:
            resp, h = call_model(router, self.node_id, user, version, system=system)
            request_hashes.append(h)
            raw = parse_json_block(resp.response_normalized)
        except AgentParseError as exc:
            return AgentNodeResult(
                role="extraction",
                model_run_id=request_hashes[0] if request_hashes else "no-call",
                request_hashes=request_hashes,
                fallback_taken=True,
                produced={"proposals": [], "dropped": 0, "error": str(exc)},
            )

        proposals: list[ConstraintProposal] = []
        dropped = 0
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                dropped += 1
                continue
            expr = str(item.get("assert_expr", ""))
            try:
                parse_assert(expr)  # deterministic compilability oracle (fail-closed)
            except Exception:  # noqa: BLE001 — any parse failure drops the proposal
                dropped += 1
                continue
            proposals.append(
                ConstraintProposal(
                    proposed_id=str(item.get("proposed_id", "")),
                    kind=str(item.get("kind", "")),
                    assert_expr=expr,
                    rationale=str(item.get("rationale", "")),
                    needs_human_authoring=True,
                )
            )

        return AgentNodeResult(
            role="extraction",
            model_run_id=request_hashes[0] if request_hashes else "no-call",
            request_hashes=request_hashes,
            fallback_taken=False,
            produced={"proposals": [p.model_dump() for p in proposals], "dropped": dropped},
        )
