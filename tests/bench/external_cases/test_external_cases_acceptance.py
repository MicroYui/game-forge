from __future__ import annotations

import ast
import hashlib
import subprocess
from pathlib import Path

from gameforge.bench.external_cases.contracts import content_sha256
from gameforge.bench.external_cases.endless_sky_fixture import load_case_specs
from gameforge.bench.external_cases.endless_sky_runner import (
    EXPECTED_MAPPING_SPEC_SHA256,
)
from gameforge.bench.external_cases.qualify import load_manifest
from gameforge.bench.external_cases.tree import tree_artifact
from gameforge.contracts.canonical import canonical_json
from gameforge.spine.ingestion.endless_sky_reader import (
    count_nodes,
    count_tokens,
    parse_data_file,
    read_source_tree,
    render_source_tree,
)


ROOT = Path(__file__).resolve().parents[3]
CORPUS = ROOT / "scenarios/external_cases/endless_sky"
MANIFEST = CORPUS / "external-corpus-manifest.json"
NATIVE_SOURCE = CORPUS / "native/endless_sky_data_parser.cpp"
FLARE_FREEZE_COMMIT = "755fe2e"
LEGACY_CORPUS = ROOT / "scenarios/external_corpus/endless_sky"
FOUR_CLASSES = {
    "cyclic_dependency",
    "dangling_reference",
    "dead_quest",
    "unreachable_target",
}


def _input_manifest_sha256(command: tuple[str, ...]) -> str:
    descriptors = []
    for relative in command[1:]:
        raw = (CORPUS / relative).read_bytes()
        descriptors.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            }
        )
    return hashlib.sha256(canonical_json(descriptors).encode("utf-8")).hexdigest()


def _native_stdout(command: tuple[str, ...]) -> bytes:
    nodes = 0
    tokens = 0
    for relative in command[1:]:
        parsed = parse_data_file((CORPUS / relative).read_bytes(), relative)
        nodes += count_nodes(parsed)
        tokens += count_tokens(parsed)
    return f"files={len(command) - 1} nodes={nodes} tokens={tokens}\n".encode()


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def _all_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _all_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_keys(item)


def test_external_case_slice_acceptance() -> None:
    manifest = load_manifest(MANIFEST)

    assert len(manifest.cases) == 8
    assert sum(case.spec.split == "verification" for case in manifest.cases) == 4
    assert {case.spec.defect_class.value for case in manifest.cases} == FOUR_CLASSES
    assert all(case.qualification_status == "qualified" for case in manifest.cases)
    assert all(case.predicate_before.status == "violation" for case in manifest.cases)
    assert all(case.predicate_after.status == "clear" for case in manifest.cases)
    assert all(not case.findings_after for case in manifest.cases)
    assert [(metric.n, metric.k) for metric in manifest.verification] == [(1, 1)] * 4
    assert manifest.after_oracle_fp.n == 8
    assert manifest.after_oracle_fp.count == 0


def test_all_source_native_and_gameforge_hashes_revalidate() -> None:
    registration = load_case_specs(CORPUS / "case-specs.json")
    manifest = load_manifest(MANIFEST)
    by_id = {case.spec.case_id: case for case in manifest.cases}
    source_sha256 = hashlib.sha256(NATIVE_SOURCE.read_bytes()).hexdigest()
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    round_tripped_trees = 0

    assert manifest.mapping_spec_sha256 == hashlib.sha256(
        (CORPUS / "mapping-spec.json").read_bytes()
    ).hexdigest()
    assert manifest.mapping_spec_sha256 == EXPECTED_MAPPING_SPEC_SHA256

    for spec in registration.cases:
        case = by_id[spec.case_id]
        case_root = CORPUS / "cases" / spec.case_id
        for side, expected_tree in (
            ("before", case.before_tree),
            ("after", case.after_tree),
        ):
            side_root = case_root / side
            raw = {path: (side_root / path).read_bytes() for path in spec.changed_paths}
            assert tree_artifact(side_root) == expected_tree
            assert render_source_tree(read_source_tree(raw)) == raw
            round_tripped_trees += 1

        assert case.human_target.patch_sha256 == hashlib.sha256(
            (case_root / "upstream.patch").read_bytes()
        ).hexdigest()
        for native in (case.native_before, case.native_after):
            assert native.source_sha256 == source_sha256
            assert native.input_manifest_sha256 == _input_manifest_sha256(native.command)
            assert native.stdout_sha256 == hashlib.sha256(
                _native_stdout(native.command)
            ).hexdigest()
            assert native.stderr_sha256 == empty_sha256
        assert case.evidence_sha256 == content_sha256(
            case,
            exclude={"evidence_sha256"},
        )

    assert round_tripped_trees == 16
    assert manifest.manifest_sha256 == content_sha256(
        manifest,
        exclude={"manifest_sha256"},
    )


def test_slice_fabricates_neither_agent_patch_nor_hed_result() -> None:
    manifest = load_manifest(MANIFEST)
    payload = manifest.model_dump(mode="json")

    assert all(case.agent_patch_sha256 is None for case in manifest.cases)
    assert all(case.agent_target_snapshot_id is None for case in manifest.cases)
    assert not any("human_edit" in key.casefold() or key.casefold() == "hed" for key in _all_keys(payload))


def test_legacy_corpora_remain_frozen_and_separate_from_the_new_runner() -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "diff",
            "--quiet",
            FLARE_FREEZE_COMMIT,
            "--",
            "scenarios/flare_corpus",
        ],
        check=True,
    )
    final_artifacts = (
        LEGACY_CORPUS / "adjudication-evidence.json",
        LEGACY_CORPUS / "candidate-ledger.json",
        LEGACY_CORPUS / "b0a-decision.json",
    )
    status = (
        "awaiting_human_evidence"
        if not any(path.exists() for path in final_artifacts)
        else "reviewed"
    )
    assert status == "awaiting_human_evidence"

    runner = ROOT / "gameforge/bench/external_cases/endless_sky_runner.py"
    imports = _imported_modules(runner)
    assert not any(module.startswith("gameforge.bench.external_corpus") for module in imports)
    assert not any("flare" in module.casefold() for module in imports)
