import asyncio
from typing import List

from orca.driver_management.drivers.driver_interfaces import ICentrifugeDriver, ISealerDriver, IShakerDriver, ITransporterDriver
from pylabrobot.sealing.backend import SealerBackend as PLRSealerBackend
from pylabrobot.shaking.backend import ShakerBackend as PLRShakerBackend
from pylabrobot.centrifuge.backend import CentrifugeBackend as PLRCentrifugeBackend

from pylabrobot.arms.backend import ArmBackend as PLRArmBackend, VerticalAccess, HorizontalAccess
from pylabrobot.arms.coords import CartesianCoords as PLRCartesianCoords, ElbowOrientation

from pylabrobot.resources import Coordinate, Rotation

from orca.resource_models.resource_extras.teachpoints import Teachpoint, TeachpointsRegistry


def convert_teachpoint_to_plr_coord(teachpoint: Teachpoint):
    """Convert Orca Teachpoint to PyLabRobot CartesianCoords.

    Args:
        teachpoint: Orca teachpoint with coordinates and orientation

    Returns:
        PLRCartesianCoords with proper Coordinate, Rotation, and ElbowOrientation

    Raises:
        ValueError: If orientation is provided but is not 'left' or 'right' (case-insensitive)
    """
    c = teachpoint.coordinates

    # Handle orientation (case-insensitive, optional)
    elbow = None
    if teachpoint.orientation is not None:
        orientation_lower = teachpoint.orientation.lower()
        if orientation_lower == "left":
            elbow = ElbowOrientation.LEFT
        elif orientation_lower == "right":
            elbow = ElbowOrientation.RIGHT
        else:
            raise ValueError(f"Invalid orientation '{teachpoint.orientation}'. Must be 'left' or 'right' (case-insensitive).")

    return PLRCartesianCoords(
        Coordinate(c.x, c.y, c.z),
        Rotation(c.roll, c.pitch, c.yaw),
        elbow
    )

class PLRTransporterBackendWrapper(ITransporterDriver):
    def __init__(self, backend: PLRArmBackend) -> None:
        self._backend = backend
        self._teachpoints = TeachpointsRegistry()
        self._is_initialized = False

    @property
    def name(self) -> str:
        """Returns the name of the transporter."""
        return type(self._backend).__name__

    @property
    def is_initialized(self) -> bool:
        """Returns whether the transporter is initialized or not."""
        return self._is_initialized

    async def initialize(self) -> None:
        """Initializes the transporter."""
        await self._backend.setup()
        self._is_initialized = True

    async def home(self) -> None:
        """Homes the transporter."""
        await self._backend.home()

    async def move_to_safe(self) -> None:
        """Moves the transporter to a safe position."""
        await self._backend.move_to_safe()

    def _teachpoint_to_plr_access(self, tp: Teachpoint):
        """Convert teachpoint access parameters to PLR AccessPattern.

        Args:
            tp: Teachpoint with access configuration

        Returns:
            VerticalAccess or HorizontalAccess based on access_type

        Raises:
            ValueError: If access_type is invalid or required parameters are None
        """
        # Validate access_type
        if tp.access_type is None:
            raise ValueError(f"Teachpoint '{tp.name}' has no access_type specified. Must be 'vertical' or 'horizontal'.")

        access_type_lower = tp.access_type.lower()

        # Validate common parameter
        if tp.gripper_offset is None:
            raise ValueError(f"Teachpoint '{tp.name}' has no gripper_offset specified.")

        if access_type_lower == "vertical":
            # Validate vertical-specific parameters
            if tp.retract_distance is None:
                raise ValueError(f"Teachpoint '{tp.name}' (vertical access) has no retract_distance specified.")

            return VerticalAccess(
                approach_height_mm=tp.retract_distance,
                clearance_mm=tp.retract_distance,
                gripper_offset_mm=tp.gripper_offset
            )
        elif access_type_lower == "horizontal":
            # Validate horizontal-specific parameters
            if tp.vertical_clearance is None:
                raise ValueError(f"Teachpoint '{tp.name}' (horizontal access) has no vertical_clearance specified.")
            if tp.z_above is None:
                raise ValueError(f"Teachpoint '{tp.name}' (horizontal access) has no z_above specified.")

            return HorizontalAccess(
                approach_distance_mm=tp.vertical_clearance,
                clearance_mm=tp.vertical_clearance,
                lift_height_mm=tp.z_above,
                gripper_offset_mm=tp.gripper_offset
            )
        else:
            raise ValueError(f"Invalid access_type '{tp.access_type}' for teachpoint '{tp.name}'. Must be 'vertical' or 'horizontal'.")

    async def pick(self, position_name: str, labware_type: str) -> None:
        tp = self._teachpoints.get(position_name)
        if tp is None:
            raise ValueError(f"The position '{position_name}' is not taught for {self.name}")
        coords = convert_teachpoint_to_plr_coord(tp)
        access = self._teachpoint_to_plr_access(tp)
        await self._backend.pick_plate(coords, access)

    async def place(self, position_name: str, labware_type: str) -> None:
        tp = self._teachpoints.get(position_name)
        if tp is None:
            raise ValueError(f"The position '{position_name}' is not taught for {self.name}")
        coords = convert_teachpoint_to_plr_coord(tp)
        access = self._teachpoint_to_plr_access(tp)
        await self._backend.place_plate(coords, access)

    def get_teachpoints(self) -> List[Teachpoint]:
        return self._teachpoints.list()

    def load_teachpoints(self, teachpoints: List[Teachpoint]) -> None:
        """Load taught positions from a list of Teachpoint objects."""
        self._teachpoints.clear()
        [self._teachpoints.add(t) for t in teachpoints]

