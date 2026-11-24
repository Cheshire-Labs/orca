"""Workflow run modes and dispatch resolution.

Covers:

- `device_run_mode`: what one device runs as under each run mode and setting.
- `runtime.submit_workflow(mode=...)`: refuses a sim or live run whose devices are not ready.
"""

from orca.config import OrcaConfig
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest
from tests.no_devices import NullDeviceConnectionSource
from orca.runtime.server_events import ServerEvents
from orca.runtime.startup import build_device_gateway

from tests.device_modes import run_by_an_orca_client

from orca.system.errors import DevicesNotReadyError

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from tests.no_devices import NullGatewayRegistry
from orca.modes.device_modes import DeviceMode
from orca.modes.run_modes import WorkflowRunMode, device_run_mode
from orca.runtime.status_models import (
    ConnectionCard,
    GatewayDeviceEntry,
)
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import (
    ActionTemplate,
    MethodTemplate,
    WorkflowTemplate,
)
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from tests.runtime.registries.test_device_registry import (
    FakeConnectionSource,
    FakeTopologyRegistry,
)
from tests.mock import TRANSPORTER_MOCK_INTERFACES, UNIVERSAL_MOCK_INTERFACES
from tests.test_helpers import (
    create_test_device,
    create_test_plate_template,
    create_test_transporter,
    wire_system_map,
)
from tests.memory_activity import memory_ops_store, memory_runtime, memory_variable_store
from tests.memory_activity import memory_unsaved_positions
from tests.labware_catalog import bundled_labware_catalog
from tests.memory_activity import memory_labware_service


# --- Resolver matrix --------------------------------------------------------


@pytest.mark.parametrize(("run_mode", "setting", "runs_as"), [
    (WorkflowRunMode.CHECK_RUN, DeviceMode.SIM, WorkflowRunMode.CHECK_RUN),
    (WorkflowRunMode.CHECK_RUN, DeviceMode.LIVE, WorkflowRunMode.CHECK_RUN),
    (WorkflowRunMode.SIM, DeviceMode.SIM, WorkflowRunMode.SIM),
    (WorkflowRunMode.SIM, DeviceMode.LIVE, WorkflowRunMode.SIM),
    (WorkflowRunMode.LIVE, DeviceMode.SIM, WorkflowRunMode.SIM),
    (WorkflowRunMode.LIVE, DeviceMode.LIVE, WorkflowRunMode.LIVE),
])
def test_a_live_run_uses_each_devices_setting_and_the_other_modes_decide_for_every_device(
    run_mode: WorkflowRunMode, setting: DeviceMode, runs_as: WorkflowRunMode,
) -> None:
    assert device_run_mode(run_mode, setting) is runs_as


# --- Real-system fixtures ---------------------------------------------------


async def _build_system() -> ISystem:
    """Construct a minimal System with a shaker + transporter for run-mode tests."""
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    shaker = create_test_device("shaker1")
    registry.add_resource(shaker)
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [shaker])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": shaker}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate])
    async def shake_action(ctx: ActionContext) -> None:
        # Sim devices don't actually run; the function body is never invoked
        # in the topology-validation-only tests below.
        del ctx

    @orca.method
    async def shake_method(
        ctx: MethodContext,
    ) -> AsyncGenerator[ActionTemplate, None]:
        del ctx
        yield shake_action

    pad_loc = system_map.get_location("pad1")

    @orca.thread(labware=plate, start=pad_loc, end=pad_loc)
    async def plate_thread(
        ctx: ThreadContext,
    ) -> AsyncGenerator[MethodTemplate, None]:
        del ctx
        yield shake_method

    workflow = WorkflowTemplate("run_mode_test_workflow")
    workflow.add_thread(plate_thread, is_start=True)

    builder = SdkToSystemBuilder(
        name="run_mode_test",
        description="",
        labwares=[plate],
        resources_registry=registry,
        system_map=system_map,
        workflows=[workflow],
        event_bus=EventBus(), config=OrcaConfig(), ops_history_store=memory_ops_store(), variable_store=memory_variable_store(),
        labware_catalog=bundled_labware_catalog(),
        labware_service=memory_labware_service(),
        unsaved_positions=memory_unsaved_positions(),
    )
    await builder.bind_labwares()
    return builder.get_system()


# --- submit_workflow validation --------------------------------------------


class _StubGateway:
    """Gateway registry that returns a fixed list of GatewayDeviceEntry."""

    def __init__(self, entries: list[GatewayDeviceEntry]) -> None:
        self._entries = entries

    async def list_connected(self) -> list[GatewayDeviceEntry]:
        return list(self._entries)

    async def get_gateway_status(self, name: str) -> GatewayDeviceEntry | None:
        for e in self._entries:
            if e.name == name:
                return e
        return None


