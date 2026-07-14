from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from urllib.parse import parse_qs, urlencode, urlparse

from pydantic import ValidationError
import pytest
from sqlalchemy import Engine, update
from sqlalchemy.orm import Session

from gameforge.contracts.auth import (
    ApiKeyAuthRequestV1,
    ApiKeyRecordV1,
    LoginNameNormalizationPolicyV1,
    OidcAuthorizationRedirectV1,
    OidcBeginRequestV1,
    OidcCallbackV1,
    OidcCode,
    OidcIdentityV1,
    OidcProvider,
    PasswordAuthRequestV1,
    PasswordCredentialRecordV1,
    PasswordHashPolicyV1,
    SecretText,
    SessionIssueRequestV1,
    SessionManager,
    SessionPolicyV1,
    compute_login_name_normalization_policy_digest,
)
from gameforge.contracts.errors import (
    AuthFailed,
    CredentialDisabled,
    CredentialExpired,
    IntegrityViolation,
    OidcStateInvalid,
    SessionExpired,
    SessionRevoked,
)
from gameforge.runtime.auth.local import (
    LocalApiKeyAuthenticator,
    LocalPasswordAuthenticator,
    LocalSessionRuntime,
)
from gameforge.runtime.auth.passwords import Argon2PasswordRuntime
from gameforge.runtime.auth.tokens import (
    ApiKeyRuntime,
    SessionSigningKey,
    SessionSigningKeySet,
    SessionTokenRuntime,
)
from gameforge.runtime.persistence.auth import SqlAuthRepository
from gameforge.runtime.persistence.engine import get_engine
from gameforge.runtime.persistence.identity import SqlIdentityRepository
from gameforge.runtime.persistence.models import Base, SessionRow


T0 = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)


@dataclass
class _Clock:
    current: datetime

    def now_utc(self) -> datetime:
        return self.current


