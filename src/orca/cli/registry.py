"""`orca workflow|method|thread|location` sub-apps.

Inventory: ``list`` verbs are reads against either backend.

Verbs:
    orca workflow load         -- register a workflow (local daemon: factory
                                  spec; cloud: source file).
    orca method execute        -- POST /api/method-executions (both backends)
"""

import json
from pathlib import Path
from typing import NamedTuple

import typer

from orca.cli import output
from orca.cli.backend import (
    active_backend, cloud_backend_installed, cloud_client, cloud_help, get_client, local_client,
)


class MethodRow(NamedTuple):
    """Typed row for the `orca method list` table.

    `NamedTuple` so `emit_table` can still unpack via `*row`, while the
    field names document the columns and `mypy`/`pyright` catch
    header/value drift at construction time.
    """
    workflow_name: str
    name: str
    failure_policy: str


class ThreadRow(NamedTuple):
    """Typed row for `orca thread list`."""
    workflow_name: str
    name: str
    labware: str
    start: str
    end: str


app = typer.Typer(help="Registry inspection (shared entry point).", hidden=True)
workflow_app = typer.Typer(help="Workflow-template inventory.", no_args_is_help=True)
method_app = typer.Typer(help="Method-template inventory.", no_args_is_help=True)
thread_app = typer.Typer(help="Thread-template inventory.", no_args_is_help=True)
location_app = typer.Typer(help="Location inventory.", no_args_is_help=True)


# ---------------- workflow ----------------


@workflow_app.command("list")
def workflow_list() -> None:
    """List all workflow templates. Works against both backends."""
    if active_backend() == "cloud":
        items = cloud_client().list_workflows()
    else:
        items = local_client().list_workflows()
    if output.get_mode().value == "json":
        output.emit_json([w.model_dump(mode="json") for w in items])
        return
    output.emit_table("Workflows", ["name"], [[w.name] for w in items])


@workflow_app.command("get")
def workflow_get(
    name: str = typer.Argument(..., help="Workflow name."),
) -> None:
    """Fetch a single workflow's summary. Works against both backends.

    Registry snapshot only, not the workflow source.
    """
    client = get_client()
    payload = client.workflow_get(name)
    if output.get_mode().value == "json":
        output.emit_json(payload.model_dump(mode="json"))
        return
    dumped = payload.model_dump(mode="json")
    output.emit_kv(
        f"workflow {dumped.get('name', name)}",
        [(k, str(v)) for k, v in dumped.items() if k != "name"],
    )


@workflow_app.command("load")
def workflow_load(
    target: str = typer.Argument(
        ...,
        help="A 'module:build_workflow' factory spec, which the daemon "
             "imports from the directory `orca start` ran in."
             + cloud_help(" On the cloud backend, a workflow source file, submitted to the deployment."),
    ),
    name: str = typer.Option(
        "", "--name",
        help="Cloud only: workflow name (must match @orca.workflow(name=...)).",
        hidden=not cloud_backend_installed(),
    ),
    message: str = typer.Option(
        "", "--message", "-m",
        help="Cloud only: commit message for the submitted workflow source.",
        hidden=not cloud_backend_installed(),
    ),
) -> None:
    """Load a workflow: validate + register on the runtime. Does NOT run it.

    The daemon registers a `module:build_workflow` spec against the mounted
    topology.

    Refused while any execution is active (paused included): re-importing
    would swap definitions out from under live threads. To add a single
    method or action to a running workflow instead, use
    `orca thread insert-method` / `orca thread insert-action`.
    """
    from orca.cli.backend import active_backend
    if active_backend() == "local":
        from orca.cli.client import LocalDaemonClient
        client = LocalDaemonClient()
        registered = client.load_workflow(target)
        output.info(f"loaded workflow {registered!r}")
        return

    source = Path(target)
    if not source.is_file():
        output.fail(
            f"cloud workflow load expects a source file; not found: {target}",
            code=output.EXIT_USAGE,
        )
    if not name:
        output.fail(
            "cloud workflow load requires --name (must match @orca.workflow(name=...))",
            code=output.EXIT_USAGE,
        )
    if not message:
        output.fail(
            "cloud workflow load requires --message/-m (commit message)",
            code=output.EXIT_USAGE,
        )
    cloud = cloud_client()
    payload = cloud.workflow_load(
        name=name, source=source.read_text(encoding="utf-8"), message=message,
    )
    if output.get_mode().value == "json":
        output.emit_json(payload.model_dump(mode="json"))
        return
    typer.echo(
        f"loaded workflow {payload.name} as "
        f"{(payload.commit_sha or '')[:8]}"
    )


# ---------------- method ----------------


@method_app.command("list")
def method_list() -> None:
    """List all method templates. Works against both backends.

    Both backends return the Protocol's ``MethodSummaryDTO`` with
    ``failure_policy`` as its name string.
    """
    items = get_client().methods_list()
    if output.get_mode().value == "json":
        output.emit_json([m.model_dump(mode="json") for m in items])
        return
    output.emit_table(
        "Methods",
        list(MethodRow._fields),
        [
            MethodRow(
                workflow_name=m.workflow_name,
                name=m.name,
                failure_policy=m.failure_policy,
            )
            for m in items
        ],
    )


