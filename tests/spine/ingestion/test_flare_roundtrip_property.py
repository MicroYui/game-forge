"""Property tests for FlareTxtAdapter (M1 Task 10, §12A.1 round-trip property line).

Two independent properties, mirroring `test_roundtrip_property.py`'s pattern
for the Aureus CSV adapter:

1. Text-level: for ANY generated Flare-format text (comments / blanks /
   `key=value` lines, including repeated keys, section headers, and opaque
   directive lines), `render_flare_lines(parse_flare_text(x)) == x` — parsing
   is lossless by construction at the raw-text level.
2. IR-level: for ANY generated set of item/enemy records (including repeated
   keys within a record), `from_ir(to_ir(x)) == x` at the record level, and
   re-`to_ir`-ing the reconstructed workbook produces a snapshot with an
   empty diff against the original.
"""

from hypothesis import example, given, strategies as st

from gameforge.spine.ingestion.flare_adapter import (
    FlareTxtAdapter,
    parse_flare_text,
    render_flare_lines,
)

# --- text-level: parse -> render reproduces the exact original text ---

_key = st.from_regex(r"[a-z][a-z_]{0,8}", fullmatch=True)
_value = st.from_regex(r"[A-Za-z0-9_./,:-]{0,12}", fullmatch=True)  # never contains '=' or '\n'
_comment_line = st.from_regex(r"#[A-Za-z0-9_ ]{0,20}", fullmatch=True)
_raw_line = st.from_regex(r"[A-Z][A-Za-z0-9_/. ]{0,20}", fullmatch=True)  # no '=', no leading '#'/'['

_line_text = st.one_of(
    st.just(""),  # blank
    _comment_line,
    st.builds(lambda k, v: f"{k}={v}", _key, _value),  # kv (repeats occur naturally across draws)
    st.just("[item]"),  # section header
    _raw_line,  # opaque directive-like line (e.g. "INCLUDE foo/bar.txt")
)


@given(st.lists(_line_text, max_size=25))
@example([])
@example(["id=1", "name=X", "bonus=a", "bonus=b", "bonus=c"])  # explicit repeated-key case
@example(["# banner", "", "[item]", "id=1", "#disabled=true", "stat=hp,1", "stat=mp,2"])
def test_parse_then_render_reproduces_original_text(lines):
    content = "\n".join(lines)
    parsed = parse_flare_text(content)
    assert render_flare_lines(parsed) == content


# --- IR-level: from_ir(to_ir(x)) == x (record level) + snapshot diff empty ---

_extra_kv = st.builds(
    lambda k, v: {"kind": "kv", "key": k, "value": v},
    st.sampled_from(["stat", "bonus", "requires_stat", "dmg", "price"]),
    _value,
)

_item_record = st.builds(
    lambda pk, extra: {"pk": pk, "lines": [{"kind": "kv", "key": "id", "value": pk}] + extra},
    st.from_regex(r"[1-9][0-9]{0,3}", fullmatch=True),
    st.lists(_extra_kv, max_size=6),
)

_enemy_record = st.builds(
    lambda pk, extra: {"pk": pk, "lines": extra},
    st.from_regex(r"[a-z]{3,10}", fullmatch=True),
    st.lists(_extra_kv, min_size=1, max_size=6),
)


def _drop_empty_sheets(wb):
    return {k: v for k, v in wb.items() if v}


def _wrap(records, file_ref):
    return [
        {"pk": r["pk"], "file": file_ref, "row": i, "lines": r["lines"]}
        for i, r in enumerate(records)
    ]


@given(
    items=st.lists(_item_record, max_size=5, unique_by=lambda r: r["pk"]),
    enemies=st.lists(_enemy_record, max_size=5, unique_by=lambda r: r["pk"]),
)
@example(
    items=[{"pk": "1", "lines": [{"kind": "kv", "key": "id", "value": "1"},
                                  {"kind": "kv", "key": "bonus", "value": "a"},
                                  {"kind": "kv", "key": "bonus", "value": "b"}]}],
    enemies=[{"pk": "goblin", "lines": [{"kind": "kv", "key": "loot", "value": "1,5"},
                                        {"kind": "kv", "key": "loot", "value": "loot/x.txt"}]}],
)
def test_records_roundtrip_through_ir_and_snapshot_diff_empty(items, enemies):
    wb = {
        "items": _wrap(items, "items.txt"),
        "enemies": _wrap(enemies, "enemies/gen.txt"),
    }
    adapter = FlareTxtAdapter()
    snap_a = adapter.to_ir(wb, file_ref="gen")
    wb2 = adapter.from_ir(snap_a)
    assert _drop_empty_sheets(wb2) == _drop_empty_sheets(wb)  # field-level equality
    snap_b = adapter.to_ir(wb2, file_ref="gen")
    assert snap_b.to_graph().diff(snap_a.to_graph()).is_empty()  # snapshot diff = ∅
