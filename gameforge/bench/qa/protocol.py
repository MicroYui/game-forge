"""Frozen counterbalanced protocol for the one-participant QA-hours study."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from gameforge.bench.external_cases.contracts import ExternalCorpusManifest
from gameforge.bench.external_cases.qualify import load_manifest as load_external
from gameforge.bench.hed.contracts import HedEvidenceManifest, content_sha256, load_evidence
from gameforge.bench.qa.contracts import (
    QA_ACTIVE_CAP_NS,
    QA_TOTAL_ACTIVE_CAP_NS,
    QaSessionSpec,
    StableId,
)
from gameforge.bench.taxonomy import DefectClass
from gameforge.contracts.canonical import canonical_json

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

_PATTERN = (
    (("development", "manual"), ("verification", "assisted")),
    (("development", "assisted"), ("verification", "manual")),
    (("verification", "manual"), ("development", "assisted")),
    (("verification", "assisted"), ("development", "manual")),
)


class QaProtocol(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["qa-protocol@1"] = "qa-protocol@1"
    participant_id: StableId = "participant-01"
    external_manifest_sha256: Sha256
    hed_evidence_sha256: Sha256
    correctness_protocol_id: Literal["external-submission-verdict@1"] = (
        "external-submission-verdict@1"
    )
    active_cap_ns: Literal[480000000000] = QA_ACTIVE_CAP_NS
    total_active_cap_ns: Literal[3840000000000] = QA_TOTAL_ACTIVE_CAP_NS
    sessions: tuple[QaSessionSpec, ...]
    frozen: Literal[True] = True
    protocol_sha256: Sha256

    @classmethod
    def seal(cls, **values: Any) -> QaProtocol:
        payload = dict(values)
        payload.pop("protocol_sha256", None)
        payload.setdefault("schema_version", "qa-protocol@1")
        payload.setdefault("participant_id", "participant-01")
        payload.setdefault(
            "correctness_protocol_id",
            "external-submission-verdict@1",
        )
        payload.setdefault("active_cap_ns", QA_ACTIVE_CAP_NS)
        payload.setdefault("total_active_cap_ns", QA_TOTAL_ACTIVE_CAP_NS)
        payload.setdefault("frozen", True)
        payload["protocol_sha256"] = content_sha256(payload)
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def validate_protocol(self) -> QaProtocol:
        if len(self.sessions) != 8:
            raise ValueError("QA protocol requires exactly eight sessions")
        orders = tuple(item.order for item in self.sessions)
        if orders != tuple(range(1, 9)):
            raise ValueError("QA session order must be exactly 1 through 8")
        for field, values in (
            ("session IDs", [item.session_id for item in self.sessions]),
            ("case IDs", [item.case_id for item in self.sessions]),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"QA protocol contains duplicate {field}")
        pair_ids = sorted({item.pair_id for item in self.sessions})
        if len(pair_ids) != 4:
            raise ValueError("QA protocol requires four matched pairs")
        classes = sorted(
            {item.defect_class for item in self.sessions},
            key=lambda item: item.value,
        )
        if len(classes) != 4:
            raise ValueError("QA protocol requires four defect classes")
        id_namespace = _schedule_id_namespace(self.sessions)
        for index, defect_class in enumerate(classes):
            pair = [item for item in self.sessions if item.defect_class is defect_class]
            if len(pair) != 2 or len({item.pair_id for item in pair}) != 1:
                raise ValueError("each QA class must form exactly one pair")
            if pair[0].pair_id != f"{id_namespace}-pair-{index + 1:02d}":
                raise ValueError("QA pair ID differs from the frozen namespace")
            if {item.arm for item in pair} != {"manual", "assisted"}:
                raise ValueError("each QA pair requires both arms")
            if {item.split for item in pair} != {"development", "verification"}:
                raise ValueError("each QA pair requires both corpus splits")
            ordered = sorted(pair, key=lambda item: item.order)
            signature = tuple((item.split, item.arm) for item in ordered)
            if signature != _PATTERN[index]:
                raise ValueError("QA schedule differs from the frozen row pattern")
        expected_hash = content_sha256(self, exclude={"protocol_sha256"})
        if self.protocol_sha256 != expected_hash:
            raise ValueError("protocol_sha256 does not bind QA protocol")
        return self


def _schedule_id_namespace(sessions: tuple[QaSessionSpec, ...]) -> str:
    first_suffix = "-session-01"
    first_id = sessions[0].session_id
    if not first_id.endswith(first_suffix):
        raise ValueError("QA session ID differs from the frozen namespace")
    namespace = first_id[: -len(first_suffix)]
    if not namespace:
        raise ValueError("QA session ID requires a non-empty namespace")
    for item in sessions:
        if item.session_id != f"{namespace}-session-{item.order:02d}":
            raise ValueError("QA session ID differs from the frozen namespace")
    return namespace


def _build_schedule(
    manifest: ExternalCorpusManifest,
    *,
    id_namespace: str = "qa",
) -> tuple[QaSessionSpec, ...]:
    grouped: dict[DefectClass, dict[str, Any]] = {}
    for case in manifest.cases:
        bucket = grouped.setdefault(case.spec.defect_class, {})
        if case.spec.split in bucket:
            raise ValueError("QA external denominator has duplicate class/split cases")
        bucket[case.spec.split] = case.spec
    classes = sorted(grouped, key=lambda item: item.value)
    if len(classes) != 4 or any(
        set(grouped[item]) != {"development", "verification"} for item in classes
    ):
        raise ValueError("QA requires four classes with development/verification cases")

    sessions: list[QaSessionSpec] = []
    order = 1
    for index, defect_class in enumerate(classes):
        pair_id = f"{id_namespace}-pair-{index + 1:02d}"
        for split, arm in _PATTERN[index]:
            spec = grouped[defect_class][split]
            sessions.append(
                QaSessionSpec(
                    session_id=f"{id_namespace}-session-{order:02d}",
                    pair_id=pair_id,
                    case_id=spec.case_id,
                    defect_class=defect_class,
                    split=split,
                    arm=arm,
                    order=order,
                )
            )
            order += 1
    return tuple(sessions)


def seal_qa_protocol(
    external: ExternalCorpusManifest,
    hed: HedEvidenceManifest,
    *,
    participant_id: StableId = "participant-01",
    id_namespace: StableId = "qa",
) -> QaProtocol:
    if hed.external_manifest_sha256 != external.manifest_sha256:
        raise ValueError("HED evidence does not bind the supplied external manifest")
    if hed.metric.evaluated_n != 8 or hed.metric.protocol_failure_count:
        raise ValueError("QA protocol requires eight HED outcomes without protocol failure")
    if any(item.status == "protocol_failure" for item in hed.outcomes):
        raise ValueError("QA protocol cannot consume a HED protocol failure")
    external_ids = {item.spec.case_id for item in external.cases}
    outcome_ids = {item.case_id for item in hed.outcomes}
    if len(external.cases) != 8 or len(hed.outcomes) != 8 or outcome_ids != external_ids:
        raise ValueError("QA HED/external outcome denominator mismatch")
    if external.manifest_sha256 != content_sha256(
        external,
        exclude={"manifest_sha256"},
    ):
        raise ValueError("external manifest self hash is invalid")
    if hed.evidence_sha256 != content_sha256(hed, exclude={"evidence_sha256"}):
        raise ValueError("HED evidence self hash is invalid")
    return QaProtocol.seal(
        participant_id=participant_id,
        external_manifest_sha256=external.manifest_sha256,
        hed_evidence_sha256=hed.evidence_sha256,
        sessions=_build_schedule(external, id_namespace=id_namespace),
    )


def assert_qa_protocol_ready(
    protocol: QaProtocol,
    external: ExternalCorpusManifest,
    hed: HedEvidenceManifest,
) -> None:
    expected = seal_qa_protocol(
        external,
        hed,
        participant_id=protocol.participant_id,
        id_namespace=_schedule_id_namespace(protocol.sessions),
    )
    if protocol != expected:
        raise ValueError("QA protocol differs from the frozen external/HED inputs")


def canonical_protocol_bytes(protocol: QaProtocol) -> bytes:
    return (canonical_json(protocol.model_dump(mode="json")) + "\n").encode("utf-8")


def load_protocol(path: str | Path) -> QaProtocol:
    raw = Path(path).read_bytes()
    protocol = QaProtocol.model_validate_json(raw)
    if canonical_protocol_bytes(protocol) != raw:
        raise ValueError("QA protocol is not canonical JSON")
    return protocol


def write_protocol(path: str | Path, protocol: QaProtocol) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(canonical_protocol_bytes(protocol))


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seal", action="store_true", required=True)
    parser.add_argument("--external", type=Path, required=True)
    parser.add_argument("--hed", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--participant-id", default="participant-01")
    parser.add_argument("--id-namespace", default="qa")
    args = parser.parse_args()

    external = load_external(args.external)
    hed = load_evidence(args.hed)
    protocol = seal_qa_protocol(
        external,
        hed,
        participant_id=args.participant_id,
        id_namespace=args.id_namespace,
    )
    assert_qa_protocol_ready(protocol, external, hed)
    write_protocol(args.output, protocol)
    print(protocol.protocol_sha256)


if __name__ == "__main__":
    _main()


__all__ = [
    "QaProtocol",
    "assert_qa_protocol_ready",
    "canonical_protocol_bytes",
    "load_protocol",
    "seal_qa_protocol",
    "write_protocol",
]
