from abc import ABC, abstractmethod

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar, runtime_checkable

from cheshire_drivers.command_responses import EmptyCommandResponse
from cheshire_drivers.command_table import COMMANDS
from cheshire_drivers.command_timings import collect_command_timings

from orca.gateway.controller.command_pipeline import IDeviceCommandRunner
from orca.runtime.recoverable_timeout import time_limit, within_the_time_limit
from orca.modes.device_modes import DeviceMode, IDeviceModes
from orca.resource_models.tracked_lock import TrackedLock
from orca.modes.run_modes import BaseMode, WorkflowRunMode, out_of_run

orca_logger = logging.getLogger("orca")

T = TypeVar("T")


class IResource(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError


def _unbounded(_command: str) -> float | None:
    return None


def engine_time_limit(driver_cls: type, command: str) -> float | None:
    """The limit a run puts on one engine command; None leaves it unbounded.

    A command that answers with a value stays unbounded: settling its timeout as complete gives no value to read.
    """
    found = COMMANDS.find(getattr(driver_cls, "interfaces", frozenset()), command)
    if found is not None and found.response_model is not EmptyCommandResponse:
        return None
    timing = collect_command_timings(driver_cls).get(command)
    return time_limit(
        None if timing is None else timing.max_seconds, None if timing is None else timing.typical_seconds, None,
    )


class EngineCommandLog:
    """Runs the commands the engine sends a device's driver outside any action, through the system's pipeline.

    Inside a run thread each one runs under the run's recoverable timeout at the limit `limit_of` gives, as an
    action's device call does.
    """

    def __init__(self, device_name: str, limit_of: Callable[[str], float | None] = _unbounded) -> None:
        self._device_name = device_name
        self._limit_of = limit_of
        self._wiring: tuple[Callable[[str], Awaitable[None]], IDeviceCommandRunner] | None = None

    def save_with(self, save: Callable[[str], Awaitable[None]], pipeline: IDeviceCommandRunner) -> None:
        self._wiring = (save, pipeline)

    @property
    def pipeline(self) -> IDeviceCommandRunner:
        """The system pipeline this device's commands run through."""
        _, pipeline = self._wired("a command")
        return pipeline

    async def run(
        self, command: str, call: Callable[[], Awaitable[T]], *, carries_out: tuple[str, str] | None = None,
    ) -> T:
        return await self._run(command, call, carries_out=carries_out, world_upkeep=False)

    async def keep_world(self, command: str, call: Callable[[], Awaitable[T]]) -> T:
        """A command that lays out, rebuilds or reads this driver's own world: it is how the world is kept, so the
        world is not rebuilt before it."""
        return await self._run(command, call, carries_out=None, world_upkeep=True)

    async def _run(
        self, command: str, call: Callable[[], Awaitable[T]], *, carries_out: tuple[str, str] | None,
        world_upkeep: bool,
    ) -> T:
        save, pipeline = self._wired(command)
        limit = self._limit_of(command)
        return await pipeline.run_engine_command(
            self._device_name, command, lambda: within_the_time_limit(self._device_name, command, limit, call),
            lambda: save(command), carries_out=carries_out, world_upkeep=world_upkeep,
        )

    def _wired(self, command: str) -> tuple[Callable[[str], Awaitable[None]], IDeviceCommandRunner]:
        if self._wiring is None:
            raise RuntimeError(
                f"{command!r} was not sent to {self._device_name!r}: it is in no system, so nothing could save it"
            )
        return self._wiring


class IModeAware(ABC):
    """A resource that can say which world it dispatches in.

    Nominal rather than structural on purpose: whether a command reaches real
    hardware turns on this check, and a duck-typed one silently accepts a test
    double whose answer is a mock object.
    """

    @abstractmethod
    async def mode_under(self, base: WorkflowRunMode) -> WorkflowRunMode:
        """The mode this resource dispatches under if `base` is in force."""
        raise NotImplementedError

    @abstractmethod
    async def effective_mode(self) -> WorkflowRunMode:
        """`mode_under` applied to whatever base is in force right now."""
        raise NotImplementedError

    @property
    @abstractmethod
    def engine_commands(self) -> EngineCommandLog:
        """Where this resource saves the commands the engine sends its driver outside any action."""
        raise NotImplementedError

    @abstractmethod
    def use_device_modes(self, modes: IDeviceModes, base_mode: BaseMode = out_of_run) -> None:
        """Read this resource's sim or live setting from `modes` from now on, against `base_mode` outside a run."""
        raise NotImplementedError

    @abstractmethod
    async def setting(self) -> DeviceMode:
        """This resource's sim or live setting."""
        raise NotImplementedError


@runtime_checkable
class ILabwareStateHolder(Protocol):
    """The single contract for a holder of labware-projection state.

    A holder owns a derived view of where labware sits (a transporter world
    graph, a liquid-handler deck tree). `clear_all_labware` drives
    `reset_labware_state_everywhere` across every holder (devices +
    transporters) so the projection reconciles to the engine ledger from one
    place instead of N bespoke reset paths. Stateless holders (most devices)
    no-op.

    Two verbs because a holder can have more than one projection. A sim driver
    and a live one are separate worlds, and a reset in one must not disarm the
    other's identity guard, so `reset_labware_state` means the world the caller
    is dispatching in. The panic button means all of them.
    """

    @property
    def name(self) -> str: ...

    async def reset_labware_state(self) -> None: ...

    async def reset_labware_state_everywhere(self) -> None: ...


class IInitializable(ABC):
    @property
    @abstractmethod
    def is_initialized(self) -> bool:
        """
        Check if the driver is initialized.
        Returns:
            bool: True if the driver is initialized, False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    async def initialize(self) -> None:
        """
        Initialize the driver.
        """
        raise NotImplementedError


class IConnectable(ABC):
    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """
        Check if the driver is connected.
        Returns:
            bool: True if the driver is connected, False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    async def connect(self) -> None:
        """
        Connect to the driver.
        """
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        """
        Disconnect from the driver.
        """
        raise NotImplementedError


class IDevice(IResource, IInitializable, ABC):
    @property
    def lock(self) -> TrackedLock:
        """The lock serializing driver calls to this device."""
        raise NotImplementedError

    @property
    @abstractmethod
    def in_use(self) -> bool:
        """ Check if the device is running."""
        raise NotImplementedError
