import yaml

from gameforge.apps.cli.run_slice import run_slice


def test_vertical_slice_completes_three_step_chain():
    out = run_slice("scenarios/caravan.yaml", seed=0)
    assert out["findings"] == []  # clean config passes the checker gate
    assert out["completed"] is True  # talk -> collect -> turn_in reached "completed"
    assert out["ticks"] >= 3


def test_slice_is_reproducible():
    a = run_slice("scenarios/caravan.yaml", seed=0)
    b = run_slice("scenarios/caravan.yaml", seed=0)
    assert a["final_hash"] == b["final_hash"]
    assert a["trajectory"] == b["trajectory"]


def test_checker_gate_blocks_broken_scenario(tmp_path):
    data = yaml.safe_load(open("scenarios/caravan.yaml"))
    data["interactables"] = []  # remove the collect source
    data["spawn_points"] = []
    p = tmp_path / "broken.yaml"
    p.write_text(yaml.safe_dump(data))
    out = run_slice(str(p), seed=0)
    assert any(f["defect_class"] == "missing_drop_source" for f in out["findings"])
    assert out["completed"] is False
    assert out["blocked_by_checker"] is True


def test_ir_to_world_reconstructs_ordered_steps():
    from gameforge.apps.cli.ir_to_world import snapshot_to_world
    from gameforge.spine.ir.loader import load_scenario

    wc = snapshot_to_world(load_scenario("scenarios/caravan.yaml"))
    steps = wc.quests[0].steps
    assert [s.kind for s in steps] == ["talk", "collect", "turn_in"]
    assert wc.grid.width == 12 and wc.scenario.start_pos == (0, 0)
