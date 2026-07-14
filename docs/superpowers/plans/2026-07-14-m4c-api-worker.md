# M4c API Gateway and Persistent Worker Implementation Plan

> **Execution rule:** implement task by task with TDD. A task is complete only after its focused tests pass and its stated invariants are exercised. M4c is not complete until Journey A, Journey B, the constraint-publication journey, and the repository-wide gates in the final tasks pass.

**Goal:** Implement the frozen M4c service surface: local identity and sessions, the versioned FastAPI `/api/v1` contract, bounded authorized reads, persistent Run admission and worker execution, exact RunKind/terminal-publication registries, resumable SSE and durable WS commands, and the three required zero-network end-to-end journeys.

**Architecture:** `gameforge.contracts` owns pure wire DTOs and Protocols; `gameforge.runtime` owns SQLite/local/crypto/transport mechanisms; `gameforge.platform` owns identity, authorization, workflow, Run admission, registry validation, publication, and business orchestration; `gameforge.apps.api` and `gameforge.apps.worker` are the two composition entries of one modular-monolith artifact. The API never becomes the only workflow guard, and the worker never bypasses M4a fencing or M4b cost/cassette/trace controls.

**Truth sources:**

1. `docs/superpowers/specs/2026-07-03-gameforge-prd.md`
2. `docs/superpowers/specs/2026-07-03-gameforge-foundations-contracts.md` v0.3
3. `docs/superpowers/specs/2026-07-13-m4-production-hardening-design.md`, especially §§5 and 9

**Exact framework pins:** Python 3.12; `fastapi==0.139.0`; `uvicorn==0.51.0`; `argon2-cffi==25.1.0`; `pydantic==2.13.4` (the current lock version); SQLAlchemy 2/Alembic/SQLite WAL; httpx; canonical SHA-256 JSON; pytest/Hypothesis/import-linter/Ruff. Update `uv.lock` once in Task 1 and keep the resolved framework graph fixed for the OpenAPI artifact.

## Scope Boundary

M4c implements:

- the complete local password, API-key, session, CSRF, bootstrap, and current-role authentication path; OIDC DTOs and Protocols are fixed now, while a real OIDC provider remains deferred;
- all `/api/v1` resources frozen in §5.3, RFC 9457 errors, exact idempotency/OCC, bounded authorized pagination, liveness/readiness, and OpenAPI plus SSE/WS schemas;
- the complete active RunKind registry, referenced policy registries, exact execution-profile resolution, Run admission, DB-authoritative dispatch, worker leases/fencing, terminal publication, model/cassette/cost/trace bridges, and all stage-correct M4c handlers;
- resumable SSE and durable REST/WS Run commands;
- Journey B, Journey A, and constraint-publication happy/failure E2E paths with two independent human sessions and zero external network.

M4c does not implement React pages or visual design (M4d), PostgreSQL/S3/OTLP/Prometheus production adapters, OIDC network integration, WORM/external anchoring, DR execution, artifact migration execution, solver process isolation, general external-ingestion sanitization, containers, deployment, or capacity baselines (M4e). `artifact.migrate@1` and `dr.drill@1` are nevertheless registered with complete contracts and trusted internal creation boundaries; their M4c executors return a typed, versioned unavailable failure and can never fabricate a success Artifact. M4e replaces those executor seams with real success paths without changing the RunKind/API contract.

## Compatibility and Correctness Rules

