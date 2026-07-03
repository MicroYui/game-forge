"""WorldConfig — the runtime config the Aureus kernel consumes (built from IR).

This is the IR-exported config that drives the reference game (PRD §7.7). For
M0a it carries grid + placements + quests (talk/collect/turn_in). Combat/economy
placements and battle encounters extend this in M0b without breaking the shape.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from gameforge.contracts.ir import NodeType
from gameforge.contracts.versions import ENV_CONTRACT_VERSION

QuestStepKind = Literal["talk", "collect", "turn_in"]


class GridSpec(BaseModel):
    width: int
    height: int
    blocked: list[tuple[int, int]] = Field(default_factory=list)


class Placement(BaseModel):
    entity_id: str
    type: NodeType
    pos: tuple[int, int]
    attrs: dict[str, Any] = Field(default_factory=dict)


class QuestStepSpec(BaseModel):
    step_id: str
    kind: QuestStepKind
    target: str | None = None  # npc for talk/turn_in
    item: str | None = None  # item for collect
    count: int = 1


class QuestSpec(BaseModel):
    quest_id: str
    giver: str
    steps: list[QuestStepSpec] = Field(default_factory=list)
    reward: dict[str, Any] = Field(default_factory=dict)


class ScenarioConfig(BaseModel):
    scenario_id: str
    start_pos: tuple[int, int]


class WorldConfig(BaseModel):
    scenario: ScenarioConfig
    grid: GridSpec
    placements: list[Placement] = Field(default_factory=list)
    quests: list[QuestSpec] = Field(default_factory=list)
    env_contract_version: str = ENV_CONTRACT_VERSION
