"""A server's configured plugins hear every event the server sends, from its one event hub, with or without a runtime."""

import time

from cheshire_drivers.device_setup import BUILTIN_CLIENT

from orca.resource_models.labware_placement import IUnsavedPositions
from orca.resource_models.labware_service_interface import ILabwareService
from orca.events.execution_context import DeviceConnectionContext, HostedEventContext
from orca.events.runtime_event import RuntimeEvent
from orca.plugins.async_offloading import AsyncOffloadingPlugin
from orca.runtime.server_events import ServerEvents
from orca.runtime.startup import ConfiguredPlugins, PackageSetupStoreFactory, RuntimeSettings, start_runtime
from orca.runtime.store_factory import default_package_root
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import SystemBuild
from orca.events.event_bus_interface import IEventBus
from tests.example_builds import build_smc
from tests.memory_activity import memory_activity_services, memory_connections
from tests.test_helpers import wait_until


class _Recording(AsyncOffloadingPlugin):
    def __init__(self) -> None:
        super().__init__(queue_size=100, stop_drain_timeout=1.0)
        self.delivered: list[str] = []

    async def deliver(self, event: RuntimeEvent) -> None:
        self.delivered.append(event.event_name)


class _Raising:
    def on_event(self, event: RuntimeEvent) -> None:
        raise RuntimeError(f"cannot take {event.event_name}")


def _disconnected(device: str) -> RuntimeEvent:
    return RuntimeEvent(
        event_name=f"DEVICE.{device}.DISCONNECTED", execution_id="_system", timestamp=time.time(),
        entity_type="DEVICE", entity_id=device, status="DISCONNECTED",
        context=DeviceConnectionContext(device_name=device, client_id=None),
    )


async def test_a_plugin_hears_a_device_disconnect_with_no_runtime_built() -> None:
    events = ServerEvents()
    plugin = _Recording()
    configured = ConfiguredPlugins([plugin])
    configured.start(events)
    try:
        events.emit(_disconnected("shaker_1"))
        await wait_until(lambda: plugin.delivered == ["DEVICE.shaker_1.DISCONNECTED"])
    finally:
        await configured.stop()

    events.emit(_disconnected("shaker_2"))
    assert plugin.delivered == ["DEVICE.shaker_1.DISCONNECTED"]


async def test_a_hosted_servers_own_events_do_not_reach_a_plugin() -> None:
    events = ServerEvents()
    plugin = _Recording()
    configured = ConfiguredPlugins([plugin])
    configured.start(events)
    try:
        events.emit(RuntimeEvent(
            event_name="hosted.state", execution_id="_system", timestamp=time.time(), entity_type="HOSTED",
            entity_id="pf400_1", status="sample", context=HostedEventContext.model_validate({"device_name": "pf400_1"}),
        ))
        events.emit(_disconnected("marker"))
        await wait_until(lambda: "DEVICE.marker.DISCONNECTED" in plugin.delivered)
    finally:
        await configured.stop()

    assert plugin.delivered == ["DEVICE.marker.DISCONNECTED"]


async def test_a_failing_watcher_on_the_hub_does_not_keep_an_event_from_a_plugin() -> None:
    events = ServerEvents()
    events.add_sink(_Raising())
    plugin = _Recording()
    configured = ConfiguredPlugins([plugin])
    configured.start(events)
    try:
        events.emit(_disconnected("shaker_1"))
        await wait_until(lambda: plugin.delivered == ["DEVICE.shaker_1.DISCONNECTED"])
    finally:
        await configured.stop()


async def test_a_runtime_event_reaches_a_configured_plugin_once() -> None:
    build = await build_smc()

    async def _step(_event_bus: IEventBus, _labware: ILabwareService, _unsaved: IUnsavedPositions) -> SystemBuild:
        return build

    events = ServerEvents()
    plugin = _Recording()
    configured = ConfiguredPlugins([plugin])
    configured.start(events)
    started = await start_runtime(
        _step,
        setup=PackageSetupStoreFactory(default_package_root(), default_client=BUILTIN_CLIENT),
        activity=await memory_activity_services(build.system),
        connections=memory_connections(),
        sinks=[events],
        labware_events=events,
        plugins=configured.plugins,
        settings=RuntimeSettings(
            default_sim_speedup=None, start_timeout=60.0, stall_check_interval=None, stall_required_stable_ticks=2,
        ),
    )
    runtime = started.runtime
    assert isinstance(runtime, SystemRuntime)
    try:
        assert runtime.get_plugin(_Recording) is plugin
        runtime._system_event_bus.emit(_disconnected("sealer_1"))
        # The queue is first in, first out, so once this one is delivered every copy before it has been too.
        events.emit(_disconnected("marker"))
        await wait_until(lambda: "DEVICE.marker.DISCONNECTED" in plugin.delivered)
        await runtime.shutdown(confirm=True)
    finally:
        await configured.stop()

    assert plugin.delivered.count("DEVICE.sealer_1.DISCONNECTED") == 1
