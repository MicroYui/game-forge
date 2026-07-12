"""Hash-bound contracts for replayable narrative benchmark evidence."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.bench.narrative.contracts import (
    NARRATIVE_CLASSES,
    NarrativeCase,
    content_sha256,
)
from gameforge.bench.taxonomy import DefectClass
from gameforge.contracts.agent_io import ConsistencyHint
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.model_router import ModelSnapshot
from gameforge.spine.stats import wilson_ci

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
RequestHash = Annotated[
    str,
    StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$"),
]
StableId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$",
    ),
]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Split = Literal["development", "verification"]
OutcomeStatus = Literal[
    "evaluated",
    "partial_parse_failure",
    "fallback",
    "cassette_miss",
    "runner_error",
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_float(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if not value.startswith("f:"):
        raise ValueError("metric float string must use the canonical f: prefix")
    raw = value.removeprefix("f:")
    try:
        decimal = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("metric contains an invalid canonical float") from exc
    canonical = format(decimal.normalize(), "f")
    if raw != canonical or not decimal.is_finite():
        raise ValueError("metric float string is not canonical and finite")
    return float(decimal)


def _validate_indexes(indexes: tuple[int, ...], hint_count: int, field: str) -> None:
    if indexes != tuple(sorted(set(indexes))):
        raise ValueError(f"{field} must be unique and sorted")
    if any(index < 0 or index >= hint_count for index in indexes):
        raise ValueError(f"{field} references an unknown hint")


class NarrativeCaseOutcome(_StrictModel):
    case_id: StableId
    case_sha256: Sha256
    protocol_sha256: Sha256
    status: OutcomeStatus
    request_hashes: tuple[RequestHash, ...]
    parse_failures: int = Field(ge=0)
    invalid_hint_items: int = Field(ge=0)
    hints: tuple[ConsistencyHint, ...]
    detected: bool
    false_positive: bool
    matched_hint_indexes: tuple[int, ...]
    constraint_match_indexes: tuple[int, ...]
    failure_reason: NonEmptyText | None = None
    outcome_sha256: Sha256

    @classmethod
    def seal(cls, **values: Any) -> NarrativeCaseOutcome:
        payload = dict(values)
        payload.pop("outcome_sha256", None)
        payload.setdefault("status", "evaluated")
        payload.setdefault("request_hashes", ())
        payload.setdefault("parse_failures", 0)
        payload.setdefault("invalid_hint_items", 0)
        payload.setdefault("hints", ())
        payload.setdefault("detected", False)
        payload.setdefault("false_positive", False)
        payload.setdefault("matched_hint_indexes", ())
        payload.setdefault("constraint_match_indexes", ())
        payload.setdefault("failure_reason", None)
        payload["outcome_sha256"] = content_sha256(payload)
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def validate_outcome(self) -> NarrativeCaseOutcome:
        if len(self.request_hashes) != len(set(self.request_hashes)):
            raise ValueError("request_hashes must not contain duplicates")
        _validate_indexes(
            self.matched_hint_indexes,
            len(self.hints),
            "matched_hint_indexes",
        )
        _validate_indexes(
            self.constraint_match_indexes,
            len(self.hints),
            "constraint_match_indexes",
        )
        if self.detected and self.false_positive:
            raise ValueError("an outcome cannot be both detected and false positive")
        terminal = self.status in {"fallback", "cassette_miss", "runner_error"}
        if terminal and (
            self.hints
            or self.detected
            or self.false_positive
            or self.matched_hint_indexes
            or self.constraint_match_indexes
        ):
            raise ValueError("terminal execution outcomes cannot contain scored hints")
        if terminal and self.failure_reason is None:
            raise ValueError("terminal execution outcomes require a failure_reason")
        if not terminal and self.failure_reason is not None:
            raise ValueError("evaluated outcomes cannot contain a failure_reason")
        expected = content_sha256(self, exclude={"outcome_sha256"})
        if self.outcome_sha256 != expected:
            raise ValueError("outcome_sha256 does not bind narrative case outcome")
        return self


class NarrativeClassMetric(_StrictModel):
    defect_class: DefectClass
    split: Split
    n: int = Field(gt=0)
    k: int = Field(ge=0)
    rate: float = Field(ge=0.0, le=1.0)
    ci_low: float = Field(ge=0.0, le=1.0)
    ci_high: float = Field(ge=0.0, le=1.0)
    ci_method: Literal["wilson95"] = "wilson95"

    @field_validator("rate", "ci_low", "ci_high", mode="before")
    @classmethod
    def parse_canonical_floats(cls, value: Any) -> Any:
        return _canonical_float(value)

    @model_validator(mode="after")
    def validate_metric(self) -> NarrativeClassMetric:
        if self.defect_class not in NARRATIVE_CLASSES:
            raise ValueError("narrative metric requires a narrative defect class")
        if self.k > self.n:
            raise ValueError("narrative metric k cannot exceed n")
        expected_rate = self.k / self.n
        expected_low, expected_high = wilson_ci(self.k, self.n)
        if abs(self.rate - expected_rate) > 1e-12:
            raise ValueError("narrative metric rate does not equal k/n")
        if (
            abs(self.ci_low - expected_low) > 1e-12
            or abs(self.ci_high - expected_high) > 1e-12
        ):
            raise ValueError("narrative metric does not use the Wilson95 interval")
        return self


class NarrativeFpMetric(_StrictModel):
    split: Split
    n: int = Field(ge=0)
    count: int = Field(ge=0)
    rate: float = Field(ge=0.0, le=1.0)
    ci_low: float = Field(ge=0.0, le=1.0)
    ci_high: float = Field(ge=0.0, le=1.0)
    ci_method: Literal["wilson95"] = "wilson95"

    @field_validator("rate", "ci_low", "ci_high", mode="before")
    @classmethod
    def parse_canonical_floats(cls, value: Any) -> Any:
        return _canonical_float(value)

    @model_validator(mode="after")
    def validate_metric(self) -> NarrativeFpMetric:
        if self.count > self.n:
            raise ValueError("narrative FP count cannot exceed n")
        expected_rate = self.count / self.n if self.n else 0.0
        expected_low, expected_high = wilson_ci(self.count, self.n)
        if abs(self.rate - expected_rate) > 1e-12:
            raise ValueError("narrative FP rate does not equal count/n")
        if (
            abs(self.ci_low - expected_low) > 1e-12
            or abs(self.ci_high - expected_high) > 1e-12
        ):
            raise ValueError("narrative FP metric does not use the Wilson95 interval")
        return self


class NarrativeScore(_StrictModel):
    by_class: tuple[NarrativeClassMetric, ...]
    clean_fp: NarrativeFpMetric


class NarrativeEvidenceManifest(_StrictModel):
    schema_version: Literal["narrative-evidence@1"] = "narrative-evidence@1"
    split: Split
    protocol_sha256: Sha256
    corpus_manifest_sha256: Sha256
    model_snapshot: ModelSnapshot
    outcomes: tuple[NarrativeCaseOutcome, ...]
    by_class: tuple[NarrativeClassMetric, ...]
    clean_fp: NarrativeFpMetric
    evidence_sha256: Sha256

    @classmethod
    def seal(cls, **values: Any) -> NarrativeEvidenceManifest:
        payload = dict(values)
        payload.pop("evidence_sha256", None)
        payload.setdefault("schema_version", "narrative-evidence@1")
        payload["evidence_sha256"] = content_sha256(payload)
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def validate_manifest(self) -> NarrativeEvidenceManifest:
        if not self.outcomes:
            raise ValueError("narrative evidence must contain outcomes")
        case_ids = tuple(item.case_id for item in self.outcomes)
        if case_ids != tuple(sorted(case_ids)):
            raise ValueError("narrative outcomes must be sorted by case ID")
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("narrative evidence contains duplicate case IDs")
        if any(item.protocol_sha256 != self.protocol_sha256 for item in self.outcomes):
            raise ValueError("outcome protocol_sha256 differs from evidence protocol")
        classes = tuple(item.defect_class for item in self.by_class)
        canonical_classes = tuple(item for item in NARRATIVE_CLASSES if item in classes)
        if classes != canonical_classes or len(classes) != len(set(classes)):
            raise ValueError("narrative metrics must use canonical unique class order")
        if any(item.split != self.split for item in self.by_class):
            raise ValueError("narrative class metric split differs from evidence split")
        if self.clean_fp.split != self.split:
            raise ValueError("narrative FP split differs from evidence split")
        expected = content_sha256(self, exclude={"evidence_sha256"})
        if self.evidence_sha256 != expected:
            raise ValueError("evidence_sha256 does not bind narrative evidence")
        return self


def seal_evidence_manifest(**values: Any) -> NarrativeEvidenceManifest:
    return NarrativeEvidenceManifest.seal(**values)


def canonical_evidence_bytes(manifest: NarrativeEvidenceManifest) -> bytes:
    return (canonical_json(manifest.model_dump(mode="json")) + "\n").encode("utf-8")


def validate_evidence_manifest(
    manifest: NarrativeEvidenceManifest,
    cases: Sequence[NarrativeCase],
    *,
    corpus_manifest_sha256: str,
    protocol_sha256: str,
    protocol_model_snapshot: ModelSnapshot,
) -> None:
    """Re-derive outcomes and metrics against the exact frozen denominator."""

    from gameforge.bench.narrative.score import score_case, score_outcomes

    if manifest.protocol_sha256 != protocol_sha256:
        raise ValueError("protocol_sha256 does not match the frozen protocol")
    if manifest.corpus_manifest_sha256 != corpus_manifest_sha256:
        raise ValueError(
            "corpus_manifest_sha256 does not match the frozen narrative corpus"
        )
    if manifest.model_snapshot.model_dump() != protocol_model_snapshot.model_dump():
        raise ValueError("evidence model snapshot differs from protocol model snapshot")

    case_values = tuple(cases)
    if not case_values or any(case.split != manifest.split for case in case_values):
        raise ValueError("evidence cases differ from the declared split")
    case_ids = [case.case_id for case in case_values]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("frozen case denominator contains duplicate case IDs")
    outcome_ids = [outcome.case_id for outcome in manifest.outcomes]
    if len(outcome_ids) != len(set(outcome_ids)):
        raise ValueError("narrative evidence contains duplicate case IDs")
    if set(outcome_ids) != set(case_ids) or len(outcome_ids) != len(case_ids):
        raise ValueError("narrative evidence denominator does not match frozen cases")

    outcomes_by_id = {item.case_id: item for item in manifest.outcomes}
    rebuilt: list[NarrativeCaseOutcome] = []
    for case in sorted(case_values, key=lambda item: item.case_id):
        outcome = outcomes_by_id[case.case_id]
        if outcome.case_sha256 != case.case_sha256:
            raise ValueError(f"case_sha256 mismatch for {case.case_id}")
        expected = score_case(
            case,
            outcome.hints,
            protocol_sha256=outcome.protocol_sha256,
            status=outcome.status,
            request_hashes=outcome.request_hashes,
            parse_failures=outcome.parse_failures,
            invalid_hint_items=outcome.invalid_hint_items,
            failure_reason=outcome.failure_reason,
        )
        if expected != outcome:
            raise ValueError(f"stored outcome fields do not rescore for {case.case_id}")
        rebuilt.append(expected)

    derived = score_outcomes(rebuilt, case_values)
    if derived.by_class != manifest.by_class or derived.clean_fp != manifest.clean_fp:
        raise ValueError("narrative evidence derived metrics do not match outcomes")
    expected_hash = content_sha256(manifest, exclude={"evidence_sha256"})
    if manifest.evidence_sha256 != expected_hash:
        raise ValueError("evidence_sha256 does not bind narrative evidence")


__all__ = [
    "NarrativeCaseOutcome",
    "NarrativeClassMetric",
    "NarrativeEvidenceManifest",
    "NarrativeFpMetric",
    "NarrativeScore",
    "OutcomeStatus",
    "canonical_evidence_bytes",
    "seal_evidence_manifest",
    "validate_evidence_manifest",
]
