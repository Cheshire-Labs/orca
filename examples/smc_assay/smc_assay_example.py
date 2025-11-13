import asyncio
import os
import logging
import sys
import time

from orca.devices.centrifuge import Centrifuge
from orca.devices.devices import Delidder, LiquidHandler, PlateWasher, Reader, Storage, Waste
from orca.devices.shaker import Shaker
from orca.driver_management.drivers.sims import SimCentrifugeDriver, SimDelidderDriver, SimLiquidHandlerDriver, SimPlateWasherDriver, SimReaderDriver, SimShakerDriver, SimStorageDriver, SimTransporterDriver, SimWasteDriver
from orca.resource_models.transporter import Transporter
from orca.resource_models.plate_pad import PlatePad
from orca.sdk.system import SdkToSystemBuilder, WorkflowExecutor, ResourceRegistry, SystemMap, ExecutingLabwareThread, StandalonMethodExecutor
from orca.sdk.workflow import WorkflowTemplate, ThreadTemplate, MethodTemplate, SharedMethodTemplate
from orca.sdk.events import EventBus, SystemBoundEventHandler, ExecutionContext, ThreadExecutionContext, WorkflowExecutionContext, LabwareThreadStatus
from orca.sdk.devices import ResourcePool, A4SSealer
from orca.sdk.labware import AnyLabwareTemplate, PlateTemplate, TipRackTemplate
from orca.sdk.actions import Spin, Delid, Read, RunProtocol, Shake


from pylabrobot.resources.agenbio import AGenBio_1_troughplate_190000uL_Fl
from pylabrobot.resources.biorad import BioRad_384_wellplate_50uL_Vb
from pylabrobot.resources.corning.falcon.plates import Cor_Falcon_96_wellplate_340ul_Fb_Black
from pylabrobot.resources.hamilton.tip_racks import hamilton_96_tiprack_10uL_filter

# Setup a logger (Optional)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout 
)
orca_logger = logging.getLogger("orca")


# Create your labware
sample_plate = PlateTemplate("sample_plate",  Cor_Falcon_96_wellplate_340ul_Fb_Black) # TODDO: Create ThermoFisher Matrix 96 Definition
plate_1 = PlateTemplate("plate_1",  Cor_Falcon_96_wellplate_340ul_Fb_Black)
final_plate = PlateTemplate("final_plate",  BioRad_384_wellplate_50uL_Vb) # TODO: Create an SMC compliant 384 plate definition
bead_reservoir = PlateTemplate("bead_reservoir",  AGenBio_1_troughplate_190000uL_Fl) # Not really needed for this example, but included for completeness
buffer_b_reservoir = PlateTemplate("buffer_b_reservoir",  AGenBio_1_troughplate_190000uL_Fl) # Not really needed for this example, but included for completeness
buffer_d_reservoir = PlateTemplate("buffer_d_reservoir",  AGenBio_1_troughplate_190000uL_Fl) # Not really needed for this example, but included for completeness
detection_reservoir = PlateTemplate("detection_reservoir",  AGenBio_1_troughplate_190000uL_Fl) # Not really needed for this example, but included for completeness
tips_96 = TipRackTemplate("tips_96",  hamilton_96_tiprack_10uL_filter, True) # TODO: Hamilton tips for now
tips_384 = TipRackTemplate("tips_384", hamilton_96_tiprack_10uL_filter, True)

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
ddr1_points = os.path.join(teachpoints_dir, "ddr1.json")
ddr2_points = os.path.join(teachpoints_dir, "ddr2.json")
ddr3_points = os.path.join(teachpoints_dir, "ddr3.json")
translator1_points = os.path.join(teachpoints_dir, "translator1.json")
translator2_points = os.path.join(teachpoints_dir, "translator2.json")
ddr_1 = Transporter("ddr_1", SimTransporterDriver("ddr"), load_positions=ddr1_points)
ddr_2 = Transporter("ddr_2", SimTransporterDriver("ddr"), load_positions=ddr2_points)
ddr_3 = Transporter("ddr_3", SimTransporterDriver("ddr"), load_positions=ddr3_points)
translator_1 = Transporter("translator_1", SimTransporterDriver("translator"), load_positions=translator1_points)
translator_2 = Transporter("translator_2", SimTransporterDriver("translator"), load_positions=translator2_points)

