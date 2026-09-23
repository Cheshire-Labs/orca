"""``CoLabwareCoordinator``: owns the AWAITING_CO_THREADS race + the
post-execution tip-rack event emission.
"""
import asyncio
from enum import Enum

from orca.async_util import drain_cancelled_waiters
from orca.events.event_bus_interface import IEventBus
from orca.events.execution_context import ThreadExecutionContext
from orca.workflow_models.actions.executable_location_action import ExecutableLocationAction


class CoLabwareWaitOutcome(Enum):
    """Result of the AWAITING_CO_THREADS race. Priority is asymmetric:
    fast path STOP > EXIT > CO_LABWARE; race STOP > PAUSE > EXIT > CO_LABWARE > TIMEOUT.
    See ``CoLabwareCoordinator`` for rationale.
    """
    STOP_REQUESTED = "stop_requested"
    METHOD_EXIT = "method_exit"
    PAUSE_REQUESTED = "pause_requested"
    CO_LABWARE_PRESENT = "co_labware_present"
    TIMEOUT = "timeout"


class CoLabwareCoordinator:
    """Stateless helper for the AWAITING_CO_THREADS race + tip emission.

    Owned conceptually by ``ExecutingLabwareThread`` but invoked as
    static methods so unit tests can drive the race with bare
    ``asyncio.Event`` instances and the orchestrator stays the only
    site that touches thread / method state.
    """

    @staticmethod
    async def wait(
        *,
        stop_event: asyncio.Event,
        exit_event: asyncio.Event,
        pause_event: asyncio.Event,
        co_labware_event: asyncio.Event,
        timeout: float | None,
    ) -> CoLabwareWaitOutcome:
        """Race four events with the asymmetric priority documented on
        ``CoLabwareWaitOutcome``.
        """
        if stop_event.is_set():
            return CoLabwareWaitOutcome.STOP_REQUESTED
        if exit_event.is_set():
            return CoLabwareWaitOutcome.METHOD_EXIT
        if co_labware_event.is_set():
            return CoLabwareWaitOutcome.CO_LABWARE_PRESENT

        stop_task = asyncio.ensure_future(stop_event.wait())
        exit_task = asyncio.ensure_future(exit_event.wait())
        pause_task = asyncio.ensure_future(pause_event.wait())
        co_task = asyncio.ensure_future(co_labware_event.wait())

        try:
            done, _ = await asyncio.wait(
                {stop_task, exit_task, pause_task, co_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            await drain_cancelled_waiters(
                stop_task, exit_task, pause_task, co_task,
            )

        if stop_task in done:
            return CoLabwareWaitOutcome.STOP_REQUESTED
        if pause_task in done:
            return CoLabwareWaitOutcome.PAUSE_REQUESTED
        if exit_task in done:
            return CoLabwareWaitOutcome.METHOD_EXIT
        if co_task in done:
            return CoLabwareWaitOutcome.CO_LABWARE_PRESENT
        return CoLabwareWaitOutcome.TIMEOUT

    @staticmethod
    async def emit_tip_events(
        action: ExecutableLocationAction,
        context: ThreadExecutionContext,
        event_bus: IEventBus,
    ) -> None:
        """Emit ``TIP_RACK.LOW`` / ``TIP_RACK.EMPTY`` events derived
        from the ledger after the action executes. Tip occupancy is
        read via ``tips_present(await labware.ops(), labware.name)`` on
        the system's OpsHistory (written by ``TrackingContext.store_record``
        on the same tick).
        """
        from orca.resource_models.labware import TipRackInstance
        from orca.state.projections import (
            has_tip_baseline,
            tips_present,
        )

        for labware in action.action.expected_inputs:
            if not isinstance(labware, TipRackInstance):
                continue
            num_tips = labware.tip_rack.num_tips
            if num_tips == 0:
                continue
            ops = await labware.ops()
            if not has_tip_baseline(ops, labware.name):
                # Nothing bound says where this rack started, so an empty fold
                # would announce EMPTY for a rack nobody has touched.
                continue
            present_count = len(tips_present(ops, labware.name))
            used_fraction = 1.0 - (present_count / num_tips)
            if present_count == 0:
                event_bus.emit("TIP_RACK.EMPTY", context)
            elif used_fraction > 0.75:
                event_bus.emit("TIP_RACK.LOW", context)
