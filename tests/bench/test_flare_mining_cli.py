# tests/bench/test_flare_mining_cli.py
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gameforge.bench.flare_evidence import (
    AdjudicationEvidence,
    B0ADecision,
    CandidateLedger,
    DiscoveryLedger,
    canonical_bytes,
    sha256_hex,
)
from gameforge.bench.flare_mining import main


def assert_complete_chain(
    ledger_path,
    decision_path,
    discovered_path,
    evidence_path,
    prior_ledger_path=None,
    prior_decision_path=None,
):
    ledger_bytes = ledger_path.read_bytes()
    decision_bytes = decision_path.read_bytes()
    ledger = CandidateLedger.model_validate_json(ledger_bytes)
    decision = B0ADecision.model_validate_json(decision_bytes)
    assert ledger_bytes == canonical_bytes(ledger)
    assert decision_bytes == canonical_bytes(decision)
    assert ledger.discovery_ledger_sha256 == sha256_hex(discovered_path.read_bytes())
    assert ledger.adjudication_evidence_sha256 == sha256_hex(evidence_path.read_bytes())
    assert decision.candidate_ledger_sha256 == sha256_hex(ledger_bytes)
    assert decision.gate == ledger.gate_summary
    if prior_ledger_path is None:
        assert prior_decision_path is None
        assert ledger.prior_candidate_ledger_sha256 is None
        assert ledger.prior_decision_sha256 is None
    else:
        assert ledger.prior_candidate_ledger_sha256 == sha256_hex(prior_ledger_path.read_bytes())
        assert ledger.prior_decision_sha256 == sha256_hex(prior_decision_path.read_bytes())
    return ledger, decision


def approved_evidence_with_source_artifact(base, artifact_bytes):
    digest = sha256_hex(artifact_bytes)
    payload = base.model_dump(mode="json", exclude={"review_attestation"}, exclude_none=True)
    artifact_id = "flare-issue-source-1"
    payload["source_artifacts"] = [
        {
            "artifact_id": artifact_id,
            "artifact_type": "issue",
            "source_url": "https://github.com/flareteam/flare-game/issues/1",
            "retrieval_date": "2026-07-10",
            "blob_path": f"blobs/{digest}",
            "blob_sha256": digest,
        }
    ]
    payload["group_decisions"][0]["root_cause_evidence_refs"].append(
        {
            "kind": "source_artifact",
            "target_id": artifact_id,
        }
    )
    attestation = base.review_attestation.model_dump(mode="json", exclude_none=True)
    attestation["reviewed_payload_sha256"] = sha256_hex(canonical_bytes(payload))
    payload["review_attestation"] = attestation
    return AdjudicationEvidence.model_validate(payload), digest


def test_discover_then_adjudicate_is_byte_deterministic(
    flare_git_repo, search_spec_path, initial_positive_evidence_path, tmp_path
):
    discovered = tmp_path / "candidate-ledger.discovered.json"
    blobs = tmp_path / "blobs"
    assert (
        main(
            [
                "discover",
                "--repo",
                str(flare_git_repo.path),
                "--search-spec",
                str(search_spec_path),
                "--registration-commit",
                "a" * 40,
                "--registration-path",
                "scenarios/flare_corpus/search-spec.json",
                "--round",
                "initial",
                "--out",
                str(discovered),
                "--blob-dir",
                str(blobs),
            ]
        )
        == 0
    )
    first = discovered.read_bytes()
    discovery_model = DiscoveryLedger.model_validate_json(first)
    assert discovery_model.search_registration.project_commit_oid == "a" * 40
    assert discovery_model.search_registration.repo_relative_path == (
        "scenarios/flare_corpus/search-spec.json"
    )
    assert (
        main(
            [
                "discover",
                "--repo",
                str(flare_git_repo.path),
                "--search-spec",
                str(search_spec_path),
                "--registration-commit",
                "a" * 40,
                "--registration-path",
                "scenarios/flare_corpus/search-spec.json",
                "--round",
                "initial",
                "--out",
                str(discovered),
                "--blob-dir",
                str(blobs),
            ]
        )
        == 0
    )
    assert discovered.read_bytes() == first

    ledger = tmp_path / "candidate-ledger.json"
    decision = tmp_path / "b0a-decision.json"
    assert (
        main(
            [
                "adjudicate",
                "--ledger",
                str(discovered),
                "--evidence",
                str(initial_positive_evidence_path),
                "--blob-dir",
                str(blobs),
                "--out",
                str(ledger),
                "--decision-out",
                str(decision),
            ]
        )
        == 0
    )
    assert b'"status":"provisional_pass"' in decision.read_bytes()
    first_ledger = ledger.read_bytes()
    first_decision = decision.read_bytes()

    second_ledger = tmp_path / "second" / "candidate-ledger.json"
    second_decision = tmp_path / "second" / "b0a-decision.json"
    assert (
        main(
            [
                "adjudicate",
                "--ledger",
                str(discovered),
                "--evidence",
                str(initial_positive_evidence_path),
                "--blob-dir",
                str(blobs),
                "--out",
                str(second_ledger),
                "--decision-out",
                str(second_decision),
            ]
        )
        == 0
    )
    assert second_ledger.read_bytes() == first_ledger
    assert second_decision.read_bytes() == first_decision