# These are devices capable of reciving labware
biotek_1 = PlateWasher("biotek", SimPlateWasherDriver("biotek"))
biotek_2 = PlateWasher("biotek_2", SimPlateWasherDriver("biotek_2"))
bravo_96 = LiquidHandler("bravo_96", SimLiquidHandlerDriver("bravo_96"))
bravo_384 = LiquidHandler("bravo_384", SimLiquidHandlerDriver("bravo_384"))
sealer = A4SSealer("sealer", "COM3", sim=True)
centrifuge = Centrifuge("centrifuge", SimCentrifugeDriver("centrifuge"), True)
plate_hotel = Storage("plate_hotel", SimStorageDriver("plate_hotel"))
delidder = Delidder("delidder", SimDelidderDriver("delidder"))
smc_pro = Reader("smc_pro", SimReaderDriver("smc_pro"))
stacker_sample_start = Storage("stacker_simple_start", SimStorageDriver("stacker_simple_start"))
stacker_sample_end = Storage("stacker_sample_end", SimStorageDriver("stacker_sample_end"))
stacker_plate_1_start = Storage("stacker_plate_1_start", SimStorageDriver("stacker_plate_1_start"))
stacker_final_plate_start = Storage("stacker_final_plate_start", SimStorageDriver("stacker_final_plate_start"))
stacker_96_tips = Storage("stacker_96_tips", SimStorageDriver("stacker_96_tips")) 
stacker_384_tips_start = Storage("stacker_384_tips_start", SimStorageDriver("stacker_384_tips_start"))
stacker_384_tips_end = Storage("stacker_384_tips_end", SimStorageDriver("stacker_384_tips_end"))
shaker_1 = Shaker("shaker_1", SimShakerDriver("shaker_1"), True)
shaker_2 = Shaker("shaker_2", SimShakerDriver("shaker_2"), True)
shaker_3 = Shaker("shaker_3", SimShakerDriver("shaker_3"), True)
shaker_4 = Shaker("shaker_4", SimShakerDriver("shaker_4"), True)
shaker_5 = Shaker("shaker_5", SimShakerDriver("shaker_5"), True)
shaker_6 = Shaker("shaker_6", SimShakerDriver("shaker_6"), True)
shaker_7 = Shaker("shaker_7", SimShakerDriver("shaker_7"), True)
shaker_8 = Shaker("shaker_8", SimShakerDriver("shaker_8"), True)
shaker_9 = Shaker("shaker_9", SimShakerDriver("shaker_9"), True)
shaker_10 = Shaker("shaker_10", SimShakerDriver("shaker_10"), True)
waste_1 = Waste("waste_1", SimWasteDriver("waste_1"))
waste_2 = Waste("waste_2", SimWasteDriver("waste_2"))

# Build any resource pools - Orca will resolve what resource to use once it reaches that step
shaker_collection = ResourcePool("shaker_collection", resources=[shaker_1, shaker_2, shaker_3, shaker_4, shaker_5, shaker_6, shaker_7, shaker_8, shaker_9, shaker_10])

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
    smc_pro,
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
    waste_2,
    shaker_collection
])


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
    "smc_pro": smc_pro,
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
    "waste_2": waste_2,
    # Mark translator waypoints as not supporting deadlock resolution
    # These are transit points between robots, not parking locations
    "translator_1_start": PlatePad("translator_1_start", supports_deadlock_resolution=False),
    "translator_1_end": PlatePad("translator_1_end", supports_deadlock_resolution=False),
    "translator_2_start": PlatePad("translator_2_start", supports_deadlock_resolution=False),
    "translator_2_end": PlatePad("translator_2_end", supports_deadlock_resolution=False),
})


# Build your methods
# Methods are a collection of actions
# Each action takes in set of labware and then run a command on the labware
# All labware must be present at the resource before the actions runs
sample_to_bead_plate_method = MethodTemplate(
    name="sample_to_bead_plate",
    actions=[
        RunProtocol(bravo_96,
                    "sample_to_bead_plate.pro",
                    {},
                    [sample_plate, tips_96, plate_1],
                    [sample_plate, tips_96, plate_1]
                    )
    ]
)

incubate_2hrs = MethodTemplate("incubate_2hrs",
    [
        Shake(
            shaker_collection,
            7200,
            800,
            inputs=[plate_1],
            outputs=[plate_1],
        )
])

post_capture_wash = MethodTemplate("post_capture_wash", [
    RunProtocol(
        biotek_1,
        "post_capture_wash.pro",
        {},
        [plate_1],
        [plate_1]
    )
])

add_detection_antibody = MethodTemplate("add_detection_antibody", [
    RunProtocol(
        bravo_96,
        "add_detection_antibody.pro",
        {},
        [plate_1, tips_96],
        [plate_1, tips_96]
    ),
])

incubate_1hr = MethodTemplate("incubate_1hr", [
    Shake(
        shaker_collection,
        3600,
        800,
        [plate_1],
        outputs=[plate_1]
    )
    ])

