"""Fail-closed lifecycle for transaction-bound runtime capabilities."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable, Literal

from gameforge.contracts.errors import InvalidStateTransition, TransactionClosed


_CONSTRUCTION_TOKEN = object()
_ACTIVE_TRANSACTION: ContextVar[object | None] = ContextVar(
    "gameforge_active_transaction",
    default=None,
)
_CAPABILITY_NAMES = frozenset(
    {
        "refs",
        "audit",
        "approvals",
        "lineage",
        "object_bindings",
        "runs",
        "cost",
        "slo",
        "identity",
        "auth",
        "policies",
        "idempotency",
    }
)
_TerminalState = Literal["committed", "rolled_back"]


def ensure_transaction_context_available() -> None:
    """Fail before acquiring database resources when a UoW is already active."""

    if _ACTIVE_TRANSACTION.get() is not None:
        raise InvalidStateTransition("nested transactions are forbidden across UnitOfWork owners")


@dataclass(frozen=True, slots=True)
class TransactionCapabilities:
    refs: Any
    audit: Any
    approvals: Any
    lineage: Any
    object_bindings: Any
    runs: Any
    cost: Any
    slo: Any = None
    identity: Any = None
    auth: Any = None
    policies: Any = None
    idempotency: Any = None


class TransactionHandle:
    """Owner-bound capability handle invalidated by every terminal action."""

    def __init__(
        self,
        *,
        _construction_token: object | None = None,
        _owner_token: object | None = None,
        _capabilities: TransactionCapabilities | None = None,
        _release: Callable[[TransactionHandle], None] | None = None,
        _finish_transaction: Callable[[_TerminalState], None] | None = None,
    ) -> None:
        if _construction_token is not _CONSTRUCTION_TOKEN:
            raise InvalidStateTransition("transaction handles must be created by their factory")
        if _owner_token is None or _capabilities is None or _release is None:
            raise InvalidStateTransition("transaction factory creation is incomplete")
        self.__owner_token = _owner_token
        self.__capabilities = {
            name: _BoundTransactionCapability(
                target=getattr(_capabilities, name),
                require_active=self.__require_active,
                capability_name=name,
            )
            for name in _CAPABILITY_NAMES
        }
        self.__release = _release
        self.__finish_transaction = _finish_transaction or (lambda state: None)
        self.__state: Literal["active", "committed", "rolled_back"] = "active"
        self.__entered = False

    @property
    def state(self) -> Literal["active", "committed", "rolled_back"]:
        return self.__state

    def __enter__(self) -> TransactionHandle:
        self.__require_active()
        if self.__entered:
            raise InvalidStateTransition("transaction context is already entered")
        self.__entered = True
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> Literal[False]:
        if self.__state == "active":
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        self.__entered = False
        return False

    def commit(self) -> None:
        self.__finish("committed")

    def rollback(self) -> None:
        self.__finish("rolled_back")

    def capability(self, name: str) -> Any:
        self.__require_active()
        if name not in _CAPABILITY_NAMES:
            raise InvalidStateTransition(
                "unknown transaction capability",
                capability=name,
            )
        return self.__capabilities[name]

    @property
    def refs(self) -> Any:
        return self.capability("refs")

    @property
    def audit(self) -> Any:
        return self.capability("audit")

    @property
    def approvals(self) -> Any:
        return self.capability("approvals")

    @property
    def lineage(self) -> Any:
        return self.capability("lineage")

    @property
    def object_bindings(self) -> Any:
        return self.capability("object_bindings")

    @property
    def runs(self) -> Any:
        return self.capability("runs")

    @property
    def cost(self) -> Any:
        return self.capability("cost")

    @property
    def slo(self) -> Any:
        return self.capability("slo")

    @property
    def identity(self) -> Any:
        return self.capability("identity")

    @property
    def auth(self) -> Any:
        return self.capability("auth")

    @property
    def policies(self) -> Any:
        return self.capability("policies")

    @property
    def idempotency(self) -> Any:
        return self.capability("idempotency")

    def _require_owner(self, owner_token: object) -> None:
        if owner_token is not self.__owner_token:
            raise InvalidStateTransition("transaction handle belongs to a different owner")
        self.__require_active()

    def __finish(self, state: _TerminalState) -> None:
        self.__require_active()
        # Resetting the token proves this is the exact owning Context, not a
        # copied asyncio/task context. It must happen before physical commit.
        self.__release(self)
        try:
            self.__finish_transaction(state)
        except BaseException:
            self.__state = "rolled_back"
            raise
        self.__state = state

    def __require_active(self) -> None:
        if self.__state != "active":
            raise TransactionClosed(
                "transaction handle is closed",
                terminal_state=self.__state,
            )
        if _ACTIVE_TRANSACTION.get() is not self:
            raise InvalidStateTransition(
                "transaction handle is outside its owning UnitOfWork context"
            )


class _BoundTransactionCapability:
    """Recheck transaction liveness when a capability method is invoked."""

    __slots__ = ("__capability_name", "__require_active", "__target")

    def __init__(
        self,
        *,
        target: Any,
        require_active: Callable[[], None],
        capability_name: str,
    ) -> None:
        self.__target = target
        self.__require_active = require_active
        self.__capability_name = capability_name

    def __getattr__(self, attribute: str) -> Any:
        self.__require_active()
        value = getattr(self.__target, attribute)
        if not callable(value):
            raise InvalidStateTransition(
                "transaction capabilities expose only lifecycle-bound methods",
                capability=self.__capability_name,
                attribute=attribute,
            )

        @wraps(value)
        def guarded(*args: Any, **kwargs: Any) -> Any:
            self.__require_active()
            return value(*args, **kwargs)

        return guarded


class TransactionHandleFactory:
    """Own one active handle and reject cross-owner capability binding."""

    def __init__(self) -> None:
        self.__owner_token = object()
        self.__active: TransactionHandle | None = None
        self.__context_token: object | None = None

    def begin(
        self,
        capabilities: TransactionCapabilities,
        *,
        finish_transaction: Callable[[_TerminalState], None] | None = None,
    ) -> TransactionHandle:
        if self.__active is not None:
            raise InvalidStateTransition("nested transactions are forbidden for the same owner")
        ensure_transaction_context_available()
        transaction = TransactionHandle(
            _construction_token=_CONSTRUCTION_TOKEN,
            _owner_token=self.__owner_token,
            _capabilities=capabilities,
            _release=self.__release,
            _finish_transaction=finish_transaction,
        )
        self.__active = transaction
        self.__context_token = _ACTIVE_TRANSACTION.set(transaction)
        return transaction

    def require_owned(self, transaction: TransactionHandle) -> TransactionHandle:
        if not isinstance(transaction, TransactionHandle):
            raise InvalidStateTransition("value is not a transaction handle")
        transaction._require_owner(self.__owner_token)
        return transaction

    def capability(self, transaction: TransactionHandle, name: str) -> Any:
        return self.require_owned(transaction).capability(name)

    def __release(self, transaction: TransactionHandle) -> None:
        if self.__active is not transaction:
            raise InvalidStateTransition(
                "transaction handle does not match the active owner transaction"
            )
        if _ACTIVE_TRANSACTION.get() is not transaction or self.__context_token is None:
            raise InvalidStateTransition(
                "transaction handle is outside its owning UnitOfWork context"
            )
        try:
            _ACTIVE_TRANSACTION.reset(self.__context_token)
        except ValueError as exc:
            raise InvalidStateTransition(
                "transaction handle is outside its owning UnitOfWork context"
            ) from exc
        self.__context_token = None
        self.__active = None