@pytest.mark.parametrize("round_name", ["initial", "expanded"])
def test_valid_negative_gate_writes_complete_canonical_outputs_and_uses_exit_three(
    round_name, request, blob_dir, tmp_path
):
    discovered = request.getfixturevalue(f"{round_name}_discovered_path")
    evidence = request.getfixturevalue(f"{round_name}_insufficient_evidence_path")
    ledger_path = tmp_path / round_name / "ledger.json"
    decision_path = tmp_path / round_name / "decision.json"
    args = [
        "adjudicate",
        "--ledger",
        str(discovered),
        "--evidence",
        str(evidence),
        "--blob-dir",
        str(blob_dir),
        "--out",
        str(ledger_path),
        "--decision-out",
        str(decision_path),
    ]
    prior_ledger_path = prior_decision_path = None
    if round_name == "expanded":
        prior_ledger_path = request.getfixturevalue("initial_ledger_path")
        prior_decision_path = request.getfixturevalue("initial_decision_path")
        args[5:5] = [
            "--prior-ledger",
            str(prior_ledger_path),
            "--prior-decision",
            str(prior_decision_path),
        ]

    assert main(args) == 3
    ledger, decision = assert_complete_chain(
        ledger_path,
        decision_path,
        discovered,
        evidence,
        prior_ledger_path,
        prior_decision_path,
    )
    expected = "expanded_round_required" if round_name == "initial" else "insufficient_evidence"
    expected_action = (
        "run_expanded_round" if round_name == "initial" else "stop_flare_heavy_investment"
    )
    assert decision.gate.status == expected
    assert decision.gate.next_action == expected_action


def test_expanded_exit_three_consumes_the_cli_published_initial_pair(
    initial_discovered_path,
    initial_insufficient_evidence_path,
    expanded_discovered_path,
    expanded_insufficient_evidence_path,
    blob_dir,
    tmp_path,
):
    initial_ledger = tmp_path / "published-initial" / "ledger.json"
    initial_decision = tmp_path / "published-initial" / "decision.json"
    assert (
        main(
            [
                "adjudicate",
                "--ledger",
                str(initial_discovered_path),
                "--evidence",
                str(initial_insufficient_evidence_path),
                "--blob-dir",
                str(blob_dir),
                "--out",
                str(initial_ledger),
                "--decision-out",
                str(initial_decision),
            ]
        )
        == 3
    )

    expanded_ledger = tmp_path / "published-expanded" / "ledger.json"
    expanded_decision = tmp_path / "published-expanded" / "decision.json"
    assert (
        main(
            [
                "adjudicate",
                "--ledger",
                str(expanded_discovered_path),
                "--evidence",
                str(expanded_insufficient_evidence_path),
                "--prior-ledger",
                str(initial_ledger),
                "--prior-decision",
                str(initial_decision),
                "--blob-dir",
                str(blob_dir),
                "--out",
                str(expanded_ledger),
                "--decision-out",
                str(expanded_decision),
            ]
        )
        == 3
    )
    initial_model, initial_marker = assert_complete_chain(
        initial_ledger,
        initial_decision,
        initial_discovered_path,
        initial_insufficient_evidence_path,
    )
    assert initial_marker.gate.status == "expanded_round_required"
    assert initial_model.gate_summary == initial_marker.gate
    expanded_model, expanded_marker = assert_complete_chain(
        expanded_ledger,
        expanded_decision,
        expanded_discovered_path,
        expanded_insufficient_evidence_path,
        initial_ledger,
        initial_decision,
    )
    assert expanded_marker.gate.status == "insufficient_evidence"
    assert expanded_model.gate_summary == expanded_marker.gate