class _Entropy:
    def __init__(self) -> None:
        self._ordinal = 0

    def __call__(self, size: int) -> bytes:
        self._ordinal += 1
        block = hashlib.sha512(f"entropy:{self._ordinal}".encode()).digest()
        return (block * ((size // len(block)) + 1))[:size]


@pytest.fixture
def engine(tmp_path) -> Iterator[Engine]:
    database = get_engine(f"sqlite:///{tmp_path / 'local-auth.db'}")
    Base.metadata.create_all(database)
    yield database
    database.dispose()


def _normalization_policy(
    version: str = "normalization@1",
    *,
    minimum_codepoints: int = 3,
) -> LoginNameNormalizationPolicyV1:
    payload = {
        "policy_version": version,
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


def _hash_policy(version: str, *, iterations: int = 1) -> PasswordHashPolicyV1:
    return PasswordHashPolicyV1(
        policy_version=version,
        algorithm="argon2id",
        memory_kib=8192,
        iterations=iterations,
        parallelism=1,
        salt_bytes=16,
        rehash_on_login=True,
        effective_from="2026-07-14T00:00:00Z",
    )


def _session_policy() -> SessionPolicyV1:
    return SessionPolicyV1(
        policy_version="session@1",
        absolute_ttl_s=3600,
        idle_ttl_s=600,
        touch_interval_s=60,
        signing_key_set_version="keys@1",
        csrf_mode="synchronizer_token",
        same_site="strict",
        secure_cookie_required=True,
    )


def _password_record(
    password_runtime: Argon2PasswordRuntime,
    hash_policy: PasswordHashPolicyV1,
    *,
    principal_id: str = "human:alice",
    credential_version: int = 1,
    normalization_policy: LoginNameNormalizationPolicyV1 | None = None,
) -> PasswordCredentialRecordV1:
    normalization = normalization_policy or _normalization_policy()
    return PasswordCredentialRecordV1(
        credential_id="password:alice",
        principal_id=principal_id,
        normalized_login_name="alice",
        normalization_policy_version=normalization.policy_version,
        normalization_policy_digest=normalization.policy_digest,
        password_hash=password_runtime.hash_password(SecretText("correct-password"), hash_policy),
        hash_policy_version=hash_policy.policy_version,
        credential_version=credential_version,
        status="active",
        changed_at="2026-07-14T08:00:00Z",
        revision=1,
    )


def _repositories(
    session: Session,
    clock: _Clock,
) -> tuple[SqlAuthRepository, SqlIdentityRepository]:
    return (
        SqlAuthRepository(session, clock=clock),
        SqlIdentityRepository(session, clock=clock),
    )


def _create_human(
    auth: SqlAuthRepository,
    identities: SqlIdentityRepository,
    password_runtime: Argon2PasswordRuntime,
    hash_policy: PasswordHashPolicyV1,
) -> PasswordCredentialRecordV1:
    identities.create(principal_id="human:alice", kind="human", display_name="Alice")
    return auth.create_password(_password_record(password_runtime, hash_policy))


def _token_runtime(entropy: _Entropy | None = None) -> SessionTokenRuntime:
    key_set = SessionSigningKeySet(
        key_set_version="keys@1",
        keys=(
            SessionSigningKey(
                key_id="session-key-1",
                secret=b"s" * 32,
                status="active",
            ),
        ),
    )
    return SessionTokenRuntime(
        key_set_resolver=lambda version: key_set if version == key_set.key_set_version else None,
        token_digest_key=b"t" * 32,
        csrf_digest_key=b"c" * 32,
        random_bytes=entropy or _Entropy(),
    )


def _session_runtime(
    auth: SqlAuthRepository,
    identities: SqlIdentityRepository,
    clock: _Clock,
    *,
    token_runtime: SessionTokenRuntime | None = None,
    policy_resolver=None,
) -> LocalSessionRuntime:
    policy = _session_policy()
    return LocalSessionRuntime(
        auth_repository=auth,
        identity_repository=identities,
        session_policy_resolver=(
            policy_resolver
            if policy_resolver is not None
            else lambda version: policy if version == policy.policy_version else None
        ),
        token_runtime=token_runtime or _token_runtime(),
        clock=clock,
        session_id_generator=lambda: "session:alice:1",
    )


def test_password_login_uses_exact_bound_policies_and_cas_rehashes_in_transaction(
    engine: Engine,
) -> None:
    clock = _Clock(T0)
    password_runtime = Argon2PasswordRuntime(random_bytes=_Entropy())
    old_policy = _hash_policy("argon2@1")
    current_policy = _hash_policy("argon2@2", iterations=2)

    with Session(engine) as session:
        auth, identities = _repositories(session, clock)
        original = _create_human(auth, identities, password_runtime, old_policy)
        session.commit()

        authenticator = LocalPasswordAuthenticator(
            auth_repository=auth,
            identity_repository=identities,
            normalization_policy_resolver=lambda version, digest: (
                _normalization_policy()
                if (version, digest)
                == (
                    _normalization_policy().policy_version,
                    _normalization_policy().policy_digest,
                )
                else None
            ),
            hash_policy_resolver=lambda version: {
                old_policy.policy_version: old_policy,
                current_policy.policy_version: current_policy,
            }.get(version),
            current_hash_policy=current_policy,
            password_runtime=password_runtime,
            clock=clock,
        )
        result = authenticator.verify_password(
            PasswordAuthRequestV1(
                login_name="\u3000ＡＬＩＣＥ\u00a0",
                password=SecretText("correct-password"),
            )
        )
        retained = auth.get_password(original.credential_id)

        assert result.principal_id == "human:alice"
        assert result.principal_kind == "human"
        assert result.credential_version == original.credential_version
        assert retained is not None
        assert retained.hash_policy_version == current_policy.policy_version
        assert retained.revision == original.revision + 1
        assert retained.credential_version == original.credential_version
        assert password_runtime.verify_password(
            SecretText("correct-password"), retained.password_hash, current_policy
        )

        session.rollback()
        session.expire_all()
        assert auth.get_password(original.credential_id) == original


def test_password_login_fails_closed_for_bad_secret_policy_binding_and_principal_kind(
    engine: Engine,
) -> None:
    clock = _Clock(T0)
    password_runtime = Argon2PasswordRuntime(random_bytes=_Entropy())
    policy = _hash_policy("argon2@1")

    with Session(engine) as session:
        auth, identities = _repositories(session, clock)
        _create_human(auth, identities, password_runtime, policy)
        session.commit()
        base = dict(
            auth_repository=auth,
            identity_repository=identities,
            normalization_policy_resolver=lambda version, digest: (
                _normalization_policy()
                if (version, digest)
                == (
                    _normalization_policy().policy_version,
                    _normalization_policy().policy_digest,
                )
                else None
            ),
            hash_policy_resolver=lambda version: (
                policy if version == policy.policy_version else None
            ),
            current_hash_policy=policy,
            password_runtime=password_runtime,
            clock=clock,
        )

        with pytest.raises(AuthFailed):
            LocalPasswordAuthenticator(**base).verify_password(
                PasswordAuthRequestV1(login_name="alice", password=SecretText("wrong"))
            )
        with pytest.raises(AuthFailed):
            LocalPasswordAuthenticator(**base).verify_password(
                PasswordAuthRequestV1(login_name="   ", password=SecretText("wrong"))
            )
        with pytest.raises(IntegrityViolation, match="hash policy"):
            LocalPasswordAuthenticator(
                **{**base, "hash_policy_resolver": lambda version: None}
            ).verify_password(
                PasswordAuthRequestV1(login_name="alice", password=SecretText("correct-password"))
            )
        with pytest.raises(IntegrityViolation, match="normalization policy"):
            LocalPasswordAuthenticator(
                **{**base, "normalization_policy_resolver": lambda version, digest: None}
            ).verify_password(
                PasswordAuthRequestV1(login_name="alice", password=SecretText("correct-password"))
            )

        principal = identities.get("human:alice")
        assert principal is not None
        identities.disable(
            principal.principal_id,
            disabled_reason="security",
            expected_revision=principal.revision,
        )
        with pytest.raises(CredentialDisabled):
            LocalPasswordAuthenticator(**base).verify_password(
                PasswordAuthRequestV1(login_name="alice", password=SecretText("correct-password"))
            )


def test_password_login_uses_the_credential_bound_historical_normalization_policy(
    engine: Engine,
) -> None:
    clock = _Clock(T0)
    password_runtime = Argon2PasswordRuntime(random_bytes=_Entropy())
    hash_policy = _hash_policy("argon2@1")
    historical = _normalization_policy("normalization@1", minimum_codepoints=1)
    current = _normalization_policy("normalization@2", minimum_codepoints=3)

    with Session(engine) as session:
        auth, identities = _repositories(session, clock)
        identities.create(principal_id="human:alice", kind="human", display_name="Alice")
        auth.create_password(
            _password_record(
                password_runtime,
                hash_policy,
                normalization_policy=historical,
            )
        )
        session.commit()
        policies = {
            (historical.policy_version, historical.policy_digest): historical,
            (current.policy_version, current.policy_digest): current,
        }

        result = LocalPasswordAuthenticator(
            auth_repository=auth,
            identity_repository=identities,
            normalization_policy_resolver=lambda version, digest: policies.get((version, digest)),
            hash_policy_resolver=lambda version: (
                hash_policy if version == hash_policy.policy_version else None
            ),
            current_hash_policy=hash_policy,
            password_runtime=password_runtime,
            clock=clock,
        ).verify_password(
            PasswordAuthRequestV1(
                login_name="\u3000ＡＬＩＣＥ\u00a0",
                password=SecretText("correct-password"),
            )
        )

        assert result.principal_id == "human:alice"


def test_api_key_authenticates_only_active_unexpired_service_credentials(
    engine: Engine,
) -> None:
    clock = _Clock(T0)
    key_runtime = ApiKeyRuntime(digest_key=b"a" * 32, random_bytes=_Entropy())
    issued = key_runtime.issue()

    with Session(engine) as session:
        auth, identities = _repositories(session, clock)
        identities.create(principal_id="service:worker", kind="service", display_name="Worker")
        record = auth.create_api_key(
            ApiKeyRecordV1(
                api_key_id="api-key:worker:1",
                principal_id="service:worker",
                key_prefix=issued.key_prefix,
                key_digest=issued.key_digest,
                credential_version=1,
                status="active",
                created_at="2026-07-14T08:00:00Z",
                expires_at="2026-07-14T09:00:00Z",
                revision=1,
            )
        )
        session.commit()
        authenticator = LocalApiKeyAuthenticator(
            auth_repository=auth,
            identity_repository=identities,
            api_key_runtime=key_runtime,
            clock=clock,
        )

        result = authenticator.authenticate(ApiKeyAuthRequestV1(api_key=issued.api_key))
        assert (result.principal_id, result.principal_kind, result.credential_id) == (
            "service:worker",
            "service",
            record.api_key_id,
        )

        with pytest.raises(AuthFailed):
            authenticator.authenticate(
                ApiKeyAuthRequestV1(api_key=type(issued.api_key)("gfk_unknown.value"))
            )
        clock.current = T0 + timedelta(hours=1)
        with pytest.raises(CredentialExpired):
            authenticator.authenticate(ApiKeyAuthRequestV1(api_key=issued.api_key))


def test_api_key_rejects_revoked_or_non_service_principal_binding(engine: Engine) -> None:
    clock = _Clock(T0)
    key_runtime = ApiKeyRuntime(digest_key=b"a" * 32, random_bytes=_Entropy())
    issued = key_runtime.issue()

    with Session(engine) as session:
        auth, identities = _repositories(session, clock)
        identities.create(principal_id="human:alice", kind="human", display_name="Alice")
        auth.create_api_key(
            ApiKeyRecordV1(
                api_key_id="api-key:bad-binding",
                principal_id="human:alice",
                key_prefix=issued.key_prefix,
                key_digest=issued.key_digest,
                credential_version=1,
                status="active",
                created_at="2026-07-14T08:00:00Z",
                revision=1,
            )
        )
        authenticator = LocalApiKeyAuthenticator(
            auth_repository=auth,
            identity_repository=identities,
            api_key_runtime=key_runtime,
            clock=clock,
        )
        with pytest.raises(IntegrityViolation, match="service principal"):
            authenticator.authenticate(ApiKeyAuthRequestV1(api_key=issued.api_key))


def test_session_issue_resolve_csrf_and_touch_return_only_frozen_transport_dtos(
    engine: Engine,
) -> None:
    clock = _Clock(T0)
    password_runtime = Argon2PasswordRuntime(random_bytes=_Entropy())
    hash_policy = _hash_policy("argon2@1")

    with Session(engine) as session:
        auth, identities = _repositories(session, clock)
        password = _create_human(auth, identities, password_runtime, hash_policy)
        runtime = _session_runtime(auth, identities, clock)
        issued = runtime.issue(
            SessionIssueRequestV1(
                principal_id=password.principal_id,
                source_credential_id=password.credential_id,
                credential_version=password.credential_version,
                session_policy_version="session@1",
            )
        )
        session.commit()

        stored = auth.get_session(issued.session_id)
        assert stored is not None
        assert issued.session_token.get_secret_value() not in stored.model_dump_json()
        assert issued.csrf_token.get_secret_value() not in stored.model_dump_json()

        initial = runtime.resolve(
            issued.session_token,
            csrf_token=None,
            request_method="GET",
        )
        assert initial.session_id == issued.session_id
        assert auth.get_session(issued.session_id).revision == 1  # type: ignore[union-attr]

        clock.current += timedelta(seconds=61)
        touched = runtime.resolve(
            issued.session_token,
            csrf_token=issued.csrf_token,
            request_method="POST",
        )
        retained = auth.get_session(issued.session_id)
        assert retained is not None
        assert retained.revision == 2
        assert retained.last_seen_at == "2026-07-14T08:01:01Z"
        assert touched.idle_expires_at == retained.idle_expires_at
        with pytest.raises(ValidationError):
            touched.idle_expires_at = "changed"

        with pytest.raises(AuthFailed, match="CSRF"):
            runtime.resolve(
                issued.session_token,
                csrf_token=SecretText("wrong"),
                request_method="PATCH",
            )


def test_session_resolve_rejects_revocation_expiry_and_stale_password_version(
    engine: Engine,
) -> None:
    clock = _Clock(T0)
    password_runtime = Argon2PasswordRuntime(random_bytes=_Entropy())
    hash_policy = _hash_policy("argon2@1")

    with Session(engine) as session:
        auth, identities = _repositories(session, clock)
        password = _create_human(auth, identities, password_runtime, hash_policy)
        runtime = _session_runtime(auth, identities, clock)
        issued = runtime.issue(
            SessionIssueRequestV1(
                principal_id=password.principal_id,
                source_credential_id=password.credential_id,
                credential_version=password.credential_version,
                session_policy_version="session@1",
            )
        )
        session.commit()

        rotated = password.model_copy(
            update={
                "credential_version": 2,
                "changed_at": "2026-07-14T08:00:01Z",
                "revision": 2,
            }
        )
        auth.compare_and_set_password(rotated, expected_revision=1)
        with pytest.raises(CredentialDisabled, match="version"):
            runtime.resolve(issued.session_token, csrf_token=None, request_method="GET")
        session.rollback()
        session.expire_all()

        clock.current = T0 + timedelta(minutes=10)
        with pytest.raises(SessionExpired):
            runtime.resolve(issued.session_token, csrf_token=None, request_method="GET")
        clock.current = T0 + timedelta(minutes=1)
        current = auth.get_session(issued.session_id)
        assert current is not None
        auth.revoke_session(current.session_id, expected_revision=current.revision, reason="logout")
        with pytest.raises(SessionRevoked):
            runtime.resolve(issued.session_token, csrf_token=None, request_method="GET")


def test_session_fails_closed_on_unknown_policy_or_record_key_mismatch(engine: Engine) -> None:
    clock = _Clock(T0)
    password_runtime = Argon2PasswordRuntime(random_bytes=_Entropy())
    hash_policy = _hash_policy("argon2@1")
    token_runtime = _token_runtime()

    with Session(engine) as session:
        auth, identities = _repositories(session, clock)
        password = _create_human(auth, identities, password_runtime, hash_policy)
        runtime = _session_runtime(
            auth,
            identities,
            clock,
            token_runtime=token_runtime,
        )
        issued = runtime.issue(
            SessionIssueRequestV1(
                principal_id=password.principal_id,
                source_credential_id=password.credential_id,
                credential_version=password.credential_version,
                session_policy_version="session@1",
            )
        )
        session.commit()

        unknown_policy = _session_runtime(
            auth,
            identities,
            clock,
            token_runtime=token_runtime,
            policy_resolver=lambda version: None,
        )
        with pytest.raises(IntegrityViolation, match="session policy"):
            unknown_policy.resolve(issued.session_token, csrf_token=None, request_method="GET")

        session.execute(
            update(SessionRow)
            .where(SessionRow.session_id == issued.session_id)
            .values(signing_key_id="different-key")
        )
        session.commit()
        session.expire_all()
        with pytest.raises(IntegrityViolation, match="signing key"):
            runtime.resolve(issued.session_token, csrf_token=None, request_method="GET")


def test_session_reloads_principal_and_checks_absolute_expiry(engine: Engine) -> None:
    clock = _Clock(T0)
    password_runtime = Argon2PasswordRuntime(random_bytes=_Entropy())
    hash_policy = _hash_policy("argon2@1")

    with Session(engine) as session:
        auth, identities = _repositories(session, clock)
        password = _create_human(auth, identities, password_runtime, hash_policy)
        runtime = _session_runtime(auth, identities, clock)
        issued = runtime.issue(
            SessionIssueRequestV1(
                principal_id=password.principal_id,
                source_credential_id=password.credential_id,
                credential_version=password.credential_version,
                session_policy_version="session@1",
            )
        )
        session.commit()

        principal = identities.get(password.principal_id)
        assert principal is not None
        identities.disable(
            principal.principal_id,
            disabled_reason="security",
            expected_revision=principal.revision,
        )
        with pytest.raises(CredentialDisabled, match="principal"):
            runtime.resolve(issued.session_token, csrf_token=None, request_method="GET")
        session.rollback()
        session.expire_all()

        stored = auth.get_session(issued.session_id)
        assert stored is not None
        session.execute(
            update(SessionRow)
            .where(SessionRow.session_id == issued.session_id)
            .values(idle_expires_at=stored.absolute_expires_at)
        )
        session.commit()
        session.expire_all()
        clock.current = T0 + timedelta(hours=1)
        with pytest.raises(SessionExpired):
            runtime.resolve(issued.session_token, csrf_token=None, request_method="GET")


def test_low_level_session_runtime_intentionally_does_not_implement_revoke_protocol(
    engine: Engine,
) -> None:
    clock = _Clock(T0)
    with Session(engine) as session:
        auth, identities = _repositories(session, clock)
        runtime = _session_runtime(auth, identities, clock)

        assert not isinstance(runtime, SessionManager)
        assert not hasattr(runtime, "revoke")


class _DeterministicOidcFake:
    def __init__(self) -> None:
        self._state = "state-1"
        self._nonce = "nonce-1"
        self._pkce_verifier = "pkce-verifier-1"
        self._redirect_uri_id = "console"
        self._consumed = False

    def begin(self, request: OidcBeginRequestV1) -> OidcAuthorizationRedirectV1:
        if request.provider_id != "oidc:test" or request.redirect_uri_id != "console":
            raise OidcStateInvalid("OIDC redirect is not allowed")
        challenge = hashlib.sha256(self._pkce_verifier.encode()).hexdigest()
        query = urlencode(
            {
                "state": self._state,
                "nonce": self._nonce,
                "code_challenge": challenge,
                "redirect_uri_id": request.redirect_uri_id,
            }
        )
        return OidcAuthorizationRedirectV1(
            authorization_url=f"https://issuer.invalid/authorize?{query}",
            state_handle=self._state,
            expires_at="2026-07-14T08:05:00Z",
        )

    def complete(self, callback: OidcCallbackV1) -> OidcIdentityV1:
        expected_code = hashlib.sha256(f"{self._nonce}:{self._pkce_verifier}".encode()).hexdigest()
        if self._consumed:
            raise OidcStateInvalid("OIDC transaction was already consumed")
        if (
            callback.provider_id != "oidc:test"
            or callback.redirect_uri_id != self._redirect_uri_id
            or callback.state.get_secret_value() != self._state
            or callback.code.get_secret_value() != expected_code
        ):
            raise OidcStateInvalid("OIDC state/nonce/PKCE/redirect validation failed")
        self._consumed = True
        return OidcIdentityV1(
            issuer="https://issuer.invalid",
            subject="subject-1",
            claims_digest=hashlib.sha256(b"claims:subject-1").hexdigest(),
            provider_id=callback.provider_id,
        )


def test_deterministic_oidc_protocol_fake_covers_redirect_state_nonce_pkce_and_single_use() -> None:
    provider = _DeterministicOidcFake()
    assert isinstance(provider, OidcProvider)
    redirect = provider.begin(
        OidcBeginRequestV1(provider_id="oidc:test", redirect_uri_id="console")
    )
    query = parse_qs(urlparse(redirect.authorization_url).query)
    assert query["nonce"] == ["nonce-1"]
    assert query["code_challenge"] == [hashlib.sha256(b"pkce-verifier-1").hexdigest()]
    code = hashlib.sha256(b"nonce-1:pkce-verifier-1").hexdigest()
    callback = OidcCallbackV1(
        provider_id="oidc:test",
        state=SecretText(redirect.state_handle),
        code=OidcCode(code),
        redirect_uri_id="console",
    )

    assert provider.complete(callback).subject == "subject-1"
    with pytest.raises(OidcStateInvalid, match="consumed"):
        provider.complete(callback)

    bad_state = _DeterministicOidcFake()
    bad_state.begin(OidcBeginRequestV1(provider_id="oidc:test", redirect_uri_id="console"))
    with pytest.raises(OidcStateInvalid, match="state/nonce/PKCE"):
        bad_state.complete(callback.model_copy(update={"state": SecretText("wrong-state")}))

    bad_proof = _DeterministicOidcFake()
    bad_proof.begin(OidcBeginRequestV1(provider_id="oidc:test", redirect_uri_id="console"))
    with pytest.raises(OidcStateInvalid, match="state/nonce/PKCE"):
        bad_proof.complete(callback.model_copy(update={"code": OidcCode("wrong-code")}))

    with pytest.raises(OidcStateInvalid, match="redirect"):
        _DeterministicOidcFake().begin(
            OidcBeginRequestV1(provider_id="oidc:test", redirect_uri_id="unknown")
        )
