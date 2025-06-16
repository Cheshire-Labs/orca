import asyncio
import os
import logging
import sys
import time
from orca.sdk.system import SdkToSystemBuilder, WorkflowExecutor, ResourceRegistry, SystemMap, ExecutingLabwareThread, StandalonMethodExecutor
from orca.sdk.workflow import WorkflowTemplate, ThreadTemplate, MethodTemplate, ActionTemplate, JunctionMethodTemplate
from orca.sdk.events import EventBus, SystemBoundEventHandler, ExecutionContext, ThreadExecutionContext, WorkflowExecutionContext, LabwareThreadStatus
from orca.sdk.devices import Device, EquipmentResourcePool, TransporterEquipment
from orca.sdk.labware import AnyLabwareTemplate, LabwareTemplate
from orca.sdk.drivers import SimulationDeviceDriver, SimulationRoboticArmDriver

# Setup a logger (Optional)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout 
)
orca_logger = logging.getLogger("orca")


# Create your labware
sample_plate = LabwareTemplate(name="sample_plate", type="Matrix 96 Well")
plate_1 = LabwareTemplate(name="plate_1", type="Corning 96 Well")
final_plate = LabwareTemplate(name="final_plate", type="SMC 384 plate")
bead_reservoir = LabwareTemplate(name="bead_reservoir", type="reservoir")
buffer_b_reservoir = LabwareTemplate(name="buffer_b_reservoir", type="reservoir")
buffer_d_reservoir = LabwareTemplate(name="buffer_d_reservoir", type="reservoir")
detection_reservoir = LabwareTemplate(name="detection_reservoir", type="reservoir")
tips_96 = LabwareTemplate(name="tips_96", type="96 Tips")
tips_384 = LabwareTemplate(name="tips_384", type="384 Tips")

# Add your labware to a list
labwares = [
    sample_plate,
    plate_1,
    final_plate,
    bead_reservoir,
    buffer_b_reservoir,
    buffer_d_reservoir,
    detection_reservoir,
    tips_96,
    tips_384
]

# Setup your devices, each device needs a driver assigneed to it
# Transorter equipment are devices capable of moving labwaare
# For this simulation, the teachpoints are saved within a local file
teachpoints_dir = "examples\\smc_assay\\teachpoints"
ddr1_points = os.path.join(teachpoints_dir, "ddr1.xml")
ddr2_points = os.path.join(teachpoints_dir, "ddr2.xml")
ddr3_points = os.path.join(teachpoints_dir, "ddr3.xml")
translator1_points = os.path.join(teachpoints_dir, "translator1.xml")
translator2_points = os.path.join(teachpoints_dir, "translator2.xml")
ddr_1 = TransporterEquipment(name="ddr_1", driver=SimulationRoboticArmDriver(name="ddr_1_driver", mocking_type="ddr", teachpoints_filepath=ddr1_points))
ddr_2 = TransporterEquipment(name="ddr_2", driver=SimulationRoboticArmDriver(name="ddr_2_driver", mocking_type="ddr", teachpoints_filepath=ddr2_points))
ddr_3 = TransporterEquipment(name="ddr_3", driver=SimulationRoboticArmDriver(name="ddr_3_driver", mocking_type="ddr", teachpoints_filepath=ddr3_points))
translator_1 = TransporterEquipment(name="translator_1", driver=SimulationRoboticArmDriver(name="translator_1_driver", mocking_type="translator", teachpoints_filepath=translator1_points))
translator_2 = TransporterEquipment(name="translator_2", driver=SimulationRoboticArmDriver(name="translator_2_driver", mocking_type="translator", teachpoints_filepath=translator2_points))