- Preserve all M0-M4b public imports, hashes, stored rows, committed cassette bytes, and V1 readers. New schema is additive migration `0008`; it never rewrites historical authority.
- `Problem` remains one class and one truth source in `gameforge/contracts/jobs.py`; `contracts/api.py` re-exports it. Do not create a second structurally similar Problem model.
- Keep existing Run/Outcome/manifest types in `contracts/jobs.py`. `contracts/api.py` contains only transport request/response/SSE/WS DTOs; auth records live with identity contracts, and playtest/config-export domain payloads live in dedicated contract modules with compatibility re-exports where needed.
- Human authenticates only through password/OIDC to a session; service authenticates only through API key; system is created only by a trusted composition root and is impossible to authenticate from HTTP.
- Authentication reloads current Principal, credential status/version, and active roles. Tokens never carry authoritative roles. Mutations reauthorize inside the authoritative UnitOfWork after loading the real resource.
- Every command except login uses a server-scoped idempotency key plus canonical request hash and OCC revision. A duplicate exact request replays its committed result; the same key with another payload is `409 idempotency_conflict`.
- All collection responses are bounded and snapshot-consistent. Mutable filtered lists materialize the authorized canonical view; immutable/append-only lists use a retained high-watermark. No endpoint silently truncates an over-broad query.
- SSE earliest cursor is derived from retained persistent RunEvents and retention state. Do not add a speculative `earliest_cursor` authority column that can diverge from the event store.
- The SQLite RunStore is the queue authority. In-process notification is only a latency hint. Execution is at least once; fencing gives exactly-once publication and idempotent accounting, not exactly-once execution.
- Worker output is always one sealed `PreparedRunOutcome`; only the terminal publisher may create authoritative Artifacts, Finding revisions, workflow transitions, result/failure manifests, RunEvents, audit, and cost closure.
- `executor_key`, terminal hooks, workflow effects, completion-oracle keys, and handler keys resolve only through trusted registries. They are never client callables/import paths.
- Every LLM call uses the M4b router/cassette/cost bridge, reserves before use, publishes the exact rendered source before invocation, records every attempt, and reconciles usage. REPLAY misses fail closed; no test journey uses live network.
- Do not introduce M4d frontend files or M4e production adapters while closing M4c.

## File Map

| Area | Planned files |
|---|---|
| Contracts | new `gameforge/contracts/api.py`, `auth.py`, `playtest.py`, `config_export.py`; additive compatibility exports and bounded payload closure in `identity.py`, `jobs.py`, `errors.py` |
| Identity/runtime | new `gameforge/runtime/auth/`; additive rows/repository in `runtime/persistence/models.py`, `auth.py`, migration `0008_auth_sessions.py`; generic signing-key access in `runtime/secrets/` |
| Platform | new `gameforge/platform/identity/`, `registry/`, `read_models/`, `publication/`, `run_handlers/`; narrow additions to existing approvals/runs/provenance seams |
| API | new `gameforge/apps/api/` with app factory, middleware, dependencies, error mapping, pagination, streaming, and resource routers |
| Worker | new `gameforge/apps/worker/` with dispatcher, lease heartbeat/reaper, executor registry, runner, terminal bridge, and entry point |
| CLI | additive trusted `identity bootstrap` composition under `gameforge/apps/cli/` |
| Frozen artifacts | canonical OpenAPI and versioned SSE/WS JSON Schemas under `docs/api/` |
| Tests | new `tests/contracts/m4c/`, `tests/runtime/auth/`, `tests/runtime/persistence/test_auth_repository.py`, `tests/platform/m4c/`, `tests/apps/api/`, `tests/apps/worker/`, `tests/e2e/m4c/` |

## Task 1: Close the M4c Contract Surface and Pin Its Runtime

**RED tests**

- Freeze redacted secret types, password/API-key/session/OIDC DTOs and Protocols, auth errors, login-name/password/session policies, and canonical policy digests.
- Freeze transport-only auth requests/responses, `RunAccepted`, `RunView`, Approval request/view projections, bounded query DTOs, SSE envelope/wire fields, and WS frames.
- Freeze `ConfigExportPackageV1` deterministic path/file framing and `ScenarioSpecV1`, `TaskSuiteV1`, completion-oracle registry, reset bindings, and exact cross-field invariants.
- Close every §5 Run payload/result/report bound and discriminator. Preserve existing `jobs.py` imports for types moved to a semantic module.
- Assert `contracts.api.Problem is contracts.jobs.Problem`; reject duplicate Problem schemas or divergent JSON Schema refs.

**Implementation**

- Add only pure Pydantic types/Protocols and digest helpers. Keep handler implementations and framework imports out of contracts.
- Add typed transport errors to `contracts/errors.py`; never map failures by matching exception strings.
- Use explicit bounds on every string, array, count, query range, page size, and frame.

**Verify**

```bash
uv run pytest tests/contracts/m4c tests/contracts/m4 tests/contracts/m4b -q
uv run pytest tests/test_dependency_lint.py -q
```

### Runtime pinning substep

**RED tests**

- Assert runtime package versions equal the four exact pins and that generated OpenAPI is stable under a clean environment sync.
- Smoke an app factory and a separately invokable Uvicorn API entry without starting a worker in the API process.

**Implementation**

- Update `pyproject.toml` and `uv.lock` exactly once for FastAPI, Uvicorn, Argon2, and exact Pydantic.
- Add empty composition packages and CLI entry seams; do not expose an endpoint until error/auth middleware tests exist.

