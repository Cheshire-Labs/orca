"""The CLI and the daemon REST reach the same commands MCP does.

A connected driver advertises its vendor commands at the handshake, and the
gateway's capability gate accepts them, so a hosted deployment's MCP and REST dispatch them.
The orca facade behind `orca device ...` and the daemon routes read a different
source: the orca-side interface bridge. Anything the driver forwards to a vendor
object (a PF400 controller command, the Flex gripper's jaws) was therefore
listed nowhere, refused by `device invoke`, and refused by `device send` unless
the device happened to implement IGenericExecutable, which only a VENUS does.

`invoke` also stripped everything up to the last dot before dispatching, so a
prefixed vendor name (`gripper.ungrip`) would have reached for `ungrip` on the
wrong object.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cheshire_drivers.driver_introspection import MethodInfo

from orca.gateway.adhoc import AdhocResult
from orca.gateway.registry.snapshot import DeviceSnapshot
from orca.runtime.facades.devices import DeviceFacade


DEVICE = "flex_1"


def _snapshot() -> DeviceSnapshot:
    """What a connected Flex advertises: interface verbs plus vendor extras."""
    return DeviceSnapshot(
        type="FlexLiquidHandlerDriver",
        name=DEVICE,
        interfaces=["ILiquidHandler", "IForceGripperJaw"],
        capabilities=["retract_axis", "gripper.ungrip", "gripper.grip"],
        provides_state=True,
        methods={
            "gripper.ungrip": MethodInfo(
                kind="method",
                params={},
                returns="None",
                docstring="Release a labware the jaws are holding.",
            ),
            "gripper.grip": MethodInfo(
                kind="method",
                params={"force": {"type": "float", "required": False, "default": None}},
                returns="None",
                docstring="Close the jaws to a force.",
            ),
            "retract_axis": MethodInfo(
                kind="method",
                params={"axis": {"type": "str", "required": True}},
                returns="None",
                docstring="Retract one axis to its home position.",
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
    system = MagicMock()
    # Specced so an unknown attribute raises: a remote device's orca-side
    # resource carries no vendor method, and the only way to reach one is the
    # gateway controller.
    device = MagicMock(spec=["under_external_control", "name"])
    device.under_external_control = False
    device.name = DEVICE
    system.get_device.return_value = device
    faults = MagicMock()
    # Explicit: a MagicMock returns a truthy Mock for fault(), which would read
    # every device as faulted in the union view.
    faults.fault = MagicMock(return_value=None)
    return DeviceFacade(
        system, MagicMock(), MagicMock(), MagicMock(), MagicMock(), faults,
    )


@pytest.fixture(autouse=True)
def connected():
    """The device is registered by an orca-client connection."""
    with patch(
        "orca.runtime.facades.devices.device_connection_tracker.peek_snapshot",
        return_value=_snapshot(),
    ):
        yield


def test_a_vendor_command_is_listed_by_device_capabilities(facade: DeviceFacade) -> None:
    """`orca device capabilities` is where an operator finds the name to send.
    A command missing here reads as a command that does not exist."""
    listed = {c.capability for c in facade.get_supported_commands(DEVICE)}

    assert "gripper.ungrip" in listed
    assert "retract_axis" in listed


def test_a_listed_vendor_command_carries_its_signature(facade: DeviceFacade) -> None:
    """The catalog's params come from the handshake, the only place a forwarded
    command's signature exists on this side of the wire."""
    listed = {c.capability: c for c in facade.get_supported_commands(DEVICE)}

    grip = listed["gripper.grip"]
    assert [p.name for p in grip.params] == ["force"]
    assert grip.description == "Close the jaws to a force."


