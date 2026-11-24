from orca.operations.device_models import (
    CLEAR_DEVICE_FAULT_ROUTE,
    COMPARE_DECK_ROUTE,
    CONFIRM_MOUNTED_TIPS_ROUTE,
    CONNECT_DEVICE_ROUTE,
    DISCONNECT_DEVICE_ROUTE,
    GET_MOUNTED_TIPS_ROUTE,
    INITIALIZE_DEVICE_ROUTE,
    RECONCILE_DECK_ROUTE,
    RELEASE_DEVICE_CONTROL_ROUTE,
    SET_DEVICE_MODE_ROUTE,
    SET_MOUNTED_TIPS_ROUTE,
    TAKE_DEVICE_CONTROL_ROUTE,
)
"""Device Operations.

The device lifecycle (initialize, connect, disconnect, reconcile), its mode, the operator's
hold, faults and mounted tips. One device's state is read from the device registry.
"""

from orca.operations.binding import RestBinding
import dataclasses
from collections.abc import Iterator
from contextlib import contextmanager
from typing import ClassVar
from orca.modes.run_modes import current_run_mode, mode_scope
from pydantic import BaseModel
from orca.operations.error_codes import ErrorCode
from orca.operations.protocol import OperationError, confirm_refused
from orca.gateway.controller.exceptions import DeviceError, DeviceLockedError, DeviceUnknownError
from orca.gateway.device_command_runner import IDeviceCommandRunner
from orca.operations.device_commands import device_error, device_not_found
from orca.state.confirm_refusals import ConfirmRefusedError
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
    DisconnectDeviceRequest,
    DisconnectDeviceResponse,
    GetMountedTipsRequest,
    GetMountedTipsResponse,
    InitializeDeviceRequest,
    InitializeDeviceResponse,
    MountedTipReadDTO,
    MountedTipsMutationResponse,
    ReconcileDeckRequest,
    ReconcileDeckResponse,
    ReleaseDeviceControlRequest,
    ReleaseDeviceControlResponse,
    SetDeviceModeRequest,
    SetDeviceModeResponse,
    SetMountedTipsRequest,
    TakeDeviceControlRequest,
    TakeDeviceControlResponse,
)
from orca.runtime.facades.device_modes import (
    DeviceHasNoRealDriverError,
    ModeSwitchDuringRunError,
)


# -- InitializeDevice -------------------------------------------------------


@contextmanager
def _one_mode(runtime: ISystemRuntime) -> Iterator[None]:
    """The run mode is read once, so a run starting or ending mid-operation cannot split it across two worlds."""
    with mode_scope(current_run_mode.get(runtime.operator_base_mode())):
        yield


async def _lifecycle(
    runtime: ISystemRuntime, devices: IDeviceCommandRunner, device_name: str, command: str, reason: str | None,
) -> None:
    """An operator's initialize, connect or disconnect: through the runner, then the deck brought in line."""
    try:
        with _one_mode(runtime):
            await devices.run_named(device_name, command, {}, reason=reason)
            await runtime.devices.after_lifecycle(device_name, command)
    except (DeviceUnknownError, KeyError) as exc:
        raise device_not_found(device_name) from exc
    except DeviceError as exc:
        raise device_error(exc, device_name) from exc
    except RuntimeError as exc:
        raise OperationError.of(ErrorCode.CONFLICT, str(exc)) from exc


class InitializeDeviceOperation:
    Request: ClassVar[type[BaseModel]] = INITIALIZE_DEVICE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = INITIALIZE_DEVICE_ROUTE.response
    binding: ClassVar[RestBinding] = INITIALIZE_DEVICE_ROUTE.binding

    def __init__(self, *, runtime: ISystemRuntime, devices: IDeviceCommandRunner) -> None:
        self._runtime = runtime
        self._devices = devices

    async def run(self, req: InitializeDeviceRequest) -> InitializeDeviceResponse:
        await _lifecycle(self._runtime, self._devices, req.device_id, "initialize", req.reason)
        return InitializeDeviceResponse(device_name=req.device_id)


# -- ConnectDevice / DisconnectDevice ---------------------------------------


