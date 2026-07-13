# M4a Platform Core and Persistence Implementation Plan

> **Execution rule:** implement task by task with TDD. A task is complete only after its focused tests pass and its stated invariants are exercised. M4a is not complete until the slice-wide gates in Task 16 pass.

**Goal:** Implement the M4a platform core from the frozen M4 design: additive lineage/audit/workflow contracts, immutable artifact and object storage, SQLite UnitOfWork semantics, revisioned refs, RBAC and maker-checker approval, field-level diff/rebase/rollback, and persistent fenced Run state.

**Architecture:** Pure wire DTOs and Protocols live in `gameforge.contracts`; deterministic diff/hash/patch computation stays in `gameforge.spine`; SQLite and local-object-store adapters live in `gameforge.runtime`; transactional business guards and workflows live in `gameforge.platform`. Existing M0b-M3 readers and constructors remain supported. M4a freezes later integration boundaries but does not pull M4b cost governance, M4c API/worker composition, or M4e cloud adapters into this slice.

**Truth sources:**

1. `docs/superpowers/specs/2026-07-03-gameforge-prd.md`
2. `docs/superpowers/specs/2026-07-03-gameforge-foundations-contracts.md` v0.3
3. `docs/superpowers/specs/2026-07-13-m4-production-hardening-design.md`, especially §§3 and 9

**Tech stack:** Python 3.12, Pydantic v2, SQLAlchemy 2, Alembic, SQLite WAL, canonical SHA-256 JSON, Hypothesis, pytest, import-linter, Ruff.

## Scope Boundary

M4a implements the local deterministic core and the following M4 acceptance anchors:

- lineage@2/ObjectRef/VersionTuple producer validation, additive legacy readers, immutable artifacts, revisioned refs, audit@2, and exact rollback without a lineage edge;
- SubjectHead/ApprovalItem/Finding/Patch immutable revision semantics, maker-checker/domain routing, validation supersede races, and auto-apply proof guards;
- stable field diff, missing-vs-null, three-way conflicts, and reapproval after rebase;
- SQLite UoW, LocalObjectStore, ObjectBinding/ObjectGc, stable read-snapshot pagination, and integrity failures;
- persistent Run/Attempt/Lease/Event/Command records, monotonic allocation, fencing, idempotency, retry/cancel/timeout state, and stale-worker rejection.

The following exact boundaries are frozen now but become runnable integrations later:

- M4b implements `CostLedger`, budget holds/reservations/permits, and closes the cost portions of Run UoWs.
- M4c composes authentication, API/worker, RunKind/OutcomeArtifactPolicy publishers, SSE/WS, and Journey A/B.
- M4e implements PostgreSQL/S3, DB/object WORM controls, external anchoring, DR, migrations, solver isolation, and production deployment.

No M4a result may claim a later-slice acceptance item early. There is no permissive production fallback for an unavailable later capability: composition fails closed until the required adapter is supplied.

## Compatibility Rules

- Preserve `lineage@1`, `audit@1`, `patch@1`, flat Finding, `model-router@1`, and `cassette@1` readers and ID/hash behavior byte-for-byte.
- Add explicit V2/revision types and discriminated parsers. Do not reinterpret a legacy row as V2 and do not synthesize missing ObjectRef, actor, subject, correlation, approval, or evidence.
- M4 ArtifactKind is a closed enum. Unknown kinds and malformed lower-hex SHA-256 fields fail closed.
- Never use `session.merge` for immutable records, `MAX(...)+1` for monotonic identities, an unbounded collection read, or a mutable status in Patch payload.
- ObjectStore writes happen before DB publication. DB rollback may leave only a verified orphan eligible for safe-window GC.
- Every state-changing command uses one UnitOfWork and emits its authoritative audit/event record in that same transaction.
- `spine` imports only `contracts`; no observability or platform types enter canonical hashes.
- M3 human QA remains `qa.evidence_missing` and does not block this implementation.

## File Map

| Area | Planned files |
|---|---|
| Additive wire contracts | `gameforge/contracts/lineage.py`, `findings.py`, `versions.py`, new `storage.py`, `identity.py`, `workflow.py`, `runs.py`, `diff.py`, `errors.py` |
| Deterministic cores | new `gameforge/spine/diffing/`, existing `gameforge/spine/patch.py`, new lineage/producer validators that depend only on contracts |
| SQLite/local adapters | `gameforge/runtime/persistence/engine.py`, `models.py`, new `uow.py` and repository modules, new `gameforge/runtime/object_store/`, Alembic `0002+` |
| Platform services | new `gameforge/platform/storage/`, `rbac/`, `approvals/`, `diff/`, `runs/`, `rollback/`; compatibility adapters in existing `platform/lineage` and `platform/audit` |
| Tests | `tests/contracts/m4/`, `tests/runtime/persistence/`, `tests/runtime/object_store/`, `tests/platform/m4/`, focused updates to legacy lineage/audit/patch tests |

