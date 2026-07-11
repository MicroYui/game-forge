"""Source-bound validation for the preregistered Endless Sky profile."""

from __future__ import annotations

from gameforge.bench.external_corpus.contracts import SourceProfile


ENDLESS_SKY_SOURCE_ID = "endless_sky"
ENDLESS_SKY_REPOSITORY_URL = "https://github.com/endless-sky/endless-sky.git"
ENDLESS_SKY_PINNED_HEAD = "b10b7d6c24496e2f67a230a2553b344e200ba289"
ENDLESS_SKY_CONFIG_INCLUDE_GLOBS = ("data/**/*.txt",)
ENDLESS_SKY_CONFIG_EXCLUDE_GLOBS: tuple[str, ...] = ()
ENDLESS_SKY_LICENSE_ID = "GPL-3.0-or-later"


def validate_endless_sky_source_profile(profile: SourceProfile) -> SourceProfile:
    """Validate the generic model and the immutable source-identity fields."""

    validated = SourceProfile.model_validate(profile.model_dump(mode="json"))
    expected = {
        "source_id": ENDLESS_SKY_SOURCE_ID,
        "repository_url": ENDLESS_SKY_REPOSITORY_URL,
        "pinned_head": ENDLESS_SKY_PINNED_HEAD,
        "config_include_globs": ENDLESS_SKY_CONFIG_INCLUDE_GLOBS,
        "config_exclude_globs": ENDLESS_SKY_CONFIG_EXCLUDE_GLOBS,
        "license_id": ENDLESS_SKY_LICENSE_ID,
    }
    for field, expected_value in expected.items():
        if getattr(validated, field) != expected_value:
            raise ValueError(f"endless_sky source profile has unexpected {field}")
    return validated


__all__ = [
    "ENDLESS_SKY_CONFIG_EXCLUDE_GLOBS",
    "ENDLESS_SKY_CONFIG_INCLUDE_GLOBS",
    "ENDLESS_SKY_LICENSE_ID",
    "ENDLESS_SKY_PINNED_HEAD",
    "ENDLESS_SKY_REPOSITORY_URL",
    "ENDLESS_SKY_SOURCE_ID",
    "validate_endless_sky_source_profile",
]
