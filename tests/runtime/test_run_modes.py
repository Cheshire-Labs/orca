"""Workflow run modes and dispatch resolution.

Covers:

- `resolve_effective_mode_for_device`: the 12-row v3.4 lookup combining
  a submission's run_mode with a per-device topology sim_override.
- `runtime.submit_workflow(mode=...)`: dispatches `assert_runnable` via
  the unified DeviceRegistry, raising the right error class for each gap.
"""

from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.registries import NullGatewayRegistry
from orca.runtime.run_modes import (
    ResolvedDeviceMode,
    WorkflowRunMode,
    resolve_effective_mode_for_device,
)
from orca.runtime.runtime_interface import (
    WorkflowDeviceNotConnectedError,
)
from orca.runtime.status_models import (
    ConnectionCard,
    GatewayDeviceEntry,
)
from orca.runtime.system_runtime import SystemRuntime
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


# --- Resolver matrix --------------------------------------------------------


class TestResolveEffectiveModeForDevice:
    """v3.4 12-row table: submission_mode x device_sim_override.

    Per-device overrides only ratchet TOWARD sim relative to submission
    mode. Toward-live overrides are silently inert. Warning fires only
    when going LIVE with a sim-direction override present.
    """

    # PURE_SIM submission row: always resolves to PURE_SIM, no warnings.
    @pytest.mark.parametrize("override", [
        None,
        WorkflowRunMode.PURE_SIM,
        WorkflowRunMode.DEVICE_SIM,
        WorkflowRunMode.LIVE,
    ])
    def test_pure_sim_submission_always_pure_sim_no_warning(
        self, override: WorkflowRunMode | None,
    ) -> None:
        result = resolve_effective_mode_for_device(
            WorkflowRunMode.PURE_SIM, override,
        )
        assert result == ResolvedDeviceMode(WorkflowRunMode.PURE_SIM, None)

    def test_device_sim_submission_no_override(self) -> None:
        result = resolve_effective_mode_for_device(
            WorkflowRunMode.DEVICE_SIM, None,
        )
        assert result == ResolvedDeviceMode(WorkflowRunMode.DEVICE_SIM, None)

    def test_device_sim_submission_pure_sim_override_ratchets_down(self) -> None:
        result = resolve_effective_mode_for_device(
            WorkflowRunMode.DEVICE_SIM, WorkflowRunMode.PURE_SIM,
        )
        assert result == ResolvedDeviceMode(WorkflowRunMode.PURE_SIM, None)

    def test_device_sim_submission_device_sim_override_redundant(self) -> None:
        result = resolve_effective_mode_for_device(
            WorkflowRunMode.DEVICE_SIM, WorkflowRunMode.DEVICE_SIM,
        )
        assert result == ResolvedDeviceMode(WorkflowRunMode.DEVICE_SIM, None)

    def test_device_sim_submission_live_override_silently_inert(self) -> None:
        # Toward-live override has no effect under a sim submission.
        result = resolve_effective_mode_for_device(
            WorkflowRunMode.DEVICE_SIM, WorkflowRunMode.LIVE,
        )
        assert result == ResolvedDeviceMode(WorkflowRunMode.DEVICE_SIM, None)

    def test_live_submission_no_override(self) -> None:
        result = resolve_effective_mode_for_device(
            WorkflowRunMode.LIVE, None,
        )
        assert result == ResolvedDeviceMode(WorkflowRunMode.LIVE, None)

    def test_live_submission_live_override_redundant(self) -> None:
        result = resolve_effective_mode_for_device(
            WorkflowRunMode.LIVE, WorkflowRunMode.LIVE,
        )
        assert result == ResolvedDeviceMode(WorkflowRunMode.LIVE, None)

    def test_live_submission_pure_sim_override_warns(self) -> None:
        result = resolve_effective_mode_for_device(
            WorkflowRunMode.LIVE,
            WorkflowRunMode.PURE_SIM,
            device_name="shaker_1",
        )
        assert result.resolved is WorkflowRunMode.PURE_SIM
        assert result.warning is not None
        assert "shaker_1" in result.warning
        assert "PURE_SIM" in result.warning

    def test_live_submission_device_sim_override_warns(self) -> None:
        result = resolve_effective_mode_for_device(
            WorkflowRunMode.LIVE,
            WorkflowRunMode.DEVICE_SIM,
            device_name="centrifuge_1",
        )
        assert result.resolved is WorkflowRunMode.DEVICE_SIM
        assert result.warning is not None
        assert "centrifuge_1" in result.warning
        assert "DEVICE_SIM" in result.warning


# --- Real-system fixtures ---------------------------------------------------