## Task 1: Freeze Additive M4 Wire Contracts

**RED tests**

- Add contract tests for every ArtifactKind, strict 64-lower-hex digests, ObjectRef/ObjectLocation/ObjectBinding, ArtifactV2 invariants, AuditRecordV2 hash payload, ExecutionIdentity projections, PatchV2, FindingRevisionV1, storage/page/conflict DTOs, identity/RBAC DTOs, approval/evidence/auto-proof DTOs, and Run DTOs.
- Add compatibility fixtures proving all legacy V1 payloads parse and reserialize without new fields or changed canonical bytes.
- Add malformed-discriminator tests proving a V2 payload cannot fall through to a V1 reader.

**Implementation**

- Keep legacy public readers usable; add explicit V2/revision classes and parser functions returning discriminated unions.
- Canonicalize stable-unique collections only where the contract says order is semantic-free; reject duplicates where identity/cardinality requires rejection.
- Implement digest constructors/validators exactly once using `contracts.canonical`.
- Encode `expected_ref=null` as an explicit CAS precondition, never as an omitted/unknown value.

**Verify**

```bash
uv run pytest tests/contracts/test_lineage.py tests/contracts/test_findings.py tests/contracts/m4 -q
uv run pytest tests/test_dependency_lint.py -q
```

## Task 2: Enforce ArtifactV2 Identity and Producer Matrix

**RED tests**

- Prove ArtifactV2 ID binds schema, kind, VersionTuple, sorted-unique lineage, payload hash, and immutable meta but excludes `created_at` and ObjectLocation.
- Prove `payload_hash == object_ref.sha256`, deterministic key layout, size/hash validation, DAG parent uniqueness, and closed ArtifactKind.
- Parameterize every ArtifactKind producer rule from foundations v0.3 §5.1, including conditional LLM/cassette/environment/seed requirements and honest `evidence_missing` for legacy reads.
- Prove single/multi prompt/model ExecutionIdentity projections recompute and mismatch fails closed.

**Implementation**

- Add pure producer-matrix validation and ArtifactV2 construction in a contracts-only deterministic module.
- Do not mechanically merge parent VersionTuples; require typed role/projection bindings for every M4 multi-parent producer.

**Verify**

```bash
uv run pytest tests/contracts/m4/test_artifact_v2.py tests/spine/versioning -q
```

## Task 3: Define Storage Protocols, Typed Failures, Cursors, and Clocks

**RED tests**

- Freeze `Repository`, `RefStore`, `AuditSink`, `ObjectBindingRepository`, `ObjectStore`, `ObjectGc`, `Transaction`, `UnitOfWork`, and `StorageFacade` signatures.
- Test signed canonical cursors bind version, sort, filters, read snapshot, principal/authz revision, and expiry; tamper or cross-context reuse is a typed failure.
- Test injected UTC clock for persisted timestamps/deadlines and monotonic clock for durations; never compare or substitute the two.
- Test transaction handles reject direct construction, nesting, cross-UoW use, and access after commit/rollback.

**Implementation**

- Put pure Protocols/DTOs/errors in contracts and deterministic clock implementations in runtime.
- Use typed `IntegrityViolation`, `Conflict`, `CursorInvalid`, `CursorExpired`, `Forbidden`, and state-transition failures; no naked boolean CAS result.

**Verify**

```bash
uv run pytest tests/contracts/m4/test_storage.py tests/runtime/persistence/test_clock.py -q
```

## Task 4: Implement Durable LocalObjectStore

**RED tests**

- Stream bytes through temp file, SHA-256/size verification, file fsync, atomic same-filesystem rename, and directory fsync.
- Same bytes are idempotent; same key with differing content/size is `IntegrityViolation`.
- `open/stat/list_versions/delete_if_generation` enumerate and condition on concrete backend generations.
- Crash leftovers are invisible to normal reads and retained until the configured safe window.

**Implementation**

- Add `runtime/object_store/local.py`; keep publication/binding knowledge outside ObjectStore.
- Make filesystem and fsync operations injectable only where needed for deterministic failure testing.

**Verify**

```bash
uv run pytest tests/runtime/object_store -q
```

## Task 5: Expand SQLite Schema and Build One-Connection UnitOfWork

**RED tests**

