from typing import Generic, TypeVar
from orca.resource_models.resources import ISimulationable

T = TypeVar("T")
class SimulationManager(Generic[T], ISimulationable):
    """A base class for managing simulation state."""

    def __init__(self, live_driver: T, sim_driver: T, sim: bool) -> None:
        self._live_driver = live_driver
        self._sim_driver = sim_driver
        self._sim = sim

    @property
    def driver(self) -> T:
        """Returns the live driver if not simulating, otherwise returns the simulation driver."""
        return self._sim_driver if self._sim else self._live_driver

    @property
    def is_simulating(self) -> bool:
        """Returns whether the manager is simulating or not."""
        return self._sim

    def set_simulating(self, sim: bool) -> None:
        """Sets the simulation state of the manager."""
        self._sim = sim