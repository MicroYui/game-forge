"""Freeze the eight preregistered Endless Sky before/after source trees.

This module is deliberately source-bound. It is an evidence builder, not part
of the generic checker and not a continuation of the legacy B0A approval path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from gameforge.bench.external_cases.contracts import (
    ExternalCaseRegistration,
    ExternalCaseSpec,
    TargetLocator,
    canonical_bytes,
)
from gameforge.bench.external_cases.tree import tree_artifact
from gameforge.bench.taxonomy import DefectClass
from gameforge.contracts.canonical import canonical_json
from gameforge.spine.ingestion.endless_sky_reader import (
    DataNode,
    TopLevelChunk,
    parse_data_file,
    top_level_chunks,
)


ENDLESS_SKY_PINNED_HEAD = "b10b7d6c24496e2f67a230a2553b344e200ba289"
ENDLESS_SKY_REPOSITORY_URL = "https://github.com/endless-sky/endless-sky.git"
ENDLESS_SKY_SOURCE_ID = "endless_sky"
ENDLESS_SKY_LICENSE_ID = "GPL-3.0-or-later"


@dataclass(frozen=True)
class _Seed:
    case_id: str
    after_commit: str
    defect_class: DefectClass
    split: Literal["development", "verification"]
    predicate_id: str
    upstream_pr: int | None
    target_rule: Literal["explicit", "added_clearance", "added_landing_access"]
    target_kind: str = "mission"
    target_name: str | None = None


_SEEDS = (
    _Seed(
        "endless-sky.dangling-reference.development",
        "02e6ded1e7cb9ef7a8e401e71c9accd6133a68b5",
        DefectClass.dangling_reference,
        "development",
        "reference_resolves",
        10424,
        "explicit",
        "effect",
        "star tail hit",
    ),
    _Seed(
        "endless-sky.dangling-reference.verification",
        "61425f7538b33ed5bddd77ea9c29ffd7737a242b",
        DefectClass.dangling_reference,
        "verification",
        "reference_resolves",
        9557,
        "explicit",
        target_name="Saryd University Lecture",
    ),
    _Seed(
        "endless-sky.cyclic-dependency.development",
        "2476129506e96086b00b09e1999dcb10ff8390fd",
        DefectClass.cyclic_dependency,
        "development",
        "dependency_acyclic",
        12045,
        "explicit",
        target_name="Lost Racer 3",
    ),
    _Seed(
        "endless-sky.cyclic-dependency.verification",
        "95b5c4e95f715c2a13c201396d6dda5ea33d8cf7",
        DefectClass.cyclic_dependency,
        "verification",
        "dependency_acyclic",
        9348,
        "explicit",
        target_name="Care Package to South 3a",
    ),
    _Seed(
        "endless-sky.unreachable-target.development",
        "9e437162fffef43da5f836d1f92bb265ccc75c52",
        DefectClass.unreachable_target,
        "development",
        "target_reachable",
        11977,
        "added_clearance",
    ),
    _Seed(
        "endless-sky.unreachable-target.verification",
        "34383dd960f42de2537a06c2bb0ba3f35a8a73c0",
        DefectClass.unreachable_target,
        "verification",
        "target_reachable",
        11174,
        "added_landing_access",
    ),
    _Seed(
        "endless-sky.dead-quest.development",
        "de8385df680ba81c70f13b380ef0b13070eba49b",
        DefectClass.dead_quest,
        "development",
        "mission_offerable",
        4576,
        "explicit",
        target_name="Terraforming 7",
    ),
    _Seed(
        "endless-sky.dead-quest.verification",
        "9b29c95b99e67efbd1acda09a9994fe37405278e",
        DefectClass.dead_quest,
        "verification",
        "mission_offerable",
        None,
        "explicit",
        target_name="FWC Pug 1",
    ),
)


_MAPPING_SPEC: dict[str, Any] = {
    "schema_version": "endless-sky-mapping-spec@1",
    "reader_version": "endless-sky-reader@1",
    "adapter_version": "endless-sky-adapter@1",
    "semantic_scope": "target locators plus direct mission-state dependencies",
    "raw_preservation": "one ordered base64 source chunk per top-level record",
    "top_level_records": {
        "mission": "QUEST",
        "effect": "EFFECT",
        "fallback": "EVENT",
    },
    "relations": {
        "mission_step": "HAS_STEP",
        "mission_start": "STARTS_AT",
        "mission_state_condition": "REQUIRES",
        "destination": "LOCATED_IN",
        "access_gate": "GATED_BY",
        "clearance": "UNLOCKS",
        "dialogue_transition": "PRECEDES",
        "named_resource": "REFERENCES",
    },
    "bounded_transition": {
        "guard": "to display / not <condition>",
        "discharge": "target action / set <same condition>",
        "ir_attr": {"repeatability": "once"},
    },
}


_NOTICE = """# Endless Sky External-Case Notice

