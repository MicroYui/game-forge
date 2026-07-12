# GameForge Pre-M4 Core Corrections Design

> `DROPS_FROM` direction, Patch stale-base semantics, and repair cassette stability

| Field | Value |
|---|---|
| Status | Approved by the already-approved Pre-M4 product-closure design and the user's standing implementation authorization |
| Date | 2026-07-12 |
| Scope | Pre-M4 product-closure sub-milestone 2 only |
| Truth sources | `2026-07-03-gameforge-prd.md`, `2026-07-03-gameforge-foundations-contracts.md`, `2026-07-11-pre-m4-product-closure-design.md` |
| Branch | `codex/pre-m4-core-contracts` |

## 0. Decision Summary

This sub-milestone corrects three existing contract violations before a second
external-game Adapter can copy them:

1. Keep `ir-core@1` and make every emitted `DROPS_FROM` relation point from a
   producer to an item or currency.
2. Make `apply_patch` reject a Patch whose `base_snapshot_id` is not the exact
   current Snapshot before evaluating any precondition or operation.
3. Remove `base_snapshot_id` from the repair model request, bump the repair
   prompt to `repair@4`, and give each drafted Patch a deterministic identity
   bound to `{request_hash, base_snapshot_id, ops}`.
4. Re-record only the active repair requests and the one active generation
   request with `openai / gpt-5.6-sol / pre-m4@1`. Preserve historical
   extraction, consistency, and playtest cassettes under
   `anthropic / claude-opus-4-8 / m2a@1`.

The work is a correction of already-locked interfaces, not a new feature or a
game-specific optimization. It does not authorize Endless Sky B0B, a new
Adapter, narrative evaluation, HED, QA-hours measurement, or M4.

## 1. Current Failures

### 1.1 Reverse item-drop relations

The contract-wide consumer convention is `producer -> product`:

```text
MONSTER | DROP_TABLE | INTERACTABLE | EVENT | BATTLE_ENCOUNTER
    --DROPS_FROM--> ITEM | CURRENCY
```

Graph, ASP, scenario generation, benchmark injection, and economy simulation
already consume that direction. Aureus item drops and Flare direct loot are the
two reverse producers:

```text
current Aureus: ITEM -> MONSTER
current Flare:  ITEM -> MONSTER
correct:        MONSTER -> ITEM
```

The reverse edge fails the real `collect needs source` query because that query
looks for a `GRANTS` or `DROPS_FROM` edge whose destination is the collected
item. Aureus currency drops are already correct as `MONSTER -> CURRENCY`.

No tracked database, object-store Snapshot, or serialized IR artifact contains
the reverse relation as a published compatibility surface. The reverse shape is
derived afresh from source workbooks, so this correction remains `ir-core@1`.

### 1.2 Patch base is recorded but unenforced

`Patch.base_snapshot_id` exists in the foundational contract, but
`apply_patch()` currently starts from whichever Snapshot the caller supplies.
An old Patch can therefore apply to a different Snapshot if its narrower
preconditions happen to pass. Missing precondition fields can also leak
`KeyError` instead of a domain-level `PatchRejected`.

### 1.3 Repair requests bind irrelevant content hashes

`RepairDrafter._build_user_prompt()` appends the complete base Snapshot ID.
Any unrelated IR attr change then changes the model request hash even when the
finding, focus nodes, incident relations, neighboring IDs, entity catalog, and
evidence are identical. This invalidates the whole repair cassette corpus for
changes that cannot affect the model's answer.

The existing Patch ID is exactly that request hash. Once the request becomes
stable across semantically equivalent bases, using it as the Patch ID would
make two base-bound Patches collide.

## 2. Chosen Architecture

### 2.1 `DROPS_FROM` remains one source relation

The two Adapter changes are local boundary translations:

```text
Aureus monster row + referenced drop-table entry
    -> MONSTER --DROPS_FROM--> ITEM

Flare enemy `loot=<known-item-id>,<chance>`
    -> MONSTER --DROPS_FROM--> ITEM

Aureus monster gold attrs
    -> MONSTER --DROPS_FROM--> CURRENCY  (unchanged)
```

Aureus keeps `monster.attrs.drop_table_id` and the DropTable entity so the CSV
round trip remains field-lossless. The derived direct-source edge uses the
Monster as producer because `ir-core@1` has no ownership relation between a
Monster and its DropTable. This sub-milestone will not smuggle ownership through
`REFERENCES`, `TRIGGERED_BY`, or another unrelated edge.

