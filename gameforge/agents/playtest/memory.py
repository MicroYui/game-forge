"""MemTrace (M2b-2): from-scratch long-horizon memory for the Playtest Agent.

Design borrowed (not dependencies): TITAN (down-weight repeated no-progress) +
Voyager (reusable skill layer) + Generative-Agents (recency×relevance×verdict
recall) + HiAgent/AWM/MemGPT (compaction at task-step boundaries). Recall is
DETERMINISTIC arithmetic — no model call — so ranking is fully reproducible and
unit-testable. Only `compact` touches the Router (replayable), and only when a
MemTrace instance is actually attached (`memory is not None`); with no memory
the Playtest loop is byte-identical to M2b-1.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

# Results that mean the state/action pair advanced nothing (TITAN down-weight).
_NO_PROGRESS_RESULTS = frozenset({"blocked", "unreachable", "noop", "invalid", "already"})


@dataclass(frozen=True)
class Episode:
    state_abstract: str
    action: dict
    result: str
    state_hash: str = ""
    tick: int = -1
    step_index: int = 0
    verdict: float = 0.0  # verifier-conditioned weight delta (reflect/compact set it)


class MemTrace:
    def __init__(self) -> None:
        self.trace: list[Episode] = []
        # state_hash -> action_key -> Counter[result] (persistent transition graph).
        self.transitions: dict[str, dict[str, Counter]] = {}

    def record(self, step: dict) -> None:
        action_dict = dict(step["action"])
        state_hash = str(step.get("state_hash", ""))
        result = str(step["result"])
        self.trace.append(
            Episode(
                state_abstract=str(step["state"]),
                action=action_dict,
                result=result,
                state_hash=state_hash,
                tick=int(step.get("tick", -1)),
                step_index=int(step.get("step_index", len(self.trace))),
            )
        )
        if state_hash:
            bucket = self.transitions.setdefault(state_hash, {})
            bucket.setdefault(self.action_key(action_dict), Counter())[result] += 1

    @staticmethod
    def action_key(action: dict) -> str:
        kind = str(action.get("kind", action.get("type", "?")))
        target = action.get("target") or action.get("target_id") or ""
        return f"{kind}:{target}"

    def no_progress_count(self, state_hash: str, action: dict) -> int:
        bucket = self.transitions.get(state_hash, {}).get(self.action_key(action))
        if bucket is None:
            return 0
        return sum(n for r, n in bucket.items() if r in _NO_PROGRESS_RESULTS)
