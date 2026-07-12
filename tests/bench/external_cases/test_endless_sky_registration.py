from __future__ import annotations

import json
import re
from pathlib import Path

from gameforge.bench.external_cases.endless_sky_fixture import (
    ENDLESS_SKY_PINNED_HEAD,
    load_case_specs,
)


ROOT = Path(__file__).resolve().parents[3]
CORPUS = ROOT / "scenarios/external_cases/endless_sky"

EXPECTED = {
    ("dangling_reference", "development"): "02e6ded1e7cb9ef7a8e401e71c9accd6133a68b5",
    ("dangling_reference", "verification"): "61425f7538b33ed5bddd77ea9c29ffd7737a242b",
    ("cyclic_dependency", "development"): "2476129506e96086b00b09e1999dcb10ff8390fd",
    ("cyclic_dependency", "verification"): "95b5c4e95f715c2a13c201396d6dda5ea33d8cf7",
    ("unreachable_target", "development"): "9e437162fffef43da5f836d1f92bb265ccc75c52",
    ("unreachable_target", "verification"): "34383dd960f42de2537a06c2bb0ba3f35a8a73c0",
    ("dead_quest", "development"): "de8385df680ba81c70f13b380ef0b13070eba49b",
    ("dead_quest", "verification"): "9b29c95b99e67efbd1acda09a9994fe37405278e",
}

EXPECTED_TARGET_COUNTS = {
    "endless-sky.dangling-reference.development": 1,
    "endless-sky.dangling-reference.verification": 1,
    "endless-sky.cyclic-dependency.development": 1,
    "endless-sky.cyclic-dependency.verification": 1,
    "endless-sky.unreachable-target.development": 5,
    "endless-sky.unreachable-target.verification": 35,
    "endless-sky.dead-quest.development": 1,
    "endless-sky.dead-quest.verification": 1,
}


def test_registration_is_exact_balanced_and_canonical() -> None:
    registration = load_case_specs(CORPUS / "case-specs.json")
    specs = registration.cases

    assert registration.pinned_head == ENDLESS_SKY_PINNED_HEAD
    assert {(s.defect_class.value, s.split): s.after_commit for s in specs} == EXPECTED
    assert len({s.case_id for s in specs}) == 8
    assert {s.case_id: len(s.target_locators) for s in specs} == EXPECTED_TARGET_COUNTS
    assert all(s.changed_paths and s.target_locators for s in specs)
    assert (CORPUS / "case-specs.json").read_bytes().endswith(b"\n")


def test_every_target_is_a_changed_top_level_record() -> None:
    specs = load_case_specs(CORPUS / "case-specs.json").cases

    for spec in specs:
        assert {target.path for target in spec.target_locators} <= set(spec.changed_paths)
        assert {target.record_kind for target in spec.target_locators} <= {"mission", "effect"}


def test_mapping_spec_contains_semantics_not_frozen_case_identity() -> None:
    raw = (CORPUS / "mapping-spec.json").read_text(encoding="utf-8")
    payload = json.loads(raw)

    assert payload["schema_version"] == "endless-sky-mapping-spec@1"
    assert payload["reader_version"] == "endless-sky-reader@1"
    assert payload["adapter_version"] == "endless-sky-adapter@1"
    assert re.search(r"\b[0-9a-f]{40}\b", raw) is None
    assert "data/" not in raw
    for object_name in (
        "Lost Racer 3",
        "Care Package to South 3a",
        "Terraforming 7",
        "FWC Pug 1",
        "Hai-home",
    ):
        assert object_name not in raw
