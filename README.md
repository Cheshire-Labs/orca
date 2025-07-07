# 🐋 Orca: Lab Automation Scheduler

### Welcome to Orca!  

Orca is a laboratory automation scheduler designed from the ground up with development, testing, and integration in mind.

Orca is currently in development, so stay tuned for frequent updates.

***HAVE FEEDBACK?***

***NEED A DEVICE SUPPORTED?***

[Contact Us](#contact)

<h1 id="warning"> ⚠️ WARNING ⚠️</h1>

***This code has only been tested with mocked drivers and has not been run on a live system.*** 

⚠️ **Live System Usage**: Connecting Orca to a driver running a live instrument is done at your own risk.  Please exercise caution to protect your personnel and equipment.

⚠️ **Stopping Orca**: To stop Orca, you need to terminate the program.  (Ctrl+C) 

Cheshire Labs is seeking laboratories interested in using Orca.  Please [contact Cheshire Labs](https://cheshirelabs.io/contact/) if you may be interested.

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
        - [Actions](#actions)
            - [Actions Lits](#actions-list)
        - [Methods](#methods)
        - [Labware Threads](#labware-threads)
        - [Workflows](#workflows)
    - [Building the System](#building-the-system)
    - [Running a Workflow or Method](#running-a-workflow-or-method)
- [🤖 Device List](#device-list)
    - [Venus](#venus)
    - [Human Transfer](#human-transfer)
    - [pyLabRobot](#pylabrobot-device)
- [🔨 Development](#development)
    - [Scripting](#scripting)
    - [Drivers](#drivers)
- [🙏 Acknowledgements](#acknowledgements)
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

Be sure to read through the provided examples.  Each contains comments about how the workflow is set up:

- [SMC Assay](./examples/smc_assay/smc_assay_example.py) - A large workflow that simulates an SMC assay using simulated devices and drivers
- [Simple Venus Method](./examples/simple_venus_example/simple_venus_example.py) - A workflow that uses an active Venus driver (_requires the Venus driver to be installed_)
- [PyLabRobot Driver Example](./examples/pylabrobot_example/pylabrobot_example.py) - A workflow that uses a pyLabRobot driver (_requires pyLabRobot to be installed_)



### Demo
To see a quick demo of how orca works:
1. Be sure to read our [Warning](#warning) regarding Orca before running
2. Clone this repository and install Orca locally (we recommend this over pip for now, as things are changing frequently):
    ```bash
        git clone https://github.com/Cheshire-Labs/orca.git
        cd orca
        pip install -e .
    ```
3. Run the provided example python files using python
    ```bash
    python <path_to_example>.py
    ```
    - SMC Assay example 
    ```bash
    python ./examples/smc_assay/smc_assay_example.py
    ```



<h1 id="installation">💾 Installation</h1>

1. Create Python vitual environment (Optional)
    ```bash
    python -m venv <env-name>
    <env-name>\Scripts\activate
    ```
2. Install Orca
    - __It's recommended to install Orca directly from the repo as it's in rapid development__

    - Clone the repository and install locally:
    
        ```bash
        git clone https://github.com/Cheshire-Labs/orca.git
        cd orca
        pip install -e .
        ```

    - **OR** Install the latest published version from PyPI (***This method is not kept up-to-date***):
    
        ```bash
        pip install cheshire-orca
        ```

3. To uninstall Orca:
    
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
Orca uses pyLabRobot's Labware standard.  Labwares are created using a Labware template.  These can be PlateTemplate, TipRackTemplate, etc.

Labware Templates create a new labware instance when labware thread spawns.

To create a Labware template, select the pyLabRobot labware type you want to create and pass a reference to PLR's labware function.  The labware template will create and instance of that labware type when a labware thread is created.
```py
from orca.sdk.labware import PlateTemplate, TipRackTemplate

from pylabrobot.resources.corning.falcon.plates import Cor_Falcon_96_wellplate_340ul_Fb_Black
from pylabrobot.resources.hamilton.tip_racks import LTF

sample_plate = PlateTemplate("sample_plate",  Cor_Falcon_96_wellplate_340ul_Fb_Black)
tips_96 = TipRackTemplate("tips_96",  LTF, True) 
labwares = [
    sample_plate,
    tips_96
    ]
```

## Defining Devices
Theres 2 types of equipment within Orca
- **Transporters** - Equipment capable of moving labware.  Orca builds a map from the teachpoints of transporters and will automatically use them to build  routes and move your labware around your system.
- **Devices** - Equipment capable of recieving labware and performing an operation on them.

Devices are setup as follows:

```py
from orca.sdk.devices import A4SSealer, MockDevice, MockTransporter

sealer = A4SSealer("sealer", "COM3", sim=True)
centrifuge = MockDevice("centrifuge", "centrifuge")
ddr_1 = MockTransporter("ddr_1", "ddr", "examples\\smc_assay\\teachpoints\\ddr1.xml")

```
 Generic devices (`Sealer`, `Shaker`, etc) can use pyLabRobot drivers.

```py
from orca.sdk.devices import Sealer
from pylabrobot.sealing.a4s_backend import A4SBackend

a4s_sealer_driver = A4SBackend(port="/dev/tty.usbserial-0001", timeout=10)
sealer = Sealer("a4s_sealer", a4s_sealer_driver)
```

Resource pools can also be created.  These are a colletion of resources that an action can be performed on.  The system will decide which resource to use once the labware gets to that step.
```py
shaker_collection = ResourcePool(name="shaker_collection", resources=[shaker_1, shaker_2, shaker_3, shaker_4, shaker_5, shaker_6, shaker_7, shaker_8, shaker_9, shaker_10])
```

Register the resources within the system by adding them to the Resource Registry

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
However, the map also needs to know what devices are located at each teach point.  Those must be defined using a dictionary.  These teachpoints must match the names of the teachpoints in your robotic arms and other transporting devices.

```py
map.assign_resource({
    "teachpoint_name_1": ml_star,
    "teachpoint_name_2": ddr_1,
    "teachpoint_name_3": shaker_collection
})
```


## Defining Workflow

### Actions

**Actions are the base unit of an operation on a single plate or collection of plates.**  

The device paramater tells the system what device will perform the action.  It also tells the system map where to perform the action.  If a resource pool is provided to the action, the system will determine the resource to use at runtime.

The input parameter defines plates that are needed to arrive at the resource to perform the action.  The action awaits all the labwares to arrive at the resource before executing the action.

The output parameter defines plates that are expected to leave the resource after the action is performed.  If no outputs are entered, it's assuemd they are the same as the inputs, unless an empty list is provided. 

The basic action is an ExecuteCommand action.  A list of actions can be find in the [Actions List](#actions-list) section:

```py
from orca.sdk.actions import ExecuteCommand

execute_run = ExecuteCommand(
            resource=ml_star,
            command="run",
            inputs=[sample_plate],
            outputs=[sample_plate],
            options={}
            )
```
### Actions List

 **ExecuteCommand** - Sends a command string and options dictionary to the resource's driver.  This is the generic action to allow users to just send a string and dictionary to a device driver.
    
- **command** (str) - string to be sent to the device's driver 
- **options** (dict) - key value pairs to be sent with the command string

 **Centrifuge** - Spins the labware using the provided resource.
- **speed** (int) - how fast to spin the centrifuge (rpm)
- **duration** (int) - how long to spin the centrifuge (seconds)
 
 **Delid** - Delids the labware using the provided resource.

 **Read** - Reads the plate using the provided resource.
 - **protocol_filepath** (str) - filepath to method/protocol to run on the device
 - **output_filepath** (str) - where to output the instrument's results
 
 **RunProtocol** - Runs a protocol file at the specified file path on the resource provided.
 - **protocol_filepath** (str) - path to a method/protocol to run
 - **parameters** (dict) - Dictionary of key-value pairs to pass to the method.  Different than options.  Options get passed to the device driver.  Parameters, in this case, get passed into the device's method. 
 
 **Shake** - Shakes the labware using the resource provided.
 - **speed** (int) - speed to shake the device (rpm)
 - **duration** (int) - how long to shake (seconds)
 
 **Seal** - Seals the labware using the resource provided.
 - **temperature** (int) - temperature at which to seal the plate (Celsius)
 - **duration** (float) - length of time to apply the seal (seconds)
 
 **PythonMethod** - Runs a python method once the labware gets to the specified device.
 - **method** (function) - This is the python function that will be executed once all input labware arrives at the device.  The python method should be written to accept the following paramaters at the time of execution:
    - **Device** - the device at which the function is being executed.
    - **List[LabwareInstance]** - the list of input labware instances
    - **List[LabwareInstance]** - the list of output labware instances
    - **Dict[str, Any]** - the action's options parameter


### Methods


**A method is just a sequence of actions.**  These are used to build labware threads.  Methods can also be run by themselves.
```py
example_method_1 = MethodTemplate(
    name="example_method_1",
    actions=[
        execute_run, 
        seal
        ]
    )
```

### Labware Threads

**A labware thread is a sequence of methods that need to be performed on a specified labware item.** 

A start location and end location is provided to a labware thread and then the labware travels through all the methods to get to the end position.  The route the labware travels is dynamically determined by the actions within the methods.

When building labware threads, it's usually best to think of a main thread and other threads spawning from that thread.

If a labware instance needs to interact with another labware instance (such as for a plate transfer), then one of the labware instances should include a 'SharedMethodTemplate' where they interact.  At runtime, the workflow will replace the 'SharedMethodTemplate' object with an instance of the shared method.

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
        SharedMethodTemplate()
    ]
)
```

### Workflow

**A workflow is a collection of labware threads with instructions on how they interact with each other.** 

Workflows must have 1 or more threads set as a start thread.  These threads start when the workflow starts.  They are set with the 'is_start' option set to True.

Spawn points and workflow-level event handlers are also set here.

Spawn points are set with a thread to spawn when another thread reaches a designated method.  If you set the 'join' option, the spawning thread will set the method to be shared between the threads.  The 'SharedMethodTemplate' needs to be on the spawning thread.

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
<h1 id="device-list">🤖 Device List</h1>

| Driver Name            | Description                           | Equipment <br> Manufacturer            | Status     | Import Example               |
| ---------------------- | ------------------------------------- | -------------------------------------- | ---------- | ---------------------------- |
| [Venus](#venus)        | Runs a Venus method.  Can also inject parameters to Benus.  | **MLSTAR, Vantage, etc** <br> Hamilton | 🟢 Stable  | `Venus`        |
| A4S Sealer | Azenta A4S Sealer (pyLabRobot)        | **A4S Sealer** <br> Azenta             | ⚪ Untested | `A4Sealer`                   |
| [Sealer](#pylabrobot-device) | Generic sealer that works with pyLabRobot Sealer backends | **Generic Sealer** <br> Any PLR SealerBackend | ⚪ Untested | `Sealer` |
| [Shaker](#pylabrobot-device) | Generic shaker that works with pyLabRobot Shaker backends | **Generic Shaker** <br> Any PLR ShakerBackend | ⚪ Untested | `Shaker` |
| [Human Transfer](#human-transfer)         | Requests a human to manually move labware      | **Human** <br> TBD                     | 🟢 Stable  | `HumanTransfer`        |
| Mock Transporter | Mocks a device capable of moving labware | **Robotic Arm** <br> N/A (Simulation)  | 🟢 Stable  | `MockTransporter` |
| Mock Device     | Mocks a Device     | **Device** <br> N/A (Simulation)       | 🟢 Stable  | `MockDevice`     |

**Need a device to be supported? [Reach out](#contact)**

## Venus
The Venus device driver will run a Hamilton Venus method to operate a Hamilton MLSTAR, MLSTARlet, Vantage, etc

The Venus device driver can also pass parameters to be used within your Venus method.  The parameters can be retrieved using the [Orca Venus Submethod](./src/orca/driver_management/drivers/venus/venus_submethod/)

**Venus Initialization**

- name (str): The name of the Venus device.

- init_protocol (Optional[str]): The protocol to run when Orca initializes the device.

- picked_protocol (Optional[str]): The protocol to run when labware is picked.

- placed_protocol (Optional[str]): The protocol to run when labware is placed.

- prepare_pick_protocol (Optional[str]): The protocol to prepare for picking labware.

- prepare_place_protocol (Optional[str]): The protocol to prepare for placing labware.

- exe_path (str): Path to the HxRun executable on your computer. Defaults: `C:\Program Files (x86)\HAMILTON\Bin\HxRun.exe`

- methods_folder (str): Path to the folder containing Venus methods.  This is prepended to the protocol paths.  Defaults: `C:\Program Files (x86)\HAMILTON\Methods`

- sim (bool): Whether to use simulation mode.

**Orca Venus Submethod** 

Once Orca starts your Venus Method, this submethod can be used to get the values of the parameter dictionary that was passed by Orca.



- Initialize(useDefaultValues: int) - initializes the submethod
    - useDefaultValues (int) 
        - 0 = Use values set by Orca.  Will throw an error if the value is not found.
        - 1 = Use the default values set for each GetConfigProperty - this is useful for when you want to run the Venus method without Orca integrated
    
- GetConfigProperty_Float(propertyName: string, defaultValue: float, out value: float)
- GetConfigProperty_Integer(propertyName: string, defaultValue: int, out value: int)
- GetConfigProperty_String(propertyName: string, defaultValue: string, out value: string)
    - propertyName (string) - name of the parameter passed by Orca
    - defaultValue (T) - defaults to this value when Submethod is initialized with 1
    - value (out T) - retrieved value of the parameter passed in by Orca

**Example**

In Orca:
```py
example_method_1 = MethodTemplate(
    "example_method_1",
    actions=[
        RunProtocol(resource=ml_star,
            method="Cheshire Labs\\VariableAccessTesting.hsl",
            parameters={
                    "strParam": "strParam value transmitted",
                    "intParam": 123,
                    "fltParam": 1.003
                },
            inputs=[sample_plate],
            outputs=[sample_plate])
    ]
)
```
In this example, the method VariableAccessTesting.hsl would be executed.  The parameter values can then be retrieve using the following methods in the submethod library within Venus.
- ORCA::GetConfigProperty_String("strParam", "default", value)
    - Result: value = "strParam value transmitted"
- ORCA::GetConfigProperty_Integer("intParam", "default", value)
    - Result: value = 123
- ORCA::GetConfigProperty_Float("fltParam", "default", value)
    - Result: value = 1.003

## Human Transfer
The human transfer device driver will wait print a prompt request the plate to be manually picked and place by the user.

It will then wait for the user to press Enter confirming they have picked or placed the plate the labware before continuing the workflow.

## pyLabRobot Device

Generic Devices accept pyLabRobot Backends.

Generic Devices:
- Sealer
- Shaker


Example:
```py
from orca.sdk.devices import Sealer
from pylabrobot.sealing.a4s_backend import A4SBackend

a4s_sealer_driver = A4SBackend(port="/dev/tty.usbserial-0001", timeout=10)
sealer = Sealer("a4s_sealer", a4s_sealer_driver)
```



<h1 id="development">🔨 Development</h1>

## Scripting

Scripting is necessary in lab automation for situations involving fine control over the process.  

__If you need help here please reach out.  Scripting will be simplified with future releases.__

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

More information will be provided for writing drivers once the interface is settled more.  If you need a driver, please reach out.

<h1 id="acknowledgements">🙏 Acknowledgements</h1>

Orca builds on the work of the [PyLabRobot](https://github.com/PyLabRobot/pylabrobot) community, who have been doing an excellent job standardizing and open-sourcing drivers and labware for laboratory automation. Orca recognizes and appreciates their commitment to open standards and interoperability.

If you use Orca in research, we encourage you to credit PyLabRobot with the following citation:
```
@article{WIERENGA2023100111,
    title = {PyLabRobot: An open-source, hardware-agnostic interface for liquid-handling robots and accessories},
    journal = {Device},
    volume = {1},
    number = {4},
    pages = {100111},
    year = {2023},
    author = {Rick P. Wierenga and Stefan M. Golas and Wilson Ho and Connor W. Coley and Kevin M. Esvelt},
    doi = {https://doi.org/10.1016/j.device.2023.100111},
}
```

<h1 id="contributing">🤝 Contributing</h1>

Thank you for your interest in contributing!

Please read over the [contributing documentation](./CONTRIBUTING).

Please Note: Cheshire Labs follows an open core business model, offering Orca under a dual license structure. To align with this model and the AGPL license, contributors need to submit a contributor license agreement.


<h1 id="license">📜 License</h1> 

This project is released to under [AGPLv3 license](./LICENSE).  

Plugins, scripts, and drivers are considered derivatives of this project.

To obtain an alternative license [contact Cheshire Labs](https://cheshirelabs.io/contact/).

<h1 id="need-more">⭐ Need More?</h1>

Please [contact Cheshire Labs](https://cheshirelabs.io/contact/) if you're looking for:
- More Features
- A Graphical Interface
- Driver Development
- Hosted Cloud Environment
- Help Setting Up Your System 
- Custom Scripting

<h1 id="contact">☎️ Contact</h1>

[Cheshire Labs Contact](https://cheshirelabs.io/contact/)

or contact a Cheshire Labs maintainer


