"""Paired QA-hours scoring and hash-bound measured evidence."""

from __future__ import annotations

import hashlib
import statistics
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Literal, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.bench.hed.contracts import content_sha256
from gameforge.bench.qa.contracts import QA_ACTIVE_CAP_NS, QaSessionEvidence, load_session
from gameforge.bench.qa.protocol import QaProtocol
from gameforge.bench.stats import percentile, percentile_bootstrap_ci
from gameforge.bench.taxonomy import DefectClass
from gameforge.contracts.canonical import canonical_json
from gameforge.spine.stats import wilson_ci

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
StableId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$",
    ),
]
_NS_PER_MINUTE = 60_000_000_000
_ACTIVE_CAP_MINUTES = QA_ACTIVE_CAP_NS / _NS_PER_MINUTE


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _canonical_float(value: Any) -> Any:
    if value is None or not isinstance(value, str):
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


class QaBinaryMetric(_StrictModel):
    n: int = Field(ge=0, le=4)
    k: int = Field(ge=0, le=4)
    rate: float = Field(ge=0.0, le=1.0)
    ci_low: float = Field(ge=0.0, le=1.0)
    ci_high: float = Field(ge=0.0, le=1.0)
    ci_method: Literal["wilson95"] = "wilson95"

    @field_validator("rate", "ci_low", "ci_high", mode="before")
    @classmethod
    def parse_canonical_floats(cls, value: Any) -> Any:
        return _canonical_float(value)

    @model_validator(mode="after")
    def validate_metric(self) -> QaBinaryMetric:
        if self.k > self.n:
            raise ValueError("QA success k cannot exceed n")
        expected_rate = self.k / self.n if self.n else 0.0
        low, high = wilson_ci(self.k, self.n)
        if abs(self.rate - expected_rate) > 1e-12:
            raise ValueError("QA success rate does not equal k/n")
        if abs(self.ci_low - low) > 1e-12 or abs(self.ci_high - high) > 1e-12:
            raise ValueError("QA success metric does not use Wilson95")
        return self


def _binary_metric(values: Sequence[bool]) -> QaBinaryMetric:
    n = len(values)
    k = sum(values)
    low, high = wilson_ci(k, n)
    return QaBinaryMetric(
        n=n,
        k=k,
        rate=k / n if n else 0.0,
        ci_low=low,
        ci_high=high,
    )


class QaPairOutcome(_StrictModel):
    pair_id: StableId
    defect_class: DefectClass
    manual_session_id: StableId
    assisted_session_id: StableId
    manual_active_minutes: float = Field(gt=0.0)
    assisted_active_minutes: float = Field(ge=0.0)
    manual_scored_minutes: float = Field(gt=0.0)
    assisted_scored_minutes: float = Field(ge=0.0)
    saved_minutes: float
    saved_fraction: float
    manual_correct: bool
    assisted_correct: bool
    pair_sha256: Sha256

    @field_validator(
        "manual_active_minutes",
        "assisted_active_minutes",
        "manual_scored_minutes",
        "assisted_scored_minutes",
        "saved_minutes",
        "saved_fraction",
        mode="before",
    )
    @classmethod
    def parse_canonical_floats(cls, value: Any) -> Any:
        return _canonical_float(value)

    @classmethod
    def seal(cls, **values: Any) -> QaPairOutcome:
        payload = dict(values)
        payload.pop("pair_sha256", None)
        payload["pair_sha256"] = content_sha256(payload)
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def validate_pair(self) -> QaPairOutcome:
        expected_manual = self.manual_active_minutes if self.manual_correct else _ACTIVE_CAP_MINUTES
        expected_assisted = (
            self.assisted_active_minutes if self.assisted_correct else _ACTIVE_CAP_MINUTES
        )
        if abs(self.manual_scored_minutes - expected_manual) > 1e-12:
            raise ValueError("manual scored minutes do not follow the correctness rule")
        if abs(self.assisted_scored_minutes - expected_assisted) > 1e-12:
            raise ValueError("assisted scored minutes do not follow the correctness rule")
        expected_saved = self.manual_scored_minutes - self.assisted_scored_minutes
        expected_fraction = expected_saved / self.manual_scored_minutes
        if abs(self.saved_minutes - expected_saved) > 1e-12:
            raise ValueError("saved_minutes does not rederive from arm times")
        if abs(self.saved_fraction - expected_fraction) > 1e-12:
            raise ValueError("saved_fraction does not use manual time as denominator")
        expected_hash = content_sha256(self, exclude={"pair_sha256"})
        if self.pair_sha256 != expected_hash:
            raise ValueError("pair_sha256 does not bind QA pair outcome")
        return self


