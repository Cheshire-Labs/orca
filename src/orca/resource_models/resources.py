from abc import ABC, abstractmethod

import logging
from typing import Protocol, runtime_checkable

from orca.resource_models.labware_placeable_interface import ILabwarePlaceable
from orca.resource_models.tracked_lock import TrackedLock
from orca.runtime.run_modes import WorkflowRunMode

orca_logger = logging.getLogger("orca")


class IResource(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError


class IModeAware(ABC):
    """A resource that can say which world it dispatches in.

    Nominal rather than structural on purpose: whether a command reaches real
    hardware turns on this check, and a duck-typed one silently accepts a test
    double whose answer is a mock object.
    """

    @abstractmethod
    def mode_under(self, base: WorkflowRunMode) -> WorkflowRunMode:
        """The mode this resource dispatches under if `base` is in force."""
        raise NotImplementedError

    @property
    @abstractmethod
    def effective_mode(self) -> WorkflowRunMode:
        """`mode_under` applied to whatever base is in force right now."""
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