- Migration upgrade from real `0001` fixture preserves legacy rows; downgrade/upgrade round trip is explicit and deterministic.
- Every SQLite connection has `foreign_keys=ON`, WAL, and a finite busy timeout; write UoW begins with `BEGIN IMMEDIATE`.
- Two repositories from one transaction share the exact connection and snapshot; exception rolls all writes back.
- Nested/expired transaction handles fail closed.

**Implementation**

- Add narrow Alembic revisions for storage/object binding, identity/workflow, and runs rather than one opaque migration.
- Expand legacy tables without rewriting legacy hashes. Add revision/head columns, unique constraints, foreign keys, and indexes required by cursor order and CAS.
- Build runtime UoW and tx-bound repositories; no repository commits independently.

**Verify**

```bash
uv run pytest tests/runtime/test_persistence_migration.py tests/runtime/persistence/test_uow.py -q
```

## Task 6: Implement Immutable Artifact/ObjectBinding Repositories and GC

**RED tests**

- `put` of exact canonical ArtifactV1/V2 is idempotent; same ID with any changed immutable field or ObjectRef is rejected without overwrite.
- V2 publication requires a verified active binding in the same UoW; retry never silently remaps a binding.
- Pagination is stable and bounded.
- GC live set includes every committed Artifact ObjectRef plus pinned recovery references, not only current refs; delete performs a second DB reference check and generation-CAS.

**Implementation**

- Replace `session.merge` with insert-or-compare logic.
- Add revision-CAS binding/remap/retire operations and a separate platform ObjectGc service.
- Keep the old store facade behavior for legacy callers while routing writes through the new UoW.

**Verify**

```bash
uv run pytest tests/platform/test_lineage_store.py tests/runtime/persistence/test_artifact_repository.py tests/platform/m4/test_object_gc.py -q
```

## Task 7: Implement Revisioned RefStore and AuditGate

**RED tests**

- Ref create requires expected null; update matches both artifact ID and revision; A→B→A cannot satisfy an old expected value.
- Successful ref revision is the history sequence; multi-connection writers yield one winner and no duplicate/gap caused by `MAX+1`.
- Audit@2 append verifies/locks its chain head and predecessor in the same UoW as the business write.
- Full-chain verification detects altered/missing middle records and honestly does not claim unanchored tail-truncation proof.
- Audit@1 remains readable without synthesized identity fields.

**Implementation**

- Use conditional `UPDATE ... WHERE artifact_id AND revision`, checking `rowcount == 1`; create uses an insert conflict path.
- Add chain IDs/heads and audit@2 JSON columns while preserving audit@1 rows.
- Make startup/privileged/periodic explicit verification separate from per-append predecessor verification.

**Verify**

```bash
uv run pytest tests/platform/test_audit_worm.py tests/runtime/persistence/test_ref_repository.py tests/platform/m4/test_audit_gate.py -q
```

## Task 8: Implement Identity, Domain Registry, and RBAC

**RED tests**

- Validate registry digests, sorted unique domain IDs, parent existence/acyclicity, and retained deprecated domains.
- Grant/revoke/disable increment the exact principal revision/credential epoch/authz revision in one UoW.
- Authorization computes assignment-scope ∩ policy-scope, uses all-of coverage across multiple roles, and enforces `null != all`.
- Default domain routing maps numeric/narrative/gacha/structural duties through configuration only; a new game/domain needs no code branch.

**Implementation**

- Add identity/domain/role-policy repositories and a pure `authorize` engine.
- Persist exact historical registry and policy versions; never resolve a referenced digest through a current alias.

**Verify**

```bash
uv run pytest tests/platform/m4/test_identity_repository.py tests/platform/m4/test_rbac.py tests/platform/m4/test_domain_routing.py -q
```

## Task 9: Implement Finding/Patch Revisions and Field-Level Diff

**RED tests**

- Finding digest excludes `created_at`, includes the domain separator, and changes for every semantic revision field.
- PatchV2 has immutable revision/supersedes semantics and no mutable validation/approval status; human producer has no Run ID, agent producer must bind a valid Run at workflow publication.
- Snapshot diff compares complete canonical objects, escapes JSON Pointer correctly, distinguishes missing/null, treats arrays as ordered unless schema identity declares a set, and is stable under map insertion order.
- Hypothesis differential tests compare optimized diff against a small naive implementation.

**Implementation**

- Keep `apply_patch` pure and exact-base; add a spine-only canonical diff engine and platform repository-loading facade.
- Persist Finding series/revisions separately from Artifact identity and expose bounded revision pages.

**Verify**