def _connection_card(
    name: str,
    *,
    interfaces: frozenset[str] = UNIVERSAL_MOCK_INTERFACES,
    advertised_kind: str = "UniversalMockDevice",
    last_heartbeat: datetime | None = None,
) -> ConnectionCard:
    return ConnectionCard(
        name=name,
        client_id=f"client-{name}",
        connection_id=f"conn-{name}",
        last_heartbeat=last_heartbeat,
        advertised_kind=advertised_kind,
        advertised_interfaces=interfaces,
    )


async def test_submit_workflow_check_run_succeeds_with_topology_only() -> None:
    system = await _build_system()
    runtime = await memory_runtime(
        system,
        gateway_registry=NullGatewayRegistry(),
    )
    await runtime.start()
    record = await runtime.submit_workflow("run_mode_test_workflow", mode=WorkflowRunMode.CHECK_RUN)
    assert record.workflow_name == "run_mode_test_workflow"
    await runtime.shutdown()


async def test_submit_workflow_requires_run_mode() -> None:
    """v3.4: omitting `mode=` raises `RunModeRequiredError` (no fallback).

    The deployment base mode is no longer a submit-time fallback under
    sim-hierarchy v3.4 -- the operator must declare a mode per submission.
    """
    from orca.runtime.errors import RunModeRequiredError
    system = await _build_system()
    runtime = await memory_runtime(
        system,
        gateway_registry=NullGatewayRegistry(),
    )
    await runtime.start()
    with pytest.raises(RunModeRequiredError):
        await runtime.submit_workflow("run_mode_test_workflow")
    await runtime.shutdown()


async def test_submit_workflow_sim_raises_for_topology_only() -> None:
    """A sim run is refused at its start when a device's orca-client is not connected."""
    system = await _build_system()
    run_by_an_orca_client(*system.resources)
    runtime = await memory_runtime(
        system,
        gateway_registry=NullGatewayRegistry(),
        connection_source=NullDeviceConnectionSource(),
        gateway=build_device_gateway(ServerEvents(), None),
    )
    await runtime.start()
    with pytest.raises(DevicesNotReadyError) as exc:
        await runtime.submit_workflow(
            "run_mode_test_workflow", mode=WorkflowRunMode.SIM,
        )
    assert "orca-client is not connected" in exc.value.devices["shaker1"]
    await runtime.shutdown()


async def test_submit_workflow_live_succeeds_when_every_device_connected() -> None:
    """Inject a connection source that reports every topology device as live."""
    system = await _build_system()
    now = datetime.now(timezone.utc)
    cards = [
        _connection_card("shaker1", last_heartbeat=now),
        _connection_card(
            "robot1",
            interfaces=TRANSPORTER_MOCK_INTERFACES,
            advertised_kind="SimTransporterDriver",
            last_heartbeat=now,
        ),
    ]
    source = FakeConnectionSource(cards, now=now)

    # Build matching gateway entries so the topology x gateway collision
    # validator at start-up does not refuse our otherwise-clean topology.
    # Driver_class_observed must equal the topology's driver_class.
    runtime = await memory_runtime(
        system,
        gateway_registry=NullGatewayRegistry(),
        connection_source=source,
    )
    await runtime.start()

    record = await runtime.submit_workflow(
        "run_mode_test_workflow", mode=WorkflowRunMode.LIVE,
    )
    assert record.workflow_name == "run_mode_test_workflow"
    await runtime.shutdown()


# --- Runtime startup --------------------------------------------------------


async def test_start_with_no_overrides_passes_cleanly() -> None:
    """runtime.start() seeds ContextVar and proceeds."""
    system = await _build_system()
    runtime = await memory_runtime(
        system,
        gateway_registry=NullGatewayRegistry(),
    )
    await runtime.start()
    await runtime.shutdown()


# --- The refusal names every device ------------------------------------------


async def test_the_refusal_names_every_device_and_every_mover() -> None:
    """Nothing is connected. A run brings up every device and every transporter (owner, 2026-11-13), so the arm
    is named though it is on no route the workflow declares."""
    system = await _build_system()
    run_by_an_orca_client(*system.resources)
    runtime = await memory_runtime(
        system,
        gateway_registry=NullGatewayRegistry(),
        connection_source=NullDeviceConnectionSource(),
        gateway=build_device_gateway(ServerEvents(), None),
    )
    await runtime.start()
    with pytest.raises(DevicesNotReadyError) as exc:
        await runtime.submit_workflow(
            "run_mode_test_workflow", mode=WorkflowRunMode.LIVE,
        )
    assert set(exc.value.devices) == {"shaker1", "robot1"}
    await runtime.shutdown()


# --- FakeTopologyRegistry sanity (sanity-check the test imports) ------------


def test_fake_topology_registry_imports_clean() -> None:
    """Sanity-check that the topology fakes are importable from this module.

    The mode tests ride on these fakes; if the import breaks, the rest of
    this file's tests mass-fail with confusing messages. This
    single-function test keeps the import surface stable.
    """
    fake = FakeTopologyRegistry([])
    assert fake.list_devices() == []
