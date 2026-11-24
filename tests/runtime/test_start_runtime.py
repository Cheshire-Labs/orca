"""Both servers build their package and start their runtime through `start_runtime`, under one time limit."""

import asyncio

import pytest

from orca.resource_models.labware_placement import IUnsavedPositions
from orca.resource_models.labware_service_interface import ILabwareService
from orca.events.event_bus_interface import IEventBus
from orca.events.runtime_event import RuntimeEvent
from orca.plugins.base import OrcaPlugin
from orca.runtime import startup
from orca.events.emitter import IEventSink
from orca.runtime.server_events import ServerEvents
from orca.runtime.startup import (
    START_TIMEOUT_SECONDS,
    BuildStep,
    RuntimeSettings,
    RuntimeStartTimeoutError,
    start_runtime,
)
from orca.runtime.store_factory import default_package_root
from orca.runtime.startup import PackageSetupStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import SystemBuild
from tests.memory_activity import memory_activity_services, memory_connections, memory_ops_store
from tests.example_builds import build_smc
from cheshire_drivers.device_setup import BUILTIN_CLIENT


class _RecordingSink:
    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []

    def on_event(self, event: RuntimeEvent) -> None:
        self.events.append(event)


async def _build() -> SystemBuild:
    return await build_smc()


def _settings(timeout: float = START_TIMEOUT_SECONDS) -> RuntimeSettings:
    return RuntimeSettings(
        default_sim_speedup=None, start_timeout=timeout,
        stall_check_interval=None, stall_required_stable_ticks=2,
    )


def _built(build: SystemBuild) -> BuildStep:
    async def _step(_event_bus: IEventBus, _labware: ILabwareService, _unsaved: IUnsavedPositions) -> SystemBuild:
        return build
    return _step


async def _start(build: SystemBuild, timeout: float = START_TIMEOUT_SECONDS) -> SystemRuntime:
    started = await start_runtime(
        _built(build),
        setup=PackageSetupStoreFactory(default_package_root(), default_client=BUILTIN_CLIENT),
        activity=await memory_activity_services(build.system),
        connections=memory_connections(),
        sinks=[],
        labware_events=ServerEvents(),
        plugins=[],
        settings=_settings(timeout),
    )
    return started.runtime


def _shutdowns(monkeypatch: pytest.MonkeyPatch) -> list[SystemRuntime]:
    seen: list[SystemRuntime] = []
    real = SystemRuntime.shutdown

    async def _recording(self: SystemRuntime, *, confirm: bool = False) -> None:
        seen.append(self)
        await real(self, confirm=confirm)

    monkeypatch.setattr(SystemRuntime, "shutdown", _recording)
    return seen


async def test_every_sink_is_registered_before_the_runtime_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real_register, real_start = SystemRuntime.register_sink, SystemRuntime.start

    def _register(self: SystemRuntime, sink: IEventSink) -> None:
        calls.append("register_sink")
        real_register(self, sink)

    async def _start_and_note(self: SystemRuntime, *, after_reload: bool = False) -> None:
        calls.append("start")
        await real_start(self, after_reload=after_reload)

    monkeypatch.setattr(SystemRuntime, "register_sink", _register)
    monkeypatch.setattr(SystemRuntime, "start", _start_and_note)
    build = await _build()

    started = await start_runtime(
        _built(build),
        setup=PackageSetupStoreFactory(default_package_root(), default_client=BUILTIN_CLIENT),
        activity=await memory_activity_services(build.system),
        connections=memory_connections(),
        sinks=[_RecordingSink(), _RecordingSink()],
        labware_events=ServerEvents(),
        plugins=[],
        settings=_settings(),
    )
    await started.runtime.shutdown(confirm=True)

    # The runtime registers sinks of its own during start; the two passed in come first.
    assert calls[:3] == ["register_sink", "register_sink", "start"]


