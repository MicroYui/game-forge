from __future__ import annotations

import json

import pytest

from gameforge.bench.external_corpus.contracts import DiscoveryLedger, canonical_bytes
from gameforge.bench.external_corpus.mining import main
from tests.bench.external_corpus.adjudication_fixture import (
    discovery_ledger,
    reviewed_evidence,
    write_cas,
)
from tests.bench.external_corpus.git_fixture import build_generic_git_repo
from tests.bench.external_corpus.test_discovery import _sky_profile


def _write_inputs(tmp_path, *, groups: int = 8):
    discovery = discovery_ledger()
    evidence = reviewed_evidence(discovery, group_count=groups)
    ledger_path = tmp_path / "discovery.json"
    evidence_path = tmp_path / "evidence.json"
    blob_dir = tmp_path / "blobs"
    ledger_path.write_bytes(canonical_bytes(discovery))
    evidence_path.write_bytes(canonical_bytes(evidence))
    write_cas(discovery, blob_dir)
    return discovery, ledger_path, evidence_path, blob_dir


def test_review_package_cli_never_adds_approval_fields(tmp_path):
    _discovery, ledger_path, _evidence_path, _blob_dir = _write_inputs(tmp_path)
    output = tmp_path / "review-package.json"

    assert main(["review-package", "--ledger", str(ledger_path), "--out", str(output)]) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["review_status"] == "awaiting_human"
    assert "review_attestation" not in payload
    assert all("disposition" not in row for row in payload["rows"])


def test_discover_cli_runs_the_registered_profile_without_source_subcommands(tmp_path):
    upstream = build_generic_git_repo(tmp_path / "upstream")
    profile = _sky_profile(upstream)
    profile_path = tmp_path / "profile.json"
    profile_path.write_bytes(canonical_bytes(profile))
    output = tmp_path / "discovery.json"
    blob_dir = tmp_path / "discovery-blobs"

    assert (
        main(
            [
                "discover",
                "--repo",
                str(upstream.path),
                "--profile",
                str(profile_path),
                "--registration-commit",
                "1" * 40,
                "--registration-path",
                "scenarios/external_corpus/fixture_sky/profile.json",
                "--out",
                str(output),
                "--blob-dir",
                str(blob_dir),
            ]
        )
        == 0
    )
    ledger = DiscoveryLedger.model_validate_json(output.read_bytes())
    assert ledger.source_id == "fixture_sky"
    assert len(ledger.discovered_candidates) == 3


@pytest.mark.parametrize(("groups", "exit_code"), [(8, 0), (7, 3)])
def test_adjudicate_cli_uses_stable_gate_exit_codes(tmp_path, groups, exit_code):
    _discovery, ledger_path, evidence_path, blob_dir = _write_inputs(tmp_path, groups=groups)
    output = tmp_path / "candidate-ledger.json"
    decision = tmp_path / "decision.json"

    assert (
        main(
            [
                "adjudicate",
                "--ledger",
                str(ledger_path),
                "--evidence",
                str(evidence_path),
                "--blob-dir",
                str(blob_dir),
                "--out",
                str(output),
                "--decision-out",
                str(decision),
            ]
        )
        == exit_code
    )
    assert output.exists()
    assert decision.exists()


def test_adjudicate_cli_rejects_cas_tamper_with_one_line_error(tmp_path, capsys):
    discovery, ledger_path, evidence_path, blob_dir = _write_inputs(tmp_path)
    digest = discovery.discovered_candidates[0].diff_evidence.patch_sha256
    (blob_dir / digest).write_bytes(b"tampered\n")

    result = main(
        [
            "adjudicate",
            "--ledger",
            str(ledger_path),
            "--evidence",
            str(evidence_path),
            "--blob-dir",
            str(blob_dir),
            "--out",
            str(tmp_path / "candidate-ledger.json"),
            "--decision-out",
            str(tmp_path / "decision.json"),
        ]
    )

    assert result == 1
    error = capsys.readouterr().err
    assert len(error.splitlines()) == 1
    assert "CAS" in error


def test_cli_rejects_noncanonical_input_and_immutable_output_conflict(tmp_path):
    discovery, ledger_path, _evidence_path, _blob_dir = _write_inputs(tmp_path)
    ledger_path.write_text(
        json.dumps(discovery.model_dump(mode="json"), indent=2), encoding="utf-8"
    )
    output = tmp_path / "review-package.json"
    assert main(["review-package", "--ledger", str(ledger_path), "--out", str(output)]) == 1
    assert not output.exists()

    ledger_path.write_bytes(canonical_bytes(discovery))
    output.write_bytes(b"conflict\n")
    assert main(["review-package", "--ledger", str(ledger_path), "--out", str(output)]) == 1


def test_argparse_failure_uses_exit_code_two():
    with pytest.raises(SystemExit) as exc_info:
        main(["adjudicate"])
    assert exc_info.value.code == 2
