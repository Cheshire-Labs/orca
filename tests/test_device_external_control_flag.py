"""External-control flag on Device + Transporter.

The device integration gateway is the higher-priority surface by
design: it is the troubleshooting path an operator (or AI agent) uses
when Orca is stuck or hardware needs hands-on attention. To prevent
production samples racing against operator recovery commands, the
gateway sets ``device.take_external_control()`` around its commands;
Orca's action-dispatch path raises ``DeviceUnderExternalControlError``
when the flag is set.

These tests pin the flag's contract on the resource side. The
gateway's take/release per command lives in the hosting deployment.

No reason / audit string is recorded here. Reasons live in operations
history + the pause UI; the flag itself is just a bool.
"""

from __future__ import annotations

import asyncio

import pytest

from orca.resource_models.device_error import DeviceUnderExternalControlError
from tests.test_helpers import create_test_device, create_test_transporter


def test_device_flag_starts_clear() -> None:
    device = create_test_device("shaker1")
    assert device.under_external_control is False


def test_device_take_sets_flag() -> None:
    device = create_test_device("shaker1")
    device.take_external_control()
    assert device.under_external_control is True


def test_device_release_clears_flag() -> None:
    device = create_test_device("shaker1")
    device.take_external_control()
    device.release_external_control()
    assert device.under_external_control is False


def test_device_take_is_idempotent() -> None:
    device = create_test_device("shaker1")
    device.take_external_control()
    device.take_external_control()
    device.take_external_control()
    assert device.under_external_control is True


def test_device_release_is_idempotent() -> None:
    device = create_test_device("shaker1")
    # Clearing an already-clear flag is a no-op (mirror of an in-flight
    # gateway command sequence where the release-side path runs in a
    # finally; safe to call multiple times).
    device.release_external_control()
    device.release_external_control()
    assert device.under_external_control is False


def test_transporter_flag_starts_clear() -> None:
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    assert transporter.under_external_control is False


def test_transporter_take_and_release() -> None:
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    transporter.take_external_control()
    assert transporter.under_external_control is True
    transporter.release_external_control()
    assert transporter.under_external_control is False


def test_device_in_use_reflects_external_control() -> None:
    """``in_use`` is the operator-meaningful "this device cannot be
    dispatched against right now" answer. Lock-held OR gateway-held
    both flip it to True. Pre-S5b ``in_use`` was lock-only, which left
    snapshots reporting ``is_busy=False`` for gateway-held devices and
    ``ResourcePool.available_count`` counting them as available --
    inconsistent with ``under_external_control=True``.
    """
    device = create_test_device("shaker1")
    assert device.in_use is False
    device.take_external_control()
    assert device.in_use is True
    device.release_external_control()
    assert device.in_use is False


def test_transporter_in_use_reflects_external_control() -> None:
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    assert transporter.in_use is False
    transporter.take_external_control()
    assert transporter.in_use is True
    transporter.release_external_control()
    assert transporter.in_use is False


def test_device_under_external_control_error_carries_name() -> None:
    """The typed exception carries the device name so the operator-facing
    envelope can point at the right resource. No reason string -- that
    belongs in operations history.
    """
    err = DeviceUnderExternalControlError(device_name="mlstar_1")
    assert err.device_name == "mlstar_1"
    # The base ``DeviceError`` formats ``[name] message`` so the str-repr
    # surfaces the device name without parsing.
    assert "mlstar_1" in str(err)