async def test_a_start_that_never_returns_times_out_and_shuts_the_runtime_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _never(self: SystemRuntime, *, after_reload: bool = False) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(SystemRuntime, "start", _never)
    shut_down = _shutdowns(monkeypatch)

    with pytest.raises(RuntimeStartTimeoutError):
        await _start(await _build(), timeout=0.05)

    assert len(shut_down) == 1


async def test_a_package_build_that_never_returns_times_out_under_the_same_limit() -> None:
    build = await _build()

    async def _stalls(_event_bus: IEventBus, _labware: ILabwareService, _unsaved: IUnsavedPositions) -> SystemBuild:
        await asyncio.sleep(30)
        return build

    with pytest.raises(RuntimeStartTimeoutError, match="did not build and start"):
        await asyncio.wait_for(
            start_runtime(
                _stalls,
                setup=PackageSetupStoreFactory(default_package_root(), default_client=BUILTIN_CLIENT),
                activity=await memory_activity_services(build.system),
                connections=memory_connections(),
                sinks=[],
                labware_events=ServerEvents(),
                plugins=[],
                settings=_settings(0.05),
            ),
            timeout=5.0,
        )


async def test_a_start_that_fails_shuts_the_runtime_down_and_raises_the_start_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fails(self: SystemRuntime, *, after_reload: bool = False) -> None:
        raise ValueError("bad topology")

    monkeypatch.setattr(SystemRuntime, "start", _fails)
    shut_down = _shutdowns(monkeypatch)

    with pytest.raises(ValueError, match="bad topology"):
        await _start(await _build())

    assert len(shut_down) == 1


async def test_a_build_that_records_operations_somewhere_else_is_refused() -> None:
    build = await _build()
    activity = await memory_activity_services(build.system)
    elsewhere = startup.ActivityServices(
        labware=activity.labware,
        incidents=activity.incidents,
        execution_records=activity.execution_records,
        ops_history=memory_ops_store(),
        device_modes=activity.device_modes,
        unsaved_records=activity.unsaved_records,
    )

    with pytest.raises(ValueError, match="different operation store"):
        await start_runtime(
            _built(build),
            setup=PackageSetupStoreFactory(default_package_root(), default_client=BUILTIN_CLIENT),
            activity=elsewhere,
            connections=memory_connections(),
            sinks=[],
            labware_events=ServerEvents(),
            plugins=[],
            settings=_settings(),
        )


class _Plugin(OrcaPlugin):
    def handle_runtime_event(self, event: RuntimeEvent) -> None:
        del event


async def test_every_plugin_is_registered_on_the_started_runtime() -> None:
    build = await _build()
    plugin = _Plugin()

    started = await start_runtime(
        _built(build),
        setup=PackageSetupStoreFactory(default_package_root(), default_client=BUILTIN_CLIENT),
        activity=await memory_activity_services(build.system),
        connections=memory_connections(),
        sinks=[],
        labware_events=ServerEvents(),
        plugins=[plugin],
        settings=_settings(),
    )
    registered = started.runtime.get_plugin(_Plugin)
    await started.runtime.shutdown(confirm=True)

    assert registered is plugin


@pytest.mark.parametrize("after_reload", [False, True])
async def test_start_runtime_tells_the_runtime_whether_it_is_a_reload(
    monkeypatch: pytest.MonkeyPatch, after_reload: bool,
) -> None:
    seen: list[bool] = []
    real_start = SystemRuntime.start

    async def _start_and_note(self: SystemRuntime, *, after_reload: bool = False) -> None:
        seen.append(after_reload)
        await real_start(self, after_reload=after_reload)

    monkeypatch.setattr(SystemRuntime, "start", _start_and_note)
    build = await _build()

    started = await start_runtime(
        _built(build),
        setup=PackageSetupStoreFactory(default_package_root(), default_client=BUILTIN_CLIENT),
        activity=await memory_activity_services(build.system),
        connections=memory_connections(),
        sinks=[],
        labware_events=ServerEvents(),
        plugins=[],
        settings=_settings(),
        after_reload=after_reload,
    )
    await started.runtime.shutdown(confirm=True)

    assert seen == [after_reload]
