"""Spec-IR core types (contract §2.1–§2.3).

Logical model = typed property graph. Node/edge type sets are the FULL contract
sets: `core` members implemented in M0a; `combat-economy` members are declared
here now (impl deferred to M0b) — declared, not cut (不简化只延后).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from gameforge.contracts.versions import IR_SCHEMA_VERSION


class NodeType(str, Enum):
    # --- core (impl@M0a) ---
    FACTION = "FACTION"
    CHARACTER = "CHARACTER"
    NPC = "NPC"
    QUEST = "QUEST"
    QUEST_STEP = "QUEST_STEP"
    DIALOGUE_NODE = "DIALOGUE_NODE"
    REGION = "REGION"
    SPAWN_POINT = "SPAWN_POINT"
    INTERACTABLE = "INTERACTABLE"
    ITEM = "ITEM"
    MONSTER = "MONSTER"
    CURRENCY = "CURRENCY"
    SHOP = "SHOP"
    DROP_TABLE = "DROP_TABLE"
    REWARD_TABLE = "REWARD_TABLE"
    GACHA_POOL = "GACHA_POOL"
    EVENT = "EVENT"
    UNLOCK_CONDITION = "UNLOCK_CONDITION"
    # --- combat-economy (declared now, impl@M0b) ---
    EQUIPMENT = "EQUIPMENT"
    SKILL = "SKILL"
    STATUS_EFFECT = "STATUS_EFFECT"
    EFFECT = "EFFECT"
    BATTLE_ENCOUNTER = "BATTLE_ENCOUNTER"
    FORMULA = "FORMULA"


class EdgeType(str, Enum):
    # structural / quest
    HAS_STEP = "HAS_STEP"
    PRECEDES = "PRECEDES"
    REQUIRES = "REQUIRES"
    GATED_BY = "GATED_BY"
    UNLOCKS = "UNLOCKS"
    STARTS_AT = "STARTS_AT"
    TALKS_TO = "TALKS_TO"
    TRIGGERED_BY = "TRIGGERED_BY"
    # spatial / reachability
    LOCATED_IN = "LOCATED_IN"
    CONTAINS = "CONTAINS"
    SPAWNS = "SPAWNS"
    PATH_TO = "PATH_TO"
    # economy / produce-consume
    DROPS_FROM = "DROPS_FROM"
    GRANTS = "GRANTS"
    CONSUMES = "CONSUMES"
    REWARDS = "REWARDS"
    SELLS = "SELLS"
    # combat / effects (declared now, impl@M0b)
    USES_SKILL = "USES_SKILL"
    APPLIES_EFFECT = "APPLIES_EFFECT"
    HAS_STAT_CURVE = "HAS_STAT_CURVE"
    # narrative / faction
    HOSTILE_TO = "HOSTILE_TO"
    ALLY_WITH = "ALLY_WITH"
    BELONGS_TO = "BELONGS_TO"
    REVEALS = "REVEALS"
    REFERENCES = "REFERENCES"


class SourceRef(BaseModel):
    """round-trip + minimal-repro provenance (contract §2.1)."""

    model_config = ConfigDict(extra="forbid")

    adapter: str
    file: str
    sheet: str | None = None
    row: int | None = None
    column: str | None = None


class Entity(BaseModel):
    id: str
    type: NodeType
    attrs: dict[str, Any] = Field(default_factory=dict)
    source_ref: SourceRef | None = None
    tags: list[str] | None = None
    schema_version: str = IR_SCHEMA_VERSION


class Relation(BaseModel):
    id: str  # contract §2.1: relations MUST have an id (multi-edge + precise targeting)
    type: EdgeType
    src_id: str
    dst_id: str
    attrs: dict[str, Any] | None = None
    source_ref: SourceRef | None = None
    schema_version: str = IR_SCHEMA_VERSION
