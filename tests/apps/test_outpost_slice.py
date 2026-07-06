from gameforge.apps.cli.run_slice import run_slice_workbook


def test_outpost_four_system_slice_completes_and_is_deterministic():
    a = run_slice_workbook("scenarios/outpost", seed=0)
    assert a["findings"] == []          # clean config passes the checker gate
    assert a["completed"] is True       # talk->collect->fight->turn_in reached completion
    b = run_slice_workbook("scenarios/outpost", seed=0)
    assert a["final_hash"] == b["final_hash"] and a["trajectory"] == b["trajectory"]


def test_outpost_exercises_all_four_systems():
    out = run_slice_workbook("scenarios/outpost", seed=0)
    assert {"quest", "combat", "economy", "gacha"} <= set(out["systems_exercised"])