def test_expanded_requires_both_prior_files_and_initial_rejects_them(
    expanded_discovered_path,
    expanded_evidence_path,
    initial_discovered_path,
    initial_positive_evidence_path,
    initial_ledger_path,
    initial_decision_path,
    blob_dir,
    tmp_path,
):
    common_out = [
        "--blob-dir",
        str(blob_dir),
        "--out",
        str(tmp_path / "ledger.json"),
        "--decision-out",
        str(tmp_path / "decision.json"),
    ]
    assert (
        main(
            [
                "adjudicate",
                "--ledger",
                str(expanded_discovered_path),
                "--evidence",
                str(expanded_evidence_path),
                *common_out,
            ]
        )
        == 1
    )
    assert not (tmp_path / "ledger.json").exists()
    assert not (tmp_path / "decision.json").exists()
    assert (
        main(
            [
                "adjudicate",
                "--ledger",
                str(initial_discovered_path),
                "--evidence",
                str(initial_positive_evidence_path),
                "--prior-ledger",
                str(initial_ledger_path),
                "--prior-decision",
                str(initial_decision_path),
                *common_out,
            ]
        )
        == 1
    )
    assert not (tmp_path / "ledger.json").exists()
    assert not (tmp_path / "decision.json").exists()


@pytest.mark.parametrize(
    ("lone_flag", "fixture_name"),
    [
        ("--prior-ledger", "initial_ledger_path"),
        ("--prior-decision", "initial_decision_path"),
    ],
)
def test_lone_prior_flag_is_an_argparse_syntax_error(
    lone_flag,
    fixture_name,
    request,
    expanded_discovered_path,
    expanded_evidence_path,
    blob_dir,
    tmp_path,
):
    out = tmp_path / "ledger.json"
    decision = tmp_path / "decision.json"
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "adjudicate",
                "--ledger",
                str(expanded_discovered_path),
                "--evidence",
                str(expanded_evidence_path),
                lone_flag,
                str(request.getfixturevalue(fixture_name)),
                "--blob-dir",
                str(blob_dir),
                "--out",
                str(out),
                "--decision-out",
                str(decision),
            ]
        )
    assert exc.value.code == 2
    assert not out.exists() and not decision.exists()


def test_adjudicate_preflights_both_outputs_before_writing(
    initial_discovered_path, initial_positive_evidence_path, blob_dir, tmp_path
):
    ledger = tmp_path / "candidate-ledger.json"
    decision = tmp_path / "b0a-decision.json"
    decision.write_bytes(b"conflicting-existing-decision\n")
    assert (
        main(
            [
                "adjudicate",
                "--ledger",
                str(initial_discovered_path),
                "--evidence",
                str(initial_positive_evidence_path),
                "--blob-dir",
                str(blob_dir),
                "--out",
                str(ledger),
                "--decision-out",
                str(decision),
            ]
        )
        == 1
    )
    assert not ledger.exists()
    assert decision.read_bytes() == b"conflicting-existing-decision\n"


