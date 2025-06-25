from abc import ABC
import asyncio
import logging
import sys

from orca.events.event_bus import EventBus
from orca.sdk.labware import LabwareTemplate
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, Seal
from orca.sdk.drivers import A4SSealerDriver, SimulationRoboticArmDriver
from orca.sdk.devices import Sealer, TransporterEquipment
from orca.system.SdkToSystemBuilder import SdkToSystemBuilder
from orca.system.executors import WorkflowExecutor
from orca.system.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap
from orca.workflow_models.workflow_templates import WorkflowTemplate


# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout 
)
orca_logger = logging.getLogger("orca")

# Create your labware
sample_plate = LabwareTemplate("sample_plate", "Matrix 96-well plate")
transfer_plate = LabwareTemplate("transfer_plate", "Matrix 96-well plate")

# Add your labware to a list
labwares = [
    sample_plate,
    transfer_plate
    ]

# install the pylabrobot package to use the A4SSealerDriver
a4s_sealer_driver = A4SSealerDriver(port="/dev/tty.usbserial-0001", timeout=10)
sealer = Sealer("a4s_sealer", a4s_sealer_driver)
robotic_arm = TransporterEquipment("robotic_arm", SimulationRoboticArmDriver("robotic_arm", "ddr", "examples\\pylabrobot_example\\teachpoints\\teachpoints.xml"))

# Register the resources
resources = ResourceRegistry()
resources.add_resources(
    [
        sealer,
        robotic_arm
    ]
)

# Build a method to seal
seal_method = MethodTemplate(
    name="Test Method",
    actions=[
        Seal(
            resource=sealer,
            temperature=100,
            duration=60,
            inputs=[sample_plate],
            outputs=[sample_plate],
            options={"temperature": 100, "duration": 60}
        ),
    ]
)
methods = [seal_method]

# build the map and assign where the resources are located
map = SystemMap(resources)
map.assign_resources({
    "sealer": sealer,
})

sample_plate_thread = ThreadTemplate(
    sample_plate,
    map.get_location("plate_pad_1"),
    map.get_location("plate_pad_2"),
    [seal_method]
)

example_workflow = WorkflowTemplate("example_workflow")
example_workflow.add_thread(sample_plate_thread, True)

event_bus = EventBus()

builder = SdkToSystemBuilder(
    "pylabrobot_example",
    "An example workflow using PylabRobot",
    labwares,
    resources,
    map,
    methods,
    [example_workflow],
    event_bus,
)

# Build the system
system = builder.get_system()

async def run(sim: bool):
    orca_logger.info("Starting pyLabRobot workflow execution.")
    if not sim:
        await system.initialize_all()
    executor = WorkflowExecutor(example_workflow, system)
    await executor.start(sim)
    orca_logger.info("pyLabRobot workflow completed.")

if __name__ == "__main__":
    asyncio.run(run(True))
    orca_logger.info("Workflow execution finished.")