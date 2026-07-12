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
against concrete deterministic feedback. The changed prompt gets a fresh model
request hash (`Patch.producer_run_id`); Patch identity separately binds that run
to the exact base Snapshot and typed ops.
"""

from __future__ import annotations

import hashlib
import json

from gameforge.agents.base import AgentParseError, call_model, parse_json_block
from gameforge.agents.prompts.library import register_all_prompts
from gameforge.agents.prompts.registry import get_prompt, render
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.findings import Finding, Patch, TypedOp
from gameforge.contracts.ir import EdgeType
from gameforge.runtime.model_router.router import ModelRouter
from gameforge.spine.ir.snapshot import Snapshot

register_all_prompts()

_VALID_OP_KINDS = {
    "add_entity", "delete_entity", "set_entity_attr",
    "add_relation", "delete_relation", "set_relation_attr", "replace_subgraph",
}

# old_value optimistic concurrency is meaningful only for in-place updates
# (set_*/replace_subgraph); for add_* (no prior value) and delete_* (identified
# by id) a model-supplied old_value only causes spurious apply_patch rejections.
_DROP_OLD_VALUE_OPS = {"add_entity", "add_relation", "delete_entity", "delete_relation"}

_CATALOG_CAP = 50  # per-type cap on the available-entity catalog (bounds token size)


def _patch_id(
    request_hash: str, base_snapshot_id: str, ops: list[TypedOp]
) -> str:
    payload = {
        "request_hash": request_hash,
        "base_snapshot_id": base_snapshot_id,
        "ops": [op.model_dump(mode="json") for op in ops],
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


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
            id=_patch_id(request_hash, snapshot.snapshot_id, ops),
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
        parts.append(
            "IR context (JSON): focus_nodes = the defect's own nodes with full attrs; "
            "incident_relations = the real relations touching them (use these exact ids to "
            "delete_relation / set_relation_attr, and their endpoints); neighbor_nodes = nodes "
            "one edge away; entity_catalog = available entity ids grouped by node type (use these "
            "as real src_id/dst_id when you add_relation); edge_types = the valid relation types."
        )
        parts.append(self._ir_context(finding, snapshot))
        return "\n".join(parts)

    def _ir_context(self, finding: Finding, snapshot: Snapshot) -> str:
        """Structural context the model needs to target real ops: the defect's own
        nodes, the real relations incident to them (ids/types/endpoints), their
        neighbors, a per-type catalog of available entity ids, and the edge-type
        vocabulary. Bounded (catalog capped) — not a whole-snapshot dump."""
        graph = snapshot.to_graph()

        focus_ids = [e for e in finding.entities if graph.get_node(e) is not None]
        focus_set = set(focus_ids)
        focus_nodes = []
        for eid in focus_ids:
            node = graph.get_node(eid)
            if node is not None:
                focus_nodes.append({"id": node.id, "type": node.type.value, "attrs": node.attrs})

        incident_relations = []
        neighbor_ids: set[str] = set()
        for rel in graph.all_relations():
            if rel.src_id in focus_set or rel.dst_id in focus_set:
                incident_relations.append({
                    "id": rel.id, "type": rel.type.value,
                    "src_id": rel.src_id, "dst_id": rel.dst_id,
                })
                neighbor_ids.add(rel.src_id)
                neighbor_ids.add(rel.dst_id)
        neighbor_ids -= focus_set

        neighbor_nodes = []
        for nid in sorted(neighbor_ids):
            node = graph.get_node(nid)
            if node is not None:
                neighbor_nodes.append(
                    {"id": node.id, "type": node.type.value, "name": node.attrs.get("name")}
                )

        entity_catalog: dict[str, list[str]] = {}
        for entity in graph.all_entities():
            bucket = entity_catalog.setdefault(entity.type.value, [])
            if len(bucket) < _CATALOG_CAP:
                bucket.append(entity.id)

        return json.dumps(
            {
                "focus_nodes": focus_nodes,
                "incident_relations": incident_relations,
                "neighbor_nodes": neighbor_nodes,
                "entity_catalog": entity_catalog,
                "edge_types": [et.value for et in EdgeType],
            },
            sort_keys=True,
            default=str,
        )

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
            # old_value is an optimistic-concurrency assertion about a
            # PRE-EXISTING value, so it is meaningful only for in-place updates
            # (set_entity_attr / set_relation_attr / replace_subgraph). For
            # add_* (no prior value) and delete_* (identified by target id
            # alone), a model-summarized old_value can never equal apply_patch's
            # full-object current value and would only trip its concurrency
            # pre-check into a spurious PatchRejected — so drop it there.
            drop_old_value = op_kind in _DROP_OLD_VALUE_OPS
            ops.append(
                TypedOp(
                    op_id=str(item.get("op_id", f"op{i}")),
                    op=op_kind,  # type: ignore[arg-type]  # validated against _VALID_OP_KINDS
                    target=target,
                    old_value=None if drop_old_value else item.get("old_value"),
                    new_value=item.get("new_value"),
                    source_ref=item.get("source_ref"),
                )
            )
        return ops
