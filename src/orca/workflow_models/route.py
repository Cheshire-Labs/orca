from orca.resource_models.labware import Labware
from orca.resource_models.location import Location
from orca.resource_models.transporter_resource import TransporterEquipment


from typing import Iterator, List


class RouteStep:
    def __init__(self, labware: Labware, source: Location, target: Location, transporter: TransporterEquipment) -> None:
        self.labware: Labware = labware 
        self.source: Location = source
        self.target: Location = target
        self.transporter: TransporterEquipment = transporter
        self.target_loc_reservation_id: str | None = None

    def __str__(self) -> str:
        return f"Route Step (labware: {self.labware}): {self.source.name} -> {self.target.name} with {self.transporter.name}"

class Route:
    def __init__(self, labware: Labware) -> None:
        self._steps: List[RouteStep] = []
        self._labware: Labware = labware

    def add_step(self, source: Location, target: Location, transporter: TransporterEquipment) -> None:
        """Add a new step to the route."""
        step = RouteStep(self._labware, source, target, transporter)
        self._steps.append(step)

    def get_total_steps(self) -> int:
        """Return the total number of steps in the route."""
        return len(self._steps)

    def __iter__(self) -> Iterator[RouteStep]:
        """Allow iterating over the route steps."""
        return iter(self._steps)

    def __getitem__(self, index: int) -> RouteStep:
        """Allow direct access to a route step by its index."""
        return self._steps[index]

    def __len__(self) -> int:
        """Allow getting the length of the route (number of steps)."""
        return len(self._steps)