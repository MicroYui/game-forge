# M4b Observability, Cost Governance, and Reliability Implementation Plan

> **Execution rule:** implement task by task with TDD. A task is complete only after its focused tests pass and its stated invariants are exercised. M4b is not complete until the slice-wide gates in Task 12 pass.

**Goal:** Implement the frozen M4b contracts and complete local deterministic implementations for trace/metrics/log telemetry, persistent cost admission and accounting, Model Router/Cassette V2 compatibility, reliability controls, and replayable SLO/alert evaluation.

**Architecture:** Pure wire DTOs and Protocols live in `gameforge.contracts`; runtime mechanisms and SQLite/local adapters live in `gameforge.runtime`; cost/routing policy and SLO evaluation live in `gameforge.platform`. Existing M4a Run/UoW/fencing state is reused through its reserved cost seams. Telemetry is bounded best-effort, while Audit and CostLedger remain authoritative and fail closed.

**Truth sources:**

1. `docs/superpowers/specs/2026-07-03-gameforge-prd.md`
2. `docs/superpowers/specs/2026-07-03-gameforge-foundations-contracts.md` v0.3, especially §7
3. `docs/superpowers/specs/2026-07-13-m4-production-hardening-design.md`, especially §§4 and 9

**Tech stack:** Python 3.12, Pydantic v2, SQLAlchemy 2, Alembic, SQLite WAL, canonical SHA-256 JSON, contextvars, Hypothesis, pytest, import-linter, Ruff.

## Scope Boundary

M4b implements:

- separate Run correlation and W3C trace propagation, immutable spans, bounded trace/log/metric query DTOs and stores, injected UTC/monotonic clocks, and best-effort exporters;
- exact metric registries, bounded cardinality, deterministic aggregation, structured redacted logging, in-memory stores, file exporters, and a restart-readable local SQLite telemetry store;
- persistent Budget/BudgetSetSnapshot/ReservationGroup/UsageEntry/PermitGroup accounting with reserve-before-use, unknown-use retention, conservative and late settlement, all-scope atomicity, and M4a fencing integration;
- additive `model-router@2` and `cassette@2`, typed usage/latency/cache observations, exact full-request response caching, provider prefix-cache directives, model catalog/routing policy/decision persistence, and an honestly unavailable default PriceBook;
- typed retry classification, deadline/budget-aware backoff, infrastructure-only circuit breaking, versioned SLO evaluation, and replayable alert state transitions with in-memory/file sinks.

M4b does not implement FastAPI, worker composition, SSE/WS, React pages, PostgreSQL/S3, real OTLP/Prometheus/Tempo/Loki, Grafana, PagerDuty, or cloud deployment. Those remain M4c-M4e work. It also does not redesign M4a Run/lease/fencing contracts. M4b freezes the complete SLO contracts, evaluator, alert state machine, and exact-descriptor retention path; the measured checker/sim/50-task numeric baselines and their versioned initial production configurations are created by the preregistered M4e §7.H capacity run, not invented during M4b.

## Compatibility Rules

- Preserve `model-router@1`, nested and standalone `cassette@1`, all existing aliases, request-hash formulas, and committed cassette bytes. Compatibility tests hash the original fixture bytes before/after typed reads; they do not mistake model reserialization for byte preservation.
- M4-native requests and recordings write V2; legacy readers map only fields that actually exist. Legacy zero/empty usage and latency remain `unavailable`, never reported zero.
- `RunCorrelation` and `TraceContext` are distinct. Trace context is operational metadata and never enters `spine`, Run payload hashes, request hashes, Artifact payload/meta, or canonical artifact identity.
- Span UTC timestamps are wall-clock facts; `duration_ns` is a monotonic difference. Cassette provider latency is a separate observation.
- Telemetry failures may increment bounded dropped counters but may not change business outcomes. Audit and CostLedger failures do change outcomes and fail closed.
- Metric descriptor refs are exact historical refs. High-cardinality identity labels are forbidden, queries are bounded, and raw numerator/denominator observations are never averaged as ratios.
- Cost reservation precedes work. Unknown usage does not release reservation. Already incurred use is reconciled even after lease expiry, while stale fencing cannot create new reservations or publish worker results.
- Provider prefix reuse still performs a provider call. Local response reuse requires the complete exact request hash. REPLAY remains cassette-authoritative and misses fail closed.
- Breakers observe only classified external/infrastructure failures, never solver `unknown`/`unproven`, validation failures, or deterministic checker outcomes.
- Monetary values remain unavailable without an exact provider/model/effective-time PriceBook match; no tier is called cheaper without evidence.

