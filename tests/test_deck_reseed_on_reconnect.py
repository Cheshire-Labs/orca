"""A gateway device that reconnects gets its deck re-seeded, hands-off.

The gap this pins (ruling 5, state-reconciliation rulings): the device bridge
that reconnects may have restarted, and a restarted device bridge holds a
rebuilt driver session with an empty deck. Nothing re-declared the world, so
the next command failed resource-not-found hours into a run. Reconnect now
re-seeds automatically -- a state push, no motion; bring-up and homing still
wait for an explicit operator go.
"""

import asyncio

from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.gateway_protocol import DeviceConnectInfo
from cheshire_drivers.liquid_handler_models import (
    DeckLayoutConfig,
    LabwareStateResponse,
    ReconcileDeckOccupancyRequest,
)
from cheshire_drivers.plr import ChatterboxLiquidHandlerDriver
from orca.gateway.websocket.connection_events import connection_events
from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.system_runtime import SystemRuntime
from tests.test_labware_state_reconciliation import RESERVOIR_SITE, _build


def _count(recorder: RecordingLiquidHandlerDriver, method: str) -> int:
    return sum(1 for call in recorder.calls if call.method == method)


def _connect_info(name: str) -> DeviceConnectInfo:
    return DeviceConnectInfo(
        name=name, type="liquid_handler", interfaces=frozenset(),
    )


async def _runtime_with_resident():
    recorder = RecordingLiquidHandlerDriver(ChatterboxLiquidHandlerDriver(num_channels=8))
    build, lh = await _build(recorder, wf_name="deck_reconnect")
    runtime = SystemRuntime(build.system, event_bus=build.event_bus,
                            labware_store=InMemoryLabwareStore())
    await runtime.start()
    await runtime.labware.register(
        "reservoir", location=RESERVOIR_SITE, confirm=True,
    )
    return build, lh, runtime, recorder


async def test_reconnect_re_seeds_the_deck() -> None:
    build, lh, runtime, recorder = await _runtime_with_resident()
    try:
        configures = _count(recorder, "configure_deck")
        reconciles = _count(recorder, "reconcile_deck_occupancy")

        await connection_events.emit_connected(_connect_info(lh.name), "client-1")
        await runtime.flush_deck_reseeds()

        assert _count(recorder, "configure_deck") == configures + 1
        assert _count(recorder, "reconcile_deck_occupancy") == reconciles + 1
    finally:
        await runtime.shutdown()
        connection_events.clear()


async def test_reconnect_of_unknown_or_non_lh_device_is_a_no_op() -> None:
    build, lh, runtime, recorder = await _runtime_with_resident()
    try:
        configures = _count(recorder, "configure_deck")

        await connection_events.emit_connected(_connect_info("stacker"), "client-1")
        await connection_events.emit_connected(
            _connect_info("no_such_device"), "client-1",
        )
        await runtime.flush_deck_reseeds()

        assert _count(recorder, "configure_deck") == configures
    finally:
        await runtime.shutdown()
        connection_events.clear()


async def test_shutdown_unsubscribes_the_reconnect_listener() -> None:
    """A hosted deployment rebuilds SystemRuntime in place; a listener left behind would
    push state through a dead runtime on the next reconnect."""
    build, lh, runtime, recorder = await _runtime_with_resident()
    try:
        await runtime.shutdown()
        configures = _count(recorder, "configure_deck")

        await connection_events.emit_connected(_connect_info(lh.name), "client-1")

        assert _count(recorder, "configure_deck") == configures
    finally:
        connection_events.clear()


class _WireGatedRecorder(RecordingLiquidHandlerDriver):
    """Deck pushes that stall until 'the receive loop' answers them.

    On the real gateway the reseed's commands resolve only when the SAME
    socket's receive loop settles their response futures, and that loop starts
    after the handshake returns. Gating the deck ops on an event the test sets
    later reproduces that ordering constraint against a local driver.
    """

    def __init__(
        self, inner: ChatterboxLiquidHandlerDriver, responses_flowing: asyncio.Event,
    ) -> None:
        super().__init__(inner)
        self._responses_flowing = responses_flowing

    async def configure_deck(self, config: DeckLayoutConfig) -> LabwareStateResponse:
        await self._responses_flowing.wait()
        return await super().configure_deck(config)

    async def reconcile_deck_occupancy(
        self, request: ReconcileDeckOccupancyRequest,
    ) -> LabwareStateResponse:
        await self._responses_flowing.wait()
        return await super().reconcile_deck_occupancy(request)


async def test_handshake_returns_before_the_reseed_completes() -> None:
    """The connected emit runs inside the websocket handshake, before that
    socket's receive loop exists; the reseed's commands complete only once the
    loop answers them. The emit must return with the reseed still pending
    (inline awaiting self-deadlocks until CommandTimeoutError), and the reseed
    must then finish once responses flow."""
    responses_flowing = asyncio.Event()
    responses_flowing.set()  # open during startup's own deck pushes
    recorder = _WireGatedRecorder(
        ChatterboxLiquidHandlerDriver(num_channels=8), responses_flowing,
    )
    build, lh = await _build(recorder, wf_name="deck_reconnect_wire")
    runtime = SystemRuntime(build.system, event_bus=build.event_bus,
                            labware_store=InMemoryLabwareStore())
    await runtime.start()
    try:
        configures = _count(recorder, "configure_deck")
        responses_flowing.clear()

        await asyncio.wait_for(
            connection_events.emit_connected(_connect_info(lh.name), "client-1"),
            timeout=5.0,
        )
        assert _count(recorder, "configure_deck") == configures

        responses_flowing.set()
        await asyncio.wait_for(runtime.flush_deck_reseeds(), timeout=5.0)
        assert _count(recorder, "configure_deck") == configures + 1
    finally:
        responses_flowing.set()
        await runtime.shutdown()
        connection_events.clear()
