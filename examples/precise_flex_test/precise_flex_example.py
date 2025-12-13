
import asyncio
import logging
import os
import sys

from orca.devices.centrifuge import Centrifuge
from orca.devices.sealer import Sealer
from orca.devices.shaker import Shaker
from cheshire_drivers import HumanSim, SimCentrifugeDriver, SimSealerDriver, SimShakerDriver
from orca.events.event_bus import EventBus
from orca.resource_models.labware import PlateTemplate
from orca.resource_models.transporter import Transporter
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.executors import WorkflowExecutor
from orca.system.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap
from orca.workflow_models.action_template import Seal, Shake, Spin
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflow_templates import WorkflowTemplate
from pylabrobot.resources.corning.falcon.plates import Cor_Falcon_96_wellplate_340ul_Fb_Black
from pylabrobot.arms.precise_flex.pf_3400 import PreciseFlex3400Backend

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

pf_teachpoints = os.path.join("examples", "precise_flex_test", "teachpoints", "precise_flex.json")

pf_arm = Transporter("pf_arm", PreciseFlex3400Backend("192.168.0.1", 10100), pf_teachpoints, sim=False)

shaker = Shaker("shaker", SimShakerDriver("shaker", HumanSim()))
centrifuge = Centrifuge("centrifuge", SimCentrifugeDriver("centrifuge", HumanSim()))
sealer = Sealer("sealer", SimSealerDriver("peeler_driver", HumanSim()))

resource_registry = ResourceRegistry()
resource_registry.add_resources([
    pf_arm,
    shaker,
    sealer,
    centrifuge
])

map = SystemMap(resource_registry)
map.assign_resources({
"centrifuge": centrifuge,
"sealer": sealer,
"shaker": shaker,
})

test_method_1 = MethodTemplate("test_method_1", [
    Shake(shaker, 10, 12, [src_plate], [src_plate]),
    Spin(centrifuge, 10, 10, [src_plate], [src_plate])
])

test_method_2 = MethodTemplate("test_method_2", [
    Seal(sealer, 120, 20, [dest_plate], [dest_plate])
])


sample_plate_thread = ThreadTemplate(
    src_plate,
    map.get_location("nest_4"),
    map.get_location("nest_4"),
    [
    test_method_1
])

destination_plate_thread = ThreadTemplate(
    dest_plate, 
    map.get_location("nest_5"),
    map.get_location("nest_5"),
    [
    test_method_2
])



test_workflow = WorkflowTemplate("test_workflow")
test_workflow.add_thread(sample_plate_thread, True)
test_workflow.add_thread(destination_plate_thread)
test_workflow.set_spawn_point(destination_plate_thread, 
                              sample_plate_thread, 
                              test_method_1)

event_bus = EventBus()

builder = SdkToSystemBuilder(
    "Precise Flex Test", 
    "An example workflow to test the Precise Flex arm", 
    labwares, 
    resource_registry, 
    map, 
    [test_method_1, test_method_2], 
    [test_workflow], 
    event_bus)

# Build the system
system = builder.get_system()

# run the workflow using this function
async def run(sim: bool):
    orca_logger.info("Starting pyLabRobot workflow execution.")
    if not sim:
        await system.initialize_all()
    executor = WorkflowExecutor(test_workflow, system)
    await executor.start(sim)
    orca_logger.info("pyLabRobot workflow completed.")

if __name__ == "__main__":
    asyncio.run(run(False))
    orca_logger.info("Workflow execution finished.")