## File Map

| Area | Planned files |
|---|---|
| Additive contracts | new `gameforge/contracts/observability.py`, `cost.py`, `reliability.py`, `slo.py`; additive `model_router.py`, `cassette.py`, and narrow exports |
| Runtime mechanisms | fill `gameforge/runtime/observability/`; add `runtime/cost/`; extend `runtime/model_router/` and `runtime/cassette/`; add typed retry/breaker modules |
| SQLite adapters | additive cost and routing rows/repositories plus Alembic `0006+`; independent telemetry SQLite schema/store outside the authoritative business UoW |
| Platform policy | new `gameforge/platform/cost_policy/` and `gameforge/platform/slo/`; narrow integration with existing `platform/runs/` seams |
| Tests | `tests/contracts/m4b/`, `tests/runtime/observability/`, `tests/runtime/cost/`, focused router/cassette/persistence tests, `tests/platform/m4b/` |

## Task 1: Freeze Additive M4b Wire Contracts

**RED tests**

- Freeze RunCorrelation/TraceContext/TraceCarrier, SpanData and bounded trace/log/metric query/result DTOs, exact descriptor and registry digests, and metric union invariants.
- Freeze CostAmount/Budget/BudgetSnapshot/BudgetSetSnapshot/Reservation/Usage/Permit, typed TokenUsage/Latency/CacheHit/Monetary observations, PriceQuote, and their scope/status invariants.
- Freeze ModelCatalog/RoutingPolicy/RoutingDecision, typed retry/breaker, SLODefinition/SLOEvaluation/Alert DTOs, stable canonical sorting, and exact digest rules.
- Freeze the full foundations §7 verified-import closure: verification policy/registry, call evidence, run manifest, input/profile/policy/schema bindings, three-level CassetteBundle, original-wire digest, and verified/evidence-missing execution rules.
- Prove V1 router/cassette fixture bytes and hashes remain unchanged across typed reads; V2 discriminators cannot fall through to V1 readers; unknown remains unavailable.

**Implementation**

- Add pure Pydantic DTOs, Protocols, discriminated parsers, digest constructors, and typed failures in `contracts` only.
- Reuse M4a bounded IDs, digest types, clocks, and cursor mechanisms rather than creating parallel concepts. Telemetry wire output remains the frozen dedicated TraceSummaryPageV1/SpanPageV1/MetricPageV1 DTOs, never generic storage PageV1.
- Export only contract-level types; keep runtime Span/Tracer/registry objects out of contracts and spine.

**Verify**

```bash
uv run pytest tests/contracts/m4b tests/contracts/test_model_router.py tests/contracts/test_cassette.py -q
uv run pytest tests/test_dependency_lint.py -q
```

## Task 2: Implement Trace Context, Spans, and Best-Effort Export

**RED tests**

- Round-trip valid W3C traceparent/tracestate through a bounded carrier; reject malformed input without changing business flow.
- Propagate current context through nested sync/async contextvars; preserve parent/child and link relationships across carrier extraction.
- With fake clocks, prove UTC start/end and monotonic duration are separate, including wall-clock jumps and REPLAY provider-latency separation.
- Prove immutable completed SpanData, bounded/redacted attributes/events/resources, deterministic injected ID generation/sampling, exporter failure isolation, and dropped telemetry accounting.

