"""Sim-compatible labware templates for source-available/sim execution.

Uses simple dataclass implementations of IPlate/ITipRack that satisfy the
interface protocols without any PLR dependency. Sufficient for graph
traversal, name tracking, and reservation scheduling in simulation.
"""

from dataclasses import dataclass, field

from cheshire_drivers.labware_interfaces import IPlate, ITipRack, ITipSpot, IWell
from orca.resource_models.labware import (
    LabwareTemplate,
    PlateInstance,
    PlateTemplate,
    TipRackInstance,
    TipRackTemplate,
    TroughInstance,
    TroughTemplate,
)
from orca.runtime.labware_catalog_protocol import ILabwareCatalog


@dataclass
class SimWell:
    """Minimal IWell implementation for simulation."""
    parent_name: str = ""
    identifier: str = ""
    row: int = 0
    col: int = 0
    position_x: float = 0.0
    position_y: float = 0.0
    position_z: float = 0.0
    size_x: float = 9.0
    size_y: float = 9.0
    size_z: float = 17.0
    max_volume: float = 385.0
    volume: float = 0.0
    bottom_type: str = "flat"

    @property
    def resource_name(self) -> str:
        return self.parent_name

    @property
    def position(self) -> str | None:
        return self.identifier

    def set_volume(self, volume: float) -> None:
        self.volume = volume


@dataclass
class SimTrough:
    """Minimal ITrough (single-pool container) implementation for simulation."""
    name: str = ""
    model: str | None = None
    size_x: float = 8.0
    size_y: float = 72.0
    size_z: float = 40.0
    max_volume: float = 100_000.0
    volume: float = 0.0

    @property
    def resource_name(self) -> str:
        return self.name

    @property
    def position(self) -> str | None:
        return None

    def set_volume(self, volume: float) -> None:
        self.volume = volume


@dataclass
class SimTipSpot:
    """Minimal ITipSpot implementation for simulation."""
    identifier: str = ""
    has_tip: bool = True
    parent_name: str = ""

    def set_tip(self, has_tip: bool) -> None:
        self.has_tip = has_tip


class SimPlate:
    """Minimal IPlate implementation for simulation. No PLR dependency."""

    def __init__(self, name: str, with_lid: bool | None = None) -> None:
        self._name = name
        self._wells: dict[str, SimWell] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str | None:
        return None

    @property
    def barcode(self) -> str | None:
        return None

    @property
    def num_rows(self) -> int:
        return 0

    @property
    def num_cols(self) -> int:
        return 0

    @property
    def size_x(self) -> float:
        return 127.76

    @property
    def size_y(self) -> float:
        return 85.48

    @property
    def size_z(self) -> float:
        return 14.5

    @property
    def has_lid(self) -> bool:
        return False

    def well(self, identifier: str) -> IWell:
        if identifier not in self._wells:
            self._wells[identifier] = SimWell(parent_name=self._name, identifier=identifier)
        return self._wells[identifier]

    def wells(self, identifiers: list[str]) -> list[IWell]:
        return [self.well(i) for i in identifiers]


class SimTipRack:
    """Minimal ITipRack implementation for simulation. No PLR dependency."""

    def __init__(self, name: str, with_tips: bool = True) -> None:
        self._name = name
        self._with_tips = with_tips
        self._spots: dict[str, SimTipSpot] = {}

    @property
    def name(self) -> str:
        return self._name

    @property
    def size_z(self) -> float:
        """A filled 1000 uL rack, the tallest thing an arm routinely carries.

        Nominal like every other dimension here. Erring tall is the safe
        direction: it makes a sim move lift further than it needs to, where
        erring short is what puts a gripper through a rack.
        """
        return 99.0

    @property
    def model(self) -> str | None:
        return None

    @property
    def num_tips(self) -> int:
        return 0

    @property
    def has_tips(self) -> bool:
        return self._with_tips

    def tip_spot(self, identifier: str) -> ITipSpot:
        if identifier not in self._spots:
            self._spots[identifier] = SimTipSpot(identifier=identifier, has_tip=self._with_tips, parent_name=self._name)
        return self._spots[identifier]

    def tip_spots(self) -> list[ITipSpot]:
        return list(self._spots.values())


_SIM_PLATE_LABWARE_TYPE = "_sim_plate"
_SIM_TIP_RACK_LABWARE_TYPE = "_sim_tip_rack"
_SIM_TROUGH_LABWARE_TYPE = "_sim_trough"


class SimPlateTemplate(PlateTemplate):
    """PlateTemplate that constructs a `SimPlate` stub directly.

    Sim instances bypass the runtime labware catalog (no real PLR
    geometry needed) and override `_build_instance()` to build a
    `SimPlate` from the template name. The placeholder ``labware_type``
    is never resolved because `bind_catalog()` is a no-op here.
    """

    def __init__(self, name: str) -> None:
        super().__init__(name, labware_type=_SIM_PLATE_LABWARE_TYPE)

    async def bind_catalog(self, catalog: ILabwareCatalog) -> None:
        # Sim templates ignore the catalog; placeholder labware_type is
        # never looked up.
        return

    async def _build_instance(self, instance_id: str, instance_name: str) -> PlateInstance:
        instance = PlateInstance(
            SimPlate(instance_name),
            template_name=self.name,
            labware_type=self.labware_type,
            instance_id=instance_id,
        )
        instance._template = self
        return instance


class SimTipRackTemplate(TipRackTemplate):
    """TipRackTemplate that constructs a `SimTipRack` stub directly."""

    def __init__(self, name: str) -> None:
        super().__init__(
            name, labware_type=_SIM_TIP_RACK_LABWARE_TYPE, with_tips=True
        )

    async def bind_catalog(self, catalog: ILabwareCatalog) -> None:
        return

    async def _build_instance(self, instance_id: str, instance_name: str) -> TipRackInstance:
        instance = TipRackInstance(
            SimTipRack(instance_name, self._with_tips),
            template_name=self.name,
            labware_type=self.labware_type,
            instance_id=instance_id,
        )
        instance._template = self
        return instance


class SimTroughTemplate(TroughTemplate):
    """TroughTemplate that constructs a `SimTrough` stub directly."""

    def __init__(self, name: str) -> None:
        super().__init__(name, labware_type=_SIM_TROUGH_LABWARE_TYPE)

    async def bind_catalog(self, catalog: ILabwareCatalog) -> None:
        return

    async def _build_instance(self, instance_id: str, instance_name: str) -> TroughInstance:
        instance = TroughInstance(
            SimTrough(name=instance_name),
            template_name=self.name,
            labware_type=self.labware_type,
            instance_id=instance_id,
        )
        instance._template = self
        return instance


LABWARE_TYPE_MAP: dict[str, type[LabwareTemplate]] = {
    "plate": SimPlateTemplate,
    "tip_rack": SimTipRackTemplate,
    "trough": SimTroughTemplate,
}
