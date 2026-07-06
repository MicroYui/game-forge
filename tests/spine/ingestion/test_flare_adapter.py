"""FlareTxtAdapter — typed entities + DROPS_FROM + source_ref + lossless round-trip
(M1 Task 10 / contract §12A.1 external-validity anchor).

Mirrors `test_aureus_adapter.py` / `test_outpost_scenario.py`'s structure, but
for the Flare INI-like `key=value` record format instead of CSV: each record
(an `[item]` block, or a whole enemy file) becomes one Entity whose `attrs`
holds the FULL ordered line content (comments, blanks, repeated keys — not a
plain dict, which would drop repeats/order), so `from_ir` can reconstruct the
exact original text purely from `attrs` + pk + `source_ref.row`.
"""

from pathlib import Path

from gameforge.contracts.ir import EdgeType, NodeType
from gameforge.spine.ingestion.flare_adapter import FlareTxtAdapter, read_flare_dir, render_records

_DIR = "scenarios/flare_sample"


def test_read_flare_dir_parses_items_and_enemies_preserving_repeats_and_comments():
    wb = read_flare_dir(_DIR)
    assert {r["pk"] for r in wb["items"]} == {"10", "31", "32", "56", "9100"}
    assert {r["pk"] for r in wb["enemies"]} == {"goblin", "skeleton"}

    gold = next(r for r in wb["items"] if r["pk"] == "10")
    kinds = [e["kind"] for e in gold["lines"]]
    assert "comment" in kinds  # "# Currency" / banner preserved
    assert "blank" in kinds
    loot_anim_values = [e["value"] for e in gold["lines"]
                        if e["kind"] == "kv" and e["key"] == "loot_animation"]
    assert len(loot_anim_values) == 3  # repeated key preserved as 3 separate entries, in order
    quality_values = [e["value"] for e in gold["lines"]
                      if e["kind"] == "kv" and e["key"] == "quality"]
    assert quality_values == ["normal", "currency"]  # literal repeat, order preserved

    goblin = next(r for r in wb["enemies"] if r["pk"] == "goblin")
    stat_values = [e["value"] for e in goblin["lines"] if e["kind"] == "kv" and e["key"] == "stat"]
    assert len(stat_values) >= 4  # goblin.txt has multiple `stat=` lines
    loot_values = [e["value"] for e in goblin["lines"] if e["kind"] == "kv" and e["key"] == "loot"]
    assert "32,5" in loot_values
    assert "loot/leveled_low.txt" in loot_values  # loot-table pointer, not an item ref


def test_to_ir_builds_typed_entities_with_source_ref():
    wb = read_flare_dir(_DIR)
    snap = FlareTxtAdapter().to_ir(wb, file_ref=_DIR)
    g = snap.to_graph()

    dagger = g.get_node("items:32")
    assert dagger is not None
    assert dagger.type is NodeType.ITEM
    assert dagger.source_ref.adapter == "flare"
    assert dagger.source_ref.sheet == "items"
    assert dagger.source_ref.file == "items.txt"

    goblin = g.get_node("enemies:goblin")
    assert goblin is not None
    assert goblin.type is NodeType.MONSTER
    assert goblin.source_ref.sheet == "enemies"
    assert goblin.source_ref.file == "enemies/goblin.txt"


def test_to_ir_derives_drops_from_edges_for_resolvable_loot_refs_only():
    wb = read_flare_dir(_DIR)
    g = FlareTxtAdapter().to_ir(wb, file_ref=_DIR).to_graph()

    # goblin: loot=32,5 -> item 32 exists -> one DROPS_FROM edge
    goblin_drops = g.neighbors("enemies:goblin", EdgeType.DROPS_FROM, direction="in")
    assert {r.src_id for r in goblin_drops} == {"items:32"}

    # skeleton: loot=32,5 AND loot=56,5 -> both items exist -> two DROPS_FROM edges
    skeleton_drops = g.neighbors("enemies:skeleton", EdgeType.DROPS_FROM, direction="in")
    assert {r.src_id for r in skeleton_drops} == {"items:32", "items:56"}

    # the loot-table pointer (`loot=loot/leveled_low.txt`) must NOT become an edge
    assert len(list(g.all_relations())) == 3  # exactly goblin's 1 + skeleton's 2, nothing spurious


def test_from_ir_reconstructs_records_field_level():
    wb = read_flare_dir(_DIR)
    adapter = FlareTxtAdapter()
    back = adapter.from_ir(adapter.to_ir(wb, file_ref=_DIR))
    assert back == wb  # contract §2 anchor: from_ir(to_ir(x)) == x, record level


def test_round_trip_reproduces_vendored_files_byte_for_byte():
    wb = read_flare_dir(_DIR)
    adapter = FlareTxtAdapter()
    back = adapter.from_ir(adapter.to_ir(wb, file_ref=_DIR))

    original_items = Path(_DIR, "items.txt").read_text(encoding="utf-8")
    assert render_records(back["items"]) == original_items

    original_goblin = Path(_DIR, "enemies/goblin.txt").read_text(encoding="utf-8")
    goblin_rec = [r for r in back["enemies"] if r["pk"] == "goblin"]
    assert render_records(goblin_rec) == original_goblin

    original_skeleton = Path(_DIR, "enemies/skeleton.txt").read_text(encoding="utf-8")
    skeleton_rec = [r for r in back["enemies"] if r["pk"] == "skeleton"]
    assert render_records(skeleton_rec) == original_skeleton


def test_snapshot_diff_of_two_to_ir_passes_is_empty():
    wb = read_flare_dir(_DIR)
    adapter = FlareTxtAdapter()
    snap_a = adapter.to_ir(wb, file_ref=_DIR)
    wb2 = adapter.from_ir(snap_a)
    snap_b = adapter.to_ir(wb2, file_ref=_DIR)
    assert snap_b.to_graph().diff(snap_a.to_graph()).is_empty()


def test_comment_that_looks_like_a_kv_line_is_not_parsed_as_kv():
    # Scathelocke's Spellbook (id=9100) has a commented-out `#quest_item=true`
    # line -- it must round-trip as a COMMENT, never as a kv pair (fail-open
    # `#`-prefixed lines would silently resurrect a disabled field).
    wb = read_flare_dir(_DIR)
    spellbook = next(r for r in wb["items"] if r["pk"] == "9100")
    kv_keys = [e["key"] for e in spellbook["lines"] if e["kind"] == "kv"]
    assert "quest_item" not in kv_keys
    comment_texts = [e["text"] for e in spellbook["lines"] if e["kind"] == "comment"]
    assert "#quest_item=true" in comment_texts
