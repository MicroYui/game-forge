"""Explicit registry for source-specific external-corpus profile bindings."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Mapping

from gameforge.bench.external_corpus.contracts import SourceProfile
from gameforge.bench.external_corpus.profiles.endless_sky import (
    validate_endless_sky_source_profile,
)
from gameforge.bench.external_corpus.profiles.flare import validate_flare_source_profile


ProfileValidator = Callable[[SourceProfile], SourceProfile]


@dataclass(frozen=True, slots=True)
class SourceProfileBinding:
    """A source identity bound to the shared profile contract and validator."""

    source_id: str
    profile_model: type[SourceProfile]
    validate_source_profile: ProfileValidator


FLARE_PROFILE_BINDING = SourceProfileBinding(
    source_id="flare",
    profile_model=SourceProfile,
    validate_source_profile=validate_flare_source_profile,
)
ENDLESS_SKY_PROFILE_BINDING = SourceProfileBinding(
    source_id="endless_sky",
    profile_model=SourceProfile,
    validate_source_profile=validate_endless_sky_source_profile,
)

PROFILE_BINDINGS: Mapping[str, SourceProfileBinding] = MappingProxyType(
    {
        "flare": FLARE_PROFILE_BINDING,
        "endless_sky": ENDLESS_SKY_PROFILE_BINDING,
    }
)


def get_profile_binding(source_id: str) -> SourceProfileBinding:
    """Return a statically registered source binding or fail closed."""

    try:
        return PROFILE_BINDINGS[source_id]
    except KeyError as exc:
        raise ValueError(f"unknown external source profile: {source_id}") from exc


__all__ = [
    "ENDLESS_SKY_PROFILE_BINDING",
    "FLARE_PROFILE_BINDING",
    "PROFILE_BINDINGS",
    "SourceProfileBinding",
    "get_profile_binding",
]
