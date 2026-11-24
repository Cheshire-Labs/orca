"""`RuntimeHolder` builds, rebuilds and unloads a server's runtime. The daemon and Orca AI both construct it."""

import asyncio
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from cheshire_drivers.gateway_protocol import AssignedDevice

from orca.resource_models.labware_placement import IUnsavedPositions
from orca.resource_models.labware_service_interface import ILabwareService
from tests.memory_activity import memory_labware_service, memory_unsaved_positions
from orca.events.event_bus import EventBus
from orca.events.event_bus_interface import IEventBus
from orca.plugins.webhook import WebhookPlugin
from orca.runtime import runtime_holder, startup
from tests.real_device_path import the_real_device_path
from orca.driver_management.device_factory_protocol import IDeviceDriverProvider
from orca.events.emitter import IEventSink
from orca.runtime.package_import import PackageLoadError
from orca.runtime.runtime_holder import AfterBuild, RuntimeBusyError, RuntimeHolder, RuntimeNotReadyError
from orca.runtime.server_events import ServerEvents
from orca.runtime.startup import (
    BuildStep,
    ConfiguredPlugins,
    PackageSetupStoreFactory,
    RuntimeSettings,
    RuntimeStartTimeoutError,
    StartedRuntime,
)
from orca.runtime.store_factory import IRuntimeStoreFactory
from orca.sdk.build import SystemBuild
from orca.state.ops_store import IOpsHistoryStore
from tests.daemon.fixture_package import SIMPLE_WORKFLOW, SLOW_WORKFLOW, write_package

_PACKAGE = "deployment_package"


def _settings(start_timeout: float = 120.0, default_sim_speedup: float | None = None) -> RuntimeSettings:
    return RuntimeSettings(
        default_sim_speedup=default_sim_speedup, start_timeout=start_timeout,
        stall_check_interval=None, stall_required_stable_ticks=2,
    )


def _connections() -> Mock:
    connections = Mock()
    connections.gateway.assignments.publish = AsyncMock()
    connections.gateway.assignments.wait_for_every_sent_build = AsyncMock(return_value=[])
    return connections


def _holder(
    package_dir: Path,
    *,
    setup: IRuntimeStoreFactory | None = None,
    activity: Mock | None = None,
    connections: Mock | None = None,
    sinks: Sequence[IEventSink] = (),
    plugins: ConfiguredPlugins | None = None,
    settings: RuntimeSettings | None = None,
    after_build: Sequence[AfterBuild] = (),
) -> RuntimeHolder:
    return RuntimeHolder(
        package_dir=package_dir,
        setup=setup or Mock(),
        activity=activity or MagicMock(),
        connections=connections or _connections(),
        sinks=sinks,
        labware_events=ServerEvents(),
        device_factory=the_real_device_path().device_factory,
        plugins=plugins or ConfiguredPlugins(),
        settings=settings or _settings(),
        check_run_stores=Mock(),
        after_build=after_build,
    )


def _runs(*, active: int = 0, finished: int = 0) -> list[Mock]:
    records = [Mock(id=f"run-{i}", status=Mock(is_terminal=Mock(return_value=False))) for i in range(active)]
    return records + [Mock(id=f"done-{i}", status=Mock(is_terminal=Mock(return_value=True))) for i in range(finished)]


def _runtime() -> AsyncMock:
    runtime = AsyncMock()
    runtime.system.get_workflow_templates = Mock(return_value={})
    runtime.list_executions = Mock(return_value=[])
    return runtime


async def _started(build_step: BuildStep, builds: list[SystemBuild]) -> StartedRuntime:
    build = await build_step(EventBus(), memory_labware_service(), memory_unsaved_positions())
    builds.append(build)
    return StartedRuntime(build=build, runtime=_runtime())


