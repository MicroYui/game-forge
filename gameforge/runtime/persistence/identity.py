"""Transaction-bound SQLite identity and role-assignment persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from gameforge.contracts.canonical import canonical_json
from gameforge.contracts.errors import Conflict, IntegrityViolation
from gameforge.contracts.identity import (
    DomainScopeValue,
    Principal,
    PrincipalKind,
    PrincipalRecordV1,
    Role,
    RoleAssignmentV1,
)
from gameforge.contracts.lineage import AuditActor
from gameforge.contracts.storage import UtcClock
from gameforge.runtime.persistence.models import PrincipalRow, RoleAssignmentRow


def _require_positive_revision(value: int, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _utc_text(clock: UtcClock) -> str:
    try:
        now = clock.now_utc()
    except (AttributeError, TypeError, ValueError) as exc:
        raise IntegrityViolation("identity repository clock must return UTC") from exc
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
        or now.utcoffset() != timedelta(0)
    ):
        raise IntegrityViolation("identity repository clock must return UTC")
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _scope_key(scope: DomainScopeValue) -> str:
    if scope is None:
        return "0:null"
    if scope == "all":
        return "1:all"
    return "2:" + canonical_json(scope.model_dump(mode="json"))


def _principal_wire(row: PrincipalRow) -> dict[str, Any]:
    return {
        "principal_schema_version": row.principal_schema_version,
        "principal_id": row.principal_id,
        "kind": row.kind,
        "display_name": row.display_name,
        "status": row.status,
        "credential_epoch": row.credential_epoch,
        "authz_revision": row.authz_revision,
        "revision": row.revision,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "disabled_at": row.disabled_at,
        "disabled_reason": row.disabled_reason,
    }


def _principal_from_row(row: PrincipalRow) -> PrincipalRecordV1:
    wire = _principal_wire(row)
    try:
        record = PrincipalRecordV1.model_validate(wire)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored principal row is invalid",
            principal_id=row.principal_id,
        ) from exc
    if canonical_json(record.model_dump(mode="json")) != canonical_json(wire):
        raise IntegrityViolation(
            "stored principal row is noncanonical",
            principal_id=row.principal_id,
        )
    return record


def _assignment_wire(row: RoleAssignmentRow) -> dict[str, Any]:
    return {
        "assignment_schema_version": row.assignment_schema_version,
        "assignment_id": row.assignment_id,
        "principal_id": row.principal_id,
        "role": row.role,
        "scope": row.scope,
        "status": row.status,
        "revision": row.revision,
        "granted_at": row.granted_at,
        "granted_by": row.granted_by,
        "revoked_at": row.revoked_at,
        "revoked_by": row.revoked_by,
        "revoke_reason": row.revoke_reason,
    }


def _assignment_from_row(row: RoleAssignmentRow) -> RoleAssignmentV1:
    wire = _assignment_wire(row)
    try:
        assignment = RoleAssignmentV1.model_validate(wire)
    except (TypeError, ValueError, ValidationError) as exc:
        raise IntegrityViolation(
            "stored role assignment row is invalid",
            assignment_id=row.assignment_id,
        ) from exc
    if canonical_json(assignment.model_dump(mode="json")) != canonical_json(
        wire
    ) or row.scope_key != _scope_key(assignment.scope):
        raise IntegrityViolation(
            "stored role assignment row is noncanonical",
            assignment_id=row.assignment_id,
        )
    return assignment


def _principal_values(record: PrincipalRecordV1) -> dict[str, Any]:
    return record.model_dump(mode="json")


def _assignment_values(assignment: RoleAssignmentV1) -> dict[str, Any]:
    values = assignment.model_dump(mode="json")
    values["scope_key"] = _scope_key(assignment.scope)
    return values


class SqlIdentityRepository:
    """Persist the authoritative principal record and its assignment history.

    The owning UnitOfWork supplies the Session and owns commit/rollback. This
    repository only flushes enough to make conditional writes observable in
    the current transaction.
    """

    def __init__(self, session: Session, *, clock: UtcClock) -> None:
        if session.get_bind().dialect.name != "sqlite":
            raise ValueError("SqlIdentityRepository requires a SQLite session")
        self._session = session
        self._clock = clock

    def create(
        self,
        *,
        principal_id: str,
        kind: PrincipalKind,
        display_name: str,
    ) -> PrincipalRecordV1:
        now = _utc_text(self._clock)
        try:
            candidate = PrincipalRecordV1(
                principal_id=principal_id,
                kind=kind,
                display_name=display_name,
                status="active",
                credential_epoch=0,
                authz_revision=0,
                revision=1,
                created_at=now,
                updated_at=now,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityViolation("principal create payload is invalid") from exc

        existing = self.get(candidate.principal_id)
        if existing is not None:
            self._raise_principal_create_collision(existing, candidate)

        result = self._session.execute(
            sqlite_insert(PrincipalRow)
            .values(**_principal_values(candidate))
            .on_conflict_do_nothing(index_elements=[PrincipalRow.principal_id])
        )
        if result.rowcount != 1:
            self._session.expire_all()
            actual = self.get(candidate.principal_id)
            if actual is None:
                raise IntegrityViolation(
                    "principal create did not publish a row",
                    principal_id=candidate.principal_id,
                )
            self._raise_principal_create_collision(actual, candidate)
        return candidate

    def get(self, principal_id: str) -> PrincipalRecordV1 | None:
        row = self._session.get(PrincipalRow, principal_id)
        if row is None:
            return None
        return _principal_from_row(row)

    def get_assignment(self, assignment_id: str) -> RoleAssignmentV1 | None:
        row = self._session.get(RoleAssignmentRow, assignment_id)
        if row is None:
            return None
        return _assignment_from_row(row)

    def project(self, principal_id: str) -> Principal | None:
        record = self.get(principal_id)
        if record is None:
            return None
        rows = self._session.scalars(
            select(RoleAssignmentRow)
            .where(
                RoleAssignmentRow.principal_id == principal_id,
                RoleAssignmentRow.status == "active",
            )
            .order_by(
                RoleAssignmentRow.role,
                RoleAssignmentRow.scope_key,
                RoleAssignmentRow.assignment_id,
            )
        ).all()
        assignments = tuple(_assignment_from_row(row) for row in rows)
        try:
            return Principal(
                id=record.principal_id,
                kind=record.kind,
                display_name=record.display_name,
                status=record.status,
                revision=record.revision,
                credential_epoch=record.credential_epoch,
                authz_revision=record.authz_revision,
                roles=assignments,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityViolation(
                "stored identity projection is invalid",
                principal_id=record.principal_id,
            ) from exc

    def grant(
        self,
        *,
        assignment_id: str,
        principal_id: str,
        role: Role,
        scope: DomainScopeValue,
        granted_by: AuditActor,
        expected_principal_revision: int,
    ) -> RoleAssignmentV1:
        expected = _require_positive_revision(
            expected_principal_revision,
            field_name="expected_principal_revision",
        )
        principal = self._require_principal(principal_id)
        self._require_principal_revision(principal, expected)
        if principal.status != "active":
            raise Conflict(
                "cannot grant a role to a disabled principal",
                principal_id=principal_id,
                principal_revision=principal.revision,
            )

        try:
            candidate = RoleAssignmentV1(
                assignment_id=assignment_id,
                principal_id=principal_id,
                role=role,
                scope=scope,
                status="active",
                revision=1,
                granted_at=_utc_text(self._clock),
                granted_by=granted_by,
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityViolation("role grant payload is invalid") from exc

        existing = self.get_assignment(candidate.assignment_id)
        if existing is not None:
            if (
                existing.principal_id != candidate.principal_id
                or existing.role != candidate.role
                or existing.scope != candidate.scope
                or existing.granted_by != candidate.granted_by
            ):
                raise IntegrityViolation(
                    "assignment id is bound to different assignment content",
                    assignment_id=candidate.assignment_id,
                )
            raise Conflict(
                "role assignment already exists",
                assignment_id=candidate.assignment_id,
            )

        active_row = self._session.scalar(
            select(RoleAssignmentRow).where(
                RoleAssignmentRow.principal_id == candidate.principal_id,
                RoleAssignmentRow.role == candidate.role,
                RoleAssignmentRow.scope_key == _scope_key(candidate.scope),
                RoleAssignmentRow.status == "active",
            )
        )
        if active_row is not None:
            active = _assignment_from_row(active_row)
            raise Conflict(
                "active role assignment identity already exists",
                principal_id=candidate.principal_id,
                role=candidate.role,
                scope_key=_scope_key(candidate.scope),
                assignment_id=active.assignment_id,
            )

        result = self._session.execute(
            update(PrincipalRow)
            .where(
                PrincipalRow.principal_id == principal.principal_id,
                PrincipalRow.revision == expected,
                PrincipalRow.status == "active",
            )
            .values(
                revision=PrincipalRow.revision + 1,
                authz_revision=PrincipalRow.authz_revision + 1,
                updated_at=candidate.granted_at,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self._session.expire_all()
            actual = self.get(principal.principal_id)
            raise Conflict(
                "principal revision changed during role grant",
                principal_id=principal.principal_id,
                expected_revision=expected,
                actual_revision=None if actual is None else actual.revision,
            )

        insert_result = self._session.execute(
            sqlite_insert(RoleAssignmentRow)
            .values(**_assignment_values(candidate))
            .on_conflict_do_nothing()
        )
        if insert_result.rowcount != 1:
            raise Conflict(
                "role assignment publication conflicted",
                assignment_id=candidate.assignment_id,
            )
        self._session.expire_all()
        return self._require_assignment(candidate.assignment_id)

    def revoke(
        self,
        *,
        assignment_id: str,
        revoked_by: AuditActor,
        revoke_reason: str,
        expected_principal_revision: int,
        expected_assignment_revision: int,
    ) -> RoleAssignmentV1:
        expected_principal = _require_positive_revision(
            expected_principal_revision,
            field_name="expected_principal_revision",
        )
        expected_assignment = _require_positive_revision(
            expected_assignment_revision,
            field_name="expected_assignment_revision",
        )
        assignment = self._require_assignment(assignment_id)
        principal = self._require_principal(assignment.principal_id)
        self._require_principal_revision(principal, expected_principal)
        if assignment.revision != expected_assignment:
            raise Conflict(
                "role assignment revision did not match",
                assignment_id=assignment.assignment_id,
                expected_revision=expected_assignment,
                actual_revision=assignment.revision,
            )
        if assignment.status != "active":
            raise Conflict(
                "role assignment is not active",
                assignment_id=assignment.assignment_id,
                assignment_revision=assignment.revision,
            )

        now = _utc_text(self._clock)
        try:
            candidate = assignment.model_copy(
                update={
                    "status": "revoked",
                    "revision": assignment.revision + 1,
                    "revoked_at": now,
                    "revoked_by": revoked_by,
                    "revoke_reason": revoke_reason,
                }
            )
            candidate = RoleAssignmentV1.model_validate(candidate.model_dump(mode="json"))
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityViolation("role revoke payload is invalid") from exc

        principal_result = self._session.execute(
            update(PrincipalRow)
            .where(
                PrincipalRow.principal_id == principal.principal_id,
                PrincipalRow.revision == expected_principal,
            )
            .values(
                revision=PrincipalRow.revision + 1,
                authz_revision=PrincipalRow.authz_revision + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if principal_result.rowcount != 1:
            self._session.expire_all()
            actual = self.get(principal.principal_id)
            raise Conflict(
                "principal revision changed during role revoke",
                principal_id=principal.principal_id,
                expected_revision=expected_principal,
                actual_revision=None if actual is None else actual.revision,
            )

        assignment_result = self._session.execute(
            update(RoleAssignmentRow)
            .where(
                RoleAssignmentRow.assignment_id == assignment.assignment_id,
                RoleAssignmentRow.principal_id == assignment.principal_id,
                RoleAssignmentRow.status == "active",
                RoleAssignmentRow.revision == expected_assignment,
            )
            .values(
                status=candidate.status,
                revision=candidate.revision,
                revoked_at=candidate.revoked_at,
                revoked_by=candidate.revoked_by.model_dump(mode="json"),
                revoke_reason=candidate.revoke_reason,
            )
            .execution_options(synchronize_session=False)
        )
        if assignment_result.rowcount != 1:
            raise Conflict(
                "role assignment revision changed during revoke",
                assignment_id=assignment.assignment_id,
                expected_revision=expected_assignment,
            )
        self._session.expire_all()
        return self._require_assignment(assignment.assignment_id)

    def disable(
        self,
        principal_id: str,
        *,
        disabled_reason: str,
        expected_revision: int,
    ) -> PrincipalRecordV1:
        expected = _require_positive_revision(expected_revision, field_name="expected_revision")
        principal = self._require_principal(principal_id)
        self._require_principal_revision(principal, expected)
        if principal.status != "active":
            raise Conflict(
                "principal is already disabled",
                principal_id=principal_id,
                principal_revision=principal.revision,
            )

        now = _utc_text(self._clock)
        try:
            candidate = principal.model_copy(
                update={
                    "status": "disabled",
                    "revision": principal.revision + 1,
                    "authz_revision": principal.authz_revision + 1,
                    "credential_epoch": principal.credential_epoch + 1,
                    "updated_at": now,
                    "disabled_at": now,
                    "disabled_reason": disabled_reason,
                }
            )
            candidate = PrincipalRecordV1.model_validate(candidate.model_dump(mode="json"))
        except (TypeError, ValueError, ValidationError) as exc:
            raise IntegrityViolation("principal disable payload is invalid") from exc

        result = self._session.execute(
            update(PrincipalRow)
            .where(
                PrincipalRow.principal_id == principal.principal_id,
                PrincipalRow.revision == expected,
                PrincipalRow.status == "active",
            )
            .values(
                status=candidate.status,
                revision=candidate.revision,
                authz_revision=candidate.authz_revision,
                credential_epoch=candidate.credential_epoch,
                updated_at=candidate.updated_at,
                disabled_at=candidate.disabled_at,
                disabled_reason=candidate.disabled_reason,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            self._session.expire_all()
            actual = self.get(principal.principal_id)
            raise Conflict(
                "principal revision changed during disable",
                principal_id=principal.principal_id,
                expected_revision=expected,
                actual_revision=None if actual is None else actual.revision,
            )
        self._session.expire_all()
        return self._require_principal(principal.principal_id)

    def _require_principal(self, principal_id: str) -> PrincipalRecordV1:
        principal = self.get(principal_id)
        if principal is None:
            raise Conflict("principal does not exist", principal_id=principal_id)
        return principal

    def _require_assignment(self, assignment_id: str) -> RoleAssignmentV1:
        assignment = self.get_assignment(assignment_id)
        if assignment is None:
            raise Conflict("role assignment does not exist", assignment_id=assignment_id)
        return assignment

    @staticmethod
    def _require_principal_revision(
        principal: PrincipalRecordV1,
        expected_revision: int,
    ) -> None:
        if principal.revision != expected_revision:
            raise Conflict(
                "principal revision did not match",
                principal_id=principal.principal_id,
                expected_revision=expected_revision,
                actual_revision=principal.revision,
            )

    @staticmethod
    def _raise_principal_create_collision(
        existing: PrincipalRecordV1,
        candidate: PrincipalRecordV1,
    ) -> None:
        if existing.kind != candidate.kind or existing.display_name != candidate.display_name:
            raise IntegrityViolation(
                "principal id is bound to different identity content",
                principal_id=candidate.principal_id,
            )
        raise Conflict(
            "principal already exists",
            principal_id=candidate.principal_id,
            actual_revision=existing.revision,
        )


__all__ = ["SqlIdentityRepository"]
