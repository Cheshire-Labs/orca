import asyncio
import os
import logging
import sys
import time
from typing import Any, List
from orca.driver_management.drivers.simulation_labware_placeable.simulation_labware_placeable import SimulationDeviceDriver
from orca.driver_management.drivers.simulation_robotic_arm.simulation_robotic_arm import SimulationRoboticArmDriver
from orca.resource_models.base_resource import Device
from orca.resource_models.labware import AnyLabwareTemplate, LabwareTemplate
from orca.resource_models.resource_pool import EquipmentResourcePool
from orca.resource_models.transporter_resource import TransporterEquipment
from orca.sdk.SdkToSystemBuilder import SdkToSystemBuilder
from orca.sdk.events.event_bus import EventBus
from orca.sdk.events.event_handlers import Spawn, SystemBoundEventHandler
from orca.sdk.events.execution_context import ExecutionContext, ThreadExecutionContext, WorkflowExecutionContext
from orca.system.resource_registry import ResourceRegistry
from orca.system.system_map import SystemMap
from orca.workflow_models.action_template import MethodActionTemplate
from orca.workflow_models.labware_threads.executing_labware_thread import ExecutingLabwareThread
from orca.workflow_models.labware_threads.labware_thread import LabwareThreadInstance
from orca.workflow_models.method_template import JunctionMethodTemplate, MethodTemplate
from orca.workflow_models.status_enums import LabwareThreadStatus
from orca.workflow_models.thread_template import ThreadTemplate
from orca.workflow_models.workflow_templates import WorkflowTemplate

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout 
)

orca_logger = logging.getLogger("orca")

sample_plate = LabwareTemplate(name="sample_plate", type="Matrix 96 Well")
plate_1 = LabwareTemplate(name="plate_1", type="Corning 96 Well")
final_plate = LabwareTemplate(name="final_plate", type="SMC 384 plate")
bead_reservoir = LabwareTemplate(name="bead_reservoir", type="reservoir")
buffer_b_reservoir = LabwareTemplate(name="buffer_b_reservoir", type="reservoir")
buffer_d_reservoir = LabwareTemplate(name="buffer_d_reservoir", type="reservoir")
detection_reservoir = LabwareTemplate(name="detection_reservoir", type="reservoir")
tips_96 = LabwareTemplate(name="tips_96", type="96 Tips")
tips_384 = LabwareTemplate(name="tips_384", type="384 Tips")

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

# TRANSPORTERS
teachpoints_dir = "examples\\smc_assay"
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

# EQUIPMENT
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

# COLLECTIONS
shaker_collection = EquipmentResourcePool(name="shaker_collection", resources=[shaker_1, shaker_2, shaker_3, shaker_4, shaker_5, shaker_6, shaker_7, shaker_8, shaker_9, shaker_10])

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

def initialize_all_transporters(resource_registry: ResourceRegistry):
    async def _run_all():
        await asyncio.gather(*(t.initialize() for t in resource_registry.transporters))
    asyncio.run(_run_all())

initialize_all_transporters(resource_registry)

map = SystemMap(resource_registry)
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





# METHODS

