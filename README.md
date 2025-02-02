# 🐋 Orca: Lab Automation Scheduler

### Welcome to Orca!  

Orca is a laboratory automation scheduler designed from the ground up with development, testing, and integration in mind.

<h1 id="warning"> ⚠️ WARNING ⚠️</h1>

🚧🚧🚧 

Orca is currently in it's early beta release with limited functionality and without integrated drivers.  ***This code has only been tested with mocked drivers and has not been run on a live system.*** 

Despite its current limitations, Cheshire Labs sees value in releasing the software at this stage for the following reason:
- It allows users to explore and experiment with the advantages of Orca
- It provides access for contributors and developers to build their own drivers
- It enables developers to contribute enhancements and new features to the software
- It enables Cheshire Labs to gather feedback and gauge industry interest to ensure Orca meets the needs of the lab automation sector

⚠️ **Live System Usage**: Connecting Orca to a driver running a live instrument is done at your own risk.  Please exercise caution to protect your personnel and equipment.

⚠️ **Stopping Orca**: To stop Orca, you need to terminate the program.  (Ctrl+C) 

Cheshire Labs is actively seeking laboratories interested in using Orca.  Please [contact Cheshire Labs](https://cheshirelabs.io/contact/) if you may be interested.

# 📚 Table of Contents

- [How it works](#how-it-works)
- [Features](#features)
- [Quick Start](#quick-start)
- [Installation](#installation)
- [Usage](#usage)
- [Command Line Commands](#command-line-commands)
    - [Deploy a Workflow](#deploy-a-workflow)
    - [Deploy a Single Method](#deploy-a-single-method)
    - [Load a Configuration File Only](#load-configuration-file-only)
    - [Initialize the System Only](#initialize-system-only)
    - [Exit the Shell](#exit)
- [Define Your Configuration File](#define-your-configuration-file)
    - [Configuration File Overview](#configuration-file-overview)
    - [System Section](#system-section)
    - [Labwares Section](#labwares-section)
    - [Resources Section](#resources-section)
        - [Resources](#resources)
        - [Resource Pools](#resource-pools)
    - [Config Section](#config-section)
    - [Methods Section](#methods-setion)
    - [Workflows Section](#workflows-section)
    - [Scripting Section](#scripting-section)
- [Development](#development)
    - [Scripting](#scripting)
    - [Drivers](#drivers)
    - [Plugins](#plugins)
- [Contributing](#contributing)
- [License](#license)
- [Need More?](#need-more)
- [Contact](#contact)

<h1 id="how-it-works">⚙️ How It Works</h1>

Orca simplifies complex lab automation workflows by allowing users to deploy their systems using just a configuration file. Streamline your automation setup like you would your cloud IT infrastructure.

Your configuration file defines different aspects of your workflow, including: labware, resources, system options, methods, and workflows. The configuration file supports the integration of custom scripts, and variables can be used to deploy different configurations depending on the context of the deployment.

Orca is currently operates in two modes, a server mode and an interactive command line. Users can choose to run entire workflows or individual methods as needed, offering flexibility to adapt to changing lab environments.

<h1 id="features">🚀 Features</h1>

💡 **Git & Diff Friendly**
    
You own your workflow, and it integrates seamlessly into your local git repo like any other code. Easily track changes with clear, diff-able workflows, making it simple to see what has changed since the last run.

💡 **Configurable Environments**

Deploy an environment to load a collection of variables across your entire workflow.  This helps to set things like shake times to 0 during testing and resetting them for production.

💡 **Modular Workflow Design**
    
Workflows are designed modularly by methods.  This allows you to easily swap methods, run entire workflows, or just run single methods within workflows.  Great for adaptability & testing!

💡 **Resource Pools**
    
Define a collection of resources from which Orca can dynamically select to execute actions within your workflow.

💡 **LLM Compatible**

Your configuration file is clear enough that your favorite large language model can understand what’s going on and help you design your workflow.

💡 **Quickly Change Labware Start and End Locations**
    
Avoid reloading your plate store. Change the start point to a nearby plate pad and relaunch quickly.

💡 **Python Scripting**

No scheduler software fits every need. Orca offers powerful Python scripting to ensure your workflows perform as required.

<h1 id="quick-start">⚡ Demo Quick Start</h1>

Orca can be run via a server or by command line.  It is suggested to use Orca with it's [VS Code Extension](https://github.com/Cheshire-Labs/orca-vs-code-ext) which can be installed via the VS Code Marketplace.

### Basic Overview

1. [Define your configuration file](#define-your-configuration-file)
2. Launch Orca command shell
3. Run your entire workflow or run a single method

### Demo
To see a quick demo of how orca works:
1. Be sure to read our [Warning](#warning) regarding Orca before running
2. Review Example Files:
    - [Example Configuration File](./examples/smc_assay/smc_assay_example.orca.yml) - Example Small Molecule Counting (SMC) Assay
    - [Example Spawn384TipsScript File](./examples/smc_assay/spawn_384_tips.py) - Scripting to get a new 384 tip box after all quadrants are used
    - [Example SpawnFinalPlateScript File](./examples/smc_assay/spawn_final_plate.py) - Scripting to get a new final plate after all quandrants are used
3. Download the [Examples Directory](./examples/) -> [Download Link](https://download-directory.github.io/?url=https%3A%2F%2Fgithub.com%2FCheshire-Labs%2Forca%2Ftree%2Fdev%2Fexamples) (Via https://download-directory.github.io/)
4. Unzip the directory
5. Open your command line
6. Install Orca
    ```bash
    pip install cheshire-orca
    ```


7. Launch the Orca Shell by typing the following into your command line
    ```bash
    orca
    ```
8. Run the example ```smc-assay``` workflow as defined under the [workflows](#workflows) section of the configuation file (All drivers are currently simulated).
    ```bash
    run --workflow smc-assay --config <path>/examples/smc_assay/smc_assay_example.orca.yml
    ```
    _Note: Be sure to use forward slashes ```/``` in the filepath_
9. Wait for the method to complete logging to the terminal.  This is a long, complicated method and will take almost 2 minutes to complete the log output.

10. Try running just a portion of the workflow, by selecting ```add-detection-antibody``` to run.  To do this you must define where each plate used by the method will begin and end using JSON for a start-map and end-map.
    ```bash
    run_method --method add-detection-antibody --start-map {\"plate-1\":\"pad_1\",\"tips-96\":\"pad_2\"} --end-map {\"plate-1\":\"pad_4\",\"tips-96\":\"pad_5\"} --config <path>/examples/smc_assay/smc_assay_example.orca.yml
    ```

11. Exit Orca
    ```bash
    exit
    ```



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

3. Start Orca's interactive command shell
    ```bash
    orca interactive
    ```

4. Exit Orca command shell
    ```bash
    exit
    ```


To uninstall:
    
```bash
pip uninstall cheshire-orca
```

<h1 id="usage">🧰 Usage</h1> 

### Basic Overview

1. [Define your configuration file](#define-your-configuration-file)
2. Launch Orca's interactive command shell
3. Run your entire workflow or run a single method

### Example
To run the [Example Configuration File](./examples/smc_assay/smc_assay_example.orca.yml)
1. Using python launch the Orca command shell from your command line

    ```bash
    orca interactive
    ```

2. Load your desired configuration file
    ```bash
     load --config <config_file_path>
    ```
2. Run the entire ```smc-assay``` workflow as defined under the [workflows](#workflows) section of the configuation file
    ```bash
    run --workflow smc-assay --stage test
    ```

3. OR instead of running an entire workflow, just run a single method.  To do this you must define where each plate used by the method will begin and end using JSON for a start-map and end-map
    ```bash
    run_method --method add-detection-antibody --start-map {\"plate-1\":\"pad_1\",\"tips-96\":\"pad_2\"} --end-map {\"plate-1\":\"pad_4\",\"tips-96\":\"pad_5\"} [--stage test
    ```

4. Exit Shell
    ```bash
    exit
    ```
---
<h1 id="server">Server</h1>
Orca can also be run as a server.  This server mode is used by VS Code Extension and features and API.

## Starting the server
```bash
orca server
```

---
<h1 id="command-line-commands">⌨️ Command Line Commands</h1>

## Starting the Shell

**Explanation** 

Orca is run from a shell interface.  This must be started before issuing commands.

**Starting Orca**

Start the Orca interactive shell:
```bash
orca interactive
```


**Help**

Type ```help``` or ```?``` by themselves to display available commands

Otherwise, use ```help <command>``` or ```? <command>``` to display help for a specific command

**Example**

```bash
help run_method 
```



## Deploy a Workflow
**Explanation**

Runs an entire workflow as defined in your configuration file

**Command**

```run```

**Usage**

```bash
run --workflow <workflow_name> [--config <path_to_config_file>] [--stage <development_stage>]
```

**Options**

* _--workflow <workflow_name>_
    
    Specifies the name of the workflow to be run. This name must match a workflow name defined in the configuration file.

* _--config <path_to_config_file>_
    
    Specifies the path to the YAML configuration file. If not provided, the previously loaded configuration is used. Required if the config file has not already been loaded.

* _--stage <development_stage>_

    Specifies the development stage to be run (e.g., prod, dev). If not provided, dev is run.

**Example** 

```bash
run --workflow smc-assay --config <path>/examples/smc_assay/smc_assay_example.orca.yml
```

## Deploy a Single Method

**Explanation**

Runs a single method defined within the configuration file

**Command**

```run_method```

**Usage**

```bash
run_method --method <method_name> --start-map <start_map_json> --end-map <end_map_json> [--config <path_to_config_file>] [--stage <development_stage>]
```

**Options**

* _--method <method_name>_

    Specifies the name of the method to be run. This name must match a method name defined in the configuration file.

* _--start-map <start_map_json>_

    A JSON dictionary of labware mapped to it’s desired starting location. Labware names must match labware names defined in the method specified within the configuration file.

* _--end-map <end_map_json>_

    A JSON dictionary of labware mapped to it’s desired starting location. Labware names must match labware names defined in the method specified within the configuration file.

* _--config <path_to_config_file>_

    Specifies the path to the YAML configuration file. If not provided, the previously loaded configuration is used.  Required if the config file as not already been loaded.

* _--stage <development_stage>_

    Specifies the development stage to be run (e.g., prod, dev). Optional, if not provided, dev is run.


**Example**
```bash
run_method --method add-detection-antibody --start-map {\"plate-1\":\"pad_1\",\"tips-96\":\"pad_2\"} --end-map {\"plate-1\":\"pad_4\",\"tips-96\":\"pad_5\"} --config <path>/examples/smc_assay/smc_assay_example.orca.yml
```

## Load Configuration File Only

**Explanation**

Loads the configuration file.  Used to ensure configuration file is valid.

**Command**

```load```

**Usage**

```bash
load --config <path_to_config_file>
```

**Options**
* _--config <path_to_config_file>_
    
    Specifies the path to the YAML configuration file.

**Example**

```bash
load --config <path>/examples/smc_assay/smc_assay_example.orca.yml
```

## Initialize System Only

**Explanation**

Initializes the resources defined in the configuration file

**Command**

```init```

**Usage**

```bash
init [--config <path_to_config_file>] [--resources <resource_list>]
```

**Options**
* _--config <path_to_config_file>_ 

    Specifies the path to the YAML configuration file. If not provided, the previously loaded configuration is used.

* _--resources <resource_list>_ 
    
    A comma-separated JSON list of resources to initialize. If not provided, all resources are initialized.

**Example**

```bash
init --config <path>/examples/smc_assay/smc_assay_example.orca.yml --resources [resource1, resource2]
```

## Exit


**Explanation**

Exit or quit the Orca shell

**Command**

```exit``` or ```quit```

**Usage**

```bash
quit
```

**Options**

None

**Example**

```bash
quit
```
---
---

<h1 id="define-your-configuration-file">📋 Define Your Configuration File</h1>

## Configuration File Overview

The Orca configuration file is a YAML file used to define various elements of your lab automation system.  It is the backbone of Orca, used to define everything in your system.

### Sections
- ```system``` Defines the overall system information, including name, version, and description
- ```labwares``` Specifies defintions of the labware types used in the workflow
- ```resources``` List of resources and resource pools, including initialization information for each resource
- ```config``` Defines deployable environments and associates a collection of variable values for that deployment
- ```methods``` Specifies inidividual methods and the actions to be performed with those methods
- ```workflows``` Defines workflows as a sequence of methods to be executed
- ```scripting``` Maps custom scripts to be used within the workflows

### Example
[Example SMC Assay Yml Configuration File](./examples/smc_assay/smc_assay_example.orca.yml)

## System Section


**Explanation**

Defines the overall system configuration

**Identifier**

```system```

**Structure**
- ```system:```
    - ```name: <name>``` - The name of the system
    - ```version: <version>``` - The version of Orca to use
    - ```description: <description>``` - A brief description of the system

**Example**

```yml
system:
    name: "smc-assay"
    version: "0.0.1"
    description: "SMC Assay Example"
```

## Labwares Section

**Explanation**

Provides defintions for the labware types used in the workflows

**Identifier**

```labwares```

**Structure**
- ```labwares:```
    - ```<name>:``` - Choose a name for your labware.  This will be the identifier of that labware throughout the configurations file.
        - ```type: <type>``` - A string specifiying the labware type.  Any equipment, such as a robot, that handles labware must have a matching labware definition in its programming.  
    _Note: This string must correspond to the identifier used in the equipment's labware defintions to ensure proper handling._

**Example**

```yml
labwares:
    plate-1:
        type: "Corning 96 Well"
    final-plate:
        type: "SMC 384 plate"
```

## Resources Section

### Resources
**Explanation**

Lists resources and resource pools, including initialization information for each resource.

Resources include lab equipment, such as robotic arms, liquid handlers, plate washers, etc, but they can also include network switches and waste positions.

**Identifier**

```resources```

**Structure**

- ```resources:```
    - ```<name>:``` - Choose a name for your resource.  This will be the identifier of that resource throughout the configurations file.
        - ```type: <type>``` - string used to discover the resource's driver
        - ```com: <com>``` - COM address of the resource _(com or ip is required)_
        - ```ip: <ip>``` - IP address of the resource _(com or ip is required)_
        - ```plate-pad: <teachpoint-name>``` - _(optional)_ Optional teachpoint name.  If name is not provided, the resource's name is used as the teachpoint name.  
        _Note: This string must match the teachpoint location identifier within the robotic plate transporters._  

**Example**

```yml
resources:
    bravo-96-head:
        type: bravo
        ip: 192.168.1.123
        plate-pad: "bravo_96"
```

### Resource Pools

**Explanation**

Resource pools are collections of resources that Orca may use to perform a certain task.

_Note: All resources identified in a resource poll must first be defined elsewhere in the resource section._

**Structure**
- ```resources:```
    - ```<name>: [<resources-list>]``` - Name your resource pool.  This name is used to identify your pool within methods.  Following your resource pool name, provide a YAML list of the resources to include in that resource pool.

    _Note: The resources included in the list must be defined in the resources section_

**Example**
```yml
resources:
    shakers: [shaker-1, shaker-2, shaker-3]
```
OR
```yml
resources:
    shakers:
        - shaker-1
        - shaker-2
        - shaker-3
```

## Config Section

**Explanation**

Defines deployable environments and an associated collection of variable values for those deployments.

Each environment is named and then acts as a dictionary lookup to find the values of variables else where within the configuration.

**Identifier**

```config```

**Structure**
- ```config:```
    - ```<config-name>:``` - Environment Identifier.  This is passed to Orca later when deploying the system 
        - ```<variable-name>: <value>``` - The environment identifier is followed by a dictionary of lookup values.  Here you identify variable names to be referenced elsewhere in the configuration file.  The values maybe be string, int, float, list, or dictionary.

**Defintion Example**
```yml
config:
    prod: 
        shake-time: 120
        bead-incubation-time: 7200
    test:
        shake-time: 0
        bead-incubation-time: 0
```

**Referencing Variables within the Workflow Example**

_Config File:_
```yml
methods:
    incubate-10-min: 
        actions:
            - shaker-1:
                command: shake
                inputs: [plate-1]
                shake-time: ${config:${opt:stage}.shake-time}
```

**Example Explanation**

When the configuration file is run with the command line option ```--stage prod```, the method will use the shake-time value from the “prod” configuration, which is 120. If the configuration file is run with the stage option ```--stage prod```, it will use the shake-time value from the “test” configuration, which is 0.

The method looks up the configuration module, finds the stage option value, and retrieves the corresponding shake-time value. This allows for flexible and dynamic adjustment of parameters based on the specified deployment stage.


**Benefit**

Use this when dry testing vs wet testing vs production to prevent having to adjust values for each deployment type.

## Methods Section

**Explanation**

Defines individual methods and the actions to be performed within those methods.  These methods are the building blocks of workflows.

**Identifier**

```methods```


### Method
**Structure**

- ```methods:```
    - ```<method-name>:``` - Method identifier used by the workflow section to reference the method
        - ```actions: <actions-list>``` - List of a sequence of action defintions to be performed

### Action

**Explanation**

An action object defines an action to be performed by a resource 

**structure**
- ```methods:```
    - ```<method-name>:```
        - ```actions:```
            - ```- <resource-name>``` - Specifies which resource or resource pool to use to perform the action
            
                **Notice the '-' character before ```<resource-name>```. Items under ```actions``` are a list and require the '-' character for each item.**
            - ```command: <command-string>``` - Command string to execute on the resource
            - ```inputs: [<labwares-list>]``` - List of expected labwares to arrive before an action executes
            - ```<extra-command-keys>:<extra-command-values>``` - A dictionary of extra command key-value pairs maybe appended here.  These will be passed to the resource's driver and handled by the logic within the driver.

**Example**

```yml
methods:
    sample-to-bead-plate:
        actions:
            - bravo-96-head:
              command: run
              inputs: [sample-plate, tips-96, plate-1]
              protocol: sample-to-bead.pro
```

**Example Explanation**

Here ```sample-to-bead-plate``` is the name of the method.  The method has one action: to run the protocol ```sample-to-bead-plate.pro```. After all labwares ```sample-plate```, ```tips-96```, and ```plate-1``` arrive at the ```bravo-96-head``` resource, the extra key-value pair ```protocol: sample-to-bead.pro``` is passed to the driver and the command ```run``` is executed. 


## Workflows Section


**Explanation**

Defines the workflows as a sequence of methods to be executed.  Additionally, the workflow includes options and scripting to be executed along the workflow.

**Identifier**

```workflows```

### Workflow

**Structure**
- ```workflows:```
    - ```<workflow-name>:``` - Workflow name.  Identifier used by Orca to run a workflow.
        - ```threads: <labware-threads-list>``` - list of labware thread definitions

### Labware Thread

**Explanation** 

Defines the process a labware is expected to flow through.  Certain properties are defined here, such as the labware, start position, end position, and scripts.  Ultimately, the process is defined with the ```steps``` property.

### Steps

**Explanation**
Defines the sequence of methods tho be executed on a labware item as proceeeds through the thread's process

**Structure**
- ```workflows:```
    - ```<workflow-name>:```
        - ```threads:```
            - ```<thread-name>:``` - Thread identifier 
                - ```labware: <labware-name>``` - Name of the labware.  This identifier must be defined in the labware section.
                - ```type: <type>``` - Thread type.  Current values include: _start, wrapper_.      
                    - _start_ threads begin execution as soon as the workflow starts. 
                    -  _wrapper_ threads are spawned by another thread when they're included in the ```spawn``` property described below.
                - ```start: <start-location>``` - The starting location Orca can find the labware at when the thread begins execution.
                - ```end: <end-location>``` - The ending location Orca should place the labware once workflow execution has completed.
                - ```scripts: <scripts-list>``` - _(Optional)_ A list of ordered scripts to be executed when the thread begins execution.
                - ```steps:``` - Sequence of [methods](#methods-section) the plate should perform.  these method names must be defined in the [methods](#methods-section) section. 
                    - ```- method: <method-name>``` - Name of the method to execute.  
                    **Notice the '-' character before ```method```. Items under ```steps``` are a list and require the '-' character for each item.**
                    **Note: if no other options are included here, just the ```<method-name>``` can be written, excluding the ```method``` keyword.**
                    - ```spawn: <list-of-threads-to-spawn>``` - A list of threads to spawn once the labware reaches this step.


**Workflow Example**

```yml
workflows:
    smc-assay:
        threads:
            plate-1: 
                labware: plate-1
                type: start
                start: stacker-plate-1-start
                end: waste-1
                steps:
                    - method: sample-to-bead-plate 
                      spawn: 
                        - sample-plate
                        - tips-96
                    - incubate-2hrs
```
**Wrapper Thread Example**

```yml
workflows:
    ## omitted code
                plate-1:
                    steps:
                    ## omitted code
                    - post-capture-wash
                    - method: add-detection-antibody
                      spawn: 
                        - tips-96
                    ## omitted code
                tips-96:
                    type: wrapper
                    labware: tips-96
                    start: stacker-96-tips
                    end: waste-1
                    steps:
                        - delid
                        - ${0}


```

**Wrapper Thread Example Explanation**

The workflow for ```plate-1``` executes it's methods until it gets to method ```add-detection-antibody```.  Once ```plate-1``` arrives at the resource to perform ```add-detection-antibody```, it spawns the ```tips-96``` thread.

When ```tips-96``` thread executes, it does the following:

1. Retrieves the plate from ```stacker-96-tips```
2. Executes the the delid step
3. Performs a variable step denoted by ```${0}```.
    - In this case, the cariable step is replaced by the calling method ```add-detection-antibody```
4. After completing method ```add-detection-antibody```, ```tips-96``` is then placed in the position ```waste-1```
    - Alternatively, another step could be added after the variable step ```${0}``` and it would complete that step before ending at position ```waste-1```

**Benefit**

This approach eliminates duplicate code, allowing engineers to make changes in one place and apply them reliably throughout the process. No more searching through methods to update every instance.



## Scripting Section

**Explanation**

The scripting section maps scripts to an identifier to be used within your configuration file. 


**Identifier**

```scripting```

**Structure**

- ```scripting:```
    - ```base-dir: <base-dir>``` - _(Optional)_ Defines the base directory to find scripts.  This directory is relative to where Orca is called from.  This is an issue that needs to be fixed.
    - ```scripts: ``` - Defines a list of scripts
        - ```<script-name>``` - Define name for your script.  This will be used to identifier your script.
            - ```source: <script-filename>:<script-class-name>``` - Your script source needs to identify which file the script is in and the script's class name within that file.  Orca first searched the current working directory for the scripting file, if it's not found, it then searches the configuration file's directory.

**Scripting Map Example**

```yml
scripting:
    base-dir: examples/smc_assay
    scripts:
        spawn-384-tips-script:
            source: spawn_384_tips.py:Spawn384TipsScript
        spawn-final-plate-script:
            source: spawn_final_plate.py:SpawnFinalPlateScript
```

**Scripting Reference Example**

```yml
workflows:
    smc-assay:
        threads:
            # omitted code
            tips-384:
                type: wrapper
                labware: tips-384
                start: stacker-384-tips-start
                end: stacker-384-tips-end
                scripts: 
                    - spawn-384-tips-script
                steps:
                    - delid
                    - ${0}
```

**Scripting Reference Explanation**

_In this case, the purpose of ```spawn-384-tips-script``` is to allow a 96-head to use every quadrant of a 384 tip box before retrieving a new tip box. The script keeps count of how many times the thread it's attached to has been called.  If the script has been called a multiple of 4 times, it does nothing.  Otherwise, it changes the workflows start location to be it's end location._

Script ```spawn-384-tips-script``` maps to file ```<current-working-directory>/examples/smc_assay/spawn_384_tips.py:Spawn384TipsScript```.  Class ```Spawn384TipsScript``` is retrieved and initialized by Orca.  This script is then attached to thread ```tips-384```.  When ```tips-384``` thread is spawned an event is sent to the ```Spawn384TipsScript``` script object and the internal code is executed.

**More Information**

For more information regarding scripting read the [Scripting Development](#dev-scripting) section.

<h1 id="development">🔨 Development</h1>

## Scripting

Scripting is necessary in lab automation for situations involving fine control over the process.

- Orca scripts are written in python.  
- All scripts must implement from one of the abstract base classes listed in script types below.
- Multiple scripts can be included in a single file.
- Scripts are referenced within the workflow configuration as described in [Scripting Section](#scripting-section)

**Script Types**

Currently there is only one script type available.
- [ThreadScript](./src/scripting/scripting.py) - Attaches to a thread instance.  

**Type: [ThreadScript](./src/scripting/scripting.py)**

- Methods:
    - ```thread_notify(self, event:str, thread: LabwareThread) -> None``` - When a thread event occurs this method is called with a string describing the event and the LabwareThread object of the thread executing the event.

- Properties:
    - ```system(self) -> ISystem``` - The ISystem interface is passed to the script at initialization for access during the lifetime of the script.

## Drivers

***These drivers have now been moved to a new repo... more information to follow soon***

**Driver Types**
- **IInitializeableDriver(ABC)** - Base class for drivers that can only be initialized.
- **IDriver(IInitializeableDriver)** - Base class for drivers that can execute commands.
- **ILabwarePlaceableDriver(IDriver)** - Equipment that may have labware placed at the equipment.
- **ITransporterDriver(IDriver)** - Equipment capable of transporting labware items.


**Adding Drivers**

Drivers must be manually added to the [ResourceFactory](./src/yml_config_builder/resource_factory.py)

In the future, a ```driver``` command will be added to the command line to install and uninstall drivers.

## Plugins

🚧🚧🚧 Coming Soon...

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


