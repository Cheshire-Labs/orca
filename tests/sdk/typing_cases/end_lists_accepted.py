"""End arguments `orca.thread` must accept. Read by pyright, never run."""

import orca.orca as orca
from orca.resource_models.location import Location
from orca.sdk.labware import PlateTemplate
from orca.sdk.workflow import LEAVE_IN_PLACE

plate = PlateTemplate("plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
names: list[str] = ["hotel_pad_1", "hotel_pad_2"]
locations: list[Location] = []
mixed: list[Location | str] = ["hotel_pad_1"]

orca.thread(labware=plate, start="pad", end="hotel_pad_1")
orca.thread(labware=plate, start="pad", end=["hotel_pad_1", "hotel_pad_2"])
orca.thread(labware=plate, start="pad", end=names)
orca.thread(labware=plate, start="pad", end=locations)
orca.thread(labware=plate, start="pad", end=mixed)
orca.thread(labware=plate, start="pad", end=(names, LEAVE_IN_PLACE))