class ConnectDeviceOperation:
    """Take a device without readying it: opens the link, moves nothing."""
    Request: ClassVar[type[BaseModel]] = CONNECT_DEVICE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = CONNECT_DEVICE_ROUTE.response
    binding: ClassVar[RestBinding] = CONNECT_DEVICE_ROUTE.binding

    def __init__(self, *, runtime: ISystemRuntime, devices: IDeviceCommandRunner) -> None:
        self._runtime = runtime
        self._devices = devices

    async def run(self, req: ConnectDeviceRequest) -> ConnectDeviceResponse:
        await _lifecycle(self._runtime, self._devices, req.device_id, "connect", req.reason)
        return ConnectDeviceResponse(device_name=req.device_id)


class DisconnectDeviceOperation:
    """Hand a device back without homing it. Drops motor power where the device holds it."""
    Request: ClassVar[type[BaseModel]] = DISCONNECT_DEVICE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = DISCONNECT_DEVICE_ROUTE.response
    binding: ClassVar[RestBinding] = DISCONNECT_DEVICE_ROUTE.binding

    def __init__(self, *, runtime: ISystemRuntime, devices: IDeviceCommandRunner) -> None:
        self._runtime = runtime
        self._devices = devices

    async def run(self, req: DisconnectDeviceRequest) -> DisconnectDeviceResponse:
        await _lifecycle(self._runtime, self._devices, req.device_id, "disconnect", req.reason)
        return DisconnectDeviceResponse(device_name=req.device_id)


# -- ReconcileDeck ------------------------------------------------------------


class ReconcileDeckOperation:
    """Re-seed a liquid handler from the world model: layout + occupancy.

    A state push, no motion. The operator's lever after a driver session was
    rebuilt outside orca (touchscreen cancel_run + initialize, driver-internal
    recovery), so a RETRY can find its labware again.
    """
    Request: ClassVar[type[BaseModel]] = RECONCILE_DECK_ROUTE.request
    Response: ClassVar[type[BaseModel]] = RECONCILE_DECK_ROUTE.response
    binding: ClassVar[RestBinding] = RECONCILE_DECK_ROUTE.binding

    def __init__(self, *, runtime: ISystemRuntime, devices: IDeviceCommandRunner) -> None:
        self._runtime = runtime
        self._devices = devices

    async def run(self, req: ReconcileDeckRequest) -> ReconcileDeckResponse:
        try:
            # Held as every operator command is, so the engine cannot take the deck while it is pushed.
            with _one_mode(self._runtime):
                async with self._devices.hold(req.device_id):
                    comparison = await self._runtime.devices.reconcile_deck(
                        req.device_id, reason=req.reason, confirm=True,
                    )
        except DeviceUnknownError as exc:
            raise device_not_found(req.device_id) from exc
        except DeviceError as exc:
            raise device_error(exc, req.device_id) from exc
        except KeyError as exc:
            raise device_not_found(req.device_id) from exc
        except ValueError as exc:
            raise OperationError.validation_error(str(exc)) from exc
        if comparison is None:
            return ReconcileDeckResponse(device_name=req.device_id)
        return ReconcileDeckResponse(
            device_name=req.device_id,
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
            detail=conflict.detail,
        )
        for conflict in comparison.disagreements
    ]


