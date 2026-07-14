"""Fail-closed local providers for versioned browser-session signing keys."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable, Mapping
import json
import os
import re
from typing import Any, Literal, cast

from gameforge.runtime.auth.tokens import SessionSigningKey, SessionSigningKeySet


SESSION_SIGNING_KEY_SETS_ENV = "GAMEFORGE_SESSION_SIGNING_KEY_SETS"
_KEY_SET_FIELDS = frozenset({"key_set_version", "keys"})
_KEY_FIELDS = frozenset({"key_id", "secret_base64", "status"})
_ALLOWED_STATUSES = frozenset({"active", "grace"})
_KEY_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class SessionSigningKeyConfigurationError(ValueError):
    """The injected or environment-backed signing-key configuration is unsafe."""


class SessionSigningKeyProvider:
    """Resolve immutable key sets without exposing their key material."""

    __slots__ = ("_key_sets", "_versions")

    def __init__(self, key_sets: Iterable[SessionSigningKeySet]) -> None:
        try:
            supplied = tuple(key_sets)
        except TypeError as exc:
            raise SessionSigningKeyConfigurationError(
                "session signing key sets must be iterable"
            ) from exc
        if not supplied:
            raise SessionSigningKeyConfigurationError(
                "at least one session signing key set is required"
            )

        by_version: dict[str, SessionSigningKeySet] = {}
        for key_set in supplied:
            self._validate_key_set(key_set)
            if key_set.key_set_version in by_version:
                raise SessionSigningKeyConfigurationError(
                    "duplicate key-set version in session signing configuration"
                )
            by_version[key_set.key_set_version] = key_set

        self._versions = tuple(sorted(by_version))
        self._key_sets = {version: by_version[version] for version in self._versions}

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> SessionSigningKeyProvider:
        """Parse exact key sets from ``GAMEFORGE_SESSION_SIGNING_KEY_SETS`` JSON."""

        source = os.environ if environment is None else environment
        raw = source.get(SESSION_SIGNING_KEY_SETS_ENV)
        if not isinstance(raw, str) or not raw.strip():
            raise SessionSigningKeyConfigurationError(f"{SESSION_SIGNING_KEY_SETS_ENV} is required")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            raise SessionSigningKeyConfigurationError(
                f"{SESSION_SIGNING_KEY_SETS_ENV} contains invalid JSON"
            ) from None
        if not isinstance(payload, list) or not payload:
            raise SessionSigningKeyConfigurationError(
                f"{SESSION_SIGNING_KEY_SETS_ENV} must be a non-empty JSON array"
            )
        return cls(tuple(_parse_key_set(item) for item in payload))

    @property
    def versions(self) -> tuple[str, ...]:
        return self._versions

    def resolve(self, key_set_version: str) -> SessionSigningKeySet | None:
        if not isinstance(key_set_version, str):
            return None
        return self._key_sets.get(key_set_version)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(versions={self._versions!r})"

    @staticmethod
    def _validate_key_set(key_set: SessionSigningKeySet) -> None:
        if not isinstance(key_set, SessionSigningKeySet):
            raise SessionSigningKeyConfigurationError(
                "session signing provider accepts only SessionSigningKeySet values"
            )
        key_ids: set[str] = set()
        secrets_seen: set[bytes] = set()
        active_count = 0
        for key in key_set.keys:
            if not isinstance(key, SessionSigningKey):
                raise SessionSigningKeyConfigurationError(
                    "session signing key set contains an invalid key"
                )
            if key.status not in _ALLOWED_STATUSES:
                raise SessionSigningKeyConfigurationError(
                    "session signing key status must be active or grace"
                )
            if key.key_id in key_ids:
                raise SessionSigningKeyConfigurationError("session signing key ids must be unique")
            key_ids.add(key.key_id)
            active_count += key.status == "active"
            if key.secret in secrets_seen:
                raise SessionSigningKeyConfigurationError(
                    "duplicate secret in session signing key set"
                )
            secrets_seen.add(key.secret)
        if active_count != 1:
            raise SessionSigningKeyConfigurationError(
                "session signing key set requires exactly one active key"
            )


def _parse_key_set(value: Any) -> SessionSigningKeySet:
    item = _require_exact_object(value, fields=_KEY_SET_FIELDS, label="key set")
    version = item["key_set_version"]
    keys = item["keys"]
    if not isinstance(version, str) or not version or len(version) > 512:
        raise SessionSigningKeyConfigurationError("key_set_version is invalid")
    if not isinstance(keys, list) or not keys:
        raise SessionSigningKeyConfigurationError("session key set keys must be non-empty")
    parsed_keys = tuple(_parse_key(entry) for entry in keys)
    key_ids = tuple(key.key_id for key in parsed_keys)
    if len(key_ids) != len(set(key_ids)):
        raise SessionSigningKeyConfigurationError("session signing key ids must be unique")
    if sum(key.status == "active" for key in parsed_keys) != 1:
        raise SessionSigningKeyConfigurationError(
            "session signing key set requires exactly one active key"
        )
    try:
        return SessionSigningKeySet(key_set_version=version, keys=parsed_keys)
    except (TypeError, ValueError):
        raise SessionSigningKeyConfigurationError("session signing key set is invalid") from None


def _parse_key(value: Any) -> SessionSigningKey:
    item = _require_exact_object(value, fields=_KEY_FIELDS, label="key")
    key_id = item["key_id"]
    status = item["status"]
    encoded_secret = item["secret_base64"]
    if (
        not isinstance(key_id, str)
        or not key_id
        or len(key_id) > 512
        or _KEY_ID.fullmatch(key_id) is None
    ):
        raise SessionSigningKeyConfigurationError("session signing key_id is invalid")
    if not isinstance(status, str) or status not in _ALLOWED_STATUSES:
        raise SessionSigningKeyConfigurationError(
            "session signing key status must be active or grace"
        )
    if not isinstance(encoded_secret, str) or not encoded_secret:
        raise SessionSigningKeyConfigurationError(
            "session signing key secret_base64 must be a non-empty string"
        )
    try:
        secret = base64.b64decode(encoded_secret, validate=True)
    except (binascii.Error, ValueError):
        raise SessionSigningKeyConfigurationError(
            "session signing key secret_base64 is invalid"
        ) from None
    if len(secret) < 32:
        raise SessionSigningKeyConfigurationError(
            "session signing key must contain at least 32 bytes"
        )
    try:
        return SessionSigningKey(
            key_id=key_id,
            secret=secret,
            status=cast(Literal["active", "grace"], status),
        )
    except (TypeError, ValueError):
        raise SessionSigningKeyConfigurationError("session signing key is invalid") from None


def _require_exact_object(
    value: Any,
    *,
    fields: frozenset[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise SessionSigningKeyConfigurationError(
            f"session signing {label} must contain exactly the supported fields"
        )
    return value


__all__ = [
    "SESSION_SIGNING_KEY_SETS_ENV",
    "SessionSigningKeyConfigurationError",
    "SessionSigningKeyProvider",
]
