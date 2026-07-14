"""One-time API keys and signed opaque browser-session tokens."""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import re
import secrets
from typing import Literal

from gameforge.contracts.auth import (
    ApiKeySecret,
    SecretText,
    SessionPolicyV1,
    SessionToken,
)
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import AuthFailed


_TOKEN_COMPONENT = re.compile(r"^[A-Za-z0-9_-]+$")
_SESSION_TOKEN_SCHEMA = "session-token@1"
_MAX_TOKEN_BYTES = 8192


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if not value or _TOKEN_COMPONENT.fullmatch(value) is None:
        raise AuthFailed("session token is invalid")
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise AuthFailed("session token is invalid") from exc


def _digest(key: bytes, value: bytes) -> str:
    return hmac.new(key, value, hashlib.sha256).hexdigest()


def _require_key(key: bytes, *, label: str) -> bytes:
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError(f"{label} must contain at least 32 bytes")
    return key


@dataclass(frozen=True, slots=True)
class ApiKeyLookup:
    key_prefix: str
    key_digest: str


@dataclass(frozen=True, slots=True)
class IssuedApiKey(ApiKeyLookup):
    api_key: ApiKeySecret


class ApiKeyRuntime:
    def __init__(
        self,
        *,
        digest_key: bytes,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._digest_key = _require_key(digest_key, label="API-key digest key")
        self._random_bytes = random_bytes

    def issue(self) -> IssuedApiKey:
        entropy = self._random_bytes(32)
        if not isinstance(entropy, bytes) or len(entropy) != 32:
            raise ValueError("API-key entropy source returned invalid bytes")
        encoded = _b64url(entropy)
        prefix = f"gfk_{encoded[:12]}"
        secret = ApiKeySecret(f"{prefix}.{encoded}")
        lookup = self.derive_lookup(secret)
        return IssuedApiKey(
            key_prefix=lookup.key_prefix,
            key_digest=lookup.key_digest,
            api_key=secret,
        )

    def derive_lookup(self, api_key: ApiKeySecret) -> ApiKeyLookup:
        raw = api_key.get_secret_value()
        prefix, separator, body = raw.partition(".")
        if (
            not separator
            or not prefix.startswith("gfk_")
            or len(prefix) > 64
            or not body
            or len(raw.encode("utf-8")) > 4096
        ):
            raise AuthFailed("API key is invalid")
        return ApiKeyLookup(
            key_prefix=prefix,
            key_digest=_digest(self._digest_key, raw.encode("utf-8")),
        )


@dataclass(frozen=True, slots=True)
class SessionSigningKey:
    key_id: str
    secret: bytes = field(repr=False)
    status: Literal["active", "grace"]

    def __post_init__(self) -> None:
        if (
            not self.key_id
            or len(self.key_id) > 512
            or _TOKEN_COMPONENT.fullmatch(self.key_id) is None
        ):
            raise ValueError("session signing key_id is invalid")
        if self.status not in {"active", "grace"}:
            raise ValueError("session signing key status must be active or grace")
        _require_key(self.secret, label="session signing key")


@dataclass(frozen=True, slots=True)
class SessionSigningKeySet:
    key_set_version: str
    keys: tuple[SessionSigningKey, ...]

    def __post_init__(self) -> None:
        if not self.key_set_version or len(self.key_set_version) > 512:
            raise ValueError("session key-set version is invalid")
        key_ids = [key.key_id for key in self.keys]
        if len(key_ids) != len(set(key_ids)):
            raise ValueError("session signing key ids must be unique")
        if sum(key.status == "active" for key in self.keys) != 1:
            raise ValueError("session signing key set requires exactly one active key")

    @property
    def active_key(self) -> SessionSigningKey:
        return next(key for key in self.keys if key.status == "active")

    def accepted_key(self, key_id: str) -> SessionSigningKey | None:
        return next((key for key in self.keys if key.key_id == key_id), None)


@dataclass(frozen=True, slots=True)
class IssuedSessionSecrets:
    session_token: SessionToken
    csrf_token: SecretText
    token_digest: str
    csrf_secret_digest: str
    signing_key_id: str


@dataclass(frozen=True, slots=True)
class VerifiedSessionToken:
    session_id: str
    credential_version: int
    session_policy_version: str
    signing_key_set_version: str
    signing_key_id: str
    token_digest: str


class SessionTokenRuntime:
    def __init__(
        self,
        *,
        key_set_resolver: Callable[[str], SessionSigningKeySet | None],
        token_digest_key: bytes,
        csrf_digest_key: bytes,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self._key_set_resolver = key_set_resolver
        self._token_digest_key = _require_key(token_digest_key, label="session digest key")
        self._csrf_digest_key = _require_key(csrf_digest_key, label="CSRF digest key")
        self._random_bytes = random_bytes

    def issue(
        self,
        *,
        session_id: str,
        credential_version: int,
        policy: SessionPolicyV1,
    ) -> IssuedSessionSecrets:
        if (
            not isinstance(session_id, str)
            or not session_id
            or len(session_id) > 512
            or isinstance(credential_version, bool)
            or not isinstance(credential_version, int)
            or credential_version < 1
            or type(policy) is not SessionPolicyV1
        ):
            raise ValueError("session issue inputs are invalid")
        key_set = self._key_set_resolver(policy.signing_key_set_version)
        if key_set is None or key_set.key_set_version != policy.signing_key_set_version:
            raise ValueError("session signing key set is unavailable")
        nonce = self._random_bytes(32)
        csrf = self._random_bytes(32)
        if (
            not isinstance(nonce, bytes)
            or len(nonce) != 32
            or not isinstance(csrf, bytes)
            or len(csrf) != 32
        ):
            raise ValueError("session entropy source returned invalid bytes")

        claims = {
            "credential_version": credential_version,
            "nonce": _b64url(nonce),
            "schema_version": _SESSION_TOKEN_SCHEMA,
            "session_id": session_id,
            "session_policy_version": policy.policy_version,
            "signing_key_set_version": key_set.key_set_version,
        }
        payload = _b64url(canonical_json(claims).encode("utf-8"))
        signing_key = key_set.active_key
        signed = f"{signing_key.key_id}.{payload}".encode("ascii")
        signature = _b64url(hmac.new(signing_key.secret, signed, hashlib.sha256).digest())
        token = SessionToken(f"{signed.decode('ascii')}.{signature}")
        token_digest = self._token_digest(token)
        csrf_token = SecretText(_b64url(csrf))
        return IssuedSessionSecrets(
            session_token=token,
            csrf_token=csrf_token,
            token_digest=token_digest,
            csrf_secret_digest=self._csrf_digest(
                csrf_token,
                token_digest=token_digest,
            ),
            signing_key_id=signing_key.key_id,
        )

    def verify(self, token: SessionToken) -> VerifiedSessionToken:
        raw = token.get_secret_value()
        if len(raw.encode("utf-8")) > _MAX_TOKEN_BYTES:
            raise AuthFailed("session token is invalid")
        parts = raw.split(".")
        if len(parts) != 3:
            raise AuthFailed("session token is invalid")
        key_id, payload_text, signature_text = parts
        if _TOKEN_COMPONENT.fullmatch(key_id) is None:
            raise AuthFailed("session token is invalid")
        payload_bytes = _b64url_decode(payload_text)
        signature = _b64url_decode(signature_text)
        claims = self._parse_untrusted_claims(payload_bytes)
        key_set = self._key_set_resolver(claims["signing_key_set_version"])
        if key_set is None or key_set.key_set_version != claims["signing_key_set_version"]:
            raise AuthFailed("session token is invalid")
        signing_key = key_set.accepted_key(key_id)
        if signing_key is None:
            raise AuthFailed("session token is invalid")
        expected = hmac.new(
            signing_key.secret,
            f"{key_id}.{payload_text}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise AuthFailed("session token is invalid")
        return VerifiedSessionToken(
            session_id=claims["session_id"],
            credential_version=claims["credential_version"],
            session_policy_version=claims["session_policy_version"],
            signing_key_set_version=claims["signing_key_set_version"],
            signing_key_id=key_id,
            token_digest=self._token_digest(token),
        )

    def verify_csrf(
        self,
        csrf_token: SecretText,
        *,
        token_digest: str,
        expected_digest: str,
    ) -> bool:
        return hmac.compare_digest(
            self._csrf_digest(csrf_token, token_digest=token_digest),
            expected_digest,
        )

    def _token_digest(self, token: SessionToken) -> str:
        return _digest(self._token_digest_key, token.get_secret_value().encode("utf-8"))

    def _csrf_digest(self, token: SecretText, *, token_digest: str) -> str:
        bound = token_digest.encode("ascii") + b"\x00" + token.get_secret_value().encode("utf-8")
        return _digest(self._csrf_digest_key, bound)

    @staticmethod
    def _parse_untrusted_claims(payload: bytes) -> dict[str, str | int]:
        if len(payload) > 4096:
            raise AuthFailed("session token is invalid")
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise AuthFailed("session token is invalid") from exc
        expected_keys = {
            "credential_version",
            "nonce",
            "schema_version",
            "session_id",
            "session_policy_version",
            "signing_key_set_version",
        }
        if not isinstance(value, dict) or set(value) != expected_keys:
            raise AuthFailed("session token is invalid")
        if value.get("schema_version") != _SESSION_TOKEN_SCHEMA:
            raise AuthFailed("session token is invalid")
        if (
            isinstance(value.get("credential_version"), bool)
            or not isinstance(value.get("credential_version"), int)
            or value["credential_version"] < 1
        ):
            raise AuthFailed("session token is invalid")
        for key in ("nonce", "session_id", "session_policy_version", "signing_key_set_version"):
            item = value.get(key)
            if not isinstance(item, str) or not item or len(item) > 512:
                raise AuthFailed("session token is invalid")
        _b64url_decode(value["nonce"])
        return value


__all__ = [
    "ApiKeyLookup",
    "ApiKeyRuntime",
    "IssuedApiKey",
    "IssuedSessionSecrets",
    "SessionSigningKey",
    "SessionSigningKeySet",
    "SessionTokenRuntime",
    "VerifiedSessionToken",
]