Economy extraction accepts a currency faucet only when:

- the relation is `DROPS_FROM`;
- the destination is a `CURRENCY`; and
- the source node is one of the contract-legal producer types.

This prevents an arbitrary reverse or malformed entity-to-currency relation
from becoming simulated income.

Adapter conformance tests inspect every emitted `DROPS_FROM` relation and require
a legal producer source and an `ITEM` or `CURRENCY` destination. Focused Graph
and ASP tests prove that a correct forward edge clears `missing_drop_source`
and a reverse edge does not.

### 2.2 Exact-base Patch rejection

The apply order is locked as:

```text
apply_patch(current, patch):
  1. current.snapshot_id must equal patch.base_snapshot_id
  2. copy current graph
  3. validate every existing precondition
  4. validate and apply ordered ops to the private graph
  5. return a newly content-addressed Snapshot
```

Base mismatch rejects no-op, add, delete, set, replace, and `dry_run` alike.
It happens before graph copying, precondition lookup, or operation handling.
There is no implicit rebase.

The existing precondition vocabulary remains exactly:

```text
entity_exists: {kind, id}
attr_equals:   {kind, target, value}
```

Missing, empty, or wrongly typed required fields are malformed conditions and
raise `PatchRejected`. Unknown `kind` also raises `PatchRejected`. The engine
does not add a new DSL, a missing-vs-null sentinel, or relation-level merge
semantics.

The private-graph application model remains atomic: a rejection after an
earlier operation discards the private copy, and the input Snapshot remains
unchanged.

### 2.3 Stable request identity and base-bound Patch identity

The repair model request continues to include every model-relevant input:

- defect class, message, entities, relations, and evidence;
- full focus-node attrs;
- incident relation IDs/types/endpoints;
- neighboring nodes;
- bounded entity ID catalog;
- valid edge types; and
- deterministic counterexample on refinement rounds.

It no longer includes `base_snapshot_id`. The prompt version changes from
`repair@3` to `repair@4`, so old repair cassettes cannot be mistaken for the new
request contract.

The identities are separated:

```text
producer_run_id = model request_hash

patch.id = sha256(canonical_json({
  "request_hash": request_hash,
  "base_snapshot_id": snapshot.snapshot_id,
  "ops": [typed op JSON in model order]
}))
```

Thus two semantically identical requests on bases that differ only in an
irrelevant attr reuse one model cassette and retain the same producer run ID,
but produce different Patch IDs and cannot cross-apply.

### 2.4 Mixed historical and active cassette policy

The repository intentionally contains two model generations after this work:

| Surface | Model snapshot | Action |
|---|---|---|
| Active repair corpus | `openai/gpt-5.6-sol/pre-m4@1` | Re-record all active requests |
| Active generation sample | `openai/gpt-5.6-sol/pre-m4@1` | Re-record once because the clean Snapshot changes |
| Extraction sample | `anthropic/claude-opus-4-8/m2a@1` | Preserve bytes; replay only |
| Consistency sample | `anthropic/claude-opus-4-8/m2a@1` | Preserve bytes; replay only |
| Playtest corpus | `anthropic/claude-opus-4-8/m2a@1` | Preserve bytes; replay only |

The repair harness REPLAY router moves to the active `DEFAULT_SNAPSHOT` after
the new cassettes exist. The playtest harness stays pinned to
`M2_REPLAY_SNAPSHOT`. During a repair RECORD run, extraction and consistency
samples are exercised through a separate historical REPLAY router, while
repair and generation use the active RECORD router. This prevents a routine
repair refresh from silently re-recording unrelated M2 evidence.

New `gpt-5.6-sol` requests use the Responses transport and omit `temperature`.
Historical Opus request hashes keep their original `temperature=0`; no cassette
is rewritten to pretend it was produced by another model or parameter set.

After all active requests replay successfully twice, old root-level cassettes
whose `agent_node_id` is `repair` or `generation` are deleted. Root-level
`extraction` and `consistency` files and all `cassettes/playtest/**` bytes are
left unchanged. This is a bounded cleanup, not a general cassette GC service.

## 3. Alternatives Rejected

### 3.1 `ir-core@2`, dual direction, or a migrator

