from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from gameforge.bench.external_cases.endless_sky_fixture import _chunk_index, load_case_specs
from gameforge.bench.external_cases.tree import tree_artifact


ROOT = Path(__file__).resolve().parents[3]
CORPUS = ROOT / "scenarios/external_cases/endless_sky"
DEFAULT_REPO = Path("/Users/liyifan/.cache/gameforge/endless-sky.git")


def _repo() -> Path | None:
    configured = os.environ.get("GAMEFORGE_ENDLESS_SKY_REPO")
    candidate = Path(configured) if configured else DEFAULT_REPO
    return candidate if candidate.is_dir() else None


def _git(repo: Path, *args: str) -> bytes:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
    }
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), *args],
        check=True,
        env=env,
        stdout=subprocess.PIPE,
    ).stdout


def test_target_index_ignores_duplicate_unmapped_top_level_records() -> None:
    raw = b'phrase "same"\n\tword one\nphrase "same"\n\tword two\n'

    assert _chunk_index({"data/phrases.txt": raw}) == {}


def test_all_fixture_trees_are_exact_and_context_is_bound() -> None:
    registration = load_case_specs(CORPUS / "case-specs.json")

    for spec in registration.cases:
        case_root = CORPUS / "cases" / spec.case_id
        before_root = case_root / "before"
        after_root = case_root / "after"
        before = tree_artifact(before_root)
        after = tree_artifact(after_root)
        assert [item.path for item in before.files] == list(spec.changed_paths)
        assert [item.path for item in after.files] == list(spec.changed_paths)
        context = json.loads((case_root / "context.json").read_bytes())
        assert context["schema_version"] == "endless-sky-case-context@1"
        assert context["case_id"] == spec.case_id
        assert context["before_tree_sha256"] == before.tree_sha256
        assert context["after_tree_sha256"] == after.tree_sha256
        assert context["target_locators"] == [
            target.model_dump(mode="json") for target in spec.target_locators
        ]
        patch = (case_root / "upstream.patch").read_bytes()
        assert context["upstream_patch_sha256"] == hashlib.sha256(patch).hexdigest()


def test_sound_reference_context_uses_pinned_tree_entries_not_audio_payloads() -> None:
    path = (
        CORPUS
        / "cases/endless-sky.dangling-reference.development/context.json"
    )
    context = json.loads(path.read_bytes())

    assert {item["name"] for item in context["resources"]} == {"explosion small"}
    assert context["resources"][0]["path"] == "sounds/explosion small.wav"
    assert len(context["resources"][0]["git_blob_oid"]) == 40


def test_committed_fixtures_match_local_pinned_git_when_available() -> None:
    repo = _repo()
    if repo is None:
        pytest.skip("local Endless Sky bare repository is not available")
    registration = load_case_specs(CORPUS / "case-specs.json")

    for spec in registration.cases:
        case_root = CORPUS / "cases" / spec.case_id
        for path in spec.changed_paths:
            assert (case_root / "before" / path).read_bytes() == _git(
                repo, "show", f"{spec.before_commit}:{path}"
            )
            assert (case_root / "after" / path).read_bytes() == _git(
                repo, "show", f"{spec.after_commit}:{path}"
            )


def test_license_and_notice_bind_the_upstream_source() -> None:
    license_text = (CORPUS / "LICENSE.upstream.txt").read_text(encoding="utf-8")
    notice = (CORPUS / "NOTICE.md").read_text(encoding="utf-8")

    assert "GNU GENERAL PUBLIC LICENSE" in license_text
    assert "GPL-3.0-or-later" in notice
    assert "https://github.com/endless-sky/endless-sky" in notice
    assert "b10b7d6c24496e2f67a230a2553b344e200ba289" in notice
