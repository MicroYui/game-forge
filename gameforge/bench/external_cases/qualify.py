"""Source-neutral qualification and scoring for external before/after cases."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from gameforge.bench.external_cases.contracts import (
    ExternalCaseEvidence,
    ExternalCaseScore,
    ExternalCaseSpec,
    ExternalClassMetric,
    ExternalCorpusManifest,
    ExternalFpMetric,
    FindingEvidence,
    HumanTarget,
    NativeEvidence,
    PredicateEvidence,
    TreeArtifact,
    canonical_bytes,
)
from gameforge.bench.taxonomy import DefectClass
from gameforge.spine.stats import wilson_ci


def qualify_case(
    *,
    spec: ExternalCaseSpec,
    before_tree: TreeArtifact,
    after_tree: TreeArtifact,
    native_before: NativeEvidence,
    native_after: NativeEvidence,
    predicate_before: PredicateEvidence,
    predicate_after: PredicateEvidence,
    reader_version: str,
    adapter_version: str,
    mapping_spec_sha256: str,
    expected_reader_version: str,
    expected_adapter_version: str,
    expected_mapping_spec_sha256: str,
    target_entity_ids: Sequence[str],
    findings_before: Sequence[FindingEvidence],
    findings_after: Sequence[FindingEvidence],
    human_target: HumanTarget,
    upstream_patch_sha256: str,
    agent_patch_sha256: str | None = None,
    agent_target_snapshot_id: str | None = None,
) -> ExternalCaseEvidence:
    """Retain a complete row and classify every failed qualification gate."""

    failures: list[str] = []
    if native_before.exit_code != 0:
        failures.append("native_before_failed")
    if native_after.exit_code != 0:
        failures.append("native_after_failed")
    if predicate_before.status != "violation":
        failures.append("predicate_before_not_violation")
    if predicate_after.status != "clear":
        failures.append("predicate_after_not_clear")
    if (
        predicate_before.predicate_id != spec.predicate_id
        or predicate_before.target_locators != spec.target_locators
    ):
        failures.append("predicate_before_binding_mismatch")
    if (
        predicate_after.predicate_id != spec.predicate_id
        or predicate_after.target_locators != spec.target_locators
    ):
        failures.append("predicate_after_binding_mismatch")
    if reader_version != expected_reader_version:
        failures.append("reader_version_mismatch")
    if adapter_version != expected_adapter_version:
        failures.append("adapter_version_mismatch")
    if mapping_spec_sha256 != expected_mapping_spec_sha256:
        failures.append("mapping_spec_mismatch")

    resolved_targets = tuple(sorted(set(target_entity_ids)))
    if not resolved_targets:
        failures.append("target_entities_missing")
    target_set = set(resolved_targets)
    before = tuple(findings_before)
    after = tuple(findings_after)
    expected_class = spec.defect_class.value
    before_hit = any(
        finding.defect_class == expected_class
        and finding.status == "confirmed"
        and bool(set(finding.entities) & target_set)
        for finding in before
    )
    if not before_hit:
        failures.append("before_expected_finding_missing")
    if any(
        finding.defect_class == expected_class
        and finding.status in {"confirmed", "unproven"}
        and bool(set(finding.entities) & target_set)
        for finding in after
    ):
        failures.append("after_expected_finding_present")
    if after:
        failures.append("after_snapshot_not_clean")
    if human_target.patch_sha256 != upstream_patch_sha256:
        failures.append("human_patch_digest_mismatch")

    return ExternalCaseEvidence.seal(
        schema_version="external-case@1",
        spec=spec,
        before_tree=before_tree,
        after_tree=after_tree,
        native_before=native_before,
        native_after=native_after,
        predicate_before=predicate_before,
        predicate_after=predicate_after,
        reader_version=reader_version,
        adapter_version=adapter_version,
        mapping_spec_sha256=mapping_spec_sha256,
        target_entity_ids=resolved_targets,
        findings_before=before,
        findings_after=after,
        human_target=human_target,
        agent_patch_sha256=agent_patch_sha256,
        agent_target_snapshot_id=agent_target_snapshot_id,
        qualification_status="miss" if failures else "qualified",
        failure_reasons=tuple(failures),
    )


def _class_metrics(
    cases: Sequence[ExternalCaseEvidence],
    split: str,
) -> tuple[ExternalClassMetric, ...]:
    grouped: dict[DefectClass, list[ExternalCaseEvidence]] = {}
    for case in cases:
        if case.spec.split == split:
            grouped.setdefault(case.spec.defect_class, []).append(case)
    metrics: list[ExternalClassMetric] = []
    for defect_class, rows in sorted(grouped.items(), key=lambda item: item[0].value):
        n = len(rows)
        k = sum(row.qualification_status == "qualified" for row in rows)
        low, high = wilson_ci(k, n)
        metrics.append(
            ExternalClassMetric(
                defect_class=defect_class,
                split=split,
                n=n,
                k=k,
                rate=k / n,
                ci_low=low,
                ci_high=high,
            )
        )
    return tuple(metrics)


def score_external_cases(cases: Sequence[ExternalCaseEvidence]) -> ExternalCaseScore:
    rows = tuple(cases)
    if not rows:
        raise ValueError("external score requires at least one case")
    after_count = sum(bool(case.findings_after) for case in rows)
    after_low, after_high = wilson_ci(after_count, len(rows))
    return ExternalCaseScore(
        development=_class_metrics(rows, "development"),
        verification=_class_metrics(rows, "verification"),
        after_oracle_fp=ExternalFpMetric(
            n=len(rows),
            count=after_count,
            rate=after_count / len(rows),
            ci_low=after_low,
            ci_high=after_high,
        ),
    )


def load_manifest(path: str | Path) -> ExternalCorpusManifest:
    manifest_path = Path(path)
    raw = manifest_path.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid external corpus manifest: {manifest_path}") from exc
    manifest = ExternalCorpusManifest.model_validate(payload)
    if canonical_bytes(manifest) != raw:
        raise ValueError("external corpus manifest is not canonical JSON")
    return manifest


__all__ = ["load_manifest", "qualify_case", "score_external_cases"]
