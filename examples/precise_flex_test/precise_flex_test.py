
import logging
import os
import sys

from orca.driver_management.drivers.robotic_arm import RoboticArm
from orca.resource_models.labware import PlateTemplate
from orca.resource_models.transporter import Transporter
from pylabrobot.resources.corning.falcon.plates import Cor_Falcon_96_wellplate_340ul_Fb_Black
from pylabrobot.arms.precise_flex.pf_3400 import PreciseFlex400Backend

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout 
)
orca_logger = logging.getLogger("orca")

# Labware
test_plate = PlateTemplate("test_plate", Cor_Falcon_96_wellplate_340ul_Fb_Black)

labwares = [
    test_plate
]

pf_teachpoints = os.path.join("examples", "precise_flex_test", "teachpoints", "precise_flex.xml")

test_plate = PlateTemplate("test_plate", Cor_Falcon_96_wellplate_340ul_Fb_Black, None)

pf_arm = RoboticArm("pf_arm", PreciseFlex400Backend("192.168.0.1", 10100))