**Verify**

```bash
uv lock --check
uv run pytest tests/apps/api/test_bootstrap.py tests/apps/worker/test_entrypoint.py -q
```

## Task 2: Add and Implement the Minimal Local Authentication Store

**RED tests**

- Upgrade a populated `0007` database through `0008`, preserve all prior rows, and round-trip downgrade/upgrade under the repository migration policy.
- Retain normalization/hash/session policy documents in the existing immutable `policy_snapshots` authority, and persist only revisioned password credentials, API keys, and sessions with exact unique/index/foreign-key constraints.
- Enforce globally unique normalized login names, digested key/token/CSRF storage, expiry/revoke fields, and CAS revisions.
- Prove credential/session writes share the same SQLite connection as Principal epoch/revision and audit writes.
- Prove `0008` creates exactly three auth tables and no OIDC transaction/binding/provider, bootstrap sentinel, policy, role-claims, or session-claims table.

**Implementation**

- Add `0008_auth_sessions.py`, exactly three auth table models in `models.py`, and tx-bound repositories in `runtime/persistence/auth.py`.
- `password_credentials`: PK `credential_id`; principal FK; globally unique normalized login; exact normalization/hash policy refs, credential version/status/change time/revision; `(principal_id,status,credential_id)` index. Do not make `principal_id` unique.
- `api_keys`: PK `api_key_id`; principal FK; non-authoritative display prefix; unique secret digest; credential version/status/create/optional expiry+revoke/revision; `(principal_id,status,api_key_id)` index. Add no prefix index unless the implemented lookup actually uses it.
- `sessions`: PK `session_id`; principal FK; polymorphic `source_credential_id` without a password-table FK; credential version, unique token digest, CSRF digest, signing-key ID, issue/absolute+idle expiry/last-seen, optional revoke fields/revision; exact principal-expiry and source-credential indexes. Store the session-policy version only in the signed opaque token as frozen by the contract; do not add an unfrozen projection column.
- Reuse `SqlPolicySnapshotRepository` for exact retained policy documents. Signing keys remain in the injected secret provider and all plaintext secrets remain memory-only.
- Extend `TransactionCapabilities` only with the identity/auth/idempotency/policy capabilities required by same-UoW services, as optional additive fields that preserve all existing constructors; stop borrowing unrelated capability slots.

**Verify**

```bash
uv run pytest tests/runtime/test_persistence_migration.py tests/runtime/persistence/test_auth_repository.py tests/runtime/persistence/test_uow.py -q
```

### Credential mechanism substep

**RED tests**

- Normalize NFKC/Unicode whitespace/casefold exactly; reject forbidden categories, length violations, and policy-migration collisions.
- Hash and verify with the frozen Argon2id policy; successful login can CAS-rehash under a newer policy without weakening failure behavior.
- Issue API-key/session/CSRF plaintext once, persist only digests, redact repr/serialization/logging, and reject secret recovery.
- Verify signed opaque session tokens against active/grace key sets, absolute/idle expiry, CAS touch interval, credential version/epoch, revoke, and Principal disable.
- OIDC Protocol conformance covers state/nonce/PKCE/redirect allowlist and single-consumption semantics using a deterministic fake only; there is no real network adapter or DB table.

**Implementation**

- Add `runtime/auth/passwords.py`, `tokens.py`, `local.py`, and a generic env/injected signing-key provider under `runtime/secrets/`.
- Keep crypto mechanisms below platform orchestration and inject clock, random bytes, and key sets for deterministic tests.

**Verify**

```bash
uv run pytest tests/runtime/auth -q
```

## Task 3: Implement Identity Management, Sessions, and Bootstrap

**RED tests**

- Bootstrap on an empty identity store creates exactly one human admin with `identity_admin + tooling`; concurrent attempts yield one winner.
- Create/disable principal, grant/revoke role, issue/rotate/revoke credential/session all require exact current permissions and revisions, update the specified epoch/revision, and append audit in one UoW.
- Enforce human/password, service/API-key, and system/trusted-internal kind boundaries at every service entry.
- Password failures are externally indistinguishable while internal typed reasons remain restricted.
- Every session/API-key resolve rebuilds current `Principal` and `ActorContext`; old role claims and disabled credentials fail immediately.

**Implementation**

