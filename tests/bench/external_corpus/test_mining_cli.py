from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import gameforge.bench.external_corpus.mining as mining_module
from gameforge.bench.external_corpus.contracts import (
    DiscoveryLedger,
    SourceProfile,
    canonical_bytes,
)
from gameforge.bench.external_corpus.mining import main
from gameforge.bench.external_corpus.profiles import SourceProfileBinding
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


def _registered_profile_repo(
    root: Path,
    profile: SourceProfile,
) -> tuple[Path, Path, str, str]:
    project_root = root / "project"
    project_root.mkdir()
    subprocess.run(["git", "init", "-q", str(project_root)], check=True)
    subprocess.run(
        ["git", "-C", str(project_root), "config", "user.email", "fixture@example.test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(project_root), "config", "user.name", "Fixture"],
        check=True,
    )
    relative_path = "scenarios/external_corpus/fixture_sky/source-profile.json"
    profile_path = project_root / relative_path
    profile_path.parent.mkdir(parents=True)
    profile_path.write_bytes(canonical_bytes(profile))
    subprocess.run(["git", "-C", str(project_root), "add", relative_path], check=True)
    subprocess.run(
        ["git", "-C", str(project_root), "commit", "-q", "-m", "register profile"],
        check=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(project_root), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    return project_root, profile_path, relative_path, commit


def _fixture_binding(source_id: str) -> SourceProfileBinding:
    assert source_id == "fixture_sky"
    return SourceProfileBinding(
        source_id=source_id,
        profile_model=SourceProfile,
        validate_source_profile=lambda profile: SourceProfile.model_validate(
            profile.model_dump(mode="json")
        ),
    )


def test_review_package_cli_never_adds_approval_fields(tmp_path):
    _discovery, ledger_path, _evidence_path, _blob_dir = _write_inputs(tmp_path)
    output = tmp_path / "review-package.json"

    assert main(["review-package", "--ledger", str(ledger_path), "--out", str(output)]) == 0

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["review_status"] == "awaiting_human"
    assert "review_attestation" not in payload
    assert all("disposition" not in row for row in payload["rows"])


def test_discover_cli_runs_the_registered_profile_without_source_subcommands(
    tmp_path, capsys, monkeypatch
):
    upstream = build_generic_git_repo(tmp_path / "upstream")
    profile = _sky_profile(upstream)
    project_root, profile_path, registration_path, registration_commit = (
        _registered_profile_repo(tmp_path, profile)
    )
    monkeypatch.setattr(mining_module, "PROJECT_ROOT", project_root, raising=False)
    monkeypatch.setattr(
        mining_module,
        "get_profile_binding",
        _fixture_binding,
        raising=False,
    )
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
                registration_commit,
                "--registration-path",
                registration_path,
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
    assert ledger.discovery_tool.project_commit_oid == registration_commit
    assert len(ledger.discovered_candidates) == 3
    assert (
        capsys.readouterr().err
        == "discovery complete: selected=3 matched=9 config_only=7\n"
    )


def test_discover_cli_rejects_tracked_profile_changed_after_registration(
    tmp_path, capsys, monkeypatch
):
    upstream = build_generic_git_repo(tmp_path / "upstream")
    profile = _sky_profile(upstream)
    project_root, profile_path, registration_path, registration_commit = (
        _registered_profile_repo(tmp_path, profile)
    )
    changed = profile.model_copy(update={"profile_version": "fixture_sky@2"})
    profile_path.write_bytes(canonical_bytes(changed))
    monkeypatch.setattr(mining_module, "PROJECT_ROOT", project_root, raising=False)
    monkeypatch.setattr(
        mining_module,
        "get_profile_binding",
        _fixture_binding,
        raising=False,
    )

    result = main(
        [
            "discover",
            "--repo",
            str(upstream.path),
            "--profile",
            str(profile_path),
            "--registration-commit",
            registration_commit,
            "--registration-path",
            registration_path,
            "--out",
            str(tmp_path / "discovery.json"),
            "--blob-dir",
            str(tmp_path / "blobs"),
        ]
    )

    assert result == 1
    assert "tracked worktree" in capsys.readouterr().err


def test_discover_cli_rejects_profile_path_other_than_registered_path(
    tmp_path, capsys, monkeypatch
):
    upstream = build_generic_git_repo(tmp_path / "upstream")
    profile = _sky_profile(upstream)
    project_root, profile_path, registration_path, registration_commit = (
        _registered_profile_repo(tmp_path, profile)
    )
    duplicate_path = project_root / "duplicate-profile.json"
    duplicate_path.write_bytes(profile_path.read_bytes())
    monkeypatch.setattr(mining_module, "PROJECT_ROOT", project_root, raising=False)
    monkeypatch.setattr(
        mining_module,
        "get_profile_binding",
        _fixture_binding,
        raising=False,
    )

    result = main(
        [
            "discover",
            "--repo",
            str(upstream.path),
            "--profile",
            str(duplicate_path),
            "--registration-commit",
            registration_commit,
            "--registration-path",
            registration_path,
            "--out",
            str(tmp_path / "discovery.json"),
            "--blob-dir",
            str(tmp_path / "blobs"),
        ]
    )

    assert result == 1
    assert "registered profile path" in capsys.readouterr().err


def test_discover_cli_rejects_registration_commit_other_than_current_project_head(
    tmp_path, capsys, monkeypatch
):
    upstream = build_generic_git_repo(tmp_path / "upstream")
    profile = _sky_profile(upstream)
    project_root, profile_path, registration_path, registration_commit = (
        _registered_profile_repo(tmp_path, profile)
    )
    marker = project_root / "marker.txt"
    marker.write_text("later\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(project_root), "add", "marker.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(project_root), "commit", "-q", "-m", "later commit"],
        check=True,
    )
    monkeypatch.setattr(mining_module, "PROJECT_ROOT", project_root, raising=False)
    monkeypatch.setattr(
        mining_module,
        "get_profile_binding",
        _fixture_binding,
        raising=False,
    )

    result = main(
        [
            "discover",
            "--repo",
            str(upstream.path),
            "--profile",
            str(profile_path),
            "--registration-commit",
            registration_commit,
            "--registration-path",
            registration_path,
            "--out",
            str(tmp_path / "discovery.json"),
            "--blob-dir",
            str(tmp_path / "blobs"),
        ]
    )

    assert result == 1
    assert "current project HEAD" in capsys.readouterr().err


def test_discover_cli_rejects_source_without_a_static_profile_binding(
    tmp_path, capsys, monkeypatch
):
    upstream = build_generic_git_repo(tmp_path / "upstream")
    profile = _sky_profile(upstream)
    project_root, profile_path, registration_path, registration_commit = (
        _registered_profile_repo(tmp_path, profile)
    )
    monkeypatch.setattr(mining_module, "PROJECT_ROOT", project_root, raising=False)

    result = main(
        [
            "discover",
            "--repo",
            str(upstream.path),
            "--profile",
            str(profile_path),
            "--registration-commit",
            registration_commit,
            "--registration-path",
            registration_path,
            "--out",
            str(tmp_path / "discovery.json"),
            "--blob-dir",
            str(tmp_path / "blobs"),
        ]
    )

    assert result == 1
    assert "unknown external source profile" in capsys.readouterr().err


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


def test_adjudicate_cli_rejects_eligible_patch_cas_tamper(tmp_path, capsys):
    discovery, ledger_path, evidence_path, blob_dir = _write_inputs(tmp_path)
    digest = next(
        candidate.diff_evidence.eligible_patch_sha256
        for candidate in discovery.discovered_candidates
        if candidate.diff_evidence.eligible_patch_sha256 is not None
    )
    assert digest is not None
    (blob_dir / digest).write_bytes(b"tampered eligible patch\n")

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
    assert "eligible patch CAS" in error


def test_adjudicate_cli_replays_registered_direct_match_rules(tmp_path, capsys):
    discovery = discovery_ledger()
    payload = discovery.model_dump(mode="json")
    candidate = next(
        item
        for item in payload["discovered_candidates"]
        if item["diff_evidence"]["eligible_patch_sha256"] is not None
    )
    direct = next(
        reason for reason in candidate["selection_reasons"] if reason["kind"] == "direct_match"
    )
    assert direct["rule_ids"] == ["diff.eligible_marker", "message.fix"]
    direct["rule_ids"] = ["diff.eligible_marker"]
    forged = DiscoveryLedger.model_validate(payload)
    evidence = reviewed_evidence(forged)
    ledger_path = tmp_path / "discovery.json"
    evidence_path = tmp_path / "evidence.json"
    blob_dir = tmp_path / "blobs"
    ledger_path.write_bytes(canonical_bytes(forged))
    evidence_path.write_bytes(canonical_bytes(evidence))
    write_cas(forged, blob_dir)

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
    assert "direct-match replay" in capsys.readouterr().err


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
