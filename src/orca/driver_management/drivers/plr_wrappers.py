import asyncio
from orca.resource_models.resource_extras.teachpoints import Teachpoint
from pylabrobot.arms.backend import ArmBackend as PLRArmBackend
from pylabrobot.arms.coords import CartesianCoords


from typing import List

from pylabrobot.sealing.backend import SealerBackend as PLRSealerBackend
from pylabrobot.shaking.backend import ShakerBackend as PLRShakerBackend
from pylabrobot.centrifuge.backend import CentrifugeBackend as PLRCentrifugeBackend


class PLRTransporterBackendWrapper:
    def __init__(self, backend: PLRArmBackend) -> None:
        self._backend = backend
        self._positions: dict[str, Teachpoint] = {}
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

    async def pick(self, position_name: str, labware_type: str) -> None:
        tp = self._positions.get(position_name)
        if tp is None:
            raise ValueError(f"The position '{position_name}' is not taught for {self.name}")
        coords = CartesianCoords(x=tp.x, y=tp.y, z=tp.z, yaw=tp.yaw, pitch=tp.pitch, roll=tp.roll)
        await self._backend.pick_plate(coords, tp.approach_height)

    async def place(self, position_name: str, labware_type: str) -> None:
        teachpoint = self._positions.get(position_name)
        if teachpoint is None:
            raise ValueError(f"The position '{position_name}' is not taught for {self.name}")
        coords = CartesianCoords(x=teachpoint.x, y=teachpoint.y, z=teachpoint.z, yaw=teachpoint.yaw, pitch=teachpoint.pitch, roll=teachpoint.roll)
        await self._backend.place_plate(coords, teachpoint.approach_height)

    def get_taught_positions(self) -> List[str]:
        return list(self._positions.keys())

    def load_positions(self, positions: List[Teachpoint]) -> None:
        """Load taught positions from a list of Teachpoint objects."""
        self._positions = {tp.name: tp for tp in positions}


class PLRSealerBackendWrapper:
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


class PLRShakerBackendWrapper:
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

class PLRCentrifugeBackendWrapper:
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
        await self._backend.start_spin_cycle(g, duration, self._acceleration)
    
    async def open(self) -> None:
        await self._backend.open_door()

    async def close(self) -> None:
        await self._backend.close_door()