**Implementation**

- Add `Tracer`, span context manager, `TraceCarrier`, injected IdGenerator/Sampler/SpanProcessor/SpanExporter, contextvars, InMemoryExporter, and deterministic NDJSON FileExporter.
- Validate and bound telemetry at ingestion; never serialize raw prompts, responses, credentials, or arbitrary large objects.

**Verify**

```bash
uv run pytest tests/runtime/observability/test_context.py tests/runtime/observability/test_trace.py tests/runtime/observability/test_exporters.py -q
```

## Task 3: Implement Metric Registry, In-Memory Telemetry, and Structured Logs

**RED tests**

- Reject forbidden identity label keys, duplicate/mismatched descriptors, non-finite values, invalid histogram bounds, undeclared labels, and descriptor/global series overflow.
- Prove point idempotency and conflict behavior; deterministic counter sum, last-value gauge, and cumulative histogram aggregation on epoch-aligned buckets without zero filling or interpolation.
- Prove exact descriptor-version isolation, stable ordering/cursors, bounded queries, matcher restrictions, and `query_too_broad` failures instead of silent truncation.
- Prove structured logs inherit trace/run correlation, redact sensitive and oversized fields, remain valid NDJSON, and never leak prompt/raw response/API-key fixtures.

**Implementation**

- Build registry-bound Counter/Histogram/Gauge handles and an `InMemoryTelemetryStore` implementing trace/log/metric query Protocols.
- Add a StructuredLogger with injectable clock/ID source and deterministic file/in-memory sinks.
- Keep dropped-telemetry metrics bounded and non-recursive when the telemetry path itself fails.
- Define one telemetry-store conformance suite that both the in-memory and local adapters must pass.

**Verify**

```bash
uv run pytest tests/runtime/observability/test_metrics.py tests/runtime/observability/test_in_memory_store.py tests/runtime/observability/test_logs.py -q
```

## Task 4: Implement Restart-Readable Local Telemetry Storage

**RED tests**

- Two independent store instances/process-style connections see committed spans/logs/points after reopen under SQLite WAL.
- Stable pagination excludes writes beyond the retained read snapshot, binds cursor to canonical query/authz/retention state, and rejects expired/tampered cursors.
- Enforce configured time range, page, series, point, span-count, and response-byte caps before returning data.
- Retention removes only expired telemetry and preserves exact descriptors required by retained points, SLOs, alerts, saved queries, and live cursors.

**Implementation**

- Add an independent SQLite telemetry schema and `LocalTelemetryStore`; do not enlist best-effort telemetry in authoritative M4a UnitOfWork commits.
- Store canonical payloads and indexed query columns; expose only the frozen DTOs, never SQLite/Tempo/Loki/PromQL-specific shapes.

**Verify**

```bash
uv run pytest tests/runtime/observability/test_local_store.py -q
```

## Task 5: Add Cost, Catalog, Routing, and Decision Persistence

**RED tests**

- Alembic upgrade from the real M4a head preserves all existing rows; downgrade/upgrade is deterministic.
- Freeze and retrieve budgets, budget-set snapshots, reservations, usage, permits, model catalogs, routing policies, and decisions without mutable overwrite.
- Enforce unique idempotency/request identities, routing variant referential integrity, exact historical digest resolution, and bounded stable queries.
- Prove all new repositories share the M4a UoW connection through `TransactionCapabilities.cost`; no repository commits independently.

**Implementation**

- Add narrow Alembic revisions and SQLAlchemy rows/indexes/constraints required by CostLedger and routing persistence.
- Implement tx-bound repositories plus read services; wire the existing reserved cost capability without changing other M4a capabilities.

**Verify**

```bash
uv run pytest tests/runtime/test_persistence_migration.py tests/runtime/cost/test_repository.py tests/runtime/model_router/test_routing_repository.py -q
```

