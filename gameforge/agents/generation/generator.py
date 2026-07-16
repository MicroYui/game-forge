"""Content Generator (M2a-part2 Task 7): design goal + grounding snapshot ->
proposed typed ops. Generated content is ALWAYS a proposal — `ContentProposal.
passed_gate` is decided entirely by `agents.generation.gate.gate_proposal`
(deterministic checkers + economy sim), never by the model's own claim.

The generator holds the grounding snapshot and the compiled checkers it must
be gated against (constructor injection, same shape the repair layer uses for
its snapshot-scoped verifier); `run` uses the snapshot for BOTH the prompt's
grounding context (a compact entity/attr summary — never the whole raw
snapshot dump) and the gate call itself, so a proposal can only ever be judged
against the exact content it was grounded in.
"""

from __future__ import annotations

import json

from gameforge.agents.base import AgentParseError, call_model, parse_json_block
from gameforge.agents.generation.gate import gate_proposal
from gameforge.agents.prompts.library import register_all_prompts
from gameforge.agents.prompts.registry import get_prompt
from gameforge.contracts.agent_io import AgentNodeResult, ContentProposal, DesignGoalInput
from gameforge.runtime.model_router.router import ModelRouter
from gameforge.spine.checkers.base import Checker
from gameforge.spine.ir.snapshot import Snapshot

register_all_prompts()


class ContentGenerator:
    node_id = "generation"

    def __init__(self, snapshot: Snapshot, checkers: list[Checker]) -> None:
        self._snapshot = snapshot
        self._checkers = checkers

    def run(
        self,
        input: object,
        router: ModelRouter,
        *,
        execute_local_gate: bool = True,
    ) -> AgentNodeResult:
        goal = input if isinstance(input, DesignGoalInput) else DesignGoalInput(**input)  # type: ignore[arg-type]
        version, system = get_prompt("generation.system")
        user = self._build_user_prompt(goal)

        request_hashes: list[str] = []
        try:
            resp, h = call_model(router, self.node_id, user, version, system=system)
            request_hashes.append(h)
            raw = parse_json_block(resp.response_normalized)
        except AgentParseError as exc:
            empty = ContentProposal(proposed_ops=[], passed_gate=False)
            return AgentNodeResult(
                role="generation",
                model_run_id=request_hashes[0] if request_hashes else "no-call",
                request_hashes=request_hashes,
                fallback_taken=True,
                produced={"proposal": empty.model_dump(), "blocking": [], "error": str(exc)},
            )

        ops = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
        # M2 callers retain the historical local fixed-budget gate. M4 supplies
        # an exact profile-hashed gate outside this class and disables the local
        # one so hidden process constants cannot execute or influence authority.
        passed, blocking = (
            gate_proposal(self._snapshot, ops, self._checkers)
            if execute_local_gate
            else (False, [])
        )
        proposal = ContentProposal(proposed_ops=ops, passed_gate=passed)

        return AgentNodeResult(
            role="generation",
            model_run_id=request_hashes[0] if request_hashes else "no-call",
            request_hashes=request_hashes,
            fallback_taken=False,
            produced={
                "proposal": proposal.model_dump(),
                "blocking": [f.defect_class for f in blocking],
            },
        )

    def _build_user_prompt(self, goal: DesignGoalInput) -> str:
        parts = [
            f"Design goal: {goal.goal}",
            f"grounding_snapshot_id: {goal.grounding_snapshot_id}",
            "",
            "Available entities in the grounding snapshot (id, type, attrs):",
            self._snapshot_summary(),
        ]
        return "\n".join(parts)

    def _snapshot_summary(self) -> str:
        """Compact JSON of every entity's (id, type, attrs) — the minimal
        grounding context the model needs to target new ops at real entities
        and real numeric ranges, no narrative/relation dump."""
        graph = self._snapshot.to_graph()
        nodes = [
            {"id": e.id, "type": e.type.value, "attrs": e.attrs}
            for e in sorted(graph.all_entities(), key=lambda e: e.id)
        ]
        return json.dumps(nodes, sort_keys=True, default=str)
