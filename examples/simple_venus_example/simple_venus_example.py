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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout 
)
orca_logger = logging.getLogger("orca")

sample_plate = LabwareTemplate("sample_plate", "Matrix 96-well plate")
transfer_plate = LabwareTemplate("transfer_plate", "Matrix 96-well plate")

labwares = [
    sample_plate,
    transfer_plate
    ]
venus_driver = VenusProtocolDriver("venus")

ml_star = Device("ml_star", venus_driver)
human_transfer = TransporterEquipment("human_transfer", HumanTransferDriver("venus", "examples\\simple_venus_example\\teachpoints\\human_transfer_teachpoints.xml"))

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

map = SystemMap(resource_registry)
map.assign_resources({
    "ml_star_position_1": ml_star,
}
)

# The methods here are included in the orca subfolder examples\simple_venus_example\venus_protocols\
# The Venus Protocol Driver will look with the folder C:\Program Files (x86)\HAMILTON\Methods\{method_filepath}
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

methods = [
    example_method_1,
    transfer_method
]

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
    

example_workflow = WorkflowTemplate("example_workflow")
example_workflow.add_thread(sample_plate_thread, True)
example_workflow.add_thread(transfer_plate_thread)
example_workflow.set_spawn_point(transfer_plate_thread, sample_plate_thread, transfer_method, True)

event_bus = EventBus()
builder = SdkToSystemBuilder(
    "SMC Assay",
    "SMC Assay",
    labwares,
    resource_registry,
    map,
    methods,
    [example_workflow],
    event_bus
)
system = builder.get_system()

async def run():
    orca_logger.info("Starting SMC Assay workflow execution.")
    await system.initialize_all()
    executor = WorkflowExecutor(example_workflow, system)
    await executor.start()
    orca_logger.info("SMC Assay workflow completed.")

if __name__ == "__main__":
    asyncio.run(run())