async def _build_system(
    *,
    extra_devices: dict[str, WorkflowRunMode | None] | None = None,
) -> ISystem:
    """Construct a minimal System with a shaker + transporter for run-mode tests.

    `extra_devices` lets a test add additional Devices to the topology by
    name, optionally forcing a per-device topology sim_override on each.
    The dict value is the override mode (or None for no override).
    """
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    shaker = create_test_device("shaker1")
    registry.add_resource(shaker)
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [shaker])
    registry.add_resource_pool(pool)

    if extra_devices:
        for dev_name, override in extra_devices.items():
            dev = create_test_device(dev_name, sim_override=override)
            registry.add_resource(dev)
            registry.add_resource_pool(ResourcePool(dev_name, [dev]))

    # Extra devices live in the registry but are NOT mounted on the SystemMap;
    # the topology-card build path reads `system.devices` directly, so the
    # registry membership alone is sufficient for run-mode tests. Skipping
    # mounting avoids needing additional Locations on the map for every
    # ad-hoc device a test wants to inject.
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
        event_bus=EventBus(),
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


async def test_submit_workflow_pure_sim_succeeds_with_topology_only() -> None:
    system = await _build_system()
    runtime = SystemRuntime(
        system,
        gateway_registry=NullGatewayRegistry(),
    )
    await runtime.start()
    record = await runtime.submit_workflow("run_mode_test_workflow", mode=WorkflowRunMode.PURE_SIM)
    assert record.workflow_name == "run_mode_test_workflow"
    await runtime.shutdown()


async def test_submit_workflow_requires_run_mode() -> None:
    """v3.4: omitting `mode=` raises `RunModeRequiredError` (no fallback).

    The deployment base mode is no longer a submit-time fallback under
    sim-hierarchy v3.4 -- the operator must declare a mode per submission.
    """
    from orca.runtime.runtime_interface import RunModeRequiredError
    system = await _build_system()
    runtime = SystemRuntime(
        system,
        gateway_registry=NullGatewayRegistry(),
    )
    await runtime.start()
    with pytest.raises(RunModeRequiredError):
        await runtime.submit_workflow("run_mode_test_workflow")
    await runtime.shutdown()


async def test_submit_workflow_device_sim_raises_for_topology_only() -> None:
    """DEVICE_SIM requires every topology-declared device to be connected."""
    system = await _build_system()
    runtime = SystemRuntime(
        system,
        gateway_registry=NullGatewayRegistry(),
    )
    await runtime.start()
    with pytest.raises(WorkflowDeviceNotConnectedError) as exc:
        await runtime.submit_workflow(
            "run_mode_test_workflow", mode=WorkflowRunMode.DEVICE_SIM,
        )
    # Topology has shaker1 + robot1; both should appear since neither has a
    # gateway connection.
    assert "shaker1" in exc.value.missing
    assert "robot1" in exc.value.missing
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
    runtime = SystemRuntime(
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
    """v3.4: runtime.start() seeds ContextVar and proceeds without C1 checks."""
    system = await _build_system()
    runtime = SystemRuntime(
        system,
        gateway_registry=NullGatewayRegistry(),
    )
    await runtime.start()
    await runtime.shutdown()


async def test_start_with_topology_sim_override_proceeds() -> None:
    """v3.4: topology sim_override no longer hard-errors at start.

    The C1 conformance rule was deleted along with `validate_topology_overrides`.
    Per-device overrides are evaluated at submit time via the 12-row
    resolver; start() is purely lifecycle.
    """
    system = await _build_system(extra_devices={"sim_only": WorkflowRunMode.PURE_SIM})
    runtime = SystemRuntime(
        system,
        gateway_registry=NullGatewayRegistry(),
    )
    await runtime.start()
    await runtime.shutdown()


# --- Topology -> TopologyCard sim_override plumbing -------------------------


async def test_device_sim_override_surfaces_on_topology_card() -> None:
    """`Device(sim_override=...)` flows through to `TopologyCard.topology_sim_override`."""
    system = await _build_system(extra_devices={"forced_sim": WorkflowRunMode.PURE_SIM})
    runtime = SystemRuntime(
        system,
        gateway_registry=NullGatewayRegistry(),
    )
    entry = await runtime.device_registry.get("forced_sim")
    assert entry is not None
    assert entry.topology_card is not None
    assert (
        entry.topology_card.topology_sim_override is WorkflowRunMode.PURE_SIM
    )


# --- DeviceRegistry assert_runnable still aggregates ------------------------


async def test_assert_runnable_aggregates_via_runtime() -> None:
    """End-to-end sanity: submit_workflow surfaces every offending device.

    Builds a system with TWO topology devices, neither connected, then calls
    `submit_workflow(mode=DEVICE_SIM)`. The error must list both devices.
    """
    system = await _build_system()
    runtime = SystemRuntime(
        system,
        gateway_registry=NullGatewayRegistry(),
    )
    await runtime.start()
    with pytest.raises(WorkflowDeviceNotConnectedError) as exc:
        await runtime.submit_workflow(
            "run_mode_test_workflow", mode=WorkflowRunMode.LIVE,
        )
    # shaker1 + robot1 both unconnected.
    missing = sorted(exc.value.missing)
    assert "shaker1" in missing
    assert "robot1" in missing
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