## Task 6: Implement Persistent CostLedger Reservation and Settlement

**RED tests**

- Freeze run/principal/system budgets and create the run hold atomically; any scope conflict/exhaustion leaves no partial snapshot, hold, or reservation.
- Concurrent reserve attempts cannot exceed any budget. Child call/step reservations allocate only from matching parent hold balance and do not double-increment Budget.reserved.
- Reconcile one real observation exactly once across all scopes; release unused allocation; preserve per-call identity without summing scope copies as separate calls.
- Unknown usage becomes `held_unknown`, survives restart, settles conservatively without releasing unknown cost, and accepts one idempotent late actual adjustment without double counting.
- New reservation rejects stale fencing/deadline; already incurred usage still reconciles after lease expiry; idempotency key with a different canonical request conflicts.
- Closing a run hold releases only unallocated balance and leaves no orphan reservation after every terminal path.

**Implementation**

- Implement `freeze_budget_set`, `reserve_many`, `reconcile_group`, `settle_unknown_group`, `late_reconcile_group`, and `close_hold_group` with stable multi-budget lock/update order.
- Use current-row OCC for shared budgets after freeze; never retain the frozen revision as a permanent lock.
- Keep authoritative ledger failures fail-closed and separate from telemetry failure handling.
- Exact `reserve_many` replay still revalidates the current fence/deadline and returns the retained status. The M4c call-admission composition must require `reserved` before starting new work; a retained terminal status is history, not fresh execution permission.

**Verify**

```bash
uv run pytest tests/runtime/cost/test_ledger.py tests/runtime/cost/test_recovery.py tests/runtime/cost/test_concurrency.py -q
```

## Task 7: Implement Concurrency Permits and Wire M4a Run Seams

**RED tests**

- Acquire one all-scope PermitGroup per current lease, respecting `concurrent_run` limits without adding that dimension to reserved/consumed usage.
- Renew/release/expire validates lease and fencing; a retry claim obtains a new group, stale workers cannot retain capacity, and every terminal/reaper path releases or expires it.
- Run create enforces exact equality among payload, RunRecord, and ledger budget-set snapshot/hold IDs.
- Run admission, claim, retry, cancellation, timeout, recovery, and terminal publication each fail atomically when their required ledger action fails.

**Implementation**

- Implement acquire/renew/release/expire PermitGroup operations and the M4a `RunAdmissionCostManager`/`AttemptCostAccounting`/retry-budget adapters.
- Integrate only at existing M4a lifecycle seams; do not add worker polling, HTTP, or application composition.

**Verify**

```bash
uv run pytest tests/platform/m4b/test_run_cost_integration.py tests/runtime/cost/test_permits.py tests/platform/m4/test_run_create_claim.py tests/platform/m4/test_run_fencing.py tests/platform/m4/test_run_retry_cancel_timeout.py tests/platform/m4/test_run_event_atomicity.py -q
```

## Task 8: Implement Typed Observations, Cassette V2, and Exact Caches

**RED tests**

- Parse V2 token/cache/latency observations with reported-vs-unavailable semantics; map legacy zero/empty values to unavailable without changing V1 bytes.
- Write/read `cassette@2` and `model-router@2`; preserve exact request hashes, model snapshot, source, route refs, prefix directives, and recorded provider latency.
- Deterministically verify legacy raw wire, field-presence mapping, request/profile/policy/schema bindings, call evidence, manifest identity, and three-level bundle closure; only fully verified imports may enter M4 REPLAY, while evidence-missing imports remain non-executable and structural contradictions raise IntegrityViolation.
- REPLAY returns only the exact cassette record and misses fail closed; it charges recorded observations or a configured conservative upper bound, never local execution latency as provider latency.
- Full-response cache hits only the complete request hash. Two requests sharing a prefix but differing in any suffix do not share a response; provider prefix cache directives still invoke transport.
- Cache entries bind model/catalog/policy/request/response digests and reject stale or mismatched provenance.

