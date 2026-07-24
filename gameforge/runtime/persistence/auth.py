"""Transaction-bound SQLite persistence for local credentials and sessions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gameforge.contracts.auth import (
    ApiKeyRecordV1,
    PasswordCredentialRecordV1,
    SessionRecordV1,
)
from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.storage import UtcClock
from gameforge.runtime.persistence.models import ApiKeyRow, PasswordCredentialRow, SessionRow


_RecordT = TypeVar("_RecordT", bound=BaseModel)


def _utc_text(clock: UtcClock) -> str:
    try:
        now = clock.now_utc()
    except (AttributeError, TypeError, ValueError) as exc:
        raise IntegrityViolation("auth repository clock must return UTC") from exc
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
        or now.utcoffset() != timedelta(0)
    ):
        raise IntegrityViolation("auth repository clock must return UTC")
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object, *, field_name: str, stored: bool) -> datetime:
    detail = (
        f"stored {field_name} is not a canonical UTC timestamp"
        if stored
        else f"{field_name} must be a canonical UTC timestamp"
    )
    if not isinstance(value, str) or not value.endswith("Z"):
        raise IntegrityViolation(detail)
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise IntegrityViolation(detail) from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.utcoffset() != timedelta(0)
        or parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") != value
    ):
        raise IntegrityViolation(detail)
    return parsed.astimezone(timezone.utc)


def _require_identifier(value: object, *, field_name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"{field_name} must be a non-empty bounded string")
    return value


def _require_digest(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _canonical_record(
    value: _RecordT,
    model_type: type[_RecordT],
    *,
    label: str,
    stored: bool,
) -> _RecordT:
    detail = f"stored {label} row is invalid" if stored else f"{label} payload is invalid"
    if type(value) is not model_type or set(value.__dict__) != set(model_type.model_fields):
        raise IntegrityViolation(detail)
    wire = value.model_dump(mode="json")
    try:
        parsed = model_type.model_validate(wire)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(detail) from exc
    if parsed != value or canonical_json(parsed.model_dump(mode="json")) != canonical_json(wire):
        raise IntegrityViolation(detail)
    return parsed


def _password_wire(row: PasswordCredentialRow) -> dict[str, Any]:
    return {
        "credential_id": row.credential_id,
        "principal_id": row.principal_id,
        "normalized_login_name": row.normalized_login_name,
        "normalization_policy_version": row.normalization_policy_version,
        "normalization_policy_digest": row.normalization_policy_digest,
        "password_hash": row.password_hash,
        "hash_policy_version": row.hash_policy_version,
        "credential_version": row.credential_version,
        "status": row.status,
        "changed_at": row.changed_at,
        "revision": row.revision,
    }


def _api_key_wire(row: ApiKeyRow) -> dict[str, Any]:
    return {
        "api_key_id": row.api_key_id,
        "principal_id": row.principal_id,
        "key_prefix": row.key_prefix,
        "key_digest": row.key_digest,
        "credential_version": row.credential_version,
        "status": row.status,
        "created_at": row.created_at,
        "expires_at": row.expires_at,
        "revoked_at": row.revoked_at,
        "revision": row.revision,
    }


def _session_wire(row: SessionRow) -> dict[str, Any]:
    return {
        "session_id": row.session_id,
        "principal_id": row.principal_id,
        "source_credential_id": row.source_credential_id,
        "credential_version": row.credential_version,
        "token_digest": row.token_digest,
        "csrf_secret_digest": row.csrf_secret_digest,
        "signing_key_id": row.signing_key_id,
        "issued_at": row.issued_at,
        "absolute_expires_at": row.absolute_expires_at,
        "idle_expires_at": row.idle_expires_at,
        "last_seen_at": row.last_seen_at,
        "revoked_at": row.revoked_at,
        "revoke_reason": row.revoke_reason,
        "revision": row.revision,
    }


def _validate_password(
    value: PasswordCredentialRecordV1,
    *,
    stored: bool,
) -> PasswordCredentialRecordV1:
    record = _canonical_record(
        value,
        PasswordCredentialRecordV1,
        label="password credential",
        stored=stored,
    )
    _parse_utc(record.changed_at, field_name="password credential changed_at", stored=stored)
    return record


def _validate_api_key(value: ApiKeyRecordV1, *, stored: bool) -> ApiKeyRecordV1:
    record = _canonical_record(value, ApiKeyRecordV1, label="API key", stored=stored)
    created = _parse_utc(record.created_at, field_name="API key created_at", stored=stored)
    expires = (
        None
        if record.expires_at is None
        else _parse_utc(record.expires_at, field_name="API key expires_at", stored=stored)
    )
    revoked = (
        None
        if record.revoked_at is None
        else _parse_utc(record.revoked_at, field_name="API key revoked_at", stored=stored)
    )
    if expires is not None and expires <= created:
        detail = (
            "stored API key row has invalid timestamp order"
            if stored
            else "API key expiry must follow creation"
        )
        raise IntegrityViolation(detail)
    if revoked is not None and revoked < created:
        detail = (
            "stored API key row has invalid timestamp order"
            if stored
            else "API key revocation cannot precede creation"
        )
        raise IntegrityViolation(detail)
    return record


def _validate_session(value: SessionRecordV1, *, stored: bool) -> SessionRecordV1:
    record = _canonical_record(value, SessionRecordV1, label="session", stored=stored)
    issued = _parse_utc(record.issued_at, field_name="session issued_at", stored=stored)
    absolute = _parse_utc(
        record.absolute_expires_at,
        field_name="session absolute_expires_at",
        stored=stored,
    )
    idle = _parse_utc(
        record.idle_expires_at,
        field_name="session idle_expires_at",
        stored=stored,
    )
    last_seen = _parse_utc(record.last_seen_at, field_name="session last_seen_at", stored=stored)
    revoked = (
        None
        if record.revoked_at is None
        else _parse_utc(record.revoked_at, field_name="session revoked_at", stored=stored)
    )
    valid_order = issued <= last_seen < idle <= absolute
    valid_revocation = revoked is None or last_seen <= revoked
    if not valid_order or not valid_revocation:
        detail = (
            "stored session row has invalid timestamp order"
            if stored
            else "session timestamps are inconsistent"
        )
        raise IntegrityViolation(detail)
    return record


def _password_from_row(row: PasswordCredentialRow) -> PasswordCredentialRecordV1:
    try:
        record = PasswordCredentialRecordV1.model_validate(_password_wire(row))
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored password credential row is invalid",
            credential_id=row.credential_id,
        ) from exc
    return _validate_password(record, stored=True)


def _api_key_from_row(row: ApiKeyRow) -> ApiKeyRecordV1:
    try:
        record = ApiKeyRecordV1.model_validate(_api_key_wire(row))
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored API key row is invalid", api_key_id=row.api_key_id
        ) from exc
    return _validate_api_key(record, stored=True)


def _session_from_row(row: SessionRow) -> SessionRecordV1:
    try:
        record = SessionRecordV1.model_validate(_session_wire(row))
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored session row is invalid", session_id=row.session_id
        ) from exc
    return _validate_session(record, stored=True)


class SqlAuthRepository:
    """Persist credential/session authority without owning transaction completion."""

    def __init__(self, session: Session, *, clock: UtcClock) -> None:
        if session.get_bind().dialect.name != "sqlite":
            raise ValueError("SqlAuthRepository requires a SQLite session")
        self._session = session
        self._clock = clock

    def create_password(
        self,
        record: PasswordCredentialRecordV1,
    ) -> PasswordCredentialRecordV1:
        candidate = _validate_password(record, stored=False)
        if candidate.revision != 1 or candidate.status != "active":
            raise IntegrityViolation("new password credential must be active at revision 1")
        existing = self.get_password(candidate.credential_id)
        if existing is not None:
            self._raise_password_id_collision(existing, candidate)
        login_owner = self.get_password_by_normalized_login(candidate.normalized_login_name)
        if login_owner is not None:
            raise Conflict(
                "normalized login name is already bound to a password credential",
                normalized_login_name=candidate.normalized_login_name,
                credential_id=login_owner.credential_id,
            )
        try:
            result = self._session.execute(
                sqlite_insert(PasswordCredentialRow)
                .values(**candidate.model_dump(mode="json"))
                .on_conflict_do_nothing()
            )
        except IntegrityError as exc:
            raise IntegrityViolation("password credential principal binding is invalid") from exc
        if result.rowcount != 1:
            self._session.expire_all()
            retained = self.get_password(candidate.credential_id)
            if retained is not None:
                self._raise_password_id_collision(retained, candidate)
            login_owner = self.get_password_by_normalized_login(candidate.normalized_login_name)
            if login_owner is not None:
                raise Conflict(
                    "normalized login name is already bound to a password credential",
                    normalized_login_name=candidate.normalized_login_name,
                    credential_id=login_owner.credential_id,
                )
            raise IntegrityViolation(
                "password credential insert conflicted without retained authority"
            )
        self._session.flush()
        return candidate

    def get_password(self, credential_id: str) -> PasswordCredentialRecordV1 | None:
        selected = _require_identifier(credential_id, field_name="credential_id")
        row = self._session.get(PasswordCredentialRow, selected)
        return None if row is None else _password_from_row(row)

    def get_password_by_normalized_login(
        self,
        normalized_login_name: str,
    ) -> PasswordCredentialRecordV1 | None:
        selected = _require_identifier(
            normalized_login_name,
            field_name="normalized_login_name",
            maximum=256,
        )
        row = self._session.scalar(
            select(PasswordCredentialRow).where(
                PasswordCredentialRow.normalized_login_name == selected
            )
        )
        return None if row is None else _password_from_row(row)

    def compare_and_set_password(
        self,
        record: PasswordCredentialRecordV1,
        *,
        expected_revision: int,
    ) -> PasswordCredentialRecordV1:
        candidate = _validate_password(record, stored=False)
        expected = self._positive_revision(expected_revision, field_name="expected_revision")
        current = self._require_password(candidate.credential_id)
        if current.revision != expected:
            self._raise_revision_conflict(
                label="password credential",
                record_id=current.credential_id,
                expected=expected,
                actual=current.revision,
            )
        self._validate_password_transition(current, candidate)
        owner = self.get_password_by_normalized_login(candidate.normalized_login_name)
        if owner is not None and owner.credential_id != candidate.credential_id:
            raise Conflict(
                "normalized login name is already bound to a password credential",
                normalized_login_name=candidate.normalized_login_name,
                credential_id=owner.credential_id,
            )
        try:
            result = self._session.execute(
                update(PasswordCredentialRow)
                .where(
                    PasswordCredentialRow.credential_id == current.credential_id,
                    PasswordCredentialRow.revision == expected,
                )
                .values(**candidate.model_dump(mode="json"))
                .execution_options(synchronize_session=False)
            )
        except IntegrityError as exc:
            raise Conflict(
                "normalized login name is already bound to a password credential",
                normalized_login_name=candidate.normalized_login_name,
            ) from exc
        if result.rowcount != 1:
            self._session.expire_all()
            actual = self.get_password(candidate.credential_id)
            self._raise_revision_conflict(
                label="password credential",
                record_id=candidate.credential_id,
                expected=expected,
                actual=None if actual is None else actual.revision,
            )
        self._session.expire_all()
        retained = self._require_password(candidate.credential_id)
        if retained != candidate:
            raise IntegrityViolation("password credential CAS retained different content")
        return retained

    def disable_password(
        self,
        credential_id: str,
        *,
        expected_revision: int,
    ) -> PasswordCredentialRecordV1:
        current = self._require_password(credential_id)
        expected = self._positive_revision(expected_revision, field_name="expected_revision")
        if current.revision != expected:
            self._raise_revision_conflict(
                label="password credential",
                record_id=current.credential_id,
                expected=expected,
                actual=current.revision,
            )
        if current.status != "active":
            raise Conflict("password credential is already disabled", credential_id=credential_id)
        candidate = current.model_copy(
            update={
                "status": "disabled",
                "changed_at": _utc_text(self._clock),
                "revision": current.revision + 1,
            }
        )
        return self.compare_and_set_password(candidate, expected_revision=expected)

    def create_api_key(self, record: ApiKeyRecordV1) -> ApiKeyRecordV1:
        candidate = _validate_api_key(record, stored=False)
        if candidate.revision != 1 or candidate.status != "active":
            raise IntegrityViolation("new API key must be active at revision 1")
        existing = self.get_api_key(candidate.api_key_id)
        if existing is not None:
            self._raise_api_key_id_collision(existing, candidate)
        digest_owner = self.get_api_key_by_digest(candidate.key_digest)
        if digest_owner is not None:
            raise Conflict(
                "API key digest is already bound to a credential",
                api_key_id=digest_owner.api_key_id,
            )
        try:
            result = self._session.execute(
                sqlite_insert(ApiKeyRow)
                .values(**candidate.model_dump(mode="json"))
                .on_conflict_do_nothing()
            )
        except IntegrityError as exc:
            raise IntegrityViolation("API key principal binding is invalid") from exc
        if result.rowcount != 1:
            self._session.expire_all()
            retained = self.get_api_key(candidate.api_key_id)
            if retained is not None:
                self._raise_api_key_id_collision(retained, candidate)
            digest_owner = self.get_api_key_by_digest(candidate.key_digest)
            if digest_owner is not None:
                raise Conflict(
                    "API key digest is already bound to a credential",
                    api_key_id=digest_owner.api_key_id,
                )
            raise IntegrityViolation("API key insert conflicted without retained authority")
        self._session.flush()
        return candidate

    def get_api_key(self, api_key_id: str) -> ApiKeyRecordV1 | None:
        selected = _require_identifier(api_key_id, field_name="api_key_id")
        row = self._session.get(ApiKeyRow, selected)
        return None if row is None else _api_key_from_row(row)

    def get_api_key_by_digest(self, key_digest: str) -> ApiKeyRecordV1 | None:
        selected = _require_digest(key_digest, field_name="key_digest")
        row = self._session.scalar(select(ApiKeyRow).where(ApiKeyRow.key_digest == selected))
        return None if row is None else _api_key_from_row(row)

    def revoke_api_key(
        self,
        api_key_id: str,
        *,
        expected_revision: int,
    ) -> ApiKeyRecordV1:
        current = self._require_api_key(api_key_id)
        expected = self._positive_revision(expected_revision, field_name="expected_revision")
        if current.revision != expected:
            self._raise_revision_conflict(
                label="API key",
                record_id=current.api_key_id,
                expected=expected,
                actual=current.revision,
            )
        if current.status != "active":
            raise Conflict("API key is not active", api_key_id=current.api_key_id)
        candidate = ApiKeyRecordV1.model_validate(
            current.model_copy(
                update={
                    "status": "revoked",
                    "revoked_at": _utc_text(self._clock),
                    "revision": current.revision + 1,
                }
            ).model_dump(mode="json")
        )
        _validate_api_key(candidate, stored=False)
        result = self._session.execute(
            update(ApiKeyRow)
            .where(
                ApiKeyRow.api_key_id == current.api_key_id,
                ApiKeyRow.revision == expected,
                ApiKeyRow.status == "active",
            )
            .values(
                status=candidate.status,
                revoked_at=candidate.revoked_at,
                revision=candidate.revision,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self._session.expire_all()
            actual = self.get_api_key(current.api_key_id)
            self._raise_revision_conflict(
                label="API key",
                record_id=current.api_key_id,
                expected=expected,
                actual=None if actual is None else actual.revision,
            )
        self._session.expire_all()
        retained = self._require_api_key(current.api_key_id)
        if retained != candidate:
            raise IntegrityViolation("API key revoke retained different content")
        return retained

    def create_session(self, record: SessionRecordV1) -> SessionRecordV1:
        candidate = _validate_session(record, stored=False)
        if candidate.revision != 1 or candidate.revoked_at is not None:
            raise IntegrityViolation("new session must be unrevoked at revision 1")
        existing = self.get_session(candidate.session_id)
        if existing is not None:
            self._raise_session_id_collision(existing, candidate)
        digest_owner = self.get_session_by_token_digest(candidate.token_digest)
        if digest_owner is not None:
            raise Conflict(
                "session token digest is already bound to a session",
                session_id=digest_owner.session_id,
            )
        try:
            result = self._session.execute(
                sqlite_insert(SessionRow)
                .values(**candidate.model_dump(mode="json"))
                .on_conflict_do_nothing()
            )
        except IntegrityError as exc:
            raise IntegrityViolation("session principal binding is invalid") from exc
        if result.rowcount != 1:
            self._session.expire_all()
            retained = self.get_session(candidate.session_id)
            if retained is not None:
                self._raise_session_id_collision(retained, candidate)
            digest_owner = self.get_session_by_token_digest(candidate.token_digest)
            if digest_owner is not None:
                raise Conflict(
                    "session token digest is already bound to a session",
                    session_id=digest_owner.session_id,
                )
            raise IntegrityViolation("session insert conflicted without retained authority")
        self._session.flush()
        return candidate

    def get_session(self, session_id: str) -> SessionRecordV1 | None:
        selected = _require_identifier(session_id, field_name="session_id")
        row = self._session.get(SessionRow, selected)
        return None if row is None else _session_from_row(row)

    def get_session_by_token_digest(self, token_digest: str) -> SessionRecordV1 | None:
        selected = _require_digest(token_digest, field_name="token_digest")
        row = self._session.scalar(select(SessionRow).where(SessionRow.token_digest == selected))
        return None if row is None else _session_from_row(row)

    def touch_session(
        self,
        session_id: str,
        *,
        expected_revision: int,
        last_seen_at: str,
        idle_expires_at: str,
    ) -> SessionRecordV1:
        current = self._require_session(session_id)
        expected = self._positive_revision(expected_revision, field_name="expected_revision")
        if current.revision != expected:
            self._raise_revision_conflict(
                label="session",
                record_id=current.session_id,
                expected=expected,
                actual=current.revision,
            )
        if current.revoked_at is not None:
            raise Conflict("session is revoked", session_id=current.session_id)
        now_instant = _parse_utc(
            last_seen_at,
            field_name="session last_seen_at",
            stored=False,
        )
        repository_now = _parse_utc(
            _utc_text(self._clock),
            field_name="auth repository clock",
            stored=False,
        )
        if now_instant > repository_now:
            raise IntegrityViolation("session touch cannot be later than repository time")
        if now_instant >= _parse_utc(
            current.idle_expires_at,
            field_name="session idle_expires_at",
            stored=True,
        ) or now_instant >= _parse_utc(
            current.absolute_expires_at,
            field_name="session absolute_expires_at",
            stored=True,
        ):
            raise Conflict("session has expired", session_id=current.session_id)
        candidate = SessionRecordV1.model_validate(
            current.model_copy(
                update={
                    "last_seen_at": last_seen_at,
                    "idle_expires_at": idle_expires_at,
                    "revision": current.revision + 1,
                }
            ).model_dump(mode="json")
        )
        _validate_session(candidate, stored=False)
        if now_instant < _parse_utc(
            current.last_seen_at,
            field_name="session last_seen_at",
            stored=True,
        ):
            raise IntegrityViolation("session touch clock moved backwards")
        if _parse_utc(
            candidate.idle_expires_at,
            field_name="session idle_expires_at",
            stored=False,
        ) < _parse_utc(
            current.idle_expires_at,
            field_name="session idle_expires_at",
            stored=True,
        ):
            raise IntegrityViolation("session touch cannot shorten idle expiry")
        result = self._session.execute(
            update(SessionRow)
            .where(
                SessionRow.session_id == current.session_id,
                SessionRow.revision == expected,
                SessionRow.revoked_at.is_(None),
            )
            .values(
                last_seen_at=candidate.last_seen_at,
                idle_expires_at=candidate.idle_expires_at,
                revision=candidate.revision,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self._session.expire_all()
            actual = self.get_session(current.session_id)
            self._raise_revision_conflict(
                label="session",
                record_id=current.session_id,
                expected=expected,
                actual=None if actual is None else actual.revision,
            )
        self._session.expire_all()
        retained = self._require_session(current.session_id)
        if retained != candidate:
            raise IntegrityViolation("session touch retained different content")
        return retained

    def revoke_session(
        self,
        session_id: str,
        *,
        expected_revision: int,
        reason: str,
    ) -> SessionRecordV1:
        current = self._require_session(session_id)
        expected = self._positive_revision(expected_revision, field_name="expected_revision")
        if current.revision != expected:
            self._raise_revision_conflict(
                label="session",
                record_id=current.session_id,
                expected=expected,
                actual=current.revision,
            )
        if current.revoked_at is not None:
            raise Conflict("session is already revoked", session_id=current.session_id)
        selected_reason = _require_identifier(reason, field_name="reason")
        candidate = SessionRecordV1.model_validate(
            current.model_copy(
                update={
                    "revoked_at": _utc_text(self._clock),
                    "revoke_reason": selected_reason,
                    "revision": current.revision + 1,
                }
            ).model_dump(mode="json")
        )
        _validate_session(candidate, stored=False)
        result = self._session.execute(
            update(SessionRow)
            .where(
                SessionRow.session_id == current.session_id,
                SessionRow.revision == expected,
                SessionRow.revoked_at.is_(None),
            )
            .values(
                revoked_at=candidate.revoked_at,
                revoke_reason=candidate.revoke_reason,
                revision=candidate.revision,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self._session.expire_all()
            actual = self.get_session(current.session_id)
            self._raise_revision_conflict(
                label="session",
                record_id=current.session_id,
                expected=expected,
                actual=None if actual is None else actual.revision,
            )
        self._session.expire_all()
        retained = self._require_session(current.session_id)
        if retained != candidate:
            raise IntegrityViolation("session revoke retained different content")
        return retained

    def _require_password(self, credential_id: str) -> PasswordCredentialRecordV1:
        record = self.get_password(credential_id)
        if record is None:
            raise Conflict("password credential does not exist", credential_id=credential_id)
        return record

    def _require_api_key(self, api_key_id: str) -> ApiKeyRecordV1:
        record = self.get_api_key(api_key_id)
        if record is None:
            raise Conflict("API key does not exist", api_key_id=api_key_id)
        return record

    def _require_session(self, session_id: str) -> SessionRecordV1:
        record = self.get_session(session_id)
        if record is None:
            raise Conflict("session does not exist", session_id=session_id)
        return record

    @staticmethod
    def _positive_revision(value: int, *, field_name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{field_name} must be a positive integer")
        return value

    @staticmethod
    def _raise_revision_conflict(
        *,
        label: str,
        record_id: str,
        expected: int,
        actual: int | None,
    ) -> None:
        raise Conflict(
            f"{label} revision did not match",
            record_id=record_id,
            expected_revision=expected,
            actual_revision=actual,
        )

    @staticmethod
    def _raise_password_id_collision(
        existing: PasswordCredentialRecordV1,
        candidate: PasswordCredentialRecordV1,
    ) -> None:
        if existing != candidate:
            raise IntegrityViolation(
                "password credential id is bound to different password credential content",
                credential_id=candidate.credential_id,
            )
        raise Conflict("password credential already exists", credential_id=candidate.credential_id)

    @staticmethod
    def _raise_api_key_id_collision(
        existing: ApiKeyRecordV1,
        candidate: ApiKeyRecordV1,
    ) -> None:
        if existing != candidate:
            raise IntegrityViolation(
                "API key id is bound to different API key content",
                api_key_id=candidate.api_key_id,
            )
        raise Conflict("API key already exists", api_key_id=candidate.api_key_id)

    @staticmethod
    def _raise_session_id_collision(
        existing: SessionRecordV1,
        candidate: SessionRecordV1,
    ) -> None:
        if existing != candidate:
            raise IntegrityViolation(
                "session id is bound to different session content",
                session_id=candidate.session_id,
            )
        raise Conflict("session already exists", session_id=candidate.session_id)

    @staticmethod
    def _validate_password_transition(
        current: PasswordCredentialRecordV1,
        candidate: PasswordCredentialRecordV1,
    ) -> None:
        if (
            candidate.credential_id != current.credential_id
            or candidate.principal_id != current.principal_id
        ):
            raise IntegrityViolation("password credential CAS changed immutable identity")
        if current.status != "active":
            raise Conflict(
                "disabled password credential is terminal", credential_id=current.credential_id
            )
        if candidate.revision != current.revision + 1:
            raise IntegrityViolation("password credential CAS must advance revision exactly once")
        if candidate.status not in {"active", "disabled"}:
            raise IntegrityViolation("password credential CAS has an invalid status")
        if candidate.credential_version not in {
            current.credential_version,
            current.credential_version + 1,
        }:
            raise IntegrityViolation(
                "password credential version must remain stable or advance exactly once"
            )
        if _parse_utc(
            candidate.changed_at,
            field_name="password credential changed_at",
            stored=False,
        ) < _parse_utc(
            current.changed_at,
            field_name="password credential changed_at",
            stored=True,
        ):
            raise IntegrityViolation("password credential changed_at cannot move backwards")


__all__ = ["SqlAuthRepository"]
