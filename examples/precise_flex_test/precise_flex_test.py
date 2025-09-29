
import logging
import os
import sys

from orca.devices.centrifuge import Centrifuge
from orca.devices.sealer import Sealer
from orca.devices.shaker import Shaker
from orca.driver_management.drivers.sims import HumanSim, SimCentrifugeDriver, SimSealerDriver, SimShakerDriver
from orca.events.event_bus import EventBus
from orca.resource_models.labware import PlateTemplate
from orca.resource_models.transporter import Transporter
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap
from orca.workflow_models.action_template import Seal, Shake, Spin, Read
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
src_plate = PlateTemplate("src_plate", Cor_Falcon_96_wellplate_340ul_Fb_Black)
dest_plate = PlateTemplate("dest_plate", Cor_Falcon_96_wellplate_340ul_Fb_Black)
labwares = [
    src_plate,
    dest_plate
]

pf_teachpoints = os.path.join("examples", "precise_flex_test", "teachpoints", "precise_flex.xml")

pf_arm = Transporter("pf_arm", PreciseFlex400Backend("192.168.0.1", 10100), pf_teachpoints, sim=True)

shaker = Shaker("human_device", SimShakerDriver("human_device", HumanSim()))
centrifuge = Centrifuge("centrifuge", SimCentrifugeDriver("centrifuge", HumanSim()))
sealer = Sealer("sealer", SimSealerDriver("peeler_driver", HumanSim()))

resource_registry = ResourceRegistry()
resources = [
    pf_arm,
    shaker,
    sealer,
    centrifuge
]

map = SystemMap(resource_registry)
map.assign_resources({

})

test_method = MethodTemplate("test_method", [
    Shake(shaker, 10, 12, [src_plate], [src_plate]),
    Spin(centrifuge, 10, 10, [src_plate], [src_plate])
])

method_template = MethodTemplate("test_method", [
    Seal(sealer, 120, 20, [src_plate], [src_plate])
])




sample_plate_thread = ThreadTemplate(
    src_plate,
    map.get_location("location_1"),
    map.get_location("location_2"),
    [
    test_method
])

destination_plate_thread = ThreadTemplate(
    dest_plate, 
    map.get_location("location_3"),
    map.get_location("location_4"),
    [
    method_template
])



test_workflow = WorkflowTemplate("test_workflow")
test_workflow.add_thread(sample_plate_thread, True)

event_bus = EventBus()

builder = SdkToSystemBuilder(
    "Precise Flex Test", 
    "An example workflow to test the Precise Flex arm", 
    labwares, 
    resource_registry, 
    map, 
    [test_method, method_template], 
    [test_workflow], 
    event_bus)