"""Repair Drafter (PRD §7.5): defect Finding + IR context → proposed typed Patch.

The LLM only PROPOSES the patch ops; whether the patch actually fixes anything
is decided entirely by the deterministic verifier (`agents.repair.verify`), not
here. This module's own fail-closed oracle is structural: any op the model
returns that is not a well-formed `TypedOp` (unknown op kind, missing target,
non-dict entry) is dropped; if nothing survives — or the output can't be parsed
as a JSON ops array at all — `draft` returns `None` (no patch proposed), never a
malformed `Patch`.

On a refine round the caller passes the verifier's `counterexample`; it is
appended to the prompt via the `repair.refine` template so the model re-drafts
against concrete deterministic feedback (and, because the prompt now differs, the
request gets a fresh request_hash — the drafted `Patch.id`).
"""

from __future__ import annotations

import json

from gameforge.agents.base import AgentParseError, call_model, parse_json_block
from gameforge.agents.prompts.library import register_all_prompts
from gameforge.agents.prompts.registry import get_prompt, render
from gameforge.contracts.findings import Finding, Patch, TypedOp
from gameforge.runtime.model_router.router import ModelRouter
from gameforge.spine.ir.snapshot import Snapshot

register_all_prompts()

_VALID_OP_KINDS = {
    "add_entity", "delete_entity", "set_entity_attr",
    "add_relation", "delete_relation", "set_relation_attr", "replace_subgraph",
}


class RepairDrafter:
    node_id = "repair"

    def draft(
        self,
        finding: Finding,
        snapshot: Snapshot,
        router: ModelRouter,
        *,
        counterexample: str | None = None,
    ) -> Patch | None:
        version, system = get_prompt("repair.system")
        user = self._build_user_prompt(finding, snapshot)
        if counterexample is not None:
            _, refine = render("repair.refine", counterexample=counterexample)
            user = f"{user}\n\n{refine}"

        try:
            resp, request_hash = call_model(router, self.node_id, user, version, system=system)
            raw = parse_json_block(resp.response_normalized)
        except AgentParseError:
            return None  # unparseable model output → no patch proposed (fail closed)

        ops = self._build_ops(raw)
        if not ops:
            return None  # no well-formed ops survived → no patch proposed

        return Patch(
            id=request_hash,
            base_snapshot_id=snapshot.snapshot_id,
            target_snapshot_id="",
            side_effect_risk="low",
            ops=ops,
            produced_by="agent",
            producer_run_id=request_hash,
            rationale="verifier-guided repair",
            expected_to_fix=[finding.id],
        )

    def _build_user_prompt(self, finding: Finding, snapshot: Snapshot) -> str:
        parts = [
            "Defect finding to repair:",
            f"- defect_class: {finding.defect_class}",
            f"- message: {finding.message}",
            f"- entities: {finding.entities}",
        ]
        if finding.relations:
            parts.append(f"- relations: {finding.relations}")
        if finding.evidence:
            parts.append(
                f"- evidence: {json.dumps(finding.evidence, sort_keys=True, default=str)}"
            )
        parts.append("")
        parts.append("Relevant IR nodes (id, type, attrs):")
        parts.append(self._ir_context(finding, snapshot))
        parts.append("")
        parts.append(f"base_snapshot_id: {snapshot.snapshot_id}")
        return "\n".join(parts)

    def _ir_context(self, finding: Finding, snapshot: Snapshot) -> str:
        """Compact JSON of the nodes the finding implicates — the minimal graph
        context the model needs to target its ops, no whole-snapshot dump."""
        graph = snapshot.to_graph()
        nodes = []
        for entity_id in finding.entities:
            node = graph.get_node(entity_id)
            if node is not None:
                nodes.append({
                    "id": node.id,
                    "type": node.type.value,
                    "attrs": node.attrs,
                })
        return json.dumps(nodes, sort_keys=True, default=str)

    def _build_ops(self, raw: object) -> list[TypedOp]:
        ops: list[TypedOp] = []
        if not isinstance(raw, list):
            return ops
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            op_kind = item.get("op")
            if op_kind not in _VALID_OP_KINDS:
                continue  # unknown/missing op kind → drop (fail closed)
            target = item.get("target")
            if not isinstance(target, str) or not target:
                continue  # every TypedOp needs a target
            ops.append(
                TypedOp(
                    op_id=str(item.get("op_id", f"op{i}")),
                    op=op_kind,  # type: ignore[arg-type]  # validated against _VALID_OP_KINDS
                    target=target,
                    old_value=item.get("old_value"),
                    new_value=item.get("new_value"),
                    source_ref=item.get("source_ref"),
                )
            )
        return ops
