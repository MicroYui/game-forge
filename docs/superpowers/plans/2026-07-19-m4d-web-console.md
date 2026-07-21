# M4d Web Console Implementation Plan

> **Execution rule:** implement task by task with TDD. A task is complete only after its focused tests pass and its stated user-visible states are exercised. M4d is not complete until all three human visual-approval gates, Journey A/B browser suites, visual/accessibility evidence, and the repository-wide gates in Task 19 pass.
>
> **Visual rule:** the frozen M4 editorial direction is the default. Any proposed deviation from its typography, color, density, navigation, visualization semantics, or approved page composition—and any display problem that cannot be resolved within that direction—must be discussed with the product owner before implementation continues.

**Goal:** Deliver the complete, polished GameForge Web Console on top of the completed M4c API/worker platform: eight first-class pages, contract-generated TypeScript, resumable run interaction, exact workflow/approval presentation, real Journey A/B browser execution, and production-quality responsive visual and accessibility evidence.

**Architecture:** Keep a single React/Vite SPA under web. Generated OpenAPI and SSE/WS types define the transport boundary. A thin same-origin client owns cookies, CSRF, ETag/If-Match, idempotency, Problem responses, opaque cursors, SSE, and WS commands. TanStack Query owns server state; URL state owns filters and selection; local component state owns transient UI such as trace playback and merge choices. The backend remains authoritative for permissions, validation, deterministic verdicts, workflow transitions, merge results, and content authority.

**Truth sources:**