def test_adjudicate_writes_decision_last_and_rolls_back_on_marker_failure(
    initial_discovered_path, initial_positive_evidence_path, blob_dir, tmp_path, monkeypatch
):
    ledger = tmp_path / "candidate-ledger.json"
    decision = tmp_path / "b0a-decision.json"
    real_open = Path.open
    exclusive_attempts = []

    def fail_completion_marker(path, mode="r", *args, **kwargs):
        if mode == "xb":
            exclusive_attempts.append(path)
            if path == decision:
                raise OSError("injected decision-marker failure")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_completion_marker)
    assert (
        main(
            [
                "adjudicate",
                "--ledger",
                str(initial_discovered_path),
                "--evidence",
                str(initial_positive_evidence_path),
                "--blob-dir",
                str(blob_dir),
                "--out",
                str(ledger),
                "--decision-out",
                str(decision),
            ]
        )
        == 1
    )
    assert exclusive_attempts == [ledger, decision]
    assert not ledger.exists() and not decision.exists()


def test_adjudicate_rejects_same_or_aliased_output_paths_before_writing(
    initial_discovered_path, initial_positive_evidence_path, blob_dir, tmp_path, capsys
):
    exact = tmp_path / "same.json"
    assert (
        main(
            [
                "adjudicate",
                "--ledger",
                str(initial_discovered_path),
                "--evidence",
                str(initial_positive_evidence_path),
                "--blob-dir",
                str(blob_dir),
                "--out",
                str(exact),
                "--decision-out",
                str(exact),
            ]
        )
        == 1
    )
    assert not exact.exists()
    assert "output paths" in capsys.readouterr().err

    target = tmp_path / "existing.json"
    alias = tmp_path / "alias.json"
    target.write_bytes(b"preexisting\n")
    os.link(target, alias)
    assert (
        main(
            [
                "adjudicate",
                "--ledger",
                str(initial_discovered_path),
                "--evidence",
                str(initial_positive_evidence_path),
                "--blob-dir",
                str(blob_dir),
                "--out",
                str(target),
                "--decision-out",
                str(alias),
            ]
        )
        == 1
    )
    assert target.read_bytes() == alias.read_bytes() == b"preexisting\n"
    assert "alias" in capsys.readouterr().err


def test_adjudicate_rejects_normalized_resolved_output_alias_before_writing(
    initial_discovered_path,
    initial_positive_evidence_path,
    blob_dir,
    tmp_path,
    monkeypatch,
    capsys,
):
    out = tmp_path / "normalized" / "result.json"
    decision = out.parent / "child" / ".." / out.name
    out.parent.mkdir(parents=True)
    real_open = Path.open
    exclusive_attempts = []

    def record_exclusive_open(path, mode="r", *args, **kwargs):
        if mode == "xb":
            exclusive_attempts.append(path)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", record_exclusive_open)
    assert (
        main(
            [
                "adjudicate",
                "--ledger",
                str(initial_discovered_path),
                "--evidence",
                str(initial_positive_evidence_path),
                "--blob-dir",
                str(blob_dir),
                "--out",
                str(out),
                "--decision-out",
                str(decision),
            ]
        )
        == 1
    )
    assert exclusive_attempts == []
    assert not out.exists()
    assert "output paths" in capsys.readouterr().err


def test_discover_rejects_noncanonical_search_spec_without_output(
    flare_git_repo, search_spec_path, tmp_path
):
    changed = tmp_path / "noncanonical-search-spec.json"
    changed.write_bytes(b" \n" + search_spec_path.read_bytes())
    out = tmp_path / "discovered.json"
    blobs = tmp_path / "blobs"
    assert (
        main(
            [
                "discover",
                "--repo",
                str(flare_git_repo.path),
                "--search-spec",
                str(changed),
                "--registration-commit",
                "a" * 40,
                "--registration-path",
                "scenarios/flare_corpus/search-spec.json",
                "--round",
                "initial",
                "--out",
                str(out),
                "--blob-dir",
                str(blobs),
            ]
        )
        == 1
    )
    assert not out.exists() and not blobs.exists()


