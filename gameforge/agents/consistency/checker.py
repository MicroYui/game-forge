"""ConsistencyChecker (M1 `LlmRoutedChecker` placeholder's real evaluation).

Implements `spine.checkers.base.Checker` but lives in `agents` (agents -> spine
is the allowed dependency direction; spine never imports agents/LLM SDKs). It
wraps a `ConsistencyAssistant` run and converts each quorum-surviving hint into
a `Finding` tagged `oracle_type="llm-assisted"`. That tag is load-bearing: in
`ReviewReport.partition`, `oracle_type == "llm-assisted"` is checked FIRST, so
these Findings are routed into `llm_assisted_findings` and can NEVER land in
`deterministic_findings` — no matter how confident the model sounds, it never
gets to masquerade as a proven structural/ASP/SMT defect. `status="unproven"`
reinforces the same fact: a human must confirm before this counts as real.
"""
from __future__ import annotations

from gameforge.agents.consistency.assistant import ConsistencyAssistant
from gameforge.contracts.agent_io import DialogueNarrativeInput
from gameforge.contracts.findings import Finding
from gameforge.runtime.model_router.router import ModelRouter
from gameforge.spine.ir.snapshot import Snapshot
from gameforge.spine.ir.store import NavProvider


class ConsistencyChecker:
    id = "consistency"

    def __init__(
        self,
        assistant: ConsistencyAssistant,
        router: ModelRouter,
        dialogue_input: DialogueNarrativeInput,
    ) -> None:
        self._assistant = assistant
        self._router = router
        self._dialogue_input = dialogue_input

    def check(self, snapshot: Snapshot, nav: NavProvider | None = None) -> list[Finding]:
        result = self._assistant.run(self._dialogue_input, self._router)
        hints = result.produced.get("hints", [])
        findings: list[Finding] = []
        for i, hint in enumerate(hints):
            findings.append(
                Finding(
                    id=f"{result.model_run_id}#{i}",
                    source="llm",
                    producer_id=self.id,
                    producer_run_id=result.model_run_id,
                    oracle_type="llm-assisted",
                    defect_class=hint["defect_class"],
                    severity="major",
                    snapshot_id=snapshot.snapshot_id,
                    entities=hint["entity_ids"],
                    constraint_id=hint["constraint_ids"][0],
                    evidence={
                        "span": hint["span"],
                        "rationale": hint["rationale"],
                        "constraint_ids": hint["constraint_ids"],
                    },
                    status="unproven",
                    message=hint["rationale"],
                )
            )
        return findings