```bash
uv run pytest tests/contracts/m4/test_finding_revision.py tests/spine/test_patch.py tests/spine/diffing tests/platform/m4/test_finding_repository.py -q
```

## Task 10: Implement ApprovalItem, SubjectHead, Routing, and Decisions

**RED tests**

- Freeze every allowed state transition and reject all unlisted edges.
- Draft publication atomically inserts immutable subject/preview/conditional export artifacts, target binding, SubjectHead, ApprovalItem, and audit without moving a ref.
- Superseding a revision CASes the old item to `superseded`, requests cancellation of an active validation Run, creates a fresh item, and inherits no evidence/decision/proof.
- Requirements cover the full DomainScope; partial approval remains pending; min/distinct/maker-checker/human/current-permission guards are all enforced.
- Duplicate decisions are idempotent; same key with different payload conflicts.

**Implementation**

- Add tx-bound repositories and platform command services for draft, validation start, submit, decision, and state projection.
- Treat ApprovalItem as the only mutable workflow truth; PatchView is derived.
- Require exact historical Approval/Role/Route/Domain policy refs at every decision and apply check.

**Verify**

```bash
uv run pytest tests/platform/m4/test_approval_state_machine.py tests/platform/m4/test_approval_commands.py tests/platform/m4/test_maker_checker.py -q
```

## Task 11: Implement Validation Completion and Auto-Apply Guards

**RED tests**

- Patch/rollback validation evidence matches the draft-time target binding exactly; constraint candidate binding may transition null→exact once and never change.
- SubjectHead-vs-validation completion race always leaves old item `superseded`, terminates the old Run as `subject_superseded`, and attaches no EvidenceSet/proof to the new head.
- Execution failure/cancel/timeout returns current validating item to draft; deterministic validation failure becomes `validation_failed` and is not disguised as infrastructure failure.
- Auto-apply proof exists only for the dedicated Patch outcome, all affected domains are allowed and none forbidden, all deterministic oracle/outcome bindings close, and validation/submit/apply rerun the same guard.

**Implementation**

- Implement validation completion as one guarded UoW with injected exact policy/profile resolvers; no permissive default resolver.
- Keep target/evidence blob computation outside the write transaction and publish only preverified prepared objects inside it.

**Verify**

```bash
uv run pytest tests/platform/m4/test_validation_completion.py tests/platform/m4/test_validation_supersede_race.py tests/platform/m4/test_auto_apply.py -q
```

## Task 12: Implement Three-Way Conflict/Rebase Workflow

**RED tests**

- Compute base/current/proposed conflicts at stable JSON Pointers with full missing/null states and deterministic non-conflicting-op digest.
- Cursor-page immutable conflicts; reject stale ref revisions and invalid resolutions.
- Clean rebase or explicit conflict resolution creates a new PatchV2 Artifact and ApprovalItem revision, reruns validation, and never carries old approval/evidence/auto proof.
- LLM output cannot choose a resolution in this service.

**Implementation**

- Put pure three-way computation in spine and workflow orchestration in platform.
- Use only `keep_current`, `take_proposed`, or explicit typed custom value.

**Verify**

```bash
uv run pytest tests/spine/diffing/test_three_way.py tests/platform/m4/test_rebase_workflow.py -q
```

## Task 13: Implement Persistent Run Store, Idempotency, and Monotonic Allocation

**RED tests**

- Persist Run/Attempt/Lease/Event/Command/intermediate/finding links and immutable payload hash/bindings.
- Same scoped idempotency key and request hash returns the original Run; different hash conflicts.
- Queue creation has no fake attempt/lease/token; event seq starts from the Run head and is emitted atomically.
- Multi-connection claim allocates unique monotonic attempt/fencing values with one current lease; retry does not preallocate the next attempt.
- Prompt publication allocates call ordinal through the Attempt head, never `MAX+1`.

**Implementation**

- Add strict Run repositories and state-machine functions with a tx-bound admission/publication Protocol for M4b/M4c composition.
- Require immutable budget/profile/policy/schema/version bindings on records; M4a validates stored equality but does not implement CostLedger policy or arbitrary RunKind publishers.

**Verify**

```bash
uv run pytest tests/runtime/persistence/test_run_repository.py tests/platform/m4/test_run_create_claim.py tests/platform/m4/test_run_ordinals.py -q
```

## Task 14: Implement Lease Fencing, Retry, Cancel, Timeout, and Terminal Hooks

**RED tests**

