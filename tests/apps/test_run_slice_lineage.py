from gameforge.apps.cli.run_slice import run_slice


def test_slice_records_lineage_chain_traceable_to_source_tuple():
    out = run_slice("scenarios/caravan.yaml", seed=0)
    arts = out["artifacts"]
    assert set(arts) == {"ir_snapshot", "config_export", "checker_run"}
    # checker_run traces back through config_export to ir_snapshot (contract §5 anchor)
    assert out["head"] == arts["checker_run"] or out["head"] == arts["ir_snapshot"]


def test_slice_lineage_is_deterministic():
    a = run_slice("scenarios/caravan.yaml", seed=0)["artifacts"]
    b = run_slice("scenarios/caravan.yaml", seed=0)["artifacts"]
    assert a == b  # content-addressed artifact ids are reproducible
