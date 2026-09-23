"""Homing must be reachable on anything that advertises it, not just on arms.

Bring-up stopped homing devices by itself, so homing a handler before the first
move of the day is the operator's own call. On the gateway path that call is
resolved by name: the gate looks the advertised interface up in
`NAME_TO_INTERFACE` and asks whether the command is on it. A name with no entry
resolves to no class, so every command behind it reads as unsupported.

An arm hid this. `home` also sits on `ITransporterDriver`, which IS mapped, so
a transporter kept working while a liquid handler -- which advertises
`IHomeable` without being a transporter -- was refused.
"""

import pytest

from orca.devices.devices import LiquidHandlerProtocol
from orca.gateway.registry.capabilities import (
    NAME_TO_INTERFACE,
    validate_capability_for_device,
)
from orca.gateway.remote_transporter_driver import RemoteTransporterDriver


def _advertised_by_sim_handler() -> frozenset[str]:
    return frozenset(type(LiquidHandlerProtocol("lh1").live_driver).interfaces)


def test_a_handler_that_advertises_homing_can_be_homed() -> None:
    assert validate_capability_for_device(
        interfaces_advertised=_advertised_by_sim_handler(),
        capabilities_advertised=frozenset(),
        command="home",
    ), (
        "a liquid handler advertising IHomeable was refused `home` because the "
        "name resolved to no interface class; bring-up no longer homes it, so "
        "this is the operator's only way to reference its axes"
    )


def test_an_arm_can_still_be_homed() -> None:
    """The case that masked the gap: `home` also resolves via ITransporter."""
    assert validate_capability_for_device(
        interfaces_advertised=RemoteTransporterDriver.interfaces,
        capabilities_advertised=frozenset(),
        command="home",
    )


@pytest.mark.parametrize("advertised", [
    pytest.param(_advertised_by_sim_handler, id="sim_liquid_handler"),
    pytest.param(lambda: RemoteTransporterDriver.interfaces, id="remote_transporter"),
])
def test_every_advertised_interface_resolves_to_a_class(advertised) -> None:
    """The general rule. An unmapped name is not a loud failure anywhere; it
    silently subtracts whatever commands sat behind it."""
    unmapped = sorted(set(advertised()) - set(NAME_TO_INTERFACE))
    assert not unmapped, (
        f"advertised {unmapped} with no NAME_TO_INTERFACE entry; the gate "
        f"cannot resolve them and the commands behind them read as unsupported"
    )
