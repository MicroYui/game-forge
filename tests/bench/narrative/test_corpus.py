from __future__ import annotations

from collections import Counter

import pytest

from gameforge.bench.narrative.contracts import NARRATIVE_CLASSES, to_agent_input
from gameforge.bench.narrative.corpus import (
    build_corpus,
    clean_family_counts,
    load_cases,
    load_manifest,
    positive_counts,
    validate_corpus_manifest,
    write_corpora,
)


def test_corpus_counts_are_exact_and_balanced():
    development = build_corpus("development")
    verification = build_corpus("verification")

    assert len(development) == 160
    assert positive_counts(development) == {item: 20 for item in NARRATIVE_CLASSES}
    assert clean_family_counts(development) == {item: 20 for item in NARRATIVE_CLASSES}

    assert len(verification) == 1_905
    assert positive_counts(verification) == {item: 381 for item in NARRATIVE_CLASSES}
    clean_counts = clean_family_counts(verification)
    assert sum(clean_counts.values()) == 381
    assert max(clean_counts.values()) - min(clean_counts.values()) <= 1
    assert tuple(clean_counts[item] for item in NARRATIVE_CLASSES) == (96, 95, 95, 95)


@pytest.mark.parametrize("split", ["development", "verification"])
def test_every_case_and_model_payload_is_unique_within_split(split):
    cases = build_corpus(split)
    case_ids = [case.case_id for case in cases]
    payloads = [to_agent_input(case).model_dump_json() for case in cases]

    assert len(case_ids) == len(set(case_ids))
    assert len(payloads) == len(set(payloads))
    assert case_ids == sorted(case_ids)


def test_development_and_verification_seed_domains_do_not_overlap():
    development = build_corpus("development")
    verification = build_corpus("verification")
    assert not ({case.seed for case in development} & {case.seed for case in verification})


def test_write_load_and_manifest_validation_are_byte_reproducible(tmp_path):
    first = write_corpora(tmp_path)
    development_bytes = (tmp_path / "development.jsonl").read_bytes()
    verification_bytes = (tmp_path / "verification.jsonl").read_bytes()
    manifest_bytes = (tmp_path / "corpus-manifest.json").read_bytes()

    second = write_corpora(tmp_path)

    assert second == first
    assert (tmp_path / "development.jsonl").read_bytes() == development_bytes
    assert (tmp_path / "verification.jsonl").read_bytes() == verification_bytes
    assert (tmp_path / "corpus-manifest.json").read_bytes() == manifest_bytes
    assert load_cases(tmp_path / "development.jsonl") == build_corpus("development")
    assert load_cases(tmp_path / "verification.jsonl") == build_corpus("verification")
    assert load_manifest(tmp_path / "corpus-manifest.json") == first
    validate_corpus_manifest(tmp_path, first)


def test_manifest_rejects_tampered_case_bytes(tmp_path):
    manifest = write_corpora(tmp_path)
    path = tmp_path / "development.jsonl"
    path.write_bytes(path.read_bytes().replace(b"archive", b"archives", 1))

    with pytest.raises(ValueError, match="sha256"):
        validate_corpus_manifest(tmp_path, manifest)


def test_each_split_has_expected_family_and_clean_status_matrix():
    for split in ("development", "verification"):
        cases = build_corpus(split)
        matrix = Counter((case.benchmark_family, case.is_clean) for case in cases)
        assert all(matrix[(item, False)] > 0 for item in NARRATIVE_CLASSES)
        assert all(matrix[(item, True)] > 0 for item in NARRATIVE_CLASSES)
