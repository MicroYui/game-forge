"""Constraint DSL schema (contract §3) — machine-readable predicate-level oracle grammar.

Schema only: this module defines the *shape* of a constraint (what the DSL text
compiles from). Parsing the `assert` mini-expression into a typed AST and
compiling constraints into Checkers is `spine/dsl/` (M1 Task 3/7) — kept out of
contracts because contracts must stay import-free of any execution engine.
"""

from __future__ import annotations

from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from gameforge.contracts.findings import Severity
from gameforge.contracts.versions import DSL_GRAMMAR_VERSION

ConstraintKind = Literal["structural", "numeric", "narrative"]
PredicateOracle = Literal["deterministic", "llm-assisted"]


class Predicate(BaseModel):
    expr: str
    oracle: PredicateOracle = "deterministic"


class Selector(BaseModel):
    var: str
    node_type: str
    where: dict[str, Any] = Field(default_factory=dict)


class Constraint(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: str
    dsl_grammar_version: str = DSL_GRAMMAR_VERSION
    kind: ConstraintKind
    oracle: Literal["deterministic", "llm-assisted", "mixed"]
    predicates: list[Predicate] = Field(default_factory=list)
    scope: Selector | None = None
    forall: Selector | None = None
    assert_: str = Field(alias="assert")
    severity: Severity
    note: str | None = None

    def has_llm_predicate(self) -> bool:
        return self.oracle in ("llm-assisted", "mixed") or any(
            p.oracle == "llm-assisted" for p in self.predicates
        )

    @classmethod
    def from_yaml(cls, text: str) -> list["Constraint"]:
        raw = yaml.safe_load(text) or []
        return [cls.model_validate(item) for item in raw]
