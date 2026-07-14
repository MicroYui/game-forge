from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import json

import pytest
from pydantic import ValidationError

from gameforge.apps.api.pagination import (
    MAX_OPAQUE_PAGE_CURSOR_CHARS,
    OpaquePageCursorCodec,
    principal_binding,
    to_opaque_page,
)
from gameforge.contracts.api import OpaquePageV1
from gameforge.contracts.canonical import canonical_json, canonical_sha256
from gameforge.contracts.errors import CursorInvalid
from gameforge.contracts.storage import PageV1, ReadSnapshotV1
from gameforge.runtime.persistence.cursor import CursorSigner


NOW = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
QUERY_HASH = canonical_sha256({"resource": "runs", "filter": "queued"})


@dataclass(frozen=True)
class _Clock:
    def now_utc(self) -> datetime:
        return NOW


def _snapshot() -> ReadSnapshotV1:
    return ReadSnapshotV1(
        snapshot_id="read-snapshot:runs:1",
        resource_kind="runs",
        query_hash=QUERY_HASH,
        authz_fingerprint="a" * 64,
        stable_sort_schema_id="runs-created-at-id@1",
        strategy="materialized_view",
        materialized_item_count=3,
        created_at="2026-07-14T09:00:00Z",
        expires_at="2026-07-14T09:05:00Z",
    )


def _cursor():
    return CursorSigner(
        signing_key=b"opaque-page-cursor-test-signing-key",
        clock=_Clock(),
    ).issue(
        snapshot=_snapshot(),
        position=canonical_json(
            {
                "position_schema_version": "materialized-position@1",
                "ordinal": 2,
                "principal_binding": "b" * 64,
            }
        ),
        page_size=2,
    )


def _encode_raw(value: object) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_codec_round_trips_one_canonical_bounded_opaque_token() -> None:
    codec = OpaquePageCursorCodec()
    cursor = _cursor()

    token = codec.encode(cursor)

    assert 1 <= len(token) <= MAX_OPAQUE_PAGE_CURSOR_CHARS
    assert "=" not in token
    assert codec.decode(token) == cursor
    assert token == codec.encode(codec.decode(token))


@pytest.mark.parametrize(
    "token",
    [
        "",
        "has=padding",
        "not+base64url",
        "é",
        "a" * (MAX_OPAQUE_PAGE_CURSOR_CHARS + 1),
        _encode_raw([]),
        _encode_raw({"unexpected": "shape"}),
    ],
)
def test_codec_rejects_malformed_noncanonical_or_unbounded_tokens(token: str) -> None:
    with pytest.raises(CursorInvalid):
        OpaquePageCursorCodec().decode(token)


def test_codec_rejects_valid_json_with_extra_fields_or_noncanonical_framing() -> None:
    payload = _cursor().model_dump(mode="json")
    with pytest.raises(CursorInvalid, match="schema"):
        OpaquePageCursorCodec().decode(_encode_raw({**payload, "extra": "field"}))

    reordered = json.dumps(payload, separators=(", ", ": ")).encode("utf-8")
    token = base64.urlsafe_b64encode(reordered).decode("ascii").rstrip("=")
    with pytest.raises(CursorInvalid, match="canonical"):
        OpaquePageCursorCodec().decode(token)


def test_codec_rejects_noncanonical_base64url_pad_bits() -> None:
    token = OpaquePageCursorCodec().encode(_cursor())
    assert len(token) % 4 == 3
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    last_index = alphabet.index(token[-1])
    assert last_index % 4 == 0
    replacement = alphabet[last_index + 1]
    alias = f"{token[:-1]}{replacement}"
    assert base64.urlsafe_b64decode(alias + "=") == base64.urlsafe_b64decode(token + "=")

    with pytest.raises(CursorInvalid, match="canonical"):
        OpaquePageCursorCodec().decode(alias)


def test_codec_revalidates_constructed_cursor_before_encoding() -> None:
    invalid = _cursor().model_copy(update={"page_size": 0})
    with pytest.raises(ValidationError):
        OpaquePageCursorCodec().encode(invalid)


def test_http_page_exposes_only_the_opaque_token_not_structured_cursor_fields() -> None:
    internal = PageV1[str](
        read_snapshot_id=_snapshot().snapshot_id,
        items=("run:a", "run:b"),
        next_cursor=_cursor(),
        expires_at=_snapshot().expires_at,
    )

    page = to_opaque_page(internal, codec=OpaquePageCursorCodec())
    wire = page.model_dump(mode="json")

    assert isinstance(page, OpaquePageV1)
    assert page.items == internal.items
    assert isinstance(wire["next_cursor"], str)
    assert "opaque_signature" not in wire["next_cursor"]


def test_principal_binding_is_stable_and_binds_id_and_kind() -> None:
    value = principal_binding(principal_id="human:a", principal_kind="human")

    assert value == principal_binding(principal_id="human:a", principal_kind="human")
    assert value != principal_binding(principal_id="human:b", principal_kind="human")
    assert value != principal_binding(principal_id="human:a", principal_kind="service")
    assert len(value) == 64

    with pytest.raises(ValueError, match="principal_id"):
        principal_binding(principal_id="", principal_kind="human")
