"""Pause-during-AWAITING_CO_THREADS regression test.

The co-thread wait races ``all_labware_is_present`` against
``method.exit_signal``, ``_stop_event``, and ``_pause_request_event``.
Without the pause race, a thread parked on co-labware would never
honor an operator pause until its peer arrived (or the wait timed out).

Priority is deterministic but asymmetric: fast path
``STOP > EXIT > CO_LABWARE`` (PAUSE deferred when the wait would
complete naturally on entry, so a paused thread does not hold its
device reservation across the pause); race
``STOP > PAUSE > EXIT > CO_LABWARE > TIMEOUT`` (an operator pause that
arrives mid-wait is durable). Full priority-collapse coverage lives in
``test_co_labware_coordinator.py``; this file pins the pause-during-wait
scenarios that motivated the original co-thread-pause fix.
"""

import asyncio

import pytest

from orca.workflow_models.labware_threads.co_labware_coordinator import (
    CoLabwareCoordinator,
    CoLabwareWaitOutcome,
)


pytestmark = pytest.mark.asyncio


async def test_co_labware_wait_returns_co_labware_when_peer_arrives() -> None:
    stop_event = asyncio.Event()
    co_event = asyncio.Event()
    exit_event = asyncio.Event()
    pause_event = asyncio.Event()

    co_event.set()

    outcome = await CoLabwareCoordinator.wait(
        stop_event=stop_event,
        exit_event=exit_event,
        pause_event=pause_event,
        co_labware_event=co_event,
        timeout=1.0,
    )
    assert outcome == CoLabwareWaitOutcome.CO_LABWARE_PRESENT


async def test_co_labware_wait_returns_pause_when_pause_requested() -> None:
    stop_event = asyncio.Event()
    co_event = asyncio.Event()
    exit_event = asyncio.Event()
    pause_event = asyncio.Event()

    pause_event.set()

    outcome = await CoLabwareCoordinator.wait(
        stop_event=stop_event,
        exit_event=exit_event,
        pause_event=pause_event,
        co_labware_event=co_event,
        timeout=1.0,
    )
    assert outcome == CoLabwareWaitOutcome.PAUSE_REQUESTED


async def test_co_labware_wait_returns_pause_when_pause_set_during_wait() -> None:
    """Pause set after the wait begins still wins -- the race is hot,
    not a one-shot pre-check."""
    stop_event = asyncio.Event()
    co_event = asyncio.Event()
    exit_event = asyncio.Event()
    pause_event = asyncio.Event()

    async def fire_pause_after_delay() -> None:
        # One yield: this task is created before wait() is awaited, and wait()'s
        # pre-checks are synchronous, so wait() parks before this fires the event.
        await asyncio.sleep(0)
        pause_event.set()

    pause_setter = asyncio.create_task(fire_pause_after_delay())
    try:
        outcome = await CoLabwareCoordinator.wait(
            stop_event=stop_event,
            exit_event=exit_event,
            pause_event=pause_event,
            co_labware_event=co_event,
            timeout=1.0,
        )
        assert outcome == CoLabwareWaitOutcome.PAUSE_REQUESTED
    finally:
        await pause_setter


async def test_co_labware_wait_returns_exit_when_method_aborts() -> None:
    stop_event = asyncio.Event()
    co_event = asyncio.Event()
    exit_event = asyncio.Event()
    pause_event = asyncio.Event()

    exit_event.set()

    outcome = await CoLabwareCoordinator.wait(
        stop_event=stop_event,
        exit_event=exit_event,
        pause_event=pause_event,
        co_labware_event=co_event,
        timeout=1.0,
    )
    assert outcome == CoLabwareWaitOutcome.METHOD_EXIT


async def test_co_labware_wait_returns_timeout_when_nothing_fires() -> None:
    stop_event = asyncio.Event()
    co_event = asyncio.Event()
    exit_event = asyncio.Event()
    pause_event = asyncio.Event()

    outcome = await CoLabwareCoordinator.wait(
        stop_event=stop_event,
        exit_event=exit_event,
        pause_event=pause_event,
        co_labware_event=co_event,
        timeout=0.05,
    )
    assert outcome == CoLabwareWaitOutcome.TIMEOUT


async def test_co_labware_wait_exit_beats_co_labware_when_both_preset() -> None:
    """If a joined participant enters the wait after the shared method
    has been aborted AND its peer's co-labware arrival has already
    fired, EXIT must win over CO_LABWARE. Otherwise the participant
    slips into EXECUTING_ACTION and runs the device action even though
    the method that owned the action was aborted -- regression caught
    by ``test_abort_shared_method_interrupts_co_labware_wait``."""
    stop_event = asyncio.Event()
    co_event = asyncio.Event()
    exit_event = asyncio.Event()
    pause_event = asyncio.Event()

    co_event.set()
    exit_event.set()

    outcome = await CoLabwareCoordinator.wait(
        stop_event=stop_event,
        exit_event=exit_event,
        pause_event=pause_event,
        co_labware_event=co_event,
        timeout=1.0,
    )
    assert outcome == CoLabwareWaitOutcome.METHOD_EXIT


async def test_co_labware_wait_pause_beats_co_labware_on_simultaneous_fire() -> None:
    """When pause and co_labware fire in the same tick, pause wins
    deterministically. Pre-Phase-1C this was non-deterministic
    (``asyncio.wait`` done-set order); under the deterministic
    priority STOP > EXIT > PAUSE > CO_LABWARE > TIMEOUT, the operator's
    pause is durable even when the wait would otherwise have unblocked."""
    stop_event = asyncio.Event()
    co_event = asyncio.Event()
    exit_event = asyncio.Event()
    pause_event = asyncio.Event()

    async def fire_both_after_delay() -> None:
        await asyncio.sleep(0)
        co_event.set()
        pause_event.set()

    setter = asyncio.create_task(fire_both_after_delay())
    try:
        outcome = await CoLabwareCoordinator.wait(
            stop_event=stop_event,
            exit_event=exit_event,
            pause_event=pause_event,
            co_labware_event=co_event,
            timeout=1.0,
        )
        assert outcome == CoLabwareWaitOutcome.PAUSE_REQUESTED
    finally:
        await setter
