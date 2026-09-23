"""Submit-time validation gates for run-mode resolution and lazy device
initialization.

The route-level envelope shape is tested in
`tests/daemon/test_routes_submit_envelope.py`; the 12-row resolver and
`RunModeRequiredError` are tested in `tests/runtime/test_run_modes.py`;
`RunModeMismatchError` (which replaces the v3.4
`ConcurrentSubmissionRefusedError` refuse-all) and the lazy
first-thread-touch seam are tested in
`tests/runtime/test_lazy_init_and_resolver.py`. This file covers the
remaining runtime-layer surface:

- `LiveSubmissionWithSimOverridesUnacknowledgedError`: class shape + the
  runtime gate that raises it for a LIVE submission whose topology has a
  sim-direction `sim_override` device + the `acknowledge_warnings=True`
  bypass.

These tests cover behavior the route-level envelope tests cannot exercise
(the route's `assert_runnable` gate for LIVE submissions rejects on
disconnected devices before the typed envelope can fire; the runtime
tests below stand up a connected `FakeConnectionSource` so LIVE
submissions reach the typed-error path).
"""

from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest

import orca.orca as orca
from orca.resource_models.resource_pool import ResourcePool
from orca.runtime.registries import NullGatewayRegistry
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.runtime_interface import (
    LiveSubmissionWithSimOverridesUnacknowledgedError,
    RunModeMismatchError,
)
from orca.runtime.status_models import ConnectionCard
from orca.runtime.system_runtime import SystemRuntime
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
            existing_run_mode=WorkflowRunMode.PURE_SIM,
            submitted_run_mode=WorkflowRunMode.LIVE,
        )
        assert exc.blocking_execution_id == "exec-abc"
        assert exc.blocking_workflow_name == "sample_workflow"
        assert exc.existing_run_mode is WorkflowRunMode.PURE_SIM
        assert exc.submitted_run_mode is WorkflowRunMode.LIVE

    def test_message_mentions_identifiers(self) -> None:
        exc = RunModeMismatchError(
            blocking_execution_id="exec-abc",
            blocking_workflow_name="sample_workflow",
            existing_run_mode=WorkflowRunMode.PURE_SIM,
            submitted_run_mode=WorkflowRunMode.LIVE,
        )
        msg = str(exc)
        assert "exec-abc" in msg
        assert "sample_workflow" in msg
        assert "PURE_SIM" in msg
        assert "LIVE" in msg

    def test_inherits_runtime_error(self) -> None:
        """Inheritance gates the daemon route handler's exception ladder:
        the route catches the typed class BEFORE the generic `RuntimeError`
        catch (`daemon/routes.py:425-429`). A base-class change would
        flip 409 to a different status without anything else surfacing
        the regression."""
        exc = RunModeMismatchError(
            blocking_execution_id="x",
            blocking_workflow_name="y",
            existing_run_mode=WorkflowRunMode.PURE_SIM,
            submitted_run_mode=WorkflowRunMode.LIVE,
        )
        assert isinstance(exc, RuntimeError)


class TestLiveSubmissionWithSimOverridesUnacknowledgedErrorShape:
    """`LiveSubmissionWithSimOverridesUnacknowledgedError` carries the
    `devices` tuple-list the route helper enumerates into
    `extras.devices[]`. The tuple shape `(name, override, resolved)` is
    load-bearing because the helper assumes positional unpacking."""

    # `devices` populated-from-real-validation is covered by the runtime gate
    # test `test_runtime_refuses_live_when_sim_direction_override_unacknowledged`.

    def test_message_mentions_devices(self) -> None:
        exc = LiveSubmissionWithSimOverridesUnacknowledgedError(
            devices=[
                ("shaker_1", WorkflowRunMode.PURE_SIM, WorkflowRunMode.PURE_SIM),
            ],
        )
        msg = str(exc)
        assert "shaker_1" in msg

    def test_inherits_value_error(self) -> None:
        """`ValueError` base flips the route handler to 422 (not 409 or
        500). An earlier `RuntimeError` base bypassed the typed-envelope
        ladder; this test pins the contract."""
        exc = LiveSubmissionWithSimOverridesUnacknowledgedError(devices=[])
        assert isinstance(exc, ValueError)


# -- Runtime gate fixtures --------------------------------------------------


async def _build_system_with_sim_override(
    override_device: tuple[str, WorkflowRunMode],
) -> ISystem:
    """Build a one-device-with-sim_override system for LIVE-gate testing.

    The override device is added as the workflow's shake-action target
    so the topology gate finds it during submit. Returns the System;
    caller wraps in `SystemRuntime` with a `_StubGateway` reporting the
    device as connected.
    """
    name, override = override_device
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    device = create_test_device(name, sim_override=override)
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
        event_bus=EventBus(),
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
    The LIVE-branch of `assert_runnable` consults this; without it,
    LIVE submissions short-circuit on `WorkflowDeviceNotConnectedError`
    before reaching the v3.4 LIVE-with-sim-overrides gate."""
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


# -- LiveSubmissionWithSimOverridesUnacknowledgedError gate behavior --------


async def test_runtime_refuses_live_when_sim_direction_override_unacknowledged() -> None:
    """A LIVE submission against a system with a sim-direction
    `sim_override` device raises the typed error carrying the offending
    device list. Requires a connected gateway so `assert_runnable`
    passes and the v3.4 LIVE-with-sim-overrides gate is reached."""
    system = await _build_system_with_sim_override(("shaker1", WorkflowRunMode.PURE_SIM))
    runtime = SystemRuntime(
        system,
        gateway_registry=NullGatewayRegistry(),
        connection_source=_live_connection_source(),
    )
    await runtime.start()
    try:
        with pytest.raises(LiveSubmissionWithSimOverridesUnacknowledgedError) as exc_info:
            await runtime.submit_workflow(
                "override_workflow", mode=WorkflowRunMode.LIVE,
            )
        err = exc_info.value
        assert err.devices == [
            ("shaker1", WorkflowRunMode.PURE_SIM, WorkflowRunMode.PURE_SIM),
        ]
    finally:
        await runtime.shutdown()


async def test_runtime_accepts_live_when_acknowledge_warnings_true() -> None:
    """`acknowledge_warnings=True` bypasses the LIVE-with-sim-overrides
    gate. The submission proceeds; the operator has accepted the
    topology drift."""
    system = await _build_system_with_sim_override(("shaker1", WorkflowRunMode.PURE_SIM))
    runtime = SystemRuntime(
        system,
        gateway_registry=NullGatewayRegistry(),
        connection_source=_live_connection_source(),
    )
    await runtime.start()
    try:
        record = await runtime.submit_workflow(
            "override_workflow",
            mode=WorkflowRunMode.LIVE,
            acknowledge_warnings=True,
        )
        assert record.workflow_name == "override_workflow"
    finally:
        await runtime.shutdown()


async def test_runtime_accepts_live_with_no_sim_override_devices() -> None:
    """A LIVE submission against a system with NO sim-direction
    sim_overrides should NOT raise. The gate's warning-only filter
    must skip when there's nothing to warn about."""
    # Build a system where the device has NO sim_override.
    plate = create_test_plate_template("plate_96")
    registry = ResourceRegistry()
    shaker = create_test_device("shaker1", sim_override=None)
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
        event_bus=EventBus(),
    )
    await builder.bind_labwares()
    system = builder.get_system()

    runtime = SystemRuntime(
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