# These are devices capable of reciving labware
biotek_1 = Device(name="biotek_1", driver=SimulationDeviceDriver(name="biotek_1_driver", mocking_type="biotek"))
biotek_2 = Device(name="biotek_2", driver=SimulationDeviceDriver(name="biotek_2_driver", mocking_type="biotek"))
bravo_96 = Device(name="bravo_96_head", driver=SimulationDeviceDriver(name="bravo_96_head_driver", mocking_type="bravo"))
bravo_384 = Device(name="bravo_384_head", driver=SimulationDeviceDriver(name="bravo_384_head_driver", mocking_type="bravo"))
sealer = Device(name="sealer", driver=SimulationDeviceDriver(name="sealer_driver", mocking_type="plateloc"))
centrifuge = Device(name="centrifuge", driver=SimulationDeviceDriver(name="centrifuge_driver", mocking_type="vspin"))
plate_hotel = Device(name="plate_hotel", driver=SimulationDeviceDriver(name="plate_hotel_driver", mocking_type="agilent_hotel"))
delidder = Device(name="delidder", driver=SimulationDeviceDriver(name="delidder_driver", mocking_type="delidder"))
smc_pro = Device(name="smc_pro", driver=SimulationDeviceDriver(name="smc_pro_driver", mocking_type="smc_pro"))
stacker_sample_start = Device(name="stacker_sample_start", driver=SimulationDeviceDriver(name="stacker_sample_start_driver", mocking_type="vstack"))
stacker_sample_end = Device(name="stacker_sample_end", driver=SimulationDeviceDriver(name="stacker_sample_end_driver", mocking_type="vstack"))
stacker_plate_1_start = Device(name="stacker_plate_1_start", driver=SimulationDeviceDriver(name="stacker_plate_1_start_driver", mocking_type="vstack"))
stacker_final_plate_start = Device(name="stacker_final_plate_start", driver=SimulationDeviceDriver(name="stacker_final_plate_start_driver", mocking_type="vstack"))
stacker_96_tips = Device(name="stacker_96_tips", driver=SimulationDeviceDriver(name="stacker_96_tips_driver", mocking_type="vstack"))
stacker_384_tips_start = Device(name="stacker_384_tips_start", driver=SimulationDeviceDriver(name="stacker_384_tips_start_driver", mocking_type="vstack"))
stacker_384_tips_end = Device(name="stacker_384_tips_end", driver=SimulationDeviceDriver(name="stacker_384_tips_end_driver", mocking_type="vstack"))
shaker_1 = Device(name="shaker_1", driver=SimulationDeviceDriver(name="shaker_1_driver", mocking_type="shaker"))
shaker_2 = Device(name="shaker_2", driver=SimulationDeviceDriver(name="shaker_2_driver", mocking_type="shaker"))
shaker_3 = Device(name="shaker_3", driver=SimulationDeviceDriver(name="shaker_3_driver", mocking_type="shaker"))
shaker_4 = Device(name="shaker_4", driver=SimulationDeviceDriver(name="shaker_4_driver", mocking_type="shaker"))
shaker_5 = Device(name="shaker_5", driver=SimulationDeviceDriver(name="shaker_5_driver", mocking_type="shaker"))
shaker_6 = Device(name="shaker_6", driver=SimulationDeviceDriver(name="shaker_6_driver", mocking_type="shaker"))
shaker_7 = Device(name="shaker_7", driver=SimulationDeviceDriver(name="shaker_7_driver", mocking_type="shaker"))
shaker_8 = Device(name="shaker_8", driver=SimulationDeviceDriver(name="shaker_8_driver", mocking_type="shaker"))
shaker_9 = Device(name="shaker_9", driver=SimulationDeviceDriver(name="shaker_9_driver", mocking_type="shaker"))
shaker_10 = Device(name="shaker_10", driver=SimulationDeviceDriver(name="shaker_10_driver", mocking_type="shaker"))
waste_1 = Device(name="waste_1", driver=SimulationDeviceDriver(name="waste_1_driver", mocking_type="waste"))

# Build any resource pools - Orca will resolve what resource to use once it reaches that step
shaker_collection = EquipmentResourcePool(name="shaker_collection", resources=[shaker_1, shaker_2, shaker_3, shaker_4, shaker_5, shaker_6, shaker_7, shaker_8, shaker_9, shaker_10])