def _stub_the_build(monkeypatch: pytest.MonkeyPatch, *, assignment: list[AssignedDevice] | None = None) -> AsyncMock:
    """start_runtime runs the holder's build step and hands back a stub runtime; the package import is stubbed."""
    monkeypatch.setattr(
        runtime_holder, "build_system_from_package",
        AsyncMock(return_value=Mock(device_assignment=assignment or [])),
    )
    async def _start(build_step: BuildStep, **_kwargs) -> StartedRuntime:
        return await _started(build_step, [])

    start = AsyncMock(side_effect=_start)
    monkeypatch.setattr(runtime_holder, "start_runtime", start)
    return start


def _start_over_the_real_import(monkeypatch: pytest.MonkeyPatch) -> list[SystemBuild]:
    builds: list[SystemBuild] = []

    async def _start(build_step: BuildStep, **_kwargs) -> StartedRuntime:
        return await _started(build_step, builds)

    monkeypatch.setattr(runtime_holder, "start_runtime", _start)
    return builds


@pytest.fixture
def package_imports_restored() -> Iterator[None]:
    path_before = list(sys.path)
    yield
    sys.path[:] = path_before
    for name in [m for m in sys.modules if m == _PACKAGE or m.startswith(f"{_PACKAGE}.")]:
        del sys.modules[name]


async def test_the_build_hands_start_runtime_the_stores_sinks_plugins_and_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = _stub_the_build(monkeypatch)
    setup, activity, connections, sink = Mock(), MagicMock(), _connections(), Mock()
    plugin = WebhookPlugin(url="https://lims.example/hook")
    settings = _settings(default_sim_speedup=0.25)
    holder = _holder(
        tmp_path, setup=setup, activity=activity, connections=connections,
        sinks=[sink], plugins=ConfiguredPlugins([plugin]), settings=settings,
    )

    await holder.build()
    kwargs = start.call_args.kwargs

    assert (kwargs["setup"], kwargs["activity"], kwargs["connections"]) == (setup, activity, connections)
    assert list(kwargs["sinks"]) == [sink]
    assert list(kwargs["plugins"]) == [plugin]
    assert kwargs["settings"] is settings
    await holder.unload()


