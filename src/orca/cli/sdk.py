class ConfigBuilder():
    pass


builder = ConfigBuilder()

builder.set_info(SystemInfo(
    name="amp-seq-system",
    version="1.0.0",
    description="Amp Seq System Example"
))

builder.add_labware(Labware(
    name="source-plate", 
    type="Biorad Half skirt"
))
builder.add_labware(Labware(
    name="destination-plate", 
    type="Biorad Half skirt"
))

builder.add_resource(Resource(
    name="robot-1", 
    type="ddr",
    positions=load_file("mock_teachpoints.xml"), 
    sim=True
))

builder.add_resource(Resource(
    name="mlstar",
    type="venus",
    plate_pad="hamilton_1",
))
builder.add_resource(Resource(
    name="mlstar",
    type="venus",
    plate_pad="hamilton_2",
))

builder.add_config(
    name="prod",
    config={
        "stage": "prod",
        "numOfPlates": 4,
        "waterVol": 40.5,
        "dyeVol": 5.5,
        "tipEjectPos": 1,  # waste tips
        "asp": {
            "clld": 5,  # turn on liquid sensing
            "liquidClass": None
        },
        "disp": {
            "clld": 5
        },
        "wait": 300,
        "intParam": 1,
        "strParam": "Production"
    }
)
builder.add_config(
    name="wetTest",
    config={
        "stage": "Water Test",
        "numOfPlates": 1,
        "waterVol": 5.2,
        "dyeVol": 5.2,
        "tipEjectPos": 2,  # return tips
        "asp": {
            "clld": 5  # turn on liquid sensing
        },
        "disp": {
            "clld": 5
        },
        "wait": 1,
        "intParam": 2,
        "strParam": "Wet Testing"
    }
)

builder.add_config(
    name="dryTest",
    config={
        "stage": "Dry Test",
        "numOfPlates": 1,
        "waterVol": 0,
        "dyeVol": 0,
        "tipEjectPos": 2,  # return tips
        "asp": {
            "clld": 0  # turn off liquid sensing
        },
        "disp": {
            "clld": 0
        },
        "wait": 1,
        "intParam": 3,
        "strParam": "Dry Testing"
    }
)

builder.add_method(name="plate-stamp").add_action(
    Action(
        resource="mlstar",
        command="run",
        inputs=[
            labware.get_labware("source-plate"),
            labware.get_labware("destination-plate"),
        ]
        method="Cheshire Labs\\SimplePlateStamp.hsl"
        params={
            "numOfPlates": config.get_value("numOfPlates"),
            "stage": config.get_value("stage")
            "waterVol": config.get_value("waterVol"),
            "dyeVol": config.get_value("dyeVol"),
            "clld": config.get_value("asp.clld"),
            "wait": config.get_value("wait"),
            "tipEjectPos": config.get_value("tipEjectPos"),
        }
    )
)
builder.add_method(name="variable-access").add_action(
    Action(
        resource="mlstar-2",
        command="run",
        inputs=[
            labware.get_labware("destination-plate"),
        ],
        method="Cheshire Labs\\VariableAccess.hsl"
        params={
            "intParam": config.get_value("intParam"),
            "strParam": config.get_value("strParam")
        }
    )
)

builder.add_workflow(name="test-workflow").add_thread(
    name="dna-plate",
    labware="source-plate",
    type="start",
    start="pad_1",
    end="pad_2",
    ).add_step(
        method="plate-stamp",
        spawn=["destination-plate"]
    )

builder.get_workflow("test-workflow").add_thread("product-plate").add_step(
    name="product-plate",
    type="wrapper",
    start="pad_3",
    end="pad_4").add_wrapper_step().add_step(
        method="variable-access")





