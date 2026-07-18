"""Opaque HTTP framing for signed internal page cursors."""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Annotated, TypeVar

from pydantic import ValidationError, WithJsonSchema

from gameforge.contracts.api import OpaquePageV1
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import CursorInvalid, IntegrityViolation
from gameforge.contracts.storage import MAX_PAGE_ITEMS, PageCursorV1, PageV1
from gameforge.platform.read_models.authorization import principal_identity_binding


MAX_OPAQUE_PAGE_CURSOR_CHARS = 4096
_MAX_DECODED_CURSOR_BYTES = 3072
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
OpaquePageCursorParameter = Annotated[
    str,
    WithJsonSchema({"type": "string", "minLength": 1, "maxLength": MAX_OPAQUE_PAGE_CURSOR_CHARS}),
]
PageLimitParameter = Annotated[
    int,
    WithJsonSchema({"type": "integer", "minimum": 1, "maximum": MAX_PAGE_ITEMS}),
]
T = TypeVar("T")


class OpaquePageCursorCodec:
    """Canonical base64url transport framing; authenticity remains CursorSigner's job."""

    __slots__ = ()

    def encode(self, cursor: PageCursorV1) -> str:
        if not isinstance(cursor, PageCursorV1):
            raise TypeError("cursor must be PageCursorV1")
        canonical = PageCursorV1.model_validate(cursor.model_dump(mode="json"))
        payload = canonical_json(canonical.model_dump(mode="json")).encode("utf-8")
        if len(payload) > _MAX_DECODED_CURSOR_BYTES:
            raise IntegrityViolation("internal page cursor exceeds the transport byte limit")
        token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        if not token or len(token) > MAX_OPAQUE_PAGE_CURSOR_CHARS:
            raise IntegrityViolation("internal page cursor exceeds the transport character limit")
        return token

    def decode(self, token: str) -> PageCursorV1:
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= MAX_OPAQUE_PAGE_CURSOR_CHARS
            or not token.isascii()
            or _BASE64URL.fullmatch(token) is None
        ):
            raise CursorInvalid("opaque page cursor is malformed")
        try:
            decoded = base64.b64decode(
                token + "=" * (-len(token) % 4),
                altchars=b"-_",
                validate=True,
            )
        except (binascii.Error, ValueError) as exc:
            raise CursorInvalid("opaque page cursor is malformed") from exc
        if not decoded or len(decoded) > _MAX_DECODED_CURSOR_BYTES:
            raise CursorInvalid("opaque page cursor is malformed")
        canonical_token = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
        if token != canonical_token:
            raise CursorInvalid("opaque page cursor base64url framing is not canonical")
        try:
            payload = json.loads(decoded)
        except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CursorInvalid("opaque page cursor is malformed") from exc
        if not isinstance(payload, dict):
            raise CursorInvalid("opaque page cursor schema is invalid")
        try:
            cursor = PageCursorV1.model_validate(payload)
        except (TypeError, ValueError, ValidationError) as exc:
            raise CursorInvalid("opaque page cursor schema is invalid") from exc
        try:
            expected = canonical_json(cursor.model_dump(mode="json")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CursorInvalid("opaque page cursor schema is invalid") from exc
        if decoded != expected:
            raise CursorInvalid("opaque page cursor framing is not canonical")
        return cursor


def principal_binding(*, principal_id: str, principal_kind: str) -> str:
    """Compatibility transport helper backed by the platform's single algorithm."""

    return principal_identity_binding(
        principal_id=principal_id,
        principal_kind=principal_kind,
    )


def to_opaque_page(
    page: PageV1[T],
    *,
    codec: OpaquePageCursorCodec,
) -> OpaquePageV1[T]:
    """Project an internal signed-cursor page onto the HTTP transport contract."""

    if not isinstance(page, PageV1):
        raise TypeError("page must be PageV1")
    if not isinstance(codec, OpaquePageCursorCodec):
        raise TypeError("codec must be OpaquePageCursorCodec")
    return OpaquePageV1[T](
        read_snapshot_id=page.read_snapshot_id,
        items=page.items,
        next_cursor=None if page.next_cursor is None else codec.encode(page.next_cursor),
        expires_at=page.expires_at,
    )


__all__ = [
    "MAX_OPAQUE_PAGE_CURSOR_CHARS",
    "OpaquePageCursorCodec",
    "OpaquePageCursorParameter",
    "PageLimitParameter",
    "principal_binding",
    "to_opaque_page",
]