async def test_a_check_run_sends_its_events_to_the_sinks_the_runtime_gets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server's event hub, so a check run's events are stored and read by its execution."""
    _stub_the_build(monkeypatch)
    check_runs = Mock(return_value=Mock(aclose=AsyncMock(), busy=False))
    monkeypatch.setattr(runtime_holder, "CheckRuns", check_runs)
    sink = Mock()
    holder = _holder(tmp_path, sinks=[sink])

    await holder.build()

    assert list(check_runs.call_args.kwargs["sinks"]) == [sink]
    await holder.unload()


async def test_a_start_that_times_out_is_the_last_build_error_and_no_runtime_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout = RuntimeStartTimeoutError("the package did not build and start within 0.05s")
    monkeypatch.setattr(runtime_holder, "start_runtime", AsyncMock(side_effect=timeout))
    holder = _holder(tmp_path)

    with pytest.raises(PackageLoadError, match="did not build and start"):
        await asyncio.wait_for(holder.build(), timeout=5.0)

    assert holder.last_build_error is timeout
    assert not holder.is_built
    with pytest.raises(RuntimeNotReadyError) as not_ready:
        holder.runtime
    assert not_ready.value.cause is timeout


async def test_a_package_build_that_never_returns_fails_and_leaves_the_holder_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _never_returns(
        package_name: str, stores: IRuntimeStoreFactory, ops_history_store: IOpsHistoryStore,
        device_factory: IDeviceDriverProvider, labware_service: ILabwareService,
        unsaved_positions: IUnsavedPositions, *, event_bus: IEventBus,
    ) -> SystemBuild:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    monkeypatch.setattr(runtime_holder, "build_system_from_package", _never_returns)
    holder = _holder(tmp_path, settings=_settings(start_timeout=0.05))

    with pytest.raises(PackageLoadError):
        await asyncio.wait_for(holder.build(), timeout=5.0)

    assert isinstance(holder.last_build_error, RuntimeStartTimeoutError)
    assert not holder.is_building
    await asyncio.wait_for(holder.unload(), timeout=1.0)


async def test_a_rebuild_whose_old_runtime_never_shuts_down_fails_under_the_same_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_the_build(monkeypatch)
    holder = _holder(tmp_path, settings=_settings(start_timeout=0.05))
    await holder.build()

    async def _stalls(*, confirm: bool) -> None:
        del confirm
        await asyncio.sleep(30)

    monkeypatch.setattr(holder.runtime, "shutdown", _stalls)
    monkeypatch.setattr(runtime_holder, "start_runtime", startup.start_runtime)

    with pytest.raises(PackageLoadError, match="did not build and start"):
        await asyncio.wait_for(holder.rebuild(), timeout=5.0)

    assert not holder.is_built
    await asyncio.wait_for(holder.unload(), timeout=1.0)


async def test_a_successful_build_publishes_its_device_assignment_and_an_unload_publishes_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignment = [AssignedDevice(name="shaker_1", kind="shaker")]
    _stub_the_build(monkeypatch, assignment=assignment)
    connections = _connections()
    holder = _holder(tmp_path, connections=connections)

    await holder.build()
    await holder.unload()

    publish = connections.gateway.assignments.publish
    assert [call.args[0] for call in publish.await_args_list] == [assignment, []]


async def test_a_build_waits_for_every_client_it_sent_devices_to_before_its_after_build_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_the_build(monkeypatch, assignment=[AssignedDevice(name="shaker_1", kind="shaker")])
    connections = _connections()
    order: list[str] = []
    connections.gateway.assignments.wait_for_every_sent_build = AsyncMock(
        side_effect=lambda timeout: order.append("clients built") or [],
    )

    async def after_build(started: StartedRuntime) -> None:
        order.append("after build")

    await _holder(tmp_path, connections=connections, after_build=[after_build]).build()

    assert order == ["clients built", "after build"]


async def test_a_failed_start_keeps_the_last_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_holder, "start_runtime", AsyncMock(side_effect=RuntimeError("no")))
    connections = _connections()

    with pytest.raises(PackageLoadError):
        await _holder(tmp_path, connections=connections).build()

    connections.gateway.assignments.publish.assert_not_awaited()


async def test_a_rebuild_is_refused_while_a_run_is_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = _stub_the_build(monkeypatch)
    holder = _holder(tmp_path)
    await holder.build()
    monkeypatch.setattr(holder.runtime, "list_executions", Mock(return_value=_runs(active=1)))

    assert holder.has_active_executions()
    with pytest.raises(RuntimeBusyError, match="Active executions exist"):
        await holder.rebuild()

    assert start.await_count == 1
    assert holder.last_build_error is None
    assert holder.is_built


async def test_a_rebuild_proceeds_when_every_run_has_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = _stub_the_build(monkeypatch)
    holder = _holder(tmp_path)
    await holder.build()
    monkeypatch.setattr(holder.runtime, "list_executions", Mock(return_value=_runs(finished=2)))

    await holder.rebuild()

    assert start.await_count == 2
    await holder.unload()


async def test_a_rebuild_is_refused_while_a_check_run_is_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = _stub_the_build(monkeypatch)
    monkeypatch.setattr(runtime_holder, "CheckRuns", Mock(return_value=Mock(aclose=AsyncMock(), busy=True)))
    holder = _holder(tmp_path)
    await holder.build()

    assert holder.has_active_executions()
    with pytest.raises(RuntimeBusyError, match="check run"):
        await holder.rebuild()

    assert start.await_count == 1


async def test_a_holder_over_another_folder_builds_only_its_own_files_once_the_first_unloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package_imports_restored: None,
) -> None:
    first_dir = write_package(tmp_path / "a" / _PACKAGE, workflows=SIMPLE_WORKFLOW)
    second_dir = write_package(tmp_path / "b" / _PACKAGE, workflows=SLOW_WORKFLOW)
    builds = _start_over_the_real_import(monkeypatch)
    first = _holder(first_dir, setup=PackageSetupStoreFactory(first_dir, default_client="local"))
    await first.build()
    await first.unload()
    second = _holder(second_dir, setup=PackageSetupStoreFactory(second_dir, default_client="local"))

    await second.build()

    assert set(builds[-1].system.get_workflow_templates()) == {"slow_window_workflow"}
    await second.unload()


async def test_a_check_run_builds_the_workflows_on_disk_now_over_its_own_operation_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package_imports_restored: None,
) -> None:
    package_dir = write_package(tmp_path / _PACKAGE, workflows=SIMPLE_WORKFLOW)
    builds = _start_over_the_real_import(monkeypatch)
    check_runs = Mock(return_value=Mock(aclose=AsyncMock(), busy=False))
    monkeypatch.setattr(runtime_holder, "CheckRuns", check_runs)
    holder = _holder(package_dir, setup=PackageSetupStoreFactory(package_dir, default_client="local"))
    await holder.build()
    write_package(package_dir, workflows={**SIMPLE_WORKFLOW, **SLOW_WORKFLOW})
    check_run_ops = MagicMock()

    build_steps = check_runs.call_args.kwargs["build_steps"]
    build = await build_steps(check_run_ops, the_real_device_path().device_factory)(EventBus(), memory_labware_service(), memory_unsaved_positions())

    assert set(build.system.get_workflow_templates()) == {"simple_workflow", "slow_window_workflow"}
    assert build.system.ops_history.store is check_run_ops
    assert build.system is not builds[0].system
    await holder.unload()


async def test_a_check_run_builds_from_a_topology_file_edited_after_the_runtime_was_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package_imports_restored: None,
) -> None:
    package_dir = write_package(tmp_path / _PACKAGE, workflows=SIMPLE_WORKFLOW)
    builds = _start_over_the_real_import(monkeypatch)
    check_runs = Mock(return_value=Mock(aclose=AsyncMock(), busy=False))
    monkeypatch.setattr(runtime_holder, "CheckRuns", check_runs)
    holder = _holder(package_dir, setup=PackageSetupStoreFactory(package_dir, default_client="local"))
    await holder.build()
    topology = package_dir / "topology.py"
    edited = topology.read_text(encoding="utf-8") + 'SYSTEM_NAME = "Edited Deployment"'
    topology.write_text(f"{edited}\n", encoding="utf-8")

    build_steps = check_runs.call_args.kwargs["build_steps"]
    build = await build_steps(MagicMock(), the_real_device_path().device_factory)(EventBus(), memory_labware_service(), memory_unsaved_positions())

    assert builds[0].system.name != "Edited Deployment"
    assert build.system.name == "Edited Deployment"
    await holder.unload()


async def test_a_successful_build_clears_the_last_build_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_holder, "start_runtime", AsyncMock(side_effect=RuntimeError("no")))
    holder = _holder(tmp_path)
    with pytest.raises(PackageLoadError):
        await holder.build()
    assert isinstance(holder.last_build_error, RuntimeError)
    _stub_the_build(monkeypatch)

    await holder.build()

    assert holder.last_build_error is None
    await holder.unload()


async def test_a_rebuild_waits_while_a_caller_holds_the_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    start = _stub_the_build(monkeypatch)
    holder = _holder(tmp_path)
    await holder.build()

    async with holder.holding() as held:
        rebuild = asyncio.create_task(holder.rebuild())
        for _ in range(10):
            await asyncio.sleep(0)
        assert not rebuild.done()
        assert start.await_count == 1
        assert holder.runtime is held

    await asyncio.wait_for(rebuild, timeout=1.0)
    assert start.await_count == 2
    await holder.unload()


async def test_only_a_rebuild_starts_the_runtime_as_a_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reload shuts one runtime down and starts the next in the same process, so nothing went unwatched."""
    start = _stub_the_build(monkeypatch)
    holder = _holder(tmp_path)

    await holder.build()
    await holder.rebuild()

    assert [call.kwargs["after_reload"] for call in start.call_args_list] == [False, True]
    await holder.unload()
