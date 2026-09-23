from abc import ABC, abstractmethod
from typing import Any, Dict, List, Sequence

from cheshire_drivers.labware_interfaces import IContainer, IPlate, ITipRack, ITipSpot, ITrough, IWell
from cheshire_drivers.liquid_handler_models import (
    LabwareStateResponse,
    ReconcileHardwareStateResponse,
)
from cheshire_drivers.pipetting import MixParams, PipettingProfile
from cheshire_drivers.thermocycler_models import Protocol

from orca.resource_models.tracking_interpreter import IOperationInterpreter


class ITrackedDevice(ABC):
    """Devices whose actions emit ops_history records via a tracking interpreter.

    Subclasses declare which interpreter to use via the ``operation_interpreter``
    classmethod. The location-action dispatcher walks the device's MRO and
    selects the first ITrackedDevice subclass that explicitly overrides
    ``operation_interpreter``. The build-time guard in ``orca.sdk.build`` fails
    deployment if any registered ITrackedDevice resolves to ``DefaultInterpreter``;
    that prevents future tracked interfaces from silently dropping records.
    """

    @classmethod
    @abstractmethod
    def operation_interpreter(cls) -> IOperationInterpreter:
        """Return an IOperationInterpreter for commands routed to this device."""
        ...


class IGenericExecutable(ABC):
    @abstractmethod
    async def execute(self, command: str, options: Dict[str, Any]) -> None:
        """Execute a command with the driver."""
        ...

class ITempSettable(ABC):
    @abstractmethod
    async def set_temperature(self, temperature: float) -> None:
        """Set the temperature of the device."""
        ...


class ITempGettable(ABC):
    @abstractmethod
    async def get_temperature(self) -> float:
        """Get the current temperature of the device."""
        ...


class ISealer(ABC):
    @abstractmethod
    async def seal(self, temperature: int, duration: float) -> None:
        """Seal the plate at a specified temperature and duration."""
        ...


class IProtocolRunner(ABC):
    @abstractmethod
    async def run_protocol(self, protocol_filepath: str, params: Dict[str, Any]) -> None:
        """Execute a protocol run command."""
        ...    
    

class IShaker(ABC):
    @abstractmethod
    async def shake(self, duration: int, speed: int) -> None:
        """Shake the device for a specified duration and speed."""
        ...


class ICentrifuge(ABC):
    @abstractmethod
    async def centrifuge(self, g: int, duration: int) -> None:
        """Spin the centrifuge at a relative centrifugal force (g) for a duration in seconds."""
        ...  

  
class IThermocycler(ABC):
    @abstractmethod
    async def run_protocol(self, protocol: Protocol, block_max_volume: float) -> None:
        """Run a thermal protocol (stages of temperature steps)."""
        ...

    @abstractmethod
    async def open_lid(self) -> None:
        """Open the thermocycler lid."""
        ...

    @abstractmethod
    async def close_lid(self) -> None:
        """Close the thermocycler lid."""
        ...

    @abstractmethod
    async def set_block_temperature(self, temperature: List[float]) -> None:
        """Set the block temperature (one value per zone)."""
        ...

    @abstractmethod
    async def set_lid_temperature(self, temperature: List[float]) -> None:
        """Set the lid temperature (one value per zone)."""
        ...

    @abstractmethod
    async def deactivate_block(self) -> None:
        """Deactivate block temperature control."""
        ...

    @abstractmethod
    async def deactivate_lid(self) -> None:
        """Deactivate lid temperature control."""
        ...

    @abstractmethod
    async def get_block_current_temperature(self) -> List[float]:
        """Get the current block temperature per zone."""
        ...

    @abstractmethod
    async def get_block_target_temperature(self) -> List[float]:
        """Get the block target temperature per zone."""
        ...

    @abstractmethod
    async def get_lid_current_temperature(self) -> List[float]:
        """Get the current lid temperature per zone."""
        ...

    @abstractmethod
    async def get_lid_target_temperature(self) -> List[float]:
        """Get the lid target temperature per zone."""
        ...

    @abstractmethod
    async def get_lid_open(self) -> bool:
        """Return whether the lid is open."""
        ...

    @abstractmethod
    async def get_lid_status(self) -> str:
        """Get the lid temperature status."""
        ...

    @abstractmethod
    async def get_block_status(self) -> str:
        """Get the block temperature status."""
        ...

    @abstractmethod
    async def get_hold_time(self) -> float:
        """Get the remaining hold time in seconds."""
        ...

    @abstractmethod
    async def get_current_cycle_index(self) -> int:
        """Get the zero-based index of the current cycle."""
        ...

    @abstractmethod
    async def get_total_cycle_count(self) -> int:
        """Get the total cycle count."""
        ...

    @abstractmethod
    async def get_current_step_index(self) -> int:
        """Get the zero-based index of the current step within the cycle."""
        ...

    @abstractmethod
    async def get_total_step_count(self) -> int:
        """Get the total number of steps in the current cycle."""
        ...


class IReader(ABC):
    @abstractmethod
    async def read(self, protocol_filepath: str, output_filepath: str) -> None:
        """Read data using the specified protocol and save results to a file."""
        ...


class IDelidder(ABC):
    @abstractmethod
    async def delid(self) -> None:
        """Remove the lid from the specified labware."""
        ...


class IPlateWasher(IProtocolRunner, ABC):
    pass


