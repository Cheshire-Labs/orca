"""Device Operations.

Covers the engine-side `IDeviceFacade` surface for introspection and
initialize. The `execute` / `invoke` operator-driven device-command
paths flow through a hosted deployment's `DeviceController` (WebSocket gateway) and
are not Operations yet: they need both runtime and hosted-specific gateway
plumbing that the Operation framework does not abstract cleanly.

What is here: ListDevices, GetDeviceStatus, GetDeviceCommands,
GetDeviceIntrospection, InitializeDevice, ConnectDevice, DisconnectDevice.

Wire mirrors: ``DeviceUnionEntry`` + ``DeviceSnapshot`` are frozen
dataclasses on ``orca.runtime.status_models``. Operations expose them
as Pydantic mirrors whose fields match the dataclass one-for-one;
``dataclasses.asdict`` + ``model_validate`` does the conversion so a
field rename on the dataclass surfaces at type-check time.
"""

import dataclasses
from typing import ClassVar
from pydantic import BaseModel
from orca.operations._protocol import OperationError
from orca.runtime.runtime_interface import ISystemRuntime
from orca.system.system_interface import DeckComparison
from orca.operations.device_models import (
    ClearDeviceFaultRequest,
    ClearDeviceFaultResponse,
    CompareDeckRequest,
    CompareDeckResponse,
    ConfirmMountedTipsRequest,
    ConnectDeviceRequest,
    ConnectDeviceResponse,
    DeckDisagreementDTO,
    DeviceFaultModel,
    DeviceStatusModel,
    DeviceUnionEntryModel,
    DisconnectDeviceRequest,
    DisconnectDeviceResponse,
    GetDeviceStatusRequest,
    GetDeviceStatusResponse,
    GetMountedTipsRequest,
    GetMountedTipsResponse,
    InitializeDeviceRequest,
    InitializeDeviceResponse,
    ListDevicesRequest,
    ListDevicesResponse,
    MountedTipReadDTO,
    MountedTipsMutationResponse,
    ReconcileDeckRequest,
    ReconcileDeckResponse,
    ReleaseDeviceControlRequest,
    ReleaseDeviceControlResponse,
    SetMountedTipsRequest,
    TakeDeviceControlRequest,
    TakeDeviceControlResponse,
)
from orca.runtime.status_models import DeviceSnapshot, DeviceUnionEntry


def _device_entry_to_model(entry: DeviceUnionEntry) -> DeviceUnionEntryModel:
    return DeviceUnionEntryModel.model_validate(dataclasses.asdict(entry))


# -- ListDevices -------------------------------------------------------------


class ListDevicesOperation:
    Request: ClassVar[type[BaseModel]] = ListDevicesRequest
    Response: ClassVar[type[BaseModel]] = ListDevicesResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: ListDevicesRequest) -> ListDevicesResponse:
        del req
        entries = await self._runtime.devices.list_devices()
        return ListDevicesResponse(
            devices=[_device_entry_to_model(e) for e in entries],
        )


# -- GetDeviceStatus ---------------------------------------------------------


def _device_status_to_model(snap: DeviceSnapshot) -> DeviceStatusModel:
    return DeviceStatusModel.model_validate(dataclasses.asdict(snap))


class GetDeviceStatusOperation:
    Request: ClassVar[type[BaseModel]] = GetDeviceStatusRequest
    Response: ClassVar[type[BaseModel]] = GetDeviceStatusResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: GetDeviceStatusRequest) -> GetDeviceStatusResponse:
        try:
            snap = self._runtime.devices.get_device_status(req.device_name)
        except KeyError as exc:
            raise OperationError.not_found(
                f"device {req.device_name!r} not found",
                device_name=req.device_name,
            ) from exc
        return GetDeviceStatusResponse(device=_device_status_to_model(snap))


# -- InitializeDevice -------------------------------------------------------


class InitializeDeviceOperation:
    Request: ClassVar[type[BaseModel]] = InitializeDeviceRequest
    Response: ClassVar[type[BaseModel]] = InitializeDeviceResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: InitializeDeviceRequest) -> InitializeDeviceResponse:
        try:
            await self._runtime.devices.initialize(
                req.device_name, mode=req.mode, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                f"device {req.device_name!r} not found",
                device_name=req.device_name,
            ) from exc
        except RuntimeError as exc:
            raise OperationError.conflict(str(exc)) from exc
        return InitializeDeviceResponse(device_name=req.device_name)


