from orca.modes.device_modes import DeviceMode, IDeviceModes
from orca.modes.run_modes import (
    BaseMode,
    WorkflowRunMode,
    current_run_mode,
    device_run_mode,
    out_of_run,
)


class DeviceRunMode:
    """Where a device's run mode meets its sim or live setting."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._modes: IDeviceModes | None = None
        self._base_mode: BaseMode = out_of_run

    def use_device_modes(self, modes: IDeviceModes, base_mode: BaseMode = out_of_run) -> None:
        self._modes = modes
        self._base_mode = base_mode

    async def setting(self) -> DeviceMode:
        """This device's sim or live setting, read from the database; sim until the runtime hands over the store."""
        return DeviceMode.SIM if self._modes is None else await self._modes.mode_of(self._name)

    async def mode_under(self, base: WorkflowRunMode) -> WorkflowRunMode:
        """The mode this device dispatches under if `base` is the run mode.

        The snapshot facades, the deck worlds and the `effective_mode` stamped on every wire command all
        resolve through it, so they cannot disagree.
        """
        return device_run_mode(base, await self.setting())

    async def effective_mode(self) -> WorkflowRunMode:
        """`mode_under` the run mode in force; outside a run thread, the base the runtime hands over."""
        return await self.mode_under(current_run_mode.get(self._base_mode()))
