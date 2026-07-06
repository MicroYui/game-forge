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

QuestStepKind = Literal["talk", "collect", "turn_in", "fight"]


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
    encounter: str | None = None  # encounter_id for fight


class QuestSpec(BaseModel):
    quest_id: str
    giver: str
    steps: list[QuestStepSpec] = Field(default_factory=list)
    reward: dict[str, Any] = Field(default_factory=dict)


class ScenarioConfig(BaseModel):
    scenario_id: str
    start_pos: tuple[int, int]


class CurrencySpec(BaseModel):
    currency_id: str
    name: str | None = None


class FormulaSpec(BaseModel):
    formula_id: str
    expr: str
    kind: Literal["damage", "curve", "other"] = "damage"


class EffectSpec(BaseModel):
    effect_id: str
    kind: Literal["damage", "heal", "buff", "debuff", "dot"]
    stat: str | None = None
    magnitude: int = 0
    duration: int = 0


class StatusEffectSpec(BaseModel):
    status_effect_id: str
    effect_id: str
    duration: int = 1


class SkillSpec(BaseModel):
    skill_id: str
    name: str | None = None
    cost: int = 0
    power: int = 100
    formula_id: str | None = None
    target: Literal["enemy", "self", "ally"] = "enemy"
    applies_status: str | None = None


class EquipmentSpec(BaseModel):
    equipment_id: str
    slot: str
    stat_mods: dict[str, int] = Field(default_factory=dict)


class MonsterSpec(BaseModel):
    monster_id: str
    name: str | None = None
    stats: dict[str, int] = Field(default_factory=dict)
    skills: list[str] = Field(default_factory=list)
    drop_table_id: str | None = None
    ai: Literal["aggressive", "passive"] = "aggressive"


class DropEntry(BaseModel):
    item: str
    probability: float


class DropTableSpec(BaseModel):
    drop_table_id: str
    entries: list[DropEntry] = Field(default_factory=list)


class BattleEncounterSpec(BaseModel):
    encounter_id: str
    monsters: list[str] = Field(default_factory=list)
    reward: dict[str, Any] = Field(default_factory=dict)
    pos: tuple[int, int] | None = None


class ShopEntry(BaseModel):
    item: str
    price: int
    currency: str = "gold"


class ShopSpec(BaseModel):
    shop_id: str
    entries: list[ShopEntry] = Field(default_factory=list)


class GachaEntry(BaseModel):
    item: str
    weight: int


class GachaPoolSpec(BaseModel):
    gacha_pool_id: str
    cost: int = 100
    currency: str = "gold"
    entries: list[GachaEntry] = Field(default_factory=list)
    pity_threshold: int = 0
    pity_item: str | None = None


class WorldConfig(BaseModel):
    scenario: ScenarioConfig
    grid: GridSpec
    placements: list[Placement] = Field(default_factory=list)
    quests: list[QuestSpec] = Field(default_factory=list)
    env_contract_version: str = ENV_CONTRACT_VERSION
    currencies: list[CurrencySpec] = Field(default_factory=list)
    formulas: list[FormulaSpec] = Field(default_factory=list)
    effects: list[EffectSpec] = Field(default_factory=list)
    status_effects: list[StatusEffectSpec] = Field(default_factory=list)
    skills: list[SkillSpec] = Field(default_factory=list)
    equipment: list[EquipmentSpec] = Field(default_factory=list)
    monsters: list[MonsterSpec] = Field(default_factory=list)
    drop_tables: list[DropTableSpec] = Field(default_factory=list)
    encounters: list[BattleEncounterSpec] = Field(default_factory=list)
    shops: list[ShopSpec] = Field(default_factory=list)
    gacha_pools: list[GachaPoolSpec] = Field(default_factory=list)
