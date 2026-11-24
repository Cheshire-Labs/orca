"""Characterization + behavior tests for driver-sourced capability surfaces.

`DeviceFacade.get_supported_commands` and the `invoke` allow-list derive the
operator-invoke surface from the device's bound LIVE driver's ``interfaces``
ClassVar (the same source `get_device_introspection` and the topology registry
already use), not from the orca-side device class's interface bases.

Two guarantees:

1. Non-LH device output is UNCHANGED by the driver-sourcing move (their orca
   class bases already match their driver interfaces one-to-one).
2. A liquid handler's surface tracks its driver profile: plr+protocol exposes
   both atomic ops and run_protocol; plr-only exposes only atomic ops;
   protocol-only exposes only run_protocol. No LH special-casing.
"""

from typing import Callable

import pytest
from cheshire_drivers.device_setup import DeviceSetup, RealDriverSetup, TcpConnection, VenusProtocols

from orca.devices.devices import (
    Delidder,
    LiquidHandlerProtocol,
    PlateWasher,
    Reader,
    Storage,
    Waste,
)
from orca.devices.centrifuge import Centrifuge
from orca.devices.sealer import Sealer
from orca.devices.shaker import Shaker
from orca.resource_models.devices import Device
from tests.real_device_path import the_real_device_path
from orca.runtime.facades.devices import (
    _allowed_invoke_methods,
    _capabilities_for_device,
)


def _caps(device: Device) -> set[str]:
    return {d.capability for d in _capabilities_for_device(device)}


# -- Non-LH devices: surface unchanged by driver-sourcing --------------------


@pytest.mark.parametrize(
    "factory, name, expected_invoke",
    [
        (Shaker, "sh1", {"shake"}),
        (Reader, "rd1", {"read"}),
        (Delidder, "dl1", {"delid"}),
        (Sealer, "sl1", {"seal"}),
        (Centrifuge, "cf1", {"centrifuge"}),
        (PlateWasher, "pw1", {"run_protocol"}),
        (Storage, "st1", set()),
        (Waste, "wt1", set()),
    ],
)
def test_non_lh_invoke_surface_is_driver_sourced_and_unchanged(
    factory: Callable[[str], Device], name: str, expected_invoke: set[str],
) -> None:
    device = factory(name)
    assert _allowed_invoke_methods(device) == frozenset(expected_invoke)


def test_non_lh_capability_namespaces_unchanged() -> None:
    assert _caps(Shaker("sh1")) == {"shaker.shake"}
    assert _caps(Reader("rd1")) == {"reader.read"}
    assert _caps(Delidder("dl1")) == {"delidder.delid"}
    assert _caps(Sealer("sl1")) == {"sealer.seal"}
    assert _caps(Centrifuge("cf1")) == {"centrifuge.centrifuge"}
    assert _caps(PlateWasher("pw1")) == {"protocol.run_protocol"}


# -- Liquid handler: surface follows the driver profile ----------------------


_LH_ATOMIC = {
    "aspirate", "dispense", "pick_up_tips", "drop_tips", "discard_tips",
    "mix", "aspirate96", "dispense96", "pick_up_tips96", "drop_tips96",
    "return_tips96",
}


def test_lh_plr_plus_protocol_profile_exposes_atomic_and_protocol() -> None:
    """Default check-run LH (its setup names SimLiquidHandlerWithProtocolDriver) is plr+protocol."""
    lh = LiquidHandlerProtocol("lh1")
    # Stated exactly, so a forwarding driver that grows an interface has to be
    # looked at rather than passing unnoticed.
    advertised = set(type(lh.driver).interfaces)
    assert advertised == {"ILiquidHandler", "IProtocolRunner"}
    invoke = _allowed_invoke_methods(lh)
    assert _LH_ATOMIC <= invoke
    assert "run_protocol" in invoke
    caps = _caps(lh)
    assert "protocol.run_protocol" in caps
    assert "liquid_handler.aspirate" in caps


def test_lh_plr_only_profile_omits_protocol() -> None:
    """A plr-only driver (an OT-2: no IProtocolRunner) exposes atomic ops, no run_protocol."""
    the_real_device_path().set_up("lh1", DeviceSetup(
        client="lab_pc",
        real=RealDriverSetup(type="plr", backend="OpentronsOT2Backend", connection=TcpConnection(type="tcp", host="ot2")),
    ))
    lh = LiquidHandlerProtocol("lh1")
    invoke = _allowed_invoke_methods(lh)
    assert _LH_ATOMIC <= invoke
    assert "run_protocol" not in invoke
    assert "protocol.run_protocol" not in _caps(lh)


def test_lh_protocol_only_profile_omits_atomic_ops() -> None:
    """A protocol-only driver (a Venus: {IProtocolRunner}) exposes only run_protocol."""
    the_real_device_path().set_up(
        "lh1", DeviceSetup(client="hamilton_pc", real=RealDriverSetup(type="venus", venus=VenusProtocols())),
    )
    lh = LiquidHandlerProtocol("lh1")
    invoke = _allowed_invoke_methods(lh)
    assert invoke == frozenset({"run_protocol"})
    assert _caps(lh) == {"protocol.run_protocol"}
