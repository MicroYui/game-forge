from hypothesis import given, strategies as st
from gameforge.spine.ingestion.aureus_adapter import AureusCsvAdapter

_ids = st.from_regex(r"[a-z]{1,6}", fullmatch=True)


def _drop_empty_sheets(wb):
    return {k: v for k, v in wb.items() if v}


@given(
    items=st.lists(st.builds(lambda i: {"item_id": f"item:{i}", "name": i.upper()}, _ids),
                   max_size=6, unique_by=lambda r: r["item_id"]),
    monsters=st.lists(
        st.builds(lambda i, hp: {"monster_id": f"m:{i}", "name": i, "stats": {"hp": hp},
                                 "skills": [], "drop_table_id": None, "ai": "aggressive"},
                  _ids, st.integers(1, 99)),
        max_size=5, unique_by=lambda r: r["monster_id"]),
)
def test_roundtrip_is_lossless(items, monsters):
    wb = {"items": items, "monsters": monsters}
    adapter = AureusCsvAdapter()
    snapA = adapter.to_ir(wb, file_ref="gen")
    wb2 = adapter.from_ir(snapA)
    snapB = adapter.to_ir(wb2, file_ref="gen")
    # from_ir emits ONLY sheets that have entities (brief §Task 8 Step 3) — a
    # sheet key present with an empty list is indistinguishable, at the graph
    # level, from that key being absent entirely (0 entities either way), the
    # same equivalence csv_format.read_workbook already makes for a missing
    # sheet file. So compare with empty-list sheets dropped from both sides;
    # the real invariant under test is that every actual row round-trips.
    assert _drop_empty_sheets(wb2) == _drop_empty_sheets(wb)  # field-level equality
    assert snapB.to_graph().diff(snapA.to_graph()).is_empty()  # snapshot diff = ∅
