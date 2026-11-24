"""A gateway device that reconnects gets its deck re-seeded, hands-off.

The gap this pins (ruling 5, state-reconciliation rulings): the device bridge
that reconnects may have restarted, and a restarted device bridge holds a
rebuilt driver session with an empty deck. Nothing re-declared the world, so
the next command failed resource-not-found hours into a run. Reconnect now
re-seeds automatically -- a state push, no motion; bring-up and homing still
wait for an explicit operator go.
"""

import asyncio

import pytest

from cheshire_drivers import RecordingLiquidHandlerDriver
from cheshire_drivers.gateway_protocol import DeviceConnectInfo
from cheshire_drivers.liquid_handler_models import (
    DeckLayoutConfig,
    LabwareStateResponse,
    ReconcileDeckOccupancyRequest,
)
from orca.gateway.device_gateway import DeviceGateway
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.system_runtime import SystemRuntime
from orca.workflow_models.workflow_templates import WorkflowTemplate
from tests.real_device_path import the_real_device_path
from tests.test_labware_state_reconciliation import RESERVOIR_SITE, _build
from tests.memory_activity import memory_runtime
from tests.recording_handler import RecordingHandler


def _count(recorder: RecordingLiquidHandlerDriver, method: str) -> int:
    return sum(1 for call in recorder.calls if call.method == method)


def _connect_info(name: str) -> DeviceConnectInfo:
    return DeviceConnectInfo(
        name=name, type="liquid_handler", interfaces=frozenset(),
    )


async def _runtime_with_resident(gateway: DeviceGateway):
    recorder = RecordingHandler()
    build, lh = await _build(recorder, wf_name="deck_reconnect")
    runtime = await memory_runtime(build.system, event_bus=build.event_bus,
                            gateway=gateway)
    await runtime.start()
    await runtime.labware.register(
        "reservoir", location=RESERVOIR_SITE, confirm=True,
    )
    return build, lh, runtime, recorder


async def test_reconnect_re_seeds_the_deck() -> None:
    gateway = the_real_device_path().gateway
    build, lh, runtime, recorder = await _runtime_with_resident(gateway)
    try:
        configures = _count(recorder, "configure_deck")
        reconciles = _count(recorder, "reconcile_deck_occupancy")

        await gateway.events.emit_connected(_connect_info(lh.name), "client-1")
        await runtime.flush_deck_reseeds()

        assert _count(recorder, "configure_deck") == configures + 1
        assert _count(recorder, "reconcile_deck_occupancy") == reconciles + 1
    finally:
        await runtime.shutdown()


async def test_reconnect_of_unknown_or_non_lh_device_is_a_no_op() -> None:
    gateway = the_real_device_path().gateway
    build, lh, runtime, recorder = await _runtime_with_resident(gateway)
    try:
        configures = _count(recorder, "configure_deck")

        await gateway.events.emit_connected(_connect_info("stacker"), "client-1")
        await gateway.events.emit_connected(
            _connect_info("no_such_device"), "client-1",
        )
        await runtime.flush_deck_reseeds()

        assert _count(recorder, "configure_deck") == configures
    finally:
        await runtime.shutdown()


async def test_shutdown_unsubscribes_the_reconnect_listener() -> None:
    """A hosted deployment rebuilds SystemRuntime in place; a listener left behind would
    push state through a dead runtime on the next reconnect."""
    gateway = the_real_device_path().gateway
    build, lh, runtime, recorder = await _runtime_with_resident(gateway)
    await runtime.shutdown()
    configures = _count(recorder, "configure_deck")

    await gateway.events.emit_connected(_connect_info(lh.name), "client-1")

    assert _count(recorder, "configure_deck") == configures


class _WireGatedRecorder(RecordingHandler):
    """Deck pushes that stall until 'the receive loop' answers them.

    On the real gateway the reseed's commands resolve only when the SAME
    socket's receive loop settles their response futures, and that loop starts
    after the handshake returns. Gating the deck ops on an event the test sets
    later reproduces that ordering constraint against a local driver.
    """

    def __init__(self, responses_flowing: asyncio.Event) -> None:
        super().__init__()
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
    recorder = _WireGatedRecorder(responses_flowing)
    build, lh = await _build(recorder, wf_name="deck_reconnect_wire")
    gateway = the_real_device_path().gateway
    runtime = await memory_runtime(build.system, event_bus=build.event_bus,
                            gateway=gateway)
    await runtime.start()
    try:
        configures = _count(recorder, "configure_deck")
        responses_flowing.clear()

        await asyncio.wait_for(
            gateway.events.emit_connected(_connect_info(lh.name), "client-1"),
            timeout=5.0,
        )
        assert _count(recorder, "configure_deck") == configures

        responses_flowing.set()
        await asyncio.wait_for(runtime.flush_deck_reseeds(), timeout=5.0)
        assert _count(recorder, "configure_deck") == configures + 1
    finally:
        responses_flowing.set()
        await runtime.shutdown()


