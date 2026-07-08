"""Agent-Env action/observation contract (contract §4.1–§4.3).

Actions are a discriminated union on the `kind` literal. Low-level ATOMIC actions
(§4.1) enter the Env; high-level semantic MACROS (§4.2) live in the planner layer
and compile down to atomic sequences — they are NOT Env actions.

The full atomic set is declared here in M0a. Aureus's M0a kernel implements the
navigation/quest subset; combat/economy atomics (attack/cast_skill/use/equip/
buy/sell) are declared now and answered with `last_action_result="unsupported_in_m0a"`
until M0b — declared, not cut (不简化只延后).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

from gameforge.contracts.versions import ENV_CONTRACT_VERSION


# --- Low-level atomic actions (§4.1) ---
class Observe(BaseModel):
    kind: Literal["observe"] = "observe"


class NavigateTo(BaseModel):
    kind: Literal["navigate_to"] = "navigate_to"
    target: str


class Interact(BaseModel):
    kind: Literal["interact"] = "interact"
    target: str


class Choose(BaseModel):
    kind: Literal["choose"] = "choose"
    option_id: str


class Attack(BaseModel):
    kind: Literal["attack"] = "attack"
    target_id: str


class CastSkill(BaseModel):
    kind: Literal["cast_skill"] = "cast_skill"
    skill_id: str
    target_id: str


class Use(BaseModel):
    kind: Literal["use"] = "use"
    item_id: str
    target: str | None = None


class Pickup(BaseModel):
    kind: Literal["pickup"] = "pickup"
    item_id: str


class Equip(BaseModel):
    kind: Literal["equip"] = "equip"
    item_id: str


class Buy(BaseModel):
    kind: Literal["buy"] = "buy"
    shop_id: str
    item_id: str
    count: int


class Sell(BaseModel):
    kind: Literal["sell"] = "sell"
    shop_id: str
    item_id: str
    count: int


class Wait(BaseModel):
    kind: Literal["wait"] = "wait"
    ticks: int


Action = Annotated[
    Union[
        Observe, NavigateTo, Interact, Choose, Attack, CastSkill,
        Use, Pickup, Equip, Buy, Sell, Wait,
    ],
    Field(discriminator="kind"),
]

_ACTION_ADAPTER: TypeAdapter[Action] = TypeAdapter(Action)


def parse_action(data: dict | BaseModel) -> Action:
    if isinstance(data, BaseModel):
        return data  # already an Action instance
    return _ACTION_ADAPTER.validate_python(data)


# High-level semantic macros — planner layer, compiled to atomics, NOT Env actions (§4.2)
HIGH_LEVEL_MACROS: tuple[str, ...] = ("accept_quest", "turn_in", "talk")


# --- Observation (§4.3) — full field set; combat fields stay empty in M0a ---
class Observation(BaseModel):
    tick: int
    player_pos: tuple[int, int]
    player_stats: dict[str, Any] = Field(default_factory=dict)
    equipped_items: list[str] = Field(default_factory=list)
    active_effects: list[str] = Field(default_factory=list)
    active_quests: list[str] = Field(default_factory=list)
    completed_quests: list[str] = Field(default_factory=list)
    known_quests: list[str] = Field(default_factory=list)
    quest_state: dict[str, Any] = Field(default_factory=dict)
    inventory: dict[str, int] = Field(default_factory=dict)
    hp: int = 0
    nearby_entities: list[str] = Field(default_factory=list)
    reachable_targets: list[str] = Field(default_factory=list)
    available_interactions: list[str] = Field(default_factory=list)
    # Actionability (§4.3): monster ids the agent should navigate to + attack
    # next — the undefeated monsters of every active quest's CURRENT `fight`
    # step (once placed/reachable) plus, once combat is active, the alive
    # monsters of the active encounter. Additive/backward-compatible (default
    # empty); excluded from `state_hash` like the rest of `Observation`.
    pending_fight_targets: list[str] = Field(default_factory=list)
    visible_map: dict[str, Any] = Field(default_factory=dict)
    dialogue_options: list[str] = Field(default_factory=list)
    last_action_result: str = ""
    logs: list[str] = Field(default_factory=list)


class StepResult(BaseModel):
    observation: Observation
    reward: float
    done: bool
    info: dict[str, Any] = Field(default_factory=dict)


ENV_TYPES_CONTRACT_VERSION = ENV_CONTRACT_VERSION
