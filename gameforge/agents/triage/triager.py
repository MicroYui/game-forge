"""Defect Triager (PRD §7.5): findings → clusters + priority, never re-judged.

The LLM only clusters and prioritizes findings; it never restates or overrides
a Finding's deterministic verdict (status/oracle_type/defect_class). The
deterministic oracle here: any finding_id the model invents (not among the
input findings' ids) is dropped and counted; any cluster with an out-of-range
priority, or that ends up with zero surviving finding_ids, is dropped entirely.
Input Finding objects are passed through untouched — they are never echoed
back into the triager's output structure.
"""
from __future__ import annotations

from gameforge.agents.base import AgentParseError, call_model, parse_json_block
from gameforge.agents.prompts.library import register_all_prompts
from gameforge.agents.prompts.registry import get_prompt
from gameforge.contracts.agent_io import (
    AgentNodeResult,
    FindingsInput,
    TriagedCluster,
    TriagedFindings,
)
from gameforge.runtime.model_router.router import ModelRouter

register_all_prompts()

_VALID_PRIORITIES = {"p0", "p1", "p2", "p3"}


class DefectTriager:
    node_id = "triage"

    def run(self, input: object, router: ModelRouter) -> AgentNodeResult:
        findings_input = (
            input if isinstance(input, FindingsInput) else FindingsInput(**input)  # type: ignore[arg-type]
        )
        valid_ids = {f.id for f in findings_input.findings}
        version, system = get_prompt("triage.system")
        lines = [
            f"- id={f.id} defect_class={f.defect_class} severity={f.severity} message={f.message}"
            for f in findings_input.findings
        ]
        user = "Findings:\n" + "\n".join(lines)

        request_hashes: list[str] = []
        try:
            resp, h = call_model(router, self.node_id, user, version, system=system)
            request_hashes.append(h)
            raw = parse_json_block(resp.response_normalized)
        except AgentParseError as exc:
            return AgentNodeResult(
                role="triage",
                model_run_id=request_hashes[0] if request_hashes else "no-call",
                request_hashes=request_hashes,
                fallback_taken=True,
                produced={
                    "triaged": TriagedFindings(clusters=[]).model_dump(),
                    "input_findings_untouched": True,
                    "dropped_ids": 0,
                    "error": str(exc),
                },
            )

        clusters: list[TriagedCluster] = []
        dropped_ids = 0
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            priority = str(item.get("priority", ""))
            if priority not in _VALID_PRIORITIES:
                continue
            raw_ids = item.get("finding_ids", [])
            if not isinstance(raw_ids, list):
                raw_ids = []
            kept_ids: list[str] = []
            for fid in raw_ids:
                if fid in valid_ids:
                    kept_ids.append(fid)
                else:
                    dropped_ids += 1  # invented id — never let the model surface ids we don't own
            if not kept_ids:
                continue
            clusters.append(
                TriagedCluster(
                    cluster_id=str(item.get("cluster_id", "")),
                    finding_ids=kept_ids,
                    priority=priority,  # type: ignore[arg-type]  # validated above against _VALID_PRIORITIES
                    suspected_root_cause=str(item.get("suspected_root_cause", "")),
                )
            )

        triaged = TriagedFindings(clusters=clusters)
        return AgentNodeResult(
            role="triage",
            model_run_id=request_hashes[0] if request_hashes else "no-call",
            request_hashes=request_hashes,
            fallback_taken=False,
            produced={
                "triaged": triaged.model_dump(),
                "input_findings_untouched": True,
                "dropped_ids": dropped_ids,
            },
        )
