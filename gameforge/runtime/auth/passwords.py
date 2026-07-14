"""Login-name normalization and policy-bound Argon2id password handling."""

from __future__ import annotations

from collections.abc import Callable
import secrets
import unicodedata

from argon2 import PasswordHasher, Type, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import ARGON2_VERSION

from gameforge.contracts.auth import (
    LoginNameNormalizationPolicyV1,
    PasswordHashPolicyV1,
    SecretText,
)
from gameforge.contracts.errors import AuthFailed, IntegrityViolation


_REJECTED_UNICODE_CATEGORIES = {
    "Cc": "control",
    "Cs": "surrogate",
    "Co": "private_use",
}


def normalize_login_name(
    login_name: str,
    policy: LoginNameNormalizationPolicyV1,
) -> str:
    """Apply the exact retained policy bound to a password credential."""

    if type(policy) is not LoginNameNormalizationPolicyV1:
        raise AuthFailed("login name is invalid")
    normalized = canonicalize_login_name(login_name)
    if not policy.minimum_codepoints <= len(normalized) <= policy.maximum_codepoints:
        raise AuthFailed("login name is invalid")
    return normalized


def canonicalize_login_name(login_name: str) -> str:
    """Apply the invariant lookup transform before resolving the bound policy."""

    if not isinstance(login_name, str):
        raise AuthFailed("login name is invalid")
    normalized = unicodedata.normalize("NFKC", login_name).strip().casefold()
    if any(
        _REJECTED_UNICODE_CATEGORIES.get(unicodedata.category(character)) is not None
        for character in normalized
    ):
        raise AuthFailed("login name is invalid")
    if not 1 <= len(normalized) <= 256:
        raise AuthFailed("login name is invalid")
    return normalized


class Argon2PasswordRuntime:
    """Hash and verify passwords without owning credential persistence."""

    def __init__(self, *, random_bytes: Callable[[int], bytes] = secrets.token_bytes) -> None:
        self._random_bytes = random_bytes

    def hash_password(self, password: SecretText, policy: PasswordHashPolicyV1) -> str:
        hasher = self._hasher(policy)
        salt = self._random_bytes(policy.salt_bytes)
        if not isinstance(salt, bytes) or len(salt) != policy.salt_bytes:
            raise ValueError("password salt source returned an invalid salt")
        return hasher.hash(password.get_secret_value(), salt=salt)

    def verify_password(
        self,
        password: SecretText,
        encoded_hash: str,
        policy: PasswordHashPolicyV1,
    ) -> bool:
        hasher = self._hasher(policy)
        try:
            parameters = extract_parameters(encoded_hash)
            if (
                parameters.type is not Type.ID
                or parameters.time_cost != policy.iterations
                or parameters.memory_cost != policy.memory_kib
                or parameters.parallelism != policy.parallelism
                or parameters.salt_len != policy.salt_bytes
                or parameters.hash_len != 32
                or parameters.version != ARGON2_VERSION
            ):
                raise IntegrityViolation("stored password hash parameters differ from bound policy")
            return hasher.verify(encoded_hash, password.get_secret_value())
        except VerifyMismatchError:
            return False
        except (InvalidHashError, VerificationError, TypeError, ValueError) as exc:
            raise IntegrityViolation("stored password hash is invalid") from exc

    def needs_rehash(self, encoded_hash: str, policy: PasswordHashPolicyV1) -> bool:
        try:
            return self._hasher(policy).check_needs_rehash(encoded_hash)
        except (InvalidHashError, TypeError, ValueError) as exc:
            raise IntegrityViolation("stored password hash is invalid") from exc

    @staticmethod
    def _hasher(policy: PasswordHashPolicyV1) -> PasswordHasher:
        if type(policy) is not PasswordHashPolicyV1 or policy.algorithm != "argon2id":
            raise ValueError("password hashing requires an exact Argon2id policy")
        return PasswordHasher(
            time_cost=policy.iterations,
            memory_cost=policy.memory_kib,
            parallelism=policy.parallelism,
            hash_len=32,
            salt_len=policy.salt_bytes,
            type=Type.ID,
        )


__all__ = [
    "Argon2PasswordRuntime",
    "canonicalize_login_name",
    "normalize_login_name",
]
