# Examples

Each folder here is a deployment package: a `topology.py`, one `workflows/<name>.py` per workflow, and the setup files (teachpoints, deck layouts, access configs). The daemon builds a package folder and runs its workflows. None of the examples need hardware: `CHECK_RUN` runs every device in simulation.

| Folder | Workflow |
|---|---|
| `hamilton_smc` | `hamilton_smc_assay` |
| `opentrons_flex_smc` | `opentrons_flex_smc_assay` |
| `smc_assay` | `smc_assay` |
| `pylabrobot_example` | `learn_orca` |
| `multi_lineage` | `multi_lineage_example` |
| `simple_venus_example` | `example_workflow` |

A run writes its state to `<folder>/.orca/`, which git ignores. A second run starts with the plates the first run left. To start clean, stop the daemon and delete `.orca/`, or copy the folder somewhere else first.

## By CLI

From the repo root:

```bash
ORCA_PACKAGE_DIR=examples/hamilton_smc orca start
orca topology mount
orca run hamilton_smc_assay --run-mode CHECK_RUN --wait
orca shutdown
```

`multi_lineage` takes its sample plates as groups at submission:

```bash
orca submission submit multi_lineage_example --run-mode CHECK_RUN --groups-json \
  '[{"id": "g1", "members": [{"thread_template_name": "sample", "acquisition": {"kind": "pool"}}]},
    {"id": "g2", "members": [{"thread_template_name": "sample", "acquisition": {"kind": "pool"}}]}]'
```

## From Python

Start the daemon on a fixed port, then drive it with the Python client:

```bash
ORCA_PACKAGE_DIR=examples/hamilton_smc orca start --port 8765
```

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

`orca shutdown` stops the daemon when you are done.
