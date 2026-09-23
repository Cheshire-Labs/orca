"""Every driver this factory builds must admit an agent holds its instrument.

`DeviceLinkReader` refuses to answer a device's link off an in-process driver
when an agent holds the device, because nothing in process is an answer about
the instrument: for a device with a wire surface it is a proxy holding a cache
of the last command through it, and for a passive one (storage, waste) it is a
local simulator orca's own bring-up walk initialized. Being built by
`RemoteDeviceFactory` is what makes a device agent-held, so the stamp goes on
there and this walks every type the factory can build.

The earlier version of this guard walked `Remote*` classes instead, and could
not see `storage` or `waste`: those get no proxy at all, so their live slot is a
plain `SimStorageDriver` / `SimWasteDriver`. A dropped agent then handed the
operator that simulator's flags as the device's own.
"""

from typing import Optional, cast

from orca.gateway.controller.controller import DeviceController
from orca.gateway.remote_device_factory import _DRIVER_BUILDERS, RemoteDeviceFactory
from orca.runtime.registries.device_link import AGENT_HELD_ATTRIBUTE, _AgentHeld
from orca.runtime.run_modes import WorkflowRunMode
from pydantic import JsonValue


class _UncalledController:
    """Stands in for the controller. Building drivers must not dispatch."""

    async def execute_command(self, **kwargs: object) -> Optional[JsonValue]:
        raise AssertionError("building a driver pair must not send a command")


def _factory() -> RemoteDeviceFactory:
    return RemoteDeviceFactory(
        controller=cast(DeviceController, _UncalledController()),
        default_timeout=30.0,
        mode_resolver=lambda _name: WorkflowRunMode.LIVE,
    )


def test_every_device_type_the_factory_builds_is_stamped_agent_held() -> None:
    factory = _factory()
    assert _DRIVER_BUILDERS, "the walk found no device types, so it pins nothing"

    unstamped = [
        device_type
        for device_type in sorted(_DRIVER_BUILDERS)
        if not isinstance(factory.build_drivers(device_type, f"{device_type}_1")[0], _AgentHeld)
    ]
    assert not unstamped, (
        f"{unstamped} are built for an agent-held deployment but do not say so, "
        "so a dropped agent hands their in-process flags to the operator"
    )


def test_a_deck_modelling_liquid_handler_takes_the_same_stamp() -> None:
    """The one type with its own branch in `build_drivers` must not skip it."""
    live, _ = _factory().build_drivers("liquid_handler", "lh_1", deck_modeling=True)
    assert isinstance(live, _AgentHeld)


def test_the_stamp_and_the_reader_name_the_same_attribute() -> None:
    """Renaming one side only must not leave this guard passing."""
    assert hasattr(_AgentHeld, AGENT_HELD_ATTRIBUTE)
    live, _ = _factory().build_drivers("shaker", "shaker_1")
    assert getattr(live, AGENT_HELD_ATTRIBUTE) is True
