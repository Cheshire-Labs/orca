import asyncio
import logging
import sys

from orca.driver_management.drivers.simulation_robotic_arm.human_transfer import HumanTransferDriver
from orca.sdk.devices import Device, TransporterEquipment
from orca.sdk.labware import LabwareTemplate
from orca.sdk.system import ResourceRegistry, SystemMap, SdkToSystemBuilder, WorkflowExecutor
from orca.sdk.workflow import MethodTemplate, ActionTemplate, WorkflowTemplate, ThreadTemplate, JunctionMethodTemplate
from orca.sdk.events import EventBus

# Venus driver will need to be installed separately (available on Cheshire Labs' GitHub)
# This driver requires Hamilton Venus to be installed
from venus_driver.venus_driver import VenusProtocolDriver



## This example demonstrates how to use the Venus Protocol Driver with ORCA SDK.
# It includes a simple workflow that runs a method on a Venus device and transfers labware using a human transporter.
# Meaning that the user will need to pick up and place the labware manually.
# The Venus Protocol Driver is used to run methods via the Hamilton Venus software.

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

# Setup your devices
# Each device needs a driver assigned to it

venus_driver = VenusProtocolDriver("venus")
ml_star = Device("ml_star", venus_driver)

# Transorter equipment are devices capable of moving labwaare
# For this simulation, the teachpoints are saved within a local file
# we will use the HumanTransferDriver - meaning that the user will need to manually pick up and place the labware and press Enter to continue
human_transfer = TransporterEquipment("human_transfer", HumanTransferDriver("venus", "examples\\simple_venus_example\\teachpoints\\human_transfer_teachpoints.xml"))

# Create a resource registry to hold all the resources
resource_registry = ResourceRegistry()
resource_registry.add_resources([
    ml_star,
    human_transfer
    ]
)

# This function initializes the transporting equipment in order to get all the teachpoints 
# from the equipment to build a map of the system locations from them
def initialize_all_transporters(resource_registry: ResourceRegistry):
    async def _run_all():
        await asyncio.gather(*(t.initialize() for t in resource_registry.transporters))
    asyncio.run(_run_all())

initialize_all_transporters(resource_registry)

# Create a system map to define the locations of the devices
# Teachpoints are in the examples\simple_venus_example\teachpoints folder
map = SystemMap(resource_registry)
map.assign_resources({
    "ml_star_position_1": ml_star,
}
)

# The methods here are included in the orca subfolder examples\simple_venus_example\venus_protocols\
# The Venus Protocol Driver will look with the folder C:\Program Files (x86)\HAMILTON\Methods\{method_filepath}
# Within options, the 'method' key is the path to the method file relative to the Methods folder
# the 'params' key is a dictionary of parameters that will be passed to the method 
# - these are retrievable within the Venus method using the SubMethod Library provide with the Venus driver's repo on Cheshire Labs' GitHub
example_method_1 = MethodTemplate(
    "example_method_1",
    actions=[
        ActionTemplate(
            ml_star,
            "run",
            inputs=[sample_plate],
            outputs=[sample_plate],
            options={
                "method": "Cheshire Labs\\VariableAccessTesting.hsl",
                "params": {
                    "strParam": "strParam value transmitted",
                    "intParam": 123,
                    "fltParam": 1.003
                }
            }
        )
    ]
)
transfer_method = MethodTemplate(
    "transfer_method",
    actions=[
        ActionTemplate(
            ml_star,
            "run",
            inputs=[sample_plate, transfer_plate],
            outputs=[sample_plate, transfer_plate],
            options={
                "method": "Cheshire Labs\\SimplePlateStamp.hsl",
                "params": {
                    "numOfPlates": 1,
                    "waterVol": 30,
                    "dyeVol": 10,
                    "wait": 1,
                    "tipEjectPos": 2,
                    "clld": 1
                }
            }
        )
    ]
)

# create a list of all the methods to later build the system with
methods = [
    example_method_1,
    transfer_method
]

# Build your labware threadds
# Labware threads are the set of methods which you expect your labware to pass through
# If your labware interactes with another piece of labware, use a JunctionMethodTemplate() at that step, you will then define this interaction further within the workflow
# Labware threads can usually be throught of as a single piece of labware from which other labwares spawn
sample_plate_thread = ThreadTemplate(
    sample_plate,
    map.get_location("plate_pad_1"),
    map.get_location("plate_pad_2"),
    [
        example_method_1,
        transfer_method
    ]
)

transfer_plate_thread = ThreadTemplate(
    transfer_plate,
    map.get_location("plate_pad_3"),
    map.get_location("plate_pad_4"),
    [
        JunctionMethodTemplate()
    ]
)
    
# Define your workflow
# Your workflow defines how your labware threads interact with each other
example_workflow = WorkflowTemplate("example_workflow")

# Add each other your labware threads to the workflow
# Be sure to define which threads should start when the workflow starts
example_workflow.add_thread(sample_plate_thread, True) # Starts when the workflow starts
example_workflow.add_thread(transfer_plate_thread)

# Define the spawn points of the workflow
# A spawn point is a point in the workflow where a new thread is created
# Spawn points are attached to another running thread, in this case, the plate_1_thread - the main thread of the workflow
# The spawn point will create a new thread when the main thread reaches the method defined in the spawn point
# The 'join' parameter here tells the workflow whether or not to join the newly created thread to the main thread via a JunctionMethodTemplate
example_workflow.set_spawn_point(transfer_plate_thread, sample_plate_thread, transfer_method, True)

# Create an event bus to handle events in the system
event_bus = EventBus()

# Set all the components to build the system
builder = SdkToSystemBuilder(
    "Venus Example",
    "Venus Example System",
    labwares,
    resource_registry,
    map,
    methods,
    [example_workflow],
    event_bus
)
# Build the system
system = builder.get_system()

# Use the WorkflowExecutor to run the workflow
async def run():
    orca_logger.info("Starting Venus workflow execution.")
    await system.initialize_all()
    executor = WorkflowExecutor(example_workflow, system)
    await executor.start()
    orca_logger.info("Venus workflow completed.")

if __name__ == "__main__":
    asyncio.run(run())
    orca_logger.info("Workflow execution finished.")