sample_to_bead_plate_method = MethodTemplate(
    name="sample_to_bead_plate",
    actions=[
        MethodActionTemplate(
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
        MethodActionTemplate(
            resource=shaker_collection,
            command="shake",
            inputs=[plate_1],
            options={
                "shake_time": 7200
            }
        )
])

post_capture_wash = MethodTemplate("post_capture_wash", [
    MethodActionTemplate(
        resource=biotek_1,
        command="run",
        inputs=[plate_1],
        options={
            "protocol": "post_capture_wash.pro",
        }
    )
])

add_detection_antibody = MethodTemplate("add_detection_antibody", [
    MethodActionTemplate(
        resource=bravo_96,
        command="run",
        inputs=[plate_1, tips_96],
        options={
            "protocol": "add_detection_antibody.pro",
        }
    )
])

incubate_1hr = MethodTemplate("incubate_1hr", [
    MethodActionTemplate(resource=shaker_collection,
        command="shake",
        inputs=[plate_1],
        options={
            "shake_time": 3600
        }
    )])

pre_transfer_wash = MethodTemplate("pre_transfer_wash", [
    MethodActionTemplate(resource=biotek_2,
        command="run",
        inputs=[plate_1],
        options={
            "protocol": "pre_transfer_wash.pro"
        }
    )])

discard_supernatant = MethodTemplate("discard_supernatant", [
    MethodActionTemplate(resource=biotek_2,
           command="run",
        inputs=[plate_1],
        options={
            "protocol": "discard_supernatant.pro"
        }
    )
])
add_elution_buffer_b = MethodTemplate("add_elution_buffer_b", [
    MethodActionTemplate(resource=bravo_384,
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
    MethodActionTemplate(resource=shaker_collection,
        command="shake",
        inputs=[plate_1],
        options={
            "shake_time": 600
        }
    )])



add_buffer_d = MethodTemplate("add_buffer_d", [
    MethodActionTemplate(resource=bravo_384,
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
    MethodActionTemplate(resource=bravo_384,
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
    MethodActionTemplate(resource=bravo_384,
           command="run",
        inputs=[final_plate, tips_384],
        options={
            "protocol": "transfer_eluate.pro",
        }
    )])

centrifuge_method = MethodTemplate("centrifuge", [
    MethodActionTemplate(resource=centrifuge,
        command="spin",
        inputs=[final_plate],
        options={
            "speed": 2000,
            "time": 1200
        }
    )
])

read = MethodTemplate("read", [
    MethodActionTemplate(resource=smc_pro,
        command="read",
        inputs=[final_plate],
        options={
            "protocol": "read.pro",
            "filepath": "results.csv"
        }
    )]
)

delid = MethodTemplate("delid", [
    MethodActionTemplate(resource=delidder,   
        command="delid",
        inputs=[AnyLabwareTemplate()],
    )
])


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


threads = [
    plate_1_thread,
    sample_plate_thread,
    final_plate_thread,
    tips_96_thread,
    tips_384_thread
]

smc_workflow = WorkflowTemplate("smc_assay")
smc_workflow.add_thread(plate_1_thread, True)
smc_workflow.add_thread(sample_plate_thread)
smc_workflow.add_thread(final_plate_thread)
smc_workflow.add_thread(tips_96_thread)
smc_workflow.add_thread(tips_384_thread)


smc_workflow.set_spawn_point(sample_plate_thread, plate_1_thread, sample_to_bead_plate_method, True)
smc_workflow.set_spawn_point(tips_96_thread, plate_1_thread, sample_to_bead_plate_method, True)
smc_workflow.set_spawn_point(tips_96_thread, plate_1_thread, add_detection_antibody, True)
smc_workflow.set_spawn_point(tips_384_thread, plate_1_thread, add_elution_buffer_b, True)
smc_workflow.set_spawn_point(tips_384_thread, plate_1_thread, add_buffer_d, True)
smc_workflow.set_spawn_point(tips_384_thread, plate_1_thread, combine_plates, True)
smc_workflow.set_spawn_point(final_plate_thread, plate_1_thread, combine_plates, True)
smc_workflow.set_spawn_point(tips_384_thread, final_plate_thread, transfer_eluate, True)


# class SpawnNewOnFourthPlateOld(SystemBoundEventHandler):
#     def __init__(self, spawn_thread: ThreadTemplate, parent_threads: List[ThreadTemplate], parent_methods: List[MethodTemplate]) -> None:
#         self._spawn_thread = spawn_thread
#         self._parent_threads = parent_threads
#         self._parent_methods = parent_methods
#         self._num_of_spawns = 0
    
#     def handle(self, event: str, context: Any) -> None:
#         assert isinstance(context, MethodExecutionContext), "Context must be of type MethodExecutionContext"
#         if context.thread_name not in [t.name for t in self._parent_threads]:
#             return
#         if context.method_name not in [m.name for m in self._parent_methods]:
#             return
#         if event == "METHOD.IN_PROGRESS":
#             thread = self.system.create_thread_instance(self._spawn_thread, context)
#             if self._num_of_spawns % 4 != 0:
#                 thread.start_location = thread.end_location
#             self.system.add_thread(thread)
#             self._num_of_spawns += 1



class SpawnNewOnFourthPlate(SystemBoundEventHandler):
    def __init__(self, attach_thread: ThreadTemplate):
        self._attach_thread = attach_thread
        self._previous_thread: ExecutingLabwareThread | None = None
        self._num_of_spawns = 0
    
    def handle(self, event: str, context: ExecutionContext) -> None:
        assert isinstance(context, ThreadExecutionContext), "Context must be of type ThreadExecutionContext"
        if event == "THREAD.CREATED" and context.thread_name == self._attach_thread.name:
            self._handle_thread_created_event(context)
    
    def _handle_thread_created_event(self, context: ThreadExecutionContext):
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
        if self._previous_thread is None:
            return
        while self._previous_thread.status != LabwareThreadStatus.COMPLETED:
            await asyncio.sleep(1)
        thread_instance = self.system.create_and_register_thread_instance(self._attach_thread)
        thread_instance.start_location = self._previous_thread.end_location
        workflow_context = WorkflowExecutionContext(context.workflow_id, context.workflow_name)
        new_thread = self.system.create_executing_thread(thread_instance.id, workflow_context)
        self._previous_thread = new_thread

        
            

tips_384_spawner = SpawnNewOnFourthPlate(tips_384_thread)
smc_workflow.add_event_hook("THREAD.CREATED", tips_384_spawner)
smc_workflow.add_event_hook("THREAD.CREATED", SpawnNewOnFourthPlate(final_plate_thread))

event_bus = EventBus()
builder = SdkToSystemBuilder(
    "SMC Assay",
    "SMC Assay",
    labwares,
    resource_registry,
    map,
    methods,
    [smc_workflow],
    threads,    
    event_bus
)

system = builder.get_system()
workflow_instance = system.create_and_register_workflow_instance(smc_workflow)
system.add_workflow(workflow_instance)
workflow = system.get_executing_workflow(workflow_instance.id)


async def run():
    await workflow.start()
    orca_logger.info("SMC Assay workflow completed.")

if __name__ == "__main__":
    asyncio.run(run())
    orca_logger.info("Run completed successfully.")
    time.sleep(2)  # Allow time for logging to complete before exiting