# Initialize a resource registry and add all the equipment to it
resource_registry = ResourceRegistry()
resource_registry.add_resources([
    biotek_1,
    biotek_2,
    bravo_96,
    bravo_384,
    sealer,
    centrifuge, 
    plate_hotel,
    delidder,
    ddr_1,
    ddr_2,
    ddr_3,
    translator_1,
    translator_2,
    stacker_sample_start,
    stacker_sample_end,
    stacker_plate_1_start,
    stacker_final_plate_start,
    stacker_96_tips,
    stacker_384_tips_start,
    stacker_384_tips_end,
    shaker_1,
    shaker_2,
    shaker_3,
    shaker_4,
    shaker_5,
    shaker_6,
    shaker_7,
    shaker_8,
    shaker_9,
    shaker_10,
    waste_1,
    shaker_collection
])


# This function initializes the transporting equipment in order to get all the teachpoints 
# from the equipment to build a map of the system locations from them
def initialize_all_transporters(resource_registry: ResourceRegistry):
    async def _run_all():
        await asyncio.gather(*(t.initialize() for t in resource_registry.transporters))
    asyncio.run(_run_all())

initialize_all_transporters(resource_registry)

# Use the resource registry to build a system map of locations via teachpoints each robot can reach
map = SystemMap(resource_registry)

# Assign resources to their respective locations on the system map
map.assign_resources({
    "biotek_1": biotek_1,
    "biotek_2": biotek_2,
    "bravo_96": bravo_96,
    "bravo_384": bravo_384,
    "sealer": sealer,
    "centrifuge": centrifuge,
    "plate_hotel": plate_hotel,
    "delidder": delidder,
    "stacker_1": stacker_sample_start,
    "stacker_2": stacker_sample_end,
    "stacker_3": stacker_plate_1_start,
    "stacker_4": stacker_final_plate_start,
    "stacker_5": stacker_96_tips,
    "stacker_6": stacker_384_tips_start,
    "stacker_7": stacker_384_tips_end,
    "shaker_1": shaker_1,
    "shaker_2": shaker_2,
    "shaker_3": shaker_3,
    "shaker_4": shaker_4,
    "shaker_5": shaker_5,
    "shaker_6": shaker_6,
    "shaker_7": shaker_7,
    "shaker_8": shaker_8,
    "shaker_9": shaker_9,
    "shaker_10": shaker_10,
    "waste_1": waste_1,
})


# Build your methods
# Methods are a collection of actions
# Each action takes in set of labware and then run a command on the labware
# All labware must be present at the resource before the actions runs
sample_to_bead_plate_method = MethodTemplate(
    name="sample_to_bead_plate",
    actions=[
        ActionTemplate(
            resource=bravo_96,
            command="run",
            inputs=[sample_plate, tips_96, plate_1],
            options={
                "protocol": "sample_to_bead_plate.pro",
                "deck_setup": {
                    4: tips_96,
                    5: sample_plate,
                    9: plate_1,
                }
            }
        )
    ]
)

incubate_2hrs = MethodTemplate("incubate_2hrs",
    [
        ActionTemplate(
            resource=shaker_collection,
            command="shake",
            inputs=[plate_1],
            options={
                "shake_time": 7200
            }
        )
])

post_capture_wash = MethodTemplate("post_capture_wash", [
    ActionTemplate(
        resource=biotek_1,
        command="run",
        inputs=[plate_1],
        options={
            "protocol": "post_capture_wash.pro",
        }
    )
])

add_detection_antibody = MethodTemplate("add_detection_antibody", [
    ActionTemplate(
        resource=bravo_96,
        command="run",
        inputs=[plate_1, tips_96],
        options={
            "protocol": "add_detection_antibody.pro",
        }
    )
])

incubate_1hr = MethodTemplate("incubate_1hr", [
    ActionTemplate(resource=shaker_collection,
        command="shake",
        inputs=[plate_1],
        options={
            "shake_time": 3600
        }
    )])