class PLRSealerBackendWrapper(ISealerDriver):
    def __init__(self, backend: PLRSealerBackend):
        self._backend = backend
        self._is_initialized = False

    async def initialize(self) -> None:
        await self._backend.setup()
        self._is_initialized = True

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    async def open(self) -> None:
        await self._backend.open()

    async def close(self) -> None:
        await self._backend.close()

    async def seal(self, temperature: int, duration: float) -> None:
        await self._backend.seal(temperature=temperature, duration=duration)

    async def set_temperature(self, temperature: float) -> None:
        await self._backend.set_temperature(temperature)

    async def get_temperature(self) -> float:
        return await self._backend.get_temperature()


class PLRShakerBackendWrapper(IShakerDriver):
    def __init__(self, backend: PLRShakerBackend):
        self._backend = backend
        self._is_initialized = False

    async def initialize(self) -> None:
        await self._backend.setup()
        self._is_initialized = True

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    async def stop(self) -> None:
        await self._backend.stop()

    def serialize(self) -> dict:
        return {"type": self.__class__.__name__}

    @property
    def supports_locking(self) -> bool:
        """Check if the shaker supports locking the plate"""
        return self._backend.supports_locking

    async def unlock_plate(self) -> None:
        await self._backend.unlock_plate()

    async def lock_plate(self):
        await self._backend.lock_plate()

    async def shake(self, speed: float, duration: float) -> None:
        await self._backend.shake(speed)
        await asyncio.sleep(duration)
        await self.stop_shaking()

    async def stop_shaking(self) -> None:
        await self._backend.stop_shaking()

    async def open(self) -> None:
        await self.unlock_plate()

    async def close(self) -> None:
        await self.lock_plate()

class PLRCentrifugeBackendWrapper(ICentrifugeDriver):
    def __init__(self, backend: PLRCentrifugeBackend):
        self._backend = backend
        self._is_initialized = False
        self._acceleration = 7

    @property
    def is_initialized(self) -> bool:
        return self._is_initialized

    async def initialize(self) -> None:
        await self._backend.setup()
    
    async def stop(self) -> None:
        await self._backend.stop()

    async def set_acceleration(self, acceleration: float) -> None:
        self._acceleration = acceleration

    async def centrifuge(self, g: float, duration: float) -> None:
        """Spin the centrifuge at a specified speed for a specified duration."""
        await self._backend.spin(g, duration, self._acceleration)
    
    async def open(self) -> None:
        await self._backend.open_door()

    async def close(self) -> None:
        await self._backend.close_door()