- Add `platform/identity/authentication.py`, `sessions.py`, `management.py`, `bootstrap.py`, ports, and the trusted CLI adapter.
- Use one platform service from CLI and API; never provision by direct SQL.

**Verify**

```bash
uv run pytest tests/platform/m4c/test_identity_services.py tests/platform/m4c/test_bootstrap.py tests/apps/test_identity_cli.py -q
```

## Task 4: Build Exact Registries and Readiness Validation

**RED tests**

- Materialize all 14 frozen RunKind definitions and exact policy closures: outcome, lineage, version transition, runtime parents, finding output, retry/classifier, execution profile, completion oracle, event, and migration matrix references.
- Recompute all digests, reject duplicates/overlapping selectors/wildcards/missing hooks, and retain historical versions referenced by Runs/Artifacts.
- Resolve every executor, terminal hook, workflow effect, and completion-oracle key through an explicit trusted map; a missing or extra active mapping fails readiness.
- Validate all RunKind creation modes, permissions, LLM modes, seeds, commands, payload schemas, and stage-correct success/failure policies against the frozen tables.
- Register `artifact.migrate@1` and `dr.drill@1` to typed unavailable executors. Assert they can only end through the frozen failure path and cannot publish migration/DR success evidence in M4c.

**Implementation**

- Add immutable registry builders/repositories and a single `PlatformReadinessValidator` under `platform/registry/`.
- Reuse exact policy snapshots; never synthesize a missing historical version from a current default.

**Verify**

```bash
uv run pytest tests/platform/m4c/test_registry.py tests/platform/m4c/test_readiness_registry.py -q
```

## Task 5: Build the FastAPI Shell, Authentication Endpoints, and Health

**RED tests**

- Lock actual Starlette execution order: request-id/trace/error wrapping, authn/CSRF, resource authz dependency, handler.
- Wrap framework 400/404/405/422 and typed domain failures as RFC 9457 `application/problem+json` with stable codes; redact integrity internals.
- Prove cost is absent from HTTP middleware and GET requests never reserve Agent budget.
- `/livez` touches no dependency. `/readyz` checks migration head, DB/ObjectStore/CostLedger, registry closure, M4b SLO retention reconciliation, and cached latest AuditGate state without scanning the chain per probe.

**Implementation**

- Add `apps/api/app.py`, `middleware.py`, `errors.py`, `dependencies.py`, `health.py`, and injected composition configuration.
- Make request/trace IDs server-owned and pass `ActorContext` explicitly into platform commands.

**Verify**

```bash
uv run pytest tests/apps/api/test_middleware.py tests/apps/api/test_errors.py tests/apps/api/test_health.py -q
```

### Authentication endpoint substep

**RED tests**

- Two independent HTTPS clients log in to isolated HttpOnly/Secure/SameSite sessions; `/auth/me` returns current identity and no secret.
- Session-authenticated unsafe methods require the synchronizer CSRF token; API-key requests do not gain browser-session semantics.
- Logout revokes by CAS, clears the cookie, and is idempotent only for the exact request.
- Principal/credential disable, password/key rotation, role revoke, stale epoch, bad CSRF, and disallowed WS Origin take effect without cache lag.
- HTTP credentials can never create a system ActorContext.

**Implementation**

- Add `apps/api/routers/auth.py` and auth dependencies. Build actor/session state exclusively from platform identity services.

**Verify**

```bash
uv run pytest tests/apps/api/test_auth.py tests/apps/api/test_csrf.py tests/apps/api/test_auth_invalidation.py -q
```

## Task 6: Implement Bounded Authorized Read Resources

**RED tests**

- Immutable/append-only reads hold a high-watermark; mutable approval/run/session lists materialize the complete bounded authorized canonical view.
- Cross-connection inserts/status changes cannot create duplicate, missing, or drifting pages inside a retained snapshot.
- Cursor signature binds API/resource/filter/sort/projection/page size/principal/authz fingerprint/retention; tamper, cross-principal reuse, permission change, and expiry fail with the exact status/code.
- Over-broad graph/diff/log/metric/list queries return `422 query_too_broad`, never silent truncation.

**Implementation**

- Add a reusable but narrow `runtime/persistence/read_views.py` and `apps/api/pagination.py`; use existing `ReadSnapshotRow/MaterializedReadItemRow/CursorSigner`.
- Encode the structured signed cursor as one opaque transport token. Do not keep cross-request DB transactions open.

**Verify**

