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

from dataclasses import dataclass


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

    def record(self, step: dict) -> None:
        self.trace.append(
            Episode(
                state_abstract=str(step["state"]),
                action=dict(step["action"]),
                result=str(step["result"]),
                state_hash=str(step.get("state_hash", "")),
                tick=int(step.get("tick", -1)),
                step_index=int(step.get("step_index", len(self.trace))),
            )
        )