@method_app.command("get")
def method_get(
    name: str = typer.Argument(..., help="Method name."),
    workflow: str | None = typer.Option(
        None, "--workflow",
        help="Owning workflow. Required when the method name exists in more "
             "than one workflow; method names are unique per workflow.",
    ),
) -> None:
    """Fetch a single method's summary. Works against both backends.

    Registry snapshot only, not the method source.
    """
    client = get_client()
    payload = client.method_get(name, workflow_name=workflow)
    if output.get_mode().value == "json":
        output.emit_json(payload.model_dump(mode="json"))
        return
    dumped = payload.model_dump(mode="json")
    output.emit_kv(
        f"method {dumped.get('name', name)}",
        [(k, str(v)) for k, v in dumped.items() if k != "name"],
    )


@method_app.command("execute")
def method_execute(
    workflow_name: str = typer.Argument(..., help="Workflow that owns the method."),
    method_name: str = typer.Argument(..., help="Method name within the workflow's bundle."),
    labware_start: str = typer.Option(
        ..., "--labware-start",
        help="JSON dict of labware-name -> start-location-name.",
    ),
    labware_end: str = typer.Option(
        ..., "--labware-end",
        help="JSON dict of labware-name -> end-location-name.",
    ),
    variables: str | None = typer.Option(
        None, "--vars", help="JSON dict of variable values (optional).",
    ),
    run_mode: str = typer.Option(
        "", "--run-mode",
        help="PURE_SIM | DEVICE_SIM | LIVE. REQUIRED per submission.",
    ),
    confirm: bool = typer.Option(
        False, "--confirm",
        help="Acknowledge a LIVE submission against devices whose topology "
             "declares a sim-direction sim_override.",
    ),
) -> None:
    """Execute a method standalone. Works against both backends."""
    from orca.cli.backend import get_client
    from orca.daemon.schemas import RunModeStr, is_run_mode_str
    if not run_mode or not is_run_mode_str(run_mode):
        output.fail(
            "--run-mode is required; pass one of "
            "PURE_SIM, DEVICE_SIM, or LIVE.",
            code=output.EXIT_USAGE,
        )
        return  # unreachable
    run_mode_value: RunModeStr = run_mode
    try:
        start_map = json.loads(labware_start)
        end_map = json.loads(labware_end)
        var_map = json.loads(variables) if variables else None
    except json.JSONDecodeError as e:
        output.fail(f"invalid JSON: {e}", code=output.EXIT_USAGE)
    if not isinstance(start_map, dict) or not isinstance(end_map, dict):
        output.fail(
            "--labware-start and --labware-end must be JSON objects",
            code=output.EXIT_USAGE,
        )
    client = get_client()
    record = client.submit_method(
        workflow_name=workflow_name,
        method_name=method_name,
        labware_start=start_map,
        labware_end=end_map,
        variables=var_map,
        run_mode=run_mode_value,
        acknowledge_warnings=confirm,
    )
    if output.get_mode().value == "json":
        output.emit_json(record.model_dump(mode="json"))
        return
    typer.echo(f"started {workflow_name}.{method_name} as {record.id[:8]}")


# ---------------- thread ----------------


@thread_app.command("list")
def thread_list() -> None:
    """List all thread templates (authored via @orca.thread)."""
    client = get_client()
    items = client.list_thread_templates()
    if output.get_mode().value == "json":
        output.emit_json([t.model_dump(mode="json") for t in items])
        return
    output.emit_table(
        "Thread templates",
        list(ThreadRow._fields),
        [
            ThreadRow(
                workflow_name=t.workflow_name,
                name=t.name,
                labware=t.labware_template_name,
                start=t.start_position_id,
                end=", ".join(t.end_position_ids),
            )
            for t in items
        ],
    )


# ---------------- location ----------------


@location_app.command("list")
def location_list() -> None:
    """List all locations (devices + pads mounted on the system map).

    The `deck_sites` column lists a device's addressable deck-site child
    locations (e.g. "mlstar_1/carrier-9-0"), which a thread `start=`/`end=`
    or an action `deck_positions` can target. Deck sites are not routing
    nodes, so they appear only here, hanging off their device; their full
    geometry lives in the device's deck layout (`orca deck-layouts get`).
    """
    client = get_client()
    items = client.list_locations()
    if output.get_mode().value == "json":
        output.emit_json([loc.model_dump(mode="json") for loc in items])
        return
    output.emit_table(
        "Locations",
        ["name", "resource", "loaded_labware", "deck_sites"],
        [
            [
                loc.name,
                loc.resource_name or "",
                ",".join(loc.loaded_labware_ids) or "",
                ",".join(loc.deck_sites) or "",
            ]
            for loc in items
        ],
    )
