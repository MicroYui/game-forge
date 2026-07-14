"""Pure M4c authentication, credential, session, and OIDC contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from gameforge.contracts.canonical import canonical_sha256
from gameforge.contracts.errors import AuthError
from gameforge.contracts.identity import PrincipalKind
from gameforge.contracts.lineage import AuditActor


BoundedId = Annotated[str, StringConstraints(min_length=1, max_length=512)]
BoundedText = Annotated[str, StringConstraints(min_length=1, max_length=4096)]
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PositiveInt = Annotated[int, Field(ge=1)]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class SecretText(SecretStr):
    """A transport-only secret whose representation and serialization are redacted."""


class SessionToken(SecretText):
    pass


class ApiKeySecret(SecretText):
    pass


class OidcCode(SecretText):
    pass


PasswordSecretValue = Annotated[SecretText, Field(min_length=1, max_length=4096)]
ApiKeySecretValue = Annotated[ApiKeySecret, Field(min_length=1, max_length=4096)]
SessionTokenValue = Annotated[SessionToken, Field(min_length=1, max_length=8192)]
CsrfSecretValue = Annotated[SecretText, Field(min_length=1, max_length=4096)]
OidcStateValue = Annotated[SecretText, Field(min_length=1, max_length=4096)]
OidcCodeValue = Annotated[OidcCode, Field(min_length=1, max_length=8192)]


class PasswordAuthRequestV1(_FrozenModel):
    schema_version: Literal["password-auth@1"] = "password-auth@1"
    login_name: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    password: PasswordSecretValue


class ApiKeyAuthRequestV1(_FrozenModel):
    schema_version: Literal["api-key-auth@1"] = "api-key-auth@1"
    api_key: ApiKeySecretValue


class AuthenticationResultV1(_FrozenModel):
    result_schema_version: Literal["authentication-result@1"] = "authentication-result@1"
    principal_id: BoundedId
    principal_kind: PrincipalKind
    credential_id: BoundedId
    credential_version: PositiveInt
    authenticated_at: BoundedText


LoginNameRejectedCategory = Literal["control", "surrogate", "private_use"]
_REQUIRED_REJECTED_CATEGORIES = ("control", "private_use", "surrogate")


def _normalization_policy_payload(value: Mapping[str, Any] | BaseModel) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        raw = value.model_dump(mode="json", exclude={"policy_digest"})
    else:
        raw = dict(value)
        raw.pop("policy_digest", None)
    raw.setdefault("policy_schema_version", "login-name-normalization@1")
    if "reject_categories" in raw:
        raw["reject_categories"] = sorted(set(raw["reject_categories"]))
    return raw


def compute_login_name_normalization_policy_digest(
    value: Mapping[str, Any] | BaseModel,
) -> str:
    return canonical_sha256(_normalization_policy_payload(value))


class LoginNameNormalizationPolicyV1(_FrozenModel):
    policy_schema_version: Literal["login-name-normalization@1"] = "login-name-normalization@1"
    policy_version: BoundedId
    unicode_normalization: Literal["NFKC"]
    trim_unicode_whitespace: Literal[True]
    case_mapping: Literal["unicode_casefold"]
    reject_categories: tuple[LoginNameRejectedCategory, ...]
    minimum_codepoints: Annotated[int, Field(ge=1, le=256)]
    maximum_codepoints: Annotated[int, Field(ge=1, le=256)]
    policy_digest: Sha256Hex

    @field_validator("reject_categories")
    @classmethod
    def _canonical_categories(
        cls, value: tuple[LoginNameRejectedCategory, ...]
    ) -> tuple[LoginNameRejectedCategory, ...]:
        canonical = tuple(sorted(set(value)))
        if canonical != _REQUIRED_REJECTED_CATEGORIES:
            raise ValueError("reject_categories must contain all forbidden Unicode categories")
        return canonical

    @model_validator(mode="after")
    def _bounds_and_digest(self) -> "LoginNameNormalizationPolicyV1":
        if self.minimum_codepoints > self.maximum_codepoints:
            raise ValueError("minimum_codepoints cannot exceed maximum_codepoints")
        if self.policy_digest != compute_login_name_normalization_policy_digest(self):
            raise ValueError("policy_digest does not match normalization policy payload")
        return self


class PasswordCredentialRecordV1(_FrozenModel):
    credential_id: BoundedId
    principal_id: BoundedId
    normalized_login_name: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    normalization_policy_version: BoundedId
    normalization_policy_digest: Sha256Hex
    password_hash: BoundedText
    hash_policy_version: BoundedId
    credential_version: PositiveInt
    status: Literal["active", "disabled"]
    changed_at: BoundedText
    revision: PositiveInt


class ApiKeyRecordV1(_FrozenModel):
    api_key_id: BoundedId
    principal_id: BoundedId
    key_prefix: Annotated[str, StringConstraints(min_length=4, max_length=64)]
    key_digest: Sha256Hex
    credential_version: PositiveInt
    status: Literal["active", "revoked", "expired"]
    created_at: BoundedText
    expires_at: BoundedText | None = None
    revoked_at: BoundedText | None = None
    revision: PositiveInt

    @model_validator(mode="after")
    def _status_projection(self) -> "ApiKeyRecordV1":
        if self.status == "revoked" and self.revoked_at is None:
            raise ValueError("revoked API key requires revoked_at")
        if self.status != "revoked" and self.revoked_at is not None:
            raise ValueError("revoked_at belongs only to a revoked API key")
        if self.status == "expired" and self.expires_at is None:
            raise ValueError("expired API key requires expires_at")
        return self


class PasswordHashPolicyV1(_FrozenModel):
    policy_schema_version: Literal["password-hash-policy@1"] = "password-hash-policy@1"
    policy_version: BoundedId
    algorithm: Literal["argon2id"]
    memory_kib: Annotated[int, Field(ge=8192, le=4_194_304)]
    iterations: Annotated[int, Field(ge=1, le=100)]
    parallelism: Annotated[int, Field(ge=1, le=64)]
    salt_bytes: Annotated[int, Field(ge=16, le=1024)]
    rehash_on_login: bool
    effective_from: BoundedText


class SessionPolicyV1(_FrozenModel):
    policy_schema_version: Literal["session-policy@1"] = "session-policy@1"
    policy_version: BoundedId
    absolute_ttl_s: PositiveInt
    idle_ttl_s: PositiveInt
    touch_interval_s: PositiveInt
    signing_key_set_version: BoundedId
    csrf_mode: Literal["synchronizer_token"]
    same_site: Literal["strict", "lax"]
    secure_cookie_required: bool

    @model_validator(mode="after")
    def _ttl_bounds(self) -> "SessionPolicyV1":
        if self.idle_ttl_s > self.absolute_ttl_s:
            raise ValueError("idle_ttl_s cannot exceed absolute_ttl_s")
        if self.touch_interval_s > self.idle_ttl_s:
            raise ValueError("touch_interval_s cannot exceed idle_ttl_s")
        return self


class SessionRecordV1(_FrozenModel):
    session_id: BoundedId
    principal_id: BoundedId
    source_credential_id: BoundedId
    credential_version: PositiveInt
    token_digest: Sha256Hex
    csrf_secret_digest: Sha256Hex
    signing_key_id: BoundedId
    issued_at: BoundedText
    absolute_expires_at: BoundedText
    idle_expires_at: BoundedText
    last_seen_at: BoundedText
    revoked_at: BoundedText | None = None
    revoke_reason: BoundedId | None = None
    revision: PositiveInt

    @model_validator(mode="after")
    def _revocation_projection(self) -> "SessionRecordV1":
        if (self.revoked_at is None) != (self.revoke_reason is None):
            raise ValueError("session revocation fields must be present together")
        return self


class SessionIssueRequestV1(_FrozenModel):
    request_schema_version: Literal["session-issue@1"] = "session-issue@1"
    principal_id: BoundedId
    source_credential_id: BoundedId
    credential_version: PositiveInt
    session_policy_version: BoundedId


class SessionIssueV1(_FrozenModel):
    issue_schema_version: Literal["session-issued@1"] = "session-issued@1"
    session_id: BoundedId
    session_token: SessionTokenValue
    csrf_token: CsrfSecretValue
    absolute_expires_at: BoundedText
    idle_expires_at: BoundedText


class SessionContextV1(_FrozenModel):
    context_schema_version: Literal["session-context@1"] = "session-context@1"
    session_id: BoundedId
    principal_id: BoundedId
    source_credential_id: BoundedId
    credential_version: PositiveInt
    issued_at: BoundedText
    absolute_expires_at: BoundedText
    idle_expires_at: BoundedText
    session_policy_version: BoundedId


class OidcBeginRequestV1(_FrozenModel):
    request_schema_version: Literal["oidc-begin@1"] = "oidc-begin@1"
    provider_id: BoundedId
    redirect_uri_id: BoundedId
    return_to_path: Annotated[str, StringConstraints(min_length=1, max_length=2048)] | None = None


class OidcAuthorizationRedirectV1(_FrozenModel):
    response_schema_version: Literal["oidc-authorization-redirect@1"] = (
        "oidc-authorization-redirect@1"
    )
    authorization_url: Annotated[str, StringConstraints(min_length=1, max_length=8192)]
    state_handle: BoundedId
    expires_at: BoundedText


class OidcTransactionRecordV1(_FrozenModel):
    transaction_id: BoundedId
    provider_id: BoundedId
    state_digest: Sha256Hex
    nonce_digest: Sha256Hex
    sealed_pkce_verifier: BoundedText
    redirect_uri_id: BoundedId
    return_to_path: Annotated[str, StringConstraints(min_length=1, max_length=2048)] | None = None
    created_at: BoundedText
    expires_at: BoundedText
    consumed_at: BoundedText | None = None
    revision: PositiveInt


class OidcCallbackV1(_FrozenModel):
    callback_schema_version: Literal["oidc-callback@1"] = "oidc-callback@1"
    provider_id: BoundedId
    state: OidcStateValue
    code: OidcCodeValue
    redirect_uri_id: BoundedId


class OidcIdentityV1(_FrozenModel):
    identity_schema_version: Literal["oidc-identity@1"] = "oidc-identity@1"
    issuer: BoundedText
    subject: BoundedText
    email: Annotated[str, StringConstraints(min_length=3, max_length=320)] | None = None
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None
    claims_digest: Sha256Hex
    provider_id: BoundedId


@runtime_checkable
class IdentityAuthenticator(Protocol):
    def verify_password(self, request: PasswordAuthRequestV1) -> AuthenticationResultV1: ...


@runtime_checkable
class ApiKeyAuthenticator(Protocol):
    def authenticate(self, request: ApiKeyAuthRequestV1) -> AuthenticationResultV1: ...


@runtime_checkable
class SessionManager(Protocol):
    def issue(self, request: SessionIssueRequestV1) -> SessionIssueV1: ...

    def resolve(
        self,
        token: SessionToken,
        *,
        csrf_token: SecretText | None,
        request_method: str,
    ) -> SessionContextV1: ...

    def revoke(
        self,
        session_id: str,
        *,
        expected_revision: int,
        reason: str,
        actor: AuditActor,
    ) -> SessionRecordV1: ...


@runtime_checkable
class OidcProvider(Protocol):
    def begin(self, request: OidcBeginRequestV1) -> OidcAuthorizationRedirectV1: ...

    def complete(self, callback: OidcCallbackV1) -> OidcIdentityV1: ...


__all__ = [
    "ApiKeyAuthRequestV1",
    "ApiKeyAuthenticator",
    "ApiKeyRecordV1",
    "ApiKeySecret",
    "AuthError",
    "AuthenticationResultV1",
    "IdentityAuthenticator",
    "LoginNameNormalizationPolicyV1",
    "OidcAuthorizationRedirectV1",
    "OidcBeginRequestV1",
    "OidcCallbackV1",
    "OidcCode",
    "OidcIdentityV1",
    "OidcProvider",
    "OidcTransactionRecordV1",
    "PasswordAuthRequestV1",
    "PasswordCredentialRecordV1",
    "PasswordHashPolicyV1",
    "SecretText",
    "SessionContextV1",
    "SessionIssueRequestV1",
    "SessionIssueV1",
    "SessionManager",
    "SessionPolicyV1",
    "SessionRecordV1",
    "SessionToken",
    "compute_login_name_normalization_policy_digest",
]
