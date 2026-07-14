from __future__ import annotations

import itertools

import pytest

from gameforge.contracts.api import ApiKeySecret, SecretText, SessionPolicyV1
from gameforge.contracts.errors import AuthFailed
from gameforge.runtime.auth.tokens import (
    ApiKeyRuntime,
    SessionSigningKey,
    SessionSigningKeySet,
    SessionTokenRuntime,
)


def _policy(*, key_set_version: str = "keys/1") -> SessionPolicyV1:
    return SessionPolicyV1(
        policy_version="session/1",
        absolute_ttl_s=86_400,
        idle_ttl_s=3_600,
        touch_interval_s=60,
        signing_key_set_version=key_set_version,
        csrf_mode="synchronizer_token",
        same_site="strict",
        secure_cookie_required=True,
    )


def test_api_key_issue_returns_plaintext_once_and_derives_stable_lookup() -> None:
    runtime = ApiKeyRuntime(
        digest_key=b"digest-key" * 4,
        random_bytes=lambda size: b"a" * size,
    )

    issued = runtime.issue()
    lookup = runtime.derive_lookup(issued.api_key)

    assert isinstance(issued.api_key, ApiKeySecret)
    assert issued.key_prefix == lookup.key_prefix
    assert issued.key_digest == lookup.key_digest
    assert issued.api_key.get_secret_value() not in repr(issued)
    assert runtime.derive_lookup(ApiKeySecret("gfk_wrong.value")).key_digest != issued.key_digest


def test_session_token_signature_digest_and_csrf_are_bound() -> None:
    key_set = SessionSigningKeySet(
        key_set_version="keys/1",
        keys=(SessionSigningKey(key_id="key-1", secret=b"k" * 32, status="active"),),
    )
    random_values = iter((b"n" * 32, b"c" * 32))
    runtime = SessionTokenRuntime(
        key_set_resolver=lambda version: key_set if version == "keys/1" else None,
        token_digest_key=b"token-digest" * 4,
        csrf_digest_key=b"csrf-digest" * 4,
        random_bytes=lambda size: next(random_values),
    )

    issued = runtime.issue(
        session_id="session-1",
        credential_version=3,
        policy=_policy(),
    )
    verified = runtime.verify(issued.session_token)

    assert verified.session_id == "session-1"
    assert verified.credential_version == 3
    assert verified.session_policy_version == "session/1"
    assert verified.signing_key_id == "key-1"
    assert verified.token_digest == issued.token_digest
    assert runtime.verify_csrf(
        issued.csrf_token,
        token_digest=issued.token_digest,
        expected_digest=issued.csrf_secret_digest,
    )
    assert not runtime.verify_csrf(
        SecretText("wrong-csrf"),
        token_digest=issued.token_digest,
        expected_digest=issued.csrf_secret_digest,
    )
    assert issued.session_token.get_secret_value() not in repr(issued)
    assert issued.csrf_token.get_secret_value() not in repr(issued)


def test_session_key_rotation_accepts_grace_but_never_issues_with_it() -> None:
    key_set = SessionSigningKeySet(
        key_set_version="keys/2",
        keys=(
            SessionSigningKey(key_id="old", secret=b"o" * 32, status="grace"),
            SessionSigningKey(key_id="new", secret=b"n" * 32, status="active"),
        ),
    )
    values = itertools.cycle((b"x" * 32, b"y" * 32))
    runtime = SessionTokenRuntime(
        key_set_resolver=lambda version: key_set,
        token_digest_key=b"t" * 32,
        csrf_digest_key=b"c" * 32,
        random_bytes=lambda size: next(values),
    )

    issued = runtime.issue(
        session_id="session-2",
        credential_version=1,
        policy=_policy(key_set_version="keys/2"),
    )

    assert issued.signing_key_id == "new"
    assert runtime.verify(issued.session_token).signing_key_id == "new"
    assert (b"o" * 32).hex() not in repr(key_set)
    assert "oooooooo" not in repr(key_set)


def test_session_token_tamper_or_unknown_key_fails_closed() -> None:
    key_set = SessionSigningKeySet(
        key_set_version="keys/1",
        keys=(SessionSigningKey(key_id="key-1", secret=b"k" * 32, status="active"),),
    )
    values = itertools.cycle((b"n" * 32, b"c" * 32))
    runtime = SessionTokenRuntime(
        key_set_resolver=lambda version: key_set,
        token_digest_key=b"t" * 32,
        csrf_digest_key=b"c" * 32,
        random_bytes=lambda size: next(values),
    )
    issued = runtime.issue(
        session_id="session-1",
        credential_version=1,
        policy=_policy(),
    )
    token = issued.session_token.get_secret_value()
    tampered = f"{token[:-1]}{'A' if token[-1] != 'A' else 'B'}"

    with pytest.raises(AuthFailed):
        runtime.verify(type(issued.session_token)(tampered))
    with pytest.raises(AuthFailed):
        runtime.verify(type(issued.session_token)(f"unknown.{token.split('.', 1)[1]}"))


@pytest.mark.parametrize(
    ("session_id", "credential_version"),
    [
        ("s" * 513, 1),
        ("session-1", True),
        ("session-1", "1"),
    ],
)
def test_session_issue_rejects_claims_that_verify_would_reject(
    session_id: object,
    credential_version: object,
) -> None:
    key_set = SessionSigningKeySet(
        key_set_version="keys/1",
        keys=(SessionSigningKey(key_id="key-1", secret=b"k" * 32, status="active"),),
    )
    runtime = SessionTokenRuntime(
        key_set_resolver=lambda version: key_set,
        token_digest_key=b"t" * 32,
        csrf_digest_key=b"c" * 32,
        random_bytes=lambda size: b"n" * size,
    )

    with pytest.raises(ValueError, match="session issue inputs"):
        runtime.issue(
            session_id=session_id,  # type: ignore[arg-type]
            credential_version=credential_version,  # type: ignore[arg-type]
            policy=_policy(),
        )


def test_session_signing_key_rejects_unknown_runtime_status() -> None:
    with pytest.raises(ValueError, match="status"):
        SessionSigningKey(
            key_id="key-1",
            secret=b"k" * 32,
            status="retired",  # type: ignore[arg-type]
        )