1. docs/superpowers/specs/2026-07-03-gameforge-prd.md
2. docs/superpowers/specs/2026-07-03-gameforge-foundations-contracts.md v0.3
3. docs/superpowers/specs/2026-07-13-m4-production-hardening-design.md, especially §§5, 6, and 9
4. docs/api/openapi-v1.json and docs/api/schemas/*.json
5. docs/superpowers/plans/2026-07-14-m4c-api-worker.md completion evidence

**Baseline:** master at 1c17023a. M4c exposes 67 paths, 71 operations, 181 OpenAPI schemas, and versioned SSE/WS schemas. The current web directory is only the M0a React/Vite scaffold and has no route, API client, style system, or test harness.

## Scope Boundary

M4d implements:

- the eight frozen top-level pages: Spec/KG, Generation, Review, Playtest, Patch/Diff, Eval/Bench, Observability, and Approvals;
- login plus run, finding, patch, approval, artifact/lineage, and ref-history detail routes;
- exact execution-profile and run-admission selection without hard-coded current defaults;
- typed HTTP, cursor, SSE, and WS clients generated from the frozen M4c contracts;
- the frozen design tokens, self-hosted Source Han Serif SC subset, light/dark themes, responsive application shell, and Chinese-first copy extraction;
- deterministic-vs-LLM evidence presentation, KG visualization, field diff/merge, Playtest trace playback, charts, logs, cost, and requirement-level approval progress;
- one loopback-only, source-specific M3 QA Runner adapter over the already frozen benchmark harness, used outside the product Console for participant guidance;
- Chromium Playwright Journey A/B happy and failure paths against real apps/api + worker + cassette REPLAY with external egress denied;
- the fixed visual, keyboard, reduced-motion, WCAG-AA, long-content, and overflow evidence required by M4 §6.6.

M4d does not implement:

- PostgreSQL/S3/Tempo/Loki/Prometheus, DR, WORM/anchors, migration execution, solver isolation, deployment, or capacity evidence owned by M4e;
- OIDC, multi-region/HA, real alert delivery, real-time collaborative editing, complex graph auto-layout, or multilingual content owned by v-next;
- a second production/product backend, BFF, GraphQL layer, frontend checker, frontend approval engine, or frontend content authority. The isolated QA Runner is a local adapter over the existing benchmark harness, not a product backend or API.

The existing local telemetry APIs are sufficient for the M4d Observability page. M4d must not wait for M4e production adapters and must not claim their acceptance evidence.

## Right-Sizing Rules: Detailed, Not Over-Engineered

- Use React Router, TanStack Query, and small Auth/Theme/Toast contexts. Do not add Redux, Zustand, XState, a global event bus, or a second normalized domain store.
- Generate transport types once. Do not hand-copy DTOs and do not generate a second full Zod/Ajv model graph. Runtime checks are limited to generic Artifact payloads and SSE/WS discriminator boundaries where the wire is intentionally open.
- Use page-specific forms with native HTML constraints and focused helpers. Do not build a JSON form engine, generic CRUD page factory, or schema-driven workflow designer.
- Use ordinary CSS variables and component styles. Do not add Tailwind, CSS-in-JS, a UI kit, Storybook, or a separate design-system package.
- Extract a shared component only when it is required by the frozen component list or repeated in at least two real pages. Do not abstract one-off page layout.
- Mutations never retry silently. One user intent gets one idempotency key; a network retry of that same intent reuses it, while a new click gets a new key.
- PermissionGate hides/disables controls only when a response supplies an explicit server-computed eligibility signal, such as ApprovalView.current_actor_allowed_requirement_ids. It never derives permissions from role names. Where no eligibility projection exists, keep the action visible and let the authoritative server response decide.
- KG uses bounded pages and a simple stable Cytoscape layout. No 3D/WebGL scene, speculative graph analytics, or complex automatic layout framework.
- Trace renderers are a small static registry of bundled components. No dynamic scripts, plugin marketplace, or remote component loading.
- Use a small Chinese message module as the i18n scaffold. Do not add a localization framework or translations in M4d.
- Do not add SSR, micro-frontends, PWA/service workers, offline mutation queues, or persistent API-cache replication.
- Unit tests cover view logic and transport boundaries. Playwright covers required user journeys and visual behavior; it does not duplicate every one of the 71 backend operation contract tests.
- Current provide_input remains visibly unavailable until a real server interaction request and pause authority exists. M4d closes cancel and command recovery but does not simulate a playtest interaction loop.

## Minimal Frontend Stack

Retain React, ReactDOM, Vite, TypeScript, and the React Vite plugin, and pin them plus the following additions exactly in package.json and package-lock.json during Task 1.

| Purpose | Choice |
|---|---|
| Routing | react-router-dom |
| Server state | @tanstack/react-query |
| Generated HTTP client | openapi-typescript + openapi-fetch |
| SSE parsing | fetch stream + eventsource-parser |
| Graph | cytoscape |
| Charts | recharts |
| Icons | lucide-react |
| Unit/component tests | Vitest + Testing Library + user-event + jest-dom |
| Browser/accessibility | Playwright + @axe-core/playwright |
| Contract generation | openapi-typescript + json-schema-to-typescript |
| HTTPS local browser harness | official Vite basic-ssl plugin |
| Formatting | Prettier check only for web sources |

Node 24.18.0 and npm 11.16.0 are the initial frozen local toolchain. A later toolchain change requires an explicit plan update and clean lock regeneration.

## File Map

| Area | Planned files |
|---|---|
| API usability closure | gameforge/contracts/execution_profiles.py, api.py; gameforge/platform/read_models/; gameforge/apps/api/local_reads.py and routers/content.py; docs/api/; focused contract/read/OpenAPI tests |
| App composition | web/src/app/router.tsx, providers.tsx, shell/, routes.ts |
| Transport | web/src/api/generated/, client.ts, problem.ts, csrf.ts, pagination.ts, sse.ts, commands.ts |
| Shared UI | web/src/components/ for page states, evidence, finding, diff/merge, run progress, approval, charts, logs, dialogs, breadcrumb, and cursor controls |
| Features | web/src/features/{specs,generation,review,playtest,patches,eval,observability,approvals,runs}/ |
| Visual system | web/src/styles/{tokens,global,layout,utilities}.css, web/src/assets/fonts/, web/src/i18n/zh-CN.ts |
| Tests | colocated *.test.ts(x), web/e2e/, web/playwright.config.ts, web/src/test/ |
| Contract scripts | web/scripts/generate-contracts.mjs, check-contracts.mjs |
| Browser stack fixture | a narrow test-only launcher under tests/e2e/m4d_support/ that reuses M4c local composition |
| Isolated M3 QA Runner | gameforge/bench/external_cases/endless_sky_qa_runner.py, the existing endless_sky_qa.py composition, gameforge/bench/qa/runner_assets/, and focused QA/boundary tests |

Do not add extra domain/repository/view-model layers. A feature may have api.ts, queries.ts, components, and pages only when it actually needs them.

## Visual Direction and Mandatory Product-Owner Gates

The frozen direction is editorial neutral paper-white, warm restrained accents, Source Han Serif SC for UI text, SF Mono fallbacks for identifiers, and purposeful rather than decorative data visualization. Light/dark tokens, type scale, spacing, radius, motion, and semantic colors come directly from M4 design §6.2.

Three human checkpoints are mandatory:

1. **V1 — visual foundation, after Task 6:** present the real application shell, shared evidence/diff/chart components, KG, and generic/Aureus/unknown trace states at desktop/mobile and light/dark. Do not begin the page rollout until the product owner approves the visual language.
2. **V2 — all-page composition, after Task 14:** present all eight stable page compositions and the generic/Aureus/unknown trace renderers. Resolve layout, density, and visualization feedback before Journey A/B finalization.
3. **V3 — final visual evidence, in Task 18:** present the preregistered screenshot matrix, long-content stress states, and accessibility findings. M4d cannot be marked complete without explicit product-owner approval.

Minor implementation corrections that stay within the approved tokens and composition may proceed. A style change or unresolved display tradeoff is a blocking discussion, not an autonomous redesign.

After all eight pages became stable and V2 passed, the product owner selected the narrow isolated local QA Runner for guided, protocol-compliant M3 human-evidence sessions. The normal Console is the orientation and final-result surface, but the frozen manual arm remains isolated from Finding/Patch/Eval information. Task 15 ends with synthetic Runner readiness plus product-owner operability confirmation; the product owner then performs concrete tasks through eight real sessions as a separate, deferrable M3 follow-up instead of reviewing raw evidence files. That follow-up does not block Tasks 16–19. Until all sessions produce valid imported evidence, Eval must continue to show `qa.evidence_missing`, `pending_human_evidence`, and `not_measured`; scheduling or partially running the study does not rewrite those states as passed or make the previously deferred M3 evidence an implicit M4d engineering gate.

## Planning Approval

On 2026-07-19, the product owner approved this 18-task plan, the two frozen Task 2 API operations, continued use of the original editorial visual direction, and the three-stage visual review process. This approval authorizes the planned implementation sequence but does not count as V1, V2, or V3 visual approval and does not mark implementation as started.

On 2026-07-20, after explicitly approving V2, the product owner chose the recommended isolated local QA Runner so the deferred eight-session M3 QA-hours study can be performed through a guided interface instead of raw-file review. This inserts one readiness task before the browser journeys and revises the remaining sequence to 19 tasks without rewriting the historical 18-task approval. The Runner is a loopback-only benchmark surface over the already frozen protocol/harness; it is not a ninth Console page, a production API, or a generic QA platform. Real participant sessions remain human evidence rather than an M4d engineering acceptance shortcut.

The product owner later clarified that Editorial is the V1 baseline, not an immutable art template. V1 may present a small number of controlled visual alternatives over the same real components, data, information architecture, and semantic tokens for explicit comparison. No alternative becomes the rollout direction until the product owner chooses it; the page rollout still remains blocked at V1.

On 2026-07-19, the product owner compared the Editorial baseline with the controlled Inkrail Ledger alternative, selected **A — Editorial**, reviewed the complete desktop/mobile and light/dark V1 matrix, and explicitly approved V1 without requested corrections. Inkrail remains unselected review material and is removed rather than becoming a runtime theme or skin system.

## Implementation Progress

- **Task 1 ✅ (2026-07-19):** exact Node/npm and dependency pins, canonical npm/SHA-512 lock integrity, deterministic OpenAPI/SSE/WS generation and drift checking, Vitest/jsdom, HTTPS same-origin Vite proxy, Chromium Playwright smoke, and formatting gates are implemented. A clean `npm ci` run passed toolchain check, contract generation/check, TypeScript, 8 unit/contract tests, production build, the no-external-font/CDN browser smoke, Prettier, and `npm audit` with 0 vulnerabilities.
- **Task 2 ✅ (2026-07-19):** the two frozen read operations and approved D1 deployment pointer are implemented. The resolver covers all six Agent RunKinds for RECORD and native REPLAY, returns options that bridge into real admission, performs no database/ObjectStore writes, and revalidates intervening authority drift at final admission. Subject approval binding covers current and historical Patch/ConstraintProposal/RollbackRequest revisions while evidence-only Patch artifacts remain unbound; its ETag now represents the complete changing view. A shared native routing guard additionally proves one policy rule covers the complete server-resolved DomainScope for every plan node, without prematurely evaluating budget predicates; verified legacy-import REPLAY remains compatible. Independent contract and security reviews found no remaining P0/P1. Gates: 281 focused contract/API/admission tests, 203 platform admission/plan/source tests, 74 OpenAPI/streaming tests, 47 local-composition/persistence tests, schema drift check, generated TypeScript, typecheck, contract tests, Ruff/format, and diff-check all passed.
- **Task 3 ✅ (2026-07-19):** the generated HTTP client now closes cookie/CSRF session boundaries, exact mutation headers, stable intent identity, opaque cursor restart, safe RFC 9457 projection, and cross-principal query-cache clearing. SSE resumes from the canonical decimal frame ID, deduplicates only after successful consumption, and requires an explicit 410 restart. Durable RunCommand transport uses the frozen WS subprotocol, bounded ACK wait, persisted GET plus fire-and-forget SSE recovery, frozen-intent retry only for unknown transport outcomes, and a new intent only after an authoritative 409 refresh. The production `/runs/:runId` route renders exact RunView/events/findings/manifests/commands/traces with bounded explicit pagination and owner/generation guards against stale route, snapshot, page, and stream callbacks; `provide_input` remains honestly unavailable. Independent review found no remaining P0/P1. Gates: 14 focused files / 56 tests, typecheck, build, generated-contract drift check, and Task 3 Prettier all passed.
- **Task 4 ✅ (2026-07-19):** the Editorial visual foundation, self-hosted licensed font subset, light/dark tokens, responsive semantic shell, session/auth boundaries, keyboard interactions, shared page states, and production Run route composition are implemented. Independent browser/code review found no remaining P0/P1; 46 focused tests, typecheck, build, formatting, desktop/mobile light/dark inspection, real font loading, theme bootstrap, and shell smoke passed.
- **Task 5 ✅ (2026-07-19):** shared evidence, Finding, missing-vs-null diff, explicit three-way merge, cursor table, five chart forms, log explorer, and safe Artifact/lineage detail routes are implemented. Follow-up review closed camelCase free-text redaction through writer/store/query/UI copy, exact Decimal cost presentation, unavailable-state honesty, opaque cursor retry/restart, 512-character IDs, and focus tooltip clipping; no P0/P1 remain.
- **Task 6 ✅ (2026-07-19):** bounded KG plus generic/Aureus-fixture/unknown-fallback Playtest trace visualization are implemented. The trace adapter now consumes only real `PlaytestActionRecordV1` fields, labels unavailable state/events honestly, keeps raw JSON lazy and timelines bounded while surfacing all authoritative markers, and preserves keyboard, aspect-ratio, long-content, and registry constraints. Independent review found no remaining P0/P1; 44 Task 6 tests, typecheck, build, and formatting passed.
- **V1 ✅ (2026-07-19):** the product owner selected A — Editorial over the controlled Inkrail alternative and explicitly approved the complete real-component desktop/mobile and light/dark matrix without corrections. The unselected alternative is removed; the approved direction remains the Task 7 implementation baseline.
- **Task 7 ✅ (2026-07-20):** Spec/KG list/detail, bounded graph/schema inspection, typed Patch drafting, distinct Agent/Human constraint entry, Human spec upload, retained proposal/approval binding, Human revision, deterministic validation, approval handoff, publish, conflict/stale-ETag recovery, and exact candidate-versus-authority semantics are implemented over generated contracts. Independent review fixed retained historical binding composition and post-draft validate gating. Real-route browser QA covered workspace, Spec detail, constraint snapshot, and proposal workflow at desktop/mobile and light/dark with no horizontal overflow; it also closed narrow tooltip and Task 7 breadcrumb defects without changing the approved visual direction. Final web gates are 38 files / 225 tests, TypeScript, four generated-contract artifacts, production build, Prettier, and diff-check green.
- **Task 8 ✅ (2026-07-20):** the Generation page now requires exact base, constraint, generation, environment, and non-empty matching export profiles; resolves those selections through server authority; freezes an idempotent propose intent across unknown transport outcomes; and follows the exact Run/SSE/manifest chain without storing goal text in the URL. Passed, gate-rejected, and repaired outcomes render distinct authoritative states, with exact Patch/preview/config/evidence bindings, non-inherited old approval/evidence, and fail-closed Run/manifest and repair-lineage checks. A contract review found and closed a sensitive-artifact boundary defect: the browser fetches only public output/evidence artifacts, keeps source/cassette intermediates as non-clickable manifest references, and obtains base/constraint through their dedicated authority APIs. The production route passed desktop/mobile and light/dark A—Editorial visual QA without requiring a new aesthetic choice. Independent contract re-review found no remaining P0/P1; all web gates passed: 42 files / 266 tests, TypeScript, four generated-contract artifacts, production build, Prettier, and diff-check.
- **Task 9 ✅ (2026-07-20):** Review index/detail and exact Finding revision routes now preserve immutable report versions, direct preview/constraint lineage, frozen tool/model identity, deterministic/simulation/LLM/unproven partitions, lifecycle states, minimal repro/source/evidence, and explicit zero-Finding semantics. Two additive occurrence reads close exact producer authority without inventing a global owner: Review terminal manifest/outcome-policy binding and Run-scoped Finding revision/digest/evidence links. Final review also closed two subtle authority defects: bucket placement and `by_defect_class` are revalidated against `ReviewReport.partition` before Workspace or Detail rendering, while explicit `sourceRun` is verified as an independent occurrence (including zero-Finding and multi-occurrence cases) rather than being treated as a display-only hint. Desktop/mobile and light/dark real-route QA retained A—Editorial with no horizontal overflow or new aesthetic branch. Independent re-review found no remaining P0/P1. Gates: 47 Review tests, 324 full web tests, 137 focused backend tests, 76-operation OpenAPI/generated TypeScript, typecheck, production build, contract drift, Prettier, and diff-check all green.
- **Task 10 ✅ (2026-07-20):** the Playtest workspace now discovers or derives immutable TaskSuites through exact config/constraint/environment authority, binds derivation profile/oracle data through one narrow read, resolves complete execution options, launches explicit non-empty episode subsets, and follows real Run/SSE/cancel/result/trace/Finding evidence without inventing `provide_input` authority. Four TaskSuite filters are bounded server-side verified-payload filters with stable cursor snapshots; admission and worker share the same exact adapter authority. Final concurrency review closed route/query owner fencing, frozen unknown outcomes, late derive/launch recovery, accepted-receipt persistence, source-Run-bound suite recovery, automatic-history normalization, large SSE cursors, terminal exact-head versus partial-suffix EOF, and close/restart races. Desktop/narrow and light/dark real-route QA retained A—Editorial with no new visual choice. Independent zero-baseline review found no remaining P0/P1. Gates: 313 focused backend tests, 128 final focused web tests, 50 files / 438 full web tests, exact 77-operation OpenAPI/generated TypeScript, typecheck, production build, contract drift, Prettier, dependency boundary, Ruff, and diff-check all green.
- **Task 11 ✅ (2026-07-20):** Patch/Diff now exposes immutable revision ledgers, field diff and base/current/proposed authority, explicit server-defined conflict resolution, clean rebase replacement, repair/validation/regression evidence, independent approval/apply, append-only ref history, and the complete governed rollback flow. New-ref Patch base recovery is proven from unique direct lineage, absent refs are accepted only through exact `404/not_found`, cross-domain rollback drafts fail closed, and stale workflow/ref/conflict or self-approval outcomes remain visible without inheriting superseded authority. Two independent zero-baseline reviews found no remaining P0/P1. Real-route browser QA covered Patch workspace/detail, ref history, rollback detail, and the long-ID apply confirmation at desktop/narrow and light/dark with no page overflow or console errors; the only visual correction kept A—Editorial and gave long-ID ledgers internal scrolling without breaking workflow labels. Gates: 6 focused files / 54 Patch tests, 56 files / 497 full web tests, TypeScript including E2E, exact four-artifact contract drift, production build, Prettier, exact absent-ref backend admission regression, and diff-check all green.
- **Task 12 ✅ (2026-07-20):** Eval/Bench now renders all 15 defect-class BDR rows, separate oracle/constraint/narrative/external false-positive series, aggregate Fix Pass, Agent outcomes, external validity, HED, six Agent cost/latency workloads, deterministic runtime, version bindings, and the complete evidence catalog without inventing an overall score. The unchanged BenchReport body gains only an exact same-read `X-Artifact-ID` provenance header; legal reports are bounded by the frozen 8 MiB contract, and BDR power rows are bound to the exact partition, sample count, interval half-width, and evidence. Independent contract and page reviews closed QA catalog-versus-binding ambiguity and partial metric-evidence misreporting; no P0/P1 remain. Real-route browser QA covered desktop/390, light/dark, long IDs, BDR/HED charts, QA, and cost sections with no page overflow or console errors. Gates: 59 files / 527 web tests, TypeScript including E2E, four generated-contract artifacts, production build, Prettier, 124 focused backend tests, Ruff, and diff-check all green. Deferred M3 human QA remains visibly `qa.evidence_missing` and will be collected only after all eight pages are stable through a guided, protocol-compliant session. The normal Console may orient the product owner and present final results, but the frozen manual arm must remain isolated from Finding/Patch/Eval views; the post-V2 gate will choose between an external-editor hybrid and a narrow local QA Runner rather than pretending the read-only Eval page can capture evidence.
- **Task 13 ✅ (2026-07-20):** Observability now provides exact Run-correlated trace/span/log inspection, descriptor-bound system metrics, and negotiated `run-cost-view@2` settlement/usage while retaining the existing V1 cost response by default. Frozen UTC windows, owner fencing, opaque-cursor recovery, sparse-budget exact zero semantics, histogram bounds/cumulative counts, scoped truncation notices, and recursive client-side redaction were independently reviewed with no remaining P0/P1. The API and worker also install the fixed low-cardinality operational metric registry without introducing high-cardinality IDs, PromQL, a dashboard builder, or browser telemetry storage. Real-route browser QA passed desktop/390, light/dark, 512-character IDs, trace waterfall/inspector, internal table scrolling, redaction, and zero page overflow or console errors. Focused frontend gates are 5 files / 24 tests plus TypeScript, four generated-contract artifacts, production build, Prettier, and diff-check; focused backend cost/metric/observability suites, Ruff, dependency boundaries, schema, and diff-check are green.
- **Task 14 ✅ (2026-07-20):** Approvals queue/detail, frozen policy closure, immutable decision ledger, partial approve, reject/request-changes, exact ETag/revision/idempotency, 403/409/410 recovery, and per-action requirement eligibility/reasons are implemented over the existing five operations and tables. Read projection and transactional decisions resolve the ApprovalItem-frozen role/domain policy and share current-valid vote evaluation. Independent review also closed the role-restoration deadlock: when all current votes satisfy a still-pending item, only an existing effective voter may explicitly reconfirm; this advances to approved without double-counting, while apply reauthorization accepts only that exact terminal confirmation. No P0/P1 remain and no generic workflow builder was introduced. Real-route QA passed queue/detail, desktop/390, light/dark, 512-character IDs, internal keyboard scrolling, and the reconfirmation state with no page overflow or console errors. Gates: 66 files / 574 web tests, TypeScript, four generated-contract artifacts, production build, Prettier, Platform M4 813 tests, API 486 tests, Journey B 5 tests, 7/7 dependency contracts, schema, Ruff, format, and diff-check all green.
- **V2 ✅ (2026-07-20):** all eight stable page compositions were presented on desktop/mobile and light/dark real routes; generic/Aureus/unknown trace states were presented through their controlled renderer fixtures without pretending that fixture-only branches were production data. Technical visual QA found no page-level horizontal overflow or console errors. One concrete long-content defect found during the matrix was fixed without changing A—Editorial: the Patch workspace now keeps a 512-character Snapshot transition in a bounded, keyboard-focusable internal scroller instead of growing a vertical text wall. Full web gates after that correction were 66 files / 575 tests, TypeScript including E2E, exact four-artifact contract drift, production build, Prettier, and diff-check green. The product owner reviewed the compact eight-page desktop/mobile comparison and explicitly approved V2 without further corrections, then selected the recommended isolated local QA Runner for the deferred M3 sessions. This approval is not V3 approval and does not itself create human QA evidence.
- **Task 15 ✅ (2026-07-20):** the loopback-only, source-specific Runner, clean external-editor launch, frozen arm payloads, and synthetic browser rehearsal are implemented without entering the product Console/OpenAPI/database. Server-authoritative pause/resume and the exact 480-second deadline remove task/assistance outside active time and atomically capture a hash-bound submission that late editor writes, ambiguous responses, oracle failures, and interrupted publication cannot replace. Contamination failure stays visible while correctness remains hidden; participant-facing errors are stable and redacted. Independent zero-baseline review found no remaining P0/P1 or actionable code P2. Gates: 76 QA/external/boundary tests passed with one upstream TestClient deprecation warning; 17 measured-report/M3 acceptance tests separately prove `qa.evidence_missing` is unchanged; Ruff, touched-file format, JavaScript syntax, and diff-check are green. The product owner then approved the start, prepared, manual task/timer, deadline-frozen, paused, assisted, and recorded/protocol-failure states in a guided synthetic walkthrough. That walkthrough exposed one misleading recorded-state 08:00 timer; it was hidden for recorded/complete states, locked by the 16-test Runner suite, and explicitly re-approved. The disposable workspace, browser tab, and synthetic 2/8 evidence were deleted; no real participant workspace or QA evidence exists, and every pending/null M3 state remains unchanged. Task 15 engineering and operability gates are complete; the eight real sessions remain a separate post-V3 M3 follow-up.
- **Task 16 ✅ (2026-07-20):** Journey B now runs against a fresh temporary workspace through real local API/worker composition and a narrow HMR-off HTTPS Vite proxy on dynamic loopback ports. Two independent browser identities prove human Patch preview/diff, deterministic checker plus economy evidence, `Last-Event-ID` SSE recovery without duplicate events, trace/lineage/ref-history inspection, self-approval denial, stale workflow revision, apply, governed rollback, stale-ref conflict → explicit resolution → independent revision/revalidation/reapproval, and a regression Patch whose failed EvidenceSet/Finding leaves the exact ref pointer and full history unchanged. The local read composition now serves retained Spec snapshots, lineage-bound schema registry authority, deterministic snapshot diffs, and approval bindings needed by the real routes; no product call is intercepted or mocked, and browser plus launcher external egress fails closed. Exact economy tool identity remains proven by the existing M4c backend contract because the safe Artifact page intentionally does not reveal raw evidence payloads. Two independent final reviews found no remaining P0/P1; no visual or runtime-theme change was made. Gates: repeated exact Playwright 1/1 passes, 66 files / 576 web tests, 56 focused content/composition/Journey B tests, 27 dependency-boundary tests, TypeScript including E2E, four generated-contract artifacts, production build, Prettier, Ruff, touched-file format, and diff-check are green.
- **Task 17 ✅ (2026-07-20):** Journey A and constraint publication now run in fresh temporary workspaces through real local API/worker composition and the HMR-off Vite proxy. Journey A closes generation REPLAY → preliminary-gate SSE → non-empty config export → TaskSuite derivation → Review → actual Playtest → failed validation → repair REPLAY → re-derive/re-review/re-playtest → independent approval/apply → Eval/Observability. Old Patch/approval/evidence/suite authority fails closed, the target ref does not move early, and all six `generation_gate_rejected` Patch workflow commands reach the authority layer with schema-valid requests, return exact `409/revision_conflict`, and leave Run/Approval/transport/ref history unchanged. Constraint publication closes Agent and human drafts through a human-authored revision, exact deterministic validation, another identity's approval, and ref-CAS publication while preserving the candidate Artifact ID; missing revision, digest mismatch, stale ref, and proposer approval fail closed. The product owner approved the Review launch card and its minimal contract correction: `constraints=[]` requires Checker or Simulation, non-empty constraints require Checker, and read/Artifact-ID mismatch fails closed without a layout or style change. Product API calls are not mocked or intercepted, accepted Agent paths use cassette REPLAY, browser and launcher egress fail closed, and focused `repair_unverified` evidence proves no successor SubjectHead/ApprovalItem. Final independent review found no remaining P0/P1. Gates: Journey A and constraint-publication Playwright each 1/1, support fixtures 5 + 12 tests, Review 68 focused tests (17 Workspace tests), 66 files / 598 full web tests, TypeScript including E2E, production build, Prettier, Ruff, Python format, and diff-check are green.
- **Task 18 ✅ (2026-07-21):** the preregistered matrix now retains all eight stable product routes at 1440×900, 1280×720, 390×844, and 412×915 in light/dark, plus six controlled full-page fixtures covering reduced motion, long Chinese, a 512-character ID, pagination, streaming/error/empty states, diff/merge, KG, generic/Aureus/unknown trace rendering. The finite typed GET boundary runs the actual product routes, aborts undeclared product reads and all external HTTP(S)/WS, and labels every fixture synthetic, read-only, and non-authoritative. Visual QA corrected only the approved light brand-caption contrast and Review mobile snapshot-column collapse; page-specific geometry checks now cover shell, all CursorTables, key layouts, diff/merge/KG stability, and trace control overlap without introducing a general layout engine. WCAG-AA Axe, keyboard navigation, focus return, reduced motion, heading order, live-region restraint, self-hosted font weights, chart focus isolation, and waterfall keyboard access are closed. The evidence index is generated only after the update-free visual suite passes and records exact PNG dimensions/SHA-256/size plus explicit deferred scope. Independent visual, accessibility, and evidence reviews found no remaining P0/P1 or unnecessary abstraction. Gates: visual 70/70, accessibility 21/21, evidence-index tests 10/10, full frontend 67 files / 612 tests, TypeScript including E2E, production build, Prettier, and diff-check are green.
- **V3 ✅ (2026-07-21):** the product owner reviewed the local 70-capture visual evidence index and explicitly confirmed it had no issues. This approves the final A—Editorial visual, responsive, long-content, and accessibility evidence for M4d. It does not create or import any human QA session: the formal 8 sessions / 4 matched pairs remain a separate post-V3 M3 follow-up, and `qa.evidence_missing` remains authoritative until all evidence passes the frozen import protocol.
- **Task 19 ✅ (2026-07-21):** final adversarial reviews covered contract drift, fake authority, page-state gaps, keyboard/accessibility behavior, visual consistency, evidence honesty, and unnecessary abstraction; all confirmed P0/P1 findings are closed. The product owner approved the only final visible-copy correction, so Eval now states exactly that the eight sessions run through the isolated local QA Runner after V3 and that `qa.evidence_missing` remains until complete import; no layout or style changed. The final frontend gates passed `npm ci`, exact four-artifact contract drift, TypeScript, production build, Prettier, **67 files / 614 tests**, **5** real-browser E2E journeys, **70** visual checks, **21** accessibility checks, and `npm audit` with **0 vulnerabilities**. Repository gates passed exact **77 OpenAPI operations**, **78** OpenAPI/streaming tests, **31** M4c E2E tests, **4974 passed / 1 skipped** non-Bench tests, **790** Bench tests, **27** dependency-lint tests, **7/7** import contracts, four frozen schema artifacts, Ruff, the specified **80-file** format check, lock drift, and diff-check. Product APIs remain unmocked in the browser journeys, external network access remains fail-closed, and no M4e adapter, live LLM call, external font/CDN, hidden mock backend, or generic frontend framework entered the default path. M4d engineering is complete; M4 itself remains in progress until M4e, and the unexecuted 8-session/4-pair M3 follow-up remains honestly pending.
- **Post-V3 QA attempt failed / rejected from evidence (2026-07-21):** the product owner completed all eight Runner sessions, then disclosed Copilot AI use in manual sessions `qa-session-04` and `qa-session-05` despite the frozen records containing no-contamination attestations. Their recorded times and submissions remain unchanged; both sessions are `protocol_failure`, matched pairs 02 and 03 are invalid, and the whole attempt conclusion is `failed`. This is not a poor outcome eligible for retry. The attempted import, measured-evidence test, and report rebuild were stopped and removed from authoritative paths; the original outside-repository workspace is retained only for audit. No score is published, the same participant cannot retry the exposed cases, and `qa.evidence_missing` remains authoritative pending a separately approved protocol-valid follow-up.
- **Same-participant formal replacement blocked (2026-07-21):** before launching a replacement Runner, the full frozen 610 matched / 562 config-only Endless Sky universe was replayed under a preregistered exclusion and oracle policy. After excluding all exposed commits, lineage, and source paths, 427 round-trippable commits / 659 changed records produced only four generic-checker-qualified transitions: three `cyclic_dependency`, one `dangling_reference`, and none for `dead_quest` or `unreachable_target`. The required four classes × two cases cannot be met; native and lineage gates could only reduce the supply. No replacement cases, HED, protocol, workspace, or sessions were generated. The product owner approved stronger Runner contamination wording and unchanged A—Editorial layout, but the same participant was not allowed to reopen the old cases. The next protocol-valid path requires a different participant with no exposure to the old eight cases or assistance material; `qa.evidence_missing` remains unchanged.
- **Post-V3 QA accepted / measured (2026-07-21):** a different, unexposed `participant-04` completed the full frozen eight-session/four-pair study after the editor IPC path defect was corrected and independently smoke-tested. All eight sessions are protocol-valid; canonical import, final-patch replay, deterministic verdict rederivation, QA scoring, BenchReport JSON/text/HTML regeneration, and combined acceptance pass. Manual success is 0/4 and assisted success is 3/4. The authoritative `qa-evidence@2` has `evidence_sha256=e7e76d9a846efd7eeaae2b06641e170c15878f7cbf1ff98a79a733b1aa451142`, conclusion `savings`, mean 3.407599574483333 minutes, median 4.203912946883333 minutes, and 95% CI [1.2129956309041665, 5.037277463891666]. Before final acceptance, the old aggregation was found to deviate from approved design §11 `QA timeout/incorrect | cap time + success=false`: correct sessions use actual capped active time, incorrect/time-out sessions use the 8-minute cap, and immutable raw active time remains audit-only. No session event, patch, or verdict was rewritten. This is the only accepted/measured formal QA experiment. The earlier rejected attempt and launcher/infrastructure workspaces remain audit-only and contribute no observation, denominator, score, or report projection. `qa.evidence_missing` is therefore closed without retroactively changing any M4d engineering or visual gate.
- **Decision D2 ✅ (2026-07-19):** the four initial-create operations no longer require a fabricated `If-Match`; all existing-resource commands retain exact strong-ETag OCC, and pre-D2 strong-header idempotency replay remains hash-compatible without treating that header as create authority. The exact compiler-version binding read now exposes the complete canonical differential-engine tuple, while binding read, admission, and worker share one narrow built-in adapter contract covering handler/config schema, complete RunKind/input/output tuples, stochastic mode, and capabilities. Only this exact GET documents runtime 409. Five public-API adapter-drift mutations fail before 202 with no Run/budget/workflow/audit side effects. The approved four-header removal required a one-time generated OpenAPI baseline refresh after the compatibility guard correctly refused the breaking overwrite; the guard itself remains unchanged and the refreshed 4-artifact schema check plus generated TypeScript drift check pass. Independent review found no remaining P0/P1; focused gates include 201 core tests, 85 worker/registry/dependency tests, 6 full constraint-publication tests, 76 create/OpenAPI/authority tests, Ruff/format, and diff-check.

### Approved Decision D1 — live/record routing-policy authority

REPLAY can reuse the source Run's exact retained plan. LIVE/RECORD cannot currently choose among retained `RoutingPolicyV1` rows without guessing: the request intentionally carries no plan, the persistence API exposes only exact `(policy_version, routing_policy_digest)` lookup, and no active routing-policy pointer exists.

The proposed minimal closure is an exact deployment pointer, `routing_policy_version + routing_policy_digest`, configured at the local API composition boundary. The selected policy supplies its exact catalog reference; missing, stale, or mismatched authority fails closed. This adds no public DTO field, list endpoint, preset registry, table, migration, mutable “current” row, or model-selection logic to the frontend. It mirrors the existing exact role/workflow policy pointers while retaining all historical policy/catalog versions.

The product owner approved this closure on 2026-07-19. REPLAY must not read the deployment pointer.

### Approved Decision D2 — browser-safe create and constraint-validation authority

Task 7 contract tracing found two transport gaps that machine-driven M4c tests masked:

1. `POST /specs`, `POST /patches`, `POST /constraint-proposals`, and `POST /refs/{ref_name}/rollback-requests` require `If-Match`, but each creates a new immutable resource with no prior resource ETag to read. Their actual authority is the typed ref/subject guard: Spec uses `expected_ref`, Patch and rollback bind exact current refs, and a constraint draft atomically creates its absent SubjectHead before validation later freezes the target ref. The arbitrary create header is not consumed as OCC authority.
2. `ConstraintValidationAdmissionRequestV1` requires `differential_engines`, while admission accepts only the private built-in compiler tuple. Neither OpenAPI nor `ExecutionProfileViewV1` exposes that tuple, so a browser would have to duplicate hidden server authority.

The proposed minimal closure is:

- remove the semantically unusable `If-Match` requirement from those four initial-create operations while retaining CSRF, scoped idempotency, canonical request hashing, and their typed ref/subject guards; all revision/validate/submit/publish operations continue to require the latest server ETag;
- add one read-only versioned subresource bound to an exact compiler profile, returning that profile plus its required differential-engine tuple. Admission and the read projection resolve the same server-owned binding. The browser copies it verbatim; there is no solver picker, generic registry, table, migration, or frontend default.

The product owner approved D2 on 2026-07-19 and authorized future non-visual engineering decisions to follow the independently reviewed recommendation without another human gate. Frontend visual style, page composition, and aesthetic tradeoffs still require the planned product-owner discussions.

## Task 1: Pin the Frontend Toolchain, Tests, and Contract Generation

**RED tests**

- Assert Node/npm versions and exact direct dependency pins.
- Prove npm ci, TypeScript project build, Vitest/jsdom, and a Playwright smoke page work from a clean web directory.
- Prove the browser build makes no external font/CDN request.
- Generate TypeScript from OpenAPI plus the three SSE/WS JSON Schemas with stable output.
- Assert every API operation is available through generated path types, no handwritten duplicate wire DTO exists, and contract-check fails on drift.
- Compile representative discriminated unions for Problem, ApprovalView, RunCommand frames, and RunEvent data.

**Implementation**

- Replace the M0a package scripts with contract generation/check, typecheck, unit test, build, Playwright functional, Playwright visual, and format-check commands.
- Keep the pinned React/Vite/TypeScript base and add only the dependencies in the Minimal Frontend Stack table.
- Keep Vite as the only build tool; commit package-lock.json.
- Record the toolchain in .node-version plus packageManager/engines, and add all declared scripts including test:a11y.
- Configure same-origin relative API URLs. Vite HTTPS proxies /api for HTTP, SSE, and WS; Playwright ignores only the local test certificate and apps/api allowlists that exact origin. Do not add CORS to apps/api.
- Add deterministic generation/check scripts and committed read-only output under web/src/api/generated.
- Keep small handwritten UI models separate and named as view state, never as wire DTO replacements.

**Verify**

    cd web
    npm ci
    npm run toolchain:check
    npm run contracts:generate
    npm run contracts:check
    npm run typecheck
    npm run test
    npm run build
    npm exec playwright install chromium
    npm run test:e2e -- --grep "loads the console"

## Task 2: Close the Two Proven API Usability Gaps

The M4c API is complete for machine-driven tests but lacks two read projections needed by a real console. These are additive M4d prerequisites, not a frontend workaround.

**Frozen additive read contracts**

1. POST /api/v1/execution-options:resolve
   - This is a bounded, read-only resolver: it publishes no Artifact, Run, source binding, reservation, or audit mutation.
   - ExecutionOptionResolveRequestV1 freezes request_schema_version, resource_operation_id, RunKindRef, llm_execution_mode, prospective_request, and optional replay_source_run_id.
   - prospective_request is the existing closed union of Agent-capable public resource/generic Run request DTOs with execution_version_plan and cassette_artifact_id required to be null. It therefore includes the exact inputs, profiles, seed, domain, source goal text, and operation-specific parameters that will be submitted; the resolver never logs or returns source text.
   - resource_operation_id and request discriminator must map uniquely to the same active RunKind. The resolver covers every registry run kind that permits live/record/replay, including generation, repair, constraint proposal, LLM-assisted review, playtest, and Agent bench.
   - replay_source_run_id is required exactly for replay and forbidden for live/record. Replay additionally requires explicit replay + run + complete-domain authorization.
   - The server derives one exact option from the already-retained RunKind, execution-profile, model-catalog, routing-policy, policy/schema, source-Run, and cassette authorities. It creates no ExecutionPlanPreset registry or current-only default.
   - ExecutionOptionViewV1 freezes option_schema_version, option_id, resource_operation_id, RunKindRef, DomainScope, llm_execution_mode, full ExecutionVersionPlanV1, prospective_request_hash, resolved_request_hash, resolved profile binding digests, and optional source_run_id/cassette_artifact_id.
   - prospective_request_hash = sha256(canonical_json({resource_operation_id, run_kind, llm_execution_mode, prospective_request})). resolved_request_hash uses the same formula after inserting the returned execution plan and conditional cassette ID. option_id = execution-option:sha256: + sha256(canonical_json(ExecutionOptionViewV1 excluding option_id)).
   - For replay, the source Run’s exact request/input/profile/policy/schema closure and verified run-scoped cassette must equal the prospective request after deterministic source binding projection. A mismatch returns workflow_guard/revision_conflict and no option; it is never presented as compatible.
   - The caller copies the returned execution plan and cassette ID into the unchanged prospective request. Final Run admission independently reloads and revalidates all authority, so intervening drift still fails closed.
2. GET /api/v1/workflow-subjects/{artifact_id}/approval-binding
   - Response: SubjectApprovalBindingViewV1 with subject_artifact_id, subject_digest, subject_kind, subject_series_id, subject_revision, subject_head_revision, is_current_head, approval_id, workflow_revision, and approval_status.
   - It resolves Patch, ConstraintProposal, and RollbackRequest subjects, including superseded historical revisions, through authoritative SubjectHead/ApprovalItem data. Evidence-only rejected Patches have no binding and return not_found rather than a guessed ID.

The subject read uses existing read-snapshot/retention behavior. Both operations use resource/domain RBAC and Problem mapping, add no admission shortcut or new authority, and expose no prompt, raw response, secret, object-store location, or raw cassette record.

**RED tests**

- Prove an authorized caller can resolve an exact executable ExecutionVersionPlan and, for replay, an exact prospective-request-compatible source/cassette for every active Agent run kind without reading handler config, secrets, prompt bodies, or raw cassette responses.
- Prove a refreshed/deep-linked Patch, ConstraintProposal, or RollbackRequest resolves directly to its ApprovalView through SubjectApprovalBindingViewV1 with exact subject digest/head/workflow revisions, without guessing an internal ID or scanning the approval queue.
- Prove the resolver is side-effect free, bounded, fully authorized, versioned, exact-hash stable, OpenAPI-described, and additive under the existing compatibility checker.
- Prove existing 71 operations and M4c clients remain compatible; any new operation or response field is reflected by regenerated OpenAPI.

**Implementation**

- Add the exact resolver and subject read above over existing registry, Run, cassette, SubjectHead, and ApprovalItem authority.
- Derive execution options from retained authority; do not add a preset database, current-only default, BFF, or raw-registry endpoint.
- Do not change Run admission semantics, cassette validation, profile authority, or mutation endpoints.
- The product owner approved this frozen shape on 2026-07-19. Any shape change returns to discussion; no client hard-coded plan, cassette ID, or approval ID is acceptable.

**Verify**

    uv run pytest tests/contracts/m4c tests/apps/api/test_read_resources.py tests/apps/api/test_run_admission.py -q
    uv run pytest tests/apps/api/test_openapi.py tests/apps/api/test_streaming_schema.py -q
    uv run python -m gameforge.apps.api.schema --check
    cd web
    npm run contracts:generate
    npm run contracts:check
    npm run typecheck
    npm run test -- src/api/generated-contracts.test.ts

## Task 3: Implement HTTP, Auth, OCC, Cursor, SSE, and Run Commands

### HTTP/auth substep

**RED tests**

- Login stores the X-CSRF-Token only in sessionStorage, uses the HttpOnly Secure cookie, and clears local auth material on logout/401.
- Mutations attach CSRF, the operation’s exact idempotency location, and server-provided ETag/If-Match or expected revision only when the OpenAPI operation requires them.
- A retried identical intent reuses one key; a new intent gets a new crypto.randomUUID value.
- Problem responses preserve code, request_id, run_id, trace_id, earliest_cursor, and conflict_set_id while never rendering secret/raw internals.
- Opaque cursor pagination follows next_cursor verbatim. A 410 cursor_expired shows a clear restart action and never decodes or repairs the cursor client-side.

**Implementation**

- Build one openapi-fetch wrapper with credentials=include and page-specific typed functions.
- Keep CSRF/session handling small; if a new tab has a valid cookie but no CSRF token, allow reads and require re-authentication before mutation. Do not invent a refresh endpoint.
- Disable automatic mutation retry in TanStack Query.

**Verify**

    cd web
    npm run test -- src/api

### Streaming/command substep

**RED tests**

- Fetch SSE with Last-Event-ID from persisted run cursor, parse canonical frames, and deduplicate by run_id + seq.
- Backlog/reconnect duplicates do not duplicate UI state; terminal events close cleanly; 410 clears stale cursor only after an explicit restart choice.
- WS uses the frozen subprotocol/CSRF contract, sends one versioned RunCommandV1, and changes UI state only after persisted ACK.
- client_id is stable for the browser session, client_seq is positive and monotonically persisted, and retry of one command reuses command_id, idempotency_key, and client_seq.
- Duplicate ACK and reconnect recover through GET commands plus SSE without touching lease/fencing fields.
- Cancel works. provide_input stays disabled with an honest explanation when no authoritative interaction request exists.
- Run detail renders the exact RunView plus events, findings, result/failure manifest, commands, and trace links without inventing missing terminal state.

**Implementation**

- Use fetch plus eventsource-parser and sessionStorage for the per-run last seq.
- Use the browser WebSocket directly; implement a small send/await/recover client, not a socket framework.
- Add RunProgress, RunCommand controls, and the run-detail route shared by feature pages.

**Verify**

    cd web
    npm run test -- src/api/sse.test.ts src/api/commands.test.ts src/components/run-progress

## Task 4: Build the Approved Visual Foundation and App Shell

**RED/component tests**

- Apply every frozen light/dark surface, ink, line, and semantic token; type/spacing/radius/motion scales match §6.2.
- Source Han Serif SC is self-hosted with font-display swap; code/hash uses the frozen mono fallback.
- Theme preference persists, reduced-motion disables nonessential motion, and text/icon status remains understandable without color.
- The frozen --faint token is used only for disabled/nonessential decoration; small readable text uses at least --muted. Pill radius is limited to chip/status/toggle controls, and Source Han Serif VF exercises both 400 and 600 weights.
- App shell exposes the eight routes plus details, responsive navigation, breadcrumbs, identity/role chips, focus order, skip link, and semantic landmarks.
- Empty/loading/error/streaming/terminal states and Problem/Toast/Confirm controls are keyboard reachable.
- Login, /auth/me hydration, logout, session expiry, and post-login return routing work without exposing the session cookie or CSRF token.
- PermissionGate consumes explicit server eligibility booleans only and never maps role names to permissions.

**Implementation**

- Add tokens.css, global.css, layout primitives, local font subset and license notice, Lucide icons, and the responsive shell.
- Add React Router route boundaries and small Auth/Theme/Toast providers.
- Extract Chinese copy to zh-CN.ts without adding a translation framework.

**Verify**

    cd web
    npm run test -- src/app src/components src/styles
    npm run test:visual -- --grep visual-foundation

## Task 5: Build Shared Evidence, Diff/Merge, Chart, and Log Components

**RED/component tests**

- Deterministic findings and LLM suggestions are in separate labeled/icon containers, never color-only.
- Simulation evidence has its own descriptive label and is never folded into the deterministic-oracle or LLM-suggestion partition.
- FindingCard renders severity, oracle type, minimal repro, source_ref, and exact immutable revision.
- Diff renders missing separately from JSON null. MergeResolver shows base/current/proposed and only explicit keep-current/take-proposed/custom choices.
- CursorTable preserves stable pagination and restart state.
- Chart kit covers area spark, ring, horizontal bar, trace waterfall, and cost bar with textual summaries.
- LogExplorer shows redacted fields and trace links; prompt/raw response fields never render.
- Long Chinese text and 512-character IDs wrap/copy without expanding fixed toolbars.
- Artifact detail renders only the safe ArtifactSummary envelope, payload hash, VersionTuple, domain scope, and bounded lineage exposed by M4c; it never invents ObjectRef/ObjectLocation fields or treats Artifact existence as current ref authority.

**Implementation**

- Implement the frozen shared components directly under web/src/components.
- Use Recharts for standard charts and simple semantic DOM/CSS for the trace waterfall when that is clearer.
- Provide accessible table/list alternatives for data otherwise shown only graphically.
- Add artifact/lineage detail routes and link them from Patch, Review, Run, Eval, and rollback evidence.

**Verify**

    cd web
    npm run test -- src/components

## Task 6: Build KG and Playtest Trace Visualization

**RED/component tests**

- Cytoscape renders bounded graph pages with a synchronized searchable inspector/list and a stable simple layout.
- Keyboard/list users can inspect the same node/relation facts without using the canvas.
- Trace player supports play/pause/step/speed, tick/state_hash, action/event timeline, and failure/loop/stuck markers with Finding links.
- The static TraceRendererRegistry resolves bundled component keys only.
- TraceRendererRegistry enforces its frozen registry version/digest, unique sorted definitions, environment-contract/schema compatibility, capability checks, and active/disabled state.
- Generic, compatible Aureus spatial_2d, and unknown/disabled/incompatible renderer fixtures all remain inspectable; the latter always falls back to the generic timeline.

**Implementation**

- Add one Cytoscape wrapper and one generic TracePlayer state reducer.
- Add a small static versioned renderer registry plus bundled GenericTraceRenderer and Aureus2DRenderer.
- Validate only the selected renderer’s payload boundary; do not create a global plugin or payload-validation platform.

**Human gate V1**

- Present the real shell, shared evidence/diff/chart components, KG, and all three trace renderer states at desktop/mobile and light/dark.
- Record the product owner’s approved visual direction and corrections in this plan. Do not begin Task 7 page rollout until approval.

**Verify**

    cd web
    npm run test -- src/components/kg src/components/playtest

## Task 7: Implement the Spec/KG Page

**RED/feature tests**

- List/upload/read specs, page graph data, inspect schema version, and create IR edits only through typed Patch draft.
- Show Agent and human constraint-proposal entry paths distinctly.
- Require human revision, compile/validate, submit, another-human approval, and publish; make candidate Artifact versus authoritative constraint ref unmistakable.
- Display validation Run progress, failed/unproven states, exact approval navigation, and conflict resolution without guessing server IDs.

**Implementation**

- Build Spec/KG list/detail routes and constraint workflow panels over generated API calls.
- Reuse shared graph, run, evidence, and approval-link components; keep forms page-specific.

**Verify**

    cd web
    npm run test -- src/features/specs

## Task 8: Implement the Generation Page

**RED/feature tests**

- Select exact active generation, export, environment, and related profiles from the catalog; never use a hidden default.
- Require exact base/constraint and non-empty export profile for Journey A.
- Submit authenticated goal text, show SSE gate progress, and preserve the exact candidate chain.
- Gate pass shows Patch + preview + config export + evidence and offers derive/review/playtest next actions.
- generation_gate_rejected shows evidence-only Patch/preview/RunFailure and exposes no submit/apply/config execution control.
- Repair navigation makes the superseding revision and old non-inherited approval/evidence state clear.

**Implementation**

- Build one guided authoring flow backed by URL/run IDs, not a generalized workflow engine.
- Keep all verdict and eligibility text sourced from server state.

**Verify**

    cd web
    npm run test -- src/features/generation

## Task 9: Implement the Review Page

**RED/feature tests**

- List multiple Review artifacts without collapsing versions.
- Bind each report to its exact preview/snapshot/tool policy.
- Render deterministic findings and LLM suggestions in separate sections with counts and explanatory labels.
- Deep-link to exact Finding revision and preserve minimal repro/source_ref/evidence.
- Display failed, unproven, dismissed, fixed, and accepted-risk states honestly.

**Implementation**

- Build Review list/detail and Finding exact-revision routes using the shared evidence components.

**Verify**

    cd web
    npm run test -- src/features/review

## Task 10: Implement the Playtest Page

**RED/feature tests**

- Discover or derive an exact task suite from preview/config/constraint/environment.
- Require a non-empty exact episode/scenario subset and valid step budget.
- Run real Playtest through the API/worker, show resumable SSE, cancel, terminal result, trace playback, and exact Finding links.
- A stale suite against repaired config renders stale_task_suite as an actionable 409 and never silently reuses the suite.
- provide_input remains unavailable without authoritative interaction data.

**Implementation**

- Build suite discovery/derive, run launch, progress, result, and trace routes.
- Use the static renderer registry; Aureus remains a fixture, not a product branch.

**Verify**

    cd web
    npm run test -- src/features/playtest

## Task 11: Implement the Patch/Diff Page

**RED/feature tests**

- Render field-level diff, base/current/proposed merge, explicit conflict resolutions, and stale-conflict 409.
- Rebase/resolve creates a new Patch revision and clearly discards old validation/evidence/approval authority.
- Show repair, validate/regression, submit, approval, exact target binding, and apply states.
- Ref/history does not move before apply.
- Rollback follows draft → validate → submit → independent approve → apply, with ref-history and unchanged content lineage shown. RefTransition/audit authority remains verified by the existing backend regression because M4c exposes no public read endpoint for it.
- Self-approval and stale workflow/ref revisions remain visible fail-closed outcomes.

**Implementation**

- Build Patch list/detail, Diff/Merge, repair/validation, approval link, apply, rollback request, and ref-history panels.
- Do not implement client-side merge arbitration beyond submitting explicit server-defined choices.

**Verify**

    cd web
    npm run test -- src/features/patches

## Task 12: Implement the Eval/Bench Page

**RED tests**

- Show BDR/FP/Fix-Pass by defect class with confidence intervals, provenance, cost, and latency.
- Show Human-Edit-Distance and QA-hours when measured. Where BenchReport distinguishes oracle-FP and constraint-FP, render them as separate series and never merge them into one reassuring number.
- Render evidence_missing, not_measured, and pending_human_evidence as named missing states, never zero or pass.
- Preserve M3 qa.evidence_missing.

**Implementation**

- Build a report-focused page over the existing BenchReport read API and shared chart/evidence components.
- Keep provenance and missing-evidence explanations adjacent to every metric; do not build a generic BI/query designer.

**Verify**

    cd web
    npm run test -- src/features/eval

## Task 13: Implement the Observability Page

**RED tests**

- Navigate run → trace → spans/logs/exact metrics/cost with bounded queries and stable cursor handling.
- Show trace waterfall, descriptor version/unit, unknown usage distinctly from zero, and redacted log fields.
- Never expose prompt/raw response or hidden handler configuration.

**Implementation**

- Build run-correlated trace/span/log/metric/cost views using exact descriptor refs and existing bounded query controls.
- Reuse the shared chart/log primitives; do not add PromQL, a generic dashboard builder, or client-side telemetry storage.

**Verify**

    cd web
    npm run test -- src/features/observability

## Task 14: Implement the Approvals Page

**RED tests**

- List assignee=me and show proposer, domain, frozen policy, every requirement’s role/minimum/valid votes/distinct gaps, and current actor eligibility.
- Support partial approve, reject, and request changes with selected requirement IDs and exact workflow revision.
- Self-approval, stale revision, role loss, and unmet requirements show server-authoritative reasons.
- A real API+UI integration fixture covers a multi-domain partial approval and proves displayed requirement progress equals ApprovalViewV1 exactly.

**Implementation**

- Build queue/detail routes and requirement-scoped decision forms over ApprovalViewV1.
- Permission hints come only from current_actor_allowed_requirement_ids; every decision still relies on server authorization.

**Verify**

    cd web
    npm run test -- src/features/approvals

**Human gate V2**

- Present all eight stable page compositions plus generic/Aureus/unknown trace states at desktop/mobile and light/dark.
- Discuss and resolve aesthetic, density, and display issues before browser-journey finalization.

## Task 15: Provide the Isolated Local M3 QA Runner

**Boundary**

- Serve a separate loopback-only study surface over the frozen `qa-protocol@1`, existing one-session-at-a-time bundle materializer, monotonic timer, deterministic submission oracle, and evidence importer.
- The manual arm receives only its current source files, upstream task subject, fixed native syntax checker, timer, and workspace path. It never receives GameForge Finding/IR/Patch/Eval data, target locators, predicates, after bytes, future sessions, or normal Console navigation.
- The assisted arm receives only the already-frozen four-field `GAMEFORGE.json` payload for its current case. No new Agent call, generated answer, or hidden current-profile lookup is allowed.
- Use a clean extension-disabled external editor for the large source files rather than building a browser code editor. The largest frozen file is about 806 KiB and one case spans seven files; a bespoke editor would add complexity without improving the evidence contract.
- Do not add a product route, main OpenAPI operation, database table, login system, generic command framework, SSE/WS layer, or QA platform abstraction.

**TDD and implementation**

- Add a narrow source-specific local runner command to the existing Endless Sky QA composition while keeping timing, materialization, verdict, import, and scoring authority in their existing modules.
- Before `start`, expose no task subject, files, or assistance. After `start`, present only the current arm material and an exact clean-editor launch action.
- Provide explicit start, pause, resume, syntax-check, finish, contamination attestation, and next-session actions. Finish records the frozen verdict but withholds correctness feedback until all eight sessions are complete; completed sessions cannot be retried.
- Keep the browser timer advisory and derive evidence only from server-side monotonic events. Timeout and incorrect outcomes remain valid results; protocol contamination remains a visible protocol failure.
- Use a temporary synthetic workspace for code/browser QA. Delete it before the real participant begins; readiness rehearsal must not create `qa-session@1` evidence in the repository or change BenchReport/Eval pending states.

**Verify**

    uv run pytest tests/bench/qa/test_local_runner.py \
      tests/bench/external_cases/test_endless_sky_qa.py \
      tests/architecture/test_human_evidence_boundaries.py -q
    uv run ruff check gameforge/bench/qa gameforge/bench/external_cases/endless_sky_qa.py tests/bench/qa
    git diff --check

**Runner readiness and engineering completion gate**

- Present the neutral Runner start/task/timer/assistance/finish states with non-evidence fixture data and confirm that the product owner can operate it.
- Task 15 engineering completion ends when the synthetic readiness gates pass and the product owner confirms the controls are operable. It does not require, count, or simulate any real participant session.

**Separate non-blocking human-study handoff**

- After readiness confirmation, create a fresh outside-repository workspace and let the product owner perform the eight frozen sessions in order as an independent M3 follow-up. The Agent may guide controls but may not inspect, edit, answer, attest, retry, or simulate participant work.
- Until all eight sessions validate and are imported, preserve `qa.evidence_missing`, `pending_human_evidence`, `evidence_missing`, `not_measured`, and every pending/null QA metric exactly. Human session availability does not block Tasks 16–19 or change Task 15 engineering status.

## Task 16: Prove Journey B in Playwright

**Browser tests**

- Start real local apps/api and worker authority plus Vite HTTPS same-origin proxy in a fresh temporary workspace.
- Use two independent browser contexts/sessions A and B.
- Happy path: human Patch → preview → validation/economy evidence → submit → B approve → apply → rollback request → validate → submit → B approve → apply.
- Failure path: regression Patch produces Finding + failed EvidenceSet; submit/apply stay blocked and ref/history stay unchanged.
- Cover A self-approval, stale workflow/ref revision, one conflict→new revision→revalidation/reapproval UI flow, SSE reconnect, and visible lineage/ref-history/observability links. Audit linkage remains a backend gate, not a fabricated UI link.
- Deny external egress and do not intercept product API calls.

**Implementation**

- Reuse M4c local composition through a narrow test-only launcher.
- Use small journey helpers, not a broad page-object framework or mock backend.

**Verify**

    cd web
    npm run test:e2e -- --grep journey-b-liveops

## Task 17: Prove Journey A and Constraint Publication in Playwright

**Browser tests**

- Two independent contexts select exact profiles and constraints.
- Happy path: generation REPLAY → gate SSE → non-empty config export → derive suite → Review → actual Playtest Agent → exact Findings → failed validation → repair REPLAY → new preview/config → re-derive/re-review/re-playtest → passed validation → B approve → apply → Eval/Observability.
- Assert the old Patch cannot submit/apply, old approval/evidence do not carry, old suite against new config returns 409, target ref does not move before apply, and final ref equals the exact approved target binding.
- Failure path: generation_gate_rejected shows only evidence-only artifacts, every workflow command fails closed, and ref/history remain unchanged.
- Focused repair_unverified component/API integration proves no new SubjectHead/ApprovalItem without duplicating another full browser journey.
- Constraint UI flow proves Agent and human drafts both require a human-authored revision, exact candidate validation, another human approval, and ref-CAS publish. Missing human revision, target-digest mismatch, stale ref revision, and proposer approval all fail; the candidate Artifact ID is identical before and after authority moves to the ref.
- Every Agent path is cassette REPLAY and all external network is denied.

**Verify**

    cd web
    npm run test:e2e -- --grep journey-a-authoring
    npm run test:e2e -- --grep constraint-publication

## Task 18: Complete Visual, Responsive, and Accessibility Evidence

**Preregistered visual matrix**

- All eight stable pages at 1440×900, 1280×720, 390×844, and 412×915 across light/dark, using a finite matrix that covers every page, viewport, and theme without multiplying every transient state.
- Targeted fixtures additionally cover reduced-motion, long Chinese, long ID/hash, pagination, streaming, error, empty, merge, graph, and trace states.
- Generic, Aureus spatial_2d, and unknown renderer screenshots are retained.

**Automated checks**

- WCAG-AA contrast for both token sets, axe on every top-level page, keyboard-only navigation, visible focus, dialog focus return, semantic heading/landmark order, and live-region restraint.
- Dataviz palettes are checked for the frozen color-blind-safe distinctions; --faint, pill radius, and Source Han Serif 400/600 usage follow the token constraints.
- No horizontal overflow, clipped command bar, overlapping controls, unstable graph/diff/merge dimensions, or unexpected layout shift.
- Icon-only controls have aria-label plus hover/focus tooltip; status is never color-only.

**Human gate V3**

- Produce a compact contact sheet/index of screenshots and explicitly deferred non-M4d items. No frozen M4d acceptance failure may be recorded as an acceptable limitation.
- Review the rendered pages with the product owner, apply approved corrections, and record explicit approval before M4d completion.

**Verify**

    cd web
    npm run test:visual
    npm run test:a11y

## Task 19: Close M4d Repository Gates

**Integration assertions**

- Eight pages and detail routes consume generated types and real M4c APIs.
- The canonical OpenAPI operation count includes the two Task 2 operations, and generated TypeScript is clean against that committed document; 71 remains only the pre-M4d baseline.
- No client hard-codes a current profile, execution plan, cassette, approval ID, role decision, deterministic verdict, or target authority.
- SSE/WS recovery, two-identity approval, generic/Aureus/unknown rendering, and Journey A/B evidence remain green.
- No M4e adapter, live LLM call, external font/CDN, or hidden mock backend enters the default path.
- M3 QA is represented honestly: either the untouched pending/null state remains, or all eight real participant sessions have been hash-bound, deterministically revalidated, and imported before the report changes. Runner rehearsal or partial sessions never clear `qa.evidence_missing`; M4 remains incomplete until M4e.

**Verification gates**

    cd web
    npm ci
    npm run contracts:check
    npm run typecheck
    npm run test
    npm run build
    npm run format:check
    npm run test:e2e
    npm run test:visual
    npm run test:a11y

    cd ..
    uv run pytest tests/apps/api/test_openapi.py tests/apps/api/test_streaming_schema.py -q
    uv run pytest tests/e2e/m4c -q
    uv run pytest --ignore=tests/bench -q
    uv run pytest tests/bench -q
    uv run lint-imports
    uv run pytest tests/test_dependency_lint.py -q
    uv run ruff check .
    uv run ruff format --check gameforge/contracts/execution_profiles.py gameforge/contracts/api.py gameforge/platform/read_models gameforge/apps/api tests/contracts/m4c tests/apps/api
    uv run python -m gameforge.apps.api.schema --check
    uv lock --check
    git diff --check

**Review and completion**

- Run an adversarial review for contract drift, fake authority, page-state gaps, accessibility, visual consistency, and unnecessary abstraction.
- Fix confirmed findings and rerun all gates.
- Record exact passing counts, browser matrix, visual approval, and M4d-only acceptance evidence in this plan.
- Update AGENTS.md, CLAUDE.md, and docs/superpowers/plans/README.md without marking M4 or M4e complete.
- Commit without AI attribution. M4e planning begins only after M4d is green.

## Completion Evidence

- **Completed:** 2026-07-21. Tasks 1–19, D2, and human visual gates V1–V3 are complete for M4d.
- **Product and authority:** all eight stable pages plus detail routes use generated types and real M4c APIs; exact 77-operation OpenAPI and four generated artifacts match. Journey A, Journey B, constraint publication, two-identity approval, SSE recovery, generic/Aureus/unknown traces, ref-CAS, conflict recovery, and failure-without-ref-movement remain green with product API mocking/interception forbidden.
- **Visual and accessibility:** the approved A—Editorial direction covers 8 routes × 4 viewports × 2 themes = 64 product captures plus 6 targeted fixtures, for 70 update-free visual checks. The product owner approved V3 on 2026-07-21. Axe/semantic and keyboard/browser coverage passes 21/21, including visible focus, focus return, reduced motion, self-hosted font weights, bounded long-content scrollers, and mobile navigation.
- **Frontend gates:** 67 files / 614 tests, TypeScript including E2E, production build, Prettier, 5/5 real-browser E2E, 70/70 visual, 21/21 accessibility, four-artifact contract drift, clean `npm ci`, and 0 high-or-greater npm vulnerabilities.
- **Repository gates:** OpenAPI/streaming 78/78; M4c E2E 31/31; non-Bench 4974 passed / 1 skipped; Bench 790/790; dependency lint 27/27; import contracts 7 kept / 0 broken; four schema artifacts, Ruff, specified 80-file format check, uv lock, and diff-check all green. The Python suites emitted only the known upstream Starlette TestClient/httpx deprecation warning.
- **Review:** independent visual, accessibility, and evidence/authority reviews report no remaining P0/P1 and no unnecessary framework or M4e scope. No live LLM, external font/CDN, external browser egress, hidden mock backend, or production/DR adapter entered M4d.
- **Post-V3 QA failure:** M4 remains in progress and M4e production/DR adapters are not implemented. A formal eight-session attempt ran after V3, but the participant later disclosed Copilot AI use in manual sessions `qa-session-04` and `qa-session-05` while the frozen records attested no contamination. With times and submissions unchanged, those sessions are `protocol_failure`, matched pairs 02 and 03 are invalid, and the whole attempt conclusion is `failed`. Failed evidence was excluded before report publication; imported copies and the measured-acceptance change were removed, the outside-repository workspace remains only for audit, and the same exposed cases cannot be retried by this participant. At that point Eval and combined acceptance correctly remained `qa.evidence_missing` rather than zero or passed; the later independent `participant-04` evidence closed that state without contributing any observation, duration, or score from this rejected attempt.
- **Pre-push revalidation (2026-07-21):** the current tree passes 68 frontend files / 616 tests, production build and contract drift, 5 real-browser journeys, 70 visual checks, 21 accessibility checks, npm audit with zero vulnerabilities, 4974 passed / 1 skipped non-Bench tests, 813 Bench tests, 27 dependency-lint tests, 7/7 import contracts, four frozen schema artifacts, Ruff, format, lock, and diff checks. The accepted `participant-04` evidence independently revalidates to `e7e76d9a846efd7eeaae2b06641e170c15878f7cbf1ff98a79a733b1aa451142` when the exact frozen participant protocol is supplied. Journey A's final read-only Observability proof uses the still-current approver session after Apply, retaining real RBAC and the production short-session boundary rather than extending test credentials.

## M4d Traceability Matrix

| Frozen requirement | Tasks |
|---|---|
| Contract-generated typed client, auth/OCC/cursor, API usability closure | 1–3 |
| Resumable SSE and durable commands | 3 |
| Frozen editorial visual system and human aesthetic approval | 4, 6, 14, 18 |
| Shared evidence/diff/chart/log components | 5 |
| KG and generic/Aureus/unknown trace renderers | 6 |
| Spec/KG and authoritative constraint publication UX | 7, 17 |
| Generation and Review | 8–9 |
| Actual Playtest and trace playback | 10, 17 |
| Patch/Diff, merge, approval, apply, rollback | 11, 16 |
| Eval/Bench, Observability, Approvals | 12–14 |
| Isolated, protocol-preserving M3 QA Runner readiness | 15 |
| Honest M3 QA isolation and `qa.evidence_missing` preservation | 12, 15, 18, 19 |
| Journey B happy/failure, independent identities | 16 |
| Journey A happy/failure, repair and re-derived suite | 17 |
| Responsive, light/dark, keyboard, WCAG, screenshot evidence | 18 |
| Full repository compatibility and no premature M4 claim | 19 |