pre_transfer_wash = MethodTemplate("pre_transfer_wash", [
    ActionTemplate(resource=biotek_2,
        command="run",
        inputs=[plate_1],
        options={
            "protocol": "pre_transfer_wash.pro"
        }
    )])

discard_supernatant = MethodTemplate("discard_supernatant", [
    ActionTemplate(resource=biotek_2,
           command="run",
        inputs=[plate_1],
        options={
            "protocol": "discard_supernatant.pro"
        }
    )
])
add_elution_buffer_b = MethodTemplate("add_elution_buffer_b", [
    ActionTemplate(resource=bravo_384,
        command="run",
        inputs=[plate_1, tips_384],
        options={
            "protocol": "add_elution_buffer_b.pro",
            "deck_setup": {
                5: plate_1,
                9: buffer_b_reservoir
            }
        }
    )])
incubate_10min = MethodTemplate("incubate_10min", [
    ActionTemplate(resource=shaker_collection,
        command="shake",
        inputs=[plate_1],
        options={
            "shake_time": 600
        }
    )])



add_buffer_d = MethodTemplate("add_buffer_d", [
    ActionTemplate(resource=bravo_384,
        command="run",
        inputs=[plate_1, tips_384],
        options={
            "protocol": "add_buffer_d.pro",
            "deck_setup": {
                9: plate_1
            }
        }
    )])

combine_plates = MethodTemplate("combine_plates", [
    ActionTemplate(resource=bravo_384,
        command="run",
        inputs=[plate_1, final_plate, tips_384],
        options={
            "protocol": "combine_plates.pro",
            "deck_setup": {
                5: plate_1,
                9: final_plate
            }
        }
    )])

transfer_eluate = MethodTemplate("transfer_eluate", [
    ActionTemplate(resource=bravo_384,
           command="run",
        inputs=[final_plate, tips_384],
        options={
            "protocol": "transfer_eluate.pro",
        }
    )])

centrifuge_method = MethodTemplate("centrifuge", [
    ActionTemplate(resource=centrifuge,
        command="spin",
        inputs=[final_plate],
        options={
            "speed": 2000,
            "time": 1200
        }
    )
])

read = MethodTemplate("read", [
    ActionTemplate(resource=smc_pro,
        command="read",
        inputs=[final_plate],
        options={
            "protocol": "read.pro",
            "filepath": "results.csv"
        }
    )]
)

delid = MethodTemplate("delid", [
    ActionTemplate(resource=delidder,   
        command="delid",
        inputs=[AnyLabwareTemplate()],
    )
])

# create a list of all the methods to later build the system with
methods = [
    sample_to_bead_plate_method,
    incubate_2hrs,
    post_capture_wash,
    add_detection_antibody,
    incubate_1hr,
    pre_transfer_wash,
    discard_supernatant,
    add_elution_buffer_b,
    incubate_10min,
    add_buffer_d,
    combine_plates,
    transfer_eluate,
    centrifuge_method,
    read,
    delid
]

# Build your labware threadds
# Labware threads are the set of methods which you expect your labware to pass through
# If your labware interactes with another piece of labware, use a JunctionMethodTemplate() at that step, you will then define this interaction further within the workflow
# Labware threads can usually be throught of as a single piece of labware from which other labwares spawn
plate_1_thread = ThreadTemplate(
    plate_1,
    map.get_location("stacker_3"),
    map.get_location("waste_1"),
    [
    sample_to_bead_plate_method, 
    incubate_2hrs,
    post_capture_wash,
    add_detection_antibody,
    incubate_1hr,
    pre_transfer_wash,
    discard_supernatant,
    add_elution_buffer_b, 
    incubate_10min,
    add_buffer_d,
    combine_plates
])


sample_plate_thread = ThreadTemplate(
    sample_plate,
    map.get_location("stacker_1"),
    map.get_location("stacker_2"),
    [
    delid,
    JunctionMethodTemplate(),
])