class _ReconcileGatedRecorder(RecordingHandler):
    def __init__(self, responses_flowing: asyncio.Event) -> None:
        super().__init__()
        self._responses_flowing = responses_flowing
        self.reconcile_sent = asyncio.Event()

    async def reconcile_deck_occupancy(
        self, request: ReconcileDeckOccupancyRequest,
    ) -> LabwareStateResponse:
        self.reconcile_sent.set()
        await self._responses_flowing.wait()
        return await super().reconcile_deck_occupancy(request)


class _WaitsForReseeds:
    """Wraps the runtime's reseed wait and says when a caller has reached it."""

    def __init__(self, runtime: SystemRuntime) -> None:
        self._flush = runtime.flush_deck_reseeds
        self.reached = asyncio.Event()

    async def __call__(self) -> None:
        self.reached.set()
        await self._flush()


async def _gated_runtime(
    wf_name: str,
) -> tuple[SystemRuntime, DeviceGateway, str, _ReconcileGatedRecorder, asyncio.Event]:
    responses_flowing = asyncio.Event()
    responses_flowing.set()
    recorder = _ReconcileGatedRecorder(responses_flowing)
    build, lh = await _build(recorder, wf_name=wf_name)
    gateway = the_real_device_path().gateway
    runtime = await memory_runtime(build.system, event_bus=build.event_bus, gateway=gateway)
    await runtime.start()
    return runtime, gateway, lh.name, recorder, responses_flowing


async def _hold_a_reconnect_reseed(
    gateway: DeviceGateway, lh_name: str, recorder: _ReconcileGatedRecorder, responses_flowing: asyncio.Event,
) -> None:
    responses_flowing.clear()
    recorder.reconcile_sent.clear()
    await gateway.events.emit_connected(_connect_info(lh_name), "client-1")
    await asyncio.wait_for(recorder.reconcile_sent.wait(), timeout=5.0)


async def test_a_run_submitted_during_a_reseed_waits_for_it_and_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, gateway, lh_name, recorder, responses_flowing = await _gated_runtime("deck_reconnect_submit")
    waits = _WaitsForReseeds(runtime)
    monkeypatch.setattr(runtime, "flush_deck_reseeds", waits)
    try:
        await _hold_a_reconnect_reseed(gateway, lh_name, recorder, responses_flowing)

        submit = asyncio.create_task(
            runtime.submit_workflow("deck_reconnect_submit", mode=WorkflowRunMode.SIM),
        )
        reached = asyncio.create_task(waits.reached.wait())
        await asyncio.wait({submit, reached}, timeout=5.0, return_when=asyncio.FIRST_COMPLETED)
        reached.cancel()
        # The reseed holds the device, so the submit must still be waiting. If it finished, it was refused.
        assert not submit.done(), f"the run was decided before the reseed finished: {submit.exception()!r}"

        responses_flowing.set()
        await asyncio.wait_for(submit, timeout=5.0)
    finally:
        responses_flowing.set()
        await runtime.shutdown()


async def test_a_run_whose_device_reconnects_before_it_starts_waits_for_the_reseed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, gateway, lh_name, recorder, responses_flowing = await _gated_runtime("deck_reconnect_start")
    run_workflow = runtime._run_workflow
    waits = _WaitsForReseeds(runtime)

    async def reconnect_then_run(execution_id: str, workflow: WorkflowTemplate) -> None:
        await _hold_a_reconnect_reseed(gateway, lh_name, recorder, responses_flowing)
        monkeypatch.setattr(runtime, "flush_deck_reseeds", waits)
        await run_workflow(execution_id, workflow)

    monkeypatch.setattr(runtime, "_run_workflow", reconnect_then_run)
    try:
        record = await runtime.submit_workflow("deck_reconnect_start", mode=WorkflowRunMode.SIM)
        await asyncio.wait_for(waits.reached.wait(), timeout=5.0)
        responses_flowing.set()

        final = await asyncio.wait_for(runtime.wait(record.id), timeout=40.0)
        assert final.error is None, final.error
    finally:
        responses_flowing.set()
        await runtime.shutdown()
