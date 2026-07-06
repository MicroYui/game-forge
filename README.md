# GameForge

**Game-content correctness compiler + production-grade agent workbench.** Build a
versionable **Design-Spec IR** (knowledge graph + typed constraints) from design
docs / config tables → validate with **deterministic checkers + economy simulation**
(decidable, *not* LLM-judging) → use **bounded LLM agents** for extraction proposals,
defect triage, and repair drafting → close the loop with a **Playtest Agent** inside a
real runnable reference game **Aureus** — observable, reproducible, auditable, human-approved.

See `docs/superpowers/specs/` for the PRD and foundational contracts (single source of truth).

## Status

| Milestone | Theme | Status |
|---|---|---|
| **M0a** | Shortest vertical slice: contracts + IR core + canonical snapshot + Aureus minimal kernel (quest + grid nav) + minimal checker + a 3-step quest chain | ✅ acceptance passing |
| **M0b** | Aureus combat/economy/gacha; Schema Registry + Aureus CSV adapter round-trip; version/lineage/audit skeleton; Alembic DB migrations | ✅ acceptance passing |
| M1–M4 | see `docs/superpowers/specs/` and `CLAUDE.md` | ⬜ planned |

## Layout (contract §1)

All Python packages live under `gameforge/` (dependency direction enforced by
`import-linter`); `web/` (React/TS) is a repo-root sibling.

```
gameforge/
  contracts/   # schema single source of truth (IR, Env, Finding/Patch, WorldConfig)
  runtime/     # low-level capabilities (skeleton until M2)
  spine/       # deterministic trusted trunk — NO LLM (ir, checkers, dsl, sim, versioning)
  env/         # Agent-Env interface (ABC, no impl)
  game/aureus/ # reference game: deterministic kernel implementing env
  agents/      # bounded LLM layer (skeleton until M2)
  platform/    # product platform (skeleton)
  apps/cli/    # composition layer: end-to-end slice runner
  bench/       # GameForge-Bench (skeleton until M3)
scenarios/     # hand-written scenario configs
web/           # Vite + React + TS console scaffold
```

## Quickstart

```bash
uv python install 3.12 && uv sync     # provision env + deps
uv run pytest                         # full test suite (unit + property + e2e)
uv run lint-imports                   # dependency-direction gate (spine is LLM-free)
```

## M0a acceptance

One hand-written config → IR → Aureus runs a 3+ step quest chain (talk → collect → turn-in),
deterministically and reproducibly:

```bash
uv run python -m gameforge.apps.cli scenarios/caravan.yaml 0
# -> {"completed": true, "ticks": 30, "num_findings": 0, ...}
```

The frontend scaffold builds with:

```bash
cd web && npm install && npm run build
```

## M0b acceptance

A typed CSV scenario workbook (`scenarios/outpost/`) round-trips losslessly through the
Schema Registry + Aureus adapter (`workbook -> IR -> workbook` diff is empty), and the
same scenario drives all four Aureus systems — combat, economy, gacha, quest —
config-driven and deterministically to completion:

```bash
uv run python -m gameforge.apps.cli scenarios/outpost 0
# -> {"completed": true, "ticks": 29, ..., "systems_exercised": ["combat", "economy", "gacha", "quest"]}
```

Version/lineage/audit (contract §5) and the DB migration framework are exercised by:

```bash
uv run alembic -c alembic.ini upgrade head && uv run alembic -c alembic.ini downgrade base
```

`DATABASE_URL` defaults to a local sqlite file (`sqlite:///gameforge.db`, gitignored) when
unset; the schema is Postgres-ready (SQLAlchemy Core + Alembic, no sqlite-only types).

## What M0a delivers vs. deferred (不简化，只延后)

**Delivered:** monorepo + dependency lint; `contracts` (IR core types, canonical
snapshot, Env action/observation, Finding/Patch, WorldConfig — full contract field
sets); in-memory IR store + immutable content-addressed snapshots + diff; YAML
scenario → IR loader; minimal deterministic structural checker (reference integrity,
collect-needs-reachable-source, quest-DAG-acyclic); Aureus minimal kernel (quest state
machine + grid navigation) implementing the Agent-Env contract with per-tick
`state_hash` determinism; end-to-end vertical slice; React scaffold.

**Interfaces defined now, implementation deferred:** combat/economy Env atomics + IR
combat-economy node/edge impl (M0b); Schema Registry round-trip adapter, version/
lineage/audit, DB migrations (M0b); DSL grammar + Graph/ASP/SMT compiler + economy sim
(M1); bounded LLM agents + cassette/model-router (M2); GameForge-Bench (M3); full web
pages + observability/RBAC (M4).

## What M0b delivers vs. deferred (不简化，只延后)

**Delivered:** Aureus combat (formula-driven damage/hit/crit + seeded `CountingRandom`),
economy (currencies/shops/atomic buy), and gacha (drop tables, atomic buy-and-roll) —
all config-driven via `WorldConfig`, integrated into the kernel alongside quest/nav, with
per-tick `state_hash` covering combat/gacha rng; IR combat-economy node/edge types
produced by the loader and consumed by the checker/kernel; a typed CSV `FormatSchema` +
`SchemaRegistry` (structural + referential validation) and a pluggable `AureusCsvAdapter`
(`to_ir`/`from_ir`) giving a lossless workbook<->IR round trip (property-tested); the
`scenarios/outpost/` CSV scenario exercising all four systems end-to-end; version/lineage/
audit skeleton (contract §5 full `VersionTuple`, content-addressed `Artifact` + lineage
DAG, ref/rollback, append-only WORM `AuditRecord` with hash chain) with both an in-memory
store (used by `run_slice`) and a SQLAlchemy-backed store; Alembic migration framework
with a tested forward/rollback migration; the dependency lint tightened so `spine` only
depends on `contracts`.

**Interfaces defined now, implementation deferred:** DSL grammar + Graph/ASP/SMT checker
compilation + economy Monte-Carlo/ABM simulation + open-source game adapter external
validity (M1); bounded LLM agents + Agent-Env + Playtest Agent + cassette/model-router
(M2); GameForge-Bench (M3); RBAC/approval workflow + full web pages + observability
panels (M4). `VersionTuple` fields for constraint/prompt/model/agent/cassette are
schema-present and `None` until those milestones populate them.
