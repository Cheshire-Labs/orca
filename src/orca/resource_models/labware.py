from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict
import uuid


from pylabrobot.resources.tip_rack import TipRack
from pylabrobot.resources.plate import Plate, Lid
from pylabrobot.resources.tube_rack import TubeRack
from pylabrobot.resources.trough import Trough

class LabwareTemplate(ABC):
    def __init__(self, name: str) -> None:
        self._name = name
    
    @property
    def name(self) -> str:
        return self._name
    
    @abstractmethod
    def create_instance(self) -> LabwareInstance:
        raise NotImplementedError("This method should be implemented by subclasses")
    
class PlateTemplate(LabwareTemplate):
    """A class that represents a plate template"""

    def __init__(self, name: str, labware_factory: Callable[..., Plate], with_lid: Lid | None = None) -> None:
        """ Initializes a PlateTemplate instance.
        Args:
            name (str): The name of the plate.
            labware_factory (Callable[..., Plate]): A pylabrobot plate function to create a Plate instance.
            with_lid (Lid | None): Optional lid for the plate. If provided, the plate will be created with a lid.
        """
        super().__init__(name)
        self.with_lid = with_lid
        self._factory = labware_factory

    def create_instance(self) -> PlateInstance:
        try:
            plate = self._factory(self.name, self.with_lid)
        except TypeError:
            if self.with_lid:
                raise ValueError("This plate type does not support 'with_lid'")
            plate = self._factory(self.name)

        return PlateInstance(plate)
    
class TubeRackTemplate(LabwareTemplate):
    """A class that represents a tube rack template"""

    def __init__(self, name: str, labware_factory: Callable[[str], TubeRack]) -> None:
        """ Initializes a TubeRackTemplate instance.
        Args:
            name (str): The name of the tube rack.
            labware_factory (Callable[[str], TubeRack]): A pyLabRobot TubeRack function to create a TubeRack instance.
        """
        super().__init__(name)
        self._factory = labware_factory

    def create_instance(self) -> TubeRackInstance:
        tube_rack = self._factory(self.name)
        return TubeRackInstance(tube_rack)
    
class TipRackTemplate(LabwareTemplate):
    """A class that represents a tip rack template"""

    def __init__(self, name: str, labware_factory: Callable[[str, bool], TipRack], with_tips: bool) -> None:
        """ Initializes a TipRackTemplate instance.

        Args:
            name (str): The name of the tip rack.
            labware_factory (Callable[[str, bool], TipRack]): A pyLabRobot TipRack function to create a TipRack instance.
            with_tips (bool): Whether the tip rack should be created with tips.
        """
        super().__init__(name)
        self._with_tips = with_tips
        self._factory = labware_factory

    def create_instance(self) -> TipRackInstance:
        tip_rack = self._factory(self.name, self._with_tips)
        return TipRackInstance(tip_rack)
    
class TroughTemplate(LabwareTemplate):
    """A class that represents a trough template"""

    def __init__(self, name: str, labware_factory: Callable[..., Trough]) -> None:
        """ Initializes a TroughTemplate instance.
        Args:
            name (str): The name of the trough.
            labware_factory (Callable[..., Trough]): A pyLabRobot Trough function to create a Trough instance.
        """
        super().__init__(name)
        self._factory = labware_factory

    def create_instance(self) -> TroughInstance:
        trough = self._factory(self.name)
        return TroughInstance(trough)

class AnyLabwareTemplate:
    """Acts as a placeholder for any labware type, allowing for flexible methods that can accept any labware."""

    @property
    def name(self) -> str:
        return "$any"

    def create_instance(self) -> AnyLabware:
        return AnyLabware()
    
    def __str__(self) -> str:
        return "$AnyLabwareTemplate"


class AnyLabware:
    """An instance of AnyLabwareTemplate that can be used in methods that accept any labware type."""

    @property
    def name(self) -> str:
        return "$any"

    def __str__(self) -> str:
        return "$AnyLabware"


class LabwareInstance:
    """ An instance of a labware that can be used in methods and workflows."""
    
    def __init__(self, name: str, labware_type: str) -> None:
        self._id = str(uuid.uuid4())
        self._init_options: Dict[str, Any] = {}
        self._name = name
        self._labware_type = labware_type
    
    @property
    def name(self) -> str:
        return f"{self._name}"

    @property   
    def id(self) -> str:
        return self._id
    
    @property
    def labware_type(self) -> str:
        return self._labware_type

    def __str__(self) -> str:
        return f"{self._name}"
    
class PlateInstance(LabwareInstance):
    """A class that represents a plate instance"""

    def __init__(self, labware: Plate) -> None:
        super().__init__(labware.name, labware.model or labware.__class__.__name__)
        self._plate = labware

    def PLR(self) -> Plate:
        """Returns the underlying Plate object."""
        return self._plate

class TubeRackInstance(LabwareInstance):
    """A class that represents a tube rack instance"""

    def __init__(self, labware: TubeRack) -> None:
        super().__init__(labware.name, labware.model or labware.__class__.__name__)
        self._tube_rack = labware

    def PLR(self) -> TubeRack:
        """Returns the underlying TubeRack object."""
        return self._tube_rack

class TipRackInstance(LabwareInstance):
    """A class that represents a tip rack instance"""

    def __init__(self, labware: TipRack) -> None:
        super().__init__(labware.name, labware.model or labware.__class__.__name__)
        self._tip_rack = labware

    def PLR(self) -> TipRack:
        """Returns the underlying TipRack object."""
        return self._tip_rack
    
class TroughInstance(LabwareInstance):
    """A class that represents a trough instance"""

    def __init__(self, labware: Trough) -> None:
        super().__init__(labware.name, labware.model or labware.__class__.__name__)
        self._trough = labware

    def PLR(self) -> Trough:
        """Returns the underlying Trough object."""
        return self._trough
