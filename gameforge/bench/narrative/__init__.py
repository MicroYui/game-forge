"""Seeded narrative evidence with a hidden typed-fact oracle."""

from gameforge.bench.narrative.contracts import NarrativeCase, to_agent_input
from gameforge.bench.narrative.oracle import evaluate_facts

__all__ = ["NarrativeCase", "evaluate_facts", "to_agent_input"]
