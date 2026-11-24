"""Every declared device kind must resolve both in the device factory and in orca-client's simulator lookup.

A resource's `KIND` is the only channel between "what the topology declared"
and "which driver gets built". The device factory reads it for the forwarding driver,
and orca-client reads it for the simulator. A kind that only one of them knows is a
deployment that comes up differently depending on who built it -- which is how
translators lost their one-carriage declaration under a hosted deployment and let the
scheduler promise both endpoints of one physical carriage.
"""
from typing import Iterator, cast

from cheshire_drivers.device_setup import DeviceSetup
from cheshire_drivers.driver_lookup import DriverRequest, build_sim_driver

import pytest

from orca.gateway.controller.controller import DeviceController
from orca.gateway.remote_device_factory import RemoteDeviceFactory
from orca.resource_models.devices import Device
from orca.resource_models.transporter import Transporter
from orca.modes.run_modes import WorkflowRunMode

# Imported for their side effect: a KIND is only walkable once its module is
# loaded, and these are the modules that declare them.
import orca.devices.centrifuge  # noqa: F401
import orca.devices.devices  # noqa: F401
import orca.devices.sealer  # noqa: F401
import orca.devices.shaker  # noqa: F401
import orca.devices.thermocycler  # noqa: F401
import orca.resource_models.translator  # noqa: F401
from tests.gateway.mode_doubles import fixed_mode


def _walk(root: type) -> Iterator[type]:
    for sub in root.__subclasses__():
        yield sub
        yield from _walk(sub)


def _declared_kinds() -> list[str]:
    kinds = {
        getattr(cls, "KIND")
        for root in (Device, Transporter)
        for cls in _walk(root)
        # Shipped resources only: a test double's KIND is not a deployment's.
        if cls.__module__.startswith("orca.")
        and isinstance(getattr(cls, "KIND", None), str)
    }
    assert kinds, "the walk found no device kinds, so it pins nothing"
    return sorted(kinds)


@pytest.mark.parametrize("kind", _declared_kinds())
def test_orca_client_has_a_simulator_for_every_declared_kind(kind: str) -> None:
    assert build_sim_driver(DriverRequest(name=f"{kind}_1", kind=kind, setup=DeviceSetup())) is not None


@pytest.mark.parametrize("kind", _declared_kinds())
def test_the_gateway_factory_resolves_every_declared_kind(kind: str) -> None:
    factory = RemoteDeviceFactory(
        controller=cast(DeviceController, object()),
        mode_resolver=fixed_mode(WorkflowRunMode.LIVE),
    )
    assert factory.build_driver(kind, f"{kind}_1") is not None
