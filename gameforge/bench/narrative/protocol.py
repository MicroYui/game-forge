"""One-way frozen protocol for power-complete narrative evaluation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    StringConstraints,
    model_validator,
)

from gameforge.agents.base import DEFAULT_SNAPSHOT
from gameforge.agents.consistency.assistant import CURRENT_PERSPECTIVES
from gameforge.agents.consistency.normalization import MATCHER_VERSION
from gameforge.agents.prompts.library import register_all_prompts
from gameforge.agents.prompts.registry import get_prompt
from gameforge.bench.narrative.contracts import content_sha256
from gameforge.bench.narrative.corpus import (
    NarrativeCorpusManifest,
    load_manifest,
    validate_corpus_manifest,
)
from gameforge.bench.narrative.generator import GENERATOR_VERSION
from gameforge.bench.narrative.oracle import ORACLE_VERSION
from gameforge.bench.narrative.renderer import RENDERER_VERSION
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

PROMPT_NAMES = (
    "consistency.system",
    "consistency.perspective.constraint_matching",
    "consistency.perspective.causal_world_state",
    "consistency.perspective.adversarial_falsification",
    "consistency.rebuttal.constraint_matching",
    "consistency.rebuttal.causal_world_state",
    "consistency.rebuttal.adversarial_falsification",
)

register_all_prompts()


class NarrativeProtocol(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["narrative-protocol@1"] = "narrative-protocol@1"
    generator_version: StableId
    renderer_version: StableId
    oracle_version: StableId
    development_corpus_sha256: Sha256
    verification_corpus_sha256: Sha256
    prompt_version: StableId
    prompt_bundle_sha256: Sha256
    model_snapshot: ModelSnapshot
    perspectives: tuple[str, ...]
    threshold: Literal[2] = 2
    rebuttal_enabled: Literal[False] = False
    matcher_version: StableId
    frozen: Literal[True] = True
    protocol_sha256: Sha256

    @classmethod
    def seal(cls, **values: Any) -> NarrativeProtocol:
        payload = dict(values)
        payload.pop("protocol_sha256", None)
        payload.setdefault("schema_version", "narrative-protocol@1")
        payload.setdefault("generator_version", GENERATOR_VERSION)
        payload.setdefault("renderer_version", RENDERER_VERSION)
        payload.setdefault("oracle_version", ORACLE_VERSION)
        payload.setdefault("prompt_version", current_prompt_version())
        payload.setdefault("prompt_bundle_sha256", prompt_bundle_sha256())
        payload.setdefault("model_snapshot", DEFAULT_SNAPSHOT)
        payload.setdefault("perspectives", CURRENT_PERSPECTIVES)
        payload.setdefault("threshold", 2)
        payload.setdefault("rebuttal_enabled", False)
        payload.setdefault("matcher_version", MATCHER_VERSION)
        payload.setdefault("frozen", True)
        payload["protocol_sha256"] = content_sha256(payload)
        return cls.model_validate(payload)

    @model_validator(mode="after")
    def validate_protocol(self) -> NarrativeProtocol:
        expected_snapshot = {
            "provider": "openai",
            "model": "gpt-5.6-sol",
            "snapshot_tag": "pre-m4@1",
        }
        if self.model_snapshot.model_dump() != expected_snapshot:
            raise ValueError(
                "narrative protocol requires openai/gpt-5.6-sol/pre-m4@1"
            )
        if self.perspectives != CURRENT_PERSPECTIVES:
            raise ValueError("narrative protocol requires the three frozen perspectives")
        expected = content_sha256(self, exclude={"protocol_sha256"})
        if self.protocol_sha256 != expected:
            raise ValueError("protocol_sha256 does not bind narrative protocol")
        return self


def _prompt_bundle() -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "name": name,
            "version": get_prompt(name)[0],
            "text": get_prompt(name)[1],
        }
        for name in sorted(PROMPT_NAMES)
    )


def current_prompt_version() -> str:
    versions = {item["version"] for item in _prompt_bundle()}
    if len(versions) != 1:
        raise ValueError("current consistency prompts must share one prompt version")
    return versions.pop()


def prompt_bundle_sha256() -> str:
    payload = {"prompts": _prompt_bundle()}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _corpus_file_sha(manifest: NarrativeCorpusManifest, split: str) -> str:
    for item in manifest.files:
        if item.split == split:
            return item.sha256
    raise ValueError(f"narrative corpus manifest has no {split} file")


def seal_protocol(manifest: NarrativeCorpusManifest) -> NarrativeProtocol:
    return NarrativeProtocol.seal(
        development_corpus_sha256=_corpus_file_sha(manifest, "development"),
        verification_corpus_sha256=_corpus_file_sha(manifest, "verification"),
    )


def assert_verification_ready(
    protocol: NarrativeProtocol,
    corpus_manifest: NarrativeCorpusManifest,
    *,
    corpus_root: str | Path = "scenarios/narrative_bench",
) -> None:
    """Fail closed on any protocol, source, prompt, or frozen-corpus drift."""

    expected_self_hash = content_sha256(protocol, exclude={"protocol_sha256"})
    if protocol.protocol_sha256 != expected_self_hash:
        raise ValueError("protocol_sha256 does not bind the supplied protocol")
    if protocol.model_snapshot.model_dump() != DEFAULT_SNAPSHOT.model_dump():
        raise ValueError("narrative protocol model snapshot differs from GPT-5.6 policy")
    if protocol.generator_version != GENERATOR_VERSION:
        raise ValueError("narrative generator version drift")
    if protocol.renderer_version != RENDERER_VERSION:
        raise ValueError("narrative renderer version drift")
    if protocol.oracle_version != ORACLE_VERSION:
        raise ValueError("narrative oracle version drift")
    if protocol.matcher_version != MATCHER_VERSION:
        raise ValueError("narrative matcher version drift")
    if protocol.perspectives != CURRENT_PERSPECTIVES:
        raise ValueError("narrative perspective order drift")
    if protocol.threshold != 2 or protocol.rebuttal_enabled or not protocol.frozen:
        raise ValueError("narrative quorum settings drift")
    if protocol.prompt_version != current_prompt_version():
        raise ValueError("narrative prompt version drift")
    if protocol.prompt_bundle_sha256 != prompt_bundle_sha256():
        raise ValueError("prompt_bundle_sha256 differs from checked-out prompt text")

    validate_corpus_manifest(corpus_root, corpus_manifest)
    if protocol.development_corpus_sha256 != _corpus_file_sha(
        corpus_manifest,
        "development",
    ):
        raise ValueError("development corpus sha256 differs from frozen protocol")
    if protocol.verification_corpus_sha256 != _corpus_file_sha(
        corpus_manifest,
        "verification",
    ):
        raise ValueError("verification corpus sha256 differs from frozen protocol")


def canonical_protocol_bytes(protocol: NarrativeProtocol) -> bytes:
    return (canonical_json(protocol.model_dump(mode="json")) + "\n").encode("utf-8")


def load_protocol(path: str | Path) -> NarrativeProtocol:
    raw = Path(path).read_bytes()
    protocol = NarrativeProtocol.model_validate_json(raw)
    if canonical_protocol_bytes(protocol) != raw:
        raise ValueError("narrative protocol is not canonical JSON")
    return protocol


def load_frozen_protocol(
    protocol_path: str | Path = "scenarios/narrative_bench/protocol.json",
    corpus_manifest_path: str | Path = (
        "scenarios/narrative_bench/corpus-manifest.json"
    ),
) -> tuple[NarrativeProtocol, NarrativeCorpusManifest]:
    protocol = load_protocol(protocol_path)
    manifest = load_manifest(corpus_manifest_path)
    assert_verification_ready(protocol, manifest)
    return protocol, manifest


__all__ = [
    "PROMPT_NAMES",
    "NarrativeProtocol",
    "assert_verification_ready",
    "canonical_protocol_bytes",
    "current_prompt_version",
    "load_frozen_protocol",
    "load_protocol",
    "prompt_bundle_sha256",
    "seal_protocol",
]