pre_transfer_wash = MethodTemplate("pre_transfer_wash", [
    RunProtocol(biotek_2,
        "pre_transfer_wash.pro",
        {}, 
        [plate_1],
        [plate_1]
    ),
    ])

discard_supernatant = MethodTemplate("discard_supernatant", [
    RunProtocol(
        biotek_2,
        "discard_supernatant.pro",
        {},
        [plate_1],
        [plate_1]
    ),
])
add_elution_buffer_b = MethodTemplate("add_elution_buffer_b", [
    RunProtocol(bravo_384,
        "add_elution_buffer_b.pro",
        {},
        [plate_1, tips_384],
        [plate_1, tips_384]
    )])
incubate_10min = MethodTemplate("incubate_10min", [
    Shake(
        shaker_collection,
        600, 
        800,
        inputs=[plate_1],
        outputs=[plate_1]
    )
])



add_buffer_d = MethodTemplate("add_buffer_d", [
    RunProtocol(bravo_384,
        "add_buffer_d.pro",
        {},
        [plate_1, tips_384],
        [plate_1, tips_384]
    ),
])

combine_plates = MethodTemplate("combine_plates", [
    RunProtocol(
        bravo_384,
        "combine_plates.pro",
        {},
        [plate_1, final_plate, tips_384],
        [plate_1, final_plate, tips_384]
    )
   ])

transfer_eluate = MethodTemplate("transfer_eluate", [
    RunProtocol(
        bravo_384,
        "transfer_eluate.pro",
        {},
        [final_plate, tips_384],
        [final_plate, tips_384]
    )
])

centrifuge_method = MethodTemplate("centrifuge", [
    Spin(
        centrifuge,
        1200,
        2000,
        [final_plate],
        [final_plate]
    )
])

read = MethodTemplate("read", [
    Read(
        smc_pro,
        "read.pro",
        "results.csv",
        [final_plate],
        [final_plate]
    )
    ]
)

delid = MethodTemplate("delid", [
    Delid(
        delidder,
        inputs=[AnyLabwareTemplate()],
        outputs=[AnyLabwareTemplate()],
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
# If your labware interactes with another piece of labware, use a SharedMethodTemplate() at that step, you will then define this interaction further within the workflow
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
    SharedMethodTemplate(),
])

final_plate_thread = ThreadTemplate(
    final_plate,
    map.get_location("stacker_4"),
    map.get_location("plate_hotel"),
    methods=[
    SharedMethodTemplate(),
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
    SharedMethodTemplate(),
])

tips_384_thread = ThreadTemplate(
    tips_384,
    map.get_location("stacker_6"),
    map.get_location("stacker_7"),
    [
    delid,
    SharedMethodTemplate(),
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
# The 'join' parameter here tells the workflow whether or not to join the newly created thread to the main thread via a SharedMethodTemplate
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
        thread.update_start_location(self._previous_thread.end_location)
        self._previous_thread = thread

        
            
# Create an instance of the SpawnNewOnFourthPlate event handler and add it to the workflow 
tips_384_spawner = SpawnNewOnFourthPlate(tips_384_thread)
# Add all event hooks to the workflow by subscribing to the THREAD.CREATED event
smc_workflow.add_event_handler("THREAD.CREATED", tips_384_spawner)
smc_workflow.add_event_handler("THREAD.CREATED", SpawnNewOnFourthPlate(final_plate_thread))

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
async def run(sim: bool):
    orca_logger.info("Starting SMC Assay workflow execution.")
    if not sim:
        await system.initialize_all()
    executor = WorkflowExecutor(smc_workflow, system)
    await executor.start(sim)
    orca_logger.info("SMC Assay workflow completed.")

# Use the StandalonMethodExecutor to run a single method of your workflow
# You must define where each plate starts and ends to be able to run the method independently of the workflow
# You can use this to run a method independently of the workflow, which is useful for testing or debugging purposes
async def run_method(sim: bool):
    orca_logger.info("Starting Sample to Bead Plate method execution.")
    if not sim:
        await system.initialize_all()
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
    await executor.start(sim)
    orca_logger.info("Sample to Bead Plate method completed.")

# Orca supports parallel processing
# Here we run both the workflow and the method in parallel
async def run_both_in_parallel(sim: bool) -> None:
    await asyncio.gather(
        run(sim),
        run_method(sim)
    )

if __name__ == "__main__":
    asyncio.run(run(True))
    # asyncio.run(run_method(True))
    # asyncio.run(run_both_in_parallel(True))
    orca_logger.info("Run completed successfully.")
    time.sleep(2)  # Allow time for logging to complete before exiting