# -- ConnectDevice / DisconnectDevice ---------------------------------------


class ConnectDeviceOperation:
    """Take a device without readying it: opens the link, moves nothing."""
    Request: ClassVar[type[BaseModel]] = ConnectDeviceRequest
    Response: ClassVar[type[BaseModel]] = ConnectDeviceResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: ConnectDeviceRequest) -> ConnectDeviceResponse:
        try:
            await self._runtime.devices.connect(req.device_name, mode=req.mode)
        except KeyError as exc:
            raise OperationError.not_found(
                f"device {req.device_name!r} not found",
                device_name=req.device_name,
            ) from exc
        except RuntimeError as exc:
            raise OperationError.conflict(str(exc)) from exc
        return ConnectDeviceResponse(device_name=req.device_name)


class DisconnectDeviceOperation:
    """Hand a device back without homing it. Drops motor power where the device holds it."""
    Request: ClassVar[type[BaseModel]] = DisconnectDeviceRequest
    Response: ClassVar[type[BaseModel]] = DisconnectDeviceResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: DisconnectDeviceRequest) -> DisconnectDeviceResponse:
        try:
            await self._runtime.devices.disconnect(
                req.device_name, mode=req.mode, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                f"device {req.device_name!r} not found",
                device_name=req.device_name,
            ) from exc
        except RuntimeError as exc:
            raise OperationError.conflict(str(exc)) from exc
        return DisconnectDeviceResponse(device_name=req.device_name)


# -- ReconcileDeck ------------------------------------------------------------


