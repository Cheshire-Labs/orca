"""Write a deployment package folder whose files hand over to builder functions in `tests/`.

The daemon builds the folder it was given, so a daemon test writes one. Each
builder is named `module:function`, and the written file imports it under the
name the package loader looks for.
"""

from collections.abc import Mapping
from pathlib import Path

from orca.runtime.store_factory import default_package_root

FIXTURE = "tests.daemon.daemon_test_fixture_topology"
BROKEN = "tests.daemon.daemon_test_broken_topologies"

FIXTURE_TOPOLOGY = f"{FIXTURE}:build_topology"
SIMPLE_WORKFLOW = {"simple_workflow": f"{FIXTURE}:build_workflow"}
SLOW_WORKFLOW = {"slow_window_workflow": f"{FIXTURE}:build_workflow_slow"}
EVENT_WAIT_WORKFLOW = {"waits_for_an_event": f"{FIXTURE}:build_workflow_waiting_for_an_event"}


def _import_as(builder: str, name: str) -> str:
    module, function = builder.split(":", 1)
    return f"from {module} import {function} as {name}\n"


def write_package(
    root: Path | None = None,
    *,
    topology: str = FIXTURE_TOPOLOGY,
    workflows: Mapping[str, str] = SIMPLE_WORKFLOW,
) -> Path:
    """Write `topology.py` and `workflows/<name>.py` into `root` (default: the daemon's folder)."""
    folder = root if root is not None else default_package_root()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "topology.py").write_text(_import_as(topology, "build_topology"), encoding="utf-8")
    if workflows:
        workflows_dir = folder / "workflows"
        workflows_dir.mkdir(exist_ok=True)
        for file_name, builder in workflows.items():
            (workflows_dir / f"{file_name}.py").write_text(
                _import_as(builder, "build_workflow"), encoding="utf-8",
            )
    return folder
