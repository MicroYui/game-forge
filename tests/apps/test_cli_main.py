from gameforge.apps.cli.__main__ import main


def test_cli_main_runs_slice_and_exits_zero():
    assert main(["scenarios/caravan.yaml", "0"]) == 0


def test_cli_main_defaults_to_caravan():
    assert main([]) == 0