async def test_registry_list_devices_surfaces_under_external_control() -> None:
    """Round-7 review L1: the C2 fix added
    ``under_external_control=`` to ``RegistryFacade._device_snapshot``,
    but the original test exercised ``DeviceFacade._snapshot`` instead.
    Two snapshot-emitter paths -- if either is missed, the catalog
    list disagrees with the per-device read. This test pins the
    registry-list path specifically so a regression removing the
    field from ``_device_snapshot`` fails loud.
    """
    from orca.resource_models.resource_pool import ResourcePool
    from orca.runtime.system_runtime import SystemRuntime
    from orca.runtime.registries import NullGatewayRegistry
    from orca.sdk.events import EventBus
    from orca.sdk.system import ResourceRegistry, SystemMap
    from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
    from tests.test_helpers import create_test_plate_template, wire_system_map

    device = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    builder = SdkToSystemBuilder(
        name="registry_list_test", description="",
        labwares=[plate], resources_registry=registry,
        system_map=system_map, workflows=[], event_bus=EventBus(),
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(builder.get_system(), gateway_registry=NullGatewayRegistry())

    # Pre-take: registry list reports False.
    pre = next(d for d in runtime.registry.list_devices() if d.name == "shaker1")
    assert pre.under_external_control is False

    device.take_external_control()

    post = next(d for d in runtime.registry.list_devices() if d.name == "shaker1")
    assert post.under_external_control is True


async def test_registry_list_transporters_surfaces_under_external_control() -> None:
    """Sister of the device-list test. Pins
    ``RegistryFacade._transporter_snapshot`` so the C3 fix's
    ``getattr(t, "under_external_control", False)`` is regression-
    protected on the registry-list path (the original test exercised
    the unified DeviceFacade path, not this one).
    """
    from orca.resource_models.resource_pool import ResourcePool
    from orca.runtime.system_runtime import SystemRuntime
    from orca.runtime.registries import NullGatewayRegistry
    from orca.sdk.events import EventBus
    from orca.sdk.system import ResourceRegistry, SystemMap
    from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
    from tests.test_helpers import create_test_plate_template, wire_system_map

    device = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    builder = SdkToSystemBuilder(
        name="registry_list_transporter_test", description="",
        labwares=[plate], resources_registry=registry,
        system_map=system_map, workflows=[], event_bus=EventBus(),
    )
    await builder.bind_labwares()
    runtime = SystemRuntime(builder.get_system(), gateway_registry=NullGatewayRegistry())

    pre = next(t for t in runtime.registry.list_transporters() if t.name == "robot1")
    assert pre.under_external_control is False

    transporter.take_external_control()

    post = next(t for t in runtime.registry.list_transporters() if t.name == "robot1")
    assert post.under_external_control is True


async def test_snapshot_surfaces_under_external_control() -> None:
    """``runtime.devices.get_device_status`` reflects the flag.

    Round-7 reporting depends on the snapshot showing the gateway state
    so MCP clients can render "device 'X' is under control" without
    polling a separate endpoint.
    """
    from orca.resource_models.resource_pool import ResourcePool
    from orca.runtime.registries import NullGatewayRegistry
    from orca.runtime.system_runtime import SystemRuntime
    from orca.sdk.events import EventBus
    from orca.sdk.system import ResourceRegistry, SystemMap
    from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
    from tests.test_helpers import create_test_plate_template, wire_system_map

    device = create_test_device("shaker1")
    transporter = create_test_transporter("robot1", ["shaker1", "pad1"])
    plate = create_test_plate_template("plate_96")

    registry = ResourceRegistry()
    registry.add_resource(device)
    registry.add_resource(transporter)
    pool = ResourcePool("shaker1", [device])
    registry.add_resource_pool(pool)
    system_map = SystemMap(registry)
    await wire_system_map(system_map, devices={"shaker1": device}, pads=["pad1"])

    builder = SdkToSystemBuilder(
        name="ext_ctrl_snap_test", description="",
        labwares=[plate], resources_registry=registry,
        system_map=system_map, workflows=[], event_bus=EventBus(),
    )
    await builder.bind_labwares()
    system = builder.get_system()
    runtime = SystemRuntime(system, gateway_registry=NullGatewayRegistry())

    # Before take: snapshot reports False on both kinds.
    pre_device = runtime.devices.get_device_status("shaker1")
    assert pre_device.under_external_control is False
    pre_transporter = runtime.devices.get_device_status("robot1")
    assert pre_transporter.under_external_control is False

    # Take on both, then re-snapshot.
    device.take_external_control()
    transporter.take_external_control()

    post_device = runtime.devices.get_device_status("shaker1")
    assert post_device.under_external_control is True
    post_transporter = runtime.devices.get_device_status("robot1")
    assert post_transporter.under_external_control is True


# ---------------------------------------------------------------------------
# Action-loop driver tests
#
# The previous version of these tests was circular -- it raised
# ``DeviceUnderExternalControlError`` inline in the test body and asserted
# the type. A regression that deletes the gate from
# ``location_action.execute()`` would not fail that shape.
#
# Round 7 review caught this. These tests now spin a real
# ``ActionBodyLocationAction`` with a recording driver and drive
# ``await action.execute()``, asserting the gate raises before the
# recorded driver is touched.
# ---------------------------------------------------------------------------


from typing import Any

from cheshire_drivers import SimShakerDriver

from orca.devices.shaker import Shaker
from orca.resource_models.devices import Device
from orca.resource_models.location import Location
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.system.reservation_manager.location_reservation import (
    LocationReservation,
)
from orca.variables.variable_store import NullVariableResolver
from orca.workflow_models.actions.location_action import (
    ActionBodyLocationAction,
)
from orca.workflow_models.action_context import ActionContext


class _RecordingShaker(SimShakerDriver):
    """Test-local shaker driver that records every ``shake`` call.

    Mirrors the contract test pattern in
    ``tests/test_action_contracts.py::_RecordingShaker``. We use it here
    to assert the driver was NOT touched when the external-control gate
    fires, which the previous inline-raise test could not prove.
    """

    def __init__(self) -> None:
        super().__init__("recording_shaker")
        self.shake_calls: list[dict[str, Any]] = []

    async def shake(self, request: Any) -> None:  # request: ShakeRequest
        self.shake_calls.append({"speed": request.speed, "duration": request.duration})


class _RecordingShakerFactory:
    """Inject a specific shaker driver via the no-driver Device ctor."""

    def __init__(self, driver: _RecordingShaker) -> None:
        self._d = driver

    def build_drivers(
        self, device_type: str, name: str,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        del device_type, name
        return self._d, self._d


def _wire_action_to_device(action: ActionBodyLocationAction, device: Device) -> None:
    """Wire an ActionBodyLocationAction to a Device via a minimal
    reservation chain. Verbatim copy of the helper in
    ``tests/test_action_contracts.py`` so failures here line up with
    failures there.
    """
    location = Location("test_location", resource=device)
    reservation = LocationReservation(requested_location=location)
    reservation.set_location(location)
    action.set_location_reservation(reservation)
    action.set_device(device)


@pytest.mark.asyncio
async def test_action_loop_refuses_when_under_external_control() -> None:
    """Drive a real ``ActionBodyLocationAction.execute()`` against a
    device whose external-control flag is set. Gate must raise
    ``DeviceUnderExternalControlError`` BEFORE the driver method is
    invoked.

    Round-7 review L1: the previous test for this gate was
    circular (raised the typed exception inline in the test body and
    asserted on it). A regression that deletes the gate from
    ``location_action.py`` would have passed that shape. This test
    drives the production action loop and asserts the recording driver
    never sees the ``shake`` call.
    """
    driver = _RecordingShaker()
    with use_device_factory(_RecordingShakerFactory(driver)):
        shaker = Shaker("test_shaker")

    shaker.take_external_control()

    async def user_func(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=30, speed=500)

    action = ActionBodyLocationAction(func=user_func, command="shake")
    action.set_execution_context(variable_store=NullVariableResolver(), execution_id="t", thread_id="t-thread")
    _wire_action_to_device(action, shaker)

    with pytest.raises(DeviceUnderExternalControlError) as excinfo:
        await action.execute()

    assert excinfo.value.device_name == "test_shaker"
    # Driver method must NOT have been invoked. The gate fires before the
    # ``async with device.lock:`` block that would call ``method(...)``.
    assert driver.shake_calls == [], (
        f"driver was invoked despite the gate: {driver.shake_calls!r}"
    )


@pytest.mark.asyncio
async def test_action_loop_proceeds_after_release() -> None:
    """Release clears the flag; subsequent ``action.execute()`` proceeds
    and the driver IS invoked. Counterpart to the refuse-test."""
    driver = _RecordingShaker()
    with use_device_factory(_RecordingShakerFactory(driver)):
        shaker = Shaker("test_shaker")

    shaker.take_external_control()
    shaker.release_external_control()

    async def user_func(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=30, speed=500)

    action = ActionBodyLocationAction(func=user_func, command="shake")
    action.set_execution_context(variable_store=NullVariableResolver(), execution_id="t", thread_id="t-thread")
    _wire_action_to_device(action, shaker)

    await action.execute()  # no raise

    assert len(driver.shake_calls) == 1, driver.shake_calls
    assert driver.shake_calls[0]["speed"] == 500
    assert driver.shake_calls[0]["duration"] == 30


@pytest.mark.asyncio
async def test_action_loop_gate_does_not_leak_user_task() -> None:
    """When the gate raises, the user coroutine must wake with the same
    exception (via ``request.error`` + ``request.completion.set()``)
    rather than hanging on ``await action.completion.wait()``.

    Review item L2: without setting completion before raising, the
    queue loop's exception propagates while the user task is still
    awaiting the completion event -- the ``try/finally`` at the bottom
    of ``execute()`` then blocks on ``await self._user_task`` forever.
    The ``asyncio.wait_for`` deadline catches the hang as a test
    failure rather than letting it run forever.
    """
    driver = _RecordingShaker()
    with use_device_factory(_RecordingShakerFactory(driver)):
        shaker = Shaker("test_shaker")

    shaker.take_external_control()

    async def user_func(ctx: ActionContext) -> None:
        await ctx.device().shake(duration=30, speed=500)

    action = ActionBodyLocationAction(func=user_func, command="shake")
    action.set_execution_context(variable_store=NullVariableResolver(), execution_id="t", thread_id="t-thread")
    _wire_action_to_device(action, shaker)

    # A non-hanging path completes in milliseconds; a 2-second deadline
    # makes a hang fail loud (asyncio.TimeoutError) rather than block
    # the suite.
    with pytest.raises(DeviceUnderExternalControlError):
        await asyncio.wait_for(action.execute(), timeout=2.0)


# ---------------------------------------------------------------------------
# Found in review: the post-loop ``finally`` cancels
# the user task to unwedge it from ``request.completion.wait()`` when the
# queue loop exits via raise. But if the raise happens BEFORE the per-
# request ``request.completion.set()`` runs, the user task wakes with
# ``CancelledError`` -- not the driver's exception. ``except Exception``
# in the @orca.method body does not catch ``CancelledError`` (it derives
# from ``BaseException`` on 3.8+), so authors cannot recover from device
# failures in code. The gate paths already set ``error`` + completion
# before raising; the wider class (method-not-found AttributeError and
# the ``await method(...)`` raise) did not. These tests pin both paths.
# ---------------------------------------------------------------------------


class _DriverRaisesShaker(SimShakerDriver):
    """Driver whose ``shake`` always raises a recognizable exception."""

    def __init__(self) -> None:
        super().__init__("raising_shaker")

    async def shake(self, request: Any) -> None:
        del request
        raise RuntimeError("driver shake failed")


class _DriverRaisesShakerFactory:
    def __init__(self, driver: _DriverRaisesShaker) -> None:
        self._d = driver

    def build_drivers(
        self, device_type: str, name: str,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        del device_type, name
        return self._d, self._d


@pytest.mark.asyncio
async def test_action_loop_method_raise_surfaces_in_user_task() -> None:
    """When the driver method raises inside ``await method(...)``, the
    user coroutine awaiting ``ctx.device().shake(...)`` must observe the
    original exception via ``request.error``, not ``CancelledError`` from
    the post-loop cancel.

    Without the fix the queue loop's raise skipped ``request.completion.set()``
    and ``request.error = ...``; the ``finally`` then cancelled the user
    task, so user code saw ``CancelledError`` and ``except Exception``
    failed to catch it.
    """
    driver = _DriverRaisesShaker()
    with use_device_factory(_DriverRaisesShakerFactory(driver)):
        shaker = Shaker("test_shaker")

    observed: dict[str, BaseException | None] = {"exc": None}

    async def user_func(ctx: ActionContext) -> None:
        try:
            await ctx.device().shake(duration=30, speed=500)
        except Exception as e:
            observed["exc"] = e
            raise

    action = ActionBodyLocationAction(func=user_func, command="shake")
    action.set_execution_context(variable_store=NullVariableResolver(), execution_id="t", thread_id="t-thread")
    _wire_action_to_device(action, shaker)

    with pytest.raises(RuntimeError, match="driver shake failed"):
        await asyncio.wait_for(action.execute(), timeout=2.0)

    assert isinstance(observed["exc"], RuntimeError), (
        f"user task saw {type(observed['exc']).__name__} -- expected RuntimeError "
        f"from the driver. CancelledError here means the orphan-on-raise bug is back."
    )
    assert "driver shake failed" in str(observed["exc"])


@pytest.mark.asyncio
async def test_action_loop_unknown_method_surfaces_in_user_task() -> None:
    """Method-not-found raises ``AttributeError`` inside the queue loop
    BEFORE the lock acquire. Same orphan class as the driver-raise path:
    without the fix, the raise skipped ``request.completion.set()`` and
    the user task saw ``CancelledError`` instead of ``AttributeError``.
    """
    driver = _RecordingShaker()
    with use_device_factory(_RecordingShakerFactory(driver)):
        shaker = Shaker("test_shaker")

    observed: dict[str, BaseException | None] = {"exc": None}

    async def user_func(ctx: ActionContext) -> None:
        try:
            await ctx.device().not_a_real_method()
        except Exception as e:
            observed["exc"] = e
            raise

    action = ActionBodyLocationAction(func=user_func, command="not_a_real_method")
    action.set_execution_context(variable_store=NullVariableResolver(), execution_id="t", thread_id="t-thread")
    _wire_action_to_device(action, shaker)

    with pytest.raises(AttributeError, match="not_a_real_method"):
        await asyncio.wait_for(action.execute(), timeout=2.0)

    assert isinstance(observed["exc"], AttributeError), (
        f"user task saw {type(observed['exc']).__name__} -- expected AttributeError. "
        f"CancelledError here means the method-not-found path is orphaning the user task."
    )
    assert "not_a_real_method" in str(observed["exc"])
