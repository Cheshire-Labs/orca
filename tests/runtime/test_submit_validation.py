"""Submit-time validation gates for run-mode resolution and lazy device
initialization.

The route-level envelope shape is tested in
`tests/daemon/test_routes_submit_envelope.py`; what each device runs as and
`RunModeRequiredError` are tested in `tests/runtime/test_run_modes.py`;
`RunModeMismatchError` (which replaces the v3.4
`ConcurrentSubmissionRefusedError` refuse-all) and the lazy
first-thread-touch seam are tested in
`tests/runtime/test_lazy_init_and_resolver.py`. This file covers the
remaining runtime-layer surface:

- A LIVE submission runs whatever each device is set to, with no confirmation
  for devices set to sim. The tests stand up a connected
  `FakeConnectionSource` so LIVE submissions pass `unreachable`.
"""

from orca.config import OrcaConfig
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from tests.no_devices import NullGatewayRegistry
from orca.modes.device_modes import DeviceMode
from orca.modes.run_modes import WorkflowRunMode
from orca.runtime.errors import (
    RunModeMismatchError,
)
from orca.runtime.status_models import ConnectionCard
from orca.sdk.events import EventBus
from orca.sdk.system import ResourceRegistry, SystemMap
from orca.sdk.workflow import MethodTemplate, WorkflowTemplate
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_interface import ISystem
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from tests.mock import TRANSPORTER_MOCK_INTERFACES, UNIVERSAL_MOCK_INTERFACES
from tests.runtime.registries.test_device_registry import FakeConnectionSource
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


# -- Class-shape tests (cheap, no fixture) ----------------------------------


class TestRunModeMismatchErrorShape:
    """`RunModeMismatchError` carries the four typed fields the route helper
    reads to populate `extras.*`. Replaces the deleted
    `ConcurrentSubmissionRefusedError` shape contract: the refuse-all refusal
    became a per-execution mismatch check. A refactor that drops or renames
    any field silently breaks the wire envelope and/or the route's
    exception-ladder dispatch.
    """

    def test_carries_typed_fields(self) -> None:
        exc = RunModeMismatchError(
            blocking_execution_id="exec-abc",
            blocking_workflow_name="sample_workflow",
            existing_run_mode=WorkflowRunMode.CHECK_RUN,
            submitted_run_mode=WorkflowRunMode.LIVE,
        )
        assert exc.blocking_execution_id == "exec-abc"
        assert exc.blocking_workflow_name == "sample_workflow"
        assert exc.existing_run_mode is WorkflowRunMode.CHECK_RUN
        assert exc.submitted_run_mode is WorkflowRunMode.LIVE

    def test_message_mentions_identifiers(self) -> None:
        exc = RunModeMismatchError(
            blocking_execution_id="exec-abc",
            blocking_workflow_name="sample_workflow",
            existing_run_mode=WorkflowRunMode.CHECK_RUN,
            submitted_run_mode=WorkflowRunMode.LIVE,
        )
        msg = str(exc)
        assert "exec-abc" in msg
        assert "sample_workflow" in msg
        assert "CHECK_RUN" in msg
        assert "LIVE" in msg

    def test_inherits_runtime_error(self) -> None:
        """Inheritance gates the submit Operation's exception ladder:
        it catches the typed class BEFORE the generic `RuntimeError`
        catch. A base-class change would
        flip 409 to a different status without anything else surfacing
        the regression."""
        exc = RunModeMismatchError(
            blocking_execution_id="x",
            blocking_workflow_name="y",
            existing_run_mode=WorkflowRunMode.CHECK_RUN,
            submitted_run_mode=WorkflowRunMode.LIVE,
        )
        assert isinstance(exc, RuntimeError)


# -- Runtime gate fixtures --------------------------------------------------


async def _build_system_with_a_device_set_to(name: str, setting: DeviceMode) -> ISystem:
    """A system whose workflow shakes on one device with `setting`."""
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    device = create_test_device(name, setting=setting)
    registry.add_resource(device)
    transporter = create_test_transporter("robot1", [name, "pad1"])
    registry.add_resource(transporter)
    pool = ResourcePool(name, [device])
    registry.add_resource_pool(pool)

    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={name: device}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate])
    async def shake_action(ctx: ActionContext) -> None:
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

    workflow = WorkflowTemplate("override_workflow")
    workflow.add_thread(plate_thread, is_start=True)

    builder = SdkToSystemBuilder(
        name="override_system",
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


def _connection_card(
    name: str,
    *,
    interfaces: frozenset[str] = UNIVERSAL_MOCK_INTERFACES,
    advertised_kind: str = "UniversalMockDevice",
) -> ConnectionCard:
    return ConnectionCard(
        name=name,
        client_id=f"client-{name}",
        connection_id=f"conn-{name}",
        last_heartbeat=datetime.now(timezone.utc),
        advertised_kind=advertised_kind,
        advertised_interfaces=interfaces,
    )


def _live_connection_source() -> FakeConnectionSource:
    """Connection source reporting shaker1 + robot1 as live-connected.
    Without it a LIVE submission is refused with `DevicesNotReadyError`."""
    now = datetime.now(timezone.utc)
    cards = [
        _connection_card("shaker1"),
        _connection_card(
            "robot1",
            interfaces=TRANSPORTER_MOCK_INTERFACES,
            advertised_kind="SimTransporterDriver",
        ),
    ]
    return FakeConnectionSource(cards, now=now)


async def test_a_live_run_with_a_device_set_to_sim_needs_no_confirmation() -> None:
    system = await _build_system_with_a_device_set_to("shaker1", DeviceMode.SIM)
    runtime = await memory_runtime(
        system,
        gateway_registry=NullGatewayRegistry(),
        connection_source=_live_connection_source(),
    )
    await runtime.start()
    try:
        record = await runtime.submit_workflow("override_workflow", mode=WorkflowRunMode.LIVE)
        assert record.workflow_name == "override_workflow"
    finally:
        await runtime.shutdown()


async def test_runtime_accepts_live_when_every_device_is_set_to_live() -> None:
    plate = create_test_plate_template("plate_96")
    registry = ResourceRegistry()
    shaker = create_test_device("shaker1", setting=DeviceMode.LIVE)
    registry.add_resource(shaker)
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [shaker])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": shaker}, pads=["pad1"])

    @orca.action(device=pool, inputs=[plate])
    async def shake_action(ctx: ActionContext) -> None:
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

    workflow = WorkflowTemplate("no_override_workflow")
    workflow.add_thread(plate_thread, is_start=True)

    builder = SdkToSystemBuilder(
        name="no_override_system",
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
    system = builder.get_system()

    runtime = await memory_runtime(
        system,
        gateway_registry=NullGatewayRegistry(),
        connection_source=_live_connection_source(),
    )
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            "no_override_workflow", mode=WorkflowRunMode.LIVE,
        )
        assert record.workflow_name == "no_override_workflow"
    finally:
        await runtime.shutdown()
