"""Frozen development and verification corpora for narrative evaluation."""

from __future__ import annotations

import argparse
import hashlib
from collections import Counter
from pathlib import Path
from typing import Annotated, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from gameforge.bench.narrative.contracts import (
    NARRATIVE_CLASSES,
    NarrativeCase,
    canonical_case_bytes,
    content_sha256,
    to_agent_input,
)
from gameforge.bench.narrative.generator import GENERATOR_VERSION, generate_case
from gameforge.bench.narrative.oracle import ORACLE_VERSION
from gameforge.bench.narrative.renderer import RENDERER_VERSION
from gameforge.bench.taxonomy import DefectClass
from gameforge.contracts.canonical import canonical_json

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Split = Literal["development", "verification"]

_POSITIVE_COUNTS: dict[str, int] = {"development": 20, "verification": 381}
_CLEAN_COUNTS: dict[str, tuple[int, ...]] = {
    "development": (20, 20, 20, 20),
    "verification": (96, 95, 95, 95),
}
_FILE_NAMES: dict[str, str] = {
    "development": "development.jsonl",
    "verification": "verification.jsonl",
}


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NarrativeClassCount(_StrictModel):
    defect_class: DefectClass
    count: int = Field(ge=0)


class NarrativeSplitSummary(_StrictModel):
    split: Split
    total_cases: int = Field(gt=0)
    positive_counts: tuple[NarrativeClassCount, ...]
    clean_family_counts: tuple[NarrativeClassCount, ...]

    @model_validator(mode="after")
    def validate_counts(self) -> NarrativeSplitSummary:
        expected = tuple(NARRATIVE_CLASSES)
        if tuple(item.defect_class for item in self.positive_counts) != expected:
            raise ValueError("positive count classes must use canonical narrative order")
        if tuple(item.defect_class for item in self.clean_family_counts) != expected:
            raise ValueError("clean count classes must use canonical narrative order")
        derived_total = sum(item.count for item in self.positive_counts) + sum(
            item.count for item in self.clean_family_counts
        )
        if self.total_cases != derived_total:
            raise ValueError("split total does not match narrative class counts")
        return self


class NarrativeCorpusFile(_StrictModel):
    split: Split
    path: Literal["development.jsonl", "verification.jsonl"]
    case_count: int = Field(gt=0)
    sha256: Sha256

    @model_validator(mode="after")
    def validate_path(self) -> NarrativeCorpusFile:
        if self.path != _FILE_NAMES[self.split]:
            raise ValueError("corpus path does not match its split")
        return self


class NarrativeCorpusManifest(_StrictModel):
    schema_version: Literal["narrative-corpus-manifest@1"] = (
        "narrative-corpus-manifest@1"
    )
    generator_version: Literal["narrative-generator@1"] = GENERATOR_VERSION
    renderer_version: Literal["narrative-renderer@1"] = RENDERER_VERSION
    oracle_version: Literal["narrative-oracle@1"] = ORACLE_VERSION
    files: tuple[NarrativeCorpusFile, ...]
    summaries: tuple[NarrativeSplitSummary, ...]
    manifest_sha256: Sha256

    @classmethod
    def seal(cls, **values: object) -> NarrativeCorpusManifest:
        payload = dict(values)
        payload.pop("manifest_sha256", None)
        payload.setdefault("schema_version", "narrative-corpus-manifest@1")
        payload.setdefault("generator_version", GENERATOR_VERSION)
        payload.setdefault("renderer_version", RENDERER_VERSION)
        payload.setdefault("oracle_version", ORACLE_VERSION)
        payload["manifest_sha256"] = content_sha256(payload)
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def validate_manifest(self) -> NarrativeCorpusManifest:
        splits = ("development", "verification")
        if tuple(item.split for item in self.files) != splits:
            raise ValueError("manifest files must use canonical split order")
        if tuple(item.split for item in self.summaries) != splits:
            raise ValueError("manifest summaries must use canonical split order")
        for file_entry, summary in zip(self.files, self.summaries, strict=True):
            if file_entry.case_count != summary.total_cases:
                raise ValueError("manifest file count does not match split summary")
        expected = content_sha256(self, exclude={"manifest_sha256"})
        if self.manifest_sha256 != expected:
            raise ValueError("manifest_sha256 does not bind narrative corpus manifest")
        return self


def _stable_seed(
    split: Split,
    defect_class: DefectClass,
    is_clean: bool,
    index: int,
) -> int:
    label = (
        f"{GENERATOR_VERSION}|{split}|{defect_class.value}|"
        f"{'clean' if is_clean else 'positive'}|{index}"
    )
    value = int.from_bytes(hashlib.sha256(label.encode()).digest()[:8], "big")
    low_bits = value & ((1 << 62) - 1)
    return low_bits if split == "development" else (1 << 62) | low_bits


def _case_id(
    split: Split,
    defect_class: DefectClass,
    is_clean: bool,
    index: int,
) -> str:
    label = (
        f"narrative-case|{split}|{defect_class.value}|"
        f"{'clean' if is_clean else 'positive'}|{index}"
    )
    return f"nv-{hashlib.sha256(label.encode()).hexdigest()[:24]}"


