"""Profile-aware liquid-handler interface parity guard.

A liquid handler has three capability profiles. Each is decided independently
in several layers; this guard fails on drift, in the spirit of
``test_capability_sync_guard.py`` and ``test_protocol_parity_labware.py``.

| Profile        | interfaces                         | reference device |
|----------------|------------------------------------|------------------|
| plr only       | {ILiquidHandler}                   | Opentrons / PLR  |
| protocol only  | {IProtocolRunner}                  | Agilent Bravo    |
| plr + protocol | {ILiquidHandler, IProtocolRunner}  | Hamilton MLSTAR  |

The guard is PROFILE-aware, not byte-identical: the live wire driver
legitimately advertises a SUBSET of a composite sim (a protocol-only bravo's
live driver advertises {IProtocolRunner} while its PURE_SIM slot is the
composite SimLiquidHandlerWithProtocolDriver, a superset). What it pins:

- remote live driver classes advertise exactly their profile's interface set.
- The orca facade interface bridge recognizes both ILiquidHandler and
  IProtocolRunner, so a driver advertising either surfaces its verbs.
- The PURE_SIM sim slot the factory pairs per profile is a SUPERSET of
  the live driver's advertised set, and supports run_protocol whenever the
  profile does.
- The orca-client driver classes that back each profile (read from
  orca-client source) advertise a set consistent with the profile: a SUPERSET
  of the live driver's advertised set.
- cheshire-drivers' reference driver classes carry the expected profile sets.
"""

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from cheshire_drivers.plr_wrappers import PLRLiquidHandlerWrapper
from cheshire_drivers.sims import (
    SimLiquidHandlerDriver,
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


# -- PURE_SIM sim slot is a superset of the live driver --------------


def test_sim_slot_supersets_live_per_profile() -> None:
    """The factory pairs every LH profile's live driver with the composite sim.

    The PURE_SIM sim slot is always `SimLiquidHandlerWithProtocolDriver` so
    protocol-driven workflows run under PURE_SIM regardless of the live
    profile. The sim legitimately advertises a SUPERSET of the live driver
    (the operator surface + connect check read the LIVE driver, not the sim),
    so a plr-only device whose sim can also run_protocol does not over-promise.
    """
    sim_interfaces = _interfaces(SimLiquidHandlerWithProtocolDriver)
    assert _profile(SimLiquidHandlerWithProtocolDriver) == BOTH
    assert callable(getattr(SimLiquidHandlerWithProtocolDriver, "run_protocol", None))
    # The composite sim is a superset of every profile's live-driver set.
    assert sim_interfaces >= _interfaces(RemoteLiquidHandlerDriver)
    assert sim_interfaces >= _interfaces(RemoteProtocolOnlyLiquidHandlerDriver)
    assert sim_interfaces >= _interfaces(RemoteLiquidHandlerWithProtocolDriver)
    # The plr-only cheshire-drivers sim remains protocol-free (class invariant
    # the composite is built to override); pin it so a future merge that adds
    # run_protocol to the base sim surfaces here.
    assert not hasattr(SimLiquidHandlerDriver, "run_protocol")


# -- cheshire-drivers reference driver classes -----------------------------


def test_cheshire_reference_drivers_carry_profile_sets() -> None:
    # plr-only PLR backend (Opentrons / generic PLR LH).
    assert _profile(PLRLiquidHandlerWrapper) == PLR_ONLY
    # protocol-only (Hamilton Venus, Agilent VWorks-style).
    assert _profile(VenusProtocolDriver) == PROTOCOL_ONLY
    # composite plr+protocol sim (mlstar reference in PURE_SIM).
    assert _profile(SimLiquidHandlerWithProtocolDriver) == BOTH


# -- orca-client driver classes (read from orca-client source) -----------

# orca-client is a separate repo. Look where the READMEs tell you to put it:
# cloned next to this one. ORCA_CLIENT_REPO overrides for any other layout.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CANDIDATE_ORCA_CLIENT_ROOTS: list[Path] = []
_env_orca_client = os.environ.get("ORCA_CLIENT_REPO")
if _env_orca_client:
    _CANDIDATE_ORCA_CLIENT_ROOTS.append(Path(_env_orca_client))
_CANDIDATE_ORCA_CLIENT_ROOTS.append(_REPO_ROOT.parent / "orca-client")


def _find_orca_client_root() -> Path | None:
    needle = "src/orca_client/devices/sim_driver_types.py"
    for root in _CANDIDATE_ORCA_CLIENT_ROOTS:
        if (root / needle).exists():
            return root
    return None


def _load_orca_client_module(root: Path, dotted: str, rel: str) -> Any:
    """Load an orca-client module by file path under a synthetic name.

    Loaded under a synthetic name so it never shadows a real orca_client
    install. cheshire_drivers (its only third-party dep here) is on the venv.
    """
    path = root / rel
    spec = importlib.util.spec_from_file_location(dotted, path)
    if spec is None or spec.loader is None:
        pytest.fail(f"could not build import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_orca_client_lh_profiles_match_live_drivers() -> None:
    """orca-client advertises the real registered driver class's interfaces
    per instance. Verify the classes it builds for each LH profile advertise a
    set that is a SUPERSET of the matching remote live driver's advertised set
    (the live wire driver may advertise less than a composite sim).
    """
    root = _find_orca_client_root()
    if root is None:
        candidates = ", ".join(str(p) for p in _CANDIDATE_ORCA_CLIENT_ROOTS)
        pytest.fail(
            f"orca-client source not found in any of: {candidates}. The LH "
            f"profile parity guard requires the orca-client sibling checkout: "
            f"clone https://github.com/Cheshire-Labs/orca-client beside this "
            f"repository, or set ORCA_CLIENT_REPO."
        )

    sim_types = _load_orca_client_module(
        root,
        "_oc_sim_driver_types_probe",
        "src/orca_client/devices/sim_driver_types.py",
    )
    # lab_sim / sim liquid_handler -> plr-only sim, matching the plr-only
    # live driver and the factory's plr-only default profile.
    lab_sim_lh_cls = sim_types.SIM_DRIVER_CLS_BY_TYPE["liquid_handler"]
    assert _interfaces(lab_sim_lh_cls) >= _interfaces(RemoteLiquidHandlerDriver)
    assert _profile(lab_sim_lh_cls) == PLR_ONLY

    # The PLR-backed + venus paths reuse the cheshire-drivers classes asserted
    # above; orca-client's factory selects them per driver.type. Confirm the
    # factory references them (source-level), so a future rename surfaces here.
    factory_src = (
        root / "src/orca_client/devices/factory.py"
    ).read_text(encoding="utf-8")
    assert "PLRLiquidHandlerWrapper" in factory_src
    assert "VenusProtocolDriver" in factory_src