The files below `cases/` are exact before/after configuration blobs and patches
from [Endless Sky](https://github.com/endless-sky/endless-sky), pinned at
`b10b7d6c24496e2f67a230a2553b344e200ba289`.

Endless Sky code and data are distributed under GPL-3.0-or-later. The upstream
license is reproduced in `LICENSE.upstream.txt`. GameForge keeps original paths,
commit provenance, and SHA-256 bindings in each case context. These fixtures are
used only as external correctness evidence; they are not GameForge-authored game
content and are not relicensed.
"""


def _git_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
    }


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    completed = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), *args],
        check=True,
        env=_git_env(),
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout


def _descendants(node: DataNode):
    for child in node.children:
        yield child
        yield from _descendants(child)


def _token_values(node: DataNode) -> tuple[str, ...]:
    return tuple(token.value for token in node.tokens)


def _chunk_index(files: dict[str, bytes]) -> dict[tuple[str, str, str], TopLevelChunk]:
    result: dict[tuple[str, str, str], TopLevelChunk] = {}
    for path, raw in files.items():
        for chunk in top_level_chunks(parse_data_file(raw, path)):
            if chunk.kind not in {"mission", "effect"}:
                continue
            key = (path, chunk.kind, chunk.name)
            if key in result:
                raise ValueError(f"duplicate top-level source record: {key}")
            result[key] = chunk
    return result


def _has_directive(node: DataNode, name: str) -> bool:
    return any(_token_values(child) == (name,) for child in node.children)


def _landing_access_conditions(node: DataNode) -> set[str]:
    result: set[str] = set()
    for child in _descendants(node):
        values = _token_values(child)
        if len(values) >= 2 and values[0] == "has" and values[1].startswith("landing access: "):
            result.add(values[1].removeprefix("landing access: "))
    return result


def _destination(node: DataNode) -> str | None:
    for child in node.children:
        values = _token_values(child)
        if len(values) >= 2 and values[0] == "destination":
            return values[1]
    return None


def _derive_targets(
    seed: _Seed,
    before_files: dict[str, bytes],
    after_files: dict[str, bytes],
) -> tuple[TargetLocator, ...]:
    before = _chunk_index(before_files)
    after = _chunk_index(after_files)
    targets: list[TargetLocator] = []
    if seed.target_rule == "explicit":
        matches = [
            (key, chunk)
            for key, chunk in after.items()
            if key[1] == seed.target_kind and key[2] == seed.target_name
        ]
        if len(matches) != 1:
            raise ValueError(f"expected one explicit target for {seed.case_id}, got {len(matches)}")
        key, _ = matches[0]
        targets.append(TargetLocator(path=key[0], record_kind=key[1], record_name=key[2]))
    else:
        for key, after_chunk in after.items():
            if key[1] != "mission" or after_chunk.node is None:
                continue
            before_chunk = before.get(key)
            if before_chunk is None or before_chunk.node is None or before_chunk.raw == after_chunk.raw:
                continue
            selected = False
            if seed.target_rule == "added_clearance":
                selected = _has_directive(after_chunk.node, "clearance") and not _has_directive(
                    before_chunk.node, "clearance"
                )
            elif seed.target_rule == "added_landing_access":
                selected = bool(
                    _landing_access_conditions(after_chunk.node)
                    - _landing_access_conditions(before_chunk.node)
                )
            if selected:
                targets.append(
                    TargetLocator(path=key[0], record_kind=key[1], record_name=key[2])
                )
    if not targets:
        raise ValueError(f"no targets derived for {seed.case_id}")
    return tuple(sorted(targets, key=lambda item: (item.path, item.record_name)))


def _changed_paths(repo: Path, before: str, after: str) -> tuple[str, ...]:
    output = _git(
        repo,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "--no-renames",
        "-r",
        before,
        after,
    ).decode("utf-8")
    paths = tuple(sorted(line for line in output.splitlines() if line))
    if not paths or any(not path.startswith("data/") or not path.endswith(".txt") for path in paths):
        raise ValueError(f"commit {after} is not config-only: {paths}")
    return paths


def _source_files(repo: Path, commit: str, paths: tuple[str, ...]) -> dict[str, bytes]:
    return {path: _git(repo, "show", f"{commit}:{path}") for path in paths}


def _resource_context(
    repo: Path,
    targets: tuple[TargetLocator, ...],
    before_files: dict[str, bytes],
    after_files: dict[str, bytes],
) -> list[dict[str, Any]]:
    names: set[str] = set()
    for files in (before_files, after_files):
        index = _chunk_index(files)
        for target in targets:
            chunk = index.get((target.path, target.record_kind, target.record_name))
            if chunk is None or chunk.node is None or target.record_kind != "effect":
                continue
            for child in _descendants(chunk.node):
                values = _token_values(child)
                if len(values) >= 2 and values[0] == "sound":
                    names.add(values[1])

    tree_output = _git(
        repo,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        ENDLESS_SKY_PINNED_HEAD,
        "--",
        "sounds",
    )
    sound_entries: dict[str, list[tuple[str, str]]] = {}
    for record in tree_output.split(b"\x00"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        _, object_type, oid = metadata.decode("ascii").split(" ")
        if object_type != "blob":
            continue
        path = raw_path.decode("utf-8")
        stem = Path(path).stem.removesuffix("@3x").removesuffix("~")
        sound_entries.setdefault(stem, []).append((path, oid))

    resources: list[dict[str, Any]] = []
    for name in sorted(names):
        candidates = sorted(
            sound_entries.get(name, []),
            key=lambda item: (item[0] != f"sounds/{name}.wav", item[0]),
        )
        if candidates:
            path, oid = candidates[0]
            resources.append(
                {
                    "kind": "sound",
                    "name": name,
                    "path": path,
                    "git_blob_oid": oid,
                }
            )
    return resources


def _restricted_destinations(
    targets: tuple[TargetLocator, ...], after_files: dict[str, bytes]
) -> list[str]:
    index = _chunk_index(after_files)
    result: set[str] = set()
    for target in targets:
        chunk = index[(target.path, target.record_kind, target.record_name)]
        if chunk.node is None or target.record_kind != "mission":
            continue
        destination = _destination(chunk.node)
        if destination is None:
            continue
        if _has_directive(chunk.node, "clearance"):
            result.add(destination)
        if destination in _landing_access_conditions(chunk.node):
            result.add(destination)
    return sorted(result)


def _write_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def _case_spec(repo: Path, seed: _Seed) -> tuple[ExternalCaseSpec, dict[str, bytes], dict[str, bytes]]:
    parents = _git(repo, "rev-list", "--parents", "-n", "1", seed.after_commit).decode().split()
    if len(parents) != 2:
        raise ValueError(f"external case must be a single-parent commit: {seed.after_commit}")
    _, before_commit = parents
    _git(repo, "merge-base", "--is-ancestor", seed.after_commit, ENDLESS_SKY_PINNED_HEAD)
    paths = _changed_paths(repo, before_commit, seed.after_commit)
    before_files = _source_files(repo, before_commit, paths)
    after_files = _source_files(repo, seed.after_commit, paths)
    targets = _derive_targets(seed, before_files, after_files)
    subject = _git(repo, "show", "-s", "--format=%s", seed.after_commit).decode().rstrip("\n")
    spec = ExternalCaseSpec(
        schema_version="external-case-spec@1",
        case_id=seed.case_id,
        source_id=ENDLESS_SKY_SOURCE_ID,
        source_repository=ENDLESS_SKY_REPOSITORY_URL,
        license_id=ENDLESS_SKY_LICENSE_ID,
        before_commit=before_commit,
        after_commit=seed.after_commit,
        upstream_subject=subject,
        upstream_pr=seed.upstream_pr,
        changed_paths=paths,
        defect_class=seed.defect_class,
        target_locators=targets,
        split=seed.split,
        predicate_id=seed.predicate_id,
    )
    return spec, before_files, after_files


def extract_corpus(repo: str | Path, corpus: str | Path) -> ExternalCaseRegistration:
    repository = Path(repo).resolve(strict=True)
    destination = Path(corpus)
    destination.mkdir(parents=True, exist_ok=True)
    specs: list[ExternalCaseSpec] = []

    for seed in _SEEDS:
        spec, before_files, after_files = _case_spec(repository, seed)
        specs.append(spec)
        case_root = destination / "cases" / spec.case_id
        for path, raw in before_files.items():
            _write_bytes(case_root / "before" / path, raw)
        for path, raw in after_files.items():
            _write_bytes(case_root / "after" / path, raw)
        patch = _git(
            repository,
            "diff",
            "--binary",
            "--full-index",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            spec.before_commit,
            spec.after_commit,
            "--",
            *spec.changed_paths,
        )
        _write_bytes(case_root / "upstream.patch", patch)
        before_tree = tree_artifact(case_root / "before")
        after_tree = tree_artifact(case_root / "after")
        context = {
            "schema_version": "endless-sky-case-context@1",
            "case_id": spec.case_id,
            "before_tree_sha256": before_tree.tree_sha256,
            "after_tree_sha256": after_tree.tree_sha256,
            "target_locators": [target.model_dump(mode="json") for target in spec.target_locators],
            "resources": _resource_context(
                repository, spec.target_locators, before_files, after_files
            ),
            "restricted_destinations": _restricted_destinations(
                spec.target_locators, after_files
            ),
            "upstream_patch_sha256": hashlib.sha256(patch).hexdigest(),
        }
        _write_bytes(
            case_root / "context.json",
            (canonical_json(context) + "\n").encode("utf-8"),
        )

    registration = ExternalCaseRegistration.seal(
        schema_version="external-case-registration@1",
        source_id=ENDLESS_SKY_SOURCE_ID,
        pinned_head=ENDLESS_SKY_PINNED_HEAD,
        repository_url=ENDLESS_SKY_REPOSITORY_URL,
        cases=tuple(specs),
    )
    _write_bytes(destination / "case-specs.json", canonical_bytes(registration))
    _write_bytes(
        destination / "mapping-spec.json",
        (canonical_json(_MAPPING_SPEC) + "\n").encode("utf-8"),
    )
    _write_bytes(destination / "NOTICE.md", _NOTICE.encode("utf-8"))
    _write_bytes(
        destination / "LICENSE.upstream.txt",
        _git(repository, "show", f"{ENDLESS_SKY_PINNED_HEAD}:license.txt"),
    )
    return registration


def load_case_specs(path: str | Path) -> ExternalCaseRegistration:
    raw = Path(path).read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid external case registration: {path}") from exc
    registration = ExternalCaseRegistration.model_validate(payload)
    if canonical_bytes(registration) != raw:
        raise ValueError("external case registration is not canonical JSON")
    return registration


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--pinned-head", required=True)
    args = parser.parse_args(argv)
    if args.pinned_head != ENDLESS_SKY_PINNED_HEAD:
        parser.error(f"--pinned-head must be {ENDLESS_SKY_PINNED_HEAD}")
    extract_corpus(args.repo, args.corpus)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ENDLESS_SKY_PINNED_HEAD",
    "ENDLESS_SKY_REPOSITORY_URL",
    "extract_corpus",
    "load_case_specs",
    "main",
]