final_plate_thread = ThreadTemplate(
    final_plate,
    map.get_location("stacker_4"),
    map.get_location("plate_hotel"),
    methods=[
    JunctionMethodTemplate(),
    transfer_eluate,
    centrifuge_method,
    read
])

tips_96_thread = ThreadTemplate(
    tips_96,
    map.get_location("stacker_5"),
    map.get_location("waste_1"),
    [
    delid,
    JunctionMethodTemplate(),
])

tips_384_thread = ThreadTemplate(
    tips_384,
    map.get_location("stacker_6"),
    map.get_location("stacker_7"),
    [
    delid,
    JunctionMethodTemplate(),
])


# Define your workflow
# Your workflow defines how your labware threads interact with each other
smc_workflow = WorkflowTemplate("smc_assay")

# Add each other your labware threads to the workflow
# Be sure to define which threads should start when the workflow starts
smc_workflow.add_thread(plate_1_thread, True) # Starts when the workflow starts
smc_workflow.add_thread(sample_plate_thread)
smc_workflow.add_thread(final_plate_thread)
smc_workflow.add_thread(tips_96_thread)
smc_workflow.add_thread(tips_384_thread)

# Define the spawn points of the workflow
# A spawn point is a point in the workflow where a new thread is created
# Spawn points are attached to another running thread, in this case, the plate_1_thread - the main thread of the workflow
# The spawn point will create a new thread when the main thread reaches the method defined in the spawn point
# The 'join' parameter here tells the workflow whether or not to join the newly created thread to the main thread via a JunctionMethodTemplate
smc_workflow.set_spawn_point(sample_plate_thread, plate_1_thread, sample_to_bead_plate_method, True)
smc_workflow.set_spawn_point(tips_96_thread, plate_1_thread, sample_to_bead_plate_method, True)
smc_workflow.set_spawn_point(tips_96_thread, plate_1_thread, add_detection_antibody, True)
smc_workflow.set_spawn_point(tips_384_thread, plate_1_thread, add_elution_buffer_b, True)
smc_workflow.set_spawn_point(tips_384_thread, plate_1_thread, add_buffer_d, True)
smc_workflow.set_spawn_point(tips_384_thread, plate_1_thread, combine_plates, True)
smc_workflow.set_spawn_point(final_plate_thread, plate_1_thread, combine_plates, True)
smc_workflow.set_spawn_point(tips_384_thread, final_plate_thread, transfer_eluate, True)