class QaScore(_StrictModel):
    planned_pairs: Literal[4] = 4
    evaluated_pairs: int = Field(ge=0, le=4)
    protocol_failure_pairs: int = Field(ge=0, le=4)
    time_scoring: Literal["incorrect_uses_active_cap"] = "incorrect_uses_active_cap"
    pairs: tuple[QaPairOutcome, ...]
    mean_saved_minutes: float | None = None
    median_saved_minutes: float | None = None
    saved_minutes_ci_low: float | None = None
    saved_minutes_ci_high: float | None = None
    mean_saved_fraction: float | None = None
    median_saved_fraction: float | None = None
    saved_fraction_ci_low: float | None = None
    saved_fraction_ci_high: float | None = None
    ci_method: Literal["percentile-bootstrap95"] | None = None
    manual_success: QaBinaryMetric
    assisted_success: QaBinaryMetric
    conclusion: Literal["savings", "inconclusive", "negative", "failed"]

    @field_validator(
        "mean_saved_minutes",
        "median_saved_minutes",
        "saved_minutes_ci_low",
        "saved_minutes_ci_high",
        "mean_saved_fraction",
        "median_saved_fraction",
        "saved_fraction_ci_low",
        "saved_fraction_ci_high",
        mode="before",
    )
    @classmethod
    def parse_canonical_floats(cls, value: Any) -> Any:
        return _canonical_float(value)

    @model_validator(mode="after")
    def validate_score(self) -> QaScore:
        if self.evaluated_pairs + self.protocol_failure_pairs != self.planned_pairs:
            raise ValueError("QA pair counts must cover all four planned pairs")
        if len(self.pairs) != self.evaluated_pairs:
            raise ValueError("QA evaluated_pairs must equal stored pair outcomes")
        pair_ids = tuple(item.pair_id for item in self.pairs)
        if pair_ids != tuple(sorted(set(pair_ids))):
            raise ValueError("QA pair outcomes must be unique and sorted")
        if self.manual_success.n != self.evaluated_pairs:
            raise ValueError("manual success denominator differs from evaluated pairs")
        if self.assisted_success.n != self.evaluated_pairs:
            raise ValueError("assisted success denominator differs from evaluated pairs")
        statistics_values = (
            self.mean_saved_minutes,
            self.median_saved_minutes,
            self.saved_minutes_ci_low,
            self.saved_minutes_ci_high,
            self.mean_saved_fraction,
            self.median_saved_fraction,
            self.saved_fraction_ci_low,
            self.saved_fraction_ci_high,
        )
        if self.evaluated_pairs:
            if any(item is None for item in statistics_values) or self.ci_method is None:
                raise ValueError("evaluated QA pairs require complete statistics")
        elif any(item is not None for item in statistics_values) or self.ci_method is not None:
            raise ValueError("unevaluated QA score cannot carry statistics")
        expected_conclusion = _conclusion(
            self.evaluated_pairs,
            self.protocol_failure_pairs,
            self.mean_saved_minutes,
            self.saved_minutes_ci_low,
            self.manual_success,
            self.assisted_success,
        )
        if self.conclusion != expected_conclusion:
            raise ValueError("QA conclusion does not follow the frozen claim rule")
        return self


def _conclusion(
    evaluated: int,
    failures: int,
    mean_saved: float | None,
    ci_low: float | None,
    manual: QaBinaryMetric,
    assisted: QaBinaryMetric,
) -> str:
    if failures or evaluated != 4:
        return "failed"
    if mean_saved is not None and mean_saved < 0:
        return "negative"
    if ci_low is not None and ci_low > 0 and assisted.rate >= manual.rate:
        return "savings"
    return "inconclusive"


def _session_matches_spec(session: QaSessionEvidence, spec, protocol: QaProtocol) -> bool:  # noqa: ANN001
    return (
        session.protocol_sha256 == protocol.protocol_sha256
        and session.session_id == spec.session_id
        and session.participant_id == protocol.participant_id
        and session.case_id == spec.case_id
        and session.pair_id == spec.pair_id
        and session.arm == spec.arm
        and session.order == spec.order
        and session.protocol_valid
        and session.evidence_sha256 == content_sha256(session, exclude={"evidence_sha256"})
    )


