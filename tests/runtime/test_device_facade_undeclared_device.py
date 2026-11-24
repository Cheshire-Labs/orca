"""A device connected by a device bridge but never declared in topology is
still reachable.

`orca device list` merges topology with the gateway, so an operator sees such a
device. Every write verb then resolved it through the topology registry first,
which raises `KeyError`, and the daemon turned that into a 404: the device is
listed and then reported not to exist. A hosted deployment's MCP and REST never had this
problem, because they dispatch through `orca.gateway.operator_commands`, which resolves the
name against the connection tracker.

The earlier facade tests could not see any of this: each one set
`system.get_device.return_value`, so the registry always succeeded.

The other half is what an operator is told when a command fails past the gate.
The operator path raises `DeviceError` subclasses, and the daemon caught only `KeyError`,
`TypeError` and `ValueError`, so an offline device, a locked one, or a timeout
reached the operator as a bare 500.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cheshire_drivers.driver_introspection import MethodInfo

from tests.gateway_parts import gateway_with
from orca.operations.operator_device_models import BatchFailureExtras
from orca.gateway.controller.exceptions import DeviceError, DeviceOfflineError, DeviceUnknownError
from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.operations.protocol import OperationError
from orca.runtime.facades.devices import DeviceFacade
from tests.runtime.operator_calls import recording_runner, send


DEVICE = "flex_undeclared"


def _card() -> DeviceSnapshot:
    return DeviceSnapshot(
        type="FlexLiquidHandlerDriver",
        name=DEVICE,
        interfaces=["ILiquidHandler", "IForceGripperJaw"],
        capabilities=["gripper.ungrip", "gripper.grip"],
        provides_state=True,
        methods={
            "gripper.grip": MethodInfo(
                kind="method",
                params={"force": {"type": "float", "required": False, "default": None}},
                docstring="Close the jaws to a force.",
            ),
        },
        site="test",
        lab="test",
        workcell=None,
        status="ready",
        last_seen=datetime.utcnow(),
    )


@pytest.fixture
def facade() -> DeviceFacade:
    """A runtime whose topology declares nothing.

    `get_device` raises the way `ResourceRegistry` does, rather than handing
    back a mock: a double that answers every name is what hid this.
    """
    system = MagicMock()
    system.get_device.side_effect = KeyError(DEVICE)
    system.get_resource.side_effect = KeyError(DEVICE)
    faults = MagicMock()
    # Explicit: a MagicMock returns a truthy Mock for fault(), which would read
    # every device as faulted.
    faults.fault = MagicMock(return_value=None)
    return DeviceFacade(system, MagicMock(), MagicMock(), gateway_with(controller=faults))


@pytest.fixture(autouse=True)
def connected():
    with patch(
        "orca.gateway.registry.connection_tracker.DeviceConnectionTracker.peek_snapshot",
        return_value=_card(),
    ):
        yield


@pytest.fixture
def dispatch() -> MagicMock:
    return recording_runner()


@pytest.mark.asyncio
async def test_send_reaches_a_device_only_the_agent_knows(facade, dispatch) -> None:
    await send(facade, dispatch, DEVICE, "gripper.ungrip", {})

    assert dispatch.run_named.await_args_list[-1].args[1] == "gripper.ungrip"


def test_capabilities_list_a_device_only_the_agent_knows(facade) -> None:
    """An operator reads the listing to find the name. Raising here is the same
    dead end as refusing the command."""
    listed = {c.capability for c in facade.get_supported_commands(DEVICE)}

    assert "gripper.ungrip" in listed


def test_introspection_answers_for_a_device_only_the_agent_knows(facade) -> None:
    info = facade.get_device_introspection(DEVICE)

    assert "gripper.ungrip" in info.capabilities


@pytest.mark.asyncio
async def test_a_name_neither_side_knows_is_a_missing_device(facade, dispatch) -> None:
    """Not "this device has no such command": the device is the thing that is
    missing, and the daemon turns this into the 404 an operator expects."""
    dispatch.device = AsyncMock(side_effect=DeviceUnknownError("no_such_device"))
    with patch(
        "orca.gateway.registry.connection_tracker.DeviceConnectionTracker.peek_snapshot",
        return_value=None,
    ):
        with pytest.raises(OperationError) as sent:
            await send(facade, dispatch, "no_such_device", "gripper.ungrip", {})
    assert sent.value.http_status == 404
    assert dispatch.run_named.await_count == 0


@pytest.mark.asyncio
async def test_a_gateway_failure_keeps_its_own_error(facade) -> None:
    """`DeviceOfflineError` and its siblings carry what went wrong. Flattening
    them into a generic failure loses the one thing the operator needed."""
    runner = recording_runner(fails_with=DeviceOfflineError("device bridge went away"))
    with pytest.raises(OperationError) as refused:
        await send(facade, runner, DEVICE, "gripper.ungrip", {})
    assert refused.value.surface_code() == "batch_error"
    assert refused.value.extras is not None
    failed = BatchFailureExtras.model_validate(refused.value.extras)
    [ran] = failed.results
    assert ran.error_type == "DeviceOfflineError"
    assert ran.error is not None and "device bridge went away" in ran.error


def test_an_explicit_default_of_none_is_not_no_default(facade) -> None:
    """`force` defaults to None, which IS a default. Rendering it as absent
    makes an optional argument read as required."""
    listed = {c.capability: c for c in facade.get_supported_commands(DEVICE)}

    force = listed["gripper.grip"].params[0]
    assert force.required is False
    assert force.default == "None"


class TestAnInterfaceMethodTheCatalogListsIsSendable:
    """`derive_capabilities` subtracts every interface member, so an @external
    interface method rides the card's `methods` and not its `capabilities`.
    It is still a command the device can be sent by name.
    """

    async def test_an_interface_method_the_catalog_lists_is_sent(
        self, facade: DeviceFacade,
    ) -> None:
        card = _card()
        card = DeviceSnapshot(**{
            **card.model_dump(),
            "methods": {**card.methods, "retract_axis": MethodInfo(
                kind="method",
                params={"axis": {"type": "str", "required": True}},
                docstring="Retract one axis.",
            )},
        })
        with patch(
            "orca.gateway.registry.connection_tracker.DeviceConnectionTracker.peek_snapshot",
            return_value=card,
        ):
            assert "retract_axis" in set(facade.get_device_introspection(DEVICE).methods)
            assert "retract_axis" not in card.capabilities

            sent = recording_runner()
            await send(facade, sent, DEVICE, "retract_axis", {"axis": "y"})

        assert sent.run_named.await_args_list[-1].args[1] == "retract_axis"


class TestAGatewayErrorKeepsItsStatus:
    """A `DeviceError` subclass means what its parent means to an operator.

    The mapping walks the exception's parents rather than looking up its exact
    type, so a subclass added later does not silently become a 502.
    """

    def test_each_mapped_error_carries_its_own_status(self) -> None:
        from orca.gateway.controller.exceptions import (
            CommandTimeoutError,
            ConfirmationRequiredError,
            DeviceLockedError,
            DeviceUnknownError,
        )
        from orca.operations.device_commands import device_error_code

        def status(exc: DeviceError) -> int:
            return device_error_code(exc).http_status

        assert status(DeviceUnknownError("x")) == 404
        assert status(DeviceOfflineError("x")) == 503
        assert status(DeviceLockedError("x")) == 409
        assert status(CommandTimeoutError("x")) == 504
        assert status(ConfirmationRequiredError("x")) == 400

    def test_a_faulted_device_answers_device_faulted(self) -> None:
        from orca.gateway.controller.exceptions import DeviceFaultedError
        from orca.gateway.device_fault import DeviceFault, DeviceFaultOutcome
        from orca.operations.device_commands import device_error_code

        fault = DeviceFault(
            device_id="shaker1", command="shake", command_id="c1",
            outcome=next(iter(DeviceFaultOutcome)), error="x", error_type="X", at=0.0,
        )

        code = device_error_code(DeviceFaultedError("x", fault))
        assert (code, code.http_status) == ("device_faulted", 409)

    def test_an_unreadable_world_answers_runtime_not_ready(self) -> None:
        from orca.gateway.controller.exceptions import ModeUnresolvableError
        from orca.operations.device_commands import device_error_code

        code = device_error_code(ModeUnresolvableError("x"))
        assert (code, code.http_status) == ("runtime_not_ready", 503)

    def test_an_unmapped_subclass_answers_like_its_parent(self) -> None:
        from orca.operations.device_commands import device_error_code

        class StillWarmingUp(DeviceOfflineError):
            pass

        code = device_error_code(StillWarmingUp("x"))
        assert (code, code.http_status) == ("device_offline", 503)