Rejected because the bad direction is a derived implementation bug with no
published persisted IR compatibility obligation. Supporting both directions
would make the checker ambiguous and preserve a known false negative.

### 3.2 Add `USES_DROP_TABLE` now

Rejected because the foundational contract does not define it. The source
workbook already preserves `drop_table_id`; a new ownership edge requires a
separate contract change and migration design if a future product query needs
it.

### 3.3 Automatic Patch rebase or merge

Rejected for this milestone. Exact-base rejection is one of the two behaviors
allowed by the foundational `rebase-or-reject` contract and closes the current
safety hole without inventing conflict-resolution semantics.

### 3.4 Keep base hash in the prompt and tolerate re-recording

Rejected because it couples model identity to irrelevant content-addressed
state. A cassette should change when the model-visible repair problem changes,
not whenever an unrelated row changes.

### 3.5 Re-record every historical Agent role with the new model

Rejected because it would erase the evidentiary identity of already-completed
M2 experiments and spend calls unrelated to this correction. New work uses the
new model; old claims remain tied to the model that actually produced them.

## 4. Error Handling

| Failure | Required behavior |
|---|---|
| Patch base differs | Immediate `PatchRejected`; no precondition/op evaluation |
| Malformed precondition | `PatchRejected`; never `KeyError`/`TypeError` leakage |
| Old-value mismatch | Existing `PatchRejected` behavior remains |
| Later op fails | Discard private graph; base Snapshot unchanged |
| Reverse Adapter edge reappears | Conformance test fails |
| Reverse edge supplied manually | Graph/ASP still report missing source |
| Invalid currency producer type | Economy model does not create a source |
| Active cassette missing in REPLAY | `CassetteReplayMiss`; never live fallback |
| Historical sample cassette missing during RECORD | Report sample failure; do not live re-record it |
| Live gateway transient error | Existing bounded retry/backoff applies |
| Active repair fails deterministic verifier | Remains a failed corpus case; no threshold or scenario editing |

## 5. TDD and Verification

### 5.1 Patch contract

- exact-base success for every existing operation family;
- stale-base rejection for no-op, add, delete, set, and `dry_run`;
- stale rejection happens before a malformed precondition can be evaluated;
- malformed `entity_exists` and `attr_equals` shapes become `PatchRejected`;
- a failure after one valid op leaves the input Snapshot unchanged.

### 5.2 Repair identity

- `repair.system` and `repair.refine` are both `repair@4`;
- user prompt contains no base Snapshot ID;
- two bases with identical model-visible context yield one request hash;
- their Patch IDs differ and bind their exact bases;
- `producer_run_id` remains the request hash;
- applying either Patch to the other base is rejected.

### 5.3 Relation direction

- Aureus item and currency drops have legal endpoints;
- Flare direct loot has legal endpoints;
- Adapter round trips and source refs remain unchanged;
- forward relations clear missing-source in Graph and ASP;
- reverse relations do not clear it;
- the economy model ignores a relation from an illegal producer type.

### 5.4 Cassette and product acceptance

1. Run the active RECORD harness with `gpt-5.6-sol` and resume enabled.
2. Require Fix Pass Rate `10/10`; do not weaken scenarios, gates, or max-step
   accounting to reach it.
3. Run REPLAY twice with network-disabled transport and compare complete result
   objects.
4. Confirm active repair/generation cassettes name the new model snapshot and
   historical extraction/consistency/playtest hashes are unchanged.
5. Delete only unreachable old repair/generation root cassettes.
6. Run focused suites, full `pytest`, all seven import-linter contracts, Ruff,
   `git diff --check`, and Flare/Endless Sky frozen-evidence regression checks.

## 6. Acceptance Boundary

This sub-milestone is complete only when:

- `DROPS_FROM` is consistently producer-to-product across both current
  Adapters and deterministic consumers;
- stale Patch application is impossible and malformed preconditions fail
  closed;
- repair request identity is stable under model-invisible base changes while
  Patch identity remains base-bound;
- the active repair corpus replays twice at `10/10` under the new model;
- unrelated historical cassettes and all frozen external evidence remain
  byte-identical; and
- the full repository verification is green.

M3 remains incomplete after this correction. Endless Sky stays
`awaiting_human_evidence`, and M4 remains unauthorized until the separate
external qualification, narrative/HED/QA, and BenchReport acceptance work is
complete.
