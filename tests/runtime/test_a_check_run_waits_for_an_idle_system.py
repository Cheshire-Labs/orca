"""A check run starts only while no other run is running."""

from pathlib import Path

import pytest

from orca.sdk.build import build_system_over
from orca.resource_models.labware_placement import IUnsavedPositions
from orca.resource_models.labware_service_interface import ILabwareService
from orca.daemon.check_run_stores import SqliteCheckRunStores
from orca.runtime.check_runs import CheckRuns
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.errors import OtherRunModeActiveError
from orca.runtime.startup import BuildStep, RuntimeSettings
from orca.state.ops_store import IOpsHistoryStore
import orca.orca as orca
from orca.events.event_bus_interface import IEventBus
from cheshire_drivers.gateway_protocol import AssignedDevice
from orca.gateway.in_process_client import InProcessClient
from orca.resource_models.plate_pad import PlatePad
from orca.driver_management.device_factory_protocol import IDeviceDriverProvider
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import SystemBuild, Topology
from tests.memory_activity import memory_activity_services, memory_runtime
from tests.no_devices import NullGatewayRegistry
from tests.runtime.manual_place_fixtures import _build_manual_place_system, _live_connection_source
from tests.setup_package import package_stores


def _never_built(ops_history: IOpsHistoryStore, device_factory: IDeviceDriverProvider) -> BuildStep:
    raise AssertionError("a refused check run builds nothing")


def _one_pad(ops_history: IOpsHistoryStore, device_factory: IDeviceDriverProvider) -> BuildStep:
    async def build(event_bus: IEventBus, labware: ILabwareService, unsaved: IUnsavedPositions) -> SystemBuild:
        return await build_system_over(
            name="One Pad", topology=Topology(locations={"pad": PlatePad("pad")}, transporters=[]),
            stores=package_stores(), event_bus=event_bus, ops_history_store=ops_history,
            labware_service=labware, unsaved_positions=unsaved,
        )
    return build


async def test_a_check_run_is_refused_while_a_live_run_runs(tmp_path: Path) -> None:
    system = await _build_manual_place_system("wf_live")
    runtime = await memory_runtime(
        system, gateway_registry=NullGatewayRegistry(), connection_source=_live_connection_source(),
    )
    await runtime.start()
    try:
        live = await runtime.submit_workflow("wf_live", mode=WorkflowRunMode.LIVE)
        check_runs = CheckRuns(
            system=runtime,
            stores=SqliteCheckRunStores(tmp_path),
            build_steps=_never_built,
            setup=package_stores(),
            activity=await memory_activity_services(system),
            sinks=(),
            settings=RuntimeSettings(
                default_sim_speedup=None, start_timeout=30.0,
                stall_check_interval=None, stall_required_stable_ticks=1,
            ),
        )

        with pytest.raises(OtherRunModeActiveError) as refused:
            async with check_runs.submitting():
                pass

        assert (refused.value.blocking_execution_id, refused.value.existing_run_mode) == (
            live.id, WorkflowRunMode.LIVE,
        )
        assert await check_runs.stores.entries() == []
        assert check_runs.active is None
    finally:
        await runtime.shutdown()


async def test_a_check_run_whose_client_cannot_start_shuts_its_runtime_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    system = await _build_manual_place_system("wf_main")
    runtime = await memory_runtime(system, gateway_registry=NullGatewayRegistry())
    await runtime.start()
    shut_down: list[SystemRuntime] = []
    real_shutdown = SystemRuntime.shutdown

    async def recorded_shutdown(self: SystemRuntime, *, confirm: bool = False) -> None:
        shut_down.append(self)
        await real_shutdown(self, confirm=confirm)

    async def client_fails(self: InProcessClient, devices: list[AssignedDevice]) -> None:
        raise RuntimeError("the client could not start")

    monkeypatch.setattr(SystemRuntime, "shutdown", recorded_shutdown)
    monkeypatch.setattr(InProcessClient, "run", client_fails)
    try:
        check_runs = CheckRuns(
            system=runtime,
            stores=SqliteCheckRunStores(tmp_path),
            build_steps=_one_pad,
            setup=package_stores(),
            activity=await memory_activity_services(system),
            sinks=(),
            settings=RuntimeSettings(
                default_sim_speedup=None, start_timeout=30.0,
                stall_check_interval=None, stall_required_stable_ticks=1,
            ),
        )

        with pytest.raises(RuntimeError, match="could not start"):
            async with check_runs.submitting():
                pass

        assert [r for r in shut_down if r is not runtime], "the check run's runtime was left running"
        assert check_runs.active is None
    finally:
        await runtime.shutdown()
