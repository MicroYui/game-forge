from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from gameforge.bench.external_cases.contracts import content_sha256
from gameforge.bench.external_cases.endless_sky_runner import (
    EndlessSkyCaseRuntime,
    SubmissionVerdict,
    load_case_runtime,
    validate_submitted_tree,
)
from gameforge.bench.external_cases.native import compile_native_parser
from gameforge.bench.external_cases.qualify import load_manifest


ROOT = Path(__file__).resolve().parents[3]
CORPUS = ROOT / "scenarios/external_cases/endless_sky"
MANIFEST = CORPUS / "external-corpus-manifest.json"


@pytest.fixture(scope="module")
def native_binary(tmp_path_factory: pytest.TempPathFactory):
    return compile_native_parser(
        CORPUS / "native/endless_sky_data_parser.cpp",
        tmp_path_factory.mktemp("external-case-native"),
    )


def test_all_external_cases_reconstruct_manifest_bound_runtime() -> None:
    manifest = load_manifest(MANIFEST)

    for case in manifest.cases:
        runtime = load_case_runtime(CORPUS, case.spec)

        assert isinstance(runtime, EndlessSkyCaseRuntime)
        assert runtime.spec == case.spec
        assert runtime.before_tree == case.before_tree
        assert runtime.human_target_tree == case.after_tree
        assert tuple(runtime.before_raw) == case.spec.changed_paths
        assert tuple(runtime.human_target_raw) == case.spec.changed_paths
        assert runtime.before_snapshot.snapshot_id != (
            runtime.human_target_snapshot.snapshot_id
        )
        assert runtime.target_entity_ids == case.target_entity_ids
        assert runtime.protected_entity_ids
        assert set(runtime.protected_entity_ids) <= set(runtime.target_entity_ids)
        assert runtime.target_finding.defect_class == case.spec.defect_class.value
        assert runtime.target_finding.status == "confirmed"
        assert set(runtime.target_finding.entities) & set(case.target_entity_ids)

        finding_sha = content_sha256(
            runtime.target_finding.model_dump(mode="json")
        )
        assert finding_sha in {
            finding.evidence_sha256 for finding in case.findings_before
        }


def test_runtime_source_trees_still_round_trip_through_adapter() -> None:
    manifest = load_manifest(MANIFEST)

    for case in manifest.cases:
        runtime = load_case_runtime(CORPUS, case.spec)

        assert runtime.adapter.from_ir(runtime.before_snapshot) == runtime.before_raw
        assert (
            runtime.adapter.from_ir(runtime.human_target_snapshot)
            == runtime.human_target_raw
        )


def test_same_submission_oracles_reject_before_and_accept_human_target(
    native_binary,
) -> None:
    manifest = load_manifest(MANIFEST)

    for case in manifest.cases:
        runtime = load_case_runtime(CORPUS, case.spec)
        before = validate_submitted_tree(
            runtime,
            runtime.before_raw,
            native_binary=native_binary,
        )
        after = validate_submitted_tree(
            runtime,
            runtime.human_target_raw,
            native_binary=native_binary,
        )

        assert before.correct is False
        assert before.reader_round_trip is True
        assert before.native_exit_code == 0
        assert before.predicate_status == "violation"
        assert before.target_finding_clear is False

        assert after.correct is True
        assert after.reader_round_trip is True
        assert after.native_exit_code == 0
        assert after.predicate_status == "clear"
        assert after.target_finding_clear is True
        assert after.target_entities_preserved is True
        assert after.new_deterministic_findings == ()
        assert after.submitted_tree_sha256 == case.after_tree.tree_sha256


def test_submission_rejects_missing_or_extra_changed_paths(native_binary) -> None:
    case = load_manifest(MANIFEST).cases[0]
    runtime = load_case_runtime(CORPUS, case.spec)

    missing = validate_submitted_tree(runtime, {}, native_binary=native_binary)
    extra = validate_submitted_tree(
        runtime,
        {**runtime.human_target_raw, "data/not-registered.txt": b"effect extra\n"},
        native_binary=native_binary,
    )

    assert missing.correct is False
    assert isinstance(missing, SubmissionVerdict)
    assert missing.failure_reason == "submission paths differ from changed_paths"
    assert extra.correct is False
    assert extra.failure_reason == "submission paths differ from changed_paths"


def test_module_cli_is_not_preloaded_by_package_import() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-W",
            "error",
            "-m",
            "gameforge.bench.external_cases.endless_sky_runner",
            "--help",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "RuntimeWarning" not in completed.stderr
