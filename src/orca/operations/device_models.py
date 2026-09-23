"""Wire models for the device operations.

Apart from `device.py` because the Operation classes there take
`ISystemRuntime`, and the CLI reads these models over HTTP without
ever wanting the engine.
"""

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, ConfigDict
from orca.runtime.run_modes import WorkflowRunMode


class DeviceUnionEntryModel(BaseModel):
    """Mirror of ``DeviceUnionEntry``."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    in_topology: bool
    gateway_connected: bool
    last_heartbeat: datetime | None
    topology_kind: str | None
    gateway_kind: str | None
    interfaces: tuple[str, ...]
    position_ids: tuple[str, ...]
    status: str | None
    faulted: bool = False


class ListDevicesRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ListDevicesResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    devices: list[DeviceUnionEntryModel]


class GetDeviceStatusRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_name: str


class DeviceFaultModel(BaseModel):
    """Mirror of ``DeviceFaultSummary``.

    ``outcome`` is ``failed`` when the driver reported the failure itself and
    ``unknown`` when no answer came back. On ``unknown`` the command may still
    be running on the instrument, which is what ``may_still_be_moving`` says.
    """
    model_config = ConfigDict(frozen=True, extra="forbid")
    command: str
    outcome: Literal["failed", "unknown"]
    error: str
    error_type: str
    at: float
    may_still_be_moving: bool
    message: str
    execution_id: str | None = None


class DeviceStatusModel(BaseModel):
    """Mirror of ``DeviceSnapshot``."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str
    type_name: str
    is_initialized: bool
    is_busy: bool
    effective_mode: WorkflowRunMode
    position_ids: tuple[str, ...]
    loaded_labware_ids: tuple[str, ...]
    # Mirror of ``DeviceSnapshot.under_external_control``. ``extra="forbid"``
    # would reject the new field otherwise, since ``_device_status_to_model``
    # round-trips through ``dataclasses.asdict`` which emits every dataclass
    # field. Default ``False`` for forward compatibility with old construct
    # sites that pre-date the field.
    under_external_control: bool = False
    external_control_hold: str | None = None
    """Mirror of ``DeviceSnapshot.external_control_hold``: why an operator is
    holding this, or None if nobody is."""
    fault: DeviceFaultModel | None = None
    """Mirror of ``DeviceSnapshot.fault``: the command that left this device
    part-way through something, or None. The workflow cannot drive the device
    while it is set."""


class GetDeviceStatusResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device: DeviceStatusModel


class InitializeDeviceRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_name: str
    mode: WorkflowRunMode | None = None


class InitializeDeviceResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["initialized"] = "initialized"
    device_name: str


class ConnectDeviceRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_name: str
    mode: WorkflowRunMode | None = None


class ConnectDeviceResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["connected"] = "connected"
    device_name: str


class DisconnectDeviceRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_name: str
    mode: WorkflowRunMode | None = None


class DisconnectDeviceResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["disconnected"] = "disconnected"
    device_name: str


class ReconcileDeckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_name: str
    mode: WorkflowRunMode | None = None


class DeckDisagreementDTO(BaseModel):
    """One labware the ledger and the driver's deck do not agree about."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    labware_name: str
    labware_id: str
    reason: str
    ledger_site: str
    """Where the LEDGER has it, as a full position id. For labware the ledger
    does not know at all, the driver's own site label."""
    driver_site: str | None = None
    """Where the DRIVER has it, in the form its own commands take."""


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
    device_name: str
    mode: WorkflowRunMode | None = None


class CompareDeckResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_name: str
    agrees: bool
    driver_deck_empty: bool
    """The driver reports NO labware. Its session was rebuilt and the deck is
    waiting to be re-declared, which reconcile-deck does. Not a dispute about
    where anything is, so no conflicts are raised for it."""
    disagreements: list[DeckDisagreementDTO] = []
    interrupted_move_labware: str | None = None


class TakeDeviceControlRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_name: str
    reason: str | None = None


class TakeDeviceControlResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["held"] = "held"
    device_name: str


class ReleaseDeviceControlRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_name: str


class ReleaseDeviceControlResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["released"] = "released"
    device_name: str


class ClearDeviceFaultRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_name: str


class ClearDeviceFaultResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["cleared", "no_fault"]
    device_name: str
    cleared: DeviceFaultModel | None = None


class MountedTipDTO(BaseModel):
    """One channel of a head, as an operator states it."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    channel: int
    tip_rack: str
    position: str
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
    channel_is_inferred: bool
    """The operation that put this tip on named no channel, so this channel
    number was counted from zero rather than observed. A head not filled in
    order can be attributed wrongly; correct it with set-mounted-tips."""


class GetMountedTipsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_name: str


class GetMountedTipsResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_name: str
    mounted: list[MountedTipReadDTO]
    provenance: str
    """Lowercase, as the wire spells it: `unknown` means nothing has ever said
    what the head is carrying, not that it is carrying nothing."""


class SetMountedTipsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_name: str
    mounted: list[MountedTipDTO]
    reason: str | None = None


class ConfirmMountedTipsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_name: str
    reason: str | None = None


class MountedTipsMutationResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    device_name: str
