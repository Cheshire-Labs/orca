# Orca: Lab Automation Scheduler

Orca is a laboratory automation scheduler for workflows that run in parallel. It coordinates devices (liquid handlers, shakers, centrifuges, sealers, plate readers) and moves labware between them. Workflows are plain Python, so they live in your repo and diff like any other source file.

## Features

- **Parallel labware threads** - many pieces of labware move through the system at the same time.
- **Reservation-based scheduling** - devices and positions are reserved before use.
- **Resource pools** - an action targets a pool and the runtime picks a free device.
- **Event bus** - subscribe to status changes for custom integrations.
- **Standalone methods** - run a whole workflow, or run a single method on its own.
- **Four decorator levels** - `@orca.action`, `@orca.method`, `@orca.thread`, `@orca.workflow`.

## Installation

Orca needs Python 3.10 or newer. Install it from GitHub into a new virtual environment:

```bash
git clone https://github.com/Cheshire-Labs/orca.git
cd orca
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux, macOS
pip install -e ".[dev]"
```

pip also installs Orca's driver layer, [cheshire-drivers](https://github.com/Cheshire-Labs/cheshire-drivers), from GitHub at the release this version of Orca pins. cheshire-drivers installs a fork of PyLabRobot under the name `pylabrobot`, which replaces any upstream PyLabRobot already in the environment. That is why the environment should be a new one. The cheshire-drivers README explains the fork.

The `cheshire-orca` package on PyPI is the old 0.x release, not this one. The `[dev]` extra adds the test tooling (pytest, black). To run the tests, also clone [orca-client](https://github.com/Cheshire-Labs/orca-client) beside `orca`; see [CONTRIBUTING](./CONTRIBUTING).

## Example

An **action** is one step at one device. A **method** is a sequence of actions. A **labware thread** is one piece of labware moving through the system. A **workflow** composes threads. This is abridged from [examples/hamilton_smc/workflows/hamilton_smc_assay.py](./examples/hamilton_smc/workflows/hamilton_smc_assay.py). `plate_1` is a `PlateTemplate` declared in that file, and `shaker_collection` is a device pool from the topology:

```python
@orca.action(device=shaker_collection, inputs=[plate_1])
async def shake_2hrs(ctx: ActionContext):
    await ctx.device().shake(duration=7200, speed=875)

@orca.method
async def incubate_2hrs(ctx: MethodContext):
    yield shake_2hrs

# The stacker dispenses the plate. A bare "stacker_3" would mean an operator places it.
@orca.thread(labware=plate_1, start=("stacker_3", DISPENSE), end="waste_1")
async def plate_1_journey(ctx: ThreadContext):
    yield incubate_2hrs

@orca.workflow(name="hamilton_smc_assay")
def workflow(wf: WorkflowContext):
    wf.start(plate_1_journey)
```

Run the full assay in simulation with `python -m examples.hamilton_smc.hamilton_smc_example`.

## Command line

The `orca` command drives a local daemon over REST. Run these from the repo root. The daemon imports the topology and workflow modules you name, from the directory `orca start` ran in:

```bash
orca start
orca topology mount examples.hamilton_smc.topology:build_topology --sim
orca workflow load examples.hamilton_smc.workflows.hamilton_smc_assay:build_workflow
orca run hamilton_smc_assay --run-mode PURE_SIM --wait
orca shutdown
```

Every `orca run` needs `--run-mode`. `PURE_SIM` runs every device in simulation. `orca --help` lists the other commands.

## Documentation

**Full documentation is at https://cheshirelabs.io/docs/orca/intro**, including the quickstart, the SDK reference and the supported devices.

## Examples

Each runs to completion in simulation, with no hardware. Run them from the repo root:

| Example | Command |
|---|---|
| [Hamilton SMC assay](./examples/hamilton_smc/hamilton_smc_example.py): immunoassay with inline PyLabRobot pipetting on an ML STAR pair | `python -m examples.hamilton_smc.hamilton_smc_example` |
| [Opentrons Flex SMC assay](./examples/opentrons_flex_smc/opentrons_flex_smc_example.py): the same assay on an Opentrons Flex | `python -m examples.opentrons_flex_smc.opentrons_flex_smc_example` |
| [SMC assay](./examples/smc_assay/run_pure_sim.py): the same assay driven by protocol files | `python -m examples.smc_assay.run_pure_sim` |
| [PyLabRobot walkthrough](./examples/pylabrobot_example/pylabrobot_example.py): most SDK features in one workflow, with a cherry pick from a CSV worklist and a serial dilution | `python -m examples.pylabrobot_example.pylabrobot_example` |
| [Multi-lineage](./examples/multi_lineage/multi_lineage_example.py): several sample plates feeding one shared reservoir, with the group count set at submission | `python -m examples.multi_lineage.multi_lineage_example` |
| [Hamilton VENUS](./examples/simple_venus_example/simple_venus_example.py): runs VENUS methods, with a person moving the plates. Each move waits for you to press Enter; `--live` drives a real Hamilton | `python -m examples.simple_venus_example.simple_venus_example` |
| [Volume tracking](./examples/volume_tracking_example.py): how dispensed volume is recorded per well, with no workflow | `python -m examples.volume_tracking_example` |

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

## Security

**This release is meant for internal use only**: a lab machine or an internal
network you control, operated by people you trust.

The daemon has no authentication. `orca start` binds 127.0.0.1, and every route
on it is open to every process on that computer. Workflows and methods are Python
source that the daemon imports and runs. Run it on a machine only your operators
use, and do not expose the port through a tunnel, a proxy or a container port
map. [SECURITY.md](./SECURITY.md) has the detail and the address to report a
vulnerability to.

## Contributing

See [CONTRIBUTING](./CONTRIBUTING) for guidelines.

Contributors must sign the [Cheshire Labs Contributor Agreement](https://cla-assistant.io/Cheshire-Labs/orca), which assigns copyright in the contribution to Cheshire Labs.

## License

Source-available under the [Server Side Public License v1 (SSPL-1.0)](./LICENSE)
from 2.0.0 onward. Releases up to and including 1.0.0 were AGPL-3.0.
[NOTICE](./NOTICE) names the copyright holder.

## Contact

[Cheshire Labs Contact](https://cheshirelabs.io/contact/)