class TakeDeviceControlOperation:
    """Claim a device for hands-on work until it is released.

    The gateway takes external control only while an operator's commands are
    open, and gives it back when the last one ends. A workflow can start a move
    into the device between two of them. This claim stands until someone
    releases it.
    """
    Request: ClassVar[type[BaseModel]] = TAKE_DEVICE_CONTROL_ROUTE.request
    Response: ClassVar[type[BaseModel]] = TAKE_DEVICE_CONTROL_ROUTE.response
    binding: ClassVar[RestBinding] = TAKE_DEVICE_CONTROL_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(
        self, req: TakeDeviceControlRequest,
    ) -> TakeDeviceControlResponse:
        try:
            await self._runtime.devices.take_external_control(
                req.device_id, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise device_not_found(req.device_id) from exc
        return TakeDeviceControlResponse(device_name=req.device_id)


class ReleaseDeviceControlOperation:
    """Hand a device back to the workflow. Idempotent."""
    Request: ClassVar[type[BaseModel]] = RELEASE_DEVICE_CONTROL_ROUTE.request
    Response: ClassVar[type[BaseModel]] = RELEASE_DEVICE_CONTROL_ROUTE.response
    binding: ClassVar[RestBinding] = RELEASE_DEVICE_CONTROL_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(
        self, req: ReleaseDeviceControlRequest,
    ) -> ReleaseDeviceControlResponse:
        try:
            await self._runtime.devices.release_external_control(
                req.device_id, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise device_not_found(req.device_id) from exc
        return ReleaseDeviceControlResponse(device_name=req.device_id)


class ClearDeviceFaultOperation:
    """Say a device has been looked at after a command left it part-way through.

    A command that fails, times out, or is cancelled after it went out on the
    wire can leave the instrument mid-motion. The device is faulted from that
    moment and the workflow cannot drive it. Nothing lifts that on its own: a
    later command succeeding proves the machine answers, never that the plate it
    half-moved is where the ledger says it is.
    """
    Request: ClassVar[type[BaseModel]] = CLEAR_DEVICE_FAULT_ROUTE.request
    Response: ClassVar[type[BaseModel]] = CLEAR_DEVICE_FAULT_ROUTE.response
    binding: ClassVar[RestBinding] = CLEAR_DEVICE_FAULT_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(
        self, req: ClearDeviceFaultRequest,
    ) -> ClearDeviceFaultResponse:
        try:
            cleared = await self._runtime.devices.clear_fault(
                req.device_id, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise device_not_found(req.device_id) from exc
        if cleared is None:
            return ClearDeviceFaultResponse(
                status="no_fault", device_name=req.device_id,
            )
        return ClearDeviceFaultResponse(
            status="cleared",
            device_name=req.device_id,
            cleared=DeviceFaultModel.model_validate(dataclasses.asdict(cleared)),
        )


class CompareDeckOperation:
    """Ask a liquid handler whether its deck and the ledger still agree.

    Never changes the ledger. The reconcile that follows would overwrite the driver's answer,
    so this is how an operator sees a disagreement before deciding which side is
    right. Files nothing: the reconcile is the call that acts, so it is the one
    that files. A plate the ledger has in the jaws always disagrees, because a
    deck addresses labware by site and the jaws are not one.
    """
    Request: ClassVar[type[BaseModel]] = COMPARE_DECK_ROUTE.request
    Response: ClassVar[type[BaseModel]] = COMPARE_DECK_ROUTE.response
    binding: ClassVar[RestBinding] = COMPARE_DECK_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: CompareDeckRequest) -> CompareDeckResponse:
        try:
            comparison = await self._runtime.devices.compare_deck(
                req.device_id,
            )
        except KeyError as exc:
            raise device_not_found(req.device_id) from exc
        except ValueError as exc:
            raise OperationError.validation_error(str(exc)) from exc
        return CompareDeckResponse(
            device_name=comparison.device_name,
            agrees=comparison.agrees,
            driver_deck_empty=comparison.driver_deck_empty,
            disagreements=_disagreement_dtos(comparison),
            interrupted_move_labware=comparison.interrupted_move_labware,
        )


class GetMountedTipsOperation:
    Request: ClassVar[type[BaseModel]] = GET_MOUNTED_TIPS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = GET_MOUNTED_TIPS_ROUTE.response
    binding: ClassVar[RestBinding] = GET_MOUNTED_TIPS_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: GetMountedTipsRequest) -> GetMountedTipsResponse:
        try:
            mounted = await self._runtime.devices.get_mounted_tips(req.device_id)
        except KeyError as exc:
            raise device_not_found(req.device_id) from exc
        return GetMountedTipsResponse(
            device_name=req.device_id,
            mounted=[
                MountedTipReadDTO(
                    channel=channel, tip_rack=tip.tip_rack, position=tip.position, head=tip.head.value,
                    channel_is_inferred=tip.channel_is_inferred,
                )
                for carried in (mounted.by_channel, mounted.head96)
                for channel, tip in sorted(carried.items())
            ],
            provenance=mounted.provenance.value.lower(),
        )


class SetMountedTipsOperation:
    Request: ClassVar[type[BaseModel]] = SET_MOUNTED_TIPS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = SET_MOUNTED_TIPS_ROUTE.response
    binding: ClassVar[RestBinding] = SET_MOUNTED_TIPS_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: SetMountedTipsRequest) -> MountedTipsMutationResponse:
        try:
            await self._runtime.devices.set_mounted_tips(
                req.device_id,
                {m.channel: (m.tip_rack, m.position) for m in req.mounted if m.head == "channels"},
                head96={m.channel: (m.tip_rack, m.position) for m in req.mounted if m.head == "head96"},
                reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise device_not_found(req.device_id) from exc
        except ValueError as exc:
            raise OperationError.validation_error(str(exc)) from exc
        return MountedTipsMutationResponse(device_name=req.device_id)


class ConfirmMountedTipsOperation:
    Request: ClassVar[type[BaseModel]] = CONFIRM_MOUNTED_TIPS_ROUTE.request
    Response: ClassVar[type[BaseModel]] = CONFIRM_MOUNTED_TIPS_ROUTE.response
    binding: ClassVar[RestBinding] = CONFIRM_MOUNTED_TIPS_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: ConfirmMountedTipsRequest) -> MountedTipsMutationResponse:
        try:
            await self._runtime.devices.confirm_mounted_tips(
                req.device_id, reason=req.reason, confirm=True,
            )
        except KeyError as exc:
            raise device_not_found(req.device_id) from exc
        except ConfirmRefusedError as exc:
            raise confirm_refused(exc, device_name=req.device_id) from exc
        except ValueError as exc:
            raise OperationError.validation_error(str(exc)) from exc
        return MountedTipsMutationResponse(device_name=req.device_id)


