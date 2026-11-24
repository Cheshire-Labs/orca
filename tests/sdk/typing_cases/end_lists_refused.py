"""End arguments `orca.thread` must refuse. Read by pyright, never run; each refused line ends in `# refused`."""

import orca.orca as orca
from orca.sdk.labware import PlateTemplate

plate = PlateTemplate("plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black")
numbers: list[int] = [1, 2]

orca.thread(labware=plate, start="pad", end=numbers)  # refused
orca.thread(labware=plate, start="pad", end={"hotel_pad_1", "hotel_pad_2"})  # refused
orca.thread(labware=plate, start="pad", end=None)  # refused