class ILiquidHandler(ITrackedDevice, ABC):
    """Well-level liquid handling operations.

    Used with ctx.device(ILiquidHandler) in @orca.action for typed
    access to aspirate/dispense/tip management. Devices that execute a
    vendor protocol file (e.g. Hamilton Venus, Tecan EVOware) should
    implement IProtocolRunner directly instead; a LiquidHandler device
    can implement both when a driver supports both modes.

    Methods return LabwareStateResponse from the driver. PLR-driven sims
    (Chatterbox) populate labware_state with per-well/per-tip state; protocol-only
    or disconnected drivers may return an empty state. Workflow code may ignore
    the return; tracker integration consumes it.
    """

    @classmethod
    def operation_interpreter(cls) -> IOperationInterpreter:
        from orca.plugins.liquid_handler_interpreter import LiquidHandlerInterpreter
        return LiquidHandlerInterpreter()

    @abstractmethod
    async def aspirate(
        self,
        containers: IContainer | Sequence[IContainer],
        volumes: List[float],
        flow_rates: List[float] | None = None,
        offsets_z: List[float] | None = None,
        use_channels: List[int] | None = None,
        liquid_class: PipettingProfile | None = None,
        technique: PipettingProfile | None = None,
    ) -> LabwareStateResponse:
        """Aspirate from wells (plate) or a single container (trough). Pass a
        single ``IContainer`` to draw from it on every channel (count = len
        volumes), or a per-channel sequence of containers."""
        ...

    @abstractmethod
    async def dispense(
        self,
        containers: IContainer | Sequence[IContainer],
        volumes: List[float],
        flow_rates: List[float] | None = None,
        offsets_z: List[float] | None = None,
        use_channels: List[int] | None = None,
        liquid_class: PipettingProfile | None = None,
        technique: PipettingProfile | None = None,
    ) -> LabwareStateResponse:
        """Dispense into wells (plate) or a single container (trough). See
        ``aspirate`` for the single-vs-sequence container forms."""
        ...

    @abstractmethod
    async def pick_up_tips(self, tip_spots: List[ITipSpot]) -> LabwareStateResponse: ...

    @abstractmethod
    async def drop_tips(self, tip_spots: List[ITipSpot]) -> LabwareStateResponse: ...

    @abstractmethod
    async def discard_tips(self, use_channels: List[int] | None = None) -> LabwareStateResponse: ...

    @abstractmethod
    async def reconcile_hardware_state(self) -> ReconcileHardwareStateResponse:
        """Re-read hardware ground truth (session liveness, tip sensors) and repair
        the driver's cached state where a sensor is definitive; moves nothing."""
        ...

    @abstractmethod
    async def discard_stranded_tips(self) -> ReconcileHardwareStateResponse:
        """Trash tips only the hardware knows about. MOVES the robot."""
        ...

    @abstractmethod
    async def mix(
        self,
        containers: IContainer | Sequence[IContainer],
        params: MixParams,
        use_channels: List[int] | None = None,
        liquid_class: PipettingProfile | None = None,
        technique: PipettingProfile | None = None,
    ) -> LabwareStateResponse:
        """Mix in wells (plate) or a single container (trough). For a trough,
        pass the container and ``use_channels`` (the channel count).

        ``params`` names the mix itself and its rate, which is narrower than the
        two profiles and wins over them; everything else about how the cycles are
        pipetted comes from those."""
        ...

    # --- 96-head operations: typed labware, never name strings (the driver
    # deck is instance-name keyed; the bridge reads .name off the object) ---

    @abstractmethod
    async def aspirate96(
        self,
        labware: IPlate | ITrough,
        volume: float,
        flow_rate: float | None = None,
        liquid_height: float | None = None,
    ) -> LabwareStateResponse: ...

    @abstractmethod
    async def dispense96(
        self,
        labware: IPlate | ITrough,
        volume: float,
        flow_rate: float | None = None,
        liquid_height: float | None = None,
    ) -> LabwareStateResponse: ...

    @abstractmethod
    async def pick_up_tips96(self, tip_rack: ITipRack) -> LabwareStateResponse: ...

    @abstractmethod
    async def drop_tips96(self, tip_rack: ITipRack | None = None) -> LabwareStateResponse: ...

    @abstractmethod
    async def return_tips96(self) -> LabwareStateResponse: ...


class IStorage(ABC):
    pass


class IWaste(IStorage, ABC):
    pass


class IPlateSource(ABC):
    """Devices that DISPENSE plates rather than serve as a single-slot stage.

    A stacker holds many plates physically; its output position being
    occupied does NOT mean "the stage is blocked," it means "next plate is
    ready to be picked, more are queued behind it."

    Used by the pre-submit start_location check
    (``_validate_start_locations``) to skip occupancy refusal for plate-
    source-backed start_locations, and by DispenseSpawn to
    physically advance the queue before fabricating the orca-side
    ``LabwareInstance`` for a thread starting at this resource.

    Implementations forward to the driver's wire-level ``dispense()``
    command. See ``Storage.dispense`` for the canonical wrapper shape:
    ``await self.driver.dispense()``.
    """

    @abstractmethod
    async def dispense(self) -> None:
        """Release one plate from the source's queue to the output position.

        Wire-level command for IPlateSource-style devices. The device knows
        only how to advance its internal queue; orca-side concerns
        (fabricating the LabwareInstance, slot writes, observer fan-out)
        live in :class:`DispenseSpawn` above this call.
        """
        ...