@pytest.mark.parametrize(
    ("input_flag", "fixture_name"),
    [
        ("--ledger", "expanded_discovered_path"),
        ("--evidence", "expanded_evidence_path"),
        ("--prior-ledger", "initial_ledger_path"),
        ("--prior-decision", "initial_decision_path"),
    ],
)
def test_adjudicate_rejects_noncanonical_json_without_outputs(
    input_flag,
    fixture_name,
    request,
    expanded_discovered_path,
    expanded_evidence_path,
    initial_ledger_path,
    initial_decision_path,
    blob_dir,
    tmp_path,
):
    inputs = {
        "--ledger": expanded_discovered_path,
        "--evidence": expanded_evidence_path,
        "--prior-ledger": initial_ledger_path,
        "--prior-decision": initial_decision_path,
    }
    source = request.getfixturevalue(fixture_name)
    changed = tmp_path / f"noncanonical-{input_flag[2:]}.json"
    changed.write_bytes(b" \n" + source.read_bytes())
    inputs[input_flag] = changed
    out = tmp_path / "ledger.json"
    decision = tmp_path / "decision.json"
    assert (
        main(
            [
                "adjudicate",
                "--ledger",
                str(inputs["--ledger"]),
                "--evidence",
                str(inputs["--evidence"]),
                "--prior-ledger",
                str(inputs["--prior-ledger"]),
                "--prior-decision",
                str(inputs["--prior-decision"]),
                "--blob-dir",
                str(blob_dir),
                "--out",
                str(out),
                "--decision-out",
                str(decision),
            ]
        )
        == 1
    )
    assert not out.exists() and not decision.exists()


def test_adjudicate_rejects_invalid_utf8_without_outputs(
    initial_positive_evidence_path, blob_dir, tmp_path
):
    invalid = tmp_path / "invalid-utf8-ledger.json"
    invalid.write_bytes(b"\xff")
    out = tmp_path / "ledger.json"
    decision = tmp_path / "decision.json"
    assert (
        main(
            [
                "adjudicate",
                "--ledger",
                str(invalid),
                "--evidence",
                str(initial_positive_evidence_path),
                "--blob-dir",
                str(blob_dir),
                "--out",
                str(out),
                "--decision-out",
                str(decision),
            ]
        )
        == 1
    )
    assert not out.exists() and not decision.exists()


@pytest.mark.parametrize("blob_state", ["missing", "tampered"])
def test_adjudicate_rejects_missing_or_tampered_patch_cas_without_outputs(
    blob_state, initial_discovered_path, initial_positive_evidence_path, blob_dir, tmp_path
):
    replay_blobs = tmp_path / "replay-blobs"
    shutil.copytree(blob_dir, replay_blobs)
    discovered = DiscoveryLedger.model_validate_json(
        initial_discovered_path.read_text(encoding="utf-8")
    )
    digest = discovered.discovered_candidates[0].diff_evidence.patch_sha256
    if blob_state == "missing":
        (replay_blobs / digest).unlink()
    else:
        (replay_blobs / digest).write_bytes(b"tampered patch bytes")
    out = tmp_path / "ledger.json"
    decision = tmp_path / "decision.json"
    assert (
        main(
            [
                "adjudicate",
                "--ledger",
                str(initial_discovered_path),
                "--evidence",
                str(initial_positive_evidence_path),
                "--blob-dir",
                str(replay_blobs),
                "--out",
                str(out),
                "--decision-out",
                str(decision),
            ]
        )
        == 1
    )
    assert not out.exists() and not decision.exists()


@pytest.mark.parametrize("blob_state", ["present", "missing", "tampered"])
def test_adjudicate_replays_evidence_artifact_cas_at_digest_root(
    blob_state, positive_evidence, initial_discovered_path, blob_dir, tmp_path
):
    replay_blobs = tmp_path / "artifact-blobs"
    shutil.copytree(blob_dir, replay_blobs)
    artifact_bytes = b'{"issue":1,"state":"closed"}\n'
    evidence, digest = approved_evidence_with_source_artifact(positive_evidence, artifact_bytes)
    evidence_path = tmp_path / "artifact-evidence.json"
    evidence_path.write_bytes(canonical_bytes(evidence))
    if blob_state == "present":
        (replay_blobs / digest).write_bytes(artifact_bytes)
    elif blob_state == "tampered":
        (replay_blobs / digest).write_bytes(b"tampered artifact bytes")

    out = tmp_path / "ledger.json"
    decision = tmp_path / "decision.json"
    result = main(
        [
            "adjudicate",
            "--ledger",
            str(initial_discovered_path),
            "--evidence",
            str(evidence_path),
            "--blob-dir",
            str(replay_blobs),
            "--out",
            str(out),
            "--decision-out",
            str(decision),
        ]
    )
    assert result == (0 if blob_state == "present" else 1)
    assert out.exists() == decision.exists() == (blob_state == "present")


