from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from gameforge.bench.external_cases.endless_sky_fixture import load_case_specs
from gameforge.bench.external_cases.native import (
    compile_native_parser,
    native_evidence,
    run_native_parser,
)
from gameforge.spine.ingestion.endless_sky_reader import (
    count_nodes,
    count_tokens,
    parse_data_file,
)


ROOT = Path(__file__).resolve().parents[3]
CORPUS = ROOT / "scenarios/external_cases/endless_sky"
SOURCE = CORPUS / "native/endless_sky_data_parser.cpp"
PROVENANCE = CORPUS / "native/source-provenance.json"


@pytest.fixture(scope="module")
def native_binary(tmp_path_factory):
    if shutil.which("c++") is None:
        pytest.skip("a C++17 compiler is required for the native parser conformance test")
    return compile_native_parser(SOURCE, tmp_path_factory.mktemp("native-parser"))


def test_native_parser_matches_python_node_and_token_counts(tmp_path, native_binary) -> None:
    fixture = tmp_path / "example data.txt"
    fixture.write_bytes(
        b'# preamble\nmission "Example"\n\tto offer\n\t\thas `Prior: done`\n'
    )
    parsed = parse_data_file(fixture.read_bytes(), "example data.txt")

    result = run_native_parser(native_binary, [fixture], source_root=tmp_path)

    assert result.exit_code == 0
    assert result.summary == {
        "files": 1,
        "nodes": count_nodes(parsed),
        "tokens": count_tokens(parsed),
    }
    assert result.stderr == b""


def test_native_evidence_is_hashed_and_workspace_independent(tmp_path, native_binary) -> None:
    fixture = tmp_path / "nested" / "example.txt"
    fixture.parent.mkdir()
    fixture.write_bytes(b"mission Example\n\tto offer\n")

    result = run_native_parser(native_binary, [fixture], source_root=tmp_path)
    evidence = native_evidence(native_binary, result)

    assert result.command == (
        "endless-sky-datafile-native",
        "nested/example.txt",
    )
    assert evidence.parser_id == "endless-sky-datafile-native"
    assert evidence.parser_version == "endless-sky-datafile-native@1"
    assert evidence.source_sha256 == native_binary.source_sha256
    assert evidence.input_manifest_sha256 == result.input_manifest_sha256
    assert evidence.command == result.command
    assert evidence.exit_code == 0
    assert evidence.stdout_sha256 == hashlib.sha256(result.stdout).hexdigest()
    assert evidence.stderr_sha256 == hashlib.sha256(result.stderr).hexdigest()
    assert evidence.compiler == native_binary.compiler


def test_native_parser_rejects_unterminated_quote(tmp_path, native_binary) -> None:
    fixture = tmp_path / "bad.txt"
    fixture.write_bytes(b'mission "unterminated\n')

    result = run_native_parser(native_binary, [fixture], source_root=tmp_path)

    assert result.exit_code != 0
    assert b"unterminated" in result.stderr


@pytest.mark.parametrize("side", ["before", "after"])
def test_native_parser_matches_python_over_every_frozen_tree(side, native_binary) -> None:
    registration = load_case_specs(CORPUS / "case-specs.json")
    paths: list[Path] = []
    expected_nodes = 0
    expected_tokens = 0
    for spec in registration.cases:
        side_root = CORPUS / "cases" / spec.case_id / side
        for relative in spec.changed_paths:
            path = side_root / relative
            parsed = parse_data_file(path.read_bytes(), relative)
            paths.append(path)
            expected_nodes += count_nodes(parsed)
            expected_tokens += count_tokens(parsed)

    result = run_native_parser(native_binary, paths, source_root=CORPUS)

    assert result.exit_code == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.summary == {
        "files": len(paths),
        "nodes": expected_nodes,
        "tokens": expected_tokens,
    }


def test_native_source_provenance_binds_source_and_upstream_parser() -> None:
    provenance = json.loads(PROVENANCE.read_bytes())

    assert provenance["schema_version"] == "endless-sky-native-parser-provenance@1"
    assert provenance["upstream_commit"] == "b10b7d6c24496e2f67a230a2553b344e200ba289"
    assert provenance["license_id"] == "GPL-3.0-or-later"
    assert provenance["native_source_sha256"] == hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    assert {item["path"] for item in provenance["derived_from"]} == {
        "source/DataFile.cpp",
        "source/DataFile.h",
        "source/DataNode.cpp",
        "source/DataNode.h",
        "source/text/Utf8.cpp",
        "source/text/Utf8.h",
    }
    assert all(len(item["git_blob_oid"]) == 40 for item in provenance["derived_from"])
