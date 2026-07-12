from __future__ import annotations

from copy import deepcopy

import pytest

from gameforge.bench.external_cases.contracts import (
    ExternalCaseSpec,
    ExternalCorpusManifest,
    FindingEvidence,
    HumanTarget,
    NativeEvidence,
    PredicateEvidence,
    TargetLocator,
    TreeArtifact,
    TreeFile,
    content_sha256,
)
from gameforge.bench.external_cases.qualify import qualify_case, score_external_cases
from gameforge.bench.taxonomy import DefectClass


ZERO_SHA = "0" * 64
ONE_SHA = "1" * 64
TWO_SHA = "2" * 64
BEFORE = "a" * 40
AFTER = "b" * 40
PINNED = "c" * 40
READER_VERSION = "reader@1"
ADAPTER_VERSION = "adapter@1"
TARGET = "quest:fixture:target"


def _spec(
    *,
    case_id: str = "fixture.case",
    split: str = "verification",
    defect_class: DefectClass = DefectClass.dangling_reference,
) -> ExternalCaseSpec:
    return ExternalCaseSpec(
        schema_version="external-case-spec@1",
        case_id=case_id,
        source_id="fixture_game",
        source_repository="https://example.com/fixture-game.git",
        license_id="MIT",
        before_commit=BEFORE,
        after_commit=AFTER,
        upstream_subject="Fix a configuration defect",
        changed_paths=("data/content.txt",),
        defect_class=defect_class,
        target_locators=(
            TargetLocator(
                path="data/content.txt",
                record_kind="mission",
                record_name="Target Mission",
            ),
        ),
        split=split,
        predicate_id="source_predicate",
    )


def _tree(digest: str) -> TreeArtifact:
    file = TreeFile(path="data/content.txt", sha256=digest, size=10)
    return TreeArtifact(files=(file,), tree_sha256=content_sha256((file,)))


def _native(exit_code: int = 0) -> NativeEvidence:
    return NativeEvidence(
        parser_id="native-parser",
        parser_version="native-parser@1",
        source_sha256=ZERO_SHA,
        input_manifest_sha256=ONE_SHA,
        command=("native-parser", "data/content.txt"),
        exit_code=exit_code,
        stdout_sha256=ZERO_SHA,
        stderr_sha256=ZERO_SHA,
        compiler="fixture compiler",
    )


def _predicate(spec: ExternalCaseSpec, status: str) -> PredicateEvidence:
    return PredicateEvidence(
        predicate_id=spec.predicate_id,
        status=status,
        target_locators=spec.target_locators,
        evidence={"status": status},
    )


def _finding(
    defect_class: str = DefectClass.dangling_reference.value,
    *,
    entities: tuple[str, ...] = (TARGET,),
    status: str = "confirmed",
    finding_id: str = "checker@fixture#0",
) -> FindingEvidence:
    return FindingEvidence(
        finding_id=finding_id,
        defect_class=defect_class,
        status=status,
        entities=entities,
        evidence_sha256=TWO_SHA,
    )


def _inputs(
    *,
    case_id: str = "fixture.case",
    split: str = "verification",
    defect_class: DefectClass = DefectClass.dangling_reference,
) -> dict[str, object]:
    spec = _spec(case_id=case_id, split=split, defect_class=defect_class)
    return {
        "spec": spec,
        "before_tree": _tree(ZERO_SHA),
        "after_tree": _tree(ONE_SHA),
        "native_before": _native(),
        "native_after": _native(),
        "predicate_before": _predicate(spec, "violation"),
        "predicate_after": _predicate(spec, "clear"),
        "reader_version": READER_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "mapping_spec_sha256": ZERO_SHA,
        "expected_reader_version": READER_VERSION,
        "expected_adapter_version": ADAPTER_VERSION,
        "expected_mapping_spec_sha256": ZERO_SHA,
        "target_entity_ids": (TARGET,),
        "findings_before": (
            _finding(defect_class.value),
        ),
        "findings_after": (),
        "human_target": HumanTarget(
            patch_path="cases/fixture.case/upstream.patch",
            patch_sha256=ONE_SHA,
        ),
        "upstream_patch_sha256": ONE_SHA,
    }


def _mutated_inputs(mutation: str) -> dict[str, object]:
    inputs = _inputs()
    if mutation == "native_before_failed":
        inputs["native_before"] = _native(exit_code=2)
    elif mutation == "predicate_before_clear":
        spec = inputs["spec"]
        assert isinstance(spec, ExternalCaseSpec)
        inputs["predicate_before"] = _predicate(spec, "clear")
    elif mutation == "predicate_after_violation":
        spec = inputs["spec"]
        assert isinstance(spec, ExternalCaseSpec)
        inputs["predicate_after"] = _predicate(spec, "violation")
    elif mutation == "checker_before_miss":
        inputs["findings_before"] = ()
    elif mutation == "checker_after_hit":
        inputs["findings_after"] = (_finding(),)
    elif mutation == "mapping_hash_changed":
        inputs["mapping_spec_sha256"] = TWO_SHA
    elif mutation == "reader_version_changed":
        inputs["reader_version"] = "reader@2"
    elif mutation == "adapter_version_changed":
        inputs["adapter_version"] = "adapter@2"
    elif mutation == "human_patch_changed":
        inputs["upstream_patch_sha256"] = TWO_SHA
    elif mutation == "target_resolution_failed":
        inputs["target_entity_ids"] = ()
    else:  # pragma: no cover - test helper guard
        raise AssertionError(f"unknown mutation: {mutation}")
    return inputs


