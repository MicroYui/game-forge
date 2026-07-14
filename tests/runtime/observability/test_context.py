from __future__ import annotations

import asyncio

from gameforge.contracts.jobs import RunDispatchTraceCarrierV1
from gameforge.contracts.observability import TraceContextV1
from gameforge.runtime.observability.context import (
    TraceCarrier,
    current_trace_context,
    use_trace_context,
)


def _context() -> TraceContextV1:
    return TraceContextV1(
        trace_id="1" * 32,
        span_id="2" * 16,
        trace_flags="01",
        trace_state="vendor=value",
    )


def test_w3c_carrier_round_trips_and_malformed_input_is_non_disruptive() -> None:
    context = _context()

    carrier = TraceCarrier.inject(context)

    assert carrier == RunDispatchTraceCarrierV1(
        traceparent=f"00-{'1' * 32}-{'2' * 16}-01",
        tracestate="vendor=value",
    )
    assert TraceCarrier.extract(carrier) == context
    invalid_state = context.model_copy(update={"trace_state": "vendor=one,vendor=two"})
    assert TraceCarrier.inject(invalid_state).tracestate is None

    with use_trace_context(context):
        assert TraceCarrier.extract({"traceparent": "not-a-trace-parent"}) is None
        assert (
            TraceCarrier.extract(
                {
                    "traceparent": f"00-{'1' * 32}-{'2' * 16}-01",
                    "tracestate": "vendor=one,vendor=two",
                }
            )
            is None
        )
        assert current_trace_context() == context


def test_contextvars_restore_nested_sync_and_propagate_to_async_tasks() -> None:
    parent = _context()
    nested = parent.model_copy(update={"span_id": "3" * 16})

    async def observe() -> TraceContextV1 | None:
        await asyncio.sleep(0)
        return current_trace_context()

    assert current_trace_context() is None
    with use_trace_context(parent):
        assert current_trace_context() == parent
        assert asyncio.run(observe()) == parent
        with use_trace_context(nested):
            assert current_trace_context() == nested
        assert current_trace_context() == parent
    assert current_trace_context() is None


def test_contract_valid_future_traceparent_and_bounded_tracestate_are_extractable() -> None:
    future = RunDispatchTraceCarrierV1(
        traceparent=f"01-{'1' * 32}-{'2' * 16}-01",
        tracestate=" vendor=one , tenant@system=two\t",
    )
    extracted = TraceCarrier.extract(future)
    assert extracted is not None
    assert extracted.trace_id == "1" * 32
    assert extracted.span_id == "2" * 16

    too_long_member = RunDispatchTraceCarrierV1(
        traceparent=f"00-{'1' * 32}-{'2' * 16}-01",
        tracestate="a=" + "x" * 300,
    )
    assert TraceCarrier.extract(too_long_member) is None
