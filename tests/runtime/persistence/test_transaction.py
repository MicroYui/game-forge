from __future__ import annotations

from dataclasses import fields

import pytest

from gameforge.contracts.errors import InvalidStateTransition, TransactionClosed
from gameforge.runtime.persistence.transaction import (
    TransactionCapabilities,
    TransactionHandle,
    TransactionHandleFactory,
)


class _ProbeCapability:
    def __init__(self) -> None:
        self.calls = 0

    def ping(self) -> int:
        self.calls += 1
        return self.calls


def _capabilities(
    *,
    refs: object | None = None,
    artifacts: object | None = None,
    conflicts: object | None = None,
    ref_transitions: object | None = None,
) -> TransactionCapabilities:
    return TransactionCapabilities(
        refs=refs if refs is not None else object(),
        audit=object(),
        approvals=object(),
        lineage=object(),
        object_bindings=object(),
        runs=object(),
        cost=object(),
        artifacts=artifacts,
        conflicts=conflicts,
        ref_transitions=ref_transitions,
    )


def test_transaction_handle_rejects_direct_construction() -> None:
    with pytest.raises(InvalidStateTransition, match="factory"):
        TransactionHandle()


def test_active_handle_exposes_each_transaction_capability() -> None:
    capabilities = _capabilities()
    factory = TransactionHandleFactory()

    with factory.begin(capabilities) as transaction:
        for field in fields(capabilities):
            assert getattr(transaction, field.name) is not getattr(capabilities, field.name)
        assert factory.require_owned(transaction) is transaction


def test_existing_positional_capability_construction_remains_compatible() -> None:
    existing = tuple(object() for _ in range(7))

    capabilities = TransactionCapabilities(*existing)

    assert (
        tuple(getattr(capabilities, field.name) for field in fields(capabilities)[:7]) == existing
    )
    assert capabilities.artifacts is None
    assert capabilities.conflicts is None
    assert capabilities.ref_transitions is None


@pytest.mark.parametrize("capability_name", ["artifacts", "conflicts", "ref_transitions"])
def test_active_handle_exposes_new_transaction_capabilities(
    capability_name: str,
) -> None:
    target = _ProbeCapability()
    transaction = TransactionHandleFactory().begin(_capabilities(**{capability_name: target}))

    assert getattr(transaction, capability_name).ping() == 1
    transaction.commit()


def test_unknown_capability_remains_an_invalid_state_transition() -> None:
    transaction = TransactionHandleFactory().begin(_capabilities())

    with pytest.raises(InvalidStateTransition, match="unknown transaction capability"):
        transaction.capability("unknown")

    transaction.rollback()


def test_factory_rejects_nested_transaction_for_same_owner() -> None:
    factory = TransactionHandleFactory()

    with factory.begin(_capabilities()):
        with pytest.raises(InvalidStateTransition, match="nested"):
            factory.begin(_capabilities())


def test_transaction_context_rejects_nested_different_uow_owner() -> None:
    outer = TransactionHandleFactory()
    inner = TransactionHandleFactory()

    with outer.begin(_capabilities()):
        with pytest.raises(InvalidStateTransition, match="nested"):
            inner.begin(_capabilities())

    with inner.begin(_capabilities()) as transaction:
        assert transaction.refs is not None


def test_handle_rejects_nested_context_entry() -> None:
    transaction = TransactionHandleFactory().begin(_capabilities())

    with transaction:
        with pytest.raises(InvalidStateTransition, match="already entered"):
            transaction.__enter__()


def test_factory_rejects_handle_owned_by_another_uow() -> None:
    owner = TransactionHandleFactory()
    other_owner = TransactionHandleFactory()

    with owner.begin(_capabilities()) as transaction:
        with pytest.raises(InvalidStateTransition, match="different owner"):
            other_owner.require_owned(transaction)
        with pytest.raises(InvalidStateTransition, match="different owner"):
            other_owner.capability(transaction, "refs")


@pytest.mark.parametrize("terminal_action", ["commit", "rollback"])
def test_terminal_action_invalidates_every_capability(terminal_action: str) -> None:
    capabilities = _capabilities()
    transaction = TransactionHandleFactory().begin(capabilities)

    getattr(transaction, terminal_action)()

    for field in fields(capabilities):
        with pytest.raises(TransactionClosed, match="closed"):
            getattr(transaction, field.name)
    with pytest.raises(TransactionClosed, match="closed"):
        transaction.capability("refs")


@pytest.mark.parametrize("terminal_action", ["commit", "rollback"])
def test_escaped_capability_and_captured_method_remain_lifecycle_bound(
    terminal_action: str,
) -> None:
    target = _ProbeCapability()
    transaction = TransactionHandleFactory().begin(_capabilities(refs=target))
    escaped = transaction.refs
    captured_method = escaped.ping

    assert escaped is not target
    assert escaped.ping() == 1
    getattr(transaction, terminal_action)()

    with pytest.raises(TransactionClosed, match="closed"):
        escaped.ping()
    with pytest.raises(TransactionClosed, match="closed"):
        captured_method()
    assert target.calls == 1


@pytest.mark.parametrize("capability_name", ["artifacts", "conflicts", "ref_transitions"])
@pytest.mark.parametrize("terminal_action", ["commit", "rollback"])
def test_new_escaped_capability_and_captured_method_remain_lifecycle_bound(
    terminal_action: str,
    capability_name: str,
) -> None:
    target = _ProbeCapability()
    transaction = TransactionHandleFactory().begin(_capabilities(**{capability_name: target}))
    escaped = getattr(transaction, capability_name)
    captured_method = escaped.ping

    assert escaped.ping() == 1
    getattr(transaction, terminal_action)()

    with pytest.raises(TransactionClosed, match="closed"):
        escaped.ping()
    with pytest.raises(TransactionClosed, match="closed"):
        captured_method()
    assert target.calls == 1


@pytest.mark.parametrize(
    ("first_action", "second_action"),
    [
        ("commit", "commit"),
        ("commit", "rollback"),
        ("rollback", "commit"),
        ("rollback", "rollback"),
    ],
)
def test_second_terminal_action_fails(
    first_action: str,
    second_action: str,
) -> None:
    transaction = TransactionHandleFactory().begin(_capabilities())
    getattr(transaction, first_action)()

    with pytest.raises(TransactionClosed, match="closed"):
        getattr(transaction, second_action)()


def test_normal_context_exit_closes_handle_and_releases_owner() -> None:
    factory = TransactionHandleFactory()

    with factory.begin(_capabilities()) as transaction:
        assert transaction.refs is not None

    assert transaction.state == "committed"
    with pytest.raises(TransactionClosed, match="closed"):
        transaction.refs
    with factory.begin(_capabilities()) as next_transaction:
        assert next_transaction is not transaction


def test_exception_context_exit_rolls_back_closes_and_does_not_suppress() -> None:
    factory = TransactionHandleFactory()

    with pytest.raises(RuntimeError, match="boom"):
        with factory.begin(_capabilities()) as transaction:
            raise RuntimeError("boom")

    assert transaction.state == "rolled_back"
    with pytest.raises(TransactionClosed, match="closed"):
        transaction.refs
    with factory.begin(_capabilities()) as next_transaction:
        assert next_transaction.refs is not None


def test_closed_handle_cannot_be_reentered() -> None:
    transaction = TransactionHandleFactory().begin(_capabilities())
    transaction.rollback()

    with pytest.raises(TransactionClosed, match="closed"):
        transaction.__enter__()
