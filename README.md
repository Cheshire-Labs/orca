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

The first Orca command you run after installing prints nothing for up to half a minute while Python compiles the bytecode for Orca, cheshire-drivers and PyLabRobot. It is not hung. Every run after that starts in a few seconds.

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

Orca runs as a service. You write the topology, workflows, methods and event handlers with the SDK, put them in a package folder, and run them on a running server. The commands below run this assay in simulation.

## Command line

The `orca` command drives a local daemon over REST. The daemon builds its package folder: `ORCA_PACKAGE_DIR`, or the folder `orca start` ran in. A package folder holds `topology.py`, one `workflows/<name>.py` per workflow, and the setup files (teachpoints, deck layouts, settings). The daemon keeps plates, runs, incidents, events, audit rows and the record of device operations, which volumes and tips come from, in `<package>/.orca/state.db`. Run these from the repo root:

```bash
# bash
ORCA_PACKAGE_DIR=examples/hamilton_smc orca start
```

```powershell
# PowerShell
$env:ORCA_PACKAGE_DIR = "examples/hamilton_smc"; orca start
```

Then, in either shell:

```bash
orca topology mount
orca run hamilton_smc_assay --run-mode CHECK_RUN --wait
orca shutdown
```

`orca topology mount` builds `topology.py` and every workflow in `workflows/`. To pick up an edited file, run `orca runtime reload`. Every `orca run` needs `--run-mode`. `CHECK_RUN` runs every device in simulation and needs nothing else running. `orca --help` lists the other commands.

`--run-mode SIM` and `--run-mode LIVE` send every device command through an [orca-client](https://github.com/Cheshire-Labs/orca-client), so one must be running and connected first. Without one the run is refused with `devices_not_ready`. Install orca-client into its own virtual environment, write a `config.json` whose `platform.url` is the daemon's address (`ws://127.0.0.1:8765/ws/devices` after `orca start --port 8765`), and start it in a second shell:

```bash
python -m orca_client --config config.json
```

The orca-client README shows the config file.

## Python client

A lab script talks to a running server with `OrcaClient`, the same client the `orca` command uses. `DaemonLoadingClient` adds mounting, which only the local daemon has. Start the daemon on a fixed port (`orca start --port 8765`) so the script knows where it is:

```python
import time

from orca.sdk.client import DaemonLoadingClient

client = DaemonLoadingClient("http://127.0.0.1:8765")
client.mount_package()
run = client.submit_workflow("hamilton_smc_assay", run_mode="CHECK_RUN")
while (detail := client.get_execution(run.id)).status not in ("completed", "partial_completed", "failed", "aborted"):
    time.sleep(1)
print(detail.status, detail.error)
```

For a hosted server, build `OrcaClient(url, api_key)` from `orca.sdk.client`. Both serve the same routes under `/api`. A failed request raises `ControlPlaneError` with the server's error code and message.

## Documentation

**Full documentation is at https://cheshirelabs.io/docs/orca/intro**, including the quickstart, the SDK reference and the supported devices.

## Examples

None of these need hardware. Each example folder is a package the daemon builds, and CI runs every one to completion on the daemon. [examples/README.md](./examples/README.md) shows how to run one, by CLI and from Python.

| Example | Folder | Workflow |
|---|---|---|
| Hamilton SMC assay: immunoassay with inline PyLabRobot pipetting on an ML STAR pair | [examples/hamilton_smc](./examples/hamilton_smc) | `hamilton_smc_assay` |
| Opentrons Flex SMC assay: the same assay on an Opentrons Flex | [examples/opentrons_flex_smc](./examples/opentrons_flex_smc) | `opentrons_flex_smc_assay` |
| SMC assay: the same assay driven by protocol files | [examples/smc_assay](./examples/smc_assay) | `smc_assay` |
| PyLabRobot walkthrough: most SDK features in one workflow, with a cherry pick from a CSV worklist and a serial dilution | [examples/pylabrobot_example](./examples/pylabrobot_example) | `learn_orca` |
| Multi-lineage: several sample plates feeding one shared reservoir, with the group count set at submission | [examples/multi_lineage](./examples/multi_lineage) | `multi_lineage_example` |
| Hamilton VENUS: runs VENUS methods, with a person moving the plates | [examples/simple_venus_example](./examples/simple_venus_example) | `example_workflow` |

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

## Dedication

Orca is dedicated to the memory of my mom, Joanne Hull Salmi. See [DEDICATION.md](./DEDICATION.md).

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
