from orca.operations.binding import RestBinding, Route
from orca.operations.route_tags import RouteTag
from orca.operations.wire_models import DeviceFaultModel, DeviceIntrospectionDTO
"""Wire models for the device operations.

Apart from `device.py` because the Operation classes there take
`ISystemRuntime`, and the CLI reads these models over HTTP without
ever wanting the engine.
"""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from orca.modes.device_modes import DeviceMode


class SetDeviceModeRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_id: str
    mode: DeviceMode = Field(description="SIM, or LIVE for a device with a real driver.")
    confirm: bool = Field(
        default=False,
        description="Without it nothing switches: the answer carries the warning to show the user first.",
    )


class SetDeviceModeResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_name: str
    mode: DeviceMode = Field(description="The mode asked for.")
    switched: bool = Field(
        description="True when the setting changed. False when the device already had the mode, or confirm was not sent.",
    )
    warning: str | None = Field(
        description="Set whenever the mode would change: what to tell the user before resending with confirm.",
    )
    bring_up_error: str | None = Field(
        default=None,
        description=(
            "Set when the device did not come up in its new mode. The setting stands, and commands to the device "
            "are refused (device_not_brought_up) until an initialize or a run's start brings it up."
        ),
    )


class InitializeDeviceRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_id: str
    reason: str | None = None


class InitializeDeviceResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["initialized"] = "initialized"
    device_name: str


class ConnectDeviceRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_id: str
    reason: str | None = None


class ConnectDeviceResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["connected"] = "connected"
    device_name: str


class DisconnectDeviceRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_id: str
    reason: str | None = None


class DisconnectDeviceResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["disconnected"] = "disconnected"
    device_name: str


class ReconcileDeckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_id: str
    reason: str | None = None


class DeckDisagreementDTO(BaseModel):
    """One thing the ledger and the driver's deck do not agree about."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    labware_name: str | None
    labware_id: str | None
    """Both null for an interrupted gripper move that named no labware: nothing on
    record was being moved, and what the gripper holds is unknown."""
    reason: str
    ledger_site: str
    """Where the LEDGER has it, as a full position id. For labware the ledger
    does not know at all, the driver's own site label."""
    driver_site: str | None = None
    """Where the DRIVER has it, in the form its own commands take."""
    detail: str | None = None
    """What the operator has to do, in words, when the reason alone does not say."""


class ReconcileDeckResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["reconciled"] = "reconciled"
    device_name: str
    driver_deck_empty: bool = False
    disagreements: list[DeckDisagreementDTO] = []
    """What the driver said BEFORE this push overwrote it. Empty is agreement."""
    interrupted_move_labware: str | None = None


class CompareDeckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_id: str


class CompareDeckResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_name: str
    agrees: bool
    driver_deck_empty: bool
    """The driver reports NO labware. Its session was rebuilt and the deck is
    waiting to be re-declared, which reconcile-deck does. Not a dispute about
    where anything is, so no conflicts are listed for it. `agrees` is still
    false when the record has labware on this deck."""
    disagreements: list[DeckDisagreementDTO] = []
    interrupted_move_labware: str | None = None


class TakeDeviceControlRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_id: str
    reason: str | None = None


class TakeDeviceControlResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["held"] = "held"
    device_name: str


class ReleaseDeviceControlRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_id: str
    reason: str | None = None


class ReleaseDeviceControlResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["released"] = "released"
    device_name: str


class ClearDeviceFaultRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_id: str
    reason: str | None = None


class ClearDeviceFaultResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["cleared", "no_fault"]
    device_name: str
    cleared: DeviceFaultModel | None = None


MountedHead = Literal["channels", "head96"]
"""Which head a tip is on: the independent pipetting channels, or the 96 head. Each numbers its channels from 0."""


class MountedTipDTO(BaseModel):
    """One channel of a head, as an operator states it."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    channel: int
    tip_rack: str
    position: str
    head: MountedHead = "channels"
    channel_is_inferred: bool = False
    """Accepted so an entry read back from get-mounted-tips can be corrected and
    posted straight here, and ignored: an operator naming a channel has observed
    it. Only the read answers this field for real."""


class MountedTipReadDTO(BaseModel):
    """One channel of a head, as the record answers it.

    Not a subclass of the write model: the read always answers
    `channel_is_inferred` and the write does not require it, so the two
    contracts differ on the one field they both name.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    channel: int
    tip_rack: str
    position: str
    head: MountedHead = "channels"
    channel_is_inferred: bool
    """The operation that put this tip on named no channel, so this channel
    number was counted from zero rather than observed. A head not filled in
    order can be attributed wrongly; correct it with set-mounted-tips."""


class GetMountedTipsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_id: str


class GetMountedTipsResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_name: str
    mounted: list[MountedTipReadDTO]
    provenance: str
    """Lowercase, as the wire spells it: `unknown` means nothing has ever said
    what the head is carrying, not that it is carrying nothing."""


class SetMountedTipsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_id: str
    mounted: list[MountedTipDTO]
    reason: str | None = None


class ConfirmMountedTipsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_id: str
    reason: str | None = None


class MountedTipsMutationResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_name: str


class GetDeviceIntrospectionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    device_id: str


INITIALIZE_DEVICE_ROUTE = Route(
    InitializeDeviceRequest,
    InitializeDeviceResponse,
    RestBinding(
        method="POST", path="/devices/{device_id}/initialize", tag=RouteTag.DEVICES,
        drives_device=True,
    ),
)


CONNECT_DEVICE_ROUTE = Route(
    ConnectDeviceRequest,
    ConnectDeviceResponse,
    RestBinding(
        method="POST", path="/devices/{device_id}/connect", tag=RouteTag.DEVICES,
        drives_device=True,
    ),
)


DISCONNECT_DEVICE_ROUTE = Route(
    DisconnectDeviceRequest,
    DisconnectDeviceResponse,
    RestBinding(
        method="POST", path="/devices/{device_id}/disconnect", tag=RouteTag.DEVICES,
        drives_device=True,
    ),
)


RECONCILE_DECK_ROUTE = Route(
    ReconcileDeckRequest,
    ReconcileDeckResponse,
    RestBinding(
        method="POST", path="/liquid-handlers/{device_id}/reconcile-deck", tag=RouteTag.DEVICES,
        carries_blockers=True,
        drives_device=True,
    ),
)


TAKE_DEVICE_CONTROL_ROUTE = Route(
    TakeDeviceControlRequest,
    TakeDeviceControlResponse,
    RestBinding(
        method="POST", path="/devices/{device_id}/take-control", tag=RouteTag.DEVICES,
    ),
)


RELEASE_DEVICE_CONTROL_ROUTE = Route(
    ReleaseDeviceControlRequest,
    ReleaseDeviceControlResponse,
    RestBinding(
        method="POST", path="/devices/{device_id}/release-control", tag=RouteTag.DEVICES,
        carries_blockers=True,
    ),
)


CLEAR_DEVICE_FAULT_ROUTE = Route(
    ClearDeviceFaultRequest,
    ClearDeviceFaultResponse,
    RestBinding(
        method="POST", path="/devices/{device_id}/clear-fault", tag=RouteTag.DEVICES,
        carries_blockers=True,
    ),
)


COMPARE_DECK_ROUTE = Route(
    CompareDeckRequest,
    CompareDeckResponse,
    RestBinding(
        method="GET", path="/liquid-handlers/{device_id}/deck-comparison", tag=RouteTag.DEVICES,
        carries_blockers=True,
        drives_device=True,
    ),
)


GET_MOUNTED_TIPS_ROUTE = Route(
    GetMountedTipsRequest,
    GetMountedTipsResponse,
    RestBinding(
        method="GET", path="/liquid-handlers/{device_id}/mounted-tips", tag=RouteTag.DEVICES,
    ),
)


SET_MOUNTED_TIPS_ROUTE = Route(
    SetMountedTipsRequest,
    MountedTipsMutationResponse,
    RestBinding(
        method="PUT", path="/liquid-handlers/{device_id}/mounted-tips", tag=RouteTag.DEVICES,
    ),
)


CONFIRM_MOUNTED_TIPS_ROUTE = Route(
    ConfirmMountedTipsRequest,
    MountedTipsMutationResponse,
    RestBinding(
        method="POST", path="/liquid-handlers/{device_id}/mounted-tips/confirm", tag=RouteTag.DEVICES,
    ),
)


SET_DEVICE_MODE_ROUTE = Route(
    SetDeviceModeRequest,
    SetDeviceModeResponse,
    RestBinding(
        method="PUT", path="/devices/{device_id}/mode", tag=RouteTag.DEVICES,
        drives_device=True,
    ),
)


GET_DEVICE_INTROSPECTION_ROUTE = Route(
    GetDeviceIntrospectionRequest,
    DeviceIntrospectionDTO,
    RestBinding(
        method="GET", path="/devices/{device_id}/introspection", tag=RouteTag.DEVICES,
    ),
)