def test_complete_evidence_qualifies_and_binds_resolved_targets() -> None:
    evidence = qualify_case(**_inputs())

    assert evidence.qualification_status == "qualified"
    assert evidence.failure_reasons == ()
    assert evidence.target_entity_ids == (TARGET,)
    assert evidence.evidence_sha256 == content_sha256(
        evidence,
        exclude={"evidence_sha256"},
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "native_before_failed",
        "predicate_before_clear",
        "predicate_after_violation",
        "checker_before_miss",
        "checker_after_hit",
        "mapping_hash_changed",
        "reader_version_changed",
        "adapter_version_changed",
        "human_patch_changed",
        "target_resolution_failed",
    ],
)
def test_any_required_evidence_failure_remains_a_scored_miss(mutation: str) -> None:
    evidence = qualify_case(**_mutated_inputs(mutation))

    assert evidence.qualification_status == "miss"
    assert evidence.failure_reasons
    assert evidence.spec.case_id == "fixture.case"


def test_unrelated_after_finding_still_fails_clean_snapshot_gate() -> None:
    inputs = _inputs()
    inputs["findings_after"] = (
        _finding(
            DefectClass.dead_quest.value,
            entities=("quest:fixture:other",),
        ),
    )

    evidence = qualify_case(**inputs)

    assert evidence.qualification_status == "miss"
    assert "after_snapshot_not_clean" in evidence.failure_reasons


def test_unproven_before_finding_is_not_a_confirmed_detection() -> None:
    inputs = _inputs()
    inputs["findings_before"] = (_finding(status="unproven"),)

    evidence = qualify_case(**inputs)

    assert evidence.qualification_status == "miss"
    assert "before_expected_finding_missing" in evidence.failure_reasons


def test_scorer_separates_splits_keeps_misses_and_reports_after_fp() -> None:
    cases = []
    for defect_class in (DefectClass.dangling_reference, DefectClass.dead_quest):
        hit = qualify_case(
            **_inputs(
                case_id=f"{defect_class.value}.hit",
                defect_class=defect_class,
            )
        )
        miss_inputs = _inputs(
            case_id=f"{defect_class.value}.miss",
            defect_class=defect_class,
        )
        miss_inputs["native_before"] = _native(exit_code=1)
        if defect_class is DefectClass.dead_quest:
            miss_inputs["findings_after"] = (
                _finding(
                    DefectClass.dead_quest.value,
                    finding_id="checker@fixture#after",
                ),
            )
        cases.extend((hit, qualify_case(**miss_inputs)))

    development = qualify_case(
        **_inputs(
            case_id="development.hit",
            split="development",
            defect_class=DefectClass.dangling_reference,
        )
    )
    cases.append(development)

    score = score_external_cases(cases)

    assert [(metric.defect_class, metric.n, metric.k) for metric in score.verification] == [
        (DefectClass.dangling_reference, 2, 1),
        (DefectClass.dead_quest, 2, 1),
    ]
    assert [(metric.defect_class, metric.n, metric.k) for metric in score.development] == [
        (DefectClass.dangling_reference, 1, 1),
    ]
    assert all(metric.ci_low < metric.rate < metric.ci_high for metric in score.verification)
    assert score.after_oracle_fp.n == 5
    assert score.after_oracle_fp.count == 1
    assert score.after_oracle_fp.rate == pytest.approx(0.2)


def test_manifest_hash_binds_derived_external_scores() -> None:
    case = qualify_case(**_inputs())
    score = score_external_cases((case,))
    manifest = ExternalCorpusManifest.seal(
        schema_version="external-corpus-manifest@1",
        source_id="fixture_game",
        pinned_head=PINNED,
        repository_url="https://example.com/fixture-game.git",
        reader_version=READER_VERSION,
        adapter_version=ADAPTER_VERSION,
        mapping_spec_sha256=ZERO_SHA,
        cases=(case,),
        development=score.development,
        verification=score.verification,
        after_oracle_fp=score.after_oracle_fp,
    )

    payload = deepcopy(manifest.model_dump(mode="json"))
    payload["after_oracle_fp"]["count"] = 1
    payload["after_oracle_fp"]["rate"] = 1.0
    payload["after_oracle_fp"]["ci_low"] = 0.2
    payload["after_oracle_fp"]["ci_high"] = 1.0

    assert manifest.after_oracle_fp.count == 0
    with pytest.raises(ValueError, match="manifest_sha256"):
        ExternalCorpusManifest.model_validate(payload)
