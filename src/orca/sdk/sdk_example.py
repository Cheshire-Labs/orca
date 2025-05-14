from orca.driver_management.drivers.simulation_labware_placeable.simulation_labware_placeable import SimulationLabwarePlaceableDriver
from orca.driver_management.drivers.simulation_robotic_arm.simulation_robotic_arm import SimulationRoboticArmDriver
from orca.sdk.sdk import EquipmentPool, Labware, LabwareRegistry, LocationRegistry, MethodRegistry, ResourceRegistry, Equipment, Location, Method, Action, Thread, ThreadRegistry, Workflow


sample_plate = Labware(name="sample_plate", type="Matrix 96 Well")
plate_1 = Labware(name="plate_1", type="Corning 96 Well")
final_plate = Labware(name="final_plate", type="SMC 384 plate")
bead_reservoir = Labware(name="bead_reservoir", type="reservoir")
buffer_b_reservoir = Labware(name="buffer_b_reservoir", type="reservoir")
buffer_d_reservoir = Labware(name="buffer_d_reservoir", type="reservoir")
detection_reservoir = Labware(name="detection_reservoir", type="reservoir")
tips_96 = Labware(name="tips_96", type="96 Tips")
tips_384 = Labware(name="tips_384", type="384 Tips")


# TRANSPORTERS
ddr_1 = Equipment(name="ddr_1", driver=SimulationRoboticArmDriver(name="ddr_1_driver", mocking_type="ddr", teachpoints_filepath="ddr1.xml"))
ddr_2 = Equipment(name="ddr_2", driver=SimulationRoboticArmDriver(name="ddr_2_driver", mocking_type="ddr", teachpoints_filepath="ddr2.xml"))
ddr_3 = Equipment(name="ddr_3", driver=SimulationRoboticArmDriver(name="ddr_3_driver", mocking_type="ddr", teachpoints_filepath="ddr3.xml"))
translator_1 = Equipment(name="translator_1", driver=SimulationRoboticArmDriver(name="translator_1_driver", mocking_type="translator", teachpoints_filepath="translator1.xml"))
translator_2 = Equipment(name="translator_2", driver=SimulationRoboticArmDriver(name="translator_2_driver", mocking_type="translator", teachpoints_filepath="translator2.xml"))

# EQUIPMENT
biotek_1 = Equipment(name="biotek_1", driver=SimulationLabwarePlaceableDriver(name="biotek_1_driver", mocking_type="biotek"))
biotek_2 = Equipment(name="biotek_2", driver=SimulationLabwarePlaceableDriver(name="biotek_2_driver", mocking_type="biotek"))
bravo_96_head = Equipment(name="bravo_96_head", driver=SimulationLabwarePlaceableDriver(name="bravo_96_head_driver", mocking_type="bravo"))
bravo_384_head = Equipment(name="bravo_384_head", driver=SimulationLabwarePlaceableDriver(name="bravo_384_head_driver", mocking_type="bravo"))
sealer = Equipment(name="sealer", driver=SimulationLabwarePlaceableDriver(name="sealer_driver", mocking_type="plateloc"))
centrifuge = Equipment(name="centrifuge", driver=SimulationLabwarePlaceableDriver(name="centrifuge_driver", mocking_type="vspin"))
plate_hotel = Equipment(name="plate_hotel", driver=SimulationLabwarePlaceableDriver(name="plate_hotel_driver", mocking_type="agilent_hotel"))
delidder = Equipment(name="delidder", driver=SimulationLabwarePlaceableDriver(name="delidder_driver", mocking_type="delidder"))
stacker_sample_start = Equipment(name="stacker_sample_start", driver=SimulationLabwarePlaceableDriver(name="stacker_sample_start_driver", mocking_type="vstack"))
stacker_sample_end = Equipment(name="stacker_sample_end", driver=SimulationLabwarePlaceableDriver(name="stacker_sample_end_driver", mocking_type="vstack"))
stacker_plate_1_start = Equipment(name="stacker_plate_1_start", driver=SimulationLabwarePlaceableDriver(name="stacker_plate_1_start_driver", mocking_type="vstack"))
stacker_final_plate_start = Equipment(name="stacker_final_plate_start", driver=SimulationLabwarePlaceableDriver(name="stacker_final_plate_start_driver", mocking_type="vstack"))
stacker_96_tips = Equipment(name="stacker_96_tips", driver=SimulationLabwarePlaceableDriver(name="stacker_96_tips_driver", mocking_type="vstack"))
stacker_384_tips_start = Equipment(name="stacker_384_tips_start", driver=SimulationLabwarePlaceableDriver(name="stacker_384_tips_start_driver", mocking_type="vstack"))
stacker_384_tips_end = Equipment(name="stacker_384_tips_end", driver=SimulationLabwarePlaceableDriver(name="stacker_384_tips_end_driver", mocking_type="vstack"))
shaker_1 = Equipment(name="shaker_1", driver=SimulationLabwarePlaceableDriver(name="shaker_1_driver", mocking_type="shaker"))
shaker_2 = Equipment(name="shaker_2", driver=SimulationLabwarePlaceableDriver(name="shaker_2_driver", mocking_type="shaker"))
shaker_3 = Equipment(name="shaker_3", driver=SimulationLabwarePlaceableDriver(name="shaker_3_driver", mocking_type="shaker"))
shaker_4 = Equipment(name="shaker_4", driver=SimulationLabwarePlaceableDriver(name="shaker_4_driver", mocking_type="shaker"))
shaker_5 = Equipment(name="shaker_5", driver=SimulationLabwarePlaceableDriver(name="shaker_5_driver", mocking_type="shaker"))
shaker_6 = Equipment(name="shaker_6", driver=SimulationLabwarePlaceableDriver(name="shaker_6_driver", mocking_type="shaker"))
shaker_7 = Equipment(name="shaker_7", driver=SimulationLabwarePlaceableDriver(name="shaker_7_driver", mocking_type="shaker"))
shaker_8 = Equipment(name="shaker_8", driver=SimulationLabwarePlaceableDriver(name="shaker_8_driver", mocking_type="shaker"))
shaker_9 = Equipment(name="shaker_9", driver=SimulationLabwarePlaceableDriver(name="shaker_9_driver", mocking_type="shaker"))
shaker_10 = Equipment(name="shaker_10", driver=SimulationLabwarePlaceableDriver(name="shaker_10_driver", mocking_type="shaker"))
waste_1 = Equipment(name="waste_1", driver=SimulationLabwarePlaceableDriver(name="waste_1_driver", mocking_type="waste"))

