"""Typed platform failures shared by M4 adapters and services."""

from __future__ import annotations

from typing import Any, ClassVar


class GameForgeError(Exception):
    """Base for stable, non-transport platform failures."""

    code: ClassVar[str] = "gameforge_error"

    def __init__(self, detail: str = "", **context: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.context = dict(context)


class IntegrityViolation(GameForgeError):
    code = "integrity_violation"


class Conflict(GameForgeError):
    code = "revision_conflict"


class StaleConflictSet(Conflict):
    code = "stale_conflict_set"


class CursorInvalid(GameForgeError):
    code = "invalid_cursor"


class CursorExpired(GameForgeError):
    code = "cursor_expired"


class Forbidden(GameForgeError):
    code = "forbidden"


class InvalidStateTransition(GameForgeError):
    code = "invalid_state_transition"


class TransactionClosed(GameForgeError):
    code = "transaction_closed"


class RetentionActive(GameForgeError):
    code = "retention_active"