def build_corpus(split: Split) -> tuple[NarrativeCase, ...]:
    if split not in _POSITIVE_COUNTS:
        raise ValueError("narrative split must be development or verification")
    cases: list[NarrativeCase] = []
    positive_n = _POSITIVE_COUNTS[split]
    clean_ns = _CLEAN_COUNTS[split]
    for family_index, defect_class in enumerate(NARRATIVE_CLASSES):
        for index in range(positive_n):
            cases.append(
                generate_case(
                    split=split,
                    defect_class=defect_class,
                    is_clean=False,
                    seed=_stable_seed(split, defect_class, False, index),
                    case_id=_case_id(split, defect_class, False, index),
                )
            )
        for index in range(clean_ns[family_index]):
            cases.append(
                generate_case(
                    split=split,
                    defect_class=defect_class,
                    is_clean=True,
                    seed=_stable_seed(split, defect_class, True, index),
                    case_id=_case_id(split, defect_class, True, index),
                )
            )
    cases.sort(key=lambda item: item.case_id)
    payloads = [to_agent_input(case).model_dump_json() for case in cases]
    if len(payloads) != len(set(payloads)):
        raise ValueError(f"{split} narrative corpus contains repeated model payloads")
    return tuple(cases)


def build_corpora() -> dict[Split, tuple[NarrativeCase, ...]]:
    return {
        "development": build_corpus("development"),
        "verification": build_corpus("verification"),
    }


def positive_counts(cases: Sequence[NarrativeCase]) -> dict[DefectClass, int]:
    counts = Counter(
        case.defect_class for case in cases if not case.is_clean and case.defect_class
    )
    return {item: counts[item] for item in NARRATIVE_CLASSES}


def clean_family_counts(cases: Sequence[NarrativeCase]) -> dict[DefectClass, int]:
    counts = Counter(case.benchmark_family for case in cases if case.is_clean)
    return {item: counts[item] for item in NARRATIVE_CLASSES}


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _summary(split: Split, cases: Sequence[NarrativeCase]) -> NarrativeSplitSummary:
    positives = positive_counts(cases)
    clean = clean_family_counts(cases)
    return NarrativeSplitSummary(
        split=split,
        total_cases=len(cases),
        positive_counts=tuple(
            NarrativeClassCount(defect_class=item, count=positives[item])
            for item in NARRATIVE_CLASSES
        ),
        clean_family_counts=tuple(
            NarrativeClassCount(defect_class=item, count=clean[item])
            for item in NARRATIVE_CLASSES
        ),
    )


def _manifest_bytes(manifest: NarrativeCorpusManifest) -> bytes:
    return (canonical_json(manifest.model_dump(mode="json")) + "\n").encode("utf-8")


def write_corpora(root: str | Path) -> NarrativeCorpusManifest:
    destination = Path(root)
    destination.mkdir(parents=True, exist_ok=True)
    corpora = build_corpora()
    file_entries: list[NarrativeCorpusFile] = []
    summaries: list[NarrativeSplitSummary] = []
    for split in ("development", "verification"):
        cases = corpora[split]
        raw = b"".join(canonical_case_bytes(case) for case in cases)
        path = destination / _FILE_NAMES[split]
        path.write_bytes(raw)
        file_entries.append(
            NarrativeCorpusFile(
                split=split,
                path=path.name,
                case_count=len(cases),
                sha256=_sha256(raw),
            )
        )
        summaries.append(_summary(split, cases))
    manifest = NarrativeCorpusManifest.seal(
        files=tuple(file_entries),
        summaries=tuple(summaries),
    )
    (destination / "corpus-manifest.json").write_bytes(_manifest_bytes(manifest))
    validate_corpus_manifest(destination, manifest)
    return manifest


def load_cases(path: str | Path) -> tuple[NarrativeCase, ...]:
    raw = Path(path).read_bytes()
    if not raw or not raw.endswith(b"\n"):
        raise ValueError("narrative corpus must be nonempty canonical JSONL")
    cases = tuple(
        NarrativeCase.model_validate_json(line)
        for line in raw.splitlines()
        if line
    )
    if b"".join(canonical_case_bytes(case) for case in cases) != raw:
        raise ValueError("narrative corpus is not canonical JSONL")
    return cases


def load_manifest(path: str | Path) -> NarrativeCorpusManifest:
    raw = Path(path).read_bytes()
    manifest = NarrativeCorpusManifest.model_validate_json(raw)
    if _manifest_bytes(manifest) != raw:
        raise ValueError("narrative corpus manifest is not canonical JSON")
    return manifest


def validate_corpus_manifest(
    root: str | Path,
    manifest: NarrativeCorpusManifest | None = None,
) -> None:
    destination = Path(root)
    bound = manifest or load_manifest(destination / "corpus-manifest.json")
    for file_entry, summary in zip(bound.files, bound.summaries, strict=True):
        path = destination / file_entry.path
        raw = path.read_bytes()
        if _sha256(raw) != file_entry.sha256:
            raise ValueError(f"sha256 mismatch for {file_entry.path}")
        cases = load_cases(path)
        if len(cases) != file_entry.case_count:
            raise ValueError(f"case count mismatch for {file_entry.path}")
        if any(case.split != file_entry.split for case in cases):
            raise ValueError(f"split mismatch in {file_entry.path}")
        if any(
            (
                case.generator_version != bound.generator_version
                or case.renderer_version != bound.renderer_version
                or case.oracle_version != bound.oracle_version
            )
            for case in cases
        ):
            raise ValueError(f"version mismatch in {file_entry.path}")
        if _summary(file_entry.split, cases) != summary:
            raise ValueError(f"derived counts mismatch for {file_entry.path}")


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", type=Path, required=True)
    args = parser.parse_args()
    manifest = write_corpora(args.write)
    print(manifest.manifest_sha256)


if __name__ == "__main__":
    _main()


__all__ = [
    "NarrativeCorpusManifest",
    "build_corpus",
    "build_corpora",
    "clean_family_counts",
    "load_cases",
    "load_manifest",
    "positive_counts",
    "validate_corpus_manifest",
    "write_corpora",
]
