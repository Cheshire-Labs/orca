# Orca: Lab Automation Scheduler

Welcome to Orca!  

Orca is a laboratory automation scheduler designed from the ground up with development, testing, and integration in mind.

# Table of Contents

- [How it works](#how-it-works)
- [Features](#features)
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
    - [Config Section](#config-section)
    - [Methods Section](#methods-setion)
    - [Workflows Section](#workflows-section)
    - [scripting Section](#scripting-section)
- [Contributing](#contributing)
- [License](#license)
- [Need More?](#need-more)

# How It Works

Orca simplifies the orchestration of complex lab automation workflows through a YAML configuration file. In this file, users define labware, resources, system options, methods, and workflows. The configuration file supports the integration of custom scripts, allowing users to tailor their workflows to specific needs. Variables in the format ${module:object.property} can be used to deploy different configurations depending on the context of the deployment.

Orca is currently operated via the command line to allow external systems to drive the scheduler. Users can choose to run entire workflows or individual methods as needed, offering flexibility to adapt to changing lab environments.

# Features
- **Git Friendly**
    
    You own your workflow, and it integrates seamlessly into your local git repo like any other code

- **Diff Friendly**

    Easily track changes with clear, diff-able workflows, making it simple to see what has changed since the last run.



- **Configurable Environments**

    Deploy an environment to load a collection of variables across your entire workflow.  This helps to set things like shake times to 0 during testing and resetting them for production.

- **Clear workflows**
    
    Name and list your methods as they appear in your protocol, and reorder them with a simple copy and paste.

- **Quickly Change Labware Start and End Locations**
    
    Avoid reloading your plate store. Change the start point to a nearby plate pad and relaunch quickly.

- **Easily Swap Methods**

    Adapt to changing requirements by quickly swapping one method for another.

- **Run an Entire Workflow**

    Execute the workflow from start to finish in a prod, dev, or any custom environment

- **Run a Single Method** 

    No more copying and pasting parts of workflows. With Orca, you can run an entire workflow or test a single method to fix errors.

- **Python Scripting**

    No scheduler software fits every need. Orca offers powerful Python scripting to ensure your workflows perform as required.


# :computer: Installation

Instructions on how to install and set up the project.

# Usage 

### Basic Overview

1. [Define your configuration file](#define-your-configuration-file)
2. Launch Orca command shell
3. Run your entire workflow or run a single method

### Example
To run the [Example Configuration File](./examples/smc_assay/smc_assay_example.yml)
1. Using python launch the Orca command shell from your command line
```bash
python path/to/orca.py
```
2. Run the entire ```smc-assay``` workflow as defined under the [workflows](#workflows) section of the configuation file
```bash
run --workflow smc-assay --config examples/smc_assay/smc_assay_example.yml
```

3. OR instead of running an entire workflow, just run a single method.  To do this you must define where each plate used by the method will begin and end using JSON for a start-map and end-map
```bash
run-method --method add-detection-antibody --start-map '{"plate-1": "pad-1", "tips-96": "pad-2"}' --end-map '{"plate-1": "pad-4", "tips-96": "pad-5"}' --config examples/smc_assay/smc_assay_example.yml
```
---
---
# Command Line Commands


## Deploy a Workflow
**Explanation**

Runs an entire workflow as defined in your configuration file

**Command**

_run_

**Usage**

```bash
run --workflow <workflow_name> [--config <path_to_config_file>] [--stage <development_stage>]
```

**Options**

* _--workflow <workflow_name>_
    
    Specifies the name of the workflow to be run. This name must match a workflow name defined in the configuration file.

* _--config <path_to_config_file>_
    
    Specifies the path to the YAML configuration file. If not provided, the previously loaded configuration is used.

* _--stage <development_stage>_

    Specifies the development stage to be run (e.g., prod, dev). If not provided, dev is run.

**Example** 

```bash
run --workflow sample_workflow --config examples/smc_assay/smc_assay_example.yml
```

## Deploy a Single Method

**Explanation**

Runs a single method defined within the configuration file

**Command**

_run-method_

**Usage**

```bash
run-method --method <method_name> --start-map <start_map_json> --end-map <end_map_json> [--config <path_to_config_file>] [--stage <development_stage>]
```

**Options**

* _--method <method_name>_

    Specifies the name of the method to be run. This name must match a method name defined in the configuration file.

* _--start-map <start_map_json>_

    A JSON dictionary of labware mapped to it’s desired starting location. Labware names must match labware names defined in the method specified within the configuration file.

* _--end-map <end_map_json>_

    A JSON dictionary of labware mapped to it’s desired starting location. Labware names must match labware names defined in the method specified within the configuration file.

* _--config <path_to_config_file>_

    Specifies the path to the YAML configuration file. If not provided, the previously loaded configuration is used.

* _--stage <development_stage>_

    Specifies the development stage to be run (e.g., prod, dev). If not provided, dev is run.


**Example**
```bash
run-method --method sample_method --start-map '{"plate-1": "pad-1", "final-plate": "pad-2"}' --end-map '{"plate-1": "pad-4", "final-plate": "pad-5"}' --config examples/smc_assay/smc_assay_example.yml
```

## Load Configuration File Only

**Explanation**

Loads the configuration file.  Used to ensure configuration file is valid.

**Command**

_load_

**Usage**

```bash
load --config <path_to_config_file>
```

**Options**
* _--config <path_to_config_file>_
    
    Specifies the path to the YAML configuration file.

**Example**

```bash
load --config examples/smc_assay/smc_assay_example.yml
```

## Initialize System Only

**Explanation**

Initializes the resources defined in the configuration file

**Command**

_init_

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
init --config examples/smc_assay/smc_assay_example.yml --resources [resource1, resource2]
```

## Exit


**Explanation**

Exit or quit the Orca shell

**Command**

_exit_ OR _quit_

**Usage**

```bash
exit
```

```bash
quit
```

**Options**

None

**Example**

```bash
exit
```

```bash
quit
```
---
---

# :clipboard: Define Your Configuration File
## Configuration File Overview

The Orca configuration file is a YAML file used to define various elements of your lab automation system.  It is the backbone of Orca, used to define everything in your system.

### Sections
- ```system``` Defines the overall system information, including name, version, and description
- ```labwares``` Specifies defintions of the labware types used in the workflow
- ```resources``` List of resources and resource pools, including initialization information for each resource
- ```config``` Defines deployable environments and associates a collection of variable values for that deployment
- ```methods``` Specifies inidividual methods and the actions to be performed with those methods
- ```workflows``` Defines workflows as a sequence of methods to be executed
- ```scriptiong``` Maps custom scripts to be used within the workflows

### Example
[Example SMC Assay Yml Configuration File](./examples/smc_assay/smc_assay_example.yml)

## System Section


**Explanation**

Defines the overall system configuration

**Identifier**

_system_

**Properties**

- ```name``` - The name of the system
- ```version``` - The version of the configuration
- ```description``` - A breif description of the system

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

_labwares_

**Properties**

- ```<name>``` - Choose a name for your labware.  This will be the identifier of that labware throughtout the configurations file.


**Options**
- ```type``` - A string specifiying the labware type.  Any equipment, such as a robot, that handles labware must have a matching labware definition in its programming.  
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


## Config Section
## Methods Section
## Workflows Section
## Scripting Section

## Contributing

Thank you for your interest in contributing!

Please read over the [contributing documentation](./CONTRIBUTING).

Please Note: Cheshire Labs follows an open core business model, offering Orca under a dual license structure. To align with this model and the AGPL license, contributors will need to submit a contributor license agreement.

## License

This project is released to under [AGPLv3 license](./LICENSE).  

Plugins and drivers are considered derivatives of this project.

To obtain a a different license contact Cheshire Labs.

## Need More?

Please contact Cheshire Labs if you're looking for:
- More Features
- A Graphical Interface
- Driver Development
- Hosted Cloud Environment
- Help Setting Up Your System 
- Custom Scripting