```bash
uv run pytest tests/runtime/persistence/test_read_views.py tests/apps/api/test_pagination.py -q
```

### Resource router substep

**RED tests**

- Cover every bounded read from §5.3: specs/graph/schema registry, constraints/proposals, reviews/findings/revisions, task suites, patches, rollback requests, artifacts/lineage/ref history, approvals, runs/findings/commands, bench report, execution profiles, traces/spans/logs/metrics/cost.
- Load each singular resource before computing its exact permission/domain; filter each collection by the same pure RBAC function and current exact policy.
- Return revision/ETag where frozen, hide prompts/raw responses/secrets/internal handler config, and preserve exact Finding/Run/Artifact revisions.

**Implementation**

- Add resource-specific routers and query services; do not create one untyped generic CRUD router.
- Reuse M4a/M4b repositories and DTOs, adding only missing bounded query methods.

**Verify**

```bash
uv run pytest tests/apps/api/test_read_resources.py tests/apps/api/test_observability_queries.py -q
```

## Task 7: Implement Synchronous Workflow Commands

**RED tests**

- Cover human spec upload, Patch draft, human constraint draft/revision, validation submit, approval submit/decision, apply/publish, rebase/conflict resolution, rollback request/apply, and exact request/ETag/CAS/idempotency behavior.
- Approval decision derives actor/time/ID on the server, supports partial requirements, rejects proposer self-approval, and reloads exact policies/current roles in the UoW.
- Rollback cannot bypass request, validation, submit, independent approval, and apply.
- Blob-first writes may leave only verified GC-eligible orphans when DB publication fails; CPU-heavy patch/diff computation stays outside the write transaction.

**Implementation**

- Add mutation routers that call existing approval/diff/apply services and new narrow platform composition services.
- Keep every authority transition and audit append in one UoW; API validation is not the sole guard.

**Verify**

```bash
uv run pytest tests/apps/api/test_workflow_commands.py tests/apps/api/test_approvals.py tests/apps/api/test_rollback.py -q
```

## Task 8: Implement Resource-Specific and Generic Run Admission

**RED tests**

- `POST /runs` accepts only `generic_runs_endpoint`; every resource endpoint fixes its own kind/version; `internal_only` is callable only by trusted authorized services.
- Validate exact Artifact input set, resource-derived domain, profile catalog bindings, LLM mode, seed derivation, ref/subject revisions, task-suite/config/environment bindings, and current registry closure before admission.
- Generation/constraint text becomes authenticated `source_raw` Artifact before Run creation; trust/purpose/source kind are server-assigned and naked text never enters Run payload or telemetry.
- One UoW creates RunRecord, initial event, budget hold, audit, and DB queue authority; failure leaves no executable Run or partial reservation.
- Return only `202 RunAccepted`; asynchronous failures become RunFailure/Event, never retroactive HTTP errors.

**Implementation**

- Add `platform/runs/admission.py`, trusted authenticated-goal provenance composition, and resource endpoint adapters.
- Wire all Run creation endpoints from the §5.3 table, including generation, repair, constraint propose/validate, review/checker/sim/bench, task-suite, playtest, and rollback validation.

**Verify**

```bash
uv run pytest tests/platform/m4c/test_run_admission.py tests/apps/api/test_run_admission.py -q
```

## Task 9: Implement the Generic Terminal Publication Engine

**RED tests**

- Select a unique immutable publication plan from exact classifier/retry/current state and mutually exclusive policy selectors; reject any gap/overlap.
- Allocate every Prepared Artifact exactly once across attempt/run policies; verify blob kind/schema/hash/location, count/identity bindings, dispositions, typed lineage roles, VersionTuple projection, and runtime/cassette parents.
- Publish attempt failures before final run aggregate, never consume business evidence at attempt scope, and aggregate every closed-attempt manifest exactly once.
- Atomically publish Artifacts, Finding revisions/links, workflow effects, RunResult/RunFailure, terminal Event, audit, cassette closure, and cost closure; any failure rolls back all authority.
- Prove stale fencing, subject supersede, duplicate publication, mismatched prepared counts, and fabricated worker metadata fail closed.

**Implementation**

- Add `platform/publication/planner.py`, `validator.py`, `publisher.py`, lineage/version/finding adapters, and explicit workflow-effect registry.
- Keep `PreparedRunOutcome` non-authoritative until this engine commits it.

**Verify**

