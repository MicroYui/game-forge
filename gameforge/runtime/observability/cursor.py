"""Signed opaque cursors shared by local telemetry query adapters."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import CursorExpired, CursorInvalid, Forbidden
from gameforge.contracts.storage import UtcClock


@dataclass(frozen=True, slots=True)
class TelemetryCursorState:
    kind: str
    snapshot_id: str
    query_hash: str
    authz_fingerprint: str
    offset: int
    page_limit: int
    expires_at: datetime
    cursor_schema_version: str = "telemetry-cursor@1"
    principal_binding: str | None = None


class OpaqueCursorCodec:
    __slots__ = ("_clock", "_signing_key")

    def __init__(self, *, signing_key: bytes, clock: UtcClock) -> None:
        if not isinstance(signing_key, bytes) or not signing_key:
            raise ValueError("telemetry cursor signing key must be non-empty bytes")
        self._signing_key = signing_key
        self._clock = clock

    def issue(
        self,
        *,
        kind: str,
        snapshot_id: str,
        query_hash: str,
        authz_fingerprint: str,
        principal_binding: str | None = None,
        offset: int,
        page_limit: int,
        expires_at: datetime,
    ) -> str:
        if offset < 0 or page_limit <= 0:
            raise ValueError("telemetry cursor position and page limit are invalid")
        if (
            principal_binding is not None
            and re.fullmatch(r"[0-9a-f]{64}", principal_binding) is None
        ):
            raise ValueError("telemetry cursor principal binding must be a SHA-256 digest")
        schema_version = (
            "telemetry-cursor@2" if principal_binding is not None else "telemetry-cursor@1"
        )
        payload = {
            "cursor_schema_version": schema_version,
            "kind": kind,
            "snapshot_id": snapshot_id,
            "query_hash": query_hash,
            "authz_fingerprint": authz_fingerprint,
            "offset": offset,
            "page_limit": page_limit,
            "expires_at": self._format_utc(expires_at),
        }
        if principal_binding is not None:
            payload["principal_binding"] = principal_binding
        signature = self._sign(payload)
        envelope = canonical_json({"payload": payload, "signature": signature}).encode("utf-8")
        return base64.urlsafe_b64encode(envelope).decode("ascii").rstrip("=")

    def verify(
        self,
        token: str,
        *,
        expected_kind: str,
        expected_query_hash: str,
        expected_authz_fingerprint: str,
        expected_page_limit: int,
        expected_principal_binding: str | None = None,
        expected_legacy_query_hash: str | None = None,
    ) -> TelemetryCursorState:
        try:
            padding = "=" * (-len(token) % 4)
            decoded = base64.b64decode(
                token + padding,
                altchars=b"-_",
                validate=True,
            )
            envelope = json.loads(decoded)
            payload = envelope["payload"]
            signature = envelope["signature"]
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise CursorInvalid("telemetry cursor is malformed") from exc
        if not isinstance(payload, dict) or not isinstance(signature, str):
            raise CursorInvalid("telemetry cursor envelope is invalid")
        expected_signature = self._sign(payload)
        if not hmac.compare_digest(signature, expected_signature):
            raise CursorInvalid("telemetry cursor signature is invalid")
        common_required = {
            "cursor_schema_version",
            "kind",
            "snapshot_id",
            "query_hash",
            "authz_fingerprint",
            "offset",
            "page_limit",
            "expires_at",
        }
        schema_version = payload.get("cursor_schema_version")
        required = (
            common_required
            if schema_version == "telemetry-cursor@1"
            else common_required | {"principal_binding"}
            if schema_version == "telemetry-cursor@2"
            else set()
        )
        if not required or set(payload) != required:
            raise CursorInvalid("telemetry cursor schema is invalid")
        expected_hash = (
            expected_legacy_query_hash
            if schema_version == "telemetry-cursor@1" and expected_legacy_query_hash is not None
            else expected_query_hash
        )
        if (
            payload["kind"] != expected_kind
            or payload["query_hash"] != expected_hash
            or payload["page_limit"] != expected_page_limit
        ):
            raise CursorInvalid("telemetry cursor belongs to another query")
        principal_binding: str | None = None
        if schema_version == "telemetry-cursor@2":
            principal_binding = payload["principal_binding"]
            if (
                not isinstance(principal_binding, str)
                or re.fullmatch(r"[0-9a-f]{64}", principal_binding) is None
                or expected_principal_binding is None
            ):
                raise CursorInvalid("telemetry cursor principal binding is invalid")
            if principal_binding != expected_principal_binding:
                raise Forbidden("telemetry cursor belongs to another principal")
            if payload["authz_fingerprint"] != expected_authz_fingerprint:
                raise CursorExpired("telemetry cursor authorization is no longer current")
        elif payload["authz_fingerprint"] != expected_authz_fingerprint:
            raise CursorInvalid("telemetry cursor belongs to another query")
        if (
            not isinstance(payload["snapshot_id"], str)
            or not payload["snapshot_id"]
            or isinstance(payload["offset"], bool)
            or not isinstance(payload["offset"], int)
            or payload["offset"] < 0
        ):
            raise CursorInvalid("telemetry cursor position is invalid")
        expires_at = self._parse_utc(payload["expires_at"])
        now = self._clock.now_utc()
        if now.tzinfo is None or now.utcoffset() != UTC.utcoffset(now):
            raise CursorExpired("telemetry cursor clock is not UTC")
        if now >= expires_at:
            raise CursorExpired("telemetry cursor has expired")
        return TelemetryCursorState(
            cursor_schema_version=schema_version,
            kind=payload["kind"],
            snapshot_id=payload["snapshot_id"],
            query_hash=payload["query_hash"],
            authz_fingerprint=payload["authz_fingerprint"],
            principal_binding=principal_binding,
            offset=payload["offset"],
            page_limit=payload["page_limit"],
            expires_at=expires_at,
        )

    def _sign(self, payload: dict[str, object]) -> str:
        return hmac.new(
            self._signing_key,
            canonical_json(payload).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _format_utc(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
            raise ValueError("telemetry cursor expiry must be UTC")
        return value.isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_utc(value: object) -> datetime:
        if not isinstance(value, str):
            raise CursorInvalid("telemetry cursor expiry is invalid")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CursorInvalid("telemetry cursor expiry is invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
            raise CursorInvalid("telemetry cursor expiry is not UTC")
        return parsed


__all__ = ["OpaqueCursorCodec", "TelemetryCursorState"]