# This an example of an event handler - An event handler subscribes to events in the system and can perform actions when those events occur.
# Event handlers have access to the system API and can modify the system state.
# These are not a requirement, but they can be useful for more complex workflows and for creating your own plugins in the future, a powerful feature of Orca for integrating and building out your automation system.
# In this case, we are creating a system bound event handler that spawns a new thread on the fourth spawn of the tips_384_thread. 
class SpawnNewOnFourthPlate(SystemBoundEventHandler):
    """A system bound event handler that spawns a new thread on the fourth spawn of the tips_384_thread.
    This handler is attached to the THREAD.CREATED event and checks if the thread created is the tips_384_thread.
    If it is, it will wait for the previous thread to complete before setting the start location of the new thread to the end location of the previous thread.
    This allows the workflow to spawn a new thread on the fourth spawn of the tips_384_thread, which is used to transfer the eluate from the final plate to the waste.

    Args:
        SystemBoundEventHandler (_type_): A base class for event handlers that are bound to the system.
        attach_thread (ThreadTemplate): The thread template to attach to the event handler. This is the thread that will be spawned on the fourth spawn of the tips_384_thread.
    """
    def __init__(self, attach_thread: ThreadTemplate):
        self._attach_thread = attach_thread
        self._previous_thread: ExecutingLabwareThread | None = None
        self._num_of_spawns = 0
    
    def handle(self, event: str, context: ExecutionContext) -> None:
        """Handles the THREAD.CREATED event by checking if the created thread is the one we are interested in."""
        assert isinstance(context, ThreadExecutionContext), "Context must be of type ThreadExecutionContext"
        if event == "THREAD.CREATED" and context.thread_name == self._attach_thread.name:
            self._handle_thread_created_event(context)
    
    def _handle_thread_created_event(self, context: ThreadExecutionContext):
        """Handles the THREAD.CREATED event by checking if the thread is the one we are interested in and setting the start location of the new thread."""
        workflow = self.system.get_executing_workflow(context.workflow_id)
        thread = workflow.thread_manager.get_executing_thread(context.thread_id)
        if self._num_of_spawns % 4 != 0:
            asyncio.create_task(self._await_previous_thread_completion_and_set_start(thread, context))
        else:
            # if this is the first spawn, we don't have a previous thread
            # or if this is the fourth spawn, we allow the thread to end normally
            self._previous_thread = thread
        self._num_of_spawns += 1        
    
    async def _await_previous_thread_completion_and_set_start(self, thread: ExecutingLabwareThread, context: ThreadExecutionContext):
        """Awaits the completion of the previous thread and sets the start location of the new thread to the end location of the previous thread."""
        if self._previous_thread is None:
            return
        while self._previous_thread.status != LabwareThreadStatus.COMPLETED:
            await asyncio.sleep(1)
        thread_instance = self.system.create_and_register_thread_instance(self._attach_thread)
        thread_instance.start_location = self._previous_thread.end_location
        workflow_context = WorkflowExecutionContext(context.workflow_id, context.workflow_name)
        new_thread = self.system.create_executing_thread(thread_instance.id, workflow_context)
        self._previous_thread = new_thread

        
            
# Create an instance of the SpawnNewOnFourthPlate event handler and add it to the workflow 
tips_384_spawner = SpawnNewOnFourthPlate(tips_384_thread)
# Add all event hooks to the workflow by subscribing to the THREAD.CREATED event
smc_workflow.add_event_hook("THREAD.CREATED", tips_384_spawner)
smc_workflow.add_event_hook("THREAD.CREATED", SpawnNewOnFourthPlate(final_plate_thread))

# Create an event bus to handle events in the system
event_bus = EventBus()
# Set all the components to build the system
builder = SdkToSystemBuilder(
    "SMC Assay",
    "SMC Assay",
    labwares,
    resource_registry,
    map,
    methods,
    [smc_workflow],
    event_bus
)
# Build the system
system = builder.get_system()


# Use the WorkflowExecutor to run the workflow
async def run():
    orca_logger.info("Starting SMC Assay workflow execution.")
    executor = WorkflowExecutor(smc_workflow, system)
    await executor.start()
    orca_logger.info("SMC Assay workflow completed.")

# Use the StandalonMethodExecutor to run a single method of your workflow
# You must define where each plate starts and ends to be able to run the method independently of the workflow
# You can use this to run a method independently of the workflow, which is useful for testing or debugging purposes
async def run_method():
    orca_logger.info("Starting Sample to Bead Plate method execution.")
    executor = StandalonMethodExecutor(
        sample_to_bead_plate_method,
        {
            sample_plate: "stacker_4",
            tips_96: "stacker_5",
            plate_1: "stacker_3"
        },
        {
            sample_plate: "stacker_2",
            tips_96: "waste_1",
            plate_1: "stacker_3"
        },
        system,
    )
    await executor.start()
    orca_logger.info("Sample to Bead Plate method completed.")

# Orca supports parallel processing
# Here we run both the workflow and the method in parallel
async def run_both_in_parallel() -> None:
    await asyncio.gather(
        run(),
        run_method()
    )

if __name__ == "__main__":
    asyncio.run(run())
    # asyncio.run(run_method())
    # asyncio.run(run_both_in_parallel())
    orca_logger.info("Run completed successfully.")
    time.sleep(2)  # Allow time for logging to complete before exiting