```bash
uv run pytest tests/platform/m4c/test_publication_plan.py tests/platform/m4c/test_terminal_publisher.py -q
```

## Task 10: Compose the Persistent Worker and M4b Execution Bridge

**RED tests**

- Separate API and worker processes discover committed Runs through DB scanning; lost hints and restarts do not lose work.
- Claim/heartbeat/reaper/shutdown obey lease fencing; heartbeat remains live while blocking checker/sim/Agent work runs off the event loop.
- Every attempt creates trace context from the carrier, reserves cost before model/step work, records routing decisions and transport attempts, reconciles typed usage, and publishes cassette shards/bundles.
- Every actual LLM invocation first publishes canonical `source_rendered`; stale workers cannot start a new reserve or publish, while incurred usage still settles.
- Executor exceptions become classified redacted PreparedRunFailure and flow through terminal policy rather than escaping the worker loop.

**Implementation**

- Add `apps/worker/dispatcher.py`, `heartbeat.py`, `runner.py`, `model_bridge.py`, `terminal.py`, `app.py`, and entry point.
- Use an injected bounded executor pool for blocking work; in-process signals remain hints only.

**Verify**

```bash
uv run pytest tests/apps/worker/test_dispatcher.py tests/apps/worker/test_runner.py tests/apps/worker/test_model_bridge.py -q
```

## Task 11: Implement Deterministic, Composite, and Agent Run Handlers

**RED tests**

- `checker.run@1`, `simulation.run@1`, and `bench.run@1` use exact profiles/input sets, deterministic seeds, bounded outputs, and the frozen Finding policies.
- `review.run@1` always separates deterministic findings from optional LLM suggestions; `llm_triage_policy=null` requires `not_applicable`, otherwise the recorded mode is exact.
- Bench `execute_cases` versus `aggregate_results` enforces its mode/input rules and one ordered run-scoped cassette for Agent cases.
- Handlers return only valid PreparedRunOutcome and cannot directly mutate Artifact, Finding, workflow, ref, audit, or Run terminal state.

**Implementation**

- Add stage-specific adapters under `platform/run_handlers/` over existing checker/simulation/review/bench implementations.
- Keep all correctness verdicts in deterministic graph/ASP/SMT/simulation code; LLM output stays suggestion/proposal.

**Verify**

```bash
uv run pytest tests/platform/m4c/test_checker_handler.py tests/platform/m4c/test_simulation_handler.py tests/platform/m4c/test_review_bench_handlers.py -q
```

### Agent handler substep

**RED tests**

- Generation gate pass returns exact Patch/preview/config exports/gate evidence; gate reject returns evidence-only Patch/preview/evidence with no workflow subject or executable config.
- Repair loads the current SubjectHead, failed/unproven evidence and exact Findings, returns one full combined exact-base superseding Patch only after verifier closure, and returns `repair_unverified` without advancing the head otherwise.
- Agent constraint proposal creates only a draft; it can never become authoritative without a superseding human-authored revision and independent approval.
- All three paths use authenticated raw/rendered source lineage, exact replay cassette, M4b cost and trace bridges, and no live network in tests.

**Implementation**

- Compose existing generation/repair/extraction agents through typed worker adapters and the frozen terminal effects.

**Verify**

```bash
uv run pytest tests/platform/m4c/test_generation_handler.py tests/platform/m4c/test_repair_handler.py tests/platform/m4c/test_constraint_proposal_handler.py -q
```

## Task 12: Implement Config Export, Task-Suite Derivation, and Playtest

**RED tests**

- Config export validates exact profile/environment/constraint/preview bindings, safe NFC relative paths, file hashes/sizes, deterministic framing, and one package Artifact per requested profile.
- Task-suite derivation publishes one non-empty suite plus exact Scenario Artifacts, completion-oracle bindings, typed lineage, reset-schema validation, and stable identities.
- Playtest rejects stale suite/config/constraint/environment/profile/seed bindings, runs the selected non-empty episode subset, accepts only allowed bounded interaction commands, and publishes a trace plus Findings.
- Aureus is only a profile-selected fixture; an unknown environment uses the same contract and fails as typed unavailable rather than a game-specific branch.

**Implementation**

- Add config-export codec, completion-oracle registry, task-suite deriver, and environment/profile-selected Playtest handler.

**Verify**

```bash
uv run pytest tests/platform/m4c/test_config_export.py tests/platform/m4c/test_task_suite.py tests/platform/m4c/test_playtest_handler.py -q
```

