# 🐋 Orca: Lab Automation Scheduler

### Welcome to Orca!  

Orca is a laboratory automation scheduler designed from the ground up with development, testing, and integration in mind.

Orca is currently in development, so stay tuned for frequent updates.

<h1 id="warning"> ⚠️ WARNING ⚠️</h1>

Orca is currently in it's early beta release with limited functionality and without integrated drivers.  ***This code has only been tested with mocked drivers and has not been run on a live system.*** 

⚠️ **Live System Usage**: Connecting Orca to a driver running a live instrument is done at your own risk.  Please exercise caution to protect your personnel and equipment.

⚠️ **Stopping Orca**: To stop Orca, you need to terminate the program.  (Ctrl+C) 

Cheshire Labs is actively seeking laboratories interested in using Orca.  Please [contact Cheshire Labs](https://cheshirelabs.io/contact/) if you may be interested.

# 📚 Table of Contents

- [🚀 Features](#features)
- [⚡ Demo Quick Start](#quick-start)
- [💾 Installation](#installation)
- [🧰 Usage](#usage)
- [📋 Basic Structure](#basic-structure)
    - [Defining Labware](#defining-labware)
    - [Defining Devices](#defining-devices)
    - [Defining System](#defining-system)
    - [Defining Workflow](#defining-workflow)
    - [Building the System](#building-the-system)
    - [Running a Workflow or Method](#running-a-workflow-or-method)
- [🤖 Drivers List](#drivers-list)
- [🔨 Development](#development)
    - [Scripting](#scripting)
    - [Drivers](#drivers)
- [🤝 Contributing](#contributing)
- [📜 License](#license)
- [⭐ Need More?](#need-more)
- [☎️ Contact](#contact)


<h1 id="features">🚀 Features</h1>

💡 **Git & Diff Friendly**
    
You own your workflow, and it integrates seamlessly into your local git repo like any other code. Easily track changes with clear, diff-able workflows, making it simple to see what has changed since the last run.

💡 **Event Bus**

An event bus is provided to allow users to subscribe to events such as errors, completd actions, etc.  This provides a platform on which users can build custom integrations and plugins.

💡 **Parallel Processing**

Orca supports parallel processing.  Multiple labware threads run independently of each other.  Multiple Workflows and methods can run at the same time.

💡 **Modular Workflow Design**
    
Workflows are designed modularly by methods.  This allows you to easily swap methods, run entire workflows, or just run single methods within workflows.  Great for adaptability & testing!

💡 **Resource Pools**
    
Define a collection of resources from which Orca can dynamically select to execute actions within your workflow.

💡 **LLM Compatible**

The python SDK is clear enough that your favorite large language model can understand what’s going on and help you design your workflow.

💡 **Quickly Change Labware Start and End Locations**
    
Avoid reloading your plate store. Change the start point to a nearby plate pad and relaunch quickly.

💡 **Python Scripting**

No scheduler software fits every need. Orca offers powerful Python scripting to ensure your workflows perform as required.

💡 **Shareable Protocols**

Did you write an amazing Orca protocol?  Since it's python you can just share it with others and they can easily swap out your device setup for their own.

<h1 id="quick-start">⚡ Demo Quick Start</h1>

Be sure to reach through the prevoided examples:

- [SMC Assay](./examples/smc_assay/smc_assay_example.py) 
- [Simple Venus Method](./examples/simple_venus_example/simple_venus_example.py) 



### Demo
To see a quick demo of how orca works:
1. Be sure to read our [Warning](#warning) regarding Orca before running
2. Install Orca ``` pip install cheshire-orca ```
3. Install Orca's Venus driver ```pip install orca-driver-venus```
3. Run the provided example python files using python



<h1 id="installation">💾 Installation</h1>

1. Create Python vitual environment (Optional)
    ```bash
    python -m venv <env-name>
    <env-name>\Scripts\activate
    ```
2. Install via pip
    - Install from github
        ```bash
        pip install cheshire-orca
        ```

    - OR Download/Clone repo and install locally
        ```
        pip install -e <orca-root-directory>
        ```

To uninstall:
    
```bash
pip uninstall cheshire-orca
```

<h1 id="usage">🧰 Usage</h1> 

### Basic Overview

1. Define your labware
2. Define your devices and drivers
3. Define what teachpoints correspond to each device
4. Define your methods as a collection of actions
5. Define your labware threads as a collection of methods your labware should complete
6. Define your workflow as a collection of interactions between labware threads


<h1 id="basic-structure">📋 Basic Structure</h1>

### Components
- **Labware** - Specifies defintions of the labware types used on your system
- **Device** - A laboratory instrument or equipment that is capable of operating on a labware
- **Action** - Defines a single operation that a Device performs on a labware or multiple labware
- **Method** - A named collection of actions
- **Labware Thread** - Defines a sequence of methods that should be performed on a labware instance.
- **Workflow** - Defines how multiple labware threads should interact with each other


## Defining Labware
Define your labware using the Labware template and then add it to list of labwares.
```py
sample_plate = LabwareTemplate(
    name="sample_plate", 
    type="Matrix 96-well plate")
labwares = [
    sample_plate
    ]

```

## Defining Devices
Create the devices on your system.  Each device will need a driver.  Robotic arms, transporters, and other devics capable of moving devices need to be added as TransporterEquipment.
```py
venus_driver = VenusProtocolDriver(name="venus")
ml_star = Device(name="ml_star", driver=venus_driver)

sim_robotic_arm_driver = SimulationRoboticArmDriver(name="ddr_1_driver" mocking_type="ddr", teachpoints_filepath="ddr1_teachpoints.xml")
ddr_1 = TransporterEquipment(name="ddr_1", driver=sim_robotic_arm_driver)

```


Resource pools can also be created.  These are a colletion of resources that an action can be performed on.  The system will decide which resource to use once the labware gets to that step.
```py
shaker_collection = EquipmentResourcePool(name="shaker_collection", resources=[shaker_1, shaker_2, shaker_3, shaker_4, shaker_5, shaker_6, shaker_7, shaker_8, shaker_9, shaker_10])
```

Add all the resources and resource pools as a list to the resource registry

```py
resource_registry = ResourceRegistry()
resource_registry.add_resources(resources=[
    ml_star,
    ddr_1,
    shaker_collection
])
```

## Defining System

The system map contains a mapping of all the locations, which transporters can get to those locations, and what resource is at each location.

The system map can be initialized using the resource registry.  Each teachpoint from the transporters will create a location and name it after the teachpoint.


```py
map = SystemMap(resource_registry)
```
However, the map also needs to know what devices are located at each teach point.  Those must be defined using a dictionary.

```py
map.assign_resource({
    "teachpoint_name_1": ml_star,
    "teachpoint_name_2": ddr_1,
    "teachpoint_name_3": shaker_collection
})
```


## Defining Workflow

**Actions are the base unit of an operation on a single plate or collection of plates.**  The require a device or resource pool that operate a command on a single plate or collection labwares.  Inputs are plates that enter the device and the outputs are plates coming off the device.  If no outputs are entered, it's assuemd they are the same as the inputs, unless an empty list is provided.  Extra parameters may be delivered to the command via the options parameter.  

If a resource pool is provided to the action, the system will determine the resource to use at the time of execution.
```py
ActionTemplate(
            resource=ml_star,
            command="run",
            inputs=[sample_plate],
            outputs=[sample_plate],
            options={}
            )
```

**Actions make up a Method.  A method is just a collection of actions.**  These are used to build labware threads.  Methods can also be run by themselves.
```py
example_method_1 = MethodTemplate(
    name="example_method_1",
    actions=[]
    )
```

**A labware thread defines what methods an instance of labware must complete.**  Labware threads determine the path which a labware instance takes.  A starting location and ending location should also be provided.

When building labware threads, it's usually best to think of a main thread and other threads spawning from that thread.

If a labware instance needs to interact with another labware instance (such as tips, a transfer, etc), then one of the labware instances should include a 'JunctionMethodTemplate' where they interact.  At runtime, the workflow will replace the 'JunctionMethodTemplate' with an instance of the shared method.

```py
sample_plate_thread = ThreadTemplate(
    labware=sample_plate,
    start=map.get_location("plate_pad_1"),
    end=map.get_location("plate_pad_2"),
    methods=[
        example_method_1,
        transfer_method
    ]
)
transfer_plate_thread = ThreadTemplate(
    labware=transfer_plate,
    start=map.get_location("plate_pad_3"),
    end=map.get_location("plate_pad_4"),
    methods=[
        delid,
        JunctionMethodTemplate()
    ]
)
```
**The workflow defines how different labware threads interact with each other.**  Workflows are collection of threads with 1 or more threads set as start or main threads.  These threads start when the workflow starts.  They are set with the 'is_start' option set to True.

Spawn points and workflow-level event handlers are also set here.

Spawn points are set with a thread to spawn when another thread reaches a designated method.  When this happens, the method will emit a 'created' event and the thread will spawn.  If you set the 'join' option, the spawning thread will set the method to be shared between the threads.  The 'JunctionMethodTemplate' needs to be on the spawning thread.

Event handlers can also be set here.  These are custom functions or EventHandler class that run based on emitted events.
```py
example_workflow = WorkflowTemplate(name="example_workflow")
example_workflow.add_thread(thread=sample_plate_thread, is_start=True) # Starts when the workflow starts
example_workflow.add_thread(thread=transfer_plate_thread)
example_workflow.set_spawn_point(spawn_thread=transfer_plate_thread, from_thread=sample_plate_thread, at=transfer_method, join= True)

```

## Building the System
A builder is provided to help compile the components to build the system.  The following components are required.

An event bus must also be created.  Custom functions and event handlers can be subscribed to differnet event emissions here.

```py
event_bus = EventBus()

builder = SdkToSystemBuilder(
    name="Venus Example",
    description="Venus Example System",
    labwares=labwares,
    resources=resource_registry,
    system_map=map,
    methods=methods,
    workflows=[example_workflow],
    event_bus=event_bus
)

system = builder.get_system()

```

## Running a Workflow or Method

To run a workflow, create an executor object to run the workflow in context of the system that was created.
```py
async def run():
    await system.initialize_all()
    executor = WorkflowExecutor(example_workflow, system)
    await executor.start()

asyncio.run(run())
```

To run a method by iteself, create an executor object to run thte method in the context of the system.  A starting and ending position of each labware going into and coming out of the method must also be provided.

```py
async def run_method():
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
asyncio.run(run_method())
```

Orca also supports parallel processing to run multiple workflows or methods at once

```py
async def run_both_in_parallel() -> None:
    await asyncio.gather(
        run(), 
        run_method()
    )
asyncio.run(run_both_in_parallel())
```
<h1 id="drivers-list">🤖 Drivers List</h1>

| Driver Name             | Description                                   | Repo                                                                 | Equipment                | Manufacturer      | Python Import Example                                                                                   |
|-------------------------|-----------------------------------------------|----------------------------------------------------------------------|--------------------------|-------------------|---------------------------------------------------------------------------------------------------------|
| Venus Protocol          | Driver for Hamilton Venus software            | [orca-driver-venus](https://github.com/cheshire-labs/orca-driver-venus) | MLSTAR, Vantage, etc     | Hamilton          | `from venus_driver.venus_driver import VenusProtocolDriver`                                             |
| Human Transfer          | Transporter that requests a human to move the labware | [orca-core (built-in)](https://github.com/cheshire-labs/orca-core)   | Human                    | Perhaps god? TBD  | `from orca.driver_management.drivers.simulation_robotic_arm.human_transfer import HumanTransferDriver`   |
| Simulation Robotic Arm  | Mock driver to simulate a robotic arm       | [orca-core (built-in)](https://github.com/cheshire-labs/orca-core)   | Robotic Arm              | N/A (Simulation)  | `from orca.driver_management.drivers.simulation_robotic_arm.simulation_robotic_arm import SimulationRoboticArmDriver` |
| Simulation Device       | Mock driver to simulate a device              | [orca-core (built-in)](https://github.com/cheshire-labs/orca-core)   | Device                   | N/A (Simulation)  | `from orca.driver_management.drivers.simulation_device.simulation_device import SimulationDeviceDriver`   |


<h1 id="development">🔨 Development</h1>

## Scripting

Scripting is necessary in lab automation for situations involving fine control over the process.

Scripting is done via event handlers.  These can either be a function or a class that inherits from the EventHandler base class.

Functions must take in a string and event context and return None.  

```py
def method_in_progress_handler(self, event: str, context: ExecutionContext) -> None:
    assert isinstance(context, MethodExecutionContext), "Context must be of type MethodExecutionContext"
    assert context.method_id is not None, "Method ID must be provided in the context for Spawn event handler"
    if context.method_name != self._parent_method.name:
        return

    if event == "METHOD.IN_PROGRESS":
        print("Method is in progress")

```

or they must inherit the EventHandler class.  This class provides an ISystem API to interact with Orca.  This is accessed via the base class ```self.system```.

This is an example of the internal event handler responsible for spawning a new thread.
```py
class Spawn(SystemBoundEventHandler):
    def __init__(self, spawn_thread: ThreadTemplate, parent_workflow_id: str, parent_method: MethodTemplate, join_method: bool = False) -> None:
        self._spawn_thread = spawn_thread
        self._parent_workflow_id = parent_workflow_id
        self._parent_method = parent_method
        self._join_method = join_method

    def handle(self, event: str, context: ExecutionContext) -> None:
        assert isinstance(context, MethodExecutionContext), "Context must be of type MethodExecutionContext"
        assert context.method_id is not None, "Method ID must be provided in the context for Spawn event handler"
        if context.method_name != self._parent_method.name:
            return

        if event == "METHOD.IN_PROGRESS":
            workflow = self.system.get_executing_workflow(self._parent_workflow_id)
            if self._join_method:
                method = self.system.get_executing_method(context.method_id)
                self._spawn_thread.set_wrapped_method(method)
            thread_instance = self.system.create_and_register_thread_instance(self._spawn_thread)
            workflow.add_and_start_thread(thread_instance)
```

You subscribe to events by using their event names.  Event names are emitted as ```{emitter_type}.{emitter_id}.{emitter_status}```.  You can either subscribe to a collection of events or a specific emitter type, but you need the id for the specific emitter.

This example would run everytime a method is completed:
```py
event_bus.subscribe("METHOD.COMPLETED", your_event_handler)
```

This example would run only when the Action with that id is created:
```py
event_bus.subscribe("ACTION.1134ce0c-ea25-4c93-929a-4d1a4f07509a.CREATED", your_event_handler)
```


## Drivers

***These drivers have now been moved to a new repo... more information to follow soon***

**Driver Types**
- **IInitializeableDriver(ABC)** - Base class for drivers that can only be initialized.
- **IDriver(IInitializeableDriver)** - Base class for drivers that can execute commands.
- **ILabwarePlaceableDriver(IDriver)** - Equipment that may have labware placed at the equipment.
- **ITransporterDriver(IDriver)** - Equipment capable of transporting labware items.



<h1 id="contributing">🤝 Contributing</h1>

Thank you for your interest in contributing!

Please read over the [contributing documentation](./CONTRIBUTING).

Please Note: Cheshire Labs follows an open core business model, offering Orca under a dual license structure. To align with this model and the AGPL license, contributors need to submit a contributor license agreement.


<h1 id="license">📜 License</h1> 

This project is released to under [AGPLv3 license](./LICENSE).  

Plugins, scripts, and drivers are considered derivatives of this project.

To obtain an alternative license [contact Cheshire Labs](https://cheshirelabs.io/contact/).

<h1 id="need-more">⭐ Need More?</h1>

Please [contact Cheshire Labs](https://cheshirelabs.io/contact/t) if you're looking for:
- More Features
- A Graphical Interface
- Driver Development
- Hosted Cloud Environment
- Help Setting Up Your System 
- Custom Scripting

<h1 id="contact">☎️ Contact</h1>

[Cheshire Labs Contact](https://cheshirelabs.io/contact/)

or contact a Cheshire Labs maintainer


