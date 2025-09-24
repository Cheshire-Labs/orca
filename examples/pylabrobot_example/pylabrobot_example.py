import asyncio
import logging
import sys
from typing import Any, Dict, List

from orca.devices.sealer import Sealer
from orca.devices.mock_device import MockDevice
from orca.sdk.devices import MockTransporter, Device
from orca.sdk.labware import PlateTemplate, LabwareInstance
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.sdk.system import SdkToSystemBuilder, WorkflowExecutor, ResourceRegistry, SystemMap
from orca.sdk.events import EventBus
from orca.sdk.actions import Seal, PythonMethod

from pylabrobot.sealing.a4s_backend import A4SBackend
from pylabrobot.resources.thermo_fisher.plates import Thermo_Nunc_96_well_plate_1300uL_Rb


# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout 
)
orca_logger = logging.getLogger("orca")

# Create your labware
sample_plate = PlateTemplate("sample_plate",Thermo_Nunc_96_well_plate_1300uL_Rb, None)
transfer_plate = PlateTemplate("transfer_plate",Thermo_Nunc_96_well_plate_1300uL_Rb, None)

# Add your labware to a list
labwares = [
    sample_plate,
    transfer_plate
    ]

# import the pylabrobot package to use the A4SSealerDriver
a4s_sealer_driver = A4SBackend(port="/dev/tty.usbserial-0001", timeout=10)
sealer = Sealer("a4s_sealer", a4s_sealer_driver)
mock_device = MockDevice("liquid_handler", "ml_star")
robotic_arm = MockTransporter("robotic_arm", "ddr", "examples\\pylabrobot_example\\teachpoints\\teachpoints.xml")

# Register the resources
resources = ResourceRegistry()
resources.add_resources(
    [
        sealer,
        robotic_arm,
        mock_device
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
            outputs=[sample_plate]
        ),
    ]
) 

# Build any python method to run custom code
async def example_method(resource: Device, inputs: List[LabwareInstance], outputs: List[LabwareInstance], options: Dict[str, Any] | None = None):
    """
    A mock method to simulate a liquid handler operation.
    This is where you would implement the actual logic for the liquid handler.
    """
    orca_logger.info(f"Running method with resource: {resource.name}")
    orca_logger.info(f"Inputs: {[input.name for input in inputs]}")
    orca_logger.info(f"Outputs: {[output.name for output in outputs]}")
    orca_logger.info(f"Options: {options if options else 'No options provided'}")
    
    # Simulate some processing time
    await asyncio.sleep(1)
    orca_logger.info("Example method completed.")

# Pass your method to the PythonMethod command to run your custom code once the sample plate reaches the mock device
dispense_method = MethodTemplate(
    name="Dispense Method",
    actions=[
        PythonMethod(
            mock_device,
            example_method,
            [sample_plate],
            [sample_plate]
        ),
    ]
)

# build the map and assign where the resources are located
map = SystemMap(resources)
map.assign_resources({
    "sealer": sealer,
    "mock_device": mock_device,
})

# Build your sample plate thread
sample_plate_thread = ThreadTemplate(
    sample_plate,
    map.get_location("plate_pad_1"),
    map.get_location("plate_pad_2"),
    [
        dispense_method,
        seal_method
        ]
)

# Build a workflow and add the sample plate thread to it
example_workflow = WorkflowTemplate("example_workflow")
example_workflow.add_thread(sample_plate_thread, True)

# An event bus
event_bus = EventBus()

# Build the system from the components
builder = SdkToSystemBuilder(
    "pylabrobot_example",
    "An example workflow using PylabRobot",
    labwares,
    resources,
    map,
    [seal_method],
    [example_workflow],
    event_bus,
)

# Build the system
system = builder.get_system()

# run the workflow using this function
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