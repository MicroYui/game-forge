from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from gameforge.bench.external_cases.contracts import (
    ExternalCaseEvidence,
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
from gameforge.bench.taxonomy import DefectClass


ZERO_SHA = "0" * 64
ONE_SHA = "1" * 64
BEFORE = "a" * 40
AFTER = "b" * 40
PINNED = "c" * 40


def _spec(**updates) -> ExternalCaseSpec:
    payload = {
        "schema_version": "external-case-spec@1",
        "case_id": "endless-sky.dangling.dev",
        "source_id": "endless_sky",
        "source_repository": "https://github.com/endless-sky/endless-sky.git",
        "license_id": "GPL-3.0-or-later",
        "before_commit": BEFORE,
        "after_commit": AFTER,
        "upstream_subject": "Fix missing reference",
        "upstream_pr": 10424,
        "changed_paths": ("data/example.txt",),
        "defect_class": DefectClass.dangling_reference,
        "target_locators": (
            TargetLocator(
                path="data/example.txt",
                record_kind="effect",
                record_name="example effect",
            ),
        ),
        "split": "development",
        "predicate_id": "reference_resolves",
    }
    payload.update(updates)
    return ExternalCaseSpec.model_validate(payload)


def _tree(digest: str) -> TreeArtifact:
    file = TreeFile(path="data/example.txt", sha256=digest, size=7)
    return TreeArtifact(files=(file,), tree_sha256=content_sha256((file,)))


def _native() -> NativeEvidence:
    return NativeEvidence(
        parser_id="endless-sky-datafile-native",
        parser_version="endless-sky-datafile-native@1",
        source_sha256=ZERO_SHA,
        input_manifest_sha256=ONE_SHA,
        command=("native-parser", "data/example.txt"),
        exit_code=0,
        stdout_sha256=ZERO_SHA,
        stderr_sha256=ZERO_SHA,
        compiler="Apple clang 17",
    )


def _predicate(status: str) -> PredicateEvidence:
    return PredicateEvidence(
        predicate_id="reference_resolves",
        status=status,
        target_locators=_spec().target_locators,
        evidence={"reference": "sound"},
    )


def _case() -> ExternalCaseEvidence:
    return ExternalCaseEvidence.seal(
        schema_version="external-case@1",
        spec=_spec(),
        before_tree=_tree(ZERO_SHA),
        after_tree=_tree(ONE_SHA),
        native_before=_native(),
        native_after=_native(),
        predicate_before=_predicate("violation"),
        predicate_after=_predicate("clear"),
        reader_version="endless-sky-reader@1",
        adapter_version="endless-sky-adapter@1",
        mapping_spec_sha256=ZERO_SHA,
        target_entity_ids=("effect:example",),
        findings_before=(
            FindingEvidence(
                finding_id="graph@case#0",
                defect_class="dangling_reference",
                status="confirmed",
                entities=("effect:example", "sound:missing"),
                evidence_sha256=ZERO_SHA,
            ),
        ),
        findings_after=(),
        human_target=HumanTarget(
            patch_path="cases/endless-sky.dangling.dev/upstream.patch",
            patch_sha256=ONE_SHA,
        ),
        qualification_status="qualified",
        failure_reasons=(),
    )


def _manifest() -> ExternalCorpusManifest:
    from gameforge.bench.external_cases.qualify import score_external_cases

    case = _case()
    score = score_external_cases((case,))
    return ExternalCorpusManifest.seal(
        schema_version="external-corpus-manifest@1",
        source_id="endless_sky",
        pinned_head=PINNED,
        repository_url="https://github.com/endless-sky/endless-sky.git",
        reader_version="endless-sky-reader@1",
        adapter_version="endless-sky-adapter@1",
        mapping_spec_sha256=ZERO_SHA,
        cases=(case,),
        development=score.development,
        verification=score.verification,
        after_oracle_fp=score.after_oracle_fp,
    )


def test_case_spec_requires_nonempty_targets_and_config_paths() -> None:
    with pytest.raises(ValidationError, match="target_locators"):
        _spec(target_locators=())

    with pytest.raises(ValidationError, match=r"\.txt"):
        _spec(changed_paths=("source/Mission.cpp",))


def test_case_spec_rejects_target_outside_changed_paths() -> None:
    with pytest.raises(ValidationError, match="changed_paths"):
        _spec(
            target_locators=(
                TargetLocator(
                    path="data/other.txt",
                    record_kind="effect",
                    record_name="example",
                ),
            )
        )


def test_case_evidence_hash_binds_nested_evidence() -> None:
    case = _case()
    assert case.evidence_sha256 == content_sha256(case, exclude={"evidence_sha256"})

    payload = case.model_dump(mode="json")
    payload["predicate_after"]["status"] = "violation"
    with pytest.raises(ValidationError, match="evidence_sha256"):
        ExternalCaseEvidence.model_validate(payload)


def test_manifest_hash_binds_every_case_and_rejects_tampering() -> None:
    manifest = _manifest()
    assert manifest.manifest_sha256 == content_sha256(
        manifest, exclude={"manifest_sha256"}
    )

    payload = deepcopy(manifest.model_dump(mode="json"))
    payload["cases"][0]["spec"]["upstream_subject"] = "tampered"
    with pytest.raises(ValidationError, match="evidence_sha256|manifest_sha256"):
        ExternalCorpusManifest.model_validate(payload)


def test_strict_contracts_reject_unknown_fields_and_invalid_hashes() -> None:
    payload = _spec().model_dump(mode="json")
    payload["reviewer_id"] = "not-part-of-lean-contract"
    with pytest.raises(ValidationError, match="reviewer_id"):
        ExternalCaseSpec.model_validate(payload)

    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        HumanTarget(patch_path="case.patch", patch_sha256="not-a-sha")
