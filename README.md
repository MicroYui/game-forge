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
| M0b–M4 | see `docs/superpowers/specs/` and `CLAUDE.md` | ⬜ planned |

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
