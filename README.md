# Orca: Lab Automation Scheduler

Orca is a laboratory automation scheduler designed for parallel processing of laboratory workflows. It coordinates devices (liquid handlers, centrifuges, sealers, etc.) and manages labware movement across your lab system.

**Live System Usage**: Connecting Orca to a driver running a live instrument is done at your own risk. Please exercise caution to protect your personnel and equipment.

**Stopping Orca**: To stop Orca, terminate the program (Ctrl+C).

## Features

- **Git & Diff Friendly** - Workflows are Python code that integrates into your repo
- **Event Bus** - Subscribe to events for custom integrations
- **Parallel Processing** - Multiple labware threads run concurrently
- **Modular Design** - Swap methods, run workflows or single methods
- **Resource Pools** - Dynamic resource selection at runtime
- **Python Scripting** - Custom logic when needed

## Quick Start

```bash
git clone https://github.com/Cheshire-Labs/orca.git
cd orca
pip install -e .

# Run the SMC Assay demo
python ./examples/smc_assay/smc_assay_example.py
```

## Installation

**From GitHub (recommended)**:
```bash
git clone https://github.com/Cheshire-Labs/orca.git
cd orca
pip install -e .
```

**From PyPI**:
```bash
pip install cheshire-orca
```

## Basic Example

```python
import asyncio
from orca.sdk.labware import PlateTemplate
from orca.sdk.devices import Shaker, Transporter
from orca.sdk.actions import Shake
from orca.sdk.workflow import MethodTemplate, ThreadTemplate, WorkflowTemplate
from orca.sdk.system import SdkToSystemBuilder, WorkflowExecutor, ResourceRegistry, SystemMap
from orca.sdk.events import EventBus
from cheshire_drivers import SimShakerDriver, SimTransporterDriver

# Define labware
plate = PlateTemplate("sample_plate", lambda name: None)

# Define devices
shaker = Shaker("shaker", SimShakerDriver("shaker"), sim=True)
arm = Transporter("arm", SimTransporterDriver("arm"), "teachpoints/arm.json")

# Register resources
registry = ResourceRegistry()
registry.add_resources([shaker, arm])

# Create system map
system_map = SystemMap(registry)
system_map.assign_resource({"shaker": shaker})

# Define action and method
shake_action = Shake(resource=shaker, duration=60, speed=500,
                     inputs=[plate], outputs=[plate])
shake_method = MethodTemplate("shake_method", actions=[shake_action])

# Define thread and workflow
plate_thread = ThreadTemplate(
    labware_template=plate,
    start=system_map.get_location("start_pad"),
    end=system_map.get_location("end_pad"),
    methods=[shake_method]
)

workflow = WorkflowTemplate(name="simple_workflow")
workflow.add_thread(plate_thread, is_start=True)

# Build and run
event_bus = EventBus()
builder = SdkToSystemBuilder(
    name="Example System",
    description="Simple example",
    labwares=[plate],
    resources=registry,
    system_map=system_map,
    methods=[shake_method],
    workflows=[workflow],
    event_bus=event_bus
)
system = builder.get_system()

async def run():
    await system.initialize_all()
    executor = WorkflowExecutor(workflow, system)
    await executor.start()

asyncio.run(run())
```

## Supported Devices

| Device | Description | Import |
|--------|-------------|--------|
| Venus | Hamilton MLSTAR, Vantage liquid handlers | `Venus` |
| A4SSealer | Azenta A4S plate sealer | `A4SSealer` |
| Sealer | Generic sealer (PLR backends) | `Sealer` |
| Shaker | Generic shaker (PLR backends) | `Shaker` |
| Centrifuge | Generic centrifuge (PLR backends) | `Centrifuge` |
| HumanTransfer | Manual plate movement with prompts | `HumanTransfer` |
| Transporter | Robotic arm (PLR backends) | `Transporter` |

All devices are imported from `orca.sdk.devices`.

## Documentation

Full documentation available at: https://cheshirelabs.io/docs/orca-oss/intro

## Examples

- [SMC Assay](./examples/smc_assay/smc_assay_example.py) - Full workflow with simulated devices
- [Simple Venus Method](./examples/simple_venus_example/simple_venus_example.py) - Venus driver integration
- [PyLabRobot Example](./examples/pylabrobot_example/pylabrobot_example.py) - PLR driver integration

## Acknowledgements

Orca builds on the work of the [PyLabRobot](https://github.com/PyLabRobot/pylabrobot) community. If you use Orca in research, please credit PyLabRobot:

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

## Contributing

See [CONTRIBUTING](./CONTRIBUTING) for guidelines.

Cheshire Labs follows an open core business model with dual licensing. Contributors must submit a contributor license agreement.

## License

This project is released under [AGPLv3 license](./LICENSE). Plugins, scripts, and drivers are considered derivatives.

For alternative licensing, [contact Cheshire Labs](https://cheshirelabs.io/contact/).

## Need More?

[Contact Cheshire Labs](https://cheshirelabs.io/contact/) for:
- Custom features
- Graphical interface
- Driver development
- Cloud hosting
- Setup assistance
- Custom scripting

## Contact

[Cheshire Labs Contact](https://cheshirelabs.io/contact/)