**Implementation**

- Add additive V2 readers/writers, deterministic verified-import reader/importer, and compatibility adapters in cassette/router runtime modules. Actual Run/Artifact publication composition remains M4c.
- Preserve the existing session full-request cache behavior; add explicit prefix directive handling without a second response lookup path.

**Verify**

```bash
uv run pytest tests/contracts/test_cassette.py tests/runtime/cassette tests/runtime/model_router/test_cache_v2.py -q
```

## Task 9: Implement Model Catalog, Routing Policy, PriceBook, and Decisions

**RED tests**

- Validate catalog/policy digests, active status, capabilities, context/output bounds, deterministic rule matching, and single-match readiness.
- Enforce provider-qualified canonical model identity namespace/global uniqueness and close legacy structured ModelSnapshot normalization against the exact verified import binding.
- Persist each native RoutingDecision before execution with exact catalog/policy/budget snapshot and allowed fallback evidence; REPLAY reproduces its recorded model choice.
- Reject any fallback outside the ordered policy chain or any decision whose model/ref/digest differs from Run execution bindings.
- Price lookup requires an exact provider/model/effective interval and currency; default local PriceBook returns unavailable and never labels a tier cheaper.
- Verified legacy replay creates only the frozen `LegacyImportRoutingDecisionV1` path, never a fabricated historical native decision.

**Implementation**

- Add deterministic platform cost/routing policy services and repository-backed decision recording.
- Keep model names configuration-driven; do not hard-code `opus4.8` or `gpt-5.6-sol` in routing logic.

**Verify**

```bash
uv run pytest tests/platform/m4b/test_routing_policy.py tests/runtime/model_router/test_routing_v2.py tests/runtime/cost/test_price_book.py -q
```

## Task 10: Implement Typed Retry and Infrastructure Circuit Breakers

**RED tests**

- Retry only explicitly transient, idempotent failures; honor total UTC deadline, Retry-After, remaining budget, injected monotonic sleeper/jitter, and charge/span every transport attempt.
- Never retry permanent validation/auth/quota failures and never allow a backoff to start beyond the deadline.
- Prove closed/open/half-open transitions under a deterministic rolling window and clock, one bounded half-open probe, and typed `DependencyUnavailable` while open.
- Solver `unknown`/`unproven`, deterministic validation failure, and product-level rejection do not increment an infrastructure breaker; model fallback is requested only through RoutingPolicy.

**Implementation**

- Replace router-wide `except Exception` retry with a versioned FailureClassifier and injected retry executor.
- Add dependency-scoped in-process breaker state and hook it around the existing external model transport only. Keep the generic failure-classification seam for later external solver/simulation adapters; current deterministic solver `unproven` and validation paths are not connected to a breaker.

**Verify**

```bash
uv run pytest tests/runtime/model_router/test_retry.py tests/runtime/reliability/test_breaker.py tests/runtime/model_router/test_router.py -q
```

## Task 11: Implement SLO Evaluation and Alert State Machine

**RED tests**

- Evaluate eligible/good/total against exact descriptor refs, workload profile, objective, rolling window, minimum samples, and explicit missing/late-data policy.
- Freeze the complete versioned definition/evaluation path for checker/sim/50-task-regression SLOs and reject a thresholdless or windowless definition as not an SLO. M4e §7.H supplies the independently measured baseline values and freezes the initial numeric configurations before production acceptance.
- Replay pending → firing → resolved transitions with injected UTC clock, `for` duration, severity, dedup key, cooldown, and stable idempotency.
- Missing data follows the configured policy; sink failure does not rewrite alert state. In-memory/file sinks return delivered/duplicate/failed deterministically.
- ONLINE provider/service SLOs exclude REPLAY local duration; cassette-recorded provider latency may feed historical cost/bench distributions but may not be relabeled as current online latency.

**Implementation**