# COLLECTIONS
shaker_collection = EquipmentPool(name="shaker_collection", resources=[shaker_1, shaker_2, shaker_3, shaker_4, shaker_5, shaker_6, shaker_7, shaker_8, shaker_9, shaker_10])


l = LocationRegistry()
l.build_from_transporters([ddr_1, ddr_2, ddr_3])
l.assign_resources({
    "biotek-1": biotek_1,
    "biotek-2": biotek_2,
    "bravo-96-head": bravo_96_head,
    "bravo_384_head": bravo_384_head,
    "sealer": sealer,
    "centrifuge": centrifuge,
    "plate_hotel": plate_hotel,
    "delidder": delidder,
    "ddr_1": ddr_1,
    "ddr_2": ddr_2,
    "ddr_3": ddr_3,
    "translator_1": translator_1,
    "translator_2": translator_2,
    "stacker_sample_start": stacker_sample_start,
    "stacker_sample_end": stacker_sample_end,
    "stacker_plate_1_start": stacker_plate_1_start,
    "stacker_final_plate_start": stacker_final_plate_start,
    "stacker_96_tips": stacker_96_tips,
    "stacker_384_tips_start": stacker_384_tips_start,
    "stacker_384_tips_end": stacker_384_tips_end,
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





# ACTIONS



methods = MethodRegistry()

sample_to_bead_plate_method = Method(
    name="sample_to_bead_plate",
    actions=[
        Action(
            resource=bravo_96_head,
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

incubate_2hrs = Method("incubate_2hrs",
    [
    Action(
        resource=shaker_collection,
        command="shake",
        inputs=[plate_1],
        options={
            "shake_time": Variable("incubation_time", default=7200)
        }
    )
])

post_capture_wash = Method("post_capture_wash", [
    Action(
        resource=biotek_1,
        command="run",
        inputs=[plate_1],
        options={
            "protocol": "post_capture_wash.pro"
        }
    )
])

add_detection_antibody = Method("add_detection_antibody", [
    Action(
        resource=bravo_96_head,
        command="run",
        inputs=[plate_1, tips_96],
        options={
            "protocol": "add_detection_antibody.pro",
        }
    )
])

incubate_1hr = Method("incubate_1hr", [
    Action(resource=shaker_collection,
        command="shake",
        inputs=[plate_1],
        options={
            "shake_time": Variable("incubation_time", default=3600)
        }
    )])

pre_transfer_wash = Method("pre_transfer_wash", [
    Action(resource=biotek_2,
        command="run",
        inputs=[plate_1],
        options={
            "protocol": "pre_transfer_wash.pro"
        }
    )])

discard_supernatant = Method("discard_supernatant", [
    Action(resource=biotek_2,
           command="run",
        inputs=[plate_1],
        options={
            "protocol": "discard_supernatant.pro"
        }
    )
])
add_elution_buffer_b = Method("add_elution_buffer_b", [
    Action(resource=bravo_384_head,
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
incubate_10min = Method("incubate_10min", [
    Action(resource=shaker_collection,
        command="shake",
        inputs=[plate_1],
        options={
            "shake_time": Variable("incubation_time", default=600)
        }
    )])



add_buffer_d = Method("add_buffer_d", [
    Action(resource=bravo_384_head,
        command="run",
        inputs=[plate_1, tips_384],
        options={
            "protocol": "add_buffer_d.pro",
            "deck_setup": {
                9: plate_1
            }
        }
    )])

combine_plates = Method("combine_plates", [
    Action(resource=bravo_384_head,
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

transfer_eluate = Method("transfer_eluate", [
    Action(resource=bravo_384_head,
           command="run",
        inputs=[final_plate, tips_384],
        options={
            "protocol": "transfer_eluate.pro",
        }
    )])

centrifuge_method = Method("centrifuge", [
    Action(resource=centrifuge,
        command="spin",
        inputs=[final_plate],
        options={
            "speed": 2000,
            "time": 1200
        }
    )
])

read = Method("read", [
    Action(resource=smc_reader,
        command="read",
        inputs=[final_plate],
        options={
            "protocol": "read.pro",
            "filepath": "results.csv"
        }
    )]
)

delid = Method("delid", [
    Action(resource=delidder,   
        command="delid",
        inputs=["any"],
    )
])




plate_1_thread = Thread(
    labware=plate_1
)
plate_1_thread.set_start("stacker_plate_1_start")
plate_1_thread.set_end("waste_1")
plate_1_thread.add_steps([
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


sample_plate_thread = Thread(
    labware=sample_plate
)
sample_plate_thread.set_start("stacker_sample_start")
sample_plate_thread.set_end("waste_1")
sample_plate_thread.add_steps([
    delid,
    WrapperMethod(),
])

final_plate_thread = Thread(
    labware=final_plate
)
final_plate_thread.set_start("stacker_final_plate_start")
final_plate_thread.set_end("plate_hotel")
final_plate_thread.add_steps([
    WrapperMethod(),
    transfer_eluate,
    centrifuge_method,
    read
])

tips_96_thread = Thread(
    labware=tips_96
)
tips_96_thread.set_start("stacker_96_tips")
tips_96_thread.set_end("waste_1")
tips_96_thread.add_steps([
    delid,
    WrapperMethod(),
])

tips_384_thread = Thread(
    labware=tips_384
)
tips_384_thread.set_start("stacker_384_tips_start")
tips_384_thread.set_end("stacker_384_tips_end")
tips_384_thread.add_steps([
    delid,
    WrapperMethod(),
])

smc_workflow = Workflow("smc_assay")
smc_workflow.add_threads([
    plate_1_thread,
    sample_plate_thread,
    final_plate_thread,
    tips_96_thread,
    tips_384_thread
])
smc_workflow.join(plate_1_thread, [sample_plate_thread, tips_96_thread], at="sample_to_bead_plate")
smc_workflow.join(plate_1_thread, [tips_96_thread], at="add_detection_antibody")
smc_workflow.join(plate_1_thread, [tips_96_thread], at="add_buffer_d")
smc_workflow.join(plate_1_thread, [tips_384_thread], at="combine_plates")
smc_workflow.join(plate_1_thread, [final_plate_thread, tips_384_thread], at="transfer_elute")
smc_workflow.get_method(sample_to_bead_plate_method).on_start(SpawnThread(sample_plate_thread))
smc_workflow.get_method(sample_to_bead_plate_method).on_start(SpawnThread(tips_96_thread))
smc_workflow.get_method("add_detection_antibody").on_start(SpawnThread(tips_96_thread))
smc_workflow.get_method(add_elution_buffer_b).on_start(SpawnThread(tips_96_thread))
smc_workflow.get_method(add_buffer_d).on_start(SpawnThread(tips_96_thread))
smc_workflow.get_method(combine_plates).on_start(SpawnThread(tips_384_thread))
smc_workflow.get_method(combine_plates).on_start(SpawnThread(final_plate_thread))

