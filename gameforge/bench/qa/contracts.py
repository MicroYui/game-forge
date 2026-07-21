"""Strict timer, verdict, and session contracts for QA-hours evidence."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.bench.hed.contracts import content_sha256
from gameforge.bench.taxonomy import DefectClass
from gameforge.contracts.canonical import canonical_json

QA_ACTIVE_CAP_NS = 480_000_000_000
QA_TOTAL_ACTIVE_CAP_NS = 3_840_000_000_000

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
StableId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$",
    ),
]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
QaArm = Literal["manual", "assisted"]
QaSplit = Literal["development", "verification"]
FindingKey = tuple[NonEmptyStr, tuple[NonEmptyStr, ...]]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QaSessionSpec(_StrictModel):
    session_id: StableId
    pair_id: StableId
    case_id: StableId
    defect_class: DefectClass
    split: QaSplit
    arm: QaArm
    order: int = Field(ge=1, le=8)


class QaEvent(_StrictModel):
    kind: Literal["start", "pause", "resume", "finish"]
    monotonic_ns: int = Field(ge=0)


def _validate_finding_keys(values: tuple[FindingKey, ...]) -> None:
    normalized: list[FindingKey] = []
    for defect_class, entities in values:
        if entities != tuple(sorted(set(entities))):
            raise ValueError("Finding-key entities must be unique and sorted")
        normalized.append((defect_class, entities))
    if tuple(normalized) != tuple(sorted(set(normalized))):
        raise ValueError("new deterministic Finding keys must be unique and sorted")


class QaCorrectnessVerdict(_StrictModel):
    schema_version: Literal["qa-correctness-verdict@1"] = "qa-correctness-verdict@1"
    correct: bool
    reader_round_trip: bool
    native_exit_code: int | None = None
    predicate_status: Literal["violation", "clear", "unproven"]
    target_finding_clear: bool
    target_entities_preserved: bool
    new_deterministic_findings: tuple[FindingKey, ...]
    submitted_tree_sha256: Sha256 | None = None
    failure_reason: NonEmptyStr | None = None
    verdict_sha256: Sha256

    @model_validator(mode="after")
    def validate_verdict(self) -> QaCorrectnessVerdict:
        _validate_finding_keys(self.new_deterministic_findings)
        expected_correct = (
            self.reader_round_trip
            and self.native_exit_code == 0
            and self.predicate_status == "clear"
            and self.target_finding_clear
            and self.target_entities_preserved
            and not self.new_deterministic_findings
            and self.submitted_tree_sha256 is not None
        )
        if self.correct != expected_correct:
            raise ValueError("correct does not rederive from the shared QA oracle")
        if self.correct and self.failure_reason is not None:
            raise ValueError("correct verdict cannot carry a failure_reason")
        if not self.correct and self.failure_reason is None:
            raise ValueError("incorrect verdict requires a failure_reason")
        expected_hash = content_sha256(self, exclude={"verdict_sha256"})
        if self.verdict_sha256 != expected_hash:
            raise ValueError("verdict_sha256 does not bind QA correctness verdict")
        return self


def seal_qa_verdict(verdict: Any | None = None, **values: Any) -> QaCorrectnessVerdict:
    payload = dict(values)
    if verdict is not None:
        if payload:
            raise ValueError("pass either a submission verdict or keyword values")
        payload = {
            "correct": verdict.correct,
            "reader_round_trip": verdict.reader_round_trip,
            "native_exit_code": verdict.native_exit_code,
            "predicate_status": verdict.predicate_status,
            "target_finding_clear": verdict.target_finding_clear,
            "target_entities_preserved": verdict.target_entities_preserved,
            "new_deterministic_findings": verdict.new_deterministic_findings,
            "submitted_tree_sha256": verdict.submitted_tree_sha256,
            "failure_reason": verdict.failure_reason,
        }
    payload.pop("verdict_sha256", None)
    payload.setdefault("schema_version", "qa-correctness-verdict@1")
    payload["verdict_sha256"] = content_sha256(payload)
    return QaCorrectnessVerdict.model_validate(payload)


def _event_durations(events: tuple[QaEvent, ...]) -> tuple[int, int]:
    if len(events) < 2 or events[0].kind != "start" or events[-1].kind != "finish":
        raise ValueError("QA events must start with start and end with finish")
    values = tuple(item.monotonic_ns for item in events)
    if any(current <= previous for previous, current in zip(values, values[1:])):
        raise ValueError("QA event monotonic_ns values must be strictly increasing")

    state = "running"
    running_since = events[0].monotonic_ns
    active_ns = 0
    for event in events[1:]:
        if state == "running" and event.kind == "pause":
            active_ns += event.monotonic_ns - running_since
            state = "paused"
        elif state == "paused" and event.kind == "resume":
            running_since = event.monotonic_ns
            state = "running"
        elif state == "running" and event.kind == "finish":
            active_ns += event.monotonic_ns - running_since
            state = "finished"
        else:
            raise ValueError(f"invalid QA event transition: {state} -> {event.kind}")
    if state != "finished":
        raise ValueError("QA event sequence did not finish")
    return active_ns, events[-1].monotonic_ns - events[0].monotonic_ns


def _normalized_patch_path(value: str) -> str:
    if not value or "\\" in value or "\x00" in value:
        raise ValueError("final_patch_path must be a normalized relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or "." in path.parts or ".." in path.parts or str(path) != value:
        raise ValueError("final_patch_path must be a normalized relative POSIX path")
    if path.suffix != ".patch":
        raise ValueError("final_patch_path must end in .patch")
    return value


class QaSessionEvidence(_StrictModel):
    schema_version: Literal["qa-session@1"] = "qa-session@1"
    protocol_sha256: Sha256
    session_id: StableId
    participant_id: StableId
    case_id: StableId
    pair_id: StableId
    arm: QaArm
    order: int = Field(ge=1, le=8)
    events: tuple[QaEvent, ...]
    active_ns: int = Field(ge=0)
    elapsed_ns: int = Field(ge=0)
    capped_active_ns: int = Field(ge=0, le=QA_ACTIVE_CAP_NS)
    timed_out: bool
    final_patch_path: str
    final_patch_sha256: Sha256
    participant_attested_no_contamination: bool
    verdict: QaCorrectnessVerdict
    protocol_valid: bool
    failure_reasons: tuple[NonEmptyStr, ...]
    evidence_sha256: Sha256

    @field_validator("final_patch_path")
    @classmethod
    def validate_patch_path(cls, value: str) -> str:
        return _normalized_patch_path(value)

    @classmethod
    def seal(cls, **values: Any) -> QaSessionEvidence:
        payload = dict(values)
        payload.pop("evidence_sha256", None)
        payload.pop("active_ns", None)
        payload.pop("elapsed_ns", None)
        payload.pop("capped_active_ns", None)
        payload.pop("timed_out", None)
        payload.pop("protocol_valid", None)
        payload.setdefault("schema_version", "qa-session@1")
        events = tuple(
            item if isinstance(item, QaEvent) else QaEvent.model_validate(item)
            for item in payload["events"]
        )
        payload["events"] = events
        active_ns, elapsed_ns = _event_durations(events)
        payload["active_ns"] = active_ns
        payload["elapsed_ns"] = elapsed_ns
        payload["capped_active_ns"] = min(active_ns, QA_ACTIVE_CAP_NS)
        payload["timed_out"] = active_ns >= QA_ACTIVE_CAP_NS
        reasons = tuple(sorted(set(payload.pop("failure_reasons", ()))))
        if not payload["participant_attested_no_contamination"]:
            reasons = tuple(sorted((*reasons, "participant attested arm contamination")))
        payload["failure_reasons"] = reasons
        payload["protocol_valid"] = not reasons
        payload["evidence_sha256"] = content_sha256(payload)
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def validate_session(self) -> QaSessionEvidence:
        active_ns, elapsed_ns = _event_durations(self.events)
        if self.active_ns != active_ns:
            raise ValueError("active_ns does not rederive from QA events")
        if self.elapsed_ns != elapsed_ns:
            raise ValueError("elapsed_ns does not rederive from QA events")
        if self.capped_active_ns != min(active_ns, QA_ACTIVE_CAP_NS):
            raise ValueError("capped_active_ns does not use the frozen cap")
        if self.timed_out != (active_ns >= QA_ACTIVE_CAP_NS):
            raise ValueError("timed_out does not rederive from active time")
        if self.failure_reasons != tuple(sorted(set(self.failure_reasons))):
            raise ValueError("failure_reasons must be unique and sorted")
        contamination = not self.participant_attested_no_contamination
        contamination_reason = "participant attested arm contamination"
        if contamination != (contamination_reason in self.failure_reasons):
            raise ValueError("contamination attestation and failure reason differ")
        if self.protocol_valid != (not self.failure_reasons):
            raise ValueError("protocol_valid must equal absence of failure reasons")
        expected_hash = content_sha256(self, exclude={"evidence_sha256"})
        if self.evidence_sha256 != expected_hash:
            raise ValueError("evidence_sha256 does not bind QA session")
        return self


def canonical_session_bytes(session: QaSessionEvidence) -> bytes:
    return (canonical_json(session.model_dump(mode="json")) + "\n").encode("utf-8")


def load_session(path: str | Path) -> QaSessionEvidence:
    raw = Path(path).read_bytes()
    session = QaSessionEvidence.model_validate_json(raw)
    if canonical_session_bytes(session) != raw:
        raise ValueError("QA session evidence is not canonical JSON")
    return session


def write_session(path: str | Path, session: QaSessionEvidence) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical_session_bytes(session))


__all__ = [
    "QA_ACTIVE_CAP_NS",
    "QA_TOTAL_ACTIVE_CAP_NS",
    "FindingKey",
    "QaArm",
    "QaCorrectnessVerdict",
    "QaEvent",
    "QaSessionEvidence",
    "QaSessionSpec",
    "QaSplit",
    "StableId",
    "canonical_session_bytes",
    "content_sha256",
    "load_session",
    "seal_qa_verdict",
    "write_session",
]