@pytest.mark.asyncio
async def test_invoke_sends_a_prefixed_vendor_command_whole(facade: DeviceFacade) -> None:
    """The prefix names the object the command lives on. Stripping it sends
    `ungrip` to the robot, which has no such command."""
    with patch(
        "orca.runtime.facades.devices.adhoc.execute_adhoc_command",
        new=AsyncMock(return_value=AdhocResult(raw=None, interfaces=frozenset())),
    ) as dispatch:
        await facade.invoke(DEVICE, "gripper.ungrip", {}, confirm=True)

    assert dispatch.await_args.kwargs["command"] == "gripper.ungrip"


@pytest.mark.asyncio
async def test_invoke_refuses_a_command_the_device_never_advertised(
    facade: DeviceFacade,
) -> None:
    """The advertised set is the gate. Anything else is a typo or a command for
    a different device, and dispatching it would reach the wire to find out."""
    with pytest.raises(ValueError, match="ungrip_everything"):
        await facade.invoke(DEVICE, "gripper.ungrip_everything", {}, confirm=True)


@pytest.mark.asyncio
async def test_send_reaches_a_device_that_is_not_generically_executable(
    facade: DeviceFacade,
) -> None:
    """`orca device send` refused every device but a VENUS, because it required
    an orca-side interface only VENUS implements. A connected device's command
    goes to the gateway like any other."""
    with patch(
        "orca.runtime.facades.devices.adhoc.execute_adhoc_command",
        new=AsyncMock(return_value=AdhocResult(raw=None, interfaces=frozenset())),
    ) as dispatch:
        await facade.execute(DEVICE, "retract_axis", {"axis": "z"}, confirm=True)

    assert dispatch.await_args.kwargs["command"] == "retract_axis"
    assert dispatch.await_args.kwargs["params"] == {"axis": "z"}


def test_introspection_reports_what_the_connected_driver_advertises(
    facade: DeviceFacade,
) -> None:
    """Reading the remote proxy class describes the proxy, not the driver on the
    bench: it lists the proxy's own plumbing and none of the vendor surface."""
    info = facade.get_device_introspection(DEVICE)

    assert "gripper.ungrip" in info.capabilities
    assert "gripper.ungrip" in info.methods


@pytest.mark.asyncio
async def test_an_unadvertised_prefixed_name_is_refused_not_trimmed(
    facade: DeviceFacade,
) -> None:
    """`move_to` exists on the gripper and on both mounts. Trimming an
    unrecognised `gripper.move_to` down to `move_to` would find a name that is
    allowed and send the command to whichever object owns it."""
    with pytest.raises(ValueError, match="gripper.move_to"):
        await facade.invoke(DEVICE, "gripper.move_to", {}, confirm=True)


@pytest.mark.asyncio
async def test_the_vendor_confirm_reaches_the_gateway(facade: DeviceFacade) -> None:
    """The raw vendor console is the one command a driver flags
    `requires_confirm`, and the controller refuses it without one. The facade
    sent nothing, so `send_command` was unreachable from the CLI at all: the
    operator had no flag to set and the refusal named a confirm they could not
    give.
    """
    with patch(
        "orca.runtime.facades.devices.adhoc.execute_adhoc_command",
        new=AsyncMock(return_value=AdhocResult(raw=None, interfaces=frozenset())),
    ) as dispatch:
        await facade.invoke(
            DEVICE, "gripper.ungrip", {}, confirm=True, vendor_confirm=True,
        )

    assert dispatch.await_args.kwargs["confirm"] is True


@pytest.mark.asyncio
async def test_nothing_is_acknowledged_by_default(facade: DeviceFacade) -> None:
    """The audit confirm every REST route supplies is not the operator saying
    yes to this particular command."""
    with patch(
        "orca.runtime.facades.devices.adhoc.execute_adhoc_command",
        new=AsyncMock(return_value=AdhocResult(raw=None, interfaces=frozenset())),
    ) as dispatch:
        await facade.execute(DEVICE, "retract_axis", {"axis": "z"}, confirm=True)

    assert dispatch.await_args.kwargs["confirm"] is False
