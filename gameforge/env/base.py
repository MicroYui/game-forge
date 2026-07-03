"""Agent-Env contract — engine-agnostic interface, no implementation (contract §4).

Aureus (game/aureus) implements this ABC. Playtest Agent (agents/, M2) and the
Regression Harness drive it. The same contract targets real engines in v-next.
"""

from __future__ import annotations

import abc

from gameforge.contracts.env_types import Action, Observation, StepResult
from gameforge.contracts.versions import ENV_CONTRACT_VERSION


class Environment(abc.ABC):
    env_contract_version: str = ENV_CONTRACT_VERSION

    @abc.abstractmethod
    def reset(self, scenario: str, seed: int) -> Observation:
        """Seed-ize and return the initial observation."""

    @abc.abstractmethod
    def step(self, action: Action) -> StepResult:
        """Apply one atomic action; advance the tick-based deterministic core."""

    @abc.abstractmethod
    def state_hash(self) -> str:
        """Canonical hash of authoritative env state (contract §4.4 scope)."""