def test_discover_rejects_immutable_output_conflict(flare_git_repo, search_spec_path, tmp_path):
    out = tmp_path / "discovered.json"
    out.write_bytes(b"conflicting-existing-ledger\n")
    assert (
        main(
            [
                "discover",
                "--repo",
                str(flare_git_repo.path),
                "--search-spec",
                str(search_spec_path),
                "--registration-commit",
                "a" * 40,
                "--registration-path",
                "scenarios/flare_corpus/search-spec.json",
                "--round",
                "initial",
                "--out",
                str(out),
                "--blob-dir",
                str(tmp_path / "blobs"),
            ]
        )
        == 1
    )
    assert out.read_bytes() == b"conflicting-existing-ledger\n"


def test_discover_git_failure_is_one_stderr_line_without_traceback(
    search_spec_path, tmp_path, capsys
):
    out = tmp_path / "discovered.json"
    blobs = tmp_path / "blobs"
    assert (
        main(
            [
                "discover",
                "--repo",
                str(tmp_path / "missing-repo"),
                "--search-spec",
                str(search_spec_path),
                "--registration-commit",
                "a" * 40,
                "--registration-path",
                "scenarios/flare_corpus/search-spec.json",
                "--round",
                "initial",
                "--out",
                str(out),
                "--blob-dir",
                str(blobs),
            ]
        )
        == 1
    )
    stderr = capsys.readouterr().err
    assert len(stderr.splitlines()) == 1
    assert "Traceback" not in stderr
    assert not out.exists() and not blobs.exists()


def test_domain_failure_is_one_stderr_line_without_traceback(
    initial_positive_evidence_path, blob_dir, tmp_path, capsys
):
    out = tmp_path / "ledger.json"
    decision = tmp_path / "decision.json"
    assert (
        main(
            [
                "adjudicate",
                "--ledger",
                str(tmp_path / "missing-ledger.json"),
                "--evidence",
                str(initial_positive_evidence_path),
                "--blob-dir",
                str(blob_dir),
                "--out",
                str(out),
                "--decision-out",
                str(decision),
            ]
        )
        == 1
    )
    stderr = capsys.readouterr().err
    assert len(stderr.splitlines()) == 1
    assert "Traceback" not in stderr
    assert not out.exists() and not decision.exists()


def test_module_entrypoint_distinguishes_gate_outcome_from_syntax_error(
    initial_discovered_path, initial_insufficient_evidence_path, blob_dir, tmp_path
):
    ledger = tmp_path / "ledger.json"
    decision = tmp_path / "decision.json"
    command = [
        sys.executable,
        "-m",
        "gameforge.bench.flare_mining",
        "adjudicate",
        "--ledger",
        str(initial_discovered_path),
        "--evidence",
        str(initial_insufficient_evidence_path),
        "--blob-dir",
        str(blob_dir),
        "--out",
        str(ledger),
        "--decision-out",
        str(decision),
    ]
    assert subprocess.run(command, check=False).returncode == 3
    _, marker = assert_complete_chain(
        ledger,
        decision,
        initial_discovered_path,
        initial_insufficient_evidence_path,
    )
    assert marker.gate.status == "expanded_round_required"
    assert (
        subprocess.run(
            [sys.executable, "-m", "gameforge.bench.flare_mining", "probe"], check=False
        ).returncode
        == 2
    )


def test_cli_has_no_probe_or_freeze_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["probe"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
