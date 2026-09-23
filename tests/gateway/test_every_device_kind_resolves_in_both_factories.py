"""Every declared device kind must resolve in BOTH driver factories.

A resource's `KIND` is the only channel between "what the topology declared"
and "which driver pair gets built". Two factories read it: the pure-sim one for
standalone runs and the gateway one for hosted deployments. A kind that only
one of them knows is a deployment that comes up differently depending on who
built it -- which is how translators lost their one-carriage declaration under
a hosted deployment and let the scheduler promise both endpoints of one physical carriage.
"""
from typing import Iterator, cast

import pytest

from orca.gateway.controller.controller import DeviceController
from orca.gateway.remote_device_factory import RemoteDeviceFactory
from orca.resource_models.devices import Device
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory import SimDeviceFactory
from orca.runtime.run_modes import WorkflowRunMode

# Imported for their side effect: a KIND is only walkable once its module is
# loaded, and these are the modules that declare them.
import orca.devices.centrifuge  # noqa: F401
import orca.devices.devices  # noqa: F401
import orca.devices.sealer  # noqa: F401
import orca.devices.shaker  # noqa: F401
import orca.devices.thermocycler  # noqa: F401
import orca.resource_models.translator  # noqa: F401


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
def test_the_sim_factory_resolves_every_declared_kind(kind: str) -> None:
    live, sim = SimDeviceFactory().build_drivers(kind, f"{kind}_1")
    assert live is not None and sim is not None


@pytest.mark.parametrize("kind", _declared_kinds())
def test_the_gateway_factory_resolves_every_declared_kind(kind: str) -> None:
    factory = RemoteDeviceFactory(
        controller=cast(DeviceController, object()),
        mode_resolver=lambda _name: WorkflowRunMode.LIVE,
    )
    live, sim = factory.build_drivers(kind, f"{kind}_1")
    assert live is not None and sim is not None
