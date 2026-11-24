"""Sets each device to sim or live; a device with no real driver stays sim."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from cheshire_drivers.device_setup import DeviceSetup

from orca.resource_models.resources import IModeAware
from orca.modes.device_modes import DeviceMode, IDeviceModes
from orca.resource_models.store_interfaces import IDeviceSetupStore
from orca.modes.run_modes import WorkflowRunMode
from orca.system.system_interface import ISystem


class DeviceHasNoRealDriverError(ValueError):
    def __init__(self, device_name: str) -> None:
        self.device_name = device_name
        super().__init__(
            f"{device_name} has no real driver, so it can only run as sim. Give devices/{device_name}.json a "
            "'real' driver, and remove 'always_sim', to run it live."
        )


@dataclass(frozen=True)
class RunInProgress:
    execution_id: str
    workflow_name: str


class ModeSwitchDuringRunError(RuntimeError):
    def __init__(self, device_name: str, runs: Sequence[RunInProgress]) -> None:
        self.device_name = device_name
        self.runs = tuple(runs)
        named = ", ".join(f"{run.workflow_name} ({run.execution_id})" for run in self.runs)
        super().__init__(
            f"{device_name} cannot switch between sim and live while a run is going: {named}. "
            "Let the run finish, or stop it, then switch."
        )


@dataclass(frozen=True)
class ModeSwitch:
    """`switched` is False when the device already has the mode, or the switch was not confirmed yet."""
    switched: bool
    warning: str | None = None
    bring_up_error: str | None = None


def mode_switch_warning(device_name: str, mode: DeviceMode) -> str:
    """The one wording every surface shows before a switch."""
    bring_up = (
        f"Switching to live initializes the real {device_name}."
        if mode is DeviceMode.LIVE
        else f"Switching to sim brings up {device_name}'s simulator."
    )
    return (
        f"{device_name}'s current state carries over: sim and live share one record, so what stands on "
        f"it now reads stale until someone confirms it. {bring_up}"
    )


def bring_up_error(device_name: str, mode: DeviceMode, cause: BaseException) -> str:
    """What a failed bring-up left: the setting stands, and commands are refused until it comes up."""
    return (
        f"{device_name} is set to {mode.value} but did not come up ({type(cause).__name__}: {cause}). "
        "Commands to it are refused until an initialize or a run's start brings it up."
    )


CAPTURE_IN_SIM_REFUSED = "Capturing in sim would overwrite this device's live positions. Set always_sim if it has none."


class IDeviceModeFacade(Protocol):
    async def mode_of(self, device_name: str) -> DeviceMode: ...

    async def may_capture(self, device_name: str, under: WorkflowRunMode) -> bool:
        """False when the read would come from a simulator and the device has real positions to protect.

        `under` is the base an operator's command resolves against, so a device set to live reads in sim while a sim
        run is active.
        """
        ...

    async def would_change(self, device_name: str, mode: DeviceMode) -> bool:
        """True when `mode` differs from the setting. KeyError for a device the system does not have;
        DeviceHasNoRealDriverError for LIVE on one that cannot."""
        ...

    async def set(self, device_name: str, mode: DeviceMode) -> bool:
        """True when the setting changed. Raises as `would_change` does."""
        ...


def can_run_live(setup: DeviceSetup) -> bool:
    return setup.real is not None and not setup.always_sim


class DeviceModeFacade:
    def __init__(self, modes: IDeviceModes, setups: IDeviceSetupStore, system: ISystem) -> None:
        self._modes = modes
        self._setups = setups
        self._system = system

    async def mode_of(self, device_name: str) -> DeviceMode:
        return await self._modes.mode_of(device_name)

    async def may_capture(self, device_name: str, under: WorkflowRunMode) -> bool:
        if not can_run_live(self._setups.read(device_name)):
            return True
        return under is not WorkflowRunMode.SIM and await self.mode_of(device_name) is DeviceMode.LIVE

    async def would_change(self, device_name: str, mode: DeviceMode) -> bool:
        if not any(r.name == device_name and isinstance(r, IModeAware) for r in self._system.resources):
            raise KeyError(device_name)
        if await self._modes.mode_of(device_name) is mode:
            return False
        if mode is DeviceMode.LIVE and not can_run_live(self._setups.read(device_name)):
            raise DeviceHasNoRealDriverError(device_name)
        return True

    async def set(self, device_name: str, mode: DeviceMode) -> bool:
        if not await self.would_change(device_name, mode):
            return False
        await self._modes.set(device_name, mode)
        return True