class ReconcileDeckOperation:
    """Re-seed a liquid handler from the world model: layout + occupancy.

    A state push, no motion. The operator's lever after a driver session was
    rebuilt outside orca (touchscreen cancel_run + initialize, driver-internal
    recovery), so a RETRY can find its labware again.
    """
    Request: ClassVar[type[BaseModel]] = ReconcileDeckRequest
    Response: ClassVar[type[BaseModel]] = ReconcileDeckResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: ReconcileDeckRequest) -> ReconcileDeckResponse:
        try:
            comparison = await self._runtime.devices.reconcile_deck(
                req.device_name, mode=req.mode, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                f"device {req.device_name!r} not found",
                device_name=req.device_name,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        if comparison is None:
            return ReconcileDeckResponse(device_name=req.device_name)
        return ReconcileDeckResponse(
            device_name=req.device_name,
            driver_deck_empty=comparison.driver_deck_empty,
            disagreements=_disagreement_dtos(comparison),
            interrupted_move_labware=comparison.interrupted_move_labware,
        )


def _disagreement_dtos(comparison: DeckComparison) -> list[DeckDisagreementDTO]:
    return [
        DeckDisagreementDTO(
            labware_name=conflict.labware_name,
            labware_id=conflict.labware_id,
            reason=conflict.reason.value,
            ledger_site=conflict.position_id,
            driver_site=conflict.driver_site,
        )
        for conflict in comparison.disagreements
    ]


class TakeDeviceControlOperation:
    """Claim a device for hands-on work until it is released.

    The gateway takes external control around each ad-hoc command and gives it
    straight back, which says nothing about the gap between two of them. A
    workflow can start a move into the device in that gap. This claim stands
    until someone releases it.
    """
    Request: ClassVar[type[BaseModel]] = TakeDeviceControlRequest
    Response: ClassVar[type[BaseModel]] = TakeDeviceControlResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(
        self, req: TakeDeviceControlRequest,
    ) -> TakeDeviceControlResponse:
        try:
            await self._runtime.devices.take_external_control(
                req.device_name, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                f"device {req.device_name!r} not found",
                device_name=req.device_name,
            ) from exc
        return TakeDeviceControlResponse(device_name=req.device_name)


class ReleaseDeviceControlOperation:
    """Hand a device back to the workflow. Idempotent."""
    Request: ClassVar[type[BaseModel]] = ReleaseDeviceControlRequest
    Response: ClassVar[type[BaseModel]] = ReleaseDeviceControlResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(
        self, req: ReleaseDeviceControlRequest,
    ) -> ReleaseDeviceControlResponse:
        try:
            await self._runtime.devices.release_external_control(
                req.device_name, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                f"device {req.device_name!r} not found",
                device_name=req.device_name,
            ) from exc
        return ReleaseDeviceControlResponse(device_name=req.device_name)


class ClearDeviceFaultOperation:
    """Say a device has been looked at after a command left it part-way through.

    A command that fails, times out, or is cancelled after it went out on the
    wire can leave the instrument mid-motion. The device is faulted from that
    moment and the workflow cannot drive it. Nothing lifts that on its own: a
    later command succeeding proves the machine answers, never that the plate it
    half-moved is where the ledger says it is.
    """
    Request: ClassVar[type[BaseModel]] = ClearDeviceFaultRequest
    Response: ClassVar[type[BaseModel]] = ClearDeviceFaultResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(
        self, req: ClearDeviceFaultRequest,
    ) -> ClearDeviceFaultResponse:
        try:
            cleared = await self._runtime.devices.clear_fault(
                req.device_name, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                f"device {req.device_name!r} not found",
                device_name=req.device_name,
            ) from exc
        if cleared is None:
            return ClearDeviceFaultResponse(
                status="no_fault", device_name=req.device_name,
            )
        return ClearDeviceFaultResponse(
            status="cleared",
            device_name=req.device_name,
            cleared=DeviceFaultModel.model_validate(dataclasses.asdict(cleared)),
        )


class CompareDeckOperation:
    """Ask a liquid handler whether its deck and the ledger still agree.

    Reads only. The reconcile that follows would overwrite the driver's answer,
    so this is how an operator sees a disagreement before deciding which side is
    right. Files nothing: the reconcile is the call that acts, so it is the one
    that files. A plate the ledger has in the jaws always disagrees, because a
    deck addresses labware by site and the jaws are not one.
    """
    Request: ClassVar[type[BaseModel]] = CompareDeckRequest
    Response: ClassVar[type[BaseModel]] = CompareDeckResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: CompareDeckRequest) -> CompareDeckResponse:
        try:
            comparison = await self._runtime.devices.compare_deck(
                req.device_name, mode=req.mode,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                f"device {req.device_name!r} not found",
                device_name=req.device_name,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        return CompareDeckResponse(
            device_name=comparison.device_name,
            agrees=not comparison.disagreements,
            driver_deck_empty=comparison.driver_deck_empty,
            disagreements=_disagreement_dtos(comparison),
            interrupted_move_labware=comparison.interrupted_move_labware,
        )


class GetMountedTipsOperation:
    Request: ClassVar[type[BaseModel]] = GetMountedTipsRequest
    Response: ClassVar[type[BaseModel]] = GetMountedTipsResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: GetMountedTipsRequest) -> GetMountedTipsResponse:
        try:
            mounted = await self._runtime.devices.get_mounted_tips(req.device_name)
        except KeyError as exc:
            raise OperationError.not_found(
                f"device {req.device_name!r} not found", device_name=req.device_name,
            ) from exc
        return GetMountedTipsResponse(
            device_name=req.device_name,
            mounted=[
                MountedTipReadDTO(
                    channel=channel, tip_rack=tip.tip_rack, position=tip.position,
                    channel_is_inferred=tip.channel_is_inferred,
                )
                for channel, tip in sorted(mounted.by_channel.items())
            ],
            provenance=mounted.provenance.value.lower(),
        )


class SetMountedTipsOperation:
    Request: ClassVar[type[BaseModel]] = SetMountedTipsRequest
    Response: ClassVar[type[BaseModel]] = MountedTipsMutationResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: SetMountedTipsRequest) -> MountedTipsMutationResponse:
        try:
            await self._runtime.devices.set_mounted_tips(
                req.device_name,
                {m.channel: (m.tip_rack, m.position) for m in req.mounted},
                reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                f"device {req.device_name!r} not found", device_name=req.device_name,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        return MountedTipsMutationResponse(device_name=req.device_name)


class ConfirmMountedTipsOperation:
    Request: ClassVar[type[BaseModel]] = ConfirmMountedTipsRequest
    Response: ClassVar[type[BaseModel]] = MountedTipsMutationResponse

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: ConfirmMountedTipsRequest) -> MountedTipsMutationResponse:
        try:
            await self._runtime.devices.confirm_mounted_tips(
                req.device_name, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise OperationError.not_found(
                f"device {req.device_name!r} not found", device_name=req.device_name,
            ) from exc
        except ValueError as exc:
            raise OperationError.invalid_input(str(exc)) from exc
        return MountedTipsMutationResponse(device_name=req.device_name)