class SetDeviceModeOperation:
    """Set a device to sim or live. Refused during a run; a device with no real driver cannot go live."""
    Request: ClassVar[type[BaseModel]] = SET_DEVICE_MODE_ROUTE.request
    Response: ClassVar[type[BaseModel]] = SET_DEVICE_MODE_ROUTE.response
    binding: ClassVar[RestBinding] = SET_DEVICE_MODE_ROUTE.binding

    def __init__(self, runtime: ISystemRuntime):
        self._runtime = runtime

    async def run(self, req: SetDeviceModeRequest) -> SetDeviceModeResponse:
        try:
            switch = await self._runtime.switch_device_mode(req.device_id, req.mode, confirm=req.confirm)
        except KeyError as exc:
            raise device_not_found(req.device_id) from exc
        except DeviceHasNoRealDriverError as exc:
            raise OperationError.of(
                ErrorCode.DEVICE_HAS_NO_REAL_DRIVER,
                str(exc),
                device_name=req.device_id,
            ) from exc
        except ModeSwitchDuringRunError as exc:
            raise OperationError.of(
                ErrorCode.MODE_SWITCH_DURING_RUN,
                str(exc),
                device_name=req.device_id,
                execution_ids=[run.execution_id for run in exc.runs],
            ) from exc
        except DeviceLockedError as exc:
            raise device_error(exc, req.device_id) from exc
        return SetDeviceModeResponse(
            device_name=req.device_id,
            mode=req.mode,
            switched=switch.switched,
            warning=switch.warning,
            bring_up_error=switch.bring_up_error,
        )


DEVICE_LIFECYCLE_OPERATIONS = (
    InitializeDeviceOperation,
    ConnectDeviceOperation,
    DisconnectDeviceOperation,
    ReconcileDeckOperation,
)


DEVICE_OPERATIONS = (
    SetDeviceModeOperation,
    TakeDeviceControlOperation,
    ReleaseDeviceControlOperation,
    ClearDeviceFaultOperation,
    CompareDeckOperation,
    GetMountedTipsOperation,
    SetMountedTipsOperation,
    ConfirmMountedTipsOperation,
)


LIFECYCLE_OPERATIONS: dict[str, RestBinding] = {
    "initialize": InitializeDeviceOperation.binding,
    "connect": ConnectDeviceOperation.binding,
    "disconnect": DisconnectDeviceOperation.binding,
}
"""Each lifecycle command, and the route of the Operation that sends it and then brings the deck in line."""
