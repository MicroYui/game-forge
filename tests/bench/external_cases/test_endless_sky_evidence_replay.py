from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from gameforge.bench.external_cases.contracts import content_sha256
from gameforge.bench.external_cases.endless_sky_runner import replay_corpus
from gameforge.bench.external_cases.qualify import load_manifest


ROOT = Path(__file__).resolve().parents[3]
CORPUS = ROOT / "scenarios/external_cases/endless_sky"
COMMITTED_MANIFEST = CORPUS / "external-corpus-manifest.json"
FOUR_CLASSES = {
    "cyclic_dependency",
    "dangling_reference",
    "dead_quest",
    "unreachable_target",
}


@pytest.fixture(scope="module")
def replayed(tmp_path_factory):
    if shutil.which("c++") is None:
        pytest.skip("a C++17 compiler is required for external evidence replay")
    root = tmp_path_factory.mktemp("external-evidence-replay")
    first = replay_corpus(CORPUS, root / "one")
    second = replay_corpus(CORPUS, root / "two")
    return first, second, root


def test_evidence_replay_is_byte_identical(replayed) -> None:
    first, second, _ = replayed
    expected = COMMITTED_MANIFEST.read_bytes()

    assert first == second == expected


def test_replayed_manifest_qualifies_all_frozen_cases(replayed) -> None:
    first, _, root = replayed
    manifest_path = root / "one/external-corpus-manifest.json"
    manifest = load_manifest(manifest_path)

    assert manifest_path.read_bytes() == first
    assert len(manifest.cases) == 8
    assert sum(case.spec.split == "verification" for case in manifest.cases) == 4
    assert {case.spec.defect_class.value for case in manifest.cases} == FOUR_CLASSES
    assert all(case.qualification_status == "qualified" for case in manifest.cases)
    assert all(case.failure_reasons == () for case in manifest.cases)
    assert all(case.target_entity_ids for case in manifest.cases)
    assert all(case.predicate_before.status == "violation" for case in manifest.cases)
    assert all(case.predicate_after.status == "clear" for case in manifest.cases)
    assert all(case.findings_before for case in manifest.cases)
    assert all(not case.findings_after for case in manifest.cases)

    assert [
        (metric.defect_class.value, metric.n, metric.k)
        for metric in manifest.verification
    ] == [
        (name, 1, 1) for name in sorted(FOUR_CLASSES)
    ]
    assert [
        (metric.defect_class.value, metric.n, metric.k)
        for metric in manifest.development
    ] == [
        (name, 1, 1) for name in sorted(FOUR_CLASSES)
    ]
    assert manifest.after_oracle_fp.n == 8
    assert manifest.after_oracle_fp.count == 0
    assert manifest.after_oracle_fp.rate == 0.0


def test_replayed_rows_bind_native_findings_and_human_targets(replayed) -> None:
    _, _, root = replayed
    manifest = load_manifest(root / "one/external-corpus-manifest.json")

    for case in manifest.cases:
        expected_class = case.spec.defect_class.value
        targets = set(case.target_entity_ids)
        assert case.native_before.exit_code == 0
        assert case.native_after.exit_code == 0
        assert all(not part.startswith("/") for part in case.native_before.command)
        assert all(not part.startswith("/") for part in case.native_after.command)
        assert any(
            finding.status == "confirmed"
            and finding.defect_class == expected_class
            and targets.intersection(finding.entities)
            for finding in case.findings_before
        )
        assert case.human_target.patch_path == (
            f"cases/{case.spec.case_id}/upstream.patch"
        )
        assert case.agent_patch_sha256 is None
        assert case.agent_target_snapshot_id is None
        assert case.evidence_sha256 == content_sha256(
            case,
            exclude={"evidence_sha256"},
        )

    assert manifest.manifest_sha256 == content_sha256(
        manifest,
        exclude={"manifest_sha256"},
    )
