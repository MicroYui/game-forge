# Aureus Outpost — Quest & Economy Design Notes (v3)

These notes are the source material the Extraction Proposer reads. Each rule
below is stated the way a designer would write it; the agent PROPOSES a typed
constraint for each, a human authors the authoritative version, and the
deterministic checkers enforce it.

## Quest rewards
- A side quest must never award more than 100 gold. Main-story quests may award
  up to 500 gold.
- Every quest step's `giver` must reference an NPC that actually exists in the
  outpost roster — no quest may be handed out by a nonexistent character.
- A quest is only completable when its final step is reachable by following the
  step-to-step `next` links from the opening step (no orphaned end states).

## Drop tables
- Every drop-table entry must point at an item that exists in the item catalog.
- The drop probabilities within a single drop table must sum to exactly 1.0.

## Gacha
- The advertised 5-star pull rate must match the true expected pull rate within
  0.5 percentage points.
- A player pulling at the banner's stated rate must reach pity no later than the
  90th pull.

## Progression curves
- The XP-required-to-reach-next-level curve must be monotonically
  non-decreasing across every level.

## Economy
- The total currency faucet (all sources) must not exceed the total currency
  sink over a full play session by more than 20%, or the economy inflates.
