"""Transaction-bound local password, API-key, and session mechanisms.

These classes deliberately stop below authorization, audit, credential
management, and session revocation.  Their repositories must be supplied by
the owning UnitOfWork; this module never commits or rolls back a transaction.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import hmac
import re

from pydantic import BaseModel, ValidationError

from gameforge.contracts.auth import (
    ApiKeyAuthRequestV1,
    AuthenticationResultV1,
    LoginNameNormalizationPolicyV1,
    PasswordAuthRequestV1,
    PasswordCredentialRecordV1,
    PasswordHashPolicyV1,
    SecretText,
    SessionContextV1,
    SessionIssueRequestV1,
    SessionIssueV1,
    SessionPolicyV1,
    SessionRecordV1,
    SessionToken,
)
from gameforge.contracts.errors import (
    AuthFailed,
    CredentialDisabled,
    CredentialExpired,
    IntegrityViolation,
    SessionExpired,
    SessionRevoked,
)
from gameforge.contracts.identity import PrincipalRecordV1
from gameforge.contracts.storage import UtcClock
from gameforge.runtime.auth.passwords import (
    Argon2PasswordRuntime,
    canonicalize_login_name,
    normalize_login_name,
)
from gameforge.runtime.auth.tokens import ApiKeyRuntime, SessionTokenRuntime
from gameforge.runtime.persistence.auth import SqlAuthRepository
from gameforge.runtime.persistence.identity import SqlIdentityRepository


_SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_HTTP_METHOD = re.compile(r"^[A-Za-z]{1,32}$")

HashPolicyResolver = Callable[[str], PasswordHashPolicyV1 | None]
NormalizationPolicyResolver = Callable[[str, str], LoginNameNormalizationPolicyV1 | None]
SessionPolicyResolver = Callable[[str], SessionPolicyV1 | None]
SessionIdGenerator = Callable[[], str]


def _utc_now(clock: UtcClock) -> datetime:
    try:
        value = clock.now_utc()
    except (AttributeError, TypeError, ValueError) as exc:
        raise IntegrityViolation("authentication clock must return UTC") from exc
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != timedelta(0)
    ):
        raise IntegrityViolation("authentication clock must return UTC")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise IntegrityViolation(f"stored {field_name} is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise IntegrityViolation(f"stored {field_name} is not canonical UTC") from exc
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if parsed.utcoffset() != timedelta(0) or canonical != value:
        raise IntegrityViolation(f"stored {field_name} is not canonical UTC")
    return parsed.astimezone(timezone.utc)


def _canonical_contract[T: BaseModel](value: T, model_type: type[T], *, label: str) -> T:
    if type(value) is not model_type:
        raise IntegrityViolation(f"{label} resolver returned a noncanonical policy")
    try:
        parsed = model_type.model_validate(value.model_dump(mode="json"))
    except (AttributeError, TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(f"{label} resolver returned an invalid policy") from exc
    if parsed != value:
        raise IntegrityViolation(f"{label} resolver returned a noncanonical policy")
    return parsed


def _active_principal(
    identities: SqlIdentityRepository,
    principal_id: str,
    *,
    expected_kind: str,
) -> PrincipalRecordV1:
    principal = identities.get(principal_id)
    if principal is None:
        raise IntegrityViolation("credential references a missing principal")
    if principal.principal_id != principal_id or principal.kind != expected_kind:
        raise IntegrityViolation(f"credential must bind an exact {expected_kind} principal")
    if principal.status != "active":
        raise CredentialDisabled("principal is disabled")
    return principal


def _resolve_hash_policy(
    resolver: HashPolicyResolver,
    policy_version: str,
) -> PasswordHashPolicyV1:
    policy = resolver(policy_version)
    if policy is None:
        raise IntegrityViolation("password hash policy is unavailable")
    canonical = _canonical_contract(policy, PasswordHashPolicyV1, label="password hash policy")
    if canonical.policy_version != policy_version:
        raise IntegrityViolation("password hash policy resolver returned the wrong version")
    return canonical


def _resolve_normalization_policy(
    resolver: NormalizationPolicyResolver,
    policy_version: str,
    policy_digest: str,
) -> LoginNameNormalizationPolicyV1:
    policy = resolver(policy_version, policy_digest)
    if policy is None:
        raise IntegrityViolation("login normalization policy is unavailable")
    canonical = _canonical_contract(
        policy,
        LoginNameNormalizationPolicyV1,
        label="login normalization policy",
    )
    if canonical.policy_version != policy_version or canonical.policy_digest != policy_digest:
        raise IntegrityViolation(
            "login normalization policy resolver returned the wrong exact policy"
        )
    return canonical


def _resolve_session_policy(
    resolver: SessionPolicyResolver,
    policy_version: str,
) -> SessionPolicyV1:
    policy = resolver(policy_version)
    if policy is None:
        raise IntegrityViolation("session policy is unavailable")
    canonical = _canonical_contract(policy, SessionPolicyV1, label="session policy")
    if canonical.policy_version != policy_version:
        raise IntegrityViolation("session policy resolver returned the wrong version")
    return canonical


class LocalPasswordAuthenticator:
    """Verify one password credential and optionally CAS-rehash it in this UoW."""

    def __init__(
        self,
        *,
        auth_repository: SqlAuthRepository,
        identity_repository: SqlIdentityRepository,
        normalization_policy_resolver: NormalizationPolicyResolver,
        hash_policy_resolver: HashPolicyResolver,
        current_hash_policy: PasswordHashPolicyV1,
        password_runtime: Argon2PasswordRuntime,
        clock: UtcClock,
    ) -> None:
        self._auth = auth_repository
        self._identities = identity_repository
        self._normalization_policy_resolver = normalization_policy_resolver
        self._hash_policy_resolver = hash_policy_resolver
        self._current_hash_policy = _canonical_contract(
            current_hash_policy,
            PasswordHashPolicyV1,
            label="current password hash policy",
        )
        self._password_runtime = password_runtime
        self._clock = clock

    def verify_password(self, request: PasswordAuthRequestV1) -> AuthenticationResultV1:
        if type(request) is not PasswordAuthRequestV1:
            raise AuthFailed("password authentication request is invalid")
        lookup_name = canonicalize_login_name(request.login_name)
        credential = self._auth.get_password_by_normalized_login(lookup_name)
        if credential is None:
            raise AuthFailed("password authentication failed")
        normalization_policy = _resolve_normalization_policy(
            self._normalization_policy_resolver,
            credential.normalization_policy_version,
            credential.normalization_policy_digest,
        )
        try:
            normalized = normalize_login_name(request.login_name, normalization_policy)
        except AuthFailed as exc:
            raise IntegrityViolation(
                "password credential violates its bound normalization policy"
            ) from exc
        self._require_normalization_binding(
            credential,
            normalized,
            normalization_policy,
        )
        if credential.status != "active":
            raise CredentialDisabled("password credential is disabled")

        retained_policy = _resolve_hash_policy(
            self._hash_policy_resolver,
            credential.hash_policy_version,
        )
        current_policy = _resolve_hash_policy(
            self._hash_policy_resolver,
            self._current_hash_policy.policy_version,
        )
        if current_policy != self._current_hash_policy:
            raise IntegrityViolation("current password hash policy is inconsistent")
        if not self._password_runtime.verify_password(
            request.password,
            credential.password_hash,
            retained_policy,
        ):
            raise AuthFailed("password authentication failed")

        _active_principal(
            self._identities,
            credential.principal_id,
            expected_kind="human",
        )
        authenticated_at = _utc_now(self._clock)
        retained = credential
        if current_policy.rehash_on_login and (
            retained_policy != current_policy
            or self._password_runtime.needs_rehash(
                credential.password_hash,
                current_policy,
            )
        ):
            candidate = PasswordCredentialRecordV1.model_validate(
                credential.model_copy(
                    update={
                        "password_hash": self._password_runtime.hash_password(
                            request.password,
                            current_policy,
                        ),
                        "hash_policy_version": current_policy.policy_version,
                        "changed_at": _utc_text(authenticated_at),
                        "revision": credential.revision + 1,
                    }
                ).model_dump(mode="json")
            )
            retained = self._auth.compare_and_set_password(
                candidate,
                expected_revision=credential.revision,
            )
        return AuthenticationResultV1(
            principal_id=retained.principal_id,
            principal_kind="human",
            credential_id=retained.credential_id,
            credential_version=retained.credential_version,
            authenticated_at=_utc_text(authenticated_at),
        )

    def _require_normalization_binding(
        self,
        credential: PasswordCredentialRecordV1,
        normalized_login_name: str,
        policy: LoginNameNormalizationPolicyV1,
    ) -> None:
        if (
            credential.normalization_policy_version != policy.policy_version
            or credential.normalization_policy_digest != policy.policy_digest
            or credential.normalized_login_name != normalized_login_name
        ):
            raise IntegrityViolation("password credential normalization policy binding differs")


class LocalApiKeyAuthenticator:
    """Resolve an API-key digest against the current service principal."""

    def __init__(
        self,
        *,
        auth_repository: SqlAuthRepository,
        identity_repository: SqlIdentityRepository,
        api_key_runtime: ApiKeyRuntime,
        clock: UtcClock,
    ) -> None:
        self._auth = auth_repository
        self._identities = identity_repository
        self._api_key_runtime = api_key_runtime
        self._clock = clock

    def authenticate(self, request: ApiKeyAuthRequestV1) -> AuthenticationResultV1:
        if type(request) is not ApiKeyAuthRequestV1:
            raise AuthFailed("API-key authentication request is invalid")
        lookup = self._api_key_runtime.derive_lookup(request.api_key)
        credential = self._auth.get_api_key_by_digest(lookup.key_digest)
        if credential is None:
            raise AuthFailed("API-key authentication failed")
        if not hmac.compare_digest(credential.key_digest, lookup.key_digest):
            raise IntegrityViolation("API-key digest lookup returned inconsistent content")
        if credential.status == "expired":
            raise CredentialExpired("API key is expired")
        if credential.status != "active":
            raise CredentialDisabled("API key is not active")

        now = _utc_now(self._clock)
        if credential.expires_at is not None and now >= _parse_utc(
            credential.expires_at,
            field_name="API key expires_at",
        ):
            raise CredentialExpired("API key is expired")
        _active_principal(
            self._identities,
            credential.principal_id,
            expected_kind="service",
        )
        return AuthenticationResultV1(
            principal_id=credential.principal_id,
            principal_kind="service",
            credential_id=credential.api_key_id,
            credential_version=credential.credential_version,
            authenticated_at=_utc_text(now),
        )


class LocalSessionRuntime:
    """Issue and resolve sessions; audited revocation belongs to Task 3 services."""

    def __init__(
        self,
        *,
        auth_repository: SqlAuthRepository,
        identity_repository: SqlIdentityRepository,
        session_policy_resolver: SessionPolicyResolver,
        token_runtime: SessionTokenRuntime,
        clock: UtcClock,
        session_id_generator: SessionIdGenerator,
    ) -> None:
        self._auth = auth_repository
        self._identities = identity_repository
        self._session_policy_resolver = session_policy_resolver
        self._token_runtime = token_runtime
        self._clock = clock
        self._session_id_generator = session_id_generator

    def issue(self, request: SessionIssueRequestV1) -> SessionIssueV1:
        if type(request) is not SessionIssueRequestV1:
            raise AuthFailed("session issue request is invalid")
        credential = self._auth.get_password(request.source_credential_id)
        if credential is None:
            raise AuthFailed("session source credential is unavailable")
        if credential.status != "active":
            raise CredentialDisabled("session source credential is disabled")
        if (
            credential.principal_id != request.principal_id
            or credential.credential_id != request.source_credential_id
        ):
            raise AuthFailed("session source credential binding differs")
        if credential.credential_version != request.credential_version:
            raise CredentialDisabled("session source credential version differs")
        _active_principal(
            self._identities,
            credential.principal_id,
            expected_kind="human",
        )
        policy = _resolve_session_policy(
            self._session_policy_resolver,
            request.session_policy_version,
        )
        session_id = self._session_id_generator()
        if not isinstance(session_id, str) or not session_id or len(session_id) > 512:
            raise IntegrityViolation("session id generator returned an invalid id")
        try:
            secrets = self._token_runtime.issue(
                session_id=session_id,
                credential_version=credential.credential_version,
                policy=policy,
            )
        except ValueError as exc:
            raise IntegrityViolation("session token issue dependencies are inconsistent") from exc

        now = _utc_now(self._clock)
        absolute = now + timedelta(seconds=policy.absolute_ttl_s)
        idle = min(now + timedelta(seconds=policy.idle_ttl_s), absolute)
        record = SessionRecordV1(
            session_id=session_id,
            principal_id=credential.principal_id,
            source_credential_id=credential.credential_id,
            credential_version=credential.credential_version,
            token_digest=secrets.token_digest,
            csrf_secret_digest=secrets.csrf_secret_digest,
            signing_key_id=secrets.signing_key_id,
            issued_at=_utc_text(now),
            absolute_expires_at=_utc_text(absolute),
            idle_expires_at=_utc_text(idle),
            last_seen_at=_utc_text(now),
            revision=1,
        )
        retained = self._auth.create_session(record)
        if retained != record:
            raise IntegrityViolation("session repository retained different issue content")
        return SessionIssueV1(
            session_id=retained.session_id,
            session_token=secrets.session_token,
            csrf_token=secrets.csrf_token,
            absolute_expires_at=retained.absolute_expires_at,
            idle_expires_at=retained.idle_expires_at,
        )

    def resolve(
        self,
        token: SessionToken,
        *,
        csrf_token: SecretText | None,
        request_method: str,
    ) -> SessionContextV1:
        if not isinstance(token, SessionToken):
            raise AuthFailed("session token is invalid")
        verified = self._token_runtime.verify(token)
        record = self._auth.get_session(verified.session_id)
        if record is None:
            raise AuthFailed("session is unavailable")
        digest_record = self._auth.get_session_by_token_digest(verified.token_digest)
        if digest_record is None or digest_record.session_id != record.session_id:
            raise IntegrityViolation("session token digest authority is inconsistent")
        if digest_record != record:
            raise IntegrityViolation("session id and digest lookups returned different content")
        if not hmac.compare_digest(record.token_digest, verified.token_digest):
            raise IntegrityViolation("session token digest differs from stored authority")
        if record.signing_key_id != verified.signing_key_id:
            raise IntegrityViolation("session signing key differs from stored authority")
        if record.credential_version != verified.credential_version:
            raise IntegrityViolation("session credential version differs from signed authority")

        policy = _resolve_session_policy(
            self._session_policy_resolver,
            verified.session_policy_version,
        )
        if policy.signing_key_set_version != verified.signing_key_set_version:
            raise IntegrityViolation("session policy signing key set differs from signed authority")
        if record.revoked_at is not None:
            raise SessionRevoked("session is revoked")

        now = _utc_now(self._clock)
        absolute = _parse_utc(record.absolute_expires_at, field_name="session absolute expiry")
        idle = _parse_utc(record.idle_expires_at, field_name="session idle expiry")
        last_seen = _parse_utc(record.last_seen_at, field_name="session last seen")
        if now < last_seen:
            raise IntegrityViolation("session clock moved behind last_seen_at")
        if now >= absolute or now >= idle:
            raise SessionExpired("session is expired")

        credential = self._auth.get_password(record.source_credential_id)
        if credential is None:
            raise IntegrityViolation("session references a missing password credential")
        if (
            credential.credential_id != record.source_credential_id
            or credential.principal_id != record.principal_id
        ):
            raise IntegrityViolation("session password binding differs from stored authority")
        if credential.status != "active":
            raise CredentialDisabled("session password credential is disabled")
        if credential.credential_version != record.credential_version:
            raise CredentialDisabled("session password credential version differs")
        _active_principal(
            self._identities,
            record.principal_id,
            expected_kind="human",
        )

        method = self._request_method(request_method)
        if method not in _SAFE_HTTP_METHODS:
            if not isinstance(csrf_token, SecretText) or not self._token_runtime.verify_csrf(
                csrf_token,
                token_digest=record.token_digest,
                expected_digest=record.csrf_secret_digest,
            ):
                raise AuthFailed("CSRF token is invalid")

        if now >= last_seen + timedelta(seconds=policy.touch_interval_s):
            next_idle = min(now + timedelta(seconds=policy.idle_ttl_s), absolute)
            expected = SessionRecordV1.model_validate(
                record.model_copy(
                    update={
                        "last_seen_at": _utc_text(now),
                        "idle_expires_at": _utc_text(next_idle),
                        "revision": record.revision + 1,
                    }
                ).model_dump(mode="json")
            )
            record = self._auth.touch_session(
                record.session_id,
                expected_revision=record.revision,
                idle_expires_at=expected.idle_expires_at,
            )
            if record != expected:
                raise IntegrityViolation("session touch retained inconsistent content")

        return SessionContextV1(
            session_id=record.session_id,
            principal_id=record.principal_id,
            source_credential_id=record.source_credential_id,
            credential_version=record.credential_version,
            issued_at=record.issued_at,
            absolute_expires_at=record.absolute_expires_at,
            idle_expires_at=record.idle_expires_at,
            session_policy_version=verified.session_policy_version,
        )

    @staticmethod
    def _request_method(value: str) -> str:
        if not isinstance(value, str) or _HTTP_METHOD.fullmatch(value) is None:
            raise AuthFailed("HTTP request method is invalid")
        return value.upper()


__all__ = [
    "HashPolicyResolver",
    "LocalApiKeyAuthenticator",
    "LocalPasswordAuthenticator",
    "LocalSessionRuntime",
    "NormalizationPolicyResolver",
    "SessionIdGenerator",
    "SessionPolicyResolver",
]