def score_sessions(
    protocol: QaProtocol,
    sessions: Sequence[QaSessionEvidence],
) -> QaScore:
    rows = tuple(sessions)
    expected_ids = {item.session_id for item in protocol.sessions}
    supplied_ids = [item.session_id for item in rows]
    if (
        len(rows) != 8
        or len(supplied_ids) != len(set(supplied_ids))
        or set(supplied_ids) != expected_ids
    ):
        empty = _binary_metric([])
        return QaScore(
            evaluated_pairs=0,
            protocol_failure_pairs=4,
            pairs=(),
            manual_success=empty,
            assisted_success=empty,
            conclusion="failed",
        )
    by_session: dict[str, list[QaSessionEvidence]] = {}
    for session in rows:
        by_session.setdefault(session.session_id, []).append(session)

    pairs: list[QaPairOutcome] = []
    failure_pairs = 0
    for pair_id in sorted({item.pair_id for item in protocol.sessions}):
        specs = [item for item in protocol.sessions if item.pair_id == pair_id]
        actual: list[QaSessionEvidence] = []
        valid = len(specs) == 2
        for spec in specs:
            matches = by_session.get(spec.session_id, [])
            if len(matches) != 1 or not _session_matches_spec(matches[0], spec, protocol):
                valid = False
                continue
            actual.append(matches[0])
        if not valid or len(actual) != 2:
            failure_pairs += 1
            continue
        manual = next(item for item in actual if item.arm == "manual")
        assisted = next(item for item in actual if item.arm == "assisted")
        if manual.capped_active_ns <= 0:
            failure_pairs += 1
            continue
        manual_active_minutes = manual.capped_active_ns / _NS_PER_MINUTE
        assisted_active_minutes = assisted.capped_active_ns / _NS_PER_MINUTE
        manual_scored_minutes = (
            manual_active_minutes if manual.verdict.correct else _ACTIVE_CAP_MINUTES
        )
        assisted_scored_minutes = (
            assisted_active_minutes if assisted.verdict.correct else _ACTIVE_CAP_MINUTES
        )
        saved_minutes = manual_scored_minutes - assisted_scored_minutes
        pairs.append(
            QaPairOutcome.seal(
                pair_id=pair_id,
                defect_class=specs[0].defect_class,
                manual_session_id=manual.session_id,
                assisted_session_id=assisted.session_id,
                manual_active_minutes=manual_active_minutes,
                assisted_active_minutes=assisted_active_minutes,
                manual_scored_minutes=manual_scored_minutes,
                assisted_scored_minutes=assisted_scored_minutes,
                saved_minutes=saved_minutes,
                saved_fraction=saved_minutes / manual_scored_minutes,
                manual_correct=manual.verdict.correct,
                assisted_correct=assisted.verdict.correct,
            )
        )

    evaluated = len(pairs)
    manual_success = _binary_metric([item.manual_correct for item in pairs])
    assisted_success = _binary_metric([item.assisted_correct for item in pairs])
    if pairs:
        saved_minutes = [item.saved_minutes for item in pairs]
        saved_fractions = [item.saved_fraction for item in pairs]
        minutes_interval = percentile_bootstrap_ci(saved_minutes, statistics.fmean)
        fraction_interval = percentile_bootstrap_ci(saved_fractions, statistics.fmean)
        mean_minutes = statistics.fmean(saved_minutes)
        ci_low = minutes_interval.low
        values: dict[str, Any] = {
            "mean_saved_minutes": mean_minutes,
            "median_saved_minutes": percentile(saved_minutes, 0.5),
            "saved_minutes_ci_low": ci_low,
            "saved_minutes_ci_high": minutes_interval.high,
            "mean_saved_fraction": statistics.fmean(saved_fractions),
            "median_saved_fraction": percentile(saved_fractions, 0.5),
            "saved_fraction_ci_low": fraction_interval.low,
            "saved_fraction_ci_high": fraction_interval.high,
            "ci_method": minutes_interval.method,
        }
    else:
        mean_minutes = None
        ci_low = None
        values = {}
    return QaScore(
        evaluated_pairs=evaluated,
        protocol_failure_pairs=failure_pairs,
        pairs=tuple(pairs),
        manual_success=manual_success,
        assisted_success=assisted_success,
        conclusion=_conclusion(
            evaluated,
            failure_pairs,
            mean_minutes,
            ci_low,
            manual_success,
            assisted_success,
        ),
        **values,
    )


