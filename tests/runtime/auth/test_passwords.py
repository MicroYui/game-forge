from __future__ import annotations

import pytest
from argon2 import PasswordHasher, Type
from argon2.low_level import ARGON2_VERSION, hash_secret

from gameforge.contracts.api import (
    LoginNameNormalizationPolicyV1,
    PasswordHashPolicyV1,
    SecretText,
    compute_login_name_normalization_policy_digest,
)
from gameforge.contracts.errors import AuthFailed, IntegrityViolation
from gameforge.runtime.auth.passwords import Argon2PasswordRuntime, normalize_login_name


def _normalization_policy(
    *,
    minimum_codepoints: int = 3,
) -> LoginNameNormalizationPolicyV1:
    payload = {
        "policy_version": "login-normalization/1",
        "unicode_normalization": "NFKC",
        "trim_unicode_whitespace": True,
        "case_mapping": "unicode_casefold",
        "reject_categories": ("control", "surrogate", "private_use"),
        "minimum_codepoints": minimum_codepoints,
        "maximum_codepoints": 128,
    }
    return LoginNameNormalizationPolicyV1(
        **payload,
        policy_digest=compute_login_name_normalization_policy_digest(payload),
    )


def _hash_policy(*, memory_kib: int = 8192) -> PasswordHashPolicyV1:
    return PasswordHashPolicyV1(
        policy_version=f"argon2/{memory_kib}",
        algorithm="argon2id",
        memory_kib=memory_kib,
        iterations=1,
        parallelism=1,
        salt_bytes=16,
        rehash_on_login=True,
        effective_from="2026-07-14T00:00:00Z",
    )


def test_normalize_login_name_uses_frozen_unicode_pipeline() -> None:
    assert (
        normalize_login_name(
            "\u3000ＤｅＳｉＧｎｅＲ\u00a0",
            _normalization_policy(),
        )
        == "designer"
    )


@pytest.mark.parametrize("forbidden", ["\x00", "\ud800", "\ue000"])
def test_normalize_login_name_rejects_frozen_unicode_categories(forbidden: str) -> None:
    with pytest.raises(AuthFailed, match="login name"):
        normalize_login_name(f"abc{forbidden}", _normalization_policy())


def test_normalize_login_name_enforces_codepoint_bounds_after_normalization() -> None:
    with pytest.raises(AuthFailed, match="login name"):
        normalize_login_name("Ａ", _normalization_policy(minimum_codepoints=2))


def test_argon2id_hash_verify_and_rehash_are_policy_bound() -> None:
    salts = iter((b"a" * 16, b"b" * 16))
    runtime = Argon2PasswordRuntime(random_bytes=lambda size: next(salts))
    current = _hash_policy()
    stronger = _hash_policy(memory_kib=16_384)

    encoded = runtime.hash_password(SecretText("correct horse battery staple"), current)

    assert "correct horse" not in encoded
    assert encoded.startswith("$argon2id$")
    assert runtime.verify_password(
        SecretText("correct horse battery staple"),
        encoded,
        current,
    )
    assert not runtime.verify_password(SecretText("wrong"), encoded, current)
    with pytest.raises(IntegrityViolation, match="policy"):
        runtime.verify_password(
            SecretText("correct horse battery staple"),
            encoded,
            stronger,
        )
    assert not runtime.needs_rehash(encoded, current)
    assert runtime.needs_rehash(encoded, stronger)


def test_argon2_runtime_rejects_bad_salt_source_without_hashing() -> None:
    runtime = Argon2PasswordRuntime(random_bytes=lambda size: b"short")

    with pytest.raises(ValueError, match="salt"):
        runtime.hash_password(SecretText("password"), _hash_policy())


@pytest.mark.parametrize("variant", ["short_hash", "legacy_version"])
def test_argon2_verify_rejects_noncanonical_encoding_for_the_bound_policy(
    variant: str,
) -> None:
    policy = _hash_policy()
    password = SecretText("correct horse battery staple")
    if variant == "short_hash":
        encoded = PasswordHasher(
            time_cost=policy.iterations,
            memory_cost=policy.memory_kib,
            parallelism=policy.parallelism,
            hash_len=4,
            salt_len=policy.salt_bytes,
            type=Type.ID,
        ).hash(password.get_secret_value(), salt=b"c" * policy.salt_bytes)
    else:
        encoded = hash_secret(
            password.get_secret_value().encode(),
            b"d" * policy.salt_bytes,
            time_cost=policy.iterations,
            memory_cost=policy.memory_kib,
            parallelism=policy.parallelism,
            hash_len=32,
            type=Type.ID,
            version=ARGON2_VERSION - 3,
        ).decode()

    with pytest.raises(IntegrityViolation, match="policy"):
        Argon2PasswordRuntime().verify_password(password, encoded, policy)