- Start fixes attempt deadline; heartbeat CASes lease version and never extends attempt/overall deadline.
- Ordinary worker writes validate lease ID/fencing/expiry and Run revision but not the changing heartbeat version.
- Expired/replaced workers cannot publish results/events; current worker can.
- Lease expiry/retry closes the old attempt, emits its event/audit, and only a later claim allocates a new attempt/token.
- Queue, attempt, and overall timeout are distinct; cancel is cooperative for active work and direct only when no active lease exists.
- Every committed state transition has exactly one persisted event; command deduplication is exact.

**Implementation**

- Implement local SQLite time authority through injected UTC clock; keep PostgreSQL DB-time strategy for M4e.
- Provide strict terminal publication hooks that require prepared manifest/artifact data and later cost/publisher participants; do not fabricate cost settlement or outcome evidence in M4a.

**Verify**

```bash
uv run pytest tests/platform/m4/test_run_fencing.py tests/platform/m4/test_run_retry_cancel_timeout.py tests/platform/m4/test_run_event_atomicity.py -q
```

## Task 15: Implement Approved Apply and Rollback

**RED tests**

- Patch/constraint apply reauthorizes and revalidates current head, workflow/ref revision, exact target Artifact/ObjectRef/digest/evidence/policy binding, then atomically moves the ref and item state.
- Rollback requires an approved RollbackRequest with exact retained rollback ExecutionProfile, historical membership, readable target schema, and current ref CAS.
- Rollback appends RefTransition and audit in the same UoW, optionally marks the reversed item `rolled_back`, and adds no content-lineage edge.
- Any stale/missing/mismatched field leaves approval, ref, history, transition, and audit unchanged.

**Implementation**

- Reuse draft-published preview/candidate artifacts; never compute or publish a new target during apply.
- Give rollback no parallel policy registry: resolve the exact retained ExecutionProfile binding.

**Verify**

```bash
uv run pytest tests/platform/m4/test_apply.py tests/platform/m4/test_rollback.py tests/platform/m4/test_ref_transition.py -q
```

## Task 16: M4a Integration, Compatibility, Concurrency, and Review Gates

**Integration tests**

- Local service flow: publish V2 Artifact/ObjectRef → diff → draft Patch/preview → validate → independent human approval → apply → approved rollback → verify ref history, RefTransition, lineage DAG, and audit chain.
- Race flow: validation completion vs superseding subject revision.
- Multi-connection flow: claim/retry/reaper/event sequencing and stale-worker rejection.
- GC flow: current and historical rollback objects remain live; orphan generations are collected only after the safe window and second reference check.
- Pagination flow: concurrent inserts/updates/deletes do not create duplicates/omissions within one read snapshot; cursor tamper/cross-principal/authz-revision/expiry fails without leakage.
- Legacy flow: M0b-M3 lineage/audit/Finding/Patch/cassette fixtures remain byte-stable and existing public behavior remains green.

**Required gates**

```bash
uv run pytest tests/contracts tests/runtime/persistence tests/runtime/object_store tests/platform tests/spine/versioning tests/spine/diffing tests/spine/test_patch.py -q
uv run pytest -q
uv run lint-imports
uv run pytest tests/test_dependency_lint.py -q
uv run ruff check .
git diff --check
```

**Review and completion**

- Run an adversarial review against design §§3 and 9, foundations v0.3, backwards compatibility, concurrency, and layering.
- Fix confirmed findings and rerun every gate.
- Record the exact passing counts and M4a acceptance evidence in this plan and the progress anchors.
- Commit without AI attribution, merge linearly into `master`, and push only after M4a is green. Then create the M4b plan from the frozen design.

## M4a Traceability Matrix

| Frozen requirement | Tasks | Completion evidence |
|---|---|---|
| lineage@2/ObjectRef/producer matrix and legacy readers | 1, 2, 6 | contract + repository + compatibility tests |
| SQLite UoW, ref CAS, audit@2, pagination | 3, 5, 7, 16 | transaction/concurrency/integration tests |
| LocalObjectStore/ObjectBinding/ObjectGc | 4, 6, 16 | durability, generation-CAS, historical live-set tests |
| RBAC/domain routing/maker-checker | 8, 10, 11 | pure authorization and workflow tests |
| immutable Finding/Patch/Approval revisions | 1, 9, 10 | digest/revision/supersede tests |
| field diff/rebase/reapproval | 9, 12 | differential/property + workflow tests |
| Run/Attempt/Lease/Event fencing | 13, 14, 16 | multi-connection monotonicity and stale-worker tests |
| approved apply and exact rollback, no DAG cycle | 15, 16 | end-to-end local service flow |
| M4b/M4c/M4e integrations | protocol boundaries only in M4a | explicitly remain unchecked until their owning slice |

