from __future__ import annotations

import base64
import json

import pytest

from gameforge.runtime.auth.tokens import SessionSigningKey, SessionSigningKeySet
from gameforge.runtime.secrets.session_keys import (
    SESSION_SIGNING_KEY_SETS_ENV,
    SessionSigningKeyConfigurationError,
    SessionSigningKeyProvider,
)


def _encoded(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _environment_value(*key_sets: dict[str, object]) -> str:
    return json.dumps(list(key_sets), separators=(",", ":"), sort_keys=True)


def _key_set(
    version: str,
    *,
    active_id: str = "active",
    active_secret: bytes = b"a" * 32,
) -> SessionSigningKeySet:
    return SessionSigningKeySet(
        key_set_version=version,
        keys=(
            SessionSigningKey(
                key_id=active_id,
                secret=active_secret,
                status="active",
            ),
        ),
    )


def test_injected_provider_resolves_exact_version_and_redacts_material() -> None:
    old_secret = b"o" * 32
    active_secret = b"n" * 32
    current = SessionSigningKeySet(
        key_set_version="keys/2",
        keys=(
            SessionSigningKey(key_id="old", secret=old_secret, status="grace"),
            SessionSigningKey(key_id="new", secret=active_secret, status="active"),
        ),
    )
    provider = SessionSigningKeyProvider((_key_set("keys/1"), current))

    assert provider.versions == ("keys/1", "keys/2")
    assert provider.resolve("keys/2") is current
    assert provider.resolve("keys/missing") is None
    rendered = repr(provider)
    assert old_secret.decode("ascii") not in rendered
    assert active_secret.decode("ascii") not in rendered


def test_injected_provider_rejects_duplicate_version_or_secret() -> None:
    first = _key_set("keys/1", active_id="first", active_secret=b"a" * 32)
    duplicate_version = _key_set(
        "keys/1",
        active_id="second",
        active_secret=b"b" * 32,
    )
    duplicate_secret = SessionSigningKeySet(
        key_set_version="keys/2",
        keys=(
            SessionSigningKey(key_id="old", secret=b"c" * 32, status="grace"),
            SessionSigningKey(key_id="new", secret=b"c" * 32, status="active"),
        ),
    )

    with pytest.raises(SessionSigningKeyConfigurationError, match="duplicate key-set"):
        SessionSigningKeyProvider((first, duplicate_version))
    with pytest.raises(SessionSigningKeyConfigurationError, match="duplicate secret"):
        SessionSigningKeyProvider((duplicate_secret,))


def test_injected_key_rejects_unknown_status_before_provider_construction() -> None:
    with pytest.raises(ValueError, match="active or grace"):
        SessionSigningKey(
            key_id="unknown",
            secret=b"b" * 32,
            status="retired",  # type: ignore[arg-type]
        )


def test_environment_provider_parses_active_and_grace_deterministically() -> None:
    value = _environment_value(
        {
            "key_set_version": "keys/2",
            "keys": [
                {
                    "key_id": "old",
                    "secret_base64": _encoded(b"o" * 32),
                    "status": "grace",
                },
                {
                    "key_id": "new",
                    "secret_base64": _encoded(b"n" * 32),
                    "status": "active",
                },
            ],
        },
        {
            "key_set_version": "keys/1",
            "keys": [
                {
                    "key_id": "initial",
                    "secret_base64": _encoded(b"i" * 32),
                    "status": "active",
                }
            ],
        },
    )

    provider = SessionSigningKeyProvider.from_environment({SESSION_SIGNING_KEY_SETS_ENV: value})

    assert provider.versions == ("keys/1", "keys/2")
    current = provider.resolve("keys/2")
    assert current is not None
    assert current.active_key.key_id == "new"
    old = current.accepted_key("old")
    assert old is not None
    assert old.status == "grace"


def test_environment_provider_reads_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = _environment_value(
        {
            "key_set_version": "keys/1",
            "keys": [
                {
                    "key_id": "active",
                    "secret_base64": _encoded(b"a" * 32),
                    "status": "active",
                }
            ],
        }
    )
    monkeypatch.setenv(SESSION_SIGNING_KEY_SETS_ENV, value)

    provider = SessionSigningKeyProvider.from_environment()

    assert provider.resolve("keys/1") is not None


@pytest.mark.parametrize("environment", [{}, {SESSION_SIGNING_KEY_SETS_ENV: "  "}])
def test_environment_provider_rejects_missing_configuration(
    environment: dict[str, str],
) -> None:
    with pytest.raises(SessionSigningKeyConfigurationError, match="is required"):
        SessionSigningKeyProvider.from_environment(environment)


@pytest.mark.parametrize(
    ("key_sets", "message"),
    [
        (
            [
                {
                    "key_set_version": "keys/1",
                    "keys": [
                        {
                            "key_id": "same",
                            "secret_base64": _encoded(b"a" * 32),
                            "status": "active",
                        },
                        {
                            "key_id": "same",
                            "secret_base64": _encoded(b"b" * 32),
                            "status": "grace",
                        },
                    ],
                }
            ],
            "key ids must be unique",
        ),
        (
            [
                {
                    "key_set_version": "keys/1",
                    "keys": [
                        {
                            "key_id": "old",
                            "secret_base64": _encoded(b"a" * 32),
                            "status": "grace",
                        }
                    ],
                }
            ],
            "exactly one active",
        ),
        (
            [
                {
                    "key_set_version": "keys/1",
                    "keys": [
                        {
                            "key_id": "one",
                            "secret_base64": _encoded(b"a" * 32),
                            "status": "active",
                        },
                        {
                            "key_id": "two",
                            "secret_base64": _encoded(b"b" * 32),
                            "status": "active",
                        },
                    ],
                }
            ],
            "exactly one active",
        ),
        (
            [
                {
                    "key_set_version": "keys/1",
                    "keys": [
                        {
                            "key_id": "short",
                            "secret_base64": _encoded(b"a" * 31),
                            "status": "active",
                        }
                    ],
                }
            ],
            "at least 32 bytes",
        ),
        (
            [
                {
                    "key_set_version": "keys/1",
                    "keys": [
                        {
                            "key_id": "unknown",
                            "secret_base64": _encoded(b"a" * 32),
                            "status": "retired",
                        }
                    ],
                }
            ],
            "status must be active or grace",
        ),
        (
            [
                {
                    "key_set_version": "keys/1",
                    "keys": [
                        {
                            "key_id": "one",
                            "secret_base64": _encoded(b"a" * 32),
                            "status": "active",
                        }
                    ],
                },
                {
                    "key_set_version": "keys/1",
                    "keys": [
                        {
                            "key_id": "two",
                            "secret_base64": _encoded(b"b" * 32),
                            "status": "active",
                        }
                    ],
                },
            ],
            "duplicate key-set",
        ),
    ],
)
def test_environment_provider_rejects_invalid_key_sets(
    key_sets: list[dict[str, object]],
    message: str,
) -> None:
    environment = {SESSION_SIGNING_KEY_SETS_ENV: json.dumps(key_sets, separators=(",", ":"))}

    with pytest.raises(SessionSigningKeyConfigurationError, match=message):
        SessionSigningKeyProvider.from_environment(environment)


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        "{}",
        "[]",
        json.dumps([{"key_set_version": "keys/1", "keys": [], "extra": True}]),
        json.dumps(
            [
                {
                    "key_set_version": "keys/1",
                    "keys": [
                        {
                            "key_id": "key",
                            "secret_base64": "not-base64!",
                            "status": "active",
                        }
                    ],
                }
            ]
        ),
    ],
)
def test_environment_provider_rejects_malformed_configuration_without_leaking(
    value: str,
) -> None:
    with pytest.raises(SessionSigningKeyConfigurationError):
        SessionSigningKeyProvider.from_environment({SESSION_SIGNING_KEY_SETS_ENV: value})


def test_environment_error_and_provider_repr_do_not_leak_plaintext(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = b"plain-secret-must-not-appear"
    encoded = _encoded(marker)
    value = _environment_value(
        {
            "key_set_version": "keys/1",
            "keys": [
                {
                    "key_id": "short",
                    "secret_base64": encoded,
                    "status": "active",
                }
            ],
        }
    )

    with pytest.raises(SessionSigningKeyConfigurationError) as captured:
        SessionSigningKeyProvider.from_environment({SESSION_SIGNING_KEY_SETS_ENV: value})

    assert marker.decode("ascii") not in str(captured.value)
    assert encoded not in str(captured.value)
    assert marker.decode("ascii") not in caplog.text
    assert encoded not in caplog.text