## Task 13: Implement Validation Handlers and Workflow Completion

**RED tests**

- Patch validation binds exact subject/target/preview/config/review/playtest/Finding revisions, publishes passed/failed/unproven EvidenceSet, and emits auto-apply proof only for the exact deterministic eligible policy.
- Constraint validation runs at least two exact differential engines, preserves every compile stage/result, conditionally publishes one candidate, and never treats missing execution as passed.
- Rollback validation checks history/artifact/schema/profile/impact/regression bindings and reports passed/failed/unproven as a successful business result.
- Superseded validation results cannot alter the new head; execution/cancel/timeout restores only the still-current matching draft.

**Implementation**

- Add three validation executor adapters and bind their terminal effects to existing `ValidationCompletionService` and rollback guards.

**Verify**

```bash
uv run pytest tests/platform/m4c/test_patch_validation_handler.py tests/platform/m4c/test_constraint_validation_handler.py tests/platform/m4c/test_rollback_validation_handler.py -q
```

## Task 14: Lock M4e-Deferred Internal Run Seams

**RED tests**

- `artifact.migrate@1` validates its complete typed request, exact profile/matrix/source binding, and internal permission, then returns the registered typed unavailable failure without a migration report or migrated Artifact.
- `dr.drill@1` validates its typed request and internal permission, then returns typed unavailable without claiming a backup, restore, timing, RPO, or RTO result.
- Neither handler can be called through generic/resource public endpoints, and neither makes readiness fail merely because the M4e implementation is intentionally absent.

**Implementation**

- Add explicit unavailable executors under `platform/run_handlers/deferred.py`; retain all success-policy contracts and handler keys for M4e replacement.

**Verify**

```bash
uv run pytest tests/platform/m4c/test_deferred_handlers.py -q
```

## Task 15: Implement Resumable SSE and Durable Run Commands

**RED tests**

- Encode exact canonical SSE frames, use persisted seq as ID, and keep heartbeats as comments that do not advance cursors.
- Prove read backlog -> wait hint/poll -> reread DB has no committed-event gap under the boundary race; duplicate transport delivery remains client-deduplicable by `(run_id,seq)`.
- Reconnect with `Last-Event-ID`, API/worker restart, slow-client backpressure, terminal close, and retention expiry behave exactly; `410 earliest_cursor` is computed from the retained events.
- Reauthenticate and reauthorize every connection/reconnect; revoked permission cannot continue a stream.

**Implementation**

- Add `apps/api/streaming.py`, bounded event queries, and a non-authoritative notifier.

**Verify**

```bash
uv run pytest tests/apps/api/test_sse.py -q
```

### WS and REST command substep

**RED tests**

- Authenticate the handshake, validate Origin, bound frame size/sequence/backpressure, and reauthorize the real Run on every message.
- Persist command plus Run mutation/Event/audit before ACK; duplicate exact command returns stable ACK and changed payload/sequence conflicts.
- Worker claim/apply uses its current lease/fence; reaper restores claimed commands to pending and terminal Run rejects all remaining commands.
- Browser never receives lease/fencing tokens. REST cancel and WS cancel call the same `CommandService.submit` path.
- Disconnect recovery uses persisted command views plus SSE events.

**Implementation**

- Add `apps/api/commands.py` and WS router over existing M4a RunCommandService; do not add a direct cancel flag path.

**Verify**

```bash
uv run pytest tests/apps/api/test_ws_commands.py tests/apps/api/test_rest_cancel.py -q
```

## Task 16: Freeze OpenAPI and Streaming Schemas

**RED tests**

- Canonical OpenAPI contains every §5.3 endpoint under `/api/v1`, exact security/cookie/header/error/ETag/idempotency contracts, bounded schemas, and no internal callable/secret/fencing fields.
- Export versioned JSON Schemas for SSE and both WS server frame variants.
- A compatibility checker permits additive changes and rejects removed paths/methods/statuses, narrowed enums, newly required request fields, changed discriminators, and incompatible response/schema changes.
- Regeneration under the exact framework pins is clean.

**Implementation**

- Add `docs/api/openapi-v1.json`, `docs/api/schemas/`, and deterministic generation/check commands under `gameforge/apps/api/schema.py`; `python -m gameforge.apps.api.schema --check` must regenerate in memory and compare canonical bytes without rewriting the working tree.

**Verify**

