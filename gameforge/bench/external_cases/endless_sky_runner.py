"""Replay the frozen Endless Sky corpus into external-case evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from gameforge.bench.external_cases.contracts import (
    ExternalCaseEvidence,
    ExternalCaseSpec,
    ExternalCorpusManifest,
    FindingEvidence,
    HumanTarget,
    TargetLocator,
    canonical_bytes,
    content_sha256,
)
from gameforge.bench.external_cases.endless_sky_fixture import load_case_specs
from gameforge.bench.external_cases.endless_sky_predicates import evaluate_predicate
from gameforge.bench.external_cases.native import (
    NativeParserBinary,
    compile_native_parser,
    native_evidence,
    run_native_parser,
)
from gameforge.bench.external_cases.qualify import (
    load_manifest,
    qualify_case,
    score_external_cases,
)
from gameforge.bench.external_cases.tree import tree_artifact
from gameforge.bench.taxonomy import DefectClass
from gameforge.contracts.findings import Finding
from gameforge.spine.checkers.asp import ASPChecker
from gameforge.spine.checkers.graph import GraphChecker
from gameforge.spine.ingestion.endless_sky_adapter import (
    ADAPTER_VERSION,
    EndlessSkyContext,
    EndlessSkyResource,
    EndlessSkyTarget,
    EndlessSkyTxtAdapter,
)
from gameforge.spine.ingestion.endless_sky_reader import (
    READER_VERSION,
    EndlessSkyTree,
    read_source_tree,
    render_source_tree,
    top_level_chunks,
)
from gameforge.spine.ir.snapshot import Snapshot


MANIFEST_NAME = "external-corpus-manifest.json"
EXPECTED_MAPPING_SPEC_SHA256 = (
    "355d36ad6a7f92a344540c2f3d6b4c7fceeb569d7a34838370152eaca8d5cab7"
)


def _json_object(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON evidence file: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON evidence file must contain an object: {path}")
    if canonical_bytes(payload) != raw:
        raise ValueError(f"JSON evidence file is not canonical: {path}")
    return payload


def _mapping_spec(corpus: Path) -> tuple[dict[str, Any], str]:
    path = corpus / "mapping-spec.json"
    payload = _json_object(path)
    return payload, hashlib.sha256(path.read_bytes()).hexdigest()


def _context(case_root: Path, spec: ExternalCaseSpec) -> dict[str, Any]:
    payload = _json_object(case_root / "context.json")
    if payload.get("schema_version") != "endless-sky-case-context@1":
        raise ValueError(f"unsupported case context schema for {spec.case_id}")
    if payload.get("case_id") != spec.case_id:
        raise ValueError(f"case context id mismatch for {spec.case_id}")
    expected_targets = [target.model_dump(mode="json") for target in spec.target_locators]
    if payload.get("target_locators") != expected_targets:
        raise ValueError(f"case context target mismatch for {spec.case_id}")
    return payload


def _source_side(
    case_root: Path,
    spec: ExternalCaseSpec,
    side: str,
    context: dict[str, Any],
) -> tuple[dict[str, bytes], EndlessSkyTree, Any]:
    source_root = case_root / side
    artifact = tree_artifact(source_root)
    expected_tree = context.get(f"{side}_tree_sha256")
    if artifact.tree_sha256 != expected_tree:
        raise ValueError(f"{spec.case_id} {side} tree differs from frozen context")
    raw = {path: (source_root / path).read_bytes() for path in spec.changed_paths}
    tree = read_source_tree(raw)
    if render_source_tree(tree) != raw:
        raise ValueError(f"{spec.case_id} {side} reader round-trip changed bytes")
    return raw, tree, artifact


def _adapter_context(payload: dict[str, Any]) -> EndlessSkyContext:
    resources = payload.get("resources")
    restricted = payload.get("restricted_destinations")
    if not isinstance(resources, list) or not isinstance(restricted, list):
        raise ValueError("case context resources and restricted_destinations must be lists")
    return EndlessSkyContext(
        resources=tuple(
            EndlessSkyResource(kind=item["kind"], name=item["name"])
            for item in resources
        ),
        restricted_destinations=tuple(restricted),
    )


def _adapter_targets(spec: ExternalCaseSpec) -> tuple[EndlessSkyTarget, ...]:
    return tuple(
        EndlessSkyTarget(
            path=target.path,
            record_kind=target.record_kind,
            record_name=target.record_name,
        )
        for target in spec.target_locators
    )


def _target_anchors(
    tree: EndlessSkyTree,
    targets: tuple[TargetLocator, ...],
) -> set[tuple[str, str, int]]:
    by_key: dict[tuple[str, str, str], list[int]] = {}
    for data_file in tree.files:
        for chunk in top_level_chunks(data_file):
            by_key.setdefault((chunk.path, chunk.kind, chunk.name), []).append(chunk.index)
    anchors: set[tuple[str, str, int]] = set()
    for target in targets:
        rows = by_key.get((target.path, target.record_kind, target.record_name), [])
        if len(rows) != 1:
            continue
        anchors.add((target.path, target.record_kind, rows[0]))
    return anchors


def _target_entity_ids(
    tree: EndlessSkyTree,
    snapshot: Snapshot,
    targets: tuple[TargetLocator, ...],
) -> tuple[str, ...]:
    anchors = _target_anchors(tree, targets)
    result = {
        entity.id
        for entity in snapshot.entities.values()
        if entity.source_ref is not None
        and (
            entity.source_ref.file,
            entity.source_ref.sheet,
            entity.source_ref.row,
        )
        in anchors
    }
    return tuple(sorted(result))


def _finding_evidence(finding: Finding) -> FindingEvidence:
    if finding.status not in {"confirmed", "unproven"}:
        raise ValueError(f"unsupported external finding status: {finding.status}")
    return FindingEvidence(
        finding_id=finding.id,
        defect_class=finding.defect_class,
        status=finding.status,
        entities=tuple(finding.entities),
        evidence_sha256=content_sha256(finding.model_dump(mode="json")),
    )


def _run_findings(
    snapshot: Snapshot,
    defect_class: DefectClass,
) -> tuple[FindingEvidence, ...]:
    findings = GraphChecker().check(snapshot)
    if defect_class is DefectClass.cyclic_dependency:
        findings.extend(ASPChecker().check(snapshot))
    evidence = (_finding_evidence(finding) for finding in findings)
    return tuple(
        sorted(
            evidence,
            key=lambda item: (
                item.defect_class,
                item.finding_id,
                item.evidence_sha256,
            ),
        )
    )


def _native_side(
    binary: NativeParserBinary,
    corpus: Path,
    case_root: Path,
    spec: ExternalCaseSpec,
    side: str,
):
    paths = [case_root / side / path for path in spec.changed_paths]
    return native_evidence(
        binary,
        run_native_parser(binary, paths, source_root=corpus),
    )


def _case_evidence(
    corpus: Path,
    binary: NativeParserBinary,
    spec: ExternalCaseSpec,
    mapping: dict[str, Any],
    mapping_sha256: str,
) -> ExternalCaseEvidence:
    case_root = corpus / "cases" / spec.case_id
    context = _context(case_root, spec)
    before_raw, before_source, before_artifact = _source_side(
        case_root, spec, "before", context
    )
    after_raw, after_source, after_artifact = _source_side(
        case_root, spec, "after", context
    )
    adapter_context = _adapter_context(context)
    adapter_targets = _adapter_targets(spec)
    adapter = EndlessSkyTxtAdapter()
    before_snapshot = adapter.to_ir(
        before_source,
        targets=adapter_targets,
        context=adapter_context,
    )
    after_snapshot = adapter.to_ir(
        after_source,
        targets=adapter_targets,
        context=adapter_context,
    )
    if adapter.from_ir(before_snapshot) != before_raw:
        raise ValueError(f"{spec.case_id} before Adapter round-trip changed bytes")
    if adapter.from_ir(after_snapshot) != after_raw:
        raise ValueError(f"{spec.case_id} after Adapter round-trip changed bytes")

    before_predicate = evaluate_predicate(
        spec.predicate_id,
        before_source,
        spec.target_locators,
        context,
    )
    after_predicate = evaluate_predicate(
        spec.predicate_id,
        after_source,
        spec.target_locators,
        context,
    )
    targets = _target_entity_ids(before_source, before_snapshot, spec.target_locators)
    after_targets = _target_entity_ids(after_source, after_snapshot, spec.target_locators)
    if targets != after_targets:
        targets = tuple(sorted(set(targets) | set(after_targets)))

    patch_path = case_root / "upstream.patch"
    patch_sha256 = hashlib.sha256(patch_path.read_bytes()).hexdigest()
    expected_patch_sha256 = context.get("upstream_patch_sha256")
    if not isinstance(expected_patch_sha256, str):
        raise ValueError(f"{spec.case_id} context has no upstream patch digest")

    return qualify_case(
        spec=spec,
        before_tree=before_artifact,
        after_tree=after_artifact,
        native_before=_native_side(binary, corpus, case_root, spec, "before"),
        native_after=_native_side(binary, corpus, case_root, spec, "after"),
        predicate_before=before_predicate,
        predicate_after=after_predicate,
        reader_version=before_source.reader_version,
        adapter_version=ADAPTER_VERSION,
        mapping_spec_sha256=mapping_sha256,
        expected_reader_version=str(mapping.get("reader_version", "")),
        expected_adapter_version=str(mapping.get("adapter_version", "")),
        expected_mapping_spec_sha256=EXPECTED_MAPPING_SPEC_SHA256,
        target_entity_ids=targets,
        findings_before=_run_findings(before_snapshot, spec.defect_class),
        findings_after=_run_findings(after_snapshot, spec.defect_class),
        human_target=HumanTarget(
            patch_path=f"cases/{spec.case_id}/upstream.patch",
            patch_sha256=expected_patch_sha256,
        ),
        upstream_patch_sha256=patch_sha256,
    )


def build_manifest(corpus: str | Path, build_dir: str | Path) -> ExternalCorpusManifest:
    corpus_root = Path(corpus).resolve(strict=True)
    registration = load_case_specs(corpus_root / "case-specs.json")
    mapping, mapping_sha256 = _mapping_spec(corpus_root)
    if mapping.get("reader_version") != READER_VERSION:
        raise ValueError("mapping spec reader version differs from runtime reader")
    binary = compile_native_parser(
        corpus_root / "native/endless_sky_data_parser.cpp",
        Path(build_dir) / "native",
    )
    cases = tuple(
        _case_evidence(corpus_root, binary, spec, mapping, mapping_sha256)
        for spec in registration.cases
    )
    score = score_external_cases(cases)
    return ExternalCorpusManifest.seal(
        schema_version="external-corpus-manifest@1",
        source_id=registration.source_id,
        pinned_head=registration.pinned_head,
        repository_url=registration.repository_url,
        reader_version=READER_VERSION,
        adapter_version=ADAPTER_VERSION,
        mapping_spec_sha256=mapping_sha256,
        cases=cases,
        development=score.development,
        verification=score.verification,
        after_oracle_fp=score.after_oracle_fp,
    )


def replay_corpus(corpus: str | Path, output_dir: str | Path) -> bytes:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(corpus, output / "build")
    raw = canonical_bytes(manifest)
    destination = output / MANIFEST_NAME
    temporary = output / f".{MANIFEST_NAME}.tmp"
    temporary.write_bytes(raw)
    os.replace(temporary, destination)
    if canonical_bytes(load_manifest(destination)) != raw:
        raise ValueError("external corpus manifest changed during validation")
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True)
    args = parser.parse_args(argv)
    corpus = Path(args.corpus).resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="gameforge-external-evidence-") as temp:
        raw = replay_corpus(corpus, temp)
    destination = corpus / MANIFEST_NAME
    temporary = corpus / f".{MANIFEST_NAME}.tmp"
    temporary.write_bytes(raw)
    os.replace(temporary, destination)
    manifest = load_manifest(destination)
    qualified = sum(case.qualification_status == "qualified" for case in manifest.cases)
    print(
        f"qualified={qualified}/{len(manifest.cases)} "
        f"verification={sum(metric.k for metric in manifest.verification)}/"
        f"{sum(metric.n for metric in manifest.verification)} "
        f"after_oracle_fp={manifest.after_oracle_fp.count}/"
        f"{manifest.after_oracle_fp.n}"
    )
    return 0 if qualified == len(manifest.cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["EXPECTED_MAPPING_SPEC_SHA256", "build_manifest", "main", "replay_corpus"]
