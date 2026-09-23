"""Device capability validation.

Capabilities are resolved at runtime against:

1. Each device's advertised abstract interfaces (`DeviceConnectInfo.interfaces`).
   The gateway imports the cheshire-drivers interface classes and delegates to
   `cheshire_drivers.driver_introspection.interface_command_names` for the
   invokable contract. The interface class itself remains the source of truth;
   the MRO-walk helper is the single canonical algorithm for "what commands does
   this interface contract expose?" (methods only -- @property members like
   `name` / `single_carriage` are engine-read metadata, never wire-invokable),
   shared with the device bridge's handshake derivation so the two layers cannot
   drift.

2. Each device's auto-derived vendor extras (`DeviceConnectInfo.capabilities`).
   Computed by the device bridge at handshake from public-methods-minus-interfaces
   via `derive_capabilities`, which uses the same per-class helper.

`validate_capability_for_device(interfaces, capabilities, command)` consults both.

Per-command timeouts live in ``orca.gateway.controller.command_clock`` --
``DeviceCommandClock`` reads each driver's advertised ``MethodInfo.duration``
instead of a per-device-kind table.
"""

from typing import Dict

from cheshire_drivers.driver_introspection import (
    interface_command_names,
    interface_member_names,
)
from cheshire_drivers.response_lookup import WIRE_READABLE_PROPERTIES
from cheshire_drivers.interfaces import (
    ICentrifugeDriver,
    IDelidderDriver,
    IForceGripperJawDriver,
    IGantryParkingDriver,
    IGripperMotionDriver,
    IHomeableDriver,
    IGripperPositionDriver,
    IGripperRotationDriver,
    ILiquidHandlerDriver,
    ILiquidProbeDriver,
    IPipetteMotionDriver,
    IPlateWasherDriver,
    IProtocolRunnerDriver,
    IReaderDriver,
    ISealerDriver,
    IShakerDriver,
    IStorageDriver,
    ITempGettableDriver,
    ITempSettableDriver,
    IThermocyclerDriver,
    ITransporterDriver,
    IWasteDriver,
    IWidthGripperJawDriver,
)


NAME_TO_INTERFACE: Dict[str, type] = {
    "IShaker": IShakerDriver,
    "ICentrifuge": ICentrifugeDriver,
    "IThermocycler": IThermocyclerDriver,
    "ISealer": ISealerDriver,
    "IReader": IReaderDriver,
    "IDelidder": IDelidderDriver,
    "IProtocolRunner": IProtocolRunnerDriver,
    "IPlateWasher": IPlateWasherDriver,
    "ITransporter": ITransporterDriver,
    "ILiquidHandler": ILiquidHandlerDriver,
    "ILiquidProbe": ILiquidProbeDriver,
    "IStorage": IStorageDriver,
    "IWaste": IWasteDriver,
    "ITempGettable": ITempGettableDriver,
    "ITempSettable": ITempSettableDriver,
    "IPipetteMotion": IPipetteMotionDriver,
    "IGantryParking": IGantryParkingDriver,
    "IHomeable": IHomeableDriver,
    "IGripperMotion": IGripperMotionDriver,
    "IGripperPosition": IGripperPositionDriver,
    "IForceGripperJaw": IForceGripperJawDriver,
    "IWidthGripperJaw": IWidthGripperJawDriver,
    "IGripperRotation": IGripperRotationDriver,
}


def validate_capability_for_device(
    interfaces_advertised: frozenset[str],
    capabilities_advertised: frozenset[str],
    command: str,
) -> bool:
    """Resolve `command` against advertised interfaces + capabilities.

    Returns True iff:
      - command is in the invokable-command set of any class in NAME_TO_INTERFACE
        whose name appears in `interfaces_advertised`, as computed by the canonical
        `cheshire_drivers.driver_introspection.interface_command_names` helper
        (interface methods only; @property metadata is excluded), OR
      - command is in `capabilities_advertised` (auto-derived vendor extras).

    Used by `validate_capability` after looking up the per-device advertised
    sets in the device registry. Exported separately so callers (e.g. tests
    or the introspection endpoint) can answer the question without going
    through the registry indirection.
    """
    for iface_name in interfaces_advertised:
        cls = NAME_TO_INTERFACE.get(iface_name)
        if cls is None:
            continue
        if command in interface_command_names(cls):
            return True
        # A readable property is a READ, not an invocation, so the command set
        # rightly excludes it -- but the on-prem client serves exactly these
        # names by fetching the attribute, and refusing them here left
        # `is_initialized` dead on arrival since it shipped. The shared
        # `WIRE_READABLE_PROPERTIES` is what keeps the two ends agreeing.
        if command in WIRE_READABLE_PROPERTIES and command in interface_member_names(cls):
            return True
    return command in capabilities_advertised