```bash
uv run pytest tests/apps/api/test_openapi.py tests/apps/api/test_streaming_schema.py -q
uv run python -m gameforge.apps.api.schema --check
```

## Task 17: Prove Journey B End to End

**RED/E2E tests**

- Two independent session clients: human A submits a hand-written Patch and preview, validates exact invariant/economy evidence, submits; human B with current domain permission approves; apply moves ref/history; rollback request repeats validate/submit/B approve/apply.
- Failure Patch produces Finding + failed EvidenceSet, blocks submit/apply, and leaves ref/history unchanged.
- Cover A self-approval rejection, stale workflow/ref revisions, idempotency conflict, audit/lineage/trace/log/cost correlation, API/worker restart, and no external network.

**Implementation**

- Add deterministic fixtures/cassettes only where the journey actually uses an Agent. Do not bypass public API, worker, or terminal publisher.

**Verify**

```bash
uv run pytest tests/e2e/m4c/test_journey_b.py -q
```

## Task 18: Prove Journey A, Constraint Publication, and Failure Recovery End to End

**RED/E2E tests**

- Two independent sessions execute the exact §5.6 sequence: catalog selection, generation REPLAY, gate SSE, non-empty config export, task-suite derive, review, actual Playtest Agent, exact Findings, failed validation, repair REPLAY, re-derive/re-review/re-playtest, passed validation, B approval, apply, Eval/Observability reads.
- Old Patch/evidence/approval cannot carry to the repaired revision; old task suite against the new config is `409 stale_task_suite`; target ref does not move before final apply.
- Gate-rejected fixture publishes only evidence and RunFailure; playtest-stuck/repair-unverified creates no new head/item; all workflow commands fail closed.
- Tests deny external egress and reject skip/not-applicable/mock UI or a detached fixture as Journey A success.

**Verify**

```bash
uv run pytest tests/e2e/m4c/test_journey_a.py -q
```

### Constraint-publication and cross-journey failure substep

**RED/E2E tests**

- Agent proposal and human typed proposal are distinct entry paths; both require a superseding human-authored revision, exact compile/differential validation candidate, submit, B approval, and ref-CAS publish.
- Missing human revision, failed/unproven validation, digest/ref mismatch, stale revision, proposer approval, and auto-apply bypass are rejected without authority change.
- Cover three-way conflict -> new Patch revision -> revalidation/reapproval, SSE reconnect, lease expiry/reclaim, budget/permit partial-failure prevention, stale-worker publication rejection with incurred-cost settlement, WS dedup/reaper recovery, and unauthorized trace/log reads.

**Verify**

```bash
uv run pytest tests/e2e/m4c/test_constraint_publication.py tests/e2e/m4c/test_failure_matrix.py -q
```

## Task 19: Close M4c Repository Gates

**Integration assertions**

- All 14 RunKind handlers resolve, all active registry policy/hook closures pass readiness, and only migrate/DR return the documented M4e-deferred typed unavailable result.
- API and worker run as separate entries against the same SQLite/ObjectStore/telemetry authority; default tests are deterministic and zero-network.
- M0-M4b compatibility suites, cassette bytes, dependency boundaries, and M3 `qa.evidence_missing` semantics remain unchanged.

**Verification gates**

```bash
uv run pytest tests/contracts/m4c tests/runtime/auth tests/platform/m4c tests/apps/api tests/apps/worker tests/e2e/m4c -q
uv run pytest --ignore=tests/bench -q
uv run pytest tests/bench -q
uv run lint-imports
uv run pytest tests/test_dependency_lint.py -q
uv run ruff check .
# Run `ruff format --check` over every Python file touched by M4c only; do not
# rewrite unrelated inherited formatter debt.
uv lock --check
git diff --check
uv run python -m gameforge.apps.api.schema --check
uv run pytest tests/contracts/test_cassette.py tests/contracts/m4b/test_router_cassette_v2.py tests/bench/narrative/test_narrative_acceptance.py -q
```

## Completion Evidence

Fill only after all tasks pass. Record the M4b baseline commit, focused M4c result, non-Bench and Bench partition totals, migration round-trip, OpenAPI/streaming compatibility result, dependency-lint/import-linter results, touched-file format result, historical-cassette compatibility plus any new explicitly M4c-scoped replay fixtures, and the exact Journey A/B/constraint E2E results. Completion of this document proves only M4c; M4d UI and M4e production/DR adapters remain unimplemented.
