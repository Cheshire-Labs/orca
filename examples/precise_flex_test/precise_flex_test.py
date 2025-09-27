
import logging
import os
import sys

from orca.devices.sealer import Sealer
from orca.devices.shaker import Shaker
from orca.driver_management.drivers.mock import HumanSim, MockDriver
from orca.driver_management.drivers.sealer import SimulationSealerDriver
from orca.driver_management.drivers.shaker import SimulationShakerDriver
from orca.driver_management.drivers.venus.Venus import Venus
from orca.driver_management.drivers.venus.venus_driver import VenusProtocolDriver
from orca.events.event_bus import EventBus
from orca.resource_models.labware import PlateTemplate
from orca.resource_models.transporter import Transporter
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.system_map import SystemMap
from orca.workflow_models.action_template import Delid, Shake, Spin, Read
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflow_templates import WorkflowTemplate
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
pf_arm = Transporter("pf_arm", PreciseFlex400Backend("192.168.0.1", 10100), pf_teachpoints)

mock_device = Shaker("human_device", SimulationShakerDriver(0.2,HumanSim()))
sealer = Sealer("sealer", SimulationSealerDriver(, "peeler_driver"))
resources = [
    pf_arm,
    mock_device,
    sealer,
    venus
]

test_method = MethodTemplate("test_method", [
    Shake(),
    Spin
])

method_template = MethodTemplate("test_method", [
    Spin()
    Delid("test_plate"),
])


map = SystemMap(resources)
map.assign_resources({

})

sample_plate_thread = ThreadTemplate("sample_plate_thread", [
    method_template
])

destination_plate_thread = ThreadTemplate("destination_plate_thread", [
    method_template
])



test_workflow = WorkflowTemplate("test_workflow")
test_workflow.add_thread(sample_plate_thread, True)

event_bus = EventBus()

builder = SdkToSystemBuilder(
    "Precise Flex Test", 
    "An example workflow to test the Precise Flex arm", 
    labwares, 
    resources, 
    map, 
    [test_method, method_template], 
    [test_workflow], 
    event_bus)