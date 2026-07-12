"""One-way frozen protocol for upstream-human edit distance evidence."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

from gameforge.agents.base import DEFAULT_SNAPSHOT
from gameforge.agents.prompts.library import register_all_prompts
from gameforge.agents.prompts.registry import get_prompt
from gameforge.bench.external_cases.contracts import ExternalCorpusManifest
from gameforge.bench.external_cases.qualify import load_manifest
from gameforge.bench.hed.contracts import content_sha256
from gameforge.bench.hed.delta import DISTANCE_METRIC, SEMANTIC_DELTA_VERSION
from gameforge.bench.stats import BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.model_router import ModelSnapshot

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
StableId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$",
    ),
]

PROMPT_NAMES = ("repair.refine", "repair.system")

register_all_prompts()


class HedProtocol(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["hed-protocol@1"] = "hed-protocol@1"
    external_manifest_sha256: Sha256
    external_case_ids: tuple[StableId, ...]
    external_case_count: Literal[8] = 8
    repair_prompt_version: Literal["repair@4"] = "repair@4"
    repair_prompt_bundle_sha256: Sha256
    model_snapshot: ModelSnapshot
    max_steps: Literal[4] = 4
    run_regression: Literal[False] = False
    semantic_delta_version: Literal["semantic-ir-delta@1"] = SEMANTIC_DELTA_VERSION
    distance_metric: Literal[
        "semantic-jaccard-symmetric-difference@1"
    ] = DISTANCE_METRIC
    bootstrap_seed: Literal[20260712] = BOOTSTRAP_SEED
    bootstrap_resamples: Literal[10000] = BOOTSTRAP_RESAMPLES
    frozen: Literal[True] = True
    protocol_sha256: Sha256

    @classmethod
    def seal(cls, **values: Any) -> HedProtocol:
        payload = dict(values)
        payload.pop("protocol_sha256", None)
        payload.setdefault("schema_version", "hed-protocol@1")
        payload.setdefault("external_case_count", 8)
        payload.setdefault("repair_prompt_version", current_prompt_version())
        payload.setdefault("repair_prompt_bundle_sha256", prompt_bundle_sha256())
        payload.setdefault("model_snapshot", DEFAULT_SNAPSHOT)
        payload.setdefault("max_steps", 4)
        payload.setdefault("run_regression", False)
        payload.setdefault("semantic_delta_version", SEMANTIC_DELTA_VERSION)
        payload.setdefault("distance_metric", DISTANCE_METRIC)
        payload.setdefault("bootstrap_seed", BOOTSTRAP_SEED)
        payload.setdefault("bootstrap_resamples", BOOTSTRAP_RESAMPLES)
        payload.setdefault("frozen", True)
        payload["protocol_sha256"] = content_sha256(payload)
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def validate_protocol(self) -> HedProtocol:
        if len(self.external_case_ids) != 8:
            raise ValueError("HED protocol requires exactly eight external case IDs")
        if len(self.external_case_ids) != len(set(self.external_case_ids)):
            raise ValueError("external case IDs must be unique")
        if self.external_case_ids != tuple(sorted(self.external_case_ids)):
            raise ValueError("external case IDs must be sorted")
        expected_snapshot = ModelSnapshot(
            provider="openai",
            model="gpt-5.6-sol",
            snapshot_tag="pre-m4@1",
        )
        if self.model_snapshot != expected_snapshot:
            raise ValueError("HED protocol requires openai/gpt-5.6-sol/pre-m4@1")
        expected_hash = content_sha256(self, exclude={"protocol_sha256"})
        if self.protocol_sha256 != expected_hash:
            raise ValueError("protocol_sha256 does not bind HED protocol")
        return self


def _prompt_bundle() -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "name": name,
            "version": get_prompt(name)[0],
            "text": get_prompt(name)[1],
        }
        for name in PROMPT_NAMES
    )


def current_prompt_version() -> str:
    versions = {item["version"] for item in _prompt_bundle()}
    if len(versions) != 1:
        raise ValueError("current Repair prompts must share one prompt version")
    return versions.pop()


def prompt_bundle_sha256() -> str:
    payload = {"prompts": _prompt_bundle()}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def seal_protocol(manifest: ExternalCorpusManifest) -> HedProtocol:
    return HedProtocol.seal(
        external_manifest_sha256=manifest.manifest_sha256,
        external_case_ids=tuple(sorted(item.spec.case_id for item in manifest.cases)),
    )


def assert_protocol_ready(
    protocol: HedProtocol,
    manifest: ExternalCorpusManifest,
    *,
    manifest_path: str | Path | None = None,
) -> None:
    """Fail closed on any frozen prompt, model, source, or metric drift."""

    if protocol.protocol_sha256 != content_sha256(
        protocol,
        exclude={"protocol_sha256"},
    ):
        raise ValueError("protocol_sha256 does not bind the supplied HED protocol")
    if protocol.model_snapshot != DEFAULT_SNAPSHOT:
        raise ValueError("HED protocol model snapshot differs from GPT-5.6 policy")
    if (
        protocol.external_case_count != 8
        or protocol.max_steps != 4
        or protocol.run_regression
        or protocol.semantic_delta_version != SEMANTIC_DELTA_VERSION
        or protocol.distance_metric != DISTANCE_METRIC
        or protocol.bootstrap_seed != BOOTSTRAP_SEED
        or protocol.bootstrap_resamples != BOOTSTRAP_RESAMPLES
        or not protocol.frozen
    ):
        raise ValueError("HED frozen policy drift")
    if protocol.repair_prompt_version != current_prompt_version():
        raise ValueError("HED Repair prompt version drift")
    if protocol.repair_prompt_bundle_sha256 != prompt_bundle_sha256():
        raise ValueError("HED Repair prompt bundle differs from checked-out text")

    expected_manifest_hash = content_sha256(manifest, exclude={"manifest_sha256"})
    if manifest.manifest_sha256 != expected_manifest_hash:
        raise ValueError("external manifest self hash is invalid")
    if manifest_path is not None and load_manifest(manifest_path) != manifest:
        raise ValueError("external manifest path differs from supplied manifest")
    case_ids = tuple(sorted(item.spec.case_id for item in manifest.cases))
    if len(case_ids) != 8 or len(case_ids) != len(set(case_ids)):
        raise ValueError("external manifest must contain eight unique cases")
    if any(item.qualification_status != "qualified" for item in manifest.cases):
        raise ValueError("external manifest contains an unqualified HED case")
    if protocol.external_manifest_sha256 != manifest.manifest_sha256:
        raise ValueError("HED protocol external manifest hash drift")
    if protocol.external_case_ids != case_ids:
        raise ValueError("HED protocol external case denominator drift")


def canonical_protocol_bytes(protocol: HedProtocol) -> bytes:
    return (canonical_json(protocol.model_dump(mode="json")) + "\n").encode("utf-8")


def load_protocol(path: str | Path) -> HedProtocol:
    raw = Path(path).read_bytes()
    protocol = HedProtocol.model_validate_json(raw)
    if canonical_protocol_bytes(protocol) != raw:
        raise ValueError("HED protocol is not canonical JSON")
    return protocol


def write_protocol(path: str | Path, protocol: HedProtocol) -> None:
    Path(path).write_bytes(canonical_protocol_bytes(protocol))


__all__ = [
    "PROMPT_NAMES",
    "HedProtocol",
    "assert_protocol_ready",
    "canonical_protocol_bytes",
    "current_prompt_version",
    "load_protocol",
    "prompt_bundle_sha256",
    "seal_protocol",
    "write_protocol",
]
