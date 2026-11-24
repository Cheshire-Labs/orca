"""Profile-aware liquid-handler interface parity guard.

A liquid handler has three capability profiles. Each is decided independently
in several layers; this guard fails on drift, in the spirit of
``test_capability_sync_guard.py`` and ``test_protocol_parity_labware.py``.

| Profile        | interfaces                         | reference device |
|----------------|------------------------------------|------------------|
| plr only       | {ILiquidHandler}                   | Opentrons / PLR  |
| protocol only  | {IProtocolRunner}                  | Agilent Bravo    |
| plr + protocol | {ILiquidHandler, IProtocolRunner}  | Hamilton MLSTAR  |

The guard is PROFILE-aware, not byte-identical. What it pins:

- remote live driver classes advertise exactly their profile's interface set.
- The orca facade interface bridge recognizes both ILiquidHandler and
  IProtocolRunner, so a driver advertising either surfaces its verbs.
- The driver classes orca-client builds (cheshire-drivers' driver lookup)
  advertise a set consistent with the profile: a SUPERSET of the live
  driver's advertised set.
- cheshire-drivers' reference driver classes carry the expected profile sets.
"""

from cheshire_drivers.device_setup import DeviceSetup
from cheshire_drivers.driver_lookup import driver_interfaces
from cheshire_drivers.plr_wrappers import PLRLiquidHandlerWrapper
from cheshire_drivers.sims import (
    SimLiquidHandlerWithProtocolDriver,
)
from cheshire_drivers.venus_driver import VenusProtocolDriver

from orca.gateway.remote_drivers import (
    RemoteLiquidHandlerDriver,
    RemoteLiquidHandlerWithProtocolDriver,
    RemoteProtocolOnlyLiquidHandlerDriver,
)


PLR_ONLY = frozenset({"ILiquidHandler"})
PROTOCOL_ONLY = frozenset({"IProtocolRunner"})
BOTH = frozenset({"ILiquidHandler", "IProtocolRunner"})

PROFILE_NAMES = PLR_ONLY | PROTOCOL_ONLY
"""The two names a profile is made of. Everything else a liquid handler
advertises is a hardware facet (ILiquidProbe, the motion interfaces) that any
profile can carry, so a driver growing one is not profile drift."""


def _interfaces(cls: type) -> frozenset[str]:
    return frozenset(getattr(cls, "interfaces", frozenset()))


def _profile(cls: type) -> frozenset[str]:
    """A class's advertised set narrowed to the names a profile is decided by."""
    return _interfaces(cls) & PROFILE_NAMES


# -- remote live driver classes ---------------------------------------------


def test_remote_live_drivers_advertise_their_profile() -> None:
    """Narrowed to the profile names on purpose: a hardware facet such as
    ILiquidProbe is decided by the head, not by the profile, and the docstring
    above already calls carrying one legal."""
    assert _profile(RemoteLiquidHandlerDriver) == PLR_ONLY
    assert _profile(RemoteProtocolOnlyLiquidHandlerDriver) == PROTOCOL_ONLY
    assert _profile(RemoteLiquidHandlerWithProtocolDriver) == BOTH


# -- orca facade bridge recognizes both LH-relevant interfaces -------------


def test_orca_facade_bridge_recognizes_lh_interfaces() -> None:
    """The orca operator surface derives from the driver's advertised
    interfaces. Both LH-relevant interface names must be in the
    bridge, or a driver advertising one would surface no verbs for it.
    """
    from orca.runtime.facades.devices import _INTERFACE_BRIDGE

    assert "ILiquidHandler" in _INTERFACE_BRIDGE
    assert "IProtocolRunner" in _INTERFACE_BRIDGE


# -- cheshire-drivers reference driver classes -----------------------------


def test_cheshire_reference_drivers_carry_profile_sets() -> None:
    # plr-only PLR backend (Opentrons / generic PLR LH).
    assert _profile(PLRLiquidHandlerWrapper) == PLR_ONLY
    # protocol-only (Hamilton Venus, Agilent VWorks-style).
    assert _profile(VenusProtocolDriver) == PROTOCOL_ONLY
    # composite plr+protocol sim (mlstar reference in CHECK_RUN).
    assert _profile(SimLiquidHandlerWithProtocolDriver) == BOTH


# -- the drivers orca-client builds, read from cheshire-drivers' driver lookup ----


def test_the_drivers_orca_client_builds_carry_each_profile() -> None:
    """orca-client builds every driver through cheshire-drivers' driver lookup, so the server reads the same classes.

    Each class must advertise at least what the matching remote live driver advertises.
    """
    plr = DeviceSetup.model_validate({"client": "lab_pc", "real": {"type": "plr", "backend": "LiquidHandlerChatterboxBackend"}})
    venus = DeviceSetup.model_validate({"client": "lab_pc", "real": {"type": "venus"}})

    assert driver_interfaces("liquid_handler", plr) & PROFILE_NAMES == PLR_ONLY
    assert driver_interfaces("liquid_handler", venus) & PROFILE_NAMES == PROTOCOL_ONLY
    assert driver_interfaces("liquid_handler", plr) >= _interfaces(RemoteLiquidHandlerDriver)
    assert driver_interfaces("liquid_handler", venus) >= _interfaces(RemoteProtocolOnlyLiquidHandlerDriver)
    # A device with no real driver runs the sim, which can run protocols too.
    assert driver_interfaces("liquid_handler", DeviceSetup()) >= _interfaces(RemoteLiquidHandlerWithProtocolDriver)