- Add platform SLO evaluator and persistent alert state repository over metric queries.
- Publish every SLO definition through `SLODefinitionService`: retain its exact metric descriptors before opening the authoritative SLO UnitOfWork; a retention failure prevents publication, while a later authority failure may conservatively leave an orphan retention pin rather than attempting a cross-store pseudo-transaction.
- Before readiness permits telemetry retention purge, call `SLODefinitionService.reconcile_retention`: list all authoritative v1 definitions in one stable bounded read, then atomically re-pin their stable-unique exact descriptor refs in the telemetry store. Listing overflow, authority failure, or pin failure makes readiness fail and skips purge; no cross-store compensation/outbox is introduced.
- Implement InMemoryAlertSink and deterministic NDJSON FileAlertSink; retain real external delivery for M4e/v-next.

**Verify**

```bash
uv run pytest tests/platform/m4b/test_slo.py tests/platform/m4b/test_alerts.py tests/runtime/slo/test_repository.py -q
```

## Task 12: Close M4b Integration and Repository Gates

**RED/integration tests**

- Cross-carrier parent/child trace, fake-clock duration, and replay execution-vs-recorded-provider latency separation.
- Concurrent reservation never exceeds limits; crash recovery neither loses nor doubles accounting; stale fencing cannot reserve or publish but incurred usage still settles.
- Same prefix/different suffix never hits full response cache; breaker ignores unproven; high-cardinality labels reject; exporter failure leaves checker result unchanged.
- Cost and metrics separately retain logical call, full-response-cache, cassette-replay, provider-prefix-cache, transport-attempt, and retry counts; multi-scope Usage copies never inflate the logical-call count.
- SLO missing/late data and alert state replay deterministically; audit, lineage, trace, routing, cost, and Run IDs remain correlatable without entering canonical artifact hashes.
- Preserve all V1 cassette/router fixtures and all M4a tests; no M4c/M4e import or dependency leakage.

**Verification gates**

```bash
uv run pytest tests/contracts/m4b tests/runtime/observability tests/runtime/cost tests/runtime/reliability tests/runtime/slo tests/runtime/cassette tests/runtime/model_router tests/platform/m4b -q
uv run pytest -q
uv run lint-imports
uv run pytest tests/test_dependency_lint.py -q
uv run ruff check .
# Run `ruff format --check` over every Python file touched by M4b. The inherited
# repository baseline is not formatter-clean, so unrelated historical files are
# deliberately excluded from this slice gate.
git diff --check
```

## Completion Evidence

Completed on 2026-07-14 against M4a baseline `f9970fdb30e67f4a0246dd1fd3b8f7b51967946b`.

- M4b focused contracts/runtime/platform suite: `290 passed, 1 skipped`. The only skip is the explicitly opt-in live gateway test; default verification remained zero-network.
- M4a/M4b compatibility slice: `1317 passed`.
- Full repository, partitioned without overlap: non-Bench `1995 passed, 1 skipped`; Bench `761 passed`; combined `2756 passed, 1 skipped`.
- Persistence migration and cost/routing/SLO repositories: `25 passed`, including `0001 -> head -> 0001 -> head`, empty-authority downgrade, non-empty-authority fail-closed, and runtime-metadata/head checks.
- Dependency gates: `lint-imports` analyzed 331 files / 1933 dependencies with `7 kept, 0 broken`; dependency-lint `27 passed`.
- Static gates: `ruff check .` passed; all 97 Python files touched by M4b passed `ruff format --check`; `git diff --check` passed. The inherited repository-wide formatter baseline was not rewritten.
- Compatibility evidence: all V1 router/cassette tests passed in the focused and full suites, verified legacy imports remained explicitly `legacy_import`, and `git diff --name-only -- cassettes` was empty, so tracked cassette fixture bytes/digests were unchanged.

This completes only the frozen M4b slice. M4c API/worker composition, M4d Console, and M4e production adapters/DR remain unimplemented and retain their original acceptance gates.
