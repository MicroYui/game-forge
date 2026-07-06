from gameforge.apps.cli.__main__ import main


def test_cli_main_runs_slice_and_exits_zero():
    assert main(["scenarios/caravan.yaml", "0"]) == 0


def test_cli_main_defaults_to_caravan():
    assert main([]) == 0


def test_cli_main_review_clean_scenario_exits_zero(capsys):
    rc = main(["review", "scenarios/defects/clean", "scenarios/constraints"])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"deterministic_findings": 0' in out


def test_cli_main_review_defect_scenario_exits_nonzero(capsys):
    rc = main(["review", "scenarios/defects/dangling_reference", "scenarios/constraints"])
    out = capsys.readouterr().out
    assert rc == 1
    assert '"deterministic_findings": 1' in out
