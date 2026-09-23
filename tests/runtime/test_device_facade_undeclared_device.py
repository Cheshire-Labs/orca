"""A device connected by a device bridge but never declared in topology is
still reachable.

`orca device list` merges topology with the gateway, so an operator sees such a
device. Every write verb then resolved it through the topology registry first,
which raises `KeyError`, and the daemon turned that into a 404: the device is
listed and then reported not to exist. A hosted deployment's MCP and REST never had this
problem, because they dispatch through `orca.gateway.adhoc`, which resolves the
name against the connection tracker.

The earlier facade tests could not see any of this: each one set
`system.get_device.return_value`, so the registry always succeeded.

The other half is what an operator is told when a command fails past the gate.
`adhoc` raises `DeviceError` subclasses, and the daemon caught only `KeyError`,
`TypeError` and `ValueError`, so an offline device, a locked one, or a timeout
reached the operator as a bare 500.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cheshire_drivers.driver_introspection import MethodInfo

from orca.gateway.adhoc import AdhocResult
from orca.gateway.controller.exceptions import DeviceOfflineError
from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.runtime.facades.devices import DeviceFacade


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
    return DeviceFacade(
        system, MagicMock(), MagicMock(), MagicMock(), MagicMock(), faults,
    )


@pytest.fixture(autouse=True)
def connected():
    with patch(
        "orca.runtime.facades.devices.device_connection_tracker.peek_snapshot",
        return_value=_card(),
    ):
        yield


@pytest.fixture
def dispatch():
    with patch(
        "orca.runtime.facades.devices.adhoc.execute_adhoc_command",
        new=AsyncMock(return_value=AdhocResult(raw=None, interfaces=frozenset())),
    ) as sent:
        yield sent


@pytest.mark.asyncio
async def test_send_reaches_a_device_only_the_agent_knows(facade, dispatch) -> None:
    await facade.execute(DEVICE, "gripper.ungrip", {}, confirm=True)

    assert dispatch.await_args.kwargs["command"] == "gripper.ungrip"


@pytest.mark.asyncio
async def test_invoke_reaches_a_device_only_the_agent_knows(facade, dispatch) -> None:
    await facade.invoke(DEVICE, "gripper.ungrip", {}, confirm=True)

    assert dispatch.await_args.kwargs["command"] == "gripper.ungrip"


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
    with patch(
        "orca.runtime.facades.devices.device_connection_tracker.peek_snapshot",
        return_value=None,
    ):
        with pytest.raises(KeyError):
            await facade.execute("no_such_device", "gripper.ungrip", {}, confirm=True)
        with pytest.raises(KeyError):
            await facade.invoke("no_such_device", "gripper.ungrip", {}, confirm=True)
    assert dispatch.await_count == 0


@pytest.mark.asyncio
async def test_a_gateway_failure_keeps_its_own_error(facade) -> None:
    """`DeviceOfflineError` and its siblings carry what went wrong. Flattening
    them into a generic failure loses the one thing the operator needed."""
    with patch(
        "orca.runtime.facades.devices.adhoc.execute_adhoc_command",
        new=AsyncMock(side_effect=DeviceOfflineError("device bridge went away")),
    ):
        with pytest.raises(DeviceOfflineError):
            await facade.execute(DEVICE, "gripper.ungrip", {}, confirm=True)


def test_an_explicit_default_of_none_is_not_no_default(facade) -> None:
    """`force` defaults to None, which IS a default. Rendering it as absent
    makes an optional argument read as required."""
    listed = {c.capability: c for c in facade.get_supported_commands(DEVICE)}

    force = listed["gripper.grip"].params[0]
    assert force.required is False
    assert force.default == "None"


class TestTheCatalogAndTheGateAgree:
    """`device capabilities` must not list a command `invoke` refuses.

    `derive_capabilities` subtracts every interface member, so an @external
    interface method rides the card's `methods` and not its `capabilities`.
    With no orca-side resource to read interface methods off, the card's
    catalog is the only thing that can be the allow-list.
    """

    async def _card_with_an_interface_method(self) -> DeviceSnapshot:
        card = _card()
        return DeviceSnapshot(
            **{
                **card.model_dump(),
                "methods": {**card.methods, "retract_axis": MethodInfo(
                    kind="method",
                    params={"axis": {"type": "str", "required": True}},
                    docstring="Retract one axis.",
                )},
            }
        )

    async def test_an_interface_method_the_catalog_lists_is_invokable(
        self, facade: DeviceFacade,
    ) -> None:
        card = await self._card_with_an_interface_method()
        with patch(
            "orca.runtime.facades.devices.device_connection_tracker.peek_snapshot",
            return_value=card,
        ):
            listed = set(facade.get_device_introspection(DEVICE).methods)
            assert "retract_axis" in listed
            assert "retract_axis" not in card.capabilities

            with patch(
                "orca.runtime.facades.devices.adhoc.execute_adhoc_command",
                new=AsyncMock(
                    return_value=AdhocResult(raw=None, interfaces=frozenset()),
                ),
            ) as sent:
                await facade.invoke(
                    DEVICE, "retract_axis", {"axis": "y"}, confirm=True,
                )

        assert sent.await_args.kwargs["command"] == "retract_axis"

    async def test_a_command_the_catalog_never_listed_is_still_refused(
        self, facade: DeviceFacade,
    ) -> None:
        with pytest.raises(ValueError, match="no invokable capability"):
            await facade.invoke(DEVICE, "self_destruct", {}, confirm=True)


class TestAGatewayErrorKeepsItsStatus:
    """A `DeviceError` subclass means what its parent means to an operator.

    The route walks the exception's parents rather than looking up its exact
    type, so a subclass added later does not silently become a 502, and the
    two that already exist answer 409 rather than falling through.
    """

    def test_each_mapped_error_carries_its_own_status(self) -> None:
        from orca.daemon.routes import _gateway_http_error
        from orca.gateway.controller.exceptions import (
            CommandTimeoutError,
            ConfirmationRequiredError,
            DeviceLockedError,
            DeviceUnknownError,
        )

        assert _gateway_http_error(DeviceUnknownError("x")).status_code == 404
        assert _gateway_http_error(DeviceOfflineError("x")).status_code == 503
        assert _gateway_http_error(DeviceLockedError("x")).status_code == 409
        assert _gateway_http_error(CommandTimeoutError("x")).status_code == 504
        assert _gateway_http_error(ConfirmationRequiredError("x")).status_code == 409

    def test_an_unmapped_subclass_answers_like_its_parent(self) -> None:
        from orca.daemon.routes import _gateway_http_error

        class StillWarmingUp(DeviceOfflineError):
            pass

        assert _gateway_http_error(StillWarmingUp("x")).status_code == 503