class QaEvidenceManifest(_StrictModel):
    schema_version: Literal["qa-evidence@2"] = "qa-evidence@2"
    protocol_sha256: Sha256
    participant_id: StableId = "participant-01"
    sessions: tuple[QaSessionEvidence, ...]
    score: QaScore
    evidence_sha256: Sha256

    @model_validator(mode="after")
    def validate_manifest(self) -> QaEvidenceManifest:
        if len(self.sessions) != 8:
            raise ValueError("QA evidence requires exactly eight sessions")
        orders = tuple(item.order for item in self.sessions)
        if orders != tuple(range(1, 9)):
            raise ValueError("QA evidence sessions must use frozen order")
        if len({item.session_id for item in self.sessions}) != 8:
            raise ValueError("QA evidence contains duplicate sessions")
        if any(item.protocol_sha256 != self.protocol_sha256 for item in self.sessions):
            raise ValueError("QA session protocol hash differs from evidence")
        if any(item.participant_id != self.participant_id for item in self.sessions):
            raise ValueError("QA session participant differs from evidence")
        expected_hash = content_sha256(self, exclude={"evidence_sha256"})
        if self.evidence_sha256 != expected_hash:
            raise ValueError("evidence_sha256 does not bind QA evidence")
        return self


def seal_qa_evidence(
    protocol: QaProtocol,
    sessions: Sequence[QaSessionEvidence],
) -> QaEvidenceManifest:
    ordered = tuple(sorted(sessions, key=lambda item: item.order))
    payload = {
        "schema_version": "qa-evidence@2",
        "protocol_sha256": protocol.protocol_sha256,
        "participant_id": protocol.participant_id,
        "sessions": ordered,
        "score": score_sessions(protocol, ordered),
    }
    payload["evidence_sha256"] = content_sha256(payload)
    evidence = QaEvidenceManifest.model_validate(payload)
    if evidence.score != score_sessions(protocol, evidence.sessions):
        raise ValueError("QA evidence score does not rederive from sessions")
    return evidence


def canonical_evidence_bytes(evidence: QaEvidenceManifest) -> bytes:
    return (canonical_json(evidence.model_dump(mode="json")) + "\n").encode("utf-8")


def load_evidence(path: str | Path) -> QaEvidenceManifest:
    raw = Path(path).read_bytes()
    evidence = QaEvidenceManifest.model_validate_json(raw)
    if canonical_evidence_bytes(evidence) != raw:
        raise ValueError("QA evidence is not canonical JSON")
    return evidence


def write_evidence(path: str | Path, evidence: QaEvidenceManifest) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical_evidence_bytes(evidence))


def validate_qa_evidence(
    evidence: QaEvidenceManifest,
    protocol: QaProtocol,
    artifact_root: str | Path,
) -> None:
    if evidence.protocol_sha256 != protocol.protocol_sha256:
        raise ValueError("QA evidence protocol hash mismatch")
    if evidence.participant_id != protocol.participant_id:
        raise ValueError("QA evidence participant mismatch")
    if evidence.score != score_sessions(protocol, evidence.sessions):
        raise ValueError("QA evidence score does not rederive")
    root = Path(artifact_root).resolve(strict=True)
    expected_session_names = {f"{session.session_id}.json" for session in evidence.sessions}
    expected_patch_names = {f"{session.session_id}.patch" for session in evidence.sessions}
    for session in evidence.sessions:
        expected_patch_path = f"qa-patches/{session.session_id}.patch"
        if session.final_patch_path != expected_patch_path:
            raise ValueError(f"QA final patch path mismatch for {session.session_id}")
    session_root = root / "qa-sessions"
    patch_root = root / "qa-patches"
    if (
        not session_root.is_dir()
        or not patch_root.is_dir()
        or {path.name for path in session_root.iterdir()} != expected_session_names
        or {path.name for path in patch_root.iterdir()} != expected_patch_names
    ):
        raise ValueError("QA session and patch artifact sets must exactly match evidence")
    for session in evidence.sessions:
        session_path = (root / "qa-sessions" / f"{session.session_id}.json").resolve(strict=True)
        if not session_path.is_file() or not session_path.is_relative_to(root):
            raise ValueError("QA session evidence is outside the artifact root")
        if load_session(session_path) != session:
            raise ValueError(f"QA stored session mismatch for {session.session_id}")
        patch_path = (root / session.final_patch_path).resolve(strict=True)
        if not patch_path.is_file() or not patch_path.is_relative_to(root):
            raise ValueError("QA final patch is outside the artifact root")
        if hashlib.sha256(patch_path.read_bytes()).hexdigest() != session.final_patch_sha256:
            raise ValueError(f"QA final patch hash mismatch for {session.session_id}")
    if evidence.evidence_sha256 != content_sha256(
        evidence,
        exclude={"evidence_sha256"},
    ):
        raise ValueError("evidence_sha256 does not bind QA evidence")


__all__ = [
    "QaBinaryMetric",
    "QaEvidenceManifest",
    "QaPairOutcome",
    "QaScore",
    "canonical_evidence_bytes",
    "content_sha256",
    "load_evidence",
    "score_sessions",
    "seal_qa_evidence",
    "validate_qa_evidence",
    "write_evidence",
]
